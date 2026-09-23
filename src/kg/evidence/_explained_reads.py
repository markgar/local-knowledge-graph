"""Named typed wrappers; no generic public operation/callback execution endpoint."""

from kg.evidence._reads import EvidenceReads
from kg.evidence._reporting import explained
from kg.models.evidence import (
    AnchorPage,
    DocumentView,
    EvidenceView,
    RevisionAnchorPage,
    RevisionPage,
    StatePage,
    StoredCitation,
)
from kg.models.execution import SUMMARY_OPTIONS, Explained, ExplainOptions
from kg.models.foundation import ContentResult, EvidenceRef, ExternalDocument, Scope
from kg.models.indexing import PassagePage


class ExplainedReads(EvidenceReads):
    def passages_explained(
        self, scope: Scope, document_id: str, state_version: str,
        options: ExplainOptions = SUMMARY_OPTIONS, *, after_ordinal: int = 0, limit: int = 100,
    ) -> Explained[PassagePage]:
        return explained(
            options, self.passages, scope, document_id, state_version,
            after_ordinal=after_ordinal, limit=limit,
        )

    def current_explained(
        self,
        scope: Scope,
        document: ExternalDocument,
        options: ExplainOptions = SUMMARY_OPTIONS,
    ) -> Explained[DocumentView]:
        return explained(options, self.current, scope, document)

    def document_explained(
        self,
        scope: Scope,
        document_id: str,
        options: ExplainOptions = SUMMARY_OPTIONS,
    ) -> Explained[DocumentView]:
        return explained(options, self.document, scope, document_id)

    def state_explained(
        self,
        scope: Scope,
        document_id: str,
        state_version: str,
        options: ExplainOptions = SUMMARY_OPTIONS,
    ) -> Explained[DocumentView]:
        return explained(options, self.state, scope, document_id, state_version)

    def content_explained(
        self,
        scope: Scope,
        document_id: str,
        revision_id: str,
        options: ExplainOptions = SUMMARY_OPTIONS,
    ) -> Explained[ContentResult]:
        return explained(options, self.content, scope, document_id, revision_id)

    def history_explained(
        self,
        scope: Scope,
        document_id: str,
        options: ExplainOptions = SUMMARY_OPTIONS,
        *,
        after_sequence: int = 0,
        limit: int = 100,
    ) -> Explained[StatePage]:
        return explained(
            options,
            self.history,
            scope,
            document_id,
            after_sequence=after_sequence,
            limit=limit,
        )

    def revisions_explained(
        self,
        scope: Scope,
        document_id: str,
        options: ExplainOptions = SUMMARY_OPTIONS,
        *,
        after_sequence: int = 0,
        limit: int = 100,
    ) -> Explained[RevisionPage]:
        return explained(
            options,
            self.revisions,
            scope,
            document_id,
            after_sequence=after_sequence,
            limit=limit,
        )

    def evidence_explained(
        self,
        scope: Scope,
        reference: EvidenceRef,
        options: ExplainOptions = SUMMARY_OPTIONS,
        *,
        state_version: str | None = None,
    ) -> Explained[EvidenceView]:
        return explained(options, self.evidence, scope, reference, state_version=state_version)

    def citation_explained(
        self,
        scope: Scope,
        citation: StoredCitation,
        options: ExplainOptions = SUMMARY_OPTIONS,
    ) -> Explained[EvidenceView]:
        return explained(options, self.citation, scope, citation)

    def anchors_explained(
        self,
        scope: Scope,
        document_id: str,
        state_version: str,
        options: ExplainOptions = SUMMARY_OPTIONS,
        *,
        after_ordinal: int = 0,
        limit: int = 100,
    ) -> Explained[AnchorPage]:
        return explained(
            options,
            self.anchors,
            scope,
            document_id,
            state_version,
            after_ordinal=after_ordinal,
            limit=limit,
        )

    def revision_anchors_explained(
        self,
        scope: Scope,
        document_id: str,
        revision_id: str,
        options: ExplainOptions = SUMMARY_OPTIONS,
        *,
        after_ordinal: int = 0,
        limit: int = 100,
    ) -> Explained[RevisionAnchorPage]:
        return explained(
            options,
            self.revision_anchors,
            scope,
            document_id,
            revision_id,
            after_ordinal=after_ordinal,
            limit=limit,
        )
