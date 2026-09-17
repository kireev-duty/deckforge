"""HTML-экспорт: .pptx → один самодостаточный .html (без внешних скриптов и CDN, открывается с file://).

Источник — готовая колода, а не DeckIR: в IR только заполненные слоты, а фон, декор образца и логотипы
мастера живут в XML. Фигуры читаются тем же `core/deck_reader`, что и аудит, поэтому шрифты, кегли и цвета
резолвятся одинаково. Слайд — абсолютно позиционированные блоки в px при 96 dpi (`core/units`), порядок
отрисовки: фон → фигуры мастера → фигуры лейаута → фигуры слайда. Диаграммы — inline SVG по данным из XML
(цвета серий — те, что уже выбрал `render/charts.py`), таблицы — <table>, картинки — data-URI (общая часть
zip вставляется один раз через <symbol>), встроенные шрифты — @font-face из ppt/fonts (EOT → TTF, если без
сжатия). Ничего не растеризуется; текст остаётся текстом (выделяется, ищется, печатается).
"""

from __future__ import annotations

import base64
import html
import logging
import math
import re
import struct
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from deckforge.core.deck_reader import (
    ChartRec,
    ParaRec,
    ShapeRec,
    TableRec,
    background_picture_part,
    part_chain,
    read_shapes,
)
from deckforge.core.ir import ChartSpec, DeckIR
from deckforge.core.package import Package, PartCtx
from deckforge.core.units import emu_to_px

log = logging.getLogger(__name__)

PX_PER_PT = 96 / 72
MIME = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif", ".svg": "image/svg+xml",
        ".webp": "image/webp", ".bmp": "image/bmp", ".tif": "image/tiff", ".tiff": "image/tiff"}
SKIP_MEDIA = (".emf", ".wmf")  # браузер не покажет
FONT_FALLBACK = "Arial, Helvetica, sans-serif"
SAFE_NAME = re.compile(r"^[\w .\-]{1,64}$")
HEX = re.compile(r"^[0-9A-Fa-f]{6}$")
EOT_MAGIC = 0x504C
EOT_COMPRESSED = 0x00000004
EOT_XOR = 0x10000000
TTF_MAGICS = (b"\x00\x01\x00\x00", b"OTTO", b"true")
MAX_IMAGE_PX = 1920  # длинная сторона картинки в HTML; 4K-подложки шаблонов иначе дают 5–18 МБ на файл
WEBP_QUALITY = 82
# гарнитуры с открытой лицензией (OFL) на Google Fonts: если шрифт не встроен, подключаем по сети (офлайн — fallback)
GOOGLE_FONTS = {"Play", "Montserrat", "Roboto", "Open Sans", "Inter", "Manrope", "Golos Text", "PT Sans", "PT Serif",
                "Nunito", "Rubik", "Raleway", "Lato", "Source Sans 3", "Noto Sans", "Oswald", "Exo 2", "Ubuntu", "Jost"}
# для диаграмм: маркеры и линии как в правилах dataviz (тонкие бары, линии 2 px, точки r=4, hairline-сетка)
BAR_MAX_PX = 24
CHART_FONT_PX = 12


@dataclass
class _Assets:
    images: dict[str, tuple[int, int]] = field(default_factory=dict)  # part → (px_w, px_h) для <symbol>
    raw_images: dict[str, str] = field(default_factory=dict)  # part → data-URI для <img> (svg и т. п.)
    fonts: dict[str, str] = field(default_factory=dict)  # css @font-face
    used_fonts: set[str] = field(default_factory=set)  # гарнитуры из run'ов — для Google Fonts
    media: dict[str, tuple[bytes, str]] = field(default_factory=dict)  # part → (байты после пережатия, mime)


class HtmlExporter:
    def __init__(self, pptx: Path, ir: DeckIR | None = None, title: str | None = None) -> None:
        self.pptx = Path(pptx)
        self.pkg = Package(self.pptx)
        self.ir = ir
        self.title = title or self.pptx.stem
        w, h = self.pkg.slide_size
        self.w, self.h = round(emu_to_px(w)), round(emu_to_px(h))
        self.assets = _Assets()
        self._master_cache: dict[str, str] = {}
        self._slide_no = 0
        self._surface = "#FFFFFF"

    # ── документ ──

    def build(self) -> str:
        slides = [self._slide(i, part) for i, part in enumerate(self.pkg.slides)]
        self._embed_fonts()
        defs = "".join(
            f'<symbol id="{sid}" viewBox="0 0 {pw} {ph}"><image width="{pw}" height="{ph}" href="{self._data_uri(part)}"/></symbol>'
            for part, (sid, pw, ph) in self._symbols().items()
        )
        embedded = {css.split('"')[1] for css in self.assets.fonts.values()}
        web = sorted(f for f in self.assets.used_fonts if f in GOOGLE_FONTS and f not in embedded)
        link = ('<link rel="stylesheet" href="https://fonts.googleapis.com/css2?'
                + "&".join("family=" + f.replace(" ", "+") + ":wght@400;700" for f in web) + '&display=swap">') if web else ""
        return _DOC.format(
            title=html.escape(self.title), w=self.w, h=self.h, n=len(slides), fontlink=link,
            fonts="\n".join(self.assets.fonts.values()),
            defs=f'<svg width="0" height="0" style="position:absolute" aria-hidden="true"><defs>{defs}</defs></svg>',
            slides="\n".join(slides),
        )

    def _symbols(self) -> dict[str, tuple[str, int, int]]:
        return {part: (f"img{i}", pw, ph) for i, (part, (pw, ph)) in enumerate(self.assets.images.items())}

    # ── слайд ──

    def _slide(self, idx: int, part: str) -> str:
        ctx = PartCtx.for_slide(self.pkg, part)
        if ctx is None:
            return f'<section class="slide" id="s{idx + 1}"><div class="sp" style="left:0;top:0;width:{self.w}px;height:{self.h}px"></div></section>'
        _, layout_part, master_part = part_chain(ctx)
        body: list[str] = [self._background(ctx)]
        if ctx.root.get("showMasterSp", "1") not in ("0", "false") and master_part:
            body.append(self._inherited_shapes(PartCtx.for_master(self.pkg, master_part)))
        if layout_part and ctx.layout is not None and ctx.layout.get("showMasterSp", "1") not in ("0", "false"):
            lctx = PartCtx.for_layout(self.pkg, layout_part)
            if lctx is not None:
                body.append(self._inherited_shapes(lctx, cache=False))
        shapes = read_shapes(ctx)
        self._slide_no = idx + 1
        self._surface = "#" + _hex(_bg_color(ctx))
        text_color, font = _dominant_text(shapes) or (ctx.theme.colors.get(ctx.clr_map.get("tx1", "dk1"), "212121"),
                                                      ctx.theme.minor_font)
        body.extend(self._shape(s, text_color, font) for s in shapes)
        notes = ""
        if self.ir and idx < len(self.ir.slides) and self.ir.slides[idx].notes.strip():
            notes = f'<aside class="notes" hidden>{html.escape(self.ir.slides[idx].notes)}</aside>'
        return (f'<section class="slide" id="s{idx + 1}" aria-label="Слайд {idx + 1}">'
                + "".join(body) + notes + "</section>")

    def _inherited_shapes(self, ctx: PartCtx | None, cache: bool = True) -> str:
        """Фигуры мастера/лейаута под слайдом: всё, кроме плейсхолдеров (их PowerPoint не рисует)."""
        if ctx is None:
            return ""
        if cache and ctx.part in self._master_cache:
            return self._master_cache[ctx.part]
        text_color = ctx.theme.colors.get(ctx.clr_map.get("tx1", "dk1"), "212121")
        out = "".join(self._shape(s, text_color, ctx.theme.minor_font)
                      for s in read_shapes(ctx) if not s.is_placeholder)
        if cache:
            self._master_cache[ctx.part] = out
        return out

    def _background(self, ctx: PartCtx) -> str:
        style = f"left:0;top:0;width:{self.w}px;height:{self.h}px;"
        pic = background_picture_part(ctx)
        if pic:
            return f'<div class="sp bg" style="{style}">{self._image_html(pic, (0, 0, 0, 0))}</div>'
        fill, _ = ctx.background()
        colors = [c for c, _ in fill] if fill else [_bg_color(ctx)]
        return f'<div class="sp bg" style="{style}{_css_fill(colors)}"></div>'

    # ── фигуры ──

    def _shape(self, s: ShapeRec, text_color: str, font: str) -> str:
        if s.hidden or s.box.w <= 0 and s.box.h <= 0:
            return ""
        x, y, w, h = (emu_to_px(v) for v in (s.box.x, s.box.y, s.box.w, s.box.h))
        css = [f"left:{x:.1f}px", f"top:{y:.1f}px", f"width:{max(w, 0):.1f}px", f"height:{max(h, 0):.1f}px"]
        if s.tag == "cxnSp" or s.geom in ("line", "straightConnector1", "bentConnector3"):
            return self._line(s, css)
        if s.fill:
            css.append(_css_fill(s.fill, s.fill_alpha))
        if s.line and s.line_w_emu:
            css.append(f"border:{max(emu_to_px(s.line_w_emu), 1):.1f}px solid #{_hex(s.line[0])}")
        if s.geom == "ellipse":
            css.append("border-radius:50%")
        elif s.geom == "roundRect":
            css.append(f"border-radius:{min(w, h) * (s.geom_adj if s.geom_adj is not None else 0.16667):.1f}px")
        tf = _transform(s)
        if tf:
            css.append(tf)
        inner = ""
        if s.picture is not None and s.picture.part:
            inner = self._image_html(s.picture.part, s.picture.src_rect)
        if s.chart is not None and s.tag == "graphicFrame":
            inner = self._chart_html(s.chart, w, h, text_color, font)
        elif s.table is not None:
            inner = self._table_html(s.table, w, h)
        elif s.has_text:
            inner += self._text_html(s)
        if not inner and not s.fill and not s.line:
            return ""  # пустой невидимый бокс
        return f'<div class="sp" style="{";".join(css)}">{inner}</div>'

    def _line(self, s: ShapeRec, css: list[str]) -> str:
        if not s.line:
            return ""
        w, h = max(emu_to_px(s.box.w), 1), max(emu_to_px(s.box.h), 1)
        sw = max(emu_to_px(s.line_w_emu or 9525), 1)
        css[2:4] = [f"width:{max(w, sw):.1f}px", f"height:{max(h, sw):.1f}px"]  # линия нулевой высоты иначе невидима
        x1, y1, x2, y2 = (0, h, w, 0) if s.flip_v != s.flip_h and s.flip_v else (0, 0, w, h)
        if s.box.h < s.box.w * 0.02:  # горизонтальная
            x1, y1, x2, y2 = 0, h / 2, w, h / 2
        elif s.box.w < s.box.h * 0.02:
            x1, y1, x2, y2 = w / 2, 0, w / 2, h
        return (f'<div class="sp" style="{";".join(css)}"><svg width="100%" height="100%" viewBox="0 0 {w:.1f} {h:.1f}" '
                f'preserveAspectRatio="none"><line x1="{x1:.1f}" y1="{y1:.1f}" x2="{x2:.1f}" y2="{y2:.1f}" '
                f'stroke="#{_hex(s.line[0])}" stroke-width="{sw:.1f}"/></svg></div>')

    def _text_html(self, s: ShapeRec) -> str:
        l, t, r, b = (emu_to_px(v) for v in s.insets)
        justify = {"ctr": "center", "b": "flex-end"}.get(s.anchor, "flex-start")
        style = f"padding:{t:.1f}px {r:.1f}px {b:.1f}px {l:.1f}px;justify-content:{justify}"
        if not s.wrap:
            style += ";white-space:nowrap"
        cls = "tx fit" if s.autofit == "normAutofit" else "tx"  # fit: JS ужимает кегль, как PowerPoint
        return f'<div class="{cls}" style="{style}">' + "".join(self._para_html(p) for p in s.paragraphs) + "</div>"

    def _para_html(self, p: ParaRec) -> str:
        css = [f"text-align:{ {'ctr': 'center', 'r': 'right', 'just': 'justify'}.get(p.align, 'left')}"]
        if p.space_before_pt:
            css.append(f"margin-top:{p.space_before_pt * PX_PER_PT:.1f}px")
        if p.space_after_pt:
            css.append(f"margin-bottom:{p.space_after_pt * PX_PER_PT:.1f}px")
        if p.line_spacing:
            css.append(f"line-height:{p.line_spacing * 1.2:.2f}")
        if p.indent_emu:
            css.append(f"padding-left:{emu_to_px(p.indent_emu):.1f}px")
        if p.first_indent_emu and not p.bullet:
            css.append(f"text-indent:{emu_to_px(p.first_indent_emu):.1f}px")
        runs = "".join(self._run_html(r) for r in p.runs)
        if p.bullet and p.runs:
            r0 = p.runs[0]
            marker = html.escape(p.bullet_char or "•")
            cls = "bul num" if p.bullet_auto else "bul"
            mark = (f'<span class="mk" style="font-size:{r0.size_pt * PX_PER_PT:.1f}px;color:{_color(r0.color)}">'
                    f"{'' if p.bullet_auto else marker}</span>")
            return f'<p class="{cls}" style="{";".join(css)}">{mark}<span class="bt">{runs}</span></p>'
        return f'<p style="{";".join(css)}">{runs}</p>'

    def _run_html(self, r) -> str:
        if r.font:
            self.assets.used_fonts.add(r.font)
        css = [f"font-family:{_font_stack(r.font)}", f"font-size:{r.size_pt * PX_PER_PT:.1f}px", f"color:{_color(r.color)}"]
        if r.bold:
            css.append("font-weight:700")
        if r.italic:
            css.append("font-style:italic")
        if r.underline:
            css.append("text-decoration:underline")
        text = str(self._slide_no) if r.field == "slidenum" else r.text
        return f'<span style="{";".join(css)}">{html.escape(text)}</span>'

    # ── картинки ──

    def _image_html(self, part: str, src_rect: tuple[int, int, int, int]) -> str:
        ext = Path(part).suffix.lower()
        if ext in SKIP_MEDIA or part not in self.pkg.names:
            return "<!-- медиа не поддерживается браузером -->"
        size = self._image_size(part)
        if size is None:  # svg и прочее без пиксельных размеров — как есть, без кропа
            return f'<img src="{self._data_uri(part)}" alt="">'
        pw, ph = size
        self.assets.images.setdefault(part, (pw, ph))
        sid = f"img{list(self.assets.images).index(part)}"
        l, t, r, b = (v / 100000 for v in src_rect)
        vx, vy = l * pw, t * ph
        vw, vh = max(pw * (1 - l - r), 1), max(ph * (1 - t - b), 1)
        return (f'<svg width="100%" height="100%" viewBox="{vx:.1f} {vy:.1f} {vw:.1f} {vh:.1f}" '
                f'preserveAspectRatio="none"><use href="#{sid}" width="{pw}" height="{ph}"/></svg>')

    def _image_size(self, part: str) -> tuple[int, int] | None:
        import io

        from PIL import Image

        try:
            with Image.open(io.BytesIO(self.pkg.zip.read(part))) as im:
                return im.size
        except Exception:  # noqa: BLE001
            return None

    def _data_uri(self, part: str) -> str:
        if part not in self.assets.raw_images:
            data, mime = self._media(part)
            self.assets.raw_images[part] = f"data:{mime};base64," + base64.b64encode(data).decode()
        return self.assets.raw_images[part]

    def _media(self, part: str) -> tuple[bytes, str]:
        """Байты картинки для data-URI: растр крупнее MAX_IMAGE_PX или тяжелее 200 КБ → WebP (альфа сохраняется)."""
        if part in self.assets.media:
            return self.assets.media[part]
        import io

        from PIL import Image

        raw = self.pkg.zip.read(part)
        out = (raw, MIME.get(Path(part).suffix.lower(), "application/octet-stream"))
        try:
            with Image.open(io.BytesIO(raw)) as im:
                if im.format in ("PNG", "JPEG", "BMP", "TIFF", "WEBP") and (max(im.size) > MAX_IMAGE_PX or len(raw) > 200_000):
                    im = im.convert("RGBA" if im.mode in ("RGBA", "LA", "P") else "RGB")
                    im.thumbnail((MAX_IMAGE_PX, MAX_IMAGE_PX))
                    buf = io.BytesIO()
                    im.save(buf, "WEBP", quality=WEBP_QUALITY, method=4)
                    if buf.tell() < len(raw):
                        out = (buf.getvalue(), "image/webp")
        except Exception:  # noqa: BLE001 — svg/emf и битые файлы — как есть
            pass
        self.assets.media[part] = out
        return out

    # ── диаграммы ──

    def _chart_html(self, chart: ChartRec, w: float, h: float, text_color: str, font: str) -> str:
        spec = chart.spec
        if spec is None:
            return f'<div class="tx" style="justify-content:center;text-align:center;color:{_color(text_color)}"><p>{html.escape(chart.kind)}</p></div>'
        return svg_chart(spec, chart.series_colors, w, h, text_color=_color(text_color), font=_font_stack(font),
                         surface=self._surface)

    # ── таблицы ──

    def _table_html(self, t: TableRec, w: float, h: float) -> str:
        cols = "".join(f'<col style="width:{emu_to_px(cw):.1f}px">' for cw in t.col_widths_emu)
        rows: list[str] = []
        for ri, row in enumerate(t.cells):
            rh = f' style="height:{emu_to_px(t.row_heights_emu[ri]):.1f}px"' if ri < len(t.row_heights_emu) else ""
            cells = []
            for c in row:
                if c.merged:
                    continue
                attrs = (f' rowspan="{c.row_span}"' if c.row_span > 1 else "") + (f' colspan="{c.col_span}"' if c.col_span > 1 else "")
                style = f' style="background:#{_hex(c.fill)}"' if c.fill else ""
                tag = "th" if ri == 0 else "td"
                cells.append(f"<{tag}{attrs}{style}>" + "".join(self._para_html(p) for p in c.paragraphs) + f"</{tag}>")
            rows.append(f"<tr{rh}>" + "".join(cells) + "</tr>")
        return f'<table style="width:{w:.1f}px"><colgroup>{cols}</colgroup>' + "".join(rows) + "</table>"

    # ── шрифты ──

    def _embed_fonts(self) -> None:
        for part in self.pkg.parts(r"ppt/fonts/.+"):
            try:
                ttf = eot_to_ttf(self.pkg.zip.read(part))
            except Exception as e:  # noqa: BLE001
                log.info("шрифт %s не встроен: %s", part, e)
                continue
            if ttf is None:
                continue
            stem = Path(part).stem  # Play-bold, Montserrat-regular …
            family, _, style = stem.partition("-")
            if not SAFE_NAME.match(family):
                continue
            weight = "700" if "bold" in style.lower() else "400"
            fstyle = "italic" if "italic" in style.lower() else "normal"
            self.assets.fonts[stem] = (f'@font-face{{font-family:"{family}";font-weight:{weight};font-style:{fstyle};'
                                       f'src:url(data:font/ttf;base64,{base64.b64encode(ttf).decode()}) format("truetype")}}')


def eot_to_ttf(data: bytes) -> bytes | None:
    """EOT (ppt/fonts/*.fntdata) → TTF: шрифт лежит в хвосте файла; MTX-сжатие не разбираем (None)."""
    if len(data) < 36:
        return None
    eot_size, font_size, _version, flags = struct.unpack_from("<IIII", data, 0)
    magic = struct.unpack_from("<H", data, 34)[0]
    if eot_size != len(data) or magic != EOT_MAGIC or font_size <= 0 or font_size > len(data):
        return None
    if flags & EOT_COMPRESSED:
        return None
    font = data[-font_size:]
    if flags & EOT_XOR:
        font = bytes(b ^ 0x50 for b in font)
    return font if font[:4] in TTF_MAGICS else None


# ──────────────────────────── SVG-диаграммы ────────────────────────────


def svg_chart(spec: ChartSpec, colors: list[str], w: float, h: float, *, text_color: str = "#212121",
              font: str = FONT_FALLBACK, surface: str = "#FFFFFF") -> str:
    """Диаграмма по ChartSpec: bar / column / line / area / pie / doughnut. Одна ось, тонкие марки,
    легенда при ≥ 2 сериях, подписи значений — только на концах/вершинах, оси и сетка приглушены."""
    names = list(spec.series)
    palette = [f"#{_hex(c)}" for c in colors] or ["#0077FF", "#00AEE8", "#8F8F8F", "#FFB800", "#2FCC71"]
    col = {n: palette[i % len(palette)] for i, n in enumerate(names)}
    muted = _muted(text_color)
    parts = [f'<svg class="chart" viewBox="0 0 {w:.0f} {h:.0f}" width="100%" height="100%" '
             f'font-family=\'{font}\' font-size="{CHART_FONT_PX}" fill="{text_color}">']
    top = 0.0
    if spec.title:
        parts.append(f'<text x="{w / 2:.1f}" y="16" text-anchor="middle" font-size="14" font-weight="600">{html.escape(spec.title)}</text>')
        top = 24
    legend_h = 0.0
    if len(names) > 1 or spec.kind in ("pie", "doughnut"):
        legend_h = 20
        items = names if spec.kind not in ("pie", "doughnut") else spec.categories
        lcol = col if spec.kind not in ("pie", "doughnut") else {c: palette[i % len(palette)] for i, c in enumerate(items)}
        x = 8.0
        leg = []
        for it in items:
            leg.append(f'<rect x="{x:.1f}" y="{h - 14:.1f}" width="10" height="10" rx="2" fill="{lcol[it]}"/>'
                       f'<text x="{x + 14:.1f}" y="{h - 5:.1f}" fill="{text_color}">{html.escape(str(it))}</text>')
            x += 14 + len(str(it)) * CHART_FONT_PX * 0.58 + 14
        parts.append(f'<g class="legend">{"".join(leg)}</g>')
    inner = (0.0, top, w, h - top - legend_h)
    if spec.kind in ("pie", "doughnut"):
        parts.append(_svg_pie(spec, palette, inner, spec.kind == "doughnut", surface))
    elif spec.kind == "bar":
        parts.append(_svg_bars(spec, col, inner, text_color, muted, horizontal=True))
    elif spec.kind == "column":
        parts.append(_svg_bars(spec, col, inner, text_color, muted, horizontal=False))
    else:
        parts.append(_svg_lines(spec, col, inner, surface, muted, area=spec.kind == "area"))
    parts.append("</svg>")
    return "".join(parts)


def _ticks(vmax: float, vmin: float = 0.0, n: int = 4) -> list[float]:
    span = vmax - vmin
    if span <= 0:
        return [vmin, vmin + 1]
    step = 10 ** math.floor(math.log10(span / n))
    for m in (1, 2, 2.5, 5, 10):
        if span / (step * m) <= n:
            step *= m
            break
    start = math.floor(vmin / step) * step
    out = [start]
    while out[-1] < vmax - 1e-9:
        out.append(out[-1] + step)
    return out


def _fmt(v: float) -> str:
    return f"{v:,.0f}".replace(",", " ") if abs(v) >= 100 or v == int(v) else f"{v:.1f}".replace(".", ",")


def _svg_bars(spec: ChartSpec, col: dict[str, str], area: tuple[float, float, float, float], text_color: str,
              muted: str, *, horizontal: bool) -> str:
    ax, ay, aw, ah = area
    names = list(spec.series)
    n_cat, n_ser = len(spec.categories), len(names)
    vals = [v for s in spec.series.values() for v in s]
    vmax, vmin = max(vals + [0]), min(vals + [0])
    ticks = _ticks(vmax, vmin)
    lo, hi = ticks[0], ticks[-1]
    label_w = max(len(c) for c in spec.categories) * CHART_FONT_PX * 0.58 if horizontal else 0
    pl, pr, pt, pb = (ax + 8 + label_w, 8, 8, 20) if horizontal else (ax + 8 + len(_fmt(hi)) * 7, 8, (26 if spec.y_label else 12), 20)
    x0, y0 = pl, ay + pt
    pw, ph = aw - pl - pr + ax, ah - pt - pb
    out = []
    scale = (lambda v: (v - lo) / (hi - lo) if hi > lo else 0)
    # сетка и подписи оси значений
    for t in ticks:
        if horizontal:
            x = x0 + scale(t) * pw
            out.append(f'<line x1="{x:.1f}" y1="{y0:.1f}" x2="{x:.1f}" y2="{y0 + ph:.1f}" stroke="{muted}" stroke-width="1"/>'
                       f'<text x="{x:.1f}" y="{y0 + ph + 14:.1f}" text-anchor="middle" fill="{muted}">{_fmt(t)}</text>')
        else:
            y = y0 + ph - scale(t) * ph
            out.append(f'<line x1="{x0:.1f}" y1="{y:.1f}" x2="{x0 + pw:.1f}" y2="{y:.1f}" stroke="{muted}" stroke-width="1"/>'
                       f'<text x="{x0 - 4:.1f}" y="{y + 4:.1f}" text-anchor="end" fill="{muted}">{_fmt(t)}</text>')
    band = (ph if horizontal else pw) / max(n_cat, 1)
    thick = min(BAR_MAX_PX, band * 0.7 / max(n_ser, 1))
    group = thick * n_ser + 2 * (n_ser - 1)
    for ci, cat in enumerate(spec.categories):
        base = (y0 if horizontal else x0) + ci * band + (band - group) / 2
        if horizontal:
            out.append(f'<text x="{x0 - 6:.1f}" y="{y0 + ci * band + band / 2 + 4:.1f}" text-anchor="end">{html.escape(cat)}</text>')
        else:
            out.append(f'<text x="{x0 + ci * band + band / 2:.1f}" y="{y0 + ph + 14:.1f}" text-anchor="middle">{html.escape(cat)}</text>')
        for si, name in enumerate(names):
            v = spec.series[name][ci] if ci < len(spec.series[name]) else 0.0
            length = abs(scale(v) - scale(0)) * (pw if horizontal else ph)
            off = base + si * (thick + 2)
            tip = f"<title>{html.escape(name)} · {html.escape(cat)}: {_fmt(v)}</title>"
            if horizontal:
                x = x0 + min(scale(0), scale(v)) * pw
                out.append(f'<rect x="{x:.1f}" y="{off:.1f}" width="{length:.1f}" height="{thick:.1f}" rx="2" fill="{col[name]}">{tip}</rect>')
                if n_ser == 1 or v == max(spec.series[name]):
                    out.append(f'<text x="{x + length + 4:.1f}" y="{off + thick / 2 + 4:.1f}">{_fmt(v)}</text>')
            else:
                y = y0 + ph - max(scale(0), scale(v)) * ph
                out.append(f'<rect x="{off:.1f}" y="{y:.1f}" width="{thick:.1f}" height="{length:.1f}" rx="2" fill="{col[name]}">{tip}</rect>')
                if n_ser == 1 or v == max(spec.series[name]):
                    out.append(f'<text x="{off + thick / 2:.1f}" y="{y - 4:.1f}" text-anchor="middle">{_fmt(v)}</text>')
    axis = (f'<line x1="{x0:.1f}" y1="{y0:.1f}" x2="{x0:.1f}" y2="{y0 + ph:.1f}"' if horizontal
            else f'<line x1="{x0:.1f}" y1="{y0 + ph:.1f}" x2="{x0 + pw:.1f}" y2="{y0 + ph:.1f}"')
    out.append(axis + f' stroke="{muted}" stroke-width="1"/>')
    if spec.y_label and not horizontal:
        out.append(f'<text x="{x0 - 4:.1f}" y="{ay + 10:.1f}" text-anchor="end" fill="{muted}" font-size="11">{html.escape(spec.y_label)}</text>')
    return "".join(out)


def _svg_lines(spec: ChartSpec, col: dict[str, str], box: tuple[float, float, float, float], surface: str,
               muted: str, *, area: bool = False) -> str:
    ax, ay, aw, ah = box
    names = list(spec.series)
    n_cat = len(spec.categories)
    vals = [v for s in spec.series.values() for v in s]
    vmax, vmin = max(vals + [0]), min(vals + [0])
    ticks = _ticks(vmax, vmin)
    lo, hi = ticks[0], ticks[-1]
    end_label_w = max((len(_fmt(s[-1])) for s in spec.series.values() if s), default=0) * 7 + 8
    pl, pr, pt, pb = ax + 8 + len(_fmt(hi)) * 7, 8 + end_label_w, (26 if spec.y_label else 12), 20
    x0, y0 = pl, ay + pt
    pw, ph = aw - pl - pr + ax, ah - pt - pb
    scale = (lambda v: (v - lo) / (hi - lo) if hi > lo else 0)
    out = []
    for t in ticks:
        y = y0 + ph - scale(t) * ph
        out.append(f'<line x1="{x0:.1f}" y1="{y:.1f}" x2="{x0 + pw:.1f}" y2="{y:.1f}" stroke="{muted}" stroke-width="1"/>'
                   f'<text x="{x0 - 4:.1f}" y="{y + 4:.1f}" text-anchor="end" fill="{muted}">{_fmt(t)}</text>')
    step = pw / max(n_cat - 1, 1)
    every = max(1, math.ceil(n_cat * CHART_FONT_PX * 0.58 * 6 / max(pw, 1)))  # не чаще, чем влезают подписи
    for ci, cat in enumerate(spec.categories):
        if ci % every == 0 or ci == n_cat - 1:
            out.append(f'<text x="{x0 + ci * step:.1f}" y="{y0 + ph + 14:.1f}" text-anchor="middle">{html.escape(cat)}</text>')
    y_base = y0 + ph - scale(0) * ph
    for name in names:
        series = spec.series[name]
        pts = [(x0 + i * step, y0 + ph - scale(v) * ph) for i, v in enumerate(series)]
        if not pts:
            continue
        path = "M" + " L".join(f"{x:.1f},{y:.1f}" for x, y in pts)
        if area:
            out.append(f'<path d="{path} L{pts[-1][0]:.1f},{y_base:.1f} L{pts[0][0]:.1f},{y_base:.1f} Z" fill="{col[name]}" opacity=".12"/>')
        out.append(f'<path d="{path}" fill="none" stroke="{col[name]}" stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/>')
        for i, (x, y) in enumerate(pts):
            cat = spec.categories[i] if i < n_cat else str(i + 1)
            out.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="4" fill="{col[name]}" stroke="{surface}" stroke-width="2">'
                       f"<title>{html.escape(name)} · {html.escape(cat)}: {_fmt(series[i])}</title></circle>")
        out.append(f'<text x="{pts[-1][0] + 8:.1f}" y="{pts[-1][1] + 4:.1f}">{_fmt(series[-1])}</text>')
    out.append(f'<line x1="{x0:.1f}" y1="{y0 + ph:.1f}" x2="{x0 + pw:.1f}" y2="{y0 + ph:.1f}" stroke="{muted}" stroke-width="1"/>')
    if spec.y_label:
        out.append(f'<text x="{x0 - 4:.1f}" y="{ay + 10:.1f}" text-anchor="end" fill="{muted}" font-size="11">{html.escape(spec.y_label)}</text>')
    return "".join(out)


def _svg_pie(spec: ChartSpec, palette: list[str], area: tuple[float, float, float, float], doughnut: bool,
             surface: str) -> str:
    ax, ay, aw, ah = area
    name = next(iter(spec.series), None)
    vals = spec.series.get(name, []) if name else []
    total = sum(v for v in vals if v > 0) or 1.0
    cx, cy, r = ax + aw / 2, ay + ah / 2, min(aw, ah) / 2 - 12
    r_in = r * 0.55 if doughnut else 0
    out, angle = [], -math.pi / 2
    for i, v in enumerate(vals):
        if v <= 0:
            continue
        a2 = angle + 2 * math.pi * v / total
        large = 1 if a2 - angle > math.pi else 0
        x1, y1, x2, y2 = cx + r * math.cos(angle), cy + r * math.sin(angle), cx + r * math.cos(a2), cy + r * math.sin(a2)
        if doughnut:
            xi1, yi1, xi2, yi2 = (cx + r_in * math.cos(a2), cy + r_in * math.sin(a2),
                                  cx + r_in * math.cos(angle), cy + r_in * math.sin(angle))
            d = f"M{x1:.1f},{y1:.1f} A{r:.1f},{r:.1f} 0 {large} 1 {x2:.1f},{y2:.1f} L{xi1:.1f},{yi1:.1f} A{r_in:.1f},{r_in:.1f} 0 {large} 0 {xi2:.1f},{yi2:.1f} Z"
        else:
            d = f"M{cx:.1f},{cy:.1f} L{x1:.1f},{y1:.1f} A{r:.1f},{r:.1f} 0 {large} 1 {x2:.1f},{y2:.1f} Z"
        cat = spec.categories[i] if i < len(spec.categories) else str(i + 1)
        out.append(f'<path d="{d}" fill="{palette[i % len(palette)]}" stroke="{surface}" stroke-width="2">'
                   f"<title>{html.escape(cat)}: {_fmt(v)} ({v / total:.0%})</title></path>")
        mid = (angle + a2) / 2
        lx, ly = cx + (r + 10) * math.cos(mid), cy + (r + 10) * math.sin(mid)
        anchor = "start" if math.cos(mid) >= 0 else "end"
        out.append(f'<text x="{lx:.1f}" y="{ly + 4:.1f}" text-anchor="{anchor}">{v / total:.0%}</text>')
        angle = a2
    return "".join(out)


# ──────────────────────────── вспомогательное ────────────────────────────


def _bg_color(ctx: PartCtx) -> str:
    fill, _ = ctx.background()
    colors = [c for c, _ in fill] if fill else []
    return colors[0] if colors else ctx.theme.colors.get(ctx.clr_map.get("bg1", "lt1"), "FFFFFF")


def _dominant_text(shapes: list[ShapeRec]) -> tuple[str, str] | None:
    c: Counter[tuple[str, str]] = Counter()
    for s in shapes:
        for r in s.runs:
            if r.color:
                c[(r.color, r.font)] += len(r.text)
    return c.most_common(1)[0][0] if c else None


def _hex(c: str | None) -> str:
    return c.upper() if c and HEX.match(c) else "000000"


def _color(c: str | None) -> str:
    return f"#{_hex(c)}" if c else "inherit"


def _muted(text_color: str) -> str:
    """Приглушённый цвет осей: 55 % прозрачности от цвета текста (одинаково на светлом и тёмном фоне)."""
    return text_color + "8C" if text_color.startswith("#") and len(text_color) == 7 else "#8F8F8F"


def _css_fill(colors: list[str], alpha: float = 1.0) -> str:
    cs = [f"#{_hex(c)}" for c in colors]
    if len(cs) == 1:
        if alpha < 1.0:
            h = _hex(colors[0])
            return f"background:rgba({int(h[:2], 16)},{int(h[2:4], 16)},{int(h[4:], 16)},{alpha:.2f})"
        return f"background:{cs[0]}"
    return f"background:linear-gradient(180deg,{','.join(cs)})"


def _font_stack(font: str | None) -> str:
    if font and SAFE_NAME.match(font):
        return f"'{font}', {FONT_FALLBACK}"  # одинарные кавычки: имя стоит внутри style="…"
    return FONT_FALLBACK


def _transform(s: ShapeRec) -> str:
    parts = []
    if s.rot:
        parts.append(f"rotate({s.rot:.1f}deg)")
    if s.flip_h:
        parts.append("scaleX(-1)")
    if s.flip_v:
        parts.append("scaleY(-1)")
    return f"transform:{' '.join(parts)}" if parts else ""


_DOC = """<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="generator" content="deckforge">
<title>{title}</title>
{fontlink}
<style>
{fonts}
:root{{--w:{w}px;--h:{h}px;--gap:24px;--bg:#1d1f24;--ink:#e6e6e6}}
*{{box-sizing:border-box}}
html,body{{margin:0;background:var(--bg);color:var(--ink);font-family:Arial,Helvetica,sans-serif}}
.bar{{position:sticky;top:0;z-index:5;display:flex;gap:12px;align-items:center;padding:8px 16px;background:#2a2d34;font-size:14px}}
.bar b{{flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}}
.bar button{{background:#3b3f48;color:var(--ink);border:0;border-radius:6px;padding:6px 10px;cursor:pointer;font:inherit}}
.deck{{display:flex;flex-direction:column;align-items:center;gap:var(--gap);padding:var(--gap) 16px}}
.frame{{position:relative;width:var(--w);height:var(--h);flex:none}}
.slide{{position:absolute;left:0;top:0;width:var(--w);height:var(--h);overflow:hidden;transform-origin:top left;background:#fff;box-shadow:0 2px 12px rgba(0,0,0,.4)}}
.sp{{position:absolute;overflow:visible}}
.sp>svg,.sp>img{{display:block;width:100%;height:100%}}
.sp>svg.chart{{overflow:visible}}
.tx{{position:absolute;inset:0;display:flex;flex-direction:column;overflow:visible;line-height:1.2;white-space:pre-wrap;word-wrap:break-word}}
.tx p{{margin:0}}
.tx p.bul{{display:flex;gap:.4em}}
.tx p.bul .bt{{flex:1;min-width:0}}
.tx{{counter-reset:num}}
.tx p.num .mk::before{{counter-increment:num;content:counter(num) "."}}
table{{border-collapse:collapse;table-layout:fixed;position:absolute;left:0;top:0}}
td,th{{padding:4px 7px;vertical-align:middle;text-align:left;font-weight:normal;overflow-wrap:anywhere;border-bottom:1px solid rgba(128,128,128,.25)}}
td p,th p{{margin:0}}
body.present .bar{{display:none}}
body.present .deck{{padding:0;gap:0}}
body.present .frame{{display:none;margin:auto}}
body.present .frame.cur{{display:block}}
@media print{{
  @page{{size:{w}px {h}px;margin:0}}
  html,body{{background:#fff}}
  .bar{{display:none}}
  .deck{{padding:0;gap:0}}
  .frame{{transform:none!important;page-break-after:always;break-after:page}}
  .slide{{box-shadow:none}}
}}
</style>
</head>
<body>
{defs}
<div class="bar"><b>{title}</b><span id="pos">1 / {n}</span><button id="btnp" title="F — во весь экран, ←/→ — листать">Показ</button><button onclick="window.print()">Печать</button></div>
<main class="deck" id="deck">
{slides}
</main>
<script>
(function(){{
  const W={w},H={h},deck=document.getElementById('deck');
  const slides=[...deck.querySelectorAll('.slide')];
  slides.forEach(s=>{{const f=document.createElement('div');f.className='frame';s.replaceWith(f);f.appendChild(s);}});
  const frames=[...deck.querySelectorAll('.frame')];let cur=0;
  function fit(){{
    const present=document.body.classList.contains('present');
    const aw=present?window.innerWidth:Math.min(deck.clientWidth-32,W);
    const ah=present?window.innerHeight:Infinity;
    const s=Math.min(aw/W,ah/H);
    frames.forEach(f=>{{f.style.width=W*s+'px';f.style.height=H*s+'px';f.firstChild.style.transform='scale('+s+')';}});
  }}
  function go(i){{cur=Math.max(0,Math.min(frames.length-1,i));frames.forEach((f,k)=>f.classList.toggle('cur',k===cur));
    document.getElementById('pos').textContent=(cur+1)+' / '+frames.length;history.replaceState(null,'','#'+(cur+1));
    if(!document.body.classList.contains('present'))frames[cur].scrollIntoView({{block:'start'}});}}
  function toggle(){{document.body.classList.toggle('present');fit();go(cur);
    if(document.body.classList.contains('present')&&document.documentElement.requestFullscreen)document.documentElement.requestFullscreen().catch(()=>{{}});
    else if(document.fullscreenElement)document.exitFullscreen();}}
  document.getElementById('btnp').onclick=toggle;
  addEventListener('keydown',e=>{{if(e.key==='ArrowRight'||e.key==='PageDown'||e.key===' ')go(cur+1);else if(e.key==='ArrowLeft'||e.key==='PageUp')go(cur-1);
    else if(e.key==='f'||e.key==='F'||e.key==='а'||e.key==='А')toggle();else if(e.key==='Escape'&&document.body.classList.contains('present'))toggle();}});
  addEventListener('resize',fit);
  const io=new IntersectionObserver(es=>{{es.forEach(x=>{{if(x.isIntersecting&&!document.body.classList.contains('present')){{cur=frames.indexOf(x.target);document.getElementById('pos').textContent=(cur+1)+' / '+frames.length;}}}});}},{{threshold:.6}});
  frames.forEach(f=>io.observe(f));
  function shrink(){{document.querySelectorAll('.tx.fit').forEach(t=>{{
    const box=t.parentElement;let k=1;const spans=t.querySelectorAll('span[style*="font-size"]');
    const base=[...spans].map(e=>parseFloat(e.style.fontSize));const jc=t.style.justifyContent;t.style.justifyContent='flex-start';
    for(let i=0;i<10&&t.scrollHeight>box.clientHeight+1&&k>0.5;i++){{k-=0.05;spans.forEach((e,j)=>e.style.fontSize=(base[j]*k)+'px');}}
    t.style.justifyContent=jc;
  }});}}
  (document.fonts?document.fonts.ready:Promise.resolve()).then(shrink);
  fit();const h=parseInt(location.hash.slice(1));if(h)go(h-1);
  if(/present/.test(location.search)){{document.body.classList.add('present');fit();go(cur);}}
}})();
</script>
</body>
</html>
"""


def export_html(pptx: Path, out: Path, *, ir: DeckIR | None = None, title: str | None = None) -> Path:
    """Колода .pptx → самодостаточный .html рядом (или по `out`). `ir` — только для заметок к слайдам."""
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(HtmlExporter(Path(pptx), ir, title).build(), "utf-8")
    return out


__all__ = ["HtmlExporter", "eot_to_ttf", "export_html", "svg_chart"]
