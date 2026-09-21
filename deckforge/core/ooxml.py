"""Чистые OOXML-хелперы над lxml: пространства имён, геометрия и идентификация фигур."""

from __future__ import annotations

from lxml import etree

NS = {
    "a": "http://schemas.openxmlformats.org/drawingml/2006/main",
    "p": "http://schemas.openxmlformats.org/presentationml/2006/main",
    "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
    "rel": "http://schemas.openxmlformats.org/package/2006/relationships",
}
A = f"{{{NS['a']}}}"
P = f"{{{NS['p']}}}"
R = f"{{{NS['r']}}}"


def localname(el: etree._Element) -> str:
    return etree.QName(el).localname


def bbox(sp: etree._Element) -> tuple[int, int, int, int] | None:
    """(x, y, cx, cy) в EMU по первому a:xfrm фигуры; None, если геометрии нет (наследуется)."""
    xfrm = sp.find("p:spPr/a:xfrm", NS)
    if xfrm is None:
        xfrm = sp.find("p:grpSpPr/a:xfrm", NS)
    if xfrm is None:
        xfrm = sp.find("p:xfrm", NS)  # graphicFrame
    if xfrm is None:
        return None
    off, ext = xfrm.find("a:off", NS), xfrm.find("a:ext", NS)
    if off is None or ext is None:
        return None
    return int(off.get("x", 0)), int(off.get("y", 0)), int(ext.get("cx", 0)), int(ext.get("cy", 0))


def absolute_bbox(sp: etree._Element) -> tuple[int, int, int, int] | None:
    """bbox фигуры в координатах слайда с учётом вложенности в группы (chOff/chExt → off/ext)."""
    bb = bbox(sp)
    if bb is None:
        return None
    x, y, w, h = bb
    parent = sp.getparent()
    while parent is not None:
        if parent.tag == P + "grpSp":
            xfrm = parent.find("p:grpSpPr/a:xfrm", NS)
            if xfrm is not None:
                off, ext = xfrm.find("a:off", NS), xfrm.find("a:ext", NS)
                ch_off, ch_ext = xfrm.find("a:chOff", NS), xfrm.find("a:chExt", NS)
                if off is not None and ext is not None and ch_off is not None and ch_ext is not None:
                    cw, chh = int(ch_ext.get("cx", 0)), int(ch_ext.get("cy", 0))
                    sx = int(ext.get("cx", 0)) / cw if cw else 1.0
                    sy = int(ext.get("cy", 0)) / chh if chh else 1.0
                    x = int(off.get("x", 0)) + (x - int(ch_off.get("x", 0))) * sx
                    y = int(off.get("y", 0)) + (y - int(ch_off.get("y", 0))) * sy
                    w, h = w * sx, h * sy
        parent = parent.getparent()
    return int(x), int(y), int(w), int(h)


def shape_id(sp: etree._Element) -> str:
    """p:cNvPr/@id — уникален в пределах слайда; используется как Slot.id."""
    for tag in ("p:nvSpPr", "p:nvPicPr", "p:nvGraphicFramePr", "p:nvCxnSpPr", "p:nvGrpSpPr"):
        nv = sp.find(f"{tag}/p:cNvPr", NS)
        if nv is not None:
            return nv.get("id", "")
    return ""


def shape_name(sp: etree._Element) -> str:
    for tag in ("p:nvSpPr", "p:nvPicPr", "p:nvGraphicFramePr", "p:nvCxnSpPr", "p:nvGrpSpPr"):
        nv = sp.find(f"{tag}/p:cNvPr", NS)
        if nv is not None:
            return nv.get("name", "")
    return ""


def graphic_kind(sp: etree._Element) -> str:
    """Для p:graphicFrame: chart | table | diagram | ole | graphic (по uri a:graphicData)."""
    gd = sp.find(".//a:graphicData", NS)
    uri = (gd.get("uri", "") if gd is not None else "") or ""
    for key in ("chart", "table", "diagram", "ole"):
        if key in uri:
            return key
    return "graphic"


def placeholder(sp: etree._Element) -> tuple[str, str | None] | None:
    """(type, idx) плейсхолдера или None."""
    ph = sp.find("p:nvSpPr/p:nvPr/p:ph", NS)
    if ph is None:
        ph = sp.find("p:nvPicPr/p:nvPr/p:ph", NS)
    if ph is None:
        ph = sp.find("p:nvGraphicFramePr/p:nvPr/p:ph", NS)
    if ph is None:
        return None
    return ph.get("type", "body"), ph.get("idx")


def shape_text(sp: etree._Element) -> str:
    return "".join(t.text or "" for t in sp.iter(A + "t"))


def iter_shapes(tree: etree._Element | None):
    """Все p:sp / p:pic / p:cxnSp / p:graphicFrame, включая вложенные в группы."""
    if tree is None:
        return
    for el in tree.iter(P + "sp", P + "pic", P + "cxnSp", P + "graphicFrame"):
        yield el
