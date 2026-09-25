"""Подбор слайда-образца под спланированный слайд: цепочка архетипов-кандидатов и скор по вместимости."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from statistics import median

from deckforge.core.ir import Archetype, Box, Exemplar, OutlineSlide, Slot, SlotKind
from deckforge.core.strategy import Strategy
from deckforge.core.units import EMU_PER_INCH
from deckforge.layout.fitting import MIN_SIZE_SCALE, chars_at_scale, text_min_scale, title_min_scale

# хвост цепочки: текстовые образцы, затем структурные — чтобы слайд не пропал на любом шаблоне
_TEXT_TAIL = [Archetype.BULLETS, Archetype.CARDS, Archetype.TWO_COLUMN, Archetype.IMAGE_TEXT, Archetype.AGENDA]
_LAST = [Archetype.SECTION, Archetype.TITLE, Archetype.CLOSING]
FALLBACKS: dict[Archetype, list[Archetype]] = {
    Archetype.TITLE: [Archetype.SECTION, Archetype.CLOSING, *_TEXT_TAIL],
    Archetype.SECTION: [Archetype.TITLE, Archetype.CLOSING, *_TEXT_TAIL],
    Archetype.AGENDA: [Archetype.CARDS, Archetype.BULLETS, *_LAST],
    Archetype.BULLETS: [Archetype.CARDS, Archetype.TWO_COLUMN, Archetype.IMAGE_TEXT, Archetype.AGENDA, *_LAST],
    Archetype.TWO_COLUMN: [Archetype.CARDS, Archetype.BULLETS, Archetype.IMAGE_TEXT, *_LAST],
    Archetype.CARDS: [Archetype.BULLETS, Archetype.TWO_COLUMN, Archetype.AGENDA, *_LAST],
    Archetype.KPI: [Archetype.CARDS, Archetype.BULLETS, *_LAST],
    Archetype.CHART: [Archetype.TABLE, *_TEXT_TAIL],
    Archetype.TABLE: [Archetype.CHART, Archetype.CARDS, *_TEXT_TAIL],
    Archetype.PROCESS: [Archetype.CARDS, Archetype.AGENDA, Archetype.BULLETS, *_LAST],
    Archetype.QUOTE: [Archetype.SECTION, Archetype.IMAGE_TEXT, Archetype.BULLETS, Archetype.TITLE],  # цитата крупно
    Archetype.IMAGE_TEXT: [Archetype.BULLETS, Archetype.CARDS, *_LAST],
    Archetype.IMAGE_FULL: [Archetype.IMAGE_TEXT, *_TEXT_TAIL, *_LAST],
    Archetype.TEAM: [Archetype.CARDS, *_TEXT_TAIL, *_LAST],
    Archetype.CLOSING: [Archetype.TITLE, Archetype.SECTION, *_TEXT_TAIL],
    Archetype.FREEFORM: [Archetype.BULLETS, Archetype.CARDS, Archetype.TWO_COLUMN, *_LAST],
}
STRUCTURAL = (Archetype.TITLE, Archetype.SECTION, Archetype.CLOSING)
DIAGRAM_CHAIN = [Archetype.BULLETS, Archetype.TWO_COLUMN, Archetype.CARDS, Archetype.IMAGE_TEXT, Archetype.AGENDA]
NON_CONTENT_KINDS = {SlotKind.TITLE, SlotKind.SUBTITLE, SlotKind.SLIDE_NUMBER, SlotKind.FOOTER, SlotKind.DATE}
# текстовые архетипы, между которыми стратегия выбирает сама
FLEXIBLE = {Archetype.BULLETS, Archetype.CARDS, Archetype.TWO_COLUMN, Archetype.IMAGE_TEXT}
ARCH_RANK_PENALTY = 4.0  # шаг по цепочке фолбэков
FLEX_RANK_PENALTY = 2.0  # шаг по archetype_priority стратегии
REUSE_PENALTY = 2.0
# штраф за повтор растёт до трёх использований: дальше пустая карточка (EMPTY_CARD_PENALTY) не должна становиться
# «выгоднее» — в колоде из одних карточек (режим «только шаблон») иначе всплывала сетка 2×2 с пустой «04»
REUSE_MAX_COUNT = 3
NO_TITLE_PENALTY = 5.0
TRUNCATION_PENALTY = 3.0  # текст не влезет даже при минимальном кегле
BODY_TRUNCATION_PENALTY = 6.0  # весь текст слайда в одном теле и он не влезает — теряется хвост абзаца («Оплата…»)
MIN_TABLE_COL_W = int(1.0 * EMU_PER_INCH)
OVERLAP_PENALTY = 5.0  # заголовок образца заходит под контентный блок
OVERLAP_SHARE = 0.2
# ...и сам заголовок длиннее видимой части строки: текст переносится по всей ширине бокса и уходит под блок
TITLE_UNDER_CONTENT_PENALTY = 8.0
EXTRA_PICTURE_PENALTY = 2.5  # за каждую рамку под картинку сверх одной
EXTRA_DATA_SLOT_PENALTY = 3.0  # за каждую диаграмму / таблицу образца сверх одной (рендер уберёт её, останется дыра)
# рамка, в которую ляжет картинка слайда, — подложка под текст образца (VK Tech slide32: код на тёмной картинке):
# цвет текста рассчитан на подложку шаблона, на сгенерированной иллюстрации контраст — дело случая
BACKDROP_PICTURE_PENALTY = 8.0
BACKDROP_SHARE = 0.5  # текстовый слот лежит на рамке этой долей своей площади
DATA_IN_BODY_PENALTY = 6.0  # chart/table на место текстового блока
TIGHT_SHARE = 1.0  # слот «тесный», если даже при минимальном кегле не вмещает средний пункт — иначе «Соответствие…»
# пустая карточка сетки видна (у VK Tech сетка 2×2 с «01–04» нарисована картинкой в фоне лейаута и не убирается),
# судья считает её мусором (C07) — поэтому она дороже двух повторов образца
EMPTY_CARD_PENALTY = 5.0
REMOVABLE_CARD_PENALTY = 1.5  # у карточки своя подложка (Exemplar.card_frames) — пустую рендер убирает
TIGHT_SLOT_PENALTY = 4.0  # за каждый тесный слот: пункт в нём обрежется «…» — дороже одного пункта на продолжении
SHORT_CARD_PENALTY = 6.0  # карточек меньше пунктов — остаток уйдёт на продолжение: дороже пустой карточки
SPARSE_GRID_PENALTY = 4.0  # заполнено не больше половины карточек: один абзац в сетке 2×2
DIAGRAM_MIN_AREA = 0.15  # body под схему меньше этой доли слайда — шевроны не читаются
DIAGRAM_GOOD_AREA = 0.4  # с этой доли — полный бонус
DIAGRAM_MIN_ASPECT = 1.3  # ряд шевронов: бокс шире высоты
DIAGRAM_GRID_MATCH = 2.0  # колонок сетки образца столько же, сколько шагов — шаг в своей карточке
DIAGRAM_GRID_MISMATCH = 8.0  # иначе шевроны лягут поперёк неубираемых карточек
BIG_NUMBER_SHARE = 0.65  # доля от медианного кегля, с которой number-слот считается KPI-цифрой
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
    item_chars: tuple[int, ...] = ()  # длина каждого пункта — тесноту слота считаем к его пункту, а не к среднему
    diagram: int = 0  # шагов схемы SmartArt (DiagramSpec): ей нужен один широкий body

    @classmethod
    def of(cls, s: OutlineSlide) -> Needs:
        lst = s.bullets or s.steps or s.paragraphs
        return cls(items=len(lst), kpis=len(s.kpis), chart=s.chart is not None, table=s.table is not None,
                   image=has_image(s), quote=bool(s.quote), title_chars=len(s.title),
                   text_chars=sum(len(t) for t in lst) + 2 * len(lst), table_cols=len(s.table.header) if s.table else 0,
                   quote_chars=len(s.quote or "") + 2, item_chars=tuple(len(t) for t in lst),
                   diagram=len(s.diagram.items) if s.diagram else 0)


def has_image(s: OutlineSlide) -> bool:
    """Картинка слайда готова к вставке: путь задан и файл существует."""
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
    """Лучший образец и его скор; (None, IMPOSSIBLE), если ни один не годится. `exclude` — уже отвергнутые."""
    used = used or {}
    needs = Needs.of(slide)
    # схеме SmartArt нужен образец с одним широким текстовым блоком, а не process-образец шаблона
    chain = DIAGRAM_CHAIN if needs.diagram else candidate_archetypes(slide.archetype, strategy)
    rank_penalty = FLEX_RANK_PENALTY if slide.archetype in FLEXIBLE else ARCH_RANK_PENALTY
    # структурные образцы для контентного слайда — последний резерв, иначе штраф за повторы
    # сделал бы титул «выгоднее» карточек; цитата — исключение, ей section/title подходят
    passes = [True] if slide.archetype in STRUCTURAL or slide.archetype == Archetype.QUOTE else [False, True]
    for allow_structural in passes:
        best: tuple[float, int, Exemplar] | None = None
        for rank, arch in enumerate(chain):
            if not allow_structural and arch in STRUCTURAL:
                continue
            for e in exemplars:
                if e.archetype != arch or e.archetype == Archetype.FREEFORM or (exclude and e.id in exclude):
                    continue
                score = score_exemplar(e, needs, strategy, slide_area) - rank_penalty * rank - REUSE_PENALTY * min(used.get(e.id, 0), REUSE_MAX_COUNT)
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
    title_scale = title_min_scale(titles[0], e.archetype in STRUCTURAL) if (titles := [s for s in e.slots if s.kind == SlotKind.TITLE]) else MIN_SIZE_SCALE
    if n.title_chars and not (titles or n.quote):
        score -= NO_TITLE_PENALTY * (0.4 if kinds[SlotKind.SUBTITLE] else 1.0)  # заголовок ляжет в subtitle
    elif titles and n.title_chars and n.title_chars > (chars_at_scale(titles[0], title_scale) or n.title_chars):
        score -= TRUNCATION_PENALTY
    if titles and _overlaps_content(titles[0], e.slots):
        score -= OVERLAP_PENALTY
        if n.title_chars > _visible_title_chars(titles[0], e.slots, title_scale):
            score -= TITLE_UNDER_CONTENT_PENALTY
    real_bodies = [s for s in e.slots if s.kind == SlotKind.BODY]
    bodies = real_bodies
    if not bodies and e.archetype in STRUCTURAL and (n.items or n.kpis):
        bodies = [s for s in e.slots if s.kind == SlotKind.SUBTITLE]  # подзаголовок титула как тело
    if n.chart or n.table:
        data_slots = [s for s in e.slots if s.kind in (SlotKind.CHART, SlotKind.TABLE)]
        if not data_slots:
            if not real_bodies:
                return IMPOSSIBLE
            data_slots = [max(real_bodies, key=lambda s: s.box.w * s.box.h)]
            bodies = [s for s in bodies if s is not data_slots[0]]
            score -= DATA_IN_BODY_PENALTY
        else:
            score += 2.0 if (n.chart and kinds[SlotKind.CHART]) or (n.table and kinds[SlotKind.TABLE]) else 0.0
            # на слайде один объект данных: лишняя диаграмма образца уйдёт вместе с цифрами шаблона и оставит дыру
            score -= EXTRA_DATA_SLOT_PENALTY * (len(data_slots) - 1)
        if n.table_cols and data_slots[0].box.w / n.table_cols < MIN_TABLE_COL_W:
            score -= TRUNCATION_PENALTY
    if n.diagram:
        # схема занимает всю контентную область образца (карточки, иконки, картинки уходят), заголовок остаётся
        box = content_box(e.slots)
        if not real_bodies or box is None:
            return IMPOSSIBLE
        share = box.w * box.h / slide_area if slide_area else 0.0
        if share < DIAGRAM_MIN_AREA or box.w < box.h * DIAGRAM_MIN_ASPECT:
            return IMPOSSIBLE
        if titles and _intersection(titles[0].box, box) > OVERLAP_SHARE * titles[0].box.w * titles[0].box.h:
            return IMPOSSIBLE  # заголовок внутри контентной области — схема легла бы на него
        score += 4.0 * min(1.0, share / DIAGRAM_GOOD_AREA)
        cols = _body_columns(real_bodies)
        if cols > 1:
            # сетка без своих подложек (нарисована в фоне лейаута) не уберётся, а свои подложки бывают
            # фиксированными элементами шаблона (ЛЦТ2026 slide11: две белые карточки на всех слайдах) и тоже
            # остаются: шаги должны лечь по колонкам, иначе шевроны идут поперёк карточек
            score += DIAGRAM_GRID_MATCH if cols == n.diagram else -DIAGRAM_GRID_MISMATCH
        bodies = []
    if n.quote:  # нужен body под текст либо крупный заголовок
        if e.archetype in (Archetype.SECTION, Archetype.TITLE):
            if not titles:
                return IMPOSSIBLE
        elif not bodies:
            return IMPOSSIBLE
        elif (cap := chars_at_scale(bodies[0], MIN_SIZE_SCALE)) and n.quote_chars > cap:
            score -= TRUNCATION_PENALTY
    if n.kpis:
        if kinds[SlotKind.NUMBER]:
            score += _capacity_score(_big_numbers(e.slots), n.kpis, empty_penalty=2.0, short_penalty=5.0)
        elif e.archetype == Archetype.CARDS:  # значение → label, описание → body
            score += _capacity_score(kinds[SlotKind.LABEL], n.kpis, empty_penalty=1.5, short_penalty=5.0) - 1.0
        elif len(bodies) == 1 and (bodies[0].max_items or 0) >= n.kpis:  # список «значение — подпись»
            score -= 1.0
        else:
            score -= 3.0 * n.kpis
    # один body вмещает max_items пунктов, несколько body — по пункту на карточку
    if n.items:
        labels = kinds[SlotKind.LABEL] + kinds[SlotKind.CAPTION]
        if len(bodies) == 1:
            # однострочное тело (подпись-примечание) без max_items — один пункт, а не шесть по умолчанию
            cap = bodies[0].max_items or bodies[0].max_lines or 6
            score += 1.0 if n.items <= cap else -1.5 * (n.items - cap)
            if (chars := chars_at_scale(bodies[0], MIN_SIZE_SCALE)) and n.text_chars > chars:
                score -= BODY_TRUNCATION_PENALTY
        elif len(bodies) > 1:
            # пустую карточку со своей подложкой рендер уберёт — остаётся только пробел в сетке
            score += _capacity_score(len(bodies), n.items,
                                     empty_penalty=REMOVABLE_CARD_PENALTY if e.card_frames else EMPTY_CARD_PENALTY,
                                     short_penalty=SHORT_CARD_PENALTY)
            if n.items * 2 <= len(bodies):
                score -= SPARSE_GRID_PENALTY
            if kinds[SlotKind.LABEL] and kinds[SlotKind.LABEL] != len(bodies):  # нерегулярная сетка карточек
                score -= 1.0 * abs(kinds[SlotKind.LABEL] - len(bodies))
            score -= TIGHT_SLOT_PENALTY * _tight_slots(bodies[: n.items], n)
        elif labels:
            score += _capacity_score(labels, n.items, empty_penalty=EMPTY_CARD_PENALTY) - 1.0
            if n.items * 2 <= labels:
                score -= SPARSE_GRID_PENALTY
            score -= TIGHT_SLOT_PENALTY * _tight_slots([s for s in e.slots if s.kind in (SlotKind.LABEL, SlotKind.CAPTION)][: n.items], n)
        else:
            score -= 2.0 * n.items
    # без картинки в контенте рамка образца останется с чужим фото
    pics, icons = kinds[SlotKind.PICTURE], kinds[SlotKind.ICON]
    if pics:
        if n.image:
            score += {"minimal": 0.5, "preferred": 3.0, "always": 5.0}[strategy.images]
            score -= EXTRA_PICTURE_PENALTY * (pics - 1)
            if _is_backdrop(_first_picture(e.slots, slide_area), e.slots):
                score -= BACKDROP_PICTURE_PENALTY
        else:
            # картинки генерируются до вёрстки: нет image — её и не будет (лимит на колоду, сбой T2I),
            # поэтому visual штрафуется как narrative — иначе пустая рамка или мокап устройства без экрана
            score += {"minimal": -3.0, "preferred": -5.0, "always": -5.0}[strategy.images]
    if icons and strategy.icons:
        score += 1.0
    fill = min(1.0, sum(s.box.w * s.box.h for s in e.slots) / slide_area) if slide_area else 0.0
    if fill > strategy.density.max_fill_ratio:
        score -= (fill - strategy.density.max_fill_ratio) * 10.0
    return score


def _body_columns(bodies: list[Slot]) -> int:
    """Сколько колонок в сетке тел: левые кромки ближе половины самого узкого тела — одна колонка."""
    if not bodies:
        return 0
    tol = min(b.box.w for b in bodies) // 2
    cols: list[int] = []
    for x in sorted(b.box.x for b in bodies):
        if not cols or x - cols[-1] > tol:
            cols.append(x)
    return len(cols)


def _first_picture(slots: list[Slot], slide_area: int) -> Slot:
    """Рамка, куда builder положит картинку: первая в порядке чтения (`builder._slots_by_kind` — ряд по 1/12 высоты).

    Высота слайда — из площади при 16:9: она нужна только для группировки в ряды."""
    row = max(1, int((slide_area * 9 / 16) ** 0.5) // 12)
    return min((s for s in slots if s.kind == SlotKind.PICTURE), key=lambda s: (s.box.y // row, s.box.x))


def _is_backdrop(pic: Slot, slots: list[Slot]) -> bool:
    """Поверх рамки под картинку лежит текст образца (тело, подпись, цифра) — это подложка, а не место для иллюстрации."""
    return any(_intersection(pic.box, s.box) >= BACKDROP_SHARE * max(1, s.box.w * s.box.h) for s in slots
               if s.kind in (SlotKind.BODY, SlotKind.LABEL, SlotKind.CAPTION, SlotKind.NUMBER))


def _intersection(a: Box, b: Box) -> int:
    w = min(a.x2, b.x2) - max(a.x, b.x)
    h = min(a.y2, b.y2) - max(a.y, b.y)
    return w * h if w > 0 and h > 0 else 0


def content_box(slots: list[Slot]) -> Box | None:
    """Контентная область образца: объединение слотов, кроме заголовков и полей (номер, колонтитул, дата)."""
    boxes = [s.box for s in slots if s.kind not in NON_CONTENT_KINDS]
    if not boxes:
        return None
    x, y = min(b.x for b in boxes), min(b.y for b in boxes)
    return Box(x=x, y=y, w=max(b.x2 for b in boxes) - x, h=max(b.y2 for b in boxes) - y)


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


def _visible_title_chars(title: Slot, slots: list[Slot], scale: float) -> int:
    """Сколько знаков заголовка уместится в одну строку на свободной от контента части бокса."""
    left, right = title.box.x, title.box.x2
    for s in slots:
        if s.kind not in (SlotKind.BODY, SlotKind.PICTURE, SlotKind.CHART, SlotKind.TABLE):
            continue
        if min(title.box.y2, s.box.y2) <= max(title.box.y, s.box.y) or s.box.x2 <= left or s.box.x >= right:
            continue
        # блок справа обрезает строку справа, блок слева — слева
        if s.box.x > left + (right - left) / 2:
            right = s.box.x
        else:
            left = max(left, s.box.x2)
    share = max(0, right - left) / max(1, title.box.w)
    cpl = (title.max_chars or 0) / max(1, title.max_lines or 1)
    return int(cpl * share / scale)


def _big_numbers(slots: list[Slot]) -> int:
    """Сколько number-слотов «крупные» — не мельче BIG_NUMBER_SHARE от медианного кегля цифр образца."""
    sizes = [s.size_pt or 0 for s in slots if s.kind == SlotKind.NUMBER]
    if not sizes:
        return 0
    ref = median(sizes)
    return sum(1 for v in sizes if not ref or v >= ref * BIG_NUMBER_SHARE)


def _tight_slots(slots: list[Slot], n: Needs) -> int:
    """Сколько слотов не вместят свой пункт даже при минимальном кегле (пункт i — в слот i, как раскладывает builder)."""
    if not n.items or not slots:
        return 0
    avg = n.text_chars / n.items
    lens = n.item_chars or (avg,) * len(slots)
    return sum(1 for s, need in zip(slots, lens) if (cap := chars_at_scale(s, text_min_scale(s))) and cap < need * TIGHT_SHARE)


def _capacity_score(have: int, need: int, empty_penalty: float, short_penalty: float = 3.0) -> float:
    """Точное совпадение — бонус; нехватка слотов дороже пустых."""
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
