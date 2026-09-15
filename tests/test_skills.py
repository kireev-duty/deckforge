"""Целостность реестра скиллов: файлы на месте, схемы валидны, плейсхолдеры совпадают с inputs."""

import json
import re

import pytest

from deckforge.llm.skills import SKILLS_DIR, list_strategies, load_skill, load_strategy, registry

PLACEHOLDER = re.compile(r"\{\{(\w+)\}\}")


def skill_names() -> list[str]:
    return sorted(registry()["skills"])


@pytest.mark.parametrize("name", skill_names())
def test_skill_files_exist(name: str) -> None:
    entry = registry()["skills"][name]
    d = SKILLS_DIR / name / entry["current"]
    for f in ("skill.yaml", "prompt.md", "schema.json"):
        assert (d / f).exists(), f"{name}: нет {f}"


@pytest.mark.parametrize("name", skill_names())
def test_placeholders_match_inputs(name: str) -> None:
    s = load_skill(name)
    used = set(PLACEHOLDER.findall(s.system_prompt + s.user_template))
    declared = set(s.inputs)
    assert used == declared, f"{name}: в промпте {used}, в skill.yaml {declared}"


@pytest.mark.parametrize("name", skill_names())
def test_schema_is_json_object(name: str) -> None:
    s = load_skill(name)
    assert s.schema and s.schema.get("type") == "object"
    json.dumps(s.schema)  # сериализуемо


def test_render_fills_all_placeholders() -> None:
    s = load_skill("slide_filler")
    system, user = s.render(slide_outline="x", slots="y", language="ru")
    assert "{{" not in system and "{{" not in user


def test_render_rejects_missing_inputs() -> None:
    with pytest.raises(KeyError):
        load_skill("slide_filler").render(slide_outline="x")


def test_strategies_have_required_keys() -> None:
    names = list_strategies()
    assert {"executive", "narrative", "visual"} <= set(names)
    for n in names:
        st = load_strategy(n)
        for key in ("target_slides", "density", "archetype_priority", "data_visualization", "outline_rules"):
            assert key in st, f"{n}: нет {key}"
        assert st["density"]["max_bullets"] <= 6
