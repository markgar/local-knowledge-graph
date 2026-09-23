"""One-hop application semantics; G3/G4 own projection authority and freshness."""

from contextlib import ExitStack, closing
from dataclasses import replace

from pydantic import ValidationError

from kg._execution_budget import DeadlineStop, PrivateResourceStop
from kg.diagnostics._bounds import bounded_size
from kg.evidence.errors import EvidenceServiceError
from kg.graph._native import NativeError, NativeValue
from kg.graph._session_types import GraphSessionError
from kg.graph.session import GraphReadContext, _translate
from kg.knowledge._graph_export import GraphAssertion
from kg.knowledge._reader import KnowledgeReader
from kg.knowledge._selection import (
    EligibleEOF,
    EntitySelectionItem,
    EntitySelector,
    EntityWitness,
    SelectionFailure,
    SelectionStopped,
    SourceWitness,
)
from kg.knowledge._store import Store
from kg.models.foundation import Path, SourceSupport
from kg.models.graph import (
    GraphDirection,
    GraphEntitySelector,
    GraphRelationshipProof,
    GraphTraversalRequest,
    GraphTraversalResult,
)
from kg.models.knowledge import KnowledgeSchema

_OUTGOING = """
MATCH (s:Entity)-[:SUBJECT]->(a:Assertion)-[:OBJECT]->(o:Entity),
      (s)-[:SELECTED]->(sp:EntityProof),
      (o)-[:SELECTED]->(op:EntityProof)
WHERE s.entity_id = $root
  AND a.predicate = $predicate
  AND a.object_kind = 'entity'
  AND a.interpretation = 'explicit'
RETURN s.entity_id, o.entity_id, a.assertion_id, a.dependency_json,
       sp.witness_json, op.witness_json
ORDER BY a.assertion_id
"""
_INCOMING = _OUTGOING.replace("s.entity_id = $root", "o.entity_id = $root")


def preflight_relationship(
    ctx: GraphReadContext, *, custody: ExitStack, predicate: str,
    direction: GraphDirection, max_hops: int,
) -> KnowledgeSchema:
    ctx.native.check()
    if direction not in ("outgoing", "incoming") or type(max_hops) is not int or max_hops != 1:
        raise EvidenceServiceError("invalid_request")
    store = Store(
        ctx.canonical.connection, ctx.canonical.scope, ctx.meter.private_budget,
        context=ctx.canonical,
    )
    custody.callback(store.close)
    schema = store.schema()
    if not any(p.name == predicate and p.object_kind == "entity" for p in schema.predicates):
        raise EvidenceServiceError("unsupported")
    return schema


def resolve_graph_root(
    ctx: GraphReadContext, selector: GraphEntitySelector, *, custody: ExitStack,
) -> tuple[EntitySelectionItem, ...]:
    reader = KnowledgeReader(ctx.canonical, ctx.canonical.identity)
    custody.callback(reader.close)
    cursor = reader.resolve_entity(EntitySelector(name=selector.name, entity_id=selector.entity_id))
    custody.callback(cursor.close)
    # Store owns payloads; this allowance owns the adapter's bounded pointer containers.
    custody.enter_context(ctx.meter.reserve_scratch(32768, "general"))
    candidates: list[EntitySelectionItem] = []
    while True:
        ctx.native.check()
        page = cursor.read(limit=200)
        ctx.native.check()
        if len(candidates) + len(page.items) > 1000:
            raise PrivateResourceStop()
        candidates.extend(page.items)
        terminal = page.terminal
        if isinstance(terminal, EligibleEOF):
            return tuple(candidates)
        if isinstance(terminal, SelectionStopped):
            if terminal.kind == "deadline_stop":
                raise DeadlineStop()
            raise PrivateResourceStop()
        if isinstance(terminal, SelectionFailure):
            failure = _translate(EvidenceServiceError(terminal.failure.code))
            raise GraphSessionError(replace(
                failure, diagnostic_id=terminal.failure.diagnostic_id,
            ))


def check_root_type(
    ctx: GraphReadContext, root: EntitySelectionItem, schema: KnowledgeSchema, *,
    predicate: str, direction: GraphDirection,
) -> None:
    definition = next(p for p in schema.predicates if p.name == predicate)
    with closing(ctx.canonical.connection.execute(
        "SELECT entity_type FROM entity WHERE corpus_id=? AND entity_id=?",
        (ctx.canonical.scope.corpus_id, root.entity_id),
    )) as rows:
        row = rows.fetchone()
    if row is None:
        raise NativeError("invalid_projection")
    allowed = definition.subject_types if direction == "outgoing" else definition.object_types
    if row[0] not in allowed:
        raise EvidenceServiceError("unsupported")


def _witness_scope(ctx: GraphReadContext, witness: EntityWitness) -> bool:
    scope = ctx.canonical.scope
    basis = witness.basis
    if isinstance(basis, SourceWitness):
        return all(
            item.reference.corpus_id == scope.corpus_id
            and item.reference.source_namespace in scope.access.namespaces
            for item in basis.evidence
        )
    return basis.namespace in scope.access.namespaces


def decode_relationship(
    ctx: GraphReadContext, row: tuple[NativeValue, ...], *, root: EntitySelectionItem,
    schema: KnowledgeSchema, predicate: str, direction: GraphDirection,
) -> GraphRelationshipProof:
    """Caller owns decoded-payload scratch through accumulation and output admission."""
    ctx.native.check()
    ctx.meter.reserve_visits()
    if len(row) != 6:
        raise NativeError("invalid_projection")
    subject, obj, identifier, payload, subject_json, object_json = row
    if (
        not isinstance(subject, str) or not isinstance(obj, str)
        or not isinstance(identifier, str) or not isinstance(payload, str)
        or not isinstance(subject_json, str) or not isinstance(object_json, str)
    ):
        raise NativeError("invalid_projection")
    try:
        assertion = GraphAssertion.model_validate_json(payload)
        subject_witness = EntityWitness.model_validate_json(subject_json)
        object_witness = EntityWitness.model_validate_json(object_json)
        start, related = (subject, obj) if direction == "outgoing" else (obj, subject)
        selected_root = subject_witness if direction == "outgoing" else object_witness
        scope = ctx.canonical.scope
        if (
            assertion.subject_id != subject or assertion.object_entity_id != obj
            or assertion.assertion_id != identifier or assertion.predicate != predicate
            or assertion.schema_version != schema.schema_version
            or schema.corpus_id != scope.corpus_id
            or assertion.interpretation != "explicit" or assertion.object_entity_id is None
            or assertion.subject_witness != subject_witness
            or assertion.object_witness != object_witness
            or start != root.entity_id or selected_root != root.witness
            or not _witness_scope(ctx, subject_witness) or not _witness_scope(ctx, object_witness)
            or any(e.captured.reference.corpus_id != scope.corpus_id
                   or e.captured.reference.source_namespace not in scope.access.namespaces
                   for e in assertion.support)
        ):
            raise NativeError("invalid_projection")
        # G3 verified non-root entity types; they are not present in this proof row.
        return GraphRelationshipProof(
            path=Path(
                entity_ids=(start, related), assertion_ids=(assertion.assertion_id,),
                support=SourceSupport(
                    kind="source", evidence=tuple(e.captured.reference for e in assertion.support),
                ),
            ),
            assertion=assertion,
        )
    except ValidationError:
        raise NativeError("invalid_projection") from None


def traverse(ctx: GraphReadContext, request: GraphTraversalRequest) -> GraphTraversalResult:
    with ExitStack() as custody:
        schema = preflight_relationship(
            ctx, custody=custody, predicate=request.predicate,
            direction=request.direction, max_hops=request.max_hops,
        )
        roots = resolve_graph_root(ctx, request.start, custody=custody)
        custody.enter_context(ctx.meter.reserve_scratch(32768, "general"))
        if len(roots) != 1:
            result = GraphTraversalResult(
                request_id=request.request_id, scope=request.scope, generation=ctx.generation,
                outcome="ambiguous" if roots else "empty",
                candidate_ids=tuple(item.entity_id for item in roots),
            )
            del roots
            return ctx.retain(result)
        root = roots[0]
        check_root_type(
            ctx, root, schema, predicate=request.predicate, direction=request.direction,
        )
        proofs: list[GraphRelationshipProof] = []
        previous = ""
        with closing(ctx.native.execute(
            _OUTGOING if request.direction == "outgoing" else _INCOMING,
            {"root": root.entity_id, "predicate": request.predicate},
        )) as rows:
            while page := rows.read(limit=200):
                for row in page:
                    if len(proofs) == 1000:
                        raise PrivateResourceStop()
                    custody.enter_context(ctx.meter.reserve_scratch(
                        4096 + 8 * bounded_size(row, 8 << 20), "general",
                    ))
                    proof = decode_relationship(
                        ctx, row, root=root, schema=schema, predicate=request.predicate,
                        direction=request.direction,
                    )
                    if proof.assertion.assertion_id <= previous:
                        raise NativeError("invalid_projection")
                    previous = proof.assertion.assertion_id
                    proofs.append(proof)
        result = GraphTraversalResult(
            request_id=request.request_id, scope=request.scope, generation=ctx.generation,
            outcome="complete" if proofs else "empty", root_entity_id=root.entity_id,
            paths=tuple(proofs),
        )
        return ctx.retain(result)
