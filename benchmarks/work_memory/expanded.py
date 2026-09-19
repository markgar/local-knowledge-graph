"""Build a deterministic, fictional long-history work-memory benchmark.

Run ``uv run python benchmarks/work_memory/expanded.py --output .kg/expanded-input``.
The output directory must not exist. Pass the three returned ``*_path`` values to
the benchmark harness's ``prepare``; metadata.json is author-side documentation,
not an agent-visible source. No index, agent answers, or evaluation results are
generated here. All original twelve source files are copied byte-for-byte.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import yaml

HERE = Path(__file__).resolve().parent
REPOSITORY = HERE.parents[1]
PROJECTS = (
    "Atlas", "Atlascope", "Atlasbridge", "Atlasway", "Borealis", "Borealiscope",
    "Meridian", "MeridianOps", "Juniper", "JuniperCloud", "Harbor", "Harborview",
    "Keystone",
)
OWNERS = ("Priya", "Morgan", "Lena", "Diego", "Samira", "Lee")
TOPICS = (
    ("access inventory", "service-account permissions", "missing role annotations"),
    ("certificate provisioning", "staging signing requests", "renewal queue delays"),
    ("calendar reconciliation", "review invitation exports", "timezone conversion"),
    ("archive restore", "retained audit bundles", "duplicate object identifiers"),
    ("export validation", "timestamped JSON records", "truncated permission names"),
    ("deployment rehearsal", "rollback checkpoint logs", "out-of-order health probes"),
    ("review preparation", "operator evidence packets", "unsigned inventory attachments"),
    ("retention testing", "aged storage partitions", "late-arriving audit events"),
    ("handover review", "on-call runbook examples", "unmapped escalation contacts"),
)
TASK_LABEL = "Reconcile the historical permission ledger."
TASK_EVENTS = {
    0: ("2026-01-05", "Priya", "open", "2026-02-06", "The ledger reconciliation starts."),
    8: ("2026-03-02", "Priya", "completed", "2026-03-02",
        "The first sample reconciled; the checklist is explicitly completed."),
    16: ("2026-04-27", "Morgan", "open", "2026-06-12",
         "A replay exposed missing inherited roles; the completed task is reopened."),
    24: ("2026-06-22", "Lena", "open", "2026-08-14",
         "Morgan transferred the reopened reconciliation to Lena during the handover."),
    35: ("2026-09-07", "Lena", "open", "2026-10-09",
         "The remaining inherited-role rows need reconciliation; Lena retains ownership."),
}
LAUNCH_EVENTS = (
    ("2026-09-27", "2026-10-22", "October 22, 2026", "launch-oct15",
     "expanded-launch-oct22", "The certificate provisioning queue needs another week."),
    ("2026-10-02", "2026-11-05", "November 5, 2026", "expanded-launch-oct22",
     "expanded-launch-nov5", "A signing rehearsal failed validation of the archive bundle."),
    ("2026-10-08", "2026-11-12", "November 12, 2026", "expanded-launch-nov5",
     "expanded-launch-nov12", "The repeat rehearsal is scheduled after the maintenance window."),
)


def _json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _note(root: Path, relative: str, title: str, day: date | str, body: str) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"---\ntitle: {title}\ndate: {day}\n---\n\n# {title}\n\n{body.rstrip()}\n",
        encoding="utf-8",
    )


def _history_path(week: int) -> str:
    return f"history/atlas/{week + 1:02d}-working-record.md"


def _launch_path(index: int) -> str:
    return f"history/atlas/launch-{index + 1:02d}.md"


def _working_body(project: str, project_index: int, week: int) -> tuple[str, str]:
    topic, artifact, risk = TOPICS[(week + project_index) % len(TOPICS)]
    owner = OWNERS[(project_index + week // 3) % len(OWNERS)]
    partner = OWNERS[(project_index + week // 3 + 2) % len(OWNERS)]
    batch = f"{project.lower()}-{week + 1:02d}"
    samples = 40 + ((week * 13 + project_index * 17) % 91)
    exceptions = (week + project_index * 3) % 8
    region = ("east", "west", "central")[(week + project_index) % 3]
    context = (
        f"{project}'s {topic} work uses batch {batch} from the {region} staging environment. "
        f"{owner} inspected {samples} {artifact}; {exceptions} entries were set aside for "
        f"manual inspection because of {risk}. These are rehearsal observations, not a "
        "production approval or a change to another team's checklist.\n\n"
    )
    kind = ("meeting", "email", "decision", "action")[week % 4]
    bodies = {
        "meeting": (
            f"## Working session\n\n{owner} and {partner} compared the operator worksheet "
            f"with the raw {artifact}. The discussion separated reproducible failures from "
            "screenshots that lacked a batch identifier. The worksheet preserves the original "
            "timestamps so a later reviewer can repeat the comparison.\n\n"
            f"## Observations\n\nThe {region} run had {samples - exceptions} entries suitable "
            f"for comparison. The remaining {exceptions} are retained in the exception appendix, "
            f"rather than silently excluded from the {project} evidence packet."
        ),
        "email": (
            f"From: {owner}\nTo: {partner}\nSubject: {project} {topic} sample follow-up\n\n"
            f"The attached worksheet refers to {batch}, not the previous week's export. "
            "Please do not treat a forwarded copy as a signed approval. The attachment lists "
            "the inspected rows and the reason each exception was retained.\n\n"
            f"The {region} environment's evidence folder contains the raw export alongside "
            f"the comparison sheet. {partner} reported that the filenames were readable, but "
            f"that observation does not close an outstanding {project} action."
        ),
        "decision": (
            f"## Rationale\n\nFor {project}, preserving the raw {artifact} allows reviewers "
            f"to distinguish {risk} from changes introduced by the comparison tool. "
            "The summary worksheet alone cannot explain records that were omitted during "
            "normalization. Storage use was measured separately from review completeness.\n\n"
            f"The review packet identifies batch {batch}, the {region} environment, and the "
            "operator who exported it. Prior weekly notes remain useful historical evidence "
            "even when a later explicit record replaces their operating choice."
        ),
        "action": (
            f"## Handover notes\n\n{owner} walked {partner} through the {project} exception "
            f"appendix for {batch}. A useful replay includes the input checksum, the exporter "
            "version and the local timezone. The same filename can occur in different "
            "environments, so it is not sufficient to identify an evidence bundle.\n\n"
            f"The {samples - exceptions} comparable entries are kept separate from the "
            f"{exceptions} inspection cases. The handover records observations about "
            f"{topic}; any authoritative task update appears in the checklist below."
        ),
    }
    return kind, context + bodies[kind]


def _focal_history(notes: Path) -> None:
    previous_key = None
    for week in range(36):
        kind, body = _working_body("Atlas", 0, week)
        day = date(2026, 1, 5) + timedelta(days=7 * week)
        if week in TASK_EVENTS:
            event_day, owner, status, due, explanation = TASK_EVENTS[week]
            assert str(day) == event_day
            key = f"expanded-ledger-v{list(TASK_EVENTS).index(week) + 1}"
            replacement = f" [supersedes:: {previous_key}]" if previous_key else ""
            marker = "x" if status == "completed" else " "
            body += (
                f"\n\n## Reconciliation update\n\n{explanation}\n\n## Actions\n\n"
                f"- [{marker}] {TASK_LABEL} [owner:: {owner}] [due:: {due}] "
                f"[key:: {key}]{replacement}"
            )
            previous_key = key
        if week == 12:
            body += (
                "\n\n## Ledger replay measurement\n\n"
                "The March 30 Atlas ledger replay sampled 120 accounts and found 18 accounts "
                "missing inherited-role entries. The parser skipped inherited roles when a "
                "direct role had the same display name. This measurement concerns the ledger "
                "replay, not the routine weekly rehearsal batch."
            )
        if week == 30:
            body += (
                "\n\n## Ledger replay measurement\n\n"
                "The August 3 Atlas ledger replay resampled the same 120 accounts and found "
                "3 accounts missing inherited-role entries. Deduplicating by role identifier "
                "instead of display name removed 15 omissions. This observation is not a "
                "task completion; the remaining accounts still need reconciliation."
            )
        _note(notes, _history_path(week), f"Atlas {kind} working record {week + 1:02d}", day, body)
    for index, (day, _, target, previous, key, reason) in enumerate(LAUNCH_EVENTS):
        _note(
            notes, _launch_path(index), "Atlas launch steering decision", day,
            f"The steering group reviewed the pilot evidence. {reason}\n\n"
            "## Decisions\n\n"
            f"- Move the Atlas launch target to {target}. [key:: {key}] "
            f"[supersedes:: {previous}]\n\n"
            "## Recorded limits\n\n"
            "This target is conditional on security approval. Final production approval is "
            "not recorded. No production certificate serial number was recorded in this "
            "packet; staging certificate identifiers must not be substituted. "
            "The group did not verify whether the shared calendar had been corrected.",
        )


def _distractors(notes: Path) -> None:
    for project_index, project in enumerate(PROJECTS[1:], start=1):
        previous: dict[str, str] = {}
        for week in range(36):
            kind, body = _working_body(project, project_index, week)
            day = date(2026, 1, 5) + timedelta(days=week * 7 + project_index % 4)
            owner = OWNERS[(project_index + week // 3) % len(OWNERS)]
            if kind in ("decision", "action"):
                key = f"{project.lower()}-{kind}-{week + 1}"
                replaces = f" [supersedes:: {previous[kind]}]" if kind in previous else ""
                if kind == "decision":
                    duration = (14, 30, 60)[(project_index + week // 4) % 3]
                    body += (
                        "\n\n## Decisions\n\n"
                        f"- Retain {project} rehearsal bundles for {duration} days. "
                        f"[key:: {key}]{replaces}"
                    )
                else:
                    marker = "x" if (project_index + week // 4) % 3 == 0 else " "
                    due = day + timedelta(days=12)
                    body += (
                        f"\n\n## Actions\n\n- [{marker}] Reconcile the {project} evidence packet. "
                        f"[owner:: {owner}] [due:: {due}] [key:: {key}]{replaces}"
                    )
                previous[kind] = key
            _note(
                notes, f"history/{project.lower()}/{week + 1:02d}-{kind}.md",
                f"{project} {kind} record {week + 1:02d}", day, body,
            )


def _questions_and_gold() -> tuple[dict[str, Any], dict[str, Any]]:
    questions = json.loads((HERE / "questions.json").read_text(encoding="utf-8"))
    gold = json.loads((HERE / "gold.json").read_text(encoding="utf-8"))
    questions["scenario"] = (
        "Prepare for an Atlas meeting using only the supplied fictional records, spanning "
        "January 5 through October 8, 2026. All supplied records are available. Do not filter "
        "by today's real date. Unless a question supplies a historical interval, report "
        "the current explicit state after all replacements."
    )
    by_id = {question["id"]: question for question in questions["questions"]}
    by_id["launch-history"]["question"] = (
        "Within September 19 through September 26, 2026 inclusive, trace the three documented "
        "Atlas launch targets. Which target was current at the end of that interval? "
        "Exclude later steering decisions for this historical question."
    )
    answers = gold["answers"]
    answers["current-launch"]["facts"]["date"] = "2026-11-12"
    answers["current-launch"]["evidence"] = [
        {"sources": [_launch_path(2)], "contains": ["November 12, 2026", "expanded-launch-nov5"]}
    ]
    answers["calendar-state"]["facts"]["current_target"] = "2026-11-12"
    answers["calendar-state"]["evidence"][1] = answers["current-launch"]["evidence"][0]
    answers["unknown-approval"]["evidence"] = [
        {"sources": [_launch_path(2)], "contains": ["Final production approval is not recorded."]}
    ]
    answers["open-work"]["facts"]["actions"].append(
        {"task": TASK_LABEL, "owner": "Lena", "due": "2026-10-09"}
    )
    answers["open-work"]["facts"]["actions"].sort(key=lambda action: action["task"])
    answers["open-work"]["evidence"].append({
        "sources": [_history_path(35)],
        "contains": [TASK_LABEL, "[ ]", "Lena", "2026-10-09", "expanded-ledger-v4"],
    })
    additions = [
        {
            "id": "ledger-lifecycle",
            "question": (
                "Trace all five explicit versions of the Atlas historical permission-ledger "
                "reconciliation task from January through September. Give each record date, "
                "owner, status and due date, including completion, reopening and transfer. "
                "Who owns the effective task now?"
            ),
            "facts_schema": {
                "versions": [{"date": "ISO date", "owner": "name",
                              "status": "open or completed", "due": "ISO date"}],
                "current_owner": "name",
            },
        },
        {
            "id": "extended-launch-history",
            "question": (
                "Trace all six Atlas launch targets from the September 19 tentative email "
                "through the October 8 steering decision, in source-date order. What is the "
                "current target and why did the last steering decision move it?"
            ),
            "facts_schema": {
                "target_history": ["ISO dates in source-date order"],
                "current_target": "ISO date", "last_reason": "exact reason sentence",
            },
        },
        {
            "id": "ledger-replay-comparison",
            "question": (
                "Compare the March 30 and August 3 Atlas ledger replays, not the routine "
                "weekly batches: how many accounts were sampled each time, how many had "
                "missing inherited-role entries each time, and how many omissions did the "
                "identifier-based deduplication remove? Did that observation complete the task?"
            ),
            "facts_schema": {
                "sampled_accounts": ["March count (integer)", "August count (integer)"],
                "missing_accounts": ["March count (integer)", "August count (integer)"],
                "omissions_removed": "integer", "completed": "boolean",
            },
        },
        {
            "id": "unknown-production-serial",
            "question": (
                "What is the production signing-certificate serial number for Atlas as of "
                "the October 8 packet? Do not substitute a staging identifier."
            ),
            "facts_schema": {"recorded": "boolean", "serial_number": "string or null"},
        },
    ]
    questions["questions"].extend(additions)
    answers["ledger-lifecycle"] = {
        "facts": {
            "versions": [
                {"date": day, "owner": owner, "status": status, "due": due}
                for day, owner, status, due, _ in TASK_EVENTS.values()
            ],
            "current_owner": "Lena",
        },
        "abstains": False,
        "evidence": [
            {"sources": [_history_path(week)],
             "contains": [TASK_LABEL, f"[owner:: {owner}]", f"[due:: {due}]",
                          "[x]" if status == "completed" else "[ ]",
                          f"[key:: expanded-ledger-v{index + 1}]"]}
            for index, (week, (_, owner, status, due, _)) in enumerate(TASK_EVENTS.items())
        ],
    }
    answers["extended-launch-history"] = {
        "facts": {
            "target_history": [
                "2026-10-01", "2026-10-08", "2026-10-15",
                "2026-10-22", "2026-11-05", "2026-11-12",
            ],
            "current_target": "2026-11-12",
            "last_reason": "The repeat rehearsal is scheduled after the maintenance window.",
        },
        "abstains": False,
        "evidence": [
            *answers["launch-history"]["evidence"],
            *[
                {"sources": [_launch_path(index)],
                 "contains": [target, f"supersedes:: {previous}"]}
                for index, (_, _, target, previous, _, _) in enumerate(LAUNCH_EVENTS)
            ],
            {"sources": [_launch_path(2)], "contains": [LAUNCH_EVENTS[2][-1]]},
        ],
    }
    answers["ledger-replay-comparison"] = {
        "facts": {"sampled_accounts": [120, 120], "missing_accounts": [18, 3],
                  "omissions_removed": 15, "completed": False},
        "abstains": False,
        "evidence": [
            {"sources": [_history_path(12)],
             "contains": ["March 30", "120 accounts", "18 accounts", "display name"]},
            {"sources": [_history_path(30)],
             "contains": ["August 3", "120 accounts", "3 accounts", "removed 15 omissions",
                          "not a task completion"]},
        ],
    }
    answers["unknown-production-serial"] = {
        "facts": {"recorded": False, "serial_number": None},
        "abstains": True,
        "evidence": [{"sources": [_launch_path(2)],
                      "contains": ["No production certificate serial number was recorded"]}],
    }
    gold["scoring_notes"].append(
        "Expanded gold is authored with the fixture before runs, not derived from KG output. "
        "The original twelve documents are unchanged; current launch and open-work answers "
        "include the added focal chains. launch-history is explicitly scoped to September 19–26."
    )
    gold["scoring_notes"].append(
        "Ledger lifecycle dates remain exact canonical facts supported by the dated sources "
        "cited for each task version. KG exposes document dates as source_event_time metadata; "
        "raw YAML frontmatter is not an anchored quote and is not required as citation text."
    )
    gold["scoring_notes"].append(
        "Post-run gold maintenance for future builds only: redundant raw-YAML date citation "
        "rules were removed, and unknown-approval now requires the latest October 8 statement "
        "that final production approval is not recorded, rather than the older planning "
        "statement. Canonical facts and abstention expectations are unchanged by this "
        "maintenance; previously frozen run gold and scores must remain unchanged."
    )
    return questions, gold


def build(output: Path) -> dict[str, Any]:
    """Create new fixture inputs and return harness paths plus corpus size metadata."""
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=False)
    notes = output / "notes"
    original = yaml.safe_load((REPOSITORY / "corpora/atlas-state.yml").read_text())
    for directory in ("atlas-vault", "atlas-updates"):
        source = REPOSITORY / "corpora/fixtures" / directory
        for path in sorted(source.glob("*.md")):
            destination = notes / directory / path.name
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, destination)
    _focal_history(notes)
    _distractors(notes)
    manifest = {
        **original, "corpus_id": "work-memory-expanded",
        "display_name": "Expanded synthetic work-memory history",
        "vault_root": "notes", "database": "index.sqlite3",
        "include": ["**/*.md"],
    }
    manifest["seed_entities"] = [
        *original["seed_entities"],
        *[
            {"entity_id": project.lower(), "name": project, "entity_type": "project"}
            for project in PROJECTS if project not in ("Atlas", "Atlascope")
        ],
    ]
    questions, gold = _questions_and_gold()
    paths = {
        "manifest_path": output / "corpus.yml",
        "questions_path": output / "questions.json",
        "gold_path": output / "gold.json",
    }
    _json(paths["manifest_path"], manifest)
    _json(paths["questions_path"], questions)
    _json(paths["gold_path"], gold)
    sources = sorted(notes.rglob("*.md"))
    dates = [
        str(yaml.safe_load(path.read_text(encoding="utf-8").split("---", 2)[1])["date"])
        for path in sources
    ]
    summary = {
        "documents": len(sources),
        "source_utf8_bytes": sum(path.stat().st_size for path in sources),
        "projects": len(PROJECTS),
        "focal_history_documents": 39,
        "earliest_date": min(dates),
        "latest_date": max(dates),
        "span_days": (date.fromisoformat(max(dates)) - date.fromisoformat(min(dates))).days,
        "questions": len(questions["questions"]),
    }
    metadata = {
        "version": 1, "generator": "benchmarks/work_memory/expanded.py", "summary": summary,
        "projects": list(PROJECTS),
        "design": [
            "Original 12 sources retained byte-for-byte. 36 weekly records per project plus "
            "3 later focal steering decisions. All history is separate immutable source notes.",
            "Meetings, forwarded-style emails, rationale notes and action handovers share "
            "artifact vocabulary, owners and dates across 13 boundary-distinct project names.",
            "Only five ledger checklist versions and three launch decisions add focal structured "
            "state. Earlier versions are explicitly superseded. The surviving open ledger action "
            "is included in open-work; the surviving launch decision is included in current-launch "
            "and calendar-state. Narrative observations do not implicitly close checklists.",
            "Historical launch-history retains the original answer with an explicit September "
            "19–26 interval. Other current-state questions include all sources through October 8.",
            "Distractor projects have their own unambiguous action and retention-decision chains; "
            "none are counted as focal tasks or connected to Atlas by invented links.",
            "Ledger lifecycle dates are validated as exact facts with dated task-version source "
            "citations, not by requiring raw YAML frontmatter in quotes. KG source_event_time "
            "metadata exposes these dates; all citation rules can be satisfied by body anchors.",
            "Post-run gold maintenance applies only to future builds: unknown-approval cites "
            "the latest October 8 explicit absence statement, not the older pending-approval "
            "report. This and removal of raw-YAML citation rules preserve canonical facts "
            "and abstention expectations; frozen historical gold and scores are not revised.",
        ],
        "limitations": [
            "Synthetic controlled benchmark, not independently collected workplace history.",
            "Deterministic templates repeat genre and topic patterns; source bytes are not "
            "a token count and no claim of human-level linguistic diversity is made.",
            "Explicit annotations favor structured-state retrieval; prose measurements and "
            "historical questions still require source evidence, not current state alone.",
            "No real calendar, approval system or production certificate is available; absent "
            "confirmation is not proof of the state of an external system.",
            "KG has no as-of-time state API; historical answers require reading dated evidence.",
            "The 201 resolved supersessions exceed the engine record-state report's maximum "
            "200 detail entries. Truncation is explicit; current actions and decisions still "
            "resolve the complete chains.",
        ],
        "expected_source_backed_facts": gold["answers"],
        "sources": {
            path.relative_to(notes).as_posix(): {
                "bytes": path.stat().st_size,
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            }
            for path in sources
        },
    }
    _json(output / "metadata.json", metadata)
    return {**paths, "summary": summary}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    arguments = parser.parse_args()
    print(json.dumps(build(arguments.output), indent=2, default=str))


if __name__ == "__main__":
    main()
