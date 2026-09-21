"""Execute one foundation plan against an already provisioned canonical store.

Usage: uv run python examples/query_anchor.py STORE REQUEST_JSON PRINCIPAL
The request must select an anchor evidence step; other required operations are
unsupported. Provision trusted identity/policy separately through kg.evidence.
"""

import argparse
from pathlib import Path

from kg.evidence import EvidenceDatabase
from kg.models.evidence import LocalIdentity
from kg.models.foundation import QueryRequest
from kg.query import QueryService


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("store", type=Path)
    parser.add_argument("request", type=Path)
    parser.add_argument("principal")
    args = parser.parse_args()
    request = QueryRequest.model_validate_json(args.request.read_bytes())
    with QueryService(
        EvidenceDatabase(args.store),
        LocalIdentity(principal_id=args.principal),
    ) as service:
        print(service.execute_explained(request).model_dump_json(indent=2))


if __name__ == "__main__":
    # Spawned workers import the entry module: never start a query during import.
    main()
