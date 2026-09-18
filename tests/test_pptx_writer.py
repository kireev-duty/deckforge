"""render/pptx_writer: клонирование образцов, перенос rels, заполнение слотов."""

from __future__ import annotations

from pathlib import Path

from PIL import Image
from pptx import Presentation
from pptx.parts.chart import ChartPart
from pptx.util import Inches, Pt

from deckforge.core.ir import (
    ChartSpec,
    DeckIR,
    Element,
    Paragraph,
    SlideIR,
    SlotKind,
    TextRun,
)
from deckforge.core.ooxml import NS, R, iter_shapes, shape_id, shape_text
from deckforge.parsing.layout_classifier import classify_template
from deckforge.render.pptx_writer import crop_rect, fill_text, render_pptx

# ──────────────────────────── fill_text на синтетическом слайде ────────────────────────────


def _textbox_slide():
    prs = Presentation()
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    tb = slide.shapes.add_textbox(Inches(1), Inches(1), Inches(4), Inches(2))
    p = tb.text_frame.paragraphs[0]
    run = p.add_run()
    run.text = "Образец"
    run.font.bold = True
    run.font.size = Pt(20)
    run.font.color.rgb = __import__("pptx.dml.color", fromlist=["RGBColor"]).RGBColor(0xFF, 0x00, 0x53)
    return prs, tb._element


def test_fill_text_keeps_formatting():
    _, sp = _textbox_slide()
    paras = [
        Paragraph(runs=[TextRun(text="Первый\nвторая строка")], bullet=True),
        Paragraph(runs=[TextRun(text="Второй", italic=True)], level=1, bullet=True),
    ]
    fill_text(sp, paras)
    ps = sp.findall("p:txBody/a:p", NS)
    assert len(ps) == 2
    rpr = ps[0].find("a:r/a:rPr", NS)
    assert rpr.get("b") == "1" and rpr.get("sz") == "2000"
    assert rpr.find("a:solidFill/a:srgbClr", NS).get("val") == "FF0053"
    assert ps[0].find("a:br", NS) is not None and shape_text(sp) == "Первыйвторая строкаВторой"
    assert ps[0].find("a:pPr/a:buChar", NS) is not None
    assert ps[1].find("a:pPr", NS).get("lvl") == "1"
    assert ps[1].find("a:r/a:rPr", NS).get("i") == "1"


def test_fill_text_overrides_and_no_bullet():
    _, sp = _textbox_slide()
    fill_text(sp, [Paragraph(runs=[TextRun(text="x")])], {"font": "Play", "size_pt": 14, "color": "#0077FF"})
    rpr = sp.find("p:txBody/a:p/a:r/a:rPr", NS)
    assert rpr.get("sz") == "1400"
    assert rpr.find("a:latin", NS).get("typeface") == "Play"
    assert rpr.find("a:solidFill/a:srgbClr", NS).get("val") == "0077FF"
    assert sp.find("p:txBody/a:p/a:pPr/a:buChar", NS) is None


def test_crop_rect():
    assert crop_rect(400, 300, 1600, 900) == (0, 12500, 0, 12500)  # 4:3 в 16:9 — режем верх/низ
    assert crop_rect(1600, 900, 400, 400) == (21875, 0, 21875, 0)  # широкая в квадрат — бока
    assert crop_rect(160, 90, 1600, 900) is None


# ──────────────────────────── клонирование на реальном шаблоне ────────────────────────────


def _deck(exemplars, slides):
    return DeckIR(template_id="t", strategy="test", slide_w=0, slide_h=0, slides=slides)


def _no_duplicate_entries(path: Path) -> bool:
    """Новая картинка не должна получить имя части образца — иначе в zip две записи с одним именем."""
    import zipfile

    names = zipfile.ZipFile(path).namelist()
    return len(names) == len(set(names))


def _dangling(prs) -> int:
    n = 0
    for s in prs.slides:
        for el in s.part._element.iter():
            for attr in (R + "embed", R + "id", R + "link"):
                rid = el.get(attr)
                if rid and rid not in s.part.rels:
                    n += 1
    return n


def test_clone_every_exemplar(template_path, tmp_path: Path):
    tpl = template_path("VK Tech")
    src = Presentation(str(tpl))
    n_layouts = sum(len(m.slide_layouts) for m in src.slide_masters)
    n_images = sum(1 for p in src.part.package.iter_parts() if type(p).__name__ == "ImagePart")

    exemplars = [p.to_exemplar() for p in classify_template(tpl)]
    slides = []
    for i, e in enumerate(exemplars):
        title = next((s for s in e.slots if s.kind == SlotKind.TITLE), None)
        elements = []
        if title is not None:
            elements.append(Element(slot_id=title.id, kind=SlotKind.TITLE, box=title.box,
                                    paragraphs=[Paragraph(runs=[TextRun(text=f"Заголовок {i + 1}")])]))
        slides.append(SlideIR(idx=i, exemplar_id=e.id, archetype=e.archetype, elements=elements,
                              outline_ref=i, notes=f"заметка {i + 1}"))
    out = render_pptx(_deck(exemplars, slides), tpl, exemplars, tmp_path / "all.pptx")

    prs = Presentation(str(out))
    assert len(prs.slides) == len(exemplars)
    assert _dangling(prs) == 0
    assert _no_duplicate_entries(out)
    assert sum(len(m.slide_layouts) for m in prs.slide_masters) == n_layouts
    assert sum(1 for p in prs.part.package.iter_parts() if type(p).__name__ == "ImagePart") <= n_images
    assert prs.part._element.find("p:embeddedFontLst", NS) is not None
    for i, (s, e) in enumerate(zip(prs.slides, exemplars)):
        assert s.has_notes_slide and s.notes_slide.notes_text_frame.text == f"заметка {i + 1}"
        texts = {shape_id(sp): shape_text(sp) for sp in iter_shapes(s.part._element.find("p:cSld/p:spTree", NS))}
        title = next((sl for sl in e.slots if sl.kind == SlotKind.TITLE), None)
        if title is not None:
            assert texts[title.id] == f"Заголовок {i + 1}"
        # остальные текстовые слоты очищены от текста-заглушки
        for sl in e.slots:
            if sl.kind in (SlotKind.BODY, SlotKind.LABEL, SlotKind.CAPTION, SlotKind.SUBTITLE, SlotKind.NUMBER):
                assert texts.get(sl.id, "") == "", (e.id, sl.id, texts.get(sl.id))


def test_chart_slide_cloned_twice(template_path, tmp_path: Path):
    tpl = template_path("ЛЦТ2026")
    exemplars = [p.to_exemplar() for p in classify_template(tpl)]
    src = Presentation(str(tpl))
    chart_idx = next(i for i, s in enumerate(src.slides) if any(sh.has_chart for sh in s.shapes))
    e = exemplars[chart_idx]
    chart_slot = next(s for s in e.slots if s.kind == SlotKind.CHART)

    # 1) дважды без ChartSpec → две независимые копии chart-части и её xlsx
    slides = [SlideIR(idx=i, exemplar_id=e.id, archetype=e.archetype, elements=[], outline_ref=i) for i in range(2)]
    prs = Presentation(str(render_pptx(_deck(exemplars, slides), tpl, exemplars, tmp_path / "twice.pptx")))
    charts = [p for p in prs.part.package.iter_parts() if isinstance(p, ChartPart)]
    assert len(charts) == 2 and len({c.partname for c in charts}) == 2
    xlsx = {r.target_part.partname for c in charts for r in c.rels.values() if r.reltype.endswith("/package")}
    assert len(xlsx) == 2
    assert _dangling(prs) == 0

    # 2) с ChartSpec → образцовой диаграммы нет, есть новая нативная; data_labels (autofix I05) → c:dLbls, y_label → ось
    spec = ChartSpec(kind="column", title="Тест", categories=["a", "b"], series={"s1": [1, 2]}, y_label="часы")
    el = Element(slot_id=chart_slot.id, kind=SlotKind.CHART, box=chart_slot.box, chart=spec,
                 style_overrides={"data_labels": True})
    slides = [SlideIR(idx=0, exemplar_id=e.id, archetype=e.archetype, elements=[el], outline_ref=0)]
    prs = Presentation(str(render_pptx(_deck(exemplars, slides), tpl, exemplars, tmp_path / "spec.pptx")))
    charts = [sh for sh in prs.slides[0].shapes if sh.has_chart]
    assert len(charts) == 1 and charts[0].chart.chart_title.text_frame.text == "Тест"
    chart = charts[0].chart
    assert chart.plots[0].has_data_labels and chart.plots[0].data_labels.number_format == "0"
    assert chart.value_axis.has_title and chart.value_axis.axis_title.text_frame.text == "часы"
    assert _dangling(prs) == 0


def test_unfilled_text_placeholder_is_removed(template_path, tmp_path: Path):
    """ЛЦТ2026: образцы на плейсхолдерах. Незаполненный body-ph удаляется, а не остаётся пустым
    (в редакторе — подсказка лейаута, у LibreOffice в PDF — «Образец текста»)."""
    tpl = template_path("ЛЦТ2026")
    exemplars = [p.to_exemplar() for p in classify_template(tpl)]
    src = Presentation(str(tpl))
    with_body = [(i, e) for i, e in enumerate(exemplars)
                 if any(sh.is_placeholder and sh.placeholder_format.type is not None
                        and str(sh.placeholder_format.type).startswith("BODY") for sh in src.slides[i].shapes)
                 and any(s.kind == SlotKind.TITLE for s in e.slots)]
    assert with_body, "в holdout ожидался образец с body-плейсхолдером"
    idx, e = with_body[0]
    title = next(s for s in e.slots if s.kind == SlotKind.TITLE)
    el = Element(slot_id=title.id, kind=SlotKind.TITLE, box=title.box, paragraphs=[Paragraph(runs=[TextRun(text="Т")])])
    slides = [SlideIR(idx=0, exemplar_id=e.id, archetype=e.archetype, elements=[el], outline_ref=0)]
    prs = Presentation(str(render_pptx(_deck(exemplars, slides), tpl, exemplars, tmp_path / "ph.pptx")))
    empty_ph = [sh for sh in prs.slides[0].shapes if sh.is_placeholder and sh.has_text_frame and not sh.text_frame.text.strip()
                and str(sh.placeholder_format.type).startswith("BODY")]
    assert not empty_ph
    assert any(sh.has_text_frame and sh.text_frame.text == "Т" for sh in prs.slides[0].shapes)
    # подсказки плейсхолдеров лейаутов («Образец текста») тоже сняты — LibreOffice рисует их за слайдом;
    # поля лейаута (номер слайда ‹#›, дата) остаются
    for layout in prs.slide_layouts:
        for sh in layout.placeholders:
            ph_type = str(sh.placeholder_format.type)
            if sh.has_text_frame and not any(t in ph_type for t in ("SLIDE_NUMBER", "DATE", "FOOTER")):
                assert not sh.text_frame.text.strip(), f"{layout.name}: {sh.text_frame.text!r}"
    assert any(sh.text_frame.text.strip() for layout in prs.slide_layouts for sh in layout.placeholders
               if sh.has_text_frame and "SLIDE_NUMBER" in str(sh.placeholder_format.type))


def test_empty_placeholders_of_exemplar_are_dropped(template_path, tmp_path: Path):
    """VK Education slide53 (closing): автор шаблона оставил пустые плейсхолдеры QR-картинки и подписи —
    не слоты (fixed / footer). В показе их не видно, в редакторе PowerPoint — подсказки лейаута;
    писатель их убирает, заголовок и номер слайда остаются."""
    tpl = template_path("VK Education")
    exemplars = [p.to_exemplar() for p in classify_template(tpl)]
    src = Presentation(str(tpl))
    def empty_picture_ph(slide) -> bool:
        return any(sh.is_placeholder and "PICTURE" in str(sh.placeholder_format.type)
                   and sh.element.find(".//a:blip", NS) is None for sh in slide.shapes)

    idx, e = next((i, e) for i, e in enumerate(exemplars)
                  if empty_picture_ph(src.slides[i]) and any(s.kind == SlotKind.TITLE for s in e.slots))
    title = next(s for s in e.slots if s.kind == SlotKind.TITLE)
    el = Element(slot_id=title.id, kind=SlotKind.TITLE, box=title.box, paragraphs=[Paragraph(runs=[TextRun(text="Т")])])
    slides = [SlideIR(idx=0, exemplar_id=e.id, archetype=e.archetype, elements=[el], outline_ref=0)]
    prs = Presentation(str(render_pptx(_deck(exemplars, slides), tpl, exemplars, tmp_path / "empty_ph.pptx")))
    for sh in prs.slides[0].shapes:
        if not sh.is_placeholder or "SLIDE_NUMBER" in str(sh.placeholder_format.type):
            continue
        assert sh.has_text_frame and sh.text_frame.text.strip(), f"пустой плейсхолдер остался: {sh.name}"
    assert any(sh.has_text_frame and sh.text_frame.text == "Т" for sh in prs.slides[0].shapes)


def test_picture_fill_and_crop(template_path, tmp_path: Path):
    tpl = template_path("VK Tech")
    exemplars = [p.to_exemplar() for p in classify_template(tpl)]
    e, slot = next((e, s) for e in exemplars for s in e.slots if s.kind == SlotKind.PICTURE)
    img = tmp_path / "pic.png"
    Image.new("RGB", (400, 300), "#0077FF").save(img)
    el = Element(slot_id=slot.id, kind=SlotKind.PICTURE, box=slot.box, image_path=str(img))
    slides = [SlideIR(idx=0, exemplar_id=e.id, archetype=e.archetype, elements=[el], outline_ref=0)]
    out = render_pptx(_deck(exemplars, slides), tpl, exemplars, tmp_path / "pic.pptx")
    assert _no_duplicate_entries(out)
    prs = Presentation(str(out))
    root = prs.slides[0].part._element
    sp = next(s for s in iter_shapes(root.find("p:cSld/p:spTree", NS)) if shape_id(s) == slot.id)
    blip = sp.find(".//a:blipFill/a:blip", NS)
    assert blip is not None and blip.get(R + "embed") in prs.slides[0].part.rels
    expected = crop_rect(400, 300, slot.box.w, slot.box.h)
    sr = sp.find(".//a:blipFill/a:srcRect", NS)
    if expected is None:
        assert sr is None
    else:
        assert sr is not None and int(sr.get("t", 0)) == expected[1] and int(sr.get("l", 0)) == expected[0]
