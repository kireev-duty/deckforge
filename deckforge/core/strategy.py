"""Стратегия вёрстки — типизированный контракт для `strategies/<name>.yaml`.

Стратегия не меняет стиль шаблона (цвета/шрифты фиксированы ТЗ), она задаёт ось
«плотность + способ визуализации данных»: сколько слайдов, сколько текста на слайде,
какие архетипы предпочитать и во что превращать числа (table / chart / kpi).
Потребители: content/ (outline_rules, target_slides) и layout/ (всё остальное).
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, model_validator

from deckforge.core.ir import Archetype

STRATEGIES_DIR = Path(__file__).resolve().parents[2] / "strategies"


class SlideRange(BaseModel):
    min: int = Field(ge=3)
    max: int = Field(le=25)

    @model_validator(mode="after")
    def _ordered(self) -> "SlideRange":
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
    outline_rules: str = ""

    @property
    def id(self) -> str:
        return f"{self.name}@{self.version}"

    def priority_of(self, archetype: Archetype) -> int:
        """Позиция в списке приоритетов; чего нет в списке — после всех."""
        try:
            return self.archetype_priority.index(archetype)
        except ValueError:
            return len(self.archetype_priority)


@lru_cache(maxsize=None)
def load_strategy(name: str, strategies_dir: Path | None = None) -> Strategy:
    path = (strategies_dir or STRATEGIES_DIR) / f"{name}.yaml"
    if not path.exists():
        raise FileNotFoundError(f"стратегия {name!r} не найдена: {path}")
    return Strategy.model_validate(yaml.safe_load(path.read_text("utf-8")))


def list_strategies(strategies_dir: Path | None = None) -> list[str]:
    return sorted(p.stem for p in (strategies_dir or STRATEGIES_DIR).glob("*.yaml"))


__all__ = ["DataVisualization", "Density", "SlideRange", "Strategy", "list_strategies", "load_strategy"]
