"""Validate an independent entity attestation; this does not execute enrichment."""

from pathlib import Path

from kg.models.foundation import WriteRequest

if __name__ == "__main__":
    fixture = Path(__file__).resolve().parents[1] / "corpora/foundation/entity-support.json"
    request = WriteRequest.model_validate_json(fixture.read_text())
    print(request.model_dump_json(indent=2))
