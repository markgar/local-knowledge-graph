
import json
import subprocess
import sys
from pathlib import Path

import pytest
from support.graph import require_native


@pytest.mark.functional
@pytest.mark.requires_native
def test_graph_session_example(tmp_path):
    require_native()
    output = tmp_path / "demo"
    result = subprocess.run(
        [sys.executable, "examples/graph_session.py", "--output", str(output)],
        cwd=Path(__file__).parents[1], text=True, capture_output=True, timeout=120,
    )
    assert result.returncode == 0, result.stderr
    receipt = json.loads((output / "receipt.json").read_text())
    assert receipt["native_buffer_bytes"] == 256 << 20
    assert receipt["saved_receipt_after_graph_failure"] and receipt["restart_untrusted"]
    assert receipt["refreshed_assertions"] == 24
    assert receipt["phase_seconds"]["cold_build_read"] > 0
    assert not receipt["close"]["cleanup_pending"]
    assert not list((output / "derived").iterdir())
