"""pptx_to_pdf: повтор после сбоя LibreOffice и понятная ошибка. soffice не вызывается."""

import importlib
import subprocess
from pathlib import Path

import pytest

render = importlib.import_module("deckforge.export.render")  # deckforge.export.render — функция, модуль под ней


def _fake_run(outcomes: list, out_dir: Path, stem: str):
    calls: list[list[str]] = []

    def run(cmd, **kwargs):
        calls.append(cmd)
        outcome = outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        (out_dir / f"{stem}.pdf").write_bytes(b"%PDF")
        return subprocess.CompletedProcess(cmd, 0)

    return run, calls


def test_pptx_to_pdf_retries_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pptx = tmp_path / "deck.pptx"
    pptx.write_bytes(b"")
    fail = subprocess.CalledProcessError(1, ["soffice"], stderr=b"Fatal exception")
    run, calls = _fake_run([fail, "ok"], tmp_path / "out", "deck")
    monkeypatch.setattr(render.subprocess, "run", run)
    monkeypatch.setattr(render, "find_soffice", lambda: "soffice")
    monkeypatch.setattr(render, "RETRY_PAUSE_S", 0)
    assert render.pptx_to_pdf(pptx, tmp_path / "out") == tmp_path / "out" / "deck.pdf"
    assert len(calls) == 2


def test_pptx_to_pdf_error_message_has_code_and_stderr(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pptx = tmp_path / "deck.pptx"
    pptx.write_bytes(b"")
    fail = subprocess.CalledProcessError(81, ["soffice"], stderr=b"warn: x\nUser installation could not be completed")
    run, calls = _fake_run([fail, fail], tmp_path / "out", "deck")
    monkeypatch.setattr(render.subprocess, "run", run)
    monkeypatch.setattr(render, "find_soffice", lambda: "soffice")
    monkeypatch.setattr(render, "RETRY_PAUSE_S", 0)
    with pytest.raises(RuntimeError, match="LibreOffice: код 81, User installation could not be completed"):
        render.pptx_to_pdf(pptx, tmp_path / "out")
    assert len(calls) == 2

    run, _ = _fake_run([subprocess.TimeoutExpired(["soffice"], 5)], tmp_path / "out", "deck")
    monkeypatch.setattr(render.subprocess, "run", run)
    with pytest.raises(RuntimeError, match="таймаут 5 с"):
        render.pptx_to_pdf(pptx, tmp_path / "out", timeout=5, retries=0)
