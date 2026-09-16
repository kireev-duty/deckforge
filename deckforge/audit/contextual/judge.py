"""Контекстуальный аудит: VLM-судья (скилл `audit_judge`) отвечает на 11 да/нет вопросов по PNG каждого слайда.

Вход на слайд: PNG, заголовок, текст, заголовки соседей, факты из контент-пакета (фрагменты по `OutlineSlide.sources`).
«Нет» → `Finding(kind="contextual")` с severity из таблицы docs/AUDIT.md. Вызовы идут параллельно; сбой одного
слайда даёт `C00_judge_error` (info), а не падение аудита. Промпт — только в `skills/audit_judge/v<N>/`.
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from deckforge.core.ir import Archetype, DeckIR, DeckOutline, Finding, Severity, SlotKind

log = logging.getLogger(__name__)

SKILL_NAME = "audit_judge"
WORKERS = 4
MAX_FACT_CHARS = 6000  # фактов в промпт на слайд (~1,5k токенов): весь синтетический пакет без дублей ≈ 5k
MAX_TEXT_CHARS = 2000

# id проверки → (slug, severity, вопрос). Формулировки дублируют промпт для отчёта и UI.
QUESTIONS: dict[str, tuple[str, Severity, str]] = {
    "C01": ("title_insight", Severity.WARNING, "Заголовок содержит вывод, а не просто называет тему"),
    "C02": ("content_matches_title", Severity.ERROR, "Содержимое слайда соответствует заголовку"),
    "C03": ("one_message", Severity.WARNING, "Слайд пересказывается одним предложением"),
    "C04": ("facts_in_sources", Severity.ERROR, "Все цифры и факты со слайда есть в исходных материалах"),
    "C05": ("has_content", Severity.ERROR, "На слайде есть содержание, а не только заголовок"),
    "C06": ("images_relevant", Severity.WARNING, "Картинки и иконки относятся к теме слайда"),
    "C07": ("no_garbage", Severity.ERROR, "Нет служебного мусора: реплик спикера, кусков промпта, заглушек"),
    "C08": ("no_typos", Severity.WARNING, "Текст без опечаток"),
    "C09": ("one_language", Severity.WARNING, "Текст слайда на языке колоды"),
    "C10": ("table_legend_relevant", Severity.INFO, "Все строки таблицы и элементы легенды работают на мысль слайда"),
    "C11": ("flow", Severity.WARNING, "Слайд логически связан с соседними"),
}
CHECK_IDS = [f"{k}_{v[0]}" for k, v in QUESTIONS.items()]
ERROR_CHECK_ID = "C00_judge_error"
# у титульных/разделителей/финала вывод в заголовке, «одна мысль», «есть содержание», соответствие заголовку
# и связь с соседями заведомо неприменимы (на кассете судья звал разделитель «Контекст» пустым слайдом)
STRUCTURAL_ARCHETYPES = {Archetype.TITLE, Archetype.SECTION, Archetype.CLOSING}
SKIP_FOR_STRUCTURAL = {"C01", "C02", "C03", "C05", "C11"}
SEVERITY_FALLBACK = Severity.WARNING


@dataclass
class SlideText:
    """Текстовый контекст одного слайда для судьи (строится из IR + outline или из AuditContext)."""

    idx: int
    title: str = ""
    text: str = ""
    archetype: Archetype | None = None
    facts: str = ""
    sources: list[str] = field(default_factory=list)


# ──────────────────────────── контекст слайдов ────────────────────────────


def slides_from_ir(ir: DeckIR, outline: DeckOutline | None = None, pack: Any = None) -> list[SlideText]:
    """Заголовок и текст — из элементов IR; факты — фрагменты контент-пакета по `OutlineSlide.sources`."""
    by_ref = {s.idx: s for s in outline.slides} if outline else {}
    out: list[SlideText] = []
    for s in ir.slides:
        title, lines = "", []
        for el in s.elements:
            txt = " ".join(r.text for p in el.paragraphs for r in p.runs).strip()
            if el.kind == SlotKind.TITLE and not title:
                title = txt
            elif txt:
                lines.append(txt)
            if el.chart:
                lines.append(f"[диаграмма: {el.chart.title}; категории: {', '.join(el.chart.categories)}; "
                             + "; ".join(f"{k}: {v}" for k, v in el.chart.series.items()) + "]")
            if el.table:
                lines.append("[таблица: " + " | ".join(el.table.header) + "; "
                             + "; ".join(" | ".join(r) for r in el.table.rows) + "]")
        o = by_ref.get(s.outline_ref)
        sources = list(o.sources) if o else []
        out.append(SlideText(idx=s.idx, title=title or (o.title if o else ""), text="\n".join(lines)[:MAX_TEXT_CHARS],
                             archetype=s.archetype, facts=_facts(pack, sources), sources=sources))
    return out


def slides_from_context(ctx: Any) -> list[SlideText]:
    """Для чужой колоды (без IR): текст из `AuditContext`, заголовок — плейсхолдер title или самый крупный кегль."""
    out: list[SlideText] = []
    for slide in ctx.slides:
        texts = [sh for sh in slide.shapes if sh.has_text and not sh.is_fixed]
        title_sh = next((sh for sh in texts if sh.ph_type in ("title", "ctrTitle")), None)
        if title_sh is None and texts:
            title_sh = max(texts, key=lambda sh: (sh.main_run.size_pt if sh.main_run else 0))
        body = [sh.text.strip() for sh in texts if sh is not title_sh]
        out.append(SlideText(idx=slide.idx, title=title_sh.text.strip() if title_sh else "",
                             text="\n".join(body)[:MAX_TEXT_CHARS], archetype=slide.archetype))
    return out


def _facts(pack: Any, sources: list[str]) -> str:
    """Сначала фрагменты из `sources`, затем — пока есть место — остальные (кроме целого брифа: он дублирует
    свои секции). `sources` у outline часто неполные, и судья без этого зовёт реальные цифры выдуманными (C04)."""
    if pack is None:
        return ""
    ids = {f.id for f in pack.fragments}
    order = [pack.get(sid) for sid in sources]
    # целые документы, у которых есть секции (brief, doc:product), не добавляем — они дублируют свои секции
    order += [f for f in pack.fragments if f.id not in sources
              and not any(other.startswith(f.id + ":") for other in ids)]
    parts: list[str] = []
    total = 0
    for frag in order:
        if frag is None:
            continue
        chunk = frag.to_prompt()
        if total + len(chunk) > MAX_FACT_CHARS:
            if not parts:  # хотя бы первый фрагмент, обрезанный
                parts.append(chunk[:MAX_FACT_CHARS] + "…")
            break
        parts.append(chunk)
        total += len(chunk) + 2
    return "\n\n".join(parts)


# ──────────────────────────── судья ────────────────────────────


def judge_deck(pngs: list[Path], slides: list[SlideText], client: Any, language: str = "ru",
               workers: int = WORKERS) -> list[Finding]:
    """Один вызов VLM на слайд, параллельно. PNG и slides сопоставляются по порядку (idx слайда)."""
    from deckforge.llm.skills import load_skill

    skill = load_skill(SKILL_NAME)
    if len(pngs) != len(slides):
        log.warning("contextual: PNG %d, слайдов %d — сопоставление по индексу", len(pngs), len(slides))
    pairs = list(zip(pngs, slides))
    if not pairs:
        return []
    titles = [s.title for s in slides]

    def one(i: int) -> list[Finding]:
        png, s = pairs[i]
        inputs = {
            "slide_idx": s.idx + 1, "slide_title": s.title or "(без заголовка)",
            "slide_text": s.text or "(текста нет)",
            "prev_slide_title": titles[i - 1] if i > 0 else "(нет — первый слайд)",
            "next_slide_title": titles[i + 1] if i + 1 < len(titles) else "(нет — последний слайд)",
            "source_facts": s.facts or "(факты не переданы)", "language": language,
        }
        try:
            res = client.run_skill(skill, images=[png], **inputs)
        except Exception as e:  # noqa: BLE001 — сбой одного слайда не роняет аудит
            log.warning("contextual: слайд %d — %s", s.idx + 1, e)
            return [Finding(check_id=ERROR_CHECK_ID, kind="contextual", severity=Severity.INFO, slide_idx=s.idx,
                            message=f"VLM-судья не ответил: {str(e)[:120]}", evidence={"error": str(e)[:200]})]
        return findings_from_answers(res if isinstance(res, dict) else {}, s)

    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        results = list(pool.map(one, range(len(pairs))))
    findings = [f for chunk in results for f in chunk]
    findings.sort(key=lambda f: (f.slide_idx, f.check_id))
    return findings


def findings_from_answers(res: dict, s: SlideText) -> list[Finding]:
    """{"answers": {"C01": {"ok": false, "note": "…"}}} → Finding по каждому «нет»."""
    answers = res.get("answers") if isinstance(res.get("answers"), dict) else {}
    out: list[Finding] = []
    for qid, (slug, sev, question) in QUESTIONS.items():
        if s.archetype in STRUCTURAL_ARCHETYPES and qid in SKIP_FOR_STRUCTURAL:
            continue
        ans = answers.get(qid)
        if not isinstance(ans, dict):
            continue
        ok = ans.get("ok")
        if ok is True or (isinstance(ok, str) and ok.strip().lower() in ("true", "yes", "да")):
            continue
        note = str(ans.get("note") or "").strip()
        if note.lower() == "n/a":
            continue
        out.append(Finding(check_id=f"{qid}_{slug}", kind="contextual", severity=sev, slide_idx=s.idx,
                           message=note or question, evidence={"question": question, "sources": ",".join(s.sources)}))
    return out


__all__ = ["CHECK_IDS", "ERROR_CHECK_ID", "QUESTIONS", "SlideText", "findings_from_answers", "judge_deck",
           "slides_from_context", "slides_from_ir"]
