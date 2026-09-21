from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path

import pytest
from support.evidence import environment, put

from kg.evidence import _receipts, _store, _writer, database
from kg.models.foundation import WriteBatch


@pytest.mark.parametrize("stage", ["content", "state", "receipt", "ledger", "commit"])
def test_failure_boundaries_leave_no_unit_rows(tmp_path: Path, monkeypatch, stage: str) -> None:
    env = environment(tmp_path / "e.db")
    if stage == "commit":
        transaction = database.write_transaction

        @contextmanager
        def fail_commit(connection):
            with transaction(connection):
                yield
                raise sqlite3.OperationalError("injected before commit")

        monkeypatch.setattr(database, "write_transaction", fail_commit)
    else:
        module, name = {
            "content": (_writer, "_content"),
            "state": (_store, "add_state"),
            "receipt": (_store, "receipt"),
            "ledger": (_receipts, "save"),
        }[stage]
        original = getattr(module, name)

        def fail_after(*args, **kwargs):
            original(*args, **kwargs)
            raise sqlite3.OperationalError("injected after stage")

        monkeypatch.setattr(module, name, fail_after)
    result = env.service.write(put(env.scope))
    assert result.status == "failed" and result.error.code == "internal_error"
    with env.database.connection() as connection:
        for table in (
            "document",
            "revision",
            "anchor",
            "anchor_set",
            "anchor_set_member",
            "document_state",
            "metadata_snapshot",
            "processing_state",
            "write_key",
            "write_response",
            "write_provenance",
            "receipt_clock",
            "state_intent",
            "sync_scope",
        ):
            assert connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0


def test_batch_interruption_replays_only_committed_units(tmp_path: Path, monkeypatch) -> None:
    env = environment(tmp_path / "e.db")
    batch = WriteBatch(
        contract_version="foundation/1",
        batch_id="batch",
        items=(
            put(env.scope, external="first"),
            put(env.scope, external="second"),
        ),
    )
    write = env.service.write
    calls = 0

    def interrupted(request):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("client stopped before second unit")
        return write(request)

    with monkeypatch.context() as patch:
        patch.setattr(env.service, "write", interrupted)
        with pytest.raises(RuntimeError):
            env.service.write_batch(batch)
    assert env.service.write_batch(batch).status == "complete"
    with env.database.connection() as connection:
        for table in ("document", "document_state", "write_key", "write_provenance"):
            assert connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 2
