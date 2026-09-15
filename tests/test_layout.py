"""layout: planner (применение стратегии), picker (выбор образца), fitting (подгонка), builder (DeckIR)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pptx import Presentation

from deckforge.core.ir import Archetype, Box, ChartSpec, DeckOutline, Exemplar, ImageSpec, OutlineSlide, Slot, SlotKind, TableSpec
from deckforge.core.strategy import Strategy, load_strategy
from deckforge.layout import build_deck_ir, plan
from deckforge.layout.exemplar_picker import candidate_archetypes, pick_exemplar
from deckforge.layout.fitting import fit_number, shorten, shorten_words, split_label_body
from deckforge.layout.planner import parse_num, table_to_chart
from deckforge.parsing.exemplars import load_exemplars
from deckforge.parsing.extract_tokens import extract_tokens
from deckforge.render import render_pptx

REPO = Path(__file__).resolve().parents[1]
OUTLINE = REPO / "examples" / "content_pack" / "outline.json"
SLIDE_W, SLIDE_H = 9144000, 5143500  # 10 × 5.625"


def outline() -> DeckOutline:
    return DeckOutline.model_validate_json(OUTLINE.read_text("utf-8"))


def strategy(**over) -> Strategy:
    base = dict(name="t", target_slides={"min": 8, "max": 12}, density={"max_bullets": 4, "max_words_per_bullet": 10, "max_fill_ratio": 0.7},
                archetype_priority=[Archetype.BULLETS, Archetype.CARDS])
    base.update(over)
    return Strategy.model_validate(base)


# ──────────────────────────── planner ────────────────────────────


def test_executive_turns_chart_into_table_and_drops_sections() -> None:
    res = plan(outline(), load_strategy("executive"), set(Archetype))
    archs = res.archetypes
    assert "section" not in archs and "chart" not in archs
    assert archs.count("table") == 2  # исходная таблица + таблица из графика
    assert load_strategy("executive").target_slides.min <= len(res.slides) <= load_strategy("executive").target_slides.max
    assert not res.warnings


def test_narrative_inserts_sections_and_splits_dense_slides() -> None:
    res = plan(outline(), load_strategy("narrative"), set(Archetype))
    archs = res.archetypes
    assert archs.count("section") >= 2 and archs[1] == "section"
    titles = [s.title for s in res.slides]
    assert any(t.endswith("(1/2)") for t in titles) and any(t.endswith("(2/2)") for t in titles)
    assert all(len(s.bullets) <= 4 for s in res.slides)
    assert len(res.slides) <= 15


def test_narrative_without_section_exemplars_has_no_sections() -> None:
    res = plan(outline(), load_strategy("narrative"), set(Archetype) - {Archetype.SECTION})
    assert "section" not in res.archetypes


def test_visual_turns_numeric_table_into_chart_but_keeps_text_table() -> None:
    res = plan(outline(), load_strategy("visual"), set(Archetype))
    assert res.archetypes.count("chart") == 2 and "table" not in res.archetypes
    text_table = TableSpec(header=["Роль", "Задача"], rows=[["Лид", "планирует"], ["Инженер", "делает"]])
    assert table_to_chart(text_table, "t") is None


def test_kpi_split_by_capacity_only_when_room() -> None:
    o = outline()
    roomy = plan(o, strategy(target_slides={"min": 8, "max": 15}, data_visualization={"key_metrics": "kpi"}), set(Archetype), kpi_capacity=2)
    assert roomy.archetypes.count("kpi") == 2
    tight = plan(o, strategy(target_slides={"min": 8, "max": 10}, data_visualization={"key_metrics": "kpi"}), set(Archetype), kpi_capacity=2)
    assert tight.archetypes.count("kpi") == 1


def test_parse_num() -> None:
    assert parse_num("1 250") == 1250 and parse_num("−89 %") == -89 and parse_num("4,7") == 4.7
    assert parse_num("6 ч") is None and parse_num("×3") == 3


# ──────────────────────────── picker ────────────────────────────


def _slot(id_: str, kind: SlotKind, x: float, y: float, w: float, h: float, max_chars: int | None = 100, size: float = 14) -> Slot:
    box = Box(x=int(x * SLIDE_W), y=int(y * SLIDE_H), w=int(w * SLIDE_W), h=int(h * SLIDE_H))
    return Slot(id=id_, kind=kind, box=box, max_chars=max_chars, max_lines=1 if kind != SlotKind.BODY else 8,
                max_items=8 if kind == SlotKind.BODY else None, size_pt=size)


def _exemplars() -> list[Exemplar]:
    title = _slot("t", SlotKind.TITLE, 0.05, 0.05, 0.9, 0.12, max_chars=60, size=24)
    bullets = Exemplar(id="slide1", source_index=0, layout_name="a", archetype=Archetype.BULLETS,
                       slots=[title, _slot("b", SlotKind.BODY, 0.05, 0.25, 0.9, 0.6, max_chars=600)])
    cards = Exemplar(id="slide2", source_index=1, layout_name="a", archetype=Archetype.CARDS, slots=[title] + [
        _slot(f"l{i}", SlotKind.LABEL, 0.05 + 0.3 * i, 0.3, 0.25, 0.05, max_chars=20) for i in range(3)
    ] + [_slot(f"b{i}", SlotKind.BODY, 0.05 + 0.3 * i, 0.36, 0.25, 0.4, max_chars=200) for i in range(3)])
    pic = Exemplar(id="slide3", source_index=2, layout_name="a", archetype=Archetype.IMAGE_TEXT,
                   slots=[title, _slot("b", SlotKind.BODY, 0.5, 0.25, 0.45, 0.6, max_chars=400),
                          _slot("p", SlotKind.PICTURE, 0.05, 0.25, 0.4, 0.6, max_chars=None)])
    sec = Exemplar(id="slide4", source_index=3, layout_name="a", archetype=Archetype.TITLE,
                   slots=[_slot("t", SlotKind.TITLE, 0.1, 0.4, 0.8, 0.2, max_chars=60, size=40)])
    return [bullets, cards, pic, sec]


def test_priority_switches_between_bullets_and_cards() -> None:
    slide = OutlineSlide(idx=1, archetype=Archetype.BULLETS, title="Три тезиса", bullets=["А — а", "Б — б", "В — в"])
    ex = _exemplars()
    e_b, _ = pick_exemplar(slide, ex, strategy(archetype_priority=[Archetype.BULLETS, Archetype.CARDS]), SLIDE_W * SLIDE_H)
    e_c, _ = pick_exemplar(slide, ex, strategy(archetype_priority=[Archetype.CARDS, Archetype.BULLETS]), SLIDE_W * SLIDE_H)
    assert e_b.archetype == Archetype.BULLETS and e_c.archetype == Archetype.CARDS


def test_section_falls_back_to_title_exemplar() -> None:
    slide = OutlineSlide(idx=1, archetype=Archetype.SECTION, title="Контекст")
    e, _ = pick_exemplar(slide, _exemplars(), strategy(), SLIDE_W * SLIDE_H)
    assert e is not None and e.archetype == Archetype.TITLE
    assert candidate_archetypes(Archetype.SECTION, strategy())[0] == Archetype.SECTION


def test_minimal_images_avoid_picture_exemplar_without_image() -> None:
    slide = OutlineSlide(idx=1, archetype=Archetype.IMAGE_TEXT, title="Т", bullets=["один", "два"])
    e, _ = pick_exemplar(slide, _exemplars(), strategy(images="minimal", archetype_priority=[Archetype.IMAGE_TEXT, Archetype.BULLETS]),
                         SLIDE_W * SLIDE_H)
    assert e.archetype != Archetype.IMAGE_TEXT
    slide.image = ImageSpec(path="x.png")
    e2, _ = pick_exemplar(slide, _exemplars(), strategy(images="minimal", archetype_priority=[Archetype.IMAGE_TEXT, Archetype.BULLETS]),
                          SLIDE_W * SLIDE_H)
    assert e2.archetype == Archetype.IMAGE_TEXT


def test_chart_without_data_slot_is_impossible() -> None:
    slide = OutlineSlide(idx=1, archetype=Archetype.CHART, title="Т",
                         chart=ChartSpec(kind="bar", title="c", categories=["a"], series={"s": [1]}))
    e, score = pick_exemplar(slide, _exemplars(), strategy(), SLIDE_W * SLIDE_H)
    assert e is None


# ──────────────────────────── fitting ────────────────────────────


def test_shorten_prefers_separators_then_words() -> None:
    assert shorten("Статусы вместо работы — до 9,5 часов в неделю", 30) == "Статусы вместо работы"
    assert shorten("Очень длинное предложение без разделителей внутри", 25).endswith("…")
    assert shorten("короткий", 100) == "короткий"
    assert shorten_words("Риски заранее — дайджест приходит в понедельник, до планирования недели", 5) == "Риски заранее"


def test_split_label_body_and_numbers() -> None:
    assert split_label_body("Лид — пояснение текста") == ("Лид", "пояснение текста")
    assert split_label_body("Просто пункт") == ("Просто пункт", "")
    slot = Slot(id="n", kind=SlotKind.NUMBER, box=Box(x=0, y=0, w=1, h=1), max_chars=2, max_lines=1, size_pt=160)
    num, unit, size = fit_number("1,8 дня", slot)
    assert (num, unit) == ("1,8", "дня") and size is not None and size < 160
    assert fit_number("42 %", slot)[:2] == ("42%", "")


# ──────────────────────────── builder e2e ────────────────────────────


@pytest.mark.parametrize("name", ["executive", "narrative", "visual"])
def test_build_and_render_on_vk_tech(template_path, tmp_path, name: str) -> None:
    pptx = template_path("VK Tech")
    exemplars = load_exemplars(pptx)
    tokens = extract_tokens(pptx)
    st = load_strategy(name)
    res = build_deck_ir(outline(), st, exemplars, tokens.template_id, tokens.slide_w, tokens.slide_h)
    ir = res.ir
    assert ir.strategy == st.id
    assert st.target_slides.min <= len(ir.slides) <= st.target_slides.max, res.warnings
    ids = {e.id for e in exemplars}
    assert all(s.exemplar_id in ids for s in ir.slides)
    assert not any(c.exemplar_id is None for c in res.choices)
    # весь контент разложен: ни одна цифра KPI и ни один буллет не потерян (кроме явно отброшенных)
    dropped = [w for w in res.warnings if "отброшен" in w]
    assert not dropped, dropped
    out = render_pptx(ir, pptx, exemplars, tmp_path / f"{name}.pptx")
    prs = Presentation(str(out))
    assert len(prs.slides) == len(ir.slides)
    kinds = json.loads(ir.model_dump_json())["slides"]
    assert kinds  # сериализуется


def test_strategies_differ_on_same_content(template_path) -> None:
    pptx = template_path("VK Tech")
    exemplars = load_exemplars(pptx)
    tokens = extract_tokens(pptx)
    seqs = {}
    for name in ("executive", "narrative", "visual"):
        res = build_deck_ir(outline(), load_strategy(name), exemplars, tokens.template_id, tokens.slide_w, tokens.slide_h)
        seqs[name] = res.plan.archetypes  # план стратегии; образцы могут быть фолбэками (цитата → section)
    assert len(seqs["executive"]) < len(seqs["narrative"])
    assert "chart" not in seqs["executive"] and "chart" in seqs["visual"]
    assert "section" in seqs["narrative"] and "section" not in seqs["visual"] and "section" not in seqs["executive"]
