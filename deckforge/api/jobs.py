"""Job'ы генерации для HTTP API: in-memory реестр, файлы в `root/jobs/<id>/`, один рабочий поток."""

from __future__ import annotations

import shutil
import threading
import time
import uuid
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from deckforge.pipeline import DeckResult, ParsedTemplate, RunResult
from deckforge.pipeline.workspace import (  # noqa: F401 — реэкспорт
    BadUpload,
    TemplateEntry,
    TemplateStore,
    check_pptx,
    safe_name,
    write_content_pack,
)


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
    fix_lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

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
    """Job'ы в памяти; `executor=None` — выполнять синхронно."""

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

    def discard(self, job: Job) -> None:
        """Убрать job, который не дошёл до запуска."""
        self._jobs.pop(job.id, None)
        shutil.rmtree(job.dir, ignore_errors=True)

    def list(self) -> list[Job]:
        return sorted(self._jobs.values(), key=lambda j: j.created, reverse=True)

    def submit(self, job: Job, fn: Callable[[Job], RunResult]) -> Job:
        def _work() -> None:
            job.status, job.started_s = "running", time.perf_counter()
            try:
                job.result = fn(job)
                job.decks = {d.strategy: d for d in job.result.decks}
                job.status = "done"
            except Exception as e:  # noqa: BLE001 — ошибка отдаётся клиенту
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
