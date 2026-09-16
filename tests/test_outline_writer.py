"""outline_writer: кассета реального ответа модели + repair на синтетических «плохих» ответах. LLM не вызывается."""

from pathlib import Path

import pytest

from deckforge.content import archetypes_prompt, load_content_pack, repair_outline, write_outline
from deckforge.core.ir import Archetype, DeckOutline
from tests.conftest import FakeClient
from tests.conftest import cassette as load_cassette

REPO = Path(__file__).resolve().parents[1]
PACK = REPO / "examples" / "content_pack"

VK_TECH_ARCHETYPES = {Archetype.TITLE, Archetype.CARDS, Archetype.KPI, Archetype.CHART, Archetype.TABLE,
                      Archetype.PROCESS, Archetype.IMAGE_TEXT, Archetype.BULLETS, Archetype.CLOSING, Archetype.SECTION}


def cassette() -> dict:
    return load_cassette("outline_writer_pulse")


def test_cassette_becomes_valid_outline() -> None:
    pack = load_content_pack(PACK)
    client = FakeClient(cassette())
    res = write_outline(client, pack, purpose="product", audience="руководители", target_slides=12,
                        available_archetypes=VK_TECH_ARCHETYPES)
    o = res.outline
    assert isinstance(o, DeckOutline) and 10 <= len(o.slides) <= 14
    assert o.slides[0].archetype == Archetype.TITLE and o.slides[-1].archetype == Archetype.CLOSING
    assert [s.idx for s in o.slides] == list(range(len(o.slides)))
    used = {src for s in o.slides for src in s.sources}
    assert used and used <= pack.ids()
    assert any(s.chart for s in o.slides) and any(s.table for s in o.slides) and any(s.kpis for s in o.slides)
    assert all(isinstance(st, str) for s in o.slides for st in s.steps)
    assert res.attempts == 1 and client.calls[0].skill == "outline_writer@v1"
    inputs = client.inputs[0]
    assert "strategy_rules" not in inputs and inputs["target_slides"] == 12
    assert "- kpi:" in inputs["available_archetypes"] and "- section:" not in inputs["available_archetypes"]
    assert "[m:coordination_hours]" in inputs["content_pack"] and "Пульс команды" in inputs["brief"]


def test_repair_fixes_bad_answer() -> None:
    raw = {
        "title": "Тест", "purpose": "sales-pitch",
        "slides": [
            {"idx": 7, "archetype": "hero", "title": "Не title первым", "bullets": [f"п{i}" for i in range(9)]},
            {"idx": 1, "archetype": "chart", "title": "Кривая диаграмма",
             "chart": {"kind": "line", "categories": ["a", "b", "c"], "series": {"x": [1, 2]}}},
            {"idx": 2, "archetype": "table", "title": "Рваная таблица",
             "table": {"header": ["к1", "к2"], "rows": [["1"], ["1", "2", "3"], {"к1": "d", "к2": "e"}]}},
            {"idx": 3, "archetype": "cards", "title": "Карточки в своём поле",
             "cards": [{"title": "А", "text": "а-текст"}, "Б"]},
            {"idx": 4, "archetype": "kpi", "title": "KPI без цифр", "kpis": None},
            {"idx": 5, "archetype": "", "title": "   "},
            {"idx": 6, "archetype": "two_column", "title": "Нет в шаблоне", "bullets": ["x"], "sources": ["nope"]},
        ],
    }
    o, warnings = repair_outline(raw, {Archetype.TITLE, Archetype.BULLETS, Archetype.CHART, Archetype.TABLE,
                                       Archetype.CARDS, Archetype.CLOSING}, {"brief"}, purpose="report")
    text = "\n".join(warnings)
    assert o.purpose == "report" and "purpose" in text
    assert o.slides[0].archetype == Archetype.TITLE and o.slides[-1].archetype == Archetype.CLOSING
    assert [s.idx for s in o.slides] == list(range(len(o.slides)))
    by_title = {s.title: s for s in o.slides}
    assert by_title["Не title первым"].archetype == Archetype.BULLETS and len(by_title["Не title первым"].bullets) == 6
    assert by_title["Кривая диаграмма"].archetype == Archetype.BULLETS and by_title["Кривая диаграмма"].chart is None
    t = by_title["Рваная таблица"].table
    assert t and t.rows == [["1", ""], ["1", "2"], ["d", "e"]]
    assert by_title["Карточки в своём поле"].bullets == ["А — а-текст", "Б"]
    assert by_title["KPI без цифр"].archetype == Archetype.BULLETS
    assert "   " not in by_title and "без заголовка" in text
    assert by_title["Нет в шаблоне"].archetype == Archetype.BULLETS and "неизвестные sources: nope" in text


def test_series_as_list_of_objects_is_accepted() -> None:
    raw = {"title": "T", "purpose": "report", "slides": [
        {"idx": 0, "archetype": "title", "title": "T"},
        {"idx": 1, "archetype": "chart", "title": "Ряд", "chart": {
            "kind": "bar", "title": "x", "categories": ["1", "2"],
            "series": [{"name": "A", "data": ["1,5", "−2"]}, {"name": "B", "values": [3, 4]}]}},
        {"idx": 2, "archetype": "closing", "title": "Конец"},
    ]}
    o, _ = repair_outline(raw, {Archetype.TITLE, Archetype.CHART, Archetype.CLOSING}, set())
    assert o.slides[1].chart and o.slides[1].chart.series == {"A": [1.5, -2.0], "B": [3.0, 4.0]}


def test_retry_then_fail_on_garbage() -> None:
    pack = load_content_pack(PACK)
    client = FakeClient({"title": "x", "slides": []}, {"nonsense": 1})
    with pytest.raises(RuntimeError, match="outline_writer"):
        write_outline(client, pack, purpose="other", audience="", available_archetypes=VK_TECH_ARCHETYPES, retries=1)
    assert len(client.calls) == 2


def test_retry_recovers() -> None:
    pack = load_content_pack(PACK)
    client = FakeClient({"slides": []}, cassette())
    res = write_outline(client, pack, purpose="product", audience="", available_archetypes=VK_TECH_ARCHETYPES)
    assert res.attempts == 2 and len(res.outline.slides) >= 10


def test_archetypes_prompt_offers_only_available_and_meaningful() -> None:
    text = archetypes_prompt({Archetype.TITLE, Archetype.SECTION, Archetype.FREEFORM, Archetype.KPI})
    assert "- title:" in text and "- kpi:" in text
    assert "section" not in text and "freeform" not in text
    assert "- bullets:" in archetypes_prompt(set())  # пустой шаблон → текстовый минимум
