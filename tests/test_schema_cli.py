"""Agent-facing schema workflow uses actual CLI/services and exact local source captures."""

import json
from pathlib import Path

import pytest
from test_canonical_cli import call, runner
from test_canonical_cli import configured as configured

from kg.cli import app
from kg.client.config import load_profile


def prepared(configured, filename="04-audit-trail.md"):
    source = Path(__file__).parents[1] / "corpora/fixtures/atlas-vault" / filename
    added = call("add", source)["result"]
    support = call("read", added["target"])["result"]["entries"][0]["support"]
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
    value["add_entity_types"][0]["review"].update(
        {
            "candidates": [
                {
                    "term": {"kind": "entity_type", "name": "project"},
                    "assessment": "The archive signing certificate is not a project.",
                }
            ],
            "no_existing_candidate_reason": None,
        }
    )
    file = configured / "proposal.json"
    file.write_text(json.dumps(value))
    return file, value, support


@pytest.mark.functional
def test_discover_validate_approve_apply_record_read_and_retry(configured):
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
        "reviewed-certificate",
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
    assert text.exit_code == 0 and "certificate" in text.stdout and "definition_hash" in text.stdout
    facts = configured / "facts.json"
    facts.write_text(
        json.dumps(
            {
                "expected_schema_revision": changed["revision"],
                "support": {"audit": support},
                "changes": [
                    {
                        "kind": "entity",
                        "local_id": "certificate",
                        "entity_type": "certificate",
                        "name": "Archive signing certificate",
                        "support": {"kind": "source", "evidence": ["audit"]},
                    }
                ],
            }
        )
    )
    mapping = call("record", facts)["result"]["write"]["receipt"]["mappings"][0]
    entity = call("read", "entity:" + mapping["stored_id"])["result"]["entity"]
    assert entity["entity_type"] == "certificate"
    assert call("find", "relationships", "entity:" + mapping["stored_id"])["status"] == "empty"


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
    text = runner.invoke(app, [
        "schema", "apply", str(file), "--approve-digest", digest,
        "--retry-key", "saved", "--approval-rationale", "Reviewed.",
    ])
    assert text.exit_code == 7
    assert "saved" in text.stdout and digest in text.stdout
    assert "prepared-correlation" in text.stdout and "Outcome unknown" in text.stdout


@pytest.mark.functional
def test_schema_history_text_continuation_uses_sequence_flag(configured):
    file, _, _ = prepared(configured)
    digest = call("schema", "validate", file)["result"]["proposal_digest"]
    call(
        "schema", "apply", file, "--approve-digest", digest,
        "--retry-key", "paged", "--approval-rationale", "Reviewed.",
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
