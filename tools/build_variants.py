"""build_variants — один контент × один шаблон × N стратегий → N колод, рендер и таблица сравнения.

    .venv\\Scripts\\python.exe tools\\build_variants.py "data\\templates\\VK Tech шаблон.pptx" [--outline examples/content_pack/outline.json]
        [--strategies executive,narrative,visual] [--no-render] [--dpi 72]

Тонкая обёртка над `deckforge.pipeline.run` с готовым outline (LLM не вызывается). Результат:
out/variants/<stem>/<strategy>.pptx, <strategy>.ir.json, <strategy>.manifest.json, рендер в out/variants/<stem>/<strategy>/
(contact.png + slide_NN.png), таблица сравнения в stdout и в out/variants/<stem>/compare.md.
Образцы — из out/archetypes/<stem>.json (см. tools/classify_layouts.py), без кэша — классификация правилами.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from deckforge.core.ir import DeckOutline  # noqa: E402
from deckforge.core.strategy import list_strategies  # noqa: E402
from deckforge.pipeline import RunConfig, run  # noqa: E402

DEFAULT_OUTLINE = ROOT / "examples" / "content_pack" / "outline.json"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("pptx", type=Path)
    ap.add_argument("--outline", type=Path, default=DEFAULT_OUTLINE)
    ap.add_argument("--strategies", default=",".join(list_strategies()))
    ap.add_argument("--no-render", action="store_true")
    ap.add_argument("--dpi", type=int, default=72)
    a = ap.parse_args()

    outline = DeckOutline.model_validate_json(a.outline.read_text("utf-8"))
    cfg = RunConfig(
        template=a.pptx.resolve(), content_pack=a.outline.resolve().parent, purpose=outline.purpose,
        audience=outline.audience, language=outline.language, strategies=a.strategies.split(","),
        output_dir=ROOT / "out" / "variants" / a.pptx.stem, render_png=not a.no_render, render_dpi=a.dpi,
        images="off", audit={"deterministic": True, "contextual": False, "autofix": False},
    )
    result = run(cfg, outline=outline, progress=lambda m: print("  " + m))
    for d in result.decks:
        print(f"\n== {d.strategy}: {d.stats['slides']} слайдов → {d.pptx}")
        for c in d.choices:
            mark = "" if c["exemplar_id"] else "  ← ПРОПУЩЕН"
            print(f"  {c['idx'] + 1:2d}. {c['archetype']:<11} → {c['exemplar_archetype'] or '-':<11} "
                  f"{c['exemplar_id'] or '-':<8} {c['score']:6.1f}{mark}")
        for w in d.warnings:
            print(f"  ! {w}")
        if d.audit_summary:
            a = d.audit_summary
            print(f"  аудит: {a['errors']} err / {a['warnings']} warn → {d.audit}  {a['by_check']}")
        if d.pngs:
            print(f"  рендер: {len(d.pngs)} PNG → {d.pngs[0].parent / 'contact.png'}")
    print("\n" + (result.output_dir / "compare.md").read_text("utf-8"))


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    main()
