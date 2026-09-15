"""Общие фикстуры: поиск шаблонов датасета.

Шаблоны не хранятся в репо (`data/templates/*.pptx` в .gitignore). Ищем сначала там,
затем в исходных папках датасета рядом с репо; если файла нет — тест пропускается.
"""

from __future__ import annotations

from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
TEMPLATE_DIRS = [REPO / "data" / "templates", REPO.parent / "Датасет", REPO.parent / "Презентация"]


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
