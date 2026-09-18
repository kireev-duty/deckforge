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
    ChartSpec,
    DeckIR,
    DeckOutline,
    Element,
    Exemplar,
    OutlineSlide,
    Paragraph,
    SlideIR,
    Slot,
    SlotKind,
    TableSpec,
    TemplateDNA,
    TextRun,
)
from deckforge.core.strategy import Strategy
from deckforge.layout.exemplar_picker import STRUCTURAL, Needs, has_image, pick_exemplar, score_exemplar
from deckforge.core.units import EMU_PER_PT
from deckforge.layout.fitting import (
    DIGIT_WIDTH, GLYPH_WIDTH, MIN_SIZE_SCALE, NUMBER_MIN_SCALE, UNIT_SCALE, chars_at_scale, fit_number, fit_size,
    normalize, shorten, slot_capacity, split_label_body, split_number_unit, xml_safe,
)
from deckforge.layout.planner import STEP_NUMBERING, PlanResult, plan

log = logging.getLogger(__name__)

MIN_CONTINUATION = 2  # меньше пунктов на слайд-продолжение не выносим
BADGE_MAX_CHARS = 4  # label-слот с номером-образцом («1», «02») и такой вместимостью — кружок шага: туда только номер
MAX_RETRIES = 3  # сколько образцов перебрать, если в выбранный не лёг ни один пункт
CONTINUATION = " (продолжение)"
# колода уже сверх объёма стратегии, а слайд в образец не влез: вместо каскада продолжений (8 KPI на образце
# с двумя цифрами — 4 слайда, 20 таких слайдов — 67 в колоде) тот же контент в компактной форме — карточки
# «значение — подпись» или список нумерованных шагов. Форма меняется, ни один пункт не теряется.
COMPACT_FORMS: dict[Archetype, tuple[Archetype, ...]] = {
    Archetype.KPI: (Archetype.CARDS, Archetype.BULLETS),
    Archetype.PROCESS: (Archetype.BULLETS, Archetype.CARDS),
}
COMPACT_TRIES = 12  # сколько образцов на форму перебрать в поиске самого вместительного (build_slide дёшев)
LABEL_MIN_PT, LABEL_MAX_PT = 10.0, 14.0  # подпись KPI внутри фигуры с цифрой — в этих пределах
KPI_IN_LABEL_SCALE = 1.8  # значение KPI в label-слоте карточки крупнее подписи максимум во столько раз
LINE_SPACING = 1.2  # высота строки в кеглях — как в оценке вместимости слотов (parsing) и L03


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
             "palette": ",".join(dna.palette("accent") + dna.palette("secondary")),
             "text_color": (dna.palette("text") or ["212121"])[0]}
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
        # образец, в который не легло ни одного пункта (у VK Tech «cards»-слайд портфеля — один title-слот),
        # не берём: пробуем следующих по скору, иначе контент пропал бы молча, а продолжение с тем же
        # образцом крутилось бы до IMPOSSIBLE — сотни пустых слайдов
        tried: set[str] = set()
        first: tuple | None = None  # лучший по скору образец — к нему возвращаемся, если и остальные не вместили
        while True:
            e, score = pick_exemplar(s, exemplars, strategy, slide_w * slide_h, used, exclude=tried)
            if e is None:
                if first is not None:
                    e, score, slide_ir, leftover = first
                break
            slide_ir, leftover = build_slide(len(result.ir.slides), s, e, slide_h, style or {})
            if leftover is None or not _nothing_placed(s, leftover) or len(tried) >= MAX_RETRIES:
                break
            first = first or (e, score, slide_ir, leftover)
            tried.add(e.id)
        if e is not None and leftover is not None and len(result.ir.slides) + len(queue) >= strategy.target_slides.max:
            alt = _compact_alternative(s, leftover, exemplars, strategy, slide_w * slide_h, used,
                                       len(result.ir.slides), slide_h, style or {})
            if alt is not None:
                e, score, slide_ir, leftover = alt
                result.warnings.append(f"слайд {s.idx} «{s.title[:40]}»: {s.archetype.value} свёрнут в образец "
                                       f"{e.archetype.value} — колода сверх объёма ({strategy.target_slides.max})")
                log.warning(result.warnings[-1])
        result.choices.append(Choice(s.idx, s.archetype.value, e.id if e else None, e.archetype.value if e else None, score))
        if e is None:
            msg = f"слайд {s.idx} ({s.archetype.value} «{s.title[:40]}»): нет подходящего образца — пропущен"
            result.warnings.append(msg)
            log.warning(msg)
            continue
        used[e.id] = used.get(e.id, 0) + 1
        result.ir.slides.append(slide_ir)
        if leftover is not None and _nothing_placed(s, leftover) and leftover.archetype == s.archetype:
            msg = (f"слайд {s.idx} «{s.title[:40]}»: в шаблоне нет образца со слотами под этот контент "
                   f"({_n_items(leftover)} пунктов не размещены, образец {e.id})")
            result.warnings.append(msg)
            log.warning(msg)
            continue  # в очередь не ставим: тот же остаток с тем же архетипом пошёл бы по кругу
        if leftover is not None:
            # в образец не влезло — продолжение на следующем слайде; даже одинокий пункт: лишний слайд
            # заметен и правится пользователем, потерянный факт — нет
            n_left = _n_items(leftover)
            queue.insert(0, leftover)
            result.warnings.append(f"слайд {s.idx} «{s.title[:40]}»: {n_left} пунктов перенесены на продолжение"
                                   + (f" (одинокий пункт: образец {e.id} тесен)" if n_left < MIN_CONTINUATION else ""))
            log.warning(result.warnings[-1])
    return result


def _compact_alternative(
    s: OutlineSlide, leftover: OutlineSlide, exemplars: list[Exemplar], strategy: Strategy, slide_area: int,
    used: dict[str, int], idx: int, slide_h: int, style: dict,
) -> tuple[Exemplar, float, SlideIR, OutlineSlide | None] | None:
    """Тот же слайд в компактной форме (COMPACT_FORMS) — вариант с наименьшим остатком, если он меньше исходного.
    Контент не меняется (kpis/steps остаются), меняется только архетип — builder сам кладёт KPI в карточки
    или списком «значение — подпись», шаги — нумерованным списком. На каждую форму перебираются несколько
    лучших по скору образцов (`exclude=`): штраф за повторы (REUSE_PENALTY) на длинной колоде уводит picker
    к мелким образцам, а здесь важна вместимость, не вкус. Среди вместивших всё выбирается лучший по «сырому»
    скору образца (без штрафов за ранг и повторы — иначе на 20-м слайде выигрывает тесный слот мокапа, у которого
    штраф за тесноту меньше накопленного штрафа за повторы у нормальных), равные вращаются по числу использований."""
    best: tuple[tuple, float, Exemplar, SlideIR, OutlineSlide | None] | None = None
    for arch in (s.archetype, *COMPACT_FORMS.get(s.archetype, ())):
        compact = s if arch == s.archetype else s.model_copy(update={"archetype": arch})
        needs = Needs.of(compact)
        tried: set[str] = set()
        while len(tried) < COMPACT_TRIES:
            e, score = pick_exemplar(compact, exemplars, strategy, slide_area, used, exclude=tried)
            if e is None or e.archetype in STRUCTURAL:
                break  # структурные — второй проход picker'а: контентных кандидатов больше нет
            tried.add(e.id)
            if e.archetype == Archetype.AGENDA:
                continue  # оглавление с десятками подписей формально вместит всё — но это не список шагов
            slide_ir, left = build_slide(idx, compact, e, slide_h, style)
            n_left = _n_items(left) if left is not None else 0
            raw = round(score_exemplar(e, needs, strategy, slide_area), 1)
            key = (n_left, -raw, used.get(e.id, 0))
            if best is None or key < best[0]:
                best = (key, score, e, slide_ir, left)
        if best is not None and best[0][0] == 0:
            break  # эта форма вместила всё — следующие формы (менее естественные) не нужны
    if best is None or best[0][0] >= _n_items(leftover):
        return None
    return best[2], best[1], best[3], best[4]


def _n_items(s: OutlineSlide) -> int:
    return len(s.kpis) + len(s.bullets) + len(s.steps)


def _nothing_placed(s: OutlineSlide, leftover: OutlineSlide) -> bool:
    """Остаток равен исходному контенту — образец не вместил ни пункта."""
    return _n_items(leftover) >= _n_items(s) > 0


# ──────────────────────────── один слайд ────────────────────────────


def build_slide(idx: int, s: OutlineSlide, e: Exemplar, slide_h: int, style: dict) -> tuple[SlideIR, OutlineSlide | None]:
    """Разложить контент слайда по слотам образца. Второй элемент — остаток, не влезший в образец (или None)."""
    by_kind = _slots_by_kind(e.slots, slide_h)
    elements: list[Element] = []
    left_kpis: list = []
    left_items: list[str] = []
    if e.archetype in STRUCTURAL and s.archetype not in STRUCTURAL and not by_kind[SlotKind.BODY]:
        # контентный слайд на титульном/разделительном образце (последний фолбэк picker'а на шаблоне
        # без текстовых образцов): подзаголовок работает телом, иначе контент некуда класть
        by_kind[SlotKind.BODY], by_kind[SlotKind.SUBTITLE] = by_kind[SlotKind.SUBTITLE][:1], by_kind[SlotKind.SUBTITLE][1:]
    if (s.chart or s.table) and not (by_kind[SlotKind.CHART] or by_kind[SlotKind.TABLE]) and by_kind[SlotKind.BODY]:
        # в шаблоне нет ни одного data-слота — нативный объект встаёт на место самого крупного текстового блока
        host = max(by_kind[SlotKind.BODY], key=lambda x: x.box.w * x.box.h)
        by_kind[SlotKind.BODY] = [b for b in by_kind[SlotKind.BODY] if b is not host]
        by_kind[SlotKind.CHART if s.chart else SlotKind.TABLE].append(host)

    def put(slot: Slot, text: str, bullet: bool = False) -> None:
        if el := text_element(slot, text, style, bullet=bullet):
            elements.append(el)

    def put_list(slot: Slot, items: list[str], bullet: bool) -> None:
        if el := list_element(slot, items, style, bullet=bullet):
            elements.append(el)

    quote_as_title = bool(s.quote) and e.archetype in (Archetype.SECTION, Archetype.TITLE)  # цитата крупно
    subtitles = list(by_kind[SlotKind.SUBTITLE])
    if by_kind[SlotKind.TITLE]:
        put(by_kind[SlotKind.TITLE][0], f"«{s.quote}»" if quote_as_title else s.title)
    elif subtitles:  # образец без заголовка, но с подзаголовком («Спасибо за внимание!» финала) — заголовок туда
        put(subtitles.pop(0), f"«{s.quote}»" if quote_as_title else s.title)
    if subtitles:
        sub = (s.quote_author or s.title) if quote_as_title else s.subtitle
        if sub:
            put(subtitles[0], sub)
    elif s.subtitle and s.archetype in STRUCTURAL and e.archetype not in STRUCTURAL and by_kind[SlotKind.BODY]             and not (s.bullets or s.steps or s.kpis or s.paragraphs):
        # титул/финал на текстовом образце (в шаблоне нет титульного): подзаголовок — в тело, иначе пропадёт
        put(by_kind[SlotKind.BODY][0], s.subtitle)

    # данные
    for slot in by_kind[SlotKind.CHART] + by_kind[SlotKind.TABLE]:
        if s.chart or s.table:
            chart = s.chart
            if chart is not None and normalize(chart.title).lower() == normalize(s.title).lower():
                chart = chart.model_copy(update={"title": ""})  # заголовок слайда не дублируем над графиком
            kind = SlotKind.CHART if chart is not None else SlotKind.TABLE  # хост может быть body-слотом (фолбэк)
            elements.append(Element(slot_id=slot.id, kind=kind, box=slot.box, chart=_clean_chart(chart),
                                    table=_clean_table(s.table), style_overrides=dict(style)))
            break
    numbers, labels, captions = by_kind[SlotKind.NUMBER], by_kind[SlotKind.LABEL], by_kind[SlotKind.CAPTION]
    bodies = by_kind[SlotKind.BODY]
    labels_used = bodies_used = 0
    if s.kpis and numbers:
        for i, (k, num) in enumerate(zip(s.kpis, numbers)):
            # цифре без своего label-слота подпись даём внутри той же фигуры вторым абзацем
            elements.append(number_element(num, k.value, style, label=k.label if i >= len(labels) else None))
        for k, lab in zip(s.kpis, labels):
            put(lab, k.label)
            labels_used += 1
        left_kpis = s.kpis[len(numbers):]
    elif s.kpis and labels and (e.archetype == Archetype.CARDS or len(labels) >= len(s.kpis)):
        # образец без крупных цифр (карточки): значение — в подпись, описание — в тело
        # (bullets-образец с одной подписью-колонтитулом (HSE) сюда не попадает — там список в body)
        # по карточкам: label и body одной карточки — пара по геометрии (pair_labels), а не по порядку чтения,
        # иначе подпись уезжает в соседнюю карточку (Пифагор: «42 %» в одной, его подпись — в предыдущей)
        paired = pair_labels(bodies, labels)
        cards = [(paired.get(b.id), b) for b in bodies if paired.get(b.id) is not None]
        cards += [(lab, None) for lab in labels if all(lab is not l for l, _ in cards)]
        taken_labels: list[Slot] = []
        taken_bodies: list[Slot] = []
        for k, (lab, body) in zip(s.kpis, cards):
            if el := kpi_in_label_element(lab, k.value, style):
                elements.append(el)
            taken_labels.append(lab)
            if body is not None:
                put(body, k.label)
                taken_bodies.append(body)
        # занятые слоты — в начало списков: ниже свободные берутся срезом [used:]
        labels = taken_labels + [l for l in labels if l not in taken_labels]
        bodies = taken_bodies + [b for b in bodies if b not in taken_bodies]
        labels_used, bodies_used = len(taken_labels), len(taken_bodies)
        left_kpis = s.kpis[len(cards):]
    elif s.kpis and len(bodies) == 1:  # ни цифр, ни карточек (HSE): список «значение — подпись» в один body
        put_list(bodies[0], [f"{k.value} — {k.label}" for k in s.kpis], bullet=False)
        bodies_used = 1
    elif s.kpis:
        for k, body in zip(s.kpis, bodies):
            put(body, f"{k.value} — {k.label}")
            bodies_used += 1
        left_kpis = s.kpis[len(bodies):]

    # списки: буллеты / шаги / абзацы / цитата
    items = s.bullets or ([STEP_NUMBERING.format(n=i + 1, text=t) for i, t in enumerate(s.steps)] if s.steps else [])
    numbered = bool(s.steps) and not s.bullets
    free_labels, bodies = labels[labels_used:], bodies[bodies_used:]
    if numbered and numbers and not s.kpis:
        # крупные цифры схемы (кружки «1…5» на таймлайне) — номера шагов, иначе рендер их сотрёт
        for i, num in enumerate(numbers[: len(s.steps)]):
            put(num, str(i + 1))
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
                if is_badge(lab):
                    # кружок «1 2 3» рядом с текстом шага: лид «Шаг 12» туда не влезет (L03) — только номер,
                    # в формате образца («1» → «12», «01» → «12»), сам пункт целиком в тело
                    put(lab, f"{i + 1:02d}" if len((lab.sample_text or "").strip()) >= 2 else str(i + 1))
                    put(body, item)
                else:
                    put(lab, f"{i + 1:02d}" if numbered and not rest else lead)
                    put(body, rest or (item if numbered else ""))
            else:
                put(body, item)
        left_items = raw[len(bodies):]
    elif items and free_labels:
        for item, lab in zip(items, free_labels):
            put(lab, item)
        left_items = (s.steps if numbered else items)[len(free_labels):]
    elif items:  # ни body, ни label — пункты некуда класть: весь список в остаток, а не в никуда
        left_items = list(s.steps if numbered else items)
    elif s.paragraphs and bodies:
        put_list(bodies[0], s.paragraphs, bullet=False)

    # «ручной» номер страницы (текст «19» без плейсхолдера sldNum) — перенумеровать; поле sldNum рендер не трогает
    for num_slot in by_kind[SlotKind.SLIDE_NUMBER]:
        if num_slot.placeholder_type is None:
            elements.append(Element(slot_id=num_slot.id, kind=num_slot.kind, box=num_slot.box,
                                    paragraphs=[Paragraph(runs=[TextRun(text=str(idx + 1))])]))

    # картинка — только первый picture-слот; файла нет — слот остаётся пустым, рендер его очистит
    if has_image(s) and by_kind[SlotKind.PICTURE]:
        pic = by_kind[SlotKind.PICTURE][0]
        elements.append(Element(slot_id=pic.id, kind=pic.kind, box=pic.box, image_path=s.image.path))

    slide_ir = SlideIR(idx=idx, exemplar_id=e.id, archetype=e.archetype, elements=elements, outline_ref=s.idx,
                       notes=xml_safe(s.speaker_notes))
    if not (left_kpis or left_items):
        return slide_ir, None
    # остаток со структурного слайда (title/section/closing: буллеты в подписи титула) — обычным текстовым
    # слайдом, иначе пункты капали бы по одному в подпись каждого следующего титульного образца
    arch = s.archetype if s.archetype not in STRUCTURAL else (Archetype.KPI if left_kpis else Archetype.BULLETS)
    title = s.title if s.title.endswith(CONTINUATION) else s.title + CONTINUATION  # не «(продолжение) (продолжение)»
    leftover = OutlineSlide(idx=s.idx, archetype=arch, title=title, section=s.section,
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


def number_element(slot: Slot, text: str, style: dict, label: str | None = None) -> Element:
    """Крупная цифра KPI одной строкой; единица измерения — мелким кеглем следом («1,8 дня»);
    `label` — подпись вторым абзацем мелким кеглем, когда у образца нет отдельного label-слота.
    Подпись добавляется, только если обе строки влезают по высоте — цифру ради неё ужимаем до NUMBER_MIN_SCALE."""
    num, unit, size = fit_number(normalize(text), slot)
    base = size or slot.size_pt or 40.0
    label = normalize(label or "")
    label_pt = min(LABEL_MAX_PT, max(LABEL_MIN_PT, round(base * UNIT_SCALE, 1)))
    if label and slot.size_pt:
        h_pt = slot.box.h / EMU_PER_PT
        room = h_pt / LINE_SPACING - label_pt  # сколько остаётся кеглю цифры рядом с подписью
        if room < slot.size_pt * NUMBER_MIN_SCALE:
            label = ""
        elif room < base:
            base = size = round(room, 1)
    runs = [TextRun(text=num, size_pt=size)]
    if unit:
        runs.append(TextRun(text=f" {unit}", size_pt=round(base * UNIT_SCALE, 1)))
    paragraphs = [Paragraph(runs=runs)]
    if label:
        paragraphs.append(Paragraph(runs=[TextRun(text=label, size_pt=label_pt)]))
    return Element(slot_id=slot.id, kind=slot.kind, box=slot.box, style_overrides=dict(style), paragraphs=paragraphs)


def kpi_in_label_element(slot: Slot, value: str, style: dict) -> Element | None:
    """Значение KPI в label-слоте карточки: кегль подписи мелкий, цифру укрупняем, пока она влезает
    в строку по ширине и в бокс по высоте."""
    el = text_element(slot, value, style)
    if el is None or not slot.size_pt or not slot.max_chars:
        return el
    num, unit = split_number_unit(value)
    width = sum(GLYPH_WIDTH.get(ch, DIGIT_WIDTH) for ch in num) + (UNIT_SCALE * (len(unit) + 1) if unit else 0)
    cpl = slot.max_chars / max(1, slot.max_lines or 1)
    by_height = (slot.box.h / EMU_PER_PT) / (LINE_SPACING * slot.size_pt)
    scale = min(KPI_IN_LABEL_SCALE, cpl / width if width else KPI_IN_LABEL_SCALE, by_height)
    if scale > 1.0 and "size_pt" not in el.style_overrides:
        el.style_overrides["size_pt"] = round(slot.size_pt * scale, 1)
    return el


def list_element(slot: Slot, items: list[str], style: dict, bullet: bool) -> Element | None:
    items = [normalize(t) for t in items if normalize(t)]
    if not items:
        return None
    overrides = dict(style)
    cap = slot.max_items or len(items)
    scale = 1.0
    if len(items) > cap:  # лишние пункты не выбрасываем — уменьшаем кегль, но не ниже MIN_SIZE_SCALE
        scale = max(MIN_SIZE_SCALE, cap / len(items))
    per_item = (slot.max_chars // len(items)) if slot.max_chars else None
    if per_item:
        # длинный пункт сначала ужимаем кеглем (вместимость ~ 1/size²) и только потом режем: «…» — крайняя мера
        longest = max(len(t) for t in items)
        if longest > per_item / (scale * scale):
            scale = min(scale, max(MIN_SIZE_SCALE, (per_item / longest) ** 0.5))
        per_item = int(per_item / (scale * scale))
    if scale < 1.0 and slot.size_pt:
        overrides["size_pt"] = round(slot.size_pt * scale, 1)
    paras = [Paragraph(runs=[TextRun(text=shorten(t, per_item))], bullet=bullet) for t in items]
    return Element(slot_id=slot.id, kind=slot.kind, box=slot.box, style_overrides=overrides, paragraphs=paras)


def _clean_chart(chart: ChartSpec | None) -> ChartSpec | None:
    """Тексты диаграммы — через ту же чистку, что и слоты (XML-недопустимые символы, пробелы)."""
    if chart is None:
        return None
    return chart.model_copy(update={
        "title": normalize(chart.title), "categories": [normalize(c) for c in chart.categories],
        "series": {normalize(k) or f"ряд {i + 1}": v for i, (k, v) in enumerate(chart.series.items())},
        "unit": normalize(chart.unit) if chart.unit else None,
        "x_label": normalize(chart.x_label) if chart.x_label else None,
        "y_label": normalize(chart.y_label) if chart.y_label else None,
    })


def _clean_table(table: TableSpec | None) -> TableSpec | None:
    if table is None:
        return None
    return TableSpec(header=[normalize(h) for h in table.header], rows=[[normalize(c) for c in r] for r in table.rows])


def is_badge(slot: Slot) -> bool:
    """Кружок с номером шага: label-слот, в образце которого стоит голое число, вместимостью в пару знаков."""
    sample = (slot.sample_text or "").strip()
    return slot.kind == SlotKind.LABEL and sample.isdigit() and len(sample) <= 2 and (slot.max_chars or 0) <= BADGE_MAX_CHARS


def pair_labels(bodies: list[Slot], labels: list[Slot]) -> dict[str, Slot]:
    """Каждому body — ближайший label над ним в той же колонке (карточка) или вплотную слева в том же ряду
    (кружок шага перед текстом на таймлайне), иначе — по порядку."""
    out: dict[str, Slot] = {}
    free = list(labels)
    for body in bodies:
        best: tuple[int, Slot] | None = None
        for lab in free:
            same_col = lab.box.x < body.box.x2 and body.box.x < lab.box.x2
            above = lab.box.y <= body.box.y + body.box.h // 4
            same_row = lab.box.y < body.box.y2 and body.box.y < lab.box.y2
            beside = lab.box.x2 <= body.box.x + body.box.w // 4 and body.box.x - lab.box.x2 <= lab.box.h
            if same_col and above:
                dist = abs(body.box.y - lab.box.y2)
            elif same_row and beside:
                dist = abs(body.box.x - lab.box.x2)
            else:
                continue
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
