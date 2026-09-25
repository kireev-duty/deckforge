"""Текст выступления колоды одним файлом: `<strategy>.speech.md` — по слайдам, с расчётом времени."""

from __future__ import annotations

from pathlib import Path

from deckforge.core.ir import DeckIR
from deckforge.core.speech import SPEECH_WPM, speech_minutes, word_count


def speech_markdown(ir: DeckIR, deck_title: str) -> str:
    words = sum(word_count(s.notes) for s in ir.slides)
    target = f" · цель {ir.talk_minutes:g} мин" if ir.talk_minutes else ""
    lines = [f"# {deck_title}", "",
             f"Текст выступления · вариант {ir.strategy} · слайдов {len(ir.slides)} · "
             f"≈ {speech_minutes(words):.1f} мин ({words} слов при {SPEECH_WPM} слов/мин){target}", ""]
    for s in ir.slides:
        title, _ = s.title_and_text()
        seconds = round(word_count(s.notes) / SPEECH_WPM * 60)
        lines += [f"## {s.idx + 1}. {title or s.archetype.value}", "", s.notes.strip() or "_(текста нет)_", "",
                  f"≈ {seconds} с", ""]
    return "\n".join(lines)


def export_speech(ir: DeckIR, deck_title: str, out_path: str | Path) -> Path:
    out_path = Path(out_path)
    out_path.write_text(speech_markdown(ir, deck_title), "utf-8")
    return out_path


__all__ = ["export_speech", "speech_markdown"]
