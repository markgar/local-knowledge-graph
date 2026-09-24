"""Composable public verbs over operator-selected excerpts, using no Python client glue."""

import json

import pytest
from test_canonical_cli import call, runner

from kg.cli import app
from kg.client.config import load_profile, profile_path


@pytest.fixture
def fresh(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))

    def forbidden(*args, **kwargs):
        pytest.fail("Model/index construction from a model-free verb")

    monkeypatch.setattr("kg.client.documents.IndexService", forbidden)
    monkeypatch.setattr("kg.client.documents.EvidenceSearchService", forbidden)
    call("setup", "--yes")
    return tmp_path


@pytest.mark.functional
@pytest.mark.parametrize(
    "domain,subject,obj,predicate,text",
    [
        (
            "botany",
            "specimen",
            "taxon",
            "classified_as",
            "Specimen F belongs to taxon Fern. Its label is green.",
        ),
        ("maintenance", "pump", "part", "requires", "Pump P requires part Seal. Its label is red."),
    ],
)
def test_public_initial_workflow_no_facts(fresh, domain, subject, obj, predicate, text):
    assert call("schema", "show")["result"]["status"] == "unconfigured"
    caps = call("capabilities")["result"]
    assert caps["knowledge"]["change_kinds"] == []
    assert caps["workflows"]["initial_generation"] == "external_agent_context"
    assert not caps["workflows"]["models_approved"]
    assert caps["workflows"]["search_readiness"] == "not_checked"
    support = []
    for filename, content in ((domain, text), ("coverage", "Only one site was selected.")):
        file = fresh / f"{filename}.txt"
        file.write_text(content)
        added = call("add", file, "--evidence-only")
        assert added["result"]["preparation"]["status"] == "not_requested"
        read = call("read", added["result"]["target"])["result"]
        support.append(read["entries"][0]["support"])
        assert read["entries"][0]["evidence"]["quote"] == content
    sample = {
        "support": support,
        "intended_use": f"Describe {domain} records.",
        "selection_rationale": "Operator explicitly selected these two excerpts.",
    }
    sample_file = fresh / "sample.json"
    sample_file.write_text(json.dumps(sample))
    generated = call("schema", "generate", sample_file)
    assert generated["status"] == "awaiting_agent"
    brief = generated["result"]
    assert brief["base_revision"] is None
    assert brief["evidence"][0]["quote"] == text
    assert call("schema", "show")["result"]["status"] == "unconfigured"
    rendered = runner.invoke(app, ["schema", "generate", str(sample_file)])
    assert rendered.exit_code == 0 and text in rendered.stdout
    review = {
        "no_existing_candidate_reason": "No configured vocabulary.",
        "reuse_assessment": "No existing terms.",
        "extension_rationale": "These explicitly supplied categories fit the selected text.",
        "defer_assessment": "No claims outside the selected site.",
        "example_ids": ["source"],
    }
    proposal = {
        "corpus_id": brief["corpus_id"],
        "base_revision": None,
        "attribution": brief["attribution"],
        "rationale": f"Initial {domain} vocabulary.",
        "add_entity_types": [
            {"definition": {"name": name, "description": f"A supplied {name}."}, "review": review}
            for name in (subject, obj)
        ],
        "add_predicates": [
            {
                "definition": {
                    "name": predicate,
                    "description": "Explicit source relationship.",
                    "subject_types": [subject],
                    "object_kind": "entity",
                    "object_types": [obj],
                },
                "review": review,
            },
            {
                "definition": {
                    "name": "label",
                    "description": "Recorded label.",
                    "subject_types": [subject],
                    "object_kind": "string",
                },
                "review": review,
            },
        ],
        "examples": [{"example_id": "source", **support[0], "explanation": "Exact source terms."}],
        "initial_generation": {
            "sample": sample,
            "coverage_status": "limited",
            "coverage_limitations": ["One selected site; other sites not represented."],
            "synonym_decisions": [
                {
                    "surface_forms": ["label", "recorded label"],
                    "term": {"kind": "predicate", "name": "label"},
                    "rationale": "One name for the recorded textual label.",
                }
            ],
        },
    }
    file = fresh / "proposal.json"
    file.write_text(json.dumps(proposal))
    validated = call("schema", "validate", file)["result"]
    assert validated["semantic_review_required"]
    assert call("schema", "show")["result"]["status"] == "unconfigured"
    # Explicit TEST-CONTROLLED approval exercises apply, not a claim of real human review.
    args = (
        "schema",
        "apply",
        file,
        "--approve-digest",
        validated["proposal_digest"],
        "--retry-key",
        "reviewed-in-test",
        "--approval-rationale",
        "Test-controlled approval only.",
    )
    saved = call(*args)["result"]["apply"]["receipt"]
    assert call(*args)["result"]["apply"]["receipt"] == saved
    schema = call("schema", "show")["result"]
    assert {t["name"] for t in schema["definition"]["entity_types"]} == {subject, obj}
    assert (
        call("schema", "change", saved["revision"]["revision_id"])["result"]["proposal"][
            "initial_generation"
        ]["sample"]["support"]
        == support
    )
    assert call("schema", "generate", sample_file, code=2)["code"] == "state_conflict"
    with load_profile().database().connection() as connection:
        assert connection.execute("SELECT count(*) FROM entity").fetchone()[0] == 0
        assert connection.execute("SELECT count(*) FROM contribution").fetchone()[0] == 0


@pytest.mark.functional
def test_evidence_only_update_permissions_and_schema_free_attach(fresh, monkeypatch):
    file = fresh / "source.txt"
    file.write_text("An exact source.")
    assert call("add", file, code=2)["code"] == "models_not_approved"
    assert call("find", "documents", "source", code=2)["code"] == "models_not_approved"
    added = call("add", file, "--evidence-only")["result"]
    file.write_text("A revised exact source.")
    assert (
        call(
            "update",
            added["target"],
            file,
            "--expect",
            added["state"],
            code=2,
        )["code"]
        == "models_not_approved"
    )
    updated = call(
        "update",
        added["target"],
        file,
        "--expect",
        added["state"],
        "--evidence-only",
    )
    assert "old state" in updated["message"]
    assert "deactivated" not in updated["message"]
    old = call("read", added["target"] + "@" + added["state"])["result"]["entries"][0]
    assert old["evidence"]["quote"] == "An exact source."
    source_profile = profile_path()
    database = load_profile().database()
    with database.connection() as connection:
        before = tuple(connection.iterdump())
    monkeypatch.setenv("XDG_CONFIG_HOME", str(fresh / "attached-config"))
    assert (
        call(
            "setup",
            "--attach",
            source_profile,
            "--schema-preset",
            "personal/1",
            "--yes",
            code=2,
        )["code"]
        == "invalid_option"
    )
    call("setup", "--attach", source_profile, "--yes")
    with database.connection() as connection:
        assert tuple(connection.iterdump()) == before
    assert call("schema", "show")["result"]["status"] == "unconfigured"


@pytest.mark.functional
def test_preset_is_explicit_and_bad_preset_does_not_provision(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    assert (
        call("setup", "--yes", "--schema-preset", "universal", code=2)["code"] == "invalid_option"
    )
    assert not (tmp_path / "data").exists()
    call("setup", "--yes", "--schema-preset", "personal/1")
    assert {t["name"] for t in call("schema", "show")["result"]["definition"]["entity_types"]} == {
        "person",
        "project",
    }


@pytest.mark.functional
@pytest.mark.parametrize(
    "kind", ["duplicate-key", "symlink", "oversize", "empty", "blank", "stale"]
)
def test_generation_errors_are_explicit(fresh, kind):
    file = fresh / "source.txt"
    file.write_text("  \n\t" if kind == "blank" else "Specimen S.")
    added = call("add", file, "--evidence-only")["result"]
    capture = call("read", added["target"])["result"]["entries"][0]["support"]
    sample = {
        "support": [capture],
        "intended_use": "Specimens.",
        "selection_rationale": "Selected.",
    }
    file = fresh / "sample.json"
    if kind == "empty":
        sample["support"] = []
    file.write_text(json.dumps(sample))
    if kind == "duplicate-key":
        file.write_text('{"support":[],"support":[]}')
    elif kind == "oversize":
        file.write_bytes(b" " * ((1 << 20) + 1))
    elif kind == "symlink":
        link = fresh / "link.json"
        link.symlink_to(file)
        file = link
    elif kind == "stale":
        call("remove", added["target"], "--expect", added["state"], "--confirm")
    result = call("schema", "generate", file, code=2)
    assert result["status"] == "rejected"
    assert call("schema", "show")["result"]["status"] == "unconfigured"


@pytest.mark.functional
def test_generation_help_and_input_schema_without_profile(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    for args in (["schema", "generate", "--help"], ["capabilities", "--help"]):
        assert runner.invoke(app, args).exit_code == 0
    assert call("schema", "generate", "--schema")["result"]["schema"]["title"] == "SchemaSample"
    text = call("schema", "generate", "--example")["result"]["example"]
    assert "initial_generation" in text and "awaiting_agent" in text
    assert call("capabilities", code=2)["code"] == "not_configured"
