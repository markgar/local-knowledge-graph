"""Private subprocess worker for frozen benchmark search, not a product CLI.

Keep the historical lexical argument grammar and JSON/error contracts independent
of the public search default. Structured/evidence commands still use ``kg``.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from kg.config import ManifestError, load_manifest
from kg.db import Database
from kg.retrieval import EmbeddingProfile, RetrievalService
from kg.retrieval.explain import SearchExplanationError, explain_search
from kg.retrieval.service import SearchQueryError


class ArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise ValueError(message)


def parse_since(value: str) -> datetime:
    match = re.fullmatch(r"([1-9][0-9]*)([dhw])", value)
    if not match:
        raise ValueError("since must use a positive duration such as 12h, 30d, or 4w")
    quantity = int(match[1])
    duration = {
        "h": timedelta(hours=quantity),
        "d": timedelta(days=quantity),
        "w": timedelta(weeks=quantity),
    }[match[2]]
    return datetime.now(UTC) - duration


def evaluate(arguments: list[str]) -> Any:
    parser = ArgumentParser(allow_abbrev=False)
    parser.add_argument("query")
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--format", choices=["json"], default="json")
    parser.add_argument("--query-mode", choices=["strict", "natural"], required=True)
    parser.add_argument("--subject")
    parser.add_argument("--source")
    parser.add_argument("--since")
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--explain", action="store_true")
    parser.add_argument("--include-quotes", action="store_true")
    parser.add_argument("--contextual", action="store_true")
    parser.add_argument("--embedding-profile", choices=list(EmbeddingProfile))
    args = parser.parse_args(arguments)
    if not 1 <= args.limit <= 100:
        raise ValueError("limit must be between 1 and 100")
    if args.include_quotes and not args.explain:
        raise SearchQueryError("--include-quotes requires --explain")
    if args.contextual:
        raise SearchQueryError("--contextual requires dense, hybrid, or reranked retrieval")
    try:
        manifest = load_manifest(args.manifest)
    except ValueError as exc:
        raise ManifestError(str(exc)) from exc
    database = Database(manifest.database)
    since = parse_since(args.since) if args.since else None
    if args.explain:
        report = explain_search(
            database, manifest.corpus_id, args.query, args.subject, args.limit,
            since, args.source, args.query_mode, include_quotes=args.include_quotes,
        )
        return report.model_dump(mode="json", exclude_none=True)
    results = RetrievalService(database, manifest.corpus_id).search(
        args.query, subject=args.subject, limit=args.limit, since=since,
        source_path=args.source, query_mode=args.query_mode,
    )
    return [result.model_dump(mode="json") for result in results]


def main() -> int:
    try:
        result = evaluate(sys.argv[1:])
    except (SearchExplanationError, ManifestError, SearchQueryError, ValueError) as exc:
        code = (
            exc.code if isinstance(exc, SearchExplanationError)
            else "invalid_manifest" if isinstance(exc, ManifestError)
            else "invalid_query"
        )
        print(json.dumps({"error": code, "message": str(exc)}), file=sys.stderr)
        return 1
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
