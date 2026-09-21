"""layout: DeckOutline + TemplateDNA + Strategy → DeckIR."""

from deckforge.core.strategy import Strategy, list_strategies, load_strategy
from deckforge.layout.autofix import FixResult, apply_fixes
from deckforge.layout.builder import LayoutResult, build_deck_ir, layout_deck
from deckforge.layout.exemplar_picker import pick_exemplar
from deckforge.layout.planner import PlanResult, plan

__all__ = [
    "FixResult", "LayoutResult", "PlanResult", "Strategy", "apply_fixes", "build_deck_ir", "layout_deck",
    "list_strategies", "load_strategy", "pick_exemplar", "plan",
]
