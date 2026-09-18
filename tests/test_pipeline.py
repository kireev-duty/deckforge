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
    assert run_json["not_implemented"] == []
    assert all(json.loads(d.manifest.read_text("utf-8"))["images"].get("generated", 0) == 0 for d in res.decks)
    assert (tmp_path / "run" / "dna.json").exists() and run_json["decks"][0]["audit"]["checks_run"] == 24
    assert (tmp_path / "run" / "compare.md").read_text("utf-8").count("\n") >= 3
    assert any(m.startswith("outline:") for m in messages)


def test_autofix_removes_our_overflow_errors(template_path, tmp_path: Path, no_fitting) -> None:
    """Без подгонки текста на VK Tech есть L03-ошибки; с фиксами их нет, а «после» ≤ «до»."""
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


class ImageFakeClient(FakeClient):
    """FakeClient с text-to-image: пишет 1×1 PNG и считает вызовы."""

    images_enabled = True
    image_model = "fake-t2i"

    def generate_image(self, prompt: str, out_path: Path, size: str = "1024x576") -> Path:
        from PIL import Image

        from deckforge.llm.client import LLMCall

        Image.new("RGB", (64, 36), (0, 119, 255)).save(out_path)
        self.calls.append(LLMCall("image_gen", self.image_model, 0.01))
        return out_path


def test_images_generated_cached_and_rendered(template_path, tmp_path: Path) -> None:
    """visual (images: always): иллюстрации генерируются до вёрстки, попадают в picture-слот и в .pptx,
    повторный прогон берёт их из кэша; images: off — ни одного вызова."""
    from deckforge.content.images import MAX_IMAGES

    cfg = RunConfig(template=template_path("VK Tech"), content_pack=REPO / "examples" / "content_pack",
                    strategies=["visual"], output_dir=tmp_path / "img", audit=NO_JUDGE)
    client = ImageFakeClient(by_skill={"image_prompter": [{"prompt": "abstract blue gradient, no text"}]})
    deck = run(cfg, client=client, outline=_outline()).decks[0]
    m = json.loads(deck.manifest.read_text("utf-8"))
    gen = [c for c in client.calls if c.skill == "image_gen"]
    assert 1 <= len(gen) <= MAX_IMAGES and m["images"]["generated"] == len(gen) and m["images"]["mode"] == "always"
    assert m["skills"]["image_prompter"] == "v1" and m["timings_s"]["images"] >= 0
    ir = json.loads(deck.ir_json.read_text("utf-8"))
    with_pic = [el for s in ir["slides"] for el in s["elements"] if el["image_path"]]
    assert with_pic, "сгенерированная картинка должна встать в picture-слот"
    from pptx import Presentation

    prs = Presentation(str(deck.pptx))
    assert sum(1 for sl in prs.slides for sh in sl.shapes if sh.shape_type is not None and "PICTURE" in str(sh.shape_type)
               or sh._element.find(".//{http://schemas.openxmlformats.org/drawingml/2006/main}blip") is not None) >= 1
    # повтор — из кэша, новых генераций нет
    again = run(cfg, client=client, outline=_outline()).decks[0]
    m2 = json.loads(again.manifest.read_text("utf-8"))
    assert m2["images"]["cache"] == len(gen) and m2["images"]["generated"] == 0
    assert len([c for c in client.calls if c.skill == "image_gen"]) == len(gen)
    off = run(cfg.model_copy(update={"images": "off", "output_dir": tmp_path / "off"}), client=client, outline=_outline()).decks[0]
    assert json.loads(off.manifest.read_text("utf-8"))["images"] == {}


def _outline():
    from deckforge.core.ir import DeckOutline

    return DeckOutline.model_validate_json((REPO / "examples" / "content_pack" / "outline.json").read_text("utf-8"))


def test_build_deck_matches_run(template_path, tmp_path: Path) -> None:
    """Этапы по отдельности (parse_template → make_outline → build_deck) дают то же, что run()."""
    from deckforge.pipeline import RunContext, build_deck, make_outline, parse_template

    cfg = RunConfig(template=template_path("VK Tech"), content_pack=REPO / "examples" / "content_pack",
                    strategies=["executive"], output_dir=tmp_path / "steps", images="off", audit=NO_JUDGE)
    parsed = parse_template(cfg.template, cfg.output_dir)
    assert parsed.meta["sha1"] and parsed.dna.exemplars and (cfg.output_dir / "dna.json").exists()
    step = make_outline(cfg, parsed, cfg.output_dir, outline=_outline())
    assert step.path.exists() and step.seconds == 0 and step.skills_used == {}
    ctx = RunContext.prepare(cfg, parsed, step)
    assert ctx.contextual_on is False and ctx.client is None
    deck = build_deck(ctx, "executive")
    whole = run(cfg.model_copy(update={"output_dir": tmp_path / "whole"}), outline=_outline()).decks[0]
    assert deck.pptx.exists() and deck.audit_summary["errors"] == whole.audit_summary["errors"]
    assert set(deck.load_manifest()) == set(whole.load_manifest())
    m = deck.load_manifest()
    assert m["exports"] == {"pptx": "executive.pptx"} and deck.pdf is None
    # пути в manifest — относительно папки прогона (примеры в репо без C:\Users\… машины сборки)
    assert m["outline"] == "outline.json" and m["audit"]["path"] == "executive.audit.json"
    assert json.loads(deck.audit.read_text("utf-8"))["deck_path"] == "executive.pptx"
    assert ":" not in json.loads((cfg.output_dir / "dna.json").read_text("utf-8"))["source_path"]
    assert deck.load_report().deck_path == "executive.pptx" and parsed.dna.source_path == str(parsed.template)
    if Path.cwd().resolve() == REPO.resolve():  # шаблон и контент-пакет — относительно репо (cwd)
        assert m["template"]["path"] == "data/templates/VK Tech шаблон.pptx" and m["content_pack"] == "examples/content_pack"
    assert deck.load_report() is not None and len(deck.load_ir().slides) == deck.stats["slides"]


def test_refine_deck_applies_user_fixes(template_path, tmp_path: Path, no_fitting) -> None:
    """Фиксы по выбору пользователя: без подгонки и автофиксов есть L03-ошибки → выбираем их индексы → после refine их нет."""
    from deckforge.pipeline import refine_deck

    cfg = RunConfig(template=template_path("VK Tech"), content_pack=REPO / "examples" / "content_pack",
                    strategies=["narrative"], output_dir=tmp_path, images="off",
                    audit={**NO_JUDGE, "autofix": False})
    res = run(cfg, outline=_outline())
    deck = res.decks[0]
    report = deck.load_report()
    selected = [i for i, f in enumerate(report.findings) if f.check_id == "L03_text_overflow" and f.severity == "error"]
    assert selected
    before_errors = deck.audit_summary["errors"]
    fixed = refine_deck(deck, res.parsed, selected)
    new_report = fixed.load_report()
    assert not [f for f in new_report.findings if f.check_id == "L03_text_overflow" and f.severity == "error"]
    assert fixed.audit_summary["errors"] < before_errors
    m = fixed.load_manifest()
    fix = m["audit"]["autofix"]
    assert fix["user_applied"] >= 1 and fix["applied"] == fix["user_applied"] and fix["after"]["errors"] < before_errors
    assert "refine" in m["timings_s"] and "contextual_stale" not in m["audit"] and m["audit"]["contextual"] == 0
    # выбор без фиксируемых находок — ничего не меняется
    same = refine_deck(fixed, res.parsed, [])
    assert same.audit_summary["errors"] == fixed.audit_summary["errors"]
    assert same.load_manifest()["audit"]["autofix"]["applied"] == fix["applied"]


@pytest.mark.skipif(not _soffice(), reason="нужен LibreOffice для PDF")
def test_pdf_export(template_path, tmp_path: Path) -> None:
    cfg = RunConfig(template=template_path("VK Tech"), content_pack=REPO / "examples" / "content_pack",
                    strategies=["executive"], output_dir=tmp_path, images="off", export=["pptx", "pdf", "html"],
                    audit={"deterministic": False, "contextual": False, "autofix": False})
    res = run(cfg, outline=_outline())
    d = res.decks[0]
    assert d.pdf is not None and d.pdf.exists() and d.pdf.name == "executive.pdf" and d.pdf.stat().st_size > 10_000
    assert not (tmp_path / "_pdf").exists()
    assert d.html is not None and d.html.exists() and d.html.name == "executive.html"
    m = d.load_manifest()
    assert m["exports"] == {"pptx": "executive.pptx", "pdf": "executive.pdf", "html": "executive.html"}
    assert "export_pdf" in m["timings_s"] and "export_html" in m["timings_s"]
    run_json = json.loads(res.run_json.read_text("utf-8"))
    assert run_json["not_implemented"] == [] and run_json["decks"][0]["exports"]["html"] == "executive.html"
    assert run_json["decks"][0]["manifest"] == "executive.manifest.json" and run_json["outline"] == "outline.json"


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
