"""Fixed knowledge ownership checks; attribution is not an authority."""

from kg.evidence._authorization import authorize
from kg.evidence._sql import AccountedConnection
from kg.evidence.errors import EvidenceServiceError
from kg.models.evidence import LocalIdentity
from kg.models.foundation import Scope


def writer(
    connection: AccountedConnection,
    identity: LocalIdentity,
    scope: Scope,
    namespace: str,
    owner_id: str,
    writer_id: str,
    *,
    seed: bool = False,
) -> None:
    authorize(connection, identity, scope, "read", namespace=namespace)
    authorize(connection, identity, scope, "write_knowledge", namespace=namespace)
    if seed:
        authorize(connection, identity, scope, "seed", namespace=namespace)
    if (
        connection.execute(
            "SELECT 1 FROM knowledge_writer_binding WHERE corpus_id=? AND namespace=? "
            "AND principal_id=? AND owner_id=? AND writer_id=?",
            (scope.corpus_id, namespace, identity.principal_id, owner_id, writer_id),
        ).fetchone()
        is None
    ):
        raise EvidenceServiceError("forbidden")
