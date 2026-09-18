from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from kg.config import load_manifest
from kg.db import Database
from kg.ingest import IngestService
from kg.retrieval import RetrievalService


@pytest.mark.parametrize(
    "acceptance_path",
    sorted(Path("corpora/acceptance").glob("*.yml")),
    ids=lambda path: path.stem,
)
def test_reviewed_acceptance_cases(
    acceptance_path: Path,
    tmp_path: Path,
) -> None:
    fixture = yaml.safe_load(acceptance_path.read_text(encoding="utf-8"))
    source_manifest = load_manifest((acceptance_path.parent / fixture["corpus"]).resolve())
    source_manifest.database = tmp_path / f"{source_manifest.corpus_id}.sqlite3"
    database = Database(source_manifest.database)
    IngestService(database).ingest(source_manifest)
    retrieval = RetrievalService(database, source_manifest.corpus_id)

    for case in fixture["cases"]:
        command = case["command"]
        if command == "actions":
            results = retrieval.actions(case["subject"], case.get("status"))
            assert [item.quote for item in results] == case["expected_quotes"]
        elif command == "search":
            results = retrieval.search(case["query"], case.get("subject"))
            assert [item.quote for item in results] == case["expected_quotes"]
        elif command == "status":
            result = retrieval.status(case["subject"])
            assert [item.summary for item in result.decisions] == case.get(
                "expected_decisions",
                [],
            )
            assert [item.summary for item in result.blockers] == case.get(
                "expected_blockers",
                [],
            )
            assert [item.summary for item in result.connected_entities] == case.get(
                "expected_connections",
                [],
            )
            assert bool(result.evidence_gaps) is case.get("expect_evidence_gap", False)
        else:
            raise AssertionError(f"Unsupported acceptance command: {command}")


def test_rebuild_produces_equivalent_logical_results(tmp_path: Path) -> None:
    manifest = load_manifest(Path("corpora/research.yml"))
    manifest.database = tmp_path / "first.sqlite3"
    IngestService(Database(manifest.database)).ingest(manifest)
    first = RetrievalService(Database(manifest.database), manifest.corpus_id).status(
        "Orbit"
    )

    manifest.database.unlink()
    IngestService(Database(manifest.database)).ingest(manifest)
    rebuilt = RetrievalService(Database(manifest.database), manifest.corpus_id).status(
        "Orbit"
    )

    assert rebuilt.model_dump() == first.model_dump()
