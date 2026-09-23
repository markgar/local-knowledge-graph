from __future__ import annotations

import copy
import hashlib
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

from kg.config import load_manifest, select_sources
from kg.markdown import parse_markdown


def _module(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        f"test_discovery_{name}", Path(f"benchmarks/work_memory/{name}.py"),
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _digest_tree(root: Path) -> dict[str, str]:
    return {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in root.rglob("*") if path.is_file()
    }


@pytest.fixture(scope="module")
def builder() -> ModuleType:
    return _module("discovery")


@pytest.fixture(scope="module")
def inputs(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("discovery-inputs") / "inputs"
    _module("interleaved").build(root)
    return root


@pytest.fixture(scope="module")
def pack(
    builder: ModuleType, inputs: Path, tmp_path_factory: pytest.TempPathFactory,
) -> dict[str, Any]:
    output = tmp_path_factory.mktemp("discovery-pack") / "questions"
    before = _digest_tree(inputs)
    result = builder.build(inputs, output)
    assert _digest_tree(inputs) == before
    return result


def test_pack_reuses_all_original_sources_and_manifest(
    inputs: Path, pack: dict[str, Any],
) -> None:
    assert pack["manifest_path"] == inputs / "corpus.yml"
    manifest = load_manifest(pack["manifest_path"])
    original = _json(inputs / "metadata.json")
    private = _json(pack["gold_path"].parent / "metadata.json")
    assert pack["summary"]["documents"] == len(select_sources(manifest).paths) == 2078
    assert pack["summary"]["source_utf8_bytes"] == original["summary"]["source_utf8_bytes"]
    assert set(private["sources"]) == set(original["sources"])
    for source, info in private["sources"].items():
        content = (manifest.vault_root / source).read_bytes()
        assert info == {"sha256": hashlib.sha256(content).hexdigest(), "bytes": len(content)}
    assert set(p.name for p in pack["gold_path"].parent.iterdir()) == {
        "questions.json", "gold.json", "metadata.json",
    }
    assert not pack["gold_path"].is_relative_to(manifest.vault_root)
    assert not (inputs / "index.sqlite3").exists()
    for name, digest in private["input_sha256"].items():
        assert hashlib.sha256((inputs / name).read_bytes()).hexdigest() == digest


def test_build_is_deterministic_and_refuses_any_existing_output(
    builder: ModuleType, inputs: Path, pack: dict[str, Any], tmp_path: Path,
) -> None:
    result = builder.build(inputs, tmp_path / "second")
    assert result["summary"] == pack["summary"]
    assert _digest_tree(result["gold_path"].parent) == _digest_tree(pack["gold_path"].parent)
    before = _digest_tree(tmp_path / "second")
    with pytest.raises(FileExistsError):
        builder.build(inputs, tmp_path / "second")
    assert _digest_tree(tmp_path / "second") == before
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(FileExistsError):
        builder.build(inputs, empty)
    file = tmp_path / "file"
    file.write_text("keep")
    with pytest.raises(FileExistsError):
        builder.build(inputs, file)
    assert file.read_text() == "keep"


@pytest.mark.parametrize("relative", ["new-pack", "notes/discovery"])
def test_private_output_cannot_enter_input_corpus(
    builder: ModuleType, inputs: Path, relative: str,
) -> None:
    with pytest.raises(ValueError, match="outside"):
        builder.build(inputs, inputs / relative)
    assert not (inputs / relative).exists()


def test_generic_public_contract_and_no_recursive_leaks(
    builder: ModuleType, pack: dict[str, Any],
) -> None:
    public = _json(pack["questions_path"])
    spec = _json(builder.SPEC_PATH)
    scenario = _json(builder.HERE / "interleaved_scenarios.json")
    builder._validate_public(public, spec, scenario)
    assert len(public["questions"]) == 10
    assert [q["id"] for q in public["questions"]] == [f"q{i:02}" for i in range(1, 11)]
    assert all(set(q) == {"id", "question", "facts_schema"} for q in public["questions"])
    assert all(q["facts_schema"] == {"project": "Canonical project name as a string, or null."}
               for q in public["questions"])
    assert not any(
        key in public for key in ("question_coverage", "sources", "original_cases", "rationale")
    )
    for q in public["questions"]:
        assert not re.search(r"\d|abstain|unresolved|if it", q["question"], re.IGNORECASE)
        assert not re.search(
            r"\b(?:January|February|March|April|May|June|July|August|September|October)\b",
            q["question"],
        )
    assert any("For every question" in item for item in public["conventions"])


@pytest.mark.parametrize("secret", [
    "NovaOps", "novaops", "expected_novaops", "selected_project_Cedar",
    "inherited-role reconciliation", "N42", "invoice-cohort-comparison", "d044",
    "appointment pilot", "Relay", "visitor-count export",
])
def test_leak_validation_includes_nested_schema_keys(
    builder: ModuleType, pack: dict[str, Any], secret: str,
) -> None:
    public = _json(pack["questions_path"])
    public["questions"][0]["facts_schema"] = {"project": {"nested": [{secret: "string"}]}}
    with pytest.raises(ValueError, match="leaks"):
        builder._validate_public(
            public, _json(builder.SPEC_PATH),
            _json(builder.HERE / "interleaved_scenarios.json"),
        )


def test_every_gold_alternative_fits_an_exact_single_source_anchor(
    pack: dict[str, Any],
) -> None:
    manifest = load_manifest(pack["manifest_path"])
    public = _json(pack["questions_path"])
    gold = _json(pack["gold_path"])
    expected_projects = {
        entity.name for entity in manifest.seed_entities if entity.entity_type == "project"
    }
    assert set(gold["answers"]) == {q["id"] for q in public["questions"]}
    alternatives = 0
    for answer in gold["answers"].values():
        assert set(answer) == {"facts", "abstains", "evidence"}
        assert set(answer["facts"]) == {"project"}
        project = answer["facts"]["project"]
        assert project is None or project in expected_projects
        assert answer["abstains"] is (project is None)
        assert answer["evidence"]
        for rule in answer["evidence"]:
            assert set(rule) == {"sources", "contains"}
            assert rule["sources"] and rule["contains"] and all(rule["contains"])
            alternatives += len(rule["sources"]) - 1
            for source in rule["sources"]:
                text = (manifest.vault_root / source).read_text(encoding="utf-8")
                matches = [
                    anchor.quote for anchor in parse_markdown(text, source).anchors
                    if all(needle in anchor.quote for needle in rule["contains"])
                ]
                assert matches, (source, rule)
                assert all(quote in text for quote in matches)
    assert alternatives >= 1


def test_frozen_time_semantics_and_deliberately_wrong_date(
    builder: ModuleType, pack: dict[str, Any],
) -> None:
    public = _json(pack["questions_path"])
    private = _json(pack["gold_path"].parent / "metadata.json")
    assert public["reference_date"] == private["reference_date"] == "2026-09-18"
    assert public["reference_date"] in public["scenario"]
    assert "never use the actual current date" in public["scenario"]
    assert any("not hard filters" in item for item in public["conventions"])
    assert builder._memory_window("last week") == ["2026-09-07", "2026-09-13"]
    assert builder._memory_window("earlier this week") == ["2026-09-14", "2026-09-18"]
    assert builder._memory_window("late last month") == ["2026-08-21", "2026-08-31"]
    assert builder._memory_window("a couple of weeks ago") == ["2026-08-28", "2026-09-11"]
    assert builder._memory_window("a while back") is None
    spec = _json(builder.SPEC_PATH)
    for question in spec["questions"]:
        memory = question["memory"]
        window = builder._memory_window(memory["hint"])
        assert memory["window"] == window
        assert all(date.fromisoformat(day) <= builder.REFERENCE_DATE
                   for day in memory["episode_dates"])
        if window:
            outside = [day for day in memory["episode_dates"] if not window[0] <= day <= window[1]]
            assert bool(outside) is memory["imperfect"]
    wrong = private["question_coverage"]["q02"]["memory"]
    assert wrong["imperfect"]
    assert max(wrong["episode_dates"]) < wrong["window"][0]
    assert _json(pack["gold_path"])["answers"]["q02"]["facts"]["project"] == "Cedar"
    assert pack["summary"]["imperfect_memory_questions"] == 1


def test_private_manual_cue_reviews_cover_every_prompt(
    builder: ModuleType, pack: dict[str, Any],
) -> None:
    spec = _json(builder.SPEC_PATH)
    scenario = _json(builder.HERE / "interleaved_scenarios.json")
    cases = {q["id"]: q for q in scenario["questions"]}
    private = _json(pack["gold_path"].parent / "metadata.json")
    for q in spec["questions"]:
        review = private["question_coverage"][q["id"]]
        assert review["rationale"] == q["rationale"]
        assert len(review["rationale"]) > 100
        case_projects = {
            p for original in review["original_cases"] for p in cases[original]["projects"]
        }
        if q["project"] is not None:
            assert q["project"] in case_projects
        else:
            assert set(q["candidate_projects"]) <= case_projects


def test_ambiguity_and_nameless_reply_have_real_thread_grounding(
    builder: ModuleType, pack: dict[str, Any],
) -> None:
    manifest = load_manifest(pack["manifest_path"])
    scenario = _json(builder.HERE / "interleaved_scenarios.json")
    documents = {d["id"]: d for d in scenario["documents"]}

    def text(identifier: str) -> str:
        return (manifest.vault_root / builder._source_path(documents[identifier], 17)).read_text()

    def message_id(identifier: str) -> str:
        return re.search(r"^Message-ID: (.+)$", text(identifier), re.MULTILINE)[1]

    gold = _json(pack["gold_path"])["answers"]
    assert gold["q10"]["facts"] == {"project": None}
    assert gold["q10"]["abstains"] is True
    assert pack["summary"]["ambiguous_questions"] == 1
    assert "From: Jamie\n" in text("d051")
    assert "From: Alex\nTo: Jamie, Noor\n" in text("d052")
    assert f"In-Reply-To: {message_id('d051')}" in text("d052")
    assert "haven't chosen which one" in text("d051")
    assert not any(p in text("d052") for p in ("Juniper", "Kite"))
    assert "To: Rina, Noor\n" in text("d075")
    assert f"In-Reply-To: {message_id('d051')}" not in text("d075")
    assert "From: Jamie\n" in text("d042")
    assert message_id("d015") in text("d042").split("References: ")[1].split("\n")[0]
    assert "Aster" not in text("d042")
    assert len(gold["q08"]["evidence"]) == 2


def _replace_spec(
    monkeypatch: pytest.MonkeyPatch, builder: ModuleType, spec: dict[str, Any],
) -> None:
    original = builder._read_json
    monkeypatch.setattr(
        builder, "_read_json",
        lambda path: copy.deepcopy(spec) if path == builder.SPEC_PATH else original(path),
    )


@pytest.mark.parametrize("corruption,match", [
    ("project", "expected project"),
    ("reference", "reference date"),
    ("window", "Relative-time window"),
    ("imperfect", "actually disagree"),
    ("ambiguity", "multiple supported"),
    ("anchor", "one exact parsed anchor"),
    ("alternative", "one exact parsed anchor"),
    ("public", "leaks"),
])
def test_invalid_specs_fail_before_creating_output(
    builder: ModuleType, inputs: Path, tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch, corruption: str, match: str,
) -> None:
    spec = _json(builder.SPEC_PATH)
    if corruption == "project":
        spec["questions"][0]["project"] = "Invented"
    elif corruption == "reference":
        spec["reference_date"] = "2026-09-19"
    elif corruption == "window":
        spec["questions"][0]["memory"]["window"][0] = "2026-09-01"
    elif corruption == "imperfect":
        spec["questions"][0]["memory"]["imperfect"] = True
    elif corruption == "ambiguity":
        spec["questions"][-1]["candidate_projects"] = ["Juniper"]
    elif corruption == "anchor":
        # Both substrings occur in the file but in different paragraphs.
        spec["questions"][0]["evidence"] = [{
            "sources": ["d044"], "contains": ["Nova N42", "The import review stays open"],
        }]
    elif corruption == "alternative":
        # At least one source still matches; that must not hide a broken alternative.
        spec["questions"][0]["evidence"][0]["sources"].append("d041")
    else:
        spec["questions"][0]["question"] += " NovaOps?"
    _replace_spec(monkeypatch, builder, spec)
    output = tmp_path / "invalid"
    with pytest.raises(ValueError, match=match):
        builder.build(inputs, output)
    assert not output.exists()


def test_source_integrity_is_checked_without_touching_input(
    builder: ModuleType, inputs: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = Path.read_bytes
    source = next((inputs / "notes").rglob("*.md"))
    before = original(source)
    monkeypatch.setattr(
        Path, "read_bytes",
        lambda path: original(path) + b"\n" if path == source else original(path),
    )
    with pytest.raises(ValueError, match="Source bytes differ"):
        builder.build(inputs, tmp_path / "invalid")
    assert original(source) == before
    assert not (tmp_path / "invalid").exists()


def test_cli_builds_only_question_pack(inputs: Path, tmp_path: Path) -> None:
    result = subprocess.run(
        [
            sys.executable, "benchmarks/work_memory/discovery.py",
            "--inputs", str(inputs), "--output", str(tmp_path / "cli"),
        ],
        text=True, capture_output=True, check=True,
    )
    summary = json.loads(result.stdout)
    assert summary["manifest_path"] == str(inputs / "corpus.yml")
    assert summary["summary"]["questions"] == 10
    assert summary["summary"]["documents"] == 2078
    assert Path(summary["questions_path"]).is_file()
    assert Path(summary["gold_path"]).is_file()
