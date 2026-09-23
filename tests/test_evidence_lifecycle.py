from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from support.evidence import environment, put, receipt

from kg.evidence import EvidenceServiceError
from kg.evidence._sql import AccountedConnection
from kg.models.evidence import LocalPolicy, PolicyGrant, WriterBinding
from kg.models.foundation import ExpectedState, RemoveDocument


def _bookkeeping(env):
    with env.database.connection() as connection:
        return (
            [
                tuple(row)
                for row in connection.execute(
                    "SELECT document_id,state_version,change_kind FROM state_intent "
                    "ORDER BY intent_sequence"
                )
            ],
            [
                tuple(row)
                for row in connection.execute(
                    "SELECT corpus_id,namespace,owner_id,synchronization_scope,"
                    "generation,mutation_epoch "
                    "FROM sync_scope ORDER BY corpus_id,namespace,owner_id,synchronization_scope"
                )
            ],
        )


def test_state_changes_atomic_intents_and_noop_replay(tmp_path: Path) -> None:
    env = environment(tmp_path / "lifecycle.db")
    request = put(env.scope)
    initial = receipt(env.service.write(request))
    before = _bookkeeping(env)
    assert receipt(env.service.write(request)) == initial
    assert (
        env.service.write(put(env.scope, state=initial.processing.state_version)).status
        == "unchanged"
    )
    assert _bookkeeping(env) == before
    edited_request = put(env.scope, state=initial.processing.state_version, text="Edited")
    edited = receipt(env.service.write(edited_request))
    removed = receipt(
        env.service.write(
            edited_request.model_copy(
                update={
                    "retry_key": "remove",
                    "payload": RemoveDocument(
                        operation="remove_document",
                        document=request.payload.document,
                        precondition=ExpectedState(
                            kind="match", state_version=edited.processing.state_version
                        ),
                    ),
                }
            )
        )
    )
    restored = receipt(env.service.write(put(env.scope, state=removed.processing.state_version)))
    metadata = receipt(
        env.service.write(
            put(env.scope, state=restored.processing.state_version, title="Changed title")
        )
    )
    assert initial.revision_id == restored.revision_id == metadata.revision_id
    assert (
        len(
            {
                item.processing.state_version
                for item in (initial, edited, removed, restored, metadata)
            }
        )
        == 5
    )
    intents, scopes = _bookkeeping(env)
    assert intents == [
        (item.document_id, item.processing.state_version, kind)
        for item, kind in zip(
            (initial, edited, removed, restored, metadata),
            ("put", "put", "remove", "put", "put"),
            strict=True,
        )
    ]
    assert scopes == [("work", "markdown", "owner", "all", 0, 5)]
    assert env.service.write(request).status == "applied"
    assert _bookkeeping(env) == (intents, scopes)


def test_policy_rotation_includes_inactive_and_only_affected_namespaces(tmp_path: Path) -> None:
    env = environment(tmp_path / "policy.db")
    active = receipt(env.service.write(put(env.scope, external="active")))
    request = put(env.scope, external="inactive")
    inactive = receipt(env.service.write(request))
    receipt(
        env.service.write(
            request.model_copy(
                update={
                    "retry_key": "remove",
                    "payload": RemoveDocument(
                        operation="remove_document",
                        document=request.payload.document,
                        precondition=ExpectedState(
                            kind="match", state_version=inactive.processing.state_version
                        ),
                    ),
                }
            )
        )
    )
    email = receipt(env.service.write(put(env.scope, namespace="email")))
    before = _bookkeeping(env)
    policy = LocalPolicy(
        corpus_id="work",
        bindings=env.policy.bindings,
        grants=(
            *env.policy.grants,
            PolicyGrant(principal_id="another", namespace="markdown", grant="read"),
        ),
    )
    changed = env.admin.replace_policy(policy, env.scope.access.policy_version)
    assert changed.changed_documents == 2
    after = _bookkeeping(env)
    assert len(after[0]) == len(before[0]) + 2
    assert {row[0] for row in after[0][-2:]} == {active.document_id, inactive.document_id}
    assert all(row[2] == "policy" for row in after[0][-2:])
    assert [row for row in after[0] if row[0] == email.document_id] == [
        row for row in before[0] if row[0] == email.document_id
    ]
    assert after[1] == [
        ("work", "email", "owner", "all", 0, 1),
        ("work", "markdown", "owner", "all", 0, 5),
    ]
    assert env.admin.replace_policy(policy, changed.policy_version).status == "unchanged"
    assert _bookkeeping(env) == after


def test_scope_epoch_uses_owner_not_writer_and_isolates_corpus(tmp_path: Path) -> None:
    env = environment(tmp_path / "scope.db")
    bindings = tuple(
        WriterBinding(
            namespace="markdown", owner_id=owner, writer_id=writer, synchronization_scope=sync
        )
        for owner, writer, sync in (
            ("owner", "second", "all"),
            ("another", "third", "all"),
            ("owner", "fourth", "other"),
        )
    )
    policy = LocalPolicy(
        corpus_id="work",
        bindings=(*env.policy.bindings, *bindings),
        grants=(
            *env.policy.grants,
            *(
                PolicyGrant(
                    principal_id="principal",
                    namespace=b.namespace,
                    grant="write_documents",
                    owner_id=b.owner_id,
                    writer_id=b.writer_id,
                    synchronization_scope=b.synchronization_scope,
                )
                for b in bindings
            ),
        ),
    )
    version = env.admin.replace_policy(policy, env.scope.access.policy_version).policy_version
    scope = env.scope.model_copy(
        update={"access": env.scope.access.model_copy(update={"policy_version": version})}
    )
    receipt(env.service.write(put(scope)))
    for binding in bindings:
        request = put(scope, external=binding.writer_id)
        request = request.model_copy(
            update={
                "attribution": request.attribution.model_copy(
                    update={
                        "owner_id": binding.owner_id,
                        "writer_id": binding.writer_id,
                    }
                ),
                "payload": request.payload.model_copy(
                    update={
                        "document": request.payload.document.model_copy(
                            update={
                                "synchronization_scope": binding.synchronization_scope,
                            }
                        ),
                    }
                ),
            }
        )
        receipt(env.service.write(request))
    other = environment(env.database.path, corpus="other")
    receipt(other.service.write(put(other.scope)))
    assert _bookkeeping(env)[1] == [
        ("other", "markdown", "owner", "all", 0, 1),
        ("work", "markdown", "another", "all", 0, 1),
        ("work", "markdown", "owner", "all", 0, 2),
        ("work", "markdown", "owner", "other", 0, 1),
    ]


@pytest.mark.parametrize("operation", ["create", "edit", "policy"])
def test_intent_failure_rolls_back_epoch_state_and_policy(
    tmp_path: Path, monkeypatch, operation: str
) -> None:
    env = environment(tmp_path / "fault.db")
    saved = receipt(env.service.write(put(env.scope)))
    with env.database.connection() as connection:
        before = tuple(connection.iterdump())

    class FaultConnection(AccountedConnection):
        def execute(self, sql, parameters=()):
            if sql.startswith("INSERT INTO state_intent"):
                raise sqlite3.OperationalError("injected between epoch and intent")
            return super().execute(sql, parameters)

    def open_fault(*, create, budget=None):
        connection = sqlite3.connect(env.database.path, factory=FaultConnection)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    with monkeypatch.context() as patch:
        patch.setattr(env.database, "_open", open_fault)
        if operation == "policy":
            changed = env.policy.model_copy(
                update={
                    "grants": (
                        *env.policy.grants,
                        PolicyGrant(principal_id="another", namespace="markdown", grant="read"),
                    )
                }
            )
            with pytest.raises(EvidenceServiceError) as error:
                env.admin.replace_policy(changed, env.scope.access.policy_version)
            assert error.value.failure.code == "internal_error"
        else:
            result = env.service.write(
                put(
                    env.scope,
                    external="new" if operation == "create" else "doc",
                    state=None if operation == "create" else saved.processing.state_version,
                    text="Changed",
                )
            )
            assert result.status == "failed" and result.error.code == "internal_error"
    with env.database.connection() as connection:
        assert tuple(connection.iterdump()) == before


@pytest.mark.parametrize(
    "column,value",
    [
        ("origin_kind", "generated"),
        ("origin_key", "wrong-origin"),
    ],
)
def test_supplied_anchor_reuse_checks_immutable_origin(tmp_path: Path, column, value) -> None:
    env = environment(tmp_path / "origin.db")
    saved = receipt(env.service.write(put(env.scope)))
    with env.database.transaction() as connection:
        connection.execute(f"UPDATE anchor SET {column}=?", (value,))
    before = _bookkeeping(env)
    result = env.service.write(put(env.scope, state=saved.processing.state_version))
    assert result.status == "failed"
    assert result.error.code == "internal_error"
    assert _bookkeeping(env) == before


@pytest.mark.parametrize("rollback", [False, True])
def test_absence_primitive_is_atomic_and_has_no_synthetic_retry_key(tmp_path, rollback) -> None:
    from kg.evidence._coordination import DocumentTarget
    from kg.evidence._lifecycle import deactivate_absent
    from kg.evidence._transactions import writing

    env = environment(tmp_path / "absence.db")
    request = put(env.scope)
    saved = receipt(env.service.write(request))
    with env.database.transaction() as connection:
        namespace_token = connection.execute(
            "SELECT policy_token FROM source_namespace WHERE namespace='markdown'",
        ).fetchone()[0]
        # Only the E1 hook is under test; E4 will own real run admission and completion.
        connection.execute(
            "INSERT INTO sync_run(run_id,corpus_id,namespace,owner_id,synchronization_scope,"
            "writer_id,principal_id,generation,expected_epoch,namespace_token,guard_epoch,status,"
            "admitted_pages,admitted_units,admitted_documents,created_at) "
            "VALUES ('run','work','markdown','owner','all','writer','principal',1,1,?,0,'open',"
            "0,0,0,'2026-01-01T00:00:00.000000+00:00')", (namespace_token,),
        )
    target = DocumentTarget(
        corpus_id="work", namespace="markdown", document_id=saved.document_id,
        revision_id=saved.revision_id, state_version=saved.processing.state_version,
        namespace_token=namespace_token,
    )
    before = _bookkeeping(env)
    try:
        with writing(env.database, env.service.identity) as context:
            removed = deactivate_absent(context, target, request.attribution, "run")
            assert removed.processing.source == "inactive"
            assert removed.revision_id == saved.revision_id
            if rollback:
                raise RuntimeError("finish acknowledgement failed")
    except RuntimeError:
        assert rollback
    with env.database.connection() as connection:
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert connection.execute("SELECT count(*) FROM write_key").fetchone()[0] == 1
        assert connection.execute(
            "SELECT count(*) FROM sync_removal_provenance",
        ).fetchone()[0] == (0 if rollback else 1)
    if rollback:
        assert _bookkeeping(env) == before
    else:
        assert len(_bookkeeping(env)[0]) == 2
        assert _bookkeeping(env)[1][0][-1] == 2
    with pytest.raises(EvidenceServiceError):
        deactivate_absent(context, target, request.attribution, "run")
