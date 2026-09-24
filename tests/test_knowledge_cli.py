"""CLI-only synthetic knowledge journey; services are real, model providers controlled."""

import json
from importlib.resources import files
from pathlib import Path

import pytest
from test_canonical_cli import call, runner
from test_canonical_cli import configured as configured

from kg.cli import app
from kg.client.config import load_profile, profile_path
from kg.evidence import EvidenceService
from kg.knowledge import KnowledgeService


def record_file(directory, support, changes):
    file = directory / "facts.json"
    file.write_text(json.dumps({"support": [support], "changes": changes}))
    return file


def entity(local_id, name, entity_type, support):
    return dict(
        kind="entity",
        local_id=local_id,
        name=name,
        entity_type=entity_type,
        support={"kind": "source", "evidence": [support["reference"]]},
    )


def assertion(local_id, subject, predicate, obj, support):
    return dict(
        kind="assertion",
        local_id=local_id,
        subject=subject,
        predicate=predicate,
        object=obj,
        interpretation="explicit",
        support={"kind": "source", "evidence": [support["reference"]]},
    )


def local(local_id):
    return {"kind": "local", "local_id": local_id}


@pytest.fixture
def recorded(configured):
    file = configured / "meeting.md"
    file.write_text("Mira owns Atlas. Atlas will ship Friday. Mira decided to approve the launch.")
    added = call("add", file)["result"]
    support = call("read", added["target"])["result"]["entries"][0]["support"]
    changes = [
        entity("mira", "Mira", "person", support),
        entity("atlas", "Atlas", "project", support),
        # A scalar assertion before the relationship makes an empty filtered first page.
        assertion(
            "approval",
            local("mira"),
            "decision",
            {"kind": "string", "value": "Approve launch"},
            support,
        ),
        assertion(
            "ownership",
            local("mira"),
            "owns",
            {"kind": "entity", "entity": local("atlas")},
            support,
        ),
        assertion(
            "release",
            local("atlas"),
            "decision",
            {"kind": "string", "value": "Ship Friday"},
            support,
        ),
    ]
    response = call("record", record_file(configured, support, changes))["result"]["write"]
    assert response["status"] == "applied"
    mappings = {item["local_id"]: item["stored_id"] for item in response["receipt"]["mappings"]}
    assert len(mappings) == len(changes)
    return configured, file, added, support, mappings


def test_complete_read_record_query_withdraw_update_journey(recorded):
    directory, file, added, support, mappings = recorded
    listed = call("find", "entities", "--limit", 1)["result"]
    assert listed["has_more"] and not listed["selection_complete"]
    rest = call("find", "entities", "--after", listed["next_after"])["result"]
    assert not rest["has_more"]
    atlas = call("find", "entities", "Atlas")["result"]["entries"][0]
    assert atlas["target"] == "entity:" + mappings["atlas"]
    assert atlas["entity"]["witness"]["basis"]["evidence"][0]["reference"] == support["reference"]
    read = call("read", atlas["target"])["result"]
    assert read["reference"] == atlas["reference"]
    # Copy actual returned support and entity reference, no rewriting or fabricated IDs.
    change = assertion(
        "again", read["reference"], "decision", {"kind": "string", "value": "Ship Friday"}, support
    )
    recorded_again = call("record", record_file(directory, support, [change]))
    assert recorded_again["result"]["write"]["receipt"]["mappings"][0]["local_id"] == "again"
    query = call("find", "decisions", "Atlas", "--limit", 1)["result"]
    assert query["query"]["result"]["data"]["count"] == 2
    assert query["query"]["result"]["data"]["exact"]
    assert len(query["inspection"]["records"]) == 1
    assert query["display_truncated"] and not query["retained_handles_usable"]
    fact = "fact:" + mappings["release"]
    fact_read = call("read", fact)["result"]
    assert fact_read["support"] == [support]
    assert call("read", fact_read["evidence_targets"][0])["result"]["support"] == support
    assert call("read", atlas["evidence_targets"][0])["result"]["support"] == support
    call("remove", fact, code=2)
    removed = call("remove", fact, "--confirm")
    assert removed["result"]["write"]["receipt"]["contribution_id"] == mappings["release"]
    call("read", fact, code=3)
    historical = call("read", fact, "--history")["result"]
    assert historical["contribution"]["withdrawal"]
    assert not historical["contribution"]["is_current"]
    assert "Historical/inactive" in runner.invoke(app, ["read", fact, "--history"]).stdout
    assert (
        call("find", "decisions", atlas["target"])["result"]["query"]["result"]["data"]["count"]
        == 1
    )
    file.write_text("Atlas will ship Monday.")
    call("update", added["target"], file, "--expect", added["state"])
    assert call("find", "entities", "Atlas")["status"] == "empty"
    history = call("read", atlas["target"], "--history")
    assert not history["result"]["entity"]["is_current"]
    # Do not replace the captured support state with the latest state.
    stale = call("record", record_file(directory, support, [change]), code=4)
    assert stale["result"]["write"]["receipt"] is None
    assert call("read", added["target"] + "@" + added["state"])["message"].startswith("Mira owns")


def test_incident_relationships_preserve_empty_page_continuation(recorded):
    _, _, _, support, mappings = recorded
    first = call("find", "relationships", "Mira", "--limit", 1)["result"]
    assert first["entries"] == [] and first["has_more"]
    second = call("find", "relationships", "Mira", "--after", first["next_after"])["result"]
    relation = second["entries"][0]
    assert relation["target"] == "fact:" + mappings["ownership"]
    assert relation["directions"] == ["outgoing"] and relation["support"] == [support]
    incoming = call("find", "relationships", "Atlas")["result"]["entries"][0]
    assert incoming["directions"] == ["incoming"]
    assert incoming["contribution"] == relation["contribution"]
    assert "outgoing" in runner.invoke(app, ["find", "relationships", "Mira"]).stdout


def test_exact_alias_ambiguity_and_no_prefix_uniqueness(recorded):
    directory, _, _, support, mappings = recorded
    changes = [
        entity("other", "Atlas", "project", support),
        {
            "kind": "alias",
            "local_id": "alias",
            "entity": {"kind": "stored", "entity_id": mappings["mira"]},
            "alias": "M",
            "support": {"kind": "source", "evidence": [support["reference"]]},
        },
    ]
    call("record", record_file(directory, support, changes))
    assert (
        call("find", "entities", "M")["result"]["entries"][0]["target"]
        == "entity:" + mappings["mira"]
    )
    ambiguous = call("find", "relationships", "Atlas", code=2)
    assert ambiguous["status"] == "ambiguous" and len(ambiguous["result"]["entries"]) == 2
    assert call("find", "decisions", "Atlas", code=3)["status"] == "ambiguous"
    assert call("find", "relationships", "atlas", code=2)["code"] == "entity_not_found"
    call("find", "relationships", "entity:" + mappings["atlas"])


@pytest.mark.parametrize("mutation", ["extra", "implicit", "missing_capture", "seed", "duplicate"])
def test_strict_record_rejects_bad_input_atomically(recorded, mutation):
    directory, _, _, support, _ = recorded
    change = entity("new", "New", "project", support)
    file = record_file(directory, support, [change])
    value = json.loads(file.read_text())
    if mutation == "extra":
        value["scope"] = {}
    elif mutation == "implicit":
        value["changes"].append(
            assertion(
                "bad", local("undeclared"), "decision", {"kind": "string", "value": "bad"}, support
            )
        )
    elif mutation == "missing_capture":
        value["support"] = []
    elif mutation == "seed":
        value["changes"][0]["support"] = {
            "kind": "seed",
            "source_namespace": "local",
            "seed_set_id": "x",
            "seed_key": "x",
        }
    else:
        value["support"].append(support)
    file.write_text(json.dumps(value))
    call("record", file, code=2)
    assert call("find", "entities", "New")["status"] == "empty"


def test_duplicate_keys_and_invalid_json(configured):
    file = configured / "invalid.json"
    for text in ('{"support":[],"support":[],"changes":[]}', "{", '{"support":NaN}'):
        file.write_text(text)
        call("record", file, code=2)


def test_owned_withdrawal_rejects_wrong_writer_and_non_assertions(recorded):
    _, _, _, _, mappings = recorded
    entity_read = call("read", "entity:" + mappings["atlas"])["result"]
    support_id = next(
        item["target"]
        for item in entity_read["entries"]
        if item["contribution"]["payload"]["kind"] == "entity_support"
    )
    call("remove", support_id, "--confirm", code=4)
    profile = load_profile()
    profile_path().write_text(
        profile.model_copy(
            update={
                "attribution": profile.attribution.model_copy(update={"writer_id": "unregistered"})
            }
        ).model_dump_json()
    )
    failed = call("remove", "fact:" + mappings["release"], "--confirm", code=4)
    assert failed["code"] == "forbidden"


def test_selection_budget_failure_is_not_unique(recorded, monkeypatch):
    from kg.evidence import EvidenceServiceError

    def exhausted(*args, **kwargs):
        raise EvidenceServiceError("budget_exceeded")

    monkeypatch.setattr(KnowledgeService, "entities", exhausted)
    response = call("find", "relationships", "Atlas", code=3)
    assert response["code"] == "budget_exceeded" and "selected" not in response["result"]


@pytest.mark.parametrize("error", [RuntimeError, KeyboardInterrupt])
def test_unknown_record_outcome_is_not_retried(recorded, monkeypatch, error):
    directory, _, _, support, mappings = recorded
    original = EvidenceService.write
    calls = []

    def lost(self, request):
        calls.append(request)
        original(self, request)
        raise error("lost delivery")

    monkeypatch.setattr(EvidenceService, "write", lost)
    change = assertion(
        "another",
        {"kind": "stored", "entity_id": mappings["atlas"]},
        "decision",
        {"kind": "string", "value": "Ship Friday"},
        support,
    )
    result = call("record", record_file(directory, support, [change]), code=7)
    assert result["status"] == "uncertain" and len(calls) == 1


@pytest.mark.parametrize(
    "args",
    [
        ["find", "entities"],
        ["find", "relationships"],
        ["find", "decisions"],
        ["record"],
        ["skill"],
    ],
)
def test_knowledge_help_needs_no_profile(tmp_path, monkeypatch, args):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "absent"))
    assert runner.invoke(app, [*args, "--help"]).exit_code == 0
    assert not (tmp_path / "absent").exists()


def test_installed_schema_example_and_strategy_are_discoverable():
    schema = call("record", "--schema")["result"]["schema"]
    assert schema["additionalProperties"] is False
    assert "ChangeSet" not in schema["properties"] and "support" in schema["properties"]
    assert "SUPPORT" in call("record", "--example")["result"]["example"]
    skill = call("skill")["result"]["skill"]
    assert "Start with `kg --help`" in skill
    assert skill == files("kg.client").joinpath("SKILL.md").read_text()


def test_repository_and_installed_skill_match():
    repository = Path(__file__).resolve().parents[1] / ".github/skills/use-knowledge-graph/SKILL.md"
    if not repository.exists():
        pytest.skip("Repository copy is deliberately absent in installed-wheel validation")
    assert repository.read_bytes() == files("kg.client").joinpath("SKILL.md").read_bytes()


@pytest.mark.parametrize("through,direction", [("owns", "outgoing"), ("^owns", "incoming")])
def test_graph_request_and_full_envelope_forwarding(recorded, monkeypatch, through, direction):
    from kg.graph import LocalGraphSession
    from kg.graph._session_types import GraphGeneration
    from kg.models.graph import GraphRelationshipDecisionsResult

    _, _, _, _, mappings = recorded
    root = mappings["mira"] if direction == "outgoing" else mappings["atlas"]
    returned = []

    def controlled(self, request):
        assert request.predicate == "owns" and request.direction == direction
        assert request.start.entity_id == root and request.display_limit == 7
        result = GraphRelationshipDecisionsResult(
            request_id=request.request_id,
            scope=request.scope,
            outcome="empty",
            generation=GraphGeneration(session_id="controlled", ordinal=1),
            root_entity_id=root,
            count=0,
            exact=True,
        )
        returned.append(result.model_dump(mode="json"))
        return result

    # Wrapper contract only; no native execution or native-readiness claim.
    monkeypatch.setattr(LocalGraphSession, "relationship_decisions", controlled)
    output = call("find", "decisions", "entity:" + root, "--through", through, "--limit", 7)
    assert output["result"]["graph"] == returned[0]


def test_actual_graph_session_unavailable_and_cleanup_failure(recorded, monkeypatch):
    from kg.graph import GraphSessionError, LocalGraphSession
    from kg.graph._native import NativeError
    from kg.graph._session_types import GraphFailure

    def unavailable():
        raise NativeError("graph_unavailable")

    monkeypatch.setattr("kg.graph._native._engine", unavailable)
    output = call("find", "decisions", "Mira", "--through", "owns", code=3)
    assert output["code"] == "graph_unavailable"
    assert output["result"]["graph"]["count"] is None

    original = LocalGraphSession.__exit__

    def cleanup_failure(self, *args):
        original(self, *args)
        raise GraphSessionError(GraphFailure(code="cleanup_failed"))

    monkeypatch.setattr(LocalGraphSession, "__exit__", cleanup_failure)
    failed = call("find", "decisions", "Mira", "--through", "owns", code=3)
    assert failed["code"] == "cleanup_failed" and not failed["result"]


def test_direct_query_partial_count_is_not_promoted_to_exact(recorded, monkeypatch):
    from kg.models.foundation import QueryBudget
    from kg.query import QueryService

    _, _, _, _, mappings = recorded
    execute = QueryService.execute

    def limited(self, request):
        return execute(
            self,
            request.model_copy(
                update={
                    "budget": QueryBudget(max_records=1),
                }
            ),
        )

    monkeypatch.setattr(QueryService, "execute", limited)
    output = call("find", "decisions", "entity:" + mappings["atlas"], code=3)
    result = output["result"]["query"]["result"]
    assert result["outcome"] == "partial"
    assert not result["data"]["exact"]
    assert output["code"] == "record_budget"


def test_read_authorization_not_weakened(recorded):
    _, _, _, _, mappings = recorded
    profile = load_profile()
    profile_path().write_text(
        profile.model_copy(
            update={
                "scope": profile.scope.model_copy(
                    update={
                        "access": profile.scope.access.model_copy(
                            update={"policy_version": "stale"}
                        )
                    }
                )
            }
        ).model_dump_json()
    )
    assert call("read", "entity:" + mappings["atlas"], code=3)["code"] == "forbidden"
