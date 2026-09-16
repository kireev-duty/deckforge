"""pipeline: оркестрация parse → content → layout → render (→ audit → export), тайминги и manifest.json."""

from deckforge.pipeline.config import RunConfig, load_config
from deckforge.pipeline.run import DeckResult, RunResult, run
from deckforge.pipeline.stats import compare_table, deck_stats

__all__ = ["DeckResult", "RunConfig", "RunResult", "compare_table", "deck_stats", "load_config", "run"]
