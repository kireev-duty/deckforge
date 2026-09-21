"""Детерминированные проверки (таблица в docs/AUDIT.md): функция `(AuditContext) -> list[Finding]` на каждую."""

from __future__ import annotations

from deckforge.audit.deterministic import density, integrity, layout, template
from deckforge.audit.deterministic.base import Check, finding

CHECKS: dict[str, Check] = {
    "L01_out_of_bounds": layout.check_L01,
    "L02_overlap": layout.check_L02,
    "L03_text_overflow": layout.check_L03,
    "L04_text_clipped": layout.check_L04,
    "L05_off_grid": layout.check_L05,
    "L06_in_margins": layout.check_L06,
    "L07_picture_stretched": layout.check_L07,
    "T01_font": template.check_T01,
    "T02_font_size": template.check_T02,
    "T03_color": template.check_T03,
    "T04_layout": template.check_T04,
    "T05_fixed_moved": template.check_T05,
    "T06_contrast": template.check_T06,
    "D01_bullets": density.check_D01,
    "D02_bullet_words": density.check_D02,
    "D03_table_size": density.check_D03,
    "D04_chart_series": density.check_D04,
    "D05_fill": density.check_D05,
    "I01_file": integrity.check_I01,
    "I02_placeholder_text": integrity.check_I02,
    "I03_empty_slide": integrity.check_I03,
    "I04_raster_slide": integrity.check_I04,
    "I05_chart_labels": integrity.check_I05,
    "I06_duplicate_slides": integrity.check_I06,
}


def check_by_prefix(prefix: str) -> tuple[str, Check]:
    """'L03' → ('L03_text_overflow', функция)."""
    for cid, fn in CHECKS.items():
        if cid.startswith(prefix):
            return cid, fn
    raise KeyError(prefix)


__all__ = ["CHECKS", "Check", "check_by_prefix", "finding"]
