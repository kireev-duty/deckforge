"""Шим: каталог автофиксов живёт в `core/autofix.py` (контракт между audit/ и layout/). Здесь — реэкспорт."""

from deckforge.core.autofix import (
    FIXES,
    FixMode,
    FixScope,
    FixSpec,
    fix_plan_rows,
    fix_spec,
    is_fixable,
    plan_fixes,
)

__all__ = ["FIXES", "FixMode", "FixScope", "FixSpec", "fix_plan_rows", "fix_spec", "is_fixable", "plan_fixes"]
