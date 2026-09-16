"""pipeline.run: конфиг → outline (фейковый LLM) → колоды + manifest.json с провенансом. Без LibreOffice."""

import json
from pathlib import Path

from deckforge.pipeline import RunConfig, load_config, run
from tests.test_outline_writer import FakeClient, cassette

REPO = Path(__file__).resolve().parents[1]


def test_example_config_loads() -> None:
    cfg = load_config(REPO / "configs" / "run.example.yaml")
    assert cfg.template.is_absolute() and cfg.content_pack.is_absolute() and cfg.output_dir.is_absolute()
    assert cfg.strategies == ["executive", "narrative", "visual"] and cfg.purpose == "product"
    assert cfg.audit.autofix is True and "pdf" in cfg.export


def test_run_with_fake_llm(template_path, tmp_path: Path) -> None:
    cfg = RunConfig(
        template=template_path("VK Tech"), content_pack=REPO / "examples" / "content_pack", purpose="product",
        audience="руководители", target_slides=12, strategies=["executive", "visual"], output_dir=tmp_path / "run",
    )
    client = FakeClient(cassette())
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
        assert {"parse", "outline", "layout", "render"} <= set(m["timings_s"])
        assert m["strategy"]["name"] == d.strategy and m["template"]["sha1"]
        assert len(m["plan"]) == len(m["choices"]) >= 8 and d.stats["skipped"] == 0
    run_json = json.loads(res.run_json.read_text("utf-8"))
    assert run_json["timings_s"]["total"] > 0 and set(run_json["not_implemented"]) == {"images", "audit"}
    assert (tmp_path / "run" / "compare.md").read_text("utf-8").count("\n") >= 3
    assert any(m.startswith("outline:") for m in messages)


def test_run_with_ready_outline_skips_llm(template_path, tmp_path: Path) -> None:
    from deckforge.core.ir import DeckOutline

    outline = DeckOutline.model_validate_json((REPO / "examples" / "content_pack" / "outline.json").read_text("utf-8"))
    cfg = RunConfig(template=template_path("VK Tech"), content_pack=REPO / "examples" / "content_pack",
                    strategies=["narrative"], output_dir=tmp_path, images="off",
                    audit={"deterministic": False, "contextual": False, "autofix": False})
    res = run(cfg, outline=outline)
    m = json.loads(res.decks[0].manifest.read_text("utf-8"))
    assert m["skills"] == {} and m["llm_calls"] == [] and m["timings_s"]["outline"] == 0
    assert json.loads(res.run_json.read_text("utf-8"))["not_implemented"] == []
