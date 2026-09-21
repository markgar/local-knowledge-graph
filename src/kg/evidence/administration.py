from __future__ import annotations

import sqlite3

from kg.evidence import _store
from kg.evidence._values import canonical, now, token, validated
from kg.evidence.database import EvidenceDatabase
from kg.evidence.errors import EvidenceServiceError
from kg.models.evidence import (
    CorpusRegistration,
    LocalAdminAuthority,
    LocalPolicy,
    PolicyChangeResult,
    PolicyGrant,
    RegistrationResult,
    WriterBinding,
)


def _policy_value(policy: LocalPolicy, namespace: str | None = None) -> str:
    return canonical(
        {
            "corpus_id": policy.corpus_id,
            "bindings": sorted(
                (
                    item.model_dump(mode="json")
                    for item in policy.bindings
                    if namespace is None or item.namespace == namespace
                ),
                key=canonical,
            ),
            "grants": sorted(
                (
                    item.model_dump(mode="json")
                    for item in policy.grants
                    if namespace is None or item.namespace == namespace
                ),
                key=canonical,
            ),
        }
    )


def _read_policy(connection: sqlite3.Connection, corpus_id: str) -> LocalPolicy:
    bindings = tuple(
        WriterBinding(
            namespace=row["namespace"],
            owner_id=row["owner_id"],
            writer_id=row["writer_id"],
            synchronization_scope=row["synchronization_scope"],
        )
        for row in connection.execute(
            "SELECT * FROM writer_binding WHERE corpus_id=?", (corpus_id,)
        )
    )
    grants = tuple(
        PolicyGrant(
            principal_id=row["principal_id"],
            namespace=row["namespace"],
            grant=row["grant_name"],
            owner_id=row["owner_id"] or None,
            writer_id=row["writer_id"] or None,
            synchronization_scope=row["synchronization_scope"] or None,
        )
        for row in connection.execute("SELECT * FROM policy_grant WHERE corpus_id=?", (corpus_id,))
    )
    return LocalPolicy(corpus_id=corpus_id, bindings=bindings, grants=grants)


def _write_policy(connection: sqlite3.Connection, policy: LocalPolicy) -> None:
    connection.execute("DELETE FROM policy_grant WHERE corpus_id=?", (policy.corpus_id,))
    connection.execute("DELETE FROM writer_binding WHERE corpus_id=?", (policy.corpus_id,))
    connection.executemany(
        "INSERT INTO writer_binding VALUES (?,?,?,?,?)",
        [
            (policy.corpus_id, b.namespace, b.owner_id, b.writer_id, b.synchronization_scope)
            for b in policy.bindings
        ],
    )
    connection.executemany(
        "INSERT INTO policy_grant VALUES (?,?,?,?,?,?,?)",
        [
            (
                policy.corpus_id,
                g.namespace,
                g.principal_id,
                g.grant,
                g.owner_id or "",
                g.writer_id or "",
                g.synchronization_scope or "",
            )
            for g in policy.grants
        ],
    )


class EvidenceAdministration:
    def __init__(self, database: EvidenceDatabase, authority: LocalAdminAuthority) -> None:
        self.database = database
        self.authority = validated(LocalAdminAuthority, authority)

    def register(self, registration: CorpusRegistration) -> RegistrationResult:
        registration = validated(CorpusRegistration, registration)
        definition = canonical(
            {
                "namespaces": sorted(registration.namespaces),
                "policy": _policy_value(registration.policy),
            }
        )
        with self.database.transaction() as connection:
            existing = connection.execute(
                "SELECT * FROM corpus WHERE corpus_id=?", (registration.corpus_id,)
            ).fetchone()
            if existing:
                if existing["registration_json"] != definition:
                    raise EvidenceServiceError("state_conflict")
                return RegistrationResult(
                    corpus_id=registration.corpus_id,
                    policy_version=existing["policy_version"],
                    status="unchanged",
                )
            version = token()
            connection.execute(
                "INSERT INTO corpus(corpus_id,policy_version,registration_json) VALUES (?,?,?)",
                (registration.corpus_id, version, definition),
            )
            connection.execute(
                "INSERT INTO processing_guard(corpus_id,epoch,blocked) VALUES (?,0,0)",
                (registration.corpus_id,),
            )
            connection.executemany(
                "INSERT INTO source_namespace VALUES (?,?,?)",
                [
                    (registration.corpus_id, namespace, token())
                    for namespace in registration.namespaces
                ],
            )
            _write_policy(connection, registration.policy)
            return RegistrationResult(
                corpus_id=registration.corpus_id,
                policy_version=version,
                status="applied",
            )

    def replace_policy(
        self,
        policy: LocalPolicy,
        expected_policy_version: str,
    ) -> PolicyChangeResult:
        policy = validated(LocalPolicy, policy)
        with self.database.transaction() as connection:
            corpus = connection.execute(
                "SELECT * FROM corpus WHERE corpus_id=?", (policy.corpus_id,)
            ).fetchone()
            if corpus is None:
                raise EvidenceServiceError("not_found")
            if corpus["policy_version"] != expected_policy_version:
                raise EvidenceServiceError("state_conflict")
            namespaces = {
                row[0]
                for row in connection.execute(
                    "SELECT namespace FROM source_namespace WHERE corpus_id=?",
                    (policy.corpus_id,),
                )
            }
            if any(item.namespace not in namespaces for item in policy.bindings) or any(
                item.namespace not in namespaces for item in policy.grants
            ):
                raise EvidenceServiceError("invalid_request")
            previous = _read_policy(connection, policy.corpus_id)
            affected = tuple(
                sorted(
                    namespace
                    for namespace in namespaces
                    if _policy_value(previous, namespace) != _policy_value(policy, namespace)
                )
            )
            version = token() if affected else expected_policy_version
            changed = 0
            if affected:
                _write_policy(connection, policy)
                connection.execute(
                    "UPDATE corpus SET policy_version=? WHERE corpus_id=?",
                    (version, policy.corpus_id),
                )
                for namespace in affected:
                    policy_token = token()
                    connection.execute(
                        "UPDATE source_namespace SET policy_token=? "
                        "WHERE corpus_id=? AND namespace=?",
                        (policy_token, policy.corpus_id, namespace),
                    )
                    documents = connection.execute(
                        "SELECT document_id FROM document WHERE corpus_id=? AND namespace=?",
                        (policy.corpus_id, namespace),
                    ).fetchall()
                    for doc in documents:
                        old = _store.head(connection, doc[0])
                        _store.add_state(
                            connection,
                            document_id=doc[0],
                            revision_id=old["revision_id"],
                            set_id=old["set_id"],
                            metadata=old["metadata_json"],
                            source=old["source"],
                            namespace_token=policy_token,
                            passage_policy=old["passage_policy"],
                            kind="policy",
                            at=now(),
                            previous=old,
                        )
                        changed += 1
            return PolicyChangeResult(
                corpus_id=policy.corpus_id,
                policy_version=version,
                previous_policy_version=expected_policy_version,
                status="applied" if affected else "unchanged",
                affected_namespaces=affected,
                changed_documents=changed,
            )
