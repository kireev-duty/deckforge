"""Нативные диаграммы python-pptx из ChartSpec.

Минимальная версия (день 5): тип, данные, заголовок, шрифт/цвета из style_overrides.
Полная стилизация под палитру DNA (сетка, подписи данных, толщина линий) — отдельный шаг.

style_overrides: font, size_pt, text_color, accent, palette ("0077FF,00AEE8,…"), data_labels (подписи значений).
"""

from __future__ import annotations

from pptx.chart.data import CategoryChartData
from pptx.dml.color import RGBColor
from pptx.enum.chart import XL_CHART_TYPE, XL_LEGEND_POSITION
from pptx.slide import Slide
from pptx.util import Pt

from deckforge.core.colors import apply_color_mods
from deckforge.core.ir import Box, ChartSpec

CHART_TYPES = {
    "bar": XL_CHART_TYPE.BAR_CLUSTERED,
    "column": XL_CHART_TYPE.COLUMN_CLUSTERED,
    "line": XL_CHART_TYPE.LINE_MARKERS,
    "pie": XL_CHART_TYPE.PIE,
    "doughnut": XL_CHART_TYPE.DOUGHNUT,
    "area": XL_CHART_TYPE.AREA,
}
DEFAULT_PALETTE = ["0077FF", "00AEE8", "212121", "8F8F8F", "FF0053", "FFB800"]


def palette_from(overrides: dict, n: int = 2) -> list[str]:
    """Цвета серий: palette → accent → дефолт; если цветов меньше n, добавляем оттенки (tint) первого."""
    raw = str(overrides.get("palette") or "")
    colors = [c.strip().lstrip("#").upper() for c in raw.split(",") if c.strip()]
    if not colors and overrides.get("accent"):
        colors = [str(overrides["accent"]).lstrip("#").upper()]
    if not colors:
        return DEFAULT_PALETTE
    base = list(colors)
    i = 0
    while len(colors) < n:
        i += 1
        k = max(0.2, 1 - 0.3 * i)
        colors.append(apply_color_mods(base[i % len(base) - 1 if len(base) > 1 else 0], {"tint": int(100000 * k)}))
    return colors


def add_chart(slide: Slide, spec: ChartSpec, box: Box, overrides: dict | None = None):
    overrides = overrides or {}
    data = CategoryChartData()
    data.categories = spec.categories
    for name, values in spec.series.items():
        data.add_series(name, values)
    frame = slide.shapes.add_chart(CHART_TYPES[spec.kind], box.x, box.y, box.w, box.h, data)
    chart = frame.chart

    font = chart.font
    if overrides.get("font"):
        font.name = str(overrides["font"])
    font.size = Pt(float(overrides.get("size_pt") or 12))
    if overrides.get("text_color"):
        font.color.rgb = RGBColor.from_string(str(overrides["text_color"]).lstrip("#"))

    chart.has_title = bool(spec.title)
    if spec.title:
        chart.chart_title.text_frame.text = spec.title
        tf_font = chart.chart_title.text_frame.paragraphs[0].runs[0].font
        tf_font.bold = True
        tf_font.size = Pt(float(overrides.get("size_pt") or 12) + 2)

    n_colors = len(spec.categories) if spec.kind in ("pie", "doughnut") else len(spec.series)
    colors = palette_from(overrides, n_colors)
    plot = chart.plots[0]
    if spec.kind in ("pie", "doughnut"):
        plot.vary_by_categories = True
        for i, point in enumerate(plot.series[0].points):
            point.format.fill.solid()
            point.format.fill.fore_color.rgb = RGBColor.from_string(colors[i % len(colors)])
    else:
        for i, series in enumerate(plot.series):
            rgb = RGBColor.from_string(colors[i % len(colors)])
            if spec.kind == "line":
                series.format.line.color.rgb = rgb
                series.format.line.width = Pt(2.25)
                series.marker.format.fill.solid()
                series.marker.format.fill.fore_color.rgb = rgb
            else:
                series.format.fill.solid()
                series.format.fill.fore_color.rgb = rgb
        if spec.kind in ("bar", "column"):
            plot.gap_width = 80
        chart.value_axis.has_major_gridlines = False
        chart.value_axis.format.line.fill.background()
        chart.category_axis.format.line.fill.background()
        axis_title = spec.unit or spec.y_label
        if axis_title:
            chart.value_axis.has_title = True
            chart.value_axis.axis_title.text_frame.text = axis_title
    if overrides.get("data_labels"):
        # подписи значений на точках (autofix add_chart_labels для I05); целые — без дробной части
        plot.has_data_labels = True
        integral = all(float(v).is_integer() for vs in spec.series.values() for v in vs)
        plot.data_labels.number_format = "0" if integral else "0.0"
        plot.data_labels.number_format_is_linked = False
        plot.data_labels.font.size = Pt(max(8.0, float(overrides.get("size_pt") or 12) - 2))
    chart.has_legend = len(spec.series) > 1 or spec.kind in ("pie", "doughnut")
    if chart.has_legend:
        chart.legend.position = XL_LEGEND_POSITION.BOTTOM
        chart.legend.include_in_layout = False
    return frame
