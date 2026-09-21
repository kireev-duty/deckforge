"""render_deck — рендер .pptx в PNG-превью через LibreOffice (headless) + PyMuPDF.

Тонкая CLI-обёртка над `deckforge.export.render` для визуальной проверки вёрстки.

Использование:
    python tools/render_deck.py <file.pptx> [--out out/render/<name>] [--dpi 72] [--slides 1,3-5] [--contact]

--contact  дополнительно собирает контактный лист (сетка миниатюр) в один PNG — удобно смотреть всю колоду сразу.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from deckforge.export.render import (  # noqa: E402, F401 — реэкспорт для старых импортов из tools
    contact_sheet,
    find_soffice,
    pdf_to_pngs,
    pptx_to_pdf,
    render,
)


def parse_pages(spec: str | None) -> set[int] | None:
    if not spec:
        return None
    pages: set[int] = set()
    for part in spec.split(","):
        if "-" in part:
            a, b = part.split("-")
            pages.update(range(int(a), int(b) + 1))
        else:
            pages.add(int(part))
    return pages


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("pptx", type=Path)
    ap.add_argument("--out", type=Path)
    ap.add_argument("--dpi", type=int, default=72)
    ap.add_argument("--slides", help="например 1,3-5 (1-based)")
    ap.add_argument("--contact", action="store_true")
    a = ap.parse_args()
    pngs = render(a.pptx, a.out, a.dpi, parse_pages(a.slides), a.contact)
    print(f"rendered {len(pngs)} slides → {pngs[0].parent}")
    for p in pngs:
        print(" ", p)
    if a.contact:
        print("  contact sheet:", pngs[0].parent / "contact.png")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
