from __future__ import annotations

import hashlib
import uuid

DOCUMENT_NAMESPACE = uuid.UUID("4b91cb31-074f-46db-abaa-d16973b77b3d")


def document_id(corpus_id: str, source_path: str, *, generation: int = 0) -> str:
    if generation < 0:
        raise ValueError("generation must be nonnegative")
    key = (
        f"{corpus_id}:{source_path}"
        if generation == 0
        else digest("document-instance", corpus_id, source_path, str(generation))
    )
    return str(uuid.uuid5(DOCUMENT_NAMESPACE, key))


def digest(*parts: str | bytes) -> str:
    value = hashlib.sha256()
    for part in parts:
        encoded = part if isinstance(part, bytes) else part.encode("utf-8")
        value.update(len(encoded).to_bytes(8, "big"))
        value.update(encoded)
    return value.hexdigest()


def revision_id(stable_document_id: str, content: bytes) -> str:
    return digest(stable_document_id, content)


def anchor_id(
    stable_revision_id: str,
    structural_path: str,
    start_offset: int,
    end_offset: int,
) -> str:
    return digest(
        stable_revision_id,
        structural_path,
        str(start_offset),
        str(end_offset),
    )


def record_id(stable_anchor_id: str, record_type: str, value: str) -> str:
    return digest(stable_anchor_id, record_type.casefold(), value.strip().casefold())
