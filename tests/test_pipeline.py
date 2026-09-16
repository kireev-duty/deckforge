"""pipeline.run: конфиг → outline (фейковый LLM) → колоды + аудит/автофикс + manifest.json с провенансом.

Контекстуальный аудит в тестах — только на FakeClient с кассетой и только если есть LibreOffice (PNG).
"""

import json
from pathlib import Path

import pytest

from deckforge.pipeline import RunConfig, load_config, run
from tests.conftest import FakeClient, cassette

REPO = Path(__file__).resolve().parents[1]
NO_JUDGE = {"deterministic": True, "contextual": False, "autofix": True}


def _soffice() -> bool:
    from deckforge.export.render import find_soffice

    try:
        find_soffice()
        return True
    except RuntimeError:
        return False


def test_example_config_loads() -> None:
    cfg = load_config(REPO / "configs" / "run.example.yaml")
    assert cfg.template.is_absolute() and cfg.content_pack.is_absolute() and cfg.output_dir.is_absolute()
    assert cfg.strategies == ["executive", "narrative", "visual"] and cfg.purpose == "product"
    assert cfg.audit.autofix is True and cfg.audit.contextual is True and "pdf" in cfg.export


def test_run_with_fake_llm(template_path, tmp_path: Path) -> None:
    cfg = RunConfig(
        template=template_path("VK Tech"), content_pack=REPO / "examples" / "content_pack", purpose="product",
        audience="руководители", target_slides=12, strategies=["executive", "visual"], output_dir=tmp_path / "run",
        audit=NO_JUDGE,
    )
    client = FakeClient(cassette("outline_writer_pulse"))
    messages: list[str] = []
    res = run(cfg, client=client, progress=messages.append)

    assert len(client.calls) == 1 and (tmp_path / "run" / "outline.json").exists()
    assert (tmp_path / "run" / "outline.raw.json").exists()
    assert [d.strategy for d in res.decks] == ["executive", "visual"]
    for d in res.decks:
        assert d.pptx.exists() and d.pptx.stat().st_size > 100_000 and d.ir_json.exists()
        m = json.loads(d.manifest.read_text("utf-8"))
        assert m["skills"] == {"outline_writer": "v1"}
        assert m["models"]["text"] == "fake-text"
        assert m["llm_calls"][0]["skill"] == "outline_writer@v1"
        assert {"parse", "outline", "layout", "render", "audit", "autofix"} <= set(m["timings_s"])
        assert d.audit is not None and d.audit.exists() and m["audit"]["checks_run"] == 24
        assert m["audit"]["errors"] == d.audit_summary["errors"] and "by_check" in m["audit"]
        assert m["audit"]["kind"] == "deterministic" and m["audit"]["contextual"] == 0
        fix = m["audit"]["autofix"]
        assert {"applied", "skipped", "before", "after", "items"} <= set(fix)
        assert fix["after"]["errors"] <= fix["before"]["errors"]
        if fix["applied"]:  # IR на диске — уже исправленный
            ir = json.loads(d.ir_json.read_text("utf-8"))
            assert any("size_pt" in el["style_overrides"] or any(r["size_pt"] for p in el["paragraphs"] for r in p["runs"])
                       for s in ir["slides"] for el in s["elements"])
        assert m["strategy"]["name"] == d.strategy and m["template"]["sha1"]
        assert len(m["plan"]) == len(m["choices"]) >= 8 and d.stats["skipped"] == 0
    run_json = json.loads(res.run_json.read_text("utf-8"))
    assert run_json["timings_s"]["total"] > 0
    assert set(run_json["not_implemented"]) == {"images"}
    assert (tmp_path / "run" / "dna.json").exists() and run_json["decks"][0]["audit"]["checks_run"] == 24
    assert (tmp_path / "run" / "compare.md").read_text("utf-8").count("\n") >= 3
    assert any(m.startswith("outline:") for m in messages)


def test_autofix_removes_our_overflow_errors(template_path, tmp_path: Path) -> None:
    """На VK Tech без фиксов есть L03-ошибки; с фиксами их нет, а «после» ≤ «до»."""
    from deckforge.core.ir import DeckOutline

    outline = DeckOutline.model_validate_json((REPO / "examples" / "content_pack" / "outline.json").read_text("utf-8"))
    base = dict(template=template_path("VK Tech"), content_pack=REPO / "examples" / "content_pack",
                strategies=["narrative"], images="off")
    raw = run(RunConfig(**base, output_dir=tmp_path / "raw", audit={**NO_JUDGE, "autofix": False}), outline=outline)
    fixed = run(RunConfig(**base, output_dir=tmp_path / "fixed", audit=NO_JUDGE), outline=outline)
    raw_report = json.loads(raw.decks[0].audit.read_text("utf-8"))
    fixed_report = json.loads(fixed.decks[0].audit.read_text("utf-8"))
    raw_l03 = [f for f in raw_report["findings"] if f["check_id"] == "L03_text_overflow" and f["severity"] == "error"]
    fixed_l03 = [f for f in fixed_report["findings"] if f["check_id"] == "L03_text_overflow" and f["severity"] == "error"]
    assert raw_l03 and not fixed_l03
    m = json.loads(fixed.decks[0].manifest.read_text("utf-8"))
    assert m["audit"]["autofix"]["applied"] >= len(raw_l03)
    assert m["audit"]["autofix"]["after"]["errors"] < m["audit"]["autofix"]["before"]["errors"]
    assert all({"slide_idx", "check_id", "fix", "before", "after"} <= set(it) for it in m["audit"]["autofix"]["items"]
               if "reason" not in it)
    assert "autofix" not in json.loads(raw.decks[0].manifest.read_text("utf-8"))["timings_s"]


def test_run_with_ready_outline_skips_llm(template_path, tmp_path: Path) -> None:
    from deckforge.core.ir import DeckOutline

    outline = DeckOutline.model_validate_json((REPO / "examples" / "content_pack" / "outline.json").read_text("utf-8"))
    cfg = RunConfig(template=template_path("VK Tech"), content_pack=REPO / "examples" / "content_pack",
                    strategies=["narrative"], output_dir=tmp_path, images="off",
                    audit={"deterministic": False, "contextual": False, "autofix": False})
    res = run(cfg, outline=outline)
    m = json.loads(res.decks[0].manifest.read_text("utf-8"))
    assert m["skills"] == {} and m["llm_calls"] == [] and m["timings_s"]["outline"] == 0
    assert m["audit"] == {} and res.decks[0].audit is None and "audit" not in m["timings_s"]
    assert json.loads(res.run_json.read_text("utf-8"))["not_implemented"] == []


@pytest.mark.skipif(not _soffice(), reason="нужен LibreOffice для PNG")
def test_run_with_contextual_judge(template_path, tmp_path: Path) -> None:
    """Судья вызывается по слайду с PNG, находки C.. попадают в тот же audit.json, версия скилла — в manifest."""
    from deckforge.core.ir import DeckOutline

    outline = DeckOutline.model_validate_json((REPO / "examples" / "content_pack" / "outline.json").read_text("utf-8"))
    cfg = RunConfig(template=template_path("VK Tech"), content_pack=REPO / "examples" / "content_pack",
                    strategies=["executive"], output_dir=tmp_path, images="off", render_dpi=40,
                    audit={"deterministic": True, "contextual": True, "autofix": False})
    client = FakeClient(by_skill={"audit_judge": cassette("audit_judge_pulse")})
    res = run(cfg, client=client, outline=outline)
    d = res.decks[0]
    m = json.loads(d.manifest.read_text("utf-8"))
    n = m["stats"]["slides"]
    assert len(client.calls) == n and all(c.skill == "audit_judge@v1" for c in client.calls)
    assert all(len(imgs) == 1 and imgs[0].suffix == ".png" for imgs in client.images)
    assert client.inputs[0]["prev_slide_title"].startswith("(нет") and client.inputs[0]["language"] == "ru"
    assert any("[brief" in i["source_facts"] or "metrics:" in i["source_facts"] for i in client.inputs)
    report = json.loads(d.audit.read_text("utf-8"))
    assert len(report["checks_run"]) == 24 + 11
    ctx = [f for f in report["findings"] if f["kind"] == "contextual"]
    assert ctx and m["audit"]["contextual"] == len(ctx) and m["audit"]["kind"] == "deterministic+contextual"
    assert m["skills"]["audit_judge"] == "v1" and "audit_contextual" in m["timings_s"] and "png" in m["timings_s"]
    assert len(d.pngs) == n and not (tmp_path / "executive" / "contact.png").exists()
