"""Статическая галерея готовых колод `examples/output` для GitHub Pages: шаблон × стратегия, без LLM и LibreOffice.

    python tools/build_gallery.py [--out out/gallery] [--demo-url URL] [--ref master]

В галерею копируются самодостаточные .html и .pdf колод; превью — первая страница PDF (PyMuPDF);
.pptx тяжёлые (≈190 МБ на все колоды) — ссылка на файл в репозитории (LFS отдаёт его как raw).
Публикует `.github/workflows/pages.yml`.
"""

from __future__ import annotations

import argparse
import html
import json
import shutil
from pathlib import Path

import pymupdf

ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = ROOT / "examples" / "output"
REPO = "kireev-duty/deckforge"
# папки examples/output, которые попадают в галерею, их порядок и подписи; остальные (сценарий жюри) — только в репозитории
TEMPLATES = {
    "vk_tech": "VK Tech",
    "vk_education": "VK Education",
    "vk_workspace": "VK WorkSpace",
}
THUMB_DPI = 40


def deck_cards(run_dir: Path, out: Path, ref: str) -> list[dict]:
    run = json.loads((run_dir / "run.json").read_text("utf-8"))
    cards = []
    for d in run["decks"]:
        name = d["strategy"]
        manifest = json.loads((run_dir / d["manifest"]).read_text("utf-8"))
        dest = out / run_dir.name
        dest.mkdir(parents=True, exist_ok=True)
        card = {"strategy": name, "slides": d["stats"]["slides"],
                "hint": (manifest.get("strategy") or {}).get("audience_hint", ""),
                "errors": (d.get("audit") or {}).get("errors"), "warnings": (d.get("audit") or {}).get("warnings"),
                "seconds": (manifest.get("timings_s") or {}).get("deck_total"),
                "pptx": f"https://github.com/{REPO}/raw/{ref}/examples/output/{run_dir.name}/{name}.pptx",
                "manifest": f"https://github.com/{REPO}/blob/{ref}/examples/output/{run_dir.name}/{d['manifest']}"}
        for ext in ("html", "pdf"):
            src = run_dir / f"{name}.{ext}"
            if src.exists() and src.stat().st_size > 1000:  # LFS-указатель не копируем
                shutil.copy2(src, dest / src.name)
                card[ext] = f"{run_dir.name}/{src.name}"
        if "pdf" in card:
            with pymupdf.open(dest / f"{name}.pdf") as doc:
                thumb = dest / f"{name}.png"
                doc[0].get_pixmap(dpi=THUMB_DPI).save(thumb)
                card["thumb"] = f"{run_dir.name}/{thumb.name}"
        cards.append(card)
    return cards


def render_page(sections: list[tuple[str, str, list[dict]]], demo_url: str, ref: str) -> str:
    e = html.escape
    blocks = []
    for title, brief_url, cards in sections:
        items = []
        for c in cards:
            thumb = (f'<img src="{e(c["thumb"])}" alt="Первый слайд: {e(title)}, {e(c["strategy"])}" loading="lazy">'
                     if "thumb" in c else '<div class="nothumb">нет превью</div>')
            link = c.get("html") or c.get("pdf") or c["pptx"]
            meta = f'{c["slides"]} слайдов'
            if c["seconds"]:
                meta += f' · {c["seconds"]:.0f} с'
            if c["errors"] is not None:
                meta += f' · аудит {c["errors"]}/{c["warnings"]}'
            links = " ".join(
                f'<a href="{e(c[k])}">{label}</a>' for k, label in
                (("html", "HTML"), ("pdf", "PDF"), ("pptx", "PPTX"), ("manifest", "manifest")) if k in c)
            items.append(f'''<article class="card">
  <a class="thumb" href="{e(link)}">{thumb}</a>
  <div class="body">
    <h3>{e(c["strategy"])}</h3>
    <p class="hint">{e(c["hint"])}</p>
    <p class="meta">{e(meta)}</p>
    <p class="links">{links}</p>
  </div>
</article>''')
        blocks.append(f'<section><h2>{e(title)}</h2><p class="sub"><a href="{e(brief_url)}">сводка вариантов '
                      f'(compare.md)</a></p><div class="grid">{"".join(items)}</div></section>')
    demo = (f'<a class="cta" href="{e(demo_url)}">Попробовать сервис онлайн →</a>' if demo_url else "")
    return f'''<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>deckforge — примеры колод</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;600;700&display=swap" rel="stylesheet">
<style>
:root {{ --bg:#f6f7f9; --panel:#fff; --text:#16181d; --muted:#5b6270; --line:#e2e5ea; --accent:#0a66ff; --accent-text:#fff; }}
@media (prefers-color-scheme: dark) {{ :root:not([data-theme="light"]) {{
  --bg:#111317; --panel:#1a1d23; --text:#eceef2; --muted:#9aa2b1; --line:#2a2f38; --accent:#4d8dff; --accent-text:#0b0d10; }} }}
:root[data-theme="dark"] {{ --bg:#111317; --panel:#1a1d23; --text:#eceef2; --muted:#9aa2b1; --line:#2a2f38;
  --accent:#4d8dff; --accent-text:#0b0d10; }}
* {{ box-sizing:border-box; }}
body {{ margin:0; background:var(--bg); color:var(--text); font:15px/1.5 Inter, system-ui, sans-serif; }}
header, main, footer {{ max-width:1200px; margin:0 auto; padding:0 16px; }}
header {{ padding-top:40px; padding-bottom:8px; }}
h1 {{ font-size:28px; margin:0 0 8px; }}
.lead {{ color:var(--muted); max-width:760px; margin:0 0 20px; overflow-wrap:anywhere; }}
.cta {{ display:inline-block; background:var(--accent); color:var(--accent-text); padding:10px 18px; border-radius:8px;
  font-weight:600; text-decoration:none; margin-right:16px; }}
a {{ color:var(--accent); }}
section {{ margin:36px 0; }}
h2 {{ font-size:20px; margin:0 0 2px; }}
.sub {{ margin:0 0 14px; font-size:13px; }}
.grid {{ display:grid; grid-template-columns:repeat(auto-fill, minmax(min(300px, 100%), 1fr)); gap:16px; }}
.card {{ background:var(--panel); border:1px solid var(--line); border-radius:10px; overflow:hidden; display:flex;
  flex-direction:column; }}
.thumb img, .nothumb {{ display:block; width:100%; aspect-ratio:16/9; object-fit:cover; background:var(--line); }}
.nothumb {{ display:grid; place-items:center; color:var(--muted); }}
.body {{ padding:12px 14px 14px; }}
h3 {{ margin:0 0 4px; font-size:16px; text-transform:capitalize; }}
.hint {{ color:var(--muted); font-size:13px; margin:0 0 8px; }}
.meta {{ font-size:13px; margin:0 0 8px; font-variant-numeric:tabular-nums; }}
.links {{ margin:0; display:flex; gap:12px; flex-wrap:wrap; font-size:14px; }}
footer {{ color:var(--muted); font-size:13px; padding-bottom:40px; }}
</style>
</head>
<body>
<header>
  <h1>deckforge — цифровой дизайнер презентаций</h1>
  <p class="lead">Один контент × 3 шаблона VK × 3 стратегии вёрстки. На входе только .pptx-шаблон: бриф выведен из
  шаблона VK Tech, его outline свёрстан по образцам каждого шаблона. HTML открывается в браузере
  (←/→ — листать, F — режим показа); .pptx — нативные объекты, редактируются в PowerPoint.
  «Аудит» — ошибки/предупреждения после автофиксов.</p>
  <p>{demo}<a href="https://github.com/{REPO}">Код и документация на GitHub</a></p>
</header>
<main>
{"".join(blocks)}
</main>
<footer>Кейс VK Tech, хакатон «Лидеры цифровой трансформации 2026». Собрано из <code>examples/output</code> @ {e(ref)}.</footer>
</body>
</html>
'''


def build(out: Path, demo_url: str = "", ref: str = "master") -> Path:
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    dirs = [EXAMPLES / name for name in TEMPLATES if (EXAMPLES / name / "run.json").exists()]
    sections = [(TEMPLATES[d.name], f"https://github.com/{REPO}/blob/{ref}/examples/output/{d.name}/compare.md",
                 deck_cards(d, out, ref)) for d in dirs]
    (out / "index.html").write_text(render_page(sections, demo_url, ref), "utf-8")
    (out / ".nojekyll").write_text("", "utf-8")
    return out / "index.html"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", type=Path, default=ROOT / "out" / "gallery")
    ap.add_argument("--demo-url", default="", help="ссылка на демо-стенд")
    ap.add_argument("--ref", default="master", help="ветка или тег для ссылок на .pptx и manifest")
    args = ap.parse_args()
    index = build(args.out, args.demo_url, args.ref)
    n = sum(1 for _ in args.out.rglob("*.html")) - 1
    print(f"{index} — {n} колод")


if __name__ == "__main__":
    main()
