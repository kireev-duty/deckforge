"""llm: единый OpenAI-совместимый клиент и загрузка скиллов из `skills/`."""

from __future__ import annotations

import os
from pathlib import Path

from deckforge.llm.client import LLMCall, LLMClient
from deckforge.llm.skills import Skill, load_skill, registry

ROOT = Path(__file__).resolve().parents[2]


def load_dotenv(path: str | Path | None = None) -> bool:
    """Подхватывает `.env` из корня репо (ключи и модели). Уже заданные переменные не перетирает."""
    p = Path(path) if path else ROOT / ".env"
    if not p.exists():
        return False
    for line in p.read_text("utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
    return True


__all__ = ["LLMCall", "LLMClient", "Skill", "load_dotenv", "load_skill", "registry"]
