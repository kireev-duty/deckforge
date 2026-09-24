"""Классификатор архетипов на шаблонах датасета (только правила) + геометрия групп. Номера слайдов 1-based."""

from __future__ import annotations

import json

import pytest
from lxml import etree

from deckforge.core.ir import Archetype, Box, Slot, SlotKind
from deckforge.parsing.layout_classifier import (
    ShapeInfo,
    SlideProfile,
    build_features,
    build_slots,
    classify_template,
)
from deckforge.parsing.ooxml import NS, absolute_bbox
from tests.conftest import REPO

A = Archetype
SW, SH = 12192000, 6858000  # 13.33 × 7.5"

EXPECTED: dict[str, dict[int, Archetype]] = {
    "VK Tech": {1: A.TITLE, 3: A.SECTION, 4: A.CLOSING, 5: A.CLOSING, 6: A.CLOSING, 12: A.TEAM, 13: A.TEAM,
                14: A.CARDS, 16: A.CARDS, 17: A.CARDS, 19: A.CARDS, 42: A.KPI, 53: A.FREEFORM},
    "VK_WorkSpace": {1: A.TITLE, 2: A.AGENDA, 6: A.CARDS, 7: A.CARDS, 9: A.CARDS, 14: A.TABLE, 17: A.KPI,
                     23: A.CARDS, 27: A.PROCESS, 28: A.CLOSING, 29: A.CLOSING},
    "VK Education": {1: A.TITLE, 16: A.SECTION, 18: A.KPI, 19: A.QUOTE, 20: A.TEAM, 24: A.TWO_COLUMN,
                     25: A.FREEFORM, 26: A.CARDS, 38: A.TABLE, 39: A.TABLE, 40: A.TABLE, 42: A.PROCESS,
                     52: A.CLOSING},
    "ЛЦТ2026": {7: A.TITLE, 9: A.TEAM, 12: A.BULLETS, 16: A.CARDS, 21: A.CHART, 22: A.CHART, 23: A.CHART,
                25: A.PROCESS, 32: A.FREEFORM, 36: A.FREEFORM},
}

# слайды, которые правила обязаны пометить как неоднозначные
MUST_BE_AMBIGUOUS: dict[str, list[int]] = {
    "VK_WorkSpace": [20, 21],
    "VK Education": [31, 47, 49],
}


@pytest.fixture(scope="module")
def profiles_cache() -> dict[str, list[SlideProfile]]:
    return {}


@pytest.fixture
def profiles(template_path, profiles_cache, request) -> list[SlideProfile]:
    name = request.param
    if name not in profiles_cache:
        profiles_cache[name] = classify_template(template_path(name))
    return profiles_cache[name]


def _names():
    return [pytest.param(n, id=n) for n in EXPECTED]


@pytest.mark.parametrize("profiles", _names(), indirect=True)
def test_known_archetypes(profiles: list[SlideProfile], request) -> None:
    name = request.node.callspec.params["profiles"]
    by_no = {p.index + 1: p for p in profiles}
    wrong = {no: (by_no[no].archetype.value, arch.value) for no, arch in EXPECTED[name].items()
             if by_no[no].archetype != arch}
    assert not wrong, f"{name}: слайд → (получено, ожидалось): {wrong}"


@pytest.mark.parametrize("profiles", _names(), indirect=True)
def test_ambiguous_go_to_vlm(profiles: list[SlideProfile], request) -> None:
    name = request.node.callspec.params["profiles"]
    by_no = {p.index + 1: p for p in profiles}
    for no in MUST_BE_AMBIGUOUS.get(name, []):
        assert by_no[no].ambiguous, f"{name}: слайд {no} должен уходить в VLM ({by_no[no].candidates[:2]})"


@pytest.mark.parametrize("profiles", _names(), indirect=True)
def test_profiles_invariants(profiles: list[SlideProfile]) -> None:
    assert profiles
    for p in profiles:
        ids = [s.id for s in p.slots]
        assert len(ids) == len(set(ids)), f"слайд {p.index + 1}: дублируются id слотов"
        assert p.candidates and p.candidates[0].archetype == p.archetype
        assert 0 < p.confidence <= 1
        if p.archetype != Archetype.FREEFORM:
            assert p.slots, f"слайд {p.index + 1}: {p.archetype} без слотов"
        if any(s.kind == "title" and s.is_placeholder for s in p.features.shapes):
            assert any(s.kind == SlotKind.TITLE for s in p.slots), f"слайд {p.index + 1}: есть ph:title, нет слота"
        for s in p.slots:
            if s.kind in (SlotKind.TITLE, SlotKind.BODY, SlotKind.LABEL):
                assert s.max_chars and s.max_chars > 0 and s.max_lines
            assert s.box.w > 0 and s.box.h > 0
        ex = p.to_exemplar()
        assert ex.id == f"slide{p.index + 1}" and ex.archetype == p.archetype
        assert ex.model_dump()["slots"] == [s.model_dump() for s in p.slots]


@pytest.mark.parametrize("profiles", _names(), indirect=True)
def test_every_template_has_core_archetypes(profiles: list[SlideProfile]) -> None:
    """Любой шаблон датасета даёт хотя бы титульный и карточки."""
    found = {p.archetype for p in profiles}
    assert Archetype.TITLE in found
    assert Archetype.CARDS in found
    assert len(found - {Archetype.FREEFORM}) >= 6


@pytest.mark.parametrize("profiles", [pytest.param("VK Education", id="VK Education")], indirect=True)
def test_timeline_years_are_labels_not_footer_dates(profiles: list[SlideProfile]) -> None:
    """Годы над событиями таймлайна, которые VLM зовёт `date`, становятся LABEL, а не полем колонтитула."""
    import copy

    from deckforge.parsing.layout_classifier import apply_vlm

    p = copy.deepcopy({p.index + 1: p for p in profiles}[42])
    years = [s.id for s in p.slots if (s.sample_text or "").strip().isdigit() and len(s.sample_text.strip()) == 4]
    assert len(years) == 7
    apply_vlm(p, {"archetype": "process", "confidence": 0.98, "slot_roles": {i: "date" for i in years}})
    kinds = {s.id: s.kind for s in p.slots}
    assert all(kinds[i] == SlotKind.LABEL for i in years), {i: kinds[i] for i in years}


@pytest.mark.parametrize("profiles", [pytest.param("VK Tech", id="VK Tech")], indirect=True)
def test_vlm_text_role_on_picture_is_decor(profiles: list[SlideProfile]) -> None:
    """Бейджи-картинки шагов (VK Tech, слайд 8), которые VLM зовёт `label`: текст в p:pic не пишется —
    слот снимается, иначе подписи шагов молча пропадали бы, а picture-слот штрафовался бы как пустая рамка."""
    import copy

    from deckforge.parsing.layout_classifier import apply_vlm

    p = copy.deepcopy({p.index + 1: p for p in profiles}[8])
    pics = [s.id for s in p.features.shapes if s.kind == "pic" and s.id in {x.id for x in p.slots}]
    assert pics
    apply_vlm(p, {"archetype": "agenda", "confidence": 0.95, "slot_roles": {i: "label" for i in pics}})
    assert not {s.id for s in p.slots} & set(pics)
    assert sum(1 for s in p.slots if s.kind == SlotKind.BODY) == 5


@pytest.mark.parametrize("profiles", [pytest.param("VK Tech", id="VK Tech")], indirect=True)
def test_card_frames(profiles: list[SlideProfile]) -> None:
    """slide14 — у каждой карточки своя подложка (пустую рендер уберёт); slide19 — полосы на ряд
    и сетка «01–04» картинкой лейаута (пустая карточка осталась бы видна)."""
    by_no = {p.index + 1: p for p in profiles}
    assert by_no[14].card_frames and by_no[14].to_exemplar().card_frames
    assert not by_no[19].card_frames


# ── чужие шаблоны из data/wild (пропускаются, если файла нет) ──

WILD_EXPECTED: dict[str, dict[int, Archetype]] = {
    # обложка — титул, «Содержание» на 48 пунктов — agenda, «лестница» без body — не bullets
    "Презентация в оформлении РУДН": {1: A.TITLE, 2: A.AGENDA, 63: A.CLOSING},
}


@pytest.mark.parametrize("name", list(WILD_EXPECTED))
def test_wild_archetypes(name: str) -> None:
    path = REPO / "data" / "wild" / f"{name}.pptx"
    if not path.exists() or path.stat().st_size < 10_000:
        pytest.skip(f"нет {path.name}")
    by_no = {p.index + 1: p for p in classify_template(path)}
    wrong = {no: (by_no[no].archetype.value, arch.value) for no, arch in WILD_EXPECTED[name].items() if by_no[no].archetype != arch}
    assert not wrong, wrong
    # глифы перед пунктами и номер страницы — не слоты
    for p in by_no.values():
        for s in p.slots:
            assert s.sample_text != "➜", f"слайд {p.index + 1}: глиф стал слотом"
            if s.kind == SlotKind.SLIDE_NUMBER:
                assert s.placeholder_type is None and s.sample_text == str(p.index + 1)
        low = [s for s in p.features.numbers if s.text == str(p.index + 1) and s.fy > 0.8]
        assert not low, f"слайд {p.index + 1}: номер страницы в подвале как KPI"


def test_wild_empty_big_placeholders_are_numbers() -> None:
    """Три пустых плейсхолдера 96 pt над подписями — KPI-цифры, а не body."""
    path = REPO / "data" / "wild" / "02_HSE_Presentation_Shablon_en.pptx"
    if not path.exists() or path.stat().st_size < 10_000:
        pytest.skip(f"нет {path.name}")
    p = {p.index + 1: p for p in classify_template(path)}[9]
    big = [s for s in p.slots if (s.size_pt or 0) >= 90]
    assert len(big) == 3 and all(s.kind == SlotKind.NUMBER for s in big), [(s.id, s.kind, s.size_pt) for s in big]
    assert p.archetype == A.KPI


# ── синтетические фигуры ──


def _shape(id_: str, text: str, x: float, y: float, w: float, h: float, size: float = 18.0, kind: str = "text",
           bold: bool = False) -> ShapeInfo:
    return ShapeInfo(id=id_, name=id_, kind=kind, box=Box(x=int(x * SW), y=int(y * SH), w=int(w * SW), h=int(h * SH)),
                     fx=x, fy=y, fw=w, fh=h, text=text, chars=len(text), paragraphs=1 if text else 0, size_pt=size, bold=bold)


def test_glyph_boxes_and_edge_caption_are_not_body_slots() -> None:
    shapes = [
        _shape("t", "Заголовок слайда", 0.05, 0.12, 0.6, 0.08, size=24),
        _shape("hdr", "Раздел 2", 0.3, 0.02, 0.6, 0.06, size=12),  # подпись раздела в шапке
        _shape("g1", "➜", 0.05, 0.3, 0.03, 0.09, size=19), _shape("b1", "Первый тезис", 0.1, 0.3, 0.8, 0.09),
        _shape("g2", "➜", 0.05, 0.42, 0.03, 0.09, size=19), _shape("b2", "Второй тезис", 0.1, 0.42, 0.8, 0.09),
    ]
    f = build_features(shapes, index=4, n_slides=20)
    assert f.title is not None and f.title.id == "t"
    assert {s.id for s in f.content} == {"hdr", "b1", "b2"}, "глифы не входят в контент"
    kinds = {s.id: s.kind for s in build_slots(f, Archetype.BULLETS)}
    assert "g1" not in kinds and kinds["hdr"] == SlotKind.CAPTION and kinds["b1"] != SlotKind.CAPTION


def test_cover_title_found_below_top_zone() -> None:
    def shapes():  # build_features мутирует фигуры — каждый вызов на свежих
        return [_shape("name", "Все макеты оформления", 0.1, 0.73, 0.82, 0.1, size=39),
                _shape("sub", "Фирменный стиль · GoSlide", 0.11, 0.86, 0.81, 0.04, size=15)]

    assert build_features(shapes(), index=0, n_slides=20).title.id == "name"
    assert build_features(shapes(), index=5, n_slides=20).title is None, "не на обложке зона заголовка — верхние 30 %"


def test_title_over_decor_line_gets_hard_lines() -> None:
    title = _shape("t", "Заголовок", 0.06, 0.06, 0.88, 0.1, size=28)
    line = _shape("ln", "", 0.06, 0.18, 0.88, 0.0, kind="connector")
    f = build_features([title, line], index=3, n_slides=20)
    slot = next(s for s in build_slots(f, Archetype.BULLETS) if s.kind == SlotKind.TITLE)
    assert slot.hard_lines and slot.max_lines == 1
    f2 = build_features([title], index=3, n_slides=20)
    assert not next(s for s in build_slots(f2, Archetype.BULLETS)).hard_lines


def _title_slot(block: tuple[float, float, float, float] | None, title_h: float) -> Slot:
    """Заголовок 36 pt в боксе как у VK WorkSpace (слайд 540 pt высотой) и, если задан, текстовый блок (x, y, w, h)."""
    shapes = [_shape("t", "Заголовок", 0.035, 0.062, 0.642, title_h, size=36)]
    if block is not None:
        shapes.append(_shape("b", "Карточка с текстом", *block))
    f = build_features(shapes, index=4, n_slides=20)
    return next(s for s in build_slots(f, Archetype.CARDS) if s.kind == SlotKind.TITLE)


def test_title_over_block_inside_box_gets_hard_lines() -> None:
    alone = _title_slot(None, 0.091)
    assert not alone.hard_lines and alone.max_lines == 1
    assert alone.wrap_w is None
    # WorkSpace slide5: колонка карточек справа начинается выше низа бокса — не помещается даже первая строка:
    # бокс сужается до карточек, строки идут левее них (вторая — как у любого заголовка, не hard)
    cards = _title_slot((0.442, 0.125, 0.485, 0.249), 0.091)
    assert cards.wrap_w == int(0.442 * SW) - int(0.035 * SW)
    assert not cards.hard_lines and cards.max_lines == 1 and cards.max_chars < alone.max_chars
    # бокс на две строки, описание во всю ширину сразу под первой (slide16) — строка одна, бокс не сужается
    two = _title_slot(None, 0.170)
    desc = _title_slot((0.035, 0.181, 0.796, 0.094), 0.170)
    assert desc.hard_lines and desc.wrap_w is None and desc.max_chars <= two.max_chars // 2 + 1
    # вторая строка помещается над блоком — ограничения нет; блок ниже бокса не смотрим
    assert not _title_slot((0.035, 0.225, 0.796, 0.094), 0.170).hard_lines
    assert not _title_slot((0.035, 0.160, 0.796, 0.094), 0.091).hard_lines


# ── геометрия групп ──


def test_absolute_bbox_applies_group_transform() -> None:
    xml = f"""
    <p:grpSp xmlns:p="{NS['p']}" xmlns:a="{NS['a']}">
      <p:grpSpPr><a:xfrm>
        <a:off x="1000" y="2000"/><a:ext cx="2000" cy="1000"/>
        <a:chOff x="0" y="0"/><a:chExt cx="1000" cy="1000"/>
      </a:xfrm></p:grpSpPr>
      <p:sp><p:spPr><a:xfrm><a:off x="500" y="500"/><a:ext cx="100" cy="200"/></a:xfrm></p:spPr></p:sp>
    </p:grpSp>"""
    grp = etree.fromstring(xml)
    sp = grp.find("p:sp", NS)
    # масштаб 2× по x, 1× по y; сдвиг (1000, 2000)
    assert absolute_bbox(sp) == (2000, 2500, 200, 200)
    assert absolute_bbox(grp) == (1000, 2000, 2000, 1000)


# ── кэш ответов VLM: out/archetypes → data/archetypes, привязка к sha1 ──


def test_bundled_vlm_cache_covers_dataset_and_holdout(template_path) -> None:
    from deckforge.parsing.exemplars import ARCHETYPES_BUNDLED, find_vlm_cache, template_sha1

    for name in EXPECTED:
        pptx = template_path(name)
        cache = find_vlm_cache(pptx, ARCHETYPES_BUNDLED)
        assert cache is not None, f"{name}: нет data/archetypes/{pptx.stem}.json — tools/classify_layouts.py --publish"
        data = json.loads(cache.read_text("utf-8"))
        assert data["template_sha1"] == template_sha1(pptx), f"{name}: кэш от другого файла шаблона"
        assert data["slides"] and all(s["vlm"] for s in data["slides"])


def test_vlm_cache_prefers_work_dir_and_checks_sha1(template_path, tmp_path, monkeypatch) -> None:
    from deckforge.parsing import exemplars as ex

    pptx = template_path("VK Tech")
    work, bundled = tmp_path / "out", tmp_path / "data"
    work.mkdir(), bundled.mkdir()
    monkeypatch.setattr(ex, "ARCHETYPES_CACHE", work)
    monkeypatch.setattr(ex, "ARCHETYPES_BUNDLED", bundled)
    assert ex.find_vlm_cache(pptx) is None
    good = {"template_sha1": ex.template_sha1(pptx), "slides": []}
    (bundled / f"{pptx.stem}.json").write_text(json.dumps(good), "utf-8")
    assert ex.find_vlm_cache(pptx) == bundled / f"{pptx.stem}.json"
    # кэш от другого файла с тем же именем пропускается
    (work / f"{pptx.stem}.json").write_text(json.dumps({"template_sha1": "0" * 40, "slides": []}), "utf-8")
    assert ex.find_vlm_cache(pptx) == bundled / f"{pptx.stem}.json"
    (work / f"{pptx.stem}.json").write_text(json.dumps(good), "utf-8")
    assert ex.find_vlm_cache(pptx) == work / f"{pptx.stem}.json"
