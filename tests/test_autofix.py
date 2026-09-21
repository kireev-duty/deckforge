"""Автофиксы: каталог полный, безопасные фиксы применяются к IR, после повторного рендера аудит чист."""

from __future__ import annotations

from pathlib import Path

from deckforge.audit import audit_deck
from deckforge.core.autofix import FIXES, fix_plan_rows, is_fixable, plan_fixes
from deckforge.core.ir import (
    AuditReport, Box, ChartSpec, DeckIR, DeckOutline, Element, Finding, Paragraph, Severity, SlideIR, SlotKind, TextRun,
)
from deckforge.core.strategy import load_strategy
from deckforge.layout import apply_fixes, build_deck_ir
from deckforge.parsing.dna import build_dna
from deckforge.render import render_pptx
from tests.fixtures.bad_slides import FIXTURES

REPO = Path(__file__).resolve().parents[1]
OUTLINE = REPO / "examples" / "content_pack" / "outline.json"


def _finding(check_id: str, fix: str | None, slide_idx: int = 0, element_id: str | None = "1", sev=Severity.WARNING,
             **evidence) -> Finding:
    return Finding(check_id=check_id, kind="deterministic", severity=sev, slide_idx=slide_idx, element_id=element_id,
                   message=check_id, autofix=fix, evidence=evidence)


def _slide(*elements: Element, idx: int = 0) -> SlideIR:
    return SlideIR(idx=idx, exemplar_id="slide1", archetype="bullets", elements=list(elements), outline_ref=idx)


def _text(slot_id: str, text: str, kind=SlotKind.BODY, **overrides) -> Element:
    return Element(slot_id=slot_id, kind=kind, box=Box(x=0, y=0, w=914400, h=914400), style_overrides=overrides,
                   paragraphs=[Paragraph(runs=[TextRun(text=text)])])


def _deck(*slides: SlideIR) -> DeckIR:
    return DeckIR(template_id="t", strategy="executive", slides=list(slides), slide_w=12192000, slide_h=6858000)


# ──────────────────────────── каталог ────────────────────────────


def test_catalogue_covers_every_autofix_name_the_checks_emit(tmp_path: Path) -> None:
    emitted: set[str] = set()
    for name, make in FIXTURES.items():
        (tmp_path / name).mkdir()
        case = make(tmp_path / name)
        report = audit_deck(case.pptx, case.dna, case.ir)
        emitted |= {f.autofix for f in report.findings if f.autofix}
    assert emitted and emitted <= set(FIXES), emitted - set(FIXES)


def test_plan_fixes_modes() -> None:
    findings = [
        _finding("L03_text_overflow", "shrink_font_by_scale", sev=Severity.ERROR, need_pt=20, have_pt=10),
        _finding("L03_text_overflow", "shrink_font_by_scale", need_pt=20, have_pt=10, in_exemplar=1),  # шаблон
        _finding("T02_font_size", "snap_font_size", sev=Severity.INFO, size_pt=10, nearest_pt=12),  # info
        _finding("T06_contrast", "snap_color", contrast=3.2),  # нет nearest
        _finding("I06_duplicate_slides", "drop_slide", element_id=None),  # ir, но не safe
        _finding("D01_bullets", "split_slide"),  # replan
    ]
    report = AuditReport(deck_path="x", findings=findings, checks_run=[])
    assert [f.check_id for f in plan_fixes(report, "safe")] == ["L03_text_overflow"]
    assert [f.check_id for f in plan_fixes(report, "all")] == ["L03_text_overflow", "I06_duplicate_slides"]
    assert [f.check_id for f in plan_fixes(report, [4, 5])] == ["I06_duplicate_slides"]
    assert not is_fixable(findings[1]) and not is_fixable(findings[2]) and not is_fixable(findings[3])
    rows = fix_plan_rows(report)
    assert [r["how"] for r in rows] == ["safe", "template", "n/a", "n/a", "ir", "replan"]


# ──────────────────────────── фиксы на синтетическом IR ────────────────────────────


def test_shrink_font_scales_runs_and_cuts_at_floor() -> None:
    el = _text("1", "Очень длинный подзаголовок, который не помещается в свою рамку по высоте", size_pt=20.0)
    ir = _deck(_slide(el))
    res = apply_fixes(ir, [_finding("L03_text_overflow", "shrink_font_by_scale", need_pt=30, have_pt=20)])
    fixed = res.ir.slides[0].elements[0]
    assert res.applied and 14.0 <= float(fixed.style_overrides["size_pt"]) <= 16.0
    assert ir.slides[0].elements[0].style_overrides["size_pt"] == 20.0  # исходный IR не тронут
    # нужно втрое больше места: кегль упирается в 70 % и текст режется
    res2 = apply_fixes(ir, [_finding("L03_text_overflow", "shrink_font_by_scale", need_pt=90, have_pt=20)])
    fixed2 = res2.ir.slides[0].elements[0]
    assert float(fixed2.style_overrides["size_pt"]) == 14.0 and "укорочен" in res2.applied[0]["after"]
    assert len(fixed2.paragraphs[0].runs[0].text) < len(el.paragraphs[0].runs[0].text)


def test_shrink_font_title_cuts_clause_not_words() -> None:
    """Заголовок режется только по разделителю, «(1/2)» сохраняется."""
    title = "Команды теряют до трети времени на согласования, и руководитель этого не видит (1/2)"
    el = _text("1", title, kind=SlotKind.TITLE, size_pt=26.6)
    res = apply_fixes(_deck(_slide(el)), [_finding("L03_text_overflow", "shrink_font_by_scale", need_pt=121, have_pt=60)])
    text = res.ir.slides[0].elements[0].paragraphs[0].runs[0].text
    assert text == "Команды теряют до трети времени на согласования (1/2)" and "…" not in text
    plain = _text("1", "Три риска запуска закрываются политикой доступа и поэтапным подключением", kind=SlotKind.TITLE,
                  size_pt=28.8)
    res2 = apply_fixes(_deck(_slide(plain)), [_finding("L03_text_overflow", "shrink_font_by_scale", need_pt=91, have_pt=60)])
    fixed = res2.ir.slides[0].elements[0]
    assert fixed.paragraphs[0].runs[0].text == plain.paragraphs[0].runs[0].text  # текст цел
    assert float(fixed.style_overrides["size_pt"]) < 28.8 and "укорочен" not in res2.applied[0]["after"]


def test_shrink_font_kpi_number_scales_run_not_unit() -> None:
    el = Element(slot_id="9", kind=SlotKind.NUMBER, box=Box(x=0, y=0, w=914400, h=914400),
                 paragraphs=[Paragraph(runs=[TextRun(text="42%", size_pt=80.0), TextRun(text=" дня", size_pt=28.0)])])
    res = apply_fixes(_deck(_slide(el)), [_finding("L03_text_overflow", "shrink_font_by_scale", element_id="9",
                                                   mode="width", need_pt=100, have_pt=80)])
    runs = res.ir.slides[0].elements[0].paragraphs[0].runs
    assert runs[0].size_pt < 80.0 and runs[1].size_pt < 28.0 and runs[0].text == "42%"
    assert "size_pt" not in res.ir.slides[0].elements[0].style_overrides


def test_snap_font_size_limits() -> None:
    el = _text("1", "текст", size_pt=13.6)
    ok = apply_fixes(_deck(_slide(el)), [_finding("T02_font_size", "snap_font_size", size_pt=13.6, nearest_pt=12)])
    assert ok.ir.slides[0].elements[0].style_overrides["size_pt"] == 12
    far = apply_fixes(_deck(_slide(_text("1", "текст", size_pt=73.8))),
                      [_finding("T02_font_size", "snap_font_size", size_pt=73.8, nearest_pt=24)])
    assert not far.applied and far.skipped[0]["reason"] == "нечего менять"
    num = Element(slot_id="1", kind=SlotKind.NUMBER, box=Box(x=0, y=0, w=1, h=1),
                  paragraphs=[Paragraph(runs=[TextRun(text="12", size_pt=70.0)])])
    assert not apply_fixes(_deck(_slide(num)), [_finding("T02_font_size", "snap_font_size", size_pt=70, nearest_pt=60)]).applied


def test_snap_color_text_and_palette() -> None:
    el = _text("1", "текст", palette="0077FF,123456", accent="123456")
    el.paragraphs[0].runs[0].color = "123456"
    ir = _deck(_slide(el))
    txt = apply_fixes(ir, [_finding("T03_color", "snap_color", color="123456", nearest="0077FF", where="текст")])
    assert txt.ir.slides[0].elements[0].paragraphs[0].runs[0].color == "0077FF"
    ser = apply_fixes(ir, [_finding("T03_color", "snap_color", color="123456", nearest="00AEE8", where="серия диаграммы")])
    ov = ser.ir.slides[0].elements[0].style_overrides
    assert ov["palette"] == "0077FF,00AEE8" and ov["accent"] == "00AEE8"


def test_refill_slot_shortens_but_keeps_quotes() -> None:
    long = "один два три четыре пять шесть семь восемь девять десять одиннадцать двенадцать тринадцать — хвост хвост хвост"
    res = apply_fixes(_deck(_slide(_text("1", long))), [_finding("D02_bullet_words", "refill_slot", words=17)])
    assert len(res.ir.slides[0].elements[0].paragraphs[0].runs[0].text.split()) <= 15
    quote = apply_fixes(_deck(_slide(_text("1", "«" + long + "»"))), [_finding("D02_bullet_words", "refill_slot", words=17)])
    assert not quote.applied
    ph = apply_fixes(_deck(_slide(_text("1", "Lorem ipsum"))),
                     [_finding("I02_placeholder_text", "refill_slot", sev=Severity.ERROR, pattern="lorem")])
    assert ph.ir.slides[0].elements[0].paragraphs == []


def test_add_chart_labels_finds_native_chart_by_box() -> None:
    chart = ChartSpec(kind="column", title="t", categories=["a", "b"], series={"s": [1, 2]}, y_label="часы")
    el = Element(slot_id="7", kind=SlotKind.CHART, box=Box(x=1000, y=1000, w=5000, h=5000), chart=chart)
    f = Finding(check_id="I05_chart_labels", kind="deterministic", severity=Severity.WARNING, slide_idx=0,
                element_id="999", box=Box(x=1000, y=1000, w=5000, h=5000), message="", autofix="add_chart_labels")
    res = apply_fixes(_deck(_slide(el)), [f])
    fixed = res.ir.slides[0].elements[0]
    assert fixed.style_overrides["data_labels"] is True and fixed.chart.unit == "часы"


def test_drop_slide_and_minor_series() -> None:
    ir = _deck(_slide(_text("1", "a"), idx=0), _slide(_text("1", "b"), idx=1), _slide(_text("1", "c"), idx=2))
    res = apply_fixes(ir, [_finding("I06_duplicate_slides", "drop_slide", slide_idx=1, element_id=None)])
    assert [s.idx for s in res.ir.slides] == [0, 1] and res.ir.slides[1].elements[0].paragraphs[0].runs[0].text == "c"
    chart = ChartSpec(kind="line", title="t", categories=["a"], series={f"s{i}": [i] for i in range(7)})
    el = Element(slot_id="7", kind=SlotKind.CHART, box=Box(x=0, y=0, w=1, h=1), chart=chart)
    res = apply_fixes(_deck(_slide(el)), [_finding("D04_chart_series", "drop_minor_series", element_id="7", series=7)])
    assert list(res.ir.slides[0].elements[0].chart.series) == ["s2", "s3", "s4", "s5", "s6"]


def test_replan_and_template_findings_are_skipped_with_reason() -> None:
    ir = _deck(_slide(_text("1", "a")))
    res = apply_fixes(ir, [_finding("D01_bullets", "split_slide"),
                           _finding("L02_overlap", "reflow_vertical", in_exemplar=1),
                           _finding("L03_text_overflow", "shrink_font_by_scale", element_id="nope", need_pt=2, have_pt=1)])
    assert not res.applied and [s["reason"] for s in res.skipped] == [
        "уровень replan: только по решению пользователя", "дизайн шаблона", "элемент не наш (не в IR)"]


# ──────────────────────────── на реальной колоде ────────────────────────────


def test_safe_fixes_clear_our_errors_on_vk_tech(template_path, tmp_path: Path) -> None:
    """После безопасных фиксов и повторного рендера L03 у наших элементов нет, новых ошибок тоже."""
    pptx = template_path("VK Tech")
    dna = build_dna(pptx)
    outline = DeckOutline.model_validate_json(OUTLINE.read_text("utf-8"))
    style = {"accent": "0077FF", "font": "Play", "palette": "0077FF,00AEE8"}
    res = build_deck_ir(outline, load_strategy("visual"), dna.exemplars, dna.template_id, dna.slide_w, dna.slide_h, style)
    out = render_pptx(res.ir, pptx, dna.exemplars, tmp_path / "visual.pptx")
    before = audit_deck(out, dna, res.ir)
    chosen = plan_fixes(before, "safe")
    # L03 у наших элементов не остаётся — чинятся D01/T02
    assert chosen
    fr = apply_fixes(res.ir, chosen, dna)
    assert fr.changed and all({"before", "after"} <= set(it) for it in fr.applied)
    out2 = render_pptx(fr.ir, pptx, dna.exemplars, tmp_path / "visual_fixed.pptx")
    after = audit_deck(out2, dna, fr.ir)
    assert after.errors == 0 and after.errors <= before.errors
    fixed_checks = {f.check_id for f in chosen}
    remaining = [f for f in after.findings if f.check_id in fixed_checks and f.severity == Severity.ERROR]
    assert not remaining
    assert not any(f.check_id == "I05_chart_labels" for f in after.findings)
