"""Реестр шаблонов (датасет + загруженные) и сборка контент-пакета из запроса — общее для UI и API.

Имена файлов от пользователя — только очищенный basename; .pptx проверяется как zip.
"""

from __future__ import annotations

import logging
import os
import re
import threading
import uuid
import zipfile
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

import httpx

from deckforge.pipeline.config import ROOT
from deckforge.pipeline.run import ParsedTemplate, parse_template, sha1_of

DATASET_DIRS = [ROOT / "data" / "templates", ROOT / "data" / "holdout"]
MAX_TEMPLATE_BYTES = 60 << 20
MAX_PACK_FILE_BYTES = 20 << 20
# текст — в корень, данные — в data/ (см. content/content_pack.py)
PACK_EXT = {".md": "", ".txt": "", ".docx": "", ".pdf": "", ".json": "data", ".csv": "data", ".xlsx": "data"}
_SAFE = re.compile(r"[^\w\-. ]+", re.UNICODE)
LFS_POINTER_MAX = 1000  # байт: указатель git-lfs вместо .pptx
LFS_TIMEOUT_S = 60
log = logging.getLogger(__name__)


class BadUpload(ValueError):
    """Загруженный файл не подходит — в HTTP это 400."""


def safe_name(name: str, default: str = "file") -> str:
    """Basename без путей и опасных символов; пустое → default."""
    base = Path(name.replace("\\", "/")).name
    base = _SAFE.sub("_", base).strip(" ._")
    return base or default


def check_pptx(path: Path) -> None:
    if path.stat().st_size > MAX_TEMPLATE_BYTES:
        raise BadUpload(f"файл больше {MAX_TEMPLATE_BYTES >> 20} МБ")
    try:
        with zipfile.ZipFile(path) as z:
            if "ppt/presentation.xml" not in z.namelist():
                raise BadUpload("это не .pptx: нет ppt/presentation.xml")
    except zipfile.BadZipFile as e:
        raise BadUpload("это не .pptx: файл не открывается как zip") from e


@dataclass
class TemplateEntry:
    id: str
    name: str
    path: Path
    builtin: bool
    parsed: ParsedTemplate | None = None


def _download(url: str) -> bytes:
    r = httpx.get(url, timeout=LFS_TIMEOUT_S, follow_redirects=True)
    r.raise_for_status()
    return r.content


def fetch_lfs(path: Path, base_url: str) -> bool:
    """Указатель git-lfs вместо шаблона датасета → скачать файл из `<base_url>/<путь от корня репо>`.

    Хостинг, который клонирует репо без LFS (Streamlit Community Cloud), иначе остался бы без шаблонов датасета.
    `DECKFORGE_LFS_BASE` — например `https://media.githubusercontent.com/media/<owner>/<repo>/<ref>`.
    Ошибка сети или не-.pptx в ответе — предупреждение в лог, шаблон пропускается, как раньше."""
    url = f"{base_url.rstrip('/')}/{quote(path.relative_to(ROOT).as_posix())}"
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.part")
    try:
        tmp.write_bytes(_download(url))
        check_pptx(tmp)
        tmp.replace(path)
        return True
    except (httpx.HTTPError, OSError, BadUpload) as e:
        log.warning("шаблон %s не скачан из LFS: %s", path.name, e)
        return False
    finally:
        tmp.unlink(missing_ok=True)


class TemplateStore:
    """Шаблоны датасета + загруженные (`root/templates/<sha1>/<name>__<sha1>.pptx`).

    sha1 в имени — чтобы кэш разметки по stem не подхватил чужой шаблон с тем же именем."""

    def __init__(self, root: Path) -> None:
        self.root = root / "templates"
        self.root.mkdir(parents=True, exist_ok=True)
        self._items: dict[str, TemplateEntry] = {}
        self._lock = threading.Lock()
        lfs_base = os.environ.get("DECKFORGE_LFS_BASE", "").strip()
        for d in DATASET_DIRS:
            if d.is_dir():
                for p in sorted(d.glob("*.pptx")):
                    if p.stat().st_size <= LFS_POINTER_MAX and lfs_base:
                        fetch_lfs(p, lfs_base)
                    if p.stat().st_size > LFS_POINTER_MAX:  # LFS-указатель пропускаем
                        sid = sha1_of(p)
                        self._items[sid] = TemplateEntry(sid, p.stem, p, True)
        for p in sorted(self.root.glob("*/*.pptx")):
            sid = p.parent.name
            self._items.setdefault(sid, TemplateEntry(sid, p.stem.rsplit("__", 1)[0], p, False))

    def list(self) -> list[TemplateEntry]:
        return list(self._items.values())

    def get(self, template_id: str) -> TemplateEntry | None:
        return self._items.get(template_id)

    def add_upload(self, filename: str, data: bytes) -> TemplateEntry:
        if len(data) > MAX_TEMPLATE_BYTES:
            raise BadUpload(f"файл больше {MAX_TEMPLATE_BYTES >> 20} МБ")
        tmp = self.root / f"_upload_{uuid.uuid4().hex}.pptx"
        tmp.write_bytes(data)
        try:
            check_pptx(tmp)
            sid = sha1_of(tmp)
            if sid in self._items:
                return self._items[sid]
            stem = safe_name(Path(filename).stem, "template")
            dest_dir = self.root / sid
            dest_dir.mkdir(exist_ok=True)
            dest = dest_dir / f"{stem}__{sid}.pptx"
            tmp.replace(dest)
            entry = TemplateEntry(sid, stem, dest, False)
            with self._lock:
                self._items[sid] = entry
            return entry
        finally:
            tmp.unlink(missing_ok=True)

    def parsed(self, entry: TemplateEntry) -> ParsedTemplate:
        with self._lock:
            if entry.parsed is None:
                entry.parsed = parse_template(entry.path)
            return entry.parsed


def write_content_pack(dest: Path, brief: str, files: list[tuple[str, bytes]]) -> Path:
    """Контент-пакет из запроса: `brief.md` + файлы (`*.md|txt` → корень, `*.json|csv` → `data/`).

    Пустой бриф файлом не становится: пакет из одних загруженных файлов — штатный случай.
    """
    dest.mkdir(parents=True, exist_ok=True)
    if brief.strip():
        (dest / "brief.md").write_text(brief.strip() + "\n", "utf-8")
    for name, data in files:
        ext = Path(name).suffix.lower()
        if ext not in PACK_EXT:
            raise BadUpload(f"{name}: допустимы только {', '.join(PACK_EXT)}")
        if len(data) > MAX_PACK_FILE_BYTES:
            raise BadUpload(f"{name}: файл больше {MAX_PACK_FILE_BYTES >> 20} МБ")
        base = safe_name(name)
        if base == "brief.md":
            base = "brief_extra.md"
        sub = dest / PACK_EXT[ext] if PACK_EXT[ext] else dest
        sub.mkdir(exist_ok=True)
        (sub / base).write_bytes(data)
    return dest


__all__ = [
    "DATASET_DIRS",
    "BadUpload",
    "TemplateEntry",
    "TemplateStore",
    "check_pptx",
    "safe_name",
    "write_content_pack",
]
