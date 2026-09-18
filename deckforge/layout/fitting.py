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
SIZE_STEPS = (0.9, 0.8, 0.7)  # сетка уменьшения кегля: кегли «между» ничего не дают, строк от них не прибавляется
MIN_HEAD_SHARE = 0.3  # голова до разделителя короче этой доли лимита — не «мысль», режем по словам
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
# символы, недопустимые в XML 1.0 (NUL, управляющие кроме \t\n\r, суррогаты, U+FFFE/FFFF): lxml на них бросает
# ValueError при записи, а модель и CSV их иногда приносят — вычищаем один раз здесь, через normalize идёт весь текст IR
_XML_BAD = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\ud800-\udfff\ufffe\uffff]")


def normalize(text: str) -> str:
    return _WS.sub(" ", _XML_BAD.sub("", text)).strip()


def xml_safe(text: str) -> str:
    """Только чистка недопустимых для XML символов, без схлопывания пробелов (заметки, ячейки таблиц)."""
    return _XML_BAD.sub("", text)


def shorten(text: str, max_chars: int | None) -> str:
    """Уложить текст в max_chars: срезать хвост по разделителям, затем по словам."""
    text = normalize(text)
    if not max_chars or len(text) <= max_chars:
        return text
    return cut_tail(text, max_chars) or _cut_words(text, max_chars)


def cut_tail(text: str, max_chars: int) -> str | None:
    """Срезать хвост по смысловому разделителю (тире, двоеточие, запятая…) так, чтобы уложиться в max_chars.
    None — ни один разделитель не подходит (голова короче MIN_HEAD_SHARE лимита или всё равно не влезает)."""
    for sep in TAIL_SEPARATORS:
        idx = text.find(sep)
        while idx > 0:
            head = text[:idx].rstrip(" ,;:—–(")
            if len(head) <= max_chars and len(head) >= max_chars * MIN_HEAD_SHARE:
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

    Ширина строки линейна по 1/size, а число строк — целое: у однострочного бокса при кегле 70 %
    остаётся одна строка (1,4 строки не бывает), поэтому подбираем масштаб по сетке SIZE_STEPS,
    а не по формуле sqrt(cap / len). Ниже MIN_SIZE_SCALE не опускаемся — дальше текст надо резать.
    """
    cap = slot_capacity(slot)
    if not cap or not slot.size_pt or len(text) <= cap:
        return None
    for scale in SIZE_STEPS:
        if scale < min_scale:
            break
        if len(text) <= chars_at_scale(slot, scale):
            return round(slot.size_pt * scale, 1)
    return round(slot.size_pt * min_scale, 1)


def chars_at_scale(slot: Slot, scale: float = MIN_SIZE_SCALE) -> int | None:
    """Сколько символов вместит слот при уменьшении кегля до scale: символов в строке — больше в 1/scale раз,
    строк — целое число (высота бокса / высота строки), заголовку — плюс разрешённые сверх бокса строки."""
    cap = slot_capacity(slot)
    if not cap:
        return None
    lines = max(1, slot.max_lines or 1)
    extra = (cap - (slot.max_chars or cap)) / max(1, slot.max_chars or 1)  # строки сверх бокса (TITLE_LINES)
    cpl = (slot.max_chars or cap) / lines
    return int(cpl / scale * (int(lines / scale) + extra * lines))


def slot_capacity(slot: Slot) -> int | None:
    """Вместимость при базовом кегле; заголовку разрешаем TITLE_LINES строк, если под ним нет декора (hard_lines)."""
    if not slot.max_chars:
        return None
    if slot.kind == SlotKind.TITLE and not slot.hard_lines and (slot.max_lines or 1) < TITLE_LINES:
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
    if not cpl or not slot.size_pt or not num:  # пустое значение KPI («», «   ») — масштабировать нечего
        return num, unit, None
    for u in (unit, ""):
        width = sum(GLYPH_WIDTH.get(ch, DIGIT_WIDTH) for ch in num) + (UNIT_SCALE * (len(u) + 1) if u else 0)
        scale = min(1.0, cpl / width)
        if scale >= NUMBER_MIN_SCALE:
            return num, u, (None if scale >= 0.999 else round(slot.size_pt * scale, 1))
    return num, "", round(slot.size_pt * NUMBER_MIN_SCALE, 1)


def split_label_body(bullet: str) -> tuple[str, str]:
    """«Лид — пояснение» / «Лид: пояснение» → (лид, пояснение); иначе (буллет, '').
    Пункт-цитата в кавычках «…» не режется внутри кавычек: разделитель ищется после закрывающей »
    («…, — и перестали…» — Автор → цитата, автор)."""
    bullet = normalize(bullet)
    start = 0
    if bullet.startswith("«"):
        close = bullet.find("»")
        if close < 0:
            return bullet, ""
        start = close + 1
    for sep in LEAD_SEPARATORS:
        idx = bullet.find(sep, start)
        if 0 < idx <= start + 60:
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


__all__ = ["chars_at_scale", "cut_tail", "fit_number", "fit_size", "normalize", "shorten", "shorten_words", "slot_capacity", "split_label_body", "split_number_unit", "xml_safe"]
