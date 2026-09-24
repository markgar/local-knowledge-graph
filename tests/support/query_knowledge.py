"""Real evidence intake and K1 production for composed query acceptance."""

from uuid import uuid4

from kg.knowledge import KnowledgeAdministration
from kg.models.evidence import KnowledgeWriterBinding, PolicyGrant
from kg.models.foundation import (
    AddAssertion,
    Attribution,
    ChangeSet,
    CountStep,
    CreateEntity,
    DocumentDependency,
    QueryBudget,
    QueryRequest,
    RecordsStep,
    ResolveStep,
    SourceSupport,
    StoredEntity,
    StringObject,
    WriteRequest,
)
from support.evidence import environment, put, receipt
from support.knowledge import preset, revision, schema


def setup(path, *, registered=True):
    env = environment(path)
    env.policy = env.policy.model_copy(
        update={
            "grants": env.policy.grants
            + tuple(
                PolicyGrant(principal_id="principal", namespace=ns, grant="write_knowledge")
                for ns in ("markdown", "email")
            ),
            "knowledge_bindings": tuple(
                KnowledgeWriterBinding(
                    namespace=ns,
                    principal_id="principal",
                    owner_id="owner",
                    writer_id="writer",
                )
                for ns in ("markdown", "email")
            ),
        }
    )
    policy = env.admin.replace_policy(env.policy, env.scope.access.policy_version)
    env.scope = env.scope.model_copy(
        update={
            "access": env.scope.access.model_copy(
                update={
                    "policy_version": policy.policy_version,
                    "grants": ("read", "write_documents", "write_knowledge"),
                }
            )
        }
    )
    if registered:
        KnowledgeAdministration(env.database, env.admin.authority).register_knowledge_schema(
            preset(schema())
        )
    saved = receipt(env.service.write(put(env.scope)))
    ref = (
        env.service.anchors(
            env.scope,
            saved.document_id,
            saved.processing.state_version,
        )
        .entries[0]
        .reference
    )
    env.support = SourceSupport(kind="source", evidence=(ref,))
    env.dependency = DocumentDependency(
        source_namespace=ref.source_namespace,
        document_id=saved.document_id,
        revision_id=saved.revision_id,
        state_version=saved.processing.state_version,
    )
    return env


def write(env, changes, *, retry=None, dependencies=None):
    result = env.service.write(
        WriteRequest(
            contract_version="foundation/1",
            request_id=str(uuid4()),
            retry_key=retry or str(uuid4()),
            scope=env.scope,
            attribution=Attribution(
                owner_id="owner",
                writer_id="writer",
                producer="query-tests",
                producer_version="1",
            ),
            payload=ChangeSet(expected_schema_revision=revision(env),
                operation="enrich",
                changes=tuple(changes),
                dependencies=(env.dependency,) if dependencies is None else tuple(dependencies),
            ),
        )
    )
    assert result.error is None, result.error
    return {m.local_id: m.stored_id for m in result.receipt.mappings}


def produce(env, count, *, name="Project"):
    entity_id = write(
        env,
        (
            CreateEntity(
                kind="entity",
                local_id="subject",
                name=name,
                entity_type="project",
                support=env.support,
            ),
        ),
    )["subject"]
    records = set()
    for start in range(0, count, 100):
        records.update(
            write(
                env,
                tuple(
                    AddAssertion(
                        kind="assertion",
                        local_id=f"d{i}",
                        subject=StoredEntity(
                            kind="stored",
                            entity_id=entity_id,
                        ),
                        predicate="work:decision",
                        object=StringObject(kind="string", value="Ship"),
                        interpretation="explicit",
                        support=env.support,
                    )
                    for i in range(start, min(start + 100, count))
                ),
            ).values()
        )
    return entity_id, records


def plan(env, entity_id=None, *, name=None, output="count", max_records=10_000):
    return QueryRequest(
        contract_version="foundation/1",
        request_id="query",
        scope=env.scope,
        budget=QueryBudget(max_records=max_records, max_milliseconds=30_000),
        steps=(
            ResolveStep(operation="resolve", step_id="subject", entity_id=entity_id, name=name),
            RecordsStep(
                operation="records",
                step_id="decisions",
                entity_step="subject",
                record_type="decision",
            ),
            CountStep(operation="count", step_id="count", records_step="decisions"),
        ),
        output_step=output,
    )
