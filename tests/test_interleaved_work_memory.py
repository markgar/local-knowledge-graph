from __future__ import annotations

import copy
import importlib.util
import json
import re
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from kg.config import load_manifest, select_sources
from kg.db import Database
from kg.ingest import IngestService
from kg.markdown import parse_markdown


def _module(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        f"test_{name}", Path(f"benchmarks/work_memory/{name}.py"),
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def generator() -> ModuleType:
    return _module("interleaved")


@pytest.fixture(scope="module")
def corpus(generator: ModuleType, tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    return generator.build(tmp_path_factory.mktemp("mixed") / "input")


def _json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.mark.acceptance
def test_default_corpus_is_large_interleaved_and_not_path_labeled(corpus: dict[str, Any]) -> None:
    manifest = load_manifest(corpus["manifest_path"])
    selected = select_sources(manifest)
    summary = corpus["summary"]
    assert selected.missing == []
    assert len(selected.paths) == summary["documents"] >= 2060
    assert summary["background_documents"] == 2000
    assert summary["source_utf8_bytes"] >= 1_000_000
    assert 10 <= summary["projects"] <= 20
    assert summary["projects_in_questions"] >= 10
    assert summary["cross_project_questions"] >= 5
    assert summary["questions"] >= 12
    assert summary["mixed_project_documents"] >= 200
    assert summary["reply_documents"] >= 100
    assert summary["earliest_date"] == "2026-01-05"
    assert summary["latest_date"] == "2026-09-18"
    texts = []
    for path in selected.paths:
        relative = path.relative_to(manifest.vault_root).as_posix()
        assert re.fullmatch(
            r"(mail|meetings|notes)/2026-\d{2}/2026\d{4}-[0-9a-f]{20}\.md", relative,
        )
        text = path.read_text(encoding="utf-8")
        texts.append(text)
        assert not re.search(r"\[\[|\[(key|supersedes|owner|due)::", text, re.IGNORECASE)
        assert "Recorded: 2026-" in text
        assert '"near_misses"' not in text and '"authored"' not in text
    assert len(set(texts)) == len(texts)
    assert not any(path.name in {"gold.json", "metadata.json"} for path in selected.paths)


@pytest.mark.acceptance
def test_frozen_gold_is_grounded_in_accessible_source_passages(corpus: dict[str, Any]) -> None:
    manifest = load_manifest(corpus["manifest_path"])
    questions = _json(corpus["questions_path"])
    gold = _json(corpus["gold_path"])
    metadata = _json(corpus["manifest_path"].parent / "metadata.json")
    assert {q["id"] for q in questions["questions"]} == set(gold["answers"])
    assert all("projects" not in q for q in questions["questions"])
    assert "expected_source_backed_facts" not in questions
    assert sum(answer["abstains"] for answer in gold["answers"].values()) >= 2
    complex_questions = 0
    for question_id, expected in gold["answers"].items():
        coverage = metadata["question_coverage"][question_id]
        assert len(set(coverage["near_misses"])) >= 2
        assert coverage["not_in_required_evidence_fraction"] >= 0.98
        sources = {s for rule in expected["evidence"] for s in rule["sources"]}
        if len(sources) >= 3 and len(expected["evidence"]) >= 5:
            complex_questions += 1
        for rule in expected["evidence"]:
            assert any(
                all(needle.casefold() in anchor.quote.casefold() for needle in rule["contains"])
                for relative in rule["sources"]
                for anchor in parse_markdown(
                    (manifest.vault_root / relative).read_text(encoding="utf-8"), relative,
                ).anchors
            ), (question_id, rule)
        for relative in coverage["near_misses"]:
            assert (manifest.vault_root / relative).is_file()
    assert complex_questions >= 8


@pytest.mark.acceptance
def test_replies_resolve_without_private_ids_or_project_name_shortcuts(
    generator: ModuleType, corpus: dict[str, Any],
) -> None:
    scenario = _json(generator.HERE / "interleaved_scenarios.json")
    documents = {document["id"]: document for document in scenario["documents"]}
    root = load_manifest(corpus["manifest_path"]).vault_root
    omitted = 0
    for document in documents.values():
        if not document.get("reply_to"):
            continue
        text = (root / generator._path(document, 17)).read_text(encoding="utf-8")
        parent = documents[document["reply_to"]]
        assert generator._message_id(parent["id"], 17) in text
        parent_text = (root / generator._path(parent, 17)).read_text(encoding="utf-8")
        assert generator._message_id(parent["id"], 17) in parent_text
        assert "{{ref:" not in text
        assert parent["date"] <= document["date"]
        if not any(project["name"].casefold() in text.casefold()
                   for project in scenario["projects"]):
            omitted += 1
    assert omitted >= 5


@pytest.mark.acceptance
def test_generation_is_reproducible_and_refuses_overwrite(
    generator: ModuleType, tmp_path: Path,
) -> None:
    first = generator.build(tmp_path / "a", background_documents=32)
    second = generator.build(tmp_path / "b", background_documents=32)
    assert first["summary"] == second["summary"]
    files = [
        {path.relative_to(root): path.read_bytes() for path in root.rglob("*") if path.is_file()}
        for root in (tmp_path / "a", tmp_path / "b")
    ]
    assert files[0] == files[1]
    before = (tmp_path / "a/gold.json").read_bytes()
    with pytest.raises(FileExistsError):
        generator.build(tmp_path / "a", background_documents=32)
    assert (tmp_path / "a/gold.json").read_bytes() == before
    third = generator.build(tmp_path / "c", background_documents=32, seed=23)
    assert set(_json(third["gold_path"])["answers"]) == set(_json(first["gold_path"])["answers"])
    assert set(_json(tmp_path / "a/metadata.json")["sources"]) != set(
        _json(tmp_path / "c/metadata.json")["sources"],
    )
    with pytest.raises(ValueError, match="nonnegative"):
        generator.build(tmp_path / "invalid", background_documents=-1)


@pytest.mark.acceptance
def test_ingestion_accepts_raw_sources_without_inventing_structured_state(
    corpus: dict[str, Any],
) -> None:
    manifest = load_manifest(corpus["manifest_path"])
    database = Database(manifest.database)
    report = IngestService(database).ingest(manifest, explain=True)
    assert report.added == corpus["summary"]["documents"]
    assert report.failed == 0 and not report.errors
    assert report.record_state is not None
    assert not report.record_state.warnings and report.record_state.supersessions_total == 0
    with database.connection() as connection:
        assert connection.execute("SELECT COUNT(*) FROM record_binding").fetchone()[0] == 0
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    # This checks index compatibility, not successful extraction of prose promises.


@pytest.mark.acceptance
def test_new_inputs_work_with_existing_frozen_comparison_harness(
    generator: ModuleType, tmp_path: Path,
) -> None:
    built = generator.build(tmp_path / "inputs", background_documents=16)
    evaluator = _module("evaluate")
    run = tmp_path / "run"
    snapshot = evaluator.prepare(
        run, built["manifest_path"], built["questions_path"], built["gold_path"],
        response_limit_bytes=12000,
    )
    assert len(snapshot["sources"]) == built["summary"]["documents"]
    assert evaluator.verify_snapshot(run) == snapshot
    info, success = evaluator.tool(run, "markdown", ["info"])
    assert success
    if info.get("paged_response"):
        info, success = evaluator.tool(
            run, "markdown", ["response", info["response_id"], "--field", "/questions"],
        )
        assert success and info["total"] == built["summary"]["questions"]
    else:
        assert len(info["questions"]) == built["summary"]["questions"]
    gold = _json(run / "gold.json")
    assert all(
        source in snapshot["sources"]
        for answer in gold["answers"].values() for rule in answer["evidence"]
        for source in rule["sources"]
    )


@pytest.mark.acceptance
def test_invalid_reply_graph_and_gold_ids_are_rejected(generator: ModuleType) -> None:
    scenario = _json(generator.HERE / "interleaved_scenarios.json")
    cyclic = copy.deepcopy(scenario)
    cyclic["documents"][0]["reply_to"] = cyclic["documents"][0]["id"]
    with pytest.raises(ValueError, match="cyclic"):
        generator._validate(cyclic)
    missing = copy.deepcopy(scenario)
    missing["documents"][0]["reply_to"] = "nonexistent-parent"
    with pytest.raises(ValueError, match="Missing"):
        generator._validate(missing)
    duplicate = copy.deepcopy(scenario)
    duplicate["documents"].append(copy.deepcopy(duplicate["documents"][0]))
    with pytest.raises(ValueError, match="unique"):
        generator._validate(duplicate)
    annotated = copy.deepcopy(scenario)
    annotated["documents"][0]["body"] += "\n[key:: hidden-helper]"
    with pytest.raises(ValueError, match="Artificial"):
        generator._validate(annotated)
