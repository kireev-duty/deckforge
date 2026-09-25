"""Текст выступления (вводная жюри): у каждого слайда есть текст докладчика, и он укладывается в длительность (N01–N02).

Читается из заметок .pptx (`core/deck_reader.read_notes`), поэтому работает и на чужой колоде; цель длительности
известна только с DeckIR (`DeckIR.talk_minutes`) — без неё N02 молчит.
"""

from __future__ import annotations

from deckforge.audit.context import AuditContext
from deckforge.audit.deterministic.base import finding
from deckforge.core.ir import Finding, Severity
from deckforge.core.speech import SPEECH_WPM, speech_minutes, word_count

NOTES_MIN_WORDS = 3  # меньше — текста у слайда нет («Далее.» — не текст выступления)
DURATION_TOLERANCE = 0.25  # отклонение расчётной длительности от цели, доля


def check_N01(ctx: AuditContext) -> list[Finding]:
    """У слайда нет текста выступления."""
    out: list[Finding] = []
    for slide in ctx.slides:
        words = word_count(slide.notes)
        if words < NOTES_MIN_WORDS:
            out.append(finding("N01_notes_missing", slide, Severity.WARNING,
                               "Нет текста выступления (заметок докладчика) к слайду", words=words))
    return out


def check_N02(ctx: AuditContext) -> list[Finding]:
    """Выступление по заметкам не укладывается в заданную длительность."""
    target = ctx.ir.talk_minutes if ctx.ir else None
    if not target or not ctx.slides:
        return []
    words = sum(word_count(s.notes) for s in ctx.slides)
    minutes = speech_minutes(words)
    if abs(minutes - target) <= DURATION_TOLERANCE * target:
        return []
    how = "длиннее" if minutes > target else "короче"
    return [finding("N02_talk_duration", -1, Severity.WARNING,
                    f"Выступление по заметкам ≈ {minutes:.1f} мин — {how} заданных {target:g} мин "
                    f"(больше чем на {DURATION_TOLERANCE:.0%}, темп {SPEECH_WPM} слов/мин)",
                    words=words, minutes=round(minutes, 2), target_minutes=target)]


__all__ = ["check_N01", "check_N02"]
