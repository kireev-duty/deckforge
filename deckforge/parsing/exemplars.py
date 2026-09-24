"""Индекс слайдов-образцов шаблона: правила классификатора + кэш ответов VLM.

Слоты и архетипы правил считаются заново при каждой загрузке; из кэша берутся только ответы
`template_tagger`. Источники: `out/archetypes/<stem>.json`, затем `data/archetypes/<stem>.json` (в репо).
Кэш привязан к sha1 файла шаблона.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from deckforge.core.ir import Archetype, Exemplar, Slot
from deckforge.parsing.layout_classifier import SlideProfile, apply_vlm, classify_template, profiles_json

ROOT = Path(__file__).resolve().parents[2]
ARCHETYPES_CACHE = ROOT / "out" / "archetypes"
ARCHETYPES_BUNDLED = ROOT / "data" / "archetypes"


def exemplars_from_json(data: list[dict]) -> list[Exemplar]:
    """Обратное к layout_classifier.profiles_json."""
    return [
        Exemplar(
            id=f"slide{d['index'] + 1}", source_index=d["index"], layout_name=d["layout"],
            archetype=Archetype(d["archetype"]), slots=[Slot(**s) for s in d["slots"]], fixed=d.get("fixed", []),
            fill_ratio=d.get("fill_ratio", 0.0), tags=d.get("tags", []), confidence=d.get("confidence", 1.0),
            card_frames=d.get("card_frames", False),
        )
        for d in data
    ]


def template_sha1(pptx: Path) -> str:
    h = hashlib.sha1()
    with pptx.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def find_vlm_cache(pptx: Path, cache_dir: Path | None = None) -> Path | None:
    """Первый файл разметки для этого шаблона: `cache_dir`, иначе out/archetypes → data/archetypes."""
    dirs = [cache_dir] if cache_dir is not None else [ARCHETYPES_CACHE, ARCHETYPES_BUNDLED]
    sha = None
    for d in dirs:
        cache = d / f"{pptx.stem}.json"
        if not cache.exists():
            continue
        data = json.loads(cache.read_text("utf-8"))
        if isinstance(data, dict) and data.get("template_sha1") is not None:
            sha = sha or template_sha1(pptx)
            if data["template_sha1"] != sha:
                continue
        return cache
    return None


def load_profiles(pptx: str | Path, cache_dir: Path | None = None) -> list[SlideProfile]:
    """Профили правил с наложенными ответами VLM из кэша, если он есть."""
    pptx = Path(pptx)
    profiles = classify_template(pptx)
    cache = find_vlm_cache(pptx, cache_dir)
    if cache is None:
        return profiles
    data = json.loads(cache.read_text("utf-8"))
    entries = data.get("slides", data) if isinstance(data, dict) else data
    by_index = {int(d["index"]): d for d in entries if d.get("vlm")}
    for p in profiles:
        if (d := by_index.get(p.index)) is not None:
            apply_vlm(p, d["vlm"])
    return profiles


def has_vlm_labels(pptx: str | Path, cache_dir: Path | None = None) -> bool:
    """Есть ли для шаблона ответы VLM в кэше (кэш без них — прогон classify_layouts без --vlm)."""
    cache = find_vlm_cache(Path(pptx), cache_dir)
    if cache is None:
        return False
    data = json.loads(cache.read_text("utf-8"))
    entries = data.get("slides", data) if isinstance(data, dict) else data
    return any(d.get("vlm") for d in entries)


def load_exemplars(pptx: str | Path, cache_dir: Path | None = None) -> list[Exemplar]:
    return [p.to_exemplar() for p in load_profiles(pptx, cache_dir)]


def cache_payload(pptx: Path, profiles: list[SlideProfile]) -> dict:
    """Что пишет tools/classify_layouts.py: снимок профилей + sha1 шаблона."""
    return {"template_sha1": template_sha1(pptx), "slides": profiles_json(profiles)}


def vlm_payload(pptx: Path, profiles: list[SlideProfile]) -> dict:
    """Что кладётся в data/archetypes: только ответы VLM по слайдам + sha1 шаблона."""
    return {
        "template_sha1": template_sha1(pptx),
        "slides": [{"index": p.index, "vlm": p.vlm_raw} for p in profiles if p.vlm_raw],
    }


__all__ = ["ARCHETYPES_BUNDLED", "ARCHETYPES_CACHE", "cache_payload", "exemplars_from_json", "find_vlm_cache",
           "has_vlm_labels", "load_exemplars", "load_profiles", "template_sha1", "vlm_payload"]
