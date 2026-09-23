"""Режим «на входе только шаблон»: слепок шаблона, бриф от модели и пакет из него."""

from __future__ import annotations

from deckforge.content.template_brief import (
    TemplateBrief,
    pack_from_brief,
    template_digest,
    write_template_brief,
)
from deckforge.core.ir import Archetype
from deckforge.llm.skills import load_skill
from deckforge.parsing.dna import build_dna
from deckforge.parsing.exemplars import load_exemplars
from deckforge.parsing.extract_tokens import extract_tokens
from tests.conftest import FakeClient

RESPONSE = {
    "topic": "VK Tech: корпоративные сервисы для команд",
    "brand": "VK Tech",
    "purpose": "product",
    "audience": "ИТ-руководители",
    "summary": "Из чего состоит платформа и зачем она командам.",
    "key_points": ["Команды теряют время на переключение", "Единая платформа закрывает коммуникации"],
    "tone": "светлый корпоративный",
}


def _dna(path):
    return build_dna(path, load_exemplars(path), extract_tokens(path))


def test_digest_is_deterministic_and_carries_template_texts(template_path) -> None:
    pptx = template_path("VK Tech")
    dna = _dna(pptx)
    digest = template_digest(dna, name=pptx.name)

    assert digest == template_digest(dna, name=pptx.name)
    assert digest.startswith("Файл шаблона: " + pptx.name)
    assert "Палитра" in digest and "Шрифты:" in digest and "Слайды-образцы" in digest
    # тексты слайдов-образцов — единственный источник смысла в этом режиме
    assert "текст: " in digest
    assert any(e.archetype.value in digest for e in dna.exemplars)
    # заглушки шаблона в промпт не идут
    assert "Образец текста" not in digest and "Вставить фото" not in digest


def test_digest_respects_limit(template_path) -> None:
    dna = _dna(template_path("VK Tech"))
    short = template_digest(dna, limit=800)
    assert len(short) <= 900 and "…ещё" in short


def test_brief_text_and_pack() -> None:
    brief = TemplateBrief.from_response({**RESPONSE, "purpose": "маркетинг"}, template_name="T.pptx")
    assert brief.purpose == "other"  # не из списка Purpose
    text = brief.to_brief_text()
    assert text.startswith("# VK Tech: корпоративные сервисы")
    assert "VK Tech" in text and RESPONSE["key_points"][0] in text and "выведен из шаблона" in text

    pack = pack_from_brief(text)
    assert pack.ids() == {"brief"} and pack.brief == text.strip()
    assert pack.to_prompt_text()[0] == ""  # бриф идёт отдельным входом, в {{content_pack}} его нет


def test_from_response_tolerates_junk() -> None:
    brief = TemplateBrief.from_response({"summary": "  ", "key_points": "один тезис"})
    assert brief.topic == "Презентация по шаблону" and brief.key_points == ["один тезис"]
    assert brief.purpose == "other" and brief.brand == ""


def test_write_template_brief_calls_skill(template_path) -> None:
    dna = _dna(template_path("VK Tech"))
    client = FakeClient(RESPONSE)
    brief = write_template_brief(client, dna, name="VK Tech.pptx", language="ru", target_slides=12,
                                 purpose="", audience="")

    assert [c.skill for c in client.calls] == ["template_brief@v2"]
    assert set(client.inputs[0]) >= set(load_skill("template_brief").inputs)
    assert brief.topic == RESPONSE["topic"] and brief.purpose == "product"
    assert brief.audience == "ИТ-руководители" and brief.template_name == "VK Tech.pptx"


def test_user_purpose_and_audience_win(template_path) -> None:
    dna = _dna(template_path("VK Tech"))
    client = FakeClient(RESPONSE)
    brief = write_template_brief(client, dna, purpose="report", audience="совет директоров")
    assert brief.purpose == "report" and brief.audience == "совет директоров"
    assert client.inputs[0]["purpose_hint"] == "report"


def test_drop_unsourced_numbers() -> None:
    """Без исходников модель дорисовывает метрики вроде «100 %» и «24/7» — они снимаются."""
    from deckforge.content.template_brief import drop_unsourced_numbers
    from deckforge.core.ir import DeckOutline

    brief = "Платформа работает в контуре заказчика. Внедрение занимает 3 месяца."
    outline = DeckOutline.model_validate({"title": "T", "purpose": "product", "slides": [
        {"idx": 0, "archetype": "title", "title": "T"},
        {"idx": 1, "archetype": "kpi", "title": "Цифры",
         "kpis": [{"value": "100%", "label": "Соответствие стандартам"}, {"value": "24/7", "label": "Поддержка"}]},
        {"idx": 2, "archetype": "kpi", "title": "Из брифа", "kpis": [{"value": "3 месяца", "label": "Внедрение"}]},
        {"idx": 3, "archetype": "table", "title": "Качественное сравнение",
         "table": {"header": ["Критерий", "Было", "Стало"], "rows": [["Интеграция", "Сложная", "Встроенная"]]}},
        {"idx": 4, "archetype": "chart", "title": "Ряд",
         "chart": {"kind": "line", "title": "x", "categories": ["май", "июнь"], "series": {"A": [7, 9]}}},
        {"idx": 5, "archetype": "closing", "title": "T"},
    ]})
    fixed, warnings = drop_unsourced_numbers(outline, brief)

    by_title = {s.title: s for s in fixed.slides}
    assert by_title["Цифры"].kpis == [] and by_title["Цифры"].archetype == Archetype.BULLETS
    assert by_title["Цифры"].bullets == ["Соответствие стандартам", "Поддержка"]  # подписи не теряются
    assert by_title["Из брифа"].kpis and by_title["Из брифа"].archetype == Archetype.KPI  # «3» есть в брифе
    assert by_title["Качественное сравнение"].table is not None  # чисел нет — проверять нечего
    assert by_title["Ряд"].chart is None and by_title["Ряд"].bullets  # числа не из брифа
    assert len(warnings) == 2


def test_outline_from_template_brief_keeps_sources() -> None:
    """sources: ["brief"] остаются валидными — пакет из брифа несёт этот id."""
    from deckforge.content.outline_writer import repair_outline

    pack = pack_from_brief(TemplateBrief.from_response(RESPONSE).to_brief_text())
    raw = {"title": "T", "purpose": "product",
           "slides": [{"idx": 0, "archetype": "title", "title": "T", "sources": ["brief"]},
                      {"idx": 1, "archetype": "bullets", "title": "Проблема", "bullets": ["раз", "два"],
                       "sources": ["brief", "m:made_up"]}]}
    outline, warnings = repair_outline(raw, {Archetype.TITLE, Archetype.BULLETS, Archetype.CLOSING}, pack.ids())

    assert outline.slides[0].sources == ["brief"] and outline.slides[1].sources == ["brief"]
    assert any("m:made_up" in w for w in warnings)
