"""Streamlit UI: страница рендерится без исключений (пайплайн не запускается), sidebar и шаблон на месте."""

from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]


@pytest.mark.filterwarnings("ignore::DeprecationWarning")
def test_app_renders(template_path) -> None:
    from streamlit.testing.v1 import AppTest

    template_path("VK Tech")  # без шаблонов страница пустая — skip
    at = AppTest.from_file(str(REPO / "deckforge" / "ui" / "app.py"), default_timeout=60).run()
    assert not at.exception, at.exception
    assert at.title[0].value.startswith("Цифровой дизайнер")
    assert at.sidebar.selectbox[0].value
    options = at.sidebar.selectbox[0].options  # в UI — только шаблоны VK, holdout ЛЦТ2026 не показывается
    assert any("VK Tech" in o for o in options) and not any("ЛЦТ" in o for o in options)
    assert any("Шаблон:" in h.value for h in at.subheader)
    assert at.button[0].label.startswith("Сгенерировать")
    assert "result" not in at.session_state


def test_overlay_draws_boxes(tmp_path: Path) -> None:
    from PIL import Image

    from deckforge.core.ir import Box, Finding
    from deckforge.ui.overlay import draw_findings

    png = tmp_path / "s.png"
    Image.new("RGB", (200, 100), "white").save(png)
    f = Finding(check_id="L03_text_overflow", kind="deterministic", severity="error", slide_idx=0,
                box=Box(x=0, y=0, w=914400, h=914400), message="x")
    im = draw_findings(png, [f, f.model_copy(update={"box": None})], slide_w=914400 * 2, slide_h=914400)
    assert im.size == (200, 100) and im.getpixel((100, 50)) != (255, 255, 255)  # рамка справа по границе
    assert draw_findings(png, [], 0, 0).size == (200, 100)


def test_apply_selected_fixes_in_ui(template_path, tmp_path: Path, no_fitting) -> None:
    """Колода без подгонки и автофиксов → в таблице есть L03 → выбрать все → «Применить»."""
    from streamlit.testing.v1 import AppTest

    from deckforge.core.ir import DeckOutline
    from deckforge.pipeline import RunConfig, run

    outline = DeckOutline.model_validate_json((REPO / "examples" / "content_pack" / "outline.json").read_text("utf-8"))
    cfg = RunConfig(template=template_path("VK Tech"), content_pack=REPO / "examples" / "content_pack",
                    strategies=["narrative"], output_dir=tmp_path, images="off",
                    audit={"deterministic": True, "contextual": False, "autofix": False})
    res = run(cfg, outline=outline)
    errors_before = res.decks[0].audit_summary["errors"]
    assert errors_before > 0

    at = AppTest.from_file(str(REPO / "deckforge" / "ui" / "app.py"), default_timeout=120)
    at.session_state["result"] = res
    at.session_state["decks"] = {d.strategy: d for d in res.decks}
    at.session_state["run_dir"] = tmp_path
    at.run()
    assert not at.exception, at.exception
    assert [t.label for t in at.tabs][0].startswith("narrative")
    assert ("Ошибок", str(errors_before)) in [(m.label, m.value) for m in at.metric]
    boxes = [c for c in at.checkbox if c.key and c.key.startswith("fix_narrative_")]
    assert boxes and any("L03_text_overflow" in c.label for c in boxes)
    assert all(c.label.startswith(("🟢", "🟠")) for c in boxes)
    apply_btn = [b for b in at.button if b.label.startswith("Применить")][0]
    assert apply_btn.disabled
    [c for c in at.checkbox if c.key == "all_narrative_0"][0].check().run()
    boxes = [c for c in at.checkbox if c.key and c.key.startswith("fix_narrative_")]
    assert all(c.value for c in boxes)
    btn = [b for b in at.button if b.label.startswith("Применить")][0]
    assert btn.label == f"Применить выбранные ({len(boxes)})" and not btn.disabled
    btn.click().run()
    assert not at.exception, at.exception
    metrics = dict((m.label, m.value) for m in at.metric)
    assert int(metrics["Ошибок"]) < errors_before and int(metrics["Автофиксов применено"]) >= 1
    assert metrics["Ошибок до → после"].endswith(f"→ {metrics['Ошибок']}")
    left = [c for c in at.checkbox if c.key and c.key.startswith("fix_narrative_")]
    assert len(left) < len(boxes) and all(not c.value for c in left)


@pytest.mark.filterwarnings("ignore::DeprecationWarning")
def test_public_mode_without_key_runs_ready_outline(template_path, tmp_path: Path,
                                                    monkeypatch: pytest.MonkeyPatch) -> None:
    """Демо-стенд без ключа: LLM-режимы и судья недоступны, «Готовый outline» собирает колоду без LLM."""
    from streamlit.testing.v1 import AppTest

    template_path("VK Tech")
    monkeypatch.setenv("DECKFORGE_PUBLIC", "1")
    monkeypatch.setenv("LLM_API_KEY", "")  # load_dotenv не перетирает заданное — ключ из .env не подхватится
    monkeypatch.setenv("DECKFORGE_UI_ROOT", str(tmp_path))
    at = AppTest.from_file(str(REPO / "deckforge" / "ui" / "app.py"), default_timeout=180).run()
    assert not at.exception, at.exception
    assert any("Демо-стенд" in i.value for i in at.info)
    judge = next(c for c in at.sidebar.checkbox if c.label.startswith("VLM-судья"))
    assert judge.disabled and not judge.value
    images = next(c for c in at.sidebar.checkbox if c.label.startswith("Иллюстрации"))
    assert images.disabled and not images.value
    notes = next(c for c in at.sidebar.checkbox if c.label.startswith("Текст выступления"))
    assert notes.disabled and not notes.value  # без ключа — только заметки из готового outline
    assert next(n for n in at.sidebar.number_input if n.label.startswith("Длительность")).value == 7
    assert at.button[0].disabled and at.warning  # «Только шаблон» без ключа

    at.radio(key="content_mode").set_value("Готовый outline").run()
    assert not at.button[0].disabled
    at.sidebar.multiselect[0].set_value(["executive"])
    for c in at.sidebar.checkbox:
        if c.label in ("PNG-превью", "Экспорт PDF"):
            c.uncheck()
    at.run()
    at.button[0].click().run()
    assert not at.exception, at.exception
    res = at.session_state["result"]
    assert res.content_source == "outline" and [d.strategy for d in res.decks] == ["executive"]
    assert res.decks[0].pptx.exists() and Path(at.session_state["run_dir"]).is_relative_to(tmp_path)


@pytest.mark.filterwarnings("ignore::DeprecationWarning")
def test_repository_mode_inputs(template_path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Режим «Репозиторий»: задача, zip и папка (по умолчанию — сам deckforge); на стенде папок сервера нет."""
    from streamlit.testing.v1 import AppTest

    template_path("VK Tech")
    monkeypatch.delenv("DECKFORGE_PUBLIC", raising=False)
    monkeypatch.setenv("LLM_API_KEY", "sk-test")
    monkeypatch.setenv("DECKFORGE_UI_ROOT", str(tmp_path))
    at = AppTest.from_file(str(REPO / "deckforge" / "ui" / "app.py"), default_timeout=120).run()
    at.radio(key="content_mode").set_value("Репозиторий").run()
    assert not at.exception, at.exception
    labels = [t.label for t in at.text_input]
    assert "Задача презентации" in labels and "или папка на этом компьютере" in labels
    assert next(t for t in at.text_input if t.label.startswith("или папка")).value == str(REPO)
    assert not at.button[0].disabled

    monkeypatch.setenv("DECKFORGE_PUBLIC", "1")
    at = AppTest.from_file(str(REPO / "deckforge" / "ui" / "app.py"), default_timeout=120).run()
    at.radio(key="content_mode").set_value("Репозиторий").run()
    assert not any(t.label.startswith("или папка") for t in at.text_input)
    assert at.button[0].disabled  # без архива запускать нечего


@pytest.mark.filterwarnings("ignore::DeprecationWarning")
def test_hosting_secrets_become_env(template_path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Streamlit Community Cloud: режим стенда и ключи приходят из st.secrets, а не из окружения."""
    import os

    from streamlit.testing.v1 import AppTest

    template_path("VK Tech")
    monkeypatch.delenv("DECKFORGE_PUBLIC", raising=False)
    monkeypatch.delenv("DECKFORGE_TEST_SECRET", raising=False)
    at = AppTest.from_file(str(REPO / "deckforge" / "ui" / "app.py"), default_timeout=60)
    at.secrets["DECKFORGE_PUBLIC"] = "1"
    at.secrets["DECKFORGE_TEST_SECRET"] = "x"
    at.run()
    assert not at.exception, at.exception
    assert any("Демо-стенд" in i.value for i in at.info)
    assert os.environ.pop("DECKFORGE_TEST_SECRET") == "x"
    os.environ.pop("DECKFORGE_PUBLIC", None)  # AppTest в том же процессе — не протекать в другие тесты
