"""parsing: .pptx → TemplateDNA. Не знает о брифе и генерации."""

from deckforge.parsing.extract_tokens import TemplateTokens, extract_tokens

__all__ = ["SlideProfile", "TemplateTokens", "classify_template", "extract_tokens"]


def __getattr__(name: str):
    # ленивый импорт: иначе `python -m deckforge.parsing.layout_classifier` ругается на двойной импорт модуля
    if name in ("SlideProfile", "classify_template"):
        from deckforge.parsing import layout_classifier

        return getattr(layout_classifier, name)
    raise AttributeError(name)
