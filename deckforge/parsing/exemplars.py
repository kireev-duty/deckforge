"""Индекс слайдов-образцов шаблона: из кэша разметки (out/archetypes/<stem>.json) или классификацией правилами.

Кэш пишет tools/classify_layouts.py (правила + VLM-уточнение). Без кэша — только правила, без VLM.
"""

from __future__ import annotations

import json
from pathlib import Path

from deckforge.core.ir import Archetype, Exemplar, Slot

ROOT = Path(__file__).resolve().parents[2]
ARCHETYPES_CACHE = ROOT / "out" / "archetypes"


def exemplars_from_json(data: list[dict]) -> list[Exemplar]:
    """Обратное к layout_classifier.profiles_json."""
    return [
        Exemplar(
            id=f"slide{d['index'] + 1}", source_index=d["index"], layout_name=d["layout"],
            archetype=Archetype(d["archetype"]), slots=[Slot(**s) for s in d["slots"]], fixed=d.get("fixed", []),
            fill_ratio=d.get("fill_ratio", 0.0), tags=d.get("tags", []), confidence=d.get("confidence", 1.0),
        )
        for d in data
    ]


def load_exemplars(pptx: str | Path, cache_dir: Path | None = None) -> list[Exemplar]:
    pptx = Path(pptx)
    cache = (cache_dir or ARCHETYPES_CACHE) / f"{pptx.stem}.json"
    if cache.exists():
        return exemplars_from_json(json.loads(cache.read_text("utf-8")))
    from deckforge.parsing.layout_classifier import classify_template

    return [p.to_exemplar() for p in classify_template(pptx)]


__all__ = ["ARCHETYPES_CACHE", "exemplars_from_json", "load_exemplars"]
