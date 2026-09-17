"""Подбор слайда-образца под спланированный слайд с учётом стратегии.

Кандидаты — целевой архетип слайда плюс цепочка совместимых по контенту (FALLBACKS): так колода
собирается и на шаблоне, где нужного архетипа нет (у VK WorkSpace нет section/bullets, у ЛЦТ2026 —
kpi/table/closing). Для «гибких» текстовых архетипов порядок кандидатов задаёт archetype_priority
стратегии (executive предпочтёт карточки, narrative — список); структурные и data-архетипы
(title, section, kpi, chart…) всегда идут первыми — их уже выбрал planner.

Скор образца = ранг архетипа + вместимость слотов + плотность + картинки/иконки + уверенность
классификации − повторное использование.
"""

from __future__ import annotations

from dataclasses import dataclass
from statistics import median

from deckforge.core.ir import Archetype, Exemplar, OutlineSlide, Slot, SlotKind
from deckforge.core.strategy import Strategy
from deckforge.core.units import EMU_PER_INCH
from deckforge.layout.fitting import MIN_SIZE_SCALE, chars_at_scale

FALLBACKS: dict[Archetype, list[Archetype]] = {
    Archetype.TITLE: [Archetype.SECTION],
    Archetype.SECTION: [Archetype.TITLE],
    Archetype.AGENDA: [Archetype.CARDS, Archetype.BULLETS],
    Archetype.BULLETS: [Archetype.CARDS, Archetype.TWO_COLUMN, Archetype.IMAGE_TEXT, Archetype.AGENDA],
    Archetype.TWO_COLUMN: [Archetype.CARDS, Archetype.BULLETS, Archetype.IMAGE_TEXT],
    Archetype.CARDS: [Archetype.BULLETS, Archetype.TWO_COLUMN, Archetype.AGENDA],
    Archetype.KPI: [Archetype.CARDS, Archetype.BULLETS],  # bullets: строки «значение — подпись», когда цифр/карточек нет
    Archetype.CHART: [Archetype.TABLE],
    Archetype.TABLE: [Archetype.CHART, Archetype.CARDS],
    Archetype.PROCESS: [Archetype.CARDS, Archetype.AGENDA, Archetype.BULLETS],
    Archetype.QUOTE: [Archetype.SECTION, Archetype.IMAGE_TEXT, Archetype.BULLETS, Archetype.TITLE],  # section/title: цитата в крупный заголовок
    Archetype.IMAGE_TEXT: [Archetype.BULLETS, Archetype.CARDS],
    Archetype.IMAGE_FULL: [Archetype.IMAGE_TEXT],
    Archetype.TEAM: [Archetype.CARDS],
    Archetype.CLOSING: [Archetype.TITLE, Archetype.SECTION],
}
# архетипы, между которыми стратегия вправе выбирать сама (текст без данных)
FLEXIBLE = {Archetype.BULLETS, Archetype.CARDS, Archetype.TWO_COLUMN, Archetype.IMAGE_TEXT}
ARCH_RANK_PENALTY = 4.0  # шаг по цепочке фолбэков (структурные/data-архетипы)
FLEX_RANK_PENALTY = 2.0  # шаг по archetype_priority среди гибких текстовых архетипов — это вкус, не структура
REUSE_PENALTY = 2.0
NO_TITLE_PENALTY = 5.0  # у образца нет слота под заголовок — заголовок образца останется/пропадёт
TRUNCATION_PENALTY = 3.0  # заголовок или список не влезут даже при минимальном кегле
MIN_TABLE_COL_W = int(1.0 * EMU_PER_INCH)  # уже — таблица нечитаема
OVERLAP_PENALTY = 5.0  # заголовок образца заходит под контентный блок — текст наложится
OVERLAP_SHARE = 0.2
EXTRA_PICTURE_PENALTY = 2.5  # каждая лишняя рамка под картинку сверх одной: контент даёт одну иллюстрацию
TIGHT_SHARE = 0.5  # слот под пункт «тесный», если при минимальном кегле вмещает меньше половины среднего пункта
BIG_NUMBER_SHARE = 0.65  # number-слот считается за KPI-цифру, если его кегль ≥ 65 % от медианного кегля цифр образца
IMPOSSIBLE = -1000.0


@dataclass(frozen=True)
class Needs:
    """Что слайду нужно от образца."""

    items: int  # буллеты / шаги / карточки
    kpis: int
    chart: bool
    table: bool
    image: bool
    quote: bool
    title_chars: int = 0
    text_chars: int = 0  # суммарная длина списка (для одного body-слота)
    table_cols: int = 0
    quote_chars: int = 0

    @classmethod
    def of(cls, s: OutlineSlide) -> "Needs":
        lst = s.bullets or s.steps or s.paragraphs
        return cls(items=len(lst), kpis=len(s.kpis), chart=s.chart is not None, table=s.table is not None,
                   image=bool(s.image and s.image.path), quote=bool(s.quote), title_chars=len(s.title),
                   text_chars=sum(len(t) for t in lst) + 2 * len(lst), table_cols=len(s.table.header) if s.table else 0,
                   quote_chars=len(s.quote or "") + 2)


def candidate_archetypes(target: Archetype, strategy: Strategy) -> list[Archetype]:
    chain = [target] + [a for a in FALLBACKS.get(target, []) if a != target]
    if target in FLEXIBLE:
        flexible = [a for a in chain if a in FLEXIBLE]
        rest = [a for a in chain if a not in FLEXIBLE]
        flexible.sort(key=lambda a: (strategy.priority_of(a), 0 if a == target else 1))
        return flexible + rest
    return chain


def pick_exemplar(
    slide: OutlineSlide, exemplars: list[Exemplar], strategy: Strategy, slide_area: int,
    used: dict[str, int] | None = None,
) -> tuple[Exemplar | None, float]:
    """Лучший образец и его скор; (None, IMPOSSIBLE), если ни один не годится."""
    used = used or {}
    needs = Needs.of(slide)
    chain = candidate_archetypes(slide.archetype, strategy)
    rank_penalty = FLEX_RANK_PENALTY if slide.archetype in FLEXIBLE else ARCH_RANK_PENALTY
    best: tuple[float, int, Exemplar] | None = None
    for rank, arch in enumerate(chain):
        for e in exemplars:
            if e.archetype != arch or e.archetype == Archetype.FREEFORM:
                continue
            score = score_exemplar(e, needs, strategy, slide_area) - rank_penalty * rank - REUSE_PENALTY * used.get(e.id, 0)
            if score <= IMPOSSIBLE:
                continue
            key = (score, -e.source_index, e)
            if best is None or key[:2] > best[:2]:
                best = key
    if best is None:
        return None, IMPOSSIBLE
    return best[2], best[0]


def score_exemplar(e: Exemplar, n: Needs, strategy: Strategy, slide_area: int) -> float:
    kinds = _count_kinds(e.slots)
    score = e.confidence
    # заголовок: слот должен быть и вмещать текст хотя бы при минимальном кегле в две строки
    titles = [s for s in e.slots if s.kind == SlotKind.TITLE]
    if n.title_chars and not (titles or n.quote):
        # без title-слота заголовок ляжет в subtitle (builder), если он есть — штраф мягче
        score -= NO_TITLE_PENALTY * (0.4 if kinds[SlotKind.SUBTITLE] else 1.0)
    elif titles and n.title_chars and (cap := chars_at_scale(titles[0], MIN_SIZE_SCALE)) and n.title_chars > cap:
        score -= TRUNCATION_PENALTY
    if titles and _overlaps_content(titles[0], e.slots):
        score -= OVERLAP_PENALTY
    # данные: без слота под chart/table образец не годится
    if n.chart or n.table:
        data_slots = [s for s in e.slots if s.kind in (SlotKind.CHART, SlotKind.TABLE)]
        if not data_slots:
            return IMPOSSIBLE
        score += 2.0 if (n.chart and kinds[SlotKind.CHART]) or (n.table and kinds[SlotKind.TABLE]) else 0.0
        if n.table_cols and data_slots[0].box.w / n.table_cols < MIN_TABLE_COL_W:
            score -= TRUNCATION_PENALTY
    bodies = [s for s in e.slots if s.kind == SlotKind.BODY]
    # цитата: нужен body под её текст либо крупный заголовок (section/title — цитата в него), иначе слайд выйдет пустым
    if n.quote:
        if e.archetype in (Archetype.SECTION, Archetype.TITLE):
            if not titles:
                return IMPOSSIBLE
        elif not bodies:
            return IMPOSSIBLE
        elif (cap := chars_at_scale(bodies[0], MIN_SIZE_SCALE)) and n.quote_chars > cap:
            score -= TRUNCATION_PENALTY
    # KPI: крупные цифры
    if n.kpis:
        if kinds[SlotKind.NUMBER]:
            score += _capacity_score(_big_numbers(e.slots), n.kpis, empty_penalty=2.0, short_penalty=5.0)
        elif e.archetype == Archetype.CARDS:  # карточки вместо крупных цифр: значение → label, описание → body
            score += _capacity_score(kinds[SlotKind.LABEL], n.kpis, empty_penalty=1.5, short_penalty=5.0) - 1.0
        elif len(bodies) == 1 and (bodies[0].max_items or 0) >= n.kpis:  # список «значение — подпись»
            score -= 1.0
        else:
            score -= 3.0 * n.kpis
    # списки: один body-слот вмещает max_items пунктов, несколько body — по пункту на карточку
    if n.items:
        labels = kinds[SlotKind.LABEL] + kinds[SlotKind.CAPTION]
        if len(bodies) == 1:
            cap = bodies[0].max_items or 6
            score += 1.0 if n.items <= cap else -1.5 * (n.items - cap)
            if (chars := chars_at_scale(bodies[0], MIN_SIZE_SCALE)) and n.text_chars > chars:
                score -= TRUNCATION_PENALTY
        elif len(bodies) > 1:
            score += _capacity_score(len(bodies), n.items, empty_penalty=1.5)
            if kinds[SlotKind.LABEL] and kinds[SlotKind.LABEL] != len(bodies):  # нерегулярная сетка карточек
                score -= 1.0 * abs(kinds[SlotKind.LABEL] - len(bodies))
            score -= TRUNCATION_PENALTY * _tight_slots(bodies[: n.items], n)
        elif labels:
            score += _capacity_score(labels, n.items, empty_penalty=1.0) - 1.0
            score -= TRUNCATION_PENALTY * _tight_slots([s for s in e.slots if s.kind in (SlotKind.LABEL, SlotKind.CAPTION)][: n.items], n)
        else:
            score -= 2.0 * n.items
    # картинки и иконки: без картинки в контенте picture-слот останется заглушкой образца
    pics, icons = kinds[SlotKind.PICTURE], kinds[SlotKind.ICON]
    if pics:
        if n.image:
            # иллюстрация уже сгенерирована — образец с одной рамкой должен обыгрывать текстовый
            score += {"minimal": 0.5, "preferred": 3.0, "always": 5.0}[strategy.images]
            score -= EXTRA_PICTURE_PENALTY * (pics - 1)  # картинка одна — остальные рамки останутся с фото образца
        else:
            # без картинки рамка образца останется чёрным прямоугольником/чужим фото (VK Education image_text)
            score += {"minimal": -3.0, "preferred": -5.0, "always": -2.0}[strategy.images]
    if icons and strategy.icons:
        score += 1.0
    # плотность
    fill = min(1.0, sum(s.box.w * s.box.h for s in e.slots) / slide_area) if slide_area else 0.0
    if fill > strategy.density.max_fill_ratio:
        score -= (fill - strategy.density.max_fill_ratio) * 10.0
    return score


def _overlaps_content(title: Slot, slots: list[Slot]) -> bool:
    """Бокс заголовка пересекает body/picture/chart/table-слот больше чем на OVERLAP_SHARE своей площади."""
    area = max(1, title.box.w * title.box.h)
    for s in slots:
        if s.kind not in (SlotKind.BODY, SlotKind.PICTURE, SlotKind.CHART, SlotKind.TABLE):
            continue
        w = min(title.box.x2, s.box.x2) - max(title.box.x, s.box.x)
        h = min(title.box.y2, s.box.y2) - max(title.box.y, s.box.y)
        if w > 0 and h > 0 and w * h / area > OVERLAP_SHARE:
            return True
    return False


def _big_numbers(slots: list[Slot]) -> int:
    """Сколько number-слотов «крупные» — не мельче BIG_NUMBER_SHARE от медианного кегля цифр образца.
    Мелкие цифры внутри нарисованного кольца (WorkSpace: 28–32 pt при 54–60 pt у остальных) в счёт
    вместимости не идут; одна цифра-акцент крупнее прочих (Education: 88 при четырёх по 44) — не помеха."""
    sizes = [s.size_pt or 0 for s in slots if s.kind == SlotKind.NUMBER]
    if not sizes:
        return 0
    ref = median(sizes)
    return sum(1 for v in sizes if not ref or v >= ref * BIG_NUMBER_SHARE)


def _tight_slots(slots: list[Slot], n: Needs) -> int:
    """Сколько слотов под пункты не вместят средний пункт даже при минимальном кегле
    («три крупных слова» HSE: 96 pt, 4 знака — пункт туда не ляжет, только «трекер…»)."""
    if not n.items or not slots:
        return 0
    avg = n.text_chars / n.items
    return sum(1 for s in slots if (cap := chars_at_scale(s, MIN_SIZE_SCALE)) and cap < avg * TIGHT_SHARE)


def _capacity_score(have: int, need: int, empty_penalty: float, short_penalty: float = 3.0) -> float:
    """Точное совпадение — бонус; нехватка слотов (контент уедет на продолжение) дороже пустых слотов."""
    if have == need:
        return 2.0
    if have < need:
        return -short_penalty * (need - have)
    return -empty_penalty * (have - need)


def _count_kinds(slots: list[Slot]) -> dict[SlotKind, int]:
    counts = {k: 0 for k in SlotKind}
    for s in slots:
        counts[s.kind] += 1
    return counts


__all__ = ["FALLBACKS", "FLEXIBLE", "Needs", "candidate_archetypes", "pick_exemplar", "score_exemplar"]
