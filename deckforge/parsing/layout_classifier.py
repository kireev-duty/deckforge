"""Архетипы слайдов-образцов по геометрии фигур, а не по именам лейаутов.

Почему по геометрии: в датасете лейауты почти пустые (`[title]`), имена дублируются
(«13_Титульный слайд» — это сетка карточек), а весь контент лежит свободными фигурами
и группами прямо на слайдах. Поэтому для каждого слайда собираем фигуры (с пересчётом
координат внутри групп), считаем признаки (текстовые блоки, крупные числа, картинки,
коннекторы, кластеры одинаковых блоков) и прогоняем набор скоринговых правил.

Ключевые слова из текста-заглушки («Спасибо за внимание», «Имя Фамилия», «Таймлайн»)
используются только как вторичный сигнал — буст к геометрии.

Неоднозначные слайды (низкий score или два близких кандидата) уточняет VLM через скилл
`template_tagger` — по PNG-рендеру и списку фигур. Модуль сам не рендерит: PNG передаёт
вызывающая сторона (см. tools/classify_layouts.py).

Использование:
    python -m deckforge.parsing.layout_classifier <file.pptx> [--thumbs DIR] [--vlm] [--md out.md] [--json out.json]
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from statistics import median
from typing import TYPE_CHECKING, Callable

from lxml import etree

from deckforge.core.ir import ARCHETYPE_HINTS, Archetype, Box, Exemplar, Slot, SlotKind
from deckforge.parsing.ooxml import (
    NS,
    A,
    P,
    Package,
    PartCtx,
    absolute_bbox,
    font_scale,
    graphic_kind,
    iter_shapes,
    localname,
    placeholder,
    shape_id,
    shape_name,
    shape_text,
)

if TYPE_CHECKING:
    from deckforge.llm.client import LLMClient

# ──────────────────────────── настраиваемые пороги ────────────────────────────

BIG_PIC_AREA = 0.12  # картинка ≥ 12 % слайда — «большая» (фото, мокап, график-картинка)
ICON_MAX_AREA = 0.01  # ≤ 1 % и почти квадратная — иконка
PICTURE_MIN_AREA = 0.02  # картинки меньше — декор, слотом не становятся
CARD_MIN = 3  # минимум одинаковых блоков для «сетки карточек»
SIZE_TOL = 0.02  # блоки считаются одинаковыми, если w и h совпадают с точностью 2 % слайда
HEIGHT_TOL = 0.06  # …а по высоте — с точностью 6 % (автоподбор высоты текстовых боксов)
CARD_STACK_MIN_H = 0.10  # блок в одной колонке — карточка, а не пункт списка, если он выше 10 % слайда
TABLE_MIN_CELLS = 12  # ≥ 12 строго одинаковых блоков в ≥2 колонках и ≥4 рядах — таблица из фигур
ALIGN_TOL = 0.03  # допуск выравнивания по одной оси (доля слайда)
TITLE_ZONE = 0.30  # заголовок без плейсхолдера ищем в верхних 30 % слайда
TITLE_MAX_CHARS = 80
BIG_TITLE_RATIO = 1.6  # заголовок «крупный», если кегль ≥ 1.6× медианного на слайде
BIG_TITLE_H = 0.12  # …или высота бокса ≥ 12 % слайда
KPI_SIZE_RATIO = 2.0  # число «крупное», если кегль ≥ 2× медианного
LIBRARY_PICS = 30  # ≥ 30 картинок — библиотека иконок, не образец
AMBIGUOUS_BELOW = 0.70  # уверенность ниже — в VLM
AMBIGUOUS_MARGIN = 0.15  # разрыв между двумя лучшими кандидатами меньше — в VLM
FIXED_MIN_SLIDES = 3  # фигура на ≥ max(3, n/4) слайдах в одной позиции — фиксированный элемент
FIXED_TEXT_ZONE = 0.12  # тексты считаем колонтитулами только в верхних/нижних 12 %
EMU_PER_PT = 12700
MAX_SUMMARY_ROWS = 40  # больше фигур VLM не показываем (таблицы из 80 ячеек, библиотеки иконок)

NUMBER_RE = re.compile(r"^[\dхx]{1,4}([.,]\d+)?\s*[%+]?\s*$|^[\dхx]{1,4}\s*%|^\d+([.,]\d+)?\s*(млн|тыс|млрд|k|m|b|x|×)\b", re.I)
SEQ_RE = re.compile(r"^0?(\d{1,2})\s*$")
ARROW_PRSTS = {"rightArrow", "leftArrow", "chevron", "homePlate", "notchedRightArrow", "curvedRightArrow", "bentArrow"}
ROUND_PRSTS = {"ellipse", "roundRect", "round2SameRect"}
THIN_LINE = 0.005  # прямоугольник тоньше 0.5 % слайда и длиннее 10 % — линия, а не фигура
LINE_PRSTS = {"line", "straightConnector1", "bentConnector3", "curvedConnector3"}

KEYWORDS: dict[str, tuple[str, ...]] = {
    "closing": ("спасибо", "thank", "q&a", "qr", "контакт", "contact", "call to action", "ссылка", "вопросы"),
    "agenda": ("оглавление", "содержание", "agenda", "contents", "план презентации"),
    "team": ("имя фамилия", "фамилия", "должность", "спикер", "speaker", "роль в команде", "фио", "капитан"),
    "process": ("таймлайн", "timeline", "этап", "шаг", "stage", "step", "стадии", "процесс", "roadmap", "схем"),
    "section": ("раздел", "section", "глава"),
    "quote": ("цитата", "quote"),
    "title": ("название презентации", "тему презентации", "тема презентации", "презентаци"),
}
PHOTO_WORDS = ("вставить фото", "вставитьфото", "фото", "photo", "иллюстрация", "изображение", "картинка", "qr", "скриншот", "image")
QUOTE_MARKS = ("«", "»", "“", "”", "„")


# ──────────────────────────── структуры ────────────────────────────


@dataclass
class ShapeInfo:
    """Фигура слайда с координатами в долях слайда и резолвленным кеглем."""

    id: str
    name: str
    kind: str  # title|subtitle|body|text|pic|pic_ph|chart|table|diagram|shape|connector|sldnum|footer|date
    box: Box
    fx: float
    fy: float
    fw: float
    fh: float
    text: str = ""
    chars: int = 0
    paragraphs: int = 0
    size_pt: float = 0.0
    bold: bool = False
    italic: bool = False
    is_placeholder: bool = False
    ph_type: str | None = None
    in_group: bool = False
    prst: str | None = None
    fixed: bool = False

    @property
    def area(self) -> float:
        return self.fw * self.fh

    @property
    def is_text(self) -> bool:
        return self.kind in ("title", "subtitle", "body", "text")

    @property
    def cx(self) -> float:
        return self.fx + self.fw / 2

    @property
    def cy(self) -> float:
        return self.fy + self.fh / 2

    def overlaps(self, other: ShapeInfo) -> bool:
        return not (self.fx + self.fw <= other.fx or other.fx + other.fw <= self.fx
                    or self.fy + self.fh <= other.fy or other.fy + other.fh <= self.fy)


@dataclass
class SlideFeatures:
    index: int
    n_slides: int
    shapes: list[ShapeInfo]
    title: ShapeInfo | None
    content: list[ShapeInfo]  # текстовые блоки без заголовка, колонтитулов и фиксированных
    numbers: list[ShapeInfo]  # кандидаты в KPI-цифры
    big_pics: list[ShapeInfo]
    pics: list[ShapeInfo]
    icons: list[ShapeInfo]
    charts: list[ShapeInfo]
    tables: list[ShapeInfo]
    diagrams: list[ShapeInfo]
    connectors: int
    arrows: int
    round_marks: list[ShapeInfo]  # мелкие круги/скругления — маркеры списка, точки таймлайна, «аватары»
    card_groups: list[list[ShapeInfo]]  # кластеры ≥ CARD_MIN одинаковых блоков, расположенных сеткой
    list_groups: list[list[ShapeInfo]]  # …расположенных одной колонкой (список)
    cell_groups: list[list[ShapeInfo]]  # строгие кластеры (и по высоте) — для «таблицы из фигур»
    sequence_row: int  # длина ряда последовательных номеров (1,2,3… в одну строку)
    sequence_col: int  # …в одну колонку
    keywords: set[str]
    median_size: float
    text_area: float
    fill_ratio: float

    @property
    def n_cards(self) -> int:
        return max((len(g) for g in self.card_groups), default=0)

    @property
    def card_pairs(self) -> bool:
        """Два кластера одинаковой мощности — «подпись + текст» в каждой карточке."""
        sizes = [len(g) for g in self.card_groups]
        return any(sizes.count(s) >= 2 for s in set(sizes))

    @property
    def team_pairs(self) -> int:
        return sum(1 for s in self.content if _has_words(s.text, KEYWORDS["team"]))

    def title_is_big(self) -> bool:
        t = self.title
        if t is None:
            return False
        return t.fh >= BIG_TITLE_H or (self.median_size > 0 and t.size_pt >= BIG_TITLE_RATIO * self.median_size)


@dataclass
class Candidate:
    archetype: Archetype
    score: float
    reasons: list[str] = field(default_factory=list)

    def __str__(self) -> str:
        return f"{self.archetype.value} {self.score:.2f} — {'; '.join(self.reasons)}"


@dataclass
class SlideProfile:
    index: int  # 0-based
    layout_name: str
    features: SlideFeatures
    candidates: list[Candidate]
    archetype: Archetype
    confidence: float
    ambiguous: bool
    slots: list[Slot]
    fixed_ids: list[str]
    source: str = "rules"  # rules | vlm
    tags: list[str] = field(default_factory=list)
    vlm_raw: dict | None = None
    rules_archetype: Archetype | None = None  # вердикт правил до VLM

    @property
    def id(self) -> str:
        return f"slide{self.index + 1}"

    def to_exemplar(self, thumbnail: str | None = None) -> Exemplar:
        return Exemplar(
            id=self.id,
            source_index=self.index,
            layout_name=self.layout_name,
            archetype=self.archetype,
            slots=self.slots,
            fixed=self.fixed_ids,
            fill_ratio=round(self.features.fill_ratio, 3),
            thumbnail=thumbnail,
            tags=self.tags,
            confidence=round(self.confidence, 2),
        )


# ──────────────────────────── сбор фигур ────────────────────────────


def _has_words(text: str, words: tuple[str, ...]) -> bool:
    t = text.lower()
    return any(w in t for w in words)


def _norm_text(sp: etree._Element) -> str:
    return re.sub(r"\s+", " ", shape_text(sp)).strip()


def _text_props(ctx: PartCtx, sp: etree._Element) -> tuple[float, bool, bool, int]:
    """(кегль, bold, italic, абзацев с текстом) по первому run'у; для пустого плейсхолдера — по endParaRPr."""
    paras = sp.findall("p:txBody/a:p", NS)
    n_paras = sum(1 for p in paras if "".join(t.text or "" for t in p.iter(A + "t")).strip())
    scale = font_scale(sp)
    for para in paras:
        ppr = para.find("a:pPr", NS)
        lvl = int(ppr.get("lvl", "0")) if ppr is not None else 0
        for run in para:
            if localname(run) in ("r", "fld") and (run.findtext("a:t", namespaces=NS) or "").strip():
                chain = ctx.run_props_chain(run, para, sp, lvl)
                return ctx.resolve_size(chain) * scale, ctx.resolve_bold(chain), _italic(chain), n_paras
    if paras:
        para = paras[0]
        ppr = para.find("a:pPr", NS)
        lvl = int(ppr.get("lvl", "0")) if ppr is not None else 0
        chain = ctx.run_props_chain(etree.Element(A + "r"), para, sp, lvl)
        epr = para.find("a:endParaRPr", NS)
        if epr is not None:
            chain.insert(0, epr)
        return ctx.resolve_size(chain) * scale, ctx.resolve_bold(chain), _italic(chain), 0
    return 0.0, False, False, 0


def _italic(chain: list[etree._Element]) -> bool:
    for el in chain:
        if el.get("i") is not None:
            return el.get("i") in ("1", "true")
    return False


def _inherited_bbox(ctx: PartCtx, sp: etree._Element) -> tuple[int, int, int, int] | None:
    """Геометрия плейсхолдера без xfrm — из лейаута, затем из мастера."""
    ph = placeholder(sp)
    if ph is None:
        return None
    for root in (ctx.layout, ctx.master):
        target = ctx.find_placeholder(root, ph)
        if target is not None and (bb := absolute_bbox(target)) is not None:
            return bb
    return None


def _kind_of(sp: etree._Element, tag: str) -> tuple[str, bool, str | None]:
    """(kind, is_placeholder, ph_type) по тегу и плейсхолдеру."""
    ph = placeholder(sp)
    if tag == "pic":
        return ("pic_ph" if ph else "pic"), bool(ph), ph[0] if ph else None
    if tag == "graphicFrame":
        g = graphic_kind(sp)
        return (g if g in ("chart", "table", "diagram") else "shape"), bool(ph), ph[0] if ph else None
    if tag == "cxnSp":
        return "connector", False, None
    if ph is not None:
        t = ph[0]
        if t in ("title", "ctrTitle"):
            return "title", True, t
        if t == "subTitle":
            return "subtitle", True, t
        if t == "sldNum":
            return "sldnum", True, t
        if t == "ftr":
            return "footer", True, t
        if t == "dt":
            return "date", True, t
        if t == "pic":
            return "pic_ph", True, t
        return "body", True, t
    return ("text" if _norm_text(sp) else "shape"), False, None


def _signature(kind: str, text: str, bb: tuple[int, int, int, int]) -> tuple:
    step = 914400 // 20  # 1/20 дюйма
    return (kind, text, *(round(v / step) for v in bb))


def find_fixed_signatures(pkg: Package) -> set[tuple]:
    """Сигнатуры фигур, повторяющихся в одной позиции на многих слайдах: лого, декор, колонтитулы.

    Плейсхолдеры не учитываются (title VK Tech повторяется 31 раз — это слот, а не декор).
    Тексты считаются только в зоне колонтитулов, чтобы не поймать одинаковые карточки.
    """
    counts: Counter[tuple] = Counter()
    sw, sh = pkg.slide_size
    for slide in pkg.slides:
        root = pkg.xml(slide)
        seen: set[tuple] = set()
        for sp in iter_shapes(root.find("p:cSld/p:spTree", NS)):
            if placeholder(sp) is not None:
                continue
            bb = absolute_bbox(sp)
            if bb is None:
                continue
            tag = localname(sp)
            text = _norm_text(sp) if tag == "sp" else ""
            if text:
                fy, fh = bb[1] / sh, bb[3] / sh
                if not (fy + fh <= FIXED_TEXT_ZONE or fy >= 1 - FIXED_TEXT_ZONE):
                    continue
            seen.add(_signature(tag, text, bb))
        counts.update(seen)
    threshold = max(FIXED_MIN_SLIDES, len(pkg.slides) // 4)
    return {sig for sig, n in counts.items() if n >= threshold}


def collect_shapes(ctx: PartCtx, fixed_sigs: set[tuple]) -> list[ShapeInfo]:
    sw, sh = ctx.pkg.slide_size
    out: list[ShapeInfo] = []
    for sp in iter_shapes(ctx.sp_tree):
        tag = localname(sp)
        bb = absolute_bbox(sp) or _inherited_bbox(ctx, sp)
        if bb is None or (bb[2] <= 0 and bb[3] <= 0):
            continue
        kind, is_ph, ph_type = _kind_of(sp, tag)
        geom = sp.find("p:spPr/a:prstGeom", NS)
        prst = geom.get("prst") if geom is not None else None
        thin = min(bb[2] / sw, bb[3] / sh) <= THIN_LINE and max(bb[2] / sw, bb[3] / sh) >= 0.1
        if kind == "shape" and (prst in LINE_PRSTS or bb[2] <= 0 or bb[3] <= 0 or (thin and not _norm_text(sp))):
            kind = "connector"  # линия, нарисованная фигурой, — тот же коннектор
        if kind == "pic" and min(bb[2] / sw, bb[3] / sh) <= 0.02 and max(bb[2] / sw, bb[3] / sh) >= 0.3:
            kind = "connector"  # линия-картинка (таймлайн VK Tech)
        if kind != "connector" and (bb[2] <= 0 or bb[3] <= 0):
            continue
        text = _norm_text(sp) if tag in ("sp", "graphicFrame") else ""
        size, bold, italic, n_paras = _text_props(ctx, sp) if tag == "sp" else (0.0, False, False, 0)
        info = ShapeInfo(
            id=shape_id(sp),
            name=shape_name(sp),
            kind=kind,
            box=Box(x=bb[0], y=bb[1], w=bb[2], h=bb[3]),
            fx=bb[0] / sw, fy=bb[1] / sh, fw=bb[2] / sw, fh=bb[3] / sh,
            text=text,
            chars=len(text),
            paragraphs=n_paras,
            size_pt=round(size, 1),
            bold=bold,
            italic=italic,
            is_placeholder=is_ph,
            ph_type=ph_type,
            in_group=any(a.tag == P + "grpSp" for a in sp.iterancestors()),
            prst=prst,
            fixed=(not is_ph) and _signature(tag, text if tag == "sp" else "", bb) in fixed_sigs,
        )
        out.append(info)
    _mark_photo_zones(out)
    return out


def _mark_photo_zones(shapes: list[ShapeInfo]) -> None:
    """«Вставить фото» / «Иллюстрация» / «QR-code» — подпись зоны под картинку.

    Содержащая её фигура (рамка) становится слотом picture, сама подпись — декором.
    Если рамки нет, слотом picture становится сам текстовый блок.
    """
    for s in shapes:
        if s.kind not in ("text", "body") or s.chars == 0 or s.chars > 25 or not _has_words(s.text, PHOTO_WORDS):
            continue
        frame = None
        for f in shapes:
            if f is s or f.is_text or f.kind not in ("shape",) or f.fixed:
                continue
            if f.fx <= s.cx <= f.fx + f.fw and f.fy <= s.cy <= f.fy + f.fh and f.area > s.area:
                if frame is None or f.area < frame.area:
                    frame = f
        if frame is not None:
            frame.kind = "pic_ph"
            s.fixed = True
        else:
            s.kind = "pic_ph"


# ──────────────────────────── признаки ────────────────────────────


def _cluster_same_size(blocks: list[ShapeInfo], h_tol: float = HEIGHT_TOL) -> list[list[ShapeInfo]]:
    """Жадная кластеризация по ширине (строго) и высоте (мягко — автоподбор высоты текста плавает)."""
    clusters: list[list[ShapeInfo]] = []
    for b in sorted(blocks, key=lambda s: (s.fw, s.fh)):
        for g in clusters:
            rep = g[0]
            if abs(rep.fw - b.fw) <= SIZE_TOL and abs(rep.fh - b.fh) <= h_tol:
                g.append(b)
                break
        else:
            clusters.append([b])
    return [g for g in clusters if len(g) >= CARD_MIN]


def _axis_count(group: list[ShapeInfo], key: Callable[[ShapeInfo], float]) -> int:
    vals = sorted(key(b) for b in group)
    n = 1
    for a, b in zip(vals, vals[1:]):
        if b - a > ALIGN_TOL:
            n += 1
    return n


def _is_grid(group: list[ShapeInfo]) -> bool:
    """Карточки = блоки в ≥ 2 колонках, либо одна колонка «толстых» блоков (заголовок + текст).

    Одна колонка тонких однострочных блоков — это список (содержание, буллеты с иконками).
    """
    if _axis_count(group, lambda b: b.fx) >= 2:
        return True
    return all(b.fh >= CARD_STACK_MIN_H or b.paragraphs >= 2 for b in group)


def _frames_for(group: list[ShapeInfo], shapes: list[ShapeInfo]) -> int:
    """Сколько блоков группы лежат внутри собственной рамки (roundRect/rect той же кратности)."""
    frames = [s for s in shapes if s.kind == "shape" and not s.fixed and s.area >= 0.01]
    n = 0
    for b in group:
        if any(f.fx <= b.cx <= f.fx + f.fw and f.fy <= b.cy <= f.fy + f.fh and f.area >= b.area for f in frames):
            n += 1
    return n


def _sequence(blocks: list[ShapeInfo], axis: str) -> int:
    """Длина ряда последовательных номеров 1,2,3… (или 01,02…), выровненных по оси."""
    nums = [(int(m.group(1)), b) for b in blocks if (m := SEQ_RE.match(b.text))]
    if len(nums) < 3:
        return 0
    key = (lambda b: b.cy) if axis == "row" else (lambda b: b.cx)
    best = 0
    for _, ref in nums:
        line = sorted({v for v, b in nums if abs(key(b) - key(ref)) <= ALIGN_TOL})
        run = 1
        for a, b in zip(line, line[1:]):
            run = run + 1 if b == a + 1 else 1
            best = max(best, run)
    return best if best >= 3 else 0


def build_features(shapes: list[ShapeInfo], index: int, n_slides: int) -> SlideFeatures:
    live = [s for s in shapes if not s.fixed]
    texts = [s for s in live if s.is_text and (s.chars > 0 or s.is_placeholder)]

    title = next((s for s in texts if s.kind == "title"), None)
    if title is None:
        # заголовок без плейсхолдера: самый крупный короткий текст в верхней зоне
        cands = [s for s in texts if s.fy < TITLE_ZONE and 0 < s.chars <= TITLE_MAX_CHARS]
        if cands:
            top = max(cands, key=lambda s: (s.size_pt, -s.fy))
            others = [s.size_pt for s in texts if s is not top and s.chars > 0]
            if not others or top.size_pt >= 1.15 * median(others):
                top.kind = "title"
                title = top

    content = [s for s in texts if s is not title and s.kind in ("body", "text", "subtitle")]
    sizes = [s.size_pt for s in content if s.size_pt > 0] or ([title.size_pt] if title and title.size_pt else [18.0])
    med = median(sizes)

    numbers = [
        s for s in content
        if s.chars > 0 and (NUMBER_RE.match(s.text) or (s.size_pt >= KPI_SIZE_RATIO * med and s.chars <= 6))
    ]
    pics = [s for s in live if s.kind in ("pic", "pic_ph")]
    big_pics = [s for s in pics if s.area >= BIG_PIC_AREA]
    icons = [
        s for s in live
        if s.kind in ("pic", "shape") and s.area <= ICON_MAX_AREA and s.box.h and 0.6 <= s.box.w / s.box.h <= 1.6
    ]
    round_marks = [s for s in live if s.kind == "shape" and s.prst in ROUND_PRSTS and s.area <= 0.02]
    blocks = [s for s in content if s.chars > 0 or s.is_placeholder]
    groups = _cluster_same_size(blocks)
    card_groups = [g for g in groups if _is_grid(g)]
    list_groups = [g for g in groups if not _is_grid(g)]
    cell_groups = [g for g in _cluster_same_size(blocks, h_tol=SIZE_TOL) if _is_grid(g)]

    all_text = " ".join(s.text for s in shapes if s.text).lower()
    keywords = {k for k, words in KEYWORDS.items() if any(w in all_text for w in words)}
    if any(m in all_text for m in QUOTE_MARKS):
        keywords.add("quote")

    text_area = sum(s.area for s in content)
    fill = min(1.0, text_area + sum(s.area for s in pics) + sum(s.area for s in live if s.kind in ("chart", "table")))
    return SlideFeatures(
        index=index, n_slides=n_slides, shapes=shapes, title=title, content=content, numbers=numbers,
        big_pics=big_pics, pics=pics, icons=icons,
        charts=[s for s in live if s.kind == "chart"], tables=[s for s in live if s.kind == "table"],
        diagrams=[s for s in live if s.kind == "diagram"],
        connectors=sum(1 for s in live if s.kind == "connector"),
        arrows=sum(1 for s in live if s.kind == "shape" and s.prst in ARROW_PRSTS),
        round_marks=round_marks, card_groups=card_groups, list_groups=list_groups, cell_groups=cell_groups,
        sequence_row=_sequence(content, "row"), sequence_col=_sequence(content, "col"),
        keywords=keywords, median_size=med, text_area=text_area, fill_ratio=fill,
    )


# ──────────────────────────── правила ────────────────────────────

Rule = Callable[[SlideFeatures], Candidate | None]


def _cand(arch: Archetype, parts: list[tuple[float, str]]) -> Candidate | None:
    score = sum(v for v, _ in parts)
    if score <= 0:
        return None
    return Candidate(arch, min(1.0, round(score, 2)), [r for v, r in parts if v])


def _sparse(f: SlideFeatures, max_content: int) -> bool:
    return len(f.content) <= max_content and not f.big_pics and f.n_cards < CARD_MIN


def _drawn_table(f: SlideFeatures) -> list[ShapeInfo] | None:
    """Крупнейший кластер одинаковых блоков, похожий на таблицу: ≥3 колонок × ≥3 рядов."""
    for g in sorted(f.cell_groups, key=len, reverse=True):
        if len(g) >= TABLE_MIN_CELLS and _axis_count(g, lambda b: b.fx) >= 2 and _axis_count(g, lambda b: b.fy) >= 4:
            return g
    return None


def _staircase(group: list[ShapeInfo]) -> bool:
    """«Лесенка»: блоки идут по диагонали (y монотонно растёт/убывает с x) с заметным перепадом."""
    if len(group) < 3:
        return False
    ys = [b.fy for b in sorted(group, key=lambda b: b.fx)]
    diffs = [b - a for a, b in zip(ys, ys[1:])]
    monotonic = all(d > ALIGN_TOL for d in diffs) or all(d < -ALIGN_TOL for d in diffs)
    return monotonic and abs(ys[-1] - ys[0]) >= 0.2


def rule_table(f: SlideFeatures) -> Candidate | None:
    if f.tables:
        return _cand(Archetype.TABLE, [(0.95, "нативная таблица")])
    if (g := _drawn_table(f)) is not None:
        return _cand(Archetype.TABLE, [(0.65, f"таблица из фигур: {len(g)} ячеек")])
    return None


def _bar_series(f: SlideFeatures) -> list[ShapeInfo]:
    """Столбцы/полосы нарисованной диаграммы.

    Вертикальные: ≥5 узких фигур одной ширины с общей базовой линией (столбчатая).
    Горизонтальные: ≥5 невысоких полос одной высоты, но разной длины (линейчатая, Гант).
    """
    cands = [s for s in f.shapes if not s.fixed and s.kind in ("shape", "pic") and not s.text]
    best: list[ShapeInfo] = []
    vertical = [s for s in cands if s.fw <= 0.12 and s.fh >= 0.05]
    for g in _cluster_same_size(vertical, h_tol=1.0):
        for edge in (lambda b: b.fy + b.fh, lambda b: b.fy):
            ref = median(edge(b) for b in g)
            aligned = [b for b in g if abs(edge(b) - ref) <= ALIGN_TOL]
            if len(aligned) >= 5 and _axis_count(aligned, lambda b: b.fx) >= 5 and len(aligned) > len(best):
                best = aligned
    horizontal = [s for s in cands if s.fh <= 0.08 and s.fw >= 0.05]
    groups: dict[int, list[ShapeInfo]] = defaultdict(list)
    for b in horizontal:
        groups[round(b.fh / SIZE_TOL)].append(b)
    for g in groups.values():
        widths = {round(b.fw / SIZE_TOL) for b in g}
        if len(g) >= 5 and len(widths) >= 3 and _axis_count(g, lambda b: b.fy) >= 4 and len(g) > len(best):
            best = g
    return best


def rule_chart(f: SlideFeatures) -> Candidate | None:
    if f.charts:
        return _cand(Archetype.CHART, [(0.95, "нативная диаграмма")])
    bars = _bar_series(f)
    if bars:
        parts = [(0.6, f"{len(bars)} столбцов одной ширины на общей базе")]
        if len(f.numbers) >= 5:
            parts.append((0.1, "много числовых подписей"))
        return _cand(Archetype.CHART, parts)
    return None


def rule_freeform(f: SlideFeatures) -> Candidate | None:
    parts: list[tuple[float, str]] = []
    if len(f.pics) >= LIBRARY_PICS:
        parts.append((0.9, f"{len(f.pics)} картинок — библиотека иконок"))
    elif f.title is None and not f.content and not f.big_pics and not f.charts and not f.tables:
        parts.append((0.9, "нет ни заголовка, ни текста"))
    else:
        parts.append((0.3, "fallback"))
    return _cand(Archetype.FREEFORM, parts)


def rule_title(f: SlideFeatures) -> Candidate | None:
    if f.title is None:
        return None
    parts: list[tuple[float, str]] = []
    if f.title.ph_type == "ctrTitle":
        parts.append((0.5, "ctrTitle"))
    elif f.index == 0:
        parts.append((0.5, "первый слайд"))
    elif f.index <= 5:
        parts.append((0.3, "один из первых слайдов"))  # в деках-примерах титульных вариантов несколько
    if f.title_is_big():
        parts.append((0.2, "крупный заголовок"))
    if _sparse(f, 3):
        parts.append((0.2, "≤3 текстовых блока, без картинок/карточек"))
    if "team" in f.keywords:
        parts.append((0.1, "имя/должность"))
    if "title" in f.keywords:
        parts.append((0.2, "«название/тема презентации»"))
    return _cand(Archetype.TITLE, parts)


def rule_closing(f: SlideFeatures) -> Candidate | None:
    parts: list[tuple[float, str]] = []
    last = f.index >= f.n_slides - 3
    if "closing" in f.keywords:
        parts.append((0.6, "спасибо/контакты/QR"))
        if last:
            parts.append((0.15, "в конце колоды"))
    elif last and _sparse(f, 3) and f.title is not None:
        parts.append((0.4, "последние слайды, почти пустой"))
    else:
        return None
    if _sparse(f, 4):
        parts.append((0.2, "мало контента"))
    if any(s.kind == "pic_ph" for s in f.shapes if not s.fixed):
        parts.append((0.15, "место под QR/фото"))
    return _cand(Archetype.CLOSING, parts)


def rule_section(f: SlideFeatures) -> Candidate | None:
    if f.title is None or f.index == 0:
        return None
    short = [s for s in f.content if s.chars <= 120 or s.area < 0.05]
    if len(f.content) > 1 or len(short) != len(f.content) or f.big_pics or f.n_cards >= CARD_MIN:
        return None
    parts: list[tuple[float, str]] = []
    if f.title_is_big():
        parts.append((0.7, "один крупный заголовок, слайд почти пуст"))
    else:
        parts.append((0.4, "заголовок и ≤1 короткий блок"))
    if "section" in f.keywords or any(SEQ_RE.match(s.text) for s in f.content):
        parts.append((0.15, "«раздел» / номер раздела"))
    return _cand(Archetype.SECTION, parts)


def rule_agenda(f: SlideFeatures) -> Candidate | None:
    parts: list[tuple[float, str]] = []
    if "agenda" in f.keywords:
        parts.append((0.5, "«содержание/оглавление»"))
    lists = [g for g in f.list_groups if len(g) >= 4]
    if lists:
        n = max(len(g) for g in lists)
        parts.append((min(0.45, 0.3 + 0.05 * (n - 4)), f"вертикальный список из {n} блоков"))
        if len(f.round_marks) >= n or len(f.icons) >= n:
            parts.append((0.2, "маркеры/иконки у пунктов"))
    if f.sequence_col >= 3:
        parts.append((0.2, f"нумерация 1..{f.sequence_col} в колонку"))
    return _cand(Archetype.AGENDA, parts)


def rule_kpi(f: SlideFeatures) -> Candidate | None:
    if not f.numbers:
        return None
    n = len(f.numbers)
    parts: list[tuple[float, str]] = [(0.5, f"{n} крупных чисел"), (0.15 * min(n, 3), "")]
    labels = [s for s in f.content if s not in f.numbers and s.chars <= 60]
    if any(abs(l.cx - num.cx) < 0.15 or abs(l.cy - num.cy) < 0.1 for num in f.numbers for l in labels):
        parts.append((0.1, "подписи рядом с числами"))
    if f.n_cards >= 4:
        parts.append((-0.3, "но это номера карточек"))
    if f.sequence_row >= 3 or f.sequence_col >= 3:
        parts.append((-0.3, "числа — нумерация, не показатели"))
    if f.big_pics or _bar_series(f):
        parts.append((-0.2, "есть крупная картинка/столбцы — возможно, график"))
    return _cand(Archetype.KPI, parts)


def rule_process(f: SlideFeatures) -> Candidate | None:
    parts: list[tuple[float, str]] = []
    if f.diagrams:
        parts.append((0.8, "SmartArt"))
    if f.connectors >= 2:
        parts.append((0.4, f"{f.connectors} коннекторов"))
    elif f.connectors == 1:
        parts.append((0.2, "коннектор"))
    if f.sequence_row >= 3:
        parts.append((0.4, f"нумерация 1..{f.sequence_row} в ряд"))
    if f.arrows:
        parts.append((0.3, f"{f.arrows} стрелок"))
    row_marks = [m for m in f.round_marks if abs(m.cy - f.round_marks[0].cy) <= ALIGN_TOL] if f.round_marks else []
    if len(row_marks) >= 3 and (f.connectors or f.sequence_row):
        parts.append((0.4, "точки в ряд на линии"))
    if "process" in f.keywords:
        parts.append((0.2, "таймлайн/этапы/шаги"))
    if any(_staircase(g) for g in f.card_groups):
        parts.append((0.5, "блоки лесенкой"))
    return _cand(Archetype.PROCESS, parts)


def rule_team(f: SlideFeatures) -> Candidate | None:
    pairs = f.team_pairs
    if pairs == 0:
        return None
    parts: list[tuple[float, str]] = []
    parts.append((0.6, f"{pairs} блоков имя/должность") if pairs >= 2 else (0.3, "один блок имя/должность"))
    names = [s for s in f.content if _has_words(s.text, KEYWORDS["team"])]
    marks = f.round_marks + f.pics + [s for s in f.shapes if s.kind == "pic_ph"]
    near = sum(1 for s in names if any(abs(m.cx - s.cx) < 0.12 and abs(m.cy - s.cy) < 0.25 for m in marks))
    if near >= min(len(names), 2):
        parts.append((0.2, "фото/круг рядом с именем"))
    if pairs >= 3:
        parts.append((0.1, "три и более"))
    return _cand(Archetype.TEAM, parts)


def rule_cards(f: SlideFeatures) -> Candidate | None:
    n = f.n_cards
    if n < CARD_MIN:
        return None
    group = max(f.card_groups, key=len)
    parts: list[tuple[float, str]] = [(0.5, f"{n} одинаковых блоков сеткой"), (min(0.4, 0.1 * (n - 2)), "")]
    if f.card_pairs:
        parts.append((0.15, "подпись + текст в каждой"))
    if _frames_for(group, f.shapes) >= n:
        parts.append((0.15, "каждая в своей рамке"))
    elif len(f.icons) >= n:
        parts.append((0.15, "иконка у каждой"))
    if f.team_pairs >= 2:
        parts.append((-0.4, "но это имена/должности"))
    if f.connectors or f.sequence_row >= 3 or f.arrows:
        parts.append((-0.3, "но есть связи/нумерация в ряд"))
    if _staircase(group):
        parts.append((-0.3, "но блоки лесенкой"))
    if f.big_pics:
        parts.append((-0.25, "но есть крупная картинка"))
    if _bar_series(f):
        parts.append((-0.4, "но это столбцы диаграммы"))
    if _drawn_table(f) is not None:
        parts.append((-0.4, "но это похоже на таблицу"))
    return _cand(Archetype.CARDS, parts)


def rule_two_column(f: SlideFeatures) -> Candidate | None:
    wide = [s for s in f.content if s.fw >= 0.25]
    if len(wide) != 2 or f.big_pics:
        return None
    a, b = sorted(wide, key=lambda s: s.fx)
    if a.fx + a.fw > b.fx + ALIGN_TOL or not (a.fy < b.fy + b.fh and b.fy < a.fy + a.fh):
        return None
    parts: list[tuple[float, str]] = [(0.7, "два широких блока рядом")]
    if a.is_placeholder and b.is_placeholder:
        parts.append((0.1, "оба — плейсхолдеры"))
    return _cand(Archetype.TWO_COLUMN, parts)


def rule_bullets(f: SlideFeatures) -> Candidate | None:
    big = [s for s in f.content if s.area >= 0.10 and s.fw >= 0.4]
    if len(big) != 1 or f.big_pics or f.n_cards >= CARD_MIN:
        return None
    parts: list[tuple[float, str]] = [(0.7, "один крупный текстовый блок")]
    if big[0].is_placeholder:
        parts.append((0.1, "body-плейсхолдер"))
    if big[0].paragraphs >= 2:
        parts.append((0.1, "несколько абзацев"))
    if f.title is None:
        parts.append((-0.3, "но нет заголовка"))
    return _cand(Archetype.BULLETS, parts)


def rule_image_text(f: SlideFeatures) -> Candidate | None:
    if not f.big_pics:
        return None
    pic = max(f.big_pics, key=lambda s: s.area)
    beside = [s for s in f.content if not s.overlaps(pic)]
    if any(s.area >= 0.05 for s in beside):
        return _cand(Archetype.IMAGE_TEXT, [(0.65, "крупная картинка + текстовый блок рядом")])
    if len(beside) >= 3:
        return _cand(Archetype.IMAGE_TEXT, [(0.6, f"крупная картинка + список из {len(beside)} блоков")])
    return None


def rule_image_full(f: SlideFeatures) -> Candidate | None:
    """Всегда ≤ 0.65: картинка-«график», фото или мокап — это различает только VLM."""
    if not f.big_pics or len(f.content) > 2:
        return None
    pic = max(f.big_pics, key=lambda s: s.area)
    if not all(s.area < 0.05 and s.chars <= 60 for s in f.content):
        return None
    parts: list[tuple[float, str]] = [(0.6, "крупная картинка, текста почти нет")]
    if pic.area >= 0.5:
        parts.append((0.05, "картинка ≥ половины слайда"))
    return _cand(Archetype.IMAGE_FULL, parts)


def rule_quote(f: SlideFeatures) -> Candidate | None:
    if f.title is not None and f.title.is_placeholder:
        return None
    long = [s for s in f.content if s.chars >= 60]
    short = [s for s in f.content if 0 < s.chars < 60]
    if len(long) != 1 or len(short) > 1 or f.big_pics:
        return None
    parts: list[tuple[float, str]] = [(0.5, "один длинный текст + подпись")]
    if "quote" in f.keywords:
        parts.append((0.3, "кавычки/«цитата»"))
    if long[0].italic:
        parts.append((0.1, "курсив"))
    if f.title is None:
        parts.append((0.2, "заголовка нет — высказывание"))
    return _cand(Archetype.QUOTE, parts)


RULES: list[Rule] = [
    rule_table, rule_chart, rule_freeform, rule_title, rule_closing, rule_section, rule_agenda, rule_kpi,
    rule_process, rule_team, rule_cards, rule_two_column, rule_bullets, rule_image_text, rule_image_full, rule_quote,
]


def score_rules(f: SlideFeatures) -> list[Candidate]:
    cands = [c for rule in RULES if (c := rule(f)) is not None]
    cands.sort(key=lambda c: -c.score)
    return cands


def pick(cands: list[Candidate]) -> tuple[Archetype, float, bool]:
    top = cands[0]
    second = cands[1].score if len(cands) > 1 else 0.0
    ambiguous = top.score < AMBIGUOUS_BELOW or round(top.score - second, 2) <= AMBIGUOUS_MARGIN
    if top.archetype == Archetype.FREEFORM and top.score >= 0.9:
        ambiguous = False  # библиотеки иконок и пустые слайды в VLM не отправляем
    return top.archetype, top.score, ambiguous


# ──────────────────────────── слоты ────────────────────────────


def _capacity(s: ShapeInfo) -> tuple[int, int]:
    size = s.size_pt or 18.0
    w_pt, h_pt = s.box.w / EMU_PER_PT, s.box.h / EMU_PER_PT
    cpl = max(1.0, w_pt / (0.5 * size))
    lines = max(1, int(h_pt / (1.2 * size)))
    return int(0.9 * cpl * lines), lines


def _slot(s: ShapeInfo, kind: SlotKind) -> Slot:
    max_chars = max_lines = max_items = None
    if kind in (SlotKind.TITLE, SlotKind.SUBTITLE, SlotKind.BODY, SlotKind.CAPTION, SlotKind.LABEL, SlotKind.NUMBER):
        max_chars, max_lines = _capacity(s)
        if kind == SlotKind.BODY:
            max_items = max_lines
    return Slot(
        id=s.id, kind=kind, box=s.box, max_chars=max_chars, max_lines=max_lines, max_items=max_items,
        size_pt=s.size_pt or None, placeholder_type=s.ph_type, sample_text=s.text[:200] or None,
    )


def build_slots(f: SlideFeatures, archetype: Archetype) -> list[Slot]:
    slots: list[Slot] = []
    if f.title is not None:
        slots.append(_slot(f.title, SlotKind.TITLE))
    grouped = {id(s) for g in f.card_groups + f.list_groups for s in g}
    subtitle_taken = False
    for s in sorted(f.content, key=lambda s: (round(s.fy, 2), s.fx)):
        if s in f.numbers and archetype in (Archetype.KPI, Archetype.CARDS, Archetype.IMAGE_TEXT, Archetype.FREEFORM):
            kind = SlotKind.NUMBER
        elif s.kind == "subtitle":
            kind = SlotKind.SUBTITLE
        elif archetype in (Archetype.TITLE, Archetype.SECTION, Archetype.CLOSING) and not subtitle_taken and (
            f.title is None or s.fy >= f.title.fy
        ):
            kind, subtitle_taken = SlotKind.SUBTITLE, True
        elif archetype == Archetype.QUOTE:
            kind = SlotKind.BODY if s.chars >= 60 else SlotKind.CAPTION
        elif (s.bold and s.chars <= 40) or (s.chars <= 30 and s.fh < 0.07 and (id(s) in grouped or s.size_pt <= f.median_size)):
            kind = SlotKind.LABEL
        elif s.chars > 0 and s.chars <= 40 and s.fh < 0.06 and s.size_pt < f.median_size:
            kind = SlotKind.CAPTION
        else:
            kind = SlotKind.BODY
        slots.append(_slot(s, kind))
    for s in f.shapes:
        if s.fixed:
            continue
        if s.kind == "chart":
            slots.append(_slot(s, SlotKind.CHART))
        elif s.kind == "table":
            slots.append(_slot(s, SlotKind.TABLE))
        elif s.kind == "diagram":
            slots.append(_slot(s, SlotKind.OTHER))
        elif s.kind in ("pic", "pic_ph"):
            if s in f.icons:
                slots.append(_slot(s, SlotKind.ICON))
            elif s.kind == "pic_ph" or s.area >= PICTURE_MIN_AREA:
                slots.append(_slot(s, SlotKind.PICTURE))
        elif s.kind == "sldnum":
            slots.append(_slot(s, SlotKind.SLIDE_NUMBER))
        elif s.kind == "footer":
            slots.append(_slot(s, SlotKind.FOOTER))
        elif s.kind == "date":
            slots.append(_slot(s, SlotKind.DATE))
    # таблица/диаграмма из фигур: десятки ячеек или столбцов → один слот, куда встанет нативный объект
    if archetype == Archetype.TABLE and not f.tables and (cells := _drawn_table(f)):
        slots = _collapse(slots, cells, SlotKind.TABLE)
    if archetype == Archetype.CHART and not f.charts and (bars := _bar_series(f)):
        slots = _collapse(slots, bars, SlotKind.CHART)
    return slots


def _collapse(slots: list[Slot], group: list[ShapeInfo], kind: SlotKind) -> list[Slot]:
    """Слоты внутри объединённого бокса группы заменяются одним слотом kind (заголовок не трогаем)."""
    x1, y1 = min(s.box.x for s in group), min(s.box.y for s in group)
    x2, y2 = max(s.box.x2 for s in group), max(s.box.y2 for s in group)
    inside = lambda b: x1 <= b.x + b.w / 2 <= x2 and y1 <= b.y + b.h / 2 <= y2  # noqa: E731
    kept = [s for s in slots if s.kind == SlotKind.TITLE or not inside(s.box)]
    kept.append(Slot(id=group[0].id, kind=kind, box=Box(x=x1, y=y1, w=x2 - x1, h=y2 - y1)))
    return kept


# ──────────────────────────── классификация ────────────────────────────


def classify_slide(ctx: PartCtx, index: int, n_slides: int, fixed_sigs: set[tuple]) -> SlideProfile:
    shapes = collect_shapes(ctx, fixed_sigs)
    f = build_features(shapes, index, n_slides)
    cands = score_rules(f)
    arch, conf, ambiguous = pick(cands)
    layout_name = ctx.layout.find("p:cSld", NS).get("name", "") if ctx.layout is not None else ""
    return SlideProfile(
        index=index, layout_name=layout_name, features=f, candidates=cands, archetype=arch, confidence=conf,
        ambiguous=ambiguous, slots=build_slots(f, arch), fixed_ids=[s.id for s in shapes if s.fixed],
        rules_archetype=arch,
    )


def shapes_summary(p: SlideProfile, limit: int = 60, max_rows: int = MAX_SUMMARY_ROWS) -> str:
    """Список фигур для VLM и отладки: id | kind | x,y w×h | кегль | текст.

    Таблицы из 80 ячеек и библиотеки иконок режем до max_rows строк — иначе ответ модели не влезает в max_tokens.
    """
    rows = []
    hidden: Counter[str] = Counter()
    for s in p.features.shapes:
        if s.fixed or (s.kind == "shape" and not s.text and s.area < 0.02):
            continue
        if len(rows) >= max_rows:
            hidden[s.kind] += 1
            continue
        t = (s.text[: limit - 1] + "…") if len(s.text) > limit else s.text
        size = f"{s.size_pt:g}pt" if s.size_pt else "-"
        rows.append(f"{s.id} | {s.kind} | {s.fx:.2f},{s.fy:.2f} {s.fw:.2f}×{s.fh:.2f} | {size} | {t}")
    if hidden:
        rows.append("… и ещё " + ", ".join(f"{n} × {k}" for k, n in hidden.most_common()) + " (не перечислены)")
    return "\n".join(rows)


def refine_with_vlm(p: SlideProfile, client: LLMClient, png: Path) -> SlideProfile:
    """Уточнение архетипа/слотов по PNG через скилл template_tagger. Ошибка вызова → профиль правил."""
    from deckforge.llm.skills import load_skill

    skill = load_skill("template_tagger")
    try:
        res = client.run_skill(
            skill, images=[png],
            archetype_guess=p.archetype.value,
            candidates="\n".join(str(c) for c in p.candidates[:3]),
            archetypes="\n".join(f"- {a.value}: {ARCHETYPE_HINTS[a]}" for a in Archetype),
            shapes_summary=shapes_summary(p),
        )
    except Exception as e:  # noqa: BLE001 — VLM не должна ронять парсинг
        p.tags = [f"vlm_error: {str(e)[:80]}"]
        return p
    if not isinstance(res, dict):
        return p
    p.vlm_raw = res
    p.source = "vlm"
    try:
        p.archetype = Archetype(str(res.get("archetype", "")).strip().lower())
    except ValueError:
        pass
    p.confidence = float(res.get("confidence", p.confidence) or p.confidence)
    p.tags = [str(t) for t in res.get("tags", [])][:6]
    decor = {str(d) for d in res.get("decor_ids", [])}
    roles = {str(k): str(v).lower() for k, v in (res.get("slot_roles") or {}).items()}
    if p.archetype != p.rules_archetype:
        p.slots = build_slots(p.features, p.archetype)
    kept: list[Slot] = []
    protected = {Archetype.CHART: SlotKind.CHART, Archetype.TABLE: SlotKind.TABLE}.get(p.archetype)
    for slot in p.slots:
        if slot.id in decor and slot.kind != protected:
            continue  # схлопнутый слот таблицы/диаграммы модель иногда считает декором — его не отдаём
        role = roles.get(slot.id)
        if role in {k.value for k in SlotKind} and slot.kind != protected:
            slot.kind = SlotKind(role)
        kept.append(slot)
    p.slots = _normalize_vlm_slots(kept, p)
    p.fixed_ids = sorted(set(p.fixed_ids) | (decor & {s.id for s in p.features.shapes}))
    p.ambiguous = False
    return p


def _normalize_vlm_slots(slots: list[Slot], p: SlideProfile) -> list[Slot]:
    """Модель иногда ставит title на имя спикера или chart на каждый столбец нарисованной диаграммы.

    Заголовок — один (плейсхолдер title, если он есть); повторные → label. Несколько chart/table-слотов
    на обычных фигурах схлопываются в один слот с объединённым боксом — туда встанет нативный объект.
    """
    title_id = p.features.title.id if p.features.title is not None else None
    titles = [s for s in slots if s.kind == SlotKind.TITLE]
    if len(titles) > 1:
        keep = next((s for s in titles if s.id == title_id), max(titles, key=lambda s: s.box.w * s.box.h))
        for s in titles:
            if s is not keep:
                s.kind = SlotKind.LABEL
    native = {s.id for s in p.features.shapes if s.kind in ("chart", "table")}
    out: list[Slot] = []
    for kind in (SlotKind.CHART, SlotKind.TABLE):
        drawn = [s for s in slots if s.kind == kind and s.id not in native]
        if len(drawn) > 1:
            x1, y1 = min(s.box.x for s in drawn), min(s.box.y for s in drawn)
            x2, y2 = max(s.box.x2 for s in drawn), max(s.box.y2 for s in drawn)
            merged = drawn[0].model_copy(update={"box": Box(x=x1, y=y1, w=x2 - x1, h=y2 - y1), "sample_text": None})
            out.append(merged)
            slots = [s for s in slots if s not in drawn]
    return slots + out


def classify_template(
    path: str | Path,
    client: LLMClient | None = None,
    thumbnails: dict[int, Path] | None = None,
    max_parallel: int = 4,
) -> list[SlideProfile]:
    """Профили всех слайдов. С client и thumbnails неоднозначные слайды уточняются VLM (параллельно)."""
    pkg = Package(path)
    fixed = find_fixed_signatures(pkg)
    profiles: list[SlideProfile] = []
    n = len(pkg.slides)
    for i, slide in enumerate(pkg.slides):
        ctx = PartCtx.for_slide(pkg, slide)
        if ctx is None:
            continue
        profiles.append(classify_slide(ctx, i, n, fixed))
    if client is not None and thumbnails:
        todo = [p for p in profiles if p.ambiguous and p.index in thumbnails]
        with ThreadPoolExecutor(max_workers=max(1, max_parallel)) as ex:
            list(ex.map(lambda p: refine_with_vlm(p, client, thumbnails[p.index]), todo))
    return profiles


# ──────────────────────────── вывод ────────────────────────────


def slots_brief(slots: list[Slot]) -> str:
    c = Counter(s.kind.value for s in slots)
    order = [k.value for k in SlotKind]
    return ", ".join(f"{k}×{n}" if n > 1 else k for k, n in sorted(c.items(), key=lambda kv: order.index(kv[0])))


def markdown_table(profiles: list[SlideProfile], name: str = "") -> str:
    lines = [f"### {name}", "", "| # | лейаут | правила | итог | слоты |", "|---|---|---|---|---|"]
    for p in profiles:
        top = p.candidates[0]
        rules = f"{top.archetype.value} {top.score:.2f}" + (" ⚠" if p.ambiguous or p.source == "vlm" else "")
        final = f"**{p.archetype.value}** ({p.source} {p.confidence:.2f})"
        if p.source == "vlm" and p.archetype != p.rules_archetype:
            final += " ←"
        tags = f" _{', '.join(p.tags)}_" if p.tags else ""
        lines.append(f"| {p.index + 1} | {p.layout_name[:24]} | {rules} | {final}{tags} | {slots_brief(p.slots)} |")
    counts = Counter(p.archetype.value for p in profiles)
    lines += ["", "Итого: " + ", ".join(f"{k}={v}" for k, v in sorted(counts.items(), key=lambda kv: -kv[1]))]
    return "\n".join(lines)


def print_table(profiles: list[SlideProfile], name: str = "") -> None:
    print(markdown_table(profiles, name))
    print()


def profiles_json(profiles: list[SlideProfile]) -> list[dict]:
    return [
        {
            "index": p.index, "layout": p.layout_name, "archetype": p.archetype.value, "confidence": p.confidence,
            "source": p.source, "ambiguous": p.ambiguous, "rules_archetype": p.rules_archetype.value if p.rules_archetype else None,
            "candidates": [str(c) for c in p.candidates[:4]], "tags": p.tags,
            "slots": [s.model_dump() for s in p.slots], "fixed": p.fixed_ids, "shapes": shapes_summary(p),
        }
        for p in profiles
    ]


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("files", nargs="+", type=Path)
    ap.add_argument("--thumbs", type=Path, help="папка с slide_NN.png (для VLM)")
    ap.add_argument("--vlm", action="store_true", help="уточнять неоднозначные слайды через VLM")
    ap.add_argument("--md", type=Path)
    ap.add_argument("--json", type=Path)
    a = ap.parse_args(argv)
    client = None
    if a.vlm:
        from deckforge.llm.client import LLMClient

        client = LLMClient()
    md, js = [], []
    for f in a.files:
        thumbs = {int(p.stem.split("_")[1]) - 1: p for p in a.thumbs.glob("slide_*.png")} if a.thumbs else None
        profiles = classify_template(f, client, thumbs)
        print_table(profiles, f.name)
        md.append(markdown_table(profiles, f.name))
        js.append({"file": str(f), "slides": profiles_json(profiles)})
    if a.md:
        a.md.write_text("\n\n".join(md), "utf-8")
    if a.json:
        a.json.write_text(json.dumps(js if len(js) > 1 else js[0], ensure_ascii=False, indent=1), "utf-8")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
