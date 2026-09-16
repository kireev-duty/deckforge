"""Аудит колоды: детерминированные проверки по XML (deterministic/) и контекстуальные по PNG (contextual/).

Аудит ничего не меняет в колоде — только Finding с подсветкой и именем автофикса (применяет layout/).
"""

from deckforge.audit.run import audit_deck, report_markdown, summary

__all__ = ["audit_deck", "report_markdown", "summary"]
