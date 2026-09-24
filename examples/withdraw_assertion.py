"""Withdraw an exact owned assertion, retain its history, and submit a separate correction."""

import argparse
import json
from contextlib import ExitStack
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

from knowledge_enrichment import supply

from kg.knowledge import KnowledgeService
from kg.models.foundation import (
    ChangeSet,
    CountStep,
    QueryBudget,
    QueryRequest,
    RecordsStep,
    ResolveStep,
    StringObject,
    WithdrawAssertion,
    WriteRequest,
)
from kg.query import QueryService


def run(path: Path, *, graph: bool = False) -> dict:
    if path.exists():
        raise ValueError(
            "Use a new empty database path; this example never resets an existing store."
        )
    evidence, scope, attribution, target = supply(path)
    knowledge = KnowledgeService(evidence.database, evidence.identity)
    original = knowledge.contribution(scope, target)
    subject = original.witnesses[0].entity_id

    def request(payload):
        return WriteRequest(
            contract_version="foundation/1", request_id=str(uuid4()), retry_key=str(uuid4()),
            scope=scope, attribution=attribution, payload=payload,
        )

    def assertion(text):
        return ChangeSet(
            operation="enrich",
            expected_schema_revision=knowledge.schema(scope).head,
            changes=(original.payload.model_copy(update={
                "object": StringObject(kind="string", value=text),
            }),),
            dependencies=tuple(p.dependency for p in original.evidence),
        )

    query = QueryRequest(
        contract_version="foundation/1", request_id="count", scope=scope,
        budget=QueryBudget(max_records=10_000, max_milliseconds=30_000),
        steps=(
            ResolveStep(operation="resolve", step_id="project", entity_id=subject),
            RecordsStep(operation="records", step_id="decisions", entity_step="project",
                        record_type="decision"),
            CountStep(operation="count", step_id="count", records_step="decisions"),
        ),
        output_step="count",
    )
    with ExitStack() as stack:
        writer = evidence
        if graph:
            from kg.graph import LocalGraphSession

            writer = stack.enter_context(LocalGraphSession(
                evidence.database, evidence.identity, scope,
                graph_directory=path.parent / f"{path.name}-graph",
            ))
            ready = writer.refresh()
            if ready.error:
                raise RuntimeError(f"Initial graph build failed: {ready.error.code}")
        independent = writer.write(request(assertion("Independently supported decision")))
        if independent.error:
            raise RuntimeError(independent.model_dump_json())
        count_service = stack.enter_context(QueryService(evidence.database, evidence.identity))
        def count():
            outcome = count_service.execute(query).result
            if outcome.error:
                raise RuntimeError(outcome.model_dump_json())
            return outcome.data.count
        before = count()
        withdrawal = request(WithdrawAssertion(
            operation="withdraw_assertion", contribution_id=target,
        ))
        saved = writer.write(withdrawal)
        if saved.error:
            raise RuntimeError(saved.model_dump_json())
        replay = writer.write(withdrawal)
        if replay.receipt != saved.receipt:
            raise RuntimeError("Withdrawal replay did not return the saved receipt")
        after = count()
        historical = knowledge.contribution(scope, target, mode="history")
        correction = writer.write(request(assertion("Corrected decision: wait for approval")))
        if correction.error:
            raise RuntimeError(correction.model_dump_json())
        refresh = "not_requested"
        if graph:
            ready = writer.refresh()
            refresh = (
                "Withdrawal saved; graph refresh failed. Refresh again; "
                "do not resubmit the saved withdrawal."
                if ready.error else "ready"
            )
        return {
            "before": before, "after": after, "after_correction": count(),
            "withdrawal": saved.model_dump(mode="json"),
            "history": historical.model_dump(mode="json"),
            "quote": evidence.evidence(scope, historical.evidence[0].reference).quote,
            "independent": independent.receipt.model_dump(mode="json"),
            "correction": correction.receipt.model_dump(mode="json"),
            "graph_refresh": refresh,
        }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, help="New empty database path; never overwritten")
    parser.add_argument(
        "--graph", action="store_true", help="Use optional graph session writes/refresh",
    )
    args = parser.parse_args()
    with TemporaryDirectory(prefix="kg-withdrawal-") as temporary:
        print(json.dumps(run(
            args.database or Path(temporary) / "evidence.sqlite", graph=args.graph,
        )))
