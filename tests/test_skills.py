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


class _Completions:
    """Подмена chat.completions: считает вызовы, каждый — исключение (ретраи) или ответ."""

    def __init__(self, log: list, fail: bool) -> None:
        self.log, self.fail = log, fail

    def create(self, **kwargs):
        self.log.append(kwargs["model"])
        if self.fail:
            raise RuntimeError("inference down")
        from types import SimpleNamespace

        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content='{"prompt": "ok"}'))], usage=None)


class _OpenAI:
    def __init__(self, log: list, fail: bool, timeouts: list) -> None:
        self.chat = type("Chat", (), {"completions": _Completions(log, fail)})()
        self._timeouts = timeouts

    def with_options(self, timeout: float):
        self._timeouts.append(timeout)
        return self


def test_client_deadline_limits_retries_and_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    """Бюджет времени: после дедлайна попыток нет, до него таймаут запроса урезается до остатка."""
    import time

    from deckforge.llm.client import LLMClient

    log: list = []
    timeouts: list = []
    client = LLMClient(api_key="x", retries=2)
    client._client = _OpenAI(log, fail=True, timeouts=timeouts)
    skill = load_skill("image_prompter")
    inputs = {"slide_title": "t", "slide_text": "x", "palette": "", "style_tags": "", "aspect": "16:9"}
    # дедлайн прошёл — ни одного запроса, ошибка про бюджет
    with pytest.raises(RuntimeError, match="бюджет"):
        client.run_skill(skill, deadline=time.monotonic() - 1, **inputs)
    assert log == [] and client.calls[-1].ok is False
    # без дедлайна — все попытки
    log.clear()
    with pytest.raises(RuntimeError, match="3 attempts"):
        client.run_skill(skill, **inputs)
    assert len(log) == 3 and timeouts == []
    # дедлайн ближе таймаута — запрос с урезанным таймаутом, ретраи прекращаются вместе с дедлайном
    log.clear()
    with pytest.raises(RuntimeError):
        client.run_skill(skill, deadline=time.monotonic() + 2, **inputs)
    assert 1 <= len(log) <= 2 and timeouts and all(t <= 2 for t in timeouts)


def test_client_fork_keeps_own_journal() -> None:
    """Колоды прогона параллельны: у форка свой журнал (manifest колоды), родитель видит все вызовы."""
    from deckforge.llm.client import LLMClient

    parent = LLMClient(api_key="x", retries=0)
    parent._client = _OpenAI([], fail=False, timeouts=[])
    a, b = parent.fork(), parent.fork()
    skill = load_skill("image_prompter")
    inputs = {"slide_title": "t", "slide_text": "x", "palette": "", "style_tags": "", "aspect": "16:9"}
    a.run_skill(skill, **inputs)
    b.run_skill(skill, **inputs)
    b.run_skill(skill, **inputs)
    assert len(a.calls) == 1 and len(b.calls) == 2 and len(parent.calls) == 3
    assert a._client is parent._client and a.text_model == parent.text_model


def test_client_concurrency_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    """LLM_MAX_CONCURRENCY: одновременных запросов не больше лимита (общий у форков); место в очереди
    ждём не дольше дедлайна."""
    import threading
    import time
    from types import SimpleNamespace

    from deckforge.llm.client import LLMClient

    active, peak, lock = [0], [0], threading.Lock()

    class SlowCompletions:
        def create(self, **kwargs):
            with lock:
                active[0] += 1
                peak[0] = max(peak[0], active[0])
            time.sleep(0.1)
            with lock:
                active[0] -= 1
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content='{"prompt": "ok"}'))],
                                   usage=None)

    monkeypatch.setenv("LLM_MAX_CONCURRENCY", "2")
    parent = LLMClient(api_key="x", retries=0)
    assert parent.max_concurrency == 2
    parent._client = SimpleNamespace(chat=SimpleNamespace(completions=SlowCompletions()),
                                     with_options=lambda timeout: parent._client)
    skill = load_skill("image_prompter")
    inputs = {"slide_title": "t", "slide_text": "x", "palette": "", "style_tags": "", "aspect": "16:9"}
    forks = [parent.fork() for _ in range(3)]
    threads = [threading.Thread(target=f.run_skill, args=(skill,), kwargs=inputs) for f in forks for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert peak[0] == 2 and len(parent.calls) == 6 and all(c.ok for c in parent.calls)
    # оба места заняты дольше дедлайна — запрос не уходит, ошибка про бюджет
    parent._sem.acquire()
    parent._sem.acquire()
    try:
        with pytest.raises(RuntimeError, match="бюджет"):
            parent.run_skill(skill, deadline=time.monotonic() + 0.2, **inputs)
    finally:
        parent._sem.release()
        parent._sem.release()
    assert parent.calls[-1].ok is False


def test_strategies_have_required_keys() -> None:
    names = list_strategies()
    assert {"executive", "narrative", "visual"} <= set(names)
    for n in names:
        st = load_strategy(n)
        for key in ("target_slides", "density", "archetype_priority", "data_visualization", "outline_rules"):
            assert key in st, f"{n}: нет {key}"
        assert st["density"]["max_bullets"] <= 6
