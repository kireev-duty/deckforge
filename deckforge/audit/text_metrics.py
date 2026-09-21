"""Оценка ширины/высоты текста метриками TTF через Pillow.

Встроенные шрифты шаблона Pillow не читает, поэтому гарнитура резолвится в ближайший доступный TTF,
а без TTF — средняя ширина символа. Точности ~10 % хватает для проверки «текст не влез».
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import cache
from pathlib import Path

from PIL import ImageFont

FONT_DIRS = [
    os.environ.get("DECKFORGE_FONTS_DIR", ""),
    r"C:\Windows\Fonts",
    "/usr/share/fonts",
    "/usr/local/share/fonts",
    os.path.expanduser("~/.fonts"),
    "/Library/Fonts",
]
# гарнитура → (regular, bold) файлы-кандидаты
ALIASES: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    "arial": (("arial.ttf", "Arial.ttf", "LiberationSans-Regular.ttf", "DejaVuSans.ttf"),
              ("arialbd.ttf", "Arial Bold.ttf", "LiberationSans-Bold.ttf", "DejaVuSans-Bold.ttf")),
    "play": (("Play-Regular.ttf", "arial.ttf", "LiberationSans-Regular.ttf", "DejaVuSans.ttf"),
             ("Play-Bold.ttf", "arialbd.ttf", "LiberationSans-Bold.ttf", "DejaVuSans-Bold.ttf")),
    "montserrat": (("Montserrat-Regular.ttf", "arial.ttf", "LiberationSans-Regular.ttf", "DejaVuSans.ttf"),
                   ("Montserrat-Bold.ttf", "arialbd.ttf", "LiberationSans-Bold.ttf", "DejaVuSans-Bold.ttf")),
}
DEFAULT_ALIAS = "arial"
# ширина гарнитуры относительно Arial, когда меряем по прокси
WIDTH_FACTOR = {"play": 0.9, "montserrat": 1.1}
AVG_CHAR_WIDTH = 0.5  # фолбэк без TTF, в долях кегля
LINE_HEIGHT = 1.2  # single, в долях кегля
MEASURE_PT = 100  # ширина линейна по кеглю


@cache
def _find_font_file(candidates: tuple[str, ...]) -> Path | None:
    for d in FONT_DIRS:
        if not d or not Path(d).is_dir():
            continue
        for name in candidates:
            p = Path(d) / name
            if p.exists():
                return p
        # Linux: шрифты лежат в подпапках
        for name in candidates:
            hits = list(Path(d).rglob(name))
            if hits:
                return hits[0]
    return None


def _alias(font: str) -> str:
    key = (font or "").strip().lower()
    return next((a for a in ALIASES if key.startswith(a)), DEFAULT_ALIAS)


@cache
def _font(font: str, bold: bool) -> tuple[ImageFont.FreeTypeFont | None, float]:
    """(шрифт Pillow, поправка ширины); поправка ≠ 1, если гарнитуру заменил прокси."""
    alias = _alias(font)
    files = ALIASES[alias]
    path = _find_font_file(files[1] if bold else files[0])
    if path is None:
        return None, 1.0
    proxy = alias.lower() not in path.name.lower()
    try:
        return ImageFont.truetype(str(path), MEASURE_PT), (WIDTH_FACTOR.get(alias, 1.0) if proxy else 1.0)
    except OSError:
        return None, 1.0


@dataclass(frozen=True)
class TextMeasurer:
    """Измеритель для одной гарнитуры/начертания. Все размеры — в pt."""

    font: str = "Arial"
    bold: bool = False

    def width(self, text: str, size_pt: float) -> float:
        f, k = _font(self.font, self.bold)
        if f is None or not text:
            return len(text) * AVG_CHAR_WIDTH * size_pt
        return f.getlength(text) * size_pt / MEASURE_PT * k

    def wrap_lines(self, text: str, size_pt: float, width_pt: float) -> list[str]:
        """Жадный перенос по словам в полосу width_pt; слово шире полосы рвётся по символам."""
        lines: list[str] = []
        for raw in text.split("\n"):
            words = raw.split()
            if not words:
                lines.append("")
                continue
            cur = ""
            for w in words:
                cand = f"{cur} {w}" if cur else w
                if self.width(cand, size_pt) <= width_pt or not cur and self.width(w, size_pt) <= width_pt:
                    cur = cand
                    continue
                if cur:
                    lines.append(cur)
                    cur = ""
                if self.width(w, size_pt) <= width_pt:
                    cur = w
                    continue
                for ch in w:
                    if self.width(cur + ch, size_pt) <= width_pt or not cur:
                        cur += ch
                    else:
                        lines.append(cur)
                        cur = ch
            lines.append(cur)
        return lines

    def block_height(self, paragraphs: list[str], size_pt: float, width_pt: float,
                     line_spacing: float = LINE_HEIGHT, space_before_pt: float = 0.0,
                     space_after_pt: float = 0.0) -> float:
        n_lines = sum(max(1, len(self.wrap_lines(p, size_pt, width_pt))) for p in paragraphs)
        return n_lines * size_pt * line_spacing + len(paragraphs) * (space_before_pt + space_after_pt)


def fonts_available() -> bool:
    return _font("Arial", False)[0] is not None


__all__ = ["AVG_CHAR_WIDTH", "LINE_HEIGHT", "TextMeasurer", "fonts_available"]
