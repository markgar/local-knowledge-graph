from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import ModuleType

import pytest


def _evaluator() -> ModuleType:
    path = Path("benchmarks/work_memory/evaluate.py")
    spec = importlib.util.spec_from_file_location("work_memory_evaluate", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def evaluator() -> ModuleType:
    return _evaluator()


@pytest.fixture
def run(evaluator: ModuleType, tmp_path: Path) -> Path:
    path = tmp_path / "run"
    evaluator.prepare(path)
    return path


def _perfect_answers(evaluator: ModuleType, run: Path) -> dict[str, object]:
    gold = json.loads((run / "gold.json").read_text())
    answers = []
    for question_id, expected in gold["answers"].items():
        citations = [
            {
                "source_path": rule["sources"][0],
                "quote": (run / "notes" / rule["sources"][0]).read_text(),
            }
            for rule in expected["evidence"]
        ]
        answers.append({
            "id": question_id, "answer": "Test-only canonical submission.",
            "facts": expected["facts"], "abstains": expected["abstains"],
            "citations": citations,
        })
    return {"answers": answers}


def test_gold_is_consistent_with_frozen_fixture(evaluator: ModuleType, run: Path) -> None:
    snapshot = evaluator.verify_snapshot(run)
    assert len(snapshot["sources"]) == 12
    questions = json.loads((run / "questions.json").read_text())
    gold = json.loads((evaluator.HERE / "gold.json").read_text())
    assert {item["id"] for item in questions["questions"]} == set(gold["answers"])
    for expected in gold["answers"].values():
        for rule in expected["evidence"]:
            assert any(
                all(
                    needle.casefold() in (run / "notes" / source).read_text().casefold()
                    for needle in rule["contains"]
                )
                for source in rule["sources"]
            )


def test_tools_expose_same_sources_without_gold(evaluator: ModuleType, run: Path) -> None:
    kg_info, success = evaluator.tool(run, "kg", ["info"])
    assert success and "expected_facts" not in json.dumps(kg_info)
    md_info, success = evaluator.tool(run, "markdown", ["info"])
    assert success
    assert kg_info["source_paths"] == md_info["source_paths"]
    assert kg_info["questions"] == md_info["questions"]
    assert kg_info["seed_entities"] == md_info["seed_entities"]
    assert kg_info["execution_rules"] == md_info["execution_rules"]
    read, success = evaluator.tool(run, "markdown", ["read", *md_info["source_paths"]])
    assert success and len(read["documents"]) == 12
    matches, success = evaluator.tool(run, "markdown", ["search", "certificate|approval"])
    assert success and matches["matches"]
    status, success = evaluator.tool(run, "kg", ["status", "Atlas"])
    assert success
    assert len(status["open_actions"]) == 2 and len(status["completed_actions"]) == 1
    event = json.loads((run / "tools-kg.jsonl").read_text().splitlines()[-1])
    assert event["request_id"] and event["run_id"]
    assert event["execution"]["backend"] == "kg_cli"
    assert event["execution"]["exit_code"] == 0
    assert "kg" in event["execution"]["argv"]
    assert event["response_shape"]["collection_counts"]["open_actions"] == 2


@pytest.mark.parametrize(
    ("arm", "arguments"),
    [
        ("markdown", ["read", "../gold.json"]),
        ("markdown", ["search", "["]),
        ("kg", ["ingest"]),
        ("kg", ["search", "anything", "--manifest=/tmp/other.yml"]),
        ("kg", ["search", "anything", "--query-mode", "dense"]),
        ("kg", ["search", "anything", "--query-mode=reranked"]),
        ("kg", []),
    ],
)
def test_invalid_tool_calls_are_logged(
    evaluator: ModuleType, run: Path, arm: str, arguments: list[str],
) -> None:
    result, success = evaluator.tool(run, arm, arguments)
    assert not success and result["error"]
    entry = json.loads((run / f"tools-{arm}.jsonl").read_text().splitlines()[-1])
    assert entry["arguments"] == arguments and not entry["success"]
    assert entry["returned_utf8_bytes"] > 0


def test_snapshot_mutation_is_detected(evaluator: ModuleType, run: Path) -> None:
    path = run / "notes/atlas-updates/12-launch-rescheduling.md"
    path.write_text("Changed source\n")
    result, success = evaluator.tool(run, "markdown", ["info"])
    assert not success and "Frozen source changed" in result["message"]
    with pytest.raises(ValueError, match="Frozen source changed"):
        evaluator.score(run)


def test_scoring_requires_submissions_and_separates_facts_from_citations(
    evaluator: ModuleType, run: Path,
) -> None:
    perfect = _perfect_answers(evaluator, run)
    for arm in evaluator.ARMS:
        (run / f"answers-{arm}.json").write_text(json.dumps(perfect))
        evaluator.tool(run, arm, ["info"])
        evaluator.tool(run, arm, ["submit"])
    result = evaluator.score(run)
    assert all(arm["passed"] == 10 for arm in result["arms"].values())
    assert all(arm["data_tool_calls"] == 0 for arm in result["arms"].values())
    bad = json.loads(json.dumps(perfect))
    bad["answers"][0]["facts"]["date"] = "2026-10-01"
    bad["answers"][1]["citations"][0]["quote"] = "Invented quote"
    bad["answers"][2]["citations"] = [{"source_path": [], "quote": "bad shape"}]
    (run / "answers-kg.json").write_text(json.dumps(bad))
    with pytest.raises(ValueError, match="changed after submission"):
        evaluator.score(run)
    evaluator.tool(run, "kg", ["submit"])
    result = evaluator.score(run)
    scores = {item["id"]: item for item in result["arms"]["kg"]["scores"]}
    assert not scores["current-launch"]["facts_correct"]
    assert scores["current-launch"]["citations_valid"]
    assert scores["open-work"]["facts_correct"]
    assert not scores["open-work"]["citations_valid"]
    assert not scores["review-confirmed"]["citations_valid"]
    assert result["arms"]["markdown"]["passed"] == 10


def test_missing_citation_is_not_full_credit(evaluator: ModuleType, run: Path) -> None:
    gold = json.loads((evaluator.HERE / "gold.json").read_text())["answers"]["current-launch"]
    result = evaluator._score_answer({
        "id": "current-launch", "facts": gold["facts"],
        "abstains": False, "citations": [],
    }, gold, {})
    assert result["facts_correct"] and not result["passed"]


def test_preparation_does_not_overwrite_existing_run(evaluator: ModuleType, run: Path) -> None:
    with pytest.raises(FileExistsError):
        evaluator.prepare(run)


def test_delivery_failures_are_distinct_from_operation_failures(
    evaluator: ModuleType, run: Path,
) -> None:
    def broken_pipe(text: str) -> None:
        raise BrokenPipeError("reader exited")

    result, success = evaluator.tool(run, "kg", ["status", "Atlas"], emit=broken_pipe)
    assert not success
    assert len(result["open_actions"]) == 2
    entry = json.loads((run / "tools-kg.jsonl").read_text().splitlines()[-1])
    assert entry["operation_success"] is True
    assert entry["execution"]["exit_code"] == 0
    assert entry["delivery_success"] is False
    assert "BrokenPipeError" in entry["delivery_error"]
    assert entry["result"] == result


def test_successful_delivery_is_recorded(evaluator: ModuleType, run: Path) -> None:
    output = []
    result, success = evaluator.tool(run, "markdown", ["info"], emit=output.append)
    assert success and json.loads(output[0]) == result
    entry = json.loads((run / "tools-markdown.jsonl").read_text().splitlines()[-1])
    assert entry["delivery_success"] is True and entry["delivery_error"] is None


def test_custom_gold_is_frozen_locally(evaluator: ModuleType, tmp_path: Path) -> None:
    questions = json.loads((evaluator.HERE / "questions.json").read_text())
    questions["questions"] = questions["questions"][:1]
    gold = json.loads((evaluator.HERE / "gold.json").read_text())
    gold["answers"] = {"current-launch": gold["answers"]["current-launch"]}
    questions_path, gold_path = tmp_path / "questions.json", tmp_path / "gold.json"
    questions_path.write_text(json.dumps(questions))
    gold_path.write_text(json.dumps(gold))
    run = tmp_path / "custom-run"
    evaluator.prepare(run, questions_path=questions_path, gold_path=gold_path)
    gold_path.write_text("{}")
    perfect = _perfect_answers(evaluator, run)
    for arm in evaluator.ARMS:
        (run / f"answers-{arm}.json").write_text(json.dumps(perfect))
        evaluator.tool(run, arm, ["submit"])
    assert evaluator.score(run)["arms"]["kg"]["passed"] == 1
    (run / "gold.json").write_text("{}")
    with pytest.raises(ValueError, match="Gold changed"):
        evaluator.score(run)


def test_paged_responses_preserve_complete_data_and_arm_isolation(
    evaluator: ModuleType, tmp_path: Path,
) -> None:
    run = tmp_path / "paged-run"
    snapshot = evaluator.prepare(run, response_limit_bytes=4000)
    text = "Evidence \U0001f4c4\n" * 1000
    result = {"large": text, "a/b~c": [1, 2, 3]}
    envelope = evaluator._bound_response(run, "kg", result, snapshot)
    assert envelope["paged_response"]
    assert len(json.dumps(envelope, ensure_ascii=False).encode()) <= 4000
    identifier = envelope["response_id"]
    pieces = []
    offset = 0
    while offset is not None:
        page, success = evaluator.tool(
            run, "kg", ["response", identifier, "--field", "/large",
                        "--offset", str(offset), "--limit", "250"],
        )
        assert success and "paged_response" not in page
        pieces.append(page["text"])
        offset = page["next_offset"]
    assert "".join(pieces) == text
    page, success = evaluator.tool(
        run, "kg", ["response", identifier, "--field", "/a~1b~0c", "--limit", "2"],
    )
    assert success and page == {"items": [1, 2], "offset": 0, "total": 3, "next_offset": 2}
    fields, success = evaluator.tool(run, "kg", ["response-fields", identifier])
    assert success and fields["items"] == ["large", "a/b~c"]
    denied, success = evaluator.tool(run, "markdown", ["response", identifier])
    assert not success and denied["error"] == "FileNotFoundError"
    (run / "responses/kg" / f"{identifier}.json").write_text("{}")
    changed, success = evaluator.tool(run, "kg", ["response", identifier])
    assert not success and changed["message"] == "Stored response changed"


def test_bounded_native_response_and_catalog_paging(
    evaluator: ModuleType, tmp_path: Path,
) -> None:
    run = tmp_path / "bounded-run"
    evaluator.prepare(run, response_limit_bytes=4000)
    result, success = evaluator.tool(run, "kg", ["status", "Atlas"])
    assert success and result["paged_response"]
    page, success = evaluator.tool(
        run, "kg", ["response", result["response_id"], "--field", "/open_actions"],
    )
    assert success and len(page["items"]) == 2
    for arm in evaluator.ARMS:
        catalog, success = evaluator.tool(
            run, arm, ["catalog", "sources", "atlas-vault", "--offset", "2", "--limit", "3"],
        )
        assert success and catalog["total"] == 10 and catalog["next_offset"] == 5
        assert len(catalog["items"]) == 3
    for arguments in (
        ["catalog", "sources", "--offset", "-1"],
        ["catalog", "sources", "--unknown"],
        ["response", "../gold.json"],
        ["response", result["response_id"], "--field", "/open_actions/999"],
    ):
        error, success = evaluator.tool(run, "kg", arguments)
        assert not success and error["error"]


def test_large_field_catalog_is_bounded(evaluator: ModuleType, tmp_path: Path) -> None:
    run = tmp_path / "fields-run"
    snapshot = evaluator.prepare(run, response_limit_bytes=4000)
    result = {f"{index}-" + "x" * 500: "y" * 500 for index in range(30)}
    envelope = evaluator._bound_response(run, "kg", result, snapshot)
    assert len(json.dumps(envelope, ensure_ascii=False).encode()) <= 4000
    assert envelope["field_count"] == 30
    page, success = evaluator.tool(
        run, "kg", ["response-fields", envelope["response_id"], "--limit", "2"],
    )
    assert success and page["items"] == list(result)[:2] and page["next_offset"] == 2
