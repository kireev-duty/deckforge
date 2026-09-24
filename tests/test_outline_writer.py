"""outline_writer: кассета ответа модели + repair на синтетических «плохих» ответах. LLM не вызывается."""

from pathlib import Path

import pytest

from deckforge.content import archetypes_prompt, load_content_pack, repair_outline, write_outline
from deckforge.content.outline_writer import missing_file_refs
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
    assert res.attempts == 1 and client.calls[0].skill == "outline_writer@v3"
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
    # kpi без цифр → bullets, а буллетов нет: остался бы один заголовок — слайд снимается
    assert "KPI без цифр" not in by_title and "«KPI без цифр»: текстовый слайд без содержимого — пропущен" in text
    assert "   " not in by_title and "без заголовка" in text
    assert by_title["Нет в шаблоне"].archetype == Archetype.BULLETS and "неизвестные sources: nope" in text
    assert by_title["Нет в шаблоне"].sources == []


def test_kpi_words_become_bullets() -> None:
    """Прогон «только шаблон» (VK Tech, 24.09): KPI-слайд со словами вместо цифр — в кольцах KPI-образца
    «Гибкая» читалась как сломанная диаграмма. Словесные KPI — буллеты, числовые остаются KPI."""
    raw = {"title": "T", "purpose": "product", "slides": [
        {"idx": 0, "archetype": "title", "title": "T"},
        {"idx": 1, "archetype": "kpi", "title": "Снижение стоимости владения", "kpis": [
            {"value": "Гибкая", "label": "Модель лицензирования"}, {"value": "Эксперты", "label": "Сопровождение"}]},
        {"idx": 2, "archetype": "kpi", "title": "Пилот", "kpis": [
            {"value": "12", "label": "команд"}, {"value": "Быстро", "label": "Подключение"}]},
        {"idx": 3, "archetype": "closing", "title": "T"},
    ]}
    o, warnings = repair_outline(raw, {Archetype.TITLE, Archetype.KPI, Archetype.BULLETS, Archetype.CLOSING}, {"brief"})
    words, mixed = o.slides[1], o.slides[2]
    assert words.archetype == Archetype.BULLETS and not words.kpis
    assert words.bullets == ["Модель лицензирования — Гибкая", "Сопровождение — Эксперты"]
    assert mixed.archetype == Archetype.KPI and [k.value for k in mixed.kpis] == ["12"]
    assert mixed.bullets == ["Подключение — Быстро"]
    assert sum("KPI без цифр" in w for w in warnings) == 2


def test_unknown_sources_are_dropped_known_kept() -> None:
    raw = {"title": "T", "purpose": "report", "slides": [
        {"idx": 0, "archetype": "title", "title": "T"},
        {"idx": 1, "archetype": "bullets", "title": "Факт", "bullets": ["x"], "sources": ["product.md", "brief", "m:x"]},
        {"idx": 2, "archetype": "closing", "title": "T"},
    ]}
    o, warnings = repair_outline(raw, {Archetype.TITLE, Archetype.BULLETS, Archetype.CLOSING}, {"brief"})
    assert o.slides[1].sources == ["brief"]
    assert any(w.startswith("неизвестные sources: m:x, product.md") for w in warnings)


def test_nested_content_object_is_lifted() -> None:
    raw = {"title": "T", "purpose": "report", "slides": [
        {"idx": 0, "archetype": "title", "title": "T", "content": {"subtitle": "Подзаголовок"}},
        {"idx": 1, "archetype": "kpi", "title": "Цифры", "content": {"kpis": [{"value": "12", "label": "команд"}]}},
        {"idx": 2, "archetype": "bullets", "title": "Тезисы", "bullets": ["своё"], "content": {"bullets": ["чужое"]}},
        {"idx": 3, "archetype": "closing", "title": "T"},
    ]}
    o, warnings = repair_outline(raw, {Archetype.TITLE, Archetype.KPI, Archetype.BULLETS, Archetype.CLOSING}, {"brief"})
    assert o.slides[0].subtitle == "Подзаголовок"
    assert o.slides[1].archetype == Archetype.KPI and o.slides[1].kpis[0].value == "12"
    assert o.slides[2].bullets == ["своё"]  # своё поле важнее вложенного
    assert sum("content" in w for w in warnings) == 3


def test_two_column_sides_and_plain_text_become_content() -> None:
    """Формы из прогона «только шаблон»: колонки объектами left/right и абзац в поле text."""
    raw = {"title": "T", "purpose": "report", "slides": [
        {"idx": 0, "archetype": "title", "title": "T"},
        {"idx": 1, "archetype": "two_column", "title": "Стиль",
         "left": {"heading": "Шрифты", "text": "Montserrat для заголовков"},
         "right": {"heading": "Цвета", "text": "акцентный розовый"}},
        {"idx": 2, "archetype": "image_text", "title": "Демонстрация", "image": "placeholder",
         "text": "Экран продукта с ключевыми функциями"},
        {"idx": 3, "archetype": "two_column", "title": "Своё важнее", "bullets": ["своё"],
         "left": {"heading": "Ч", "text": "чужое"}},
        {"idx": 4, "archetype": "closing", "title": "T"},
    ]}
    avail = {Archetype.TITLE, Archetype.TWO_COLUMN, Archetype.IMAGE_TEXT, Archetype.BULLETS, Archetype.CLOSING}
    o, warnings = repair_outline(raw, avail, {"brief"})

    by_title = {s.title: s for s in o.slides}
    assert by_title["Стиль"].bullets == ["Шрифты — Montserrat для заголовков", "Цвета — акцентный розовый"]
    assert by_title["Демонстрация"].paragraphs == ["Экран продукта с ключевыми функциями"]
    assert by_title["Демонстрация"].image is None  # "placeholder" строкой — не ImageSpec
    assert by_title["Своё важнее"].bullets == ["своё"]
    assert sum("колонки" in w or "«text»" in w for w in warnings) == 2
    assert not any("без содержимого" in w for w in warnings)


def test_empty_text_slide_is_dropped_or_filled_from_subtitle() -> None:
    """Режим «только шаблон»: модель отдаёт текстовые слайды без тела — до колоды они доезжать не должны (I03)."""
    raw = {"title": "T", "purpose": "other", "slides": [
        {"idx": 0, "archetype": "title", "title": "T"},
        {"idx": 1, "archetype": "bullets", "title": "Пустой", "bullets": []},
        {"idx": 2, "archetype": "cards", "title": "Из подзаголовка", "subtitle": "Что делает продукт"},
        {"idx": 3, "archetype": "bullets", "title": "С содержимым", "bullets": ["есть"]},
        {"idx": 4, "archetype": "section", "title": "Раздел"},
        {"idx": 5, "archetype": "closing", "title": "T"},
    ]}
    avail = {Archetype.TITLE, Archetype.BULLETS, Archetype.CARDS, Archetype.SECTION, Archetype.CLOSING}
    o, warnings = repair_outline(raw, avail, set())

    titles = [s.title for s in o.slides]
    assert titles == ["T", "Из подзаголовка", "С содержимым", "Раздел", "T"]  # пустой выброшен
    assert [s.idx for s in o.slides] == [0, 1, 2, 3, 4]  # и перенумерованы
    assert o.slides[1].paragraphs == ["Что делает продукт"] and o.slides[1].subtitle is None
    assert any("«Пустой»: текстовый слайд без содержимого — пропущен" in w for w in warnings)
    assert any("«Из подзаголовка»: текст слайда взят из «subtitle»" in w for w in warnings)


def test_brief_only_pack_warns_about_missing_files(tmp_path: Path) -> None:
    """Бриф из примера ссылается на product.md и data/metrics.json — без них модель выдумывала данные."""
    (tmp_path / "brief.md").write_text((PACK / "brief.md").read_text("utf-8"), "utf-8")
    assert missing_file_refs(load_content_pack(tmp_path)) == ["data/metrics.json", "product.md"]
    assert missing_file_refs(load_content_pack(PACK)) == []
    client = FakeClient(cassette())
    res = write_outline(client, load_content_pack(tmp_path), purpose="product", audience="руководители",
                        available_archetypes=VK_TECH_ARCHETYPES)
    assert any("которых нет в пакете: data/metrics.json, product.md" in w for w in res.warnings)
    assert all(src == "brief" or src.startswith("brief:") for s in res.outline.slides for src in s.sources)


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
