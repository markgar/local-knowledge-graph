from __future__ import annotations

import json
import logging
import re
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Any, Never

import typer

from kg.config import ManifestError, load_manifest
from kg.db import Database
from kg.ingest import IngestService
from kg.models.contracts import ErrorResult
from kg.models.manifest import CorpusManifest
from kg.retrieval.dense import DenseIndexError, DenseRetrievalService
from kg.retrieval.hybrid import HybridRetrievalService
from kg.retrieval.rerank import RerankedRetrievalService, RerankerError
from kg.retrieval.service import RecordNotFoundError, RetrievalService, SearchQueryError

app = typer.Typer(no_args_is_help=True, help="Evidence-backed local knowledge retrieval.")
ManifestOption = Annotated[
    Path,
    typer.Option(dir_okay=False),
]


class OutputFormat(StrEnum):
    text = "text"
    json = "json"


class QueryMode(StrEnum):
    strict = "strict"
    natural = "natural"
    dense = "dense"
    hybrid = "hybrid"
    reranked = "reranked"


FormatOption = Annotated[OutputFormat, typer.Option("--format")]


@app.callback()
def main(
    verbose: Annotated[
        bool,
        typer.Option("--verbose", "-v", help="Enable diagnostic logging."),
    ] = False,
) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.ERROR,
        format="%(levelname)s %(name)s: %(message)s",
    )


@app.command()
def ingest(
    manifest: ManifestOption,
    output_format: FormatOption = OutputFormat.text,
) -> None:
    """Ingest configured Markdown sources."""
    corpus = _load_manifest_or_exit(manifest, output_format)
    result = IngestService(Database(corpus.database)).ingest(corpus)
    _render(result.model_dump(mode="json"), output_format)
    if result.failed:
        raise typer.Exit(code=1)


@app.command("dense-index")
def dense_index(
    manifest: ManifestOption,
    output_format: FormatOption = OutputFormat.text,
    batch_size: Annotated[int, typer.Option(min=1)] = 32,
) -> None:
    """Build the versioned dense projection for a corpus."""
    corpus = _load_manifest_or_exit(manifest, output_format)
    try:
        result = DenseRetrievalService(
            Database(corpus.database),
            corpus.corpus_id,
        ).build_index(batch_size=batch_size)
    except (DenseIndexError, ValueError) as exc:
        _fail("dense_index_failed", str(exc), output_format)
    _render(result.model_dump(mode="json"), output_format)


@app.command()
def search(
    query: str,
    manifest: ManifestOption,
    subject: Annotated[str | None, typer.Option()] = None,
    output_format: FormatOption = OutputFormat.text,
    limit: Annotated[int, typer.Option(min=1, max=100)] = 20,
    since: Annotated[str | None, typer.Option()] = None,
    source: Annotated[str | None, typer.Option()] = None,
    query_mode: Annotated[
        QueryMode,
        typer.Option(
            help=(
                "Strict, natural BM25, dense semantic, hybrid RRF, "
                "or cross-encoder reranked search."
            )
        ),
    ] = QueryMode.strict,
) -> None:
    """Search indexed evidence."""
    corpus = _load_manifest_or_exit(manifest, output_format)
    try:
        cutoff = _parse_since(since) if since else None
        database = Database(corpus.database)
        if query_mode is QueryMode.reranked:
            results = RerankedRetrievalService(database, corpus.corpus_id).search(
                query=query,
                subject=subject,
                limit=limit,
                since=cutoff,
                source_path=source,
            )
        elif query_mode is QueryMode.hybrid:
            results = HybridRetrievalService(database, corpus.corpus_id).search(
                query=query,
                subject=subject,
                limit=limit,
                since=cutoff,
                source_path=source,
            )
        elif query_mode is QueryMode.dense:
            results = DenseRetrievalService(database, corpus.corpus_id).search(
                query=query,
                subject=subject,
                limit=limit,
                since=cutoff,
                source_path=source,
            )
        else:
            results = RetrievalService(database, corpus.corpus_id).search(
                query=query,
                subject=subject,
                limit=limit,
                since=cutoff,
                source_path=source,
                query_mode=(
                    "natural" if query_mode is QueryMode.natural else "strict"
                ),
            )
    except DenseIndexError as exc:
        _fail("dense_index_unavailable", str(exc), output_format)
    except RerankerError as exc:
        _fail("reranker_unavailable", str(exc), output_format)
    except (SearchQueryError, ValueError) as exc:
        _fail("invalid_query", str(exc), output_format)
    _render([result.model_dump(mode="json") for result in results], output_format)


@app.command()
def actions(
    subject: str,
    manifest: ManifestOption,
    status: Annotated[str | None, typer.Option()] = None,
    output_format: FormatOption = OutputFormat.text,
    since: Annotated[str | None, typer.Option()] = None,
    source: Annotated[str | None, typer.Option()] = None,
) -> None:
    """Return explicit task items associated with a subject."""
    if status not in {None, "open", "completed"}:
        _fail(
            "invalid_status",
            "status must be 'open' or 'completed'",
            output_format,
        )
    corpus = _load_manifest_or_exit(manifest, output_format)
    try:
        results = RetrievalService(Database(corpus.database), corpus.corpus_id).actions(
            subject=subject,
            status=status,
            since=_parse_since(since) if since else None,
            source_path=source,
        )
    except ValueError as exc:
        _fail("invalid_since", str(exc), output_format)
    _render([result.model_dump(mode="json") for result in results], output_format)


@app.command()
def evidence(
    record_id: str,
    manifest: ManifestOption,
    output_format: FormatOption = OutputFormat.text,
) -> None:
    """Return the exact source anchor supporting a record."""
    corpus = _load_manifest_or_exit(manifest, output_format)
    try:
        result = RetrievalService(Database(corpus.database), corpus.corpus_id).evidence(
            record_id
        )
    except RecordNotFoundError as exc:
        _fail("record_not_found", str(exc), output_format)
    _render(result.model_dump(mode="json"), output_format)


@app.command()
def status(
    subject: str,
    manifest: ManifestOption,
    since: Annotated[
        str | None,
        typer.Option(help="Optional window for dated evidence filtering."),
    ] = None,
    output_format: FormatOption = OutputFormat.text,
) -> None:
    """Return an evidence-backed subject summary."""
    corpus = _load_manifest_or_exit(manifest, output_format)
    try:
        cutoff = _parse_since(since) if since else None
        result = RetrievalService(Database(corpus.database), corpus.corpus_id).status(
            subject,
            cutoff,
        )
    except (SearchQueryError, ValueError) as exc:
        _fail("invalid_since", str(exc), output_format)
    _render(result.model_dump(mode="json"), output_format)


def _load_manifest_or_exit(
    path: Path,
    output_format: OutputFormat,
) -> CorpusManifest:
    try:
        return load_manifest(path)
    except (ManifestError, ValueError) as exc:
        _fail("invalid_manifest", str(exc), output_format)


def _render(value: Any, output_format: OutputFormat) -> None:
    if output_format is OutputFormat.json:
        typer.echo(json.dumps(value, indent=2, sort_keys=True))
        return
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


def _fail(
    error: str,
    message: str,
    output_format: OutputFormat,
    code: int = 2,
) -> Never:
    if output_format is OutputFormat.json:
        typer.echo(
            ErrorResult(error=error, message=message).model_dump_json(),
            err=True,
        )
    else:
        typer.echo(f"Error: {message}", err=True)
    raise typer.Exit(code=code)


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
