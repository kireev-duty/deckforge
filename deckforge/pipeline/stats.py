"""Метрики колоды по DeckIR — для manifest.json и таблицы сравнения стратегий."""

from __future__ import annotations

from deckforge.core.ir import SlotKind
from deckforge.layout import LayoutResult


def deck_stats(res: LayoutResult) -> dict:
    """Плотность (пунктов на слайд, слов на пункт) и способ визуализации (chart/table/kpi/picture)."""
    slides = res.ir.slides
    n_items = words = 0
    n_slides_with_items = 0
    kinds = {"chart": 0, "table": 0, "kpi": 0, "picture": 0}
    for s in slides:
        items = [p for el in s.elements if el.kind == SlotKind.BODY for p in el.paragraphs]
        items += [p for el in s.elements if el.kind in (SlotKind.LABEL, SlotKind.CAPTION) for p in el.paragraphs]
        if items:
            n_slides_with_items += 1
            n_items += len(items)
            words += sum(len(r.text.split()) for p in items for r in p.runs)
        if any(el.chart for el in s.elements):
            kinds["chart"] += 1
        if any(el.table for el in s.elements):
            kinds["table"] += 1
        if any(el.kind == SlotKind.NUMBER for el in s.elements):
            kinds["kpi"] += 1
        if any(el.image_path for el in s.elements):
            kinds["picture"] += 1
    return {
        "slides": len(slides),
        "archetypes": [s.archetype.value for s in slides],
        "exemplars": [s.exemplar_id for s in slides],
        "text_items_per_slide": round(n_items / n_slides_with_items, 1) if n_slides_with_items else 0,
        "words_per_item": round(words / n_items, 1) if n_items else 0,
        **kinds,
        "skipped": sum(1 for c in res.choices if c.exemplar_id is None),
    }


def compare_table(rows: dict[str, dict]) -> str:
    head = ("| стратегия | слайдов | архетипы по порядку | пунктов/слайд | слов/пункт | chart | table | kpi | пропущено "
            "| аудит err/warn | речь, мин | время, с |")
    sep = "|---|---|---|---|---|---|---|---|---|---|---|---|"
    lines = [head, sep]
    for name, st in rows.items():
        total = st.get("deck_total")
        lines.append(
            f"| {name} | {st['slides']} | {' → '.join(st['archetypes'])} | {st['text_items_per_slide']} | "
            f"{st['words_per_item']} | {st['chart']} | {st['table']} | {st['kpi']} | {st['skipped']} "
            f"| {st.get('audit_errors', '—')}/{st.get('audit_warnings', '—')} | {st.get('speech_minutes', '—')} "
            f"| {round(total) if total else '—'} |"
        )
    # для кого какой вариант — «актуальность различий»
    hints = [f"- **{name}** — {st['audience_hint']}" for name, st in rows.items() if st.get("audience_hint")]
    if hints:
        lines += ["", *hints]
    return "\n".join(lines)


__all__ = ["compare_table", "deck_stats"]
