"""Оркестрация прогона: шаблон (+ контент, если он есть) → outline → колоды по стратегиям.

Контент — лестница (`content_for_outline`): контент-пакет пользователя → тема одной строкой →
только шаблон, из которого бриф выводит скилл `template_brief`.

Этапы (`parse_template`, `make_outline`, `build_deck`, `refine_deck`, `run`) — отдельные функции,
из которых CLI, UI и API собирают цикл; каждая колода получает `manifest.json` с провенансом.
Колоды одного прогона собираются параллельно (потоки) под общим дедлайном: три варианта ≤ 5 минут вместе.
"""

from __future__ import annotations

import hashlib
import json
import logging
import queue
import shutil
import time
from collections.abc import Callable, Iterable
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from deckforge.audit import audit_deck, with_contextual
from deckforge.audit import summary as audit_summary
from deckforge.audit.contextual import CHECK_IDS as CONTEXTUAL_CHECKS
from deckforge.audit.contextual import ERROR_CHECK_ID, judge_deck, slides_from_ir
from deckforge.content import (
    ContentPack,
    drop_unsourced_numbers,
    load_content_pack,
    pack_from_brief,
    write_outline,
    write_template_brief,
)
from deckforge.content.images import illustrate
from deckforge.core.autofix import plan_fixes
from deckforge.core.ir import Archetype, AuditReport, DeckIR, DeckOutline, Exemplar, Finding, TemplateDNA
from deckforge.core.strategy import load_strategy
from deckforge.export.html import export_html
from deckforge.export.render import find_soffice, pptx_to_pdf
from deckforge.layout import LayoutResult, apply_fixes, build_deck_ir
from deckforge.llm.client import LLMClient
from deckforge.parsing.dna import build_dna
from deckforge.parsing.exemplars import load_exemplars
from deckforge.parsing.extract_tokens import TemplateTokens, extract_tokens
from deckforge.pipeline.config import RunConfig
from deckforge.pipeline.stats import compare_table, deck_stats
from deckforge.render import render_pptx

log = logging.getLogger(__name__)

Progress = Callable[[str], None]

# резервы бюджета времени прогона (с): картинки не начинаются, если до дедлайна меньше IMAGES_RESERVE_S,
# судья — если меньше JUDGE_RESERVE_S; EXPORT_RESERVE_S оставляется на PDF/HTML после судьи
IMAGES_RESERVE_S = 90.0
JUDGE_RESERVE_S = 30.0
EXPORT_RESERVE_S = 15.0
# как часто поток прогона разбирает очередь сообщений параллельных колод (с)
PROGRESS_POLL_S = 0.3
# проходов автофиксов: перенос строк дискретный, и одного уменьшения кегля бывает мало (ЛЦТ2026:
# заголовок 36 → 27,9 pt — всё ещё три строки в боксе на две); следующий — пока ошибок становится меньше
AUTOFIX_ROUNDS = 2

# ступени лестницы входа (OutlineStep.content_source) — для CLI/UI и run.json
CONTENT_SOURCE_NOTE = {
    "pack": "контент-пакет пользователя",
    "topic": "тема одной строкой (brief.md)",
    "template": "только шаблон — бриф выведен из него (skill template_brief)",
    "outline": "готовый outline.json",
}


def rel_path(p: Path | str | None, base: Path) -> str:
    """Путь для manifest.json / run.json: относительно папки прогона, иначе cwd, иначе как есть; POSIX-слэши."""
    if p is None:
        return ""
    p = Path(p)
    for root in (base, Path.cwd()):
        try:
            return p.resolve().relative_to(root.resolve()).as_posix()
        except (ValueError, OSError):
            continue
    return p.as_posix()


# ──────────────────────────── результаты этапов ────────────────────────────


@dataclass
class ParsedTemplate:
    """Шаблон после парсинга — всё, что нужно layout/render/audit."""

    template: Path
    exemplars: list[Exemplar]
    tokens: TemplateTokens
    dna: TemplateDNA
    style: dict[str, str]
    meta: dict[str, str]  # id, path, sha1
    seconds: float = 0.0

    @property
    def archetypes(self) -> set[Archetype]:
        return available_archetypes(self.exemplars)

    def summary(self) -> dict:
        """Компактная сводка для CLI, API и UI (только JSON-типы)."""
        from collections import Counter

        t, g = self.tokens, self.dna.grid
        by_arch = Counter(e.archetype.value for e in self.exemplars)
        return {
            "template_id": t.template_id, "path": str(self.template), "sha1": self.meta["sha1"],
            "slide_w": t.slide_w, "slide_h": t.slide_h,
            "fonts": list(t.fonts), "embedded_fonts": list(t.embedded_fonts),
            "palette": {role: t.palette(role) for role in ("background", "text", "accent", "secondary", "muted", "surface")
                        if t.palette(role)},
            "typography": [{"role": x.role, "font": x.font, "size_pt": x.size_pt, "bold": x.bold} for x in t.typography],
            "grid": {"margin_left": g.margin_left, "margin_right": g.margin_right, "margin_top": g.margin_top,
                     "margin_bottom": g.margin_bottom, "columns": len(g.columns_x), "rows": len(g.rows_y)},
            "slides": len(self.exemplars),
            "archetypes": dict(sorted(by_arch.items(), key=lambda kv: -kv[1])),
            "fixed_elements": len(self.dna.fixed_elements),
            "seconds": self.seconds,
        }


@dataclass
class OutlineStep:
    outline: DeckOutline
    path: Path
    warnings: list[str] = field(default_factory=list)
    seconds: float = 0.0
    attempts: int = 0
    skills_used: dict[str, str] = field(default_factory=dict)
    llm_calls: list[dict] = field(default_factory=list)
    content_source: str = "pack"  # pack | topic | template | outline
    brief_path: Path | None = None  # бриф, выведенный из темы или шаблона
    pack: ContentPack | None = None  # то, из чего собран outline — факты для судьи
    brief_seconds: float = 0.0  # вызов template_brief, входит в `seconds`


@dataclass
class DeckResult:
    strategy: str
    pptx: Path
    ir_json: Path
    manifest: Path
    stats: dict
    warnings: list[str] = field(default_factory=list)
    choices: list[dict] = field(default_factory=list)
    pngs: list[Path] = field(default_factory=list)
    timings_s: dict[str, float] = field(default_factory=dict)
    audit: Path | None = None  # <strategy>.audit.json (AuditReport)
    audit_summary: dict = field(default_factory=dict)
    pdf: Path | None = None
    html: Path | None = None
    audience_hint: str = ""  # кому нужен этот вариант (Strategy.audience_hint)

    @property
    def exports(self) -> dict[str, str]:
        """Экспорты для manifest/run.json, пути относительно папки колоды."""
        base = self.pptx.parent
        out = {"pptx": rel_path(self.pptx, base)}
        if self.pdf is not None:
            out["pdf"] = rel_path(self.pdf, base)
        if self.html is not None:
            out["html"] = rel_path(self.html, base)
        return out

    def load_report(self) -> AuditReport | None:
        if self.audit is None or not self.audit.exists():
            return None
        return AuditReport.model_validate_json(self.audit.read_text("utf-8"))

    def load_ir(self) -> DeckIR:
        return DeckIR.model_validate_json(self.ir_json.read_text("utf-8"))

    def load_manifest(self) -> dict:
        return json.loads(self.manifest.read_text("utf-8"))


@dataclass
class RunContext:
    """Общее для всех колод прогона: конфиг, шаблон, outline, клиент LLM, факты для судьи, дедлайн."""

    cfg: RunConfig
    parsed: ParsedTemplate
    outline: OutlineStep
    client: LLMClient | None = None
    pack: Any = None  # контент-пакет — факты для судьи
    contextual_on: bool = False
    warnings: list[str] = field(default_factory=list)
    t_start: float = 0.0  # time.monotonic() старта прогона — от него бюджет `cfg.time_budget_s`

    @property
    def deadline(self) -> float:
        """Общий дедлайн всех колод прогона (`time.monotonic()`)."""
        return self.t_start + self.cfg.time_budget_s

    def deck_client(self) -> LLMClient | None:
        """Клиент колоды: те же соединения и лимит, свой журнал вызовов (manifest колоды — только её вызовы)."""
        return self.client.fork() if self.client is not None else None

    @classmethod
    def prepare(cls, cfg: RunConfig, parsed: ParsedTemplate, outline: OutlineStep,
                client: LLMClient | None = None, t_start: float | None = None) -> RunContext:
        """Будет ли VLM-судья (нужны LibreOffice и клиент); факты из контент-пакета; часы прогона.

        Без `t_start` прогон считается начатым с разбора шаблона: `build_deck` по отдельности
        видит тот же остаток бюджета, что и внутри `run()`."""
        if t_start is None:
            t_start = time.monotonic() - parsed.seconds - outline.seconds
        ctx = cls(cfg, parsed, outline, client, t_start=t_start)
        ctx.contextual_on = cfg.audit.contextual and cfg.audit.deterministic
        if ctx.contextual_on and not soffice_available():
            ctx.warnings.append("audit.contextual: LibreOffice не найден — контекстуальный аудит пропущен")
            ctx.contextual_on = False
        if ctx.contextual_on and ctx.client is None:
            ctx.client = LLMClient()  # outline готовый, но судье нужна VLM
        if ctx.client is None and cfg.images != "off":
            # клиент картинок — здесь, а не лениво в колоде: колоды собираются параллельно
            try:
                ctx.client = LLMClient()
            except Exception as e:  # noqa: BLE001 — без .env колода собирается без картинок
                ctx.warnings.append(f"images: клиент LLM недоступен ({str(e)[:80]}) — без иллюстраций")
        if ctx.contextual_on:
            # факты судье: то, из чего собран outline; с готовым outline — пакет с диска, если он задан
            ctx.pack = outline.pack
            if ctx.pack is None and outline.brief_path is not None and outline.brief_path.is_file():
                # готовый outline с перенесённым брифом: судья сверяет колоду с ним
                ctx.pack = pack_from_brief(outline.brief_path.read_text("utf-8"))
            if ctx.pack is None and cfg.content_pack is not None:
                try:
                    ctx.pack = load_content_pack(cfg.content_pack)
                except FileNotFoundError:
                    ctx.warnings.append("audit.contextual: контент-пакет не найден — судья проверяет только "
                                        "самосогласованность колоды (C04)")
            elif ctx.pack is None:
                ctx.warnings.append("audit.contextual: исходных материалов нет — судья проверяет только "
                                    "самосогласованность колоды (C04)")
            elif outline.content_source == "template":
                ctx.warnings.append("audit.contextual: исходники — бриф, выведенный из шаблона; "
                                    "C04 проверяет самосогласованность колоды")
        return ctx


@dataclass
class RunResult:
    output_dir: Path
    outline: DeckOutline
    outline_path: Path
    decks: list[DeckResult]
    timings_s: dict[str, float]
    warnings: list[str] = field(default_factory=list)
    run_json: Path | None = None
    parsed: ParsedTemplate | None = None
    content_source: str = "pack"  # pack | topic | template | outline
    brief_path: Path | None = None  # бриф по теме или выведенный из шаблона

    @property
    def total_s(self) -> float:
        return self.timings_s.get("total", 0.0)


# ──────────────────────────── этапы ────────────────────────────


def parse_template(template: Path, out_dir: Path | None = None) -> ParsedTemplate:
    """Токены + образцы + сетка → TemplateDNA; с `out_dir` рядом пишется `dna.json`."""
    t0 = time.perf_counter()
    template = Path(template)
    exemplars = load_exemplars(template)
    tokens = extract_tokens(template)
    dna = build_dna(template, exemplars, tokens)
    if out_dir is not None:
        out_dir.mkdir(parents=True, exist_ok=True)
        # в файле путь относительный, в памяти — абсолютный
        (out_dir / "dna.json").write_text(
            dna.model_copy(update={"source_path": rel_path(template, out_dir)}).model_dump_json(indent=1), "utf-8")
    meta = {"id": tokens.template_id, "path": str(template), "sha1": sha1_of(template)}
    return ParsedTemplate(template, exemplars, tokens, dna, _style(tokens), meta, round(time.perf_counter() - t0, 3))


@dataclass
class ContentStep:
    """Из чего собирается outline: пакет пользователя, тема строкой или бриф, выведенный из шаблона."""

    pack: ContentPack
    source: str  # pack | topic | template
    purpose: str
    audience: str
    brief_path: Path | None = None
    warnings: list[str] = field(default_factory=list)
    skills_used: dict[str, str] = field(default_factory=dict)


def content_for_outline(cfg: RunConfig, parsed: ParsedTemplate, out_dir: Path, client: LLMClient) -> ContentStep:
    """Лестница входа: контент-пакет → тема одной строкой → только шаблон (бриф пишет `template_brief`).

    Пустой или отсутствующий пакет — не ошибка, а спуск на ступень ниже с предупреждением.
    """
    warnings: list[str] = []
    if cfg.content_pack is not None:
        try:
            pack = load_content_pack(cfg.content_pack)
        except FileNotFoundError:
            warnings.append(f"контент-пакет не найден ({cfg.content_pack}) — контент по теме или по шаблону")
        else:
            if pack.fragments:
                return ContentStep(pack, "pack", cfg.purpose, cfg.audience)
            warnings.append(f"контент-пакет пуст ({cfg.content_pack}) — контент по теме или по шаблону")

    if cfg.topic.strip():
        text = cfg.topic.strip()
        path = out_dir / "brief.md"
        path.write_text(text + "\n", "utf-8")
        warnings.append(f"контент-пакета нет — outline по теме: «{text[:80]}»")
        return ContentStep(pack_from_brief(text), "topic", cfg.purpose, cfg.audience, path, warnings)

    # на входе только шаблон: бренд, тексты образцов и стиль → бриф
    brief = write_template_brief(
        client, parsed.dna, name=Path(cfg.template).name, language=cfg.language, target_slides=cfg.target_slides,
        purpose="" if cfg.purpose == "other" else cfg.purpose, audience=cfg.audience,
    )
    path = out_dir / "brief.md"
    path.write_text(brief.to_brief_text(), "utf-8")
    warnings.append(f"на входе только шаблон — бриф выведен из него: «{brief.topic}»; "
                    "конкретных цифр и имён в колоде не будет")
    return ContentStep(pack_from_brief(brief.to_brief_text()), "template", brief.purpose or cfg.purpose,
                    brief.audience or cfg.audience, path, warnings,
                    {"template_brief": _skill_version(client, "template_brief")})


def make_outline(cfg: RunConfig, parsed: ParsedTemplate, out_dir: Path, client: LLMClient | None = None,
                 outline: DeckOutline | None = None, outline_path: Path | None = None) -> OutlineStep:
    """Один outline на прогон; с готовым `outline` LLM не вызывается.

    `outline_path` — откуда взят готовый outline: лежащий рядом `brief.md` переносится в прогон,
    чтобы в манифесте остался след, из чего собран контент (один контент на несколько шаблонов).
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "outline.json"
    if outline is not None:
        path.write_text(outline.model_dump_json(indent=1), "utf-8")
        return OutlineStep(outline, path, content_source="outline", brief_path=_copy_brief(outline_path, out_dir))
    t0 = time.perf_counter()
    client = client or LLMClient()
    content = content_for_outline(cfg, parsed, out_dir, client)
    t_brief = round(time.perf_counter() - t0, 3) if content.source == "template" else 0.0
    res = write_outline(
        client, content.pack, purpose=content.purpose, audience=content.audience, language=cfg.language,
        target_slides=cfg.target_slides, available_archetypes=parsed.archetypes,
    )
    outline_out, num_warnings = res.outline, []
    if content.source in ("template", "topic"):
        # источника у чисел нет: KPI «100 %» и «24/7» модель дорисовывает сама
        outline_out, num_warnings = drop_unsourced_numbers(res.outline, content.pack.brief)
    (out_dir / "outline.raw.json").write_text(json.dumps(res.raw, ensure_ascii=False, indent=1), "utf-8")
    path.write_text(outline_out.model_dump_json(indent=1), "utf-8")
    return OutlineStep(outline_out, path, content.warnings + list(res.warnings) + num_warnings,
                       round(time.perf_counter() - t0, 3),
                       res.attempts,
                       {**content.skills_used, "outline_writer": _skill_version(client, "outline_writer")},
                       [asdict(c) for c in client.calls], content.source, content.brief_path, content.pack, t_brief)


def _copy_brief(outline_path: Path | None, out_dir: Path) -> Path | None:
    """`brief.md` рядом с готовым outline → в папку прогона (провенанс общего контента)."""
    if outline_path is None:
        return None
    src = Path(outline_path).with_name("brief.md")
    if not src.is_file():
        return None
    dest = out_dir / "brief.md"
    if src.resolve() != dest.resolve():
        shutil.copyfile(src, dest)
    return dest


def build_deck(ctx: RunContext, strategy_name: str, progress: Progress | None = None) -> DeckResult:
    """Одна стратегия: layout → render → аудит → safe-автофиксы → IR → PNG → VLM-судья → PDF → manifest.

    Потокобезопасна относительно соседних колод прогона: общий только дедлайн `ctx.deadline`,
    файлы колоды названы по стратегии, журнал LLM — свой (`ctx.deck_client`)."""
    say = progress or (lambda msg: log.info(msg))
    cfg, parsed, outline = ctx.cfg, ctx.parsed, ctx.outline.outline
    out_dir = cfg.output_dir
    # диапазон объёма стратегии — под заданный пользователем объём (ТЗ: 10–15 или заданный)
    strategy = load_strategy(strategy_name).for_target(cfg.target_slides)
    deck_timings: dict[str, float] = {}
    deck_warnings: list[str] = []
    skills_used = dict(ctx.outline.skills_used)
    images_info: dict = {}
    client = ctx.deck_client()
    # дедлайн один на прогон: все колоды (параллельно) должны уложиться в бюджет от старта прогона
    t_deck0 = time.monotonic()
    deadline = ctx.deadline
    if cfg.images != "off" and deadline - time.monotonic() < IMAGES_RESERVE_S:
        deck_warnings.append("images: пропущено — бюджет времени прогона почти исчерпан после outline")
    elif cfg.images != "off":
        # иллюстрации до вёрстки: picker учитывает наличие картинки
        ill = illustrate(outline, strategy, parsed.style, client, out_dir, cfg_mode=cfg.images,
                         parallel=cfg.max_parallel_llm, deadline=deadline - IMAGES_RESERVE_S)
        outline, images_info = ill.outline, ill.summary()
        for item in images_info.get("items", []):
            item["path"] = rel_path(item["path"], out_dir) if item.get("path") else item.get("path")
        deck_warnings.extend(ill.warnings)
        if ill.seconds:
            deck_timings["images"] = ill.seconds
        if ill.skill_version:
            skills_used["image_prompter"] = ill.skill_version
    t0 = time.perf_counter()
    res: LayoutResult = build_deck_ir(outline, strategy, parsed.exemplars, parsed.tokens.template_id,
                                      parsed.tokens.slide_w, parsed.tokens.slide_h, parsed.style)
    t_layout = time.perf_counter() - t0
    pptx_out = out_dir / f"{strategy.name}.pptx"
    t0 = time.perf_counter()
    render_pptx(res.ir, parsed.template, parsed.exemplars, pptx_out)
    t_render = time.perf_counter() - t0

    deck_timings.update({"layout": round(t_layout, 3), "render": round(t_render, 3)})
    deck_warnings.extend(res.warnings)
    audit_path: Path | None = None
    audit_info: dict = {}
    report: AuditReport | None = None
    if cfg.audit.deterministic:
        t0 = time.perf_counter()
        report = audit_deck(pptx_out, parsed.dna, res.ir)
        deck_timings["audit"] = round(time.perf_counter() - t0, 3)
        if cfg.audit.autofix:
            t0 = time.perf_counter()
            res.ir, report, fix_info = _autofix(res.ir, report, parsed, pptx_out, plan_fixes(report, "safe"))
            deck_timings["autofix"] = round(time.perf_counter() - t0, 3)
            audit_info["autofix"] = fix_info
    ir_path = out_dir / f"{strategy.name}.ir.json"
    ir_path.write_text(res.ir.model_dump_json(indent=1), "utf-8")
    st = deck_stats(res)

    pngs: list[Path] = []
    if cfg.render_png or ctx.contextual_on:
        t0 = time.perf_counter()
        pngs, err = _render_pngs(pptx_out, out_dir / strategy.name, cfg.render_dpi, cfg.render_png)
        if err:
            deck_warnings.append(err)
        deck_timings["png"] = round(time.perf_counter() - t0, 3)
    if ctx.contextual_on and not pngs:
        deck_warnings.append("audit.contextual: VLM-судья пропущен — нет PNG")
    if ctx.contextual_on and pngs and report is not None and deadline - time.monotonic() < JUDGE_RESERVE_S:
        deck_warnings.append("audit.contextual: VLM-судья пропущен — бюджет времени прогона исчерпан")
    elif ctx.contextual_on and pngs and report is not None:
        t0 = time.perf_counter()
        slides = slides_from_ir(res.ir, outline, ctx.pack)
        found = judge_deck(pngs, slides, client, language=outline.language, workers=cfg.max_parallel_llm,
                           deadline=deadline - EXPORT_RESERVE_S)
        deck_timings["audit_contextual"] = round(time.perf_counter() - t0, 3)
        report = with_contextual(report, found, CONTEXTUAL_CHECKS, deck_timings["audit_contextual"])
        skills_used["audit_judge"] = _skill_version(client, "audit_judge")
        n_skipped = sum(1 for f in found if f.check_id == ERROR_CHECK_ID and "бюджет" in f.message)
        if n_skipped:
            deck_warnings.append(f"audit.contextual: {n_skipped} слайдов не проверены судьёй — бюджет времени прогона")
    if report is not None:
        audit_path = out_dir / f"{strategy.name}.audit.json"
        audit_path.write_text(_portable_report(report, out_dir), "utf-8")
        audit_info = {**audit_summary(report), "path": rel_path(audit_path, out_dir),
                      "kind": "deterministic+contextual" if "audit_contextual" in deck_timings else "deterministic",
                      **audit_info}

    pdf_path: Path | None = None
    if "pdf" in cfg.export:
        t0 = time.perf_counter()
        pdf_path, err = _export_pdf(pptx_out, out_dir / strategy.name if pngs else None)
        if err:
            deck_warnings.append(err)
        deck_timings["export_pdf"] = round(time.perf_counter() - t0, 3)
    html_path: Path | None = None
    if "html" in cfg.export:
        t0 = time.perf_counter()
        html_path, err = _export_html(pptx_out, res.ir, outline.title)
        if err:
            deck_warnings.append(err)
        deck_timings["export_html"] = round(time.perf_counter() - t0, 3)

    # deck_build — работа самой колоды (картинки … HTML); deck_total — от старта прогона (разбор, бриф, outline
    # и параллельные соседи входят) до готовности колоды: через сколько пользователь её получил
    now = time.monotonic()
    deck_timings["deck_build"] = round(now - t_deck0, 3)
    deck_timings["deck_total"] = round(now - ctx.t_start, 3)
    if deck_timings["deck_total"] > cfg.time_budget_s:
        deck_warnings.append(f"бюджет времени прогона превышен: колода готова через {deck_timings['deck_total']:.0f} с "
                             f"> {cfg.time_budget_s} с")

    # журнал вызовов: outline (общий для колод прогона) + вызовы этой колоды
    llm_calls = list(ctx.outline.llm_calls) + ([asdict(c) for c in client.calls] if client is not None else [])
    choices = [c.__dict__ for c in res.choices]
    deck = DeckResult(strategy.name, pptx_out, ir_path, out_dir / f"{strategy.name}.manifest.json", st, deck_warnings,
                      choices, pngs, deck_timings, audit_path, audit_info, pdf_path, html_path, strategy.audience_hint)
    manifest = {
        "created": datetime.now(UTC).isoformat(timespec="seconds"),
        "template": {**parsed.meta, "path": rel_path(parsed.template, out_dir)},
        "content_source": ctx.outline.content_source,
        "content_pack": rel_path(cfg.content_pack, out_dir) if cfg.content_pack else "",
        "brief": rel_path(ctx.outline.brief_path, out_dir) if ctx.outline.brief_path else "",
        "outline": rel_path(ctx.outline.path, out_dir),
        "outline_title": outline.title,
        "strategy": {"name": strategy.name, "version": strategy.version,
                     "target_slides": strategy.target_slides.model_dump(), "audience_hint": strategy.audience_hint},
        "skills": skills_used,
        "models": _models(client),
        "llm_calls": llm_calls,
        "plan": [{"idx": s.idx, "archetype": s.archetype.value, "title": s.title} for s in res.plan.slides],
        "choices": choices,
        "warnings": [f"outline: {w}" for w in ctx.outline.warnings] + deck_warnings,
        "stats": st,
        "audit": audit_info,
        "images": images_info,
        "exports": deck.exports,
        "timings_s": {"parse": parsed.seconds, **({"brief": ctx.outline.brief_seconds} if ctx.outline.brief_seconds
                                                  else {}), "outline": ctx.outline.seconds, **deck_timings},
        "time_budget_s": cfg.time_budget_s,
    }
    deck.manifest.write_text(json.dumps(manifest, ensure_ascii=False, indent=1), "utf-8")

    fix_note = ""
    if audit_info.get("autofix"):
        fi = audit_info["autofix"]
        fix_note = f", autofix {fi['applied']}: {fi['before']['errors']}→{fi['after']['errors']} err"
    say(f"{strategy.id}: {st['slides']} слайдов → {pptx_out.name} (layout {t_layout:.2f}s, render {t_render:.2f}s"
        + (f", audit {deck_timings['audit']:.1f}s: {audit_info['errors']} err / {audit_info['warnings']} warn"
           if audit_info else "") + fix_note
        + (f", judge {deck_timings['audit_contextual']:.1f}s: {audit_info.get('contextual', 0)} находок"
           if "audit_contextual" in deck_timings else "")
        + (f", png {deck_timings['png']:.1f}s" if pngs else "")
        + (f", pdf {deck_timings['export_pdf']:.1f}s" if pdf_path else "")
        + (f", html {deck_timings['export_html']:.1f}s" if html_path else "")
        + f"; колода {deck_timings['deck_build']:.0f}s, готова через {deck_timings['deck_total']:.0f}s "
          f"из {cfg.time_budget_s} бюджета прогона)")
    return deck


def refine_deck(deck: DeckResult, parsed: ParsedTemplate, selected: Iterable[int], render_png: bool | None = None,
                dpi: int = 72, progress: Progress | None = None) -> DeckResult:
    """Фиксы по выбору пользователя к уже собранной колоде: правка IR → рендер → детерминированный аудит.

    Судья не перезапускается — его находки переносятся с пометкой `contextual_stale`."""
    say = progress or (lambda msg: log.info(msg))
    report = deck.load_report()
    if report is None:
        raise ValueError("у колоды нет аудита — нечего чинить")
    t_start = time.perf_counter()
    manifest = deck.load_manifest()
    ir = deck.load_ir()
    chosen = plan_fixes(report, list(selected))
    base = report.model_copy(update={"findings": [f for f in report.findings if f.kind == "deterministic"],
                                     "checks_run": [c for c in report.checks_run if c not in CONTEXTUAL_CHECKS]})
    contextual = [f for f in report.findings if f.kind == "contextual"]
    ir, new_report, info = _autofix(ir, base, parsed, deck.pptx, chosen)
    changed = info["applied"] > 0
    if contextual:
        new_report = with_contextual(new_report, [f for f in contextual if f.slide_idx < len(ir.slides)],
                                     CONTEXTUAL_CHECKS, 0.0)
    timings = dict(deck.timings_s)
    pngs = list(deck.pngs)
    pdf = deck.pdf
    html = deck.html
    if changed:
        deck.ir_json.write_text(ir.model_dump_json(indent=1), "utf-8")
        want_png = render_png if render_png is not None else bool(deck.pngs)
        if want_png:
            t0 = time.perf_counter()
            pngs, err = _render_pngs(deck.pptx, deck.pptx.parent / deck.strategy, dpi, contact=True)
            if err:
                deck.warnings.append(err)
            timings["png"] = round(time.perf_counter() - t0, 3)
        if pdf is not None:
            t0 = time.perf_counter()
            pdf, err = _export_pdf(deck.pptx, deck.pptx.parent / deck.strategy if pngs else None)
            if err:
                deck.warnings.append(err)
            timings["export_pdf"] = round(time.perf_counter() - t0, 3)
        if html is not None:
            t0 = time.perf_counter()
            html, err = _export_html(deck.pptx, ir, manifest.get("outline_title"))
            if err:
                deck.warnings.append(err)
            timings["export_html"] = round(time.perf_counter() - t0, 3)
    deck.audit.write_text(_portable_report(new_report, deck.pptx.parent), "utf-8")  # type: ignore[union-attr]

    prev = manifest.get("audit", {}).get("autofix") or {"applied": 0, "skipped": 0, "items": [],
                                                        "before": info["before"], "after": info["before"]}
    fix_info = {
        "applied": prev["applied"] + info["applied"], "skipped": prev["skipped"] + info["skipped"],
        "user_applied": prev.get("user_applied", 0) + info["applied"],
        "before": prev["before"], "after": info["after"], "items": prev["items"] + info["items"],
    }
    audit_info = {**audit_summary(new_report), "path": rel_path(deck.audit, deck.pptx.parent),
                  "kind": manifest.get("audit", {}).get("kind", "deterministic"), "autofix": fix_info}
    if contextual and changed:
        audit_info["contextual_stale"] = True
    timings["refine"] = round(timings.get("refine", 0.0) + time.perf_counter() - t_start, 3)
    manifest["audit"] = audit_info
    manifest["timings_s"] = {**manifest.get("timings_s", {}), **timings}
    new = DeckResult(deck.strategy, deck.pptx, deck.ir_json, deck.manifest, deck.stats, deck.warnings, deck.choices,
                     pngs, timings, deck.audit, audit_info, pdf, html, deck.audience_hint)
    manifest["exports"] = new.exports
    manifest["warnings"] = list(dict.fromkeys([*manifest.get("warnings", []), *deck.warnings]))
    deck.manifest.write_text(json.dumps(manifest, ensure_ascii=False, indent=1), "utf-8")
    say(f"{deck.strategy}: применено {info['applied']} из {len(chosen)} выбранных фиксов, "
        f"{info['before']['errors']}→{info['after']['errors']} err, {info['before']['warnings']}→{info['after']['warnings']} warn")
    return new


def run(
    cfg: RunConfig,
    client: LLMClient | None = None,
    outline: DeckOutline | None = None,
    progress: Progress | None = None,
    outline_path: Path | None = None,
) -> RunResult:
    """Полный прогон по конфигу; с готовым `outline` шаг content пропускается.

    Колоды собираются параллельно (`cfg.max_parallel_decks` потоков) под общим дедлайном
    `cfg.time_budget_s` от старта прогона; `progress` вызывается только из потока, вызвавшего `run`."""
    say = progress or (lambda msg: log.info(msg))
    t_start = time.perf_counter()
    t_run0 = time.monotonic()
    out_dir = cfg.output_dir
    out_dir.mkdir(parents=True, exist_ok=True)

    parsed = parse_template(cfg.template, out_dir)
    tokens = parsed.tokens
    say(f"parse: {len(parsed.exemplars)} образцов, шрифт {tokens.fonts[0] if tokens.fonts else '?'}, "
        f"accent #{(tokens.palette('accent') or ['?'])[0]} ({parsed.seconds:.1f}s)")

    step = make_outline(cfg, parsed, out_dir, client, outline, outline_path)
    if outline is None:
        say(f"контент: {CONTENT_SOURCE_NOTE.get(step.content_source, step.content_source)}")
        say(f"outline: {len(step.outline.slides)} слайдов за {step.seconds:.1f}s, попыток {step.attempts}"
            + (f", предупреждений {len(step.warnings)}" if step.warnings else ""))
    else:
        say(f"outline: передан готовый ({len(step.outline.slides)} слайдов), LLM не вызывался")
    warnings = [f"outline: {w}" for w in step.warnings]
    timings: dict[str, float] = {"parse": parsed.seconds, "outline": step.seconds}

    ctx = RunContext.prepare(cfg, parsed, step, client, t_start=t_run0)
    workers = min(cfg.max_parallel_decks, len(cfg.strategies))
    left = ctx.deadline - time.monotonic()
    say(f"сборка: {', '.join(cfg.strategies)} — {_how_built(len(cfg.strategies), workers) or 'одна колода'}, "
        f"бюджет {cfg.time_budget_s} с на прогон, осталось {left:.0f} с")
    t0 = time.perf_counter()
    decks = _build_decks(ctx, cfg.strategies, say, workers)
    timings["decks"] = round(time.perf_counter() - t0, 3)
    warnings += ctx.warnings  # после колод: сюда же пишут шаги, идущие внутри них
    rows: dict[str, dict] = {}
    for d in decks:
        rows[d.strategy] = {**d.stats, "audit_errors": d.audit_summary.get("errors", "—"),
                            "audit_warnings": d.audit_summary.get("warnings", "—"),
                            "deck_total": d.timings_s.get("deck_total"), "audience_hint": d.audience_hint}
        warnings += [w if w.startswith(d.strategy) else f"{d.strategy}: {w}" for w in d.warnings]

    timings["total"] = round(time.perf_counter() - t_start, 3)
    if timings["total"] > cfg.time_budget_s:
        warnings.append(f"бюджет времени прогона превышен: {timings['total']:.0f} с > {cfg.time_budget_s} с")
    how = _how_built(len(decks), workers)
    (out_dir / "compare.md").write_text(
        compare_table(rows) + f"\n\n{'Варианты собраны ' + how if how else 'Колода собрана'} за {timings['total']:.0f} с "
        f"из {cfg.time_budget_s} с бюджета прогона (разбор, outline и колоды).\n", "utf-8")
    result = RunResult(out_dir, step.outline, step.path, decks, timings, warnings, parsed=parsed,
                       content_source=step.content_source, brief_path=step.brief_path)
    result.run_json = out_dir / "run.json"
    result.run_json.write_text(json.dumps(run_summary(cfg, result), ensure_ascii=False, indent=1), "utf-8")
    say(f"готово за {timings['total']:.1f}s → {out_dir}")
    return result


def run_summary(cfg: RunConfig, result: RunResult) -> dict:
    """Содержимое run.json — сводка прогона; пути относительно папки прогона."""
    out = result.output_dir
    config = json.loads(cfg.model_dump_json())
    for key in ("template", "content_pack", "output_dir"):
        if config.get(key):
            config[key] = rel_path(config[key], Path.cwd())
    return {
        "created": datetime.now(UTC).isoformat(timespec="seconds"),
        "config": config,
        "template": {**result.parsed.meta, "path": rel_path(result.parsed.template, out)} if result.parsed else {},
        "content_source": result.content_source,
        "brief": rel_path(result.brief_path, out) if result.brief_path else "",
        "outline": rel_path(result.outline_path, out),
        "decks": [{"strategy": d.strategy, "pptx": rel_path(d.pptx, out), "manifest": rel_path(d.manifest, out),
                   "stats": d.stats, "audit": d.audit_summary, "exports": d.exports, "timings_s": d.timings_s}
                  for d in result.decks],
        "timings_s": result.timings_s,
        "warnings": result.warnings,
        "not_implemented": [],
    }


# ──────────────────────────── вспомогательное ────────────────────────────


def _build_decks(ctx: RunContext, names: list[str], say: Progress, workers: int) -> list[DeckResult]:
    """Колоды по стратегиям; при `workers > 1` — в потоках под общим дедлайном `ctx.deadline`.

    Сообщения колод идут через очередь и передаются в `say` из вызывающего потока: Streamlit пишет в статус
    только из потока скрипта. Результат — в порядке `names`, а не готовности; ошибка колоды пробрасывается,
    когда досчитаются остальные."""
    if workers <= 1 or len(names) <= 1:
        return [build_deck(ctx, name, say) for name in names]
    inbox: queue.SimpleQueue[str] = queue.SimpleQueue()

    def drain() -> None:
        while True:
            try:
                msg = inbox.get_nowait()
            except queue.Empty:
                return
            say(msg)

    pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="deck")
    try:
        futures: dict[str, Future[DeckResult]] = {n: pool.submit(build_deck, ctx, n, inbox.put) for n in names}
        pending = set(futures.values())
        while pending:
            _, pending = wait(pending, timeout=PROGRESS_POLL_S, return_when=FIRST_COMPLETED)
            drain()
    finally:
        pool.shutdown(wait=True, cancel_futures=True)
    drain()
    return [futures[n].result() for n in names]


def _how_built(n_decks: int, workers: int) -> str:
    """«параллельно (3 потока)» / «по очереди»; для одной колоды — пусто."""
    if n_decks <= 1:
        return ""
    if workers <= 1:
        return "по очереди"
    return f"параллельно ({workers} {'потока' if workers < 5 else 'потоков'})"


def _portable_report(report: AuditReport, base: Path) -> str:
    """`<strategy>.audit.json` с путём колоды относительно папки прогона."""
    return report.model_copy(update={"deck_path": rel_path(report.deck_path, base)}).model_dump_json(indent=1)


def _autofix(ir: DeckIR, report: AuditReport, parsed: ParsedTemplate, pptx_out: Path,
             findings: list[Finding]) -> tuple[DeckIR, AuditReport, dict]:
    """Фиксы → правка IR → рендер → детерминированный аудит. Следующий проход (до AUTOFIX_ROUNDS) — к тем же
    находкам (проверка, слайд, элемент), что остались после рендера, пока ошибок становится меньше."""
    before = audit_summary(report)
    info = {"applied": 0, "skipped": 0, "before": {"errors": before["errors"], "warnings": before["warnings"]},
            "after": {"errors": before["errors"], "warnings": before["warnings"]}, "items": []}
    keys = {(f.check_id, f.slide_idx, f.element_id) for f in findings}
    errors = before["errors"]
    for _ in range(AUTOFIX_ROUNDS):
        if not findings:
            break
        fr = apply_fixes(ir, findings, parsed.dna)
        info["applied"] += len(fr.applied)
        info["skipped"] += len(fr.skipped)
        info["items"] += fr.applied + fr.skipped
        if not fr.changed:
            break
        render_pptx(fr.ir, parsed.template, parsed.exemplars, pptx_out)
        ir, report = fr.ir, audit_deck(pptx_out, parsed.dna, fr.ir)
        after = audit_summary(report)
        info["after"] = {"errors": after["errors"], "warnings": after["warnings"]}
        if not 0 < after["errors"] < errors:
            break
        errors = after["errors"]
        findings = [f for f in plan_fixes(report, "all") if (f.check_id, f.slide_idx, f.element_id) in keys]
    return ir, report, info


def _render_pngs(pptx: Path, out_dir: Path, dpi: int, contact: bool) -> tuple[list[Path], str | None]:
    """PNG по слайдам; ошибки LibreOffice не роняют прогон — возвращаются предупреждением."""
    from deckforge.export.render import render as render_png

    try:
        return render_png(pptx, out_dir, dpi=dpi, contact=contact), None
    except Exception as e:  # noqa: BLE001
        return [], f"png: {str(e)[:120]}"


def _export_pdf(pptx: Path, rendered_dir: Path | None) -> tuple[Path | None, str | None]:
    """<strategy>.pdf рядом с .pptx; если PNG уже рендерились, PDF берётся оттуда."""
    target = pptx.with_suffix(".pdf")
    ready = rendered_dir / pptx.with_suffix(".pdf").name if rendered_dir else None
    if ready is not None and ready.exists():
        shutil.copyfile(ready, target)
        return target, None
    if not soffice_available():
        return None, "export.pdf: LibreOffice не найден — PDF пропущен"
    # своя временная папка у колоды: колоды прогона экспортируются параллельно
    tmp_dir = pptx.parent / f"_pdf_{pptx.stem}"
    try:
        tmp = pptx_to_pdf(pptx, tmp_dir)
        shutil.move(str(tmp), target)
        return target, None
    except Exception as e:  # noqa: BLE001
        return None, f"export.pdf: {str(e)[:120]}"
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def _export_html(pptx: Path, ir: DeckIR | None, title: str | None) -> tuple[Path | None, str | None]:
    """<strategy>.html рядом с .pptx."""
    try:
        return export_html(pptx, pptx.with_suffix(".html"), ir=ir, title=title), None
    except Exception as e:  # noqa: BLE001
        return None, f"export.html: {str(e)[:120]}"


def soffice_available() -> bool:
    try:
        find_soffice()
        return True
    except RuntimeError:
        return False


def _style(tokens: TemplateTokens) -> dict:
    # text_color нужен нативным таблицам и диаграммам: иначе на тёмном шаблоне текст пропадёт
    return {
        "accent": (tokens.palette("accent") or ["0077FF"])[0],
        "palette": ",".join(tokens.palette("accent") + tokens.palette("secondary")),
        "font": tokens.fonts[0] if tokens.fonts else "Arial",
        "text_color": (tokens.palette("text") or ["212121"])[0],
        # шкала кеглей шаблона — схема из автофигур берёт кегли из неё (T02)
        "type_scale": ",".join(f"{v:g}" for v in sorted({t.size_pt for t in tokens.typography})),
    }


def _models(client: LLMClient | None) -> dict[str, str]:
    if client is None:
        return {}
    return {"text": client.text_model, "vision": client.vision_model,
            "image": client.image_model if client.images_enabled else "", "base_url": client.base_url}


def _skill_version(client: LLMClient, name: str) -> str:
    for c in client.calls:
        if c.skill.startswith(name + "@"):
            return c.skill.split("@", 1)[1]
    from deckforge.llm.skills import load_skill

    return load_skill(name).version


def sha1_of(path: Path) -> str:
    h = hashlib.sha1()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()[:12]


def available_archetypes(exemplars: list[Exemplar]) -> set[Archetype]:
    return {e.archetype for e in exemplars if e.archetype != Archetype.FREEFORM}


__all__ = ["DeckResult", "OutlineStep", "ParsedTemplate", "RunContext", "RunResult", "available_archetypes",
           "build_deck", "make_outline", "parse_template", "refine_deck", "run", "run_summary", "sha1_of",
           "soffice_available"]
