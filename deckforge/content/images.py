"""Иллюстрации к слайдам: text-to-image в стиле шаблона (скилл `image_prompter` → T2I-модель).

Что иллюстрируем — решает стратегия (`Strategy.images`) и режим конфига (`RunConfig.images`):
- `off` — ничего; `minimal` — только готовые картинки из контент-пакета (`image.path`);
- `preferred` — слайды, где outline просит картинку (`image.prompt`/`alt`) или архетип image_text/image_full;
- `always` (или `images: always` в конфиге) — дополнительно текстовые слайды без данных, титул и финал,
  пока не исчерпан лимит MAX_IMAGES на колоду.

Промпт составляет LLM по заголовку, тексту слайда и палитре шаблона (без текста на картинке);
результат кладётся в `<out_dir>/images/` и кэшируется по sha1 входов (тот же слайд на другой
стратегии картинку не платит). Ошибка API — предупреждение, слайд остаётся без картинки, и
picker тогда штрафует образцы с picture-слотом, как раньше.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from deckforge.core.ir import Archetype, DeckOutline, ImageSpec, OutlineSlide
from deckforge.core.strategy import Strategy
from deckforge.llm.client import LLMClient
from deckforge.llm.skills import load_skill

log = logging.getLogger(__name__)

MAX_IMAGES = 4  # на колоду: prompt ≈ 4 с + генерация ≈ 5 с, ×4 параллельно — укладываемся в бюджет
PARALLEL = 4
IMAGE_SIZE = "1024x576"  # 16:9 — под широкий слот; рендер делает center-crop под любой другой
PICTURE_ARCHETYPES = (Archetype.IMAGE_TEXT, Archetype.IMAGE_FULL)
TEXT_ARCHETYPES = (Archetype.BULLETS, Archetype.CARDS, Archetype.TWO_COLUMN, Archetype.QUOTE)
FRAME_ARCHETYPES = (Archetype.TITLE, Archetype.CLOSING)  # у титула/финала часто есть рамка под картинку


@dataclass
class ImageItem:
    slide_idx: int
    title: str
    source: str  # generated | cache | content | failed
    path: str | None = None
    prompt: str | None = None
    error: str | None = None


@dataclass
class IllustrateResult:
    outline: DeckOutline
    mode: str  # эффективный режим: off | minimal | preferred | always
    items: list[ImageItem] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    seconds: float = 0.0
    skill_version: str | None = None

    def summary(self) -> dict:
        counts = {k: sum(1 for i in self.items if i.source == k) for k in ("generated", "cache", "content", "failed")}
        return {"mode": self.mode, **counts, "items": [asdict(i) for i in self.items]}


def effective_mode(cfg_mode: str, strategy: Strategy) -> str:
    if cfg_mode == "off":
        return "off"
    if cfg_mode == "always":
        return "always"
    return strategy.images


def candidates(outline: DeckOutline, mode: str, limit: int = MAX_IMAGES) -> list[OutlineSlide]:
    """Слайды под иллюстрацию в порядке приоритета: картиночные архетипы → просьба outline →
    текстовые без данных → титул/финал. Слайды с готовой картинкой не считаются."""
    if mode in ("off", "minimal"):
        return []
    ranked: list[tuple[int, int, OutlineSlide]] = []
    for s in outline.slides:
        if s.image and s.image.path:
            continue
        if s.chart or s.table or s.kpis or s.steps:
            continue
        if s.archetype in PICTURE_ARCHETYPES:
            rank = 0
        elif s.image and (s.image.prompt or s.image.alt):
            rank = 1
        elif mode == "always" and s.archetype in TEXT_ARCHETYPES:
            rank = 2
        elif mode == "always" and s.archetype in FRAME_ARCHETYPES:
            rank = 3
        else:
            continue
        ranked.append((rank, s.idx, s))
    ranked.sort(key=lambda t: (t[0], t[1]))
    return [s for _, _, s in ranked[:limit]]


def illustrate(
    outline: DeckOutline, strategy: Strategy, style: dict, client: LLMClient | None, out_dir: Path,
    cfg_mode: str = "auto", limit: int = MAX_IMAGES, parallel: int = PARALLEL,
) -> IllustrateResult:
    """Копия outline с `image.path` у проиллюстрированных слайдов. Ничего не меняет, если генерация недоступна."""
    mode = effective_mode(cfg_mode, strategy)
    res = IllustrateResult(outline.model_copy(deep=True), mode)
    res.items = [ImageItem(s.idx, s.title, "content", s.image.path) for s in outline.slides if s.image and s.image.path]
    todo = candidates(outline, mode, limit)
    if not todo:
        return res
    if client is None or not client.images_enabled:
        res.warnings.append(f"images: {len(todo)} слайдов под иллюстрацию, но генерация недоступна (нет T2I-ключа/модели)")
        return res
    t0 = time.perf_counter()
    skill = load_skill("image_prompter")
    res.skill_version = skill.version
    img_dir = out_dir / "images"
    img_dir.mkdir(parents=True, exist_ok=True)
    style_tags = _style_tags(style)
    by_idx = {s.idx: s for s in res.outline.slides}

    def work(s: OutlineSlide) -> ImageItem:
        return _illustrate_one(s, client, skill, style, style_tags, img_dir)

    with ThreadPoolExecutor(max_workers=max(1, parallel)) as ex:
        items = list(ex.map(work, todo))
    for item in items:
        res.items.append(item)
        if item.path:
            target = by_idx[item.slide_idx]
            target.image = ImageSpec(prompt=item.prompt, path=item.path, alt=(target.image.alt if target.image else "") or target.title)
        else:
            res.warnings.append(f"images: слайд {item.slide_idx} «{item.title[:40]}»: {item.error}")
    res.seconds = round(time.perf_counter() - t0, 3)
    return res


def _illustrate_one(s: OutlineSlide, client: LLMClient, skill: Any, style: dict, style_tags: str, img_dir: Path) -> ImageItem:
    text = " ".join([s.subtitle or ""] + s.bullets + s.paragraphs + ([s.quote] if s.quote else [])).strip()
    hint = (s.image.prompt or s.image.alt) if s.image else ""
    key = _cache_key(s.title, text, hint, style.get("palette", ""), client.image_model)
    meta = img_dir / f"{key}.json"
    if meta.exists():
        try:
            d = json.loads(meta.read_text("utf-8"))
            cached = Path(d.get("path") or "")
            if not cached.is_absolute():  # в кэше — имя файла рядом с .json (кэш переносим вместе с папкой)
                cached = img_dir / cached
            if d.get("path") and cached.exists():
                return ImageItem(s.idx, s.title, "cache", str(cached), d.get("prompt"))
        except (OSError, ValueError):
            pass
    try:
        ans = client.run_skill(
            skill, slide_title=s.title, slide_text=(hint + "\n" + text).strip() or s.title,
            palette=style.get("palette", ""), style_tags=style_tags, aspect="16:9",
        )
        prompt = str(ans.get("prompt") if isinstance(ans, dict) else ans).strip()
        if not prompt:
            raise ValueError("image_prompter вернул пустой промпт")
        path = client.generate_image(prompt, img_dir / f"{key}.png", size=IMAGE_SIZE)
    except Exception as e:  # noqa: BLE001 — картинка не должна ронять колоду
        log.warning("images: слайд %s: %s", s.idx, e)
        return ImageItem(s.idx, s.title, "failed", error=str(e)[:160])
    meta.write_text(json.dumps({"prompt": prompt, "path": Path(path).name}, ensure_ascii=False, indent=1), "utf-8")
    return ImageItem(s.idx, s.title, "generated", str(path), prompt)


def _cache_key(*parts: str) -> str:
    return hashlib.sha1("\n".join(parts).encode("utf-8")).hexdigest()[:16]


def _style_tags(style: dict) -> str:
    """Теги стиля для промпта: светлый/тёмный фон по цвету текста шаблона + нейтральный корпоративный набор."""
    text = str(style.get("text_color") or "212121").lstrip("#")
    try:
        lum = sum(int(text[i : i + 2], 16) for i in (0, 2, 4)) / 3
    except ValueError:
        lum = 0
    bg = "dark background" if lum > 128 else "light background"
    return f"{bg}, corporate, minimal, clean composition, soft gradients, no people close-up"


__all__ = ["MAX_IMAGES", "IllustrateResult", "ImageItem", "candidates", "effective_mode", "illustrate"]
