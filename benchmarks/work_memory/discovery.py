"""Build a private recognition gold pack without copying or changing the source vault.

Usage::

    python benchmarks/work_memory/discovery.py \
        --inputs .kg/interleaved-input-v1 --output .kg/discovery-questions

Only questions.json is public. gold.json and metadata.json must remain outside
the agent-visible vault. Return the original corpus.yml to the comparison
harness; the approved catalog is shared unchanged, not narrowed to the answers.
This is project recognition, not event-date filtering or follow-up arithmetic.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from kg.config import load_manifest, select_sources
from kg.markdown import parse_markdown

HERE = Path(__file__).resolve().parent
SPEC_PATH = HERE / "discovery_questions.private.json"
REFERENCE_DATE = date(2026, 9, 18)
FACTS_SCHEMA = {"project": "Canonical project name as a string, or null."}
LIMITATIONS = [
    "Synthetic authored episodes amid deterministic template background, not real personal memory.",
    "Recognition of configured projects is tested; discovery of new entity names is not.",
    "Only ten prompts and one ambiguous exchange; not broad ambiguity calibration.",
    "Evidence alternatives are reviewed examples, not exhaustive relevance judgments.",
    "Project identity is scored objectively; the free-form explanation still needs human review.",
    "Approximate memory dates are clues, never hard source-date exclusions.",
]


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _source_path(document: dict[str, Any], seed: int) -> str:
    # Resolve the existing v1 generator's private ID; never regenerate source text.
    opaque = _sha256(f"mixed-work-v1:{seed}:{document['id']}".encode())[:20]
    day = date.fromisoformat(document["date"])
    return f"{document['channel']}/{day:%Y-%m}/{day:%Y%m%d}-{opaque}.md"


def _strings(value: Any) -> list[str]:
    """Include recursive object keys: schema keys can leak an answer too."""
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [
            text for key, child in value.items()
            for text in [str(key), *_strings(child)]
        ]
    if isinstance(value, list):
        return [text for child in value for text in _strings(child)]
    return []


def _validate_public(
    public: dict[str, Any], spec: dict[str, Any], scenario: dict[str, Any],
) -> None:
    forbidden = [
        *scenario["systems"],
        *(p["name"] for p in scenario["projects"]),
        *(alias for p in scenario["projects"] for alias in p.get("aliases", [])),
        *(q["id"] for q in scenario["questions"]),
        *(d["id"] for d in scenario["documents"]),
        *spec["forbidden_diagnostic_terms"],
    ]
    for text in _strings(public):
        for term in forbidden:
            if re.search(
                rf"(?<![a-z0-9]){re.escape(term.casefold())}(?![a-z0-9])",
                text.casefold(),
            ):
                raise ValueError(f"Public question pack leaks a private clue: {term}")
    for index, question in enumerate(public["questions"], 1):
        if question["id"] != f"q{index:02}":
            raise ValueError("Discovery question IDs must be neutral and sequential")
        if question["facts_schema"] != FACTS_SCHEMA:
            raise ValueError("All discovery questions must use the same generic facts schema")
        if re.search(r"\d|\.md\b|\.csv\b", question["question"], re.IGNORECASE):
            raise ValueError("Question text must not disclose exact dates, batches or filenames")


def _memory_window(hint: str) -> list[str] | None:
    monday = REFERENCE_DATE - timedelta(days=REFERENCE_DATE.weekday())
    previous_month_end = REFERENCE_DATE.replace(day=1) - timedelta(days=1)
    windows = {
        "last week": (monday - timedelta(days=7), monday - timedelta(days=1)),
        "earlier this week": (monday, REFERENCE_DATE),
        "late last month": (previous_month_end.replace(day=21), previous_month_end),
        "a couple of weeks ago": (
            REFERENCE_DATE - timedelta(days=21), REFERENCE_DATE - timedelta(days=7),
        ),
    }
    if hint == "a while back":
        return None
    if hint not in windows:
        raise ValueError(f"Unknown relative-time hint: {hint}")
    return [str(day) for day in windows[hint]]


def _validate_spec(
    spec: dict[str, Any], scenario: dict[str, Any], configured_projects: set[str],
) -> None:
    if spec["reference_date"] != str(REFERENCE_DATE):
        raise ValueError("Discovery reference date must stay frozen")
    questions = spec["questions"]
    if not 8 <= len(questions) <= 12:
        raise ValueError("Discovery pack requires 8-12 questions")
    originals = {q["id"] for q in scenario["questions"]}
    documents = {d["id"]: d for d in scenario["documents"]}
    ambiguous = imperfect = 0
    for question in questions:
        project = question["project"]
        candidates = question.get("candidate_projects", [])
        if project is None:
            ambiguous += 1
            if len(set(candidates)) < 2:
                raise ValueError("Ambiguity requires multiple supported candidate projects")
        expected = {project} if project is not None else set(candidates)
        if not expected <= configured_projects:
            raise ValueError("Every expected project must be configured in the original manifest")
        if not question["original_cases"] or not set(question["original_cases"]) <= originals:
            raise ValueError("Private discovery mapping names an unknown original case")
        if not question["rationale"] or not question["evidence"]:
            raise ValueError("Each discovery question requires a rationale and source evidence")
        memory = question["memory"]
        if memory["hint"] not in question["question"]:
            raise ValueError("Private memory hint must appear in its question")
        if memory["window"] != _memory_window(memory["hint"]):
            raise ValueError("Relative-time window disagrees with the frozen reference date")
        source_ids = {s for rule in question["evidence"] for s in rule["sources"]}
        if not source_ids <= documents.keys():
            raise ValueError("Unknown authored evidence document")
        dates = memory["episode_dates"]
        if not dates or not set(dates) <= {documents[s]["date"] for s in source_ids}:
            raise ValueError("Memory dates must be grounded in selected episode sources")
        if memory["imperfect"]:
            imperfect += 1
            window = memory["window"]
            if window is None or all(window[0] <= day <= window[1] for day in dates):
                raise ValueError("Imperfect memory must actually disagree with the episode date")
    if not ambiguous or not imperfect:
        raise ValueError("Discovery requires ambiguity and imperfect-memory coverage")


def build(inputs: Path, output: Path) -> dict[str, Any]:
    """Read frozen interleaved inputs and write a new, separate question/gold pack."""
    inputs, output = Path(inputs).resolve(), Path(output).resolve()
    if output.exists():
        raise FileExistsError(f"Refusing to overwrite discovery output: {output}")
    manifest_path = inputs / "corpus.yml"
    manifest = load_manifest(manifest_path)
    if output.is_relative_to(inputs) or output.is_relative_to(manifest.vault_root):
        raise ValueError("Discovery output must be outside the input corpus and source vault")
    metadata = _read_json(inputs / "metadata.json")
    if metadata["generator"] != "benchmarks/work_memory/interleaved.py":
        raise ValueError("Discovery requires rendered interleaved v1 inputs")
    scenario = _read_json(HERE / "interleaved_scenarios.json")
    spec = _read_json(SPEC_PATH)
    _validate_spec(spec, scenario, {
        entity.name for entity in manifest.seed_entities if entity.entity_type == "project"
    })

    selected = select_sources(manifest)
    paths = {p.relative_to(manifest.vault_root).as_posix(): p for p in selected.paths}
    if selected.missing or set(paths) != set(metadata["sources"]):
        raise ValueError("Selected source inventory differs from the frozen input metadata")
    fingerprints = {}
    for relative, path in sorted(paths.items()):
        content = path.read_bytes()
        fingerprint = {"sha256": _sha256(content), "bytes": len(content)}
        expected = metadata["sources"][relative]
        if any(fingerprint[key] != expected[key] for key in fingerprint):
            raise ValueError(f"Source bytes differ from the frozen input: {relative}")
        fingerprints[relative] = fingerprint

    documents = {d["id"]: d for d in scenario["documents"]}
    source_map = {
        identifier: _source_path(document, metadata["seed"])
        for identifier, document in documents.items()
    }
    public = {
        "version": 1,
        "reference_date": str(REFERENCE_DATE),
        "scenario": (
            f"Recall work from partial memories. The frozen reference date is {REFERENCE_DATE}; "
            "never use the actual current date. Identify one primary project per question "
            "using the supplied records and the shared approved entity catalog."
        ),
        "conventions": [
            "Last week means the previous Monday through Sunday; earlier this week means "
            "Monday through the frozen reference date. Late last month means its final third. "
            "A couple of weeks ago is approximate, roughly one to three weeks earlier.",
            "People's remembered timing can be wrong. Treat all time hints as soft clues, "
            "not hard filters; widen the search when the activity fits a different date.",
            "For every question, use null for project and abstains=true when the evidence "
            "does not identify one project. Explain competing possibilities with citations "
            "rather than guessing. Otherwise use the canonical project name and abstains=false.",
            "Recognize the work, not every follow-up detail. Do not calculate results or "
            "reconstruct owners, deadlines or full histories unless needed to explain identity.",
        ],
        "answer_format": {
            "answers": [{
                "id": "Question ID",
                "answer": "Brief explanation of the identification or uncertainty.",
                "facts": FACTS_SCHEMA,
                "abstains": "Boolean: true if one project cannot be identified.",
                "citations": [{
                    "source_path": "Exact source path returned by tools",
                    "quote": "Exact contiguous source excerpt",
                }],
            }],
        },
        "questions": [
            {"id": q["id"], "question": q["question"], "facts_schema": FACTS_SCHEMA}
            for q in spec["questions"]
        ],
    }
    _validate_public(public, spec, scenario)
    anchors: dict[str, list[str]] = {}
    answers = {}
    for question in spec["questions"]:
        evidence = []
        for rule in question["evidence"]:
            if not rule["sources"] or not rule["contains"] or not all(rule["contains"]):
                raise ValueError("Discovery evidence rules must be nonempty")
            sources = [source_map[identifier] for identifier in rule["sources"]]
            for relative in sources:
                if relative not in paths or not metadata["sources"][relative]["authored"]:
                    raise ValueError(f"Missing authored source: {relative}")
                if relative not in anchors:
                    anchors[relative] = [
                        anchor.quote for anchor in parse_markdown(
                            paths[relative].read_text(encoding="utf-8"), relative,
                        ).anchors
                    ]
                if not any(
                    all(needle in quote for needle in rule["contains"])
                    for quote in anchors[relative]
                ):
                    raise ValueError(
                        f"Evidence must fit one exact parsed anchor: {question['id']}: {relative}"
                    )
            evidence.append({"sources": sources, "contains": rule["contains"]})
        answers[question["id"]] = {
            "facts": {"project": question["project"]},
            "abstains": question["project"] is None,
            "evidence": evidence,
        }
    gold = {
        "version": 1,
        "answers": answers,
        "scoring_notes": [
            "Expected identities are authored from source truth, never from retrieval output.",
            "Each evidence rule accepts any listed source; every needle must fit one anchor.",
            "Only identity is scored as a fact; do not demand the original direct-task details.",
            "A different cohort can support the same identity when the memory omits the cohort.",
        ],
    }
    summary = {
        "documents": len(paths),
        "source_utf8_bytes": sum(item["bytes"] for item in fingerprints.values()),
        "questions": len(answers),
        "projects_in_questions": len({q["project"] for q in spec["questions"]} - {None}),
        "ambiguous_questions": sum(q["project"] is None for q in spec["questions"]),
        "imperfect_memory_questions": sum(q["memory"]["imperfect"] for q in spec["questions"]),
        "reference_date": str(REFERENCE_DATE),
    }
    private = {
        "version": 1,
        "generator": "benchmarks/work_memory/discovery.py",
        "reference_date": str(REFERENCE_DATE),
        "summary": summary,
        "limitations": LIMITATIONS,
        "input_sha256": {
            name: _sha256((inputs / name).read_bytes())
            for name in ("corpus.yml", "questions.json", "gold.json", "metadata.json")
        },
        "sources": fingerprints,
        "question_coverage": {
            q["id"]: {
                key: q[key] for key in (
                    "original_cases", "memory", "rationale", "candidate_projects",
                ) if key in q
            }
            for q in spec["questions"]
        },
    }
    output.mkdir(parents=True, exist_ok=False)
    _write_json(output / "questions.json", public)
    _write_json(output / "gold.json", gold)
    _write_json(output / "metadata.json", private)
    return {
        "manifest_path": manifest_path,
        "questions_path": output / "questions.json",
        "gold_path": output / "gold.json",
        "summary": summary,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inputs", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(build(args.inputs, args.output), indent=2, default=str))


if __name__ == "__main__":
    main()
