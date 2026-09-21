from __future__ import annotations

import logging
from uuid import uuid4

from kg.models.foundation import ErrorCode, Failure

LOGGER = logging.getLogger(__name__)


class EvidenceServiceError(Exception):
    def __init__(self, code: ErrorCode) -> None:
        self.failure = Failure(code=code, diagnostic_id=str(uuid4()))
        super().__init__(f"{code} ({self.failure.diagnostic_id})")


def storage_error(error: Exception) -> EvidenceServiceError:
    result = EvidenceServiceError("internal_error")
    LOGGER.error(
        "Evidence failure diagnostic=%s class=%s",
        result.failure.diagnostic_id,
        type(error).__name__,
    )
    return result
