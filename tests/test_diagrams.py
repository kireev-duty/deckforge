"""Схема из автофигур (замена SmartArt): планировщик по process_form, вёрстка, нативные фигуры, чистый аудит."""

from __future__ import annotations

from pathlib import Path

from pptx import Presentation
from pptx.enum.shapes import MSO_SHAPE_TYPE

from deckforge.audit import audit_deck
from deckforge.core.ir import Archetype, DeckOutline, DiagramSpec, OutlineSlide
from deckforge.core.strategy import load_strategy
from deckforge.layout import layout_deck
from deckforge.layout.planner import apply_process_form
from deckforge.parsing.dna import build_dna
from deckforge.render import render_pptx
from deckforge.render.diagrams import caption_size, on_fill

STEPS = ["Аудит текущей инфраструктуры", "Проектирование архитектуры", "Пилотное внедрение",
         "Масштабирование и поддержка"]


def _process(n: int = 4) -> OutlineSlide:
    return OutlineSlide(idx=1, archetype=Archetype.PROCESS, title="Этапы внедрения", steps=STEPS[:n])


def test_process_form_modes() -> None:
    visual, narrative, executive = (load_strategy(n) for n in ("visual", "narrative", "executive"))
    with_process = set(Archetype)
    without_process = set(Archetype) - {Archetype.PROCESS}
    # diagram — всегда; template — только без process-образца; list — никогда
    assert apply_process_form(_process(), visual, with_process).diagram is not None
    assert apply_process_form(_process(), narrative, with_process).diagram is None
    s = apply_process_form(_process(), narrative, without_process)
    assert s.diagram == DiagramSpec(kind="process", items=STEPS) and not s.steps
    assert apply_process_form(_process(), executive, without_process).diagram is None
    # один шаг или слишком много — остаётся списком
    assert apply_process_form(_process(1), visual, with_process).diagram is None
    many = _process()
    many.steps = [f"Шаг {i}" for i in range(9)]
    assert apply_process_form(many, visual, with_process).diagram is None


def test_caption_size_and_text_color() -> None:
    # длинное слово не рвётся: кегль уменьшается, пока слово не влезет в строку
    big = caption_size(["Масштабирование"], w=914400 * 2, h=914400)
    small = caption_size(["Масштабирование"], w=914400, h=914400)
    assert small < big
    assert on_fill("520977") == "FFFFFF" and on_fill("D6E8FF") == "212121"
    assert on_fill("D6E8FF", dark="1A1A1A") == "1A1A1A"  # тёмный текст — цвет палитры шаблона


def test_diagram_rendered_natively_and_audit_clean(template_path, tmp_path: Path) -> None:
    tpl = template_path("VK Tech")
    dna = build_dna(tpl)
    outline = DeckOutline(title="Тест", purpose="product", slides=[
        OutlineSlide(idx=0, archetype=Archetype.TITLE, title="Тест схемы"),
        _process(),
        OutlineSlide(idx=2, archetype=Archetype.CLOSING, title="Спасибо"),
    ])
    res = layout_deck(outline, dna, load_strategy("visual"))
    diagram_slides = [s for s in res.ir.slides if any(e.diagram for e in s.elements)]
    assert len(diagram_slides) == 1
    out = render_pptx(res.ir, tpl, dna.exemplars, tmp_path / "diagram.pptx")

    prs = Presentation(str(out))
    slide = prs.slides[diagram_slides[0].idx]
    groups = [sh for sh in slide.shapes if sh.shape_type == MSO_SHAPE_TYPE.GROUP and sh.name == "deckforge-diagram"]
    assert len(groups) == 1
    kids = list(groups[0].shapes)
    autoshapes = [k for k in kids if k.shape_type == MSO_SHAPE_TYPE.AUTO_SHAPE]
    assert len(autoshapes) == len(STEPS)  # шевроны — нативные автофигуры, не картинка
    assert [k.text_frame.text for k in kids if k.shape_type == MSO_SHAPE_TYPE.TEXT_BOX] == STEPS
    assert not any(k.shape_type == MSO_SHAPE_TYPE.PICTURE for k in kids)

    report = audit_deck(out, dna, res.ir)
    on_slide = [f for f in report.findings if f.slide_idx == diagram_slides[0].idx]
    assert not [f for f in on_slide if f.severity == "error"], on_slide
    # кромки шагов внутри схемы — не сетка шаблона; кегли и цвета — из шаблона
    assert not [f for f in on_slide if f.check_id[:3] in ("L05", "L06", "L02", "L03", "T01", "T02", "T03")], on_slide


def test_steps_across_fixed_cards_penalized(template_path) -> None:
    """ЛЦТ2026 slide11: две белые карточки — фиксированные элементы, рендер их оставляет, хотя у образца
    `card_frames`; три шага на двух колонках легли бы поперёк карточек (holdout visual «Этап 3»)."""
    from deckforge.layout.exemplar_picker import DIAGRAM_GRID_MATCH, DIAGRAM_GRID_MISMATCH, Needs, score_exemplar

    dna = build_dna(template_path("ЛЦТ2026"))
    two_cards = next(e for e in dna.exemplars if e.id == "slide11")
    assert two_cards.card_frames and two_cards.archetype == Archetype.TWO_COLUMN
    visual = load_strategy("visual")
    area = dna.slide_w * dna.slide_h

    def score(n: int) -> float:
        slide = _process(n)
        slide.diagram, slide.steps = DiagramSpec(kind="process", items=STEPS[:n]), []
        return score_exemplar(two_cards, Needs.of(slide), visual, area)

    assert score(3) <= score(2) - DIAGRAM_GRID_MISMATCH - DIAGRAM_GRID_MATCH + 1e-6


def test_caption_color_follows_fill_under_each_caption() -> None:
    from pptx.util import Emu

    from deckforge.core.ir import Box
    from deckforge.render.diagrams import add_diagram

    prs = Presentation()
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    box = Box(x=Emu(457200), y=Emu(914400), w=Emu(8229600), h=Emu(3200400))
    # первая подпись — на тёмном фоне, остальные — на белой карточке
    first_x = box.x
    group = add_diagram(slide, DiagramSpec(kind="process", items=STEPS[:3]), box,
                        {"text_color": "FFFFFF", "palette_text": "1A1A1A"},
                        caption_color=lambda b: "FFFFFF" if b.x == first_x else None)
    caps = [k for k in group.shapes if k.shape_type == MSO_SHAPE_TYPE.TEXT_BOX]
    colors = [str(c.text_frame.paragraphs[0].runs[0].font.color.rgb) for c in caps]
    assert colors == ["FFFFFF", "1A1A1A", "1A1A1A"]
