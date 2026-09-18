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
from pathlib import Path
from statistics import median

from deckforge.core.ir import Archetype, Exemplar, OutlineSlide, Slot, SlotKind
from deckforge.core.strategy import Strategy
from deckforge.core.units import EMU_PER_INCH
from deckforge.layout.fitting import MIN_SIZE_SCALE, chars_at_scale

# хвост цепочки для любого контентного слайда: текстовые образцы, затем структурные (section/title — текст ляжет
# в подзаголовок; см. LAST_RESORT в builder) — чтобы на шаблоне из одних «заголовок + абзац» слайды не пропадали
_TEXT_TAIL = [Archetype.BULLETS, Archetype.CARDS, Archetype.TWO_COLUMN, Archetype.IMAGE_TEXT, Archetype.AGENDA]
_LAST = [Archetype.SECTION, Archetype.TITLE, Archetype.CLOSING]
FALLBACKS: dict[Archetype, list[Archetype]] = {
    Archetype.TITLE: [Archetype.SECTION, Archetype.CLOSING, *_TEXT_TAIL],  # шаблон из одних bullets: титул на них
    Archetype.SECTION: [Archetype.TITLE, Archetype.CLOSING, *_TEXT_TAIL],
    Archetype.AGENDA: [Archetype.CARDS, Archetype.BULLETS, *_LAST],
    Archetype.BULLETS: [Archetype.CARDS, Archetype.TWO_COLUMN, Archetype.IMAGE_TEXT, Archetype.AGENDA, *_LAST],
    Archetype.TWO_COLUMN: [Archetype.CARDS, Archetype.BULLETS, Archetype.IMAGE_TEXT, *_LAST],
    Archetype.CARDS: [Archetype.BULLETS, Archetype.TWO_COLUMN, Archetype.AGENDA, *_LAST],
    Archetype.KPI: [Archetype.CARDS, Archetype.BULLETS, *_LAST],  # bullets: строки «значение — подпись», когда цифр/карточек нет
    Archetype.CHART: [Archetype.TABLE, *_TEXT_TAIL],  # без data-слота нативный объект встаёт на место body
    Archetype.TABLE: [Archetype.CHART, Archetype.CARDS, *_TEXT_TAIL],
    Archetype.PROCESS: [Archetype.CARDS, Archetype.AGENDA, Archetype.BULLETS, *_LAST],
    Archetype.QUOTE: [Archetype.SECTION, Archetype.IMAGE_TEXT, Archetype.BULLETS, Archetype.TITLE],  # section/title: цитата в крупный заголовок
    Archetype.IMAGE_TEXT: [Archetype.BULLETS, Archetype.CARDS, *_LAST],
    Archetype.IMAGE_FULL: [Archetype.IMAGE_TEXT, *_TEXT_TAIL, *_LAST],
    Archetype.TEAM: [Archetype.CARDS, *_TEXT_TAIL, *_LAST],
    Archetype.CLOSING: [Archetype.TITLE, Archetype.SECTION, *_TEXT_TAIL],
    Archetype.FREEFORM: [Archetype.BULLETS, Archetype.CARDS, Archetype.TWO_COLUMN, *_LAST],  # «не образец» в готовом outline — как текст
}
STRUCTURAL = (Archetype.TITLE, Archetype.SECTION, Archetype.CLOSING)
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
DATA_IN_BODY_PENALTY = 6.0  # chart/table на место текстового блока — только когда data-слота нет во всём шаблоне
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
                   image=has_image(s), quote=bool(s.quote), title_chars=len(s.title),
                   text_chars=sum(len(t) for t in lst) + 2 * len(lst), table_cols=len(s.table.header) if s.table else 0,
                   quote_chars=len(s.quote or "") + 2)


def has_image(s: OutlineSlide) -> bool:
    """Картинка слайда готова к вставке: путь задан и файл существует (иначе picture-образец оставит заглушку,
    а рендер — не должен падать из-за оборванной генерации или битого кэша)."""
    return bool(s.image and s.image.path and Path(s.image.path).is_file())


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
    used: dict[str, int] | None = None, exclude: set[str] | None = None,
) -> tuple[Exemplar | None, float]:
    """Лучший образец и его скор; (None, IMPOSSIBLE), если ни один не годится. `exclude` — id образцов,
    которые builder уже пробовал и в которые контент не лёг."""
    used = used or {}
    needs = Needs.of(slide)
    chain = candidate_archetypes(slide.archetype, strategy)
    rank_penalty = FLEX_RANK_PENALTY if slide.archetype in FLEXIBLE else ARCH_RANK_PENALTY
    # структурные образцы (title/section/closing) для контентного слайда — строго последний резерв: их берём
    # только когда контентных кандидатов нет вовсе, иначе на длинной колоде накопленный штраф за повторы
    # карточек сделал бы титул «выгоднее» (текст ушёл бы в подзаголовок)
    # цитата — исключение: section/title в её FALLBACKS стоят первыми намеренно (цитата крупно в заголовок),
    # иначе на VK Tech без quote-образца она уходила в image_text-мокап с телом на 21 символ (скор −10)
    passes = [True] if slide.archetype in STRUCTURAL or slide.archetype == Archetype.QUOTE else [False, True]
    for allow_structural in passes:
        best: tuple[float, int, Exemplar] | None = None
        for rank, arch in enumerate(chain):
            if not allow_structural and arch in STRUCTURAL:
                continue
            for e in exemplars:
                if e.archetype != arch or e.archetype == Archetype.FREEFORM or (exclude and e.id in exclude):
                    continue
                score = score_exemplar(e, needs, strategy, slide_area) - rank_penalty * rank - REUSE_PENALTY * used.get(e.id, 0)
                if score <= IMPOSSIBLE:
                    continue
                key = (score, -e.source_index, e)
                if best is None or key[:2] > best[:2]:
                    best = key
        if best is not None:
            return best[2], best[0]
    return None, IMPOSSIBLE


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
    real_bodies = [s for s in e.slots if s.kind == SlotKind.BODY]
    bodies = real_bodies
    if not bodies and e.archetype in STRUCTURAL and (n.items or n.kpis):
        bodies = [s for s in e.slots if s.kind == SlotKind.SUBTITLE]  # подзаголовок титула как тело (builder делает то же)
    if n.chart or n.table:
        data_slots = [s for s in e.slots if s.kind in (SlotKind.CHART, SlotKind.TABLE)]
        if not data_slots:
            if not real_bodies:
                return IMPOSSIBLE
            data_slots = [max(real_bodies, key=lambda s: s.box.w * s.box.h)]  # нативный объект на место текстового блока
            bodies = [s for s in bodies if s is not data_slots[0]]
            score -= DATA_IN_BODY_PENALTY
        else:
            score += 2.0 if (n.chart and kinds[SlotKind.CHART]) or (n.table and kinds[SlotKind.TABLE]) else 0.0
        if n.table_cols and data_slots[0].box.w / n.table_cols < MIN_TABLE_COL_W:
            score -= TRUNCATION_PENALTY
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


__all__ = ["FALLBACKS", "FLEXIBLE", "STRUCTURAL", "Needs", "candidate_archetypes", "has_image", "pick_exemplar", "score_exemplar"]
