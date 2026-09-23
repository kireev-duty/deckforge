"""Режим «на входе только шаблон»: слепок .pptx → бриф через скилл `template_brief`.

Когда контент-пакета и брифа нет, источником смысла остаётся сам шаблон: бренд и лексика живут
в текстах слайдов-образцов (`Slot.sample_text`), стиль — в палитре, шрифтах и тегах VLM.
Модель по этому слепку пишет бриф, который дальше идёт в `outline_writer` вместо `brief.md`.
Проверяемые факты (цифры, даты, имена) не выдумываются — их нечем подтвердить.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from deckforge.content.content_pack import ContentPack, Fragment
from deckforge.content.outline_writer import PURPOSES
from deckforge.core.ir import Archetype, DeckOutline, TemplateDNA
from deckforge.core.placeholders import is_placeholder_text
from deckforge.core.strategy import DEFAULT_TARGET_SLIDES
from deckforge.llm.client import LLMClient
from deckforge.llm.skills import load_skill

log = logging.getLogger(__name__)

SKILL_NAME = "template_brief"
DIGEST_CHAR_LIMIT = 6000  # слепок шаблона в промпте
SAMPLE_MAX_CHARS = 120  # текста одного слота в слепке
SAMPLES_PER_EXEMPLAR = 6
MAX_KEY_POINTS = 16  # тезис = слайд, поэтому лимит выше верхней границы объёма колоды
_LETTERS_RE = re.compile(r"[^\W\d_]", re.UNICODE)
_NUM_RE = re.compile(r"\d+")  # числа источника: любая цифровая группа («24/7» → 24 и 7)
_DATA_ARCHETYPES = {Archetype.KPI, Archetype.CHART, Archetype.TABLE}
_PALETTE_ROLES = ("background", "text", "accent", "secondary", "muted", "surface")


class TemplateBrief(BaseModel):
    """Ответ скилла `template_brief` — бриф, выведенный из шаблона."""

    topic: str
    summary: str
    brand: str = ""
    purpose: str = "other"
    audience: str = ""
    key_points: list[str] = Field(default_factory=list)
    tone: str = ""
    template_name: str = ""

    @classmethod
    def from_response(cls, raw: dict[str, Any], *, template_name: str = "") -> TemplateBrief:
        """Мягкое приведение ответа модели к контракту: списки, лимиты, purpose из списка."""
        data = dict(raw)
        purpose = str(data.get("purpose") or "").strip().lower()
        points = data.get("key_points")
        if not isinstance(points, list):
            points = [points] if points else []
        return cls(
            topic=str(data.get("topic") or "").strip() or "Презентация по шаблону",
            summary=str(data.get("summary") or "").strip(),
            brand=str(data.get("brand") or "").strip(),
            purpose=purpose if purpose in PURPOSES else "other",
            audience=str(data.get("audience") or "").strip(),
            key_points=[str(p).strip() for p in points if str(p).strip()][:MAX_KEY_POINTS],
            tone=str(data.get("tone") or "").strip(),
            template_name=template_name,
        )

    def to_brief_text(self) -> str:
        """Markdown-бриф в том же виде, в каком его написал бы пользователь (`brief.md`)."""
        lines = [f"# {self.topic}", ""]
        if self.brand:
            lines += [f"**Бренд.** {self.brand}", ""]
        if self.summary:
            lines += [f"**Задача презентации.** {self.summary}", ""]
        if self.audience:
            lines += [f"**Аудитория.** {self.audience}", ""]
        if self.tone:
            lines += [f"**Стиль шаблона.** {self.tone}", ""]
        if self.key_points:
            lines += ["**Ключевые тезисы.**", ""] + [f"- {p}" for p in self.key_points] + [""]
        src = f" «{self.template_name}»" if self.template_name else ""
        lines.append(f"_Бриф выведен из шаблона{src}: исходных материалов на входе не было, "
                     "поэтому конкретные цифры, даты и имена в нём отсутствуют._")
        return "\n".join(lines).strip() + "\n"


def drop_unsourced_numbers(outline: DeckOutline, source_text: str) -> tuple[DeckOutline, list[str]]:
    """Снять с колоды числа, которых нет в источнике — режимы «тема» и «только шаблон».

    Исходных материалов там нет, и модель охотно дорисовывает метрики вроде «100 %» и «24/7».
    KPI превращаются в тезисы из подписей, диаграмма — в буллеты, таблица с выдуманными числами
    снимается. Качественные сравнения без чисел остаются: проверять в них нечего.
    """
    known = set(_NUM_RE.findall(source_text))
    warnings: list[str] = []
    slides = []
    for s in outline.slides:
        s = s.model_copy(deep=True)
        label = f"слайд {s.idx} «{s.title[:40]}»"
        if s.kpis and any(_invented(k.value, known) for k in s.kpis):
            s.bullets = s.bullets or [k.label for k in s.kpis if k.label]
            s.kpis = []
            warnings.append(f"{label}: цифры KPI не из источника — заменены тезисами")
        if s.chart and any(_invented(f"{v}", known) for vals in s.chart.series.values() for v in vals):
            from deckforge.content.outline_writer import _chart_as_bullets

            s.bullets = s.bullets or _chart_as_bullets(s.chart.model_dump())
            s.chart = None
            warnings.append(f"{label}: данные диаграммы не из источника — сняты")
        if s.table and any(_invented(c, known) for row in s.table.rows for c in row):
            s.table = None
            warnings.append(f"{label}: числа таблицы не из источника — таблица снята")
        if s.archetype in _DATA_ARCHETYPES and not (s.kpis or s.chart or s.table):
            s.archetype = Archetype.BULLETS
        slides.append(s)
    return outline.model_copy(update={"slides": slides}), warnings


def _invented(value: str, known: set[str]) -> bool:
    """В значении есть число, которого нет в источнике."""
    return any(n not in known for n in _NUM_RE.findall(str(value)))


def pack_from_brief(text: str, *, root: str = "template:") -> ContentPack:
    """Контент-пакет из одного брифа, без обращения к диску: `sources: ["brief"]` остаются валидными."""
    return ContentPack(root=root, fragments=[Fragment(id="brief", kind="text", text=text.strip(), source="brief.md")])


def template_digest(dna: TemplateDNA, *, name: str = "", limit: int = DIGEST_CHAR_LIMIT) -> str:
    """Детерминированный текстовый слепок шаблона для промпта — без LLM.

    Имя файла, геометрия, палитра, шрифты, архетипы и по каждому образцу — лейаут, теги VLM
    и тексты слотов (заглушки вроде «Заголовок» и глифы иконочных шрифтов отброшены).
    """
    name = name or Path(dna.source_path).name
    head = [
        f"Файл шаблона: {name}",
        f"Слайд: {dna.slide_w / 914400:.2f}×{dna.slide_h / 914400:.2f} in, слайдов-образцов: {len(dna.exemplars)}",
    ]
    palette = [f"{role} #{h}" for role in _PALETTE_ROLES for h in dna.palette(role)[:2]]
    if palette:
        head.append("Палитра (по использованию): " + ", ".join(palette))
    if dna.fonts:
        head.append("Шрифты: " + ", ".join(dna.fonts[:4])
                    + (f" (встроены: {', '.join(dna.embedded_fonts)})" if dna.embedded_fonts else ""))
    kinds = sorted({f.kind for f in dna.fixed_elements})
    if kinds:
        head.append("Повторяющиеся элементы: " + ", ".join(kinds))
    head.append("")
    head.append("Слайды-образцы (архетип, лейаут, теги, тексты):")

    parts = list(head)
    total = sum(len(p) + 1 for p in parts)
    for e in dna.exemplars:
        line = f"- {e.id} ({e.archetype.value}, лейаут «{e.layout_name}»)"
        if e.tags:
            line += "; теги: " + ", ".join(e.tags[:5])
        texts = _sample_texts(e)
        if texts:
            line += "; текст: " + " | ".join(texts)
        if total + len(line) + 1 > limit:
            parts.append(f"…ещё {len(dna.exemplars) - (len(parts) - len(head))} образцов")
            break
        parts.append(line)
        total += len(line) + 1
    return "\n".join(parts)


def _sample_texts(exemplar: Any) -> list[str]:
    """Осмысленные тексты слотов образца: без заглушек, глифов и повторов."""
    out: list[str] = []
    for slot in exemplar.slots:
        text = " ".join((slot.sample_text or "").split())
        if not text or len(_LETTERS_RE.findall(text)) < 2 or is_placeholder_text(text):
            continue
        text = text[:SAMPLE_MAX_CHARS]
        if text not in out:
            out.append(text)
        if len(out) >= SAMPLES_PER_EXEMPLAR:
            break
    return out


def write_template_brief(
    client: LLMClient,
    dna: TemplateDNA,
    *,
    name: str = "",
    language: str = "ru",
    target_slides: int | None = None,
    purpose: str = "",
    audience: str = "",
    deadline: float | None = None,
) -> TemplateBrief:
    """Один вызов скилла; заданные пользователем purpose/audience побеждают ответ модели."""
    skill = load_skill(SKILL_NAME)
    name = name or Path(dna.source_path).name
    raw = client.run_skill(
        skill,
        template_digest=template_digest(dna, name=name),
        language=language,
        target_slides=target_slides or DEFAULT_TARGET_SLIDES,
        purpose_hint=purpose,
        audience_hint=audience,
        deadline=deadline,
    )
    if not isinstance(raw, dict):
        raise TypeError(f"template_brief вернул не объект: {type(raw).__name__}")
    brief = TemplateBrief.from_response(raw, template_name=name)
    if purpose:
        brief.purpose = purpose
    if audience:
        brief.audience = audience
    return brief


__all__ = ["SKILL_NAME", "TemplateBrief", "drop_unsourced_numbers", "pack_from_brief", "template_digest",
           "write_template_brief"]
