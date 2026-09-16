"""Единый разбор сгенерированной колоды для детерминированных проверок.

Колода читается один раз (core/package.py — тот же резолв наследования шрифтов/цветов, что и в парсинге),
каждая фигура превращается в ShapeRec с абсолютной геометрией, текстом по абзацам/run'ам, заливками,
деталями картинки/диаграммы/таблицы. Проверки работают только с этими записями и не трогают XML.

Два режима:
- с DeckIR — известно, какой слайд из какого образца, какие слоты заполнены нами (`is_ours`),
  какие фигуры фиксированные (`is_fixed`); проверки «шаблонности» (T02, T03, T05) точнее;
- без DeckIR (чужая колода) — «наши» = все не фиксированные контентные фигуры.
"""

from __future__ import annotations

import io
import logging
from dataclasses import dataclass, field
from functools import cached_property
from pathlib import Path

from lxml import etree
from PIL import Image

from deckforge.core.ir import Archetype, Box, DeckIR, Exemplar, SlideIR, Slot, TemplateDNA
from deckforge.core.ooxml import (
    A,
    NS,
    P,
    R,
    absolute_bbox,
    graphic_kind,
    iter_shapes,
    localname,
    placeholder,
    shape_id,
    shape_name,
    shape_text,
)
from deckforge.core.package import Package, PartCtx, font_scale

log = logging.getLogger(__name__)

C_NS = "http://schemas.openxmlformats.org/drawingml/2006/chart"
C = f"{{{C_NS}}}"
DEFAULT_INSETS = (91440, 45720, 91440, 45720)  # lIns, tIns, rIns, bIns
STEP = 914400 // 20  # 1/20" — допуск совпадения позиций
SYMBOL_FONTS = ("Wingdings", "Webdings", "Symbol", "MT Extra")
# архетипы, для которых пустой/разреженный слайд — норма
SPARSE_ARCHETYPES = {Archetype.TITLE, Archetype.SECTION, Archetype.CLOSING, Archetype.QUOTE, Archetype.IMAGE_FULL}


# ──────────────────────────── записи ────────────────────────────


@dataclass
class RunRec:
    text: str
    font: str
    size_pt: float
    bold: bool
    color: str | None


@dataclass
class ParaRec:
    text: str
    runs: list[RunRec]
    bullet: bool = False
    level: int = 0
    space_before_pt: float = 0.0
    space_after_pt: float = 0.0
    line_spacing: float | None = None  # множитель, если задан явно

    @property
    def words(self) -> int:
        return len(self.text.split())


@dataclass
class ChartRec:
    kind: str  # barChart / lineChart / pieChart …
    n_series: int
    has_legend: bool
    val_axis_title: bool
    cat_axis_title: bool
    has_data_labels: bool
    series_colors: list[str] = field(default_factory=list)


@dataclass
class TableRec:
    rows: int
    cols: int
    fills: list[str] = field(default_factory=list)


@dataclass
class PictureRec:
    px_w: int
    px_h: int
    src_rect: tuple[int, int, int, int] = (0, 0, 0, 0)  # l, t, r, b в 1/1000 %

    @property
    def cropped_aspect(self) -> float | None:
        l, t, r, b = (v / 100000 for v in self.src_rect)
        w = self.px_w * (1 - l - r)
        h = self.px_h * (1 - t - b)
        return w / h if w > 0 and h > 0 else None


@dataclass
class ShapeRec:
    id: str
    name: str
    tag: str  # sp | pic | graphicFrame | cxnSp
    box: Box
    el: etree._Element
    z: int  # порядок в spTree (больше — выше)
    text: str = ""
    paragraphs: list[ParaRec] = field(default_factory=list)
    fill: list[str] = field(default_factory=list)
    line: list[str] = field(default_factory=list)
    insets: tuple[int, int, int, int] = DEFAULT_INSETS
    wrap: bool = True
    autofit: str | None = None  # normAutofit | spAutoFit | noAutofit | None
    picture: PictureRec | None = None
    chart: ChartRec | None = None
    table: TableRec | None = None
    is_placeholder: bool = False
    ph_type: str | None = None
    is_fixed: bool = False
    is_slot: bool = False
    is_ours: bool = False
    is_decor: bool = False  # картинка образца вне слотов (иконка, подложка) — дизайн шаблона, не контент

    @property
    def has_text(self) -> bool:
        return bool(self.text.strip())

    @property
    def is_picture(self) -> bool:
        return self.picture is not None

    @property
    def is_content(self) -> bool:
        """Содержательный блок: текст (не колонтитул), картинка, диаграмма, таблица — не декор и не фон."""
        if self.is_fixed or self.is_decor or self.ph_type in ("sldNum", "ftr", "dt"):
            return False
        return self.has_text or self.is_picture or self.chart is not None or self.table is not None

    @property
    def area(self) -> int:
        return max(0, self.box.w) * max(0, self.box.h)

    @property
    def runs(self) -> list[RunRec]:
        return [r for p in self.paragraphs for r in p.runs]

    @property
    def main_run(self) -> RunRec | None:
        return max(self.runs, key=lambda r: len(r.text), default=None)


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
            rec = self._shape(ctx, sp, z)
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

    def _shape(self, ctx: PartCtx, sp: etree._Element, z: int) -> ShapeRec | None:
        tag = localname(sp)
        bb = absolute_bbox(sp) or _inherited_bbox(ctx, sp)
        if bb is None:
            return None
        ph = placeholder(sp)
        rec = ShapeRec(id=shape_id(sp), name=shape_name(sp), tag=tag, box=Box(x=bb[0], y=bb[1], w=bb[2], h=bb[3]),
                       el=sp, z=z, is_placeholder=ph is not None, ph_type=ph[0] if ph else None)
        if tag in ("sp", "pic"):
            rec.fill = [c for c, _ in ctx.shape_fill(sp)]
            rec.line = [c for c, _ in ctx.shape_line(sp)]
        if tag == "sp":
            self._text(ctx, sp, rec)
            blip_fill = sp.find("p:spPr/a:blipFill", NS)
            if blip_fill is not None:
                rec.picture = self._picture(ctx, blip_fill)
        elif tag == "pic":
            blip_fill = sp.find("p:blipFill", NS)
            if blip_fill is not None:
                rec.picture = self._picture(ctx, blip_fill)
        elif tag == "graphicFrame":
            kind = graphic_kind(sp)
            if kind == "chart":
                rec.chart = self._chart(ctx, sp)
            elif kind == "table":
                rec.table = self._table(ctx, sp, rec)
        return rec

    # ── текст ──

    def _text(self, ctx: PartCtx, sp: etree._Element, rec: ShapeRec) -> None:
        tx = sp.find("p:txBody", NS)
        if tx is None:
            return
        body_pr = tx.find("a:bodyPr", NS)
        if body_pr is not None:
            rec.insets = tuple(int(body_pr.get(k, d)) for k, d in zip(("lIns", "tIns", "rIns", "bIns"), DEFAULT_INSETS))  # type: ignore[assignment]
            rec.wrap = body_pr.get("wrap", "square") != "none"
            for child in body_pr:
                if localname(child) in ("normAutofit", "spAutoFit", "noAutofit"):
                    rec.autofit = localname(child)
        rec.paragraphs = self._paragraphs(ctx, tx.findall("a:p", NS), sp)
        rec.text = "\n".join(p.text for p in rec.paragraphs)

    def _paragraphs(self, ctx: PartCtx, paras: list[etree._Element], sp: etree._Element | None) -> list[ParaRec]:
        scale = font_scale(sp)
        out: list[ParaRec] = []
        for para in paras:
            ppr = para.find("a:pPr", NS)
            lvl = int(ppr.get("lvl", "0")) if ppr is not None else 0
            runs: list[RunRec] = []
            for run in para:
                if localname(run) not in ("r", "fld"):
                    continue
                text = run.findtext("a:t", namespaces=NS) or ""
                if not text.strip():
                    continue
                chain = ctx.run_props_chain(run, para, sp, lvl)
                runs.append(RunRec(text=text, font=ctx.resolve_font(chain, placeholder(sp) if sp is not None else None),
                                   size_pt=round(ctx.resolve_size(chain) * scale, 1), bold=ctx.resolve_bold(chain),
                                   color=ctx.resolve_text_color(chain, sp)))
            text = "".join(r.text for r in runs)
            if not text.strip():
                continue
            rec = ParaRec(text=text, runs=runs, level=lvl, bullet=_has_bullet(ctx, ppr, sp, lvl))
            if ppr is not None:
                rec.space_before_pt = _spacing_pt(ppr.find("a:spcBef", NS), runs)
                rec.space_after_pt = _spacing_pt(ppr.find("a:spcAft", NS), runs)
                ln = ppr.find("a:lnSpc/a:spcPct", NS)
                if ln is not None and ln.get("val"):
                    rec.line_spacing = int(ln.get("val")) / 100000
            out.append(rec)
        return out

    # ── картинка ──

    def _picture(self, ctx: PartCtx, blip_fill: etree._Element) -> PictureRec | None:
        blip = blip_fill.find("a:blip", NS)
        if blip is None:
            return None
        rid = blip.get(R + "embed")
        if not rid:
            return None
        target = ctx.pkg.rel_by_id(ctx.part, rid)
        if not target or target not in ctx.pkg.names:
            return None
        try:
            with Image.open(io.BytesIO(ctx.pkg.zip.read(target))) as im:
                w, h = im.size
        except Exception:  # noqa: BLE001 — svg/emf и т. п. Pillow не открывает; пропорции не проверяем
            return PictureRec(px_w=0, px_h=0)
        sr = blip_fill.find("a:srcRect", NS)
        rect = tuple(int(sr.get(k, 0)) for k in ("l", "t", "r", "b")) if sr is not None else (0, 0, 0, 0)
        return PictureRec(px_w=w, px_h=h, src_rect=rect)  # type: ignore[arg-type]

    # ── диаграмма ──

    def _chart(self, ctx: PartCtx, sp: etree._Element) -> ChartRec | None:
        ref = sp.find(".//a:graphicData/*", NS)
        rid = ref.get(R + "id") if ref is not None else None
        target = ctx.pkg.rel_by_id(ctx.part, rid) if rid else None
        if not target or target not in ctx.pkg.names:
            return ChartRec(kind="unknown", n_series=0, has_legend=False, val_axis_title=False, cat_axis_title=False,
                            has_data_labels=False)
        root = ctx.pkg.xml(target)
        plot = root.find(f".//{C}plotArea")
        if plot is None:
            return None
        kind = next((localname(c) for c in plot if localname(c).endswith("Chart")), "unknown")
        series = plot.findall(f".//{C}ser")
        colors: list[str] = []
        for ser in series:
            sppr = ser.find(f"{C}spPr")
            for fill in (sppr.find("a:solidFill", NS) if sppr is not None else None,
                         sppr.find("a:ln/a:solidFill", NS) if sppr is not None else None):
                if fill is not None:
                    c = ctx.color_of(next(iter(fill), None))
                    if c:
                        colors.append(c)
        dlbls = [d for d in plot.iter(f"{C}dLbls") if _show_val(d)]
        val_ax = plot.find(f"{C}valAx")
        cat_ax = plot.find(f"{C}catAx")
        if cat_ax is None:
            cat_ax = plot.find(f"{C}dateAx")
        return ChartRec(
            kind=kind, n_series=len(series),
            has_legend=root.find(f".//{C}legend") is not None,
            val_axis_title=val_ax is not None and val_ax.find(f"{C}title") is not None,
            cat_axis_title=cat_ax is not None and cat_ax.find(f"{C}title") is not None,
            has_data_labels=bool(dlbls), series_colors=colors,
        )

    # ── таблица ──

    def _table(self, ctx: PartCtx, sp: etree._Element, rec: ShapeRec) -> TableRec | None:
        tbl = sp.find(".//a:tbl", NS)
        if tbl is None:
            return None
        rows = tbl.findall("a:tr", NS)
        cols = tbl.findall("a:tblGrid/a:gridCol", NS)
        fills: list[str] = []
        for tc_pr in tbl.iter(A + "tcPr"):
            for hex_, _ in ctx.fill_of(tc_pr) or []:
                fills.append(hex_)
        rec.paragraphs = self._paragraphs(ctx, list(tbl.iter(A + "p")), None)
        rec.text = "\n".join(p.text for p in rec.paragraphs)
        return TableRec(rows=len(rows), cols=len(cols), fills=fills)


# ──────────────────────────── вспомогательное ────────────────────────────


def _layout_name(pkg: Package, layout_part: str) -> str:
    if not layout_part or layout_part not in pkg.names:
        return ""
    csld = pkg.xml(layout_part).find("p:cSld", NS)
    return (csld.get("name") if csld is not None else "") or layout_part.rsplit("/", 1)[-1]


def _inherited_bbox(ctx: PartCtx, sp: etree._Element) -> tuple[int, int, int, int] | None:
    ph = placeholder(sp)
    if ph is None:
        return None
    for root in (ctx.layout, ctx.master):
        target = ctx.find_placeholder(root, ph)
        if target is not None and (bb := absolute_bbox(target)) is not None:
            return bb
    return None


def _spacing_pt(el: etree._Element | None, runs: list[RunRec]) -> float:
    if el is None:
        return 0.0
    pts = el.find("a:spcPts", NS)
    if pts is not None and pts.get("val"):
        return int(pts.get("val")) / 100
    pct = el.find("a:spcPct", NS)
    if pct is not None and pct.get("val") and runs:
        return int(pct.get("val")) / 100000 * runs[0].size_pt
    return 0.0


def _has_bullet(ctx: PartCtx, ppr: etree._Element | None, sp: etree._Element | None, lvl: int) -> bool:
    """Маркер абзаца с учётом наследования: pPr → lstStyle фигуры → плейсхолдер лейаута/мастера → bodyStyle."""
    if (b := _bullet_in(ppr)) is not None:
        return b
    if sp is None:
        return False
    lvl_tag = f"a:lvl{lvl + 1}pPr"
    lst = sp.find("p:txBody/a:lstStyle", NS)
    if lst is not None and (b := _bullet_in(lst.find(lvl_tag, NS))) is not None:
        return b
    ph = placeholder(sp)
    if ph is None:
        return False
    for root in (ctx.layout, ctx.master):
        target = ctx.find_placeholder(root, ph)
        if target is not None and (lst := target.find("p:txBody/a:lstStyle", NS)) is not None:
            if (b := _bullet_in(lst.find(lvl_tag, NS))) is not None:
                return b
    if ph[0] in ("title", "ctrTitle", "subTitle"):
        return False
    tx = ctx.master.find("p:txStyles", NS)
    if tx is not None and (b := _bullet_in(tx.find(f"p:bodyStyle/{lvl_tag}", NS))) is not None:
        return b
    return False


def _bullet_in(ppr: etree._Element | None) -> bool | None:
    if ppr is None:
        return None
    for c in ppr:
        n = localname(c)
        if n == "buNone":
            return False
        if n in ("buChar", "buAutoNum", "buBlip"):
            return True
    return None


def _show_val(dlbls: etree._Element) -> bool:
    for tag in ("showVal", "showPercent", "showCatName"):
        el = dlbls.find(f"{C}{tag}")
        if el is not None and el.get("val") in ("1", "true"):
            return True
    return False


__all__ = ["AuditContext", "ChartRec", "ParaRec", "PictureRec", "RunRec", "ShapeRec", "SlideCtx", "SPARSE_ARCHETYPES",
           "STEP", "SYMBOL_FONTS", "TableRec"]
