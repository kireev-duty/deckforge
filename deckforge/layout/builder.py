"""DeckOutline + образцы + стратегия → DeckIR.

Для каждого спланированного слайда (planner) выбирается образец (exemplar_picker), затем контент
раскладывается по слотам образца по их SlotKind: заголовок → title, буллеты → один body-слот
списком или по карточке на body/label, KPI → number/label, chart/table → нативный объект,
картинка → первый picture-слот. Текст подгоняется под лимиты слотов (fitting). Ничего не
выдумывается: слот без подходящего контента остаётся пустым и очищается рендером.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from deckforge.core.ir import (
    Archetype,
    DeckIR,
    DeckOutline,
    Element,
    Exemplar,
    OutlineSlide,
    Paragraph,
    SlideIR,
    Slot,
    SlotKind,
    TemplateDNA,
    TextRun,
)
from deckforge.core.strategy import Strategy
from deckforge.layout.exemplar_picker import pick_exemplar
from deckforge.layout.fitting import (
    UNIT_SCALE, chars_at_scale, fit_number, fit_size, normalize, shorten, slot_capacity, split_label_body,
)
from deckforge.layout.planner import PlanResult, plan

log = logging.getLogger(__name__)

STEP_NUMBERING = "{n}. {text}"
MIN_CONTINUATION = 2  # меньше пунктов на слайд-продолжение не выносим


@dataclass
class Choice:
    idx: int
    archetype: str
    exemplar_id: str | None
    exemplar_archetype: str | None
    score: float


@dataclass
class LayoutResult:
    ir: DeckIR
    plan: PlanResult
    choices: list[Choice] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def layout_deck(outline: DeckOutline, dna: TemplateDNA, strategy: Strategy) -> LayoutResult:
    style = {"accent": (dna.palette("accent") or ["000000"])[0], "font": dna.fonts[0] if dna.fonts else "Arial",
             "palette": ",".join(dna.palette("accent") + dna.palette("secondary"))}
    return build_deck_ir(outline, strategy, dna.exemplars, dna.template_id, dna.slide_w, dna.slide_h, style)


def build_deck_ir(
    outline: DeckOutline, strategy: Strategy, exemplars: list[Exemplar], template_id: str,
    slide_w: int, slide_h: int, style: dict | None = None,
) -> LayoutResult:
    available = {e.archetype for e in exemplars if e.archetype != Archetype.FREEFORM}
    kpi_capacity = max((sum(1 for s in e.slots if s.kind == SlotKind.NUMBER) for e in exemplars
                        if e.archetype == Archetype.KPI), default=0)
    planned = plan(outline, strategy, available, kpi_capacity)
    result = LayoutResult(DeckIR(template_id=template_id, strategy=strategy.id, slides=[], slide_w=slide_w, slide_h=slide_h),
                          planned, warnings=list(planned.warnings))
    used: dict[str, int] = {}
    queue = list(planned.slides)
    while queue:
        s = queue.pop(0)
        e, score = pick_exemplar(s, exemplars, strategy, slide_w * slide_h, used)
        result.choices.append(Choice(s.idx, s.archetype.value, e.id if e else None, e.archetype.value if e else None, score))
        if e is None:
            msg = f"слайд {s.idx} ({s.archetype.value} «{s.title[:40]}»): нет подходящего образца — пропущен"
            result.warnings.append(msg)
            log.warning(msg)
            continue
        used[e.id] = used.get(e.id, 0) + 1
        slide_ir, leftover = build_slide(len(result.ir.slides), s, e, slide_h, style or {})
        result.ir.slides.append(slide_ir)
        if leftover is not None:
            n_left = len(leftover.kpis) + len(leftover.bullets) + len(leftover.steps)
            if n_left >= MIN_CONTINUATION:  # в образец не влезло — продолжение на следующем слайде
                queue.insert(0, leftover)
                result.warnings.append(f"слайд {s.idx} «{s.title[:40]}»: {n_left} пунктов перенесены на продолжение")
            else:  # одинокий пункт на отдельном слайде хуже, чем его отсутствие
                result.warnings.append(f"слайд {s.idx} «{s.title[:40]}»: пункт не влез в образец {e.id} и отброшен")
            log.warning(result.warnings[-1])
    return result


# ──────────────────────────── один слайд ────────────────────────────


def build_slide(idx: int, s: OutlineSlide, e: Exemplar, slide_h: int, style: dict) -> tuple[SlideIR, OutlineSlide | None]:
    """Разложить контент слайда по слотам образца. Второй элемент — остаток, не влезший в образец (или None)."""
    by_kind = _slots_by_kind(e.slots, slide_h)
    elements: list[Element] = []
    left_kpis: list = []
    left_items: list[str] = []

    def put(slot: Slot, text: str, bullet: bool = False) -> None:
        if el := text_element(slot, text, style, bullet=bullet):
            elements.append(el)

    def put_list(slot: Slot, items: list[str], bullet: bool) -> None:
        if el := list_element(slot, items, style, bullet=bullet):
            elements.append(el)

    quote_as_title = bool(s.quote) and e.archetype in (Archetype.SECTION, Archetype.TITLE)  # цитата крупно
    if by_kind[SlotKind.TITLE]:
        put(by_kind[SlotKind.TITLE][0], f"«{s.quote}»" if quote_as_title else s.title)
    if by_kind[SlotKind.SUBTITLE]:
        sub = (s.quote_author or s.title) if quote_as_title else s.subtitle
        if sub:
            put(by_kind[SlotKind.SUBTITLE][0], sub)

    # данные
    for slot in by_kind[SlotKind.CHART] + by_kind[SlotKind.TABLE]:
        if s.chart or s.table:
            elements.append(Element(slot_id=slot.id, kind=slot.kind, box=slot.box, chart=s.chart, table=s.table,
                                    style_overrides=dict(style)))
            break
    numbers, labels, captions = by_kind[SlotKind.NUMBER], by_kind[SlotKind.LABEL], by_kind[SlotKind.CAPTION]
    bodies = by_kind[SlotKind.BODY]
    labels_used = bodies_used = 0
    if s.kpis and numbers:
        for k, num in zip(s.kpis, numbers):
            put(num, k.value)
        for k, lab in zip(s.kpis, labels):
            put(lab, k.label)
            labels_used += 1
        left_kpis = s.kpis[len(numbers):]
    elif s.kpis and labels:  # образец без крупных цифр (карточки): значение — в подпись, описание — в тело
        for k, lab in zip(s.kpis, labels):
            put(lab, k.value)
            labels_used += 1
        for k, body in zip(s.kpis, bodies):
            put(body, k.label)
            bodies_used += 1
        left_kpis = s.kpis[len(labels):]
    elif s.kpis:
        for k, body in zip(s.kpis, bodies):
            put(body, f"{k.value} — {k.label}")
            bodies_used += 1
        left_kpis = s.kpis[len(bodies):]

    # списки: буллеты / шаги / абзацы / цитата
    items = s.bullets or ([STEP_NUMBERING.format(n=i + 1, text=t) for i, t in enumerate(s.steps)] if s.steps else [])
    numbered = bool(s.steps) and not s.bullets
    free_labels, bodies = labels[labels_used:], bodies[bodies_used:]
    if quote_as_title:
        pass
    elif s.quote and bodies:
        put(bodies[0], f"«{s.quote}»")
        if s.quote_author and (captions or free_labels):
            put((captions or free_labels)[0], s.quote_author)
    elif items and len(bodies) == 1:
        put_list(bodies[0], items, bullet=len(items) > 1 and not numbered)
    elif items and len(bodies) > 1:
        raw = s.steps if numbered else items
        paired = pair_labels(bodies, free_labels)
        for i, (item, body) in enumerate(zip(raw, bodies)):
            lead, rest = split_label_body(item)
            lab = paired.get(body.id)
            if lab is not None:
                put(lab, f"{i + 1:02d}" if numbered and not rest else lead)
                put(body, rest or (item if numbered else ""))
            else:
                put(body, item)
        left_items = raw[len(bodies):]
    elif items and free_labels:
        for item, lab in zip(items, free_labels):
            put(lab, item)
        left_items = (s.steps if numbered else items)[len(free_labels):]
    elif s.paragraphs and bodies:
        put_list(bodies[0], s.paragraphs, bullet=False)

    # картинка — только первый picture-слот
    if s.image and s.image.path and by_kind[SlotKind.PICTURE]:
        pic = by_kind[SlotKind.PICTURE][0]
        elements.append(Element(slot_id=pic.id, kind=pic.kind, box=pic.box, image_path=s.image.path))

    slide_ir = SlideIR(idx=idx, exemplar_id=e.id, archetype=e.archetype, elements=elements, outline_ref=s.idx,
                       notes=s.speaker_notes)
    if not (left_kpis or left_items):
        return slide_ir, None
    leftover = OutlineSlide(idx=s.idx, archetype=s.archetype, title=f"{s.title} (продолжение)", section=s.section,
                            kpis=left_kpis, sources=list(s.sources))
    if numbered:
        leftover.steps = left_items
    else:
        leftover.bullets = left_items
    return slide_ir, leftover


# ──────────────────────────── элементы ────────────────────────────


def text_element(slot: Slot, text: str, style: dict, bullet: bool = False) -> Element | None:
    text = normalize(text)
    if not text:
        return None
    if slot.kind == SlotKind.NUMBER:
        return number_element(slot, text, style)
    overrides = dict(style)
    cap = slot_capacity(slot)
    if cap and len(text) > cap:
        size = fit_size(text, slot)
        if size and slot.size_pt:
            overrides["size_pt"] = size
            text = shorten(text, chars_at_scale(slot))  # режем, только если не спасает и минимальный кегль
        else:
            text = shorten(text, cap)
    return Element(slot_id=slot.id, kind=slot.kind, box=slot.box, style_overrides=overrides,
                   paragraphs=[Paragraph(runs=[TextRun(text=text)], bullet=bullet)])


def number_element(slot: Slot, text: str, style: dict) -> Element:
    """Крупная цифра KPI одной строкой; единица измерения — мелким кеглем следом («1,8 дня»)."""
    num, unit, size = fit_number(text, slot)
    runs = [TextRun(text=num, size_pt=size)]
    if unit:
        runs.append(TextRun(text=f" {unit}", size_pt=round((size or slot.size_pt or 40.0) * UNIT_SCALE, 1)))
    return Element(slot_id=slot.id, kind=slot.kind, box=slot.box, style_overrides=dict(style),
                   paragraphs=[Paragraph(runs=runs)])


def list_element(slot: Slot, items: list[str], style: dict, bullet: bool) -> Element | None:
    items = [normalize(t) for t in items if normalize(t)]
    if not items:
        return None
    overrides = dict(style)
    cap = slot.max_items or len(items)
    if len(items) > cap:  # лишние пункты не выбрасываем — уменьшаем кегль, но не ниже 70 %
        scale = max(0.7, cap / len(items))
        if slot.size_pt:
            overrides["size_pt"] = round(slot.size_pt * scale, 1)
    per_item = (slot.max_chars // len(items)) if slot.max_chars else None
    if per_item and "size_pt" in overrides and slot.size_pt:
        per_item = int(per_item / (overrides["size_pt"] / slot.size_pt) ** 2)
    paras = [Paragraph(runs=[TextRun(text=shorten(t, per_item))], bullet=bullet) for t in items]
    return Element(slot_id=slot.id, kind=slot.kind, box=slot.box, style_overrides=overrides, paragraphs=paras)


def pair_labels(bodies: list[Slot], labels: list[Slot]) -> dict[str, Slot]:
    """Каждому body — ближайший label над ним в той же колонке (карточка), иначе — по порядку."""
    out: dict[str, Slot] = {}
    free = list(labels)
    for body in bodies:
        best: tuple[int, Slot] | None = None
        for lab in free:
            same_col = lab.box.x < body.box.x2 and body.box.x < lab.box.x2
            above = lab.box.y <= body.box.y + body.box.h // 4
            if same_col and above:
                dist = abs(body.box.y - lab.box.y2)
                if best is None or dist < best[0]:
                    best = (dist, lab)
        if best is not None:
            out[body.id] = best[1]
            free.remove(best[1])
    for body in bodies:  # без геометрической пары — остаток по порядку
        if body.id not in out and free:
            out[body.id] = free.pop(0)
    return out


def _slots_by_kind(slots: list[Slot], slide_h: int) -> dict[SlotKind, list[Slot]]:
    """Слоты по видам в порядке чтения (ряд сверху вниз, внутри ряда слева направо)."""
    row = max(1, slide_h // 12)
    out: dict[SlotKind, list[Slot]] = {k: [] for k in SlotKind}
    for s in sorted(slots, key=lambda s: (s.box.y // row, s.box.x)):
        out[s.kind].append(s)
    return out


__all__ = ["Choice", "LayoutResult", "build_deck_ir", "build_slide", "layout_deck", "list_element", "number_element", "pair_labels", "text_element"]
