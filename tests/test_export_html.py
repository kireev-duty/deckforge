"""export/html: самодостаточный HTML из готовой колоды — текст IR на месте, диаграммы SVG, таблицы."""

from __future__ import annotations

import html
import re
from pathlib import Path

import pytest

from deckforge.core.ir import ChartSpec, DeckIR, SlotKind
from deckforge.export.html import eot_to_ttf, export_html, svg_chart
from tests.conftest import build_sample_deck

REPO = Path(__file__).resolve().parents[1]
WILD = REPO / "data" / "wild" / "presentation_eng_dark.pptx"


def _ready(p: Path) -> bool:
    return p.exists() and p.stat().st_size > 10_000  # LFS-указатель весит ~130 байт


@pytest.fixture(scope="module")
def exported(tmp_path_factory) -> tuple[str, DeckIR]:
    deck, ir = build_sample_deck(tmp_path_factory.mktemp("deck"))
    out = export_html(deck, tmp_path_factory.mktemp("html") / "narrative.html", ir=ir, title="Пульс")
    assert out.stat().st_size < 3_000_000
    return out.read_text("utf-8"), ir


def test_structure(exported) -> None:
    doc, ir = exported
    assert doc.count('<section class="slide"') == len(ir.slides)
    assert "<title>Пульс</title>" in doc
    assert "<script src" not in doc and "http://" not in doc.replace("http://www.w3.org", "")
    assert re.search(r'<svg class="chart"', doc)
    n_charts = sum(1 for s in ir.slides for e in s.elements if e.kind == SlotKind.CHART)
    n_tables = sum(1 for s in ir.slides for e in s.elements if e.kind == SlotKind.TABLE)
    assert doc.count('<svg class="chart"') == n_charts and doc.count("<table") == n_tables
    assert "data:image/" in doc and "<symbol id=" in doc  # картинки вшиты один раз


def test_all_ir_text_present(exported) -> None:
    doc, ir = exported
    missing = []
    for s in ir.slides:
        for e in s.elements:
            for p in e.paragraphs:
                # run — отдельный <span>: у KPI число и единица измерения разного кегля
                for r in p.runs:
                    text = r.text.strip()
                    if text and html.escape(text) not in doc:
                        missing.append((s.idx, text[:40]))
    assert not missing, missing


def test_wild_deck_exports(tmp_path: Path) -> None:
    if not _ready(WILD):
        pytest.skip("нет data/wild")
    out = export_html(WILD, tmp_path / "wild.html")
    assert out.stat().st_size > 10_000 and '<section class="slide"' in out.read_text("utf-8")


def test_svg_chart_kinds() -> None:
    spec = ChartSpec(kind="column", title="T", categories=["a", "b"], series={"s1": [1, 2], "s2": [2, 1]})
    for kind in ("column", "bar", "line", "area", "pie", "doughnut"):
        svg = svg_chart(spec.model_copy(update={"kind": kind}), ["0077FF", "00AEE8"], 400, 300)
        assert svg.startswith("<svg") and svg.endswith("</svg>") and "#0077FF" in svg and "<title>" in svg
    assert 'class="legend"' in svg_chart(spec, [], 400, 300)
    single = spec.model_copy(update={"series": {"s1": [1, 2]}})
    assert 'class="legend"' not in svg_chart(single, [], 400, 300)  # одна серия — легенда не нужна


def test_eot_guard() -> None:
    assert eot_to_ttf(b"\x00" * 10) is None
    assert eot_to_ttf(b"\x00" * 40) is None
