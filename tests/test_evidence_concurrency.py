from __future__ import annotations

import multiprocessing
import sqlite3
from pathlib import Path

import pytest
from support.evidence import environment, put, receipt

from kg.evidence import (
    EvidenceAdministration,
    EvidenceDatabase,
    EvidenceService,
    EvidenceServiceError,
)
from kg.models.evidence import LocalAdminAuthority, LocalIdentity, LocalPolicy
from kg.models.foundation import WriteRequest


def _write_worker(path, request_json, barrier, results):
    service = EvidenceService(EvidenceDatabase(Path(path)), LocalIdentity(principal_id="principal"))
    request = WriteRequest.model_validate_json(request_json)
    barrier.wait(timeout=20)
    outcome = service.write(request)
    results.put((outcome.status, outcome.error.code if outcome.error else None))


def _revoke_worker(path, version, barrier, results):
    admin = EvidenceAdministration(
        EvidenceDatabase(Path(path)),
        LocalAdminAuthority(principal_id="admin"),
    )
    barrier.wait(timeout=20)
    admin.replace_policy(LocalPolicy(corpus_id="work"), version)
    results.put(("policy", None))


@pytest.mark.parametrize("revocation", [False, True])
def test_separate_process_cas_and_policy_linearization(tmp_path: Path, revocation: bool) -> None:
    env = environment(tmp_path / "e.db")
    first = receipt(env.service.write(put(env.scope)))
    ctx = multiprocessing.get_context("spawn")
    barrier, results = ctx.Barrier(3), ctx.Queue()
    request = put(env.scope, state=first.processing.state_version, title="writer")
    processes = [
        ctx.Process(
            target=_write_worker,
            args=(
                str(env.database.path),
                request.model_dump_json(),
                barrier,
                results,
            ),
        )
    ]
    if revocation:
        processes.append(
            ctx.Process(
                target=_revoke_worker,
                args=(
                    str(env.database.path),
                    env.scope.access.policy_version,
                    barrier,
                    results,
                ),
            )
        )
    else:
        second = put(env.scope, state=first.processing.state_version, title="competitor")
        processes.append(
            ctx.Process(
                target=_write_worker,
                args=(
                    str(env.database.path),
                    second.model_dump_json(),
                    barrier,
                    results,
                ),
            )
        )
    for process in processes:
        process.start()
    barrier.wait(timeout=20)
    outcomes = [results.get(timeout=30) for _ in processes]
    for process in processes:
        process.join(30)
        assert process.exitcode == 0
    if revocation:
        assert ("policy", None) in outcomes
        assert ("applied", None) in outcomes or ("rejected", "forbidden") in outcomes
        assert env.service.write(request).error.code == "forbidden"
    else:
        assert sorted(outcomes) == [("applied", None), ("conflict", "state_conflict")]
        assert len(env.service.history(env.scope, first.document_id).entries) == 2


def _initializer(path, kind, pause_at_schema, entered, release, result):
    import kg.db as legacy
    import kg.evidence.database as evidence
    import kg.retrieval.dense as dense

    module = {"evidence": evidence, "legacy": legacy, "projection": dense}[kind]
    if pause_at_schema:
        original = module.execute_schema

        def ddl(connection, sql):
            assert connection.in_transaction
            entered.set()
            assert release.wait(20)
            original(connection, sql)

        module.execute_schema = ddl
    else:
        name = "_check" if kind == "evidence" else "legacy_format"
        original_check = getattr(module, name)
        first = True

        def check(connection, **kwargs):
            nonlocal first
            value = original_check(connection, **kwargs)
            if first:
                first = False
                entered.set()
            return value

        setattr(module, name, check)
    try:
        if kind == "evidence":
            EvidenceDatabase(Path(path)).initialize()
        elif kind == "legacy":
            legacy.Database(Path(path)).initialize()
        else:
            connection = sqlite3.connect(path)
            try:
                connection.row_factory = sqlite3.Row
                # The independent path's admission check precedes locked initialization.
                dense.legacy_format(connection, projection=True)
                dense._initialize_projection_schema(connection)
            finally:
                connection.close()
        result.put("ok")
    except EvidenceServiceError as error:
        result.put("incompatible" if error.failure.code == "unsupported" else error.failure.code)
    except sqlite3.DatabaseError:
        result.put("incompatible")


@pytest.mark.parametrize(
    "winner,loser",
    [
        ("evidence", "evidence"),
        ("evidence", "legacy"),
        ("legacy", "evidence"),
        ("evidence", "projection"),
        ("projection", "evidence"),
    ],
)
def test_initializers_contend_after_empty_admission(
    tmp_path: Path, winner: str, loser: str
) -> None:
    path = tmp_path / "race.db"
    ctx = multiprocessing.get_context("spawn")
    entered, admitted, release = ctx.Event(), ctx.Event(), ctx.Event()
    results = ctx.Queue()
    first = ctx.Process(
        target=_initializer,
        args=(
            str(path),
            winner,
            True,
            entered,
            release,
            results,
        ),
    )
    second = ctx.Process(
        target=_initializer,
        args=(
            str(path),
            loser,
            False,
            admitted,
            release,
            results,
        ),
    )
    first.start()
    assert entered.wait(20)
    second.start()
    try:
        assert admitted.wait(20)
    finally:
        release.set()
    outcomes = [results.get(timeout=30), results.get(timeout=30)]
    for process in (first, second):
        process.join(30)
        assert process.exitcode == 0
    assert sorted(outcomes) == (["ok", "ok"] if winner == loser else ["incompatible", "ok"])
    with sqlite3.connect(path) as connection:
        application = connection.execute("PRAGMA application_id").fetchone()[0]
        names = {row[0] for row in connection.execute("SELECT name FROM sqlite_master")}
        if winner == "evidence":
            assert application == 0x4B474531
            assert connection.execute("PRAGMA user_version").fetchone()[0] == 4
            assert "revision" in names and "dense_projection" not in names
            assert "source_document" not in names
        else:
            assert application == 0
            assert "store_format" not in names and "revision" not in names
