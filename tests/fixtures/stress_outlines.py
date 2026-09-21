"""Крайние DeckOutline для стресс-теста layout/render/audit.

Реестр `CASES` читают `tools/stress_test.py` и `tests/test_stress.py`. Outline минуют `repair_outline`
намеренно — так же приходят готовые файлы через `--outline`.
"""

from __future__ import annotations

import json
import math
from collections.abc import Callable
from pathlib import Path

from deckforge.core.ir import Archetype, ChartSpec, DeckOutline, ImageSpec, KpiSpec, OutlineSlide, TableSpec

REPO = Path(__file__).resolve().parents[2]
BASE_OUTLINE = REPO / "examples" / "content_pack" / "outline.json"

LONG_WORD = "https://example.com/" + "a" * 280
LONG_TITLE = ("Очень длинный заголовок, который модель написала одним предложением без точки и который не влезет ни в один слот " * 5).strip()
LONG_BULLET = ("Пункт с очень длинным текстом, описывающим одну мысль многословно, с уточнениями, примерами и оговорками, " * 4).strip()
EMOJI = "🚀 Запуск 👨‍👩‍👧‍👦 семьи 🇷🇺 флаг ✅"
RTL = "الفريق أطلق المنتج في الربع الأول"
CJK = "团队在第一季度推出了产品，用户增长显著"
XML_SPECIAL = "<b>жирный</b> & \"кавычки\" 'апостроф' </a:t>"
CONTROL = "до\x00после \x0bвертикальная \x1fтабуляция \x08backspace"
ZERO_WIDTH = "нулевая\u200bширина‍﻿BOM"


def base() -> DeckOutline:
    return DeckOutline.model_validate_json(BASE_OUTLINE.read_text("utf-8"))


def _outline(slides: list[OutlineSlide], title: str = "Стресс-тест") -> DeckOutline:
    for i, s in enumerate(slides):
        s.idx = i
    return DeckOutline(title=title, purpose="other", audience="тест", language="ru", slides=slides)


def _title(title: str = "Титул стресс-теста") -> OutlineSlide:
    return OutlineSlide(idx=0, archetype=Archetype.TITLE, title=title, subtitle="подзаголовок")


def _closing() -> OutlineSlide:
    return OutlineSlide(idx=0, archetype=Archetype.CLOSING, title="Спасибо!", bullets=["stress@example.com"])


def _wrap(middle: list[OutlineSlide], title: str = "Стресс-тест") -> DeckOutline:
    return _outline([_title(), *middle, _closing()], title)


def _chart(n_cats: int = 6, n_series: int = 1, kind: str = "column", values: Callable[[int, int], float] | None = None) -> ChartSpec:
    values = values or (lambda i, j: float(10 * (i + 1) + j))
    cats = [f"К{i + 1}" for i in range(n_cats)]
    return ChartSpec(kind=kind, title="", categories=cats,
                     series={f"Ряд {j + 1}": [values(i, j) for i in range(n_cats)] for j in range(n_series)})


def _table(rows: int = 7, cols: int = 5, cell_len: int = 8) -> TableSpec:
    header = [f"Колонка {j + 1}" for j in range(cols)]
    body = [[("Значение " * 40)[:cell_len].strip() if j else f"Строка {i + 1}" for j in range(cols)] for i in range(rows)]
    return TableSpec(header=header, rows=body)


def _kpis(n: int) -> list[KpiSpec]:
    return [KpiSpec(value=f"{(i + 1) * 17}%", label=f"метрика {i + 1}") for i in range(n)]


# ──────────────────────────── объём ────────────────────────────


def empty() -> DeckOutline:
    return _outline([])


def only_title() -> DeckOutline:
    return _outline([_title()])


def three_slides() -> DeckOutline:
    return _outline([_title(), OutlineSlide(idx=0, archetype=Archetype.BULLETS, title="Один тезис", bullets=["Пункт"]), _closing()])


def forty_slides() -> DeckOutline:
    src = base().slides[1:-1]
    middle = []
    for k in range(math.ceil(38 / len(src))):
        for s in src:
            c = s.model_copy(deep=True)
            c.title = f"{c.title} #{k + 1}"
            c.section = f"Раздел {k + 1}"
            middle.append(c)
    return _wrap(middle[:38])


# ──────────────────────────── однородные ────────────────────────────


def _homogeneous(make: Callable[[int], OutlineSlide], n: int = 20) -> DeckOutline:
    return _wrap([make(i) for i in range(n)])


def all_kpi() -> DeckOutline:
    return _homogeneous(lambda i: OutlineSlide(idx=0, archetype=Archetype.KPI, title=f"Метрики {i + 1}", kpis=_kpis(8)))


def all_chart() -> DeckOutline:
    kinds = ["bar", "column", "line", "pie", "doughnut", "area"]
    return _homogeneous(lambda i: OutlineSlide(idx=0, archetype=Archetype.CHART, title=f"Диаграмма {i + 1}",
                                               chart=_chart(4 + i % 5, 1 + i % 3, kinds[i % 6])))


def all_table() -> DeckOutline:
    return _homogeneous(lambda i: OutlineSlide(idx=0, archetype=Archetype.TABLE, title=f"Таблица {i + 1}", table=_table()))


def all_quote() -> DeckOutline:
    return _homogeneous(lambda i: OutlineSlide(idx=0, archetype=Archetype.QUOTE, title=f"Цитата {i + 1}",
                                               quote="Мы перестали спорить о нагрузке и начали её видеть" * (1 + i % 3),
                                               quote_author=None if i % 2 else "Тимлид платформы"))


def all_process() -> DeckOutline:
    return _homogeneous(lambda i: OutlineSlide(idx=0, archetype=Archetype.PROCESS, title=f"Процесс {i + 1}",
                                               steps=[f"Шаг {k + 1}: сделать что-то важное" for k in range(12)]))


def all_image_text_no_path() -> DeckOutline:
    return _homogeneous(lambda i: OutlineSlide(idx=0, archetype=Archetype.IMAGE_TEXT, title=f"Картинка {i + 1}",
                                               bullets=["Тезис к картинке", "Ещё тезис"], image=ImageSpec(prompt="закат")))


def image_missing_path() -> DeckOutline:
    s = OutlineSlide(idx=0, archetype=Archetype.IMAGE_TEXT, title="Картинки нет на диске", bullets=["Тезис"],
                     image=ImageSpec(path=str(REPO / "out" / "no_such_dir" / "missing.png")))
    return _wrap([s])


def image_files(tmp: Path) -> DeckOutline:
    """Реальные файлы: 1×1 PNG, 20000×1 PNG, CMYK JPEG, «картинка» из текста."""
    from PIL import Image

    tmp.mkdir(parents=True, exist_ok=True)
    paths = []
    Image.new("RGB", (1, 1), "red").save(p := tmp / "tiny.png"); paths.append(p)
    Image.new("RGB", (20000, 1), "blue").save(p := tmp / "wide.png"); paths.append(p)
    Image.new("CMYK", (300, 200), (0, 100, 100, 0)).save(p := tmp / "cmyk.jpg"); paths.append(p)
    (p := tmp / "fake.png").write_text("это не картинка", "utf-8"); paths.append(p)
    slides = [OutlineSlide(idx=0, archetype=Archetype.IMAGE_TEXT, title=f"Файл {p.name}", bullets=["Тезис"],
                           image=ImageSpec(path=str(p))) for p in paths]
    return _wrap(slides)


# ──────────────────────────── текст ────────────────────────────


def text_extremes() -> DeckOutline:
    slides = [
        OutlineSlide(idx=0, archetype=Archetype.BULLETS, title=LONG_TITLE, bullets=["Обычный пункт"]),
        OutlineSlide(idx=0, archetype=Archetype.BULLETS, title="URL без пробелов", bullets=[LONG_WORD, "Короткий"]),
        OutlineSlide(idx=0, archetype=Archetype.BULLETS, title="Шесть длинных", bullets=[LONG_BULLET] * 6),
        OutlineSlide(idx=0, archetype=Archetype.CARDS, title="Шесть длинных карточек", bullets=[f"Лид {i} — {LONG_BULLET}" for i in range(6)]),
        OutlineSlide(idx=0, archetype=Archetype.BULLETS, title="Пустые пункты", bullets=["", "   ", "\n\t", "Единственный настоящий"]),
        OutlineSlide(idx=0, archetype=Archetype.BULLETS, title="   ", bullets=["Заголовок из пробелов"]),
        OutlineSlide(idx=0, archetype=Archetype.BULLETS, title=EMOJI, bullets=[EMOJI, RTL, CJK, ZERO_WIDTH]),
        OutlineSlide(idx=0, archetype=Archetype.CARDS, title=XML_SPECIAL, bullets=[XML_SPECIAL, "Обычный — текст"]),
        OutlineSlide(idx=0, archetype=Archetype.BULLETS, title=CONTROL, bullets=[CONTROL], speaker_notes=CONTROL),
        OutlineSlide(idx=0, archetype=Archetype.KPI, title="Управляющие в KPI", kpis=[KpiSpec(value="4\x002%", label=CONTROL)]),
        OutlineSlide(idx=0, archetype=Archetype.PROCESS, title="Длинные шаги", steps=[LONG_BULLET] * 5),
        OutlineSlide(idx=0, archetype=Archetype.QUOTE, title="Длинная цитата", quote=LONG_BULLET * 3, quote_author=LONG_TITLE),
        OutlineSlide(idx=0, archetype=Archetype.BULLETS, title="Заметки 20k", bullets=["Пункт"], speaker_notes="заметка " * 2500),
        OutlineSlide(idx=0, archetype=Archetype.BULLETS, title="Один пункт из одной буквы", bullets=["я"]),
    ]
    return _wrap(slides)


# ──────────────────────────── числа ────────────────────────────


def numbers_extremes() -> DeckOutline:
    slides = [
        OutlineSlide(idx=0, archetype=Archetype.CHART, title="Одна категория", chart=_chart(1)),
        OutlineSlide(idx=0, archetype=Archetype.CHART, title="Пятьдесят категорий", chart=_chart(50)),
        OutlineSlide(idx=0, archetype=Archetype.CHART, title="Десять серий", chart=_chart(6, 10)),
        OutlineSlide(idx=0, archetype=Archetype.CHART, title="Отрицательные", chart=_chart(5, 2, "column", lambda i, j: (i - 2) * 10.0)),
        OutlineSlide(idx=0, archetype=Archetype.CHART, title="NaN и inf", chart=_chart(4, 1, "line", lambda i, j: [1.0, math.nan, math.inf, -math.inf][i])),
        OutlineSlide(idx=0, archetype=Archetype.CHART, title="Pie из нулей", chart=_chart(4, 1, "pie", lambda i, j: 0.0)),
        OutlineSlide(idx=0, archetype=Archetype.CHART, title="Pie с отрицательным", chart=_chart(3, 1, "pie", lambda i, j: -5.0 if i == 1 else 5.0)),
        OutlineSlide(idx=0, archetype=Archetype.CHART, title="Серия короче категорий",
                     chart=ChartSpec(kind="column", title="", categories=["а", "б", "в"], series={"x": [1.0]})),
        OutlineSlide(idx=0, archetype=Archetype.CHART, title="Серия длиннее категорий",
                     chart=ChartSpec(kind="line", title="", categories=["а"], series={"x": [1.0, 2.0, 3.0]})),
        OutlineSlide(idx=0, archetype=Archetype.CHART, title="Без категорий", chart=ChartSpec(kind="bar", title="", categories=[], series={"x": []})),
        OutlineSlide(idx=0, archetype=Archetype.CHART, title="Без серий", chart=ChartSpec(kind="bar", title="", categories=["а"], series={})),
        OutlineSlide(idx=0, archetype=Archetype.CHART, title="Огромные числа", chart=_chart(3, 1, "column", lambda i, j: 10.0 ** (12 + i))),
        OutlineSlide(idx=0, archetype=Archetype.TABLE, title="Таблица без строк", table=TableSpec(header=["а", "б"], rows=[])),
        OutlineSlide(idx=0, archetype=Archetype.TABLE, title="Таблица одна колонка", table=_table(5, 1)),
        OutlineSlide(idx=0, archetype=Archetype.TABLE, title="Таблица без колонок", table=TableSpec(header=[], rows=[[]])),
        OutlineSlide(idx=0, archetype=Archetype.TABLE, title="Ячейки по 1000", table=_table(3, 3, 1000)),
        OutlineSlide(idx=0, archetype=Archetype.TABLE, title="Рваные строки",
                     table=TableSpec(header=["а", "б", "в"], rows=[["1"], [], ["1", "2", "3", "4", "5"]])),
        OutlineSlide(idx=0, archetype=Archetype.TABLE, title="Числовая таблица 12×5", table=TableSpec(
            header=["Месяц", "Выручка", "Δ, %", "Клиенты", "Churn"],
            rows=[[f"М{i}", str(100 + i), f"+{i}", str(1000 * i), f"{i / 10:.1f}"] for i in range(12)])),
        OutlineSlide(idx=0, archetype=Archetype.KPI, title="Странные KPI", kpis=[
            KpiSpec(value="—", label="прочерк"), KpiSpec(value="", label="пусто"), KpiSpec(value="N/A", label="нет данных"),
            KpiSpec(value="≈42 %", label="примерно")]),
        OutlineSlide(idx=0, archetype=Archetype.KPI, title="Огромное значение", kpis=[
            KpiSpec(value="1 000 000 000 000 ₽", label="триллион"), KpiSpec(value="0,0001", label="мало"),
            KpiSpec(value="−17,5 п.п.", label="минус"), KpiSpec(value="x3,5", label="кратно")]),
        OutlineSlide(idx=0, archetype=Archetype.KPI, title="KPI с буллетами и диаграммой", kpis=_kpis(2),
                     bullets=["Пояснение один", "Пояснение два"], chart=_chart(3)),
        OutlineSlide(idx=0, archetype=Archetype.CARDS, title="Карточки с KPI", kpis=_kpis(3)),
        OutlineSlide(idx=0, archetype=Archetype.KPI, title="KPI без label", kpis=[KpiSpec(value="42", label="")]),
    ]
    return _wrap(slides)


# ──────────────────────────── разделы и служебное ────────────────────────────


def many_sections() -> DeckOutline:
    slides = [OutlineSlide(idx=0, archetype=Archetype.BULLETS, title=f"Тезис {i + 1}", section=f"Раздел {i + 1}",
                           bullets=[f"Пункт {i + 1}"], sources=[f"unknown_{i}"]) for i in range(30)]
    slides[0].section = ("Очень длинное название раздела " * 7).strip()
    slides.append(OutlineSlide(idx=0, archetype=Archetype.SECTION, title="Разделитель из outline", section="Явный"))
    return _wrap(slides)


def no_title_no_closing() -> DeckOutline:
    """Outline без титульного и финального."""
    return _outline([OutlineSlide(idx=0, archetype=Archetype.BULLETS, title="Сразу тезисы", bullets=["а", "б"]),
                     OutlineSlide(idx=0, archetype=Archetype.KPI, title="И цифры", kpis=_kpis(2))])


def mixed_everything() -> DeckOutline:
    """На одном слайде всё сразу: буллеты, шаги, KPI, диаграмма, таблица, цитата, картинка."""
    s = OutlineSlide(idx=0, archetype=Archetype.BULLETS, title="Всё сразу", subtitle="и подзаголовок", bullets=["а", "б"],
                     paragraphs=["абзац"], steps=["шаг 1", "шаг 2"], kpis=_kpis(2), chart=_chart(3), table=_table(2, 2),
                     quote="цитата", quote_author="автор", image=ImageSpec(prompt="x"), section="Секция")
    return _wrap([s, s.model_copy(deep=True)])


def rare_archetypes() -> DeckOutline:
    """Архетипы, которые модель не предлагает, но в готовом outline встречаются."""
    slides = [OutlineSlide(idx=0, archetype=a, title=f"Слайд {a.value}", bullets=["раз", "два", "три"], steps=["ш1"])
              for a in (Archetype.AGENDA, Archetype.TEAM, Archetype.IMAGE_FULL, Archetype.FREEFORM, Archetype.SECTION,
                        Archetype.TWO_COLUMN, Archetype.TITLE, Archetype.CLOSING)]
    return _wrap(slides)


CASES: dict[str, Callable[[], DeckOutline]] = {
    "empty": empty,
    "only_title": only_title,
    "three_slides": three_slides,
    "forty_slides": forty_slides,
    "all_kpi": all_kpi,
    "all_chart": all_chart,
    "all_table": all_table,
    "all_quote": all_quote,
    "all_process": all_process,
    "all_image_text_no_path": all_image_text_no_path,
    "image_missing_path": image_missing_path,
    "text_extremes": text_extremes,
    "numbers_extremes": numbers_extremes,
    "many_sections": many_sections,
    "no_title_no_closing": no_title_no_closing,
    "mixed_everything": mixed_everything,
    "rare_archetypes": rare_archetypes,
}


def all_cases(tmp: Path | None = None) -> dict[str, DeckOutline]:
    out = {name: fn() for name, fn in CASES.items()}
    if tmp is not None:
        out["image_files"] = image_files(tmp / "images")
    return out


def dump(outline: DeckOutline, path: Path) -> Path:
    path.write_text(json.dumps(outline.model_dump(mode="json"), ensure_ascii=False, indent=1), "utf-8")
    return path


__all__ = ["CASES", "all_cases", "base", "dump", "image_files"]
