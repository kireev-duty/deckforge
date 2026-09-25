"""Общие фикстуры: шаблоны датасета (LFS; нет файла — skip) и `FakeClient` с ответами из `tests/cassettes/`."""

from __future__ import annotations

import copy
import json
import threading
from pathlib import Path

import pytest

from deckforge.llm.client import LLMCall

REPO = Path(__file__).resolve().parents[1]
TEMPLATE_DIRS = [REPO / "data" / "templates", REPO / "data" / "holdout", REPO / "data" / "wild"]
CASSETTES = REPO / "tests" / "cassettes"


def cassette(name: str) -> dict | list:
    return json.loads((CASSETTES / f"{name}.json").read_text("utf-8"))


class FakeClient:
    """Подменяет LLMClient: позиционные ответы по очереди (последний повторяется), `by_skill` — на конкретный скилл.

    Как у LLMClient: `fork()` — клиент колоды со своим журналом `calls` (вызов пишется и родителю); очереди
    ответов и `inputs`/`images`/`deadlines` общие и под локом — колоды и судья зовут клиент из разных потоков."""

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
        self.deadlines: list[float | None] = []
        self._lock = threading.RLock()
        self._parent: FakeClient | None = None

    def fork(self) -> FakeClient:
        child = copy.copy(self)
        child.calls = []
        child._parent = self
        return child

    def _log(self, call: LLMCall) -> None:
        with self._lock:
            self.calls.append(call)
        if self._parent is not None:
            self._parent._log(call)

    def run_skill(self, skill, images=None, deadline=None, **inputs):
        with self._lock:
            self.inputs.append(inputs)
            self.images.append(list(images or []))
            self.deadlines.append(deadline)
            queue = self.by_skill.get(skill.name)
            if queue is not None:
                resp = queue.pop(0) if len(queue) > 1 else queue[0]
            elif self.responses:
                resp = self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]
            else:
                raise RuntimeError(f"FakeClient: нет ответа для скилла {skill.id}")
        if callable(resp):  # ответ по входам вызова (fake_notes)
            resp = resp(inputs)
        if isinstance(resp, Exception):
            self._log(LLMCall(skill.id, self.text_model, 0.01, ok=False, error=str(resp)))
            raise resp
        self._log(LLMCall(skill.id, self.text_model, 0.01, 10, 10))
        return resp


def fake_notes(inputs: dict) -> dict:
    """Ответ `speaker_notes` по его входу: по тексту на слайд ровно в бюджет `words` — длительность совпадёт с целью."""
    slides = json.loads(inputs["slides"])
    return {"notes": [{"idx": s["idx"], "text": " ".join(["слово"] * s["words"])} for s in slides]}


def find_template(name_part: str) -> Path | None:
    for d in TEMPLATE_DIRS:
        if not d.is_dir():
            continue
        for f in sorted(d.glob("*.pptx")):
            if name_part.lower() in f.name.lower():
                return f
    return None


SAMPLE_OUTLINE = REPO / "examples" / "content_pack" / "outline.json"


def build_sample_deck(out_dir: Path, strategy: str = "narrative"):
    """Колода VK Tech по синтетическому outline «Пульс» (есть chart, table, KPI) без LLM и LibreOffice.

    Для тестов читателей готовой колоды: `examples/output` собран в режиме «только шаблон», данных в нём нет."""
    from deckforge.core.ir import DeckOutline
    from deckforge.core.strategy import load_strategy
    from deckforge.layout import build_deck_ir
    from deckforge.parsing.dna import build_dna
    from deckforge.render import render_pptx

    pptx = find_template("VK Tech")
    if pptx is None:
        pytest.skip("нет шаблона VK Tech (LFS?)")
    dna = build_dna(pptx)
    outline = DeckOutline.model_validate_json(SAMPLE_OUTLINE.read_text("utf-8"))
    res = build_deck_ir(outline, load_strategy(strategy), dna.exemplars, dna.template_id, dna.slide_w, dna.slide_h,
                        {"accent": "0077FF", "font": "Play", "palette": "0077FF,00AEE8"})
    return render_pptx(res.ir, pptx, dna.exemplars, out_dir / f"{strategy}.pptx"), res.ir


@pytest.fixture(autouse=True)
def no_llm_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """Тесты не ходят в API: пустой ключ, а не удалённый — `load_dotenv` (UI, CLI) не перетирает заданное
    и не подхватит ключ из .env разработчика; без ключа пайплайн не создаёт клиента сам (`RunContext.prepare`)."""
    monkeypatch.setenv("LLM_API_KEY", "")
    monkeypatch.setenv("T2I_API_KEY", "")


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
    from deckforge.layout import builder

    monkeypatch.setattr(builder, "slot_capacity", lambda slot: None)
    monkeypatch.setattr(builder, "fit_size", lambda text, slot, min_scale=0.7: None)
