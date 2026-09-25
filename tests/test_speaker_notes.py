"""Текст выступления: бюджет слов, приведение ответа модели, запись в колоду и чтение заметок из .pptx."""

from __future__ import annotations

import json
from pathlib import Path

from deckforge.content.speaker_notes import fit_to_budget, repair_notes, slides_input, write_speaker_notes
from deckforge.core.ir import Archetype, DeckIR, Element, Paragraph, SlideIR, SlotKind, TextRun
from deckforge.core.speech import MIN_SLIDE_WORDS, SPEECH_WPM, word_budget, word_count
from tests.conftest import FakeClient, fake_notes


def _ir(*archetypes: Archetype, talk_minutes: float | None = None) -> DeckIR:
    box = {"x": 0, "y": 0, "w": 100, "h": 100}
    slides = [SlideIR(idx=i, exemplar_id="s", archetype=a, outline_ref=i, elements=[
        Element(slot_id="t", kind=SlotKind.TITLE, box=box, paragraphs=[Paragraph(runs=[TextRun(text=f"Заголовок {i}")])]),
        Element(slot_id="b", kind=SlotKind.BODY, box=box, paragraphs=[Paragraph(runs=[TextRun(text="Пункт слайда")])]),
    ]) for i, a in enumerate(archetypes)]
    return DeckIR(template_id="t", strategy="narrative", slide_w=100, slide_h=100, slides=slides, talk_minutes=talk_minutes)


def test_word_budget_fits_minutes_and_weights_structural_slides() -> None:
    arch = [Archetype.TITLE, Archetype.SECTION, Archetype.CARDS, Archetype.BULLETS, Archetype.CLOSING]
    budget = word_budget(arch, 7)
    assert abs(sum(budget) - 7 * SPEECH_WPM) <= len(arch)  # округление
    assert budget[2] == budget[3] > budget[4] > budget[0] > budget[1] >= MIN_SLIDE_WORDS
    assert word_budget([], 7) == []
    assert word_count("Выручка B2B выросла на 18 % — за счёт онбординга") == 8


def test_repair_notes_accepts_odd_shapes() -> None:
    # список объектов не по порядку, markdown и ремарки докладчику
    notes, warnings = repair_notes({"notes": [
        {"idx": 1, "text": "**Главное** — сроки.\n- (пауза) Смотрим на цифры"},
        {"idx": "0", "text": "Слайд 1: Добрый день"},
        {"idx": 7, "text": "лишний"},
    ]}, 3)
    assert notes == ["Добрый день", "Главное — сроки.\nСмотрим на цифры", ""]
    assert any("несуществующим" in w for w in warnings) and any("слайдов 3" in w for w in warnings)
    # словарь «idx → текст» и нумерация с 1
    notes, warnings = repair_notes({"1": "первый", "2": "второй"}, 2)
    assert notes == ["первый", "второй"] and any("с 1" in w for w in warnings)
    # список строк без idx — по порядку; поле speaker_notes вместо text
    assert repair_notes({"notes": ["а", {"speaker_notes": "б"}]}, 2)[0] == ["а", "б"]
    # мусор вместо ответа — пустые строки и предупреждение, а не исключение
    notes, warnings = repair_notes("не json", 2)
    assert notes == ["", ""] and warnings


def test_slides_input_carries_budget_text_and_draft() -> None:
    ir = _ir(Archetype.TITLE, Archetype.CARDS, Archetype.CLOSING)
    ir.slides[1].notes = "черновик из outline"
    items = json.loads(slides_input(ir, 7))
    assert [i["archetype"] for i in items] == ["title", "cards", "closing"]
    assert items[1]["title"] == "Заголовок 1" and "Пункт слайда" in items[1]["text"]
    assert items[1]["words"] > items[0]["words"] and items[1]["draft"] == "черновик из outline"
    assert items[1]["sentences"] == round(items[1]["words"] / 12) and items[0]["sentences"] >= 1
    assert "draft" not in items[0]


def test_write_speaker_notes_uses_skill_and_repair() -> None:
    ir = _ir(Archetype.TITLE, Archetype.CARDS, Archetype.CLOSING)
    client = FakeClient(by_skill={"speaker_notes": [fake_notes]})
    res = write_speaker_notes(client, ir, talk_minutes=3, deck_title="Пульс", brief="бриф")
    assert len(res.notes) == 3 and all(res.notes) and res.skill_version == "v3" and not res.warnings
    assert abs(res.words - 3 * SPEECH_WPM) <= 3
    inputs = client.inputs[0]
    assert inputs["talk_minutes"] == "3" and inputs["brief"] == "бриф" and inputs["audience"] == "не указана"
    assert abs(inputs["total_words"] - 3 * SPEECH_WPM) <= 3


def _sentences(n: int, words: int = 10) -> str:
    return " ".join(" ".join(["слово"] * (words - 1)) + f" конец{k}." for k in range(n))


def test_fit_to_budget_trims_over_limit_from_the_longest_slides() -> None:
    """Выступление ограничено по длительности: 130 слов при бюджете 90 → последние предложения снимаются
    у слайдов, сильнее всех превысивших свой бюджет, пока итог не уложится; у слайда остаётся предложение."""
    notes = [_sentences(1), _sentences(8), _sentences(4, 5) + "\n" + _sentences(2)]  # 10 + 80 + 40 слов
    budget = [10, 40, 40]
    fitted, cut = fit_to_budget(notes, budget)
    words = [word_count(n) for n in fitted]
    assert sum(words) <= sum(budget) and cut == 130 - sum(words)
    assert fitted[0] == notes[0]  # одно предложение — не трогается
    assert words[1] == 40 and fitted[1].endswith("конец3.")  # срезан хвост самого перегруженного
    assert fitted[2] == notes[2]  # в своём бюджете — абзацы на месте

    within = [_sentences(1), _sentences(4), _sentences(4)]  # 90 слов при бюджете 85: +6 % — в допуске
    assert fit_to_budget(within, [5, 40, 40]) == (within, 0)
    short = [_sentences(1)] * 3  # короче бюджета — дописывать нечем
    assert fit_to_budget(short, [40, 40, 40]) == (short, 0)


def test_write_speaker_notes_keeps_talk_within_limit() -> None:
    """Модель написала вдвое длиннее 3 минут — в колоду уходит не больше 3 минут и предупреждение."""
    ir = _ir(Archetype.TITLE, Archetype.CARDS, Archetype.CLOSING)

    def verbose(inputs: dict) -> dict:
        slides = json.loads(inputs["slides"])
        return {"notes": [{"idx": s["idx"], "text": _sentences(max(2, s["words"] * 2 // 10))} for s in slides]}

    res = write_speaker_notes(FakeClient(by_skill={"speaker_notes": [verbose]}), ir, talk_minutes=3, deck_title="Пульс")
    assert res.words <= 3 * SPEECH_WPM and all(res.notes)
    assert any("длиннее 3 мин" in w for w in res.warnings)


def test_short_notes_get_second_pass_longer_text_per_slide() -> None:
    """Объём у модели плавает: первый ответ на треть цели → второй вызов; по слайду — более длинный текст, итог в лимите.
    Перед дедлайном второго вызова нет — колода уходит с тем, что есть."""
    import time

    ir = _ir(Archetype.TITLE, Archetype.CARDS, Archetype.CARDS, Archetype.CLOSING)

    def answer(share: float, only: set[int] | None = None):
        def f(inputs: dict) -> dict:
            slides = json.loads(inputs["slides"])
            return {"notes": [{"idx": s["idx"], "text": _sentences(max(1, round(s["words"] * share / 10)))}
                              for s in slides if only is None or s["idx"] in only]}
        return f

    # первый — треть объёма; второй — вдвое длиннее цели, но только для слайдов 1–2 (у 0 и 3 остаётся первый)
    client = FakeClient(by_skill={"speaker_notes": [answer(0.3), answer(2.0, {1, 2})]})
    res = write_speaker_notes(client, ir, talk_minutes=3, deck_title="Пульс")
    assert len(client.calls) == 2 and all(res.notes)
    assert 0.75 * 3 * SPEECH_WPM <= res.words <= 3 * SPEECH_WPM
    assert any("второй проход" in w for w in res.warnings)

    late = FakeClient(by_skill={"speaker_notes": [answer(0.3)]})
    res = write_speaker_notes(late, ir, talk_minutes=3, deck_title="Пульс", deadline=time.monotonic() + 10)
    assert len(late.calls) == 1 and res.words < 0.75 * 3 * SPEECH_WPM


def test_notes_written_into_pptx_and_read_back(template_path, tmp_path: Path) -> None:
    """write_notes пишет заметки в готовую колоду, read_notes (аудит, HTML) читает их обратно; N01/N02 по ним."""
    from deckforge.audit import audit_deck
    from deckforge.core.deck_reader import read_notes
    from deckforge.core.package import Package
    from deckforge.export.html import export_html
    from deckforge.parsing.dna import build_dna
    from deckforge.render import write_notes
    from tests.conftest import build_sample_deck

    pptx, ir = build_sample_deck(tmp_path)
    dna = build_dna(template_path("VK Tech"))
    texts = [" ".join(["слово"] * 30) if i % 2 == 0 else "" for i in range(len(ir.slides))]
    write_notes(pptx, texts)
    pkg = Package(pptx)
    assert [read_notes(pkg, p) for p in pkg.slides] == texts

    ir.talk_minutes = 7
    report = audit_deck(pptx, dna, ir, checks=["N01", "N02"])
    n01 = [f.slide_idx for f in report.findings if f.check_id == "N01_notes_missing"]
    assert n01 == [i for i, t in enumerate(texts) if not t]
    [n02] = [f for f in report.findings if f.check_id == "N02_talk_duration"]
    assert n02.slide_idx == -1 and "короче" in n02.message

    html = export_html(pptx, tmp_path / "deck.html").read_text("utf-8")
    assert html.count('<aside class="notes"') == sum(1 for t in texts if t) and 'id="btnn"' in html
    # класс «заметки видны» у body — не `notes`: правило `.notes{display:none}` спрятало бы всю страницу
    assert "classList.toggle('with-notes')" in html and "body.notes" not in html and "classList.add('notes')" not in html
