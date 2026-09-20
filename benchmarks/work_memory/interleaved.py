"""Generate raw, mixed-work documents without topic-organized paths or graph annotations."""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import re
from collections import Counter
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import yaml

HERE = Path(__file__).resolve().parent
START = date(2026, 1, 5)
END = date(2026, 9, 18)
CHANNELS = ("mail", "meetings", "notes")
REFERENCE = re.compile(r"\{\{ref:([^}]+)\}\}")
ANNOTATION = re.compile(r"\[\[|\[(?:key|supersedes|owner|due)::", re.IGNORECASE)
TOPICS = (
    ("access logs", "denied requests", "the local account cache"),
    ("calendar exports", "duplicate rows", "a timezone conversion"),
    ("signing tests", "expired staging tokens", "the test machine clock"),
    ("invoice imports", "unmatched line items", "a vendor-code mapping"),
    ("storage samples", "repeated object names", "the archive reader"),
    ("review packets", "missing page labels", "the PDF export settings"),
    ("dashboard queries", "empty result cells", "the report filter"),
    ("directory sync", "renamed accounts", "the identity adapter"),
)


def _write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _opaque(identifier: str, seed: int) -> str:
    return hashlib.sha256(f"mixed-work-v1:{seed}:{identifier}".encode()).hexdigest()[:20]


def _path(document: dict[str, Any], seed: int) -> str:
    day = date.fromisoformat(document["date"])
    return (
        f"{document['channel']}/{day:%Y-%m}/"
        f"{day:%Y%m%d}-{_opaque(document['id'], seed)}.md"
    )


def _message_id(identifier: str, seed: int) -> str:
    return f"<m-{_opaque(identifier, seed)}@work.example>"


def _expand(text: str, documents: dict[str, dict[str, Any]], seed: int) -> str:
    def replace(match: re.Match[str]) -> str:
        identifier = match[1]
        if identifier not in documents:
            raise ValueError(f"Unknown document reference: {identifier}")
        return _message_id(identifier, seed)

    return REFERENCE.sub(replace, text)


def _validate(scenario: dict[str, Any]) -> None:
    projects = [project["name"] for project in scenario["projects"]]
    if not 10 <= len(projects) <= 20 or len(set(projects)) != len(projects):
        raise ValueError("Scenario requires 10-20 distinct projects")
    if len(set(scenario["people"])) < 2 or not scenario["systems"]:
        raise ValueError("Scenario requires multiple people and at least one shared system")
    documents = {document["id"]: document for document in scenario["documents"]}
    if len(documents) != len(scenario["documents"]):
        raise ValueError("Document IDs must be unique")
    for identifier, document in documents.items():
        if document["channel"] not in CHANNELS:
            raise ValueError(f"Unknown channel: {identifier}")
        day = date.fromisoformat(document["date"])
        if not START <= day <= END:
            raise ValueError(f"Document outside scenario interval: {identifier}")
        if not set(document["projects"]).issubset(projects):
            raise ValueError(f"Unknown project in {identifier}")
        if ANNOTATION.search(document["body"]):
            raise ValueError(f"Artificial graph annotation in {identifier}")
        seen = {identifier}
        current = document
        while current.get("reply_to"):
            parent = current["reply_to"]
            if parent not in documents or parent in seen:
                raise ValueError(f"Missing or cyclic reply parent: {identifier}")
            if documents[parent]["date"] > current["date"]:
                raise ValueError(f"Reply precedes parent: {identifier}")
            seen.add(parent)
            current = documents[parent]
        _expand(document["body"], documents, 0)
    question_ids = [question["id"] for question in scenario["questions"]]
    if len(question_ids) != len(set(question_ids)) or set(question_ids) != set(
        scenario["answers"]
    ):
        raise ValueError("Questions and answers must have identical unique IDs")
    for question in scenario["questions"]:
        identifier = question["id"]
        if not set(question["projects"]).issubset(projects):
            raise ValueError(f"Unknown question project: {identifier}")
        evidence = scenario["answers"][identifier]["evidence"]
        if not evidence or any(not rule["sources"] or not rule["contains"] for rule in evidence):
            raise ValueError(f"Question needs nonempty evidence rules: {identifier}")
        references = {
            source for rule in evidence for source in rule["sources"]
        } | set(scenario["near_misses"].get(identifier, []))
        if not references.issubset(documents):
            raise ValueError(f"Unknown evidence or near-miss source: {identifier}")
        if len(set(scenario["near_misses"].get(identifier, []))) < 2:
            raise ValueError(f"Question needs at least two authored near misses: {identifier}")


def _background(scenario: dict[str, Any], count: int, seed: int) -> list[dict[str, Any]]:
    rng = random.Random(seed)
    projects = [project["name"] for project in scenario["projects"]]
    people = scenario["people"]
    systems = scenario["systems"]
    documents: list[dict[str, Any]] = []
    previous_mail: dict[str, str] = {}
    for index in range(count):
        day = START + timedelta(days=index * (END - START).days // max(1, count - 1))
        project = projects[index % len(projects)]
        sender, recipient = rng.sample(people, 2)
        system = systems[index % len(systems)]
        topic, observation, explanation = TOPICS[index % len(TOPICS)]
        work_item = _opaque(f"routine-observation-{index}", seed)[:8]
        sample = rng.randrange(24, 230)
        exceptions = rng.randrange(0, 12)
        channel = CHANNELS[index % len(CHANNELS)]
        identifier = f"background-{index}"
        members = [project]
        lead = (
            f"{sender} compared {sample} {topic} entries for {project} in {system}. "
            f"The sample, export {work_item}, contained {exceptions} {observation}. "
            f"The reproduction points to {explanation}; the attached worksheet preserves "
            "the original rows as well as the normalized output."
        )
        detail = (
            f"{recipient} read the counters from the same export rather than a dashboard "
            "screenshot. The notebook records the capture time and operator account. "
            "The notes below concern this particular sample; similarly named exports from "
            "other teams use separate input files."
        )
        genre = index % 8
        title = (
            "Sample follow-up", "Working session notes", "Export comparison",
            "Receipt details", "Weekly notes", "Office update", "Review copy", "Run notes",
        )[genre]
        if genre == 3:
            members = []
            body = (
                f"{sender} sent {recipient} the receipt for the office equipment delivery "
                f"on {day:%B %d}. Order {work_item} contains {sample} cable labels and "
                f"{exceptions} replacement adapters. The packing slip lists the quantities "
                "separately from the invoice total.\n\n"
                "The supplier combined two boxes onto one tracking entry. The receipt copy "
                "is useful for reconciling the statement, but the tracking page no longer "
                "shows the individual box weights.\n\n"
                "The finance export uses an approval column for the expense workflow. "
                "The office delivery entry is grouped with facilities purchases."
            )
        elif genre == 5:
            members = []
            body = (
                f"The office bulletin forwarded by {sender} lists {sample} available places "
                f"across the September training sessions. {recipient} noticed {exceptions} "
                "duplicate calendar entries in the copied schedule.\n\n"
                "The bulletin covers room bookings, badge collection and travel desk hours. "
                "The afternoon sessions are recordings rather than live calls; the calendar "
                "copy retains the original speaker's timezone.\n\n"
                f"Reference {work_item} identifies this copy of the bulletin. Older copies "
                "have different room numbers, so the facilities desk keeps both versions "
                "alongside the building-access instructions."
            )
        elif channel == "meetings":
            members = [project, *rng.sample([p for p in projects if p != project], 2)]
            body = (
                f"Attendees: {sender}, {recipient}, {people[(index + 2) % len(people)]}.\n\n"
                f"## {members[0]}\n\n{lead}\n\n"
                f"## {members[1]}\n\nThe group compared the labels on a separate {topic} "
                f"worksheet, sample {work_item}-b. Its {sample + 7} rows were captured in "
                "a different environment and are not pooled with the first sample.\n\n"
                f"## {members[2]}\n\n{detail}"
            )
        elif genre == 6:
            body = (
                f"{lead}\n\n{detail}\n\n"
                "> Earlier copy: the exception column was empty in the first preview.\n\n"
                f"The attached export {work_item} is the populated copy. The forwarded "
                "preview omitted that column when the table was converted to an image."
            )
        elif genre == 7:
            body = (
                f"{lead}\n\n{detail}\n\n"
                "The runbook's example uses `approval=true` as a parser input. The field "
                "is part of a local serialization test, alongside null and missing values."
            )
        else:
            body = (
                f"{lead}\n\n{detail}\n\n"
                f"The {topic} archive retains export {work_item} with its checksum and "
                "capture log. The visible totals count input rows, not people or release "
                "readiness. Different filters can produce different totals from the same file."
            )
        document = {
            "id": identifier, "channel": channel, "date": str(day), "title": title,
            "projects": members, "sender": sender, "recipients": [recipient], "body": body,
        }
        if channel == "mail" and members:
            if index % 4 == 0 and project in previous_mail:
                document["reply_to"] = previous_mail[project]
                document["body"] = (
                    f"Thanks for the earlier sample. This is a separate export, {work_item}, "
                    "rather than a revision of those measurements.\n\n" + body
                )
            previous_mail[project] = identifier
        documents.append(document)
    return documents


def _render(document: dict[str, Any], documents: dict[str, dict[str, Any]], seed: int) -> str:
    title, day = document["title"], document["date"]
    frontmatter = yaml.safe_dump({"title": title, "date": day}, sort_keys=False).rstrip()
    header = f"# {title}\n\nRecorded: {day}.\n\n"
    if document["channel"] == "mail":
        header += (
            f"From: {document['sender']}\nTo: {', '.join(document['recipients'])}\n"
            f"Sent: {day}\nSubject: {title}\n"
            f"Message-ID: {_message_id(document['id'], seed)}\n"
        )
        if document.get("reply_to"):
            parent = document["reply_to"]
            ancestors = [parent]
            while documents[parent].get("reply_to"):
                parent = documents[parent]["reply_to"]
                ancestors.append(parent)
            header += f"In-Reply-To: {_message_id(document['reply_to'], seed)}\n"
            header += "References: " + " ".join(
                _message_id(identifier, seed) for identifier in reversed(ancestors)
            ) + "\n"
        header += "\n"
    else:
        header += f"Record-ID: {_message_id(document['id'], seed)}\n\n"
    body = _expand(document["body"], documents, seed)
    return f"---\n{frontmatter}\n---\n\n{header}{body.rstrip()}\n"


def build(
    output: Path, *, background_documents: int = 2000, seed: int = 17,
) -> dict[str, Any]:
    if background_documents < 0:
        raise ValueError("background_documents must be nonnegative")
    scenario = json.loads((HERE / "interleaved_scenarios.json").read_text(encoding="utf-8"))
    _validate(scenario)
    authored = scenario["documents"]
    background = _background(scenario, background_documents, seed)
    documents = {document["id"]: document for document in [*authored, *background]}
    if len(documents) != len(authored) + len(background):
        raise ValueError("Authored IDs conflict with generated background IDs")
    paths = {identifier: _path(document, seed) for identifier, document in documents.items()}
    if len(set(paths.values())) != len(paths):
        raise ValueError("Generated source path collision")
    rendered = {
        identifier: _render(document, documents, seed)
        for identifier, document in documents.items()
    }
    gold = {"version": 1, "answers": {}, "scoring_notes": [
        "Authored source truth is frozen before agent runs; not derived from KG output.",
        "This corpus has no perfect wikilinks or machine-readable supersession annotations.",
        "Private evidence membership is not exhaustive relevance labeling.",
        "Canonical facts and exact citations are scored separately; review prose claims too.",
    ]}
    for identifier, expected in scenario["answers"].items():
        rules = []
        for rule in expected["evidence"]:
            needles = [_expand(needle, documents, seed) for needle in rule["contains"]]
            if not any(
                all(needle.casefold() in rendered[source].casefold() for needle in needles)
                for source in rule["sources"]
            ):
                raise ValueError(f"Ungrounded gold evidence rule: {identifier}: {rule}")
            rules.append({"sources": [paths[source] for source in rule["sources"]],
                          "contains": needles})
        gold["answers"][identifier] = {**expected, "evidence": rules}
    questions = {
        "version": 1,
        "scenario": (
            f"Answer questions about mixed personal work using all supplied records through "
            f"{END}. Sources span multiple concurrent efforts. Paths reflect channel/date, "
            "not project membership. Do not use today's date to discard supplied records."
        ),
        "conventions": [
            "Use dated prose and reply context to distinguish changed commitments from old "
            "reports. A newer statement about a different work item is not a replacement.",
            "A document may cover several projects. Shared people, words or systems alone "
            "do not prove that a passage concerns the same commitment.",
            "Forwarded or quoted text may be stale. A proposed date or request is not approval.",
            "Preserve genuine ambiguity and distinguish missing confirmation from a negative fact.",
        ],
        "answer_format": json.loads((HERE / "questions.json").read_text())["answer_format"],
        "questions": [
            {key: value for key, value in question.items() if key != "projects"}
            for question in scenario["questions"]
        ],
    }
    entities = [
        {"entity_id": f"{kind}-{index}", "entity_type": kind,
         "name": item["name"] if isinstance(item, dict) else item,
         "aliases": item.get("aliases", []) if isinstance(item, dict) else []}
        for kind, items in (
            ("project", scenario["projects"]), ("person", scenario["people"]),
            ("system", scenario["systems"]),
        )
        for index, item in enumerate(items)
    ]
    manifest = {
        "corpus_id": "work-memory-interleaved", "display_name": "Mixed personal work history",
        "vault_root": "notes", "database": "index.sqlite3", "include": ["**/*.md"],
        "seed_entities": entities, "metadata_fields": {"event_time": "date"},
    }
    summary = {
        "documents": len(documents), "authored_documents": len(authored),
        "background_documents": len(background), "projects": len(scenario["projects"]),
        "questions": len(questions["questions"]),
        "cross_project_questions": sum(len(q["projects"]) > 1 for q in scenario["questions"]),
        "projects_in_questions": len({p for q in scenario["questions"] for p in q["projects"]}),
        "mixed_project_documents": sum(len(d["projects"]) > 1 for d in documents.values()),
        "reply_documents": sum(bool(d.get("reply_to")) for d in documents.values()),
        "source_utf8_bytes": sum(len(text.encode("utf-8")) for text in rendered.values()),
        "channels": dict(Counter(document["channel"] for document in documents.values())),
        "earliest_date": min(d["date"] for d in documents.values()),
        "latest_date": max(d["date"] for d in documents.values()),
    }
    coverage = {}
    for question in scenario["questions"]:
        identifier = question["id"]
        required = {s for r in scenario["answers"][identifier]["evidence"] for s in r["sources"]}
        coverage[identifier] = {
            "projects": question["projects"],
            "required_sources": sorted(paths[s] for s in required),
            "evidence_rules": len(scenario["answers"][identifier]["evidence"]),
            "near_misses": [paths[s] for s in scenario["near_misses"][identifier]],
            "not_in_required_evidence_fraction": 1 - len(required) / len(documents),
        }
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=False)
    sources = {}
    authored_ids = {document["id"] for document in authored}
    for identifier, text in rendered.items():
        path = output / "notes" / paths[identifier]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        sources[paths[identifier]] = {
            "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
            "bytes": len(text.encode("utf-8")),
            "projects": documents[identifier]["projects"],
            "authored": identifier in authored_ids,
        }
    outputs = {
        "manifest_path": output / "corpus.yml", "questions_path": output / "questions.json",
        "gold_path": output / "gold.json",
    }
    for key, value in (
        ("manifest_path", manifest), ("questions_path", questions), ("gold_path", gold),
    ):
        _write_json(outputs[key], value)
    _write_json(output / "metadata.json", {
        "version": 1, "generator": "benchmarks/work_memory/interleaved.py", "seed": seed,
        "summary": summary, "design": scenario["design"],
        "limitations": [
            *scenario["limitations"],
            "Background prose uses deterministic templates, not independent real workplace data.",
            "Approved entity names/aliases are supplied equally to both arms; "
            "entity discovery is not tested.",
            "Required-source coverage is not a claim that every other source is wholly irrelevant.",
            "Current KG has no general prose commitment extraction or email-thread resolver. "
            "Successful ingestion does not imply semantic extraction or benchmark success.",
        ],
        "question_coverage": coverage, "sources": sources,
    })
    return {**outputs, "summary": summary}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--background-documents", type=int, default=2000)
    parser.add_argument("--seed", type=int, default=17)
    args = parser.parse_args()
    print(json.dumps(build(
        args.output, background_documents=args.background_documents, seed=args.seed,
    ), indent=2, default=str))


if __name__ == "__main__":
    main()
