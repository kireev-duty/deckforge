"""Контракты IR: сериализация туда-обратно и базовая геометрия."""

from deckforge.core.ir import (
    Archetype,
    AuditReport,
    Box,
    DeckOutline,
    Finding,
    OutlineSlide,
    Severity,
)


def test_box_intersection() -> None:
    a = Box(x=0, y=0, w=100, h=100)
    b = Box(x=90, y=90, w=50, h=50)
    c = Box(x=100, y=0, w=10, h=10)
    assert a.intersects(b)
    assert not a.intersects(c)  # касание по кромке — не пересечение
    assert not a.intersects(b, tolerance=15)


def test_outline_roundtrip() -> None:
    o = DeckOutline(
        title="t", purpose="product",
        slides=[OutlineSlide(idx=0, archetype=Archetype.TITLE, title="Вывод")],
    )
    data = o.model_dump_json()
    assert DeckOutline.model_validate_json(data) == o


def test_audit_report_counts() -> None:
    r = AuditReport(deck_path="x.pptx", checks_run=["L01"], findings=[
        Finding(check_id="L01", kind="deterministic", severity=Severity.ERROR, slide_idx=2, message="m"),
        Finding(check_id="C01", kind="contextual", severity=Severity.WARNING, slide_idx=2, message="m"),
    ])
    assert r.errors == 1
    assert len(r.by_slide(2)) == 2 and r.by_slide(0) == []
