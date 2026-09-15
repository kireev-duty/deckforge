"""Низкоуровневое чтение .pptx через lxml: части, связи, тема, цепочки наследования.

Это общий фундамент для всех модулей parsing/ (токены, классификатор архетипов, образцы).
Ничего не знает о TemplateDNA — только OOXML.

Ключевая идея: атрибуты текста (шрифт, кегль, цвет) редко заданы на самом run'е —
они наследуются: run → a:pPr/a:defRPr → lstStyle фигуры → плейсхолдер лейаута →
плейсхолдер мастера → p:txStyles мастера → тема. Здесь эта цепочка собирается явно.
"""

from __future__ import annotations

import posixpath
import re
import zipfile
from dataclasses import dataclass, field
from functools import cached_property
from pathlib import Path

from lxml import etree

from deckforge.core.colors import apply_color_mods, normalize_hex

NS = {
    "a": "http://schemas.openxmlformats.org/drawingml/2006/main",
    "p": "http://schemas.openxmlformats.org/presentationml/2006/main",
    "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
    "rel": "http://schemas.openxmlformats.org/package/2006/relationships",
}
A = f"{{{NS['a']}}}"
P = f"{{{NS['p']}}}"

SLIDE_RE = r"ppt/slides/slide\d+\.xml"
LAYOUT_RE = r"ppt/slideLayouts/slideLayout\d+\.xml"
MASTER_RE = r"ppt/slideMasters/slideMaster\d+\.xml"

# Цвета, для которых в теме есть запись; sysClr/prstClr резолвятся отдельно.
THEME_COLOR_KEYS = ("dk1", "lt1", "dk2", "lt2", "accent1", "accent2", "accent3", "accent4", "accent5", "accent6", "hlink", "folHlink")
DEFAULT_CLR_MAP = {"bg1": "lt1", "tx1": "dk1", "bg2": "lt2", "tx2": "dk2"}

# Ограниченный словарь prstClr — в шаблонах встречаются только базовые.
PRESET_COLORS = {
    "black": "000000", "white": "FFFFFF", "red": "FF0000", "green": "008000", "blue": "0000FF",
    "yellow": "FFFF00", "gray": "808080", "grey": "808080", "darkGray": "A9A9A9", "lightGray": "D3D3D3",
}

FillList = list[tuple[str, float]]  # [(hex, вес)] — для градиента несколько стопов


def _num(name: str) -> int:
    m = re.search(r"(\d+)", name.rsplit("/", 1)[-1])
    return int(m.group(1)) if m else 0


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


@dataclass
class ThemeInfo:
    colors: dict[str, str] = field(default_factory=dict)  # dk1/lt1/accent1... → hex
    major_font: str = ""
    minor_font: str = ""

    def font_ref(self, typeface: str) -> str:
        """'+mj-lt' / '+mn-cs' → гарнитура темы; обычное имя возвращается как есть."""
        if typeface.startswith("+mj"):
            return self.major_font or typeface
        if typeface.startswith("+mn"):
            return self.minor_font or typeface
        return typeface


class Package:
    """Zip-пакет .pptx с кэшем разобранных частей."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.zip = zipfile.ZipFile(self.path)
        self.names = set(self.zip.namelist())
        self._xml: dict[str, etree._Element] = {}
        self._rels: dict[str, dict[str, list[str]]] = {}

    def parts(self, pattern: str) -> list[str]:
        return sorted((n for n in self.names if re.fullmatch(pattern, n)), key=_num)

    def xml(self, name: str) -> etree._Element:
        if name not in self._xml:
            self._xml[name] = etree.fromstring(self.zip.read(name))
        return self._xml[name]

    def rels(self, part: str) -> dict[str, list[str]]:
        """Тип связи (суффикс, напр. 'slideLayout') → абсолютные имена частей."""
        if part not in self._rels:
            d, b = part.rsplit("/", 1)
            rname = f"{d}/_rels/{b}.rels"
            out: dict[str, list[str]] = {}
            if rname in self.names:
                for rel in self.xml(rname):
                    if rel.get("TargetMode") == "External":
                        continue
                    typ = rel.get("Type", "").rsplit("/", 1)[-1]
                    out.setdefault(typ, []).append(posixpath.normpath(posixpath.join(d, rel.get("Target", ""))))
            self._rels[part] = out
        return self._rels[part]

    def rel_by_id(self, part: str, rid: str) -> str | None:
        d, b = part.rsplit("/", 1)
        rname = f"{d}/_rels/{b}.rels"
        if rname not in self.names:
            return None
        for rel in self.xml(rname):
            if rel.get("Id") == rid:
                return posixpath.normpath(posixpath.join(d, rel.get("Target", "")))
        return None

    # ── презентация ──

    @cached_property
    def presentation(self) -> etree._Element:
        return self.xml("ppt/presentation.xml")

    @cached_property
    def slide_size(self) -> tuple[int, int]:
        sz = self.presentation.find("p:sldSz", NS)
        return int(sz.get("cx")), int(sz.get("cy"))

    @cached_property
    def embedded_fonts(self) -> list[str]:
        return [f.get("typeface", "") for f in self.presentation.findall(".//p:embeddedFont/p:font", NS)]

    @cached_property
    def slides(self) -> list[str]:
        """Части слайдов в порядке показа (по p:sldIdLst); fallback — по номеру файла."""
        ordered = []
        for sid in self.presentation.findall("p:sldIdLst/p:sldId", NS):
            target = self.rel_by_id("ppt/presentation.xml", sid.get(f"{{{NS['r']}}}id", ""))
            if target and target in self.names:
                ordered.append(target)
        return ordered or self.parts(SLIDE_RE)

    @cached_property
    def layouts(self) -> list[str]:
        return self.parts(LAYOUT_RE)

    @cached_property
    def masters(self) -> list[str]:
        return self.parts(MASTER_RE)

    def layout_of(self, slide: str) -> str | None:
        return (self.rels(slide).get("slideLayout") or [None])[0]

    def master_of(self, layout: str) -> str | None:
        return (self.rels(layout).get("slideMaster") or [None])[0]

    def theme_part_of(self, master: str) -> str | None:
        return (self.rels(master).get("theme") or [None])[0]

    def theme(self, master: str) -> ThemeInfo:
        tp = self.theme_part_of(master)
        info = ThemeInfo()
        if tp is None or tp not in self.names:
            return info
        t = self.xml(tp)
        scheme = t.find(".//a:clrScheme", NS)
        if scheme is not None:
            for el in scheme:
                c, s = el.find("a:srgbClr", NS), el.find("a:sysClr", NS)
                val = c.get("val") if c is not None else (s.get("lastClr") if s is not None else None)
                if val:
                    info.colors[localname(el)] = normalize_hex(val)
        for key, attr in (("majorFont", "major_font"), ("minorFont", "minor_font")):
            lat = t.find(f".//a:{key}/a:latin", NS)
            if lat is not None and lat.get("typeface"):
                setattr(info, attr, lat.get("typeface"))
        return info


# ──────────────────────────── контекст одной части ────────────────────────────


@dataclass
class PartCtx:
    """Слайд/лейаут/мастер вместе со всем, что нужно для резолва наследования."""

    pkg: Package
    source: str  # "slides" | "layouts" | "master"
    part: str
    root: etree._Element
    layout: etree._Element | None
    master: etree._Element
    theme: ThemeInfo
    clr_map: dict[str, str]

    @classmethod
    def for_slide(cls, pkg: Package, slide: str) -> PartCtx | None:
        lp = pkg.layout_of(slide)
        mp = pkg.master_of(lp) if lp else None
        if lp is None or mp is None:
            return None
        root, layout, master = pkg.xml(slide), pkg.xml(lp), pkg.xml(mp)
        return cls(pkg, "slides", slide, root, layout, master, pkg.theme(mp), _clr_map(master, layout, root))

    @classmethod
    def for_layout(cls, pkg: Package, layout: str) -> PartCtx | None:
        mp = pkg.master_of(layout)
        if mp is None:
            return None
        root, master = pkg.xml(layout), pkg.xml(mp)
        return cls(pkg, "layouts", layout, root, None, master, pkg.theme(mp), _clr_map(master, root))

    @classmethod
    def for_master(cls, pkg: Package, master: str) -> PartCtx:
        root = pkg.xml(master)
        return cls(pkg, "master", master, root, None, root, pkg.theme(master), _clr_map(root))

    @property
    def sp_tree(self) -> etree._Element | None:
        return self.root.find("p:cSld/p:spTree", NS)

    # ── цвета ──

    def color_of(self, clr: etree._Element | None) -> str | None:
        """a:srgbClr / a:schemeClr / a:sysClr / a:prstClr → hex с учётом модификаторов."""
        if clr is None:
            return None
        tag = localname(clr)
        base: str | None
        if tag == "srgbClr":
            base = normalize_hex(clr.get("val", ""))
        elif tag == "schemeClr":
            key = clr.get("val", "")
            key = self.clr_map.get(key, key)
            base = self.theme.colors.get(key)
        elif tag == "sysClr":
            base = normalize_hex(clr.get("lastClr", "FFFFFF"))
        elif tag == "prstClr":
            base = PRESET_COLORS.get(clr.get("val", ""))
        else:
            base = None
        if base is None or len(base) != 6:
            return None
        mods = {localname(m): int(m.get("val", 0)) for m in clr if localname(m) in ("lumMod", "lumOff", "tint", "shade")}
        return apply_color_mods(base, mods) if mods else base

    def fill_of(self, parent: etree._Element | None) -> FillList | None:
        """Заливка по дочерним элементам parent (spPr, rPr, ln, bgPr…).

        None — заливка не задана (наследуется); [] — noFill / картинка / узор.
        """
        if parent is None:
            return None
        for child in parent:
            tag = localname(child)
            if tag == "solidFill":
                c = self.color_of(next(iter(child), None))
                return [(c, 1.0)] if c else []
            if tag == "gradFill":
                stops = [self.color_of(next(iter(gs), None)) for gs in child.iter(A + "gs")]
                stops = [s for s in stops if s]
                return [(s, 1 / len(stops)) for s in stops] if stops else []
            if tag in ("noFill", "blipFill", "pattFill", "grpFill"):
                return []
        return None

    def style_ref_color(self, sp: etree._Element, ref: str) -> FillList | None:
        """p:style/a:{fillRef|lnRef|fontRef} → цвет, если idx > 0 (для fontRef — всегда)."""
        el = sp.find(f"p:style/a:{ref}", NS)
        if el is None:
            return None
        if ref != "fontRef" and int(el.get("idx", "0") or 0) == 0:
            return []
        c = self.color_of(next(iter(el), None))
        return [(c, 1.0)] if c else None

    def shape_fill(self, sp: etree._Element) -> FillList:
        f = self.fill_of(sp.find("p:spPr", NS))
        if f is None:
            f = self.style_ref_color(sp, "fillRef")
        return f or []

    def shape_line(self, sp: etree._Element) -> FillList:
        ln = sp.find("p:spPr/a:ln", NS)
        f = self.fill_of(ln)
        if f is None:
            f = self.style_ref_color(sp, "lnRef")
        return f or []

    def background(self) -> tuple[FillList | None, bool]:
        """Фон части: (цвета, является_ли_картинкой). Наследование slide → layout → master."""
        for root in (self.root, self.layout, self.master):
            if root is None:
                continue
            bg = root.find("p:cSld/p:bg", NS)
            if bg is None:
                continue
            pr = bg.find("p:bgPr", NS)
            if pr is not None:
                is_pic = pr.find("a:blipFill", NS) is not None
                return self.fill_of(pr), is_pic
            ref = bg.find("p:bgRef", NS)
            if ref is not None:
                c = self.color_of(next(iter(ref), None))
                return ([(c, 1.0)] if c else []), False
        return None, False

    # ── наследование текстовых свойств ──

    def find_placeholder(self, root: etree._Element | None, ph: tuple[str, str | None]) -> etree._Element | None:
        """Плейсхолдер в лейауте/мастере: сначала по idx, затем по типу (с учётом синонимов)."""
        if root is None:
            return None
        typ, idx = ph
        by_type: list[etree._Element] = []
        for sp in root.iter(P + "sp"):
            p = placeholder(sp)
            if p is None:
                continue
            if idx is not None and p[1] == idx and idx != "0":
                return sp
            if p[0] == typ or (typ in ("ctrTitle", "title") and p[0] in ("ctrTitle", "title")) or (
                typ in ("subTitle", "obj", "body") and p[0] == "body"
            ):
                by_type.append(sp)
        return by_type[0] if by_type else None

    def run_props_chain(
        self, run: etree._Element, para: etree._Element, sp: etree._Element | None, lvl: int
    ) -> list[etree._Element]:
        """Элементы-носители свойств run'а по убыванию приоритета (rPr, defRPr…)."""
        chain: list[etree._Element] = []
        rpr = run.find("a:rPr", NS)  # у a:r, a:fld и a:br одинаково
        if rpr is not None:
            chain.append(rpr)
        ppr = para.find("a:pPr", NS)
        if ppr is not None and (d := ppr.find("a:defRPr", NS)) is not None:
            chain.append(d)
        lvl_tag = f"a:lvl{lvl + 1}pPr"
        if sp is not None:
            ph = placeholder(sp)
            lst = sp.find("p:txBody/a:lstStyle", NS)
            if lst is not None and (d := lst.find(f"{lvl_tag}/a:defRPr", NS)) is not None:
                chain.append(d)
            if ph is not None:
                # слайд наследует от лейаута и мастера, лейаут — только от мастера, мастер — ни от кого
                parents = {"slides": (self.layout, self.master), "layouts": (self.master,)}.get(self.source, ())
                for root in parents:
                    target = self.find_placeholder(root, ph)
                    if target is not None and (lst := target.find("p:txBody/a:lstStyle", NS)) is not None:
                        if (d := lst.find(f"{lvl_tag}/a:defRPr", NS)) is not None:
                            chain.append(d)
            style = "titleStyle" if ph and ph[0] in ("title", "ctrTitle") else ("bodyStyle" if ph else "otherStyle")
        else:
            style = "otherStyle"  # текст таблиц и т. п.
        tx = self.master.find("p:txStyles", NS)
        if tx is not None:
            for st in (style, "otherStyle"):
                if (d := tx.find(f"p:{st}/{lvl_tag}/a:defRPr", NS)) is not None:
                    chain.append(d)
                if (d := tx.find(f"p:{st}/a:defPPr/a:defRPr", NS)) is not None:
                    chain.append(d)
        return chain

    def resolve_font(self, chain: list[etree._Element], ph: tuple[str, str | None] | None) -> str:
        for el in chain:
            lat = el.find("a:latin", NS)
            if lat is not None and lat.get("typeface"):
                return self.theme.font_ref(lat.get("typeface"))
        return self.theme.major_font if ph and ph[0] in ("title", "ctrTitle") else self.theme.minor_font

    def resolve_size(self, chain: list[etree._Element]) -> float:
        for el in chain:
            if el.get("sz"):
                return int(el.get("sz")) / 100
        return 18.0

    def resolve_bold(self, chain: list[etree._Element]) -> bool:
        for el in chain:
            if el.get("b") is not None:
                return el.get("b") in ("1", "true")
        return False

    def resolve_text_color(self, chain: list[etree._Element], sp: etree._Element | None) -> str | None:
        for el in chain:
            f = self.fill_of(el)
            if f:
                return f[0][0]
        if sp is not None:
            f = self.style_ref_color(sp, "fontRef")
            if f:
                return f[0][0]
        return self.theme.colors.get(self.clr_map.get("tx1", "dk1"))


def _clr_map(master: etree._Element, *overrides: etree._Element | None) -> dict[str, str]:
    """bg1→lt1, tx1→dk1… из мастера, с учётом p:clrMapOvr на лейауте/слайде."""
    cmap = dict(DEFAULT_CLR_MAP)
    m = master.find("p:clrMap", NS)
    if m is not None:
        cmap.update(m.attrib)
    for ov in overrides:
        if ov is None:
            continue
        o = ov.find("p:clrMapOvr/a:overrideClrMapping", NS)
        if o is not None:
            cmap.update(o.attrib)
    return cmap


def iter_shapes(tree: etree._Element | None):
    """Все p:sp / p:pic / p:cxnSp / p:graphicFrame, включая вложенные в группы."""
    if tree is None:
        return
    for el in tree.iter(P + "sp", P + "pic", P + "cxnSp", P + "graphicFrame"):
        yield el


def owner_shape(el: etree._Element) -> etree._Element | None:
    """Ближайший p:sp-предок (для текста в таблицах его нет)."""
    parent = el.getparent()
    while parent is not None:
        if parent.tag == P + "sp":
            return parent
        parent = parent.getparent()
    return None


def font_scale(sp: etree._Element | None) -> float:
    """a:normAutofit/@fontScale — фактическое уменьшение кегля PowerPoint'ом."""
    if sp is None:
        return 1.0
    na = sp.find("p:txBody/a:bodyPr/a:normAutofit", NS)
    if na is None or not na.get("fontScale"):
        return 1.0
    return int(na.get("fontScale")) / 100000
