"""Хранилище API: реестр шаблонов, загрузки, job'ы генерации (in-memory + файлы в `root/`).

Один рабочий поток — колоды собираются по очереди (LLM и LibreOffice всё равно не параллелятся на одной машине).
Файлы от клиента: имена не используются как пути — только очищенный basename внутри своей папки.
"""

from __future__ import annotations

import re
import threading
import time
import uuid
import zipfile
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from deckforge.pipeline import DeckResult, ParsedTemplate, RunResult, parse_template
from deckforge.pipeline.config import ROOT
from deckforge.pipeline.run import sha1_of

DATASET_DIRS = [ROOT / "data" / "templates", ROOT / "data" / "holdout"]
MAX_TEMPLATE_BYTES = 60 << 20
MAX_PACK_FILE_BYTES = 5 << 20
PACK_EXT = {".md": "", ".json": "data", ".csv": "data", ".txt": ""}
_SAFE = re.compile(r"[^\w\-. ]+", re.UNICODE)


class BadUpload(ValueError):
    """Загруженный файл не подходит (не .pptx, слишком большой, не тот тип) — в HTTP это 400."""


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


class TemplateStore:
    """Шаблоны датасета (по путям) + загруженные (`root/templates/<sha1>/<name>__<sha1>.pptx`).

    Имя загруженного файла дополняется sha1, чтобы кэш разметки образцов (`out/archetypes/<stem>.json`,
    ключ — stem) не подхватил чужой шаблон с тем же именем.
    """

    def __init__(self, root: Path) -> None:
        self.root = root / "templates"
        self.root.mkdir(parents=True, exist_ok=True)
        self._items: dict[str, TemplateEntry] = {}
        self._lock = threading.Lock()
        for d in DATASET_DIRS:
            if d.is_dir():
                for p in sorted(d.glob("*.pptx")):
                    if p.stat().st_size > 1000:  # LFS-указатель, а не файл — пропускаем
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
    """Контент-пакет из запроса: `brief.md` + дополнительные файлы (`*.md|txt` → корень, `*.json|csv` → `data/`)."""
    dest.mkdir(parents=True, exist_ok=True)
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


@dataclass
class Job:
    id: str
    dir: Path
    status: str = "queued"
    created: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat(timespec="seconds"))
    progress: list[str] = field(default_factory=list)
    error: str | None = None
    result: RunResult | None = None
    decks: dict[str, DeckResult] = field(default_factory=dict)
    template: TemplateEntry | None = None
    started_s: float = 0.0
    future: Future | None = None
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def say(self, msg: str) -> None:
        with self._lock:
            self.progress.append(msg)

    @property
    def parsed(self) -> ParsedTemplate | None:
        return self.result.parsed if self.result else None

    def wait(self, timeout: float | None = None) -> None:
        if self.future is not None:
            self.future.result(timeout=timeout)


class JobStore:
    """Job'ы в памяти; `executor=None` — выполнять синхронно (тесты)."""

    def __init__(self, root: Path, executor: ThreadPoolExecutor | None) -> None:
        self.root = root / "jobs"
        self.root.mkdir(parents=True, exist_ok=True)
        self.executor = executor
        self._jobs: dict[str, Job] = {}

    def new(self, template: TemplateEntry | None = None) -> Job:
        jid = uuid.uuid4().hex[:12]
        job = Job(jid, self.root / jid, template=template)
        job.dir.mkdir(parents=True, exist_ok=True)
        self._jobs[jid] = job
        return job

    def get(self, job_id: str) -> Job | None:
        return self._jobs.get(job_id)

    def list(self) -> list[Job]:
        return sorted(self._jobs.values(), key=lambda j: j.created, reverse=True)

    def submit(self, job: Job, fn: Callable[[Job], RunResult]) -> Job:
        def _work() -> None:
            job.status, job.started_s = "running", time.perf_counter()
            try:
                job.result = fn(job)
                job.decks = {d.strategy: d for d in job.result.decks}
                job.status = "done"
            except Exception as e:  # noqa: BLE001 — ошибка прогона отдаётся клиенту, сервер живёт
                job.error = f"{type(e).__name__}: {str(e)[:300]}"
                job.status = "error"
                job.say(f"ошибка: {job.error}")

        if self.executor is None:
            _work()
        else:
            job.future = self.executor.submit(_work)
        return job


__all__ = ["BadUpload", "Job", "JobStore", "TemplateEntry", "TemplateStore", "check_pptx", "safe_name",
           "write_content_pack"]
