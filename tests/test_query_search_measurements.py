"""Check recorded observations/labels, never turn latency into a routine-test gate."""

import json
import math
from pathlib import Path

import pytest


@pytest.mark.unit
@pytest.mark.parametrize("filename", [
    "q1-controlled-composition.json", "q1-controlled-composition-reviewed.json",
])
def test_controlled_record_preserves_samples_and_redacted_accounting(filename):
    record = json.loads(
        (Path("benchmarks/foundation") / filename).read_text(),
    )
    assert record["record_version"] == "q1-controlled-composition/1"
    assert record["recorded_at_utc"] and record["git_head"] and record["source_sha256"]
    assert not record["providers"]["real_models_loaded"]
    assert not record["providers"]["warm_model_measurement"]
    assert "controlled-test" in record["providers"]["observed_embedding"]["pipeline"]
    assert record["gates"]["e4_checkpoint_invalidation"] == "not available; #72"
    for row in record["samples"]:
        if row["work_accounting"] == "redacted":
            assert row["semantic_reservations"] is row["operations"] is None
    for scenario, summary in record["by_scenario"].items():
        rows = [r for r in record["samples"] if r["scenario"] == scenario]
        assert summary["samples"] == len(rows)
        assert summary["p95_ms"] == sorted(r["latency_ms"] for r in rows)[
            math.ceil(len(rows) * 0.95) - 1
        ]
        for flag in ("timeout", "partial", "invalidated"):
            assert summary[f"{flag}_rate"] == sum(row[flag] for row in rows) / len(rows)
