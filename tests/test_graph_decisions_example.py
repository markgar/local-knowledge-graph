import json
import subprocess
import sys
from pathlib import Path

from support.graph import require_native


def test_joined_example_displays_both_exact_proof_sides(tmp_path):
    require_native()
    output = tmp_path / "example"
    process = subprocess.run(
        [sys.executable, "examples/graph_relationship_decisions.py", "--output", str(output)],
        cwd=Path(__file__).parents[1], capture_output=True, text=True, timeout=120,
    )
    assert process.returncode == 0, process.stderr
    receipt = json.loads(process.stdout)
    result = receipt["result"]
    assert result["outcome"] == "complete"
    assert result["count"] == 12 and len(result["members"]) == 3 and result["display_truncated"]
    assert not receipt["full_graph_inspection_available"]
    quotes = receipt["separately_authorized_historical_citations"]
    assert {q["kind"] for q in quotes} == {"decision", "relationship"}
    assert all("\r\nCafe\u0301 \U0001f680" in q["citation"]["quote"] for q in quotes)
    proofs = {p["assertion"]["assertion_id"] for p in result["relationships"]}
    assert proofs == {identifier for m in result["members"] for identifier in m["relationship_ids"]}
    assert not tuple((output / "derived").iterdir())
