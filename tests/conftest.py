"""Общие фикстуры: шаблоны датасета (LFS; нет файла — skip) и `FakeClient` с ответами из `tests/cassettes/`."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from deckforge.llm.client import LLMCall

REPO = Path(__file__).resolve().parents[1]
TEMPLATE_DIRS = [REPO / "data" / "templates", REPO / "data" / "holdout", REPO / "data" / "wild"]
CASSETTES = REPO / "tests" / "cassettes"


def cassette(name: str) -> dict | list:
    return json.loads((CASSETTES / f"{name}.json").read_text("utf-8"))


class FakeClient:
    """Подменяет LLMClient: позиционные ответы по очереди (последний повторяется), `by_skill` — на конкретный скилл."""

    text_model = "fake-text"
    vision_model = "fake-vision"
    image_model = ""
    images_enabled = False
    base_url = "fake://"

    def __init__(self, *responses: dict | str, by_skill: dict[str, list] | None = None) -> None:
        self.responses = list(responses)
        self.by_skill = {k: list(v) for k, v in (by_skill or {}).items()}
        self.calls: list[LLMCall] = []
        self.inputs: list[dict] = []
        self.images: list[list[Path]] = []

    def run_skill(self, skill, images=None, **inputs):
        self.inputs.append(inputs)
        self.images.append(list(images or []))
        queue = self.by_skill.get(skill.name)
        if queue is not None:
            resp = queue.pop(0) if len(queue) > 1 else queue[0]
        elif self.responses:
            resp = self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]
        else:
            raise RuntimeError(f"FakeClient: нет ответа для скилла {skill.id}")
        if isinstance(resp, Exception):
            self.calls.append(LLMCall(skill.id, self.text_model, 0.01, ok=False, error=str(resp)))
            raise resp
        self.calls.append(LLMCall(skill.id, self.text_model, 0.01, 10, 10))
        return resp


def find_template(name_part: str) -> Path | None:
    for d in TEMPLATE_DIRS:
        if not d.is_dir():
            continue
        for f in sorted(d.glob("*.pptx")):
            if name_part.lower() in f.name.lower():
                return f
    return None


@pytest.fixture
def template_path():
    """Фабрика: template_path("VK Tech") → Path или pytest.skip."""

    def _get(name_part: str) -> Path:
        p = find_template(name_part)
        if p is None:
            pytest.skip(f"шаблон «{name_part}» не найден в {[str(d) for d in TEMPLATE_DIRS]}")
        return p

    return _get


@pytest.fixture
def no_fitting(monkeypatch: pytest.MonkeyPatch) -> None:
    """Выключить подгонку текста в layout — тестам фиксов нужна колода с гарантированными L03."""
    import deckforge.layout.builder as builder

    monkeypatch.setattr(builder, "slot_capacity", lambda slot: None)
    monkeypatch.setattr(builder, "fit_size", lambda text, slot, min_scale=0.7: None)
