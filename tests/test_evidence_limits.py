from __future__ import annotations

from pathlib import Path

import pytest
from support.evidence import environment, put, receipt

from kg.evidence import EvidenceServiceError
from kg.models.foundation import SuppliedAnchor, WriteBatch


@pytest.mark.service
def test_service_enforces_text_anchor_request_and_batch_limits(tmp_path: Path) -> None:
    env = environment(tmp_path / "e.db")
    base = put(env.scope)

    def sized(text, anchors=(), *, external="doc"):
        return base.model_copy(
            update={
                "payload": base.payload.model_copy(
                    update={
                        "document": base.payload.document.model_copy(
                            update={"external_id": external}
                        ),
                        "content": base.payload.content.model_copy(
                            update={"text": text, "anchors": anchors}
                        ),
                    }
                )
            }
        )

    at_limit = sized("a" * 5_000_000)
    saved = receipt(env.service.write(at_limit))
    assert env.service.revisions(env.scope, saved.document_id).entries[0].byte_length == 5_000_000
    anchors = tuple(
        SuppliedAnchor(local_id=f"a{i}", start=0, end=1, quote="x") for i in range(1000)
    )
    anchor_request = sized("x", anchors, external="anchors").model_copy(
        update={"retry_key": "anchors"}
    )
    anchored = receipt(env.service.write(anchor_request))
    count, after = 0, 0
    while True:
        page = env.service.anchors(
            env.scope,
            anchored.document_id,
            anchored.processing.state_version,
            after_ordinal=after,
        )
        count += len(page.entries)
        if not page.has_more:
            break
        after = page.next_after_ordinal
    assert count == 1000
    for invalid in (
        sized("a" * 5_000_001),
        sized(
            "x",
            (
                *anchors,
                SuppliedAnchor(
                    local_id="extra",
                    start=0,
                    end=1,
                    quote="x",
                ),
            ),
        ),
        sized(
            "a" * 4_000_000,
            (
                SuppliedAnchor(
                    local_id="whole",
                    start=0,
                    end=4_000_000,
                    quote="a" * 4_000_000,
                ),
            ),
        ),
    ):
        with pytest.raises(EvidenceServiceError) as error:
            env.service.write(invalid)
        assert error.value.failure.code == "invalid_request"
    items = tuple(
        sized("x" * 4_000_000, external=f"batch{i}").model_copy(
            update={
                "request_id": f"request{i}",
                "retry_key": f"retry{i}",
            }
        )
        for i in range(4)
    )
    with pytest.raises(EvidenceServiceError):
        env.service.write_batch(
            WriteBatch.model_construct(
                contract_version="foundation/1",
                batch_id="oversized",
                items=items,
            )
        )
    with env.database.connection() as connection:
        assert connection.execute("SELECT count(*) FROM document").fetchone()[0] == 2
