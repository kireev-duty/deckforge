"""render_smoke — прогон render/pptx_writer без layout/ и content/: синтетический DeckIR по образцам шаблона.

Для каждого выбранного образца создаётся слайд, каждый слот заполняется заглушкой по его kind
(заголовок, буллеты, цифры KPI, картинка-градиент, нативная диаграмма/таблица). Результат —
out/smoke/<stem>.pptx, затем рендер в out/render/<stem>_smoke/ (contact.png + slide_NN.png).

    .venv\\Scripts\\python.exe tools\\render_smoke.py "..\\Датасет\\VK Tech шаблон.pptx" [--all] [--archetypes cards,kpi] [--no-render]

Образцы берутся из out/archetypes/<stem>.json (с VLM-уточнёнными слотами), если он есть; иначе —
классификация правилами. По умолчанию — один лучший образец на архетип, --all — все по разу.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools.render_deck import render  # noqa: E402

from deckforge.core.ir import (  # noqa: E402
    Archetype,
    ChartSpec,
    DeckIR,
    Element,
    Exemplar,
    Paragraph,
    Slot,
    SlideIR,
    SlotKind,
    TableSpec,
    TextRun,
)
from deckforge.parsing.extract_tokens import extract_tokens  # noqa: E402
from deckforge.parsing.layout_classifier import classify_template  # noqa: E402
from deckforge.render import render_pptx  # noqa: E402

FILLER = [
    "Единая точка входа для команд и данных",
    "Снижение времени согласования на 40 %",
    "Интеграция с корпоративным каталогом за один день",
    "Безопасность: шифрование и аудит действий",
    "Поддержка 24/7 и SLA 99,9 %",
    "Масштабирование без остановки сервиса",
    "Открытый API и готовые коннекторы",
    "Аналитика использования в реальном времени",
]
LABELS = ["Скорость", "Надёжность", "Безопасность", "Экономия", "Прозрачность", "Гибкость", "Поддержка", "Масштаб"]
NUMBERS = ["42 %", "1,8 млн", "×3", "99,9 %", "27", "12 дней", "+58 %", "4,7"]
CHART = ChartSpec(
    kind="column", title="Динамика активных пользователей, тыс.",
    categories=["Q1", "Q2", "Q3", "Q4"], series={"2025": [120, 150, 170, 210], "2026": [180, 220, 260, 320]},
)
TABLE = TableSpec(
    header=["Показатель", "Было", "Стало", "Изменение"],
    rows=[["Время на отчёт", "6 ч", "40 мин", "−89 %"], ["Ошибок в месяц", "31", "4", "−87 %"],
          ["Активных команд", "12", "48", "×4"]],
)


def load_exemplars(pptx: Path) -> list[Exemplar]:
    js = ROOT / "out" / "archetypes" / f"{pptx.stem}.json"
    if js.exists():
        data = json.loads(js.read_text("utf-8"))
        return [
            Exemplar(
                id=f"slide{d['index'] + 1}", source_index=d["index"], layout_name=d["layout"],
                archetype=Archetype(d["archetype"]), slots=[Slot(**s) for s in d["slots"]], fixed=d["fixed"],
                tags=d.get("tags", []), confidence=d.get("confidence", 1.0),
            )
            for d in data
        ]
    return [p.to_exemplar() for p in classify_template(pptx)]


def pick(exemplars: list[Exemplar], archetypes: set[str] | None, all_: bool) -> list[Exemplar]:
    if all_:
        return [e for e in exemplars if not archetypes or e.archetype.value in archetypes]
    best: dict[Archetype, Exemplar] = {}
    for e in exemplars:
        if e.archetype == Archetype.FREEFORM or (archetypes and e.archetype.value not in archetypes):
            continue
        if e.archetype not in best or e.confidence > best[e.archetype].confidence:
            best[e.archetype] = e
    return sorted(best.values(), key=lambda e: e.source_index)


def _fit(text: str, max_chars: int | None) -> str:
    if max_chars and len(text) > max_chars:
        cut = text[: max(3, max_chars)].rsplit(" ", 1)[0]
        return cut or text[:max_chars]
    return text


def _para(text: str, bullet: bool = False) -> Paragraph:
    return Paragraph(runs=[TextRun(text=text)], bullet=bullet)


def make_image(path: Path, size=(800, 600)) -> Path:
    """Градиент с кругом — чтобы был виден center-crop и масштаб."""
    if path.exists():
        return path
    w, h = size
    img = Image.new("RGB", size)
    px = img.load()
    for y in range(h):
        for x in range(w):
            px[x, y] = (int(0 + 0 * x / w), int(80 + 100 * x / w), int(200 + 55 * y / h))
    from PIL import ImageDraw

    d = ImageDraw.Draw(img)
    d.ellipse((w * 0.3, h * 0.2, w * 0.7, h * 0.8), fill=(255, 255, 255))
    d.rectangle((0, 0, w - 1, h - 1), outline=(255, 0, 83), width=12)
    path.parent.mkdir(parents=True, exist_ok=True)
    img.save(path)
    return path


def build_slide(idx: int, e: Exemplar, image: Path, overrides: dict) -> SlideIR:
    elements: list[Element] = []
    counters: dict[SlotKind, int] = {}
    for s in e.slots:
        n = counters.get(s.kind, 0)
        counters[s.kind] = n + 1
        kw = dict(slot_id=s.id, kind=s.kind, box=s.box, style_overrides=dict(overrides))
        if s.kind == SlotKind.TITLE:
            el = Element(**kw, paragraphs=[_para(_fit(f"Архетип {e.archetype.value} — образец {e.id}", s.max_chars))])
        elif s.kind == SlotKind.SUBTITLE:
            el = Element(**kw, paragraphs=[_para(_fit("Подзаголовок: что мы предлагаем и зачем", s.max_chars))])
        elif s.kind == SlotKind.BODY:
            k = max(1, min(s.max_items or 3, 4))
            items = [FILLER[(n * 3 + i) % len(FILLER)] for i in range(k)]
            el = Element(**kw, paragraphs=[_para(_fit(t, (s.max_chars or 400) // k), bullet=k > 1) for t in items])
        elif s.kind == SlotKind.LABEL:
            el = Element(**kw, paragraphs=[_para(_fit(LABELS[n % len(LABELS)], s.max_chars))])
        elif s.kind == SlotKind.CAPTION:
            el = Element(**kw, paragraphs=[_para(_fit(f"Подпись {n + 1}: краткое пояснение", s.max_chars))])
        elif s.kind == SlotKind.NUMBER:
            el = Element(**kw, paragraphs=[_para(_fit(NUMBERS[n % len(NUMBERS)], s.max_chars))])
        elif s.kind in (SlotKind.PICTURE, SlotKind.ICON):
            el = Element(**kw, image_path=str(image))
        elif s.kind == SlotKind.CHART:
            el = Element(**kw, chart=CHART)
        elif s.kind == SlotKind.TABLE:
            el = Element(**kw, table=TABLE)
        else:
            continue  # slide_number / footer / date / other — оставляем как в образце
        elements.append(el)
    return SlideIR(idx=idx, exemplar_id=e.id, archetype=e.archetype, elements=elements, outline_ref=idx,
                   notes=f"smoke: образец {e.id}, архетип {e.archetype.value}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("pptx", type=Path)
    ap.add_argument("--all", action="store_true", help="все образцы по разу, а не один на архетип")
    ap.add_argument("--archetypes", help="через запятую: title,cards,kpi")
    ap.add_argument("--no-render", action="store_true")
    ap.add_argument("--dpi", type=int, default=72)
    a = ap.parse_args()

    exemplars = load_exemplars(a.pptx)
    chosen = pick(exemplars, set(a.archetypes.split(",")) if a.archetypes else None, a.all)
    tokens = extract_tokens(a.pptx)
    accent = (tokens.palette("accent") or ["0077FF"])[0]
    palette = ",".join(tokens.palette("accent") + tokens.palette("secondary"))
    overrides = {"accent": accent, "palette": palette, "font": tokens.fonts[0] if tokens.fonts else "Arial"}
    image = make_image(ROOT / "out" / "smoke" / "assets" / "gradient_4x3.png")

    ir = DeckIR(
        template_id=tokens.template_id, strategy="smoke", slide_w=tokens.slide_w, slide_h=tokens.slide_h,
        slides=[build_slide(i, e, image, overrides) for i, e in enumerate(chosen)],
    )
    out = ROOT / "out" / "smoke" / f"{a.pptx.stem}.pptx"
    render_pptx(ir, a.pptx, exemplars, out)
    print(f"{len(chosen)} слайдов → {out}")
    for s in ir.slides:
        print(f"  {s.idx + 1:2d}. {s.archetype.value:<11} {s.exemplar_id:<8} элементов: {len(s.elements)}")
    if not a.no_render:
        out_dir = ROOT / "out" / "render" / f"{a.pptx.stem}_smoke"
        pngs = render(out, out_dir, dpi=a.dpi, contact=True)
        print(f"рендер: {len(pngs)} PNG → {out_dir}\\contact.png")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
