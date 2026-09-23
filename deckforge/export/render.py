"""Рендер .pptx → .pdf → PNG через LibreOffice (headless) и PyMuPDF — один путь для VLM, аудита, превью и PDF."""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path

import pymupdf
from PIL import Image

RETRY_PAUSE_S = 2.0
# профиль LibreOffice общий, и два soffice на нём одновременно мешают друг другу (второй падает или ждёт первый):
# на публичном стенде в одном процессе крутятся несколько сессий UI — конвертации идут строго по одной
_SOFFICE_LOCK = threading.Lock()
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


def pptx_to_pdf(pptx: Path, out_dir: Path, timeout: int = 180, retries: int = 1) -> Path:
    """.pptx → .pdf в отдельном профиле LibreOffice, чтобы не конфликтовать с открытым GUI.

    Внутри процесса конвертации сериализуются (`_SOFFICE_LOCK`); один повтор — на случай soffice из другого
    процесса на том же профиле: он изредка роняет запуск.
    """
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
    for attempt in range(retries + 1):
        try:
            with _SOFFICE_LOCK:
                subprocess.run(cmd, check=True, timeout=timeout, capture_output=True)
            break
        except subprocess.CalledProcessError as e:
            if attempt < retries:
                time.sleep(RETRY_PAUSE_S)
                continue
            tail = (e.stderr or b"").decode("utf-8", "replace").strip().splitlines()
            raise RuntimeError(f"LibreOffice: код {e.returncode}" + (f", {tail[-1][:200]}" if tail else "")) from None
        except subprocess.TimeoutExpired:
            if attempt < retries:
                continue
            raise RuntimeError(f"LibreOffice: таймаут {timeout} с") from None
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


def render(pptx: Path, out_dir: Path | None = None, dpi: int = 72, pages: set[int] | None = None,
           contact: bool = False) -> list[Path]:
    """pptx → PNG по слайдам (+ contact.png). Возвращает пути PNG в порядке слайдов."""
    out_dir = out_dir or Path("out/render") / pptx.stem
    pdf = pptx_to_pdf(pptx, out_dir)
    pngs = pdf_to_pngs(pdf, out_dir, dpi=dpi, pages=pages)
    if contact:
        contact_sheet(pngs, out_dir / "contact.png")
    return pngs


__all__ = ["contact_sheet", "find_soffice", "pdf_to_pngs", "pptx_to_pdf", "render"]
