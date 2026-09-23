"""Fixed E4 authorization, including selections that yielded no durable objects."""

from kg.diagnostics._targets import (
    AuthorizationBinding,
    ProcessingSelectionTarget,
    ProcessingTarget,
    ReportTarget,
)
from kg.evidence._diagnostic_authorization import EvidenceReportAuthorizer
from kg.evidence._sql import AccountedConnection
from kg.evidence.errors import EvidenceServiceError
from kg.models.processing import WorkerSelection
from kg.processing import _authorization as auth


def worker(target: ProcessingSelectionTarget) -> WorkerSelection:
    return WorkerSelection.model_validate(target.model_dump(exclude={"kind"}))


class ProcessingReportAuthorizer(EvidenceReportAuthorizer):
    def authorize_target(
        self,
        connection: AccountedConnection,
        binding: AuthorizationBinding,
        target: ReportTarget,
    ) -> None:
        if isinstance(target, ProcessingSelectionTarget):
            auth.selection(
                connection,
                binding.identity,
                binding.scope,
                worker(target),
                enabled=True,
            )
        elif isinstance(target, ProcessingTarget):
            if target.job_id is None or target.batch_id is not None or target.run_id is not None:
                raise EvidenceServiceError("unsupported")
            selections = [
                worker(value)
                for value in binding.targets
                if isinstance(value, ProcessingSelectionTarget)
            ]
            row = connection.execute(
                "SELECT * FROM processing_job WHERE corpus_id=? AND principal_id=? AND job_id=?",
                (binding.scope.corpus_id, binding.identity.principal_id, target.job_id),
            ).fetchone()
            if row is None:
                raise EvidenceServiceError("not_found")
            matches = [
                selection
                for selection in selections
                if (
                    selection.namespace,
                    selection.owner_id,
                    selection.writer_id,
                    selection.plan_id,
                    selection.plan_version,
                )
                == (
                    row["namespace"],
                    row["owner_id"],
                    row["writer_id"],
                    row["plan_id"],
                    row["plan_version"],
                )
            ]
            if not matches:
                raise EvidenceServiceError("forbidden")
            selection = matches[0]
            auth.selection(connection, binding.identity, binding.scope, selection, enabled=True)
            row = auth.job(connection, binding.scope, selection, target.job_id)
            auth.document(
                connection,
                binding.scope,
                selection,
                auth.dependency(connection, row),
                current=False,
            )
        else:
            # This slice has no evidence/knowledge/indexing execution targets.
            raise EvidenceServiceError("unsupported")
