"""Agent-facing schema workflow uses actual CLI/services and exact local source captures."""

import json

import pytest
from test_canonical_cli import call, runner
from test_canonical_cli import configured as configured

from kg.cli import app
from kg.client.config import load_profile
from kg.models.foundation import WriteOutcome


def prepared(configured, filename="04-audit-trail.md"):
    source = configured / filename
    source.write_text("Device Delta is an explicitly identified maintenance device.")
    added = call("add", source)["result"]
    support = call("read", added["document"]["target"])["result"]["entries"][0]["support"]
    value = json.loads(
        call("schema", "validate", "--example")["result"]["example"]
        .split(
            "```json\n",
        )[1]
        .split("```")[0]
    )
    profile = load_profile()
    value.update(
        {
            "corpus_id": profile.scope.corpus_id,
            "base_revision": call("schema", "show")["result"]["revision"],
            "attribution": profile.attribution.model_dump(mode="json"),
        }
    )
    value["examples"][0].update(support)
    file = configured / "proposal.json"
    file.write_text(json.dumps(value))
    return file, value, support


@pytest.mark.functional
def test_discover_validate_approve_apply_record_read_and_retry(configured, monkeypatch):
    file, value, support = prepared(configured)
    before = call("schema", "show")["result"]
    validation = call("schema", "validate", file)["result"]
    assert call("schema", "show")["result"] == before
    digest = validation["proposal_digest"]
    args = (
        "schema",
        "apply",
        file,
        "--approve-digest",
        digest,
        "--retry-key",
        "reviewed-device",
        "--approval-rationale",
        "Reviewed fixture evidence.",
    )
    saved = call(*args)["result"]["apply"]["receipt"]
    assert call(*args)["result"]["apply"]["receipt"] == saved
    changed = call("schema", "show")["result"]
    assert changed["revision"] == saved["revision"] != before["revision"]
    assert (
        call("schema", "change", saved["revision"]["revision_id"])["result"]["proposal"][
            "rationale"
        ]
        == value["rationale"]
    )
    assert len(call("schema", "history")["result"]["entries"]) == 2
    text = runner.invoke(app, ["schema", "show"])
    assert text.exit_code == 0 and "device" in text.stdout and "definition_hash" in text.stdout
    search_support = call("find", "documents", "Device Delta")["result"]["entries"][0]["support"]
    bootstrap = configured / "bootstrap.json"
    bootstrap.write_text(
        json.dumps(
            {
                "expected_schema_revision": changed["revision"],
                "support": {"source": search_support},
                "changes": [
                    {
                        "kind": "entity",
                        "local_id": "base-device",
                        "name": "Device Delta",
                        "support": {"kind": "source", "evidence": ["source"]},
                    },
                ],
            }
        )
    )
    bootstrap_result = call("record", bootstrap, "--retry-key", "bootstrap")["result"]
    base_id = bootstrap_result["mappings"][0]["stored_id"]
    changes = [
        {
            "kind": "entity",
            "local_id": "device",
            "name": "Device Echo",
            "support": {"kind": "source", "evidence": ["source"]},
        },
        {
            "kind": "entity_support",
            "local_id": "base-support",
            "entity": {"kind": "stored", "entity_id": base_id},
            "name": "Device Delta",
            "support": {"kind": "source", "evidence": ["source"]},
        },
        {
            "kind": "alias",
            "local_id": "device-alias",
            "entity": {"kind": "local", "local_id": "device"},
            "alias": "Echo unit",
            "support": {"kind": "source", "evidence": ["source"]},
        },
        {
            "kind": "identifier",
            "local_id": "device-tag",
            "entity": {"kind": "local", "local_id": "device"},
            "scheme": "asset_tag",
            "value": "ECHO-7",
            "support": {"kind": "source", "evidence": ["source"]},
        },
        {
            "kind": "mention",
            "local_id": "device-mention",
            "entity": {"kind": "local", "local_id": "device"},
            "support": {"kind": "source", "evidence": ["source"]},
        },
        {
            "kind": "classification",
            "local_id": "type",
            "entity": {"kind": "local", "local_id": "device"},
            "entity_type": "device",
            "interpretation": "explicit",
            "support": {"kind": "source", "evidence": ["source"]},
        },
        {
            "kind": "classification_selection",
            "local_id": "selection",
            "entity": {"kind": "local", "local_id": "device"},
            "claim": {"kind": "local", "local_id": "type"},
            "expected_selection_id": None,
            "reviewed_candidates_digest": None,
            "reviewed_claim_ids": [],
            "review_coverage": "complete",
            "accept_incomplete_review": False,
            "rationale": "Reviewed the exact device claim.",
        },
        {
            "kind": "assertion",
            "local_id": "note",
            "subject": {"kind": "local", "local_id": "device"},
            "predicate": "maintenance_note",
            "object": {"kind": "string", "value": "Inspection complete"},
            "subject_classification": {"kind": "local", "local_id": "selection"},
            "interpretation": "explicit",
            "support": {"kind": "source", "evidence": ["source"]},
        },
    ]
    facts = configured / "facts.json"
    facts.write_text(
        json.dumps(
            {
                "expected_schema_revision": changed["revision"],
                "support": {"source": search_support},
                "changes": changes,
            }
        )
    )
    written = call("record", facts, "--retry-key", "facts")["result"]
    canonical = WriteOutcome.model_validate_json(json.dumps(written["knowledge_write"]))
    replay = call("record", facts, "--retry-key", "facts")["result"]
    assert replay["knowledge_write"]["receipt"] == written["knowledge_write"]["receipt"]
    assert replay["mappings"] == written["mappings"]
    assert [item["local_id"] for item in written["mappings"]] == [
        item["local_id"] for item in changes
    ]
    by_local = {item["local_id"]: item for item in written["mappings"]}
    entity_id = by_local["device"]["stored_id"]
    assert by_local["device"]["record_reference"] == {
        "kind": "stored",
        "entity_id": entity_id,
    }
    assert by_local["device"]["inspection_command"] == f"kg read entity:{entity_id} --json"
    assert by_local["type"]["record_reference"]["contribution_id"] == by_local["type"]["stored_id"]
    assert (
        by_local["selection"]["record_reference"]["event_id"] == by_local["selection"]["stored_id"]
    )
    assert by_local["selection"]["inspection_target"] is None
    assert by_local["selection"]["inspection_command"] is None
    for local_id in ("base-support", "device-alias", "device-tag", "device-mention", "note"):
        item = by_local[local_id]
        assert item["record_reference"] is None
        assert item["inspection_target"] == "fact:" + item["stored_id"]
        assert item["inspection_command"] == f"kg read fact:{item['stored_id']} --json"
    assert canonical.receipt is not None
    bad_mapping = canonical.receipt.mappings[0].model_copy(update={"local_id": "unexpected"})
    bad_outcome = canonical.model_copy(
        update={
            "receipt": canonical.receipt.model_copy(
                update={"mappings": (bad_mapping, *canonical.receipt.mappings[1:])}
            )
        }
    )
    monkeypatch.setattr("kg.client.knowledge.submit", lambda *args, **kwargs: bad_outcome)
    mismatch = call("record", facts, "--retry-key", "mismatch", code=6)
    assert mismatch["status"] == "partial"
    assert mismatch["code"] == "internal_mapping_mismatch"
    assert mismatch["result"]["knowledge_write"] == bad_outcome.model_dump(mode="json")
    assert "mappings" not in mismatch["result"]
    entity = call("read", "entity:" + entity_id)["result"]["entity"]
    assert entity["entity_type"] == "device"
    assert call("find", "relationships", "entity:" + entity_id)["status"] == "empty"


@pytest.mark.functional
@pytest.mark.parametrize("failure", [KeyboardInterrupt, RuntimeError])
def test_schema_apply_unknown_preserves_operator_retry_material(configured, monkeypatch, failure):
    from kg.knowledge import KnowledgeAdministration

    file, _, _ = prepared(configured)
    digest = call("schema", "validate", file)["result"]["proposal_digest"]

    def interrupted(*args):
        raise failure()

    monkeypatch.setattr("kg.client.schema.token", lambda: "prepared-correlation")
    monkeypatch.setattr(KnowledgeAdministration, "apply_schema", interrupted)
    result = call(
        "schema",
        "apply",
        file,
        "--approve-digest",
        digest,
        "--retry-key",
        "saved",
        "--approval-rationale",
        "Reviewed.",
        code=7,
    )
    assert result["status"] == "uncertain"
    assert result["result"]["retry_key"] == "saved"
    assert result["result"]["proposal_digest"] == digest
    assert result["result"]["request_id"]
    text = runner.invoke(
        app,
        [
            "schema",
            "apply",
            str(file),
            "--approve-digest",
            digest,
            "--retry-key",
            "saved",
            "--approval-rationale",
            "Reviewed.",
        ],
    )
    assert text.exit_code == 7
    assert "saved" in text.stdout and digest in text.stdout
    assert "prepared-correlation" in text.stdout and "Outcome unknown" in text.stdout


@pytest.mark.functional
def test_schema_history_text_continuation_uses_sequence_flag(configured):
    file, _, _ = prepared(configured)
    digest = call("schema", "validate", file)["result"]["proposal_digest"]
    call(
        "schema",
        "apply",
        file,
        "--approve-digest",
        digest,
        "--retry-key",
        "paged",
        "--approval-rationale",
        "Reviewed.",
    )
    text = runner.invoke(app, ["schema", "history", "--limit", "1"])
    assert text.exit_code == 0
    assert "More revisions: --after-sequence 1" in text.stdout
    assert "--after None" not in text.stdout
    second = call("schema", "history", "--after-sequence", "1")["result"]
    assert len(second["entries"]) == 1 and not second["has_more"]


@pytest.mark.functional
@pytest.mark.parametrize("invalid", ["duplicate", "oversize", "missing-approval"])
def test_schema_client_rejects_bad_input_without_apply(configured, monkeypatch, invalid):
    from kg.knowledge import KnowledgeAdministration

    file, _, _ = prepared(configured)
    monkeypatch.setattr(
        KnowledgeAdministration,
        "apply_schema",
        lambda *args: pytest.fail("unexpected apply"),
    )
    if invalid == "duplicate":
        file.write_text(file.read_text().replace('"corpus_id":', '"corpus_id": "x", "corpus_id":'))
        call("schema", "validate", file, code=2)
    elif invalid == "oversize":
        file.write_bytes(b" " * ((1 << 20) + 1))
        call("schema", "validate", file, code=2)
    else:
        result = runner.invoke(app, ["schema", "apply", str(file)])
        assert result.exit_code == 2
