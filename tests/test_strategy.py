"""core/strategy: YAML-стратегии грузятся в типизированную модель."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from deckforge.core.ir import Archetype
from deckforge.core.strategy import Strategy, list_strategies, load_strategy


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
