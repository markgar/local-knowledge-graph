"""Document workflows over the canonical public services."""

from __future__ import annotations

import base64
import binascii
import logging
from pathlib import Path
from uuid import uuid4

from pydantic import JsonValue

from kg.client.config import ClientError, Profile
from kg.evidence import EvidenceService
from kg.evidence.errors import EvidenceServiceError
from kg.indexing import EvidenceSearchService, IndexService
from kg.models.evidence import DocumentView, EvidenceView, StoredCitation
from kg.models.foundation import (
    MAX_TEXT_BYTES,
    CreateOnly,
    DocumentReceipt,
    ExpectedState,
    ExternalDocument,
    PutDocument,
    RemoveDocument,
    SourceMetadata,
    SuppliedAnchor,
    SuppliedContent,
    Value,
    WriteRequest,
)

LOGGER = logging.getLogger(__name__)


class Response(Value):
    interface_version: str = "client/1"
    status: str
    message: str
    result: dict[str, JsonValue] = {}
    code: str | None = None
    exit_code: int = 0


def token() -> str:
    return str(uuid4())


def evidence_entry(view: EvidenceView) -> dict[str, JsonValue]:
    encoded = base64.urlsafe_b64encode(view.citation.model_dump_json().encode()).decode()
    return {
        "target": "evidence:" + encoded,
        "support": {
            "reference": view.reference.model_dump(mode="json"),
            "state_version": view.state_version,
        },
        "evidence": view.model_dump(mode="json"),
    }


def capture(path: Path) -> SuppliedContent:
    if path.is_symlink() or not path.is_file():
        raise ClientError("invalid_file", "Input must be a regular UTF-8 file.")
    with path.open("rb") as stream:
        data = stream.read(MAX_TEXT_BYTES + 1)
    if len(data) > MAX_TEXT_BYTES:
        raise ClientError("invalid_file", "Input exceeds the supported document byte limit.")
    text = data.decode("utf-8")
    # Adapt window size for the canonical limit of 1,000 supplied exact anchors.
    width = max(1024, (len(text) + 999) // 1000)
    anchors = tuple(
        SuppliedAnchor(
            local_id=str(i // width + 1),
            start=i,
            end=min(i + width, len(text)),
            quote=text[i : i + width],
        )
        for i in range(0, len(text), width)
    )
    return SuppliedContent(text=text, anchors=anchors, passage_policy="codepoint-window/1")


class Documents:
    def __init__(self, profile: Profile) -> None:
        self.profile = profile
        self.database = profile.database()
        self.evidence = EvidenceService(self.database, profile.identity)

    def require_models(self) -> None:
        if not self.profile.models_approved:
            raise ClientError(
                "models_not_approved",
                "Local model execution is not approved. Review the profile's models_approved "
                "setting and the model requirements in kg setup --help before enabling it.",
            )

    def document(self, target: str) -> DocumentView:
        if not target.startswith("document:") or not target.removeprefix("document:"):
            raise ClientError("invalid_target", "Use the document:ID returned by add or search.")
        document_id, separator, state = target.removeprefix("document:").partition("@")
        if separator:
            return self.evidence.state(self.profile.scope, document_id, state)
        return self.evidence.document(self.profile.scope, document_id)

    def save(
        self,
        file: Path,
        *,
        previous: DocumentView | None = None,
        expected: str | None = None,
    ) -> Response:
        self.require_models()
        content = capture(file)
        if previous is not None:
            if expected is None or expected != previous.state_version:
                raise ClientError("state_conflict", "Document state changed; read it again.")
            document = previous.document
            metadata = previous.metadata.metadata
            precondition: CreateOnly | ExpectedState = ExpectedState(
                kind="match",
                state_version=expected,
            )
        else:
            document = ExternalDocument(
                source_namespace=self.profile.namespace,
                synchronization_scope=self.profile.synchronization_scope,
                external_id=token(),
            )
            metadata = SourceMetadata(title=file.name, location=file.resolve().as_uri())
            precondition = CreateOnly(kind="create")
        return self._write(
            PutDocument(
                operation="put_document",
                document=document,
                precondition=precondition,
                content=content,
                metadata=metadata,
            ),
            prepare=True,
        )

    def remove(self, previous: DocumentView, expected: str) -> Response:
        if previous.state_version != expected:
            raise ClientError("state_conflict", "Document state changed; read it again.")
        return self._write(
            RemoveDocument(
                operation="remove_document",
                document=previous.document,
                precondition=ExpectedState(kind="match", state_version=expected),
            ),
            prepare=False,
        )

    def _write(self, payload: PutDocument | RemoveDocument, *, prepare: bool) -> Response:
        request = WriteRequest(
            contract_version="foundation/1",
            request_id=token(),
            retry_key=token(),
            scope=self.profile.scope,
            attribution=self.profile.attribution,
            payload=payload,
        )
        try:
            outcome = self.evidence.write(request)
        except (Exception, KeyboardInterrupt):
            LOGGER.error("Canonical write raised; commit outcome is unknown.")
            return Response(
                status="uncertain",
                code="write_outcome_unknown",
                exit_code=7,
                message="Write outcome unknown. Manual resubmission may create duplicates.",
                result={"request_id": request.request_id},
            )
        result: dict[str, JsonValue] = {"write": outcome.model_dump(mode="json")}
        if not isinstance(outcome.receipt, DocumentReceipt):
            return Response(
                status=outcome.status,
                result=result,
                code=outcome.error.code if outcome.error else None,
                message="Document write did not complete.",
                exit_code=4,
            )
        receipt = outcome.receipt
        result["target"] = f"document:{receipt.document_id}"
        result["state"] = receipt.processing.state_version
        if not prepare:
            return Response(
                status="complete",
                message="Document deactivated. Exact history retained.",
                result=result,
            )
        try:
            index = IndexService(
                self.database,
                self.profile.identity,
                local_files_only=True,
                model_cache=self.profile.model_cache,
            )
            prepared = index.process(
                self.profile.scope,
                self.profile.attribution,
                receipt.document_id,
                receipt.processing.state_version,
                self.profile.index,
            )
            result["preparation"] = prepared.model_dump(mode="json")
            if prepared.outcome not in {"ready", "unchanged"}:
                return Response(
                    status="partial",
                    code=prepared.reason,
                    exit_code=5,
                    result=result,
                    message="Saved; search preparation failed. Exact text remains saved.",
                )
        except (Exception, KeyboardInterrupt) as error:
            LOGGER.error("Search preparation raised after confirmed document save.")
            return Response(
                status="partial",
                exit_code=5,
                result=result,
                code=error.failure.code
                if isinstance(error, EvidenceServiceError)
                else "preparation_failed",
                message="Saved; search preparation failed. Exact text remains saved.",
            )
        message = "Saved. Search preparation complete for this document. No facts were extracted."
        if isinstance(payload.precondition, ExpectedState):
            message += " Knowledge supported by the old state may need reassessment."
        return Response(status="complete", message=message, result=result)

    def read(self, target: str, *, history: bool, after: int, limit: int) -> Response:
        if target.startswith("evidence:"):
            if history or after:
                raise ClientError(
                    "invalid_option", "Evidence references have no history/page selector."
                )
            try:
                raw = base64.b64decode(
                    target.removeprefix("evidence:"),
                    altchars=b"-_",
                    validate=True,
                )
            except (ValueError, binascii.Error):
                raise ClientError("invalid_target", "Invalid evidence reference.") from None
            citation = StoredCitation.model_validate_json(raw)
            value = self.evidence.citation(self.profile.scope, citation)
            return Response(
                status="complete",
                message=value.quote,
                result=evidence_entry(value),
            )
        document = self.document(target)
        if history:
            history_page = self.evidence.history(
                self.profile.scope,
                document.document_id,
                after_sequence=after,
                limit=limit,
            )
            return Response(
                status="complete",
                message="Document history.",
                result={
                    "page": history_page.model_dump(mode="json"),
                    "targets": [
                        f"document:{entry.document_id}@{entry.state_version}"
                        for entry in history_page.entries
                    ],
                },
            )
        page = self.evidence.anchors(
            self.profile.scope,
            document.document_id,
            document.state_version,
            after_ordinal=after,
            limit=limit,
        )
        return Response(
            status="complete",
            message="".join(entry.quote for entry in page.entries),
            result={
                "document": document.model_dump(mode="json"),
                "entries": [evidence_entry(entry) for entry in page.entries],
                "has_more": page.has_more,
                "next_after": page.next_after_ordinal,
            },
        )

    def search(self, text: str, limit: int) -> Response:
        self.require_models()
        search = EvidenceSearchService(
            self.database,
            self.profile.identity,
            local_files_only=True,
            model_cache=self.profile.model_cache,
        )
        result = search.search(self.profile.scope, text, self.profile.index, limit=limit)
        return Response(
            status="complete" if result.hits else "empty",
            message="Ranked matching passages." if result.hits else "No matching passages.",
            result={
                "search": result.model_dump(mode="json"),
                "entries": [
                    {
                        "document": f"document:{hit.evidence.reference.document_id}",
                        "score": hit.score,
                        **evidence_entry(hit.evidence),
                    }
                    for hit in result.hits
                ],
            },
        )
