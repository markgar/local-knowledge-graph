"""Historical Markdown demonstration CLI, retained for internal regression tests."""

from __future__ import annotations

import json
import logging
import re
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Any, Never

import typer
from typer.core import TyperCommand

from kg.config import ManifestError, load_manifest
from kg.db import Database
from kg.ingest import IngestService
from kg.ingest.explain import render_ingest_report
from kg.models.contracts import ErrorResult, IngestReport
from kg.models.manifest import CorpusManifest
from kg.retrieval import SearchExplanationError, SearchService, SearchStateChangedError
from kg.retrieval.dense import (
    DEFAULT_EMBEDDING_PROFILE,
    DenseIndexError,
    DenseRetrievalService,
    EmbeddingProfile,
)
from kg.retrieval.rerank import RerankerError
from kg.retrieval.service import (
    RecordNotFoundError,
    RetrievalService,
    RevisionComparisonError,
    SearchQueryError,
)

app = typer.Typer(no_args_is_help=True, help="Evidence-backed local knowledge retrieval.")
ManifestOption = Annotated[
    Path,
    typer.Option(dir_okay=False),
]


class OutputFormat(StrEnum):
    text = "text"
    json = "json"


class SearchCommand(TyperCommand):
    def parse_args(self, ctx: Any, args: list[str]) -> list[str]:
        # Catch even missing/unknown legacy values without advertising a bypass option.
        options = args[:args.index("--")] if "--" in args else args
        if any(arg.split("=", 1)[0] == "--query-mode" for arg in options):
            output_format = OutputFormat.text
            for index, arg in enumerate(options):
                if arg == "--format" and index + 1 < len(options):
                    if options[index + 1] == "json":
                        output_format = OutputFormat.json
                elif arg == "--format=json":
                    output_format = OutputFormat.json
            _fail(
                "invalid_query",
                "--query-mode has been removed, including --query-mode reranked. "
                "Omit the flag: kg search now always uses keyword + semantic retrieval, "
                "fusion, and reranking. Run kg dense-index with the same "
                "--embedding-profile and --contextual settings before searching.",
                output_format,
            )
        return super().parse_args(ctx, args)


FormatOption = Annotated[OutputFormat, typer.Option("--format")]
ContextualOption = Annotated[
    bool,
    typer.Option(
        "--contextual",
        help="Include titles and headings in semantic retrieval; uses a separate dense index.",
    ),
]
AGENT_INTERFACE_VERSION = "2"


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
def capabilities(
    output_format: FormatOption = OutputFormat.text,
) -> None:
    """Describe the stable JSON CLI operations available to agents."""
    _render(
        {
            "interface_version": AGENT_INTERFACE_VERSION,
            "transport": "local_cli_json",
            "tools": [
                {
                    "name": "evidence_search",
                    "command": "search",
                    "result": "list[SearchResult]",
                    "pipeline": "keyword + semantic -> fusion -> reranking",
                    "rank_semantics": (
                        "Raw cross-encoder score, higher is better; returned order is "
                        "authoritative, not confidence or comparable across queries/models."
                    ),
                    "explanation": {
                        "flag": "--explain",
                        "result": "ProductSearchExplanation",
                        "report_version": "2",
                        "trace_limit": {
                            "flag": "--explain-limit", "default": 50, "min": 1, "max": 200,
                        },
                        "quotes": "Only with --include-quotes",
                    },
                    "readiness": (
                        "ingest -> matching dense-index -> search; embedding and reranker "
                        "required even for empty results; no keyword fallback"
                    ),
                    "migration": "--query-mode is removed; omit it, including reranked.",
                },
                {
                    "name": "source_range_read",
                    "command": "source-range",
                    "result": "SourceRangeResult",
                },
                {
                    "name": "source_context_read",
                    "command": "source-context",
                    "result": "SourceContextResult",
                },
                {
                    "name": "revision_list",
                    "command": "revisions",
                    "result": "list[RevisionResult]",
                },
                {
                    "name": "revision_compare",
                    "command": "compare-revisions",
                    "result": "RevisionComparisonResult",
                },
                {
                    "name": "citation_resolve",
                    "command": "evidence",
                    "result": "EvidenceResult",
                },
                {
                    "name": "subject_status",
                    "command": "status",
                    "result": "StatusResult",
                },
                {
                    "name": "record_state_audit",
                    "command": "record-state",
                    "result": "RecordStateReport",
                },
            ],
        },
        output_format,
    )


@app.command()
def ingest(
    manifest: ManifestOption,
    output_format: FormatOption = OutputFormat.text,
    explain: Annotated[
        bool, typer.Option(help="Explain per-document outcomes and stored evidence.")
    ] = False,
    include_quotes: Annotated[
        bool, typer.Option(help="Include source quotes in explain output (requires --explain).")
    ] = False,
    explain_limit: Annotated[
        int, typer.Option(min=1, max=200, help="Maximum explained records per document.")
    ] = 50,
) -> None:
    """Ingest configured Markdown sources."""
    if include_quotes and not explain:
        _fail("invalid_ingest_options", "--include-quotes requires --explain", output_format)
    corpus = _load_manifest_or_exit(manifest, output_format)
    result = IngestService(Database(corpus.database)).ingest(
        corpus, explain=explain, include_quotes=include_quotes, detail_limit=explain_limit
    )
    if isinstance(result, IngestReport):
        if output_format == OutputFormat.text:
            typer.echo(render_ingest_report(result))
        else:
            _render(result.model_dump(mode="json", exclude_none=True), output_format)
    else:
        _render(result.model_dump(mode="json"), output_format)
    if result.failed:
        raise typer.Exit(code=1)


@app.command("dense-index")
def dense_index(
    manifest: ManifestOption,
    output_format: FormatOption = OutputFormat.text,
    batch_size: Annotated[int, typer.Option(min=1)] = 32,
    embedding_profile: Annotated[
        EmbeddingProfile,
        typer.Option(help="Local single-vector embedding profile."),
    ] = DEFAULT_EMBEDDING_PROFILE,
    contextual: ContextualOption = False,
) -> None:
    """Build the versioned dense projection for a corpus."""
    corpus = _load_manifest_or_exit(manifest, output_format)
    try:
        result = DenseRetrievalService(
            Database(corpus.database),
            corpus.corpus_id,
            profile=embedding_profile,
            contextual=contextual,
        ).build_index(batch_size=batch_size)
    except (DenseIndexError, ValueError) as exc:
        _fail("dense_index_failed", str(exc), output_format)
    _render(result.model_dump(mode="json"), output_format)


@app.command(cls=SearchCommand)
def search(
    ctx: typer.Context,
    query: str,
    manifest: ManifestOption,
    subject: Annotated[str | None, typer.Option()] = None,
    output_format: FormatOption = OutputFormat.text,
    limit: Annotated[int, typer.Option(min=1, max=100)] = 20,
    since: Annotated[str | None, typer.Option()] = None,
    source: Annotated[str | None, typer.Option()] = None,
    embedding_profile: Annotated[
        EmbeddingProfile,
        typer.Option(help="Embedding profile; must match the prepared dense index."),
    ] = DEFAULT_EMBEDDING_PROFILE,
    contextual: ContextualOption = False,
    explain: Annotated[
        bool, typer.Option(help="Explain the full retrieval pipeline, filters, and subject scope.")
    ] = False,
    include_quotes: Annotated[
        bool, typer.Option(help="Include source quotes in explain output (requires --explain).")
    ] = False,
    explain_limit: Annotated[
        int,
        typer.Option(min=1, max=200, help="Maximum traced candidates (requires --explain)."),
    ] = 50,
) -> None:
    """Search evidence with keyword + semantic retrieval, fusion, and reranking.

    Prepare a matching dense-index first. No keyword-only fallback is used.
    Migration: --query-mode is removed; omit it (including reranked).
    """
    if include_quotes and not explain:
        _fail("invalid_query", "--include-quotes requires --explain", output_format)
    limit_source = ctx.get_parameter_source("explain_limit")
    if not explain and limit_source is not None and limit_source.name == "COMMANDLINE":
        _fail(
            "invalid_query",
            "--explain-limit requires --explain",
            output_format,
        )
    corpus = _load_manifest_or_exit(manifest, output_format)
    try:
        cutoff = _parse_since(since) if since else None
        database = Database(corpus.database)
        service = SearchService(
            database, corpus.corpus_id, embedding_profile=embedding_profile, contextual=contextual
        )
        if explain:
            report = service.explain_search(
                query, subject=subject, limit=limit, since=cutoff, source_path=source,
                include_quotes=include_quotes,
                trace_limit=explain_limit,
            )
            _render(report.model_dump(mode="json"), output_format)
            return
        results = service.search(
            query, subject=subject, limit=limit, since=cutoff, source_path=source
        )
    except (SearchExplanationError, SearchStateChangedError) as exc:
        _fail(exc.code, str(exc), output_format)
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


@app.command("source-range")
def source_range(
    anchor_id: str,
    manifest: ManifestOption,
    output_format: FormatOption = OutputFormat.text,
) -> None:
    """Return an exact immutable source range by anchor ID."""
    corpus = _load_manifest_or_exit(manifest, output_format)
    try:
        result = RetrievalService(
            Database(corpus.database),
            corpus.corpus_id,
        ).source_range(anchor_id)
    except RecordNotFoundError as exc:
        _fail("source_range_not_found", str(exc), output_format)
    _render(result.model_dump(mode="json"), output_format)


@app.command("source-context")
def source_context(
    anchor_id: str,
    manifest: ManifestOption,
    output_format: FormatOption = OutputFormat.text,
    max_anchors: Annotated[int, typer.Option(min=1, max=200)] = 50,
) -> None:
    """Read the containing section's anchored evidence from the same revision."""
    corpus = _load_manifest_or_exit(manifest, output_format)
    try:
        result = RetrievalService(
            Database(corpus.database),
            corpus.corpus_id,
        ).source_context(anchor_id, max_anchors=max_anchors)
    except RecordNotFoundError as exc:
        _fail("source_context_not_found", str(exc), output_format)
    _render(result.model_dump(mode="json"), output_format)


@app.command()
def revisions(
    source_path: str,
    manifest: ManifestOption,
    output_format: FormatOption = OutputFormat.text,
) -> None:
    """List immutable revisions for a source document."""
    corpus = _load_manifest_or_exit(manifest, output_format)
    try:
        results = RetrievalService(
            Database(corpus.database),
            corpus.corpus_id,
        ).revisions(source_path)
    except (RecordNotFoundError, RevisionComparisonError) as exc:
        _fail("source_not_found", str(exc), output_format)
    _render([result.model_dump(mode="json") for result in results], output_format)


@app.command("compare-revisions")
def compare_revisions(
    source_path: str,
    manifest: ManifestOption,
    from_revision: Annotated[str | None, typer.Option("--from")] = None,
    to_revision: Annotated[str | None, typer.Option("--to")] = None,
    output_format: FormatOption = OutputFormat.text,
) -> None:
    """Compare exact source ranges between two immutable revisions."""
    corpus = _load_manifest_or_exit(manifest, output_format)
    try:
        result = RetrievalService(
            Database(corpus.database),
            corpus.corpus_id,
        ).compare_revisions(
            source_path,
            from_revision_id=from_revision,
            to_revision_id=to_revision,
        )
    except RecordNotFoundError as exc:
        _fail("source_not_found", str(exc), output_format)
    except RevisionComparisonError as exc:
        _fail("invalid_revision_comparison", str(exc), output_format)
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


@app.command("record-state")
def record_state(
    manifest: ManifestOption,
    output_format: FormatOption = OutputFormat.text,
    include_quotes: Annotated[
        bool, typer.Option(help="Include exact source quotes in the state audit.")
    ] = False,
    limit: Annotated[int, typer.Option(min=1, max=200)] = 50,
) -> None:
    """Audit explicit supersession decisions and unresolved references across a corpus."""
    corpus = _load_manifest_or_exit(manifest, output_format)
    result = RetrievalService(Database(corpus.database), corpus.corpus_id).record_state(
        include_quotes=include_quotes, limit=limit
    )
    _render(result.model_dump(mode="json", exclude_none=True), output_format)


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
