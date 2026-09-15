"""Классификатор архетипов на 4 шаблонах (только правила, без VLM) + геометрия групп.

Номера слайдов — 1-based, как в PowerPoint и в contact.png. ЛЦТ2026 — holdout: проверяем, но пороги
под него не подгоняем.
"""

from __future__ import annotations

import pytest
from lxml import etree

from deckforge.core.ir import Archetype, SlotKind
from deckforge.parsing.layout_classifier import SlideProfile, classify_template
from deckforge.parsing.ooxml import NS, absolute_bbox

A = Archetype

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

# слайды, которые правила обязаны пометить как неоднозначные (картинка-график vs фото решает VLM)
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
    """Любой шаблон датасета даёт хотя бы титульный и карточки (у ЛЦТ2026 нет ни финального, ни разделителя)."""
    found = {p.archetype for p in profiles}
    assert Archetype.TITLE in found
    assert Archetype.CARDS in found
    assert len(found - {Archetype.FREEFORM}) >= 6


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
