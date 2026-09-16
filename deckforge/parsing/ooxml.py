"""Шим обратной совместимости: OOXML-чтение живёт в core (core/ooxml.py — геометрия и идентификация фигур,
core/package.py — пакет, тема, цепочки наследования). Модули parsing/ импортируют отсюда как раньше."""

from __future__ import annotations

from deckforge.core.ooxml import (  # noqa: F401
    A,
    NS,
    P,
    R,
    absolute_bbox,
    bbox,
    graphic_kind,
    iter_shapes,
    localname,
    placeholder,
    shape_id,
    shape_name,
    shape_text,
)
from deckforge.core.package import (  # noqa: F401
    DEFAULT_CLR_MAP,
    LAYOUT_RE,
    MASTER_RE,
    PRESET_COLORS,
    SLIDE_RE,
    THEME_COLOR_KEYS,
    FillList,
    Package,
    PartCtx,
    ThemeInfo,
    font_scale,
    owner_shape,
)
