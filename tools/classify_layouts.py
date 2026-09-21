"""classify_layouts — таблица «слайд → архетип → слоты» по шаблонам, с VLM-уточнением неоднозначных.

Рендер в PNG, классификация правилами, при --vlm — уточнение неоднозначных слайдов через template_tagger.
Результат — out/archetypes/<stem>.md + .json; --publish кладёт ответы VLM в data/archetypes/<stem>.json.

    .venv\\Scripts\\python.exe tools\\classify_layouts.py "data\\templates\\*.pptx" [--vlm] [--publish] [--no-render] [--parallel 4]
"""

from __future__ import annotations

import argparse
import glob
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools.check_env import load_dotenv  # noqa: E402
from tools.render_deck import render  # noqa: E402

from deckforge.parsing.exemplars import ARCHETYPES_BUNDLED, cache_payload, vlm_payload  # noqa: E402
from deckforge.parsing.layout_classifier import classify_template, markdown_table  # noqa: E402


def thumbnails_for(pptx: Path, do_render: bool) -> dict[int, Path]:
    out = ROOT / "out" / "render" / pptx.stem
    pngs = sorted(out.glob("slide_*.png"))
    if not pngs and do_render:
        print(f"  рендер {pptx.name} → {out}")
        pngs = render(pptx, out, contact=True)
    return {int(p.stem.split("_")[1]) - 1: p for p in pngs}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("patterns", nargs="+", help="пути или glob-маски .pptx")
    ap.add_argument("--vlm", action="store_true", help="уточнять неоднозначные слайды через VLM")
    ap.add_argument("--no-render", action="store_true", help="не рендерить, использовать только готовые PNG")
    ap.add_argument("--parallel", type=int, default=4)
    ap.add_argument("--out", type=Path, default=ROOT / "out" / "archetypes")
    ap.add_argument("--publish", action="store_true",
                    help="ответы VLM также в data/archetypes/<stem>.json (в репо, для чистого clone)")
    a = ap.parse_args()

    files = [Path(p) for pat in a.patterns for p in (glob.glob(pat) or [pat])]
    client = None
    if a.vlm:
        load_dotenv()
        from deckforge.llm.client import LLMClient

        client = LLMClient()
    a.out.mkdir(parents=True, exist_ok=True)

    for f in files:
        t0 = time.perf_counter()
        thumbs = thumbnails_for(f, not a.no_render)
        profiles = classify_template(f, client, thumbs, max_parallel=a.parallel)
        ambiguous = [p for p in profiles if p.source == "vlm" or p.ambiguous]
        changed = [p for p in profiles if p.source == "vlm" and p.archetype != p.rules_archetype]
        md = markdown_table(profiles, f.name)
        md += (f"\n\nНеоднозначных (→ VLM): {len(ambiguous)} из {len(profiles)}; VLM изменила вердикт: {len(changed)}"
               f" ({', '.join(str(p.index + 1) for p in changed) or '—'}); {time.perf_counter() - t0:.0f} с")
        print(md, "\n")
        (a.out / f"{f.stem}.md").write_text(md, "utf-8")
        # кэш: снимок профилей + sha1 шаблона; при загрузке берутся только ответы VLM
        (a.out / f"{f.stem}.json").write_text(json.dumps(cache_payload(f, profiles), ensure_ascii=False, indent=1), "utf-8")
        if a.publish:
            ARCHETYPES_BUNDLED.mkdir(parents=True, exist_ok=True)
            (ARCHETYPES_BUNDLED / f"{f.stem}.json").write_text(
                json.dumps(vlm_payload(f, profiles), ensure_ascii=False, indent=1), "utf-8")
        if client is not None:
            errors = [c for c in client.calls if not c.ok]
            print(f"  VLM-вызовов: {len(client.calls)}, ошибок: {len(errors)}, "
                  f"время: {sum(c.duration_s for c in client.calls):.0f} с суммарно")
            client.calls.clear()


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
