"""Соответствие шаблону: шрифты, кегли, цвета, лейауты, фиксированные элементы, контраст (T01–T06).

T02/T03 смотрят только на заполненные нами слоты и добавленные объекты: декор образца по определению в палитре.
"""

from __future__ import annotations

from deckforge.audit.context import STEP, SYMBOL_FONTS, AuditContext, ShapeRec, SlideCtx
from deckforge.audit.deterministic.base import finding
from deckforge.core.colors import contrast_ratio, delta_e, hex_to_rgb, rgb_to_hsl
from deckforge.core.ir import Finding, Severity
from deckforge.core.placeholders import is_photo_prompt

MAX_FONT_FAMILIES = 2
SIZE_TOL = 0.08  # кегль «из шкалы», если отличается меньше
FIT_MIN_SCALE = 0.7  # подгонка кегля до этого предела — info, не warning
COLOR_TOL_DE = 8.0  # ΔE76, как COLOR_MERGE_DE в extract_tokens
TINT_HUE_TOL = 10 / 360
CONTRAST_MIN = 4.5
CONTRAST_MIN_LARGE = 3.0  # WCAG для крупного текста
LARGE_PT, LARGE_BOLD_PT = 24.0, 18.7
EDGE_ZONE = 0.12  # зона колонтитулов, как FIXED_TEXT_ZONE в классификаторе


# ──────────────────────────── T01 ────────────────────────────


def check_T01(ctx: AuditContext) -> list[Finding]:
    """Шрифт не из шаблона / гарнитур больше двух."""
    out: list[Finding] = []
    seen_fonts: dict[str, tuple[SlideCtx, ShapeRec]] = {}
    for slide in ctx.slides:
        reported: set[str] = set()
        for sh in slide.shapes:
            for run in sh.runs:
                font = run.font or ""
                if not font or font.startswith(SYMBOL_FONTS):
                    continue
                key = font.lower()
                seen_fonts.setdefault(key, (slide, sh))
                if ctx.allowed_fonts and key not in ctx.allowed_fonts and key not in reported:
                    reported.add(key)
                    out.append(finding("T01_font", slide, Severity.ERROR, f"Шрифт «{font}» не из шаблона "
                                       f"(допустимы: {', '.join(sorted(ctx.allowed_fonts))})", sh, autofix="reset_font",
                                       font=font))
    if len(seen_fonts) > MAX_FONT_FAMILIES:
        extra = list(seen_fonts)[MAX_FONT_FAMILIES:]
        slide, sh = seen_fonts[extra[0]]
        out.append(finding("T01_font", slide, Severity.ERROR,
                           f"В колоде {len(seen_fonts)} гарнитур ({', '.join(seen_fonts)}), допустимо {MAX_FONT_FAMILIES}",
                           sh, autofix="reset_font", families=len(seen_fonts)))
    return out


# ──────────────────────────── T02 ────────────────────────────


def check_T02(ctx: AuditContext) -> list[Finding]:
    """Кегль не из типографической шкалы шаблона."""
    scale = ctx.scale_sizes
    if not scale:
        return []
    out: list[Finding] = []
    for slide in ctx.slides:
        for sh in slide.ours():
            if not sh.has_text or sh.table is not None:
                continue
            slot = slide.slot_of(sh.id)
            base = slot.size_pt if slot and slot.size_pt else None
            for size in sorted({r.size_pt for r in sh.runs}):
                if _near_any(size, scale) or (base and _near_any(size, [base])):
                    continue
                nearest = min(scale, key=lambda s: abs(s - size))
                if base and FIT_MIN_SCALE * base <= size <= base:
                    sev, note = Severity.INFO, f" (подгонка: {size / base:.0%} от кегля образца {base:g} pt)"
                else:
                    sev, note = Severity.WARNING, ""
                out.append(finding("T02_font_size", slide, sev,
                                   f"Кегль {size:g} pt не из шкалы шаблона (ближайший {nearest:g} pt){note}", sh,
                                   autofix="snap_font_size", size_pt=size, nearest_pt=nearest, base_pt=base or 0))
    return out


def _near_any(size: float, sizes: list[float]) -> bool:
    return any(abs(size - s) / s <= SIZE_TOL for s in sizes if s > 0)


# ──────────────────────────── T03 ────────────────────────────


def check_T03(ctx: AuditContext) -> list[Finding]:
    """Цвет не из палитры шаблона (текст, заливки, линии, серии диаграмм, ячейки таблиц)."""
    palette = sorted(ctx.palette)
    if not palette:
        return []
    out: list[Finding] = []
    for slide in ctx.slides:
        for sh in slide.ours():
            colors: dict[str, str] = {}
            for run in sh.runs:
                if run.color:
                    colors.setdefault(run.color, "текст")
            for c in sh.fill:
                colors.setdefault(c, "заливка")
            for c in sh.line:
                colors.setdefault(c, "линия")
            if sh.chart:
                for c in sh.chart.series_colors:
                    colors.setdefault(c, "серия диаграммы")
            if sh.table:
                for c in sh.table.fills:
                    colors.setdefault(c, "ячейка таблицы")
            for color, where in colors.items():
                nearest = min(palette, key=lambda p: delta_e(p, color))
                de = delta_e(nearest, color)
                if de < COLOR_TOL_DE or _is_tint_of(color, palette):
                    continue
                out.append(finding("T03_color", slide, Severity.WARNING,
                                   f"Цвет #{color} ({where}) не из палитры шаблона (ближайший #{nearest}, ΔE={de:.0f})",
                                   sh, autofix="snap_color", color=color, nearest=nearest, delta_e=round(de, 1), where=where))
    return out


def _is_tint_of(color: str, palette: list[str]) -> bool:
    """Оттенок цвета палитры: тот же hue, другая светлота."""
    h, s, _ = rgb_to_hsl(hex_to_rgb(color))
    if s < 0.15:
        return False  # иначе любой серый пройдёт
    for p in palette:
        ph, ps, _ = rgb_to_hsl(hex_to_rgb(p))
        if ps < 0.15:
            continue
        dh = abs(h - ph)
        if min(dh, 1 - dh) <= TINT_HUE_TOL:
            return True
    return False


# ──────────────────────────── T04 ────────────────────────────


def check_T04(ctx: AuditContext) -> list[Finding]:
    """Слайд собран не на лейауте/образце из шаблона."""
    names = ctx.template_layout_names
    if not names:
        return []
    out: list[Finding] = []
    for slide in ctx.slides:
        if slide.layout_name not in names:
            out.append(finding("T04_layout", slide, Severity.ERROR,
                               f"Лейаут «{slide.layout_name}» отсутствует в шаблоне", layout=slide.layout_name))
        elif slide.exemplar is not None and slide.layout_name != slide.exemplar.layout_name:
            out.append(finding("T04_layout", slide, Severity.ERROR,
                               f"Лейаут «{slide.layout_name}» не совпадает с лейаутом образца {slide.exemplar.id} "
                               f"(«{slide.exemplar.layout_name}»)", layout=slide.layout_name,
                               exemplar_layout=slide.exemplar.layout_name))
    return out


# ──────────────────────────── T05 ────────────────────────────


def check_T05(ctx: AuditContext) -> list[Finding]:
    """Логотип / колонтитул / декор сдвинут или удалён относительно образца (или fixed_elements без IR)."""
    out: list[Finding] = []
    for slide in ctx.slides:
        if slide.exemplar is not None:
            ref = ctx.exemplar_shapes(slide.exemplar)
            empty_ph = ctx.exemplar_empty_placeholders(slide.exemplar)
            # плашку-«чип» под заголовком рендер растягивает под текст (Slot.plate_id) — ширина в пределах plate_max_w
            plates = {s.plate_id: s.plate_max_w or 0 for s in slide.exemplar.slots if s.plate_id}
            for fid in slide.exemplar.fixed:
                bb = ref.get(fid)
                if bb is None or _is_zone_caption(ctx, slide, fid, bb) or _is_photo_prompt_frame(ctx, slide, bb, ref):
                    continue
                if fid in empty_ph:
                    # пустой плейсхолдер образца рендер убирает намеренно
                    continue
                sh = slide.by_id(fid)
                if sh is None:
                    out.append(finding("T05_fixed_moved", slide, Severity.ERROR,
                                       f"Фиксированный элемент {fid} образца {slide.exemplar.id} удалён",
                                       autofix="restore_fixed", element=fid))
                    continue
                dx, dy = abs(sh.box.x - bb[0]), abs(sh.box.y - bb[1])
                dw, dh = abs(sh.box.w - bb[2]), abs(sh.box.h - bb[3])
                if fid in plates and bb[2] - STEP <= sh.box.w <= max(bb[2], plates[fid]) + STEP:
                    dw = 0
                if max(dx, dy, dw, dh) > STEP:
                    out.append(finding("T05_fixed_moved", slide, Severity.ERROR,
                                       f"Фиксированный элемент {fid} сдвинут на ({dx / 914400:.2f}\", {dy / 914400:.2f}\")",
                                       sh, autofix="restore_fixed", dx=dx, dy=dy, dw=dw, dh=dh))
            continue
        for fe in ctx.dna.fixed_elements:
            if fe.kind not in ("logo", "footer"):
                continue
            at_place = any(_same_geometry(sh, fe.box, position=True) for sh in slide.shapes)
            if at_place:
                continue
            moved = next((sh for sh in slide.shapes if (sh.tag == "pic") == fe.is_picture
                          and _same_geometry(sh, fe.box, position=False)), None)
            if moved is not None:
                out.append(finding("T05_fixed_moved", slide, Severity.ERROR,
                                   f"{'Логотип' if fe.kind == 'logo' else 'Колонтитул'} сдвинут: ожидается в "
                                   f"({fe.box.x / 914400:.2f}\", {fe.box.y / 914400:.2f}\")", moved, autofix="restore_fixed",
                                   expected_x=fe.box.x, expected_y=fe.box.y))
    return out


def _is_zone_caption(ctx: AuditContext, slide: SlideCtx, fid: str, bb: tuple[int, int, int, int]) -> bool:
    """Короткий текст образца вне зон колонтитулов — подпись зоны, а не логотип; рендер убирает её намеренно."""
    text = ctx.exemplar_texts(slide.exemplar).get(fid, "") if slide.exemplar else ""
    if not text:
        return False
    fy, fh = bb[1] / ctx.slide_h, bb[3] / ctx.slide_h
    return not (fy + fh <= EDGE_ZONE or fy >= 1 - EDGE_ZONE)


def _is_photo_prompt_frame(ctx: AuditContext, slide: SlideCtx, bb: tuple[int, int, int, int],
                           ref: dict[str, tuple[int, int, int, int]]) -> bool:
    """Рамка под подсказкой «Вставить фото» в образце: без картинки рендер убирает её вместе с подсказкой."""
    for sid, text in (ctx.exemplar_texts(slide.exemplar).items() if slide.exemplar else ()):
        pb = ref.get(sid)
        if pb is None or not is_photo_prompt(text):
            continue
        cx, cy = pb[0] + pb[2] / 2, pb[1] + pb[3] / 2
        if bb[0] <= cx <= bb[0] + bb[2] and bb[1] <= cy <= bb[1] + bb[3]:
            return True
    return False


def _same_geometry(sh: ShapeRec, box, position: bool) -> bool:
    same_size = abs(sh.box.w - box.w) <= STEP and abs(sh.box.h - box.h) <= STEP
    if not position:
        return same_size
    return same_size and abs(sh.box.x - box.x) <= STEP and abs(sh.box.y - box.y) <= STEP


# ──────────────────────────── T06 ────────────────────────────


def check_T06(ctx: AuditContext) -> list[Finding]:
    """Контраст текста к фактическому фону под блоком ниже нормы WCAG."""
    out: list[Finding] = []
    for slide in ctx.slides:
        for sh in slide.text_shapes():
            bg = _background_under(slide, sh)
            if bg is None:
                continue
            worst: tuple[float, str, float, bool] | None = None
            for run in sh.runs:
                if not run.color:
                    continue
                cr = contrast_ratio(run.color, bg)
                if worst is None or cr < worst[0]:
                    worst = (cr, run.color, run.size_pt, run.bold)
            if worst is None:
                continue
            cr, color, size, bold = worst
            large = size >= LARGE_PT or (bold and size >= LARGE_BOLD_PT)
            need = CONTRAST_MIN_LARGE if large else CONTRAST_MIN
            if cr >= need:
                continue
            # цвет из палитры шаблона — так задумано дизайнером
            from_template = any(delta_e(color, p) < COLOR_TOL_DE for p in ctx.palette)
            sev = Severity.WARNING if from_template else Severity.ERROR
            out.append(finding("T06_contrast", slide, sev,
                               f"Контраст текста #{color} к фону #{bg} = {cr:.1f}:1 (нужно {need}:1)", sh,
                               autofix="snap_color", contrast=round(cr, 2), text_color=color, background=bg, need=need,
                               template_color=int(from_template)))
    return out


def _background_under(slide: SlideCtx, sh: ShapeRec) -> str | None:
    """Цвет под текстом: своя заливка → заливка под блоком → фон слайда; None — картинка."""
    if sh.fill:
        return sh.fill[0]
    cx, cy = sh.box.x + sh.box.w / 2, sh.box.y + sh.box.h / 2
    below = [o for o in slide.shapes if o.z < sh.z and o.box.x <= cx <= o.box.x2 and o.box.y <= cy <= o.box.y2
             and (o.fill or o.is_picture)]
    if below:
        top = max(below, key=lambda o: o.z)
        return None if top.is_picture and not top.fill else top.fill[0]
    if slide.bg_is_picture:
        return None
    return slide.bg_color


__all__ = ["check_T01", "check_T02", "check_T03", "check_T04", "check_T05", "check_T06"]
