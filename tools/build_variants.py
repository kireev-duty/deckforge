"""build_variants — один контент × один шаблон × N стратегий → N колод, рендер и таблица сравнения.

    .venv\\Scripts\\python.exe tools\\build_variants.py "..\\Датасет\\VK Tech шаблон.pptx" [--outline examples/content_pack/outline.json]
        [--strategies executive,narrative,visual] [--no-render] [--dpi 72]

Результат: out/variants/<stem>/<strategy>.pptx, <strategy>.ir.json, <strategy>.manifest.json,
рендер в out/variants/<stem>/<strategy>/ (contact.png + slide_NN.png) и таблица сравнения в stdout
и в out/variants/<stem>/compare.md. Образцы — из out/archetypes/<stem>.json (см. tools/classify_layouts.py),
без кэша — классификация правилами.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools.render_deck import render  # noqa: E402

from deckforge.core.ir import DeckOutline, SlotKind  # noqa: E402
from deckforge.core.strategy import list_strategies, load_strategy  # noqa: E402
from deckforge.layout import LayoutResult, build_deck_ir  # noqa: E402
from deckforge.parsing.exemplars import load_exemplars  # noqa: E402
from deckforge.parsing.extract_tokens import extract_tokens  # noqa: E402
from deckforge.render import render_pptx  # noqa: E402

DEFAULT_OUTLINE = ROOT / "examples" / "content_pack" / "outline.json"


def stats(res: LayoutResult) -> dict:
    """Метрики плотности и визуализации по DeckIR — для таблицы сравнения и manifest."""
    slides = res.ir.slides
    n_items = words = 0
    n_slides_with_items = 0
    kinds = {"chart": 0, "table": 0, "kpi": 0, "picture": 0}
    for s in slides:
        items = [p for el in s.elements if el.kind == SlotKind.BODY for p in el.paragraphs]
        items += [p for el in s.elements if el.kind in (SlotKind.LABEL, SlotKind.CAPTION) for p in el.paragraphs]
        if items:
            n_slides_with_items += 1
            n_items += len(items)
            words += sum(len(r.text.split()) for p in items for r in p.runs)
        if any(el.chart for el in s.elements):
            kinds["chart"] += 1
        if any(el.table for el in s.elements):
            kinds["table"] += 1
        if any(el.kind == SlotKind.NUMBER for el in s.elements):
            kinds["kpi"] += 1
        if any(el.image_path for el in s.elements):
            kinds["picture"] += 1
    return {
        "slides": len(slides),
        "archetypes": [s.archetype.value for s in slides],
        "exemplars": [s.exemplar_id for s in slides],
        "text_items_per_slide": round(n_items / n_slides_with_items, 1) if n_slides_with_items else 0,
        "words_per_item": round(words / n_items, 1) if n_items else 0,
        **kinds,
        "skipped": sum(1 for c in res.choices if c.exemplar_id is None),
    }


def compare_table(rows: dict[str, dict]) -> str:
    head = "| стратегия | слайдов | архетипы по порядку | пунктов/слайд | слов/пункт | chart | table | kpi | пропущено |"
    sep = "|---|---|---|---|---|---|---|---|---|"
    lines = [head, sep]
    for name, st in rows.items():
        lines.append(
            f"| {name} | {st['slides']} | {' → '.join(st['archetypes'])} | {st['text_items_per_slide']} | "
            f"{st['words_per_item']} | {st['chart']} | {st['table']} | {st['kpi']} | {st['skipped']} |"
        )
    return "\n".join(lines)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("pptx", type=Path)
    ap.add_argument("--outline", type=Path, default=DEFAULT_OUTLINE)
    ap.add_argument("--strategies", default=",".join(list_strategies()))
    ap.add_argument("--no-render", action="store_true")
    ap.add_argument("--dpi", type=int, default=72)
    a = ap.parse_args()

    outline = DeckOutline.model_validate_json(a.outline.read_text("utf-8"))
    exemplars = load_exemplars(a.pptx)
    tokens = extract_tokens(a.pptx)
    style = {
        "accent": (tokens.palette("accent") or ["0077FF"])[0],
        "palette": ",".join(tokens.palette("accent") + tokens.palette("secondary")),
        "font": tokens.fonts[0] if tokens.fonts else "Arial",
    }
    out_dir = ROOT / "out" / "variants" / a.pptx.stem
    out_dir.mkdir(parents=True, exist_ok=True)

    rows: dict[str, dict] = {}
    for name in a.strategies.split(","):
        strategy = load_strategy(name.strip())
        t0 = time.perf_counter()
        res = build_deck_ir(outline, strategy, exemplars, tokens.template_id, tokens.slide_w, tokens.slide_h, style)
        t_layout = time.perf_counter() - t0
        pptx_out = out_dir / f"{strategy.name}.pptx"
        render_pptx(res.ir, a.pptx, exemplars, pptx_out)
        t_render = time.perf_counter() - t0 - t_layout
        (out_dir / f"{strategy.name}.ir.json").write_text(res.ir.model_dump_json(indent=1), "utf-8")
        st = stats(res)
        manifest = {
            "created": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "template": {"id": tokens.template_id, "path": str(a.pptx)},
            "outline": str(a.outline),
            "strategy": {"name": strategy.name, "version": strategy.version},
            "skills": {},  # детерминированная сборка: LLM не вызывался
            "plan": [{"idx": s.idx, "archetype": s.archetype.value, "title": s.title} for s in res.plan.slides],
            "choices": [c.__dict__ for c in res.choices],
            "warnings": res.warnings,
            "stats": st,
            "timings_s": {"layout": round(t_layout, 3), "render": round(t_render, 3)},
        }
        (out_dir / f"{strategy.name}.manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=1), "utf-8")
        rows[strategy.name] = st
        print(f"\n== {strategy.id}: {st['slides']} слайдов → {pptx_out}  (layout {t_layout:.2f}s, render {t_render:.2f}s)")
        for c in res.choices:
            mark = "" if c.exemplar_id else "  ← ПРОПУЩЕН"
            print(f"  {c.idx + 1:2d}. {c.archetype:<11} → {c.exemplar_archetype or '-':<11} {c.exemplar_id or '-':<8} {c.score:6.1f}{mark}")
        for w in res.warnings:
            print(f"  ! {w}")
        if not a.no_render:
            pngs = render(pptx_out, out_dir / strategy.name, dpi=a.dpi, contact=True)
            print(f"  рендер: {len(pngs)} PNG → {out_dir / strategy.name / 'contact.png'}")

    table = compare_table(rows)
    (out_dir / "compare.md").write_text(table + "\n", "utf-8")
    print("\n" + table)


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    main()
