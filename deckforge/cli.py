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
    """Подкоманды: run (позже — parse, audit, export)."""  # callback нужен, чтобы typer не схлопнул единственную команду


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


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    app()


if __name__ == "__main__":
    main()
