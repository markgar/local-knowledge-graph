"""Model-free vocabulary discovery and explicit trusted-local administrative apply."""

import json
import logging
from pathlib import Path

from pydantic import JsonValue

from kg.client.config import ClientError, Profile
from kg.client.documents import Response, token
from kg.client.knowledge import _unique_keys
from kg.knowledge import KnowledgeAdministration, KnowledgeService
from kg.models.evidence import LocalAdminAuthority
from kg.models.schema import MAX_SCHEMA_BYTES, SchemaApplyRequest, SchemaApproval, SchemaProposal

LOGGER = logging.getLogger(__name__)


def proposal_input(file: Path) -> SchemaProposal:
    if file.is_symlink() or not file.is_file():
        raise ClientError("invalid_file", "Proposal must be a regular UTF-8 JSON file.")
    with file.open("rb") as stream:
        data = stream.read(MAX_SCHEMA_BYTES + 1)
    if len(data) > MAX_SCHEMA_BYTES:
        raise ClientError("invalid_file", "Proposal exceeds the 1 MiB limit.")
    try:
        json.loads(data.decode("utf-8"), object_pairs_hook=_unique_keys)
    except (ValueError, UnicodeError):
        raise ClientError("invalid_input", "Proposal must contain strict UTF-8 JSON.") from None
    return SchemaProposal.model_validate_json(data)


class Schema:
    def __init__(self, profile: Profile) -> None:
        self.profile = profile
        self.service = KnowledgeService(profile.database(), profile.identity)

    def show(self, revision: str | None) -> Response:
        value = self.service.schema(self.profile.scope, revision_id=revision)
        return Response(
            status="complete",
            message=f"Schema is {value.status}.",
            result=value.model_dump(mode="json"),
        )

    def history(self, after: int, limit: int) -> Response:
        value = self.service.schema_history(
            self.profile.scope,
            after_sequence=after,
            limit=limit,
        )
        return Response(
            status="complete",
            message="Immutable vocabulary revisions on this page.",
            result=value.model_dump(mode="json"),
        )

    def change(self, revision: str) -> Response:
        value = self.service.schema_change(self.profile.scope, revision)
        return Response(
            status="complete",
            message="Authorized schema change and approval provenance.",
            result=value.model_dump(mode="json"),
        )

    def validate(self, file: Path) -> Response:
        value = self.service.validate_schema(self.profile.scope, proposal_input(file))
        return Response(
            status="complete",
            message="Structurally valid proposal. Human semantic and publication review required.",
            result=value.model_dump(mode="json"),
        )

    def apply(
        self,
        file: Path,
        *,
        digest: str,
        retry_key: str,
        approval_rationale: str,
    ) -> Response:
        request = SchemaApplyRequest(
            request_id=token(),
            retry_key=retry_key,
            scope=self.profile.scope,
            proposal=proposal_input(file),
            approved_proposal_digest=digest,
            approval=SchemaApproval(human_reviewed=True, rationale=approval_rationale),
        )
        # This separate explicit operator command is the trusted-local admin boundary.
        admin = KnowledgeAdministration(
            self.profile.database(),
            LocalAdminAuthority(principal_id=self.profile.identity.principal_id),
        )
        correlation: dict[str, JsonValue] = {
            "request_id": request.request_id,
            "retry_key": retry_key,
            "proposal_digest": digest,
        }
        try:
            outcome = admin.apply_schema(request)
        except (Exception, KeyboardInterrupt):
            LOGGER.error("Schema apply raised; commit outcome is unknown.")
            return Response(
                status="uncertain",
                code="write_outcome_unknown",
                exit_code=7,
                message="Outcome unknown. Preserve and retry only this exact request and key.",
                result=correlation,
            )
        uncertain = outcome.status == "uncertain"
        return Response(
            status=outcome.status,
            code="write_outcome_unknown"
            if uncertain
            else (outcome.error.code if outcome.error else None),
            exit_code=7 if uncertain else (0 if outcome.receipt else 4),
            message="Schema committed; no facts were created."
            if outcome.receipt
            else (
                "Schema outcome unknown; preserve exact request/key."
                if uncertain
                else "Schema apply did not commit."
            ),
            result={**correlation, "apply": outcome.model_dump(mode="json")},
        )
