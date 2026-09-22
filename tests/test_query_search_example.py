import json
import subprocess
import sys
from pathlib import Path


def test_supplied_document_ranked_walkthrough_is_executable_and_exact(tmp_path):
    text = "release\r\nCafe\u0301 \U0001f680"
    source = tmp_path / "source.txt"
    source.write_bytes(text.encode())
    completed = subprocess.run(
        [sys.executable, "examples/query_search.py", "--database", str(tmp_path / "example.db"),
         "--text-file", str(source), "--query", "release"],
        capture_output=True, text=True, check=True, timeout=30,
        cwd=Path(__file__).resolve().parents[1],
    )
    output = json.loads(completed.stdout)
    assert output["providers"] == "controlled-query-example"
    assert not output["quality_evidence"]
    assert not output["separate_evidence_requests_are_atomic_with_search"]
    assert output["execution"]["result"]["records_examined"] == 5
    assert output["exact_hits"][0]["evidence"]["quote"] == text
