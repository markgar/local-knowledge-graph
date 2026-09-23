# QASPER benchmark

The runner uses the separate Markdown demonstration's `kg.retrieval` components
and SQLite source store. It does not evaluate the canonical service stack or
Ladybug; see [current architecture](../../SPEC.md#architecture-and-data-ownership).

This benchmark measures retrieval and citation integrity over scientific papers
with human-authored questions and supporting evidence.
[QASPER](https://huggingface.co/datasets/allenai/qasper) contains 5,049 questions
over 1,585 NLP papers and is distributed under
[CC BY 4.0](https://creativecommons.org/licenses/by/4.0/).
The repository contains conversion/evaluation tools, a fixed
[50-paper development selection](sample.json), and aggregate results, not the papers.
The selected sample has 179 questions.

Each paper becomes one Markdown document with its title as a seed entity.
Search is scoped to that title, so this tests within-paper retrieval, not paper
discovery. Paragraph evidence maps to source anchors, section evidence to
headings, and `FLOAT SELECTED` evidence to figure/table captions. Preparation
rejects selected gold evidence that cannot be represented in the generated corpus.

## Prepare and run lexical retrieval

Run from the repository root with the
[development environment](../../CONTRIBUTING.md) available:

```bash
uv run python benchmarks/qasper/prepare.py
uv run kg ingest --manifest benchmarks/qasper/data/corpus.yml
uv run python benchmarks/qasper/evaluate.py \
  --strategy natural --output benchmarks/qasper/data/results-natural-run-1.json
uv run python benchmarks/qasper/evaluate.py \
  --strategy strict --output benchmarks/qasper/data/results-strict-run-1.json
```

`prepare.py --output DIRECTORY` changes the generated directory (default:
`benchmarks/qasper/data/`). It reuses a checksum-valid archive already there,
otherwise downloads the pinned archive:

```text
URL: https://qasper-dataset.s3.us-west-2.amazonaws.com/qasper-train-dev-v0.3.tgz
SHA-256: a28fdf966db827bcee3d873107d6b6669864fb7ca8fbf73a192f5e39191bdb5a
```

Preparation manages generated Markdown using `.qasper-fixture.json` and refuses
unexpected Markdown rather than deleting unrelated files. Repreparing a managed
directory can replace generated sources, manifest, and gold; do not do so during
a comparison. Papers, gold, the archive, indexes, and detailed results belong in
the Git-ignored `data/` directory or another untracked workspace. If preparation
uses a different directory, pass its manifest to `kg` and both `--manifest` and
`--gold` to the evaluator.

Respect organizational network policy for dataset, package, and model downloads.
Report blocked hosts; use checksum-valid local archives and approved caches
where available. Do not bypass controls or change model pins to complete a run.

## Semantic component strategies

`--strategy` is an **evaluation-only component selector**, not a public
`kg search` mode:

| Strategy | Evaluated retrieval |
| --- | --- |
| `strict` | All-term lexical matching |
| `natural` (default) | OR-term lexical matching ranked by BM25 |
| `dense` | Dense retrieval with the selected embedding profile |
| `hybrid` | Lexical/dense candidate fusion |
| `reranked` | Fused candidates scored by the local cross-encoder |

Strict/natural need no models or vector index. For the other strategies, build a
projection matching the embedding profile and contextual setting:

```bash
uv run kg dense-index --manifest benchmarks/qasper/data/corpus.yml \
  --embedding-profile gte-modernbert
uv run python benchmarks/qasper/evaluate.py \
  --strategy dense --embedding-profile gte-modernbert \
  --output benchmarks/qasper/data/results-dense-gte-run-1.json
uv run python benchmarks/qasper/evaluate.py \
  --strategy hybrid --embedding-profile gte-modernbert \
  --output benchmarks/qasper/data/results-hybrid-gte-run-1.json
uv run python benchmarks/qasper/evaluate.py \
  --strategy reranked --embedding-profile gte-modernbert \
  --output benchmarks/qasper/data/results-reranked-gte-run-1.json
```

`--embedding-profile` defaults to `gte-modernbert`; the other supported value is
`qwen3-embedding-0.6b`. Repeat indexing and evaluation with that value and distinct
output names to compare profiles. Model warmup occurs before query timing.
Normal product search always uses the full pipeline; these measurements remain
component-specific. [Productization parity](../productization/README.md) is a
separate integration check, not new relevance gold.

### Contextual representation

Add `--contextual` to **both** indexing and semantic evaluation to supply titles
and heading paths to embedding/reranking without changing evidence quotes:

```bash
uv run kg dense-index --manifest benchmarks/qasper/data/corpus.yml \
  --embedding-profile gte-modernbert --contextual
uv run python benchmarks/qasper/evaluate.py \
  --strategy reranked --embedding-profile gte-modernbert --contextual \
  --output benchmarks/qasper/data/results-reranked-gte-contextual-run-1.json
```

Contextual and non-contextual projections coexist. Strict/natural reject
`--contextual`. `kg source-context` is a separate evidence-reading operation;
expanded passages do not count toward this benchmark's top-k recall.

## Evaluator options and outputs

`--manifest` and `--gold` default to `data/corpus.yml` and `data/gold.json`
relative to this directory. `--limit` defaults to 10 and must be at least 10.
The evaluator prints aggregate metrics and writes a JSON report including
per-question IDs, result IDs, top scores, strategy, profile, and contextual setting.
Without `--output`, the filename is `data/results-STRATEGY.json`, with
`-qwen3-embedding-0.6b` for that profile's semantic strategies and `-contextual`
when enabled. Existing report files are overwritten. Use distinct output paths
for repetitions; retain configuration, code/model versions, and index costs
alongside reports.

Metrics include evidence recall at 1/5/10, first-gold reciprocal rank, evidence-set
F1@10, false-evidence rate on questions without evidence, anchor/revision
round-trip integrity, and median/p95 query latency. Evidence matching normalizes
whitespace and heading markers; multiple annotations are scored against the best
matching gold set. Recall/MRR/F1 aggregate over questions with evidence.
This is not generated-answer evaluation or a test of graph traversal, actions,
decisions, blockers, or conflicts.

## Answerability calibration

Calibration consumes the evaluator's **reranked raw-score report**, not product
search output, and does not run retrieval or generate answers:

```bash
uv run python benchmarks/qasper/calibrate.py \
  --input benchmarks/qasper/data/results-reranked-gte-run-1.json \
  --output benchmarks/qasper/data/results-selective-gte-run-1.json
```

Always supply matching input/output paths: the calibrator's default input is
`data/results-reranked-gte.json`, unlike the evaluator's default GTE filename.
`--folds` defaults to 5 and `--seed` to `local-knowledge-graph-qasper-e4-v1`.
Whole papers are deterministically assigned to folds; thresholds maximize
balanced answerability accuracy on the other folds. The full-sample
`deployment_threshold` is separate from out-of-fold selective metrics.

## Quality limits and retained evidence

[Baseline measurements](baseline.json) and [experiment results](RESULTS.md)
are immutable comparison evidence. Publish new runs separately rather than
relabeling or replacing them. The recorded contextual reranking results retain
100% anchor integrity but miss the reranked quality gates; top-score calibration
does not pass the answerability gate. Accurate citations are not proof of
relevance or calibrated abstention.

| Retrieval target | Recall@5 | Recall@10 | MRR |
| --- | ---: | ---: | ---: |
| Dense/hybrid | ≥60% | ≥75% | ≥0.45 |
| Reranked | ≥70% | ≥80% | ≥0.55 |

All comparisons require 100% anchor integrity. Repeat full comparisons and
report latency, indexing time, and index size before claiming improvement.
These are quality targets, not implementation prerequisites.

QASPER citation: Dasigi et al. (2021), *A Dataset of Information-Seeking Questions
and Answers Anchored in Research Papers*, NAACL. See the
[project repository](https://github.com/allenai/qasper-led-baseline) for the paper,
official evaluator, and baseline implementations.
