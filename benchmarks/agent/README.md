# Agent CLI evaluation

This harness targets the separate [Markdown demonstration](../../README.md#markdown-demonstration)
and its SQLite-backed CLI, not the canonical evidence-service or Ladybug APIs.

This scripted evaluation checks seven reviewed JSON, provenance, status, and
revision workflows. It does **not** launch an agent or measure generated-answer
quality. For an agent-answer comparison, use [work-memory](../work_memory/README.md).

Search explicitly uses natural BM25 through the private
[`_lexical_search.py`](../_lexical_search.py) worker. Other operations invoke
`kg` in subprocesses. Neither requires semantic models or a vector index.
These are component/contract checks, not an alternative product search
interface: public `kg search` uses the full lexical/dense/fusion/reranking
pipeline. See [productization validation](../productization/README.md) for parity.

## Run

From the repository root, with the [development environment](../../CONTRIBUTING.md)
available:

```bash
uv run python benchmarks/agent/evaluate.py \
  --output .kg/agent/results-run-1.json
```

`--tasks PATH` selects a task file (default: [`tasks.json`](tasks.json)).
The evaluator copies the research-vault fixture into an isolated temporary
workspace, adds a conflict document, ingests it, and executes the workflows.
The revision workflow edits only that copy. The workspace is removed afterward.
The complete report is always printed; `--output` also writes it, creating parent
directories. Existing output files are overwritten, so use a distinct path for
each retained run. Inspect `passed`, `pass_rate`, and individual `task_results`;
failed expectations are recorded in JSON, not converted into a nonzero exit.

## Coverage and limits

[`tasks.json`](tasks.json) covers paraphrased lexical search, multi-document
status, explicit conflicts, ambiguous cross-source matches, unsupported subjects,
citation round-trips, and revision comparison. Keep its reviewed expectations
and the `agent-cli-e5` report label unchanged when comparing runs.

Citation checks use `kg evidence` and `kg source-range`; revision checks reingest
an edited source and use `kg compare-revisions`. Capability discovery,
`kg revisions`, and `kg source-context` have separate CLI tests; they are not workflows
in this task file. Source context reads bounded passages from the selected
revision and reports truncation. Contextual embedding/reranking is a separate
[QASPER experiment](../qasper/README.md), not part of this lexical evaluation.
