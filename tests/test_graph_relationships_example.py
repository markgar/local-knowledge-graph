
import json
import subprocess
import sys
from pathlib import Path

import pytest
from support.graph import require_native


@pytest.mark.functional
@pytest.mark.requires_native
def test_cited_query_example_uses_native_and_preserves_exact_quote(tmp_path):
    require_native()
    output = tmp_path / "example"
    process = subprocess.run(
        [sys.executable, "examples/graph_relationships.py", "--output", str(output)],
        cwd=Path(__file__).parents[1], capture_output=True, text=True, timeout=120,
    )
    assert process.returncode == 0, process.stderr
    receipt = json.loads(process.stdout)
    assert receipt["capabilities"]["runtime"] == "available"
    result = receipt["result"]
    assert result["outcome"] == "complete" and len(result["paths"]) == 1
    assert result["paths"][0]["path"]["entity_ids"][1] == receipt["expected_project"]
    citation = receipt["historical_citations"][0]
    assert citation["quote"] == "Review note\r\nAlice owns Project Atlas. Cafe\u0301 \U0001f680."
    assert citation["end"] - citation["start"] == len(citation["quote"])
    assert not tuple((output / "derived").iterdir())


@pytest.mark.functional
def test_graph_value_import_does_not_load_native_runtime():
    process = subprocess.run(
        [sys.executable, "-c",
         "import sys; from kg.models.graph import GraphTraversalRequest; "
         "from kg.graph import LocalGraphSession; assert 'ladybug' not in sys.modules"],
        text=True, capture_output=True, timeout=30,
    )
    assert process.returncode == 0, process.stderr
