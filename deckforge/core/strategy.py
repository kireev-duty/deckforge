"""Стратегия вёрстки — типизированный контракт для `strategies/<name>.yaml`: плотность и визуализация данных."""

from __future__ import annotations

from functools import cache
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, model_validator

from deckforge.core.ir import Archetype

STRATEGIES_DIR = Path(__file__).resolve().parents[2] / "strategies"
# объём по умолчанию (ТЗ: 10–15 слайдов или заданный пользователем); диапазоны в strategies/*.yaml заданы для него
DEFAULT_TARGET_SLIDES = 12
SLIDES_MIN, SLIDES_MAX = 3, 25


class SlideRange(BaseModel):
    min: int = Field(ge=SLIDES_MIN)
    max: int = Field(le=SLIDES_MAX)

    @model_validator(mode="after")
    def _ordered(self) -> SlideRange:
        if self.min > self.max:
            raise ValueError(f"target_slides: min {self.min} > max {self.max}")
        return self


class Density(BaseModel):
    max_bullets: int = Field(ge=1, le=8)
    max_words_per_bullet: int = Field(ge=3, le=30)
    max_fill_ratio: float = Field(gt=0, le=1)


class DataVisualization(BaseModel):
    """Во что превращать данные outline: числовой ряд (chart), сравнение (table), ключевые метрики (kpis)."""

    numeric_series: Literal["chart", "table", "kpi"] = "chart"
    comparison: Literal["table", "chart", "two_column"] = "table"
    key_metrics: Literal["kpi", "table", "cards"] = "kpi"


class Strategy(BaseModel):
    name: str
    version: str = "v1"
    target_slides: SlideRange
    density: Density
    archetype_priority: list[Archetype]
    data_visualization: DataVisualization = Field(default_factory=DataVisualization)
    sections: bool = False
    images: Literal["minimal", "preferred", "always"] = "minimal"
    icons: bool = False
    # шаги процесса: diagram — всегда схема SmartArt «Простой процесс»; template — process-образец шаблона,
    # а без него схема; list — process-образец шаблона, а без него нумерованный список/карточки
    process_form: Literal["diagram", "template", "list"] = "list"
    outline_rules: str = ""
    audience_hint: str = ""  # кому и когда нужен этот вариант — «актуальность различий» для UI и compare.md

    @property
    def id(self) -> str:
        return f"{self.name}@{self.version}"

    def priority_of(self, archetype: Archetype) -> int:
        """Позиция в списке приоритетов; чего нет в списке — после всех."""
        try:
            return self.archetype_priority.index(archetype)
        except ValueError:
            return len(self.archetype_priority)

    def for_target(self, target_slides: int | None) -> Strategy:
        """Копия с диапазоном объёма, сдвинутым под заданный пользователем объём.

        Диапазон в YAML описан для DEFAULT_TARGET_SLIDES; сдвиг сохраняет ось «executive короче narrative»,
        границы обрезаются по SLIDES_MIN..SLIDES_MAX. Без объёма (или при объёме по умолчанию) — та же стратегия.
        """
        if target_slides is None or target_slides == DEFAULT_TARGET_SLIDES:
            return self
        shift = target_slides - DEFAULT_TARGET_SLIDES
        lo = min(max(self.target_slides.min + shift, SLIDES_MIN), SLIDES_MAX)
        hi = min(max(self.target_slides.max + shift, SLIDES_MIN), SLIDES_MAX)
        return self.model_copy(update={"target_slides": SlideRange(min=min(lo, hi), max=hi)})


@cache
def load_strategy(name: str, strategies_dir: Path | None = None) -> Strategy:
    path = (strategies_dir or STRATEGIES_DIR) / f"{name}.yaml"
    if not path.exists():
        raise FileNotFoundError(f"стратегия {name!r} не найдена: {path}")
    return Strategy.model_validate(yaml.safe_load(path.read_text("utf-8")))


def list_strategies(strategies_dir: Path | None = None) -> list[str]:
    return sorted(p.stem for p in (strategies_dir or STRATEGIES_DIR).glob("*.yaml"))


__all__ = ["DEFAULT_TARGET_SLIDES", "DataVisualization", "Density", "SlideRange", "Strategy", "list_strategies",
           "load_strategy"]
