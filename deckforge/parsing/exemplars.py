"""Индекс слайдов-образцов шаблона: правила классификатора + (если есть) кэш ответов VLM.

Слоты и архетипы правил считаются всегда заново — так правки `layout_classifier` действуют сразу.
Из кэша `out/archetypes/<stem>.json` (пишет tools/classify_layouts.py) берутся только ответы
`template_tagger` по неоднозначным слайдам и накладываются на свежий профиль (`apply_vlm`).
Кэш привязан к sha1 файла шаблона: другой файл с тем же именем кэшем не пользуется.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from deckforge.core.ir import Archetype, Exemplar, Slot
from deckforge.parsing.layout_classifier import SlideProfile, apply_vlm, classify_template, profiles_json

ROOT = Path(__file__).resolve().parents[2]
ARCHETYPES_CACHE = ROOT / "out" / "archetypes"


def exemplars_from_json(data: list[dict]) -> list[Exemplar]:
    """Обратное к layout_classifier.profiles_json (полный снимок, без пересчёта правил)."""
    return [
        Exemplar(
            id=f"slide{d['index'] + 1}", source_index=d["index"], layout_name=d["layout"],
            archetype=Archetype(d["archetype"]), slots=[Slot(**s) for s in d["slots"]], fixed=d.get("fixed", []),
            fill_ratio=d.get("fill_ratio", 0.0), tags=d.get("tags", []), confidence=d.get("confidence", 1.0),
        )
        for d in data
    ]


def template_sha1(pptx: Path) -> str:
    h = hashlib.sha1()
    with pptx.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def load_profiles(pptx: str | Path, cache_dir: Path | None = None) -> list[SlideProfile]:
    """Профили правил с наложенными ответами VLM из кэша (если кэш есть и относится к этому файлу)."""
    pptx = Path(pptx)
    profiles = classify_template(pptx)
    cache = (cache_dir or ARCHETYPES_CACHE) / f"{pptx.stem}.json"
    if not cache.exists():
        return profiles
    data = json.loads(cache.read_text("utf-8"))
    entries = data.get("slides", data) if isinstance(data, dict) else data
    if isinstance(data, dict) and data.get("template_sha1") not in (None, template_sha1(pptx)):
        return profiles
    by_index = {int(d["index"]): d for d in entries if d.get("vlm")}
    for p in profiles:
        if (d := by_index.get(p.index)) is not None:
            apply_vlm(p, d["vlm"])
    return profiles


def load_exemplars(pptx: str | Path, cache_dir: Path | None = None) -> list[Exemplar]:
    return [p.to_exemplar() for p in load_profiles(pptx, cache_dir)]


def cache_payload(pptx: Path, profiles: list[SlideProfile]) -> dict:
    """Что пишет tools/classify_layouts.py: снимок профилей + sha1 шаблона."""
    return {"template_sha1": template_sha1(pptx), "slides": profiles_json(profiles)}


__all__ = ["ARCHETYPES_CACHE", "cache_payload", "exemplars_from_json", "load_exemplars", "load_profiles", "template_sha1"]
