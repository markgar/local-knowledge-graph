from __future__ import annotations

from pathlib import Path

import pytest
from support.evidence import environment, put, receipt

from kg.evidence import EvidenceServiceError
from kg.models.foundation import SuppliedAnchor


@pytest.mark.service
def test_historical_inventory_citations_and_chain_validation(tmp_path: Path) -> None:
    env = environment(tmp_path / "e.db")
    initial = put(env.scope, text="first anchor")
    first = receipt(env.service.write(initial))
    old = env.service.anchors(env.scope, first.document_id, first.processing.state_version).entries[
        0
    ]
    replaced = put(
        env.scope, text="first anchor", state=first.processing.state_version, title="New"
    )
    replaced = replaced.model_copy(
        update={
            "payload": replaced.payload.model_copy(
                update={
                    "content": replaced.payload.content.model_copy(
                        update={
                            "anchors": (
                                SuppliedAnchor(local_id="part", start=6, end=12, quote="anchor"),
                            )
                        }
                    ),
                }
            )
        }
    )
    second = receipt(env.service.write(replaced))
    assert second.revision_id == first.revision_id
    newer = receipt(
        env.service.write(
            put(
                env.scope,
                text="second revision",
                state=second.processing.state_version,
            )
        )
    )
    revisions = env.service.revisions(env.scope, first.document_id, limit=1)
    assert revisions.has_more and len(revisions.entries) == 1
    next_page = env.service.revisions(
        env.scope,
        first.document_id,
        after_sequence=revisions.next_after_sequence,
        limit=1,
    )
    assert not next_page.has_more and next_page.entries[0].revision_id == newer.revision_id
    history = env.service.history(env.scope, first.document_id, limit=2)
    assert history.has_more and history.next_after_sequence == 2
    assert (
        len(
            env.service.history(
                env.scope,
                first.document_id,
                after_sequence=2,
            ).entries
        )
        == 1
    )
    inventory = env.service.revision_anchors(
        env.scope, first.document_id, first.revision_id, limit=1
    )
    orphan = inventory.entries[0]
    assert orphan.reference == old.reference
    assert not orphan.is_current_support and not orphan.member_of_current_anchor_set
    assert env.service.citation(env.scope, orphan.citation).metadata.metadata.title == "Title"
    remainder = env.service.revision_anchors(
        env.scope,
        first.document_id,
        first.revision_id,
        after_ordinal=inventory.next_after_ordinal,
        limit=1,
    )
    assert not remainder.has_more and remainder.entries[0].quote == "anchor"
    assert (
        env.service.citation(env.scope, remainder.entries[0].citation).metadata.metadata.title
        == "New"
    )
    with pytest.raises(EvidenceServiceError):
        env.service.evidence(
            env.scope, orphan.reference, state_version=second.processing.state_version
        )
    with pytest.raises(EvidenceServiceError):
        env.service.citation(
            env.scope,
            old.citation.model_copy(
                update={
                    "metadata_snapshot_id": second.metadata_snapshot_id,
                }
            ),
        )
    other = receipt(env.service.write(put(env.scope, external="other", namespace="email")))
    for changes in (
        {"corpus_id": "elsewhere"},
        {"source_namespace": "email"},
        {"document_id": other.document_id},
        {"revision_id": other.revision_id},
        {"anchor_id": "missing"},
    ):
        with pytest.raises(EvidenceServiceError) as error:
            env.service.evidence(env.scope, old.reference.model_copy(update=changes))
        assert error.value.failure.code == "not_found"
    with pytest.raises(EvidenceServiceError) as error:
        env.service.evidence(env.scope, old.reference.model_copy(update={"passage_id": "future"}))
    assert error.value.failure.code == "not_found"


@pytest.mark.service
def test_content_corruption_fails_without_success_shaped_fallback(tmp_path: Path) -> None:
    env = environment(tmp_path / "e.db")
    saved = receipt(env.service.write(put(env.scope, text="abc")))
    with env.database.transaction() as connection:
        connection.execute(
            "UPDATE revision SET content=? WHERE revision_id=?", (b"xyz", saved.revision_id)
        )
    with pytest.raises(EvidenceServiceError) as error:
        env.service.content(env.scope, saved.document_id, saved.revision_id)
    assert error.value.failure.code == "internal_error"


@pytest.mark.service
def test_read_revocation_discards_snapshot(tmp_path: Path, monkeypatch) -> None:
    from kg.evidence import _store
    from kg.models.evidence import LocalPolicy

    env = environment(tmp_path / "e.db")
    saved = receipt(env.service.write(put(env.scope)))
    original = _store.content_bytes

    def revoke(connection, document, revision):
        result = original(connection, document, revision)
        env.admin.replace_policy(
            LocalPolicy(corpus_id="work", bindings=env.policy.bindings),
            env.scope.access.policy_version,
        )
        return result

    monkeypatch.setattr(_store, "content_bytes", revoke)
    with pytest.raises(EvidenceServiceError) as error:
        env.service.content(env.scope, saved.document_id, saved.revision_id)
    assert error.value.failure.code == "state_changed"


@pytest.mark.service
@pytest.mark.parametrize("limit,after", [(0, 0), (201, 0), (1, -1), (True, 0), (1, False)])
def test_page_bounds(tmp_path: Path, limit: int, after: int) -> None:
    env = environment(tmp_path / "e.db")
    saved = receipt(env.service.write(put(env.scope)))
    with pytest.raises(EvidenceServiceError) as error:
        env.service.history(env.scope, saved.document_id, limit=limit, after_sequence=after)
    assert error.value.failure.code == "invalid_request"
