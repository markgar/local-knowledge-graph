"""Public withdrawal requests over real producer IDs."""

from uuid import uuid4

from kg.models.foundation import Attribution, WithdrawAssertion, WriteRequest


def withdrawal(env, contribution_id, *, retry=None, scope=None, owner="owner", writer="writer"):
    return WriteRequest(
        contract_version="foundation/1",
        request_id=str(uuid4()),
        retry_key=retry or str(uuid4()),
        scope=scope or env.scope,
        attribution=Attribution(
            owner_id=owner, writer_id=writer, producer="withdrawal-tests", producer_version="1",
        ),
        payload=WithdrawAssertion(operation="withdraw_assertion", contribution_id=contribution_id),
    )
