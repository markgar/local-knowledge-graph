# QASPER experiment results

This file is the permanent results ledger for retrieval experiments. It keeps
the original baseline visible and records the incremental value of each
feature. Aggregate machine-readable baseline values are also available in
[`baseline.json`](baseline.json).

## Experimental protocol

Change one retrieval feature at a time:

1. Freeze the QASPER archive checksum, selected paper IDs, generated Markdown,
   gold evidence, query set, and metric implementation.
2. Start from the immediately preceding experiment.
3. Change one independent retrieval capability or one clearly bounded
   configuration choice.
4. Complete an independent code review and resolve its high-confidence
   findings before running the efficacy benchmark.
5. Run the full 179-question fixture at least twice.
6. Confirm identical logical rankings and metrics across runs. Latency may
   vary and is reported separately.
7. Record the exact feature, configuration, aggregate metrics, and delta from
   the preceding experiment before starting another feature.
8. Preserve citation and anchor integrity at 100%.

Do not combine a new embedding model, fusion method, chunking strategy, and
reranker in one experiment. If a feature requires several inseparable
mechanical changes, describe them as one capability and do not include another
retrieval improvement in the same result.

Model and index experiments must record:

- Model and revision.
- Model license.
- Embedding dimensions and normalization.
- Passage or chunk definition.
- Index implementation and distance metric.
- Candidate count.
- Fusion or reranking parameters.
- Warm-query median and p95 latency.
- Indexing time and derived-index size.

## Metrics

Every retrieval experiment reports:

- Evidence Recall@1, Recall@5, and Recall@10.
- Mean reciprocal rank.
- Evidence-set F1@10.
- Anchor integrity.
- Median and p95 query latency.

Answerability experiments additionally report:

- False-evidence rate on unanswerable questions.
- Abstention precision and recall.
- Clarification accuracy for ambiguous questions when that evaluation set is
  available.

The unanswerable false-evidence rate is diagnostic for retrieval. A relevant
passage can exist in a paper even when the annotated question has no supported
answer, so agent-readiness ultimately requires a separate answerability
decision and a dedicated in-domain evaluation set.

## Results

Contextual retrieval (`--contextual`) is measured in the E3-C1 comparison below.
All E0-E5 and E1-S1 numbers retain their original passage-only semantic
representation. The contextual result does not supersede any recorded result
or acceptance gate.

### E0: FTS5 lexical baseline

Commit: `e68c1ff`

The strict mode requires every query term in one passage. Natural mode safely
joins unique terms with FTS5 `OR` and ranks candidates using BM25.

| Metric | Strict | Natural |
| --- | ---: | ---: |
| Evidence Recall@1 | 0.0% | 12.2% |
| Evidence Recall@5 | 0.0% | 40.2% |
| Evidence Recall@10 | 0.0% | 54.6% |
| Mean reciprocal rank | 0.000 | 0.302 |
| Evidence-set F1@10 | 0.0% | 12.8% |
| Unanswerable false-evidence rate | 0.0% | 100.0% |
| Anchor integrity | 100.0% | 100.0% |
| Median query latency | 2.0 ms | 32.0 ms |
| p95 query latency | 3.2 ms | 39.6 ms |

Interpretation:

- Exact citation resolution works.
- Strict matching is unsuitable for natural-language questions.
- Natural BM25 finds useful evidence but misses 45.4% of gold evidence at
  rank 10.
- Top-rank quality is too low for autonomous agent use.
- The system has no calibrated answerability decision.

## Experiment sequence and outcomes

Each experiment was run only after the previous result was recorded.

| ID | Single feature under test | Outcome |
| --- | --- | --- |
| E1 | Dense retrieval over the existing canonical anchors | Complete: dense versus E0 natural lexical retrieval |
| E2 | Reciprocal-rank fusion of unchanged E0 lexical and E1 dense rankings | Hybrid versus the better of E0 and E1 |
| E3 | Cross-encoder reranking of the unchanged E2 candidate set | Reranked hybrid versus E2 |
| E4 | Calibrated answerability and abstention over unchanged E3 retrieval | Complete: selective answering versus E3 |
| E5 | Agent-facing tool interface over the existing retrieval and provenance services | Complete: JSON CLI workflows versus direct service use |

Chunking changes, alternate embedding models, and alternate rerankers are
separate experiments. They do not silently replace E1 or E3 configurations.

## E1: Dense retrieval

E1 adds one capability: semantic nearest-neighbor retrieval over the same
source anchors used by E0.

Configuration:

- Keep current Markdown parsing and anchor boundaries unchanged.
- Keep canonical evidence and metadata in SQLite.
- Store a disposable vector projection keyed by `anchor_id` and `revision_id`.
- Use Sentence Transformers with the Apache-2.0
  `Alibaba-NLP/gte-modernbert-base` model pinned at revision
  `752e76f479f37e13f5e956c0a277bd7ccea80714`.
- Store normalized 768-dimensional vectors in a versioned sqlite-vec
  projection keyed by passage, anchor, and revision IDs.
- Use cosine similarity and return the same public evidence contracts.
- Do not add lexical fusion, query rewriting, reranking, answer generation, or
  abstention in E1.
- Compare dense-only results directly with E0 natural lexical results.

Runtime:

- Sentence Transformers 5.7.0, Transformers 5.17.0, and PyTorch 2.14.0.
- sqlite-vec 0.1.9 with cosine distance.
- Apple M4 Pro with 24 GiB RAM on macOS 26.6.2.
- 3,599 canonical passages; unchanged parser anchor boundaries.
- Indexing time: 70.8 seconds.
- Derived-index size: 159,739,904 bytes (152.3 MiB).
- Candidate count: 10 passages per paper-scoped question.

Both full 179-question runs produced identical ranked passage IDs and logical
metrics.

| Metric | E0 natural | E1 dense | Delta |
| --- | ---: | ---: | ---: |
| Evidence Recall@1 | 12.2% | 10.0% | -2.2 pp |
| Evidence Recall@5 | 40.2% | 29.1% | -11.1 pp |
| Evidence Recall@10 | 54.6% | 42.7% | -11.9 pp |
| Mean reciprocal rank | 0.302 | 0.264 | -0.038 |
| Evidence-set F1@10 | 12.8% | 11.2% | -1.6 pp |
| Unanswerable false-evidence rate | 100.0% | 100.0% | 0.0 pp |
| Anchor integrity | 100.0% | 100.0% | 0.0 pp |
| Median query latency | 32.0 ms | 74.6-76.1 ms | +42.6-44.1 ms |
| p95 query latency | 39.6 ms | 90.1-241.5 ms | +50.5-201.9 ms |

Interpretation:

- Dense-only retrieval underperforms the natural BM25 baseline at every
  relevance metric on this fixture.
- Exact citation resolution remains intact.
- The selected model and unchanged anchor boundaries do not justify replacing
  lexical retrieval.
- E1 is retained as a reproducible negative result and as the dense component
  for the separately measured E2 reciprocal-rank-fusion experiment.

## E2: Sparse/dense reciprocal-rank fusion

E2 adds one capability: deterministic reciprocal-rank fusion over the
unchanged E0 natural-BM25 and E1 dense rankings.

Frozen configuration:

- Keep the QASPER corpus, questions, gold evidence, metric implementation,
  Markdown parsing, anchor boundaries, and E1 embedding projection unchanged.
- Request 50 candidates from E0 and 50 candidates from E1 for each
  paper-scoped question.
- Apply identical subject, source, and date filters to both parent retrievers.
- Deduplicate candidates by canonical passage ID.
- Score each candidate as `1.0 / (20 + lexical_rank)` plus
  `0.5 / (20 + dense_rank)` when it appears in the respective parent ranking.
- Order equal fusion scores by passage ID.
- Return the existing canonical evidence contract and require 100% anchor
  integrity.
- Fail explicitly if the E1 projection is missing, stale, or incompatible.

Runtime:

- Same model, projection, 3,599 canonical passages, and Apple M4 Pro host as
  E1.
- Candidate count: 50 passages from each parent ranking; 10 fused results
  returned.
- Both full 179-question runs produced identical ranked passage IDs and
  logical metrics.

| Metric | E0 natural | E1 dense | E2 hybrid | Delta vs. E0 |
| --- | ---: | ---: | ---: | ---: |
| Evidence Recall@1 | 12.2% | 10.0% | 15.2% | +3.0 pp |
| Evidence Recall@5 | 40.2% | 29.1% | 41.6% | +1.4 pp |
| Evidence Recall@10 | 54.6% | 42.7% | 60.9% | +6.4 pp |
| Mean reciprocal rank | 0.302 | 0.264 | 0.334 | +0.032 |
| Evidence-set F1@10 | 12.8% | 11.2% | 14.6% | +1.8 pp |
| Unanswerable false-evidence rate | 100.0% | 100.0% | 100.0% | 0.0 pp |
| Anchor integrity | 100.0% | 100.0% | 100.0% | 0.0 pp |
| Median query latency | 31.6-31.9 ms | 72.2-72.6 ms | 108.1-108.3 ms | +76.2-76.7 ms |
| p95 query latency | 39.7-40.2 ms | 85.8-88.0 ms | 136.5-140.0 ms | +96.3-100.3 ms |

Interpretation:

- Hybrid retrieval improves every measured relevance metric over both
  unchanged parent retrievers on this fixture.
- The largest gain is Recall@10, which rises 6.4 percentage points over E0.
- Citation integrity remains exact and deterministic.
- The result does not meet the Milestone 5 acceptance gates of 60% Recall@5,
  75% Recall@10, and 0.45 MRR.
- The 100% false-evidence rate is unchanged because E2 ranks passages but does
  not make an answerability decision.
- These measurements establish one-domain evidence only. A separate
  email-and-meeting-notes-style evaluation is required before treating the
  fusion defaults as broadly validated.

## E3: Cross-encoder reranking

E3 adds one capability: local cross-encoder scoring over the unchanged top 50
E2 hybrid candidates.

Frozen configuration:

- Keep the QASPER corpus, questions, gold evidence, metrics, parser anchors,
  E1 dense projection, and E2 reciprocal-rank fusion unchanged.
- Request the top 50 E2 candidates for each paper-scoped question.
- Score each `(question, passage_text)` pair with the Apache-2.0
  `cross-encoder/ms-marco-MiniLM-L6-v2` model pinned at revision
  `233902d25c440f23af6f7d6e94d2946bac0bee0a`.
- Use the model's raw relevance score and a batch size of 32.
- Order equal scores by canonical passage ID.
- Return the existing canonical evidence contract and require 100% anchor
  integrity.
- Fail explicitly if the dense projection or reranker is unavailable, or if
  the corpus changes during candidate generation or reranking.

Runtime:

- Sentence Transformers 5.7.0, Transformers 5.17.0, and PyTorch 2.14.0.
- Same E1 dense projection, 3,599 canonical passages, and Apple M4 Pro host as
  E1 and E2.
- Candidate count: 50 E2 hybrid passages per paper-scoped question; 10
  reranked results returned.
- Both full 179-question runs produced identical ranked passage IDs and
  logical metrics.

| Metric | E2 hybrid | E3 reranked | Delta |
| --- | ---: | ---: | ---: |
| Evidence Recall@1 | 15.2% | 22.9% | +7.7 pp |
| Evidence Recall@5 | 41.6% | 58.5% | +16.9 pp |
| Evidence Recall@10 | 60.9% | 72.2% | +11.3 pp |
| Mean reciprocal rank | 0.334 | 0.446 | +0.112 |
| Evidence-set F1@10 | 14.6% | 17.4% | +2.8 pp |
| Unanswerable false-evidence rate | 100.0% | 100.0% | 0.0 pp |
| Anchor integrity | 100.0% | 100.0% | 0.0 pp |
| Median query latency | 108.1-108.3 ms | 202.0-208.8 ms | +93.7-100.7 ms |
| p95 query latency | 136.5-140.0 ms | 254.4-314.5 ms | +114.4-178.0 ms |

Interpretation:

- Cross-encoder reranking improves every measured relevance metric over E2.
- Recall@5 gains 16.9 percentage points and MRR rises by 0.112.
- Exact citation integrity and logical ranking determinism remain intact.
- The result remains below the E3 acceptance gates of 70% Recall@5, 80%
  Recall@10, and 0.55 MRR, so the selected reranker does not yet establish
  agent-useful retrieval.
- The 100% false-evidence rate is unchanged because answerability remains the
  separately scoped E4 experiment.

## E4: Calibrated answerability and abstention

E4 adds one capability: a calibrated decision to return the unchanged E3
ranking or abstain. It does not alter candidate generation, reranker scores,
passage order, source anchors, or citations.

Configuration:

- Use the raw top E3 cross-encoder score as the only answerability feature.
- Assign whole papers to five deterministic folds using SHA-256 so questions
  about one paper never appear in both a fold's calibration and evaluation
  data.
- On the other four folds, select the score threshold that maximizes balanced
  answerability accuracy. Break equal objectives by lower false-positive rate,
  then higher answerable coverage, then the higher threshold.
- Evaluate each question only with the threshold learned without its paper.
- Report selective retrieval metrics over all answerable questions, assigning
  zero retrieval credit when the system abstains.
- Fit a separate full-sample threshold for later deployment experiments; do
  not use it for the out-of-fold metrics below.

Runtime:

- Same E3 GTE projection, hybrid candidate set, reranker, 50 papers, 3,599
  passages, and 179 questions.
- 167 questions have gold evidence and 12 are unanswerable at the question
  level.
- The full-sample threshold is `0.7454872131347656`.
- Fold thresholds range from `-4.038587212562561` to
  `0.7454872131347656`.
- Out-of-fold confusion counts are 115 true positives, 52 false negatives,
  7 false positives, and 5 true negatives.

| Metric | E3 reranked | E4 selective | Delta |
| --- | ---: | ---: | ---: |
| Evidence Recall@1 | 22.9% | 13.3% | -9.6 pp |
| Evidence Recall@5 | 58.5% | 38.6% | -19.9 pp |
| Evidence Recall@10 | 72.2% | 48.7% | -23.5 pp |
| Mean reciprocal rank | 0.446 | 0.289 | -0.157 |
| Evidence-set F1@10 | 17.4% | 12.2% | -5.2 pp |
| Unanswerable false-evidence rate | 100.0% | 58.3% | -41.7 pp |
| Answerable coverage | 100.0% | 68.9% | -31.1 pp |
| Answered precision | 93.3% | 94.3% | +1.0 pp |
| Answerability balanced accuracy | 50.0% | 55.3% | +5.3 pp |

Interpretation:

- The score threshold reduces false evidence, but still answers seven of the
  twelve unanswerable questions.
- The reduction costs 31.1 percentage points of answerable coverage and
  materially lowers every evidence-retrieval metric.
- Thresholds vary substantially across paper-grouped folds, showing that the
  top reranker score is not a stable answerability signal on this sample.
- E4 therefore does not establish calibrated abstention for the accepted
  retrieval pipeline. A later experiment needs a separately justified
  answerability signal and more question-level unanswerable examples; it must
  not tune passage ranking on E4 labels.

## E5: Agent-facing JSON CLI

E5 packages the existing retrieval and provenance services as a versioned,
machine-readable CLI interface. It deliberately does not add an agent SDK,
network service, or MCP dependency.

Interface:

- `kg capabilities` publishes interface version 1 and the supported
  operations.
- `kg search` returns evidence candidates.
- `kg evidence` resolves a result record to its citation.
- `kg source-range` returns the immutable anchor, exact offsets, quote, and
  quote hash.
- `kg revisions` lists a document's immutable history.
- `kg compare-revisions` compares added, removed, modified, and unchanged
  anchored ranges.
- `kg status` returns structured multi-document evidence, explicit conflicts,
  and evidence gaps.

The reviewed `benchmarks/agent/tasks.json` fixture exercises seven workflows:
paraphrase retrieval, multi-document synthesis, explicit conflict reporting,
ambiguous cross-source results, unsupported-subject abstention, citation
round-tripping, and revision comparison.

The workflow fixture uses natural BM25 search. E5 evaluates the command, JSON,
provenance, and revision contracts; it does not reclassify the E3 reranker or
the rejected E4 threshold as an accepted agent-ready retrieval pipeline.

| Metric | Result |
| --- | ---: |
| Workflow pass rate | 7/7 (100%) |
| Citation round-trip integrity | 100% |
| Historical source-range availability | 100% |
| Revision comparison success | 100% |

Interpretation:

- The CLI is sufficient as the first agent integration boundary; MCP can
  remain a future transport adapter.
- All reviewed deterministic workflows preserve exact citations and immutable
  revision identity.
- This integration result does not override the E3 relevance shortfall or the
  rejected E4 answerability threshold. Agents can use the tools reliably, but
  natural-language evidence selection and abstention are not yet
  production-ready.

## E1-S1: GTE versus Qwen embedding substitution

This model-substitution experiment changes only the local single-vector
embedding profile. The QASPER archive, 50 papers, 3,599 passages, 179
questions, gold evidence, source filters, E2 weights and candidate counts, E3
reranker and scoring, answerability behavior, and metric code remain
unchanged. GTE remains the CLI default.

Profile configuration:

| Property | GTE ModernBERT | Qwen3-Embedding-0.6B |
| --- | --- | --- |
| Profile | `gte-modernbert` | `qwen3-embedding-0.6b` |
| Model | `Alibaba-NLP/gte-modernbert-base` | `Qwen/Qwen3-Embedding-0.6B` |
| Revision | `752e76f479f37e13f5e956c0a277bd7ccea80714` | `97b0c614be4d77ee51c0cef4e5f07c00f9eb65b3` |
| License | Apache-2.0 | Apache-2.0 |
| Parameters | 149,014,272 | 595,776,512 |
| Dimensions | 768 | 1,024 |
| Context behavior | Model-native truncation | Model-native 32K context with truncation |
| Query encoding | Plain text | Official `query` prompt: `Instruct: Given a web search query, retrieve relevant passages that answer the query` followed by `Query:` |
| Document encoding | Plain text | Plain text with no instruction |
| Pooling | Model Sentence Transformers configuration | Last-token pooling from the pinned model configuration |
| Normalization | L2 | L2 |
| Index | sqlite-vec 0.1.9 cosine distance | sqlite-vec 0.1.9 cosine distance |

Measured runtime:

- Sentence Transformers 5.7.0, Transformers 5.17.0, PyTorch 2.14.0.
- Apple M4 Pro with 24 GiB RAM on macOS 26.6.2.
- Automatic local backend selection chose MPS. GTE used `torch.float16`; Qwen
  used `torch.bfloat16`. CPU remains a supported fallback, and device plus
  dtype are included in projection compatibility.
- E2 remains 50 natural-BM25 and 50 dense candidates, RRF `k = 20`, lexical
  weight `1.0`, and dense weight `0.5`.
- E3 remains the pinned
  `cross-encoder/ms-marco-MiniLM-L6-v2` revision
  `233902d25c440f23af6f7d6e94d2946bac0bee0a`, 50 candidates, and batch size
  32.

Cost:

| Measurement | GTE | Qwen | Qwen delta |
| --- | ---: | ---: | ---: |
| Indexing time | 71.4 s | 259.9 s | +188.5 s / 3.6x |
| Model cache size | 287.7 MiB | 1,151.6 MiB | +863.9 MiB / 4.0x |
| Derived-index size | 152.3 MiB | 202.4 MiB | +50.0 MiB / 32.8% |

Dense retrieval:

| Metric | GTE | Qwen | Delta |
| --- | ---: | ---: | ---: |
| Evidence Recall@1 | 10.0% | 11.1% | +1.2 pp |
| Evidence Recall@5 | 29.1% | 43.8% | +14.7 pp |
| Evidence Recall@10 | 42.7% | 64.2% | +21.5 pp |
| Mean reciprocal rank | 0.264 | 0.327 | +0.063 |
| Evidence-set F1@10 | 11.2% | 15.9% | +4.7 pp |
| Unanswerable false-evidence rate | 100.0% | 100.0% | 0.0 pp |
| Anchor integrity | 100.0% | 100.0% | 0.0 pp |
| Median query latency | 74.3 ms | 94.0-94.9 ms | +19.7-20.6 ms |
| p95 query latency | 93.3 ms | 122.2-151.9 ms | +28.9-58.6 ms |

Hybrid retrieval with unchanged E2:

| Metric | GTE | Qwen | Delta |
| --- | ---: | ---: | ---: |
| Evidence Recall@1 | 15.2% | 17.8% | +2.6 pp |
| Evidence Recall@5 | 41.6% | 46.6% | +5.0 pp |
| Evidence Recall@10 | 60.9% | 62.5% | +1.5 pp |
| Mean reciprocal rank | 0.334 | 0.373 | +0.039 |
| Evidence-set F1@10 | 14.6% | 15.0% | +0.4 pp |
| Unanswerable false-evidence rate | 100.0% | 100.0% | 0.0 pp |
| Anchor integrity | 100.0% | 100.0% | 0.0 pp |
| Median query latency | 109.3 ms | 131.8-136.1 ms | +22.5-26.8 ms |
| p95 query latency | 132.5 ms | 158.6-228.8 ms | +26.1-96.4 ms |

Reranked retrieval with unchanged E3:

| Metric | GTE | Qwen | Delta |
| --- | ---: | ---: | ---: |
| Evidence Recall@1 | 22.9% | 22.9% | 0.0 pp |
| Evidence Recall@5 | 58.5% | 58.5% | 0.0 pp |
| Evidence Recall@10 | 72.2% | 71.6% | -0.6 pp |
| Mean reciprocal rank | 0.446 | 0.445 | -0.001 |
| Evidence-set F1@10 | 17.4% | 17.3% | -0.1 pp |
| Unanswerable false-evidence rate | 100.0% | 100.0% | 0.0 pp |
| Anchor integrity | 100.0% | 100.0% | 0.0 pp |
| Median query latency | 200.6 ms | 218.6-221.3 ms | +18.0-20.7 ms |
| p95 query latency | 256.1 ms | 277.5-280.4 ms | +21.4-24.3 ms |

Both complete Qwen runs produced identical ranked passage IDs and identical
logical metrics for dense, hybrid, and reranked retrieval. Latency varied as
expected. Both profile projections remained on disk simultaneously, no vector
spaces or rankings were combined outside the unchanged E2 fusion, and every
returned item retained exact canonical evidence.

Interpretation:

- Qwen is substantially stronger as a dense retriever on this fixture.
- The unchanged E2 fusion captures only a modest part of that gain because
  Qwen dense and natural BM25 contribute different ranking interactions under
  the frozen weights.
- The unchanged E3 reranker removes the apparent Qwen advantage and produces
  essentially equal final quality, with slightly lower Qwen Recall@10 and F1.
- Qwen costs materially more disk, cache, indexing time, and query latency, so
  this experiment does not justify changing the compatibility default.
- The 100% unanswerable false-evidence rate is unchanged because answerability
  remains outside this model-substitution experiment.
- QASPER is one scientific-paper regression dataset. It is not the sole basis
  for selecting a general-purpose embedding model; agent-oriented and
  note/email/meeting-style evaluations remain necessary.

## E3-C1: Contextual representation on current code

Exploratory rerun on 2026-09-19 at code commit
`d4c675a9dccd070bee54c199f3b221b5cd5c99cc`. This measures the existing contextual
implementation, not a new retrieval-code change. No new independent code review
was performed for this rerun.

The fixed QASPER 0.3 archive and selected 50 development papers produced the
same 179 questions (167 answerable, 12 unanswerable) and 3,599 canonical
passages. The archive checksum matched the pinned value. Generated `gold.json`
SHA-256: `1dcf34271f3b39a598dc90d0f5ed08923adf60e7cb1244ccd4da29e2346c5e03`.

Both arms use the E1 GTE model and pinned revision, normalized 768-dimensional
vectors in sqlite-vec, unchanged E2 fusion, and the E3 pinned MiniLM reranker
over 50 candidates, returning ten passages. The sole experimental change is
adding document titles and heading paths to embedding and reranking input.
Canonical source quotes and evaluation rules are unchanged.

Runtime: Python 3.12.13, macOS 27.0 arm64, MPS embedding device with float16,
Sentence Transformers 5.7.0, Transformers 5.17.0, PyTorch 2.14.0, sqlite-vec
0.1.9. Models were loaded from the local cache with network access disabled.
The four evaluations ran sequentially, twice per arm.

| Metric | Current passage-only | Contextual | Delta |
| --- | ---: | ---: | ---: |
| Evidence Recall@1 | 22.9% | 25.0% | +2.1 pp |
| Evidence Recall@5 | 58.5% | 61.8% | +3.3 pp |
| Evidence Recall@10 | 72.2% | 75.2% | +3.0 pp |
| Mean reciprocal rank | 0.446 | 0.461 | +0.015 |
| Evidence-set F1@10 | 17.4% | 18.3% | +0.9 pp |
| Unanswerable false-evidence rate | 100.0% | 100.0% | 0.0 pp |
| Anchor integrity | 100.0% | 100.0% | 0.0 pp |
| Warm-query median latency | 190.9-191.8 ms | 206.2-207.2 ms | |
| Warm-query p95 latency | 240.5-243.1 ms | 251.1-252.1 ms | |
| Index build time | 69.1 s | 82.1 s | +13.1 s |
| Derived-index size per projection | 159,739,904 bytes | 159,739,904 bytes | 0 |

Each arm reproduced identical per-question ranked record IDs, top scores, and
logical metrics across its two runs. The current passage-only aggregate quality
matches the recorded E3 result. Detailed local outputs are
`data/results-current-reranked-{1,2}.json` and
`data/results-current-contextual-{1,2}.json`; dataset-derived details remain
ignored rather than redistributed.

Contextual Recall@10 improved for 12 answerable questions, worsened for five,
and was unchanged for 150. This is a modest aggregate gain, not a universal
improvement or a statistical significance claim. It still misses the reranked
acceptance gates of 70% Recall@5, 80% Recall@10, and 0.55 MRR. All twelve
unanswerable questions still return passages; this experiment does not solve
answerability or measure generated-answer quality. Contextual mode remains
opt-in.
