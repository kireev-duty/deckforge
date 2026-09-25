"""Заготовки SmartArt «Простой процесс» (process1) из настоящего PowerPoint (Windows, COM через pywin32).

Запускается один раз, результат лежит в репо — рендер работает без PowerPoint (Linux, Docker, Streamlit Cloud):
- `deckforge/render/assets/smartart/` — определение макета, быстрый стиль и цвета, как их пишет сам PowerPoint
  (эти части обязаны быть в пакете рядом с данными SmartArt);
- `tests/fixtures/smartart_process1.json` — геометрия, которую PowerPoint строит для 2…6 узлов в боксах разной
  формы: тест сверяет с ней формулу `render/smartart.layout_steps`.

    .venv\\Scripts\\python.exe tools\\make_smartart_assets.py
"""

from __future__ import annotations

import json
import re
import sys
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "deckforge" / "render" / "assets" / "smartart"
FIXTURE = ROOT / "tests" / "fixtures" / "smartart_process1.json"
URN = "urn:microsoft.com/office/officeart/2005/8/layout/process1"
PARTS = {"layout": "process1.layout.xml", "quickStyle": "simple1.quickStyle.xml", "colors": "accent1_2.colors.xml"}
EMU_PER_PT = 12700
# боксы (pt): широкий — ограничивает ширина, низкий — высота
BOXES = {"wide": (864, 300), "short": (864, 70)}


def _smartart(app, pres, n: int, w: float, h: float, path: Path) -> None:
    for s in list(pres.Slides):
        s.Delete()
    slide = pres.Slides.Add(1, 12)  # ppLayoutBlank
    shape = slide.Shapes.AddSmartArt(app.SmartArtLayouts(URN), 48, 120, w, h)
    nodes = shape.SmartArt.AllNodes
    while nodes.Count < n:
        nodes.Add()
    while nodes.Count > n:
        nodes(nodes.Count).Delete()
    for i in range(1, n + 1):
        nodes(i).TextFrame2.TextRange.Text = f"Шаг {i}"
    pres.SaveCopyAs(str(path))


def _geometry(pptx: Path) -> list[dict]:
    """Фигуры drawing-части: prstGeom и бокс в EMU относительно рамки SmartArt."""
    with zipfile.ZipFile(pptx) as z:
        drawing = next(n for n in z.namelist() if re.fullmatch(r"ppt/diagrams/drawing\d+\.xml", n))
        xml = z.read(drawing).decode("utf-8")
    out = []
    for sp in re.findall(r"<dsp:sp .*?</dsp:sp>", xml, re.S):
        geom = re.search(r'prst="(\w+)"', sp).group(1)
        off = re.search(r'<a:off x="(-?\d+)" y="(-?\d+)"/><a:ext cx="(\d+)" cy="(\d+)"/>', sp)
        out.append({"geom": geom, "box": [int(v) for v in off.groups()]})
    return out


def main() -> None:
    import win32com.client as w

    sys.stdout.reconfigure(encoding="utf-8")
    app = w.Dispatch("PowerPoint.Application")
    pres = app.Presentations.Add(True)
    fixture: dict[str, dict] = {}
    try:
        with tempfile.TemporaryDirectory() as tmp:
            for name, (bw, bh) in BOXES.items():
                for n in range(2, 7):
                    path = Path(tmp) / f"{name}_{n}.pptx"
                    _smartart(app, pres, n, bw, bh, path)
                    fixture[f"{name}_{n}"] = {"box": [round(bw * EMU_PER_PT), round(bh * EMU_PER_PT)], "n": n,
                                              "shapes": _geometry(path)}
                    if name == "wide" and n == 3:
                        ASSETS.mkdir(parents=True, exist_ok=True)
                        with zipfile.ZipFile(path) as z:
                            for kind, fname in PARTS.items():
                                part = next(p for p in z.namelist() if re.fullmatch(rf"ppt/diagrams/{kind}\d+\.xml", p))
                                (ASSETS / fname).write_bytes(z.read(part))
                                print(f"{part} → {ASSETS / fname}")
    finally:
        pres.Saved = True
        pres.Close()
    FIXTURE.write_text(json.dumps(fixture, ensure_ascii=False, indent=1), "utf-8")
    print(f"геометрия PowerPoint ({len(fixture)} случаев) → {FIXTURE}")


if __name__ == "__main__":
    main()
