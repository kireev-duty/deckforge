"""core/strategy: YAML-стратегии грузятся в типизированную модель."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from deckforge.core.ir import Archetype
from deckforge.core.strategy import DEFAULT_TARGET_SLIDES, Strategy, list_strategies, load_strategy


def test_three_strategies_load() -> None:
    names = list_strategies()
    assert {"executive", "narrative", "visual"} <= set(names)
    for n in names:
        st = load_strategy(n)
        assert st.name == n and st.id == f"{n}@{st.version}"
        assert st.target_slides.min <= st.target_slides.max
        assert all(isinstance(a, Archetype) for a in st.archetype_priority)
        assert st.outline_rules.strip()


def test_axis_is_density_and_visualization() -> None:
    ex, na, vi = (load_strategy(n) for n in ("executive", "narrative", "visual"))
    assert ex.target_slides.max < na.target_slides.min  # executive короче narrative
    assert ex.density.max_bullets > na.density.max_bullets
    assert ex.data_visualization.numeric_series == "table" and vi.data_visualization.numeric_series == "chart"
    assert na.sections and not ex.sections
    assert vi.priority_of(Archetype.CHART) < vi.priority_of(Archetype.TABLE)
    assert ex.priority_of(Archetype.TABLE) < ex.priority_of(Archetype.CHART)


def test_default_range_meets_tz_volume() -> None:
    """ТЗ: 10–15 слайдов при объёме по умолчанию — ни одна стратегия не опускается ниже 10."""
    for n in ("executive", "narrative", "visual"):
        st = load_strategy(n)
        assert 10 <= st.target_slides.min and st.target_slides.max <= 15, n
        assert st.audience_hint.strip(), f"{n}: нет audience_hint — для кого этот вариант"


def test_for_target_shifts_range_and_keeps_axis() -> None:
    """Объём, заданный пользователем, сдвигает диапазоны; executive остаётся короче narrative."""
    ex, na = load_strategy("executive"), load_strategy("narrative")
    assert ex.for_target(None) is ex and ex.for_target(DEFAULT_TARGET_SLIDES) is ex
    ex15, na15 = ex.for_target(15), na.for_target(15)
    assert (ex15.target_slides.min, ex15.target_slides.max) == (ex.target_slides.min + 3, ex.target_slides.max + 3)
    assert (na15.target_slides.min, na15.target_slides.max) == (na.target_slides.min + 3, na.target_slides.max + 3)
    assert ex15.target_slides.max < na15.target_slides.min
    # сдвиг не трогает остальное и не меняет исходную стратегию
    assert ex15.density == ex.density and ex15.name == ex.name and ex.target_slides.max == 11
    # обрезка по границам SlideRange
    assert na.for_target(25).target_slides.max == 25 and ex.for_target(3).target_slides.min == 3
    assert ex.for_target(3).target_slides.min <= ex.for_target(3).target_slides.max


def test_priority_of_unknown_is_last() -> None:
    st = load_strategy("executive")
    assert st.priority_of(Archetype.FREEFORM) == len(st.archetype_priority)


def test_validation_rejects_bad_range() -> None:
    with pytest.raises(ValidationError):
        Strategy(name="x", target_slides={"min": 12, "max": 10}, density={"max_bullets": 4, "max_words_per_bullet": 8, "max_fill_ratio": 0.5},
                 archetype_priority=[])
    with pytest.raises(ValidationError):
        Strategy(name="x", target_slides={"min": 8, "max": 10}, density={"max_bullets": 4, "max_words_per_bullet": 8, "max_fill_ratio": 0.5},
                 archetype_priority=["not_an_archetype"])
