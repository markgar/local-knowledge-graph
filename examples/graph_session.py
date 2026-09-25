"""Synthetic local lifecycle acceptance; private fixed reads, not a query API."""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict
from pathlib import Path
from threading import Event
from time import monotonic
from uuid import uuid4

from graph_build import fixture

from kg.evidence._read_context import read_evidence
from kg.graph import LocalGraphSession
from kg.graph._native import BUFFER_POOL_BYTES
from kg.graph._session_types import GraphSessionError
from kg.knowledge import KnowledgeService
from kg.knowledge._graph_export import GraphAssertion
from kg.models.authoring import (
    AuthoredAssertion,
    AuthoredEntity,
    CurrentSelection,
    ExistingIdentity,
    RecordAuthoringBatch,
    RecordAuthoringDocument,
    RecordAuthoringRequest,
    SourceCapture,
)
from kg.models.foundation import (
    StringObject,
)


def native_count(context):
    rows = context.native.execute("MATCH (a:Assertion) RETURN count(a)", {})
    count = rows.read()[0][0]
    rows.close()
    return context.retain(count)


def lifecycle(output: Path) -> dict:
    timings = {}

    def timed(phase, operation):
        start = monotonic()
        result = operation()
        timings[phase] = monotonic() - start
        return result

    output.mkdir(parents=True, exist_ok=False)
    env = fixture(output / "canonical.sqlite", decisions=12)
    session = timed(
        "open",
        lambda: LocalGraphSession(
            env.database,
            env.identity,
            env.scope,
            graph_directory=output / "derived",
        ),
    )
    try:
        assert session.status().state == "unbuilt"
        first = timed("cold_build_read", lambda: session._run_read(env.scope, native_count))
        generation = session._generation
        second = timed("warm_read", lambda: session._run_read(env.scope, native_count))
        assert first == second == 22 and session._generation == generation

        def proof(context):
            rows = context.native.execute(
                "MATCH (a:Assertion) WHERE a.object_kind='decision' "
                "RETURN a.dependency_json ORDER BY a.assertion_id LIMIT 1",
                {},
            )
            assertion = GraphAssertion.model_validate_json(rows.read()[0][0])
            rows.close()
            evidence = tuple(
                context.retain(
                    read_evidence(
                        context.canonical,
                        item.captured.reference,
                    )
                )
                for item in assertion.support
            )
            return context.retain((assertion, evidence))

        assertion, evidence = session._run_read(env.scope, proof)
        assert assertion.assertion_id in env.expected and evidence

        reference = env.references[0]
        entity = KnowledgeService(env.database, env.identity).entity(env.scope, env.projects[0])
        if entity.entity_type is None or entity.selection_witness is None:
            raise RuntimeError("Expected a selected project classification")
        request = RecordAuthoringRequest(
            interface_version="record-authoring/1",
            request_id=str(uuid4()),
            retry_key=str(uuid4()),
            scope=env.scope,
            attribution=env.attribution,
            document=RecordAuthoringDocument(
                interface_version="record-authoring/1",
                expected_schema_revision=env.schema_revision,
                support={
                    "source": SourceCapture(
                        kind="source",
                        reference=reference,
                        state_version=env.dependencies[reference.document_id].state_version,
                    )
                },
                entities=(
                    AuthoredEntity(
                        local_id="project",
                        identity=ExistingIdentity(kind="existing", entity_id=env.projects[0]),
                        selection=CurrentSelection(
                            kind="current",
                            expected_entity_type=entity.entity_type,
                            selection_witness=entity.selection_witness,
                        ),
                    ),
                ),
                assertions=(
                    AuthoredAssertion(
                        local_id="new-decision",
                        predicate="work:decision",
                        subject="project",
                        object=StringObject(kind="string", value="A saved decision"),
                        interpretation="explicit",
                        support=("source",),
                    ),
                ),
            ),
        )
        batch = RecordAuthoringBatch(
            interface_version="record-authoring-batch/1",
            batch_id=str(uuid4()),
            items=(
                request,
                request.model_copy(
                    update={
                        "request_id": str(uuid4()),
                        "retry_key": str(uuid4()),
                    }
                ),
            ),
        )
        written = timed("batch_write", lambda: session.record_batch(batch))
        assert written.status == "complete"
        receipt = written.outcomes[0]
        assert receipt.receipt is not None and session.status().state == "dirty"
        cancelled = Event()
        cancelled.set()
        assert session.refresh(cancel=cancelled).error.code == "cancelled"
        assert (
            KnowledgeService(env.database, env.identity).record(request).receipt == receipt.receipt
        )
        assert timed("refresh", session.refresh).state == "ready"
        refreshed = timed("refreshed_read", lambda: session._run_read(env.scope, native_count))
        assert refreshed == 24 and session._generation != generation

        def invalid_native(context):
            context.native.execute("MATCH (missing:NotARealTable) RETURN missing", {})

        try:
            session._run_read(env.scope, invalid_native)
        except GraphSessionError as error:
            assert error.failure.code == "native_error"
        else:
            raise AssertionError("Invalid native statement unexpectedly succeeded")
        assert session.status().state == "error"
        assert (
            KnowledgeService(env.database, env.identity).record(request).receipt == receipt.receipt
        )
        assert not timed("close", session.close).cleanup_pending
        with LocalGraphSession(
            env.database,
            env.identity,
            env.scope,
            graph_directory=output / "derived",
        ) as restarted:
            assert restarted.status().state == "unbuilt"
            assert (
                timed(
                    "restart_build_read",
                    lambda: restarted._run_read(env.scope, native_count),
                )
                == 24
            )
            assert restarted._generation.session_id != generation.session_id
        result = {
            "cold_assertions": first,
            "warm_assertions": second,
            "refreshed_assertions": refreshed,
            "saved_receipt_after_graph_failure": True,
            "canonical_proof_hydrated": True,
            "restart_untrusted": True,
            "native_buffer_bytes": BUFFER_POOL_BYTES,
            "close": asdict(session.status()),
            "phase_seconds": timings,
            "final_derived_inventory": [path.name for path in (output / "derived").iterdir()],
        }
        (output / "receipt.json").write_text(json.dumps(result, indent=2) + "\n")
        return result
    finally:
        status = session.close()
        if status.cleanup_pending:
            raise RuntimeError(f"Graph cleanup pending: {status.cleanup_error}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    print(json.dumps(lifecycle(parser.parse_args().output), indent=2))
