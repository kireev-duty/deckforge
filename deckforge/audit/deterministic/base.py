"""Общее для проверок: тип Check и фабрика Finding."""

from __future__ import annotations

from collections.abc import Callable

from deckforge.audit.context import AuditContext, ShapeRec, SlideCtx
from deckforge.core.ir import Box, Finding, Severity

Check = Callable[[AuditContext], list[Finding]]


def finding(check_id: str, slide: SlideCtx | int, severity: Severity, message: str, shape: ShapeRec | None = None,
            autofix: str | None = None, box: Box | None = None, **evidence: str | float | int) -> Finding:
    return Finding(
        check_id=check_id, kind="deterministic", severity=severity,
        slide_idx=slide if isinstance(slide, int) else slide.idx,
        element_id=shape.id if shape else None, box=box or (shape.box if shape else None),
        message=message, autofix=autofix, evidence=evidence,
    )


__all__ = ["Check", "finding"]
