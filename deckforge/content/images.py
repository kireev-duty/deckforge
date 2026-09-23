"""Иллюстрации к слайдам: скилл `image_prompter` → T2I-модель.

Какие слайды иллюстрировать — по `Strategy.images` и `RunConfig.images` (off / minimal / preferred / always).
Результат кладётся в `<out_dir>/images/` с кэшем по sha1 входов; ошибка API — слайд остаётся без картинки.
Кэш общий для колод прогона, а они собираются параллельно: одну картинку генерирует одна колода,
остальные ждут её по ключу и берут из кэша.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
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

MAX_IMAGES = 4  # на колоду
PARALLEL = 4
IMAGE_SIZE = "1024x576"  # 16:9; рендер делает center-crop под другой аспект
PICTURE_ARCHETYPES = (Archetype.IMAGE_TEXT, Archetype.IMAGE_FULL)
TEXT_ARCHETYPES = (Archetype.BULLETS, Archetype.CARDS, Archetype.TWO_COLUMN, Archetype.QUOTE)
FRAME_ARCHETYPES = (Archetype.TITLE, Archetype.CLOSING)
# полосатые локи по ключу кэша: одинаковую картинку (narrative и visual иллюстрируют одни слайды) генерирует
# один поток, второй ждёт и читает кэш — не платит дважды и не пишет тот же файл одновременно
_KEY_LOCKS = [threading.Lock() for _ in range(64)]


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
    """Слайды под иллюстрацию по приоритету: картиночные архетипы → просьба outline → текстовые → титул/финал."""
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
    cfg_mode: str = "auto", limit: int = MAX_IMAGES, parallel: int = PARALLEL, deadline: float | None = None,
) -> IllustrateResult:
    """Копия outline с `image.path` у проиллюстрированных слайдов.

    `deadline` (`time.monotonic()`) — бюджет времени прогона: промпт и генерация после него не вызываются,
    слайд остаётся без иллюстрации с предупреждением (кэш читается всегда).
    """
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
        return _illustrate_one(s, client, skill, style, style_tags, img_dir, deadline)

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


def _illustrate_one(s: OutlineSlide, client: LLMClient, skill: Any, style: dict, style_tags: str, img_dir: Path,
                    deadline: float | None = None) -> ImageItem:
    text = " ".join([s.subtitle or ""] + s.bullets + s.paragraphs + ([s.quote] if s.quote else [])).strip()
    hint = (s.image.prompt or s.image.alt) if s.image else ""
    key = _cache_key(s.title, text, hint, style.get("palette", ""), client.image_model)
    meta = img_dir / f"{key}.json"
    hit = _from_cache(s, meta, img_dir)
    if hit is not None:
        return hit
    lock = _KEY_LOCKS[int(key[:8], 16) % len(_KEY_LOCKS)]
    wait_s = -1 if deadline is None else max(0.0, deadline - time.monotonic())
    if not lock.acquire(timeout=wait_s):
        return ImageItem(s.idx, s.title, "failed", error="бюджет времени прогона исчерпан")
    try:
        # пока ждали лок, картинку могла сделать соседняя колода
        return _from_cache(s, meta, img_dir) or _generate(s, client, skill, style, style_tags, text, hint, key,
                                                          img_dir, deadline)
    finally:
        lock.release()


def _from_cache(s: OutlineSlide, meta: Path, img_dir: Path) -> ImageItem | None:
    if not meta.exists():
        return None
    try:
        d = json.loads(meta.read_text("utf-8"))
        cached = Path(d.get("path") or "")
        if not cached.is_absolute():  # в кэше имя файла рядом с .json
            cached = img_dir / cached
        if d.get("path") and cached.exists():
            return ImageItem(s.idx, s.title, "cache", str(cached), d.get("prompt"))
    except (OSError, ValueError):
        pass
    return None


def _generate(s: OutlineSlide, client: LLMClient, skill: Any, style: dict, style_tags: str, text: str, hint: str,
              key: str, img_dir: Path, deadline: float | None) -> ImageItem:
    if deadline is not None and time.monotonic() >= deadline:
        return ImageItem(s.idx, s.title, "failed", error="бюджет времени прогона исчерпан")
    try:
        ans = client.run_skill(
            skill, slide_title=s.title, slide_text=(hint + "\n" + text).strip() or s.title,
            palette=style.get("palette", ""), style_tags=style_tags, aspect="16:9", deadline=deadline,
        )
        prompt = str(ans.get("prompt") if isinstance(ans, dict) else ans).strip()
        if not prompt:
            raise ValueError("image_prompter вернул пустой промпт")
        path = client.generate_image(prompt, img_dir / f"{key}.png", size=IMAGE_SIZE, deadline=deadline)
    except Exception as e:  # noqa: BLE001
        log.warning("images: слайд %s: %s", s.idx, e)
        return ImageItem(s.idx, s.title, "failed", error=str(e)[:160])
    # .json — признак готовой картинки: пишется после .png и атомарно (читатель не увидит половину файла)
    meta = img_dir / f"{key}.json"
    tmp = meta.with_name(f"{meta.name}.{threading.get_ident()}.tmp")
    tmp.write_text(json.dumps({"prompt": prompt, "path": Path(path).name}, ensure_ascii=False, indent=1), "utf-8")
    os.replace(tmp, meta)
    return ImageItem(s.idx, s.title, "generated", str(path), prompt)


def _cache_key(*parts: str) -> str:
    return hashlib.sha1("\n".join(parts).encode("utf-8")).hexdigest()[:16]


def _style_tags(style: dict) -> str:
    """Теги стиля для промпта: светлый/тёмный фон по цвету текста шаблона."""
    text = str(style.get("text_color") or "212121").lstrip("#")
    try:
        lum = sum(int(text[i : i + 2], 16) for i in (0, 2, 4)) / 3
    except ValueError:
        lum = 0
    bg = "dark background" if lum > 128 else "light background"
    return f"{bg}, corporate, minimal, clean composition, soft gradients, no people close-up"


__all__ = ["MAX_IMAGES", "IllustrateResult", "ImageItem", "candidates", "effective_mode", "illustrate"]
