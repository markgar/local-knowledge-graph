from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from uuid import uuid4

from pydantic import BaseModel, ValidationError
from pydantic_core import PydanticSerializationError

from kg.evidence.errors import EvidenceServiceError
from kg.models.foundation import SourceMetadata, WriteRequest


def validated[Model: BaseModel](kind: type[Model], value: Model) -> Model:
    try:
        return kind.model_validate_json(value.model_dump_json(warnings="error"))
    except (ValidationError, ValueError, TypeError, AttributeError, PydanticSerializationError):
        raise EvidenceServiceError("invalid_request") from None


def token() -> str:
    return str(uuid4())


def now() -> datetime:
    return datetime.now(UTC)


def timestamp(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise EvidenceServiceError("internal_error")
    return value.astimezone(UTC).isoformat(timespec="microseconds")


def sha(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def metadata_json(metadata: SourceMetadata) -> str:
    value = metadata.model_dump(mode="json")
    value["attributes"] = [
        item.model_dump(mode="json") for item in sorted(metadata.attributes, key=lambda a: a.key)
    ]
    return canonical(value)


def request_digest(request: WriteRequest) -> str:
    payload = request.payload.model_dump(mode="json")
    from kg.models.foundation import PutDocument

    if isinstance(request.payload, PutDocument):
        payload["metadata"] = json.loads(metadata_json(request.payload.metadata))
        payload["content"]["anchors"] = [
            anchor.model_dump(mode="json")
            for anchor in sorted(request.payload.content.anchors, key=lambda a: a.local_id)
        ]
    return sha(
        canonical(
            {
                "version": "e1-request-digest/1",
                "corpus_id": request.scope.corpus_id,
                "payload": payload,
                "attribution": request.attribution.model_dump(mode="json"),
            }
        ).encode()
    )
