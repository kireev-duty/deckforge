"""layout: DeckOutline + TemplateDNA + Strategy → DeckIR. Подбор образца, раскладка по слотам, подгонка текста.

Не пишет файлов и не вызывает LLM (детерминированная подгонка; slide_filler подключается отдельно).
"""

from deckforge.core.strategy import Strategy, list_strategies, load_strategy
from deckforge.layout.builder import LayoutResult, build_deck_ir, layout_deck
from deckforge.layout.exemplar_picker import pick_exemplar
from deckforge.layout.planner import PlanResult, plan

__all__ = [
    "LayoutResult", "PlanResult", "Strategy", "build_deck_ir", "layout_deck", "list_strategies", "load_strategy",
    "pick_exemplar", "plan",
]
