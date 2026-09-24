"""Fixed native joins with complete proof output inside one session guard."""

from contextlib import ExitStack, closing
from typing import Literal

from pydantic import ValidationError

from kg.diagnostics._bounds import bounded_size
from kg.evidence.errors import EvidenceServiceError
from kg.graph._native import NativeError, NativeValue
from kg.graph._relationships import (
    check_root_type,
    decode_relationship,
    preflight_relationship,
    resolve_graph_root,
)
from kg.graph._session_types import GraphGeneration
from kg.graph.session import GraphReadContext
from kg.knowledge._graph_export import GraphAssertion
from kg.knowledge._selection import EntitySelectionItem
from kg.models.foundation import Token, Value
from kg.models.graph import (
    GraphDecisionMember,
    GraphRelationshipDecisionsRequest,
    GraphRelationshipDecisionsResult,
    GraphRelationshipProof,
)
from kg.models.knowledge import KnowledgeSchema

_OUTGOING = """
MATCH (s:Entity)-[:SUBJECT]->(r:Assertion)-[:OBJECT]->(o:Entity),
      (o)-[:SUBJECT]->(d:Assertion)
WHERE s.entity_id = $root AND r.predicate = $predicate
  AND r.object_kind = 'entity' AND r.interpretation = 'explicit'
  AND d.object_kind = 'decision' AND d.interpretation = 'explicit'
  AND d.predicate IN $decision_predicates
"""
_INCOMING = _OUTGOING.replace("(o)-[:SUBJECT]->(d", "(s)-[:SUBJECT]->(d").replace(
    "s.entity_id = $root", "o.entity_id = $root",
)
_SELECTED = """
      (s)-[:SELECTED]->(sp:EntityProof),
      (o)-[:SELECTED]->(op:EntityProof)
"""


class _GraphDecisionSelection(Value):
    request: GraphRelationshipDecisionsRequest
    generation: GraphGeneration
    outcome: Literal["complete", "empty", "ambiguous"]
    root_entity_id: Token | None = None
    candidate_ids: tuple[Token, ...] = ()
    count: int | None
    members: tuple[GraphDecisionMember, ...] = ()
    relationships: tuple[GraphRelationshipProof, ...] = ()


def _decode_pair(
    ctx: GraphReadContext, row: tuple[NativeValue, ...], *,
    request: GraphRelationshipDecisionsRequest, root: EntitySelectionItem,
    schema: KnowledgeSchema, predicates: tuple[str, ...],
) -> tuple[GraphAssertion, GraphRelationshipProof]:
    if len(row) != 9 or any(not isinstance(value, str) for value in row):
        raise NativeError("invalid_projection")
    identifier, payload, related = row[:3]
    assert isinstance(payload, str)
    proof = decode_relationship(
        ctx, row[3:], root=root, schema=schema,
        predicate=request.predicate, direction=request.direction,
    )
    try:
        decision = GraphAssertion.model_validate_json(payload)
    except ValidationError:
        raise NativeError("invalid_projection") from None
    witness = (
        proof.assertion.object_witness if request.direction == "outgoing"
        else proof.assertion.subject_witness
    )
    if (
        decision.assertion_id != identifier or decision.subject_id != related
        or proof.path.entity_ids[1] != related or decision.subject_witness != witness
        or decision.predicate not in predicates or decision.decision_member is None
        or decision.object_entity_id is not None or decision.interpretation != "explicit"
        or any(e.captured.reference.corpus_id != request.scope.corpus_id
               or e.captured.reference.source_namespace not in request.scope.access.namespaces
               for e in decision.support)
    ):
        raise NativeError("invalid_projection")
    ctx.require_authored_revision(decision.assertion_id, decision.schema_version)
    ctx.require_classification_captures(
        decision.assertion_id, decision.classification_witnesses,
        decision.classification_evidence,
    )
    related_capture = proof.assertion.classification_witnesses[
        1 if request.direction == "outgoing" else 0
    ]
    if decision.classification_witnesses[0] != related_capture:
        raise NativeError("invalid_projection")
    return decision, proof


def _select_relationship_decisions(
    ctx: GraphReadContext, request: GraphRelationshipDecisionsRequest,
) -> _GraphDecisionSelection:
    with ExitStack() as custody:
        schema = preflight_relationship(
            ctx, custody=custody, predicate=request.predicate,
            direction=request.direction, max_hops=request.max_hops,
        )
        predicates = tuple(p.name for p in schema.predicates if p.record_projection is not None)
        if not predicates:
            raise EvidenceServiceError("unsupported")
        roots = resolve_graph_root(ctx, request.start, custody=custody)
        custody.enter_context(ctx.meter.reserve_scratch(32768, "general"))
        if len(roots) != 1:
            result = _GraphDecisionSelection(
                request=request, generation=ctx.generation,
                outcome="ambiguous" if roots else "empty",
                candidate_ids=tuple(r.entity_id for r in roots), count=None if roots else 0,
            )
            del roots
            return ctx.retain(result)
        root = roots[0]
        check_root_type(
            ctx, root, schema, predicate=request.predicate, direction=request.direction,
        )
        pattern = _OUTGOING if request.direction == "outgoing" else _INCOMING
        parameters: dict[str, NativeValue] = {
            "root": root.entity_id, "predicate": request.predicate,
            "decision_predicates": list(predicates),
        }
        with closing(ctx.native.execute(
            pattern + "RETURN count(DISTINCT d.assertion_id)", parameters,
        )) as rows:
            page = rows.read(limit=1)
            if len(page) != 1 or len(page[0]) != 1:
                raise NativeError("invalid_projection")
            count = page[0][0]
            if type(count) is not int or count < 0 or rows.read(limit=1):
                raise NativeError("invalid_projection")
        related = "o" if request.direction == "outgoing" else "s"
        template = pattern.replace("\nWHERE", ",\n" + _SELECTED + "WHERE") + f"""
RETURN d.assertion_id, d.dependency_json, {related}.entity_id,
       s.entity_id, o.entity_id, r.assertion_id, r.dependency_json,
       sp.witness_json, op.witness_json
ORDER BY d.assertion_id, r.assertion_id
"""
        members: list[GraphDecisionMember] = []
        proofs: dict[str, GraphRelationshipProof] = {}
        current: GraphAssertion | None = None
        relationships: list[str] = []
        previous = ("", "")
        with closing(ctx.native.execute(template, parameters)) as rows:
            # Full nine-column proofs can exceed the borrowed-page byte cap at 200 rows.
            while page := rows.read(limit=32):
                for row in page:
                    # Temporary decoded duplicates expire per row; surviving payloads
                    # acquire overlapping assembly custody before this reservation ends.
                    size = 4096 + 8 * bounded_size(row, 8 << 20)
                    with ctx.meter.reserve_scratch(size, "general"):
                        decision, proof = _decode_pair(
                            ctx, row, request=request, root=root,
                            schema=schema, predicates=predicates,
                        )
                        key = (decision.assertion_id, proof.assertion.assertion_id)
                        if key <= previous:
                            raise NativeError("invalid_projection")
                        previous = key
                        if current is None or current.assertion_id != decision.assertion_id:
                            if current is not None:
                                members.append(GraphDecisionMember(
                                    decision=current, relationship_ids=tuple(relationships),
                                ))
                            custody.enter_context(ctx.meter.reserve_scratch(
                                4096 + 8 * bounded_size(row[1], 8 << 20), "general",
                            ))
                            current, relationships = decision, []
                        elif current != decision:
                            raise NativeError("invalid_projection")
                        identifier = proof.assertion.assertion_id
                        if identifier not in proofs:
                            custody.enter_context(ctx.meter.reserve_scratch(
                                4096 + 8 * bounded_size(row[3:], 8 << 20), "general",
                            ))
                            proofs[identifier] = proof
                        elif proofs[identifier] != proof:
                            raise NativeError("invalid_projection")
                        custody.enter_context(ctx.meter.reserve_scratch(
                            256 + 8 * bounded_size(identifier, 4096), "general",
                        ))
                        relationships.append(identifier)
                        del decision, proof
                # NativeRows releases the preceding page's reservation on its next read.
                del row, page
        if current is not None:
            members.append(GraphDecisionMember(
                decision=current, relationship_ids=tuple(relationships),
            ))
        if len(members) != count:
            raise NativeError("invalid_projection")
        result = _GraphDecisionSelection(
            request=request, generation=ctx.generation,
            outcome="complete" if count else "empty", root_entity_id=root.entity_id,
            count=count, members=tuple(members),
            relationships=tuple(proofs[key] for key in sorted(proofs)),
        )
        return ctx.retain(result)


def relationship_decisions(
    ctx: GraphReadContext, request: GraphRelationshipDecisionsRequest,
) -> GraphRelationshipDecisionsResult:
    selection = _select_relationship_decisions(ctx, request)
    # Keep display containers charged until G4 admits its callback return.
    ctx.canonical._reserve_scratch(
        32768 + len(selection.relationships) * 256 + len(selection.members) * 32, "general",
    )
    members = selection.members[:request.display_limit]
    identifiers = {identifier for member in members for identifier in member.relationship_ids}
    return GraphRelationshipDecisionsResult(
        request_id=request.request_id, scope=request.scope, outcome=selection.outcome,
        generation=selection.generation, root_entity_id=selection.root_entity_id,
        candidate_ids=selection.candidate_ids, count=selection.count,
        exact=None if selection.count is None else True, members=members,
        relationships=tuple(p for p in selection.relationships
                            if p.assertion.assertion_id in identifiers),
        display_truncated=selection.count is not None and selection.count > len(members),
    )
