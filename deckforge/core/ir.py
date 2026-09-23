"""Промежуточные модели данных (IR) — единственный контракт между слоями пайплайна.

Поток: .pptx ──parsing──▶ TemplateDNA
       бриф ──content──▶ DeckOutline
       (TemplateDNA, DeckOutline, Strategy) ──layout──▶ DeckIR
       DeckIR ──render──▶ .pptx ──audit──▶ AuditReport ──export──▶ .pdf/.html

Все координаты — в EMU (как в OOXML, 914400 EMU = 1 дюйм), см. core/units.py.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field

# ──────────────────────────────── общее ────────────────────────────────


class Box(BaseModel):
    """Прямоугольник в EMU."""

    x: int
    y: int
    w: int
    h: int

    @property
    def x2(self) -> int:
        return self.x + self.w

    @property
    def y2(self) -> int:
        return self.y + self.h

    def intersects(self, other: Box, tolerance: int = 0) -> bool:
        return not (
            self.x2 - tolerance <= other.x
            or other.x2 - tolerance <= self.x
            or self.y2 - tolerance <= other.y
            or other.y2 - tolerance <= self.y
        )


class Archetype(StrEnum):
    """Композиционные архетипы слайдов. Определяются по геометрии, не по именам лейаутов."""

    TITLE = "title"  # титульный: крупный заголовок, подзаголовок, дата/автор
    SECTION = "section"  # разделитель раздела
    AGENDA = "agenda"  # содержание / оглавление
    BULLETS = "bullets"  # заголовок + список
    TWO_COLUMN = "two_column"  # заголовок + две текстовые колонки
    CARDS = "cards"  # сетка из N одинаковых карточек: подпись + текст (+иконка)
    KPI = "kpi"  # 2–4 крупные цифры (фактоиды) с подписями
    CHART = "chart"  # заголовок + диаграмма (+ вывод)
    TABLE = "table"  # заголовок + таблица
    IMAGE_TEXT = "image_text"  # картинка + текст
    IMAGE_FULL = "image_full"  # полноэкранная картинка/мокап + подпись
    QUOTE = "quote"  # цитата
    PROCESS = "process"  # шаги / схема / SmartArt-подобная группа
    TEAM = "team"  # спикер / команда: фото + имя + роль
    CLOSING = "closing"  # финальный: спасибо, контакты, QR
    FREEFORM = "freeform"  # не удалось классифицировать


# Определения архетипов одной строкой — для промптов (template_tagger, outline_writer) и отчётов.
ARCHETYPE_HINTS: dict[Archetype, str] = {
    Archetype.TITLE: "титульный: крупный заголовок, подзаголовок, автор/дата",
    Archetype.SECTION: "разделитель: один крупный заголовок, почти пустой слайд",
    Archetype.AGENDA: "содержание: нумерованный/вертикальный список разделов",
    Archetype.BULLETS: "заголовок + один текстовый блок/список",
    Archetype.TWO_COLUMN: "заголовок + две текстовые колонки",
    Archetype.CARDS: "сетка из 3+ одинаковых карточек: подпись + текст (+иконка)",
    Archetype.KPI: "2–4 крупные цифры с подписями",
    Archetype.CHART: "заголовок + диаграмма (нативная или нарисованная/картинка)",
    Archetype.TABLE: "заголовок + таблица",
    Archetype.IMAGE_TEXT: "картинка/фото/мокап + текстовый блок рядом",
    Archetype.IMAGE_FULL: "полноэкранная картинка/скриншот/мокап, максимум подпись",
    Archetype.QUOTE: "цитата и автор",
    Archetype.PROCESS: "шаги/таймлайн/схема: элементы, связанные линиями или нумерацией в ряд",
    Archetype.TEAM: "спикер/команда: фото + имя + роль",
    Archetype.CLOSING: "финальный: спасибо, контакты, QR",
    Archetype.FREEFORM: "не образец: библиотека иконок, инструкция, пустой слайд",
}


class SlotKind(StrEnum):
    TITLE = "title"
    SUBTITLE = "subtitle"
    BODY = "body"  # абзацы / буллеты
    CAPTION = "caption"
    NUMBER = "number"  # крупная цифра в KPI
    LABEL = "label"  # подпись к цифре / колонке
    PICTURE = "picture"
    CHART = "chart"
    TABLE = "table"
    ICON = "icon"
    DATE = "date"
    FOOTER = "footer"
    SLIDE_NUMBER = "slide_number"
    OTHER = "other"


# ──────────────────────────────── TemplateDNA ────────────────────────────────


class ColorToken(BaseModel):
    hex: str = Field(pattern=r"^[0-9A-F]{6}$")
    role: Literal["background", "text", "accent", "secondary", "muted", "surface", "unknown"] = "unknown"
    usage: int = 0  # сколько раз встречается в шаблоне
    source: Literal["slides", "layouts", "master", "theme"] = "slides"


class TypeToken(BaseModel):
    role: Literal["display", "h1", "h2", "h3", "body", "caption", "small"]
    font: str
    size_pt: float
    bold: bool = False
    color: str | None = None
    usage: int = 0


class GridSpec(BaseModel):
    """Поля и направляющие, восстановленные по расположению фигур."""

    slide_w: int
    slide_h: int
    margin_left: int
    margin_right: int
    margin_top: int
    margin_bottom: int
    columns_x: list[int] = Field(default_factory=list)  # частые левые кромки блоков
    rows_y: list[int] = Field(default_factory=list)  # частые верхние кромки блоков


class FixedElement(BaseModel):
    """Элемент, повторяющийся в одной позиции на многих слайдах: логотип, колонтитул, номер."""

    kind: Literal["logo", "footer", "slide_number", "decoration", "unknown"]
    box: Box
    occurrences: int
    is_picture: bool = False


class Slot(BaseModel):
    """Заполняемое место на слайде-образце."""

    id: str  # id фигуры в XML образца
    kind: SlotKind
    box: Box
    max_chars: int | None = None  # оценка вместимости при базовом кегле
    max_lines: int | None = None
    max_items: int | None = None  # для body: сколько буллетов помещается
    font: str | None = None
    size_pt: float | None = None
    placeholder_type: str | None = None  # ph type из OOXML, если это плейсхолдер
    sample_text: str | None = None  # исходный текст (для отладки и few-shot)
    hard_lines: bool = False  # под слотом декор — строк больше max_lines не разрешать


class Exemplar(BaseModel):
    """Слайд-образец из шаблона: клонируется при генерации."""

    id: str  # напр. "slide12"
    source_index: int  # индекс слайда в исходном файле (0-based)
    layout_name: str
    archetype: Archetype
    slots: list[Slot]
    fixed: list[str] = Field(default_factory=list)  # id фигур, которые не трогаем (лого, декор)
    fill_ratio: float = 0.0  # доля площади, занятая контентом (0..1)
    thumbnail: str | None = None  # путь к PNG-рендеру
    tags: list[str] = Field(default_factory=list)  # семантические теги от VLM
    confidence: float = 1.0  # уверенность классификации
    # у каждой карточки сетки (≥ 2 body) своя подложка на слайде: пустую карточку рендер уберёт целиком.
    # False — карточки нарисованы общим фоном (полоса на ряд, картинка лейаута) и пустая останется видна
    card_frames: bool = False


class TemplateDNA(BaseModel):
    """Дизайн-система шаблона, извлечённая из .pptx."""

    template_id: str
    source_path: str
    slide_w: int
    slide_h: int
    colors: list[ColorToken]
    typography: list[TypeToken]
    fonts: list[str]  # гарнитуры по убыванию частоты
    grid: GridSpec
    fixed_elements: list[FixedElement]
    exemplars: list[Exemplar]
    embedded_fonts: list[str] = Field(default_factory=list)
    theme_colors: dict[str, str] = Field(default_factory=dict)  # dk1/lt1/accent1... как fallback
    stats: dict[str, int] = Field(default_factory=dict)

    def palette(self, role: str) -> list[str]:
        return [c.hex for c in self.colors if c.role == role]

    def exemplars_for(self, archetype: Archetype) -> list[Exemplar]:
        return [e for e in self.exemplars if e.archetype == archetype]


# ──────────────────────────────── DeckOutline ────────────────────────────────


class ChartSpec(BaseModel):
    kind: Literal["bar", "column", "line", "pie", "doughnut", "area"]
    title: str
    categories: list[str]
    series: dict[str, list[float]]  # имя серии → значения
    unit: str | None = None
    x_label: str | None = None
    y_label: str | None = None


class TableSpec(BaseModel):
    header: list[str]
    rows: list[list[str]]


class KpiSpec(BaseModel):
    value: str  # "42%", "1.2 млн"
    label: str


class ImageSpec(BaseModel):
    prompt: str | None = None  # для text-to-image
    path: str | None = None  # готовый файл
    alt: str = ""


class OutlineSlide(BaseModel):
    """Содержание одного слайда до вёрстки."""

    idx: int
    archetype: Archetype
    title: str
    subtitle: str | None = None
    section: str | None = None  # раздел; разделитель перед ним ставит стратегия
    bullets: list[str] = Field(default_factory=list)
    paragraphs: list[str] = Field(default_factory=list)
    kpis: list[KpiSpec] = Field(default_factory=list)
    chart: ChartSpec | None = None
    table: TableSpec | None = None
    image: ImageSpec | None = None
    steps: list[str] = Field(default_factory=list)  # для PROCESS
    quote: str | None = None
    quote_author: str | None = None
    speaker_notes: str = ""
    sources: list[str] = Field(default_factory=list)  # ссылки на фрагменты контент-пакета


class DeckOutline(BaseModel):
    title: str
    purpose: Literal["feature", "product", "project", "initiative", "report", "other"]
    audience: str = ""
    language: str = "ru"
    slides: list[OutlineSlide]


# ──────────────────────────────── DeckIR ────────────────────────────────


class TextRun(BaseModel):
    text: str
    bold: bool = False
    italic: bool = False
    color: str | None = None
    size_pt: float | None = None


class Paragraph(BaseModel):
    runs: list[TextRun]
    level: int = 0
    bullet: bool = False


class Element(BaseModel):
    """Конкретный объект на слайде, привязанный к слоту образца."""

    slot_id: str
    kind: SlotKind
    box: Box
    paragraphs: list[Paragraph] = Field(default_factory=list)
    chart: ChartSpec | None = None
    table: TableSpec | None = None
    image_path: str | None = None
    icon_name: str | None = None
    style_overrides: dict[str, str | float | bool] = Field(default_factory=dict)


class SlideIR(BaseModel):
    idx: int
    exemplar_id: str
    archetype: Archetype
    elements: list[Element]
    notes: str = ""
    outline_ref: int  # OutlineSlide.idx


class DeckIR(BaseModel):
    template_id: str
    strategy: str
    slides: list[SlideIR]
    slide_w: int
    slide_h: int


# ──────────────────────────────── Audit ────────────────────────────────


class Severity(StrEnum):
    ERROR = "error"
    WARNING = "warning"
    INFO = "info"


class Finding(BaseModel):
    check_id: str  # напр. "L01_out_of_bounds"
    kind: Literal["deterministic", "contextual"]
    severity: Severity
    slide_idx: int
    element_id: str | None = None
    box: Box | None = None  # для подсветки в UI
    message: str
    autofix: str | None = None  # имя фикса из core/autofix.FIXES
    evidence: dict[str, str | float | int] = Field(default_factory=dict)


class AuditReport(BaseModel):
    deck_path: str
    findings: list[Finding]
    checks_run: list[str]
    duration_s: float = 0.0

    def by_slide(self, idx: int) -> list[Finding]:
        return [f for f in self.findings if f.slide_idx == idx]

    @property
    def errors(self) -> int:
        return sum(1 for f in self.findings if f.severity == Severity.ERROR)
