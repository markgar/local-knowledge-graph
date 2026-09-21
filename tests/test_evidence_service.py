from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from support.evidence import environment, put, receipt

from kg.evidence import EvidenceServiceError
from kg.models.evidence import LocalPolicy, PolicyGrant, WriterBinding
from kg.models.foundation import (
    ExpectedState,
    RemoveDocument,
    SourceMetadata,
    SuppliedAnchor,
    WriteBatch,
    WriteRequest,
)


def test_identity_exact_content_noop_and_restore(tmp_path: Path) -> None:
    env = environment(tmp_path / "e.db")
    original = put(env.scope)
    first = receipt(env.service.write(original))
    assert first.processing.indexing == first.processing.enrichment == "pending"
    second = receipt(env.service.write(put(env.scope, namespace="email")))
    isolated = environment(env.database.path, corpus="isolated")
    third = receipt(isolated.service.write(put(isolated.scope)))
    assert len({first.document_id, second.document_id, third.document_id}) == 3
    assert len({first.revision_id, second.revision_id, third.revision_id}) == 3
    read = env.service.content(env.scope, first.document_id, first.revision_id)
    assert read.text == "A\r\nCafe\u0301 \U0001f680"
    assert read.text.encode() == original.payload.content.text.encode()
    noop = env.service.write(put(env.scope, state=first.processing.state_version))
    assert noop.status == "unchanged"
    assert receipt(noop) == first
    edited = receipt(
        env.service.write(
            put(
                env.scope,
                text="Edited",
                state=first.processing.state_version,
            )
        )
    )
    removal = WriteRequest(
        contract_version="foundation/1",
        request_id="remove",
        retry_key="remove",
        scope=env.scope,
        attribution=original.attribution,
        payload=RemoveDocument(
            operation="remove_document",
            document=original.payload.document,
            precondition=ExpectedState(kind="match", state_version=edited.processing.state_version),
        ),
    )
    removed = receipt(env.service.write(removal))
    assert removed.processing.source == "inactive"
    restored = receipt(env.service.write(put(env.scope, state=removed.processing.state_version)))
    assert restored.revision_id == first.revision_id
    assert (
        len(
            {
                first.processing.state_version,
                edited.processing.state_version,
                removed.processing.state_version,
                restored.processing.state_version,
            }
        )
        == 4
    )
    assert len(env.service.revisions(env.scope, first.document_id).entries) == 2
    assert len(env.service.history(env.scope, first.document_id).entries) == 4
    stale = env.service.write(put(env.scope, state=first.processing.state_version))
    assert stale.status == "conflict"
    with pytest.raises(EvidenceServiceError) as error:
        isolated.service.document(isolated.scope, first.document_id)
    assert error.value.failure.code == "not_found"


@pytest.mark.parametrize("text", ["", "\x00", "\ufeff\r\n", "a\rb\nc\r\n", "\U0001f680e\u0301"])
def test_all_supplied_bytes_roundtrip(tmp_path: Path, text: str) -> None:
    env = environment(tmp_path / "e.db")
    saved = receipt(env.service.write(put(env.scope, text=text)))
    env.database.initialize()
    result = env.service.content(env.scope, saved.document_id, saved.revision_id)
    assert result.state == "available"
    assert result.text == text


def test_invalid_construct_and_batch_are_atomic(tmp_path: Path) -> None:
    env = environment(tmp_path / "e.db")
    request = put(env.scope)
    bad = request.model_dump(mode="json")
    bad["payload"]["content"]["anchors"][0]["end"] = 1000
    invalid = request.model_copy(
        update={
            "payload": request.payload.model_copy(
                update={
                    "content": request.payload.content.model_copy(update={"text": "\ud800"}),
                }
            )
        }
    )
    with pytest.raises(EvidenceServiceError) as error:
        env.service.write(invalid)
    assert error.value.failure.code == "invalid_request"
    with pytest.raises(EvidenceServiceError):
        env.service.write_batch(
            WriteBatch.model_construct(
                contract_version="foundation/1",
                batch_id="batch",
                items=(request, invalid),
            )
        )
    with env.database.connection() as connection:
        assert connection.execute("SELECT count(*) FROM document").fetchone()[0] == 0
    # Invalid operation tags are rejected as an envelope, not as a partial batch.
    invalid_operation = request.payload.model_copy(update={"operation": "invented"})
    with pytest.raises(EvidenceServiceError):
        env.service.write_batch(
            WriteBatch.model_construct(
                contract_version="foundation/1",
                batch_id="invalid-op",
                items=(
                    request,
                    request.model_copy(
                        update={
                            "request_id": "different",
                            "payload": invalid_operation,
                        }
                    ),
                ),
            )
        )
    with env.database.connection() as connection:
        assert connection.execute("SELECT count(*) FROM document").fetchone()[0] == 0


def test_original_python_types_rejected_before_batch_item_zero(tmp_path: Path) -> None:
    env = environment(tmp_path / "e.db")
    valid = put(env.scope, external="first")
    malformed = put(env.scope, external="second", text="abc")
    malformed = malformed.model_copy(
        update={
            "payload": malformed.payload.model_copy(
                update={
                    "content": malformed.payload.content.model_copy(
                        update={
                            "anchors": (
                                SuppliedAnchor.model_construct(
                                    local_id="boolean", start=True, end=2, quote="b"
                                ),
                            )
                        }
                    ),
                }
            )
        }
    )
    for operation in (
        lambda: env.service.write(malformed),
        lambda: env.service.write_batch(
            WriteBatch.model_construct(
                contract_version="foundation/1",
                batch_id="strict",
                items=(valid, malformed),
            )
        ),
    ):
        with pytest.raises(EvidenceServiceError) as error:
            operation()
        assert error.value.failure.code == "invalid_request"
    with env.database.connection() as connection:
        for table in ("document", "revision", "write_key"):
            assert connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0


def test_ordered_mixed_batch_and_omission_safety(tmp_path: Path) -> None:
    env = environment(tmp_path / "e.db")
    existing = receipt(env.service.write(put(env.scope, external="existing")))
    batch = WriteBatch(
        contract_version="foundation/1",
        batch_id="batch",
        items=(
            put(env.scope, external="0"),
            put(
                env.scope,
                external="existing",
                state="stale",
            ),
            put(env.scope, external="2"),
        ),
    )
    result = env.service.write_batch(batch)
    assert [out.status for out in result.outcomes] == ["applied", "conflict", "applied"]
    assert result.status == "partial"
    result.validate_for(batch)
    assert env.service.document(env.scope, existing.document_id).processing.source == "active"
    assert "synchronization" in env.service.capabilities().unsupported
    assert not hasattr(env.service, "complete_snapshot")
    with env.database.connection() as connection:
        assert connection.execute("SELECT count(*) FROM document_state").fetchone()[0] == 3
        assert connection.execute("SELECT count(*) FROM write_key").fetchone()[0] == 3


def test_metadata_and_policy_states_are_independent_of_content(tmp_path: Path) -> None:
    env = environment(tmp_path / "e.db")
    first = receipt(env.service.write(put(env.scope)))
    email = receipt(env.service.write(put(env.scope, namespace="email")))
    old_citation = (
        env.service.anchors(
            env.scope,
            first.document_id,
            first.processing.state_version,
        )
        .entries[0]
        .citation
    )
    updated = receipt(
        env.service.write(
            put(
                env.scope,
                title="New",
                state=first.processing.state_version,
            )
        )
    )
    assert updated.revision_id == first.revision_id
    assert updated.metadata_snapshot_id != first.metadata_snapshot_id
    assert env.service.citation(env.scope, old_citation).metadata.metadata.title == "Title"
    policy = LocalPolicy(
        corpus_id="work",
        bindings=env.policy.bindings,
        grants=(
            *env.policy.grants,
            PolicyGrant(
                principal_id="another",
                namespace="markdown",
                grant="read",
            ),
        ),
    )
    change = env.admin.replace_policy(policy, env.scope.access.policy_version)
    assert change.affected_namespaces == ("markdown",)
    assert change.changed_documents == 1
    with pytest.raises(EvidenceServiceError) as error:
        env.service.document(env.scope, first.document_id)
    assert error.value.failure.code == "forbidden"
    scope = env.scope.model_copy(
        update={
            "access": env.scope.access.model_copy(
                update={
                    "policy_version": change.policy_version,
                }
            )
        }
    )
    current = env.service.document(scope, first.document_id)
    assert current.state_version != updated.processing.state_version
    assert current.revision_id == first.revision_id
    assert (
        env.service.document(scope, email.document_id).state_version
        == email.processing.state_version
    )
    same = env.service.write(put(scope, namespace="email", state=email.processing.state_version))
    assert same.status == "unchanged"
    assert env.admin.replace_policy(policy, change.policy_version).status == "unchanged"
    assert env.service.citation(scope, old_citation).metadata.metadata.title == "Title"


def test_multiple_owners_and_binding_checks(tmp_path: Path) -> None:
    env = environment(tmp_path / "e.db")
    first = receipt(env.service.write(put(env.scope)))
    policy = LocalPolicy(
        corpus_id="work",
        bindings=(
            *env.policy.bindings,
            WriterBinding(
                namespace="markdown",
                owner_id="other",
                writer_id="writer",
                synchronization_scope="all",
            ),
        ),
        grants=(
            *env.policy.grants,
            PolicyGrant(
                principal_id="principal",
                namespace="markdown",
                grant="write_documents",
                owner_id="other",
                writer_id="writer",
                synchronization_scope="all",
            ),
        ),
    )
    version = env.admin.replace_policy(policy, env.scope.access.policy_version).policy_version
    scope = env.scope.model_copy(
        update={
            "access": env.scope.access.model_copy(
                update={
                    "policy_version": version,
                }
            )
        }
    )
    other = put(scope, external="owned-by-other")
    other = other.model_copy(
        update={
            "attribution": other.attribution.model_copy(
                update={
                    "owner_id": "other",
                }
            )
        }
    )
    assert env.service.write(other).status == "applied"
    current = env.service.document(scope, first.document_id)
    stolen = put(scope, state=current.state_version)
    stolen = stolen.model_copy(update={"attribution": other.attribution})
    assert env.service.write(stolen).error.code == "forbidden"
    spoofed = put(scope, external="spoofed")
    spoofed = spoofed.model_copy(
        update={
            "scope": scope.model_copy(
                update={
                    "access": scope.access.model_copy(update={"principal_id": "attacker"}),
                }
            )
        }
    )
    assert env.service.write(spoofed).error.code == "forbidden"


def test_fault_rolls_back_content_state_and_receipt(tmp_path: Path, monkeypatch) -> None:
    from kg.evidence import _receipts

    env = environment(tmp_path / "e.db")

    def broken(*args, **kwargs):
        raise sqlite3.OperationalError("source text must not appear in logs")

    monkeypatch.setattr(_receipts, "save", broken)
    result = env.service.write(put(env.scope))
    assert result.status == "failed" and result.error.code == "internal_error"
    with env.database.connection() as connection:
        for table in (
            "document",
            "revision",
            "document_state",
            "anchor",
            "write_key",
            "write_response",
        ):
            assert connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0


def test_noop_metadata_order_and_attribution(tmp_path: Path) -> None:
    from kg.models.foundation import MetadataEntry

    env = environment(tmp_path / "e.db")
    request = put(env.scope)
    metadata = SourceMetadata(
        title="Title",
        location="source://doc",
        attributes=(
            MetadataEntry(key="flag", value=True),
            MetadataEntry(key="count", value=1),
        ),
    )
    request = request.model_copy(
        update={
            "payload": request.payload.model_copy(
                update={
                    "metadata": metadata,
                }
            )
        }
    )
    first = receipt(env.service.write(request))
    reordered = request.model_copy(
        update={
            "request_id": "reordered",
            "retry_key": "reordered",
            "attribution": request.attribution.model_copy(update={"producer_version": "2"}),
            "payload": request.payload.model_copy(
                update={
                    "precondition": ExpectedState(
                        kind="match", state_version=first.processing.state_version
                    ),
                    "metadata": metadata.model_copy(
                        update={"attributes": tuple(reversed(metadata.attributes))}
                    ),
                }
            ),
        }
    )
    assert env.service.write(reordered).status == "unchanged"
    with env.database.connection() as connection:
        assert connection.execute("SELECT count(*) FROM write_provenance").fetchone()[0] == 2
        assert connection.execute("SELECT count(*) FROM document_state").fetchone()[0] == 1
    assert (
        json.loads(
            env.service.content(
                env.scope,
                first.document_id,
                first.revision_id,
            ).model_dump_json()
        )["state"]
        == "available"
    )
