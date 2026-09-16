"""Каталог автофиксов: что означает имя в `Finding.autofix`, безопасно ли применять без вопросов, на каком уровне.

Живёт в core как часть контракта аудита: `audit/` выдаёт имена, `layout/autofix.apply_fixes` их применяет
(правки DeckIR → повторный рендер), и ни один слой не импортирует соседа. UI (день 10) показывает пользователю
`FIXES[name].description` и чекбокс по каждой находке; выбранные идут в `plan_fixes(report, selected)`.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Literal

from deckforge.core.ir import AuditReport, Finding, Severity

FixScope = Literal["ir", "replan", "none"]


@dataclass(frozen=True)
class FixSpec:
    name: str
    scope: FixScope  # ir — правится DeckIR; replan — нужен пересбор состава/образца (в v1 только предлагается); none — нет
    safe: bool  # применять автоматически при audit.autofix: true
    description: str  # для UI и manifest
    requires: tuple[str, ...] = ()  # поля evidence, без которых фикс неприменим (T06 тоже зовёт snap_color, но без nearest)


FIXES: dict[str, FixSpec] = {
    # ── правки DeckIR, безопасные (не меняют смысл и структуру) ──
    "shrink_font_by_scale": FixSpec("shrink_font_by_scale", "ir", True,
                                    "Уменьшить кегль, чтобы текст влез в рамку (не ниже 70 % от образца; крупная цифра — 40 %)",
                                    requires=("need_pt", "have_pt")),
    "snap_font_size": FixSpec("snap_font_size", "ir", True, "Привести кегль к ближайшему из типографической шкалы шаблона",
                              requires=("size_pt", "nearest_pt")),
    "snap_color": FixSpec("snap_color", "ir", True, "Заменить цвет на ближайший из палитры шаблона",
                          requires=("color", "nearest")),
    "refill_slot": FixSpec("refill_slot", "ir", True, "Укоротить пункт до нормы (хвост по разделителям), убрать текст-заглушку"),
    "add_chart_labels": FixSpec("add_chart_labels", "ir", True, "Включить подписи данных и подпись оси значений у диаграммы"),
    "drop_shape": FixSpec("drop_shape", "ir", True, "Удалить пустой плейсхолдер / фигуру"),
    # ── правки DeckIR, по выбору пользователя (теряют контент) ──
    "drop_slide": FixSpec("drop_slide", "ir", False, "Удалить слайд (пустой или дублирующий)"),
    "drop_minor_series": FixSpec("drop_minor_series", "ir", False, "Оставить на диаграмме пять самых крупных серий"),
    # ── нужен пересбор outline/плана или геометрии образца — v1 только предлагает ──
    "split_slide": FixSpec("split_slide", "replan", False, "Разбить слайд на два по пунктам"),
    "split_table": FixSpec("split_table", "replan", False, "Разбить таблицу на два слайда"),
    "change_exemplar": FixSpec("change_exemplar", "replan", False, "Подобрать другой образец под объём контента"),
    "reflow_vertical": FixSpec("reflow_vertical", "replan", False, "Развести наложившиеся блоки по вертикали"),
    "clamp_to_slide": FixSpec("clamp_to_slide", "replan", False, "Вернуть объект в границы слайда"),
    "snap_to_grid": FixSpec("snap_to_grid", "replan", False, "Выровнять блок по направляющим макета"),
    "crop_to_aspect": FixSpec("crop_to_aspect", "replan", False, "Обрезать картинку под пропорции рамки"),
    "restore_fixed": FixSpec("restore_fixed", "replan", False, "Вернуть логотип / колонтитул на место из шаблона"),
    "reset_font": FixSpec("reset_font", "replan", False, "Заменить гарнитуру на шрифт шаблона"),
}

FixMode = Literal["safe", "all"]


def fix_spec(finding: Finding) -> FixSpec | None:
    return FIXES.get(finding.autofix or "")


def is_fixable(finding: Finding) -> bool:
    """Есть фикс уровня IR, дефект наш (не унаследован от образца — дизайн шаблона не чиним), не info
    (например, T02 «подгонка 70–100 %» — это осознанный fitting) и в evidence есть всё нужное фиксу."""
    spec = fix_spec(finding)
    if spec is None or spec.scope != "ir" or finding.evidence.get("in_exemplar"):
        return False
    if finding.severity == Severity.INFO:
        return False
    return all(finding.evidence.get(k) not in (None, "", 0) for k in spec.requires)


def plan_fixes(report: AuditReport, mode: FixMode | Iterable[int] = "safe") -> list[Finding]:
    """Какие находки идут в `apply_fixes`.

    `mode="safe"` — только безопасные (режим `audit.autofix: true`); `"all"` — все уровня IR;
    коллекция индексов находок в `report.findings` — выбор пользователя из UI.
    """
    if mode == "safe":
        return [f for f in report.findings if is_fixable(f) and FIXES[f.autofix].safe]  # type: ignore[index]
    if mode == "all":
        return [f for f in report.findings if is_fixable(f)]
    chosen = set(mode)
    return [f for i, f in enumerate(report.findings) if i in chosen and is_fixable(f)]


def fix_plan_rows(report: AuditReport) -> list[dict]:
    """Таблица «находка → фикс → безопасный/по выбору/replan» для CLI и UI."""
    rows = []
    for i, f in enumerate(report.findings):
        spec = fix_spec(f)
        if spec is None:
            continue
        if f.evidence.get("in_exemplar"):
            how = "template"
        elif spec.scope != "ir":
            how = spec.scope
        elif not is_fixable(f):
            how = "n/a"  # info-находка или нет данных для фикса
        else:
            how = "safe" if spec.safe else "ir"
        rows.append({"n": i, "slide_idx": f.slide_idx, "check_id": f.check_id, "severity": f.severity.value,
                     "fix": spec.name, "how": how, "description": spec.description})
    return rows


__all__ = ["FIXES", "FixMode", "FixScope", "FixSpec", "fix_plan_rows", "fix_spec", "is_fixable", "plan_fixes"]
