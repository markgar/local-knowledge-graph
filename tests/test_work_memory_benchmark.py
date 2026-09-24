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


@pytest.mark.functional
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


@pytest.mark.functional
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
    assert event["execution"]["argv"][:3] == [evaluator.sys.executable, "-m", "kg.legacy_cli"]
    assert event["response_shape"]["collection_counts"]["open_actions"] == 2


@pytest.mark.functional
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


@pytest.mark.functional
def test_snapshot_mutation_is_detected(evaluator: ModuleType, run: Path) -> None:
    path = run / "notes/atlas-updates/12-launch-rescheduling.md"
    path.write_text("Changed source\n")
    result, success = evaluator.tool(run, "markdown", ["info"])
    assert not success and "Frozen source changed" in result["message"]
    with pytest.raises(ValueError, match="Frozen source changed"):
        evaluator.score(run)


@pytest.mark.functional
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


@pytest.mark.functional
def test_missing_citation_is_not_full_credit(evaluator: ModuleType, run: Path) -> None:
    gold = json.loads((evaluator.HERE / "gold.json").read_text())["answers"]["current-launch"]
    result = evaluator._score_answer({
        "id": "current-launch", "facts": gold["facts"],
        "abstains": False, "citations": [],
    }, gold, {})
    assert result["facts_correct"] and not result["passed"]


@pytest.mark.functional
def test_preparation_does_not_overwrite_existing_run(evaluator: ModuleType, run: Path) -> None:
    with pytest.raises(FileExistsError):
        evaluator.prepare(run)


@pytest.mark.functional
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


@pytest.mark.functional
def test_successful_delivery_is_recorded(evaluator: ModuleType, run: Path) -> None:
    output = []
    result, success = evaluator.tool(run, "markdown", ["info"], emit=output.append)
    assert success and json.loads(output[0]) == result
    entry = json.loads((run / "tools-markdown.jsonl").read_text().splitlines()[-1])
    assert entry["delivery_success"] is True and entry["delivery_error"] is None


@pytest.mark.functional
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


@pytest.mark.functional
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


@pytest.mark.functional
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


@pytest.mark.functional
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


@pytest.mark.functional
def test_three_arms_share_document_tools(evaluator: ModuleType, run: Path) -> None:
    snapshot = evaluator.verify_snapshot(run)
    assert snapshot["version"] == 3
    assert snapshot["arms"] == ["markdown", "index", "kg"]
    path = "atlas-vault/01-planning-meeting.md"
    results = {}
    for arm in evaluator.ARMS:
        info, success = evaluator.tool(run, arm, ["info"])
        assert success
        assert any(tool.startswith("read ") for tool in info["shared_tools"])
        assert any(tool.startswith("grep ") for tool in info["shared_tools"])
        read, success = evaluator.tool(run, arm, ["read", path])
        assert success
        matches, success = evaluator.tool(run, arm, ["grep", "approval"])
        assert success
        results[arm] = (read, matches)
    assert results["markdown"] == results["index"] == results["kg"]
    lexical, success = evaluator.tool(run, "index", ["search", "approval"])
    assert success and lexical
    same, success = evaluator.tool(run, "kg", ["search", "approval"])
    assert success and same == lexical
    contextual, success = evaluator.tool(
        run, "index", ["source-context", lexical[0]["anchor_id"]],
    )
    assert success and contextual


@pytest.mark.functional
@pytest.mark.parametrize("arguments", [
    ["status", "Atlas"], ["actions", "Atlas"], ["record-state"],
    ["search", "approval", "--subject", "Atlas"],
    ["search", "approval", "--subject=Atlas"],
    ["search", "approval", "--explain"],
    ["search", "approval", "--query-mode", "hybrid"],
    ["search", "approval", "--since", "7d"],
])
def test_index_arm_cannot_gain_graph_or_unfrozen_modes(
    evaluator: ModuleType, run: Path, arguments: list[str], monkeypatch: pytest.MonkeyPatch,
) -> None:
    def forbidden(*args: object, **kwargs: object) -> None:
        pytest.fail("Forbidden operation reached native CLI")

    monkeypatch.setattr(evaluator.subprocess, "run", forbidden)
    result, success = evaluator.tool(run, "index", arguments)
    assert not success and result["error"] == "ValueError"


@pytest.mark.functional
def test_selected_arms_and_old_two_arm_scores(
    evaluator: ModuleType, tmp_path: Path,
) -> None:
    selected = tmp_path / "selected"
    evaluator.prepare(selected, arms=("markdown", "index"))
    failure, success = evaluator.tool(selected, "kg", ["status", "Atlas"])
    assert not success and "not configured" in failure["message"]
    legacy = tmp_path / "legacy"
    evaluator.prepare(legacy)
    snapshot = json.loads((legacy / "snapshot.json").read_text())
    snapshot["version"] = 2
    snapshot.pop("arms")
    snapshot.pop("shared_document_access")
    (legacy / "snapshot.json").write_text(json.dumps(snapshot))
    assert evaluator.run_arms(snapshot) == evaluator.LEGACY_ARMS
    failure, success = evaluator.tool(legacy, "kg", ["read", "atlas-vault/01-planning-meeting.md"])
    assert not success and "Legacy" in failure["message"]
    perfect = _perfect_answers(evaluator, legacy)
    for arm in evaluator.LEGACY_ARMS:
        (legacy / f"answers-{arm}.json").write_text(json.dumps(perfect))
        assert evaluator.tool(legacy, arm, ["submit"])[1]
    scores = evaluator.score(legacy)
    assert set(scores["arms"]) == set(evaluator.LEGACY_ARMS)
    assert all(arm["passed"] == 10 for arm in scores["arms"].values())
    assert not scores["shared_document_access"]


@pytest.mark.functional
def test_replicate_summary_validates_inputs_and_submissions(
    evaluator: ModuleType, tmp_path: Path,
) -> None:
    runs = [tmp_path / f"run-{index}" for index in (1, 2)]
    for index, run in enumerate(runs, 1):
        evaluator.prepare(run, replicate=index)
        answers = _perfect_answers(evaluator, run)
        for arm in evaluator.ARMS:
            (run / f"answers-{arm}.json").write_text(json.dumps(answers))
            assert evaluator.tool(run, arm, ["submit"])[1]
    summary = evaluator.summarize(runs)
    assert summary["replicates_per_arm"] == 2
    assert summary["arms"]["index"]["passed"] == {
        "observations": [10, 10], "mean": 10, "median": 10,
    }
    assert not any((run / "scores.json").exists() for run in runs)
    with pytest.raises(ValueError, match="same run"):
        evaluator.summarize([runs[0], runs[0]])
    changed = json.loads((runs[1] / "snapshot.json").read_text())
    changed["response_limit_bytes"] = 12000
    (runs[1] / "snapshot.json").write_text(json.dumps(changed))
    with pytest.raises(ValueError, match="identical"):
        evaluator.summarize(runs)
    changed["response_limit_bytes"] = None
    (runs[1] / "snapshot.json").write_text(json.dumps(changed))
    (runs[1] / "answers-index.json").write_text(
        (runs[1] / "answers-index.json").read_text() + "\n",
    )
    with pytest.raises(ValueError, match="changed after submission"):
        evaluator.summarize(runs)


@pytest.mark.functional
def test_document_tools_and_scoring_preserve_exact_newlines(
    evaluator: ModuleType, tmp_path: Path,
) -> None:
    inputs = tmp_path / "inputs"
    inputs.mkdir()
    raw = "# Note\r\n\r\nFirst\u2028second.\r\nNext line.\r\n"
    (inputs / "note.md").write_bytes(raw.encode())
    (inputs / "corpus.yml").write_text(json.dumps({
        "corpus_id": "newlines", "display_name": "Newlines",
        "vault_root": ".", "database": "index.sqlite3", "include": ["*.md"],
    }))
    (inputs / "questions.json").write_text(json.dumps({
        "questions": [{"id": "q1", "question": "Read the note"}],
    }))
    (inputs / "gold.json").write_text(json.dumps({
        "answers": {"q1": {"facts": {"read": True}, "abstains": False,
                           "evidence": [{"sources": ["note.md"], "contains": ["First"]}]}},
    }))
    run = tmp_path / "run"
    evaluator.prepare(run, inputs / "corpus.yml", inputs / "questions.json", inputs / "gold.json")
    for arm in evaluator.ARMS:
        read, success = evaluator.tool(run, arm, ["read", "note.md"])
        assert success and read["documents"][0]["text"] == raw
        matches, success = evaluator.tool(run, arm, ["grep", "First\u2028second"])
        assert success and len(matches["matches"]) == 1
        match = matches["matches"][0]
        assert match["start_line"] == 2 and match["end_line"] == 4
        assert match["text"] in raw and "\r\n" in match["text"]
        answers = {"answers": [{
            "id": "q1", "answer": "Read.", "facts": {"read": True}, "abstains": False,
            "citations": [{"source_path": "note.md", "quote": raw}],
        }]}
        (run / f"answers-{arm}.json").write_text(json.dumps(answers))
        assert evaluator.tool(run, arm, ["submit"])[1]
    assert all(arm["passed"] == 1 for arm in evaluator.score(run)["arms"].values())


@pytest.mark.functional
def test_administrative_cli_returns_summaries_not_full_snapshots_or_gold(
    evaluator: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    run = tmp_path / "cli-run"
    monkeypatch.setattr(evaluator.sys, "argv", [
        "evaluate.py", "prepare", "--output", str(run),
        "--arms", "index", "kg", "--replicate", "2",
    ])
    evaluator.main()
    prepared = json.loads(capsys.readouterr().out)
    assert prepared["arms"] == ["index", "kg"] and prepared["replicate"] == 2
    assert prepared["documents"] == 12 and "sources" not in prepared
    answers = _perfect_answers(evaluator, run)
    for arm in prepared["arms"]:
        (run / f"answers-{arm}.json").write_text(json.dumps(answers))
        assert evaluator.tool(run, arm, ["submit"])[1]
    monkeypatch.setattr(evaluator.sys, "argv", ["evaluate.py", "score", "--run", str(run)])
    evaluator.main()
    scored = json.loads(capsys.readouterr().out)
    assert all(arm["passed"] == 10 and "scores" not in arm for arm in scored["arms"].values())
    assert "expected_facts" not in json.dumps(scored)
    assert Path(scored["scores_file"]).is_file()


@pytest.mark.functional
@pytest.mark.parametrize(
    ("field", "value"),
    [("arms", None), ("shared_document_access", False), ("retrieval", "hybrid"), ("replicate", 0)],
)
def test_invalid_v3_policy_is_not_silently_treated_as_legacy(
    evaluator: ModuleType, run: Path, field: str, value: object,
) -> None:
    snapshot = json.loads((run / "snapshot.json").read_text())
    if field == "arms":
        del snapshot[field]
    else:
        snapshot[field] = value
    (run / "snapshot.json").write_text(json.dumps(snapshot))
    with pytest.raises(ValueError):
        evaluator.verify_snapshot(run)
