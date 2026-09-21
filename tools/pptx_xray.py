"""pptx_xray — сводка по любому .pptx: размер слайда, тема vs фактические цвета/шрифты/кегли,
лейауты с плейсхолдерами, повторяющиеся фигуры, состав каждого слайда.

Использование:
    python tools/pptx_xray.py <file.pptx> [--slides] [--json out.json]
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import zipfile
from collections import Counter, defaultdict
from pathlib import Path

from lxml import etree

NS = {
    "a": "http://schemas.openxmlformats.org/drawingml/2006/main",
    "p": "http://schemas.openxmlformats.org/presentationml/2006/main",
    "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
}
EMU = 914400


def _num(name: str) -> int:
    m = re.search(r"(\d+)", name)
    return int(m.group(1)) if m else 0


def _entries(z: zipfile.ZipFile, pattern: str) -> list[str]:
    return sorted((n for n in z.namelist() if re.fullmatch(pattern, n)), key=_num)


def _xml(z: zipfile.ZipFile, name: str) -> etree._Element:
    return etree.fromstring(z.read(name))


def _bbox(sp: etree._Element) -> tuple[int, int, int, int] | None:
    off = sp.find(".//a:xfrm/a:off", NS)
    ext = sp.find(".//a:xfrm/a:ext", NS)
    if off is None or ext is None:
        return None
    return int(off.get("x")), int(off.get("y")), int(ext.get("cx")), int(ext.get("cy"))


def _text(sp: etree._Element, limit: int = 60) -> str:
    t = "".join(x.text or "" for x in sp.iter(f"{{{NS['a']}}}t")).strip()
    t = re.sub(r"\s+", " ", t)
    return (t[: limit - 1] + "…") if len(t) > limit else t


def _shape_kind(sp: etree._Element) -> str:
    tag = etree.QName(sp).localname
    if tag == "pic":
        return "pic"
    if tag == "graphicFrame":
        uri = sp.find(".//a:graphicData", NS)
        u = (uri.get("uri") if uri is not None else "") or ""
        if "chart" in u:
            return "chart"
        if "table" in u:
            return "table"
        if "diagram" in u:
            return "smartart"
        return "graphic"
    if tag == "grpSp":
        return "group"
    if tag == "cxnSp":
        return "connector"
    ph = sp.find(".//p:nvPr/p:ph", NS)
    if ph is not None:
        return f"ph:{ph.get('type', 'body')}"
    geom = sp.find(".//a:prstGeom", NS)
    has_text = bool(_text(sp))
    if has_text:
        return "text"
    return f"shape:{geom.get('prst')}" if geom is not None else "shape"


def analyze(path: Path) -> dict:
    z = zipfile.ZipFile(path)
    pres = _xml(z, "ppt/presentation.xml")
    sz = pres.find("p:sldSz", NS)
    slide_w, slide_h = int(sz.get("cx")), int(sz.get("cy"))
    embedded = [f.get("typeface") for f in pres.findall(".//p:embeddedFont/p:font", NS)]

    # тема
    theme: dict[str, str] = {}
    theme_fonts: dict[str, str] = {}
    for tname in _entries(z, r"ppt/theme/theme\d+\.xml")[:1]:
        t = _xml(z, tname)
        for el in t.find(".//a:clrScheme", NS):
            c = el.find("a:srgbClr", NS)
            s = el.find("a:sysClr", NS)
            val = c.get("val") if c is not None else (s.get("lastClr") if s is not None else None)
            if val:
                theme[etree.QName(el).localname] = val.upper()
        for key in ("majorFont", "minorFont"):
            lat = t.find(f".//a:{key}/a:latin", NS)
            if lat is not None:
                theme_fonts[key] = lat.get("typeface")

    slides = _entries(z, r"ppt/slides/slide\d+\.xml")
    layouts = _entries(z, r"ppt/slideLayouts/slideLayout\d+\.xml")
    masters = _entries(z, r"ppt/slideMasters/slideMaster\d+\.xml")

    # лейауты: имя + плейсхолдеры
    layout_info: dict[str, dict] = {}
    for ln in layouts:
        lx = _xml(z, ln)
        name = lx.find(".//p:cSld", NS).get("name", "")
        phs = []
        for sp in lx.iter(f"{{{NS['p']}}}sp"):
            ph = sp.find(".//p:nvPr/p:ph", NS)
            if ph is not None:
                bb = _bbox(sp)
                phs.append({"type": ph.get("type", "body"), "idx": ph.get("idx"), "bbox": bb})
        layout_info[ln.rsplit("/", 1)[1]] = {"name": name, "placeholders": phs}

    # слайд → лейаут
    slide_layout: dict[str, str] = {}
    for sn in slides:
        rels = f"ppt/slides/_rels/{sn.rsplit('/', 1)[1]}.rels"
        if rels in z.namelist():
            rx = _xml(z, rels)
            for rel in rx:
                if rel.get("Type", "").endswith("/slideLayout"):
                    slide_layout[sn] = rel.get("Target").rsplit("/", 1)[1]

    fonts: Counter[str] = Counter()
    colors: Counter[str] = Counter()
    sizes: Counter[float] = Counter()
    size_by_font: dict[str, Counter] = defaultdict(Counter)
    kinds: Counter[str] = Counter()
    position_sig: Counter[tuple] = Counter()
    slide_rows = []

    for sn in slides:
        sx = _xml(z, sn)
        # фактическое использование цветов/шрифтов/кеглей в тексте
        for rpr in sx.iter(f"{{{NS['a']}}}rPr", f"{{{NS['a']}}}defRPr", f"{{{NS['a']}}}endParaRPr"):
            lat = rpr.find("a:latin", NS)
            if lat is not None and lat.get("typeface"):
                fonts[lat.get("typeface")] += 1
            if rpr.get("sz"):
                s = int(rpr.get("sz")) / 100
                sizes[s] += 1
                if lat is not None and lat.get("typeface"):
                    size_by_font[lat.get("typeface")][s] += 1
        for c in sx.iter(f"{{{NS['a']}}}srgbClr"):
            colors[c.get("val").upper()] += 1
        for c in sx.iter(f"{{{NS['a']}}}schemeClr"):
            colors[f"scheme:{c.get('val')}"] += 1

        spTree = sx.find(".//p:cSld/p:spTree", NS)
        shapes = []
        for sp in spTree:
            tag = etree.QName(sp).localname
            if tag in ("nvGrpSpPr", "grpSpPr"):
                continue
            k = _shape_kind(sp)
            kinds[k] += 1
            bb = _bbox(sp)
            if bb:
                # сигнатура позиции с округлением до ~0.05" — для поиска фиксированных элементов
                sig = (k.split(":")[0], *(round(v / (EMU // 20)) for v in bb))
                position_sig[sig] += 1
            shapes.append((k, bb, _text(sp)))
        slide_rows.append(
            {
                "slide": sn.rsplit("/", 1)[1].replace(".xml", ""),
                "layout": layout_info.get(slide_layout.get(sn, ""), {}).get("name", "?"),
                "shapes": shapes,
            }
        )

    n_slides = max(len(slides), 1)
    repeated = [
        {"kind": sig[0], "bbox_in": [round(v / 20, 2) for v in sig[1:]], "count": c}
        for sig, c in position_sig.most_common()
        if c >= max(3, n_slides // 4)
    ]

    return {
        "file": str(path),
        "size_kb": round(path.stat().st_size / 1024),
        "slide_size_emu": [slide_w, slide_h],
        "slide_size_in": [round(slide_w / EMU, 2), round(slide_h / EMU, 2)],
        "counts": {
            "slides": len(slides),
            "layouts": len(layouts),
            "masters": len(masters),
            "charts": len(_entries(z, r"ppt/charts/chart\d+\.xml")),
            "media": len([n for n in z.namelist() if n.startswith("ppt/media/")]),
        },
        "theme_colors": theme,
        "theme_fonts": theme_fonts,
        "embedded_fonts": embedded,
        "fonts_used": fonts.most_common(),
        "sizes_used": sorted(sizes.items(), key=lambda kv: -kv[1]),
        "sizes_by_font": {f: sorted(c.items(), key=lambda kv: -kv[1])[:10] for f, c in size_by_font.items()},
        "colors_used": colors.most_common(25),
        "shape_kinds": kinds.most_common(),
        "repeated_shapes": repeated,
        "layouts": layout_info,
        "slides": slide_rows,
    }


def print_report(r: dict, show_slides: bool) -> None:
    p = print
    p(f"=== {Path(r['file']).name}  ({r['size_kb']} KB)")
    c = r["counts"]
    p(f"slide: {r['slide_size_in'][0]}x{r['slide_size_in'][1]} in | slides={c['slides']} layouts={c['layouts']} "
      f"masters={c['masters']} charts={c['charts']} media={c['media']}")
    p(f"theme colors : {' '.join(f'{k}=#{v}' for k, v in r['theme_colors'].items())}")
    p(f"theme fonts  : {r['theme_fonts']}   embedded: {r['embedded_fonts']}")
    p(f"fonts used   : {', '.join(f'{f}({n})' for f, n in r['fonts_used'][:8])}")
    p(f"sizes used   : {', '.join(f'{s:g}pt({n})' for s, n in r['sizes_used'][:14])}")
    for f, lst in r["sizes_by_font"].items():
        p(f"  {f:<14} {', '.join(f'{s:g}({n})' for s, n in lst)}")
    p(f"colors used  : {', '.join(f'#{h}({n})' if not h.startswith('scheme') else f'{h}({n})' for h, n in r['colors_used'][:16])}")
    p(f"shape kinds  : {', '.join(f'{k}({n})' for k, n in r['shape_kinds'])}")
    p("repeated shapes (fixed elements candidates):")
    for rs in r["repeated_shapes"][:10]:
        p(f"  {rs['kind']:<8} at {rs['bbox_in']} in  × {rs['count']}")
    p("layouts:")
    for fname, li in r["layouts"].items():
        phs = ", ".join(f"{ph['type']}" for ph in li["placeholders"])
        p(f"  {fname:<20} {li['name']!r:<40} [{phs}]")
    if show_slides:
        p("slides:")
        for s in r["slides"]:
            p(f"  {s['slide']:<8} layout={s['layout']!r}")
            for k, bb, t in s["shapes"]:
                pos = f"({bb[0]/EMU:.2f},{bb[1]/EMU:.2f} {bb[2]/EMU:.2f}x{bb[3]/EMU:.2f})" if bb else "(no xfrm)"
                p(f"      {k:<16} {pos:<28} {t}")
    p()


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("files", nargs="+", type=Path)
    ap.add_argument("--slides", action="store_true", help="печатать состав каждого слайда")
    ap.add_argument("--json", type=Path, help="сохранить полный отчёт в JSON")
    a = ap.parse_args(argv)
    reports = []
    for f in a.files:
        r = analyze(f)
        reports.append(r)
        print_report(r, a.slides)
    if a.json:
        a.json.write_text(json.dumps(reports if len(reports) > 1 else reports[0], ensure_ascii=False, indent=1), "utf-8")
        print(f"json → {a.json}")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
