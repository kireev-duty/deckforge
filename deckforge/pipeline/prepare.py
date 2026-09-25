"""Подготовка шаблона — вне бюджета генерации: PNG образцов → правила + VLM → кэш разметки.

Вводная жюри: «подготовка шаблона и контекста — без лимита времени; свой шаблон должен анализироваться».
Разбор правилами идёт при каждой загрузке и занимает секунду; здесь неоднозначные образцы уточняет VLM
(`template_tagger`) по PNG, а ответы кладутся в `out/archetypes/<stem>.json` — `parsing/exemplars.find_vlm_cache`
наложит их на профиль правил при следующем разборе. Тот же путь — у `tools/classify_layouts.py`.
"""

from __future__ import annotations

import json
import logging
import time
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from deckforge.content import context as repo_context
from deckforge.content.context import ContextDigest
from deckforge.export.render import find_soffice, render
from deckforge.llm.client import LLMClient
from deckforge.parsing import exemplars
from deckforge.parsing.exemplars import cache_payload
from deckforge.parsing.layout_classifier import SlideProfile, classify_template, markdown_table
from deckforge.pipeline.config import ROOT
from deckforge.pipeline.run import CONTEXT_DIR, prepared_template

log = logging.getLogger(__name__)
RENDER_DIR = ROOT / "out" / "render"


@dataclass
class PrepareReport:
    """Что дала подготовка: сколько образцов, что ушло в VLM и что она поменяла."""

    template: Path
    profiles: list[SlideProfile]
    thumbnails: int
    ambiguous: int
    vlm_changed: list[int]  # номера слайдов (с 1), где VLM сменила архетип правил
    vlm_calls: int
    vlm_errors: int
    seconds: float
    cache: Path | None = None
    contact: Path | None = None
    warnings: list[str] = field(default_factory=list)

    @property
    def archetypes(self) -> dict[str, int]:
        c = Counter(p.archetype.value for p in self.profiles)
        return dict(sorted(c.items(), key=lambda kv: -kv[1]))

    def markdown(self) -> str:
        return (markdown_table(self.profiles, self.template.name)
                + f"\n\nНеоднозначных (→ VLM): {self.ambiguous} из {len(self.profiles)}; VLM изменила вердикт: "
                f"{len(self.vlm_changed)} ({', '.join(map(str, self.vlm_changed)) or '—'}); {self.seconds:.0f} с")

    def summary(self) -> dict:
        """Сводка для CLI, API и UI (только JSON-типы)."""
        return {
            "template": self.template.name, "slides": len(self.profiles), "archetypes": self.archetypes,
            "thumbnails": self.thumbnails, "ambiguous": self.ambiguous, "vlm_changed": self.vlm_changed,
            "vlm_calls": self.vlm_calls, "vlm_errors": self.vlm_errors, "seconds": self.seconds,
            "cache": str(self.cache) if self.cache else "", "warnings": self.warnings,
        }


def thumbnails_for(pptx: Path, out_dir: Path, do_render: bool = True) -> dict[int, Path]:
    """PNG слайдов шаблона: готовые из `out_dir`, иначе рендер LibreOffice (+ contact.png для UI)."""
    pngs = sorted(out_dir.glob("slide_*.png"))
    if not pngs and do_render and find_soffice():
        pngs = render(pptx, out_dir, contact=True)
    return {int(p.stem.split("_")[1]) - 1: p for p in pngs}


def prepare_template(
    pptx: Path,
    client: LLMClient | None = None,
    progress: Callable[[str], None] | None = None,
    max_parallel: int = 4,
    do_render: bool = True,
    cache_dir: Path | None = None,
    render_dir: Path | None = None,
) -> PrepareReport:
    """Шаблон → PNG образцов → классификация (с `client` — VLM для неоднозначных) → кэш ответов VLM.

    .potx и шаблон без слайдов сначала приводятся к .pptx (`run.prepared_template`) — разметка и PNG
    относятся к той же копии, которую потом клонирует рендер. Кэш пишется только при вызове VLM:
    разметка одними правилами и так считается при каждой загрузке."""
    say = progress or log.info
    t0 = time.perf_counter()
    pptx, normalized = prepared_template(Path(pptx))
    warnings = [f"шаблон приведён к .pptx: {normalized}"] if normalized else []
    # каталоги — на момент вызова: кэш читает `find_vlm_cache` из того же `exemplars.ARCHETYPES_CACHE`
    cache_dir = cache_dir or exemplars.ARCHETYPES_CACHE
    out_dir = render_dir or RENDER_DIR / pptx.stem
    say("рендер слайдов шаблона в PNG…")
    thumbs = thumbnails_for(pptx, out_dir, do_render)
    if client is not None and not thumbs:
        warnings.append("нет PNG слайдов (LibreOffice не найден) — разметка только правилами")
    vlm = client if thumbs else None
    calls_before = len(client.calls) if client is not None else 0
    if vlm is not None:
        say(f"классификация: правила + VLM для неоднозначных образцов (×{max_parallel})…")
    profiles = classify_template(pptx, vlm, thumbs, max_parallel=max_parallel)
    ambiguous = sum(1 for p in profiles if p.source == "vlm" or p.ambiguous)
    changed = [p.index + 1 for p in profiles if p.source == "vlm" and p.archetype != p.rules_archetype]
    calls = client.calls[calls_before:] if client is not None else []
    cache = None
    if any(p.vlm_raw for p in profiles):
        cache_dir.mkdir(parents=True, exist_ok=True)
        cache = cache_dir / f"{pptx.stem}.json"
        cache.write_text(json.dumps(cache_payload(pptx, profiles), ensure_ascii=False, indent=1), "utf-8")
    contact = out_dir / "contact.png"
    report = PrepareReport(
        template=pptx, profiles=profiles, thumbnails=len(thumbs), ambiguous=ambiguous, vlm_changed=changed,
        vlm_calls=len(calls), vlm_errors=sum(1 for c in calls if not c.ok),
        seconds=round(time.perf_counter() - t0, 1), cache=cache, contact=contact if contact.exists() else None,
        warnings=warnings,
    )
    say(f"образцов {len(profiles)}, неоднозначных {report.ambiguous}, VLM-вызовов {report.vlm_calls} "
        f"(ошибок {report.vlm_errors}), VLM сменила архетип: {len(changed)} — {report.seconds:.0f} с")
    return report


@dataclass
class ContextReport:
    """Что дала подготовка контекста: источники, факты, вызовы, где лежит context.json."""

    digest: ContextDigest
    path: Path
    cached: bool

    def summary(self) -> dict:
        """Сводка для CLI, API и UI (только JSON-типы)."""
        d = self.digest
        return {
            "title": d.title, "sources": len(d.sources), "facts": len(d.pack.fragments), "facts_total": len(d.facts),
            "calls": d.calls, "errors": d.errors, "seconds": d.seconds, "cached": self.cached,
            "path": str(self.path), "skill": d.skill, "warnings": d.warnings,
        }

    def markdown(self, limit: int = 12) -> str:
        """Первые факты с источниками — посмотреть глазами, что увидит outline."""
        rows = [f"- `{f.id}` ({f.title}) {f.text}" for f in self.digest.pack.fragments[:limit]]
        more = len(self.digest.pack.fragments) - limit
        return "\n".join(rows + ([f"- …ещё {more}"] if more > 0 else []))


def prepare_context(
    source: Path,
    client: LLMClient,
    progress: Callable[[str], None] | None = None,
    max_parallel: int = 4,
    language: str = "ru",
    cache_dir: Path | None = None,
) -> ContextReport:
    """Папка или .zip репозитория → факты `fact:<n>` → `out/contexts/<имя>__<sha1>.json` (кэш по sha1 источников).

    Вводная жюри: подготовка контекста — без лимита времени, поэтому она здесь, а не в `run`: генерация
    получает готовый `context.json` (`RunConfig.context`). Задача (тема) в подготовку не входит."""
    say = progress or log.info
    digest, path, cached = repo_context.prepare_context(
        Path(source), client, cache_dir=cache_dir or CONTEXT_DIR, language=language, workers=max_parallel,
        progress=say)
    report = ContextReport(digest, path, cached)
    s = report.summary()
    say(f"контекст «{s['title']}»: источников {s['sources']}, фактов {s['facts']} из {s['facts_total']}, "
        f"вызовов {s['calls']} (ошибок {s['errors']}) — " + ("из кэша" if cached else f"{s['seconds']:.0f} с"))
    return report


__all__ = ["ContextReport", "PrepareReport", "prepare_context", "prepare_template", "thumbnails_for"]
