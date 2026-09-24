from __future__ import annotations

import time
from pathlib import Path

import pytest
from support.evidence import environment, put, receipt

from kg._execution_budget import Deadline, PrivateBudget, PrivateResourceStop
from kg.evidence import EvidenceServiceError
from kg.evidence._support import TransactionEvidence
from kg.evidence._transactions import writing
from kg.models.evidence import LocalPolicy, PolicyGrant
from kg.models.foundation import DocumentDependency, ExpectedState, RemoveDocument


@pytest.mark.service
@pytest.mark.parametrize("changed_index", [0, 1])
@pytest.mark.parametrize(
    "transition", ["metadata", "content", "remove", "restore", "anchors", "policy"]
)
def test_real_multidocument_current_support_and_stale_dependency(
    tmp_path: Path,
    changed_index: int,
    transition: str,
) -> None:
    env = environment(tmp_path / "e.db")
    policy = LocalPolicy(
        corpus_id="work",
        bindings=env.policy.bindings,
        grants=(
            *env.policy.grants,
            *(
                PolicyGrant(
                    principal_id="principal",
                    namespace=namespace,
                    grant="write_knowledge",
                )
                for namespace in ("markdown", "email")
            ),
        ),
    )
    version = env.admin.replace_policy(policy, env.scope.access.policy_version).policy_version
    scope = env.scope.model_copy(
        update={
            "access": env.scope.access.model_copy(
                update={
                    "policy_version": version,
                    "grants": ("read", "write_documents", "write_knowledge"),
                }
            )
        }
    )
    saved = tuple(
        receipt(env.service.write(put(scope, namespace=ns))) for ns in ("markdown", "email")
    )
    references = tuple(
        env.service.anchors(
            scope,
            item.document_id,
            item.processing.state_version,
        )
        .entries[0]
        .reference
        for item in saved
    )
    dependencies = tuple(
        DocumentDependency(
            source_namespace=ref.source_namespace,
            document_id=ref.document_id,
            revision_id=ref.revision_id,
            state_version=item.processing.state_version,
        )
        for ref, item in zip(references, saved, strict=True)
    )
    budget = PrivateBudget(Deadline(time.monotonic() + 30))
    with writing(env.database, env.service.identity, budget=budget) as context:
        validator = TransactionEvidence(context)
        supports = validator.validate_current(scope, dependencies, references)
        assert [support.metadata_snapshot_id for support in supports] == [
            item.metadata_snapshot_id for item in saved
        ]
        assert all(support.quote == "A\r\nCafe\u0301 \U0001f680" for support in supports)
        assert context.connection.in_transaction
        with (
            context.using_budget(budget.limited(max_visits=1)),
            pytest.raises(PrivateResourceStop),
        ):
            validator.validate_current(scope, dependencies, references)
        with pytest.raises(EvidenceServiceError) as error:
            validator.validate_current(
                scope,
                dependencies,
                (references[0].model_copy(update={"passage_id": "not-produced"}),),
            )
        assert error.value.failure.code == "not_found"
        with pytest.raises(EvidenceServiceError):
            validator.validate_current(
                scope,
                dependencies,
                (references[0].model_copy(update={"anchor_id": references[1].anchor_id}),),
            )
    with pytest.raises(EvidenceServiceError):
        validator.validate_current(scope, dependencies, references)
    changed = saved[changed_index]
    update = put(
        scope,
        namespace=references[changed_index].source_namespace,
        state=changed.processing.state_version,
        title="Metadata changes support state",
    )
    if transition in {"remove", "restore"}:
        removed = receipt(
            env.service.write(
                update.model_copy(
                    update={
                        "payload": RemoveDocument(
                            operation="remove_document",
                            document=update.payload.document,
                            precondition=ExpectedState(
                                kind="match", state_version=changed.processing.state_version
                            ),
                        )
                    }
                )
            )
        )
        if transition == "restore":
            restored = receipt(
                env.service.write(
                    put(
                        scope,
                        namespace=references[changed_index].source_namespace,
                        state=removed.processing.state_version,
                    )
                )
            )
            assert restored.revision_id == changed.revision_id
    elif transition == "policy":
        new_policy = policy.model_copy(
            update={
                "grants": (
                    *policy.grants,
                    PolicyGrant(
                        principal_id="another",
                        namespace=references[changed_index].source_namespace,
                        grant="read",
                    ),
                )
            }
        )
        version = env.admin.replace_policy(new_policy, version).policy_version
        scope = scope.model_copy(
            update={
                "access": scope.access.model_copy(
                    update={
                        "policy_version": version,
                    }
                )
            }
        )
    else:
        if transition in {"content", "anchors"}:
            content = update.payload.content.model_copy(
                update={
                    "text": "different" if transition == "content" else update.payload.content.text,
                    "anchors": (),
                }
            )
            update = update.model_copy(
                update={
                    "payload": update.payload.model_copy(
                        update={
                            "content": content,
                        }
                    )
                }
            )
        receipt(env.service.write(update))
    with writing(env.database, env.service.identity) as context:
        with pytest.raises(EvidenceServiceError) as error:
            TransactionEvidence(context).validate_current(scope, dependencies, references)
        assert error.value.failure.code == "state_conflict"
    if transition == "anchors":
        current = env.service.document(scope, changed.document_id)
        refreshed = tuple(
            dep.model_copy(update={"state_version": current.state_version})
            if index == changed_index
            else dep
            for index, dep in enumerate(dependencies)
        )
        with writing(env.database, env.service.identity) as context:
            with pytest.raises(EvidenceServiceError) as error:
                TransactionEvidence(context).validate_current(scope, refreshed, references)
            assert error.value.failure.code == "not_found"
