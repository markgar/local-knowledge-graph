"""Q1 event values; only a successful owning disclosure may release them."""

from typing import Literal

from pydantic import Field

from kg.models.execution_events import SafeReason
from kg.models.foundation import Token, Value


class QueryPlan(Value):
    kind: Literal["query.plan_closure"] = "query.plan_closure"
    required_steps: tuple[Token, ...] = Field(max_length=32)
    pruned_steps: tuple[Token, ...] = Field(max_length=32)


class QueryDecision(Value):
    kind: Literal[
        "query.semantics_selected",
        "query.resolution",
        "query.selection",
        "query.output",
    ]
    step_id: Token
    state: Literal[
        "supported",
        "empty",
        "unique",
        "ambiguous",
        "partial",
        "exhausted",
        "exact",
        "lower_bound",
        "displayed",
        "truncated",
    ]
    semantics: (
        Literal[
            "direct-subject-decision/1",
            "record_instances",
            "entity",
            "evidence",
            "search",
        ]
        | None
    ) = None
    support_set_id: Token | None = None
    selected_ids: tuple[Token, ...] = Field(default=(), max_length=200)


class QueryBudget(Value):
    kind: Literal["query.budget"] = "query.budget"
    step_id: Token
    stage: Literal[
        "resolve",
        "records",
        "evidence_reference",
        "passage",
        "lexical",
        "dense",
        "rerank",
        "final_evidence",
        "support",
    ]
    reservations: int = Field(ge=0)
    work_accounting: Literal["scoped_semantic_reservations/1"] = "scoped_semantic_reservations/1"
    stop: SafeReason | None = None


class QueryNested(Value):
    kind: Literal["query.nested_execution"] = "query.nested_execution"
    step_id: Token
    execution_id: Token
    report_id: Token | None = None


QueryEvent = QueryPlan | QueryDecision | QueryBudget | QueryNested
