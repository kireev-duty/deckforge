"""core/deck_reader на финальной колоде дня 11: данные диаграмм и таблиц совпадают с IR, свойства абзацев читаются."""

from __future__ import annotations

from pathlib import Path

import pytest

from deckforge.core.deck_reader import background_picture_part, read_shapes
from deckforge.core.ir import DeckIR, SlotKind
from deckforge.core.package import Package, PartCtx

REPO = Path(__file__).resolve().parents[1]
DECK = REPO / "examples" / "output" / "vk_tech" / "narrative.pptx"


@pytest.fixture(scope="module")
def deck():
    if not DECK.exists() or DECK.stat().st_size < 10_000:
        pytest.skip("нет examples/output/vk_tech/narrative.pptx (LFS?)")
    ir = DeckIR.model_validate_json(DECK.with_suffix(".ir.json").read_text("utf-8"))
    pkg = Package(DECK)
    slides = []
    for part in pkg.slides:
        ctx = PartCtx.for_slide(pkg, part)
        assert ctx is not None
        slides.append((ctx, read_shapes(ctx)))
    return ir, pkg, slides


def _elements(ir: DeckIR, kind: SlotKind):
    return [(s.idx, e) for s in ir.slides for e in s.elements if e.kind == kind]


def test_chart_spec_matches_ir(deck) -> None:
    ir, _, slides = deck
    charts = _elements(ir, SlotKind.CHART)
    assert charts, "в narrative нет диаграммы?"
    for idx, el in charts:
        recs = [s for s in slides[idx][1] if s.chart is not None and s.chart.spec is not None]
        assert recs, f"слайд {idx}: диаграмма не прочитана"
        spec = recs[0].chart.spec
        assert spec.kind == el.chart.kind
        assert spec.categories == el.chart.categories
        for name, vals in el.chart.series.items():
            assert name in spec.series and spec.series[name] == pytest.approx(vals)
        assert recs[0].chart.series_colors, "цвета серий из палитры не прочитаны"


def test_table_cells_match_ir(deck) -> None:
    ir, _, slides = deck
    tables = _elements(ir, SlotKind.TABLE)
    assert tables, "в narrative нет таблицы?"
    for idx, el in tables:
        recs = [s for s in slides[idx][1] if s.table is not None]
        assert recs
        t = recs[0].table
        assert t.rows == len(el.table.rows) + 1 and t.cols == len(el.table.header)
        assert [c.text for c in t.cells[0]] == el.table.header
        assert [[c.text for c in row] for row in t.cells[1:]] == el.table.rows
        assert len(t.col_widths_emu) == t.cols and all(w > 0 for w in t.col_widths_emu)


def test_paragraph_and_shape_props(deck) -> None:
    ir, pkg, slides = deck
    texts = [s for _, shapes in slides for s in shapes if s.has_text]
    assert texts
    assert {p.align for s in texts for p in s.paragraphs} <= {"l", "ctr", "r", "just", "dist"}
    assert {s.anchor for s in texts} <= {"t", "ctr", "b", "just", "dist"}
    bullets = [p for s in texts for p in s.paragraphs if p.bullet]  # у VK Tech списков может не быть — карточки
    assert all(p.bullet_char or p.bullet_auto for p in bullets)
    assert all(r.size_pt > 0 and r.font for s in texts for r in s.runs)
    assert all(s.line_w_emu > 0 for _, shapes in slides for s in shapes if s.line)
    pics = [s for _, shapes in slides for s in shapes if s.picture is not None]
    assert pics and all(p.picture.part in pkg.names for p in pics)


def test_background_picture_lookup(deck) -> None:
    _, pkg, slides = deck
    for ctx, _ in slides:
        part = background_picture_part(ctx)
        assert part is None or part in pkg.names
