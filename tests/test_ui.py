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
