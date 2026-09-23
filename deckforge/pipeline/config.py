"""Конфиг прогона (`configs/run.example.yaml`); относительные пути — от корня репозитория."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, field_validator

ROOT = Path(__file__).resolve().parents[2]

Purpose = Literal["feature", "product", "project", "initiative", "report", "other"]


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    return int(raw) if raw.isdigit() else default


class AuditConfig(BaseModel):
    deterministic: bool = True
    contextual: bool = True
    autofix: bool = True


class RunConfig(BaseModel):
    template: Path
    # контент-пакет и тема опциональны: без них бриф выводится из самого шаблона (content/template_brief.py)
    content_pack: Path | None = None
    topic: str = ""  # тема/задача одной строкой — средняя ступень между пакетом и «только шаблон»
    purpose: Purpose = "other"
    audience: str = ""
    language: str = "ru"
    target_slides: int | None = Field(default=None, ge=3, le=25)
    strategies: list[str] = Field(default_factory=lambda: ["executive", "narrative", "visual"])
    images: Literal["off", "auto", "always"] = "auto"
    audit: AuditConfig = Field(default_factory=AuditConfig)
    export: list[Literal["pptx", "pdf", "html"]] = Field(default_factory=lambda: ["pptx"])
    output_dir: Path = Path("out/run")
    seed: int | None = None
    render_png: bool = False  # PNG-превью + contact.png (LibreOffice)
    render_dpi: int = 72
    # бюджет времени на одну колоду (ТЗ: ≤ 5 мин) — считается от старта outline; при нехватке пропускаются
    # картинки, затем VLM-судья (вёрстка, аудит и экспорт — всегда). По умолчанию — из .env.
    time_budget_s: int = Field(default_factory=lambda: _env_int("DECK_TIME_BUDGET_S", 300), ge=10)
    max_parallel_llm: int = Field(default_factory=lambda: _env_int("DECK_MAX_PARALLEL_LLM", 4), ge=1, le=16)

    @field_validator("template", "content_pack", "output_dir", mode="after")
    @classmethod
    def _abs(cls, p: Path | None) -> Path | None:
        if p is None:
            return None
        return p if p.is_absolute() else ROOT / p

    @field_validator("strategies", mode="after")
    @classmethod
    def _non_empty(cls, v: list[str]) -> list[str]:
        if not v:
            raise ValueError("strategies: нужна хотя бы одна стратегия")
        return [s.strip() for s in v]


def load_config(path: str | Path) -> RunConfig:
    path = Path(path)
    data = yaml.safe_load(path.read_text("utf-8")) or {}
    return RunConfig.model_validate(data)


__all__ = ["ROOT", "AuditConfig", "RunConfig", "load_config"]
