"""Единый разбор сгенерированной колоды для детерминированных проверок.

Колода читается один раз через `core/deck_reader.py` (тот же читатель фигур, что у HTML-экспорта,
и тот же резолв наследования шрифтов/цветов `core/package.py`, что в парсинге). Каждая фигура — ShapeRec
с абсолютной геометрией, текстом по абзацам/run'ам, заливками, деталями картинки/диаграммы/таблицы.
Проверки работают только с этими записями и не трогают XML.

Два режима:
- с DeckIR — известно, какой слайд из какого образца, какие слоты заполнены нами (`is_ours`),
  какие фигуры фиксированные (`is_fixed`); проверки «шаблонности» (T02, T03, T05) точнее;
- без DeckIR (чужая колода) — «наши» = все не фиксированные контентные фигуры.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from functools import cached_property
from pathlib import Path

from deckforge.core.deck_reader import (  # noqa: F401 — реэкспорт для проверок и тестов
    DEFAULT_INSETS,
    SYMBOL_FONTS,
    CellRec,
    ChartRec,
    ParaRec,
    PictureRec,
    RunRec,
    ShapeRec,
    TableRec,
    read_shape,
)
from deckforge.core.ir import Archetype, DeckIR, Exemplar, SlideIR, Slot, TemplateDNA
from deckforge.core.ooxml import NS, absolute_bbox, iter_shapes, shape_id, shape_text
from deckforge.core.package import Package, PartCtx

log = logging.getLogger(__name__)

STEP = 914400 // 20  # 1/20" — допуск совпадения позиций
# архетипы, для которых пустой/разреженный слайд — норма
SPARSE_ARCHETYPES = {Archetype.TITLE, Archetype.SECTION, Archetype.CLOSING, Archetype.QUOTE, Archetype.IMAGE_FULL}


@dataclass
class SlideCtx:
    idx: int
    part: str
    ctx: PartCtx
    shapes: list[ShapeRec]
    layout_name: str
    bg_color: str | None
    bg_is_picture: bool
    exemplar: Exemplar | None = None
    ir: SlideIR | None = None

    @property
    def archetype(self) -> Archetype | None:
        return self.ir.archetype if self.ir else None

    def content(self) -> list[ShapeRec]:
        return [s for s in self.shapes if s.is_content]

    def text_shapes(self) -> list[ShapeRec]:
        return [s for s in self.shapes if s.has_text and not s.is_fixed]

    def ours(self) -> list[ShapeRec]:
        return [s for s in self.shapes if s.is_ours]

    def slot_of(self, sid: str) -> Slot | None:
        if self.exemplar is None:
            return None
        return next((s for s in self.exemplar.slots if s.id == sid), None)

    def by_id(self, sid: str) -> ShapeRec | None:
        return next((s for s in self.shapes if s.id == sid), None)

    @property
    def all_text(self) -> str:
        return "\n".join(s.text for s in self.shapes if s.has_text)


# ──────────────────────────── контекст колоды ────────────────────────────


class AuditContext:
    def __init__(self, pptx: str | Path, dna: TemplateDNA, ir: DeckIR | None = None) -> None:
        self.pptx = Path(pptx)
        self.dna = dna
        self.ir = ir
        self.pkg = Package(self.pptx)
        self.slide_w, self.slide_h = self.pkg.slide_size
        self.exemplars = {e.id: e for e in dna.exemplars}
        self.slides: list[SlideCtx] = []
        ir_slides = list(ir.slides) if ir else []
        if ir and len(ir_slides) != len(self.pkg.slides):
            log.warning("DeckIR: %d слайдов, в файле %d — сопоставление по индексу", len(ir_slides), len(self.pkg.slides))
        for i, part in enumerate(self.pkg.slides):
            ctx = PartCtx.for_slide(self.pkg, part)
            if ctx is None:
                continue
            slide_ir = ir_slides[i] if i < len(ir_slides) else None
            exemplar = self.exemplars.get(slide_ir.exemplar_id) if slide_ir else None
            self.slides.append(self._build_slide(i, part, ctx, exemplar, slide_ir))

    # ── шаблон ──

    @cached_property
    def template_pkg(self) -> Package | None:
        p = Path(self.dna.source_path)
        if not p.exists():
            log.warning("шаблон %s не найден — проверки T04/T05 неполные", p)
            return None
        return Package(p)

    @cached_property
    def template_layout_names(self) -> set[str]:
        tp = self.template_pkg
        if tp is None:
            return set()
        return {_layout_name(tp, lp) for lp in tp.layouts}

    def exemplar_shapes(self, exemplar: Exemplar) -> dict[str, tuple[int, int, int, int]]:
        """id → bbox фигур слайда-образца в шаблоне."""
        return {sid: bb for sid, (bb, _) in self._exemplar_shapes(exemplar.id).items()}

    def exemplar_texts(self, exemplar: Exemplar) -> dict[str, str]:
        """id → текст фигур слайда-образца в шаблоне."""
        return {sid: text for sid, (_, text) in self._exemplar_shapes(exemplar.id).items()}

    def _exemplar_shapes(self, exemplar_id: str) -> dict[str, tuple[tuple[int, int, int, int], str]]:
        cache = self.__dict__.setdefault("_exemplar_cache", {})
        if exemplar_id in cache:
            return cache[exemplar_id]
        exemplar = self.exemplars[exemplar_id]
        out: dict[str, tuple[tuple[int, int, int, int], str]] = {}
        tp = self.template_pkg
        if tp is not None and exemplar.source_index < len(tp.slides):
            root = tp.xml(tp.slides[exemplar.source_index])
            for sp in iter_shapes(root.find("p:cSld/p:spTree", NS)):
                bb = absolute_bbox(sp)
                if bb is not None:
                    out[shape_id(sp)] = (bb, shape_text(sp).strip())
        cache[exemplar_id] = out
        return out

    # ── справочники по DNA ──

    @cached_property
    def palette(self) -> set[str]:
        colors = {c.hex for c in self.dna.colors} | set(self.dna.theme_colors.values()) | {"FFFFFF", "000000"}
        return {c.upper() for c in colors if c}

    @cached_property
    def allowed_fonts(self) -> set[str]:
        fonts = set(self.dna.fonts) | set(self.dna.embedded_fonts)
        for t in self.dna.typography:
            fonts.add(t.font)
        tp = self.template_pkg
        if tp is not None and tp.masters:
            theme = tp.theme(tp.masters[0])
            fonts |= {theme.major_font, theme.minor_font}
        return {f.lower() for f in fonts if f}

    @cached_property
    def scale_sizes(self) -> list[float]:
        return sorted({t.size_pt for t in self.dna.typography})

    # ── разбор слайда ──

    def _build_slide(self, idx: int, part: str, ctx: PartCtx, exemplar: Exemplar | None, slide_ir: SlideIR | None) -> SlideCtx:
        bg, bg_pic = ctx.background()
        bg_color = bg[0][0] if bg else ctx.theme.colors.get(ctx.clr_map.get("bg1", "lt1"))
        layout_name = _layout_name(self.pkg, self.pkg.layout_of(part) or "")
        slot_ids = {s.id for s in exemplar.slots} if exemplar else set()
        fixed_ids = set(exemplar.fixed) if exemplar else set()
        filled = {e.slot_id for e in slide_ir.elements} if slide_ir else set()
        exemplar_ids = set(self.exemplar_shapes(exemplar)) if exemplar else set()

        shapes: list[ShapeRec] = []
        for z, sp in enumerate(iter_shapes(ctx.sp_tree)):
            rec = read_shape(ctx, sp, z)
            if rec is None:
                continue
            rec.is_slot = rec.id in slot_ids
            if exemplar is not None:
                rec.is_fixed = rec.id in fixed_ids
                rec.is_ours = rec.id in filled or (bool(exemplar_ids) and rec.id not in exemplar_ids)
                rec.is_decor = rec.is_picture and not rec.is_slot and not rec.is_ours
            else:
                rec.is_fixed = self._matches_fixed_element(rec)
                rec.is_ours = not rec.is_fixed
            shapes.append(rec)
        return SlideCtx(idx=idx, part=part, ctx=ctx, shapes=shapes, layout_name=layout_name, bg_color=bg_color,
                        bg_is_picture=bg_pic, exemplar=exemplar, ir=slide_ir)

    def _matches_fixed_element(self, rec: ShapeRec) -> bool:
        for fe in self.dna.fixed_elements:
            if fe.is_picture != (rec.tag == "pic"):
                continue
            if all(abs(a - b) <= STEP for a, b in ((fe.box.x, rec.box.x), (fe.box.y, rec.box.y),
                                                    (fe.box.w, rec.box.w), (fe.box.h, rec.box.h))):
                return True
        return False


# ──────────────────────────── вспомогательное ────────────────────────────


def _layout_name(pkg: Package, layout_part: str) -> str:
    if not layout_part or layout_part not in pkg.names:
        return ""
    csld = pkg.xml(layout_part).find("p:cSld", NS)
    return (csld.get("name") if csld is not None else "") or layout_part.rsplit("/", 1)[-1]


__all__ = ["AuditContext", "CellRec", "ChartRec", "ParaRec", "PictureRec", "RunRec", "ShapeRec", "SlideCtx",
           "SPARSE_ARCHETYPES", "STEP", "SYMBOL_FONTS", "TableRec"]
