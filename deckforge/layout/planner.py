"""Применение стратегии к DeckOutline: визуализация данных, плотность, разделы, объём.

Контент не выдумывается — все тексты и числа из исходного outline.
"""

from __future__ import annotations

import logging
import math
import re
from dataclasses import dataclass, field

from deckforge.core.ir import Archetype, ChartSpec, DeckOutline, KpiSpec, OutlineSlide, TableSpec
from deckforge.core.strategy import Strategy
from deckforge.layout.fitting import shorten_words

log = logging.getLogger(__name__)

# колонки-«дельты» в диаграмму не идут: у них другая единица
_DELTA_HEADER = re.compile(r"измен|разниц|дельт|Δ|прирост|%", re.IGNORECASE)
_NUM = re.compile(r"^[+\-−–]?\d+(?:[.,]\d+)?$")
# что можно слить в карточки при переборе объёма
_MERGEABLE = (Archetype.BULLETS, Archetype.TWO_COLUMN, Archetype.CARDS, Archetype.PROCESS, Archetype.KPI, Archetype.QUOTE)
_MERGE_LAST = (Archetype.KPI, Archetype.QUOTE)  # только когда текстовых пар не осталось
STEP_NUMBERING = "{n}. {text}"


@dataclass
class PlanResult:
    slides: list[OutlineSlide]
    warnings: list[str] = field(default_factory=list)

    @property
    def archetypes(self) -> list[str]:
        return [s.archetype.value for s in self.slides]


def plan(
    outline: DeckOutline, strategy: Strategy, available: set[Archetype] | None = None, kpi_capacity: int = 0,
) -> PlanResult:
    """Спланировать состав слайдов под стратегию.

    `available` — архетипы, для которых в шаблоне есть образцы; `kpi_capacity` — сколько крупных цифр
    вмещает лучший KPI-образец (0 — не ограничивать).
    """
    available = available or set(Archetype)
    warnings: list[str] = []
    sane = [sanitize_data(s, warnings) for s in outline.slides]
    kept = [s for s in sane if not _is_empty(s)]
    # если содержательных слайдов не осталось вовсе — колоду не выбрасываем, верстаем как есть
    if any(s.archetype not in _STRUCTURAL for s in kept):
        for s in sane:
            if _is_empty(s):
                warnings.append(f"слайд {s.idx} «{s.title[:40]}»: без содержимого — пропущен")
        sane = kept
    slides = [visualize(s, strategy) for s in sane]
    slides = split_mixed_data(slides)
    slides = apply_density(slides, strategy)
    slides = split_kpis(slides, strategy, kpi_capacity)
    slides = apply_sections(slides, strategy, available)
    slides = fit_count(slides, strategy, warnings)
    for i, s in enumerate(slides):
        s.idx = i
    return PlanResult(slides, warnings)


# ──────────────────────────── 0. санация данных ────────────────────────────

# структурные слайды живут без тела: у них содержание — заголовок и подзаголовок
_STRUCTURAL = (Archetype.TITLE, Archetype.SECTION, Archetype.CLOSING)


def _is_empty(s: OutlineSlide) -> bool:
    """Контентный слайд, на котором остался бы один заголовок (I03 в аудите).

    Готовый outline (`--outline`, build_variants) идёт мимо `content.outline_writer.repair_outline`,
    поэтому пустой слайд снимается и здесь.
    """
    if s.archetype in _STRUCTURAL:
        return False
    return not (s.bullets or s.paragraphs or s.kpis or s.steps or s.chart or s.table or s.quote or s.image)


def sanitize_data(src: OutlineSlide, warnings: list[str]) -> OutlineSlide:
    """Привести данные слайда к форме, которую переживут planner и нативные объекты.

    Серии одной длины с категориями, только конечные числа, ровные строки таблицы; что не собрать —
    убирается с предупреждением. Возвращает копию или исходный слайд, если править нечего."""
    s = src
    label = f"слайд {src.idx} «{src.title[:40]}»"
    if s.chart is not None:
        c = s.chart
        series = {name: vals for name, vals in c.series.items() if vals and all(math.isfinite(v) for v in vals)}
        n = min([len(c.categories), *map(len, series.values())]) if series else 0
        if n == 0:
            warnings.append(f"{label}: диаграмма без данных (пустые/нечисловые серии или категории) — убрана")
            s = s.model_copy(deep=True)
            s.chart = None
            if s.archetype == Archetype.CHART:
                s.archetype = Archetype.BULLETS
        elif len(series) != len(c.series) or n != len(c.categories) or any(len(v) != n for v in series.values()):
            warnings.append(f"{label}: серии диаграммы выровнены по {n} категориям")
            s = s.model_copy(deep=True)
            s.chart = c.model_copy(update={"categories": c.categories[:n], "series": {k: v[:n] for k, v in series.items()}})
    if s.table is not None:
        t = s.table
        cols = len(t.header)
        if cols == 0:
            warnings.append(f"{label}: таблица без колонок — убрана")
            s = s.model_copy(deep=True)
            s.table = None
            if s.archetype == Archetype.TABLE:
                s.archetype = Archetype.BULLETS
        elif any(len(r) != cols for r in t.rows):
            warnings.append(f"{label}: строки таблицы выровнены по {cols} колонкам")
            s = s.model_copy(deep=True)
            s.table = TableSpec(header=list(t.header), rows=[(list(r) + [""] * cols)[:cols] for r in t.rows])
    return s


# ──────────────────────────── 1. визуализация данных ────────────────────────────


def visualize(src: OutlineSlide, strategy: Strategy) -> OutlineSlide:
    """Превратить данные слайда в форму, заданную стратегией. Возвращает копию."""
    s = src.model_copy(deep=True)
    dv = strategy.data_visualization
    if s.chart is not None:
        if dv.numeric_series == "table":
            s.table, s.chart = chart_to_table(s.chart), None
            s.archetype = Archetype.TABLE
        elif dv.numeric_series == "kpi" and len(s.chart.series) <= 4:
            s.kpis, s.chart = chart_to_kpis(s.chart), None
            s.archetype = Archetype.KPI
        else:
            s.archetype = Archetype.CHART
    elif s.table is not None:
        if dv.comparison == "chart" and (chart := table_to_chart(s.table, s.title)) is not None:
            s.chart, s.table = chart, None
            s.archetype = Archetype.CHART
        elif dv.comparison == "two_column" and len(s.table.header) <= 3 and len(s.table.rows) <= 6:
            s.bullets, s.table = table_to_bullets(s.table), None
            s.archetype = Archetype.TWO_COLUMN
        else:
            s.archetype = Archetype.TABLE
    elif s.kpis and s.archetype in (Archetype.KPI, Archetype.CARDS, Archetype.TABLE):
        if dv.key_metrics == "table":
            s.table = TableSpec(header=["Показатель", "Значение"], rows=[[k.label, k.value] for k in s.kpis])
            s.kpis, s.archetype = [], Archetype.TABLE
        elif dv.key_metrics == "cards":
            s.bullets = [f"{k.value} — {k.label}" for k in s.kpis]
            s.kpis, s.archetype = [], Archetype.CARDS
        else:
            s.archetype = Archetype.KPI
    return s


def split_mixed_data(slides: list[OutlineSlide]) -> list[OutlineSlide]:
    """KPI рядом с диаграммой/таблицей — отдельным KPI-слайдом следом, иначе цифры лягут поверх объекта."""
    out: list[OutlineSlide] = []
    for s in slides:
        if s.kpis and (s.chart or s.table):
            rest = s.model_copy(deep=True)
            rest.kpis = []
            out.append(rest)
            out.append(OutlineSlide(idx=s.idx, archetype=Archetype.KPI, title=s.title, section=s.section,
                                    kpis=list(s.kpis), sources=list(s.sources)))
        else:
            out.append(s)
    return out


def chart_to_table(chart: ChartSpec) -> TableSpec:
    unit = f", {chart.unit}" if chart.unit else ""
    header = [chart.x_label or ""] + [f"{name}{unit}" for name in chart.series]
    rows = [[cat] + [fmt_num(vals[i]) for vals in chart.series.values()] for i, cat in enumerate(chart.categories)]
    return TableSpec(header=header, rows=rows)


def chart_to_kpis(chart: ChartSpec) -> list[KpiSpec]:
    """Последнее значение каждой серии как KPI (подпись — серия и последняя категория)."""
    last = chart.categories[-1] if chart.categories else ""
    unit = f" {chart.unit}" if chart.unit else ""
    return [KpiSpec(value=f"{fmt_num(vals[-1])}{unit}", label=f"{name}, {last}".strip(", ")) for name, vals in chart.series.items() if vals]


def table_to_chart(table: TableSpec, title: str) -> ChartSpec | None:
    """Таблица → столбчатая диаграмма, если хотя бы одна колонка (кроме первой) целиком числовая."""
    if len(table.header) < 2 or not table.rows:
        return None
    series: dict[str, list[float]] = {}
    for j, name in enumerate(table.header[1:], start=1):
        if _DELTA_HEADER.search(name):
            continue
        vals = [parse_num(r[j]) if j < len(r) else None for r in table.rows]
        if all(v is not None for v in vals):
            series[name] = [float(v) for v in vals]  # type: ignore[arg-type]
    if not series:
        return None
    # заголовок слайда уже есть, над графиком не дублируем
    return ChartSpec(kind="column", title="", categories=[r[0] for r in table.rows], series=series)


def table_to_bullets(table: TableSpec) -> list[str]:
    heads = [h.strip().lower() for h in table.header[1:]]
    out = []
    for r in table.rows:
        cells = [f"{h} {v}".strip() for h, v in zip(heads, r[1:])]
        out.append(f"{r[0]} — {', '.join(cells)}")
    return out


def parse_num(cell: str) -> float | None:
    """'1 250', '−89 %', '4,7', '×3' → число; текст → None."""
    s = cell.replace(" ", "").replace(" ", "").replace("%", "").replace("×", "").replace("x", "")
    s = s.replace("−", "-").replace("–", "-").replace(",", ".")
    if not _NUM.match(s):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def fmt_num(v: float) -> str:
    if float(v).is_integer():
        return f"{int(v):,}".replace(",", " ")
    return f"{v:.1f}".replace(".", ",")


def split_kpis(slides: list[OutlineSlide], strategy: Strategy, capacity: int) -> list[OutlineSlide]:
    """KPI-слайд с числом цифр больше вместимости образца → несколько KPI-слайдов, пока есть запас по объёму."""
    if strategy.data_visualization.key_metrics != "kpi" or capacity < 2:
        return slides
    out: list[OutlineSlide] = []
    room = strategy.target_slides.max - len(slides)
    for s in slides:
        n_parts = math.ceil(len(s.kpis) / capacity) if s.archetype == Archetype.KPI else 1
        if n_parts <= 1 or n_parts - 1 > room:
            out.append(s)
            continue
        room -= n_parts - 1
        size = math.ceil(len(s.kpis) / n_parts)
        for i in range(n_parts):
            part = s.model_copy(deep=True) if i == 0 else OutlineSlide(
                idx=s.idx, archetype=Archetype.KPI, title=s.title, section=s.section, sources=list(s.sources),
            )
            part.kpis = s.kpis[i * size : (i + 1) * size]
            part.title = f"{s.title} ({i + 1}/{n_parts})"
            out.append(part)
    return out


# ──────────────────────────── 3. разделы ────────────────────────────


def apply_sections(slides: list[OutlineSlide], strategy: Strategy, available: set[Archetype]) -> list[OutlineSlide]:
    if not strategy.sections or Archetype.SECTION not in available:
        return [s for s in slides if s.archetype != Archetype.SECTION]
    out: list[OutlineSlide] = []
    current: str | None = None
    for s in slides:
        if s.archetype == Archetype.SECTION:
            current = s.section or s.title
            out.append(s)
            continue
        if s.section and s.section != current and s.archetype not in (Archetype.TITLE, Archetype.CLOSING):
            out.append(OutlineSlide(idx=0, archetype=Archetype.SECTION, title=s.section, section=s.section))
            current = s.section
        out.append(s)
    return out


# ──────────────────────────── 2. плотность ────────────────────────────


def apply_density(slides: list[OutlineSlide], strategy: Strategy) -> list[OutlineSlide]:
    d = strategy.density
    out: list[OutlineSlide] = []
    for s in slides:
        s.bullets = [shorten_words(b, d.max_words_per_bullet) for b in s.bullets]
        s.steps = [shorten_words(b, d.max_words_per_bullet) for b in s.steps]
        out.extend(split_slide(s, d.max_bullets))
    return out


def split_slide(s: OutlineSlide, max_items: int, items_attr: str = "bullets") -> list[OutlineSlide]:
    """Разбить слайд с длинным списком на части; данные остаются в первой. Шаги процесса не режем."""
    if len(getattr(s, items_attr)) <= max_items or s.archetype == Archetype.AGENDA:
        return [s]
    items = getattr(s, items_attr)
    n_parts = math.ceil(len(items) / max_items)
    size = math.ceil(len(items) / n_parts)  # ровные части
    parts: list[OutlineSlide] = []
    for i in range(n_parts):
        part = s.model_copy(deep=True) if i == 0 else OutlineSlide(
            idx=s.idx, archetype=s.archetype, title=s.title, section=s.section, sources=list(s.sources),
        )
        setattr(part, items_attr, items[i * size : (i + 1) * size])
        part.title = f"{s.title} ({i + 1}/{n_parts})"
        parts.append(part)
    return parts


# ──────────────────────────── 4. объём ────────────────────────────


def fit_count(slides: list[OutlineSlide], strategy: Strategy, warnings: list[str]) -> list[OutlineSlide]:
    lo, hi = strategy.target_slides.min, strategy.target_slides.max
    if len(slides) > hi:
        # разделители — первые на вылет, начиная с самых коротких разделов
        while len(slides) > hi and any(s.archetype == Archetype.SECTION for s in slides):
            i = _shortest_section(slides)
            slides = slides[:i] + slides[i + 1 :]
        while len(slides) > hi:
            merged = merge_pair(slides, strategy.density.max_bullets)
            if merged is None:
                break
            slides = merged
    if len(slides) < lo:
        while len(slides) < lo:
            grown = extract_kpis(slides) if strategy.data_visualization.key_metrics == "kpi" else None
            grown = grown or split_longest(slides)
            if grown is None:
                break
            slides = grown
    if not lo <= len(slides) <= hi:
        warnings.append(f"{strategy.name}: {len(slides)} слайдов вне диапазона {lo}–{hi}")
        log.warning(warnings[-1])
    return slides


def _shortest_section(slides: list[OutlineSlide]) -> int:
    """Индекс разделителя самого короткого раздела (при равенстве — последний)."""
    idxs = [i for i, s in enumerate(slides) if s.archetype == Archetype.SECTION]
    bounds = idxs + [len(slides)]
    lengths = [(bounds[k + 1] - bounds[k], -bounds[k]) for k in range(len(idxs))]
    return -min(lengths)[1]


def merge_pair(slides: list[OutlineSlide], max_bullets: int) -> list[OutlineSlide] | None:
    """Слить первую подходящую пару соседних слайдов в один CARDS; None — сливать нечего.

    Сначала текстовые пары, KPI и цитата — в последнюю очередь: в карточках они теряют больше."""
    for lossy in (False, True):
        for i in range(len(slides) - 1):
            a, b = slides[i], slides[i + 1]
            if not (_mergeable(a) and _mergeable(b)) or len(_items(a)) + len(_items(b)) > max_bullets:
                continue
            if not lossy and any(x.archetype in _MERGE_LAST for x in (a, b)):
                continue
            return slides[:i] + [_merge(a, b)] + slides[i + 2 :]
    return None


def _merge(a: OutlineSlide, b: OutlineSlide) -> OutlineSlide:
    merged = a.model_copy(deep=True)
    merged.archetype = Archetype.CARDS
    merged.subtitle = b.title
    merged.bullets = _items(a) + _items(b)
    merged.steps, merged.kpis, merged.quote, merged.quote_author, merged.paragraphs = [], [], None, None, []
    merged.image = a.image or b.image
    merged.sources = list(dict.fromkeys(a.sources + b.sources))
    merged.speaker_notes = "\n".join(x for x in (a.speaker_notes, b.speaker_notes) if x)
    return merged


def extract_kpis(slides: list[OutlineSlide]) -> list[OutlineSlide] | None:
    """Вынести kpis со смешанного слайда (kpi + буллеты) в отдельный KPI-слайд после него."""
    for i, s in enumerate(slides):
        if s.kpis and (s.bullets or s.chart or s.table) and s.archetype != Archetype.KPI:
            kpi = OutlineSlide(idx=s.idx, archetype=Archetype.KPI, title=s.title, section=s.section,
                               kpis=list(s.kpis), sources=list(s.sources))
            rest = s.model_copy(deep=True)
            rest.kpis = []
            return slides[: i] + [rest, kpi] + slides[i + 1 :]
    return None


def split_longest(slides: list[OutlineSlide]) -> list[OutlineSlide] | None:
    """Разбить пополам самый длинный текстовый слайд (≥ 4 буллетов)."""
    cands = [(len(s.bullets), i) for i, s in enumerate(slides) if _text_only(s) and len(s.bullets) >= 4]
    if not cands:
        return None
    _, i = max(cands)
    parts = split_slide(slides[i], math.ceil(len(slides[i].bullets) / 2))
    return slides[:i] + parts + slides[i + 1 :]


def _text_only(s: OutlineSlide) -> bool:
    return (
        s.archetype in _MERGEABLE and bool(s.bullets)
        and not (s.kpis or s.chart or s.table or s.image or s.steps or s.quote)
    )


def _mergeable(s: OutlineSlide) -> bool:
    """Слайд с одним видом текстового контента, который можно превратить в пункты карточек."""
    if s.chart or s.table:
        return False
    if s.archetype == Archetype.IMAGE_TEXT:
        # без готовой картинки это обычный текст (executive картинок не генерирует); с картинкой — разворот, не трогаем
        if s.image is not None and s.image.path:
            return False
    elif s.archetype not in _MERGEABLE:
        return False  # картинка слиянию не мешает
    kinds = sum(1 for x in (s.bullets, s.steps, s.kpis, s.quote, s.paragraphs) if x)
    return kinds == 1


def _items(s: OutlineSlide) -> list[str]:
    """Пункты слайда для слияния."""
    if s.bullets:
        return list(s.bullets)
    if s.steps:
        return [STEP_NUMBERING.format(n=i + 1, text=t) for i, t in enumerate(s.steps)]
    if s.kpis:
        return [f"{k.value} — {k.label}" for k in s.kpis]
    if s.quote:
        return [f"«{s.quote}»" + (f" — {s.quote_author}" if s.quote_author else "")]
    if s.paragraphs:  # абзац image_text без картинки — такая же карточка
        return list(s.paragraphs)
    return []


__all__ = ["STEP_NUMBERING", "PlanResult", "chart_to_kpis", "chart_to_table", "parse_num", "plan", "sanitize_data", "split_kpis", "split_mixed_data", "split_slide", "table_to_chart", "visualize"]
