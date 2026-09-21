"""Сборка полной TemplateDNA: токены + сетка (перцентили кромок контентных фигур) + фиксированные элементы + образцы."""

from __future__ import annotations

from collections import Counter
from pathlib import Path

from deckforge.core.ir import Box, FixedElement, GridSpec, TemplateDNA
from deckforge.core.ooxml import NS, absolute_bbox, iter_shapes, localname, placeholder
from deckforge.core.package import Package
from deckforge.parsing.exemplars import load_exemplars
from deckforge.parsing.extract_tokens import TemplateTokens, extract_tokens
from deckforge.parsing.layout_classifier import find_fixed_signatures, _norm_text, _signature

STEP = 914400 // 20  # 1/20", шаг округления кромок
FULL_BLEED = 0.6  # фигура крупнее — фон, в сетке не участвует
GUIDE_MIN_SHARE = 0.2  # кромка на такой доле слайдов — направляющая
MARGIN_PERCENTILE = 0.05
FOOTER_ZONE = 0.12  # зона колонтитулов у краёв
LOGO_MAX_AREA = 0.03


def _percentile(values: list[int], q: float) -> int:
    vals = sorted(values)
    if not vals:
        return 0
    idx = min(len(vals) - 1, max(0, int(round(q * (len(vals) - 1)))))
    return vals[idx]


def _content_boxes(pkg: Package, fixed_sigs: set[tuple]) -> list[list[tuple[int, int, int, int]]]:
    """По слайдам: bbox фигур с текстом / картинок / графиков, кроме фона и фиксированных элементов."""
    sw, sh = pkg.slide_size
    area = sw * sh
    out: list[list[tuple[int, int, int, int]]] = []
    for slide in pkg.slides:
        root = pkg.xml(slide)
        boxes: list[tuple[int, int, int, int]] = []
        for sp in iter_shapes(root.find("p:cSld/p:spTree", NS)):
            bb = absolute_bbox(sp)
            if bb is None or bb[2] <= 0 or bb[3] <= 0:
                continue
            if bb[2] * bb[3] >= FULL_BLEED * area:
                continue
            tag = localname(sp)
            text = _norm_text(sp) if tag == "sp" else ""
            if tag == "sp" and not text:
                continue  # декоративная фигура без текста
            if placeholder(sp) is None and _signature(tag, text, bb) in fixed_sigs:
                continue
            boxes.append(bb)
        out.append(boxes)
    return out


def _guides(edges: list[int], n_slides: int) -> list[int]:
    counts = Counter(round(e / STEP) for e in edges)
    need = max(2, int(GUIDE_MIN_SHARE * n_slides))
    return sorted(k * STEP for k, n in counts.items() if n >= need)


def build_grid(pkg: Package, fixed_sigs: set[tuple] | None = None) -> GridSpec:
    sw, sh = pkg.slide_size
    fixed_sigs = fixed_sigs if fixed_sigs is not None else find_fixed_signatures(pkg)
    per_slide = _content_boxes(pkg, fixed_sigs)
    lefts = [b[0] for boxes in per_slide for b in boxes]
    tops = [b[1] for boxes in per_slide for b in boxes]
    rights = [b[0] + b[2] for boxes in per_slide for b in boxes]
    bottoms = [b[1] + b[3] for boxes in per_slide for b in boxes]
    if not lefts:
        m = sw // 20
        return GridSpec(slide_w=sw, slide_h=sh, margin_left=m, margin_right=m, margin_top=m, margin_bottom=m)
    ml = max(0, _percentile(lefts, MARGIN_PERCENTILE))
    mt = max(0, _percentile(tops, MARGIN_PERCENTILE))
    mr = max(0, sw - _percentile(rights, 1 - MARGIN_PERCENTILE))
    mb = max(0, sh - _percentile(bottoms, 1 - MARGIN_PERCENTILE))
    # кромки считаем по одному разу на слайд, иначе сетка карточек перевесит
    uniq_lefts = [e for boxes in per_slide for e in {round(b[0] / STEP) * STEP for b in boxes}]
    uniq_tops = [e for boxes in per_slide for e in {round(b[1] / STEP) * STEP for b in boxes}]
    return GridSpec(
        slide_w=sw, slide_h=sh, margin_left=ml, margin_right=mr, margin_top=mt, margin_bottom=mb,
        columns_x=_guides(uniq_lefts, len(per_slide)), rows_y=_guides(uniq_tops, len(per_slide)),
    )


def build_fixed_elements(pkg: Package, fixed_sigs: set[tuple] | None = None) -> list[FixedElement]:
    """Фиксированные элементы шаблона с числом слайдов, на которых они стоят."""
    sw, sh = pkg.slide_size
    fixed_sigs = fixed_sigs if fixed_sigs is not None else find_fixed_signatures(pkg)
    counts: Counter[tuple] = Counter()
    for slide in pkg.slides:
        seen: set[tuple] = set()
        for sp in iter_shapes(pkg.xml(slide).find("p:cSld/p:spTree", NS)):
            if placeholder(sp) is not None:
                continue
            bb = absolute_bbox(sp)
            if bb is None:
                continue
            tag = localname(sp)
            sig = _signature(tag, _norm_text(sp) if tag == "sp" else "", bb)
            if sig in fixed_sigs:
                seen.add(sig + (bb,))
        counts.update(seen)
    out: list[FixedElement] = []
    for sig, n in counts.most_common():
        tag, text, bb = sig[0], sig[1], sig[-1]
        box = Box(x=bb[0], y=bb[1], w=bb[2], h=bb[3])
        area = bb[2] * bb[3] / (sw * sh)
        fy, fh = bb[1] / sh, bb[3] / sh
        in_edge_zone = fy + fh <= FOOTER_ZONE or fy >= 1 - FOOTER_ZONE
        if tag == "pic" and area <= LOGO_MAX_AREA:
            kind = "logo"
        elif text and in_edge_zone:
            kind = "footer"
        elif tag == "sp" and not text:
            kind = "decoration"
        else:
            kind = "unknown"
        out.append(FixedElement(kind=kind, box=box, occurrences=n, is_picture=tag == "pic"))
    return out


def build_dna(template: str | Path, exemplars=None, tokens: TemplateTokens | None = None) -> TemplateDNA:
    """TemplateDNA целиком: токены + сетка + фиксированные элементы + образцы."""
    template = Path(template)
    pkg = Package(template)
    tokens = tokens or extract_tokens(template)
    exemplars = exemplars if exemplars is not None else load_exemplars(template)
    fixed_sigs = find_fixed_signatures(pkg)
    return TemplateDNA(
        **tokens.dna_kwargs(),
        grid=build_grid(pkg, fixed_sigs),
        fixed_elements=build_fixed_elements(pkg, fixed_sigs),
        exemplars=list(exemplars),
    )


__all__ = ["build_dna", "build_fixed_elements", "build_grid"]
