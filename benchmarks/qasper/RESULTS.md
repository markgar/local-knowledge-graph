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
4. Run the full 179-question fixture at least twice.
5. Confirm identical logical rankings and metrics across runs. Latency may
   vary and is reported separately.
6. Record the exact feature, configuration, aggregate metrics, and delta from
   the preceding experiment before starting another feature.
7. Preserve citation and anchor integrity at 100%.

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

## Planned experiments

Each experiment begins only after the previous result is recorded.

| ID | Single feature under test | Comparison |
| --- | --- | --- |
| E1 | Dense retrieval over the existing canonical anchors | Dense versus E0 natural lexical retrieval |
| E2 | Reciprocal-rank fusion of unchanged E0 lexical and E1 dense rankings | Hybrid versus the better of E0 and E1 |
| E3 | Cross-encoder reranking of the unchanged E2 candidate set | Reranked hybrid versus E2 |
| E4 | Calibrated answerability and abstention over unchanged E3 retrieval | Selective answering versus E3 |
| E5 | Agent-facing tool interface over the accepted retrieval pipeline | End-to-end agent tasks versus direct retrieval |

Chunking changes, alternate embedding models, and alternate rerankers are
separate experiments. They do not silently replace E1 or E3 configurations.

## Next experiment: E1 dense retrieval

E1 will add one capability: semantic nearest-neighbor retrieval over the same
source anchors used by E0.

Planned constraints:

- Keep current Markdown parsing and anchor boundaries unchanged.
- Keep canonical evidence and metadata in SQLite.
- Store a disposable vector projection keyed by `anchor_id` and `revision_id`.
- Use Sentence Transformers with one pinned, permissively licensed embedding
  model. The initial candidate is `Alibaba-NLP/gte-modernbert-base`.
- Use cosine similarity and return the same public evidence contracts.
- Do not add lexical fusion, query rewriting, reranking, answer generation, or
  abstention in E1.
- Compare dense-only results directly with E0 natural lexical results.

The model revision, inference runtime, vector-index implementation, and exact
hardware measurements must be recorded when E1 begins; they are not considered
fixed until the implementation dependency and local-runtime constraints have
been validated.
