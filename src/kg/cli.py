from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated, Any

import typer

from kg.config import ManifestError, load_manifest
from kg.db import Database
from kg.ingest import IngestService
from kg.models.manifest import CorpusManifest
from kg.retrieval.service import RecordNotFoundError, RetrievalService, SearchQueryError

app = typer.Typer(no_args_is_help=True, help="Evidence-backed local knowledge retrieval.")
ManifestOption = Annotated[
    Path,
    typer.Option(exists=True, dir_okay=False, readable=True),
]
FormatOption = Annotated[str, typer.Option("--format")]


@app.command()
def ingest(
    manifest: ManifestOption,
    output_format: FormatOption = "text",
) -> None:
    """Ingest configured Markdown sources."""
    corpus = _load_manifest_or_exit(manifest)
    result = IngestService(Database(corpus.database)).ingest(corpus)
    _render(result.model_dump(mode="json"), output_format)
    if result.failed:
        raise typer.Exit(code=1)


@app.command()
def search(
    query: str,
    manifest: ManifestOption,
    subject: Annotated[str | None, typer.Option()] = None,
    output_format: FormatOption = "text",
    limit: Annotated[int, typer.Option(min=1, max=100)] = 20,
) -> None:
    """Search indexed evidence using SQLite FTS5."""
    corpus = _load_manifest_or_exit(manifest)
    try:
        results = RetrievalService(Database(corpus.database), corpus.corpus_id).search(
            query,
            subject,
            limit,
        )
    except SearchQueryError as exc:
        raise typer.BadParameter(str(exc), param_hint="query") from exc
    _render([result.model_dump(mode="json") for result in results], output_format)


@app.command()
def actions(
    subject: str,
    manifest: ManifestOption,
    status: Annotated[str | None, typer.Option()] = None,
    output_format: FormatOption = "text",
) -> None:
    """Return explicit task items associated with a subject."""
    if status not in {None, "open", "completed"}:
        raise typer.BadParameter("status must be 'open' or 'completed'")
    corpus = _load_manifest_or_exit(manifest)
    results = RetrievalService(Database(corpus.database), corpus.corpus_id).actions(
        subject,
        status,
    )
    _render([result.model_dump(mode="json") for result in results], output_format)


@app.command()
def evidence(
    record_id: str,
    manifest: ManifestOption,
    output_format: FormatOption = "text",
) -> None:
    """Return the exact source anchor supporting a record."""
    corpus = _load_manifest_or_exit(manifest)
    try:
        result = RetrievalService(Database(corpus.database), corpus.corpus_id).evidence(
            record_id
        )
    except RecordNotFoundError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=2) from exc
    _render(result.model_dump(mode="json"), output_format)


@app.command()
def status(
    subject: str,
    manifest: ManifestOption,
    since: Annotated[str, typer.Option(help="Window for dated evidence filtering.")] = "30d",
    output_format: FormatOption = "text",
) -> None:
    """Return an evidence-backed subject summary."""
    corpus = _load_manifest_or_exit(manifest)
    try:
        cutoff = _parse_since(since)
        result = RetrievalService(Database(corpus.database), corpus.corpus_id).status(
            subject,
            cutoff,
        )
    except (SearchQueryError, ValueError) as exc:
        raise typer.BadParameter(str(exc), param_hint="since") from exc
    _render(result.model_dump(mode="json"), output_format)


def _load_manifest_or_exit(path: Path) -> CorpusManifest:
    try:
        return load_manifest(path)
    except (ManifestError, ValueError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=2) from exc


def _render(value: Any, output_format: str) -> None:
    if output_format == "json":
        typer.echo(json.dumps(value, indent=2, sort_keys=True))
        return
    if output_format != "text":
        raise typer.BadParameter("format must be 'text' or 'json'")
    if isinstance(value, list):
        if not value:
            typer.echo("No results.")
            return
        for item in value:
            typer.echo(
                f"{item.get('record_type', 'record')}: "
                f"{item.get('summary') or item.get('quote')}"
            )
            typer.echo(f"  {item.get('source_path')} :: {' / '.join(item.get('heading_path', []))}")
        return
    typer.echo(json.dumps(value, indent=2, sort_keys=True))


def _parse_since(value: str) -> datetime:
    match = re.fullmatch(r"([1-9][0-9]*)([dhw])", value)
    if not match:
        raise ValueError("since must use a positive duration such as 12h, 30d, or 4w")
    quantity = int(match.group(1))
    unit = match.group(2)
    duration = {
        "h": timedelta(hours=quantity),
        "d": timedelta(days=quantity),
        "w": timedelta(weeks=quantity),
    }[unit]
    return datetime.now(UTC) - duration
