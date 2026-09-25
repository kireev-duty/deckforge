"""Контекст из репозитория (вводная жюри): источники → факты `fact:<n>` → ступень `context` лестницы входа. LLM — FakeClient."""

import shutil
import subprocess
import zipfile
from pathlib import Path

import pytest

from deckforge.content.context import (
    ContextError,
    Fact,
    chunk_sources,
    collect_sources,
    load_context,
    prepare_context,
    repair_digest,
    select_facts,
)
from deckforge.pipeline import RunConfig
from deckforge.pipeline.run import content_for_outline, make_outline, parse_template
from tests.conftest import FakeClient, cassette

HAS_GIT = shutil.which("git") is not None


def _repo(root: Path, git: bool = True) -> Path:
    """Маленький репозиторий: README с бейджами, docs, служебные папки, которые обходить нельзя, лицензия, манифест."""
    root.mkdir(parents=True, exist_ok=True)
    (root / "README.md").write_text(
        '<div align="center">\n\n# Пульс\n\n[![CI](https://x/badge.svg)](https://x)\n</div>\n\n'
        "Сервис опросов для команд. Пилот — 12 команд, 118 участников.\n", "utf-8")
    (root / "docs").mkdir()
    (root / "docs" / "ARCH.md").write_text("# Архитектура\n\nТри слоя: сбор, анализ, отчёты.\n", "utf-8")
    (root / "src" / "pulse").mkdir(parents=True)
    (root / "src" / "pulse" / "README.md").write_text("Модуль анализа ответов.\n", "utf-8")
    (root / "src" / "pulse" / "core.py").write_text("print('hi')\n", "utf-8")
    for skip in ("node_modules/lib", ".venv/lib"):
        (root / skip).mkdir(parents=True)
        (root / skip / "NOTES.md").write_text("чужой текст\n", "utf-8")
    (root / "LICENSE").write_text("MIT License\n\nCopyright…\n", "utf-8")
    (root / "requirements.txt").write_text("httpx>=0.27\n", "utf-8")
    (root / "pyproject.toml").write_text(
        '[project]\nname = "pulse"\nversion = "0.2.0"\ndescription = "опросы команд"\n'
        'dependencies = ["httpx>=0.27", "pydantic>=2"]\n', "utf-8")
    if git and HAS_GIT:
        run = lambda *a: subprocess.run(["git", "-C", str(root), *a], check=True, capture_output=True)
        run("init", "-q")
        run("-c", "user.email=t@t", "-c", "user.name=t", "add", "-A")
        run("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "Первый прогон пилота")
        run("tag", "v0.1.0")
    return root


def test_collect_sources_order_and_skips(tmp_path: Path) -> None:
    """README первым, затем манифест, дерево и история, потом docs и README модулей; служебные папки и лицензия — мимо."""
    src = collect_sources(_repo(tmp_path / "pulse"))
    ids = [f.id for f in src.fragments]
    expected = ["doc:README", "repo:manifest", "repo:tree"] + (["history:git"] if HAS_GIT else []) + \
        ["doc:docs/ARCH", "doc:src/pulse/README"]
    assert ids == expected
    assert src.title == "Пульс"
    readme = src.fragments[0].text
    assert "badge" not in readme and "<div" not in readme and "118 участников" in readme
    manifest = src.fragments[1].text
    assert "название: pulse" in manifest and "httpx, pydantic" in manifest and "лицензия: MIT License" in manifest
    assert "node_modules" not in src.fragments[2].text and ".venv" not in src.fragments[2].text
    if HAS_GIT:
        history = src.fragments[3].text
        assert "коммитов: 1" in history and "v0.1.0" in history and "Первый прогон пилота" in history


def test_zip_with_single_top_folder(tmp_path: Path) -> None:
    """GitHub «Download ZIP»: одна корневая папка, .git нет — факты из документов, история — предупреждением."""
    repo = _repo(tmp_path / "pulse-main", git=False)
    archive = tmp_path / "pulse.zip"
    with zipfile.ZipFile(archive, "w") as z:
        for p in repo.rglob("*"):
            if p.is_file():
                z.write(p, p.relative_to(tmp_path).as_posix())
    src = collect_sources(archive)
    assert src.fragments[0].id == "doc:README" and src.title == "Пульс"
    assert not any("node_modules" in f.id for f in src.fragments)
    assert any("истории git нет" in w for w in src.warnings)


def test_zip_slip_is_rejected(tmp_path: Path) -> None:
    archive = tmp_path / "evil.zip"
    with zipfile.ZipFile(archive, "w") as z:
        z.writestr("../evil.md", "x")
    with pytest.raises(ContextError, match="вне архива"):
        collect_sources(archive)
    assert not (tmp_path.parent / "evil.md").exists()


def test_not_a_folder_or_zip(tmp_path: Path) -> None:
    f = tmp_path / "notes.txt"
    f.write_text("x", "utf-8")
    with pytest.raises(ContextError):
        collect_sources(f)


def test_repair_digest_shapes() -> None:
    """Строки вместо объектов, sources строкой и чужие id, kind не из списка, weight вне 1–3, повторы и обрывки."""
    raw = {"facts": [
        "Пилот прошли 12 команд и 118 участников.",
        {"text": "Три слоя: сбор, анализ, отчёты.", "sources": "doc:docs/ARCH", "kind": "architecture", "weight": 5},
        {"text": "Три слоя: сбор, анализ, отчёты", "sources": ["doc:docs/ARCH"]},  # повтор
        {"fact": "Версия 0.2.0 вышла после пилота.", "sources": ["doc:CHANGELOG"], "kind": "веха", "weight": "x"},
        {"text": "ок"},  # обрывок
        42,
    ]}
    facts = repair_digest(raw, ["doc:README", "doc:docs/ARCH"])
    assert [f.text for f in facts] == ["Пилот прошли 12 команд и 118 участников.", "Три слоя: сбор, анализ, отчёты.",
                                       "Версия 0.2.0 вышла после пилота."]
    assert facts[0].sources == ["doc:README"]  # источник не назван — чанк, из которого факт
    assert facts[1].sources == ["doc:docs/ARCH"] and facts[1].weight == 3 and facts[1].kind == "architecture"
    assert facts[2].sources == ["doc:README"] and facts[2].kind == "other" and facts[2].weight == 2
    assert repair_digest("не JSON", ["doc:README"]) == []


def test_select_facts_keeps_heavy_first_in_source_order() -> None:
    texts = ["Опросы занимают минуту в неделю", "Пилот прошли двенадцать команд", "Отчёт руководителю приходит сам",
             "Данные хранятся внутри контура", "Интеграция с календарём готова"]
    facts = [Fact(text=t, weight=w, sources=["doc:README"]) for t, w in zip(texts, [1, 3, 2, 3, 1])]
    size = max(len(f.to_prompt()) for f in select_facts(facts, {"doc:README": "README.md"}).fragments) + 2
    pack = select_facts(facts, {"doc:README": "README.md"}, limit=3 * size)
    assert [f.id for f in pack.fragments] == ["fact:1", "fact:2", "fact:3"]
    assert [f.text.split(" (")[0] for f in pack.fragments] == texts[1:4]  # вес 3, 2, 3 — в исходном порядке
    assert all("(источник: README.md)" in f.text for f in pack.fragments)


def test_select_facts_drops_rephrased_duplicates() -> None:
    """README и docs повторяют одно и то же другими словами — повтор места в лимите не занимает, остаётся весомый."""
    facts = [Fact(text="В репозитории 12 готовых колод, сгенерированных на 4 шаблонах тремя стратегиями", weight=2),
             Fact(text="В репозитории хранится 12 финальных колод, сгенерированных из 4 шаблонов", weight=3),
             Fact(text="Аудит включает 26 детерминированных проверок и VLM-судью", weight=2)]
    pack = select_facts(facts, {})
    assert [f.text.split(" (")[0] for f in pack.fragments] == [facts[1].text, facts[2].text]


def test_chunks_split_long_docs_and_drop_tail(tmp_path: Path) -> None:
    src = collect_sources(_repo(tmp_path / "pulse", git=False))
    src.fragments[0].text = "\n\n".join(f"Абзац {i}. " + "слово " * 150 for i in range(40))  # ≈ 37 тыс. символов
    chunks, dropped = chunk_sources(src, size=8000, max_chunks=3)
    assert len(chunks) == 3 and all(len(text) <= 8000 for text, _ in chunks)
    assert "[doc:README] (часть 1/" in chunks[0][0] and chunks[0][1] == ["doc:README"]
    assert "doc:docs/ARCH" in dropped


def _digest_answer(inputs: dict) -> dict:
    ids = inputs["source_ids"].split(", ")
    return {"facts": [{"text": f"Факт из {i}: пилот прошли 12 команд и 118 участников за 4 недели.",
                       "sources": [i], "kind": "metric", "weight": 3} for i in ids]}


def test_prepare_context_caches_by_sources(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "pulse", git=False)
    client = FakeClient(by_skill={"context_digest": [_digest_answer]})
    digest, path, cached = prepare_context(repo, client, cache_dir=tmp_path / "cache")
    assert not cached and path.is_file() and digest.calls == len(client.calls) >= 1
    assert digest.pack.fragments[0].id == "fact:1" and digest.skill == "context_digest@v1"
    assert load_context(path).pack == digest.pack
    _, path2, cached2 = prepare_context(repo, client, cache_dir=tmp_path / "cache")
    assert cached2 and path2 == path and len(client.calls) == digest.calls  # второй раз — без модели
    (repo / "docs" / "ARCH.md").write_text("# Архитектура\n\nЧетыре слоя.\n", "utf-8")
    _, path3, cached3 = prepare_context(repo, client, cache_dir=tmp_path / "cache")
    assert not cached3 and path3 != path  # источники изменились — новый ключ


def _context_json(tmp_path: Path) -> Path:
    client = FakeClient(by_skill={"context_digest": [_digest_answer]})
    _, path, _ = prepare_context(_repo(tmp_path / "pulse", git=False), client, cache_dir=tmp_path / "cache")
    return path


def test_context_is_first_step_of_the_ladder(template_path, tmp_path: Path) -> None:
    """Контекст + задача: бриф — задача, фрагменты — факты; контент-пакет, если задан, добавляется рядом."""
    ctx = _context_json(tmp_path)
    cfg = RunConfig(template=template_path("VK Tech"), context=ctx, topic="Питч «Пульса» на 7 минут",
                    content_pack=Path(__file__).resolve().parents[1] / "examples" / "content_pack",
                    output_dir=tmp_path / "run")
    parsed = parse_template(cfg.template)
    (tmp_path / "run").mkdir()
    step = content_for_outline(cfg, parsed, tmp_path / "run", FakeClient())
    assert step.source == "context" and step.skills_used == {"context_digest": "v1"}
    ids = [f.id for f in step.pack.fragments]
    assert ids[0] == "brief" and "fact:1" in ids and "m:pilot" in ids and "doc:product" in ids
    assert step.pack.brief.startswith("# Питч «Пульса» на 7 минут")
    assert (tmp_path / "run" / "brief.md").read_text("utf-8").startswith("# Питч")

    # без задачи — бриф по умолчанию: презентация о проекте по его материалам
    bare = content_for_outline(cfg.model_copy(update={"topic": "", "content_pack": None}), parsed,
                               tmp_path / "run", FakeClient())
    assert bare.pack.brief.startswith("# Пульс") and "по материалам репозитория" in bare.pack.brief

    # битый context.json — спуск по лестнице, а не падение
    broken = tmp_path / "broken.json"
    broken.write_text("{}", "utf-8")
    down = content_for_outline(cfg.model_copy(update={"context": broken}), parsed, tmp_path / "run", FakeClient())
    assert down.source == "pack" and any("контекст не прочитан" in w for w in down.warnings)


def test_context_numbers_survive_only_if_in_facts(template_path, tmp_path: Path) -> None:
    """Числа колоды — только из фактов: KPI пилота (12, 118, 4) остаются, оценки 3,4 / 4,7 / 42% — нет."""
    ctx = _context_json(tmp_path)
    cfg = RunConfig(template=template_path("VK Tech"), context=ctx, output_dir=tmp_path / "run")
    parsed = parse_template(cfg.template)
    client = FakeClient(by_skill={"outline_writer": [cassette("outline_writer_pulse")]})
    step = make_outline(cfg, parsed, tmp_path / "run", client)
    assert step.content_source == "context"
    kpis = [[k.value for k in s.kpis] for s in step.outline.slides if s.kpis]
    assert ["12", "118", "4"] in kpis and not any("42%" in v for vals in kpis for v in vals)
    assert any("цифры KPI не из источника" in w for w in step.warnings)
