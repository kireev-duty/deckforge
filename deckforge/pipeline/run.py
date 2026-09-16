"""Оркестрация прогона: шаблон + контент-пакет → outline (LLM, один на прогон) → N колод по стратегиям.

Пишет в output_dir: outline.json, <strategy>.pptx / .ir.json / .manifest.json, compare.md, run.json.
manifest.json — провенанс колоды: версии скиллов и моделей, стратегия, план, выбранные образцы, тайминги.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from deckforge.content import load_content_pack, write_outline
from deckforge.core.ir import Archetype, DeckOutline, Exemplar
from deckforge.core.strategy import load_strategy
from deckforge.layout import LayoutResult, build_deck_ir
from deckforge.llm.client import LLMClient
from deckforge.parsing.exemplars import load_exemplars
from deckforge.parsing.extract_tokens import TemplateTokens, extract_tokens
from deckforge.pipeline.config import RunConfig
from deckforge.pipeline.stats import compare_table, deck_stats
from deckforge.render import render_pptx

log = logging.getLogger(__name__)

Progress = Callable[[str], None]


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


@dataclass
class RunResult:
    output_dir: Path
    outline: DeckOutline
    outline_path: Path
    decks: list[DeckResult]
    timings_s: dict[str, float]
    warnings: list[str] = field(default_factory=list)
    run_json: Path | None = None

    @property
    def total_s(self) -> float:
        return self.timings_s.get("total", 0.0)


def run(
    cfg: RunConfig,
    client: LLMClient | None = None,
    outline: DeckOutline | None = None,
    progress: Progress | None = None,
) -> RunResult:
    """Полный прогон по конфигу. `outline` передан → шаг content пропускается (режим build_variants и тестов)."""
    say = progress or (lambda msg: log.info(msg))
    t_start = time.perf_counter()
    timings: dict[str, float] = {}
    warnings: list[str] = []
    out_dir = cfg.output_dir
    out_dir.mkdir(parents=True, exist_ok=True)

    # 1. parse — токены + образцы (кэш разметки в out/archetypes, если есть)
    t0 = time.perf_counter()
    exemplars = load_exemplars(cfg.template)
    tokens = extract_tokens(cfg.template)
    timings["parse"] = round(time.perf_counter() - t0, 3)
    say(f"parse: {len(exemplars)} образцов, шрифт {tokens.fonts[0] if tokens.fonts else '?'}, "
        f"accent #{(tokens.palette('accent') or ['?'])[0]} ({timings['parse']:.1f}s)")

    # 2. content — один outline на прогон
    outline_warnings: list[str] = []
    llm_calls: list[dict] = []
    skills_used: dict[str, str] = {}
    if outline is None:
        t0 = time.perf_counter()
        client = client or LLMClient()
        pack = load_content_pack(cfg.content_pack)
        res = write_outline(
            client, pack, purpose=cfg.purpose, audience=cfg.audience, language=cfg.language,
            target_slides=cfg.target_slides, available_archetypes=available_archetypes(exemplars),
        )
        outline, outline_warnings = res.outline, res.warnings
        (out_dir / "outline.raw.json").write_text(json.dumps(res.raw, ensure_ascii=False, indent=1), "utf-8")
        skills_used["outline_writer"] = _skill_version(client, "outline_writer")
        llm_calls = [asdict(c) for c in client.calls]
        timings["outline"] = round(time.perf_counter() - t0, 3)
        say(f"outline: {len(outline.slides)} слайдов за {timings['outline']:.1f}s, попыток {res.attempts}"
            + (f", предупреждений {len(outline_warnings)}" if outline_warnings else ""))
        warnings += [f"outline: {w}" for w in outline_warnings]
    else:
        timings["outline"] = 0.0
        say(f"outline: передан готовый ({len(outline.slides)} слайдов), LLM не вызывался")
    outline_path = out_dir / "outline.json"
    outline_path.write_text(outline.model_dump_json(indent=1), "utf-8")

    # 3. per strategy — layout → render → manifest
    style = _style(tokens)
    models = _models(client)
    template_meta = {"id": tokens.template_id, "path": str(cfg.template), "sha1": _sha1(cfg.template)}
    decks: list[DeckResult] = []
    rows: dict[str, dict] = {}
    for name in cfg.strategies:
        strategy = load_strategy(name)
        t0 = time.perf_counter()
        res: LayoutResult = build_deck_ir(outline, strategy, exemplars, tokens.template_id, tokens.slide_w,
                                          tokens.slide_h, style)
        t_layout = time.perf_counter() - t0
        pptx_out = out_dir / f"{strategy.name}.pptx"
        t0 = time.perf_counter()
        render_pptx(res.ir, cfg.template, exemplars, pptx_out)
        t_render = time.perf_counter() - t0
        ir_path = out_dir / f"{strategy.name}.ir.json"
        ir_path.write_text(res.ir.model_dump_json(indent=1), "utf-8")

        st = deck_stats(res)
        deck_timings = {"layout": round(t_layout, 3), "render": round(t_render, 3)}
        pngs: list[Path] = []
        if cfg.render_png:
            from deckforge.export.render import render as render_png

            t0 = time.perf_counter()
            pngs = render_png(pptx_out, out_dir / strategy.name, dpi=cfg.render_dpi, contact=True)
            deck_timings["png"] = round(time.perf_counter() - t0, 3)
        choices = [c.__dict__ for c in res.choices]
        manifest = {
            "created": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "template": template_meta,
            "content_pack": str(cfg.content_pack),
            "outline": str(outline_path),
            "strategy": {"name": strategy.name, "version": strategy.version},
            "skills": skills_used,
            "models": models,
            "llm_calls": llm_calls,
            "plan": [{"idx": s.idx, "archetype": s.archetype.value, "title": s.title} for s in res.plan.slides],
            "choices": choices,
            "warnings": [f"outline: {w}" for w in outline_warnings] + res.warnings,
            "stats": st,
            "timings_s": {"parse": timings["parse"], "outline": timings["outline"], **deck_timings},
        }
        manifest_path = out_dir / f"{strategy.name}.manifest.json"
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=1), "utf-8")
        decks.append(DeckResult(strategy.name, pptx_out, ir_path, manifest_path, st, list(res.warnings), choices,
                                pngs, deck_timings))
        rows[strategy.name] = st
        say(f"{strategy.id}: {st['slides']} слайдов → {pptx_out.name} (layout {t_layout:.2f}s, render {t_render:.2f}s"
            + (f", png {deck_timings['png']:.1f}s" if pngs else "") + ")")
        warnings += [w if w.startswith(strategy.name) else f"{strategy.name}: {w}" for w in res.warnings]

    (out_dir / "compare.md").write_text(compare_table(rows) + "\n", "utf-8")
    timings["total"] = round(time.perf_counter() - t_start, 3)
    result = RunResult(out_dir, outline, outline_path, decks, timings, warnings)
    not_implemented = [k for k, on in (("images", cfg.images != "off"), ("audit", cfg.audit.deterministic or cfg.audit.contextual),
                                       ("export", any(e != "pptx" for e in cfg.export))) if on]
    run_json = {
        "created": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "config": json.loads(cfg.model_dump_json()),
        "template": template_meta,
        "outline": str(outline_path),
        "decks": [{"strategy": d.strategy, "pptx": str(d.pptx), "manifest": str(d.manifest), "stats": d.stats,
                   "timings_s": d.timings_s} for d in decks],
        "timings_s": timings,
        "warnings": warnings,
        "not_implemented": not_implemented,  # читаются из конфига, но пока не выполняются
    }
    result.run_json = out_dir / "run.json"
    result.run_json.write_text(json.dumps(run_json, ensure_ascii=False, indent=1), "utf-8")
    say(f"готово за {timings['total']:.1f}s → {out_dir}")
    return result


# ──────────────────────────── вспомогательное ────────────────────────────


def _style(tokens: TemplateTokens) -> dict:
    return {
        "accent": (tokens.palette("accent") or ["0077FF"])[0],
        "palette": ",".join(tokens.palette("accent") + tokens.palette("secondary")),
        "font": tokens.fonts[0] if tokens.fonts else "Arial",
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


def _sha1(path: Path) -> str:
    h = hashlib.sha1()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()[:12]


def available_archetypes(exemplars: list[Exemplar]) -> set[Archetype]:
    return {e.archetype for e in exemplars if e.archetype != Archetype.FREEFORM}


__all__ = ["DeckResult", "RunResult", "available_archetypes", "run"]
