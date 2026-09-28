"""pipeline/workspace: шаблоны датасета, пришедшие указателями git-lfs, докачиваются для демо-стенда."""

import tomllib
from pathlib import Path

import pytest

from deckforge.pipeline import workspace

REPO = Path(__file__).resolve().parents[1]
POINTER = b"version https://git-lfs.github.com/spec/v1\noid sha256:00\nsize 123\n"


def _dataset(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    d = tmp_path / "data" / "templates"
    d.mkdir(parents=True)
    (d / "Шаблон VK.pptx").write_bytes(POINTER)
    monkeypatch.setattr(workspace, "ROOT", tmp_path)
    monkeypatch.setattr(workspace, "DATASET_DIRS", [d])
    return d


def test_lfs_pointer_fetched_when_base_set(template_path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    real = template_path("VK Tech").read_bytes()
    d = _dataset(tmp_path, monkeypatch)
    urls: list[str] = []
    monkeypatch.setattr(workspace, "_download", lambda url: urls.append(url) or real)
    monkeypatch.setenv("DECKFORGE_LFS_BASE", "https://media.example/media/o/r/master/")
    store = workspace.TemplateStore(tmp_path / "ui")
    assert urls == ["https://media.example/media/o/r/master/data/templates/%D0%A8%D0%B0%D0%B1%D0%BB%D0%BE%D0%BD%20VK.pptx"]
    assert [e.name for e in store.list()] == ["Шаблон VK"]
    assert (d / "Шаблон VK.pptx").read_bytes() == real
    assert not list(d.glob(".*.part"))


def test_lfs_pointer_skipped_without_base_or_on_bad_download(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    d = _dataset(tmp_path, monkeypatch)
    monkeypatch.delenv("DECKFORGE_LFS_BASE", raising=False)
    assert workspace.TemplateStore(tmp_path / "ui").list() == []

    monkeypatch.setenv("DECKFORGE_LFS_BASE", "https://media.example")
    monkeypatch.setattr(workspace, "_download", lambda url: b"<html>not found</html>")
    assert workspace.TemplateStore(tmp_path / "ui").list() == []
    assert (d / "Шаблон VK.pptx").read_bytes() == POINTER and not list(d.glob(".*.part"))


def test_dataset_dirs_limit_registry(template_path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """UI передаёт свои папки датасета (без holdout); без них — модульный DATASET_DIRS."""
    real = template_path("VK Tech").read_bytes()
    vk, holdout = tmp_path / "templates", tmp_path / "holdout"
    for d, name in ((vk, "VK.pptx"), (holdout, "Holdout.pptx")):
        d.mkdir()
        (d / name).write_bytes(real + name.encode())  # разный sha1 — разные записи реестра
    monkeypatch.setattr(workspace, "DATASET_DIRS", [vk, holdout])
    assert [e.name for e in workspace.TemplateStore(tmp_path / "ui", dataset_dirs=[vk]).list()] == ["VK"]
    assert sorted(e.name for e in workspace.TemplateStore(tmp_path / "ui").list()) == ["Holdout", "VK"]


def test_requirements_match_pyproject() -> None:
    """Streamlit Community Cloud ставит requirements.txt — он не должен отставать от pyproject."""
    deps = tomllib.loads((REPO / "pyproject.toml").read_text("utf-8"))["project"]["dependencies"]
    lines = [ln.split("#")[0].strip() for ln in (REPO / "requirements.txt").read_text("utf-8").splitlines()]
    assert sorted(ln for ln in lines if ln) == sorted(deps)
