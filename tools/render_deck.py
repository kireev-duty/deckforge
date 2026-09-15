"""render_deck — рендер .pptx в PNG-превью через LibreOffice (headless) + PyMuPDF.

Используется в парсинге (VLM-разметка образцов), аудите (контекстуальные проверки),
UI (превью) и как экспорт в PDF. И для визуальной проверки вёрстки Claude'ом.

Использование:
    python tools/render_deck.py <file.pptx> [--out out/render/<name>] [--dpi 72] [--slides 1,3-5] [--contact]

--contact  дополнительно собирает контактный лист (сетка миниатюр) в один PNG — удобно смотреть всю колоду сразу.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pymupdf
from PIL import Image

SOFFICE_CANDIDATES = [
    os.environ.get("SOFFICE_PATH", ""),
    r"C:\Program Files\LibreOffice\program\soffice.exe",
    r"C:\Program Files (x86)\LibreOffice\program\soffice.exe",
    "/usr/bin/soffice",
    "/usr/lib/libreoffice/program/soffice",
    "/Applications/LibreOffice.app/Contents/MacOS/soffice",
]


def find_soffice() -> str:
    for c in SOFFICE_CANDIDATES:
        if c and Path(c).exists():
            return c
    found = shutil.which("soffice") or shutil.which("libreoffice")
    if found:
        return found
    raise RuntimeError("LibreOffice не найден: задайте SOFFICE_PATH")


def pptx_to_pdf(pptx: Path, out_dir: Path, timeout: int = 180) -> Path:
    """Конвертирует .pptx → .pdf. Отдельный профиль LibreOffice, чтобы не конфликтовать с открытым GUI."""
    out_dir.mkdir(parents=True, exist_ok=True)
    profile = Path(tempfile.gettempdir()) / "deckforge_lo_profile"
    cmd = [
        find_soffice(),
        f"-env:UserInstallation={profile.as_uri()}",
        "--headless",
        "--norestore",
        "--convert-to",
        "pdf",
        "--outdir",
        str(out_dir),
        str(pptx),
    ]
    subprocess.run(cmd, check=True, timeout=timeout, capture_output=True)
    pdf = out_dir / (pptx.stem + ".pdf")
    if not pdf.exists():
        raise RuntimeError(f"LibreOffice не создал {pdf}")
    return pdf


def pdf_to_pngs(pdf: Path, out_dir: Path, dpi: int = 72, pages: set[int] | None = None) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    paths = []
    with pymupdf.open(pdf) as doc:
        for i, page in enumerate(doc, start=1):
            if pages and i not in pages:
                continue
            pix = page.get_pixmap(dpi=dpi)
            p = out_dir / f"slide_{i:02d}.png"
            pix.save(p)
            paths.append(p)
    return paths


def contact_sheet(pngs: list[Path], out: Path, cols: int = 4, thumb_w: int = 320) -> Path:
    if not pngs:
        raise ValueError("нет изображений")
    first = Image.open(pngs[0])
    ratio = first.height / first.width
    tw, th = thumb_w, int(thumb_w * ratio)
    pad = 8
    rows = (len(pngs) + cols - 1) // cols
    sheet = Image.new("RGB", (cols * (tw + pad) + pad, rows * (th + pad) + pad), "#DDDDDD")
    for i, p in enumerate(pngs):
        im = Image.open(p).convert("RGB").resize((tw, th))
        x = pad + (i % cols) * (tw + pad)
        y = pad + (i // cols) * (th + pad)
        sheet.paste(im, (x, y))
    sheet.save(out)
    return out


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


def render(pptx: Path, out_dir: Path | None = None, dpi: int = 72, pages: set[int] | None = None,
           contact: bool = False) -> list[Path]:
    out_dir = out_dir or Path("out/render") / pptx.stem
    pdf = pptx_to_pdf(pptx, out_dir)
    pngs = pdf_to_pngs(pdf, out_dir, dpi=dpi, pages=pages)
    if contact:
        contact_sheet(pngs, out_dir / "contact.png")
    return pngs


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
