"""Supplied synthetic facts -> one joined graph API call -> both proof sides.

Uses no inference models. The optional in-process Ladybug runtime may crash the
host; its 256 MiB buffer is not a hard memory limit or crash boundary.
"""

import argparse
import json
from pathlib import Path

from graph_build import fixture

from kg.graph import LocalGraphSession
from kg.models.evidence import StoredCitation
from kg.models.graph import GraphEntitySelector, GraphRelationshipDecisionsRequest


def run(output: Path) -> dict:
    output.mkdir(parents=True, exist_ok=False)
    env = fixture(output / "canonical.sqlite", decisions=12)
    with LocalGraphSession(
        env.database, env.identity, env.scope, graph_directory=output / "derived",
    ) as graph:
        result = graph.relationship_decisions(GraphRelationshipDecisionsRequest(
            request_id="owned-project-decisions", scope=env.scope,
            start=GraphEntitySelector(entity_id=env.person), predicate="work:owns",
            direction="outgoing", display_limit=3,
        ))
        if result.outcome != "complete":
            raise RuntimeError(result.model_dump_json())
        expected = sorted(key for key, value in env.expected.items() if value["object"] is None)
        assert result.count == len(expected) == 12
        assert [m.decision.assertion_id for m in result.members] == expected[:3]
        quotes = []
        for kind, assertions in (
            ("relationship", tuple(p.assertion for p in result.relationships)),
            ("decision", tuple(m.decision for m in result.members)),
        ):
            for assertion in assertions:
                for evidence in assertion.support:
                    capture = evidence.captured
                    view = env.evidence.citation(env.scope, StoredCitation(
                        reference=capture.reference,
                        metadata_snapshot_id=capture.metadata_snapshot_id,
                        state_version=capture.dependency.state_version,
                    ))
                    quotes.append({
                        "kind": kind, "assertion_id": assertion.assertion_id,
                        "citation": view.model_dump(mode="json"),
                    })
        return {
            "capabilities": graph.capabilities().model_dump(mode="json"),
            "result": result.model_dump(mode="json"),
            "separately_authorized_historical_citations": quotes,
            "full_graph_inspection_available": False,
        }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    print(json.dumps(run(parser.parse_args().output), ensure_ascii=False, indent=2))
