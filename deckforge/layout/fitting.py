"""Детерминированная подгонка текста под лимиты слотов образца (без LLM).

Порядок: сначала отбрасываем «хвост» по смысловым разделителям (тире, двоеточие, точка с запятой,
запятая) — так теряется уточнение, а не мысль; затем обрезаем по границе слова с многоточием;
если и после этого не влезает — уменьшаем кегль в пределах 70 % от кегля образца.
"""

from __future__ import annotations

import re

from deckforge.core.ir import Slot, SlotKind

# разделители в порядке «сначала самые безопасные для смысла»
TAIL_SEPARATORS = (" — ", " – ", ": ", "; ", ", ", " (")
LEAD_SEPARATORS = (" — ", " – ", ": ")
MIN_SIZE_SCALE = 0.7
NUMBER_MIN_SCALE = 0.4  # крупная цифра KPI: можно ужать сильнее, она всё равно остаётся крупной
UNIT_SCALE = 0.35  # единица измерения рядом с крупной цифрой («1,8 дня») — мелким кеглем
# ширина знаков крупной цифры в долях «средней буквы», на которую рассчитан max_chars (жирные display-гарнитуры)
GLYPH_WIDTH = {"%": 1.9, "‰": 2.2, "×": 1.4, ",": 0.6, ".": 0.6, " ": 0.5}
DIGIT_WIDTH = 1.3
TITLE_LINES = 2  # заголовок может занять две строки, даже если бокс образца рассчитан на одну
ELLIPSIS = "…"
WORD_TOLERANCE = 3  # превышение лимита слов, при котором пункт не режем (обрезка «…» хуже лишних слов)
MIN_WORDS_TO_CUT = 2  # тексты не длиннее стольких слов по словам не режем (нечего терять — только калечить)
_WS = re.compile(r"\s+")


def normalize(text: str) -> str:
    return _WS.sub(" ", text).strip()


def shorten(text: str, max_chars: int | None) -> str:
    """Уложить текст в max_chars: срезать хвост по разделителям, затем по словам."""
    text = normalize(text)
    if not max_chars or len(text) <= max_chars:
        return text
    return cut_tail(text, max_chars) or _cut_words(text, max_chars)


def cut_tail(text: str, max_chars: int) -> str | None:
    """Срезать хвост по смысловому разделителю (тире, двоеточие, запятая…) так, чтобы уложиться в max_chars.
    None — ни один разделитель не подходит (голова короче 40 % лимита или всё равно не влезает)."""
    for sep in TAIL_SEPARATORS:
        idx = text.find(sep)
        while idx > 0:
            head = text[:idx].rstrip(" ,;:—–(")
            if len(head) <= max_chars and len(head) >= max_chars * 0.4:
                return head
            idx = text.find(sep, idx + 1)
    return None


def shorten_words(text: str, max_words: int | None) -> str:
    """Уложить в max_words слов: сначала хвост по разделителям, затем обрезка по словам."""
    text = normalize(text)
    if not max_words or _n_words(text) <= max_words:
        return text
    for sep in TAIL_SEPARATORS:
        idx = text.find(sep)
        while idx > 0:
            head = text[:idx].rstrip(" ,;:—–(")
            if _n_words(head) <= max_words and _n_words(head) >= max(2, max_words // 2):
                return head
            idx = text.find(sep, idx + 1)
    words = text.split(" ")
    if len(words) <= max_words + WORD_TOLERANCE:  # чуть длиннее лимита — лучше целиком, чем «…» посреди мысли
        return text
    return " ".join(words[:max_words]).rstrip(" ,;:—–(") + ELLIPSIS


def fit_size(text: str, slot: Slot, min_scale: float = MIN_SIZE_SCALE) -> float | None:
    """Кегль, при котором text влезает в слот, если базового не хватает; None — менять не нужно.

    Вместимость слота (max_chars) линейна по 1/size² (ширина строки × число строк), поэтому
    масштаб = sqrt(max_chars / len). Ниже MIN_SIZE_SCALE не опускаемся — дальше текст надо резать.
    """
    cap = slot_capacity(slot)
    if not cap or not slot.size_pt or len(text) <= cap:
        return None
    scale = max(min_scale, (cap / len(text)) ** 0.5)
    return round(slot.size_pt * scale, 1)


def chars_at_scale(slot: Slot, scale: float = MIN_SIZE_SCALE) -> int | None:
    """Сколько символов вместит слот при уменьшении кегля до scale."""
    cap = slot_capacity(slot)
    if not cap:
        return None
    return int(cap / (scale * scale))


def slot_capacity(slot: Slot) -> int | None:
    """Вместимость при базовом кегле; заголовку разрешаем TITLE_LINES строк."""
    if not slot.max_chars:
        return None
    if slot.kind == SlotKind.TITLE and (slot.max_lines or 1) < TITLE_LINES:
        return slot.max_chars * TITLE_LINES
    return slot.max_chars


_NUMBER_UNIT = re.compile(r"^([+\-−–×]?\s*[\d\s.,]*\d\s*[%‰×]?)\s*(.*)$")


def split_number_unit(text: str) -> tuple[str, str]:
    """«1,8 дня» → ('1,8', 'дня'); «42%» → ('42%', ''); нечисловое → (text, '')."""
    m = _NUMBER_UNIT.match(normalize(text))
    if not m or not m.group(1).strip():
        return normalize(text), ""
    return m.group(1).replace(" ", ""), m.group(2).strip()


def fit_number(text: str, slot: Slot) -> tuple[str, str, float | None]:
    """Крупная цифра — одна строка: (число, единица, кегль|None). Единица идёт мелким кеглем;
    если и так не влезает — единица отбрасывается. Ширина линейна по кеглю (строка одна)."""
    num, unit = split_number_unit(text)
    cpl = (slot.max_chars or 0) / max(1, slot.max_lines or 1)
    if not cpl or not slot.size_pt:
        return num, unit, None
    for u in (unit, ""):
        width = sum(GLYPH_WIDTH.get(ch, DIGIT_WIDTH) for ch in num) + (UNIT_SCALE * (len(u) + 1) if u else 0)
        scale = min(1.0, cpl / width)
        if scale >= NUMBER_MIN_SCALE:
            return num, u, (None if scale >= 0.999 else round(slot.size_pt * scale, 1))
    return num, "", round(slot.size_pt * NUMBER_MIN_SCALE, 1)


def split_label_body(bullet: str) -> tuple[str, str]:
    """«Лид — пояснение» / «Лид: пояснение» → (лид, пояснение); иначе (буллет, '')."""
    bullet = normalize(bullet)
    for sep in LEAD_SEPARATORS:
        idx = bullet.find(sep)
        if 0 < idx <= 60:
            return bullet[:idx].strip(), bullet[idx + len(sep):].strip()
    return bullet, ""


def _n_words(text: str) -> int:
    return len([w for w in text.split(" ") if w])


def _cut_words(text: str, max_chars: int) -> str:
    # одно-два слова («Октябрь», «0,6 дня») не режем: «Октяб…» хуже переноса или лёгкого выхода за слот
    if _n_words(text) <= MIN_WORDS_TO_CUT:
        return text
    cut = text[: max_chars - len(ELLIPSIS) + 1]
    # лимит короче первого слова — оставляем хотя бы его целиком, а не «Подключе…»
    cut = cut.rsplit(" ", 1)[0] if " " in cut else text.split(" ", 1)[0]
    return cut.rstrip(" ,;:—–(") + ELLIPSIS


__all__ = ["chars_at_scale", "cut_tail", "fit_number", "fit_size", "normalize", "shorten", "shorten_words", "slot_capacity", "split_label_body", "split_number_unit"]
