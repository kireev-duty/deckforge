"""classify_layouts — таблица «слайд → архетип → слоты» по шаблонам, с VLM-уточнением неоднозначных.

Обёртка над `deckforge.pipeline.prepare.prepare_template` (то же делают `deckforge prepare`, кнопка в UI и
`POST /templates/{id}/prepare`): рендер в PNG, классификация правилами, при --vlm — уточнение неоднозначных
слайдов через template_tagger и кэш ответов в out/archetypes/<stem>.json. Таблица — out/archetypes/<stem>.md;
--publish кладёт ответы VLM в data/archetypes/<stem>.json (в репо, для чистого clone).

    .venv\\Scripts\\python.exe tools\\classify_layouts.py "data\\templates\\*.pptx" [--vlm] [--publish] [--no-render] [--parallel 4]
"""

from __future__ import annotations

import argparse
import glob
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from deckforge.parsing.exemplars import ARCHETYPES_BUNDLED, vlm_payload
from deckforge.pipeline.prepare import prepare_template
from tools.check_env import load_dotenv


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
        report = prepare_template(f, client, progress=lambda m: print(f"  {m}"), max_parallel=a.parallel,
                                  do_render=not a.no_render, cache_dir=a.out)
        md = report.markdown()
        print(md, "\n")
        for w in report.warnings:
            print(f"  ! {w}")
        (a.out / f"{report.template.stem}.md").write_text(md, "utf-8")
        if a.publish and report.cache is not None:
            ARCHETYPES_BUNDLED.mkdir(parents=True, exist_ok=True)
            (ARCHETYPES_BUNDLED / f"{report.template.stem}.json").write_text(
                json.dumps(vlm_payload(report.template, report.profiles), ensure_ascii=False, indent=1), "utf-8")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
