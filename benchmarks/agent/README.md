# Agent CLI evaluation

This historical E5 evaluation preserves the original natural-BM25 search,
JSON, provenance, and revision contracts independently from retrieval-quality
and answerability gates. Search runs through the private evaluation worker
[`../_lexical_search.py`](../_lexical_search.py), explicitly selecting the
low-level natural lexical service. Other operations still invoke `kg` in a
subprocess. Neither lexical evaluation nor structured/evidence operations
require semantic models or a vector index.

Normal product search uses the full retrieval pipeline; this frozen baseline
is not an alternative supported product interface. Its historical labels and
reviewed expectations are unchanged. Full-pipeline parity validation is recorded
separately under [`../productization/`](../productization/).

The historical evaluator's operation set is (current capability discovery
advertises interface version 2; frozen search still uses its original backend):

| Agent operation | CLI command |
| --- | --- |
| Discover interface capabilities | `kg capabilities --format json` |
| Search for evidence | Internal natural-BM25 evaluation worker (not product `kg search`) |
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

The evaluator creates an isolated temporary corpus and invokes the appropriate
worker or CLI in a subprocess for every operation. Generated results are not committed.

`source-context` is an additive operation covered by the source-context CLI
tests and reviewed corpus acceptance cases, not by the original seven E5
workflows. It returns bounded context from the selected revision and reports
truncation explicitly. The optional `--contextual` source representation is
measured separately through QASPER; E5 continues to use natural BM25.
