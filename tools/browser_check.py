"""browser_check — HTML-экспорт и Streamlit UI в трёх браузерных движках (Playwright): Chromium, Firefox, WebKit.

WebKit — движок Safari; на Windows это ближайшая к Safari проверка (живой Safari на macOS — отдельно).

HTML: каждая колода в режиме показа (`?present#N`) — ошибки JS, число слайдов, скриншоты, расхождение
WebKit/Firefox с Chromium и Chromium с PNG LibreOffice. UI: Streamlit поднимается на свободном порту без ключа
LLM (бесплатно: режим «Готовый outline», судья выключен), в каждом движке — открыть, выбрать режим,
сгенерировать варианты, дождаться вкладок стратегий, переключить вкладку; ошибки JS и скриншоты.

    .venv\\Scripts\\python.exe -m pip install playwright && .venv\\Scripts\\python.exe -m playwright install chromium firefox webkit
    .venv\\Scripts\\python.exe tools\\browser_check.py ["examples\\output\\*\\*.html"] [--engines chromium,firefox,webkit]
        [--no-ui] [--no-html] [--slides 1,5] [--out out\\browsers]

Результат: out/browsers/report.md + report.json, скриншоты out/browsers/<движок>/….
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import socket
import subprocess
import sys
import time
import urllib.request
from dataclasses import asdict, dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from PIL import Image, ImageChops, ImageStat

VIEWPORT = {"width": 1280, "height": 720}
COMPARE_SIZE = (640, 360)
UI_TIMEOUT_S = 420  # генерация трёх вариантов с PNG — до пары минут на медленной машине
DIFF_WARN = 0.06  # средняя разница яркости (0..1) между движками, выше — посмотреть глазами


@dataclass
class HtmlResult:
    deck: str
    engine: str
    slides: int = 0
    errors: list[str] = field(default_factory=list)
    shots: list[str] = field(default_factory=list)
    diff_vs_chromium: float | None = None
    diff_vs_libreoffice: float | None = None


@dataclass
class UiResult:
    engine: str
    ok: bool = False
    steps: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    shots: list[str] = field(default_factory=list)
    seconds: float = 0.0


def mean_diff(a: Path, b: Path) -> float:
    """Средняя разница яркости двух картинок (0 — одинаковы, 1 — противоположны)."""
    with Image.open(a) as ia, Image.open(b) as ib:
        da = ia.convert("L").resize(COMPARE_SIZE)
        db = ib.convert("L").resize(COMPARE_SIZE)
        return ImageStat.Stat(ImageChops.difference(da, db)).mean[0] / 255.0


def _watch_errors(page, sink: list[str]) -> None:
    page.on("pageerror", lambda e: sink.append(f"pageerror: {e}"))
    page.on("console", lambda m: sink.append(f"console.{m.type}: {m.text}") if m.type == "error" else None)


# ──────────────────────────── HTML ────────────────────────────


def check_html(pw, decks: list[Path], engines: list[str], slides: list[int], out: Path) -> list[HtmlResult]:
    results: list[HtmlResult] = []
    for engine in engines:
        browser = getattr(pw, engine).launch()
        for deck in decks:
            rel = deck.relative_to(ROOT) if deck.is_relative_to(ROOT) else deck
            res = HtmlResult(str(rel).replace("\\", "/"), engine)
            for n in slides:
                # новая страница на каждый слайд: переход только по #N — та же страница, скрипт колоды его не видит
                page = browser.new_page(viewport=VIEWPORT)
                _watch_errors(page, res.errors)
                page.goto(deck.resolve().as_uri() + f"?present#{n}")
                page.wait_for_load_state("load")
                page.wait_for_timeout(400)  # fit() и шрифты
                res.slides = page.evaluate("document.querySelectorAll('section.slide').length")
                if n <= res.slides:
                    shot = out / engine / f"{deck.parent.name}_{deck.stem}_{n:02d}.png"
                    shot.parent.mkdir(parents=True, exist_ok=True)
                    page.screenshot(path=str(shot))
                    res.shots.append(str(shot.relative_to(out)).replace("\\", "/"))
                page.close()
            results.append(res)
        browser.close()
    # расхождения: движок против Chromium и Chromium против LibreOffice (slide_NN.png рядом с колодой)
    base = {r.deck: r for r in results if r.engine == "chromium"}
    for r in results:
        ref = base.get(r.deck)
        if r.engine != "chromium" and ref and r.shots and ref.shots:
            pairs = [(out / a, out / b) for a, b in zip(r.shots, ref.shots)]
            r.diff_vs_chromium = round(max(mean_diff(a, b) for a, b in pairs), 4)
        if r.engine == "chromium" and r.shots:
            deck = ROOT / r.deck
            diffs = []
            for shot in r.shots:
                n = int(Path(shot).stem.rsplit("_", 1)[1])
                lo = deck.parent / deck.stem / f"slide_{n:02d}.png"
                if lo.exists():
                    diffs.append(mean_diff(out / shot, lo))
            r.diff_vs_libreoffice = round(max(diffs), 4) if diffs else None
    return results


# ──────────────────────────── UI ────────────────────────────


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def start_ui(port: int, log: Path) -> subprocess.Popen:
    env = dict(os.environ, LLM_API_KEY="", DECKFORGE_PUBLIC="0", PYTHONIOENCODING="utf-8")
    log.parent.mkdir(parents=True, exist_ok=True)
    proc = subprocess.Popen(
        [sys.executable, "-m", "streamlit", "run", str(ROOT / "deckforge" / "ui" / "app.py"), "--server.port", str(port),
         "--server.headless", "true", "--browser.gatherUsageStats", "false"],
        cwd=ROOT, env=env, stdout=log.open("w", encoding="utf-8"), stderr=subprocess.STDOUT,
    )
    for _ in range(120):
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/_stcore/health", timeout=2)
            return proc
        except OSError:
            time.sleep(0.5)
    proc.kill()
    raise RuntimeError(f"Streamlit не поднялся на :{port}, лог: {log}")


def check_ui(pw, engines: list[str], port: int, out: Path) -> list[UiResult]:
    results: list[UiResult] = []
    url = f"http://127.0.0.1:{port}/"
    for engine in engines:
        res = UiResult(engine)
        t0 = time.time()
        browser = getattr(pw, engine).launch()
        page = browser.new_page(viewport={"width": 1440, "height": 900})
        _watch_errors(page, res.errors)

        def shot(name: str) -> None:
            p = out / engine / f"ui_{name}.png"
            p.parent.mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(p), full_page=True)
            res.shots.append(str(p.relative_to(out)).replace("\\", "/"))

        try:
            page.goto(url)
            page.get_by_text("Цифровой дизайнер презентаций").first.wait_for(timeout=60_000)
            res.steps.append("страница открылась")
            page.get_by_text("Шаблон:").first.wait_for(timeout=60_000)
            res.steps.append("шаблон разобран (палитра, шрифты, образцы)")
            shot("1_template")
            page.get_by_text("Готовый outline", exact=True).first.click()
            page.get_by_role("button", name="Сгенерировать варианты").click()
            res.steps.append("запущена генерация (Готовый outline, без LLM)")
            page.get_by_role("tab", name="visual").first.wait_for(timeout=UI_TIMEOUT_S * 1000)
            res.steps.append("вкладки вариантов появились")
            page.wait_for_timeout(1500)
            shot("2_variants")
            page.get_by_role("tab", name="visual").first.click()
            page.wait_for_timeout(1500)
            res.steps.append("переключена вкладка visual")
            shot("3_visual_tab")
            res.ok = True
        except Exception as e:  # noqa: BLE001 — любой сбой шага фиксируем в отчёте
            res.errors.append(f"шаг не пройден: {type(e).__name__}: {str(e).splitlines()[0][:200]}")
            shot("fail")
        res.seconds = round(time.time() - t0, 1)
        browser.close()
        results.append(res)
    return results


# ──────────────────────────── отчёт ────────────────────────────


def report(html: list[HtmlResult], ui: list[UiResult], out: Path) -> Path:
    lines = ["# Проверка в браузерных движках (Playwright)", "",
             "WebKit — движок Safari; живой Safari на macOS этим не заменяется полностью.", ""]
    if ui:
        lines += ["## Streamlit UI", "", "| движок | итог | шаги | ошибки JS | время, с |", "|---|---|---|---|---|"]
        for r in ui:
            errs = "; ".join(r.errors)[:200] or "—"
            lines.append(f"| {r.engine} | {'ok' if r.ok else 'FAIL'} | {len(r.steps)} | {errs} | {r.seconds} |")
        lines.append("")
    if html:
        lines += ["## HTML-экспорт", "",
                  "| колода | движок | слайдов | ошибки JS | Δ к Chromium | Δ Chromium к LibreOffice |", "|---|---|---|---|---|---|"]
        for r in html:
            flag = " ⚠" if r.diff_vs_chromium is not None and r.diff_vs_chromium > DIFF_WARN else ""
            errs = "; ".join(r.errors)[:120] or "—"
            lines.append(f"| {r.deck} | {r.engine} | {r.slides} | {errs} | "
                         f"{'' if r.diff_vs_chromium is None else r.diff_vs_chromium}{flag} | "
                         f"{'' if r.diff_vs_libreoffice is None else r.diff_vs_libreoffice} |")
        bad = [r for r in html if r.errors]
        worst = max((r.diff_vs_chromium or 0 for r in html), default=0)
        lines += ["", f"Колод×движков: {len(html)}, с ошибками JS: {len(bad)}, "
                      f"наибольшее расхождение с Chromium: {worst:.3f} (порог внимания {DIFF_WARN})."]
    path = out / "report.md"
    path.write_text("\n".join(lines) + "\n", "utf-8")
    (out / "report.json").write_text(json.dumps({"html": [asdict(r) for r in html], "ui": [asdict(r) for r in ui]},
                                                ensure_ascii=False, indent=1), "utf-8")
    return path


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("decks", nargs="*", default=["examples/output/*/*.html"])
    ap.add_argument("--engines", default="chromium,firefox,webkit")
    ap.add_argument("--slides", default="1,5", help="какие слайды снимать в режиме показа")
    ap.add_argument("--no-ui", action="store_true")
    ap.add_argument("--no-html", action="store_true")
    ap.add_argument("--out", default=str(ROOT / "out" / "browsers"))
    args = ap.parse_args()
    from playwright.sync_api import sync_playwright

    engines = [e.strip() for e in args.engines.split(",") if e.strip()]
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    decks = sorted({Path(p).resolve() for pat in args.decks for p in glob.glob(pat)})
    slides = [int(x) for x in args.slides.split(",") if x.strip()]
    html_res: list[HtmlResult] = []
    ui_res: list[UiResult] = []
    with sync_playwright() as pw:
        if not args.no_html and decks:
            html_res = check_html(pw, decks, engines, slides, out)
        if not args.no_ui:
            port = _free_port()
            proc = start_ui(port, out / "streamlit.log")
            try:
                ui_res = check_ui(pw, engines, port, out)
            finally:
                proc.terminate()
                try:
                    proc.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    proc.kill()
    path = report(html_res, ui_res, out)
    print(path.read_text("utf-8"))
    failed = any(r.errors for r in html_res) or any(not r.ok or r.errors for r in ui_res)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
