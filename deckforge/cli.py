"""CLI: `deckforge run --config configs/run.example.yaml`. Точка входа без бизнес-логики — всё в pipeline/."""

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
    """Подкоманды: run, audit (позже — parse, export)."""


@app.command("run")
def run_cmd(
    config: Path = typer.Option(..., "--config", "-c", help="YAML-конфиг прогона (см. configs/run.example.yaml)"),
    outline: Optional[Path] = typer.Option(None, "--outline", help="готовый outline.json — шаг content и LLM пропускаются"),
    output_dir: Optional[Path] = typer.Option(None, "--output-dir", "-o", help="переопределить output_dir из конфига"),
    render_png: bool = typer.Option(False, "--png", help="PNG-превью и contact.png для каждой колоды"),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Прогон по конфигу: шаблон + контент-пакет → outline → колоды по стратегиям + manifest.json."""
    logging.basicConfig(level=logging.INFO if verbose else logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    load_dotenv()
    cfg = load_config(config)
    if output_dir is not None:
        cfg = cfg.model_copy(update={"output_dir": output_dir.resolve()})
    if render_png:
        cfg = cfg.model_copy(update={"render_png": True})
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
    json_out: Optional[Path] = typer.Option(None, "--json", help="сохранить AuditReport в JSON"),
    limit: int = typer.Option(80, "--limit", help="сколько строк показать"),
) -> None:
    """Детерминированный аудит колоды: таблица находок (слайд, severity, проверка, сообщение, autofix)."""
    from deckforge.audit import audit_deck, report_markdown
    from deckforge.core.ir import DeckIR
    from deckforge.parsing.dna import build_dna

    dna = build_dna(template)
    deck_ir = DeckIR.model_validate_json(ir.read_text("utf-8")) if ir else None
    report = audit_deck(deck, dna, deck_ir, checks=checks.split(",") if checks else None)
    if json_out:
        json_out.write_text(report.model_dump_json(indent=1), "utf-8")
    typer.echo(report_markdown(report, max_rows=limit))
    raise typer.Exit(code=1 if report.errors else 0)


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    app()


if __name__ == "__main__":
    main()
