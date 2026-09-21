"""pipeline: оркестрация parse → content → layout → render → audit → export."""

from deckforge.pipeline.config import RunConfig, load_config
from deckforge.pipeline.run import (
    DeckResult,
    OutlineStep,
    ParsedTemplate,
    RunContext,
    RunResult,
    build_deck,
    make_outline,
    parse_template,
    refine_deck,
    run,
    run_summary,
    soffice_available,
)
from deckforge.pipeline.stats import compare_table, deck_stats

__all__ = [
    "DeckResult", "OutlineStep", "ParsedTemplate", "RunConfig", "RunContext", "RunResult", "build_deck",
    "compare_table", "deck_stats", "load_config", "make_outline", "parse_template", "refine_deck", "run",
    "run_summary", "soffice_available",
]
