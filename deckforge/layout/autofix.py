"""Применение автофиксов из каталога `core/autofix.FIXES` к копии DeckIR; replan-фиксы только в `skipped`."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from deckforge.core.autofix import FIXES, is_fixable
from deckforge.core.ir import Archetype, Box, DeckIR, Element, Finding, SlideIR, Slot, SlotKind, TemplateDNA
from deckforge.layout.fitting import (
    MIN_SIZE_SCALE,
    NUMBER_MIN_SCALE,
    cut_tail,
    normalize,
    shorten,
    shorten_words,
    text_min_scale,
    title_min_scale,
)

MAX_BULLET_WORDS = 15  # норма D02
MAX_CHART_SERIES = 5  # норма D04
SHRINK_SAFETY = 0.95  # запас: метрики считаются по прокси-шрифту
SNAP_MAX_RATIO = 0.25  # дальше ±25 % от кегля — уже не «привести к шкале»
CUT_TOL = 0.1  # допуск обрезки по разделителю, как у L03
_CONTINUATION = re.compile(r"\s*\(\d+/\d+\)$")


@dataclass
class FixResult:
    ir: DeckIR
    applied: list[dict] = field(default_factory=list)
    skipped: list[dict] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return bool(self.applied)


def apply_fixes(ir: DeckIR, findings: list[Finding], dna: TemplateDNA | None = None) -> FixResult:
    """Применить фиксы к копии IR. Возвращает новый IR и журнал."""
    out = FixResult(ir.model_copy(deep=True))
    slots = _slot_index(dna) if dna else {}
    drop_slides: set[int] = set()
    for f in findings:
        rec = {"slide_idx": f.slide_idx, "element_id": f.element_id, "check_id": f.check_id, "fix": f.autofix}
        if not is_fixable(f):
            spec = FIXES.get(f.autofix or "")
            rec["reason"] = ("дизайн шаблона" if f.evidence.get("in_exemplar") else
                             "нет фикса" if spec is None else f"уровень {spec.scope}: только по решению пользователя")
            out.skipped.append(rec)
            continue
        slide = _slide(out.ir, f.slide_idx)
        if slide is None:
            out.skipped.append({**rec, "reason": "слайд не найден в IR"})
            continue
        if f.autofix == "drop_slide":
            drop_slides.add(f.slide_idx)
            out.applied.append({**rec, "before": slide.exemplar_id, "after": "удалён"})
            continue
        el = _element(slide, f.element_id, f.box)
        if el is None:
            out.skipped.append({**rec, "reason": "элемент не наш (не в IR)"})
            continue
        slot = slots.get((slide.exemplar_id, el.slot_id))
        handler = _HANDLERS[f.autofix]  # type: ignore[index]
        result = handler(slide, el, slot, f)
        if result is None:
            out.skipped.append({**rec, "reason": "нечего менять"})
        else:
            before, after = result
            out.applied.append({**rec, "before": before, "after": after})
    if drop_slides:
        out.ir.slides = [s for s in out.ir.slides if s.idx not in drop_slides]
        for i, s in enumerate(out.ir.slides):
            s.idx = i
    return out


# ──────────────────────────── фиксы ────────────────────────────

Change = tuple[str, str] | None  # (было, стало)


def fix_shrink_font(slide: SlideIR, el: Element, slot: Slot | None, f: Finding) -> Change:
    """L03: уменьшить кегль; если упёрлись в порог — ещё и подрезать текст."""
    need, have = float(f.evidence.get("need_pt") or 0), float(f.evidence.get("have_pt") or 0)
    if need <= 0 or have <= 0 or need <= have:
        return None
    ratio = have / need
    scale = (ratio ** 0.5 if f.evidence.get("mode") != "width" else ratio) * SHRINK_SAFETY
    base = slot.size_pt if slot and slot.size_pt else None
    current = _current_size(el, base)
    if current is None:
        return None
    if el.kind == SlotKind.NUMBER:
        floor = NUMBER_MIN_SCALE
    elif el.kind == SlotKind.TITLE and slot is not None:
        # тот же пол, что у подгонки: крупный заголовок — до 18 pt, титул/раздел/финал — до 55 %
        floor = title_min_scale(slot, slide.archetype in (Archetype.TITLE, Archetype.SECTION, Archetype.CLOSING))
    else:
        floor = text_min_scale(slot) if slot is not None else MIN_SIZE_SCALE
    min_size = (base or current) * floor
    new_size = max(round(current * scale, 1), round(min_size, 1))
    if new_size >= current:
        return None  # кегль уже на пороге — дальше только replan
    factor = new_size / current
    cut = ""
    if el.kind != SlotKind.NUMBER and current * scale < min_size:
        # влезает доля (нужный масштаб / достигнутый)², т.к. вместимость ~ 1/size²
        keep = (scale / factor) ** 2 if f.evidence.get("mode") != "width" else scale / factor
        for p in el.paragraphs:
            for r in p.runs:
                cap = max(3, int(len(r.text) * keep))
                if len(r.text) <= cap:
                    continue
                m = _CONTINUATION.search(r.text)  # «(1/2)» сохраняем
                core, suffix = (r.text[: m.start()], m.group(0)) if m else (r.text, "")
                # сначала хвост по смысловому разделителю
                short = cut_tail(core, int(cap * (1 + CUT_TOL)))
                if short is None and el.kind == SlotKind.TITLE:
                    # заголовок по словам не режем — только последнее придаточное
                    short = cut_tail(core, len(core) - 1)
                elif short is None:
                    short = shorten(core, cap)
                if short and short != core:
                    r.text = short + suffix
                    cut = ", текст укорочен"
    _scale_runs(el, factor, current)
    return f"{current:g} pt", f"{new_size:g} pt{cut}"


def fix_snap_font_size(slide: SlideIR, el: Element, slot: Slot | None, f: Finding) -> Change:
    """T02: кегль → ближайший из шкалы шаблона; крупные цифры KPI не трогаем."""
    nearest = float(f.evidence.get("nearest_pt") or 0)
    size = float(f.evidence.get("size_pt") or 0)
    if not nearest or not size or el.kind == SlotKind.NUMBER or abs(nearest - size) / size > SNAP_MAX_RATIO:
        return None
    hit = False
    for p in el.paragraphs:
        for r in p.runs:
            if r.size_pt and abs(r.size_pt - size) < 0.6:
                r.size_pt, hit = nearest, True
    if not hit and el.style_overrides.get("size_pt") and abs(float(el.style_overrides["size_pt"]) - size) < 0.6:
        el.style_overrides["size_pt"], hit = nearest, True
    if not hit and not el.style_overrides.get("size_pt") and not any(r.size_pt for p in el.paragraphs for r in p.runs):
        el.style_overrides["size_pt"], hit = nearest, True
    return (f"{size:g} pt", f"{nearest:g} pt") if hit else None


def fix_snap_color(slide: SlideIR, el: Element, slot: Slot | None, f: Finding) -> Change:
    """T03: наш цвет → ближайший из палитры."""
    color, nearest, where = (str(f.evidence.get(k) or "") for k in ("color", "nearest", "where"))
    if not color or not nearest:
        return None
    color, nearest = color.upper().lstrip("#"), nearest.upper().lstrip("#")
    hit = False
    if where == "текст":
        for p in el.paragraphs:
            for r in p.runs:
                if (r.color or "").upper().lstrip("#") == color:
                    r.color, hit = nearest, True
        if not hit and str(el.style_overrides.get("text_color") or "").upper().lstrip("#") == color:
            el.style_overrides["text_color"], hit = nearest, True
    else:
        for key in ("palette", "accent"):
            raw = str(el.style_overrides.get(key) or "")
            parts = [c.strip().upper().lstrip("#") for c in raw.split(",") if c.strip()]
            if color in parts:
                el.style_overrides[key] = ",".join(nearest if c == color else c for c in parts)
                hit = True
    return (f"#{color}", f"#{nearest}") if hit else None


def fix_refill_slot(slide: SlideIR, el: Element, slot: Slot | None, f: Finding) -> Change:
    """D02: длинный пункт режется по разделителям / словам. I02: абзац-заглушка удаляется."""
    if f.check_id.startswith("I02"):
        n = len(el.paragraphs)
        el.paragraphs = []
        return (f"{n} абз.", "пусто") if n else None
    changed = []
    for p in el.paragraphs:
        text = normalize(" ".join(r.text for r in p.runs))
        if len(text.split()) <= MAX_BULLET_WORDS or text.startswith(("«", "\"", "“")):
            continue  # цитату не режем
        short = shorten_words(text, MAX_BULLET_WORDS)
        p.runs = [p.runs[0].model_copy(update={"text": short})] if p.runs else []
        changed.append((len(text.split()), len(short.split())))
    if not changed:
        return None
    return ", ".join(f"{a} слов" for a, _ in changed), ", ".join(f"{b} слов" for _, b in changed)


def fix_add_chart_labels(slide: SlideIR, el: Element, slot: Slot | None, f: Finding) -> Change:
    """I05: подписи данных; подпись оси — из unit/y_label."""
    if el.chart is None:
        return None
    if el.style_overrides.get("data_labels"):
        return None
    el.style_overrides["data_labels"] = True
    if not el.chart.unit and el.chart.y_label:
        el.chart.unit = el.chart.y_label
    return "без подписей", "подписи данных" + (f", ось: {el.chart.unit}" if el.chart.unit else "")


def fix_drop_shape(slide: SlideIR, el: Element, slot: Slot | None, f: Finding) -> Change:
    slide.elements = [e for e in slide.elements if e is not el]
    return el.slot_id, "удалён"


def fix_drop_minor_series(slide: SlideIR, el: Element, slot: Slot | None, f: Finding) -> Change:
    """D04: оставить MAX_CHART_SERIES серий с наибольшей суммой значений."""
    if el.chart is None or len(el.chart.series) <= MAX_CHART_SERIES:
        return None
    keep = sorted(el.chart.series, key=lambda k: -sum(abs(v) for v in el.chart.series[k]))[:MAX_CHART_SERIES]
    n = len(el.chart.series)
    el.chart.series = {k: v for k, v in el.chart.series.items() if k in keep}
    return f"{n} серий", f"{MAX_CHART_SERIES} серий"


_HANDLERS = {
    "shrink_font_by_scale": fix_shrink_font,
    "snap_font_size": fix_snap_font_size,
    "snap_color": fix_snap_color,
    "refill_slot": fix_refill_slot,
    "add_chart_labels": fix_add_chart_labels,
    "drop_shape": fix_drop_shape,
    "drop_minor_series": fix_drop_minor_series,
}


# ──────────────────────────── вспомогательное ────────────────────────────


def _slot_index(dna: TemplateDNA) -> dict[tuple[str, str], Slot]:
    return {(e.id, s.id): s for e in dna.exemplars for s in e.slots}


def _slide(ir: DeckIR, idx: int) -> SlideIR | None:
    return next((s for s in ir.slides if s.idx == idx), None)


def _element(slide: SlideIR, element_id: str | None, box: Box | None = None) -> Element | None:
    """По id слота; нативные chart/table получают новый id в файле — ищем по боксу."""
    if element_id is not None:
        el = next((e for e in slide.elements if e.slot_id == element_id), None)
        if el is not None:
            return el
    if box is None:
        return None
    cx, cy = box.x + box.w // 2, box.y + box.h // 2
    for e in slide.elements:
        if (e.chart or e.table) and e.box.x <= cx <= e.box.x2 and e.box.y <= cy <= e.box.y2:
            return e
    return None


def _current_size(el: Element, base: float | None) -> float | None:
    """Действующий кегль: override → первый run → слот образца."""
    if el.style_overrides.get("size_pt"):
        return float(el.style_overrides["size_pt"])
    for p in el.paragraphs:
        for r in p.runs:
            if r.size_pt:
                return float(r.size_pt)
    return base


def _scale_runs(el: Element, factor: float, current: float) -> None:
    """Масштабировать все явные кегли элемента; если явных нет — задать override."""
    explicit = False
    for p in el.paragraphs:
        for r in p.runs:
            if r.size_pt:
                r.size_pt, explicit = round(r.size_pt * factor, 1), True
    if el.style_overrides.get("size_pt"):
        el.style_overrides["size_pt"] = round(float(el.style_overrides["size_pt"]) * factor, 1)
    elif not explicit:
        el.style_overrides["size_pt"] = round(current * factor, 1)


__all__ = ["FixResult", "apply_fixes"]
