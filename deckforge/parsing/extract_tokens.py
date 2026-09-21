"""Токены дизайн-системы шаблона из фактического использования, а не из theme.xml.

Гистограммы цветов и кеглей по слайдам (вес 1.0), лейаутам и мастеру (0.25); роли цветов —
по площади и контрасту; типографика — бины кеглей относительно body.

Использование:
    python -m deckforge.parsing.extract_tokens <file.pptx> [--json out.json]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from lxml import etree
from pydantic import BaseModel, Field

from deckforge.core.colors import chroma, contrast_ratio, delta_e
from deckforge.core.ir import ColorToken, TypeToken
from deckforge.parsing.ooxml import (
    NS,
    Package,
    PartCtx,
    bbox,
    font_scale,
    iter_shapes,
    localname,
    owner_shape,
    placeholder,
)

# ──────────────────────────── настраиваемые пороги ────────────────────────────

SOURCE_WEIGHT = {"slides": 1.0, "layouts": 0.25, "master": 0.25}
COLOR_MERGE_DE = 8.0  # ΔE76, ниже которого два hex — один цвет
CHROMATIC_MIN_CHROMA = 20.0  # Lab-хрома: выше — «цветной»
NEUTRAL_MAX_CHROMA = 12.0  # ниже — «нейтральный»
TEXT_MIN_CONTRAST = 3.0
BG_COVER_RATIO = 0.6  # фигура крупнее — фон
SURFACE_MIN_AREA = 0.15  # площадь нейтральной заливки (в слайдах) для роли surface
SECONDARY_MIN_RATIO = 0.15  # secondary — если score ≥ 15 % от accent
MAX_COLORS = 16
MIN_COLOR_WEIGHT_RATIO = 0.01  # цвета слабее отбрасываются
TEXT_CHARS_PER_UNIT = 20  # столько символов текста ≈ одна заливка

SIZE_MERGE_RATIO = 0.08  # кегли ближе — один бин
BODY_RANGE_PT = (9.0, 24.0)
# роль → [нижняя, верхняя) граница как доля от body
SCALE_BANDS: list[tuple[str, float, float]] = [
    ("display", 2.4, float("inf")),
    ("h1", 1.6, 2.4),
    ("h2", 1.25, 1.6),
    ("h3", 1.08, 1.25),
    ("body", 0.92, 1.08),
    ("caption", 0.7, 0.92),
    ("small", 0.0, 0.7),
]
SYMBOL_FONTS = ("Wingdings", "Webdings", "Symbol", "MT Extra")
ROLE_ORDER = ("background", "text", "accent", "secondary", "surface", "muted", "unknown")


# ──────────────────────────── результат ────────────────────────────


class TemplateTokens(BaseModel):
    """Токены шаблона; имена полей совпадают с TemplateDNA."""

    template_id: str
    source_path: str
    slide_w: int
    slide_h: int
    colors: list[ColorToken]
    typography: list[TypeToken]
    fonts: list[str]
    embedded_fonts: list[str] = Field(default_factory=list)
    theme_colors: dict[str, str] = Field(default_factory=dict)
    theme_fonts: dict[str, str] = Field(default_factory=dict)  # major / minor
    stats: dict[str, int] = Field(default_factory=dict)

    def dna_kwargs(self) -> dict:
        """Поля для TemplateDNA(**tokens.dna_kwargs(), grid=..., fixed_elements=..., exemplars=...)."""
        return self.model_dump(exclude={"theme_fonts"})

    def palette(self, role: str) -> list[str]:
        return [c.hex for c in self.colors if c.role == role]

    def type_token(self, role: str) -> TypeToken | None:
        return next((t for t in self.typography if t.role == role), None)


# ──────────────────────────── сбор статистики ────────────────────────────


@dataclass
class ColorStat:
    bg_slides: float = 0.0  # слайдов с этим фоном (взвешенно)
    cover_area: float = 0.0  # площадь фигур-подложек
    fill_area: float = 0.0  # площадь заливок в долях слайда
    fill_count: float = 0.0
    line_count: float = 0.0
    text_chars: float = 0.0
    by_source: Counter = field(default_factory=Counter)
    members: list[str] = field(default_factory=list)  # hex, слитые в кластер

    def add(self, other: ColorStat) -> None:
        self.bg_slides += other.bg_slides
        self.cover_area += other.cover_area
        self.fill_area += other.fill_area
        self.fill_count += other.fill_count
        self.line_count += other.line_count
        self.text_chars += other.text_chars
        self.by_source.update(other.by_source)
        self.members.extend(other.members)

    @property
    def weight(self) -> float:
        return self.fill_count + self.line_count + self.text_chars / TEXT_CHARS_PER_UNIT + self.bg_slides

    @property
    def accent_score(self) -> float:
        return 3 * self.fill_count + 2 * self.line_count + self.text_chars / TEXT_CHARS_PER_UNIT

    @property
    def background_score(self) -> float:
        return self.bg_slides + self.cover_area


@dataclass
class RunSample:
    font: str
    size_pt: float
    bold: bool
    color: str | None
    chars: float  # символов × вес источника
    is_title: bool
    source: str


@dataclass
class Usage:
    colors: dict[str, ColorStat] = field(default_factory=lambda: defaultdict(ColorStat))
    runs: list[RunSample] = field(default_factory=list)
    stats: Counter = field(default_factory=Counter)


def _collect_part(ctx: PartCtx, usage: Usage, slide_area: int) -> None:
    w = SOURCE_WEIGHT[ctx.source]
    colors = usage.colors
    is_slide = ctx.source == "slides"

    bg, is_pic = ctx.background()
    if is_pic and is_slide:
        usage.stats["picture_backgrounds"] += 1
    for hex_, k in bg or []:
        colors[hex_].bg_slides += w * k
        colors[hex_].by_source[ctx.source] += w * k

    for sp in iter_shapes(ctx.sp_tree):
        tag = localname(sp)
        if is_slide:
            usage.stats["shapes"] += 1
        if tag == "pic":
            if is_slide:
                usage.stats["pictures"] += 1
            continue
        if tag == "graphicFrame":
            uri = (sp.find(".//a:graphicData", NS).get("uri", "") if sp.find(".//a:graphicData", NS) is not None else "")
            if is_slide and "chart" in uri:
                usage.stats["charts"] += 1
            if is_slide and "table" in uri:
                usage.stats["tables"] += 1
            _collect_paragraphs(ctx, usage, sp.iter(f"{{{NS['a']}}}p"), None, w)
            continue

        bb = bbox(sp)
        area = (bb[2] * bb[3] / slide_area) if bb else 0.0
        for hex_, k in ctx.shape_fill(sp):
            st = colors[hex_]
            st.fill_count += w * k
            st.fill_area += w * k * area
            st.by_source[ctx.source] += w * k
            if area >= BG_COVER_RATIO:
                st.cover_area += w * k * area
        for hex_, k in ctx.shape_line(sp):
            colors[hex_].line_count += w * k
            colors[hex_].by_source[ctx.source] += w * k
        if tag == "sp":
            _collect_paragraphs(ctx, usage, sp.findall("p:txBody/a:p", NS), sp, w)


def _collect_paragraphs(ctx: PartCtx, usage: Usage, paragraphs, sp: etree._Element | None, w: float) -> None:
    """Run абзацев → цвет текста по символам + сэмплы для типографики."""
    scale = font_scale(sp)
    ph = placeholder(sp) if sp is not None else None
    is_title = bool(ph and ph[0] in ("title", "ctrTitle"))
    for para in paragraphs:
        ppr = para.find("a:pPr", NS)
        lvl = int(ppr.get("lvl", "0")) if ppr is not None else 0
        owner = sp if sp is not None else owner_shape(para)
        for run in para:
            if localname(run) not in ("r", "fld"):
                continue
            chars = len((run.findtext("a:t", namespaces=NS) or "").strip())
            if not chars:
                continue
            chain = ctx.run_props_chain(run, para, owner, lvl)
            color = ctx.resolve_text_color(chain, owner)
            size = ctx.resolve_size(chain) * scale
            if scale < 1.0 and ctx.source == "slides":
                usage.stats["autofit_scaled_runs"] += 1
            if color:
                usage.colors[color].text_chars += w * chars
                usage.colors[color].by_source[ctx.source] += w * chars / TEXT_CHARS_PER_UNIT
            usage.runs.append(
                RunSample(ctx.resolve_font(chain, ph), size, ctx.resolve_bold(chain), color, w * chars, is_title, ctx.source)
            )
            if ctx.source == "slides":
                usage.stats["text_runs"] += 1
                usage.stats["text_chars"] += chars


def collect_usage(pkg: Package) -> Usage:
    usage = Usage()
    sw, sh = pkg.slide_size
    slide_area = max(sw * sh, 1)
    ctxs: list[PartCtx] = []
    for s in pkg.slides:
        if (c := PartCtx.for_slide(pkg, s)) is not None:
            ctxs.append(c)
    for l in pkg.layouts:
        if (c := PartCtx.for_layout(pkg, l)) is not None:
            ctxs.append(c)
    ctxs.extend(PartCtx.for_master(pkg, m) for m in pkg.masters)
    for ctx in ctxs:
        _collect_part(ctx, usage, slide_area)
    usage.stats["slides"] = len(pkg.slides)
    usage.stats["layouts"] = len(pkg.layouts)
    usage.stats["masters"] = len(pkg.masters)
    return usage


# ──────────────────────────── цвета: кластеры и роли ────────────────────────────


def cluster_colors(colors: dict[str, ColorStat], theme_values: set[str]) -> dict[str, ColorStat]:
    """Жадно сливает близкие цвета; представитель — цвет темы, если он в кластере."""
    clusters: list[tuple[str, ColorStat]] = []
    for hex_, st in sorted(colors.items(), key=lambda kv: -kv[1].weight):
        st.members = [hex_]
        for i, (rep, acc) in enumerate(clusters):
            if delta_e(rep, hex_) < COLOR_MERGE_DE:
                acc.add(st)
                clusters[i] = (rep, acc)
                break
        else:
            clusters.append((hex_, st))
    out: dict[str, ColorStat] = {}
    for rep, acc in clusters:
        themed = [m for m in acc.members if m in theme_values]
        key = max(themed, key=lambda m: colors[m].weight) if themed else rep
        out[key] = acc
    return out


def assign_roles(clusters: dict[str, ColorStat]) -> dict[str, str]:
    """hex → роль. Каждая из background/text/accent/secondary/muted назначается один раз."""
    if not clusters:
        return {}
    roles: dict[str, str] = {}
    free = lambda: [h for h in clusters if h not in roles]  # noqa: E731

    # background — чем залиты слайды; если фоны — картинки, то самая большая заливка
    scored = {h: st.background_score for h, st in clusters.items()}
    bg = max(scored, key=scored.get) if max(scored.values()) > 0 else max(clusters, key=lambda h: clusters[h].fill_area)
    roles[bg] = "background"

    # text — больше всего символов при достаточном контрасте к фону
    readable = [h for h in free() if contrast_ratio(h, bg) >= TEXT_MIN_CONTRAST and clusters[h].text_chars > 0]
    if readable:
        roles[max(readable, key=lambda h: clusters[h].text_chars)] = "text"

    # accent / secondary — хроматические по score
    chrom = sorted((h for h in free() if chroma(h) >= CHROMATIC_MIN_CHROMA), key=lambda h: -clusters[h].accent_score)
    if chrom:
        roles[chrom[0]] = "accent"
        if len(chrom) > 1 and clusters[chrom[1]].accent_score >= SECONDARY_MIN_RATIO * clusters[chrom[0]].accent_score:
            roles[chrom[1]] = "secondary"

    # surface — нейтральные подложки заметной площади
    for h in free():
        if chroma(h) < NEUTRAL_MAX_CHROMA and clusters[h].fill_area >= SURFACE_MIN_AREA:
            roles[h] = "surface"

    # muted — нейтральный цвет вторичного текста
    neutral = [h for h in free() if chroma(h) < NEUTRAL_MAX_CHROMA]
    if neutral:
        roles[max(neutral, key=lambda h: clusters[h].text_chars + clusters[h].fill_count)] = "muted"

    for h in free():
        roles[h] = "unknown"
    return roles


def build_color_tokens(usage: Usage, theme_colors: dict[str, str]) -> list[ColorToken]:
    clusters = cluster_colors(dict(usage.colors), set(theme_colors.values()))
    usage.stats["color_clusters"] = len(clusters)
    roles = assign_roles(clusters)
    top = max((st.weight for st in clusters.values()), default=0.0)
    tokens = []
    for hex_, st in clusters.items():
        role = roles.get(hex_, "unknown")
        if role == "unknown" and st.weight < MIN_COLOR_WEIGHT_RATIO * top:
            continue
        src = st.by_source.most_common(1)[0][0] if st.by_source else "slides"
        tokens.append(ColorToken(hex=hex_, role=role, usage=round(st.weight), source=src))
    # тема — только fallback, когда на слайдах хроматических цветов нет
    if not any(t.role == "accent" for t in tokens) and theme_colors.get("accent1"):
        tokens.append(ColorToken(hex=theme_colors["accent1"], role="accent", usage=0, source="theme"))
    tokens.sort(key=lambda t: (ROLE_ORDER.index(t.role), -t.usage))
    return tokens[:MAX_COLORS]


# ──────────────────────────── шрифты и типографика ────────────────────────────


def rank_fonts(runs: list[RunSample]) -> list[str]:
    chars: Counter[str] = Counter()
    for r in runs:
        if r.font and not r.font.startswith(SYMBOL_FONTS):
            chars[r.font] += r.chars
    if not chars:
        return []
    top = chars.most_common(1)[0][1]
    return [f for f, n in chars.most_common() if n >= MIN_COLOR_WEIGHT_RATIO * top]


@dataclass
class SizeBin:
    size_pt: float
    runs: list[RunSample] = field(default_factory=list)

    @property
    def chars(self) -> float:
        return sum(r.chars for r in self.runs)

    @property
    def title_chars(self) -> float:
        return sum(r.chars for r in self.runs if r.is_title)


def _size_bins(runs: list[RunSample]) -> list[SizeBin]:
    """Бины по кеглю с шагом 0.5 pt; соседние ближе SIZE_MERGE_RATIO сливаются."""
    raw: dict[float, list[RunSample]] = defaultdict(list)
    for r in runs:
        raw[round(r.size_pt * 2) / 2].append(r)
    bins: list[SizeBin] = []
    for size in sorted(raw):
        if bins and size / bins[-1].size_pt - 1 < SIZE_MERGE_RATIO:
            b = bins[-1]
            b.runs.extend(raw[size])
            b.size_pt = sum(r.size_pt * r.chars for r in b.runs) / max(b.chars, 1e-9)
        else:
            bins.append(SizeBin(size, list(raw[size])))
    return bins


def _mode(runs: list[RunSample], key) -> object:
    c: Counter = Counter()
    for r in runs:
        c[key(r)] += r.chars
    return c.most_common(1)[0][0]


def build_typography(runs: list[RunSample]) -> list[TypeToken]:
    if not runs:
        return []
    bins = _size_bins(runs)
    in_range = [b for b in bins if BODY_RANGE_PT[0] <= b.size_pt <= BODY_RANGE_PT[1]]
    body = max(in_range or bins, key=lambda b: b.chars)

    role_of: dict[int, str] = {}  # индекс бина → роль
    for role, lo, hi in SCALE_BANDS:
        band = [i for i, b in enumerate(bins) if lo <= b.size_pt / body.size_pt < hi]
        if band:
            role_of[max(band, key=lambda i: bins[i].chars)] = role
    # h1 нет, но есть заголовочные плейсхолдеры крупнее body — их кегль и есть h1
    if "h1" not in role_of.values():
        titled = [i for i, b in enumerate(bins) if b.size_pt > body.size_pt and b.title_chars > 0]
        if titled:
            i = max(titled, key=lambda i: bins[i].title_chars)
            role_of[i] = "h1"

    tokens = []
    for i, role in role_of.items():
        b = bins[i]
        tokens.append(
            TypeToken(
                role=role,
                font=str(_mode(b.runs, lambda r: r.font)),
                size_pt=round(b.size_pt, 1),
                bold=bool(_mode(b.runs, lambda r: r.bold)),
                color=_mode(b.runs, lambda r: r.color) or None,  # type: ignore[arg-type]
                usage=round(b.chars),
            )
        )
    order = [r for r, _, _ in SCALE_BANDS]
    tokens.sort(key=lambda t: order.index(t.role))
    return tokens


# ──────────────────────────── точка входа ────────────────────────────


def extract_tokens(path: str | Path) -> TemplateTokens:
    path = Path(path)
    pkg = Package(path)
    sw, sh = pkg.slide_size
    theme = pkg.theme(pkg.masters[0]) if pkg.masters else None
    theme_colors = dict(theme.colors) if theme else {}
    theme_fonts = {"major": theme.major_font, "minor": theme.minor_font} if theme else {}

    usage = collect_usage(pkg)
    colors = build_color_tokens(usage, theme_colors)
    return TemplateTokens(
        template_id=hashlib.sha1(path.read_bytes()).hexdigest()[:12],
        source_path=str(path),
        slide_w=sw,
        slide_h=sh,
        colors=colors,
        typography=build_typography(usage.runs),
        fonts=rank_fonts(usage.runs),
        embedded_fonts=pkg.embedded_fonts,
        theme_colors=theme_colors,
        theme_fonts=theme_fonts,
        stats={k: int(v) for k, v in usage.stats.items()},
    )


def print_summary(t: TemplateTokens) -> None:
    p = print
    p(f"=== {Path(t.source_path).name}  id={t.template_id}  {t.slide_w}x{t.slide_h} EMU")
    p(f"stats  : {t.stats}")
    p(f"theme  : fonts={t.theme_fonts}  colors={' '.join(f'{k}=#{v}' for k, v in t.theme_colors.items())}")
    p(f"fonts  : {', '.join(t.fonts)}   embedded: {t.embedded_fonts}")
    p("colors :")
    for c in t.colors:
        p(f"  {c.role:<10} #{c.hex}  usage={c.usage:<6} src={c.source}")
    p("type   :")
    for tt in t.typography:
        p(f"  {tt.role:<8} {tt.size_pt:>5}pt  {tt.font:<18} {'bold' if tt.bold else '    '}  #{tt.color}  chars={tt.usage}")
    p()


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("files", nargs="+", type=Path)
    ap.add_argument("--json", type=Path, help="сохранить TemplateTokens в JSON")
    a = ap.parse_args(argv)
    results = []
    for f in a.files:
        t = extract_tokens(f)
        results.append(t.model_dump())
        print_summary(t)
    if a.json:
        a.json.write_text(json.dumps(results if len(results) > 1 else results[0], ensure_ascii=False, indent=1), "utf-8")
        print(f"json → {a.json}")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
