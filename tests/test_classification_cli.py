"""Classification operations through JSON-only public CLI output."""

import json

from test_canonical_cli import call
from test_canonical_cli import configured as configured


def test_unresolved_claim_selection_history_and_withdrawal_json_workflow(configured):
    note = configured / "note.md"
    note.write_text("Horizon is a project. The review decided to ship Horizon.")
    target = call("add", note)["result"]["target"]
    support = call("read", target)["result"]["entries"][0]["support"]
    revision = call("schema", "show")["result"]["revision"]
    file = configured / "facts.json"

    def record(changes, key, supports):
        file.write_text(
            json.dumps(
                {
                    "expected_schema_revision": revision,
                    "support": supports,
                    "changes": changes,
                }
            )
        )
        return call("record", file, "--retry-key", key)["result"]["write"]

    created = record(
        [
            {
                "kind": "entity",
                "local_id": "horizon",
                "name": "Horizon",
                "support": {"kind": "source", "evidence": [support["reference"]]},
            }
        ],
        "identity",
        [support],
    )
    entity_id = created["receipt"]["mappings"][0]["stored_id"]
    entity_target = "entity:" + entity_id
    view = call("read", entity_target)["result"]["entity"]
    assert view["entity_type"] is None and view["classification"]["status"] == "unresolved"
    claim = record(
        [
            {
                "kind": "classification",
                "local_id": "type",
                "entity": {"kind": "stored", "entity_id": entity_id},
                "entity_type": "project",
                "interpretation": "explicit",
                "support": {"kind": "source", "evidence": [support["reference"]]},
            }
        ],
        "claim",
        [support],
    )
    claim_id = claim["receipt"]["mappings"][0]["stored_id"]
    reviewed = call("classifications", entity_target)["result"]
    selected = record(
        [
            {
                "kind": "classification_selection",
                "local_id": "selection",
                "entity": {"kind": "stored", "entity_id": entity_id},
                "claim": {"kind": "stored", "contribution_id": claim_id},
                "expected_selection_id": reviewed["selection_id"],
                "reviewed_candidates_digest": reviewed["reviewed_candidates_digest"],
                "reviewed_claim_ids": reviewed["reviewed_claim_ids"],
                "review_coverage": reviewed["review_coverage"],
                "accept_incomplete_review": False,
                "rationale": "Selected the reviewed exact source-backed project claim.",
            }
        ],
        "selection",
        [],
    )
    replay = call("record", file, "--retry-key", "selection")["result"]["write"]
    assert replay["receipt"] == selected["receipt"]
    assert call("read", entity_target)["result"]["entity"]["entity_type"] == "project"
    history = call("classifications", entity_target, "--history")["result"]
    assert len(history["entries"]) == 2
    withdrawn = call(
        "withdraw-classification",
        "fact:" + claim_id,
        "--retry-key",
        "withdraw",
    )["result"]["write"]
    assert withdrawn["receipt"]["contribution_id"] == claim_id
    assert call("read", entity_target)["result"]["entity"]["entity_type"] is None
    assert call("find", "entities", "Horizon")["result"]["entries"]
