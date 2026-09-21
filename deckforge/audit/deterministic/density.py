"""Плотность: буллеты, длина пунктов, размер таблиц, число серий, заполнение слайда (D01–D05)."""

from __future__ import annotations

from deckforge.audit.context import SPARSE_ARCHETYPES, AuditContext, ShapeRec, SlideCtx
from deckforge.audit.deterministic.base import finding
from deckforge.core.ir import Box, Finding, Severity, SlotKind

MAX_BULLETS = 6
MAX_BULLET_WORDS = 15
MAX_TABLE_ROWS = 7  # без шапки
MAX_TABLE_COLS = 5
MAX_SERIES = 5
FILL_MIN, FILL_MAX = 0.25, 0.75
SPARSE_MAX_CONTENT = 2  # без IR слайд из стольких блоков считаем структурным


def _is_title(sh: ShapeRec, slide: SlideCtx) -> bool:
    if sh.ph_type in ("title", "ctrTitle"):
        return True
    slot = slide.slot_of(sh.id)
    return slot is not None and slot.kind == SlotKind.TITLE


def _bullet_count(slide: SlideCtx) -> tuple[int, list[ShapeRec]]:
    n, shapes = 0, []
    for sh in slide.text_shapes():
        if sh.table is not None or _is_title(sh, slide):
            continue
        k = len(sh.paragraphs) if len(sh.paragraphs) >= 2 else sum(1 for p in sh.paragraphs if p.bullet)
        if k:
            n += k
            shapes.append(sh)
    return n, shapes


def check_D01(ctx: AuditContext) -> list[Finding]:
    """Больше MAX_BULLETS пунктов на слайде."""
    out: list[Finding] = []
    for slide in ctx.slides:
        n, shapes = _bullet_count(slide)
        if n > MAX_BULLETS:
            biggest = max(shapes, key=lambda s: len(s.paragraphs))
            out.append(finding("D01_bullets", slide, Severity.WARNING, f"На слайде {n} пунктов (норма ≤ {MAX_BULLETS})",
                               biggest, autofix="split_slide", bullets=n))
    return out


def check_D02(ctx: AuditContext) -> list[Finding]:
    """Пункт длиннее MAX_BULLET_WORDS слов."""
    out: list[Finding] = []
    for slide in ctx.slides:
        for sh in slide.text_shapes():
            if sh.table is not None:
                continue
            for p in sh.paragraphs:
                if p.words > MAX_BULLET_WORDS:
                    out.append(finding("D02_bullet_words", slide, Severity.WARNING,
                                       f"Пункт из {p.words} слов (норма ≤ {MAX_BULLET_WORDS}): «{p.text[:60]}…»", sh,
                                       autofix="refill_slot", words=p.words))
    return out


def check_D03(ctx: AuditContext) -> list[Finding]:
    """Таблица больше MAX_TABLE_ROWS строк или MAX_TABLE_COLS колонок."""
    out: list[Finding] = []
    for slide in ctx.slides:
        for sh in slide.shapes:
            t = sh.table
            if t is None:
                continue
            rows = max(0, t.rows - 1)
            if rows > MAX_TABLE_ROWS or t.cols > MAX_TABLE_COLS:
                out.append(finding("D03_table_size", slide, Severity.WARNING,
                                   f"Таблица {rows}×{t.cols} (норма ≤ {MAX_TABLE_ROWS} строк и ≤ {MAX_TABLE_COLS} колонок)",
                                   sh, autofix="split_table", rows=rows, cols=t.cols))
    return out


def check_D04(ctx: AuditContext) -> list[Finding]:
    """Больше MAX_SERIES серий на диаграмме."""
    out: list[Finding] = []
    for slide in ctx.slides:
        for sh in slide.shapes:
            if sh.chart and sh.chart.n_series > MAX_SERIES:
                out.append(finding("D04_chart_series", slide, Severity.WARNING,
                                   f"На диаграмме {sh.chart.n_series} серий (норма ≤ {MAX_SERIES})", sh,
                                   autofix="drop_minor_series", series=sh.chart.n_series))
    return out


def fill_ratio(ctx: AuditContext, slide: SlideCtx) -> float:
    """Доля рабочей области, занятая контентными блоками."""
    g = ctx.dna.grid
    work = Box(x=g.margin_left, y=g.margin_top, w=max(1, ctx.slide_w - g.margin_left - g.margin_right),
               h=max(1, ctx.slide_h - g.margin_top - g.margin_bottom))
    total = 0
    for sh in slide.content():
        w = min(sh.box.x2, work.x2) - max(sh.box.x, work.x)
        h = min(sh.box.y2, work.y2) - max(sh.box.y, work.y)
        if w > 0 and h > 0:
            total += w * h
    return min(1.0, total / (work.w * work.h))


def check_D05(ctx: AuditContext) -> list[Finding]:
    """Слайд заполнен меньше четверти или больше трёх четвертей рабочей области."""
    out: list[Finding] = []
    for slide in ctx.slides:
        if slide.archetype in SPARSE_ARCHETYPES:
            continue
        if slide.archetype is None and len(slide.content()) <= SPARSE_MAX_CONTENT:
            continue
        if not slide.content():
            continue  # это I03
        ratio = fill_ratio(ctx, slide)
        if FILL_MIN <= ratio <= FILL_MAX:
            continue
        kind = "пустоват" if ratio < FILL_MIN else "перегружен"
        out.append(finding("D05_fill", slide, Severity.WARNING,
                           f"Слайд {kind}: контент занимает {ratio:.0%} рабочей области (норма {FILL_MIN:.0%}–{FILL_MAX:.0%})",
                           autofix="change_exemplar", fill=round(ratio, 3)))
    return out


__all__ = ["check_D01", "check_D02", "check_D03", "check_D04", "check_D05", "fill_ratio"]
