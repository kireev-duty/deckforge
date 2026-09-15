"""parsing: .pptx → TemplateDNA. Не знает о брифе и генерации."""

from deckforge.parsing.extract_tokens import TemplateTokens, extract_tokens

__all__ = ["TemplateTokens", "extract_tokens"]
