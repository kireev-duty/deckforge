"""stress_test — крайние outline × шаблоны × стратегии без LLM: падения, зацикливания, потеря контента.

    .venv\\Scripts\\python.exe tools\\stress_test.py [--wild] [--cases all_kpi,text_extremes] [--templates "VK Tech"]
        [--strategies executive,narrative,visual] [--timeout 120] [--png-sample 3]
    .venv\\Scripts\\python.exe tools\\stress_test.py --audit-all      # кросс-аудит и HTML всех колод, что есть

Кейсы — `tests/fixtures/stress_outlines.py`. Каждая пара «кейс × шаблон» идёт отдельным подпроцессом с таймаутом.
Результат: `out/stress/<template>/<case>/<strategy>.*`, сводка `out/stress/report.md` + `report.json`.

Кейс «упал», если: исключение, таймаут, пропущенный слайд, пустая колода или раздувание (см. `bloat_problems`).
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

OUT = ROOT / "out" / "stress"
TEMPLATE_DIRS = [ROOT / "data" / "templates", ROOT / "data" / "holdout"]
WILD_DIR = ROOT / "data" / "wild"
MAX_PER_REF = 4  # слайдов колоды на один слайд outline
BLOAT = 2.0  # слайдов колоды к max(len(outline), target_slides.max)


def per_ref_max(slides: list) -> int:
    """Сколько слайдов колоды породил самый «плодовитый» слайд outline."""
    per_ref: dict[int, int] = {}
    for sl in slides:
        per_ref[sl.outline_ref] = per_ref.get(sl.outline_ref, 0) + 1
    return max(per_ref.values(), default=0)


def bloat_problems(slides: list, n_outline: int, strategy, case: str = "") -> list[str]:
    """Критерии раздувания/пустоты колоды (те же в `tests/test_stress.py`)."""
    limit = int(BLOAT * max(n_outline, strategy.target_slides.max))
    problems: list[str] = []
    if case != "empty" and len(slides) < 1:
        problems.append("колода пустая")
    if len(slides) > limit:
        problems.append(f"slides={len(slides)} > {limit}")
    if (m := per_ref_max(slides)) > MAX_PER_REF:
        problems.append(f"на один слайд outline — {m} слайдов колоды")
    return problems


# ──────────────────────────── worker: один кейс × один шаблон ────────────────────────────


def worker(case: str, template: Path, strategies: list[str], out_dir: Path, html: bool = True) -> dict:
    from deckforge.core.strategy import load_strategy
    from deckforge.pipeline import RunConfig, run
    from tests.fixtures.stress_outlines import all_cases

    outline = all_cases(out_dir / "_files")[case]
    cfg = RunConfig(
        template=template, content_pack=ROOT / "examples" / "content_pack", purpose=outline.purpose,
        audience=outline.audience, language=outline.language, strategies=strategies, output_dir=out_dir,
        render_png=False, images="off", audit={"deterministic": True, "contextual": False, "autofix": True},
        export=["pptx", "html"] if html else ["pptx"],
    )
    log: list[str] = []
    t0 = time.perf_counter()
    res = run(cfg, outline=outline, progress=log.append)
    rows = []
    for d in res.decks:
        m = d.load_manifest()
        ir = d.load_ir()
        max_per_ref = per_ref_max(ir.slides)
        problems = bloat_problems(ir.slides, len(outline.slides), load_strategy(d.strategy), case)
        if d.stats["skipped"]:
            problems.insert(0, f"skipped={d.stats['skipped']}")
        if not d.pptx.exists() or d.pptx.stat().st_size < 1000:
            problems.append("pptx пустой/нет")
        if "html" in cfg.export and d.html is None:
            problems.append("html не собран")
        rows.append({
            "strategy": d.strategy, "status": "fail" if problems else "ok", "problems": problems,
            "seconds": round(sum(v for k, v in d.timings_s.items() if k not in ("parse", "outline")), 2),
            "slides": d.stats["slides"], "skipped": d.stats["skipped"], "ir_slides": len(ir.slides),
            "max_per_ref": max_per_ref, "outline_slides": len(outline.slides),
            "errors": d.audit_summary.get("errors"), "warnings": d.audit_summary.get("warnings"),
            "by_check": d.audit_summary.get("by_check", {}),
            "run_warnings": len(d.warnings), "run_warnings_sample": d.warnings[:5],
            "pptx": str(d.pptx),
        })
    return {"status": "ok", "seconds": round(time.perf_counter() - t0, 2), "decks": rows, "log": log[-6:]}


def run_worker_cli(a: argparse.Namespace) -> None:
    out_dir = Path(a.worker_out)
    try:
        result = worker(a.worker[0], Path(a.worker[1]), a.strategies.split(","), out_dir, html=not a.no_html)
    except BaseException as e:  # noqa: BLE001 — падение — результат кейса
        result = {"status": "exception", "error": f"{type(e).__name__}: {str(e)[:300]}",
                  "traceback": traceback.format_exc()[-3000:], "decks": []}
    (out_dir / "_result.json").write_text(json.dumps(result, ensure_ascii=False, indent=1), "utf-8")


# ──────────────────────────── матрица ────────────────────────────


def templates_for(a: argparse.Namespace) -> list[Path]:
    dirs = list(TEMPLATE_DIRS) + ([WILD_DIR] if a.wild else [])
    paths = [p for d in dirs if d.is_dir() for p in sorted(d.glob("*.pptx")) if p.stat().st_size > 1000]
    if a.templates:
        wanted = [w.strip().lower() for w in a.templates.split(",")]
        paths = [p for p in paths if any(w in p.name.lower() for w in wanted)]
    return paths


def run_matrix(a: argparse.Namespace) -> int:
    from tests.fixtures.stress_outlines import CASES

    cases = list(CASES) + ["image_files"]
    if a.cases:
        cases = [c.strip() for c in a.cases.split(",")]
    templates = templates_for(a)
    if not templates:
        print("шаблоны не найдены (LFS?)")
        return 2
    OUT.mkdir(parents=True, exist_ok=True)
    results: list[dict] = []
    total = len(cases) * len(templates)
    n = 0
    for tpl in templates:
        for case in cases:
            n += 1
            out_dir = OUT / _stem(tpl) / case
            out_dir.mkdir(parents=True, exist_ok=True)
            (out_dir / "_result.json").unlink(missing_ok=True)
            cmd = [sys.executable, str(Path(__file__)), "--worker", case, str(tpl), "--worker-out", str(out_dir),
                   "--strategies", a.strategies] + (["--no-html"] if a.no_html else [])
            t0 = time.perf_counter()
            try:
                proc = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace",
                                      timeout=a.timeout)
                result = _read_result(out_dir, proc)
            except subprocess.TimeoutExpired:
                result = {"status": "timeout", "error": f"> {a.timeout} с", "decks": []}
            result.update({"case": case, "template": _stem(tpl), "wall_s": round(time.perf_counter() - t0, 1)})
            results.append(result)
            print(f"[{n:3d}/{total}] {_stem(tpl)[:28]:<28} {case:<24} {_line(result)}", flush=True)
    (OUT / "report.json").write_text(json.dumps(results, ensure_ascii=False, indent=1), "utf-8")
    (OUT / "report.md").write_text(report_md(results), "utf-8")
    if a.png_sample:
        _png_sample(results, a.png_sample)
    bad = [r for r in results if r["status"] != "ok" or any(d["status"] != "ok" for d in r["decks"])]
    print(f"\nитого: {len(results)} пар, проблемных {len(bad)} → {OUT / 'report.md'}")
    return 1 if bad else 0


def _read_result(out_dir: Path, proc: subprocess.CompletedProcess) -> dict:
    p = out_dir / "_result.json"
    if p.exists():
        r = json.loads(p.read_text("utf-8"))
        if proc.returncode and r["status"] == "ok":
            r["stderr"] = proc.stderr[-1500:]
        return r
    return {"status": "crash", "error": f"процесс завершился с кодом {proc.returncode} без результата",
            "traceback": (proc.stderr or proc.stdout)[-3000:], "decks": []}


def _line(r: dict) -> str:
    if r["status"] != "ok":
        return f"{r['status'].upper()}: {r.get('error', '')[:100]}"
    parts = []
    for d in r["decks"]:
        mark = "" if d["status"] == "ok" else " ← " + "; ".join(d["problems"])
        parts.append(f"{d['strategy'][:3]} {d['slides']}сл {d['errors']}/{d['warnings']} {d['seconds']}s{mark}")
    return " | ".join(parts)


def report_md(results: list[dict]) -> str:
    lines = ["# Стресс-тест: крайние outline × шаблоны × стратегии", "",
             f"Пар «кейс × шаблон»: {len(results)}; исключений: {sum(r['status'] == 'exception' for r in results)}; "
             f"таймаутов: {sum(r['status'] == 'timeout' for r in results)}; крашей: {sum(r['status'] == 'crash' for r in results)}; "
             f"колод с проблемами: {sum(d['status'] != 'ok' for r in results for d in r['decks'])}.", ""]
    bad = [r for r in results if r["status"] != "ok"]
    if bad:
        lines += ["## Исключения и таймауты", ""]
        for r in bad:
            lines += [f"### {r['template']} × {r['case']} — {r['status']}", "", f"`{r.get('error', '')}`", "",
                      "```", (r.get("traceback") or "")[-1500:], "```", ""]
    lines += ["## Колоды", "", "| шаблон | кейс | стратегия | статус | с | слайдов | skipped | err/warn | warn прогона | проблемы |",
              "|---|---|---|---|---|---|---|---|---|---|"]
    for r in results:
        for d in r["decks"]:
            lines.append(f"| {r['template'][:30]} | {r['case']} | {d['strategy']} | {d['status']} | {d['seconds']} | "
                         f"{d['slides']} | {d['skipped']} | {d['errors']}/{d['warnings']} | {d['run_warnings']} | "
                         f"{'; '.join(d['problems'])} |")
    slow = sorted((d["seconds"], r["template"], r["case"], d["strategy"]) for r in results for d in r["decks"])[-5:]
    lines += ["", "## Самые медленные колоды (layout+render+audit+autofix+html)", ""]
    lines += [f"- {s} с — {t} × {c} × {st}" for s, t, c, st in reversed(slow)]
    checks: dict[str, int] = {}
    for r in results:
        for d in r["decks"]:
            for k, v in (d.get("by_check") or {}).items():
                checks[k] = checks.get(k, 0) + v
    lines += ["", "## Находки аудита по проверкам (сумма по всем колодам)", ""]
    lines += [f"- {k}: {v}" for k, v in sorted(checks.items(), key=lambda kv: -kv[1])]
    return "\n".join(lines) + "\n"


def _png_sample(results: list[dict], n: int) -> None:
    """PNG для первых n проблемных (или просто первых) колод."""
    from deckforge.export.render import render

    decks = [d for r in results for d in r["decks"] if d["status"] != "ok"] or [d for r in results for d in r["decks"]]
    for d in decks[:n]:
        pptx = Path(d["pptx"])
        try:
            pngs = render(pptx, pptx.parent / pptx.stem, dpi=72, contact=True)
            print(f"png: {len(pngs)} → {pptx.parent / pptx.stem / 'contact.png'}")
        except Exception as e:  # noqa: BLE001
            print(f"png: {pptx.name}: {e}")


# ──────────────────────────── --audit-all ────────────────────────────


def audit_all(a: argparse.Namespace) -> int:
    """Аудит, deck_reader и HTML-экспорт всех колод против DNA чужого шаблона: не падать."""
    from deckforge.audit import audit_deck
    from deckforge.core.deck_reader import read_shapes
    from deckforge.core.package import Package, PartCtx
    from deckforge.export.html import export_html
    from deckforge.pipeline import parse_template

    templates = templates_for(a)
    decks = sorted({*(ROOT / "examples" / "output").rglob("*.pptx"), *(ROOT / "examples" / "pitch").glob("*.pptx"),
                    *OUT.rglob("*.pptx"), *(WILD_DIR.glob("*.pptx") if a.wild else [])})
    decks = [d for d in decks if d.stat().st_size > 1000]
    print(f"колод: {len(decks)}, шаблонов для кросс-аудита: {len(templates)}")
    dnas = {}
    for t in templates:
        try:
            dnas[t] = parse_template(t).dna
        except Exception as e:  # noqa: BLE001
            print(f"parse {t.name}: {type(e).__name__}: {e}")
    rows = []
    html_dir = OUT / "_audit_all"
    html_dir.mkdir(parents=True, exist_ok=True)
    for i, deck in enumerate(decks):
        tpl = list(dnas)[i % len(dnas)]  # чужой шаблон по кругу
        row = {"deck": str(deck.relative_to(ROOT)), "template": _stem(tpl), "audit": "", "reader": "", "html": ""}
        t0 = time.perf_counter()
        try:
            rep = audit_deck(deck, dnas[tpl])
            row["audit"] = f"ok {rep.duration_s}s {rep.errors} err / {len(rep.findings)} находок"
            if rep.duration_s > 5:
                row["audit"] += " МЕДЛЕННО"
        except Exception as e:  # noqa: BLE001
            row["audit"] = f"FAIL {type(e).__name__}: {str(e)[:120]}"
        try:
            pkg = Package(deck)
            ctxs = [c for s in pkg.slides if (c := PartCtx.for_slide(pkg, s)) is not None]
            n = sum(len(read_shapes(c)) for c in ctxs)
            row["reader"] = f"ok {len(ctxs)} сл / {n} фигур"
        except Exception as e:  # noqa: BLE001
            row["reader"] = f"FAIL {type(e).__name__}: {str(e)[:120]}"
        try:
            out = export_html(deck, html_dir / f"{deck.stem}_{i}.html")
            row["html"] = f"ok {out.stat().st_size >> 10} КБ"
        except Exception as e:  # noqa: BLE001
            row["html"] = f"FAIL {type(e).__name__}: {str(e)[:120]}"
        row["seconds"] = round(time.perf_counter() - t0, 1)
        rows.append(row)
        print(f"[{i + 1:3d}/{len(decks)}] {deck.name[:40]:<40} vs {_stem(tpl)[:20]:<20} {row['audit']} | {row['reader']} | {row['html']}")
    md = ["# Кросс-аудит, deck_reader и HTML всех колод", "", "| колода | DNA шаблона | аудит | reader | html | с |", "|---|---|---|---|---|---|"]
    md += [f"| {r['deck']} | {r['template']} | {r['audit']} | {r['reader']} | {r['html']} | {r['seconds']} |" for r in rows]
    (OUT / "audit_all.md").write_text("\n".join(md) + "\n", "utf-8")
    bad = [r for r in rows if "FAIL" in r["audit"] + r["reader"] + r["html"]]
    print(f"\nитого: {len(rows)} колод, с ошибками {len(bad)} → {OUT / 'audit_all.md'}")
    return 1 if bad else 0


def _stem(p: Path) -> str:
    return p.stem.replace(" ", "_")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--wild", action="store_true", help="плюс 13 чужих шаблонов data/wild")
    ap.add_argument("--cases", default="", help="через запятую; по умолчанию все из tests/fixtures/stress_outlines.CASES")
    ap.add_argument("--templates", default="", help="подстроки имён через запятую")
    ap.add_argument("--strategies", default="executive,narrative,visual")
    ap.add_argument("--timeout", type=int, default=180, help="секунд на пару кейс × шаблон (все стратегии)")
    ap.add_argument("--png-sample", type=int, default=0, help="отрендерить PNG для N проблемных колод")
    ap.add_argument("--no-html", action="store_true", help="без HTML-экспорта (быстрее на большой матрице)")
    ap.add_argument("--audit-all", action="store_true")
    ap.add_argument("--worker", nargs=2, metavar=("CASE", "TEMPLATE"), help=argparse.SUPPRESS)
    ap.add_argument("--worker-out", help=argparse.SUPPRESS)
    a = ap.parse_args()
    if a.worker:
        run_worker_cli(a)
        return 0
    if a.audit_all:
        return audit_all(a)
    return run_matrix(a)


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    sys.exit(main())
