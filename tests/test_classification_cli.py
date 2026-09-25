"""Classification operations through JSON-only public CLI output."""

import json

import pytest
from test_canonical_cli import call
from test_canonical_cli import configured as configured

pytestmark = pytest.mark.functional


def test_recorded_classification_history_and_withdrawal_json_workflow(configured):
    note = configured / "note.md"
    note.write_text("Horizon is a project. The review decided to ship Horizon.")
    target = call("add", note)["result"]["document"]["target"]
    support = call("read", target)["result"]["entries"][0]["support"]
    revision = call("schema", "show")["result"]["head"]
    file = configured / "facts.json"
    file.write_text(
        json.dumps(
            {
                "interface_version": "record-authoring/1",
                "expected_schema_revision": revision,
                "support": {"source": {"kind": "source", **support}},
                "entities": [
                    {
                        "local_id": "horizon",
                        "identity": {
                            "kind": "new",
                            "name": "Horizon",
                            "support": ["source"],
                        },
                        "classifications": [
                            {
                                "local_id": "type",
                                "entity_type": "project",
                                "interpretation": "explicit",
                                "support": ["source"],
                                "select": {
                                    "rationale": (
                                        "The selected source explicitly identifies a project."
                                    )
                                },
                            }
                        ],
                    }
                ],
                "assertions": [],
            }
        )
    )

    created = call("record", file, "--retry-key", "classification")["result"]
    replay = call("record", file, "--retry-key", "classification")["result"]
    assert replay["receipt"] == created["receipt"]
    entity = created["receipt"]["entities"][0]
    entity_id = entity["entity_id"]
    claim_id = next(
        item["stored_id"] for item in entity["items"] if item["local_id"] == "type"
    )
    entity_target = "entity:" + entity_id
    assert call("read", entity_target)["result"]["entity"]["entity_type"] == "project"
    review = call("classifications", entity_target)["result"]
    assert review["review_witness"]["entity_id"] == entity_id
    history = call("classifications", entity_target, "--history")["result"]
    assert history["entries"]

    withdrawn = call(
        "withdraw-classification",
        "fact:" + claim_id,
        "--retry-key",
        "withdraw",
    )["result"]["knowledge_write"]
    assert withdrawn["receipt"]["contribution_id"] == claim_id
    assert call("read", entity_target)["result"]["entity"]["entity_type"] is None
    assert call("find", "entities", "Horizon")["result"]["entries"]
