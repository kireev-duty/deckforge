"""outline_writer: бриф + контент-пакет → DeckOutline через скилл `outline_writer`.

Outline один на прогон и не зависит от стратегии вёрстки — стратегия применяется в layout/.
Ответ модели проходит `repair_outline`: мягкие поправки вместо отказа (перенумерация, недоступный архетип → bullets,
битая диаграмма → буллеты, обязательные title/closing), потому что один лишний ретрай LLM дороже пяти строк кода.
Промптов в коде нет — только сборка входов скилла.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, get_args

from pydantic import ValidationError

from deckforge.content.content_pack import ContentPack
from deckforge.core.ir import ARCHETYPE_HINTS, Archetype, DeckOutline, OutlineSlide
from deckforge.llm.client import LLMClient
from deckforge.llm.skills import load_skill

log = logging.getLogger(__name__)

DEFAULT_TARGET_SLIDES = 12
MAX_BULLETS = 6
MAX_KPIS = 4
TABLE_MAX_ROWS, TABLE_MAX_COLS = 7, 5
# Архетипы, которые модели предлагать нет смысла: их ставит planner (section) или это не образцы (freeform)
NOT_OFFERED = {Archetype.SECTION, Archetype.FREEFORM, Archetype.TEAM, Archetype.AGENDA}
TEXT_FALLBACK = Archetype.BULLETS  # дальше сработают цепочки FALLBACKS в layout/exemplar_picker
PURPOSES: tuple[str, ...] = get_args(DeckOutline.model_fields["purpose"].annotation)
ALIAS_LIST_FIELDS = ("cards", "items", "points", "columns", "benefits", "risks", "list")
TEXT_ARCHETYPES = {a.value for a in (Archetype.BULLETS, Archetype.CARDS, Archetype.TWO_COLUMN, Archetype.IMAGE_TEXT,
                                     Archetype.AGENDA)}


@dataclass
class OutlineResult:
    outline: DeckOutline
    warnings: list[str] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)
    attempts: int = 1


def archetypes_prompt(available: set[Archetype]) -> str:
    """Список доступных архетипов с определениями — вход {{available_archetypes}}."""
    offered = [a for a in Archetype if a in available and a not in NOT_OFFERED]
    if not offered:  # шаблон без размеченных образцов — предлагаем текстовый минимум
        offered = [Archetype.TITLE, Archetype.BULLETS, Archetype.CLOSING]
    return "\n".join(f"- {a.value}: {ARCHETYPE_HINTS[a]}" for a in offered)


def write_outline(
    client: LLMClient,
    pack: ContentPack,
    *,
    purpose: str,
    audience: str,
    language: str = "ru",
    target_slides: int | None = None,
    available_archetypes: set[Archetype],
    retries: int = 1,
) -> OutlineResult:
    """Один вызов скилла (+ повтор, если ответ не собирается в DeckOutline даже после repair)."""
    skill = load_skill("outline_writer")
    content_text, warnings = pack.to_prompt_text()
    inputs = {
        "brief": pack.brief,
        "purpose": purpose,
        "audience": audience,
        "language": language,
        "target_slides": target_slides or DEFAULT_TARGET_SLIDES,
        "content_pack": content_text,
        "available_archetypes": archetypes_prompt(available_archetypes),
    }
    last_err: Exception | None = None
    for attempt in range(retries + 1):
        raw = client.run_skill(skill, **inputs)
        if not isinstance(raw, dict):
            last_err = TypeError(f"outline_writer вернул не объект: {type(raw).__name__}")
            continue
        try:
            outline, fix_warnings = repair_outline(raw, available_archetypes, pack.ids(), purpose=purpose,
                                                   audience=audience, language=language)
        except ValidationError as e:
            last_err = e
            log.warning("outline_writer: ответ не прошёл валидацию (попытка %d): %s", attempt + 1, str(e)[:300])
            continue
        return OutlineResult(outline, warnings + fix_warnings, raw, attempts=attempt + 1)
    raise RuntimeError(f"outline_writer: не удалось собрать DeckOutline за {retries + 1} попытки: {last_err}")


# ──────────────────────────── repair ────────────────────────────


def repair_outline(
    raw: dict[str, Any], available: set[Archetype], pack_ids: set[str], *,
    purpose: str = "other", audience: str = "", language: str = "ru",
) -> tuple[DeckOutline, list[str]]:
    """Мягко приводит ответ модели к контракту DeckOutline. Бросает ValidationError, если не получается."""
    warnings: list[str] = []
    data = dict(raw)
    if data.get("purpose") not in PURPOSES:
        if data.get("purpose") not in (None, purpose):
            warnings.append(f"purpose «{data.get('purpose')}» не из списка — заменён на «{purpose}»")
        data["purpose"] = purpose if purpose in PURPOSES else "other"
    data.setdefault("audience", audience)
    data.setdefault("language", language)
    data["title"] = str(data.get("title") or "").strip() or _first_title(data)

    slides_raw = [s for s in data.get("slides") or [] if isinstance(s, dict)]
    slides: list[dict[str, Any]] = []
    for s in slides_raw:
        s = _repair_slide(dict(s), available, warnings)
        if s is not None:
            slides.append(s)
    if not slides:
        raise ValidationError.from_exception_data("DeckOutline", [
            {"type": "too_short", "loc": ("slides",), "input": [], "ctx": {"field_type": "list", "min_length": 1, "actual_length": 0}},
        ])

    # обязательные title / closing по краям
    if slides[0]["archetype"] != Archetype.TITLE.value:
        if Archetype.TITLE in available:
            slides.insert(0, {"archetype": Archetype.TITLE.value, "title": data["title"], "sources": ["brief"]})
            warnings.append("первый слайд не title — добавлен титульный")
    if slides[-1]["archetype"] != Archetype.CLOSING.value:  # closing есть в FALLBACKS picker'а — ставим всегда
        slides.append({"archetype": Archetype.CLOSING.value, "title": data["title"], "sources": ["brief"]})
        warnings.append("последний слайд не closing — добавлен финальный")

    for i, s in enumerate(slides):
        if s.get("idx") != i:
            s["idx"] = i
    unknown = sorted({src for s in slides for src in s.get("sources", []) if src not in pack_ids})
    if unknown:
        warnings.append(f"неизвестные sources: {', '.join(unknown[:8])}{'…' if len(unknown) > 8 else ''}")
    data["slides"] = slides
    return DeckOutline.model_validate(data), warnings


def _first_title(data: dict[str, Any]) -> str:
    for s in data.get("slides") or []:
        if isinstance(s, dict) and s.get("title"):
            return str(s["title"]).strip()
    return "Презентация"


def _repair_slide(s: dict[str, Any], available: set[Archetype], warnings: list[str]) -> dict[str, Any] | None:
    title = str(s.get("title") or "").strip()
    label = f"слайд {s.get('idx', '?')} «{title[:40]}»"
    if not title:
        warnings.append(f"{label}: без заголовка — пропущен")
        return None
    s["title"] = title

    # архетип: из enum и из доступных в шаблоне; иначе текстовый фолбэк
    try:
        arch = Archetype(str(s.get("archetype", "")).strip().lower())
    except ValueError:
        warnings.append(f"{label}: неизвестный архетип «{s.get('archetype')}» → {TEXT_FALLBACK.value}")
        arch = TEXT_FALLBACK
    if arch not in available and arch not in (Archetype.TITLE, Archetype.CLOSING, Archetype.SECTION):
        # data-архетипы оставляем: planner превратит chart↔table↔kpi по стратегии, picker найдёт фолбэк
        if arch not in (Archetype.CHART, Archetype.TABLE, Archetype.KPI, Archetype.QUOTE, Archetype.PROCESS):
            warnings.append(f"{label}: архетипа {arch.value} нет в шаблоне → {TEXT_FALLBACK.value}")
            arch = TEXT_FALLBACK
    s["archetype"] = arch.value

    # null → значения по умолчанию, чтобы pydantic не спотыкался на list-полях
    for key in ("bullets", "paragraphs", "kpis", "steps", "sources"):
        if s.get(key) is None:
            s.pop(key, None)
        elif not isinstance(s[key], list):
            s[key] = [s[key]]
    for key in ("subtitle", "section", "quote", "quote_author"):
        if s.get(key) is not None and not isinstance(s[key], str):
            s[key] = str(s[key])
    if s.get("speaker_notes") is None:
        s.pop("speaker_notes", None)
    s["bullets"] = [_item_text(b) for b in s.get("bullets", []) if _item_text(b)]
    # модель иногда кладёт контент в поле по имени архетипа (cards, items, columns…) — сворачиваем в буллеты
    for alias in ALIAS_LIST_FIELDS:
        extra = s.pop(alias, None)
        if isinstance(extra, list) and extra and not s["bullets"]:
            s["bullets"] = [_item_text(b) for b in extra if _item_text(b)]
            warnings.append(f"{label}: поле «{alias}» → bullets")
    if len(s["bullets"]) > MAX_BULLETS:
        warnings.append(f"{label}: {len(s['bullets'])} буллетов → первые {MAX_BULLETS}")
        s["bullets"] = s["bullets"][:MAX_BULLETS]
    s["steps"] = [_item_text(b) for b in s.get("steps", []) if _item_text(b)]
    s["paragraphs"] = [_item_text(b) for b in s.get("paragraphs", []) if _item_text(b)]
    s["sources"] = [str(x) for x in s.get("sources", [])]

    # KPI
    kpis = []
    for k in s.get("kpis", []):
        if isinstance(k, dict) and k.get("value") is not None and k.get("label"):
            kpis.append({"value": str(k["value"]).strip(), "label": str(k["label"]).strip()})
    if len(kpis) > MAX_KPIS:
        warnings.append(f"{label}: {len(kpis)} KPI → первые {MAX_KPIS}")
        kpis = kpis[:MAX_KPIS]
    s["kpis"] = kpis

    # chart: серии одной длины с categories, числа — числа
    chart = s.get("chart")
    if chart is not None:
        fixed = _repair_chart(chart, title)
        if fixed is None:
            warnings.append(f"{label}: диаграмма без корректных данных — убрана")
            s["chart"] = None
            if arch == Archetype.CHART:
                s["archetype"] = TEXT_FALLBACK.value
                if not s["bullets"]:
                    s["bullets"] = _chart_as_bullets(chart)
        else:
            s["chart"] = fixed

    # table: строки выровнены по header, лимиты
    table = s.get("table")
    if table is not None:
        fixed_t = _repair_table(table)
        if fixed_t is None:
            warnings.append(f"{label}: таблица без данных — убрана")
            s["table"] = None
            if arch == Archetype.TABLE:
                s["archetype"] = TEXT_FALLBACK.value
        else:
            s["table"] = fixed_t

    if s.get("image") is not None and not isinstance(s["image"], dict):
        s["image"] = None
    if s.get("archetype") == Archetype.QUOTE.value and not s.get("quote"):
        warnings.append(f"{label}: quote без текста цитаты → {TEXT_FALLBACK.value}")
        s["archetype"] = TEXT_FALLBACK.value
    if s.get("archetype") == Archetype.KPI.value and not kpis:
        warnings.append(f"{label}: kpi без цифр → {TEXT_FALLBACK.value}")
        s["archetype"] = TEXT_FALLBACK.value
    if s.get("archetype") == Archetype.PROCESS.value and not s["steps"]:
        if s["bullets"]:
            s["steps"], s["bullets"] = s["bullets"], []
        else:
            warnings.append(f"{label}: process без шагов → {TEXT_FALLBACK.value}")
            s["archetype"] = TEXT_FALLBACK.value
    if s["archetype"] in TEXT_ARCHETYPES and not (s["bullets"] or s.get("paragraphs")):
        warnings.append(f"{label}: текстовый слайд без содержимого — останется только заголовок")
    return s


def _item_text(item: Any) -> str:
    """Пункт списка: строка или объект {title|label|name, text|description|body} → «Заголовок — текст»."""
    if isinstance(item, dict):
        head = str(item.get("title") or item.get("label") or item.get("name") or "").strip()
        body = str(item.get("text") or item.get("description") or item.get("body") or item.get("value") or "").strip()
        return f"{head} — {body}" if head and body else head or body
    return str(item).strip() if item is not None else ""


def _num(v: Any) -> float | None:
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        t = v.replace("−", "-").replace(",", ".").replace(" ", "").rstrip("%")
        try:
            return float(t)
        except ValueError:
            return None
    return None


def _repair_chart(chart: Any, slide_title: str) -> dict[str, Any] | None:
    if not isinstance(chart, dict):
        return None
    cats = [str(c) for c in chart.get("categories") or []]
    series_raw = chart.get("series") or {}
    if isinstance(series_raw, list):  # [{name, values|data|points}] → {name: values}
        series_raw = {
            str(x.get("name") or x.get("label") or f"ряд {i + 1}"): x.get("values") or x.get("data") or x.get("points") or []
            for i, x in enumerate(series_raw) if isinstance(x, dict)
        }
    if not cats or not isinstance(series_raw, dict):
        return None
    series: dict[str, list[float]] = {}
    for name, vals in series_raw.items():
        nums = [_num(v) for v in (vals or [])]
        if len(nums) != len(cats) or any(n is None for n in nums):
            continue
        series[str(name)] = [float(n) for n in nums]  # type: ignore[arg-type]
    if not series:
        return None
    kind = str(chart.get("kind") or "column").lower()
    if kind not in ("bar", "column", "line", "pie", "doughnut", "area"):
        kind = "column"
    return {
        "kind": kind, "title": str(chart.get("title") or slide_title), "categories": cats, "series": series,
        "unit": chart.get("unit") or None, "x_label": chart.get("x_label") or None, "y_label": chart.get("y_label") or None,
    }


def _chart_as_bullets(chart: Any) -> list[str]:
    """Диаграмму не собрать — хотя бы не потерять числа."""
    if not isinstance(chart, dict) or not isinstance(chart.get("series"), dict):
        return []
    cats = [str(c) for c in chart.get("categories") or []]
    out = []
    for name, vals in chart["series"].items():
        pairs = [f"{c} — {v}" for c, v in zip(cats, vals or [])]
        if pairs:
            out.append(f"{name}: {', '.join(pairs)}")
    return out[:MAX_BULLETS]


def _repair_table(table: Any) -> dict[str, Any] | None:
    if not isinstance(table, dict):
        return None
    header = [str(h) for h in table.get("header") or []][:TABLE_MAX_COLS]
    rows_raw = table.get("rows") or []
    if not header or not rows_raw:
        return None
    n = len(header)
    rows = []
    for r in rows_raw[:TABLE_MAX_ROWS]:
        if isinstance(r, dict):
            r = [r.get(h, "") for h in header]
        cells = [str(c) if c is not None else "" for c in list(r)[:n]]
        rows.append(cells + [""] * (n - len(cells)))
    return {"header": header, "rows": rows}


__all__ = ["OutlineResult", "archetypes_prompt", "repair_outline", "write_outline"]
