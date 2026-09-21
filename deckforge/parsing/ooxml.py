"""Шим обратной совместимости: OOXML-чтение живёт в core/ooxml.py и core/package.py."""

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
