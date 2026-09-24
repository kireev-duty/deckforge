"""audit/context + parsing/dna на реальном шаблоне и на сгенерированной колоде (без LibreOffice и LLM)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from deckforge.audit import audit_deck, report_markdown, summary
from deckforge.audit.context import AuditContext
from deckforge.audit.deterministic import CHECKS
from deckforge.audit.text_metrics import TextMeasurer, fonts_available
from deckforge.core.ir import DeckIR, DeckOutline
from deckforge.core.strategy import load_strategy
from deckforge.core.units import EMU_PER_INCH
from deckforge.layout import build_deck_ir
from deckforge.parsing.dna import build_dna
from deckforge.render import render_pptx

REPO = Path(__file__).resolve().parents[1]
OUTLINE = REPO / "examples" / "content_pack" / "outline.json"


def test_build_dna_vk_tech(template_path) -> None:
    dna = build_dna(template_path("VK Tech"))
    g = dna.grid
    assert 0.1 * EMU_PER_INCH <= g.margin_left <= 1.5 * EMU_PER_INCH
    assert 0.1 * EMU_PER_INCH <= g.margin_top <= 1.5 * EMU_PER_INCH
    assert g.columns_x and g.rows_y and g.slide_w == dna.slide_w
    assert dna.exemplars and dna.fonts[0] == "Play"
    assert json.loads(dna.model_dump_json())["grid"]["margin_left"] == g.margin_left


def test_text_measurer_wraps_and_scales() -> None:
    m = TextMeasurer("Arial")
    w10, w20 = m.width("Согласование", 10), m.width("Согласование", 20)
    assert w10 > 0 and abs(w20 / w10 - 2) < 0.01
    lines = m.wrap_lines("одно два три четыре пять", 18, m.width("одно два три", 18) + 1)
    assert len(lines) >= 2 and lines[0].startswith("одно")
    assert m.block_height(["a", "b"], 18, 1000) > m.block_height(["a"], 18, 1000)
    if fonts_available():
        # поправка прокси: кириллица Play в рендере LibreOffice — как Arial (замер по WorkSpace), Montserrat шире
        assert TextMeasurer("Play").width("текст", 18) == pytest.approx(m.width("текст", 18))
        assert TextMeasurer("Montserrat").width("текст", 18) > m.width("текст", 18)


def _generated_deck(template_path, tmp_path: Path, strategy: str = "narrative"):
    pptx = template_path("VK Tech")
    dna = build_dna(pptx)
    outline = DeckOutline.model_validate_json(OUTLINE.read_text("utf-8"))
    res = build_deck_ir(outline, load_strategy(strategy), dna.exemplars, dna.template_id, dna.slide_w, dna.slide_h,
                        {"accent": "0077FF", "font": "Play", "palette": "0077FF,00AEE8"})
    out = render_pptx(res.ir, pptx, dna.exemplars, tmp_path / f"{strategy}.pptx")
    return out, dna, res.ir


def test_context_on_generated_deck(template_path, tmp_path: Path) -> None:
    out, dna, ir = _generated_deck(template_path, tmp_path)
    ctx = AuditContext(out, dna, ir)
    assert len(ctx.slides) == len(ir.slides)
    assert "play" in ctx.allowed_fonts and ctx.scale_sizes and "0077FF" in ctx.palette
    first = ctx.slides[0]
    assert first.exemplar is not None and first.ir is ir.slides[0] and first.layout_name
    title = next(s for s in first.shapes if s.has_text and s.is_ours)
    run = title.main_run
    assert run is not None and run.font == "Play" and run.size_pt > 0 and run.color
    # заполненные слоты — наши, декор образца — нет
    assert any(s.is_ours for s in first.shapes) and all(not s.is_ours for s in first.shapes if s.is_decor)
    kpi = next((s for s in ctx.slides if any(sh.has_text and len(sh.text) <= 4 and sh.main_run.size_pt > 60
                                            for sh in s.shapes if sh.main_run)), None)
    assert kpi is not None, "ожидался KPI-слайд с крупной цифрой"
    chart_slide = next((s for s in ctx.slides if any(sh.chart for sh in s.shapes)), None)
    if chart_slide is not None:
        ch = next(sh.chart for sh in chart_slide.shapes if sh.chart)
        assert ch.n_series >= 1 and ch.kind.endswith("Chart")


def test_audit_generated_deck_runs_all_checks(template_path, tmp_path: Path) -> None:
    out, dna, ir = _generated_deck(template_path, tmp_path)
    report = audit_deck(out, dna, ir)
    assert report.checks_run == list(CHECKS) and report.duration_s < 10
    ids = {f.check_id for f in report.findings}
    # файл открывается, лейауты и фиксированные элементы на месте
    assert not ids & {"I01_file", "I04_raster_slide", "T04_layout", "T05_fixed_moved", "T01_font"}
    for f in report.findings:
        assert 0 <= f.slide_idx < len(ir.slides) and f.message
    print(report_markdown(report))
    print(summary(report))


def test_t05_ignores_empty_placeholders_of_exemplar(template_path, tmp_path: Path) -> None:
    """Пустой плейсхолдер образца из fixed писатель убирает, и T05 не считает это удалением."""
    from deckforge.core.ir import Element, Paragraph, SlideIR, SlotKind, TextRun

    pptx = template_path("VK Education")
    dna = build_dna(pptx)
    ctx_src = AuditContext(pptx, dna)
    e = next((e for e in dna.exemplars if ctx_src.exemplar_empty_placeholders(e) & set(e.fixed)
              and any(s.kind == SlotKind.TITLE for s in e.slots)), None)
    assert e is not None, "ожидался образец с пустым плейсхолдером среди fixed"
    title = next(s for s in e.slots if s.kind == SlotKind.TITLE)
    el = Element(slot_id=title.id, kind=SlotKind.TITLE, box=title.box, paragraphs=[Paragraph(runs=[TextRun(text="Т")])])
    ir = DeckIR(template_id=dna.template_id, strategy="test", slide_w=dna.slide_w, slide_h=dna.slide_h,
                slides=[SlideIR(idx=0, exemplar_id=e.id, archetype=e.archetype, elements=[el], outline_ref=0)])
    out = render_pptx(ir, pptx, dna.exemplars, tmp_path / "closing.pptx")
    report = audit_deck(out, dna, ir, checks=["T05"])
    assert not report.findings, [f.message for f in report.findings]
