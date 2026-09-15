"""render: DeckIR → .pptx клонированием образцов шаблона. Не вызывает LLM."""

from deckforge.render.pptx_writer import DeckWriter, render_deck, render_pptx

__all__ = ["DeckWriter", "render_deck", "render_pptx"]
