"""pipeline.run на фейковом LLM: outline → колоды + аудит/автофикс + manifest.json."""

import json
import time
from pathlib import Path

import pytest

from deckforge.pipeline import RunConfig, load_config, run
from tests.conftest import FakeClient, cassette

REPO = Path(__file__).resolve().parents[1]
NO_JUDGE = {"deterministic": True, "contextual": False, "autofix": True}
# ответ template_brief в режиме «на входе только шаблон»
TEMPLATE_BRIEF = {
    "topic": "VK Tech: корпоративные сервисы для команд",
    "brand": "VK Tech",
    "purpose": "product",
    "audience": "ИТ-руководители крупных компаний",
    "summary": "Показать, из чего состоит платформа и зачем она корпоративным командам.",
    "key_points": ["Команды теряют время на переключение между сервисами",
                   "Единая платформа закрывает коммуникации и документы",
                   "Разворачивается в контуре заказчика"],
    "tone": "светлый корпоративный, синий акцент",
}


def _soffice() -> bool:
    from deckforge.export.render import find_soffice

    try:
        find_soffice()
        return True
    except RuntimeError:
        return False


def test_example_config_loads() -> None:
    """В датасете только шаблоны: конфиги-примеры работают без контент-пакета."""
    cfg = load_config(REPO / "configs" / "run.example.yaml")
    assert cfg.template.is_absolute() and cfg.output_dir.is_absolute() and cfg.content_pack is None
    assert cfg.strategies == ["executive", "narrative", "visual"] and cfg.purpose == "other"
    assert cfg.audit.autofix is True and cfg.audit.contextual is True and "pdf" in cfg.export


def test_final_configs_run_on_templates_alone() -> None:
    """9 витринных колод + holdout собираются из шаблонов; контент один на все — через --outline."""
    finals = sorted((REPO / "configs" / "final").glob("*.yaml"))
    assert [p.stem for p in finals] == ["lct2026_holdout", "vk_education", "vk_tech", "vk_workspace"]
    for path in finals:
        cfg = load_config(path)
        assert cfg.content_pack is None and not cfg.topic, f"{path.name}: контент-пакета в датасете нет"
        assert cfg.template.is_absolute() and cfg.strategies == ["executive", "narrative", "visual"]
        assert cfg.output_dir.name == path.stem and cfg.output_dir.parent.name == "output"


def test_empty_pack_falls_back_to_template_brief(template_path, tmp_path: Path) -> None:
    """Пустой пакет — не ошибка: бриф выводится из шаблона (ТЗ: на входе может быть только шаблон)."""
    (tmp_path / "pack").mkdir()
    cfg = RunConfig(template=template_path("VK Tech"), content_pack=tmp_path / "pack", strategies=["executive"],
                    output_dir=tmp_path / "run", audit=NO_JUDGE)
    client = FakeClient(by_skill={"template_brief": [TEMPLATE_BRIEF], "outline_writer": [cassette("outline_writer_pulse")]})
    res = run(cfg, client=client)

    assert [c.skill for c in client.calls] == ["template_brief@v2", "outline_writer@v3"]
    assert res.content_source == "template"
    brief = (tmp_path / "run" / "brief.md").read_text("utf-8")
    assert TEMPLATE_BRIEF["topic"] in brief and TEMPLATE_BRIEF["key_points"][0] in brief
    m = json.loads(res.decks[0].manifest.read_text("utf-8"))
    assert m["content_source"] == "template" and m["brief"] == "brief.md"
    assert m["skills"]["template_brief"] == "v2" and m["timings_s"]["brief"] >= 0
    assert any("только шаблон" in w for w in res.warnings)
    # слепок шаблона дошёл до модели цельным, а бриф — в outline_writer
    digest = client.inputs[0]["template_digest"]
    assert "Файл шаблона:" in digest and "Слайды-образцы" in digest
    assert TEMPLATE_BRIEF["topic"] in client.inputs[1]["brief"]


def test_topic_only_run(template_path, tmp_path: Path) -> None:
    """Тема одной строкой: LLM зовётся только за outline, бриф — сама тема."""
    cfg = RunConfig(template=template_path("VK Tech"), topic="Пульс команды: как мерить вовлечённость",
                    strategies=["executive"], output_dir=tmp_path / "run", audit=NO_JUDGE)
    client = FakeClient(cassette("outline_writer_pulse"))
    res = run(cfg, client=client)

    assert [c.skill for c in client.calls] == ["outline_writer@v3"]
    assert res.content_source == "topic"
    assert (tmp_path / "run" / "brief.md").read_text("utf-8").startswith("Пульс команды")
    assert client.inputs[0]["brief"].startswith("Пульс команды") and client.inputs[0]["content_pack"] == ""
    m = json.loads(res.decks[0].manifest.read_text("utf-8"))
    assert m["content_source"] == "topic" and m["content_pack"] == ""


def test_ready_outline_carries_brief_to_next_template(template_path, tmp_path: Path) -> None:
    """Один контент на несколько шаблонов: с готовым outline рядом лежащий brief.md едет в прогон."""
    from deckforge.core.ir import DeckOutline

    src = tmp_path / "first"
    src.mkdir()
    outline_path = src / "outline.json"
    outline = DeckOutline.model_validate_json(
        (REPO / "examples" / "content_pack" / "outline.json").read_text("utf-8"))
    outline_path.write_text(outline.model_dump_json(indent=1), "utf-8")
    (src / "brief.md").write_text("# Тема из шаблона\n\nТезис один.\n", "utf-8")

    cfg = RunConfig(template=template_path("VK Tech"), strategies=["executive"], output_dir=tmp_path / "run",
                    audit=NO_JUDGE)
    res = run(cfg, outline=outline, outline_path=outline_path)

    assert res.content_source == "outline"
    assert (tmp_path / "run" / "brief.md").read_text("utf-8").startswith("# Тема из шаблона")
    m = json.loads(res.decks[0].manifest.read_text("utf-8"))
    assert m["content_source"] == "outline" and m["brief"] == "brief.md" and m["content_pack"] == ""


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
        assert m["skills"] == {"outline_writer": "v3"}
        assert m["models"]["text"] == "fake-text"
        assert m["llm_calls"][0]["skill"] == "outline_writer@v3"
        assert {"parse", "outline", "layout", "render", "audit", "autofix"} <= set(m["timings_s"])
        assert d.audit is not None and d.audit.exists() and m["audit"]["checks_run"] == 24
        assert m["audit"]["errors"] == d.audit_summary["errors"] and "by_check" in m["audit"]
        assert m["audit"]["kind"] == "deterministic" and m["audit"]["contextual"] == 0
        fix = m["audit"]["autofix"]
        assert {"applied", "skipped", "before", "after", "items"} <= set(fix)
        assert fix["after"]["errors"] <= fix["before"]["errors"]
        if fix["applied"]:
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
    """Без подгонки текста есть L03; с фиксами их нет."""
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

    def generate_image(self, prompt: str, out_path: Path, size: str = "1024x576", deadline: float | None = None) -> Path:
        from PIL import Image

        from deckforge.llm.client import LLMCall

        Image.new("RGB", (64, 36), (0, 119, 255)).save(out_path)
        self._log(LLMCall("image_gen", self.image_model, 0.01))
        return out_path


def test_images_generated_cached_and_rendered(template_path, tmp_path: Path) -> None:
    """Иллюстрации генерируются до вёрстки и попадают в .pptx; повтор — из кэша; images: off — ни одного вызова."""
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
    # повтор — из кэша
    again = run(cfg, client=client, outline=_outline()).decks[0]
    m2 = json.loads(again.manifest.read_text("utf-8"))
    assert m2["images"]["cache"] == len(gen) and m2["images"]["generated"] == 0
    assert len([c for c in client.calls if c.skill == "image_gen"]) == len(gen)
    off = run(cfg.model_copy(update={"images": "off", "output_dir": tmp_path / "off"}), client=client, outline=_outline()).decks[0]
    assert json.loads(off.manifest.read_text("utf-8"))["images"] == {}


def test_image_generated_once_for_parallel_decks(tmp_path: Path) -> None:
    """narrative и visual иллюстрируют одни слайды одновременно: картинку генерирует одна колода, вторая ждёт
    её по ключу и берёт из кэша — без второй оплаты и без записи в тот же файл."""
    import threading

    from deckforge.content.images import _illustrate_one
    from deckforge.core.ir import Archetype, OutlineSlide
    from deckforge.llm.skills import load_skill

    class SlowImages(ImageFakeClient):
        def generate_image(self, prompt: str, out_path: Path, size: str = "1024x576", deadline: float | None = None):
            time.sleep(0.2)
            return super().generate_image(prompt, out_path, size, deadline)

    client = SlowImages(by_skill={"image_prompter": [{"prompt": "abstract blue gradient"}]})
    slide = OutlineSlide(idx=3, archetype=Archetype.IMAGE_TEXT, title="Платформа", paragraphs=["Один абзац текста"])
    skill = load_skill("image_prompter")
    results = []

    def work(c) -> None:
        results.append(_illustrate_one(slide, c, skill, {"palette": "0077FF"}, "light background", tmp_path))

    threads = [threading.Thread(target=work, args=(client.fork(),)) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(r.source for r in results) == ["cache", "generated"]
    assert results[0].path == results[1].path and Path(results[0].path).exists()
    assert len([c for c in client.calls if c.skill == "image_gen"]) == 1
    assert not list(tmp_path.glob("*.tmp"))


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
    # пути в manifest — относительные
    assert m["outline"] == "outline.json" and m["audit"]["path"] == "executive.audit.json"
    assert json.loads(deck.audit.read_text("utf-8"))["deck_path"] == "executive.pptx"
    assert ":" not in json.loads((cfg.output_dir / "dna.json").read_text("utf-8"))["source_path"]
    assert deck.load_report().deck_path == "executive.pptx" and parsed.dna.source_path == str(parsed.template)
    if Path.cwd().resolve() == REPO.resolve():
        assert m["template"]["path"] == "data/templates/VK Tech шаблон.pptx" and m["content_pack"] == "examples/content_pack"
    assert deck.load_report() is not None and len(deck.load_ir().slides) == deck.stats["slides"]


def test_refine_deck_applies_user_fixes(template_path, tmp_path: Path, no_fitting) -> None:
    """Фиксы по выбору пользователя: выбранные L03 после refine исчезают."""
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
    # выбор без фиксируемых находок ничего не меняет
    same = refine_deck(fixed, res.parsed, [])
    assert same.audit_summary["errors"] == fixed.audit_summary["errors"]
    assert same.load_manifest()["audit"]["autofix"]["applied"] == fix["applied"]


@pytest.mark.skipif(not _soffice(), reason="нужен LibreOffice для PDF")
def test_pdf_export(template_path, tmp_path: Path) -> None:
    """PDF без PNG конвертируется во временную папку колоды; колоды экспортируются параллельно и не мешают друг другу."""
    cfg = RunConfig(template=template_path("VK Tech"), content_pack=REPO / "examples" / "content_pack",
                    strategies=["executive", "narrative"], output_dir=tmp_path, images="off",
                    export=["pptx", "pdf", "html"], max_parallel_decks=2,
                    audit={"deterministic": False, "contextual": False, "autofix": False})
    res = run(cfg, outline=_outline())
    d = res.decks[0]
    assert d.pdf is not None, d.warnings
    assert d.pdf.exists() and d.pdf.name == "executive.pdf" and d.pdf.stat().st_size > 10_000
    assert res.decks[1].pdf is not None, res.decks[1].warnings
    assert res.decks[1].pdf.name == "narrative.pdf" and res.decks[1].pdf.exists()
    assert not list(tmp_path.glob("_pdf*"))
    assert d.html is not None and d.html.exists() and d.html.name == "executive.html"
    m = d.load_manifest()
    assert m["exports"] == {"pptx": "executive.pptx", "pdf": "executive.pdf", "html": "executive.html"}
    assert "export_pdf" in m["timings_s"] and "export_html" in m["timings_s"]
    run_json = json.loads(res.run_json.read_text("utf-8"))
    assert run_json["not_implemented"] == [] and run_json["decks"][0]["exports"]["html"] == "executive.html"
    assert run_json["decks"][0]["manifest"] == "executive.manifest.json" and run_json["outline"] == "outline.json"


@pytest.mark.skipif(not _soffice(), reason="нужен LibreOffice для PNG")
def test_run_with_contextual_judge(template_path, tmp_path: Path) -> None:
    """Судья вызывается по слайду, находки C.. попадают в audit.json, версия скилла — в manifest."""
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
    assert len(client.calls) == n and all(c.skill == "audit_judge@v2" for c in client.calls)
    assert all(len(imgs) == 1 and imgs[0].suffix == ".png" for imgs in client.images)
    assert client.inputs[0]["prev_slide_title"].startswith("(нет") and client.inputs[0]["language"] == "ru"
    assert any("[brief" in i["source_facts"] or "metrics:" in i["source_facts"] for i in client.inputs)
    report = json.loads(d.audit.read_text("utf-8"))
    assert len(report["checks_run"]) == 24 + 11
    ctx = [f for f in report["findings"] if f["kind"] == "contextual"]
    assert ctx and m["audit"]["contextual"] == len(ctx) and m["audit"]["kind"] == "deterministic+contextual"
    assert m["skills"]["audit_judge"] == "v2" and "audit_contextual" in m["timings_s"] and "png" in m["timings_s"]
    assert len(d.pngs) == n and not (tmp_path / "executive" / "contact.png").exists()
    # бюджет времени: судья получил дедлайн, время колоды и бюджет — в manifest и compare.md
    assert all(dl is not None for dl in client.deadlines) and m["time_budget_s"] == 300
    assert 0 < m["timings_s"]["deck_total"] < 300 and not any("бюджет" in w for w in m["warnings"])
    compare = (tmp_path / "compare.md").read_text("utf-8")
    assert "| время, с |" in compare and "**executive** — руководителю" in compare
    assert m["strategy"]["audience_hint"].startswith("руководителю") and d.audience_hint == m["strategy"]["audience_hint"]


def test_time_budget_skips_judge_and_images(template_path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """ТЗ: три варианта ≤ 5 мин независимо от инференса — при исчерпанном бюджете прогона судья и картинки
    пропускаются, вёрстка, аудит и экспорт делаются всегда; бюджет читается из RUN_TIME_BUDGET_S."""
    from deckforge.core.ir import DeckOutline

    monkeypatch.setenv("RUN_TIME_BUDGET_S", "10")
    monkeypatch.setenv("DECK_MAX_PARALLEL_LLM", "2")
    outline = DeckOutline.model_validate_json((REPO / "examples" / "content_pack" / "outline.json").read_text("utf-8"))
    cfg = RunConfig(template=template_path("VK Tech"), content_pack=REPO / "examples" / "content_pack",
                    strategies=["visual"], output_dir=tmp_path, images="auto", render_dpi=40,
                    audit={"deterministic": True, "contextual": True, "autofix": True})
    assert cfg.time_budget_s == 10 and cfg.max_parallel_llm == 2
    client = FakeClient(by_skill={"audit_judge": cassette("audit_judge_pulse")})
    client.images_enabled = True
    res = run(cfg, client=client, outline=outline)
    d = res.decks[0]
    m = json.loads(d.manifest.read_text("utf-8"))
    assert client.calls == []  # ни судьи, ни промптов картинок: до дедлайна меньше резерва
    assert d.pptx.exists() and d.audit is not None and m["audit"]["kind"] == "deterministic"
    assert any("images: пропущено" in w for w in m["warnings"])
    assert any("VLM-судья пропущен" in w for w in m["warnings"])
    assert m["time_budget_s"] == 10 and 0 < m["timings_s"]["deck_build"] <= m["timings_s"]["deck_total"]
    # превышение бюджета не замалчивается: предупреждение есть ровно тогда, когда колода вышла за него
    assert (m["timings_s"]["deck_total"] > 10) == any("бюджет времени прогона превышен" in w for w in m["warnings"])
    assert m["strategy"]["target_slides"] == {"min": 10, "max": 12}
    run_json = json.loads(res.run_json.read_text("utf-8"))
    assert (run_json["timings_s"]["total"] > 10) == any("бюджет времени прогона превышен: " in w
                                                        for w in run_json["warnings"] if not w.startswith("visual"))


def test_run_config_budget_and_strategies(monkeypatch: pytest.MonkeyPatch) -> None:
    """Бюджет — на прогон (прежнее имя DECK_TIME_BUDGET_S читается как запасное); повторы стратегий убираются."""
    monkeypatch.delenv("RUN_TIME_BUDGET_S", raising=False)
    monkeypatch.delenv("RUN_MAX_PARALLEL_DECKS", raising=False)
    monkeypatch.setenv("DECK_TIME_BUDGET_S", "42")
    cfg = RunConfig(template=Path("t.pptx"), strategies=["visual", " executive", "visual"])
    assert cfg.time_budget_s == 42 and cfg.max_parallel_decks == 3
    assert cfg.strategies == ["visual", "executive"]
    monkeypatch.setenv("RUN_TIME_BUDGET_S", "240")
    monkeypatch.setenv("RUN_MAX_PARALLEL_DECKS", "1")
    cfg = RunConfig(template=Path("t.pptx"))
    assert cfg.time_budget_s == 240 and cfg.max_parallel_decks == 1


def test_decks_build_in_parallel(monkeypatch: pytest.MonkeyPatch) -> None:
    """Колоды прогона — в потоках одновременно; результат в порядке конфига, прогресс — только из вызывающего
    потока (Streamlit пишет в статус только из потока скрипта), ошибка колоды — после того, как досчитались остальные."""
    import importlib
    import threading

    run_mod = importlib.import_module("deckforge.pipeline.run")  # в пакете имя run — функция
    names = ["executive", "narrative", "visual"]
    active, peak, finished, lock = [0], [0], [], threading.Lock()
    barrier = threading.Barrier(len(names), timeout=10)

    def fake_build(ctx, name, progress=None):
        with lock:
            active[0] += 1
            peak[0] = max(peak[0], active[0])
        if ctx != "seq":
            barrier.wait()  # не дойдут все три одновременно — BrokenBarrierError
        time.sleep({"executive": 0.3, "narrative": 0.05, "visual": 0.15}[name])
        progress(f"{name}: готово")
        with lock:
            active[0] -= 1
            finished.append(name)
        if ctx == "boom" and name == "narrative":
            raise RuntimeError("narrative упала")
        return name

    monkeypatch.setattr(run_mod, "build_deck", fake_build)
    main = threading.current_thread()
    said: list[tuple[str, bool]] = []

    def say(msg: str) -> None:
        said.append((msg, threading.current_thread() is main))

    assert run_mod._build_decks("ok", names, say, workers=3) == names
    assert peak[0] == 3 and all(on_main for _, on_main in said)
    assert sorted(m for m, _ in said) == sorted(f"{n}: готово" for n in names)

    finished.clear()
    barrier.reset()
    with pytest.raises(RuntimeError, match="narrative упала"):
        run_mod._build_decks("boom", names, say, workers=3)
    assert sorted(finished) == sorted(names)

    peak[0] = 0
    assert run_mod._build_decks("seq", names, say, workers=1) == names and peak[0] == 1


@pytest.mark.skipif(not _soffice(), reason="нужен LibreOffice для PNG")
def test_parallel_decks_share_deadline_and_keep_own_calls(template_path, tmp_path: Path) -> None:
    """Три колоды с судьёй параллельно: дедлайн один на прогон, в manifest колоды — только её вызовы LLM."""
    from deckforge.pipeline.run import EXPORT_RESERVE_S

    cfg = RunConfig(template=template_path("VK Tech"), content_pack=REPO / "examples" / "content_pack",
                    strategies=["executive", "narrative", "visual"], output_dir=tmp_path, images="off", render_dpi=40,
                    time_budget_s=300, max_parallel_decks=3,
                    audit={"deterministic": True, "contextual": True, "autofix": False})
    client = FakeClient(by_skill={"audit_judge": cassette("audit_judge_pulse")})
    messages: list[str] = []
    t0 = time.monotonic()
    res = run(cfg, client=client, outline=_outline(), progress=messages.append)
    assert [d.strategy for d in res.decks] == cfg.strategies
    # дедлайн судьи у всех колод один: старт прогона + бюджет − резерв на экспорт
    assert len(set(client.deadlines)) == 1
    assert abs(client.deadlines[0] - (t0 + 300 - EXPORT_RESERVE_S)) < 5
    n_judge = 0
    for d in res.decks:
        m = d.load_manifest()
        judge = [c for c in m["llm_calls"] if c["skill"].startswith("audit_judge")]
        assert len(judge) == m["stats"]["slides"] and m["audit"]["kind"] == "deterministic+contextual"
        assert m["time_budget_s"] == 300 and 0 < m["timings_s"]["deck_build"] <= m["timings_s"]["deck_total"] < 300
        n_judge += len(judge)
    assert len(client.calls) == n_judge
    run_json = json.loads(res.run_json.read_text("utf-8"))
    assert run_json["config"]["max_parallel_decks"] == 3 and "decks" in run_json["timings_s"]
    assert max(d["timings_s"]["deck_total"] for d in run_json["decks"]) <= run_json["timings_s"]["total"]
    assert any(m.startswith("сборка:") and "параллельно (3 потока)" in m for m in messages)
    assert "параллельно (3 потока)" in (tmp_path / "compare.md").read_text("utf-8")


def test_target_slides_shifts_strategy_ranges(template_path, tmp_path: Path) -> None:
    """Объём, заданный пользователем, доходит до стратегий: 15 → executive 13–14, narrative 15–18."""
    from deckforge.core.ir import DeckOutline

    outline = DeckOutline.model_validate_json((REPO / "examples" / "content_pack" / "outline.json").read_text("utf-8"))
    cfg = RunConfig(template=template_path("VK Tech"), content_pack=REPO / "examples" / "content_pack",
                    strategies=["executive", "narrative"], output_dir=tmp_path, images="off", target_slides=15,
                    audit=NO_JUDGE)
    res = run(cfg, outline=outline)
    by = {d.strategy: json.loads(d.manifest.read_text("utf-8")) for d in res.decks}
    assert by["executive"]["strategy"]["target_slides"] == {"min": 13, "max": 14}
    assert by["narrative"]["strategy"]["target_slides"] == {"min": 15, "max": 18}
    assert 13 <= by["executive"]["stats"]["slides"] <= 14
    assert 15 <= by["narrative"]["stats"]["slides"] <= 18
