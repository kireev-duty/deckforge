"""Текст выступления к каждому слайду готовой колоды: скилл `speaker_notes` → заметки докладчика.

Вызывается на DeckIR каждой стратегии, а не на outline: у вариантов разный состав слайдов (разделители,
продолжения, слитые слайды), а текст нужен к каждому. Объём — `core/speech.word_budget` под заданные минуты.
Черновик — заметки, которые уже есть в IR (`speaker_notes` outline). Ответ модели проходит `repair_notes`:
мягкие поправки формы, как `repair_outline`; «странность» модели чинится там, а не ретраем.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from typing import Any

from deckforge.core.ir import DeckIR
from deckforge.core.speech import word_budget, word_count
from deckforge.llm.client import LLMClient
from deckforge.llm.skills import load_skill

MAX_SLIDE_TEXT = 600  # символов текста слайда во входе: модели нужен смысл, а не весь текст
MAX_BRIEF_CHARS = 1500
# объём модели задаётся и в предложениях: слова она не считает (v1 писала в 2–3 раза короче бюджета, skill.yaml v2–v3)
WORDS_PER_SENTENCE = 12
# символы, недопустимые в XML 1.0 (как `layout/fitting.xml_safe`) — заметки пишутся в .pptx
_XML_BAD = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\ud800-\udfff\ufffe\uffff]")
# ремарки докладчику вместо текста: «(пауза)», «[кликнуть]», «(переход к слайду 4)»
_STAGE = re.compile(r"\s*[(\[](?:пауза|pause|переход|клик|кликнуть|жест|смотрит|указывает|слайд\s*\d+)[^)\]]*[)\]]",
                    re.IGNORECASE)
_MARKDOWN = re.compile(r"\*\*|__|`|^#+\s*|^\s*[-•*]\s+", re.MULTILINE)
_PREFIX = re.compile(r"^\s*(?:слайд\s*\d+\s*[.:—-]|текст\s*:|заметки\s*:)\s*", re.IGNORECASE)
TEXT_KEYS = ("text", "notes", "speaker_notes", "speech", "content")
# выступление ограничено по длительности (вводная жюри): текст длиннее цели больше чем на эту долю
# сокращается до цели — модель на 15 слайдах пишет до 8,8 мин вместо 7 (narrative, 25.09)
OVER_LIMIT = 0.1
# короче цели больше чем на эту долю (допуск N02) — второй вызов, по слайду берётся более длинный текст.
# Объём у модели плавает от раза к разу: на одном входе 3,5–10 мин при цели 7 (VK Tech, 25.09); недобор
# детерминированно не починить, а перебор после второго прохода снимает `fit_to_budget`
UNDER_LIMIT = 0.25
SECOND_PASS_MIN_S = 45.0  # столько должно остаться до дедлайна, чтобы звать модель второй раз
_SENTENCE_END = re.compile(r"[.!?…][»\")]?(?=\s)")


@dataclass
class NotesResult:
    notes: list[str]  # по слайдам IR, пустая строка — текста нет
    warnings: list[str] = field(default_factory=list)
    seconds: float = 0.0
    skill_version: str | None = None

    @property
    def words(self) -> int:
        return sum(word_count(n) for n in self.notes)


def write_speaker_notes(
    client: LLMClient, ir: DeckIR, *, talk_minutes: float, deck_title: str, purpose: str = "other",
    audience: str = "", language: str = "ru", brief: str = "", deadline: float | None = None,
) -> NotesResult:
    """Вызов скилла на колоду (второй — если текст вышел намного короче цели и есть время); ошибки клиента
    в первом вызове (таймаут, бюджет) — наверх, вызывающий решает, что делать."""
    skill = load_skill("speaker_notes")
    t0 = time.perf_counter()
    budget = word_budget([s.archetype for s in ir.slides], talk_minutes)
    inputs = {"deck_title": deck_title, "purpose": purpose, "audience": audience or "не указана", "language": language,
              "talk_minutes": f"{talk_minutes:g}", "total_words": sum(budget),
              "brief": (brief or "").strip()[:MAX_BRIEF_CHARS] or "(брифа нет)", "slides": slides_input(ir, talk_minutes)}
    notes, warnings = repair_notes(client.run_skill(skill, deadline=deadline, **inputs), len(ir.slides))
    words = sum(word_count(n) for n in notes)
    if words < sum(budget) * (1 - UNDER_LIMIT) and (deadline is None or deadline - time.monotonic() > SECOND_PASS_MIN_S):
        try:
            again, _ = repair_notes(client.run_skill(skill, deadline=deadline, **inputs), len(ir.slides))
        except Exception as e:  # noqa: BLE001 — первый ответ уже есть
            warnings.append(f"notes: второй проход не удался ({type(e).__name__}) — остался первый ответ")
        else:
            notes = [b if word_count(b) > word_count(a) else a for a, b in zip(notes, again)]
            warnings.append(f"notes: первый ответ короче цели ({words} из {sum(budget)} слов) — второй проход, "
                            "по каждому слайду взят более длинный текст")
    notes, cut = fit_to_budget(notes, budget)
    if cut:
        warnings.append(f"notes: текст длиннее {talk_minutes:g} мин — снято {cut} слов "
                        "(последние предложения самых длинных слайдов)")
    return NotesResult(notes, warnings, round(time.perf_counter() - t0, 3), skill.version)


def fit_to_budget(notes: list[str], budget: list[int]) -> tuple[list[str], int]:
    """Лимит выступления: если текст длиннее бюджета больше чем на `OVER_LIMIT`, снимать последние предложения
    у слайда, сильнее всех превысившего свой бюджет, пока итог не уложится в бюджет. У слайда остаётся хотя бы
    одно предложение; короткий текст не трогается — дописывать за модель нечем. → (заметки, снято слов)."""
    words = [word_count(n) for n in notes]
    total = sum(budget)
    if not total or sum(words) <= total * (1 + OVER_LIMIT):
        return notes, 0
    notes = list(notes)
    cut = 0
    while sum(words) > total:
        cands = [i for i, n in enumerate(notes) if _last_sentence_start(n) is not None]
        if not cands:
            break
        i = max(cands, key=lambda k: words[k] / max(budget[k], 1))
        start = _last_sentence_start(notes[i])
        kept = notes[i][:start].rstrip()
        cut += words[i] - word_count(kept)
        notes[i], words[i] = kept, word_count(kept)
    return notes, cut


def _last_sentence_start(text: str) -> int | None:
    """Где начинается последнее предложение (после конца предпоследнего); None — предложение одно."""
    ends = [m.end() for m in _SENTENCE_END.finditer(text.rstrip())]
    return ends[-1] if ends else None


def slides_input(ir: DeckIR, talk_minutes: float) -> str:
    """Вход {{slides}}: что на слайде и сколько слов о нём сказать."""
    budget = word_budget([s.archetype for s in ir.slides], talk_minutes)
    items = []
    for s, words in zip(ir.slides, budget):
        title, lines = s.title_and_text()
        item: dict[str, Any] = {"idx": s.idx, "archetype": s.archetype.value, "title": title,
                                "text": "\n".join(lines)[:MAX_SLIDE_TEXT],
                                "sentences": max(1, round(words / WORDS_PER_SENTENCE)), "words": words}
        if s.notes.strip():
            item["draft"] = s.notes.strip()[:MAX_SLIDE_TEXT]
        items.append(item)
    return json.dumps(items, ensure_ascii=False, indent=1)


# ──────────────────────────── repair ────────────────────────────


def repair_notes(raw: Any, n_slides: int) -> tuple[list[str], list[str]]:
    """Ответ модели → текст по слайдам 0..n-1. Не бросает: чего нет в ответе — пустая строка и предупреждение."""
    warnings: list[str] = []
    pairs = _pairs(raw, warnings)
    idxs = [i for i, _ in pairs if i is not None]
    # слайды пронумерованы с 1: «1..n» без нуля
    if idxs and 0 not in idxs and min(idxs) == 1 and max(idxs) == n_slides:
        pairs = [(i - 1 if i is not None else None, t) for i, t in pairs]
        warnings.append("notes: слайды пронумерованы с 1 — сдвинуты")
    notes = [""] * n_slides
    extra = 0
    for pos, (i, text) in enumerate(pairs):
        i = pos if i is None else i
        if not 0 <= i < n_slides:
            extra += 1
            continue
        clean = _clean(text)
        notes[i] = f"{notes[i]}\n{clean}".strip() if notes[i] else clean
    if extra:
        warnings.append(f"notes: {extra} текстов к несуществующим слайдам — отброшены")
    missing = [i + 1 for i, n in enumerate(notes) if not n]
    if missing:
        warnings.append(f"notes: нет текста у слайдов {', '.join(map(str, missing[:12]))}"
                        + ("…" if len(missing) > 12 else ""))
    return notes, warnings


def _pairs(raw: Any, warnings: list[str]) -> list[tuple[int | None, str]]:
    """(idx или None, текст) из разных форм ответа: список объектов, список строк, словарь «idx → текст»."""
    if isinstance(raw, dict):
        body = next((raw[k] for k in ("notes", "slides", "speaker_notes") if k in raw), None)
        if body is None and raw and all(_as_idx(k) is not None for k in raw):
            body = raw  # {"0": "…", "1": "…"}
    else:
        body = raw
    if isinstance(body, dict):
        return [(_as_idx(k), _text(v)) for k, v in body.items()]
    if isinstance(body, list):
        out: list[tuple[int | None, str]] = []
        for item in body:
            if isinstance(item, dict):
                idx = next((_as_idx(item[k]) for k in ("idx", "slide", "slide_idx", "index") if k in item), None)
                out.append((idx, _text(item)))
            else:
                out.append((None, _text(item)))
        return out
    warnings.append(f"notes: ответ без списка заметок ({type(raw).__name__})")
    return []


def _as_idx(v: Any) -> int | None:
    if isinstance(v, bool):
        return None
    if isinstance(v, int):
        return v
    if isinstance(v, str) and v.strip().isdigit():
        return int(v.strip())
    return None


def _text(v: Any) -> str:
    if isinstance(v, dict):
        v = next((v[k] for k in TEXT_KEYS if isinstance(v.get(k), str | list)), "")
    if isinstance(v, list):
        return "\n".join(_text(x) for x in v)
    return "" if v is None else str(v)


def _clean(text: str) -> str:
    text = _XML_BAD.sub("", text)
    text = _STAGE.sub("", text)
    text = _MARKDOWN.sub("", text)
    text = _PREFIX.sub("", text)
    lines = [re.sub(r"[ \t]+", " ", ln).strip() for ln in text.splitlines()]
    return "\n".join(ln for ln in lines if ln)


__all__ = ["NotesResult", "fit_to_budget", "repair_notes", "slides_input", "write_speaker_notes"]
