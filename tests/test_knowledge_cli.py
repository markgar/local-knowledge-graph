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
    head = call("schema", "show")["result"]["head"]
    file.write_text(json.dumps({
        "expected_schema_revision": head, "support": [support], "changes": changes,
    }))
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
    assert query["count"] == 2 and query["exact"] and query["selection_complete"]
    assert not query["display_complete"]
    decision = query["decisions"][0]
    assert decision["assertion_id"] == query["inspection"]["records"][0]["record_id"]
    assert decision["text"] == "Ship Friday" and decision["support"] == [support]
    assert call("read", decision["target"])["result"]["support"] == [support]
    assert call("read", decision["evidence_targets"][0])["result"]["support"] == support
    human = runner.invoke(app, ["find", "decisions", "Atlas", "--limit", "1"]).stdout
    assert "Ship Friday" in human and "2 (exact" in human
    assert "Display complete: False" in human and "Query:" not in human
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
    assert output["result"]["count"] == 0
    assert not output["result"]["exact"]
    assert not output["result"]["selection_complete"]
    assert not output["result"]["display_complete"]
    assert not output["result"]["display_truncated"]
    assert output["result"]["decisions"] == []


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


def test_named_support_and_returned_entities_roundtrip(recorded):
    directory, _, added, support, mappings = recorded
    stored = call("read", "entity:" + mappings["atlas"])["result"]["reference"]
    changes = [
        entity("new", "New", "project", support),
        assertion("reuse", stored, "decision", {"kind": "string", "value": "Ship Friday"}, support),
    ]
    for change in changes:
        change["support"]["evidence"] = ["meeting"]
    path = directory / "named.json"
    path.write_text(json.dumps({
        "expected_schema_revision": call("schema", "show")["result"]["revision"],
        "support": {"meeting": support}, "changes": changes,
    }))
    response = call("record", path)["result"]["write"]
    assert len(response["receipt"]["mappings"]) == 2
    identifier = response["receipt"]["mappings"][1]["stored_id"]
    result = call("read", "fact:" + identifier)["result"]
    assert result["support"] == [support]
    assert result["contribution"]["payload"]["subject"] == stored
    call("remove", "fact:" + identifier, "--confirm")
    historical = call("read", "fact:" + identifier, "--history")["result"]["contribution"]
    assert not historical["is_current"]
    # Never substitute the new state when the supplied capture becomes stale.
    revised = directory / "revised.md"
    revised.write_text("New source state.")
    call("update", added["target"], revised, "--expect", added["state"])
    stale = call("record", path, code=4)
    assert stale["result"]["write"]["receipt"] is None


@pytest.mark.parametrize(
    "mutation",
    ["unknown", "unused", "duplicate_capture", "duplicate_name", "duplicate_occurrence",
     "conflict", "mixed", "native_names", "extra", "blank", "empty", "malformed", "missing"],
)
def test_named_input_rejection_precedes_any_write(recorded, monkeypatch, mutation):
    from copy import deepcopy

    directory, _, _, support, _ = recorded
    changes = [entity("new", "New", "project", support)]
    changes[0]["support"]["evidence"] = ["meeting"]
    value = {
        "expected_schema_revision": call("schema", "show")["result"]["revision"],
        "support": {"meeting": deepcopy(support)}, "changes": changes,
    }
    if mutation == "unknown":
        changes[0]["support"]["evidence"] = ["missing"]
    elif mutation == "unused":
        value["support"]["unused"] = support
    elif mutation == "duplicate_capture":
        value["support"]["same"] = support
        changes[0]["support"]["evidence"].append("same")
    elif mutation == "duplicate_occurrence":
        changes[0]["support"]["evidence"].append("meeting")
    elif mutation == "conflict":
        conflicting = deepcopy(support)
        conflicting["reference"]["anchor_id"] = "another-anchor"
        conflicting["state_version"] = "different-state"
        value["support"]["conflict"] = conflicting
        changes[0]["support"]["evidence"].append("conflict")
    elif mutation == "mixed":
        changes[0]["support"]["evidence"].append(support["reference"])
    elif mutation == "native_names":
        value["support"] = [support]
    elif mutation == "extra":
        changes[0]["support"]["extra"] = True
    elif mutation == "blank":
        value["support"] = {" ": support}
        changes[0]["support"]["evidence"] = [" "]
    elif mutation == "empty":
        value["support"] = {}
    elif mutation == "malformed":
        value["support"]["meeting"] = "not a capture"
    elif mutation == "missing":
        del value["support"]["meeting"]["state_version"]
    text = json.dumps(value)
    if mutation == "duplicate_name":
        text = text.replace('"meeting":', '"meeting": {}, "meeting":', 1)
    path = directory / "bad-named.json"
    path.write_text(text)
    writes = []
    monkeypatch.setattr(EvidenceService, "write", lambda *args: writes.append(args))
    call("record", path, code=2)
    assert not writes
    assert call("find", "entities", "New")["status"] == "empty"


def test_named_conjunction_and_expanded_occurrence_limit(recorded, monkeypatch):
    from copy import deepcopy

    directory, _, _, support, _ = recorded
    another = directory / "another.md"
    another.write_text("New project has corroborating support.")
    other_target = call("add", another)["result"]["target"]
    other_support = call("read", other_target)["result"]["entries"][0]["support"]
    change = entity("new", "New", "project", support)
    change["support"]["evidence"] = ["meeting", "other"]
    path = directory / "conjunction.json"
    value = {
        "expected_schema_revision": call("schema", "show")["result"]["revision"],
        "support": {"meeting": support, "other": other_support}, "changes": [change],
    }
    path.write_text(json.dumps(value))
    receipt = call("record", path)["result"]["write"]["receipt"]
    stored = receipt["mappings"][0]["stored_id"]
    entity_read = call("read", "entity:" + stored)["result"]
    assert entity_read["entries"][0]["support"] == [support, other_support]

    # 100 changes x two supports is accepted; one more occurrence must fail,
    # even though the JSON declares only three supports.
    value["changes"] = [
        {**deepcopy(change), "local_id": f"new{i}", "name": f"New {i}"} for i in range(100)
    ]
    path.write_text(json.dumps(value))
    assert len(call("record", path)["result"]["write"]["receipt"]["mappings"]) == 100
    third = deepcopy(support)
    third["reference"]["anchor_id"] = "distinct-anchor"
    value["support"]["third"] = third
    value["changes"][0]["support"]["evidence"].append("third")
    path.write_text(json.dumps(value))
    writes = []
    monkeypatch.setattr(EvidenceService, "write", lambda *args: writes.append(args))
    call("record", path, code=2)
    assert not writes


def test_record_file_and_expanded_request_byte_limits(recorded, monkeypatch):
    from kg.client.knowledge import record_input
    from kg.models.foundation import MAX_REQUEST_BYTES, WriteRequest

    directory, _, _, support, _ = recorded
    change = entity("new", "New", "project", support)
    change["support"]["evidence"] = ["meeting"]
    path = directory / "size.json"
    text = json.dumps({
        "expected_schema_revision": call("schema", "show")["result"]["revision"],
        "support": {"meeting": support}, "changes": [change],
    })
    path.write_text(text + " " * (MAX_REQUEST_BYTES - len(text.encode())))
    assert record_input(path).payload().changes
    with path.open("a") as stream:
        stream.write(" ")
    writes = []
    monkeypatch.setattr(EvidenceService, "write", lambda *args: writes.append(args))
    call("record", path, code=2)
    path.write_text(text)
    profile = load_profile()
    request = WriteRequest(
        contract_version="foundation/1", request_id="x" * 36, retry_key="y" * 36,
        scope=profile.scope, attribution=profile.attribution, payload=record_input(path).payload(),
    )
    size = len(request.model_dump_json().encode())
    assert size > len(text.encode())
    # Test the existing full-envelope bound after expansion, not only file size.
    monkeypatch.setattr("kg.models.foundation.MAX_REQUEST_BYTES", size - 1)
    call("record", path, code=2)
    assert not writes


def test_entity_matching_help_and_empty_guidance(recorded):
    help_text = runner.invoke(app, ["find", "entities", "--help"]).stdout
    assert "not semantic search" in help_text
    empty = call("find", "entities", "unmatched")
    assert "not semantic search" in empty["message"]
    assert "do not prove an entity is new" in empty["message"]
    assert "source evidence" in empty["message"] and "--after" in empty["message"]


@pytest.mark.parametrize("mutation", ["withdraw", "update", "unrelated", "revoke", "expiry"])
def test_direct_hydration_withholds_composite_after_change(recorded, monkeypatch, mutation):
    from kg.evidence import EvidenceAdministration
    from kg.models.evidence import LocalAdminAuthority, LocalPolicy
    from kg.query import QueryService

    directory, file, added, _, mappings = recorded
    original = QueryService.inspect_support
    calls = []

    def inspect(self, request):
        calls.append(request)
        if len(calls) == 2:
            if mutation == "withdraw":
                call("remove", "fact:" + mappings["release"], "--confirm")
            elif mutation == "update":
                file.write_text("Changed source.")
                call("update", added["target"], file, "--expect", added["state"])
            elif mutation == "unrelated":
                other = directory / "unrelated.md"
                other.write_text("Unrelated new document.")
                call("add", other)
            elif mutation == "revoke":
                profile = load_profile()
                EvidenceAdministration(
                    profile.database(), LocalAdminAuthority(principal_id="local"),
                ).replace_policy(
                    LocalPolicy(corpus_id=profile.scope.corpus_id, bindings=(), grants=()),
                    profile.scope.access.policy_version,
                )
            else:
                self._support.sets[request.result_set_id].expires = 0
        return original(self, request)

    monkeypatch.setattr(QueryService, "inspect_support", inspect)
    result = call("find", "decisions", "Atlas", code=3)
    assert len(calls) == 2 and result["code"] in {"state_changed", "forbidden", "not_found"}
    assert not ({"query", "inspection", "decisions", "count"} & result["result"].keys())


@pytest.mark.parametrize("mutation", ["identity", "support", "denied", "budget"])
def test_direct_hydration_rejects_mismatch_and_read_failure(recorded, monkeypatch, mutation):
    from kg.evidence import EvidenceServiceError

    original = KnowledgeService.contribution

    def changed(self, scope, identifier, **kwargs):
        if mutation in {"denied", "budget"}:
            raise EvidenceServiceError("forbidden" if mutation == "denied" else "budget_exceeded")
        result = original(self, scope, identifier, **kwargs)
        return result.model_copy(update={
            "contribution_id": "wrong-id",
        } if mutation == "identity" else {"evidence": ()})

    monkeypatch.setattr(KnowledgeService, "contribution", changed)
    result = call("find", "decisions", "Atlas", code=3)
    assert not ({"query", "inspection", "decisions", "count"} & result["result"].keys())


@pytest.mark.parametrize("field", ["total", "exact", "read_state_id", "result_set_id",
                                 "records_step_id", "records", "next_ordinal", "exhausted"])
def test_direct_inspection_correlation_rejects_mismatch(recorded, monkeypatch, field):
    from kg.query import QueryService

    original = QueryService.inspect_support
    calls = []

    def changed(self, request):
        result = original(self, request)
        calls.append(request)
        if len(calls) == 2:
            values = {
                "total": 2, "exact": False, "read_state_id": "other", "result_set_id": "other",
                "records_step_id": "other", "records": (), "next_ordinal": 1, "exhausted": False,
            }
            return result.model_copy(update={field: values[field]})
        return result

    monkeypatch.setattr(QueryService, "inspect_support", changed)
    result = call("find", "decisions", "Atlas", code=3)
    assert result["code"] == "state_changed"
    assert not ({"query", "inspection", "decisions", "count"} & result["result"].keys())


def test_direct_display_hydrates_only_selected_ids(recorded, monkeypatch):
    from kg.query import QueryService

    read = KnowledgeService.contribution
    inspect = QueryService.inspect_support
    reads, inspections = [], []

    def observed(self, scope, identifier, **kwargs):
        reads.append(identifier)
        return read(self, scope, identifier, **kwargs)

    def inspected(self, request):
        inspections.append(request)
        return inspect(self, request)

    monkeypatch.setattr(KnowledgeService, "contribution", observed)
    monkeypatch.setattr(QueryService, "inspect_support", inspected)
    result = call("find", "decisions", "Atlas", "--limit", 1)["result"]
    assert reads == [entry["assertion_id"] for entry in result["decisions"]]
    assert len(inspections) == 2 and all(request.limit == 1 for request in inspections)
    assert result["display_complete"] and result["selection_complete"]


def test_named_example_executes_and_schema_describes_both_modes(configured):
    document = configured / "meeting.md"
    document.write_text("Mira owns Atlas; Atlas will ship Friday.")
    target = call("add", document)["result"]["target"]
    support = call("read", target)["result"]["entries"][0]["support"]
    example = call("record", "--example")["result"]["example"]
    value = json.loads(example.split("```json\n")[1].split("```")[0])
    value["support"]["meeting"] = support
    value["expected_schema_revision"] = call("schema", "show")["result"]["revision"]
    path = configured / "example.json"
    path.write_text(json.dumps(value))
    assert len(call("record", path)["result"]["write"]["receipt"]["mappings"]) == 4
    schema = call("record", "--schema")["result"]["schema"]
    assert len(schema["properties"]["support"]["oneOf"]) == 2
    assert [entry["properties"]["support"]["type"] for entry in schema["oneOf"]] == [
        "array", "object",
    ]
    assert schema["$defs"]["SourceSupport"]["properties"]["evidence"]["items"]["oneOf"][1][
        "type"
    ] == "string"


@pytest.mark.parametrize("truncated", [False, True])
def test_graph_decision_presentation_preserves_both_proof_sides(recorded, monkeypatch, truncated):
    from kg.graph import LocalGraphSession
    from kg.graph._session_types import GraphGeneration
    from kg.knowledge._graph_export import GraphAssertion, GraphEvidence
    from kg.knowledge._selection import DecisionDependencies, DecisionSelectionItem
    from kg.models.foundation import Path as EvidencePath
    from kg.models.foundation import Record
    from kg.models.graph import (
        GraphDecisionMember,
        GraphRelationshipDecisionsResult,
        GraphRelationshipProof,
    )

    _, _, _, support, mappings = recorded
    profile = load_profile()
    service = KnowledgeService(profile.database(), profile.identity)

    def graph_assertion(local_id):
        view = service.contribution(profile.scope, mappings[local_id])
        payload = view.payload
        is_decision = payload.object.kind == "string"
        member = DecisionSelectionItem(
            record=Record(
                record_id=view.contribution_id, record_type="decision", support=payload.support,
            ),
            subject_id=payload.subject.entity_id,
            schema_version=view.schema_version,
            dependencies=DecisionDependencies(
                assertion_support=view.evidence, subject_witness=view.witnesses[0],
            ),
        ) if is_decision else None
        return GraphAssertion(
            assertion_id=view.contribution_id, contribution_sequence=view.sequence,
            schema_version=view.schema_version, predicate=payload.predicate,
            interpretation=payload.interpretation, subject_id=payload.subject.entity_id,
            object_entity_id=None if is_decision else payload.object.entity.entity_id,
            decision_text=payload.object.value if is_decision else None,
            attribution=view.attribution, committed_at=view.committed_at,
            support=tuple(
                GraphEvidence(captured=item, passage_set_id=None) for item in view.evidence
            ),
            subject_witness=view.witnesses[0],
            object_witness=None if is_decision else view.witnesses[1], decision_member=member,
        )

    relationship, decision = graph_assertion("ownership"), graph_assertion("release")
    returned = []

    def controlled(self, request):
        result = GraphRelationshipDecisionsResult(
            request_id=request.request_id, scope=request.scope, outcome="complete",
            generation=GraphGeneration(session_id="controlled", ordinal=1),
            root_entity_id=mappings["mira"], count=2 if truncated else 1, exact=True,
            display_truncated=truncated,
            members=(GraphDecisionMember(
                decision=decision, relationship_ids=(mappings["ownership"],),
            ),),
            relationships=(GraphRelationshipProof(
                assertion=relationship,
                path=EvidencePath(
                    entity_ids=(mappings["mira"], mappings["atlas"]),
                    assertion_ids=(mappings["ownership"],),
                    support=decision.decision_member.record.support,
                ),
            ),),
        )
        returned.append(result.model_dump(mode="json"))
        return result

    def forbidden_read(*args, **kwargs):
        pytest.fail("Graph presentation must not hydrate with additional knowledge reads")

    monkeypatch.setattr(LocalGraphSession, "relationship_decisions", controlled)
    monkeypatch.setattr(KnowledgeService, "contribution", forbidden_read)
    result = call("find", "decisions", "Mira", "--through", "owns", "--limit", 1)["result"]
    assert result["graph"] == returned[0]
    assert result["count"] == (2 if truncated else 1)
    assert result["exact"] and result["selection_complete"]
    assert result["display_complete"] is not truncated
    entry = result["decisions"][0]
    assert entry["text"] == "Ship Friday" and entry["support"] == [support]
    assert entry["relationship_ids"] == [mappings["ownership"]]
    assert call("read", entry["evidence_targets"][0])["result"]["support"] == support
    human = runner.invoke(app, ["find", "decisions", "Mira", "--through", "owns"]).stdout
    assert "Ship Friday" in human and "Relationship fact:" + mappings["ownership"] in human
    assert "--owns-->" in human and "Relationship support:" in human
    assert "Graph:" not in human


def test_partial_nonzero_count_and_empty_exact_display(recorded, monkeypatch):
    from kg.models.foundation import QueryBudget
    from kg.query import QueryService

    directory, _, _, support, mappings = recorded
    change = assertion(
        "again", {"kind": "stored", "entity_id": mappings["atlas"]}, "decision",
        {"kind": "string", "value": "Ship Friday"}, support,
    )
    call("record", record_file(directory, support, [change]))
    original = QueryService.execute

    def limited(self, request):
        return original(
            self, request.model_copy(update={"budget": QueryBudget(max_records=2)}),
        )

    with monkeypatch.context() as context:
        context.setattr(QueryService, "execute", limited)
        result = call("find", "decisions", "Atlas", code=3)["result"]
    assert result["count"] == 1 and len(result["decisions"]) == 1
    assert not result["exact"] and not result["selection_complete"]
    assert not result["display_complete"] and not result["display_truncated"]
    assert result["decisions"][0]["text"] == "Ship Friday"
    call("remove", "fact:" + mappings["approval"], "--confirm")
    empty = call("find", "decisions", "Mira")["result"]
    assert empty["count"] == 0 and empty["exact"] and empty["decisions"] == []
    assert empty["selection_complete"] and empty["display_complete"]


def test_final_inspection_release_withholds_intervening_write(recorded, monkeypatch):
    from kg.query import QueryService

    directory, _, _, _, _ = recorded
    original_inspect = QueryService.inspect_support
    original_worker = QueryService._run_worker
    inspections = []

    def inspect(self, request):
        inspections.append(request)
        return original_inspect(self, request)

    def worker(self, *args, **kwargs):
        result = original_worker(self, *args, **kwargs)
        if len(inspections) == 2:
            unrelated = directory / "during-final-release.md"
            unrelated.write_text("A canonical commit during final inspection.")
            call("add", unrelated)
        return result

    monkeypatch.setattr(QueryService, "inspect_support", inspect)
    monkeypatch.setattr(QueryService, "_run_worker", worker)
    result = call("find", "decisions", "Atlas", code=3)
    assert len(inspections) == 2 and result["code"] == "state_changed"
    assert not ({"query", "inspection", "decisions", "count"} & result["result"].keys())


@pytest.mark.parametrize("oversized", ["changes", "declarations", "occurrences", "duplicates"])
def test_named_support_is_bounded_before_expansion(recorded, monkeypatch, oversized):
    from kg.client.knowledge import normalize_record
    from kg.models.foundation import EvidenceRef

    _, _, _, support, _ = recorded
    change = entity("new", "New", "project", support)
    change["support"]["evidence"] = ["meeting"]
    value = {"support": {"meeting": support}, "changes": [change]}
    if oversized == "changes":
        value["changes"] *= 101
    elif oversized == "declarations":
        value["support"] = {str(i): support for i in range(201)}
    elif oversized == "occurrences":
        change["support"]["evidence"] *= 100_000
    else:
        change["support"]["evidence"] *= 2

    def no_expansion(*args, **kwargs):
        pytest.fail("Oversized or duplicate input must fail before evidence serialization")

    monkeypatch.setattr(EvidenceRef, "model_dump", no_expansion)
    from kg.client.config import ClientError

    with pytest.raises(ClientError):
        normalize_record(value)
