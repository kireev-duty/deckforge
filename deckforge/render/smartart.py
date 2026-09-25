"""Шаги процесса настоящим SmartArt «Простой процесс» (process1): узлы с текстом, стрелки между ними.

Пять частей, как их пишет сам PowerPoint:
- данные (`dgm:dataModel`) — узлы шагов со своим оформлением + презентационные точки макета;
- определение макета, быстрый стиль и цвета — заготовки `assets/smartart/`, снятые с PowerPoint
  (`tools/make_smartart_assets.py`);
- готовая отрисовка (`dsp:drawing`) — её рисуют LibreOffice (PDF, PNG, судья) и HTML-экспорт.

PowerPoint при правке пересчитывает раскладку по макету, поэтому геометрия отрисовки повторяет правила
layoutDef process1 (`layout_steps`, сверено с отрисовкой PowerPoint — `tests/fixtures/smartart_process1.json`).
Цвета и шрифт — шаблона, явно: и в данных (пользовательское оформление узла, `custT`), и в отрисовке.
Тема шаблона часто стандартная Office (ARCHITECTURE, решение 1), и схемные цвета дали бы чужой синий.

style_overrides: font, size_pt, text_color, palette_text, accent, palette ("FF0053,520977,…"), type_scale.
"""

from __future__ import annotations

import uuid
from importlib.resources import files
from xml.sax.saxutils import escape

from lxml import etree
from pptx.opc.package import Part
from pptx.opc.packuri import PackURI
from pptx.slide import Slide

from deckforge.core.colors import apply_color_mods
from deckforge.core.ir import Box, DiagramSpec
from deckforge.render.charts import palette_from
from deckforge.render.diagrams import DARK, LIGHT, lines_needed, on_fill, snap_size, type_scale

LAYOUT_URN = "urn:microsoft.com/office/officeart/2005/8/layout/process1"
QUICKSTYLE_URN = "urn:microsoft.com/office/officeart/2005/8/quickstyle/simple1"
COLORS_URN = "urn:microsoft.com/office/officeart/2005/8/colors/accent1_2"
ASSETS = files("deckforge.render") / "assets" / "smartart"
PARTS = {  # роль → (заготовка или None, content type, тип связи, шаблон имени части)
    "dm": (None, "application/vnd.openxmlformats-officedocument.drawingml.diagramData+xml",
           "http://schemas.openxmlformats.org/officeDocument/2006/relationships/diagramData", "/ppt/diagrams/data%d.xml"),
    "lo": ("process1.layout.xml", "application/vnd.openxmlformats-officedocument.drawingml.diagramLayout+xml",
           "http://schemas.openxmlformats.org/officeDocument/2006/relationships/diagramLayout",
           "/ppt/diagrams/layout%d.xml"),
    "qs": ("simple1.quickStyle.xml", "application/vnd.openxmlformats-officedocument.drawingml.diagramStyle+xml",
           "http://schemas.openxmlformats.org/officeDocument/2006/relationships/diagramQuickStyle",
           "/ppt/diagrams/quickStyle%d.xml"),
    "cs": ("accent1_2.colors.xml", "application/vnd.openxmlformats-officedocument.drawingml.diagramColors+xml",
           "http://schemas.openxmlformats.org/officeDocument/2006/relationships/diagramColors",
           "/ppt/diagrams/colors%d.xml"),
    "dr": (None, "application/vnd.ms-office.drawingml.diagramDrawing+xml",
           "http://schemas.microsoft.com/office/2007/relationships/diagramDrawing", "/ppt/diagrams/drawing%d.xml"),
}
DIAGRAM_DRAWING_RT = PARTS["dr"][2]
NS_DGM = "http://schemas.openxmlformats.org/drawingml/2006/diagram"
NS_DSP = "http://schemas.microsoft.com/office/drawing/2008/diagram"
NS_A = "http://schemas.openxmlformats.org/drawingml/2006/main"
NS_R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
NS_P = "http://schemas.openxmlformats.org/presentationml/2006/main"

# правила layoutDef process1: узел h = 0,6·w; промежуток (sibTrans) = 0,4·w узла; стрелка — от 0,25 до 0,78
# промежутка (begPad 0,25, endPad 0,22), её высота — 0,62 промежутка; в низком боксе высота урезается до бокса
NODE_ASPECT, GAP_SHARE, ARROW_BEG, ARROW_END, ARROW_ASPECT = 0.6, 0.4, 0.25, 0.22, 0.62
ROUND_ADJ = 0.1  # скругление узла (adj 10000)
MARGIN_SHARE = 0.3  # поле текста узла — 0,3 кегля с каждой стороны
LINE_SPACING = 0.9 * 1.2  # lnSpc 90 %
# ширина знака в долях кегля: с запасом на широкие гарнитуры (Montserrat у ЛЦТ2026) — при 0,62 длинные слова
# рвались посреди («Подключени/е»); на узкой гарнитуре кегль выйдет чуть меньше возможного
CHAR_WIDTH = 0.7
SIZE_MAX_PT, SIZE_MIN_PT = 28.0, 10.0
# правила узла process1: кегль до 18 pt → высота до ×1,5 → кегль дальше вниз (у PowerPoint — до 5 pt, у нас до 10)
GROW_FROM_PT, GROW = 18.0, 1.5
LINE_W = 19050  # белая обводка узла (simple1)
ARROW_TINT = 60000  # стрелки — светлый оттенок акцента (accent1 tint 60 %)
EMU_PER_PT = 12700


def layout_steps(n: int, w: int, h: int, grow: float = 1.0) -> list[tuple[str, Box]]:
    """Фигуры отрисовки относительно рамки w×h: узлы (roundRect) и стрелки (rightArrow) по порядку.

    `grow` — во сколько раз узел выше обычного (правило макета: текст не влез — высота до ×1,5)."""
    if n <= 0:
        return []
    node_w = w / (n + GAP_SHARE * (n - 1))
    node_h = min(NODE_ASPECT * node_w * grow, h)
    gap = GAP_SHARE * node_w
    arrow_w = gap * (1 - ARROW_BEG - ARROW_END)
    arrow_h = min(ARROW_ASPECT * gap, h)
    x0 = (w - (n * node_w + (n - 1) * gap)) / 2
    out: list[tuple[str, Box]] = []
    for i in range(n):
        x = x0 + i * (node_w + gap)
        out.append(("roundRect", Box(x=round(x), y=round((h - node_h) / 2), w=round(node_w), h=round(node_h))))
        if i < n - 1:
            out.append(("rightArrow", Box(x=round(x + node_w + ARROW_BEG * gap), y=round((h - arrow_h) / 2),
                                          w=round(arrow_w), h=round(arrow_h))))
    return out


def fits(texts: list[str], node_w: int, node_h: int, size: float) -> bool:
    """Все шаги влезают в узел w×h с полями при кегле size: по строкам и без разрыва слов."""
    margin = 2 * MARGIN_SHARE * size * EMU_PER_PT
    cpl = max(1, int((node_w - margin) / EMU_PER_PT / (CHAR_WIDTH * size)))
    lines = max(1, int((node_h - margin) / EMU_PER_PT / (LINE_SPACING * size)))
    return all(lines_needed(t, cpl) <= lines and max(map(len, t.split()), default=0) <= cpl for t in texts)


def fit_size(texts: list[str], n: int, w: int, h: int, start: float = SIZE_MAX_PT) -> tuple[float, float]:
    """Кегль (один на все узлы — primFontSz макета общий) и рост узла, как их подбирает правило макета:
    кегль до GROW_FROM_PT → высота узла до ×1,5 → кегль до SIZE_MIN_PT."""
    top = max(SIZE_MIN_PT, min(start, SIZE_MAX_PT))
    sizes = [top - i for i in range(int(top - SIZE_MIN_PT) + 1)]
    first = [s for s in sizes if s >= GROW_FROM_PT] or sizes[:1]
    for grow, candidates in ((1.0, first), (GROW, [s for s in sizes if s <= first[-1]])):
        node = next(b for kind, b in layout_steps(n, w, h, grow) if kind == "roundRect")
        for size in candidates:
            if fits(texts, node.w, node.h, size):
                return size, grow
    return SIZE_MIN_PT, GROW


def add_smartart(slide: Slide, spec: DiagramSpec, box: Box, overrides: dict | None = None,
                 name: str = "Шаги процесса") -> etree._Element | None:
    """SmartArt в боксе: пять частей со связями от слайда + graphicFrame в spTree. Возвращает graphicFrame."""
    overrides = overrides or {}
    items = [t for t in spec.items if t.strip()]
    n = len(items)
    if n == 0:
        return None
    size, grow = fit_size(items, n, box.w, box.h,
                          float(overrides["size_pt"]) if overrides.get("size_pt") else SIZE_MAX_PT)
    size = snap_size(size, type_scale(overrides), SIZE_MIN_PT)
    geometry = layout_steps(n, box.w, box.h, grow)
    dark = str(overrides.get("palette_text") or overrides.get("text_color") or DARK).lstrip("#").upper()
    if dark == LIGHT:
        dark = DARK
    colors = palette_from(overrides, n)
    fills = [colors[i % len(colors)].lstrip("#").upper() for i in range(n)]
    style = _Style(font=str(overrides["font"]) if overrides.get("font") else None, size_pt=size,
                   fills=fills, texts=[on_fill(f, dark) for f in fills],
                   arrow=apply_color_mods(fills[0], {"tint": ARROW_TINT}).lstrip("#").upper())

    package = slide.part.package
    rids: dict[str, str] = {}
    parts: dict[str, Part] = {}
    for role, (asset, content_type, reltype, tmpl) in PARTS.items():
        blob = (ASSETS / asset).read_bytes() if asset else b""
        part = Part(PackURI(package.next_partname(tmpl)), content_type, package, blob)
        parts[role] = part
        rids[role] = slide.part.relate_to(part, reltype)
    ids = _Ids(str(parts["dm"].partname))
    parts["dr"]._blob = _drawing_xml(items, geometry, ids, style)
    parts["dm"]._blob = _data_xml(items, ids, style, rids["dr"])

    frame = etree.fromstring(
        f'<p:graphicFrame xmlns:p="{NS_P}" xmlns:a="{NS_A}" xmlns:r="{NS_R}">'
        f'<p:nvGraphicFramePr><p:cNvPr id="{slide.shapes._next_shape_id}" name="{escape(name)}"/>'
        f"<p:cNvGraphicFramePr/><p:nvPr/></p:nvGraphicFramePr>"
        f'<p:xfrm><a:off x="{box.x}" y="{box.y}"/><a:ext cx="{box.w}" cy="{box.h}"/></p:xfrm>'
        f'<a:graphic><a:graphicData uri="{NS_DGM}"><dgm:relIds xmlns:dgm="{NS_DGM}" '
        f'r:dm="{rids["dm"]}" r:lo="{rids["lo"]}" r:qs="{rids["qs"]}" r:cs="{rids["cs"]}"/>'
        f"</a:graphicData></a:graphic></p:graphicFrame>")
    slide.shapes._spTree.insert_element_before(frame, "p:extLst")
    return frame


# ──────────────────────────── части ────────────────────────────


class _Style:
    def __init__(self, font: str | None, size_pt: float, fills: list[str], texts: list[str], arrow: str) -> None:
        self.font, self.size_pt, self.fills, self.texts, self.arrow = font, size_pt, fills, texts, arrow

    def rpr(self, i: int) -> str:
        latin = f'<a:latin typeface="{escape(self.font, {chr(34): "&quot;"})}"/>' if self.font else ""
        return (f'<a:rPr lang="ru-RU" sz="{round(self.size_pt * 100)}">'
                f'<a:solidFill><a:srgbClr val="{self.texts[i]}"/></a:solidFill>{latin}</a:rPr>')


class _Ids:
    """modelId точек и связей: детерминированные GUID от части и роли — один IR даёт один и тот же файл."""

    def __init__(self, seed: str) -> None:
        self.seed = seed

    def __call__(self, *key: object) -> str:
        return "{" + str(uuid.uuid5(uuid.NAMESPACE_URL, f"deckforge:{self.seed}:" + ":".join(map(str, key)))).upper() + "}"


def _data_xml(items: list[str], ids: _Ids, style: _Style, drawing_rid: str) -> bytes:
    """dgm:dataModel как у PowerPoint: doc, по шагу node + parTrans + sibTrans, презентационные точки макета."""
    n = len(items)
    pts = [f'<dgm:pt modelId="{ids("doc")}" type="doc"><dgm:prSet loTypeId="{LAYOUT_URN}" loCatId="process" '
           f'qsTypeId="{QUICKSTYLE_URN}" qsCatId="simple" csTypeId="{COLORS_URN}" csCatId="accent1" phldr="1"/>'
           f"<dgm:spPr/></dgm:pt>"]
    cxns: list[str] = []
    empty_t = '<dgm:t><a:bodyPr/><a:lstStyle/><a:p><a:endParaRPr lang="ru-RU"/></a:p></dgm:t>'
    for i, text in enumerate(items):
        pts.append(f'<dgm:pt modelId="{ids("node", i)}"><dgm:prSet phldrT="[Текст]" custT="1"/>'
                   f'<dgm:spPr><a:solidFill><a:srgbClr val="{style.fills[i]}"/></a:solidFill></dgm:spPr>'
                   f"<dgm:t><a:bodyPr/><a:lstStyle/><a:p><a:r>{style.rpr(i)}<a:t>{escape(text)}</a:t></a:r></a:p>"
                   f"</dgm:t></dgm:pt>")
        pts.append(f'<dgm:pt modelId="{ids("par", i)}" type="parTrans" cxnId="{ids("cxn", i)}">'
                   f"<dgm:prSet/><dgm:spPr/>{empty_t}</dgm:pt>")
        # стрелка после шага — пользовательская заливка точки sibTrans (так её хранит PowerPoint)
        arrow_fill = (f'<dgm:spPr><a:solidFill><a:srgbClr val="{style.arrow}"/></a:solidFill></dgm:spPr>'
                      if i < n - 1 else "<dgm:spPr/>")
        pts.append(f'<dgm:pt modelId="{ids("sib", i)}" type="sibTrans" cxnId="{ids("cxn", i)}">'
                   f"<dgm:prSet/>{arrow_fill}{empty_t}</dgm:pt>")
        cxns.append(f'<dgm:cxn modelId="{ids("cxn", i)}" srcId="{ids("doc")}" destId="{ids("node", i)}" '
                    f'srcOrd="{i}" destOrd="0" parTransId="{ids("par", i)}" sibTransId="{ids("sib", i)}"/>')
    # презентационные точки: корень макета, узлы, промежутки со стрелкой и подписью соединителя
    pts.append(f'<dgm:pt modelId="{ids("pres", "root")}" type="pres"><dgm:prSet presAssocID="{ids("doc")}" '
               f'presName="Name0" presStyleCnt="0"><dgm:presLayoutVars><dgm:dir/><dgm:resizeHandles val="exact"/>'
               f"</dgm:presLayoutVars></dgm:prSet><dgm:spPr/></dgm:pt>")
    cxns.append(_pres_of(ids, ids("doc"), ids("pres", "root"), 0, ("presOf", "root")))
    order = 0
    for i in range(n):
        pts.append(f'<dgm:pt modelId="{ids("pres", "node", i)}" type="pres"><dgm:prSet presAssocID="{ids("node", i)}" '
                   f'presName="node" presStyleLbl="node1" presStyleIdx="{i}" presStyleCnt="{n}"><dgm:presLayoutVars>'
                   f'<dgm:bulletEnabled val="1"/></dgm:presLayoutVars></dgm:prSet><dgm:spPr/></dgm:pt>')
        cxns.append(_pres_of(ids, ids("node", i), ids("pres", "node", i), 0, ("presOf", "node", i)))
        cxns.append(_pres_par_of(ids, ids("pres", "root"), ids("pres", "node", i), order, ("presParOf", "node", i)))
        order += 1
        if i == n - 1:
            continue
        for kind, src_ord in (("sibTrans", 0), ("connectorText", 1)):
            pts.append(f'<dgm:pt modelId="{ids("pres", kind, i)}" type="pres"><dgm:prSet presAssocID="{ids("sib", i)}" '
                       f'presName="{kind}" presStyleLbl="sibTrans2D1" presStyleIdx="{i}" presStyleCnt="{n - 1}"/>'
                       f"<dgm:spPr/></dgm:pt>")
            cxns.append(_pres_of(ids, ids("sib", i), ids("pres", kind, i), src_ord, ("presOf", kind, i)))
        cxns.append(_pres_par_of(ids, ids("pres", "root"), ids("pres", "sibTrans", i), order, ("presParOf", "sib", i)))
        cxns.append(_pres_par_of(ids, ids("pres", "sibTrans", i), ids("pres", "connectorText", i), 0,
                                 ("presParOf", "conn", i)))
        order += 1
    xml = (f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
           f'<dgm:dataModel xmlns:dgm="{NS_DGM}" xmlns:a="{NS_A}"><dgm:ptLst>{"".join(pts)}</dgm:ptLst>'
           f'<dgm:cxnLst>{"".join(cxns)}</dgm:cxnLst><dgm:bg/><dgm:whole/>'
           f'<dgm:extLst><a:ext uri="http://schemas.microsoft.com/office/drawing/2008/diagram">'
           f'<dsp:dataModelExt xmlns:dsp="{NS_DSP}" relId="{drawing_rid}" minVer="{NS_DGM}"/></a:ext></dgm:extLst>'
           f"</dgm:dataModel>")
    return xml.encode("utf-8")


def _pres_of(ids: _Ids, src: str, dest: str, src_ord: int, key: tuple) -> str:
    return (f'<dgm:cxn modelId="{ids(*key)}" type="presOf" srcId="{src}" destId="{dest}" srcOrd="{src_ord}" '
            f'destOrd="0" presId="{LAYOUT_URN}"/>')


def _pres_par_of(ids: _Ids, src: str, dest: str, src_ord: int, key: tuple) -> str:
    return (f'<dgm:cxn modelId="{ids(*key)}" type="presParOf" srcId="{src}" destId="{dest}" srcOrd="{src_ord}" '
            f'destOrd="0" presId="{LAYOUT_URN}"/>')


def _drawing_xml(items: list[str], geometry: list[tuple[str, Box]], ids: _Ids, style: _Style) -> bytes:
    """dsp:drawing — готовая отрисовка в координатах рамки (как у PowerPoint: без смещения на бокс)."""
    shapes: list[str] = []
    node = 0
    for kind, b in geometry:
        if kind == "roundRect":
            i = node
            node += 1
            inset = round(ROUND_ADJ * min(b.w, b.h) * 0.2929)  # текстовая область скруглённого прямоугольника
            margin = round(MARGIN_SHARE * style.size_pt * EMU_PER_PT)
            shapes.append(
                f'<dsp:sp modelId="{ids("pres", "node", i)}"><dsp:nvSpPr><dsp:cNvPr id="0" name=""/><dsp:cNvSpPr/>'
                f"</dsp:nvSpPr><dsp:spPr>{_xfrm(b)}"
                f'<a:prstGeom prst="roundRect"><a:avLst><a:gd name="adj" fmla="val {round(ROUND_ADJ * 100000)}"/>'
                f'</a:avLst></a:prstGeom><a:solidFill><a:srgbClr val="{style.fills[i]}"/></a:solidFill>'
                f'<a:ln w="{LINE_W}"><a:solidFill><a:srgbClr val="{LIGHT}"/></a:solidFill></a:ln></dsp:spPr>'
                f"{_dsp_style(line=2, font_color=style.texts[i])}"
                f'<dsp:txBody><a:bodyPr wrap="square" lIns="{margin}" tIns="{margin}" rIns="{margin}" bIns="{margin}" '
                f'anchor="ctr" anchorCtr="0"><a:noAutofit/></a:bodyPr><a:lstStyle/><a:p><a:pPr algn="ctr">'
                f'<a:lnSpc><a:spcPct val="90000"/></a:lnSpc><a:buNone/></a:pPr><a:r>{style.rpr(i)}'
                f"<a:t>{escape(items[i])}</a:t></a:r></a:p></dsp:txBody>"
                f"<dsp:txXfrm>{_off_ext(b.x + inset, b.y + inset, b.w - 2 * inset, b.h - 2 * inset)}</dsp:txXfrm>"
                f"</dsp:sp>")
        else:
            i = node - 1
            shapes.append(
                f'<dsp:sp modelId="{ids("pres", "sibTrans", i)}"><dsp:nvSpPr><dsp:cNvPr id="0" name=""/><dsp:cNvSpPr/>'
                f"</dsp:nvSpPr><dsp:spPr>{_xfrm(b)}"
                f'<a:prstGeom prst="rightArrow"><a:avLst><a:gd name="adj1" fmla="val 60000"/>'
                f'<a:gd name="adj2" fmla="val 50000"/></a:avLst></a:prstGeom>'
                f'<a:solidFill><a:srgbClr val="{style.arrow}"/></a:solidFill><a:ln><a:noFill/></a:ln></dsp:spPr>'
                f"{_dsp_style(line=0)}"
                f'<dsp:txBody><a:bodyPr wrap="square" lIns="0" tIns="0" rIns="0" bIns="0" anchor="ctr"><a:noAutofit/>'
                f'</a:bodyPr><a:lstStyle/><a:p><a:endParaRPr lang="ru-RU"/></a:p></dsp:txBody>'
                f"<dsp:txXfrm>{_off_ext(b.x, b.y + round(0.2 * b.h), round(0.7 * b.w), round(0.6 * b.h))}</dsp:txXfrm>"
                f"</dsp:sp>")
    xml = (f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
           f'<dsp:drawing xmlns:dgm="{NS_DGM}" xmlns:dsp="{NS_DSP}" xmlns:a="{NS_A}"><dsp:spTree>'
           f'<dsp:nvGrpSpPr><dsp:cNvPr id="0" name=""/><dsp:cNvGrpSpPr/></dsp:nvGrpSpPr><dsp:grpSpPr/>'
           f'{"".join(shapes)}</dsp:spTree></dsp:drawing>')
    return xml.encode("utf-8")


def _dsp_style(line: int, font_color: str | None = None) -> str:
    """Ссылки на быстрый стиль, как у PowerPoint: без них он при открытии собирает фигуру без обводки и заливки.

    Цвет в fontRef — цвет текста узла: LibreOffice берёт цвет текста отрисовки отсюда, а не из a:rPr."""
    zero = '<a:scrgbClr r="0" g="0" b="0"/>'
    font = f'<a:srgbClr val="{font_color}"/>' if font_color else '<a:schemeClr val="lt1"/>'
    return (f'<dsp:style><a:lnRef idx="{line}">{zero}</a:lnRef><a:fillRef idx="1">{zero}</a:fillRef>'
            f'<a:effectRef idx="0">{zero}</a:effectRef><a:fontRef idx="minor">{font}</a:fontRef></dsp:style>')


def _xfrm(b: Box) -> str:
    return f"<a:xfrm>{_off_ext(b.x, b.y, b.w, b.h)}</a:xfrm>"


def _off_ext(x: int, y: int, w: int, h: int) -> str:
    return f'<a:off x="{x}" y="{y}"/><a:ext cx="{max(0, w)}" cy="{max(0, h)}"/>'


__all__ = ["DIAGRAM_DRAWING_RT", "LAYOUT_URN", "add_smartart", "fit_size", "layout_steps"]
