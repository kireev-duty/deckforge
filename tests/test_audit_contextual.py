"""VLM-судья: разбор ответов с кассеты, контекст слайдов, устойчивость к сбоям. LLM не вызывается."""

from __future__ import annotations

from pathlib import Path

from deckforge.audit.contextual import (
    CHECK_IDS, ERROR_CHECK_ID, QUESTIONS, SlideText, findings_from_answers, judge_deck, slides_from_ir,
)
from deckforge.content import load_content_pack
from deckforge.core.ir import Archetype, DeckOutline, Severity
from deckforge.core.strategy import load_strategy
from deckforge.layout import build_deck_ir
from deckforge.parsing.dna import build_dna
from tests.conftest import FakeClient, cassette

REPO = Path(__file__).resolve().parents[1]
PACK = REPO / "examples" / "content_pack"


def _png(tmp_path: Path, n: int) -> list[Path]:
    from PIL import Image

    out = []
    for i in range(n):
        p = tmp_path / f"slide_{i + 1:02d}.png"
        Image.new("RGB", (16, 9), "white").save(p)
        out.append(p)
    return out


def test_question_table_matches_audit_md() -> None:
    md = (REPO / "docs" / "AUDIT.md").read_text("utf-8")
    assert len(QUESTIONS) == 11 and len(CHECK_IDS) == 11
    for cid in CHECK_IDS:
        assert cid in md, cid
    assert QUESTIONS["C02"][1] == Severity.ERROR and QUESTIONS["C10"][1] == Severity.INFO


def test_findings_from_cassette() -> None:
    answers = cassette("audit_judge_pulse")
    assert len(answers) == 15
    # слайд 3: судья не нашёл цифру в источниках и принял многоточия за заглушку
    s = SlideText(idx=2, title="t", archetype=Archetype.CARDS, sources=["brief:продукт"])
    found = findings_from_answers(answers[2], s)
    ids = {f.check_id: f for f in found}
    assert set(ids) == {"C04_facts_in_sources", "C07_no_garbage"}
    assert ids["C04_facts_in_sources"].severity == Severity.ERROR and ids["C04_facts_in_sources"].kind == "contextual"
    assert "9,5" in ids["C04_facts_in_sources"].message and ids["C04_facts_in_sources"].evidence["sources"] == "brief:продукт"
    assert all(f.autofix is None and f.slide_idx == 2 for f in found)
    # титул: всё «да» → находок нет
    assert findings_from_answers(answers[0], SlideText(idx=0, archetype=Archetype.TITLE)) == []


def test_structural_slides_skip_inapplicable_questions() -> None:
    answers = cassette("audit_judge_pulse")
    section = SlideText(idx=1, title="Контекст", archetype=Archetype.SECTION)
    assert findings_from_answers(answers[1], section) == []  # для разделителя эти вопросы не считаются
    as_cards = findings_from_answers(answers[1], SlideText(idx=1, archetype=Archetype.CARDS))
    assert {f.check_id.split("_")[0] for f in as_cards} == {"C01", "C02", "C03", "C05", "C11"}


def test_answer_forms_tolerated() -> None:
    raw = {"answers": {"C01": {"ok": "false", "note": "нет вывода"}, "C08": {"ok": False, "note": "n/a"},
                       "C02": {"ok": "yes"}, "C99": {"ok": False}, "C03": "мусор"}}
    found = findings_from_answers(raw, SlideText(idx=0, archetype=Archetype.BULLETS))
    assert [f.check_id for f in found] == ["C01_title_insight"]
    assert findings_from_answers({}, SlideText(idx=0)) == [] and findings_from_answers({"answers": []}, SlideText(idx=0)) == []


def test_judge_deck_calls_per_slide_and_survives_errors(tmp_path: Path) -> None:
    answers = cassette("audit_judge_pulse")
    slides = [SlideText(idx=i, title=f"T{i}", text=f"текст {i}", archetype=Archetype.CARDS) for i in range(3)]
    client = FakeClient(by_skill={"audit_judge": [answers[2], RuntimeError("timeout"), answers[0]]})
    # workers=1: очередь ответов разбирается по порядку слайдов
    found = judge_deck(_png(tmp_path, 3), slides, client, language="ru", workers=1)
    assert len(client.calls) == 3 and all(c.skill == "audit_judge@v1" for c in client.calls)
    by_slide = {i: [f.check_id for f in found if f.slide_idx == i] for i in range(3)}
    assert by_slide[0] == ["C04_facts_in_sources", "C07_no_garbage"]
    assert by_slide[1] == [ERROR_CHECK_ID] and found[2].severity == Severity.INFO and "timeout" in found[2].message
    assert by_slide[2] == []
    inputs = {i["slide_idx"]: i for i in client.inputs}
    assert inputs[1]["prev_slide_title"].startswith("(нет") and inputs[1]["next_slide_title"] == "T1"
    assert inputs[2]["prev_slide_title"] == "T0" and inputs[2]["next_slide_title"] == "T2"
    assert inputs[3]["next_slide_title"].startswith("(нет") and inputs[3]["language"] == "ru"
    assert all(len(imgs) == 1 for imgs in client.images)


def test_judge_deck_parallel_keeps_slide_order(tmp_path: Path) -> None:
    import threading
    import time

    answers = cassette("audit_judge_pulse")
    slides = [SlideText(idx=i, title=f"T{i}", archetype=Archetype.CARDS) for i in range(6)]
    seen: list[str] = []
    lock = threading.Lock()

    class Slow(FakeClient):
        def run_skill(self, skill, images=None, **inputs):
            time.sleep(0.02 * (6 - int(inputs["slide_idx"])))  # первые слайды отвечают последними
            with lock:
                seen.append(threading.current_thread().name)
            return answers[2]  # C04 + C07 на каждом

    found = judge_deck(_png(tmp_path, 6), slides, Slow(), workers=3)
    assert len(set(seen)) > 1  # реально в нескольких потоках
    assert [f.slide_idx for f in found] == sorted(f.slide_idx for f in found) and len(found) == 12


def test_slides_from_ir_collects_text_and_facts(template_path) -> None:
    dna = build_dna(template_path("VK Tech"))
    outline = DeckOutline.model_validate_json((PACK / "outline.json").read_text("utf-8"))
    res = build_deck_ir(outline, load_strategy("narrative"), dna.exemplars, dna.template_id, dna.slide_w, dna.slide_h,
                        {"accent": "0077FF", "font": "Play", "palette": "0077FF"})
    pack = load_content_pack(PACK)
    slides = slides_from_ir(res.ir, outline, pack)
    assert len(slides) == len(res.ir.slides) and slides[0].archetype == Archetype.TITLE
    assert outline.slides[0].title.startswith(slides[0].title)  # судья видит текст после fitting
    kpi = next(s for s in slides if s.archetype == Archetype.KPI)
    assert any(ch.isdigit() for ch in kpi.text)
    chart = next(s for s in slides if s.archetype == Archetype.CHART)
    assert "[диаграмма:" in chart.text and "категории:" in chart.text
    # факты: сначала свои sources, затем остальное, но не целый бриф
    with_src = next(s for s in slides if s.sources and "brief" not in s.sources and "doc:product" not in s.sources)
    assert with_src.facts.startswith(f"[{with_src.sources[0]}]")
    assert "[brief]" not in with_src.facts and "[doc:product]" not in with_src.facts and "[m:pilot]" in with_src.facts
    assert len(with_src.facts) <= 6000
