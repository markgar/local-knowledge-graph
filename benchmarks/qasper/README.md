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

## Run it

From the repository root:

```bash
uv run python benchmarks/qasper/prepare.py
uv run kg ingest --manifest benchmarks/qasper/data/corpus.yml
uv run python benchmarks/qasper/evaluate.py
```

Generated papers, gold annotations, the SQLite database, and detailed results
are written beneath `benchmarks/qasper/data/`, which Git ignores.

The evaluator uses natural search by default. Run it with `--strategy strict`
to reproduce the original all-term baseline:

```bash
uv run python benchmarks/qasper/evaluate.py --strategy strict
```

This baseline is intentionally not presented as a solved benchmark. It creates
a stable, public regression target for better lexical ranking and abstention
without adding embeddings or answer generation.

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
