"""Контекстуальные проверки (C01–C11): VLM-судья по PNG слайда, скилл `audit_judge`."""

from deckforge.audit.contextual.judge import (
    CHECK_IDS,
    ERROR_CHECK_ID,
    QUESTIONS,
    SlideText,
    findings_from_answers,
    judge_deck,
    slides_from_context,
    slides_from_ir,
)

__all__ = ["CHECK_IDS", "ERROR_CHECK_ID", "QUESTIONS", "SlideText", "findings_from_answers", "judge_deck",
           "slides_from_context", "slides_from_ir"]
