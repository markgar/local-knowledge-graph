from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from kg.models.foundation import (
    MAX_TEXT_BYTES,
    BatchResult,
    ChangeSet,
    ContentResult,
    FoundationCapabilities,
    QueryRequest,
    QueryResult,
    SourceMetadata,
    SuppliedContent,
    SynchronizationSnapshot,
    WriteBatch,
    WriteRequest,
)

FIXTURES = Path(__file__).resolve().parents[2] / "corpora" / "foundation"


def enrichment() -> dict:
    return json.loads((FIXTURES / "enrichment.json").read_text())


def query() -> dict:
    return json.loads((FIXTURES / "query.json").read_text())


def document() -> dict:
    request = enrichment()
    request["scope"]["access"]["grants"] = ["read", "write_documents"]
    request["payload"] = {
        "operation": "put_document",
        "document": {
            "source_namespace": "email", "synchronization_scope": "inbox", "external_id": "item-1",
        },
        "precondition": {"kind": "create"},
        "content": {
            "text": "A\r\nCafe\u0301 \U0001f680",
            "anchors": [{
                "local_id": "quote", "start": 3, "end": 10, "quote": "Cafe\u0301 \U0001f680",
            }],
            "passage_policy": "supplied/1",
        },
        "metadata": {"title": "Original", "location": "synthetic:1"},
    }
    return request


def parse_request(value: dict) -> WriteRequest:
    return WriteRequest.model_validate_json(json.dumps(value))


@pytest.mark.parametrize("factory", [document, enrichment])
def test_request_json_round_trip(factory) -> None:
    request = parse_request(factory())
    assert WriteRequest.model_validate_json(request.model_dump_json()) == request
    assert request.contract_version == "foundation/1"
    with pytest.raises(ValidationError):
        request.request_id = "changed"


def test_exact_unicode_and_no_normalization() -> None:
    payload = document()["payload"]["content"]
    content = SuppliedContent.model_validate_json(json.dumps(payload))
    assert content.content_hash == hashlib.sha256(payload["text"].encode()).hexdigest()
    assert content.anchors[0].end == len(content.text)
    assert len(content.text.encode()) > len(content.text)
    assert "\\r\\n" in content.model_dump_json()


@pytest.mark.parametrize("field,value", [
    ("start", -1), ("start", 10), ("end", 11), ("end", 3), ("end", True),
    ("start", "3"), ("quote", "Café 🚀"), ("quote", "wrong"),
])
def test_invalid_exact_ranges(field, value) -> None:
    request = document()
    request["payload"]["content"]["anchors"][0][field] = value
    with pytest.raises(ValidationError):
        parse_request(request)


def test_text_byte_boundary_and_surrogates() -> None:
    content = SuppliedContent(text="x" * MAX_TEXT_BYTES, passage_policy="plain/1")
    assert len(content.text) == MAX_TEXT_BYTES
    with pytest.raises(ValidationError):
        SuppliedContent(text="é" * (MAX_TEXT_BYTES // 2 + 1), passage_policy="plain/1")
    with pytest.raises(ValidationError):
        SuppliedContent(text="\ud800", passage_policy="plain/1")


@pytest.mark.parametrize("mutate", [
    lambda r: r.update(contract_version="2"),
    lambda r: r.update(arbitrary_sql="delete"),
    lambda r: r["payload"].update(operation="execute"),
    lambda r: r["payload"].pop("precondition"),
    lambda r: r["payload"].update(precondition={"kind": "match"}),
    lambda r: r["payload"]["document"].update(source_namespace="denied"),
    lambda r: r["scope"]["access"].update(grants=["read"]),
    lambda r: r["scope"]["access"].update(namespaces=["email", "email"]),
    lambda r: r["attribution"].update(owner_id=" "),
])
def test_invalid_write_envelopes(mutate) -> None:
    request = document()
    mutate(request)
    with pytest.raises(ValidationError):
        parse_request(request)


def test_remove_requires_state_not_create() -> None:
    request = document()
    payload = request["payload"]
    payload["operation"] = "remove_document"
    del payload["content"], payload["metadata"]
    with pytest.raises(ValidationError):
        parse_request(request)
    payload["precondition"] = {"kind": "match", "state_version": "s1"}
    assert parse_request(request).payload.operation == "remove_document"


@pytest.mark.parametrize("mutate", [
    lambda p: p["changes"][2]["entity"].update(local_id="missing"),
    lambda p: p["changes"][3]["object"]["entity"].update(local_id="sam-alias"),
    lambda p: p["changes"][1].update(local_id="sam"),
    lambda p: p["dependencies"].pop(),
    lambda p: p["dependencies"][0].update(revision_id="old"),
    lambda p: p["dependencies"].append(p["dependencies"][0]),
    lambda p: p["changes"][0]["support"]["evidence"][0].update(corpus_id="elsewhere"),
    lambda p: p["changes"][0]["support"]["evidence"][0].update(source_namespace="denied"),
    lambda p: p["changes"][0]["support"]["evidence"].clear(),
    lambda p: p["changes"][3].update(predicate="unvalidated predicate"),
    lambda p: p["changes"][3].update(object={"kind": "json", "value": {"a": 1}}),
])
def test_invalid_change_set(mutate) -> None:
    request = enrichment()
    mutate(request["payload"])
    with pytest.raises(ValidationError):
        parse_request(request)


def test_forward_local_references_are_within_atomic_set() -> None:
    request = enrichment()
    request["payload"]["changes"].reverse()
    assert isinstance(parse_request(request).payload, ChangeSet)


def test_seed_support_requires_explicit_grant_and_no_dependencies() -> None:
    request = enrichment()
    request["payload"]["dependencies"] = []
    request["payload"]["changes"] = [{
        "kind": "entity", "local_id": "seed", "name": "Sam",
        "support": {
            "kind": "seed", "source_namespace": "email",
            "seed_set_id": "manifest", "seed_key": "manifest:1",
        },
    }]
    with pytest.raises(ValidationError, match="seed grant"):
        parse_request(request)
    request["scope"]["access"]["grants"].append("seed")
    parse_request(request)


@pytest.mark.parametrize("kind,value", [
    ("string", "open"), ("integer", 42), ("boolean", True),
    ("timestamp", "2026-09-20T00:00:00Z"),
])
def test_assertion_values_round_trip(kind, value) -> None:
    request = enrichment()
    request["payload"]["changes"][3]["object"] = {"kind": kind, "value": value}
    request["payload"]["changes"][3]["object_classification"] = None
    parse_request(request)


@pytest.mark.parametrize("kind,value", [
    ("integer", True), ("integer", "42"), ("boolean", 1),
    ("timestamp", "2026-09-20T00:00:00"), ("string", ""),
])
def test_assertion_values_do_not_coerce(kind, value) -> None:
    request = enrichment()
    request["payload"]["changes"][3]["object"] = {"kind": kind, "value": value}
    request["payload"]["changes"][3]["object_classification"] = None
    with pytest.raises(ValidationError):
        parse_request(request)


def test_mentions_identifiers_and_stored_entities() -> None:
    request = enrichment()
    alias = request["payload"]["changes"][2]
    alias["entity"] = {"kind": "stored", "entity_id": "stored-sam"}
    alias["kind"] = "identifier"
    alias["scheme"] = "email"
    alias["value"] = alias.pop("alias")
    parse_request(request)
    alias["kind"] = "mention"
    del alias["scheme"], alias["value"]
    parse_request(request)
    alias["support"]["evidence"][0]["passage_id"] = None
    with pytest.raises(ValidationError, match="passage"):
        parse_request(request)


def test_limits_are_executable_not_advisory() -> None:
    request = enrichment()
    entity = request["payload"]["changes"][0]
    request["payload"]["changes"] = [
        {**entity, "local_id": f"entity-{index}"} for index in range(100)
    ]
    request["payload"]["dependencies"] = request["payload"]["dependencies"][:1]
    parse_request(request)
    request["payload"]["changes"].append({**entity, "local_id": "overflow"})
    with pytest.raises(ValidationError):
        parse_request(request)
    request = document()
    request["payload"]["content"]["text"] = "\x00" * 1_400_000
    request["payload"]["content"]["anchors"] = []
    with pytest.raises(ValidationError, match="serialized byte"):
        parse_request(request)


def test_metadata_validation() -> None:
    with pytest.raises(ValidationError):
        SourceMetadata.model_validate_json(json.dumps({
            "title": "x", "location": "x", "attributes": [{"key": "arbitrary", "value": {}}],
        }))
    with pytest.raises(ValidationError, match="duplicate"):
        SourceMetadata.model_validate_json(json.dumps({
            "title": "x", "location": "x",
            "attributes": [{"key": "a", "value": 1}, {"key": "a", "value": 2}],
        }))


def batch_and_result() -> tuple[dict, dict]:
    first, second = enrichment(), document()
    second["request_id"] = "document-2"
    batch = {"contract_version": "foundation/1", "batch_id": "batch", "items": [first, second]}
    result = {
        "contract_version": "foundation/1", "batch_id": "batch", "status": "partial",
        "outcomes": [
            {"request_id": first["request_id"], "status": "applied", "receipt": {
                "kind": "enrichment", "mappings": [
                    {"local_id": change["local_id"], "stored_id": f"durable-{index}"}
                    for index, change in enumerate(first["payload"]["changes"])
                ],
            }},
            {"request_id": second["request_id"], "status": "conflict",
             "error": {"code": "state_conflict", "diagnostic_id": "diagnostic-1"}},
        ],
    }
    return batch, result


def test_ordered_batch_mapping_round_trip() -> None:
    batch, result = batch_and_result()
    request = WriteBatch.model_validate_json(json.dumps(batch))
    parsed = BatchResult.model_validate_json(json.dumps(result))
    parsed.validate_for(request)
    assert BatchResult.model_validate_json(parsed.model_dump_json()) == parsed
    result["outcomes"].reverse()
    with pytest.raises(ValueError, match="input order"):
        BatchResult.model_validate_json(json.dumps(result)).validate_for(request)


@pytest.mark.parametrize("mutate", [
    lambda r: r.update(status="complete"),
    lambda r: r["outcomes"][0].pop("receipt"),
    lambda r: r["outcomes"][1].update(status="applied"),
    lambda r: r["outcomes"][1]["error"].update(code="forbidden"),
    lambda r: r["outcomes"][1]["error"].update(message="source text"),
])
def test_result_rejects_success_shaped_errors(mutate) -> None:
    _, result = batch_and_result()
    mutate(result)
    with pytest.raises(ValidationError):
        BatchResult.model_validate_json(json.dumps(result))


def test_result_mapping_must_cover_whole_set() -> None:
    batch, result = batch_and_result()
    result["outcomes"][0]["receipt"]["mappings"].pop()
    with pytest.raises(ValueError, match="every change"):
        BatchResult.model_validate_json(json.dumps(result)).validate_for(
            WriteBatch.model_validate_json(json.dumps(batch))
        )


def test_legacy_unavailable_is_not_fabricated_empty_content() -> None:
    result = ContentResult(
        contract_version="foundation/1", document_id="legacy", revision_id="r1",
        state="unavailable", text=None,
    )
    assert '"text":null' in result.model_dump_json()
    with pytest.raises(ValidationError):
        ContentResult(
            contract_version="foundation/1", document_id="legacy", revision_id="r1",
            state="unavailable", text="",
        )
    ContentResult(
        contract_version="foundation/1", document_id="empty", revision_id="r1",
        state="available", text="",
    )


def test_query_round_trip_and_exhaustive_count_dependency() -> None:
    request = QueryRequest.model_validate_json(json.dumps(query()))
    assert QueryRequest.model_validate_json(request.model_dump_json()) == request
    assert request.steps[-1].operation == "count"


@pytest.mark.parametrize("mutate", [
    lambda q: q["steps"][0].update(operation="sql"),
    lambda q: q["steps"][0].update(step_id="tasks"),
    lambda q: q["steps"][1].update(entity_step="missing"),
    lambda q: q["steps"][1].update(entity_step="tasks"),
    lambda q: q["steps"][2].update(records_step="sam"),
    lambda q: q["steps"].reverse(),
    lambda q: q.update(output_step="missing"),
    lambda q: q["budget"].update(max_operations=2),
    lambda q: q["steps"][1].update(record_type="decision"),
    lambda q: q["scope"]["access"].update(grants=["write_documents"]),
])
def test_query_rejects_invalid_plans(mutate) -> None:
    request = query()
    mutate(request)
    with pytest.raises(ValidationError):
        QueryRequest.model_validate_json(json.dumps(request))


def query_result() -> dict:
    return {
        "contract_version": "foundation/1", "request_id": "count-1", "scope": query()["scope"],
        "outcome": "complete", "read_state_id": "s1", "result_set_id": "set-1",
        "operations_executed": 3, "records_examined": 216, "exhaustion": "eligible_set",
        "data": {
            "kind": "aggregate", "count": 216, "exact": True, "supporting_records_step": "tasks",
        },
    }


def test_exact_count_result_and_state_changed_failure() -> None:
    result = QueryResult.model_validate_json(json.dumps(query_result()))
    result.validate_for(QueryRequest.model_validate_json(json.dumps(query())))
    assert QueryResult.model_validate_json(result.model_dump_json()) == result
    payload = query_result()
    payload.update(outcome="state_changed", data=None,
                   error={"code": "state_changed", "diagnostic_id": "d1"})
    QueryResult.model_validate_json(json.dumps(payload))


@pytest.mark.parametrize("mutate", [
    lambda r: r.update(outcome="partial"),
    lambda r: r.update(truncated=True),
    lambda r: r.update(exhaustion="candidate_pool"),
    lambda r: r.update(read_state_id=None),
    lambda r: r.update(outcome="state_changed"),
    lambda r: r.update(continuation="cursor", result_set_id=None),
])
def test_query_results_cannot_claim_false_exactness(mutate) -> None:
    result = query_result()
    mutate(result)
    with pytest.raises(ValidationError):
        QueryResult.model_validate_json(json.dumps(result))


def test_capabilities_are_separate_from_product_interface() -> None:
    capabilities = FoundationCapabilities(contract_version="foundation/1")
    assert capabilities.implementation == "validation_only"
    assert capabilities.max_changes == 100
    assert capabilities.max_text_bytes == MAX_TEXT_BYTES
    for model in (WriteRequest, WriteBatch, BatchResult, QueryRequest, QueryResult):
        assert model.model_json_schema()["additionalProperties"] is False


def test_snapshot_is_scoped_and_records_completion_not_deletion() -> None:
    request = document()
    payload = {
        "contract_version": "foundation/1", "scope": request["scope"],
        "attribution": request["attribution"], "source_namespace": "email",
        "synchronization_scope": "inbox", "run_id": "run-1",
        "expected_generation": "g1", "enumeration": "partial",
    }
    snapshot = SynchronizationSnapshot.model_validate_json(json.dumps(payload))
    assert snapshot.enumeration == "partial"
    payload["source_namespace"] = "denied"
    with pytest.raises(ValidationError):
        SynchronizationSnapshot.model_validate_json(json.dumps(payload))


@pytest.mark.parametrize("mutate", [
    lambda r: r.update(request_id="wrong"),
    lambda r: r["scope"].update(corpus_id="wrong"),
    lambda r: r.update(operations_executed=4),
    lambda r: r["data"].update(supporting_records_step="missing"),
    lambda r: r["data"].update(exact=False),
    lambda r: r.update(data={"kind": "entities", "entity_ids": ["sam"]}),
])
def test_query_result_correlation(mutate) -> None:
    result = query_result()
    mutate(result)
    with pytest.raises(ValueError):
        QueryResult.model_validate_json(json.dumps(result)).validate_for(
            QueryRequest.model_validate_json(json.dumps(query()))
        )


def test_query_other_operators_and_result_scope() -> None:
    request = query()
    request["steps"] = [
        request["steps"][0],
        {"operation": "paths", "step_id": "paths", "entity_step": "sam",
         "predicate": "work:owns", "max_hops": 3},
    ]
    request["output_step"] = "paths"
    QueryRequest.model_validate_json(json.dumps(request))
    request["steps"][1]["max_hops"] = 4
    with pytest.raises(ValidationError):
        QueryRequest.model_validate_json(json.dumps(request))
    evidence = enrichment()["payload"]["changes"][0]["support"]["evidence"][0]
    request["steps"] = [{"operation": "evidence", "step_id": "read", "evidence": evidence}]
    request["output_step"] = "read"
    QueryRequest.model_validate_json(json.dumps(request))
    result = query_result()
    result["data"] = {"kind": "ranked", "hits": [{"evidence": evidence, "score": 1.5}]}
    QueryResult.model_validate_json(json.dumps(result))
    evidence["corpus_id"] = "denied"
    with pytest.raises(ValidationError, match="outside declared scope"):
        QueryResult.model_validate_json(json.dumps(result))


def test_batch_count_and_size_limits() -> None:
    request = document()
    batch = {"contract_version": "foundation/1", "batch_id": "b1", "items": [
        {**request, "request_id": f"request-{index}"} for index in range(100)
    ]}
    WriteBatch.model_validate_json(json.dumps(batch))
    batch["items"].append({**request, "request_id": "overflow"})
    with pytest.raises(ValidationError):
        WriteBatch.model_validate_json(json.dumps(batch))
    request["payload"]["content"]["text"] = "x" * MAX_TEXT_BYTES
    request["payload"]["content"]["anchors"] = []
    batch["items"] = [{**request, "request_id": f"large-{index}"} for index in range(4)]
    with pytest.raises(ValidationError, match="batch exceeds serialized"):
        WriteBatch.model_validate_json(json.dumps(batch))


def test_total_evidence_limit_counts_occurrences() -> None:
    request = enrichment()
    entity = request["payload"]["changes"][0]
    evidence = entity["support"]["evidence"][0]
    entity["support"]["evidence"] = [
        {**evidence, "anchor_id": f"anchor-{index}"} for index in range(200)
    ]
    request["payload"]["changes"] = [entity]
    request["payload"]["dependencies"] = request["payload"]["dependencies"][:1]
    parse_request(request)
    request["payload"]["changes"].append({
        **entity, "local_id": "second",
        "support": {"kind": "source", "evidence": [evidence]},
    })
    with pytest.raises(ValidationError, match="total evidence"):
        parse_request(request)


def test_ambiguity_must_belong_to_output_dependency_chain() -> None:
    request = query()
    request["steps"].append({"operation": "search", "step_id": "search", "text": "Sam"})
    request["output_step"] = "search"
    result = query_result()
    result.update(outcome="ambiguous", data={"kind": "entities", "entity_ids": ["sam-a", "sam-b"]})
    with pytest.raises(ValueError, match="output dependency chain"):
        QueryResult.model_validate_json(json.dumps(result)).validate_for(
            QueryRequest.model_validate_json(json.dumps(request))
        )
    request["steps"] = request["steps"][-1:]
    result["operations_executed"] = 1
    with pytest.raises(ValueError, match="output dependency chain"):
        QueryResult.model_validate_json(json.dumps(result)).validate_for(
            QueryRequest.model_validate_json(json.dumps(request))
        )
    ambiguous = QueryRequest.model_validate_json((FIXTURES / "query-ambiguous.json").read_text())
    result["request_id"] = ambiguous.request_id
    QueryResult.model_validate_json(json.dumps(result)).validate_for(ambiguous)


@pytest.mark.parametrize("entity_ids", [[], ["sam-a", "sam-b"]])
def test_complete_resolution_requires_one_candidate(entity_ids) -> None:
    request = query()
    request["steps"] = [{"operation": "resolve", "step_id": "sam", "name": "Sam"}]
    request["output_step"] = "sam"
    result = query_result()
    result.update(operations_executed=1, data={"kind": "entities", "entity_ids": entity_ids})
    with pytest.raises(ValueError, match="exactly one"):
        QueryResult.model_validate_json(json.dumps(result)).validate_for(
            QueryRequest.model_validate_json(json.dumps(request))
        )


def test_exact_entity_selection_is_executable_and_unambiguous() -> None:
    request = query()
    request["steps"] = request["steps"][:1]
    request["output_step"] = "sam"
    parsed = QueryRequest.model_validate_json(json.dumps(request))
    result = query_result()
    result.update(operations_executed=1,
                  data={"kind": "entities", "entity_ids": ["sam-primary"]})
    QueryResult.model_validate_json(json.dumps(result)).validate_for(parsed)
    result["data"]["entity_ids"] = ["sam-alternative"]
    with pytest.raises(ValueError, match="requested entity"):
        QueryResult.model_validate_json(json.dumps(result)).validate_for(parsed)
    result.update(outcome="ambiguous",
                  data={"kind": "entities", "entity_ids": ["sam-primary", "sam-alternative"]})
    with pytest.raises(ValueError, match="name resolution"):
        QueryResult.model_validate_json(json.dumps(result)).validate_for(parsed)
    request["steps"][0]["name"] = "Sam"
    with pytest.raises(ValueError, match="exactly one"):
        QueryRequest.model_validate_json(json.dumps(request))
    del request["steps"][0]["name"], request["steps"][0]["entity_id"]
    with pytest.raises(ValueError, match="exactly one"):
        QueryRequest.model_validate_json(json.dumps(request))


def test_record_selection_and_path_hop_constraints() -> None:
    request = query()
    request["steps"] = request["steps"][:2]
    request["output_step"] = "tasks"
    support = enrichment()["payload"]["changes"][0]["support"]
    result = query_result()
    result.update(operations_executed=2, data={
        "kind": "records",
        "records": [{"record_id": "r1", "record_type": "decision", "support": support}],
    })
    with pytest.raises(ValueError, match="record type"):
        QueryResult.model_validate_json(json.dumps(result)).validate_for(
            QueryRequest.model_validate_json(json.dumps(request))
        )
    result["data"]["records"][0]["record_type"] = "action"
    QueryResult.model_validate_json(json.dumps(result)).validate_for(
        QueryRequest.model_validate_json(json.dumps(request))
    )
    request["steps"][1] = {
        "operation": "paths", "step_id": "paths", "entity_step": "sam",
        "predicate": "work:owns", "max_hops": 1,
    }
    request["output_step"] = "paths"
    result["data"] = {"kind": "paths", "paths": [{
        "entity_ids": ["a", "b", "c", "d"], "assertion_ids": ["ab", "bc", "cd"],
        "support": support,
    }]}
    with pytest.raises(ValueError, match="hop limit"):
        QueryResult.model_validate_json(json.dumps(result)).validate_for(
            QueryRequest.model_validate_json(json.dumps(request))
        )
    request["steps"][1]["max_hops"] = 3
    QueryResult.model_validate_json(json.dumps(result)).validate_for(
        QueryRequest.model_validate_json(json.dumps(request))
    )


def test_aggregate_support_requires_retained_selection_identity() -> None:
    result = query_result()
    result["result_set_id"] = None
    with pytest.raises(ValueError, match="retained result set"):
        QueryResult.model_validate_json(json.dumps(result))
