from __future__ import annotations

import importlib.util
import json
import re
import subprocess
import sys
from datetime import date
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
import yaml

from kg.config import load_manifest, select_sources
from kg.db import Database
from kg.ingest import IngestService
from kg.markdown import parse_markdown
from kg.models import IngestReport
from kg.record_state import resolve_record_state
from kg.retrieval import RetrievalService


@pytest.fixture(scope="module")
def generator() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "expanded_work_memory", Path("benchmarks/work_memory/expanded.py"),
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def expanded(generator: ModuleType, tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    return generator.build(tmp_path_factory.mktemp("expanded") / "input")


def _json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def test_output_is_byte_deterministic_and_refuses_overwrite(
    generator: ModuleType, tmp_path: Path,
) -> None:
    first = generator.build(tmp_path / "first")
    second = generator.build(tmp_path / "second")
    assert first["summary"] == second["summary"]
    snapshots = [
        {path.relative_to(root): path.read_bytes() for path in root.rglob("*") if path.is_file()}
        for root in (tmp_path / "first", tmp_path / "second")
    ]
    assert snapshots[0] == snapshots[1]
    with pytest.raises(FileExistsError):
        generator.build(tmp_path / "first")
    assert (tmp_path / "first/gold.json").read_bytes() == snapshots[0][Path("gold.json")]


def test_cli_builds_inputs_in_new_directory(tmp_path: Path) -> None:
    output = tmp_path / "cli-input"
    command = [
        sys.executable, "benchmarks/work_memory/expanded.py", "--output", str(output),
    ]
    result = subprocess.run(command, capture_output=True, text=True, check=True)
    payload = json.loads(result.stdout)
    assert payload["summary"]["documents"] == 483
    for key in ("manifest_path", "questions_path", "gold_path"):
        assert Path(payload[key]).is_file()
    second = subprocess.run(command, capture_output=True, text=True, check=False)
    assert second.returncode != 0 and "FileExistsError" in second.stderr


def test_scale_dates_genres_and_original_sources(expanded: dict[str, Any]) -> None:
    manifest = load_manifest(expanded["manifest_path"])
    selection = select_sources(manifest)
    assert selection.missing == []
    assert len(selection.paths) == 483
    assert sum(path.stat().st_size for path in selection.paths) >= 200_000
    projects = [entity for entity in manifest.seed_entities if entity.entity_type == "project"]
    assert len(projects) == 13
    texts = [path.read_text(encoding="utf-8") for path in selection.paths]
    dates = [
        date.fromisoformat(str(yaml.safe_load(text.split("---", 2)[1])["date"]))
        for text in texts
    ]
    assert (min(dates), max(dates)) == (date(2026, 1, 5), date(2026, 10, 8))
    assert (max(dates) - min(dates)).days >= 180
    focal = sorted((manifest.vault_root / "history/atlas").glob("*.md"))
    assert len(focal) == 39
    assert sum("## Working session" in text for text in texts) >= 100
    assert sum("\nFrom: " in text for text in texts) >= 100
    assert sum("## Decisions" in text for text in texts) >= 100
    assert sum("## Actions" in text for text in texts) >= 100
    assert len(set(texts)) == len(texts)
    # The old evidence and its path identity must survive the larger fixture.
    original = load_manifest(Path("corpora/atlas-state.yml"))
    for source in select_sources(original).paths:
        target = manifest.vault_root / source.relative_to(original.vault_root)
        assert target.read_bytes() == source.read_bytes()
    metadata = _json(expanded["manifest_path"].parent / "metadata.json")
    assert metadata["summary"] == expanded["summary"]
    assert metadata["expected_source_backed_facts"] == _json(expanded["gold_path"])["answers"]
    assert metadata["design"] and metadata["limitations"]
    assert len(metadata["sources"]) == 483


def test_gold_is_frozen_upfront_and_citations_exist(expanded: dict[str, Any]) -> None:
    questions = _json(expanded["questions_path"])
    gold = _json(expanded["gold_path"])["answers"]
    root = load_manifest(expanded["manifest_path"]).vault_root
    assert {question["id"] for question in questions["questions"]} == set(gold)
    assert len(gold) == 14
    original = _json(Path("benchmarks/work_memory/gold.json"))["answers"]
    for key in original:
        if key not in {"current-launch", "calendar-state", "open-work", "unknown-approval"}:
            assert gold[key] == original[key]
    assert gold["current-launch"]["facts"] == {"date": "2026-11-12"}
    assert gold["calendar-state"]["facts"]["current_target"] == "2026-11-12"
    assert gold["launch-history"]["facts"]["current_target"] == "2026-10-15"
    historical = next(q for q in questions["questions"] if q["id"] == "launch-history")
    assert "September 19 through September 26, 2026 inclusive" in historical["question"]
    assert gold["ledger-lifecycle"]["facts"]["versions"] == [
        {"date": "2026-01-05", "owner": "Priya", "status": "open", "due": "2026-02-06"},
        {"date": "2026-03-02", "owner": "Priya", "status": "completed", "due": "2026-03-02"},
        {"date": "2026-04-27", "owner": "Morgan", "status": "open", "due": "2026-06-12"},
        {"date": "2026-06-22", "owner": "Lena", "status": "open", "due": "2026-08-14"},
        {"date": "2026-09-07", "owner": "Lena", "status": "open", "due": "2026-10-09"},
    ]
    assert gold["ledger-replay-comparison"]["facts"] == {
        "sampled_accounts": [120, 120], "missing_accounts": [18, 3],
        "omissions_removed": 15, "completed": False,
    }
    assert gold["unknown-production-serial"]["facts"] == {
        "recorded": False, "serial_number": None,
    }
    assert gold["unknown-production-serial"]["abstains"] is True
    for expected in gold.values():
        assert expected["evidence"]
        for rule in expected["evidence"]:
            for source in rule["sources"]:
                text = (root / source).read_text(encoding="utf-8")
                assert all(needle.casefold() in text.casefold() for needle in rule["contains"])
    # Exact numeric observations are independently present, not inferred from weekly counters.
    march = (root / "history/atlas/13-working-record.md").read_text()
    august = (root / "history/atlas/31-working-record.md").read_text()
    assert "sampled 120 accounts and found 18 accounts" in march
    assert "resampled the same 120 accounts and found 3 accounts" in august
    assert "removed 15 omissions" in august and "not a task completion" in august


def test_every_gold_rule_has_an_anchored_citation(expanded: dict[str, Any]) -> None:
    root = load_manifest(expanded["manifest_path"]).vault_root
    gold = _json(expanded["gold_path"])["answers"]
    for question_id, expected in gold.items():
        for rule in expected["evidence"]:
            assert any(
                all(needle.casefold() in anchor.quote.casefold() for needle in rule["contains"])
                for source in rule["sources"]
                for anchor in parse_markdown(
                    (root / source).read_text(encoding="utf-8"), source,
                ).anchors
            ), (question_id, rule)
    lifecycle = gold["ledger-lifecycle"]
    assert len(lifecycle["evidence"]) == 5
    for index, rule in enumerate(lifecycle["evidence"], start=1):
        assert f"[key:: expanded-ledger-v{index}]" in rule["contains"]
        assert not any(needle.startswith("date: ") for needle in rule["contains"])


def test_unknown_approval_cites_latest_explicit_absence(expanded: dict[str, Any]) -> None:
    gold = _json(expanded["gold_path"])
    expected = gold["answers"]["unknown-approval"]
    original = _json(Path("benchmarks/work_memory/gold.json"))["answers"]["unknown-approval"]
    assert expected["facts"] == original["facts"] == {
        "approval_confirmed": False, "approver": None,
    }
    assert expected["abstains"] is original["abstains"] is True
    assert expected["evidence"] == [{
        "sources": ["history/atlas/launch-03.md"],
        "contains": ["Final production approval is not recorded."],
    }]
    assert any("Post-run gold maintenance" in note for note in gold["scoring_notes"])
    metadata = _json(expanded["manifest_path"].parent / "metadata.json")
    assert any("Post-run gold maintenance" in note for note in metadata["design"])


def test_ingestion_effective_state_and_exact_provenance(expanded: dict[str, Any]) -> None:
    manifest = load_manifest(expanded["manifest_path"])
    database = Database(manifest.database)
    ingestion = IngestService(database)
    report = ingestion.ingest(manifest, explain=True, include_quotes=True)
    assert isinstance(report, IngestReport)
    assert report.added == 483 and report.failed == 0 and report.errors == []
    assert report.record_state is not None and report.record_state.warnings == []
    retrieval = RetrievalService(database, manifest.corpus_id)
    state = retrieval.record_state(limit=200, include_quotes=True)
    assert state.truncated and not state.warnings
    assert state.supersessions_total == 201 and len(state.supersessions) == 200
    with database.connection() as connection:
        complete_state = resolve_record_state(connection, manifest.corpus_id)
    assert len(complete_state.supersessions) == 201
    assert {link.resolution for link in complete_state.supersessions} == {"applied"}
    status = retrieval.status("Atlas")
    assert status.evidence_gaps == []
    actual = sorted(
        (item.summary, item.owner, item.due_date) for item in status.open_actions
    )
    assert actual == [
        ("Reconcile the historical permission ledger.", "Lena", "2026-10-09"),
        ("Update the launch calendar.", "Morgan", "2026-09-25"),
        ("Upload access evidence.", "Morgan", "2026-09-24"),
    ]
    assert [(item.summary, item.owner) for item in status.completed_actions] == [
        ("Confirm the review date.", "Priya"),
    ]
    assert {item.summary for item in status.decisions} == {
        "Keep Atlas in pilot until the security review is complete.",
        "Require the signed access inventory before approval.",
        "Move the Atlas launch target to November 12, 2026.",
    }
    assert len(status.blockers) == 2 and len(status.conflicts) == 1
    assert len(retrieval.actions("Atlascope")) == 2
    assert {item.summary for item in retrieval.actions("Atlasbridge")} == {
        "Reconcile the Atlasbridge evidence packet.",
    }
    # Every added focal record is either superseded or explicitly counted above.
    focal_records = [
        detail for document in report.documents if document.source_path.startswith("history/atlas/")
        for detail in document.details if detail.record_type in ("action", "decision")
    ]
    assert len(focal_records) == 8
    effective_ids = {item.record_id for item in [*status.open_actions, *status.decisions]}
    for record in focal_records:
        assert record.record_id in effective_ids or record.record_id in state.superseded_record_ids
    for document in report.documents:
        text = (manifest.vault_root / document.source_path).read_text(encoding="utf-8")
        assert not document.details_truncated and not document.anchors_truncated
        for detail in document.details:
            assert text[detail.start_offset:detail.end_offset] == detail.quote
            assert retrieval.evidence(detail.record_id).quote == detail.quote
    bindings = re.findall(
        r"\[key:: ([A-Za-z0-9][A-Za-z0-9._:-]*)\]",
        "\n".join(path.read_text() for path in select_sources(manifest).paths),
    )
    assert len(bindings) == len(set(bindings))
    # Superseded completion remains searchable evidence, but not current completion state.
    historical = retrieval.search(
        "reconciliation", source_path="history/atlas/09-working-record.md",
    )
    assert historical
    assert any("[x]" in item.quote for item in retrieval.search(
        "historical permission ledger", source_path="history/atlas/09-working-record.md",
    ))
    again = ingestion.ingest(manifest, explain=True)
    assert again.unchanged == 483 and again.failed == 0
    assert again.record_state is not None and not again.record_state.warnings
    with database.connection() as connection:
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
