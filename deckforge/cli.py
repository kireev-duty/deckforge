"""CLI: parse / run / audit / export."""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Optional

import typer

from deckforge.core.ir import DeckOutline
from deckforge.llm import load_dotenv
from deckforge.pipeline import load_config, run

app = typer.Typer(help="deckforge — цифровой дизайнер презентаций", no_args_is_help=True)


@app.callback()
def _root() -> None:
    """Подкоманды: parse, run, audit, export."""


@app.command("parse")
def parse_cmd(
    template: Path = typer.Argument(..., help="шаблон .pptx"),
    json_out: Optional[Path] = typer.Option(None, "--json", help="сохранить полную TemplateDNA в JSON"),
) -> None:
    """Разбор шаблона: палитра с ролями, шрифты, типографическая шкала, сетка, образцы по архетипам."""
    from deckforge.pipeline import parse_template

    parsed = parse_template(template)
    s = parsed.summary()
    typer.echo(f"{s['template_id']}  ({s['slide_w']}×{s['slide_h']} EMU, {s['slides']} слайдов-образцов, "
               f"{s['seconds']:.1f}s)")
    typer.echo(f"шрифты: {', '.join(s['fonts'][:4]) or '—'}" + (f"  (встроены: {', '.join(s['embedded_fonts'])})"
                                                                if s["embedded_fonts"] else ""))
    for role, hexes in s["palette"].items():
        typer.echo(f"  {role:<11} " + " ".join(f"#{h}" for h in hexes[:6]))
    typer.echo("шкала: " + ", ".join(f"{x['role']} {x['size_pt']:g}pt{' b' if x['bold'] else ''}" for x in s["typography"]))
    g = s["grid"]
    typer.echo(f"поля (EMU): {g['margin_left']} / {g['margin_right']} / {g['margin_top']} / {g['margin_bottom']}, "
               f"колонок {g['columns']}, строк {g['rows']}, фиксированных элементов {s['fixed_elements']}")
    typer.echo("\n| архетип | образцов |\n|---|---|")
    for name, n in s["archetypes"].items():
        typer.echo(f"| {name} | {n} |")
    if json_out:
        json_out.write_text(parsed.dna.model_dump_json(indent=1), "utf-8")
        typer.echo(f"\nTemplateDNA → {json_out}")


@app.command("run")
def run_cmd(
    config: Path = typer.Option(..., "--config", "-c", help="YAML-конфиг прогона (см. configs/run.example.yaml)"),
    outline: Optional[Path] = typer.Option(None, "--outline", help="готовый outline.json — шаг content и LLM пропускаются"),
    output_dir: Optional[Path] = typer.Option(None, "--output-dir", "-o", help="переопределить output_dir из конфига"),
    render_png: bool = typer.Option(False, "--png", help="PNG-превью и contact.png для каждой колоды"),
    no_fix: bool = typer.Option(False, "--no-fix", help="не применять автофиксы (audit.autofix: false)"),
    no_judge: bool = typer.Option(False, "--no-judge", help="без VLM-судьи (audit.contextual: false) — быстрее и без API"),
    no_images: bool = typer.Option(False, "--no-images", help="без иллюстраций (images: off)"),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Прогон по конфигу: outline → колоды по стратегиям → аудит и автофиксы → экспорт."""
    logging.basicConfig(level=logging.INFO if verbose else logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    load_dotenv()
    cfg = load_config(config)
    if output_dir is not None:
        cfg = cfg.model_copy(update={"output_dir": output_dir.resolve()})
    if render_png:
        cfg = cfg.model_copy(update={"render_png": True})
    if no_fix or no_judge:
        audit = cfg.audit.model_copy(update={**({"autofix": False} if no_fix else {}),
                                             **({"contextual": False} if no_judge else {})})
        cfg = cfg.model_copy(update={"audit": audit})
    if no_images:
        cfg = cfg.model_copy(update={"images": "off"})
    ready = DeckOutline.model_validate_json(outline.read_text("utf-8")) if outline else None
    result = run(cfg, outline=ready, progress=lambda m: typer.echo(f"  {m}"))
    for w in result.warnings:
        typer.echo(f"  ! {w}")
    typer.echo(f"\n{(result.output_dir / 'compare.md').read_text('utf-8')}")


@app.command("audit")
def audit_cmd(
    deck: Path = typer.Argument(..., help="колода .pptx"),
    template: Path = typer.Option(..., "--template", "-t", help="шаблон .pptx, по которому собрана колода"),
    ir: Optional[Path] = typer.Option(None, "--ir", help="<strategy>.ir.json — точнее проверки шаблонности (T02/T03/T05)"),
    checks: Optional[str] = typer.Option(None, "--checks", help="какие проверки: L03,T06 (по умолчанию все)"),
    contextual: bool = typer.Option(False, "--contextual", help="плюс VLM-судья по PNG (нужны LibreOffice и API)"),
    png_dir: Optional[Path] = typer.Option(None, "--png-dir", help="готовые PNG слайдов (иначе рендерятся в out/render)"),
    outline: Optional[Path] = typer.Option(None, "--outline", help="outline.json — факты для судьи по sources"),
    content_pack: Optional[Path] = typer.Option(None, "--content-pack", help="папка контент-пакета (факты для C04)"),
    fix_plan: bool = typer.Option(False, "--fix-plan", help="показать, какие находки чинятся автофиксом и как"),
    json_out: Optional[Path] = typer.Option(None, "--json", help="сохранить AuditReport в JSON"),
    limit: int = typer.Option(80, "--limit", help="сколько строк показать"),
) -> None:
    """Аудит колоды: детерминированные проверки (+ VLM-судья); колода не меняется."""
    from deckforge.audit import audit_deck, report_markdown
    from deckforge.core.autofix import fix_plan_rows
    from deckforge.core.ir import DeckIR
    from deckforge.parsing.dna import build_dna

    dna = build_dna(template)
    deck_ir = DeckIR.model_validate_json(ir.read_text("utf-8")) if ir else None
    report = audit_deck(deck, dna, deck_ir, checks=checks.split(",") if checks else None)
    if contextual:
        report = _judge(report, deck, dna, deck_ir, png_dir, outline, content_pack)
    if json_out:
        json_out.write_text(report.model_dump_json(indent=1), "utf-8")
    typer.echo(report_markdown(report, max_rows=limit))
    if fix_plan:
        rows = fix_plan_rows(report)
        typer.echo("\n| # | слайд | проверка | фикс | как | что сделает |\n|---|---|---|---|---|---|")
        for r in rows[:limit]:
            typer.echo(f"| {r['n']} | {r['slide_idx'] + 1} | {r['check_id']} | {r['fix']} | {r['how']} | {r['description']} |")
        typer.echo(f"\nsafe — применится в `run` автоматически; ir — по выбору пользователя; replan — только предложение; "
                   f"template — дизайн шаблона, не чиним. Всего {len(rows)} из {len(report.findings)} находок с фиксом.")
    raise typer.Exit(code=1 if report.errors else 0)


@app.command("export")
def export_cmd(
    deck: Path = typer.Argument(..., help="колода .pptx (своя или чужая)"),
    html: Optional[Path] = typer.Option(None, "--html", help="куда писать .html (по умолчанию рядом с .pptx)"),
    pdf: bool = typer.Option(False, "--pdf", help="плюс .pdf через LibreOffice"),
    png: bool = typer.Option(False, "--png", help="плюс PNG по слайдам и contact.png (LibreOffice) в out/render/<stem>"),
    ir: Optional[Path] = typer.Option(None, "--ir", help="<strategy>.ir.json — заметки к слайдам в HTML"),
    title: Optional[str] = typer.Option(None, "--title", help="заголовок HTML-страницы"),
) -> None:
    """Экспорт готовой колоды: .html, опционально .pdf и PNG."""
    import shutil

    from deckforge.core.ir import DeckIR
    from deckforge.export import export_html, pptx_to_pdf, render

    deck_ir = DeckIR.model_validate_json(ir.read_text("utf-8")) if ir else None
    out = export_html(deck, html or deck.with_suffix(".html"), ir=deck_ir, title=title)
    typer.echo(f"html → {out} ({out.stat().st_size / 1e6:.1f} МБ)")
    if png:
        pngs = render(deck, Path("out/render") / deck.stem, contact=True)
        typer.echo(f"png → {pngs[0].parent} ({len(pngs)} слайдов)")
    if pdf:
        tmp = pptx_to_pdf(deck, deck.parent / "_pdf")
        shutil.move(str(tmp), deck.with_suffix(".pdf"))
        shutil.rmtree(deck.parent / "_pdf", ignore_errors=True)
        typer.echo(f"pdf → {deck.with_suffix('.pdf')}")


def _judge(report, deck: Path, dna, deck_ir, png_dir: Optional[Path], outline: Optional[Path], content_pack: Optional[Path]):
    """VLM-судья для CLI: PNG (готовые или рендер), текст из IR или из самого pptx."""
    import time

    from deckforge.audit import with_contextual
    from deckforge.audit.contextual import CHECK_IDS, judge_deck, slides_from_context, slides_from_ir
    from deckforge.llm.client import LLMClient

    load_dotenv()
    if png_dir is not None:
        pngs = sorted(p for p in png_dir.glob("*.png") if p.name != "contact.png")
    else:
        from deckforge.export.render import render

        pngs = render(deck, Path("out/render") / deck.stem)
    ready = DeckOutline.model_validate_json(outline.read_text("utf-8")) if outline else None
    if deck_ir is not None:
        from deckforge.content import load_content_pack

        pack = load_content_pack(content_pack) if content_pack else None
        slides = slides_from_ir(deck_ir, ready, pack)
    else:
        from deckforge.audit.context import AuditContext

        slides = slides_from_context(AuditContext(deck, dna))
    t0 = time.perf_counter()
    found = judge_deck(pngs, slides, LLMClient(), language=ready.language if ready else "ru")
    return with_contextual(report, found, CHECK_IDS, time.perf_counter() - t0)


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    app()


if __name__ == "__main__":
    main()
