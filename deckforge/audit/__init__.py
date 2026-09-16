"""Аудит колоды: детерминированные проверки по XML (deterministic/) и контекстуальные по PNG (contextual/).

Аудит ничего не меняет в колоде — только Finding с подсветкой и именем автофикса (каталог `core/autofix`,
применяет `layout/autofix`).
"""

from deckforge.audit.run import audit_deck, report_markdown, summary, with_contextual

__all__ = ["audit_deck", "report_markdown", "summary", "with_contextual"]
