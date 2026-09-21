"""powerpoint_check — открыть колоды в настоящем PowerPoint (COM, только Windows) и проверить нативность.

Для каждого .pptx: открытие без «восстановления» и SaveCopyAs; объекты по слайдам (текст / chart / table /
picture / пустые плейсхолдеры); шрифты против dna.json; PNG средствами PowerPoint и доля пикселей,
отличающихся от LibreOffice-рендера.

    .venv\\Scripts\\python.exe tools\\powerpoint_check.py "examples\\output\\*\\*.pptx" docs\\deckforge_pitch.pptx
        [--out out\\ppt] [--no-render] [--diff-threshold 0.15]

Результат: out/ppt/report.md + report.json, out/ppt/<колода>/slide_NN.png + contact.png + roundtrip.pptx.
"""

from __future__ import annotations

import argparse
import glob
import json
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from PIL import Image, ImageChops

from deckforge.export.render import contact_sheet, render

MSO_GROUP, MSO_PICTURE, MSO_LINKED_PICTURE, MSO_PLACEHOLDER = 6, 13, 11, 14
MSO_FILL_PICTURE = 6
PP_ALERTS_NONE = 1
EXPORT_W, EXPORT_H = 1280, 720
PIXEL_DELTA = 40  # разница по каналу, с которой пиксель считается «другим»
# шрифты, которые PowerPoint подставляет сам
FONT_IGNORE = {"", "Wingdings", "Symbol", "Arial", "Calibri", "+mj-lt", "+mn-lt", "+mj-ea", "+mn-ea"}


@dataclass
class SlideStat:
    index: int
    text: int = 0
    chart: int = 0
    table: int = 0
    picture: int = 0
    empty_placeholders: int = 0
    charts_without_series: int = 0
    fonts: list[str] = field(default_factory=list)
    diff_vs_libreoffice: float | None = None

    @property
    def is_picture_only(self) -> bool:
        return self.picture > 0 and self.text + self.chart + self.table == 0


@dataclass
class DeckReport:
    deck: str
    opened: bool = False
    error: str = ""
    roundtrip: bool = False
    slides: list[SlideStat] = field(default_factory=list)
    foreign_fonts: list[str] = field(default_factory=list)
    suspicious: list[int] = field(default_factory=list)  # 1-based
    seconds: float = 0.0

    def totals(self) -> dict[str, int]:
        keys = ("text", "chart", "table", "picture", "empty_placeholders", "charts_without_series")
        return {k: sum(getattr(s, k) for s in self.slides) for k in keys}


def walk_shapes(shapes):
    """Фигуры слайда, группы — насквозь."""
    for sh in shapes:
        if sh.Type == MSO_GROUP:
            yield from walk_shapes(sh.GroupItems)
        else:
            yield sh


def inspect_slide(slide, index: int) -> SlideStat:
    st = SlideStat(index)
    fonts: set[str] = set()
    for sh in walk_shapes(slide.Shapes):
        try:
            if sh.HasChart:
                st.chart += 1
                if sh.Chart.SeriesCollection().Count == 0:
                    st.charts_without_series += 1
                continue
            if sh.HasTable:
                st.table += 1
                continue
        except Exception:  # noqa: BLE001
            pass
        contained, fill_type, fill_visible = sh.Type, None, False
        if sh.Type == MSO_PLACEHOLDER:  # заполненный плейсхолдер остаётся msoPlaceholder
            try:
                contained = sh.PlaceholderFormat.ContainedType
                fill_type, fill_visible = sh.Fill.Type, bool(sh.Fill.Visible)
            except Exception:  # noqa: BLE001
                pass
        if contained in (MSO_PICTURE, MSO_LINKED_PICTURE) or fill_type == MSO_FILL_PICTURE:
            st.picture += 1  # blipFill на плейсхолдере
            continue
        has_text = False
        try:
            has_text = bool(sh.HasTextFrame and sh.TextFrame.HasText)
        except Exception:  # noqa: BLE001
            pass
        if has_text:
            st.text += 1
            try:
                for run in sh.TextFrame.TextRange.Runs():
                    fonts.add(run.Font.Name)
            except Exception:  # noqa: BLE001
                pass
        elif sh.Type == MSO_PLACEHOLDER and not fill_visible:
            st.empty_placeholders += 1  # в редакторе будет подсказка
    st.fonts = sorted(fonts)
    return st


def template_fonts(deck: Path) -> set[str] | None:
    dna = deck.parent / "dna.json"
    if not dna.exists():
        return None
    d = json.loads(dna.read_text("utf-8"))
    fonts = set(d.get("fonts", [])) | set(d.get("embedded_fonts", []))
    fonts |= {t["font"] for t in d.get("typography", []) if t.get("font")}
    return fonts


def libreoffice_pngs(deck: Path, do_render: bool) -> list[Path]:
    """PNG LibreOffice рядом с колодой, иначе рендер в out/render."""
    sub = deck.parent / deck.stem
    pngs = sorted(sub.glob("slide_*.png"))
    if pngs:
        return pngs
    if not do_render:
        return []
    return render(deck, ROOT / "out" / "render" / deck.stem)


def pixel_diff(a: Path, b: Path) -> float:
    """Доля пикселей, отличающихся более чем на PIXEL_DELTA."""
    ia = Image.open(a).convert("RGB")
    ib = Image.open(b).convert("RGB").resize(ia.size)
    diff = ImageChops.difference(ia, ib).convert("L").point(lambda v: 255 if v > PIXEL_DELTA else 0)
    hist = diff.histogram()
    return hist[255] / (ia.width * ia.height)


def check_deck(app, deck: Path, out: Path, do_render: bool, threshold: float) -> DeckReport:
    rep = DeckReport(str(deck.relative_to(ROOT)) if deck.is_relative_to(ROOT) else str(deck))
    t0 = time.perf_counter()
    out.mkdir(parents=True, exist_ok=True)
    try:
        pres = app.Presentations.Open(str(deck), True, False, False)  # ReadOnly, Untitled, WithWindow
    except Exception as e:  # noqa: BLE001
        rep.error = f"не открылся: {str(e)[:200]}"
        rep.seconds = round(time.perf_counter() - t0, 1)
        return rep
    rep.opened = True
    try:
        try:
            pres.SaveCopyAs(str(out / "roundtrip.pptx"))
            rep.roundtrip = True
        except Exception as e:  # noqa: BLE001
            rep.error = f"SaveCopyAs: {str(e)[:200]}"
        pngs: list[Path] = []
        for i, slide in enumerate(pres.Slides):
            rep.slides.append(inspect_slide(slide, i + 1))
            png = out / f"slide_{i + 1:02d}.png"
            slide.Export(str(png), "PNG", EXPORT_W, EXPORT_H)
            pngs.append(png)
        if pngs:
            contact_sheet(pngs, out / "contact.png")
    finally:
        pres.Close()

    allowed = template_fonts(deck)
    if allowed is not None:
        used = {f for s in rep.slides for f in s.fonts}
        rep.foreign_fonts = sorted(used - allowed - FONT_IGNORE)

    lo = libreoffice_pngs(deck, do_render)
    for st, png in zip(rep.slides, pngs):
        if st.index - 1 < len(lo):
            st.diff_vs_libreoffice = round(pixel_diff(png, lo[st.index - 1]), 3)
            if st.diff_vs_libreoffice > threshold:
                rep.suspicious.append(st.index)
    rep.seconds = round(time.perf_counter() - t0, 1)
    return rep


def markdown(reports: list[DeckReport], threshold: float) -> str:
    lines = [
        "# PowerPoint check", "",
        f"Расхождение — доля пикселей, отличающихся от LibreOffice-рендера более чем на {PIXEL_DELTA}/255; "
        f"«смотреть» — слайды с расхождением > {threshold:.0%}.", "",
        "| колода | открылась | roundtrip | слайдов | текст | chart | table | picture | пустых плейсхолдеров | "
        "слайдов-картинок | chart без серий | чужие шрифты | расхождение ср./макс. | смотреть | с |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for r in reports:
        t = r.totals()
        diffs = [s.diff_vs_libreoffice for s in r.slides if s.diff_vs_libreoffice is not None]
        avg = f"{sum(diffs) / len(diffs):.1%} / {max(diffs):.1%}" if diffs else "—"
        pic_only = sum(1 for s in r.slides if s.is_picture_only)
        lines.append(
            f"| {r.deck} | {'да' if r.opened else 'НЕТ: ' + r.error} | {'да' if r.roundtrip else 'нет'} | {len(r.slides)} | "
            f"{t['text']} | {t['chart']} | {t['table']} | {t['picture']} | {t['empty_placeholders']} | {pic_only} | "
            f"{t['charts_without_series']} | {', '.join(r.foreign_fonts) or '—'} | {avg} | "
            f"{', '.join(map(str, r.suspicious)) or '—'} | {r.seconds} |"
        )
    problems = [r for r in reports if not r.opened or not r.roundtrip or r.foreign_fonts
                or any(s.is_picture_only or s.empty_placeholders or s.charts_without_series for s in r.slides)]
    lines += ["", f"Колод с проблемами (кроме расхождений рендера): {len(problems)} из {len(reports)}."]
    return "\n".join(lines) + "\n"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("patterns", nargs="+", help="пути или glob-маски .pptx")
    ap.add_argument("--out", type=Path, default=ROOT / "out" / "ppt")
    ap.add_argument("--no-render", action="store_true", help="не рендерить LibreOffice-PNG, если их нет рядом с колодой")
    ap.add_argument("--diff-threshold", type=float, default=0.15)
    a = ap.parse_args()

    files = [Path(p).resolve() for pat in a.patterns for p in (glob.glob(pat) or [pat])]
    a.out = a.out.resolve()  # COM относительных путей не понимает
    import win32com.client

    app = win32com.client.DispatchEx("PowerPoint.Application")  # свой экземпляр
    app.DisplayAlerts = PP_ALERTS_NONE
    reports: list[DeckReport] = []
    try:
        for f in files:
            name = f.parent.name + "_" + f.stem if f.parent.name != "output" else f.stem
            rep = check_deck(app, f, a.out / name, not a.no_render, a.diff_threshold)
            reports.append(rep)
            t = rep.totals()
            print(f"{rep.deck}: {'ok' if rep.opened else rep.error}; слайдов {len(rep.slides)}, текст {t['text']}, "
                  f"chart {t['chart']}, table {t['table']}, picture {t['picture']}, пустых плейсхолдеров "
                  f"{t['empty_placeholders']}, смотреть: {rep.suspicious or '—'} ({rep.seconds} с)")
    finally:
        app.Quit()

    a.out.mkdir(parents=True, exist_ok=True)
    (a.out / "report.md").write_text(markdown(reports, a.diff_threshold), "utf-8")
    (a.out / "report.json").write_text(json.dumps([asdict(r) for r in reports], ensure_ascii=False, indent=1), "utf-8")
    print(f"\n→ {a.out / 'report.md'}")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
