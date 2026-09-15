"""Цветовая математика без зависимостей: конвертации, контраст WCAG, ΔE, модификаторы OOXML.

Все hex — 6 символов верхнего регистра без «#». Используется парсингом (роли цветов)
и аудитом (контраст текста и фона), поэтому живёт в core.
"""

from __future__ import annotations

import colorsys

RGB = tuple[int, int, int]


def normalize_hex(value: str) -> str:
    """'#0077ff' / '0077FFAA' → '0077FF'."""
    v = value.strip().lstrip("#").upper()
    return v[:6]


def hex_to_rgb(value: str) -> RGB:
    v = normalize_hex(value)
    return int(v[0:2], 16), int(v[2:4], 16), int(v[4:6], 16)


def rgb_to_hex(rgb: tuple[float, float, float]) -> str:
    return "".join(f"{max(0, min(255, round(c))):02X}" for c in rgb)


def rgb_to_hsl(rgb: RGB) -> tuple[float, float, float]:
    """→ (h, s, l) в диапазоне 0..1."""
    r, g, b = (c / 255 for c in rgb)
    h, l, s = colorsys.rgb_to_hls(r, g, b)
    return h, s, l


def hsl_to_rgb(hsl: tuple[float, float, float]) -> RGB:
    h, s, l = hsl
    r, g, b = colorsys.hls_to_rgb(h, max(0.0, min(1.0, l)), max(0.0, min(1.0, s)))
    return round(r * 255), round(g * 255), round(b * 255)


def saturation(value: str) -> float:
    """Насыщенность HSL 0..1 — критерий «хроматический / нейтральный»."""
    return rgb_to_hsl(hex_to_rgb(value))[1]


def lightness(value: str) -> float:
    return rgb_to_hsl(hex_to_rgb(value))[2]


# ──────────────────────────── контраст (WCAG 2.x) ────────────────────────────


def _srgb_channel(c: int) -> float:
    v = c / 255
    return v / 12.92 if v <= 0.04045 else ((v + 0.055) / 1.055) ** 2.4


def relative_luminance(value: str) -> float:
    r, g, b = (_srgb_channel(c) for c in hex_to_rgb(value))
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast_ratio(a: str, b: str) -> float:
    """Контраст по WCAG: 1..21."""
    la, lb = relative_luminance(a), relative_luminance(b)
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)


# ──────────────────────────── CIE Lab / ΔE ────────────────────────────


def rgb_to_lab(rgb: RGB) -> tuple[float, float, float]:
    r, g, b = (_srgb_channel(c) for c in rgb)
    # sRGB → XYZ (D65)
    x = (r * 0.4124 + g * 0.3576 + b * 0.1805) / 0.95047
    y = (r * 0.2126 + g * 0.7152 + b * 0.0722) / 1.00000
    z = (r * 0.0193 + g * 0.1192 + b * 0.9505) / 1.08883

    def f(t: float) -> float:
        return t ** (1 / 3) if t > 0.008856 else 7.787 * t + 16 / 116

    fx, fy, fz = f(x), f(y), f(z)
    return 116 * fy - 16, 500 * (fx - fy), 200 * (fy - fz)


def chroma(value: str) -> float:
    """Хрома в Lab: sqrt(a² + b²). Серые ≈ 0, светлые оттенки (EBF3F9) ≈ 5, #0077FF ≈ 90.

    В отличие от HSL-насыщенности не «взрывается» у почти белых и почти чёрных цветов.
    """
    _, a, b = rgb_to_lab(hex_to_rgb(value))
    return (a * a + b * b) ** 0.5


def delta_e(a: str, b: str) -> float:
    """ΔE CIE76 — евклидово расстояние в Lab. < ~2 неразличимо, < ~6 «тот же цвет»."""
    la, lb = rgb_to_lab(hex_to_rgb(a)), rgb_to_lab(hex_to_rgb(b))
    return sum((p - q) ** 2 for p, q in zip(la, lb)) ** 0.5


# ──────────────────────────── модификаторы OOXML ────────────────────────────


def apply_color_mods(value: str, mods: dict[str, int]) -> str:
    """Применить дочерние элементы a:srgbClr/a:schemeClr: lumMod, lumOff, tint, shade.

    Значения — как в OOXML, в тысячных долях процента (100000 = 100 %).
    alpha намеренно игнорируется (цвет остаётся тем же, меняется только прозрачность).
    """
    rgb = hex_to_rgb(value)
    if "tint" in mods:  # к белому: c' = 255 - (255 - c) * tint
        k = mods["tint"] / 100000
        rgb = tuple(255 - (255 - c) * k for c in rgb)  # type: ignore[assignment]
    if "shade" in mods:  # к чёрному
        k = mods["shade"] / 100000
        rgb = tuple(c * k for c in rgb)  # type: ignore[assignment]
    if "lumMod" in mods or "lumOff" in mods:
        h, s, l = rgb_to_hsl(tuple(round(c) for c in rgb))  # type: ignore[arg-type]
        l = l * mods.get("lumMod", 100000) / 100000 + mods.get("lumOff", 0) / 100000
        rgb = hsl_to_rgb((h, s, l))
    return rgb_to_hex(rgb)
