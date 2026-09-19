# Agent CLI evaluation

E5 treats the existing `kg` executable and its JSON output as the agent tool
boundary. No agent SDK or MCP dependency is required. A future protocol adapter
can call the same reusable retrieval services without changing the contracts.
The reviewed workflows use natural BM25 search so this benchmark measures the
agent-facing command, JSON, provenance, and revision contracts independently
from the E3 retrieval-quality and E4 answerability gates.

The stable command set is:

| Agent operation | CLI command |
| --- | --- |
| Discover interface capabilities | `kg capabilities --format json` |
| Search for evidence | `kg search ... --format json` |
| Read an exact source range | `kg source-range <anchor-id> ... --format json` |
| Read surrounding anchored context | `kg source-context <anchor-id> ... --format json` |
| List source revisions | `kg revisions <source-path> ... --format json` |
| Compare revisions | `kg compare-revisions <source-path> ... --format json` |
| Resolve a citation | `kg evidence <record-id> ... --format json` |
| Retrieve structured status | `kg status <subject> ... --format json` |

`tasks.json` contains reviewed workflows for paraphrased search,
multi-document synthesis, explicit conflicts, ambiguous cross-source results,
unsupported subjects, citation round-trips, and revision comparison.

Run the evaluation from the repository root:

```bash
uv run python benchmarks/agent/evaluate.py \
  --output benchmarks/agent/results.json
```

The evaluator creates an isolated temporary corpus and invokes the CLI in a
subprocess for every operation. Generated results are not committed.

`source-context` is an additive operation covered by the source-context CLI
tests and reviewed corpus acceptance cases, not by the original seven E5
workflows. It returns bounded context from the selected revision and reports
truncation explicitly. The optional `--contextual` semantic search mode is
measured separately through QASPER; E5 continues to use natural BM25.
