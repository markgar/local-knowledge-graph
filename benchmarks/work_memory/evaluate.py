from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import re
import shutil
import subprocess
import sys
import uuid
from collections import Counter
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter
from typing import Any

from kg.config import load_manifest, select_sources
from kg.db import Database
from kg.ingest import IngestService

HERE = Path(__file__).resolve().parent
REPOSITORY = HERE.parents[1]
KG_COMMANDS = {
    "search", "status", "actions", "evidence", "source-range", "source-context",
    "record-state", "revisions", "compare-revisions",
}
ARMS = ("kg", "markdown")


def _json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def prepare(
    output: Path,
    manifest_path: Path = REPOSITORY / "corpora/atlas-state.yml",
    questions_path: Path = HERE / "questions.json",
    gold_path: Path = HERE / "gold.json",
    *,
    response_limit_bytes: int | None = None,
) -> dict[str, Any]:
    if response_limit_bytes is not None and response_limit_bytes < 4000:
        raise ValueError("Response limit must be at least 4000 bytes")
    questions = _json(questions_path)["questions"]
    question_ids = [item["id"] for item in questions]
    if len(set(question_ids)) != len(question_ids) or set(question_ids) != set(
        _json(gold_path)["answers"]
    ):
        raise ValueError("Questions and gold must contain identical, unique question IDs")
    source = load_manifest(manifest_path)
    selection = select_sources(source)
    if selection.missing:
        raise ValueError(f"Missing fixture sources: {selection.missing}")
    output.mkdir(parents=True, exist_ok=False)
    sources = {}
    for path in selection.paths:
        relative = path.relative_to(source.vault_root).as_posix()
        destination = output / "notes" / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, destination)
        sources[relative] = {"sha256": _hash(destination), "bytes": destination.stat().st_size}
    shutil.copyfile(questions_path, output / "questions.json")
    shutil.copyfile(gold_path, output / "gold.json")
    manifest = source.model_dump(mode="json")
    manifest["vault_root"] = "notes"
    manifest["database"] = "index.sqlite3"
    _write_json(output / "corpus.yml", manifest)
    snapshot = {
        "version": 2,
        "run_id": str(uuid.uuid4()),
        "created_at": datetime.now(UTC).isoformat(),
        "sources": sources,
        "questions_sha256": _hash(output / "questions.json"),
        "gold_sha256": _hash(output / "gold.json"),
        "gold_file": "gold.json",
        "response_limit_bytes": response_limit_bytes,
        "manifest_sha256": _hash(output / "corpus.yml"),
        "protocol": "Two fresh general-purpose agents using the same runtime defaults; "
                    "different data tools only. Protocol restriction, not a security sandbox.",
    }
    _write_json(output / "snapshot.json", snapshot)
    corpus = load_manifest(output / "corpus.yml")
    started = perf_counter()
    result = IngestService(Database(corpus.database)).ingest(corpus, explain=True)
    if result.failed:
        raise RuntimeError(f"Fixture ingestion failed: {result.errors}")
    _write_json(output / "ingestion.json", result.model_dump(mode="json", exclude_none=True))
    _write_json(output / "setup.json", {
        "ingestion_ms": (perf_counter() - started) * 1000,
        "included_in_query_tool_metrics": False,
    })
    return snapshot


def verify_snapshot(run: Path) -> dict[str, Any]:
    snapshot = _json(run / "snapshot.json")
    for relative, expected in snapshot["sources"].items():
        if _hash(run / "notes" / relative) != expected["sha256"]:
            raise ValueError(f"Frozen source changed: {relative}")
    for filename, key in (
        ("questions.json", "questions_sha256"), ("corpus.yml", "manifest_sha256"),
    ):
        if _hash(run / filename) != snapshot[key]:
            raise ValueError(f"Frozen input changed: {filename}")
    return snapshot


def _answers(run: Path, arm: str) -> list[dict[str, Any]]:
    payload = _json(run / f"answers-{arm}.json")
    answers = payload["answers"]
    expected = {item["id"] for item in _json(run / "questions.json")["questions"]}
    if len(answers) != len(expected) or {item["id"] for item in answers} != expected:
        raise ValueError("Submit exactly one answer for each question")
    for answer in answers:
        if (
            not isinstance(answer.get("answer"), str) or not answer["answer"].strip()
            or not isinstance(answer.get("facts"), dict)
            or not isinstance(answer.get("abstains"), bool)
            or not isinstance(answer.get("citations"), list)
        ):
            raise ValueError(f"Invalid answer shape: {answer['id']}")
    return answers


class ToolArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise ValueError(message)


def _page_options(args: list[str], *, response: bool = False) -> argparse.Namespace:
    parser = ToolArgumentParser(add_help=False)
    if response:
        parser.add_argument("response_id")
        parser.add_argument("--field", default="")
    else:
        parser.add_argument("catalog", choices=["sources", "entities"])
        parser.add_argument("pattern", nargs="?", default=".")
    parser.add_argument("--offset", type=int, default=0)
    parser.add_argument("--limit", type=int, default=20)
    options = parser.parse_args(args)
    if options.offset < 0 or not 1 <= options.limit <= 2000:
        raise ValueError("offset must be nonnegative and limit must be between 1 and 2000")
    return options


def _page(value: Any, offset: int, limit: int) -> Any:
    if not isinstance(value, (list, str)):
        if offset:
            raise ValueError("Only lists and strings accept an offset")
        return value
    end = min(len(value), offset + limit)
    return {
        "text" if isinstance(value, str) else "items": value[offset:end],
        "total": len(value), "offset": offset,
        "next_offset": end if end < len(value) else None,
    }


def _bound_response(
    run: Path, arm: str, result: Any, snapshot: dict[str, Any],
) -> Any:
    limit = snapshot.get("response_limit_bytes")
    encoded = json.dumps(result, ensure_ascii=False).encode("utf-8")
    if limit is None or len(encoded) <= limit:
        return result
    response_id = hashlib.sha256(encoded).hexdigest()
    directory = run / "responses" / arm
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{response_id}.json").write_bytes(encoded)
    fields = [
        {"pointer": "/" + key.replace("~", "~0").replace("/", "~1"),
         "type": type(value).__name__,
         "length": len(value) if isinstance(value, (dict, list, str)) else None}
        for key, value in result.items()
    ] if isinstance(result, dict) else []
    # Field catalogs can themselves be large; the original object is never discarded.
    envelope = {
        "paged_response": True, "response_id": response_id,
        "full_response_utf8_bytes": len(encoded), "type": type(result).__name__,
        "length": len(result) if isinstance(result, (dict, list, str)) else None,
        "fields": fields[:10], "field_count": len(fields),
        "instructions": (
            "Full result is saved, not silently truncated. Use response ID --field /FIELD "
            "--offset N --limit N. Field is a JSON pointer; /items/0/text selects nested text. "
            "Lists/strings return slices with total and next_offset; "
            "strings use character offsets. "
            "Use response-fields ID to list all top-level field names."
        ),
    }
    if len(json.dumps(envelope, ensure_ascii=False).encode("utf-8")) > limit:
        envelope["fields"] = []
    return envelope


def _perform(
    run: Path, arm: str, args: list[str], snapshot: dict[str, Any], execution: dict[str, Any],
) -> Any:
    if not args:
        raise ValueError("A tool action is required")
    command, *rest = args
    if command == "catalog":
        options = _page_options(rest)
        expression = re.compile(options.pattern, re.IGNORECASE)
        items = (
            sorted(snapshot["sources"]) if options.catalog == "sources"
            else _json(run / "corpus.yml")["seed_entities"]
        )
        execution["backend"] = "catalog"
        return _page(
            [item for item in items if expression.search(json.dumps(item, ensure_ascii=False))],
            options.offset, options.limit,
        )
    if command in {"response", "response-fields"}:
        options = _page_options(rest, response=True)
        if not re.fullmatch(r"[0-9a-f]{64}", options.response_id):
            raise ValueError("Invalid response ID")
        encoded = (run / "responses" / arm / f"{options.response_id}.json").read_bytes()
        if hashlib.sha256(encoded).hexdigest() != options.response_id:
            raise ValueError("Stored response changed")
        value = json.loads(encoded)
        if options.field:
            if not options.field.startswith("/"):
                raise ValueError("field must be a JSON pointer starting with /")
            for part in options.field[1:].split("/"):
                key = part.replace("~1", "/").replace("~0", "~")
                if isinstance(value, list):
                    if not key.isdecimal() or int(key) >= len(value):
                        raise ValueError("List pointer index is out of range")
                    value = value[int(key)]
                elif isinstance(value, dict):
                    value = value[key]
                else:
                    raise ValueError("Pointer does not address a container")
        if command == "response-fields":
            if not isinstance(value, dict):
                raise ValueError("response-fields requires an object")
            value = list(value)
        execution.update({"backend": "response_cache", "response_id": options.response_id})
        return _page(value, options.offset, options.limit)
    if command == "info":
        source_paths = sorted(snapshot["sources"])
        entities = _json(run / "corpus.yml")["seed_entities"]
        page_size = 20 if snapshot.get("response_limit_bytes") else max(
            len(source_paths), len(entities)
        )
        return {
            **_json(run / "questions.json"),
            "source_paths": source_paths[:page_size],
            "source_count": len(source_paths),
            "seed_entities": entities[:page_size],
            "seed_entity_count": len(entities),
            "catalogs_complete": len(source_paths) <= page_size and len(entities) <= page_size,
            "shared_tools": [
                "catalog sources|entities [REGEX] [--offset N] [--limit N]",
                "response ID [--field /JSON/POINTER] [--offset N] [--limit N]",
                "response-fields ID [--field /JSON/POINTER] [--offset N] [--limit N]",
            ],
            "response_limit_bytes": snapshot.get("response_limit_bytes"),
            "arm": arm,
            "execution_rules": [
                "Run one wrapper invocation per tool call; do not chain commands or pipe stdout.",
                "This avoids combined-output truncation and makes delivery failures observable.",
                "Do not rerun a successful query just to reformat its output.",
            ],
            "tools": (
                [
                    "status SUBJECT",
                    "actions SUBJECT [--status open|completed]",
                    "search QUERY [--subject SUBJECT] [--query-mode strict|natural]",
                    "search QUERY --explain [--include-quotes] (diagnostic trace)",
                    "evidence RECORD_ID",
                    "source-range ANCHOR_ID",
                    "source-context ANCHOR_ID",
                    "record-state [--include-quotes] (current record supersession audit)",
                ] if arm == "kg" else [
                    "read PATH [PATH ...] (batch reading all documents is allowed)",
                    "search REGEX (case-insensitive, with one line of surrounding context)",
                ]
            ),
            "submission": f"Write answers-{arm}.json in the run directory, then call submit. "
                          "No gold feedback is returned. Do not read files outside these tools.",
        }
    if command == "submit":
        answers = _answers(run, arm)
        return {
            "submitted": len(answers), "answers_sha256": _hash(run / f"answers-{arm}.json"),
            "scoring": "withheld until both arms have submitted",
        }
    if arm == "kg":
        if command not in KG_COMMANDS or any(
            arg.split("=")[0] in {"--manifest", "--format"} for arg in rest
        ):
            raise ValueError("Only the frozen corpus and read-only KG commands are allowed")
        for index, arg in enumerate(rest):
            if arg == "--query-mode" and (
                index + 1 >= len(rest) or rest[index + 1] not in {"strict", "natural"}
            ):
                raise ValueError("This comparison uses lexical retrieval only")
            if (
                arg.startswith("--query-mode=")
                and arg.split("=", 1)[1] not in {"strict", "natural"}
            ):
                raise ValueError("This comparison uses lexical retrieval only")
        argv = [
            sys.executable, "-m", "kg", command, *rest,
            "--manifest", str(run / "corpus.yml"), "--format", "json",
        ]
        execution.update({"backend": "kg_cli", "argv": argv})
        backend_started = perf_counter()
        process = subprocess.run(
            argv,
            capture_output=True, text=True, check=False,
        )
        execution.update({
            "exit_code": process.returncode,
            "elapsed_ms": (perf_counter() - backend_started) * 1000,
            "stdout_utf8_bytes": len(process.stdout.encode("utf-8")),
            "stderr_utf8_bytes": len(process.stderr.encode("utf-8")),
            "stderr": process.stderr,
        })
        if process.returncode:
            raise ValueError(process.stderr.strip() or process.stdout.strip())
        return json.loads(process.stdout)
    if command == "read":
        if not rest:
            raise ValueError("read requires one or more paths")
        if any(path not in snapshot["sources"] for path in rest):
            raise ValueError("Path is not in the frozen corpus")
        execution.update({"backend": "markdown", "document_read_calls": 0})
        documents = []
        for path in rest:
            text = (run / "notes" / path).read_text(encoding="utf-8")
            execution["document_read_calls"] += 1
            documents.append({"source_path": path, "text": text})
        return {"documents": documents}
    if command == "search" and len(rest) == 1:
        expression = re.compile(rest[0], re.IGNORECASE)
        matches = []
        execution.update({"backend": "markdown", "document_read_calls": 0})
        for path in sorted(snapshot["sources"]):
            lines = (run / "notes" / path).read_text(encoding="utf-8").splitlines()
            execution["document_read_calls"] += 1
            for index, line in enumerate(lines):
                if expression.search(line):
                    start, end = max(0, index - 1), min(len(lines), index + 2)
                    matches.append({
                        "source_path": path, "start_line": start + 1, "end_line": end,
                        "text": "\n".join(lines[start:end]),
                    })
        return {"matches": matches}
    raise ValueError(f"Unsupported Markdown operation: {command}")


def tool(
    run: Path,
    arm: str,
    args: list[str],
    *,
    emit: Callable[[str], None] | None = None,
) -> tuple[Any, bool]:
    if arm not in ARMS:
        raise ValueError("Unknown arm")
    started_at = datetime.now(UTC).isoformat()
    started = perf_counter()
    success = True
    execution: dict[str, Any] = {}
    run_id = None
    try:
        snapshot = verify_snapshot(run)
        run_id = snapshot.get("run_id")
        result = _perform(run, arm, args, snapshot, execution)
        verify_snapshot(run)
        result = _bound_response(run, arm, result, snapshot)
    except (ValueError, OSError, KeyError, TypeError, re.error) as exc:
        result = {"error": type(exc).__name__, "message": str(exc)}
        success = False
    encoded = json.dumps(result, ensure_ascii=False)
    operation_elapsed_ms = (perf_counter() - started) * 1000
    operation_success = success
    delivery_success = None
    delivery_error = None
    delivery_started = perf_counter()
    if emit is not None:
        try:
            emit(encoded)
            delivery_success = True
        except OSError as exc:
            success = False
            delivery_success = False
            delivery_error = f"{type(exc).__name__}: {exc}"
    entry = {
        "request_id": str(uuid.uuid4()),
        "run_id": run_id,
        "started_at": started_at,
        "completed_at": datetime.now(UTC).isoformat(),
        "arm": arm, "arguments": args, "success": success,
        "operation_success": operation_success,
        "delivery_success": delivery_success,
        "delivery_error": delivery_error,
        "operation_elapsed_ms": operation_elapsed_ms,
        "delivery_elapsed_ms": (perf_counter() - delivery_started) * 1000 if emit else None,
        "execution": execution,
        "elapsed_ms": (perf_counter() - started) * 1000,
        "returned_utf8_bytes": len(encoded.encode("utf-8")),
        "response_sha256": hashlib.sha256(encoded.encode("utf-8")).hexdigest(),
        "response_shape": {
            "type": type(result).__name__,
            "collection_counts": (
                {key: len(value) for key, value in result.items() if isinstance(value, list)}
                if isinstance(result, dict) else {"items": len(result)}
                if isinstance(result, list) else {}
            ),
        },
        "result": result,
    }
    with (run / f"tools-{arm}.jsonl").open("a", encoding="utf-8") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        stream.write(json.dumps(entry, ensure_ascii=False) + "\n")
        stream.flush()
    return result, success


def _score_answer(
    answer: dict[str, Any], expected: dict[str, Any], sources: dict[str, str],
) -> dict[str, Any]:
    citations = answer["citations"]
    valid = [
        isinstance(citation, dict)
        and isinstance(citation.get("source_path"), str)
        and citation.get("source_path") in sources
        and isinstance(citation.get("quote"), str)
        and bool(citation["quote"].strip())
        and citation["quote"] in sources[citation["source_path"]]
        for citation in citations
    ]
    covered = [
        any(
            is_valid and citation["source_path"] in rule["sources"]
            and all(
                needle.casefold() in citation["quote"].casefold() for needle in rule["contains"]
            )
            for citation, is_valid in zip(citations, valid, strict=True)
        )
        for rule in expected["evidence"]
    ]
    facts_correct = json.dumps(answer["facts"], sort_keys=True) == json.dumps(
        expected["facts"], sort_keys=True
    )
    abstention_correct = answer["abstains"] is expected["abstains"]
    citations_correct = bool(citations) and all(valid) and all(covered)
    return {
        "id": answer["id"],
        "facts_correct": facts_correct,
        "abstention_correct": abstention_correct,
        "citations_valid": bool(citations) and all(valid),
        "evidence_rules_covered": sum(covered),
        "evidence_rules_total": len(covered),
        "missing_evidence_rules": [
            rule for rule, found in zip(expected["evidence"], covered, strict=True) if not found
        ],
        "invalid_citations": [
            citation for citation, found in zip(citations, valid, strict=True) if not found
        ],
        "passed": facts_correct and abstention_correct and citations_correct,
        "expected_facts": expected["facts"],
        "actual_facts": answer["facts"],
    }


def score(run: Path) -> dict[str, Any]:
    snapshot = verify_snapshot(run)
    gold_path = run / "gold.json" if snapshot.get("gold_file") else HERE / "gold.json"
    if _hash(gold_path) != snapshot["gold_sha256"]:
        raise ValueError("Gold changed after preparation; create a new comparison")
    gold = _json(gold_path)
    sources = {
        path: (run / "notes" / path).read_text(encoding="utf-8")
        for path in snapshot["sources"]
    }
    arms = {}
    for arm in ARMS:
        answers = _answers(run, arm)
        journal = [
            json.loads(line)
            for line in (run / f"tools-{arm}.jsonl").read_text(encoding="utf-8").splitlines()
        ]
        submissions = [
            event for event in journal
            if event["arguments"] == ["submit"] and event["success"]
        ]
        if not submissions or submissions[-1]["result"]["answers_sha256"] != _hash(
            run / f"answers-{arm}.json"
        ):
            raise ValueError(f"{arm}: answers were not submitted or changed after submission")
        scores = [
            _score_answer(answer, gold["answers"][answer["id"]], sources)
            for answer in answers
        ]
        data_calls = [
            entry for entry in journal if entry["arguments"][:1] not in (["info"], ["submit"])
        ]
        arms[arm] = {
            "questions": len(scores),
            "passed": sum(item["passed"] for item in scores),
            "facts_correct": sum(item["facts_correct"] for item in scores),
            "valid_citations": sum(item["citations_valid"] for item in scores),
            "abstention_correct": sum(item["abstention_correct"] for item in scores),
            "data_tool_calls": len(data_calls),
            "failed_data_tool_calls": sum(not entry["success"] for entry in data_calls),
            "observed_delivery_failures": sum(
                entry.get("delivery_success") is False for entry in data_calls
            ),
            "data_response_utf8_bytes": sum(entry["returned_utf8_bytes"] for entry in data_calls),
            "data_tool_elapsed_ms": sum(entry["elapsed_ms"] for entry in data_calls),
            "operations": dict(Counter(
                entry["arguments"][0] if entry["arguments"] else "<missing>"
                for entry in data_calls
            )),
            "scores": scores,
        }
    result = {
        "benchmark_version": snapshot.get("version", 1),
        "response_limit_bytes": snapshot.get("response_limit_bytes"),
        "run": str(run),
        "source_documents": len(sources),
        "source_bytes": sum(value["bytes"] for value in snapshot["sources"].values()),
        "questions_sha256": snapshot["questions_sha256"],
        "gold_sha256": snapshot["gold_sha256"],
        "replicates_per_arm": 1,
        "agent_configuration": "general-purpose agent type and runtime defaults in both arms; "
                               "model IDs not independently verified by this harness",
        "limitations": [
            "Synthetic, structured corpus; correlated questions; one run per arm.",
            "No conclusion about real work data or semantic retrieval quality.",
            "Optional benchmark response paging is shared by both arms, not a native KG feature.",
            "Tool elapsed time excludes model reasoning; response bytes are not token usage.",
            "Response bytes count complete serialized payloads, not successful stdout delivery "
            "or model-visible context; tool hosts can still truncate successful stdout writes.",
            "Data tool metrics exclude shared info/submission calls and one-time KG ingestion.",
            "Source restrictions are agent protocol instructions, not a filesystem sandbox.",
            "Natural-language answers require separate review for unsupported extra claims.",
        ],
        "setup": _json(run / "setup.json"),
        "arms": arms,
    }
    _write_json(run / "scores.json", result)
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="Frozen KG versus Markdown agent comparison.")
    subparsers = parser.add_subparsers(dest="operation", required=True)
    prepare_parser = subparsers.add_parser("prepare")
    prepare_parser.add_argument("--output", required=True, type=Path)
    prepare_parser.add_argument(
        "--manifest", type=Path, default=REPOSITORY / "corpora/atlas-state.yml",
    )
    prepare_parser.add_argument("--questions", type=Path, default=HERE / "questions.json")
    prepare_parser.add_argument("--gold", type=Path, default=HERE / "gold.json")
    prepare_parser.add_argument("--response-limit-bytes", type=int)
    tool_parser = subparsers.add_parser("tool")
    tool_parser.add_argument("--run", required=True, type=Path)
    tool_parser.add_argument("--arm", choices=ARMS, required=True)
    tool_parser.add_argument("arguments", nargs=argparse.REMAINDER)
    score_parser = subparsers.add_parser("score")
    score_parser.add_argument("--run", required=True, type=Path)
    args = parser.parse_args()
    if args.operation == "prepare":
        result = prepare(
            args.output, args.manifest, args.questions, args.gold,
            response_limit_bytes=args.response_limit_bytes,
        )
    elif args.operation == "score":
        result = score(args.run)
    else:
        arguments = args.arguments[1:] if args.arguments[:1] == ["--"] else args.arguments
        result, success = tool(
            args.run, args.arm, arguments, emit=lambda text: print(text, flush=True)
        )
        raise SystemExit(0 if success else 1)
    print(json.dumps(result, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
