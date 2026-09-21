"""Схемы запросов/ответов HTTP API."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from deckforge.core.ir import AuditReport

JobStatus = Literal["queued", "running", "done", "error"]


class Health(BaseModel):
    version: str
    models: dict[str, str]  # text / vision / image / base_url — без ключей
    api_key_set: bool
    soffice: bool
    strategies: list[str]


class StrategyInfo(BaseModel):
    name: str
    version: str
    target_slides: dict[str, int]
    density: dict[str, float]
    data_visualization: dict[str, str]
    sections: bool
    images: str
    icons: bool
    archetype_priority: list[str]
    audience_hint: str = ""


class TemplateInfo(BaseModel):
    """Шаблон в реестре: датасет (`builtin`) или загруженный."""

    id: str = Field(description="sha1 файла (12 символов) — ключ для /generate")
    name: str
    builtin: bool
    path: str
    summary: dict | None = None  # ParsedTemplate.summary(), если уже разобран


class DeckInfo(BaseModel):
    strategy: str
    stats: dict
    audit: dict
    warnings: list[str]
    timings_s: dict[str, float]
    files: dict[str, str | list[str]]  # имя → URL для скачивания


class JobInfo(BaseModel):
    id: str
    status: JobStatus
    created: str
    progress: list[str]
    error: str | None = None
    template: dict | None = None
    outline: str | None = None
    decks: list[DeckInfo] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    timings_s: dict[str, float] = Field(default_factory=dict)
    not_implemented: list[str] = Field(default_factory=list)


class JobCreated(BaseModel):
    id: str
    status: JobStatus


class FixRequest(BaseModel):
    findings: list[int] = Field(default_factory=list, description="индексы находок в audit.json, которые применить")
    render_png: bool | None = Field(default=None, description="перерисовать PNG (по умолчанию — если были)")


class FixResponse(BaseModel):
    strategy: str
    applied: int
    skipped: int
    before: dict[str, int]
    after: dict[str, int]
    items: list[dict]
    audit: dict
    contextual_stale: bool = False


class AuditResponse(BaseModel):
    summary: dict
    report: AuditReport
    fix_plan: list[dict]


__all__ = ["AuditResponse", "DeckInfo", "FixRequest", "FixResponse", "Health", "JobCreated", "JobInfo", "JobStatus",
           "StrategyInfo", "TemplateInfo"]
