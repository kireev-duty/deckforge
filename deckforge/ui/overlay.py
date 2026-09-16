"""Подсветка находок аудита на PNG слайда: `Finding.box` (EMU) → прямоугольник в пикселях картинки."""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

from deckforge.core.ir import Finding, Severity

COLORS = {Severity.ERROR: (229, 57, 53), Severity.WARNING: (251, 140, 0), Severity.INFO: (30, 136, 229)}


def draw_findings(png: Path, findings: list[Finding], slide_w: int, slide_h: int, width: int = 3) -> Image.Image:
    """Копия PNG с рамками находок (только у которых есть box). Цвет — по severity, номер — индекс в списке."""
    im = Image.open(png).convert("RGB")
    if not findings or slide_w <= 0 or slide_h <= 0:
        return im
    draw = ImageDraw.Draw(im)
    sx, sy = im.width / slide_w, im.height / slide_h
    for n, f in enumerate(findings, start=1):
        if f.box is None:
            continue
        x0, y0 = f.box.x * sx, f.box.y * sy
        x1, y1 = (f.box.x + f.box.w) * sx, (f.box.y + f.box.h) * sy
        color = COLORS.get(f.severity, COLORS[Severity.INFO])
        draw.rectangle([x0, y0, x1, y1], outline=color, width=width)
        label = f"{n} {f.check_id.split('_', 1)[0]}"
        tw = 7 * len(label) + 6
        draw.rectangle([x0, max(0, y0 - 14), x0 + tw, max(14, y0)], fill=color)
        draw.text((x0 + 3, max(0, y0 - 13)), label, fill=(255, 255, 255))
    return im


__all__ = ["COLORS", "draw_findings"]
