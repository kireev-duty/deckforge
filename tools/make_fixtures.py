"""Выгрузить синтетические колоды-нарушения из tests/fixtures/bad_slides.py, чтобы посмотреть их глазами.

    python tools/make_fixtures.py [--out out/fixtures] [--only L03,T06] [--audit]

--audit — прогнать аудит по каждой и напечатать таблицу.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tests.fixtures.bad_slides import FIXTURES, make_clean  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", type=Path, default=ROOT / "out" / "fixtures")
    ap.add_argument("--only", default="")
    ap.add_argument("--audit", action="store_true")
    a = ap.parse_args()
    a.out.mkdir(parents=True, exist_ok=True)
    names = a.only.split(",") if a.only else list(FIXTURES)
    cases = [make_clean(a.out)] + [FIXTURES[n](a.out) for n in names]
    for c in cases:
        print(f"{c.check or 'clean':<6} → {c.pptx}")
        if a.audit:
            from deckforge.audit import audit_deck, report_markdown

            print(report_markdown(audit_deck(c.pptx, c.dna, c.ir)), end="\n\n")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
