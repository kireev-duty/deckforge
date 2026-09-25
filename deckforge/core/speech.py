"""Текст выступления: темп речи и бюджет слов по слайдам — общее для content/, audit/, export/ и pipeline/.

Вводная жюри: на выходе слайды и текст к каждому слайду, выступление ограничено по длительности.
Длительность считается по словам заметок докладчика при спокойном темпе доклада.
"""

from __future__ import annotations

import re
from collections.abc import Sequence

from deckforge.core.ir import Archetype

SPEECH_WPM = 120  # слов в минуту: спокойный доклад со слайдами на русском (разговорный темп — 130–150)
DEFAULT_TALK_MINUTES = 7.0  # вводная жюри: выступление 7 минут
MIN_SLIDE_WORDS = 10  # даже разделителю — одна фраза-переход
# доля времени слайда относительно содержательного: титул и финал короче, разделитель — одна фраза
ARCHETYPE_WEIGHT = {Archetype.TITLE: 0.5, Archetype.SECTION: 0.25, Archetype.CLOSING: 0.6}

_WORD = re.compile(r"\w+(?:[-’']\w+)*")


def word_count(text: str) -> int:
    return len(_WORD.findall(text or ""))


def speech_minutes(words: int) -> float:
    return words / SPEECH_WPM


def word_budget(archetypes: Sequence[Archetype], talk_minutes: float) -> list[int]:
    """Слов на слайд пропорционально весам архетипов; сумма ≈ `talk_minutes × SPEECH_WPM`."""
    if not archetypes:
        return []
    weights = [ARCHETYPE_WEIGHT.get(a, 1.0) for a in archetypes]
    total = talk_minutes * SPEECH_WPM
    return [max(MIN_SLIDE_WORDS, round(total * w / sum(weights))) for w in weights]


__all__ = ["ARCHETYPE_WEIGHT", "DEFAULT_TALK_MINUTES", "MIN_SLIDE_WORDS", "SPEECH_WPM", "speech_minutes",
           "word_budget", "word_count"]
