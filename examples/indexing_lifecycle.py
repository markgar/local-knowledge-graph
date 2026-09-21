"""Index one existing supplied document using its current trusted scope and attribution.

Usage: uv run python examples/indexing_lifecycle.py STORE SCOPE_JSON ATTRIBUTION_JSON
       PRINCIPAL DOCUMENT_ID STATE_VERSION

This initializes the pinned local embedding provider. Prepare an approved model cache
first; HF_HUB_OFFLINE=1 prevents downloads. No canonical full search is exposed.
"""

import argparse
from pathlib import Path

from kg.evidence import EvidenceDatabase
from kg.indexing import IndexService
from kg.models.evidence import LocalIdentity
from kg.models.foundation import Attribution, Scope


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("store", type=Path)
    parser.add_argument("scope", type=Path)
    parser.add_argument("attribution", type=Path)
    parser.add_argument("principal")
    parser.add_argument("document_id")
    parser.add_argument("state_version")
    args = parser.parse_args()
    scope = Scope.model_validate_json(args.scope.read_bytes())
    attribution = Attribution.model_validate_json(args.attribution.read_bytes())
    service = IndexService(EvidenceDatabase(args.store), LocalIdentity(principal_id=args.principal))
    print(
        service.process_explained(
            scope,
            attribution,
            args.document_id,
            args.state_version,
        ).model_dump_json(indent=2)
    )
    print(service.status(scope, args.document_id).model_dump_json(indent=2))


if __name__ == "__main__":
    main()
