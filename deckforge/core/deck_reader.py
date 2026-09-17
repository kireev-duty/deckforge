"""Чтение фигур готовой колоды (или любого .pptx) в плоские записи с резолвленными свойствами.

Общий слой для audit/ (детерминированные проверки) и export/ (HTML-рендер): каждая фигура → `ShapeRec`
с абсолютной геометрией, текстом по абзацам/run'ам (шрифт, кегль, цвет, жирность — через цепочки
наследования `core/package.PartCtx`), заливками, деталями картинки / диаграммы / таблицы.
Ничего не знает ни о TemplateDNA, ни о DeckIR — флаги «наше / фиксированное / декор» ставит аудит.
"""

from __future__ import annotations

import copy
import io
import logging
from dataclasses import dataclass, field

from lxml import etree
from PIL import Image

from deckforge.core.ir import Box, ChartSpec
from deckforge.core.ooxml import (
    A,
    NS,
    R,
    absolute_bbox,
    graphic_kind,
    iter_shapes,
    localname,
    placeholder,
    shape_id,
    shape_name,
)
from deckforge.core.package import PartCtx, font_scale

log = logging.getLogger(__name__)

C_NS = "http://schemas.openxmlformats.org/drawingml/2006/chart"
C = f"{{{C_NS}}}"
DEFAULT_INSETS = (91440, 45720, 91440, 45720)  # lIns, tIns, rIns, bIns
SYMBOL_FONTS = ("Wingdings", "Webdings", "Symbol", "MT Extra")
DEFAULT_LINE_W = 9525  # 0,75 pt — толщина линии PowerPoint по умолчанию
CHART_KINDS = {"lineChart": "line", "pieChart": "pie", "doughnutChart": "doughnut", "areaChart": "area"}


# ──────────────────────────── записи ────────────────────────────


@dataclass
class RunRec:
    text: str
    font: str
    size_pt: float
    bold: bool
    color: str | None
    italic: bool = False
    underline: bool = False
    field: str | None = None  # a:fld/@type (slidenum, datetime…) — потребитель подставляет актуальное значение


@dataclass
class ParaRec:
    text: str
    runs: list[RunRec]
    bullet: bool = False
    level: int = 0
    space_before_pt: float = 0.0
    space_after_pt: float = 0.0
    line_spacing: float | None = None  # множитель, если задан явно
    align: str = "l"  # l | ctr | r | just
    bullet_char: str | None = None  # символ маркера (a:buChar); None при нумерации или без маркера
    bullet_auto: bool = False  # a:buAutoNum — нумерованный список
    indent_emu: int = 0  # a:pPr/@marL — отступ абзаца слева
    first_indent_emu: int = 0  # a:pPr/@indent — сдвиг первой строки (отрицательный — висячий, под маркер)

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
    spec: ChartSpec | None = None  # данные диаграммы (категории, серии) — для HTML/сравнений


@dataclass
class CellRec:
    paragraphs: list[ParaRec]
    fill: str | None = None
    row_span: int = 1
    col_span: int = 1
    merged: bool = False  # ячейка поглощена объединением (hMerge / vMerge)

    @property
    def text(self) -> str:
        return "\n".join(p.text for p in self.paragraphs)


@dataclass
class TableRec:
    rows: int
    cols: int
    fills: list[str] = field(default_factory=list)
    cells: list[list[CellRec]] = field(default_factory=list)
    col_widths_emu: list[int] = field(default_factory=list)
    row_heights_emu: list[int] = field(default_factory=list)


@dataclass
class PictureRec:
    px_w: int
    px_h: int
    src_rect: tuple[int, int, int, int] = (0, 0, 0, 0)  # l, t, r, b в 1/1000 %
    part: str | None = None  # имя части в zip (ppt/media/…) — байты читает потребитель

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
    anchor: str = "t"  # a:bodyPr/@anchor: t | ctr | b
    fill_alpha: float = 1.0  # a:alpha у сплошной заливки (0..1)
    line_w_emu: int = 0  # толщина обводки; 0 — обводки нет
    geom: str = "rect"  # a:prstGeom/@prst
    geom_adj: float | None = None  # a:avLst/a:gd[adj] в долях (радиус скругления roundRect); None — по умолчанию
    rot: float = 0.0  # градусы по часовой
    flip_h: bool = False
    flip_v: bool = False
    hidden: bool = False

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


# ──────────────────────────── чтение ────────────────────────────


def read_shapes(ctx: PartCtx, tree: etree._Element | None = None) -> list[ShapeRec]:
    """Все фигуры части (по умолчанию — её spTree) в порядке отрисовки; без геометрии — пропускаются."""
    out: list[ShapeRec] = []
    for z, sp in enumerate(iter_shapes(ctx.sp_tree if tree is None else tree)):
        rec = read_shape(ctx, sp, z)
        if rec is not None:
            out.append(rec)
    return out


def read_shape(ctx: PartCtx, sp: etree._Element, z: int = 0) -> ShapeRec | None:
    tag = localname(sp)
    bb = absolute_bbox(sp) or inherited_bbox(ctx, sp)
    if bb is None:
        return None
    ph = placeholder(sp)
    rec = ShapeRec(id=shape_id(sp), name=shape_name(sp), tag=tag, box=Box(x=bb[0], y=bb[1], w=bb[2], h=bb[3]),
                   el=sp, z=z, is_placeholder=ph is not None, ph_type=ph[0] if ph else None)
    _geometry(sp, rec)
    if tag in ("sp", "pic", "cxnSp"):
        rec.fill = [c for c, _ in ctx.shape_fill(sp)]
        rec.line = [c for c, _ in ctx.shape_line(sp)]
        rec.line_w_emu = _line_width(sp, bool(rec.line))
        rec.fill_alpha = _fill_alpha(sp)
    if tag == "sp":
        _text(ctx, sp, rec)
        blip_fill = sp.find("p:spPr/a:blipFill", NS)
        if blip_fill is not None:
            rec.picture = read_picture(ctx, blip_fill)
    elif tag == "pic":
        blip_fill = sp.find("p:blipFill", NS)
        if blip_fill is not None:
            rec.picture = read_picture(ctx, blip_fill)
    elif tag == "graphicFrame":
        kind = graphic_kind(sp)
        if kind == "chart":
            rec.chart = read_chart(ctx, sp)
        elif kind == "table":
            rec.table = read_table(ctx, sp, rec)
    return rec


def background_picture_part(ctx: PartCtx) -> str | None:
    """Имя части картинки-фона (slide → layout → master), если фон — картинка."""
    for root, part in zip((ctx.root, ctx.layout, ctx.master), part_chain(ctx)):
        if root is None or part is None:
            continue
        bg = root.find("p:cSld/p:bg", NS)
        if bg is None:
            continue
        blip = bg.find("p:bgPr/a:blipFill/a:blip", NS)
        if blip is None:
            return None  # фон задан цветом — дальше по цепочке не идём
        target = ctx.pkg.rel_by_id(part, blip.get(R + "embed") or "")
        return target if target and target in ctx.pkg.names else None
    return None


def part_chain(ctx: PartCtx) -> tuple[str | None, str | None, str | None]:
    """Имена частей (root, layout, master) для контекста слайда / лейаута / мастера."""
    if ctx.source == "slides":
        lp = ctx.pkg.layout_of(ctx.part)
        return ctx.part, lp, ctx.pkg.master_of(lp) if lp else None
    if ctx.source == "layouts":
        return ctx.part, None, ctx.pkg.master_of(ctx.part)
    return ctx.part, None, ctx.part


# ── геометрия ──


def _geometry(sp: etree._Element, rec: ShapeRec) -> None:
    xfrm = sp.find("p:spPr/a:xfrm", NS)
    if xfrm is None:
        xfrm = sp.find("p:xfrm", NS)
    if xfrm is not None:
        rec.rot = int(xfrm.get("rot", "0") or 0) / 60000
        rec.flip_h = xfrm.get("flipH") in ("1", "true")
        rec.flip_v = xfrm.get("flipV") in ("1", "true")
    geom = sp.find("p:spPr/a:prstGeom", NS)
    if geom is not None and geom.get("prst"):
        rec.geom = geom.get("prst")
        gd = geom.find("a:avLst/a:gd[@name='adj']", NS)
        if gd is not None and (gd.get("fmla") or "").startswith("val "):
            try:
                rec.geom_adj = int(gd.get("fmla").split()[1]) / 100000
            except ValueError:
                pass
    for tag in ("p:nvSpPr", "p:nvPicPr", "p:nvGraphicFramePr", "p:nvCxnSpPr"):
        nv = sp.find(f"{tag}/p:cNvPr", NS)
        if nv is not None:
            rec.hidden = nv.get("hidden") in ("1", "true")
            break


def _fill_alpha(sp: etree._Element) -> float:
    a = sp.find("p:spPr/a:solidFill/*/a:alpha", NS)
    if a is None or not a.get("val"):
        return 1.0
    return max(0.0, min(1.0, int(a.get("val")) / 100000))


def _line_width(sp: etree._Element, has_line: bool) -> int:
    ln = sp.find("p:spPr/a:ln", NS)
    if ln is not None and ln.get("w"):
        return int(ln.get("w"))
    return DEFAULT_LINE_W if has_line else 0


# ── текст ──


def _text(ctx: PartCtx, sp: etree._Element, rec: ShapeRec) -> None:
    tx = sp.find("p:txBody", NS)
    if tx is None:
        return
    body_pr = tx.find("a:bodyPr", NS)
    if body_pr is not None:
        rec.insets = tuple(int(body_pr.get(k, d)) for k, d in zip(("lIns", "tIns", "rIns", "bIns"), DEFAULT_INSETS))  # type: ignore[assignment]
        rec.wrap = body_pr.get("wrap", "square") != "none"
        rec.anchor = body_pr.get("anchor", "t") or "t"
        for child in body_pr:
            if localname(child) in ("normAutofit", "spAutoFit", "noAutofit"):
                rec.autofit = localname(child)
    rec.paragraphs = read_paragraphs(ctx, tx.findall("a:p", NS), sp)
    rec.text = "\n".join(p.text for p in rec.paragraphs)


def read_paragraphs(ctx: PartCtx, paras: list[etree._Element], sp: etree._Element | None) -> list[ParaRec]:
    """Абзацы с непустым текстом; run'ы с резолвленными шрифтом/кеглем/цветом. `sp=None` — текст таблицы."""
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
                               color=ctx.resolve_text_color(chain, sp),
                               italic=_flag(chain, "i"), underline=_underline(chain),
                               field=run.get("type") if localname(run) == "fld" else None))
        text = "".join(r.text for r in runs)
        if not text.strip():
            continue
        chain = para_props_chain(ctx, ppr, sp, lvl)
        bu = _bullet_el(chain)
        rec = ParaRec(text=text, runs=runs, level=lvl, bullet=bu is not None and localname(bu) != "buNone",
                      align=_attr(chain, "algn", "l"), indent_emu=int(_attr(chain, "marL", "0") or 0),
                      first_indent_emu=int(_attr(chain, "indent", "0") or 0))
        if bu is not None:
            n = localname(bu)
            rec.bullet_char = bu.get("char") if n == "buChar" else ("•" if n == "buBlip" else None)
            rec.bullet_auto = n == "buAutoNum"
        if ppr is not None:
            rec.space_before_pt = _spacing_pt(ppr.find("a:spcBef", NS), runs)
            rec.space_after_pt = _spacing_pt(ppr.find("a:spcAft", NS), runs)
            ln = ppr.find("a:lnSpc/a:spcPct", NS)
            if ln is not None and ln.get("val"):
                rec.line_spacing = int(ln.get("val")) / 100000
        out.append(rec)
    return out


def para_props_chain(ctx: PartCtx, ppr: etree._Element | None, sp: etree._Element | None,
                     lvl: int) -> list[etree._Element]:
    """Носители свойств абзаца по убыванию приоритета: pPr → lstStyle фигуры → плейсхолдер лейаута/мастера
    → bodyStyle мастера (только для плейсхолдеров-не-заголовков — заголовки маркеров не наследуют)."""
    chain: list[etree._Element] = []
    if ppr is not None:
        chain.append(ppr)
    if sp is None:
        return chain
    lvl_tag = f"a:lvl{lvl + 1}pPr"
    lst = sp.find("p:txBody/a:lstStyle", NS)
    if lst is not None and (d := lst.find(lvl_tag, NS)) is not None:
        chain.append(d)
    ph = placeholder(sp)
    if ph is None:
        return chain
    for root in (ctx.layout, ctx.master):
        target = ctx.find_placeholder(root, ph)
        if target is not None and (lst := target.find("p:txBody/a:lstStyle", NS)) is not None:
            if (d := lst.find(lvl_tag, NS)) is not None:
                chain.append(d)
    tx = ctx.master.find("p:txStyles", NS)
    if tx is not None:
        # заголовки → titleStyle, тело/подзаголовок → bodyStyle, колонтитулы/номер/дата и прочее → otherStyle
        style = ("titleStyle" if ph[0] in ("title", "ctrTitle")
                 else "bodyStyle" if ph[0] in ("body", "obj", "subTitle") else "otherStyle")
        if (d := tx.find(f"p:{style}/{lvl_tag}", NS)) is not None:
            if style == "titleStyle" or ph[0] == "subTitle":
                d = _without_bullets(d)
            chain.append(d)
    return chain


def _without_bullets(ppr: etree._Element) -> etree._Element:
    """Копия lvlNpPr без маркеров: subTitle/title наследуют выравнивание от titleStyle, но не буллеты."""
    clone = etree.Element(ppr.tag, attrib=dict(ppr.attrib))
    for c in ppr:
        if not localname(c).startswith("bu"):
            clone.append(copy.deepcopy(c))  # deepcopy: append переносит элемент из кэшированного дерева мастера
    return clone


def _bullet_el(chain: list[etree._Element]) -> etree._Element | None:
    for ppr in chain:
        for c in ppr:
            n = localname(c)
            if n in ("buNone", "buChar", "buAutoNum", "buBlip"):
                return c
    return None


def _attr(chain: list[etree._Element], name: str, default: str) -> str:
    for el in chain:
        if el.get(name):
            return el.get(name)  # type: ignore[return-value]
    return default


def _flag(chain: list[etree._Element], name: str) -> bool:
    for el in chain:
        if el.get(name) is not None:
            return el.get(name) in ("1", "true")
    return False


def _underline(chain: list[etree._Element]) -> bool:
    for el in chain:
        if el.get("u") is not None:
            return el.get("u") != "none"
    return False


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


# ── картинка ──


def read_picture(ctx: PartCtx, blip_fill: etree._Element) -> PictureRec | None:
    blip = blip_fill.find("a:blip", NS)
    if blip is None:
        return None
    rid = blip.get(R + "embed")
    if not rid:
        return None
    target = ctx.pkg.rel_by_id(ctx.part, rid)
    if not target or target not in ctx.pkg.names:
        return None
    sr = blip_fill.find("a:srcRect", NS)
    rect = tuple(int(sr.get(k, 0)) for k in ("l", "t", "r", "b")) if sr is not None else (0, 0, 0, 0)
    try:
        with Image.open(io.BytesIO(ctx.pkg.zip.read(target))) as im:
            w, h = im.size
    except Exception:  # noqa: BLE001 — svg/emf и т. п. Pillow не открывает; пропорции не проверяем
        return PictureRec(px_w=0, px_h=0, src_rect=rect, part=target)  # type: ignore[arg-type]
    return PictureRec(px_w=w, px_h=h, src_rect=rect, part=target)  # type: ignore[arg-type]


# ── диаграмма ──


def read_chart(ctx: PartCtx, sp: etree._Element) -> ChartRec | None:
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
    kind_el = next((c for c in plot if localname(c).endswith("Chart")), None)
    kind = localname(kind_el) if kind_el is not None else "unknown"
    series = plot.findall(f".//{C}ser")
    colors: list[str] = []
    for ser in series:
        # цвет серии — c:ser/c:spPr; у pie/doughnut цвета по точкам — c:dPt/c:spPr (так пишет render/charts.py)
        holders = [ser.find(f"{C}spPr")] + [dpt.find(f"{C}spPr") for dpt in ser.findall(f"{C}dPt")]
        for sppr in holders:
            if sppr is None:
                continue
            for fill in (sppr.find("a:solidFill", NS), sppr.find("a:ln/a:solidFill", NS)):
                if fill is not None:
                    c = ctx.color_of(next(iter(fill), None))
                    if c:
                        colors.append(c)
                        break
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
        spec=_chart_spec(root, kind_el, series),
    )


def _chart_spec(root: etree._Element, kind_el: etree._Element | None, series: list[etree._Element]) -> ChartSpec | None:
    """Категории и значения из кэшей c:cat / c:val; тип — по элементу *Chart и barDir."""
    if kind_el is None:
        return None
    name = localname(kind_el)
    if name == "barChart":
        bar_dir = kind_el.find(f"{C}barDir")
        kind = "bar" if bar_dir is not None and bar_dir.get("val") == "bar" else "column"
    elif name in CHART_KINDS:
        kind = CHART_KINDS[name]
    else:
        return None
    categories: list[str] = []
    data: dict[str, list[float]] = {}
    for i, ser in enumerate(series):
        name_pts = _cache_points(ser.find(f"{C}tx"))
        ser_name = name_pts[0] if name_pts else f"Серия {i + 1}"
        cats = _cache_points(ser.find(f"{C}cat"))
        if len(cats) > len(categories):
            categories = cats
        vals: list[float] = []
        for v in _cache_points(ser.find(f"{C}val")):
            try:
                vals.append(float(v))
            except ValueError:
                vals.append(0.0)
        data[ser_name] = vals
    if not data:
        return None
    n = max(len(categories), max(len(v) for v in data.values()))
    categories = categories + [str(i + 1) for i in range(len(categories), n)]
    for k, v in data.items():
        data[k] = v + [0.0] * (n - len(v))
    chart = root.find(f"{C}chart")
    title = _title_text(chart.find(f"{C}title") if chart is not None else None)
    cat_ax = root.find(f".//{C}catAx")
    if cat_ax is None:
        cat_ax = root.find(f".//{C}dateAx")
    val_ax = root.find(f".//{C}valAx")
    return ChartSpec(kind=kind, title=title, categories=categories, series=data,
                     x_label=_title_text(cat_ax.find(f"{C}title")) or None if cat_ax is not None else None,
                     y_label=_title_text(val_ax.find(f"{C}title")) or None if val_ax is not None else None)


def _title_text(title_el: etree._Element | None) -> str:
    return "".join(t.text or "" for t in title_el.iter(A + "t")) if title_el is not None else ""


def _cache_points(holder: etree._Element | None) -> list[str]:
    """c:tx / c:cat / c:val → значения из strCache/numCache в порядке idx (или литералы strLit/numLit)."""
    if holder is None:
        return []
    pts: list[tuple[int, str]] = []
    for pt in holder.iter(f"{C}pt"):
        v = pt.find(f"{C}v")
        pts.append((int(pt.get("idx", len(pts))), (v.text or "") if v is not None else ""))
    pts.sort()
    return [v for _, v in pts]


def _show_val(dlbls: etree._Element) -> bool:
    for tag in ("showVal", "showPercent", "showCatName"):
        el = dlbls.find(f"{C}{tag}")
        if el is not None and el.get("val") in ("1", "true"):
            return True
    return False


# ── таблица ──


def read_table(ctx: PartCtx, sp: etree._Element, rec: ShapeRec) -> TableRec | None:
    tbl = sp.find(".//a:tbl", NS)
    if tbl is None:
        return None
    rows = tbl.findall("a:tr", NS)
    cols = tbl.findall("a:tblGrid/a:gridCol", NS)
    fills: list[str] = []
    cells: list[list[CellRec]] = []
    for tr in rows:
        row: list[CellRec] = []
        for tc in tr.findall("a:tc", NS):
            tc_pr = tc.find("a:tcPr", NS)
            fill = ctx.fill_of(tc_pr) or []
            fills.extend(h for h, _ in fill)
            row.append(CellRec(
                paragraphs=read_paragraphs(ctx, tc.findall("a:txBody/a:p", NS), None),
                fill=fill[0][0] if fill else None,
                row_span=int(tc.get("rowSpan", "1") or 1), col_span=int(tc.get("gridSpan", "1") or 1),
                merged=tc.get("hMerge") in ("1", "true") or tc.get("vMerge") in ("1", "true"),
            ))
        cells.append(row)
    rec.paragraphs = [p for row in cells for c in row for p in c.paragraphs]
    rec.text = "\n".join(p.text for p in rec.paragraphs)
    return TableRec(rows=len(rows), cols=len(cols), fills=fills, cells=cells,
                    col_widths_emu=[int(c.get("w", "0") or 0) for c in cols],
                    row_heights_emu=[int(r.get("h", "0") or 0) for r in rows])


# ── вспомогательное ──


def inherited_bbox(ctx: PartCtx, sp: etree._Element) -> tuple[int, int, int, int] | None:
    """Геометрия плейсхолдера без xfrm — из лейаута/мастера."""
    ph = placeholder(sp)
    if ph is None:
        return None
    for root in (ctx.layout, ctx.master):
        target = ctx.find_placeholder(root, ph)
        if target is not None and (bb := absolute_bbox(target)) is not None:
            return bb
    return None


__all__ = ["C", "C_NS", "CellRec", "ChartRec", "DEFAULT_INSETS", "ParaRec", "PictureRec", "RunRec", "SYMBOL_FONTS",
           "ShapeRec", "TableRec", "background_picture_part", "inherited_bbox", "para_props_chain", "read_chart",
           "read_paragraphs", "read_picture", "read_shape", "read_shapes", "read_table"]
