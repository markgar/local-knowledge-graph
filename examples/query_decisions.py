"""Count a registered subject's submitted decisions and inspect every retained member.

Usage: uv run python examples/query_decisions.py STORE REQUEST_JSON PRINCIPAL
REQUEST_JSON is a foundation/1 resolve -> records(decision) -> count plan over
actual EvidenceService enrichment. No model, search, or inferred event count.
The same service must stay open for inspection; any canonical write invalidates
its support set, and sets expire after five minutes.
"""

import argparse
from pathlib import Path

from kg.evidence import EvidenceDatabase
from kg.models.evidence import LocalIdentity
from kg.models.foundation import AggregateResult, QueryRequest
from kg.models.query import SupportInspectionRequest
from kg.query import QueryService


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("store", type=Path)
    parser.add_argument("request", type=Path)
    parser.add_argument("principal")
    args = parser.parse_args()
    request = QueryRequest.model_validate_json(args.request.read_bytes())
    with QueryService(
        EvidenceDatabase(args.store), LocalIdentity(principal_id=args.principal)
    ) as service:
        execution = service.execute(request)
        print(execution.model_dump_json())
        result = execution.result
        if not isinstance(result.data, AggregateResult) or result.result_set_id is None:
            raise SystemExit("No count released; inspect the typed outcome above.")
        start = 0
        while True:
            page = service.inspect_support(
                SupportInspectionRequest(
                    request_id=f"{request.request_id[:100]}/support/{start}",
                    scope=request.scope,
                    result_set_id=result.result_set_id,
                    records_step_id=result.data.supporting_records_step,
                    start_ordinal=start,
                    budget=request.budget,
                )
            )
            print(page.model_dump_json())
            if page.error is not None:
                raise SystemExit("Support unavailable; no automatic query retry was performed.")
            if page.next_ordinal is None:
                break
            start = page.next_ordinal


if __name__ == "__main__":
    main()
