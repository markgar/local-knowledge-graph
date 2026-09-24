from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from support.evidence import environment, put, receipt
from support.modules import module

from kg.evidence import EvidenceServiceError
from kg.models.foundation import SuppliedAnchor


@pytest.mark.service
def test_a01_actual_workload_indices_and_isolated_copy(tmp_path: Path) -> None:
    work = environment(tmp_path / "e.db")
    isolated = environment(work.database.path, corpus="isolated")
    documents = module("benchmarks/foundation/workload.py").documents()
    saved = []
    for env, source in ((work, documents[0]), (work, documents[1]), (isolated, documents[0])):
        request = put(
            env.scope,
            external=source["external_id"],
            namespace=source["source_namespace"],
            text=source["text"],
        )
        first = env.service.write(request)
        replay = env.service.write(request.model_copy(update={"request_id": "replay"}))
        assert replay.receipt == first.receipt and replay.status == first.status
        saved.append(receipt(first))
    assert len({item.document_id for item in saved}) == 3
    for env, other in ((work, saved[2]), (isolated, saved[0])):
        with pytest.raises(EvidenceServiceError) as error:
            env.service.document(env.scope, other.document_id)
        assert error.value.failure.code == "not_found"
    with work.database.connection() as connection:
        for table in ("document", "document_state", "write_key", "write_provenance"):
            assert connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 3


@pytest.mark.service
def test_a02_exact_recipe_and_rejected_ranges(tmp_path: Path) -> None:
    env = environment(tmp_path / "e.db")
    text = "A\r\nCafe\u0301 \U0001f680"
    base = put(env.scope, text=text)
    anchor = SuppliedAnchor(local_id="slice", start=3, end=10, quote="Cafe\u0301 \U0001f680")
    request = base.model_copy(
        update={
            "payload": base.payload.model_copy(
                update={
                    "content": base.payload.content.model_copy(update={"anchors": (anchor,)}),
                }
            )
        }
    )
    saved = receipt(env.service.write(request))
    env.database.initialize()
    assert env.service.content(env.scope, saved.document_id, saved.revision_id).text == text
    assert (
        env.service.revisions(
            env.scope,
            saved.document_id,
        )
        .entries[0]
        .content_hash
        == hashlib.sha256(text.encode()).hexdigest()
    )
    evidence = env.service.anchors(
        env.scope,
        saved.document_id,
        saved.processing.state_version,
    ).entries[0]
    assert (evidence.start, evidence.end, evidence.quote) == (3, 10, anchor.quote)
    for bad in (
        anchor.model_copy(update={"end": 11}),
        anchor.model_copy(update={"quote": "Caf\u00e9 \U0001f680"}),
    ):
        malformed = request.model_copy(
            update={
                "payload": request.payload.model_copy(
                    update={
                        "content": request.payload.content.model_copy(update={"anchors": (bad,)}),
                    }
                )
            }
        )
        with pytest.raises(EvidenceServiceError) as error:
            env.service.write(malformed)
        assert error.value.failure.code == "invalid_request"
    with env.database.connection() as connection:
        for table in ("document", "revision", "document_state", "write_key"):
            assert connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 1
