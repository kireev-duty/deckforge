"""build_variants — один контент × один шаблон × N стратегий → N колод, рендер и таблица сравнения.

    .venv\\Scripts\\python.exe tools\\build_variants.py "data\\templates\\VK Tech шаблон.pptx" [--outline examples/content_pack/outline.json]
        [--strategies executive,narrative,visual] [--no-render] [--dpi 72]

Обёртка над `deckforge.pipeline.run` с готовым outline, без LLM. Результат — out/variants/<stem>/.
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
    ap.add_argument("--no-fix", action="store_true", help="без safe-автофиксов (аудит «как есть»)")
    ap.add_argument("--judge", action="store_true", help="плюс VLM-судья по PNG (нужен API; ≈30 с на колоду)")
    ap.add_argument("--dpi", type=int, default=72)
    a = ap.parse_args()

    outline = DeckOutline.model_validate_json(a.outline.read_text("utf-8"))
    cfg = RunConfig(
        template=a.pptx.resolve(), content_pack=a.outline.resolve().parent, purpose=outline.purpose,
        audience=outline.audience, language=outline.language, strategies=a.strategies.split(","),
        output_dir=ROOT / "out" / "variants" / a.pptx.stem, render_png=not a.no_render, render_dpi=a.dpi,
        images="off", audit={"deterministic": True, "contextual": a.judge, "autofix": not a.no_fix},
    )
    if a.judge:
        from deckforge.llm import load_dotenv

        load_dotenv()
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
            s = d.audit_summary
            fix = s.get("autofix") or {}
            fix_note = (f"; autofix {fix['applied']} применено, {fix['skipped']} пропущено, "
                        f"ошибок {fix['before']['errors']}→{fix['after']['errors']}" if fix else "")
            print(f"  аудит: {s['errors']} err / {s['warnings']} warn{fix_note} → {d.audit}  {s['by_check']}")
        if d.pngs:
            print(f"  рендер: {len(d.pngs)} PNG → {d.pngs[0].parent / 'contact.png'}")
    print("\n" + (result.output_dir / "compare.md").read_text("utf-8"))


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    main()
