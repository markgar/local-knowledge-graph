import json
import sqlite3
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import ValidationError
from support.query_knowledge import produce, setup
from support.withdrawal import withdrawal

from kg._sqlite import EVIDENCE_APPLICATION_ID
from kg.evidence import EvidenceDatabase, EvidenceServiceError
from kg.evidence._format import FORMAT_GUIDANCE, MANIFEST_VERSION, _catalog, _manifest
from kg.knowledge import KnowledgeService
from kg.models.foundation import BatchResult, FoundationCapabilities, WriteBatch, WriteRequest


def test_exact_old_format_rejected_without_repair_with_actionable_message(tmp_path, caplog):
    path = tmp_path / "old.sqlite"
    # Exact prior schema is negative input, never a supported runtime compatibility branch.
    old_sql = Path(__file__).with_name("support").joinpath("evidence-store-2.sql").read_text()
    with sqlite3.connect(path) as c:
        c.executescript(old_sql)
        expected = _manifest(c, _catalog(c))
        c.executemany(
            "INSERT INTO schema_object_manifest VALUES (?,?,?,?)", expected.objects,
        )
        c.execute("INSERT INTO store_format VALUES (1,?,?,?)", (
            "evidence-store/2", MANIFEST_VERSION, expected.signature,
        ))
        c.execute(f"PRAGMA application_id={EVIDENCE_APPLICATION_ID}")
        c.execute("PRAGMA user_version=2")
    before = path.read_bytes()
    database = EvidenceDatabase(path)
    for action in (database.initialize, lambda: database.connection().__enter__()):
        with pytest.raises(EvidenceServiceError) as failure:
            action()
        assert failure.value.failure.code == "unsupported"
        assert FORMAT_GUIDANCE in str(failure.value)
        assert failure.value.failure.diagnostic_id in caplog.text
        assert path.read_bytes() == before
    assert "reload" in caplog.text


def test_withdrawal_capabilities_shape_grants_and_batch_correlation(tmp_path):
    env = setup(tmp_path / "shape.sqlite")
    _, ids = produce(env, 1)
    req = withdrawal(env, next(iter(ids)))
    assert WriteRequest.model_validate_json(req.model_dump_json()) == req
    assert "withdraw_assertion" in env.service.capabilities().operations
    assert "withdraw_assertion" in FoundationCapabilities(
        contract_version="foundation/1",
    ).write_operations
    capabilities = KnowledgeService(env.database, env.service.identity).capabilities(env.scope)
    assert capabilities.withdrawal == "owned_assertion"
    assert "retraction" in capabilities.unsupported
    for missing in ("read", "write_knowledge"):
        raw = req.model_dump(mode="json")
        raw["scope"]["access"]["grants"].remove(missing)
        with pytest.raises(ValidationError):
            WriteRequest.model_validate(raw)
    for extra in ("reason", "expected_state", "replacement"):
        raw = req.model_dump(mode="json")
        raw["payload"][extra] = "not supported"
        with pytest.raises(ValidationError):
            WriteRequest.model_validate(raw)
    outcome = env.service.write(req)
    batch = WriteBatch(contract_version="foundation/1", batch_id="batch", items=(req,))
    result = BatchResult(
        contract_version="foundation/1", batch_id="batch", status="complete", outcomes=(outcome,),
    )
    result.validate_for(batch)
    tampered = outcome.model_copy(update={
        "receipt": outcome.receipt.model_copy(update={"contribution_id": str(uuid4())}),
    })
    with pytest.raises(ValueError, match="exact contribution"):
        result.model_copy(update={"outcomes": (tampered,)}).validate_for(batch)


@pytest.mark.parametrize("graph", [False, True])
def test_actual_example_and_refusal_to_overwrite(tmp_path, graph):
    if graph:
        from support.graph import require_native
        require_native()
    path = tmp_path / "example.sqlite"
    command = [sys.executable, "examples/withdraw_assertion.py", "--database", str(path)]
    if graph:
        command.append("--graph")
    result = subprocess.run(command, text=True, capture_output=True, check=True)
    value = json.loads(result.stdout)
    assert (value["before"], value["after"], value["after_correction"]) == (2, 1, 2)
    assert value["history"]["withdrawal"]["withdrawal_id"] == (
        value["withdrawal"]["receipt"]["withdrawal_id"]
    )
    assert value["quote"] and not value["history"]["is_current"]
    assert value["graph_refresh"] == ("ready" if graph else "not_requested")
    before = path.read_bytes()
    repeated = subprocess.run(command, text=True, capture_output=True)
    assert repeated.returncode != 0 and "never resets" in repeated.stderr
    assert path.read_bytes() == before
