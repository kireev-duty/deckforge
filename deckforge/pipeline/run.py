"""Оркестрация прогона: шаблон + контент-пакет → outline (LLM, один на прогон) → N колод по стратегиям.

Этапы — отдельные функции, чтобы CLI, UI и API собирали цикл из одних и тех же кирпичей:

- `parse_template`  — .pptx → `ParsedTemplate` (образцы, токены, `TemplateDNA`, `dna.json`);
- `make_outline`    — бриф + контент-пакет → `OutlineStep` (один вызов LLM или готовый outline);
- `build_deck`      — одна стратегия: layout → render → аудит → safe-автофиксы (правка IR, повторный рендер и аудит)
                      → PNG → VLM-судья → PDF → `manifest.json`;
- `refine_deck`     — фиксы по выбору пользователя к уже собранной колоде (UI/API): правка IR → рендер → аудит;
- `run`             — всё вместе по `RunConfig`: outline.json, <strategy>.pptx / .pdf / .ir.json / .audit.json /
                      .manifest.json, compare.md, run.json.

manifest.json — провенанс колоды: версии скиллов и моделей, стратегия, план, выбранные образцы, что чинил autofix,
экспорты, тайминги.
"""

from __future__ import annotations

import hashlib
import json
import logging
import shutil
import time
from collections.abc import Iterable
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from deckforge.audit import audit_deck, with_contextual
from deckforge.audit import summary as audit_summary
from deckforge.audit.contextual import CHECK_IDS as CONTEXTUAL_CHECKS
from deckforge.audit.contextual import judge_deck, slides_from_ir
from deckforge.content import load_content_pack, write_outline
from deckforge.content.images import illustrate
from deckforge.core.autofix import plan_fixes
from deckforge.core.ir import Archetype, AuditReport, DeckIR, DeckOutline, Exemplar, Finding, TemplateDNA
from deckforge.core.strategy import load_strategy
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


# ──────────────────────────── результаты этапов ────────────────────────────


@dataclass
class ParsedTemplate:
    """Шаблон после парсинга — всё, что нужно layout/render/audit. Один на прогон, переиспользуется UI."""

    template: Path
    exemplars: list[Exemplar]
    tokens: TemplateTokens
    dna: TemplateDNA
    style: dict[str, str]
    meta: dict[str, str]  # id, path, sha1 — в manifest
    seconds: float = 0.0

    @property
    def archetypes(self) -> set[Archetype]:
        return available_archetypes(self.exemplars)

    def summary(self) -> dict:
        """Компактная сводка для `cli parse`, API `/templates` и шага «Шаблон» в UI (только JSON-типы)."""
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

    @property
    def exports(self) -> dict[str, str]:
        out = {"pptx": str(self.pptx)}
        if self.pdf is not None:
            out["pdf"] = str(self.pdf)
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
    """Общее для всех колод прогона: конфиг, шаблон, outline, клиент LLM, факты для судьи."""

    cfg: RunConfig
    parsed: ParsedTemplate
    outline: OutlineStep
    client: LLMClient | None = None
    pack: Any = None  # контент-пакет — факты для судьи (C04)
    contextual_on: bool = False
    warnings: list[str] = field(default_factory=list)

    def client_for_images(self) -> LLMClient | None:
        """Клиент для иллюстраций: общий клиент прогона, иначе — новый (outline мог быть передан готовым).
        Без ключа T2I `illustrate` сам напишет предупреждение и ничего не сгенерирует."""
        if self.client is None and self.cfg.images != "off":
            try:
                self.client = LLMClient()
            except Exception as e:  # noqa: BLE001 — нет .env: колода собирается без картинок
                self.warnings.append(f"images: клиент LLM недоступен ({str(e)[:80]}) — без иллюстраций")
        return self.client

    @classmethod
    def prepare(cls, cfg: RunConfig, parsed: ParsedTemplate, outline: OutlineStep,
                client: LLMClient | None = None) -> RunContext:
        """Решает, будет ли VLM-судья (нужны LibreOffice и клиент), и подгружает факты из контент-пакета."""
        ctx = cls(cfg, parsed, outline, client)
        ctx.contextual_on = cfg.audit.contextual and cfg.audit.deterministic
        if ctx.contextual_on and not soffice_available():
            ctx.warnings.append("audit.contextual: LibreOffice не найден — контекстуальный аудит пропущен")
            ctx.contextual_on = False
        if ctx.contextual_on and ctx.client is None:
            ctx.client = LLMClient()  # outline передан готовым, но судье нужна VLM
        if ctx.contextual_on:
            try:
                ctx.pack = load_content_pack(cfg.content_pack)
            except FileNotFoundError:
                ctx.warnings.append("audit.contextual: контент-пакет не найден — судья работает без фактов (C04 неточна)")
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

    @property
    def total_s(self) -> float:
        return self.timings_s.get("total", 0.0)


# ──────────────────────────── этапы ────────────────────────────


def parse_template(template: Path, out_dir: Path | None = None) -> ParsedTemplate:
    """Токены + образцы (кэш разметки в out/archetypes, если есть) + сетка/фиксированные → TemplateDNA.

    `out_dir` задан → рядом с колодами пишется `dna.json` (полная TemplateDNA прогона).
    """
    t0 = time.perf_counter()
    template = Path(template)
    exemplars = load_exemplars(template)
    tokens = extract_tokens(template)
    dna = build_dna(template, exemplars, tokens)
    if out_dir is not None:
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "dna.json").write_text(dna.model_dump_json(indent=1), "utf-8")
    meta = {"id": tokens.template_id, "path": str(template), "sha1": sha1_of(template)}
    return ParsedTemplate(template, exemplars, tokens, dna, _style(tokens), meta, round(time.perf_counter() - t0, 3))


def make_outline(cfg: RunConfig, parsed: ParsedTemplate, out_dir: Path, client: LLMClient | None = None,
                 outline: DeckOutline | None = None) -> OutlineStep:
    """Один outline на прогон. `outline` передан → LLM не вызывается (режим build_variants, тестов и UI-повтора)."""
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "outline.json"
    if outline is not None:
        path.write_text(outline.model_dump_json(indent=1), "utf-8")
        return OutlineStep(outline, path)
    t0 = time.perf_counter()
    client = client or LLMClient()
    pack = load_content_pack(cfg.content_pack)
    res = write_outline(
        client, pack, purpose=cfg.purpose, audience=cfg.audience, language=cfg.language,
        target_slides=cfg.target_slides, available_archetypes=parsed.archetypes,
    )
    (out_dir / "outline.raw.json").write_text(json.dumps(res.raw, ensure_ascii=False, indent=1), "utf-8")
    path.write_text(res.outline.model_dump_json(indent=1), "utf-8")
    return OutlineStep(res.outline, path, list(res.warnings), round(time.perf_counter() - t0, 3), res.attempts,
                       {"outline_writer": _skill_version(client, "outline_writer")}, [asdict(c) for c in client.calls])


def build_deck(ctx: RunContext, strategy_name: str, progress: Progress | None = None) -> DeckResult:
    """Одна стратегия: layout → render → аудит → safe-автофиксы → IR → PNG → VLM-судья → PDF → manifest."""
    say = progress or (lambda msg: log.info(msg))
    cfg, parsed, outline = ctx.cfg, ctx.parsed, ctx.outline.outline
    out_dir = cfg.output_dir
    strategy = load_strategy(strategy_name)
    deck_timings: dict[str, float] = {}
    deck_warnings: list[str] = []
    skills_used = dict(ctx.outline.skills_used)
    images_info: dict = {}
    if cfg.images != "off":
        # иллюстрации до вёрстки: picker учитывает наличие картинки при выборе образца
        ill = illustrate(outline, strategy, parsed.style, ctx.client_for_images(), out_dir, cfg_mode=cfg.images)
        outline, images_info = ill.outline, ill.summary()
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
    if ctx.contextual_on and pngs and report is not None:
        t0 = time.perf_counter()
        slides = slides_from_ir(res.ir, outline, ctx.pack)
        found = judge_deck(pngs, slides, ctx.client, language=outline.language)
        deck_timings["audit_contextual"] = round(time.perf_counter() - t0, 3)
        report = with_contextual(report, found, CONTEXTUAL_CHECKS, deck_timings["audit_contextual"])
        skills_used["audit_judge"] = _skill_version(ctx.client, "audit_judge")
    if report is not None:
        audit_path = out_dir / f"{strategy.name}.audit.json"
        audit_path.write_text(report.model_dump_json(indent=1), "utf-8")
        audit_info = {**audit_summary(report), "path": str(audit_path),
                      "kind": "deterministic+contextual" if "audit_contextual" in deck_timings else "deterministic",
                      **audit_info}

    pdf_path: Path | None = None
    if "pdf" in cfg.export:
        t0 = time.perf_counter()
        pdf_path, err = _export_pdf(pptx_out, out_dir / strategy.name if pngs else None)
        if err:
            deck_warnings.append(err)
        deck_timings["export_pdf"] = round(time.perf_counter() - t0, 3)

    llm_calls = [asdict(c) for c in ctx.client.calls] if ctx.client is not None else list(ctx.outline.llm_calls)
    choices = [c.__dict__ for c in res.choices]
    deck = DeckResult(strategy.name, pptx_out, ir_path, out_dir / f"{strategy.name}.manifest.json", st, deck_warnings,
                      choices, pngs, deck_timings, audit_path, audit_info, pdf_path)
    manifest = {
        "created": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "template": parsed.meta,
        "content_pack": str(cfg.content_pack),
        "outline": str(ctx.outline.path),
        "strategy": {"name": strategy.name, "version": strategy.version},
        "skills": skills_used,
        "models": _models(ctx.client),
        "llm_calls": llm_calls,
        "plan": [{"idx": s.idx, "archetype": s.archetype.value, "title": s.title} for s in res.plan.slides],
        "choices": choices,
        "warnings": [f"outline: {w}" for w in ctx.outline.warnings] + deck_warnings,
        "stats": st,
        "audit": audit_info,
        "images": images_info,
        "exports": deck.exports,
        "timings_s": {"parse": parsed.seconds, "outline": ctx.outline.seconds, **deck_timings},
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
        + (f", pdf {deck_timings['export_pdf']:.1f}s" if pdf_path else "") + ")")
    return deck


def refine_deck(deck: DeckResult, parsed: ParsedTemplate, selected: Iterable[int], render_png: bool | None = None,
                dpi: int = 72, progress: Progress | None = None) -> DeckResult:
    """Фиксы по выбору пользователя (индексы находок в `deck.audit`) к уже собранной колоде.

    Правка IR → повторный рендер поверх файла → повторный детерминированный аудит. Контекстуальные находки
    прошлого отчёта переносятся как есть (судья не перезапускается; в manifest — `audit.contextual_stale`).
    PNG перерисовываются, если были или `render_png`; PDF — если был. Возвращает обновлённый `DeckResult`.
    """
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
    deck.audit.write_text(new_report.model_dump_json(indent=1), "utf-8")  # type: ignore[union-attr]

    prev = manifest.get("audit", {}).get("autofix") or {"applied": 0, "skipped": 0, "items": [],
                                                        "before": info["before"], "after": info["before"]}
    fix_info = {
        "applied": prev["applied"] + info["applied"], "skipped": prev["skipped"] + info["skipped"],
        "user_applied": prev.get("user_applied", 0) + info["applied"],
        "before": prev["before"], "after": info["after"], "items": prev["items"] + info["items"],
    }
    audit_info = {**audit_summary(new_report), "path": str(deck.audit),
                  "kind": manifest.get("audit", {}).get("kind", "deterministic"), "autofix": fix_info}
    if contextual and changed:
        audit_info["contextual_stale"] = True
    timings["refine"] = round(timings.get("refine", 0.0) + time.perf_counter() - t_start, 3)
    manifest["audit"] = audit_info
    manifest["timings_s"] = {**manifest.get("timings_s", {}), **timings}
    manifest["exports"] = {"pptx": str(deck.pptx), **({"pdf": str(pdf)} if pdf else {})}
    manifest["warnings"] = list(dict.fromkeys([*manifest.get("warnings", []), *deck.warnings]))
    deck.manifest.write_text(json.dumps(manifest, ensure_ascii=False, indent=1), "utf-8")
    say(f"{deck.strategy}: применено {info['applied']} из {len(chosen)} выбранных фиксов, "
        f"{info['before']['errors']}→{info['after']['errors']} err, {info['before']['warnings']}→{info['after']['warnings']} warn")
    return DeckResult(deck.strategy, deck.pptx, deck.ir_json, deck.manifest, deck.stats, deck.warnings, deck.choices,
                      pngs, timings, deck.audit, audit_info, pdf)


def run(
    cfg: RunConfig,
    client: LLMClient | None = None,
    outline: DeckOutline | None = None,
    progress: Progress | None = None,
) -> RunResult:
    """Полный прогон по конфигу. `outline` передан → шаг content пропускается (режим build_variants и тестов)."""
    say = progress or (lambda msg: log.info(msg))
    t_start = time.perf_counter()
    out_dir = cfg.output_dir
    out_dir.mkdir(parents=True, exist_ok=True)

    parsed = parse_template(cfg.template, out_dir)
    tokens = parsed.tokens
    say(f"parse: {len(parsed.exemplars)} образцов, шрифт {tokens.fonts[0] if tokens.fonts else '?'}, "
        f"accent #{(tokens.palette('accent') or ['?'])[0]} ({parsed.seconds:.1f}s)")

    step = make_outline(cfg, parsed, out_dir, client, outline)
    if outline is None:
        say(f"outline: {len(step.outline.slides)} слайдов за {step.seconds:.1f}s, попыток {step.attempts}"
            + (f", предупреждений {len(step.warnings)}" if step.warnings else ""))
    else:
        say(f"outline: передан готовый ({len(step.outline.slides)} слайдов), LLM не вызывался")
    warnings = [f"outline: {w}" for w in step.warnings]
    timings: dict[str, float] = {"parse": parsed.seconds, "outline": step.seconds}

    ctx = RunContext.prepare(cfg, parsed, step, client)
    warnings += ctx.warnings
    decks: list[DeckResult] = []
    rows: dict[str, dict] = {}
    for name in cfg.strategies:
        d = build_deck(ctx, name, say)
        decks.append(d)
        rows[d.strategy] = {**d.stats, "audit_errors": d.audit_summary.get("errors", "—"),
                            "audit_warnings": d.audit_summary.get("warnings", "—")}
        warnings += [w if w.startswith(d.strategy) else f"{d.strategy}: {w}" for w in d.warnings]

    (out_dir / "compare.md").write_text(compare_table(rows) + "\n", "utf-8")
    timings["total"] = round(time.perf_counter() - t_start, 3)
    result = RunResult(out_dir, step.outline, step.path, decks, timings, warnings, parsed=parsed)
    result.run_json = out_dir / "run.json"
    result.run_json.write_text(json.dumps(run_summary(cfg, result), ensure_ascii=False, indent=1), "utf-8")
    say(f"готово за {timings['total']:.1f}s → {out_dir}")
    return result


def run_summary(cfg: RunConfig, result: RunResult) -> dict:
    """Содержимое run.json — сводка прогона (читают UI и API)."""
    not_implemented = [k for k, on in (("export:html", "html" in cfg.export),) if on]
    return {
        "created": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "config": json.loads(cfg.model_dump_json()),
        "template": result.parsed.meta if result.parsed else {},
        "outline": str(result.outline_path),
        "decks": [{"strategy": d.strategy, "pptx": str(d.pptx), "manifest": str(d.manifest), "stats": d.stats,
                   "audit": d.audit_summary, "exports": d.exports, "timings_s": d.timings_s} for d in result.decks],
        "timings_s": result.timings_s,
        "warnings": result.warnings,
        "not_implemented": not_implemented,  # читаются из конфига, но пока не выполняются
    }


# ──────────────────────────── вспомогательное ────────────────────────────


def _autofix(ir: DeckIR, report: AuditReport, parsed: ParsedTemplate, pptx_out: Path,
             findings: list[Finding]) -> tuple[DeckIR, AuditReport, dict]:
    """Фиксы → правка IR → повторный рендер поверх файла → повторный детерминированный аудит. Один проход."""
    before = audit_summary(report)
    info = {"applied": 0, "skipped": 0, "before": {"errors": before["errors"], "warnings": before["warnings"]},
            "after": {"errors": before["errors"], "warnings": before["warnings"]}, "items": []}
    if not findings:
        return ir, report, info
    fr = apply_fixes(ir, findings, parsed.dna)
    info["applied"], info["skipped"], info["items"] = len(fr.applied), len(fr.skipped), fr.applied + fr.skipped
    if not fr.changed:
        return ir, report, info
    render_pptx(fr.ir, parsed.template, parsed.exemplars, pptx_out)
    report = audit_deck(pptx_out, parsed.dna, fr.ir)
    after = audit_summary(report)
    info["after"] = {"errors": after["errors"], "warnings": after["warnings"]}
    return fr.ir, report, info


def _render_pngs(pptx: Path, out_dir: Path, dpi: int, contact: bool) -> tuple[list[Path], str | None]:
    """PNG по слайдам; ошибки LibreOffice не роняют прогон — возвращаются предупреждением."""
    from deckforge.export.render import render as render_png

    try:
        return render_png(pptx, out_dir, dpi=dpi, contact=contact), None
    except Exception as e:  # noqa: BLE001 — LibreOffice капризен
        return [], f"png: {str(e)[:120]}"


def _export_pdf(pptx: Path, rendered_dir: Path | None) -> tuple[Path | None, str | None]:
    """<strategy>.pdf рядом с .pptx. Если PNG уже рендерились, PDF из той папки копируется, а не конвертируется заново."""
    target = pptx.with_suffix(".pdf")
    ready = rendered_dir / pptx.with_suffix(".pdf").name if rendered_dir else None
    if ready is not None and ready.exists():
        shutil.copyfile(ready, target)
        return target, None
    if not soffice_available():
        return None, "export.pdf: LibreOffice не найден — PDF пропущен"
    try:
        tmp = pptx_to_pdf(pptx, pptx.parent / "_pdf")
        shutil.move(str(tmp), target)
        shutil.rmtree(pptx.parent / "_pdf", ignore_errors=True)
        return target, None
    except Exception as e:  # noqa: BLE001
        return None, f"export.pdf: {str(e)[:120]}"


def soffice_available() -> bool:
    try:
        find_soffice()
        return True
    except RuntimeError:
        return False


def _style(tokens: TemplateTokens) -> dict:
    # text_color — роль text из палитры: на тёмном шаблоне (WorkSpace) текст таблиц и осей диаграмм
    # иначе получил бы дефолтный тёмный цвет и пропал на фоне
    return {
        "accent": (tokens.palette("accent") or ["0077FF"])[0],
        "palette": ",".join(tokens.palette("accent") + tokens.palette("secondary")),
        "font": tokens.fonts[0] if tokens.fonts else "Arial",
        "text_color": (tokens.palette("text") or ["212121"])[0],
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
