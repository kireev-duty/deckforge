"""Загрузка скиллов сервиса из `skills/<name>/v<N>/` через реестр. Промптов в коде нет."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
SKILLS_DIR = ROOT / "skills"
STRATEGIES_DIR = ROOT / "strategies"

_PLACEHOLDER = re.compile(r"\{\{(\w+)\}\}")


@dataclass(frozen=True)
class Skill:
    name: str
    version: str
    model_role: str  # text | vision | image
    temperature: float
    max_tokens: int
    reasoning: bool  # включить «размышления» модели (дороже и медленнее; по умолчанию выкл.)
    inputs: tuple[str, ...]
    system_prompt: str
    user_template: str
    schema: dict | None

    def render(self, **kwargs: str) -> tuple[str, str]:
        """Возвращает (system, user) с подставленными плейсхолдерами. Все inputs обязательны."""
        missing = [k for k in self.inputs if k not in kwargs]
        if missing:
            raise KeyError(f"skill {self.name}@{self.version}: нет входов {missing}")
        user = _PLACEHOLDER.sub(lambda m: str(kwargs.get(m.group(1), m.group(0))), self.user_template)
        system = _PLACEHOLDER.sub(lambda m: str(kwargs.get(m.group(1), m.group(0))), self.system_prompt)
        return system, user

    @property
    def id(self) -> str:
        return f"{self.name}@{self.version}"


def _split_prompt(md: str) -> tuple[str, str]:
    """prompt.md имеет секции `# system` и `# user`."""
    parts = re.split(r"^#\s*(system|user)\s*$", md, flags=re.M)
    sections = {parts[i].strip(): parts[i + 1].strip() for i in range(1, len(parts) - 1, 2)}
    if "system" not in sections:
        raise ValueError("prompt.md: нет секции '# system'")
    return sections["system"], sections.get("user", "")


@lru_cache(maxsize=None)
def registry() -> dict:
    return yaml.safe_load((SKILLS_DIR / "registry.yaml").read_text("utf-8"))


@lru_cache(maxsize=None)
def load_skill(name: str, version: str | None = None) -> Skill:
    entry = registry()["skills"].get(name)
    if entry is None:
        raise KeyError(f"скилл {name!r} не зарегистрирован в skills/registry.yaml")
    version = version or entry["current"]
    d = SKILLS_DIR / name / version
    meta = yaml.safe_load((d / "skill.yaml").read_text("utf-8"))
    system, user = _split_prompt((d / "prompt.md").read_text("utf-8"))
    schema_path = d / "schema.json"
    schema = json.loads(schema_path.read_text("utf-8")) if schema_path.exists() else None
    return Skill(
        name=name,
        version=version,
        model_role=meta.get("model_role", "text"),
        temperature=float(meta.get("temperature", 0.3)),
        max_tokens=int(meta.get("max_tokens", 4000)),
        reasoning=bool(meta.get("reasoning", False)),
        inputs=tuple(meta.get("inputs", [])),
        system_prompt=system,
        user_template=user,
        schema=schema,
    )


@lru_cache(maxsize=None)
def load_strategy(name: str) -> dict:
    return yaml.safe_load((STRATEGIES_DIR / f"{name}.yaml").read_text("utf-8"))


def list_strategies() -> list[str]:
    return sorted(p.stem for p in STRATEGIES_DIR.glob("*.yaml"))
