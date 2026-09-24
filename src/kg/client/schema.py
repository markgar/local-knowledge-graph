"""Model-free vocabulary discovery and explicit trusted-local administrative apply."""

import json
import logging
from pathlib import Path

from pydantic import JsonValue

from kg.client.config import ClientError, Profile
from kg.client.documents import Response, token
from kg.client.knowledge import _unique_keys
from kg.evidence import EvidenceService
from kg.evidence.errors import EvidenceServiceError
from kg.knowledge import KnowledgeAdministration, KnowledgeService
from kg.models.evidence import LocalAdminAuthority
from kg.models.schema import (
    MAX_SCHEMA_BYTES,
    SchemaApplyRequest,
    SchemaApproval,
    SchemaProposal,
    SchemaSample,
)

LOGGER = logging.getLogger(__name__)


def schema_input(file: Path) -> bytes:
    if file.is_symlink() or not file.is_file():
        raise ClientError("invalid_file", "Schema input must be a regular UTF-8 JSON file.")
    with file.open("rb") as stream:
        data = stream.read(MAX_SCHEMA_BYTES + 1)
    if len(data) > MAX_SCHEMA_BYTES:
        raise ClientError("invalid_file", "Schema input exceeds the 1 MiB limit.")
    try:
        json.loads(data.decode("utf-8"), object_pairs_hook=_unique_keys)
    except (ValueError, UnicodeError):
        raise ClientError("invalid_input", "Schema input must contain strict UTF-8 JSON.") from None
    return data


def proposal_input(file: Path) -> SchemaProposal:
    return SchemaProposal.model_validate_json(schema_input(file))


class Schema:
    def __init__(self, profile: Profile) -> None:
        self.profile = profile
        self.service = KnowledgeService(profile.database(), profile.identity)

    def capabilities(self) -> Response:
        knowledge = self.service.capabilities(self.profile.scope)
        return Response(
            status="complete",
            message="Installed capabilities, not search readiness or external-agent permission.",
            result={
                "evidence": EvidenceService(
                    self.profile.database(), self.profile.identity,
                ).capabilities().model_dump(mode="json"),
                "knowledge": knowledge.model_dump(mode="json"),
                "workflows": {
                    "exact_intake_read": True,
                    "initial_generation": "external_agent_context"
                    if knowledge.schema_status == "unconfigured" else "already_configured",
                    "embedded_interpretation": False,
                    "human_review_required": True,
                    "models_approved": self.profile.models_approved,
                    "search_readiness": "not_checked",
                    "search_requires": ["approved_cached_models", "prepared_indexes"],
                    "typed_knowledge_requires": "approved_schema",
                },
            },
        )

    def generate(self, file: Path) -> Response:
        sample = SchemaSample.model_validate_json(schema_input(file))
        try:
            value = self.service.schema_generation_context(self.profile.scope, sample)
        except EvidenceServiceError as error:
            if error.failure.code == "invalid_request":
                raise ClientError(
                    "invalid_request", "No usable selected source text. Revise the sample.",
                ) from None
            if error.failure.code == "state_conflict":
                raise ClientError(
                    "state_conflict", "Sample or initial schema head changed. Inspect schema show "
                    "and source states; use an additive proposal if already configured.",
                ) from None
            raise
        return Response(
            status="awaiting_agent",
            message="Exact selected-excerpt context prepared. The external agent must interpret "
            "it and author a proposal; no schema or facts created. Validate, then stop for "
            "human review of the exact digest.",
            result={
                **value.model_dump(mode="json"),
                "attribution": self.profile.attribution.model_dump(mode="json"),
            },
        )

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
        proposal = proposal_input(file)
        if (
            proposal.initial_generation is not None
            and proposal.initial_generation.coverage_status == "insufficient"
        ):
            raise ClientError(
                "invalid_request", "Sample coverage is insufficient. Revise the operator-selected "
                "sample or report deferral; no approvable digest.",
            )
        value = self.service.validate_schema(self.profile.scope, proposal)
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
