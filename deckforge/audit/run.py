"""Раннер аудита: .pptx + TemplateDNA (+ DeckIR) → AuditReport; сводка для manifest и таблица для CLI."""

from __future__ import annotations

import time
from collections import Counter
from collections.abc import Iterable
from pathlib import Path

from deckforge.audit.context import AuditContext
from deckforge.audit.deterministic import CHECKS, check_by_prefix
from deckforge.audit.deterministic.integrity import probe_file
from deckforge.core.ir import AuditReport, DeckIR, Finding, Severity, TemplateDNA

SEVERITY_ORDER = {Severity.ERROR: 0, Severity.WARNING: 1, Severity.INFO: 2}


def audit_deck(pptx: str | Path, dna: TemplateDNA, ir: DeckIR | None = None,
               checks: Iterable[str] | None = None) -> AuditReport:
    """Детерминированные проверки; `checks` — id или префиксы, по умолчанию все. Файл не открылся — только I01."""
    t0 = time.perf_counter()
    pptx = Path(pptx)
    selected = _select(checks)
    broken = probe_file(pptx)
    if broken is not None:
        return AuditReport(deck_path=str(pptx), findings=[broken], checks_run=["I01_file"],
                           duration_s=round(time.perf_counter() - t0, 3))
    ctx = AuditContext(pptx, dna, ir)
    findings: list[Finding] = []
    for cid, fn in selected.items():
        findings.extend(fn(ctx))
    findings.sort(key=lambda f: (f.slide_idx, SEVERITY_ORDER[f.severity], f.check_id))
    return AuditReport(deck_path=str(pptx), findings=findings, checks_run=list(selected),
                       duration_s=round(time.perf_counter() - t0, 3))


def _select(checks: Iterable[str] | None) -> dict:
    if not checks:
        return dict(CHECKS)
    out = {}
    for c in checks:
        cid, fn = (c, CHECKS[c]) if c in CHECKS else check_by_prefix(c)
        out[cid] = fn
    return out


def with_contextual(report: AuditReport, findings: list[Finding], checks: Iterable[str], duration_s: float = 0.0) -> AuditReport:
    """Добавить находки VLM-судьи к детерминированному отчёту."""
    merged = [*report.findings, *findings]
    merged.sort(key=lambda f: (f.slide_idx, SEVERITY_ORDER[f.severity], f.check_id))
    return report.model_copy(update={"findings": merged, "checks_run": [*report.checks_run, *checks],
                                     "duration_s": round(report.duration_s + duration_s, 3)})


def summary(report: AuditReport) -> dict:
    """Компактная сводка для manifest.json."""
    by_sev = Counter(f.severity.value for f in report.findings)
    by_check = Counter(f.check_id for f in report.findings)
    by_slide = Counter(f.slide_idx for f in report.findings if f.severity != Severity.INFO)
    return {
        "errors": by_sev.get("error", 0), "warnings": by_sev.get("warning", 0), "info": by_sev.get("info", 0),
        "contextual": sum(1 for f in report.findings if f.kind == "contextual"),
        "by_check": dict(sorted(by_check.items())),
        "slides_with_issues": sorted(by_slide),
        "checks_run": len(report.checks_run), "duration_s": report.duration_s,
    }


def report_markdown(report: AuditReport, max_rows: int = 80) -> str:
    s = summary(report)
    head = (f"**{Path(report.deck_path).name}**: ошибок {s['errors']}, предупреждений {s['warnings']}, "
            f"info {s['info']} · проверок {s['checks_run']} · {report.duration_s:.1f} с")
    if not report.findings:
        return head + "\n\nНаходок нет."
    lines = [head, "", "| слайд | severity | проверка | сообщение | autofix |", "|---|---|---|---|---|"]
    for f in report.findings[:max_rows]:
        slide = "файл" if f.slide_idx < 0 else str(f.slide_idx + 1)
        lines.append(f"| {slide} | {f.severity.value} | {f.check_id} | {f.message.replace('|', '/')} | {f.autofix or '—'} |")
    if len(report.findings) > max_rows:
        lines.append(f"| … | | | ещё {len(report.findings) - max_rows} находок | |")
    return "\n".join(lines)


__all__ = ["audit_deck", "report_markdown", "summary", "with_contextual"]
