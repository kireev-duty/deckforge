"""Общее для схем по DiagramSpec: кегль по месту, цвет текста на заливке, шкала кеглей шаблона.

Сама схема процесса — настоящий SmartArt «Простой процесс» (`render/smartart.py`).
"""

from __future__ import annotations

from deckforge.core.colors import contrast_ratio

CAPTION_MAX_PT, CAPTION_MIN_PT = 18.0, 10.0
CHAR_WIDTH = 0.62  # ширина знака в долях кегля: с запасом, иначе длинное слово рвётся («Масштабировани/е»)
LINE_SPACING = 1.2
EMU_PER_PT = 12700
LIGHT, DARK = "FFFFFF", "212121"


def caption_size(texts: list[str], w: int, h: int, base_pt: float | None = None) -> float:
    """Наибольший кегль из [CAPTION_MIN_PT, CAPTION_MAX_PT], при котором самая длинная подпись влезает в w×h."""
    size = min(CAPTION_MAX_PT, base_pt or CAPTION_MAX_PT)
    while size > CAPTION_MIN_PT:
        cpl = max(1, int(w / EMU_PER_PT / (CHAR_WIDTH * size)))
        lines = max(1, int(h / EMU_PER_PT / (LINE_SPACING * size)))
        if all(lines_needed(t, cpl) <= lines and max(map(len, t.split()), default=0) <= cpl for t in texts):
            return size
        size -= 1.0
    return CAPTION_MIN_PT


def lines_needed(text: str, cpl: int) -> int:
    """Строк при переносе по словам."""
    lines, cur = 1, 0
    for word in text.split():
        add = len(word) + (1 if cur else 0)
        if cur and cur + add > cpl:
            lines, cur = lines + 1, len(word)
        else:
            cur += add
    return lines


def on_fill(fill: str, dark: str = DARK) -> str:
    """Цвет текста на заливке фигуры: белый или тёмный текст палитры — что контрастнее."""
    return LIGHT if contrast_ratio(LIGHT, fill) >= contrast_ratio(dark, fill) else dark


def snap_size(size: float, scale: list[float], floor: float) -> float:
    """Ближайший не больший кегль из шкалы шаблона (не ниже floor); шкалы нет — как есть."""
    fits = [s for s in scale if floor <= s <= size]
    return max(fits) if fits else size


def type_scale(overrides: dict) -> list[float]:
    """Шкала кеглей шаблона из `style_overrides["type_scale"]` ("12,18,24")."""
    out = []
    for v in str(overrides.get("type_scale") or "").split(","):
        try:
            out.append(float(v))
        except ValueError:
            continue
    return sorted(out)


__all__ = ["caption_size", "lines_needed", "on_fill", "snap_size", "type_scale"]
