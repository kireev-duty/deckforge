"""Вёрстка: границы слайда, наложения, переполнение текста, сетка, поля, пропорции картинок (L01–L07).

Геометрия, унаследованная от образца, — дизайн шаблона: warning с `in_exemplar`, а не error.
"""

from __future__ import annotations

from deckforge.audit.context import STEP, AuditContext, ShapeRec, SlideCtx
from deckforge.audit.deterministic.base import finding
from deckforge.audit.text_metrics import LINE_HEIGHT, TextMeasurer
from deckforge.core.ir import Box, Finding, Severity, Slot, SlotKind
from deckforge.core.units import EMU_PER_PT

TITLE_LINES = 2  # = layout/fitting.TITLE_LINES
OUT_OF_BOUNDS_TOL = 0.01  # доля стороны слайда
OVERLAP_MIN = 0.05  # доля площади меньшего блока
NESTED_MIN = 0.90  # выше — вложенность, не наложение
OVERFLOW_TOL = 0.10  # точность метрик прокси-шрифта
OVERFLOW_MIN_PT = 2.0
REACH_SLACK = 0.25  # базовая линия — выше низа строки на столько кеглей (= layout_classifier.LINE_SLACK)
MIN_PICTURE_PX = 4  # картинки-линии пропорций не имеют
GRID_TOL = 91440  # 0.1"
FULL_BLEED = 0.6
ASPECT_TOL = 0.03


# ──────────────────────────── общее ────────────────────────────


def _in_exemplar(slide: SlideCtx, shape: ShapeRec) -> bool:
    """Бокс фигуры совпадает с боксом слота образца — геометрия унаследована (по боксу, т.к. id меняется).

    Фигуры схемы SmartArt (Element.diagram) — тоже: внешний бокс схемы — контентная область образца,
    а кромки шагов внутри неё задаёт ритм схемы, а не сетка шаблона."""
    if slide.exemplar is None:
        return False
    if slide.ir is not None and any(el.diagram is not None and _inside(shape.box, el.box) for el in slide.ir.elements):
        return True
    return any(_same_box(slot.box, shape.box) or _grown_with_plate(slot, shape.box) for slot in slide.exemplar.slots)


def _grown_with_plate(slot: Slot, box: Box) -> bool:
    """Бокс заголовка, растянутый рендером вместе с плашкой-«чипом» (Slot.plate_id): x, y, h — как у слота."""
    return (slot.plate_id is not None and abs(slot.box.x - box.x) <= STEP and abs(slot.box.y - box.y) <= STEP
            and abs(slot.box.h - box.h) <= STEP and slot.box.w - STEP <= box.w <= slot.box.w + (slot.plate_max_w or 0))


def _inside(inner: Box, outer: Box, tol: int = STEP) -> bool:
    return (inner.x >= outer.x - tol and inner.y >= outer.y - tol
            and inner.x2 <= outer.x2 + tol and inner.y2 <= outer.y2 + tol)


def _same_box(a: Box, b: Box, tol: int = STEP) -> bool:
    return abs(a.x - b.x) <= tol and abs(a.y - b.y) <= tol and abs(a.w - b.w) <= tol and abs(a.h - b.h) <= tol


def _intersection(a: Box, b: Box) -> int:
    w = min(a.x2, b.x2) - max(a.x, b.x)
    h = min(a.y2, b.y2) - max(a.y, b.y)
    return w * h if w > 0 and h > 0 else 0


def _outside(shape: ShapeRec, ctx: AuditContext) -> dict[str, int]:
    tol_x, tol_y = int(ctx.slide_w * OUT_OF_BOUNDS_TOL), int(ctx.slide_h * OUT_OF_BOUNDS_TOL)
    b = shape.box
    out = {"left": -b.x, "top": -b.y, "right": b.x2 - ctx.slide_w, "bottom": b.y2 - ctx.slide_h}
    return {k: v for k, v in out.items() if v > (tol_x if k in ("left", "right") else tol_y)}


def _severity(slide: SlideCtx, shape: ShapeRec) -> tuple[Severity, int]:
    inherited = _in_exemplar(slide, shape)
    return (Severity.WARNING if inherited else Severity.ERROR), int(inherited)


def _check_bounds(ctx: AuditContext, check_id: str, text_shapes: bool) -> list[Finding]:
    out: list[Finding] = []
    for slide in ctx.slides:
        for sh in slide.content():
            if sh.has_text != text_shapes:
                continue
            over = _outside(sh, ctx)
            if not over:
                continue
            sev, inherited = _severity(slide, sh)
            what = "Текст" if text_shapes else {"pic": "Картинка", "graphicFrame": "Объект"}.get(sh.tag, "Фигура")
            side = ", ".join(f"{k} на {round(v / EMU_PER_PT)} pt" for k, v in over.items())
            out.append(finding(check_id, slide, sev, f"{what} выходит за край слайда ({side})", sh,
                               autofix="clamp_to_slide", in_exemplar=inherited, **{f"over_{k}": v for k, v in over.items()}))
    return out


# ──────────────────────────── L01 / L04 ────────────────────────────


def check_L01(ctx: AuditContext) -> list[Finding]:
    """Картинка / диаграмма / таблица вышла за границы слайда."""
    return _check_bounds(ctx, "L01_out_of_bounds", text_shapes=False)


def check_L04(ctx: AuditContext) -> list[Finding]:
    """Текстовый блок пересекает край слайда — текст обрезан."""
    return _check_bounds(ctx, "L04_text_clipped", text_shapes=True)


# ──────────────────────────── L02 ────────────────────────────


def check_L02(ctx: AuditContext) -> list[Finding]:
    """Два содержательных блока наложились друг на друга."""
    out: list[Finding] = []
    for slide in ctx.slides:
        content = slide.content()
        for i, a in enumerate(content):
            for b in content[i + 1:]:
                inter = _intersection(a.box, b.box)
                if inter <= 0:
                    continue
                smaller = min(a.area, b.area) or 1
                ratio = inter / smaller
                if ratio <= OVERLAP_MIN:
                    continue
                if ratio >= NESTED_MIN and not (a.has_text and b.has_text):
                    continue  # вложенность, а не наложение
                # боксы пересекались ещё в образце — дизайн шаблона, пока на чужой блок не легли строки нашего
                # текста, которых в образце не было (заголовок в образце в одну строку, у нас — в две)
                in_ex = _pair_in_exemplar(slide, a, b)
                reached = in_ex and (_text_reaches(a, b, _exemplar_lines(ctx, slide, a))
                                     or _text_reaches(b, a, _exemplar_lines(ctx, slide, b)))
                sev = Severity.WARNING if in_ex and not reached else Severity.ERROR
                box = Box(x=max(a.box.x, b.box.x), y=max(a.box.y, b.box.y),
                          w=min(a.box.x2, b.box.x2) - max(a.box.x, b.box.x), h=min(a.box.y2, b.box.y2) - max(a.box.y, b.box.y))
                msg = f"Блоки {_label(a)} и {_label(b)} наложились ({ratio:.0%} площади меньшего)"
                out.append(finding("L02_overlap", slide, sev, msg + (" — текст зашёл на блок" if reached else ""),
                                   a, autofix="reflow_vertical", box=box, other_id=b.id, ratio=round(ratio, 3),
                                   in_exemplar=int(in_ex and not reached), text_reached=int(reached)))
    return out


def _exemplar_lines(ctx: AuditContext, slide: SlideCtx, sh: ShapeRec) -> int:
    """Сколько строк было у текста этой фигуры в образце (0 — образца нет или текста не было)."""
    if slide.exemplar is None:
        return 0
    rec = ctx.exemplar_records(slide.exemplar).get(sh.id)
    return len(_line_boxes(rec)) if rec is not None else 0


def _text_reaches(a: ShapeRec, b: ShapeRec, skip: int = 0) -> bool:
    """Строка нашего текста a — кроме первых skip, которые были и в образце, — легла на блок b: на его рамку,
    если он виден (заливка, обводка, картинка, данные), иначе — на его строки: пустая часть текстового бокса
    не блок. Видимый b, внутри которого лежит a (подпись в карточке), — контейнер: тогда тоже только строки.

    Заголовок WorkSpace, ушедший второй строкой под колонку карточек: боксы пересекались уже в образце
    (in_exemplar), но там заголовок был в одну строку. Крупная цифра KPI, чья запятая заходит на подпись,
    как и в образце, — дизайн шаблона."""
    if not a.is_ours:
        return False
    solid = (bool(b.fill) and b.fill_alpha > 0.05) or (bool(b.line) and b.line_w_emu > 0) or b.is_picture \
        or b.chart is not None or b.table is not None
    nested = _intersection(a.box, b.box) >= NESTED_MIN * max(1, a.area)
    targets = [b.box] if solid and not nested else _line_boxes(b)
    return any(_intersection(line, tb) > 0 for line in _line_boxes(a)[skip:] for tb in targets)


def _line_boxes(sh: ShapeRec) -> list[Box]:
    """Строки текста фигуры: по горизонтали — ширина строки, по вертикали — от верха строки до базовой линии.

    Метрики — прокси-шрифт (шире настоящего), поэтому край строки с допуском OVERFLOW_TOL, а выносные
    элементы не считаются (REACH_SLACK) — касание блока низом букв не находка."""
    if not (sh.has_text and sh.tag == "sp" and sh.wrap):
        return []
    l, t, r, bot = sh.insets
    width_pt = (sh.box.w - l - r) / EMU_PER_PT
    if width_pt <= 0:
        return []
    # (верх, базовая линия — pt от верха текста; отступ слева и ширина — pt; выравнивание)
    lines: list[tuple[float, float, float, float, str]] = []
    y = 0.0
    for p in sh.paragraphs:
        run = max(p.runs, key=lambda x: len(x.text), default=None)
        if run is None or not p.text.strip():
            continue
        m = TextMeasurer(run.font, run.bold)
        step = (p.line_spacing or LINE_HEIGHT) * run.size_pt
        indent = p.indent_emu / EMU_PER_PT
        y += p.space_before_pt
        for line in m.wrap_lines(p.text, run.size_pt, width_pt - indent):
            lines.append((y, y + step - REACH_SLACK * run.size_pt, indent, m.width(line, run.size_pt), p.align))
            y += step
        y += p.space_after_pt
    inner_h = (sh.box.h - t - bot) / EMU_PER_PT
    shift_y = {"ctr": (inner_h - y) / 2, "b": inner_h - y}.get(sh.anchor, 0.0)
    y0, x0 = sh.box.y + t + shift_y * EMU_PER_PT, sh.box.x + l
    out: list[Box] = []
    for top, base, indent, w, align in lines:
        free = width_pt - indent - w
        x = x0 + (indent + {"ctr": free / 2, "r": free}.get(align, 0.0)) * EMU_PER_PT
        out.append(Box(x=int(x), y=int(y0 + top * EMU_PER_PT), w=int(w * (1 - OVERFLOW_TOL) * EMU_PER_PT),
                       h=max(1, int((base - top - OVERFLOW_MIN_PT) * EMU_PER_PT))))
    return out


def _pair_in_exemplar(slide: SlideCtx, a: ShapeRec, b: ShapeRec) -> bool:
    sa, sb = slide.slot_of(a.id), slide.slot_of(b.id)
    if sa is None or sb is None:
        return False
    inter = _intersection(sa.box, sb.box)
    smaller = min(sa.box.w * sa.box.h, sb.box.w * sb.box.h) or 1
    return inter / smaller > OVERLAP_MIN


def _label(sh: ShapeRec) -> str:
    if sh.has_text:
        t = sh.text.replace("\n", " ")
        return f"«{t[:30]}…»" if len(t) > 30 else f"«{t}»"
    if sh.chart:
        return "диаграмма"
    if sh.table:
        return "таблица"
    if sh.is_picture:
        return "картинка"
    return f"фигура {sh.id}"


# ──────────────────────────── L03 ────────────────────────────


def check_L03(ctx: AuditContext) -> list[Finding]:
    """Текст не помещается в свою рамку (оценка метриками шрифта)."""
    out: list[Finding] = []
    for slide in ctx.slides:
        for sh in slide.text_shapes():
            if not sh.is_ours or sh.table is not None or sh.tag != "sp":
                continue
            need, have, mode = _text_fit(sh)
            if need is None or have is None:
                continue
            slot = slide.slot_of(sh.id)
            if mode == "height" and slot is not None and slot.kind == SlotKind.TITLE and sh.main_run:
                # заголовку разрешены TITLE_LINES строк, как в layout/fitting
                have = max(have, TITLE_LINES * sh.main_run.size_pt * LINE_HEIGHT)
            limit = have * (1 + OVERFLOW_TOL) + OVERFLOW_MIN_PT
            if need <= limit:
                continue
            what = "ширине" if mode == "width" else "высоте"
            # autofit: PowerPoint ужмёт кегль сам, другие вьюеры — не всегда
            sev = Severity.WARNING if sh.autofit in ("normAutofit", "spAutoFit") else Severity.ERROR
            out.append(finding("L03_text_overflow", slide, sev,
                               f"Текст {_label(sh)} не влезает по {what}: нужно {need:.0f} pt, есть {have:.0f} pt"
                               + (f" ({sh.autofit})" if sh.autofit else ""),
                               sh, autofix="shrink_font_by_scale", need_pt=round(need, 1), have_pt=round(have, 1),
                               mode=mode, autofit=sh.autofit or ""))
    return out


def _text_fit(sh: ShapeRec) -> tuple[float | None, float | None, str]:
    """(нужно, есть, режим) в pt: по высоте при переносе, по ширине при wrap=none."""
    l, t, r, b = sh.insets
    width_pt = (sh.box.w - l - r) / EMU_PER_PT
    height_pt = (sh.box.h - t - b) / EMU_PER_PT
    if width_pt <= 0 or height_pt <= 0:
        return None, None, "none"
    if not sh.wrap:
        widest = 0.0
        for p in sh.paragraphs:
            run = max(p.runs, key=lambda r: len(r.text), default=None)
            if run is None:
                continue
            m = TextMeasurer(run.font, run.bold)
            widest = max(widest, max(m.width(line, run.size_pt) for line in p.text.split("\n")))
        return widest, width_pt, "width"
    total = 0.0
    for p in sh.paragraphs:
        run = max(p.runs, key=lambda r: len(r.text), default=None)
        if run is None:
            continue
        m = TextMeasurer(run.font, run.bold)
        total += m.block_height([p.text], run.size_pt, width_pt, line_spacing=p.line_spacing or 1.2,
                                space_before_pt=p.space_before_pt, space_after_pt=p.space_after_pt)
    return total, height_pt, "height"


# ──────────────────────────── L05 / L06 ────────────────────────────


def check_L05(ctx: AuditContext) -> list[Finding]:
    """Блок не выровнен по направляющим макета (левая кромка)."""
    grid = ctx.dna.grid
    guides = sorted(set(grid.columns_x) | {grid.margin_left})
    if not grid.columns_x:
        return []
    out: list[Finding] = []
    for slide in ctx.slides:
        for sh in slide.content():
            if not sh.is_ours or _in_exemplar(slide, sh):
                continue
            centered = abs((sh.box.x + sh.box.w / 2) - ctx.slide_w / 2) <= GRID_TOL
            nearest = min(guides, key=lambda g: abs(g - sh.box.x))
            if centered or abs(nearest - sh.box.x) <= GRID_TOL:
                continue
            out.append(finding("L05_off_grid", slide, Severity.WARNING,
                               f"Блок {_label(sh)} не на направляющей: кромка {sh.box.x / 914400:.2f}\", "
                               f"ближайшая {nearest / 914400:.2f}\"", sh, autofix="snap_to_grid",
                               nearest_x=nearest, delta=abs(nearest - sh.box.x)))
    return out


def check_L06(ctx: AuditContext) -> list[Finding]:
    """Контент заходит в поля у краёв слайда."""
    g = ctx.dna.grid
    area = ctx.slide_w * ctx.slide_h
    out: list[Finding] = []
    for slide in ctx.slides:
        for sh in slide.content():
            if sh.area >= FULL_BLEED * area or _in_exemplar(slide, sh):
                continue
            depth = {
                "left": g.margin_left - sh.box.x, "top": g.margin_top - sh.box.y,
                "right": sh.box.x2 - (ctx.slide_w - g.margin_right), "bottom": sh.box.y2 - (ctx.slide_h - g.margin_bottom),
            }
            depth = {k: v for k, v in depth.items() if v > GRID_TOL}
            if not depth:
                continue
            sides = ", ".join(f"{k} на {v / 914400:.2f}\"" for k, v in depth.items())
            out.append(finding("L06_in_margins", slide, Severity.WARNING, f"Блок {_label(sh)} заходит в поля ({sides})",
                               sh, autofix="snap_to_grid", **{f"depth_{k}": v for k, v in depth.items()}))
    return out


# ──────────────────────────── L07 ────────────────────────────


def check_L07(ctx: AuditContext) -> list[Finding]:
    """Картинка растянута: пропорции рамки не совпадают с изображением (с учётом crop)."""
    out: list[Finding] = []
    for slide in ctx.slides:
        for sh in slide.shapes:
            pic = sh.picture
            if pic is None or sh.is_decor or sh.box.w <= 0 or sh.box.h <= 0:
                continue
            if min(pic.px_w, pic.px_h) < MIN_PICTURE_PX:
                continue
            img_ar = pic.cropped_aspect
            if img_ar is None:
                continue
            frame_ar = sh.box.w / sh.box.h
            diff = abs(img_ar - frame_ar) / frame_ar
            if diff <= ASPECT_TOL:
                continue
            # картинку образца, которую мы не подменяли, растянул сам шаблон
            inherited = slide.exemplar is not None and not sh.is_ours
            out.append(finding("L07_picture_stretched", slide, Severity.WARNING if inherited else Severity.ERROR,
                               f"Картинка растянута: пропорции {img_ar:.2f} vs рамка {frame_ar:.2f} ({diff:.0%})",
                               sh, autofix="crop_to_aspect", image_aspect=round(img_ar, 3), frame_aspect=round(frame_ar, 3),
                               diff=round(diff, 3), in_exemplar=int(inherited)))
    return out


__all__ = ["check_L01", "check_L02", "check_L03", "check_L04", "check_L05", "check_L06", "check_L07"]
