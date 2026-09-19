# QASPER benchmark

This benchmark tests the local knowledge graph against real scientific papers
with human-authored questions and paragraph-level supporting evidence.
[QASPER](https://huggingface.co/datasets/allenai/qasper) contains 5,049
questions over 1,585 NLP papers and is distributed under
[CC BY 4.0](https://creativecommons.org/licenses/by/4.0/).

The repository does not redistribute the papers. It contains only conversion
and evaluation tooling, a deterministic list of 50 development-paper IDs, and
aggregate results. Running the preparation command downloads QASPER 0.3 from
the official archive and verifies its SHA-256 checksum.

## Plan

The first pilot uses 50 complete development papers and 179 questions:

- 32 unanswerable annotations.
- 25 yes/no annotations.
- 73 free-form annotations.
- 182 extractive annotations.
- 69 questions with at least one multi-paragraph evidence annotation.

Each paper becomes one Markdown document. Its title is configured as a seed
entity so evaluation searches remain scoped to the paper associated with the
question. Paragraph evidence becomes exact source anchors, section evidence
maps to Markdown headings, and QASPER's `FLOAT SELECTED` evidence maps to
figure and table caption passages. Preparation fails if any selected gold
evidence cannot be represented by the generated corpus.

The initial measurements are:

- Paragraph evidence recall at 1, 5, and 10.
- Mean reciprocal rank of the first gold paragraph.
- Evidence-set F1 over the first 10 results.
- False-evidence rate for unanswerable questions.
- Exact anchor and revision integrity.
- Median and p95 query latency.

This benchmark evaluates retrieval, citations, and abstention. It does not
evaluate generated answer quality, actions, decisions, blockers, conflicts, or
graph traversal. The synthetic acceptance corpora remain the tests for those
explicit structures. A later 2WikiMultiHopQA benchmark can focus specifically
on one- and two-hop relationships.

## Initial baseline

The first run produced:

| Metric | Strict search | Natural search |
| --- | ---: | ---: |
| Evidence Recall@1 | 0.0% | 12.2% |
| Evidence Recall@5 | 0.0% | 40.2% |
| Evidence Recall@10 | 0.0% | 54.6% |
| Mean reciprocal rank | 0.000 | 0.302 |
| Evidence-set F1@10 | 0.0% | 12.8% |
| Unanswerable false-evidence rate | 0.0% | 100.0% |
| Anchor integrity | 100.0% | 100.0% |

Strict search returned no passage for 175 of 179 questions because natural
questions contain words that do not all occur in one paragraph. The engine's
natural query mode safely joins unique terms with FTS5 `OR` and lets BM25 rank
the candidates. This is a public search feature rather than benchmark-specific
query rewriting.

The low top-rank score and false-evidence rate show that ranking and calibrated
abstention need more work. These are retrieval failures, not citation failures:
every returned result resolved back to the exact indexed quote and immutable
revision.

The aggregate measurements are checked in as
[`baseline.json`](baseline.json). Detailed per-question output remains local
because it includes dataset-derived identifiers and is more useful as a
working artifact than as source code.

The permanent experiment history and one-feature-at-a-time methodology are in
[`RESULTS.md`](RESULTS.md). New retrieval capabilities must record their
metrics and delta there before the next capability is introduced.

## Run it

From the repository root:

```bash
uv sync --extra dev
uv run python benchmarks/qasper/prepare.py
uv run kg ingest --manifest benchmarks/qasper/data/corpus.yml
uv run kg dense-index --manifest benchmarks/qasper/data/corpus.yml --embedding-profile gte-modernbert
uv run kg dense-index --manifest benchmarks/qasper/data/corpus.yml --embedding-profile qwen3-embedding-0.6b
uv run python benchmarks/qasper/evaluate.py --strategy dense --embedding-profile gte-modernbert --output benchmarks/qasper/data/results-dense-gte.json
uv run python benchmarks/qasper/evaluate.py --strategy dense --embedding-profile qwen3-embedding-0.6b --output benchmarks/qasper/data/results-dense-qwen.json
uv run python benchmarks/qasper/evaluate.py --strategy hybrid --embedding-profile gte-modernbert --output benchmarks/qasper/data/results-hybrid-gte.json
uv run python benchmarks/qasper/evaluate.py --strategy hybrid --embedding-profile qwen3-embedding-0.6b --output benchmarks/qasper/data/results-hybrid-qwen.json
uv run python benchmarks/qasper/evaluate.py --strategy reranked --embedding-profile gte-modernbert --output benchmarks/qasper/data/results-reranked-gte.json
uv run python benchmarks/qasper/evaluate.py --strategy reranked --embedding-profile qwen3-embedding-0.6b --output benchmarks/qasper/data/results-reranked-qwen.json
uv run python benchmarks/qasper/calibrate.py --input benchmarks/qasper/data/results-reranked-gte.json --output benchmarks/qasper/data/results-selective-gte.json
uv run python benchmarks/qasper/calibrate.py --input benchmarks/qasper/data/results-reranked-qwen.json --output benchmarks/qasper/data/results-selective-qwen.json
uv run python benchmarks/qasper/evaluate.py
```

Generated papers, gold annotations, the SQLite database, and detailed results
are written beneath `benchmarks/qasper/data/`, which Git ignores.
The environment-specific `uv.lock` is also ignored because the configured
package feed may differ between development environments.

The evaluator uses natural search and the GTE profile by default. Run it with
`--strategy strict` to reproduce the original all-term baseline, or use
`--strategy dense`, `--strategy hybrid`, or `--strategy reranked` after
building the selected profile projection:

```bash
uv run python benchmarks/qasper/evaluate.py --strategy strict
```

This baseline is intentionally not presented as a solved benchmark. It creates
a stable, public regression target for better lexical ranking and abstention
without adding embeddings or answer generation.

## Improvement plan

The benchmark now gates the next retrieval milestones:

1. Add a versioned dense embedding index whose rows retain canonical
   `anchor_id` and `revision_id` values.
2. Evaluate lexical and dense retrieval independently.
3. Combine their candidates using deterministic reciprocal-rank fusion.
4. Rerank the top candidates with a local cross-encoder.
5. Calibrate an answerability decision independently from passage relevance.

E4 uses the unchanged E3 top reranker score as its only answerability feature.
`calibrate.py` assigns whole papers to deterministic folds, learns the
balanced-accuracy threshold on the other folds, and reports out-of-fold
selective metrics. Grouping by paper prevents questions about the same source
from appearing in both a fold's calibration and evaluation data. The output
also records a full-sample deployment threshold separately from the
out-of-fold experiment metrics.

Target measurements are:

| Stage | Recall@5 | Recall@10 | MRR |
| --- | ---: | ---: | ---: |
| Dense plus hybrid | at least 60% | at least 75% | at least 0.45 |
| Cross-encoder reranked | at least 70% | at least 80% | at least 0.55 |

Every stage must retain 100% anchor integrity and publish its results
separately. Model-card or external benchmark scores are only selection inputs;
changes are accepted based on this fixture and the later agent-oriented
evaluation set.

Experiments are deliberately sequential:

1. Dense retrieval only.
2. Sparse/dense fusion without changing either retriever.
3. Reranking without changing candidate generation.
4. Answerability and abstention without changing retrieval.

This isolates the value and cost of each feature.

The pinned archive is:

```text
URL: https://qasper-dataset.s3.us-west-2.amazonaws.com/qasper-train-dev-v0.3.tgz
SHA-256: a28fdf966db827bcee3d873107d6b6669864fb7ca8fbf73a192f5e39191bdb5a
```

QASPER citation:

> Dasigi et al. (2021), *A Dataset of Information-Seeking Questions and Answers
> Anchored in Research Papers*, NAACL.

See the [QASPER project repository](https://github.com/allenai/qasper-led-baseline)
for the paper, official evaluator, and baseline implementations.
