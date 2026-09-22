# Foundation workload and initial budgets

Status: deterministic inputs and **proposed engineering targets**.
No service-performance baseline, model quality, ACL enforcement, recovery result
or production readiness is claimed. Evaluation work is tracked in
[V1](https://github.com/markgar/local-knowledge-graph/issues/46).

## Reproduction and exact workload

Run `uv run python benchmarks/foundation/workload.py` to print the manifest.
The importable `workload()` returns the full deterministic operation/document
input and `serialized()` returns canonical UTF-8 JSON bytes (sorted keys, compact
separators, trailing LF). This prints metadata, not a benchmark score. It requires
no models, network, service, or random library. Seed 1729 is a formula input;
version `foundation-workload/2` freezes that formula and serialization.
Long documents have 40 separate paragraphs after their introductory block
(41 source anchors). Structural tests run the real Markdown parser to pin this.
The workload names two distinct Sam entities for the ambiguity scenario.

| Property | Exact value |
| --- | --- |
| Documents | 1,000: work=800, isolated=200 |
| Source namespaces | markdown=500, email=500; both occur in each corpus |
| Text mix | 800 small notes, 200 forty-paragraph documents; all include CRLF, combining accent and non-BMP character |
| UTF-8 source bytes | 283,240 |
| Canonical workload bytes | 544,977 |
| SHA-256 | `d627ee88e0d5d5fa7ef1a5babea74ffb611a7865a4ff1c37095b49072d022ef0` |
| Task selection | 240 work tasks, 216 visible to allowed-only access; display limit=20 |
| Mutations | 1,000 creates; 100 identical retries; 100 edits; 50 remove/restore pairs; 25 metadata updates; 25 stale writes; 25-document failed enumeration |
| Queries | 100 each structured, graph, search, count; ambiguity variant with distinct Sam identities |
| Graph setup | 243 entities (two distinct Sams, Atlas, 240 tasks); 480 inferred edges (Sam owns task, task part_of Atlas), each supported by its task document index |

The [A01-A17 recipes](https://github.com/markgar/local-knowledge-graph/issues/28#acceptance-recipes) specify namespace
collisions, denied sources, competing identities, retry/restore/concurrency and
failure injection. The generator's labels are expected setup, not access checks.
Measuring service correctness requires invoking actual service boundaries;
counting these setup flags is not evidence of service behavior.

## Initial targets, rationale and measurement

Reference evaluation class: local 8-core ARM64 or x86-64 laptop, 16 GiB RAM,
SSD, Python 3.12+, SQLite as shipped by Python, one worker/writer. Record exact
CPU/OS/RAM/disk, Python/SQLite/package versions, git SHA, workload hash and
configuration with every measurement; results on another class are separately
labeled. No actual machine performance has been measured by this record.

For model runs, record pinned profile/revision, reranker revision, context policy,
device/dtype, runtime and approved cache identity. Default comparison is
`gte-modernbert`, non-contextual, existing pinned MiniLM reranker; Qwen/contextual
are separate runs, never blended into one latency. No routine test downloads/runs
a model and no hosted egress is authorized.

| Metric | Initial target | Measurement method | Rationale |
| --- | --- | --- | --- |
| Text intake (no embeddings/enrichment) | >=100 documents/s | Fresh DB, 1,000 creates, 5 runs; total commits/time per run, report median and worst | Ten-second local import goal for small supplied inputs. |
| Structured reads | p95 <=100 ms | 100 warm-up then 1,000 measured calls, nearest-rank p95 | Interactive exact reads should not be model-bound. |
| Graph reads (<=3 hops) | p95 <=250 ms | Same timing schedule, generated 243-entity/480-edge graph; report eligible node/edge totals | Allow bounded joins without seconds-long navigation. |
| Warm product search | p95 <=2,000 ms | Ready real local providers, matching index, 100 warm-up and 1,000 measured queries | Human-facing local search goal, not achieved baseline. |
| Cold product search | p95 <=15,000 ms | 20 new processes with approved local model cache and ready index, no downloads; process start to first response | Separates provider initialization from steady-state cost. |
| Peak RSS | <=2 GiB mechanical; <=8 GiB including local search models | Sample entire process tree at 50 ms plus OS high-water mark, report larger | Leave headroom on a 16-GiB laptop. |
| Managed disk | <=100 MiB canonical + lexical; <=250 MiB vector projections | Total logical file sizes including WAL/SHM after workload, before/after checkpoint; exclude/report model caches separately | Small workload should not require large retained stores. |
| Recovery | <=30 s | Kill process after canonical commit and before index/job acknowledgment at deterministic fault points, restart until explicit ready/failed state | Short, observable local recovery; no endless retry loop. |
| Query planner | <=5 s, <=2 calls, <=8,000 input + 2,000 output tokens per request | Count total across attempts, monotonic deadline | Bounded planning rather than autonomous investigation. |
| Enrichment agent per document | <=30 s, <=4 calls, <=16,000 input + 4,000 output tokens | Total across attempts, monotonic deadline | Enough room for evidence lookup, not open-ended loops. |
| External inference cost | $0 authorized; proposed future caps $0.02/query, $0.05/document | Current local-only execution; if separately authorized, record provider/pricing/date and all charged attempts | Cost ceiling is not permission for hosted inference. |

Report successes, failures, timeouts and correctness separately; never discard
failed samples to improve p95. Repeat timed runs five times except the stated
cold-process experiment. Report each run's p95 and the worst run against target,
not a favorable pooled average. For throughput, any run under target is a miss.
No wall-clock threshold is enforced in routine contract tests.

Correctness targets are zero cross-scope disclosures, zero duplicate committed
retry writes, zero lost/repointed historical citations outside explicit purge,
100% exact authored quotes/counts, and explicit ambiguity in every authored case.
Shape tests do not establish these service-level outcomes.
Preserve all existing relevance/answerability thresholds and frozen gold:
see [historical gates](../history/evidence-mvp.md#retrieval-acceptance-gates) and
[QASPER results](../qasper/RESULTS.md). Synthetic correctness does not establish
real-world recall, answerability or production quality.

Record misses as misses. Changes to workload/hardware/model/limits require a new
labeled run and reviewed rationale; changing a target never retroactively passes
a prior run. Evaluation ownership and pending service/live acceptance live in the
[tracking issue](https://github.com/markgar/local-knowledge-graph/issues/28#evaluation-ownership).

## Controlled Q1 composition measurements

`measure_query_search.py` exercises actual E1 intake, IndexService, full E3 search
and the spawned QueryService with deterministic controlled providers. It also
measures the real 25/1,001 decision counts and retained support inspections.
Run from the repository root with development dependencies and a fresh directory:

```bash
uv run python benchmarks/foundation/measure_query_search.py \
  --directory /tmp/q1-composition-measurements --samples 20 > /tmp/q1-composition.json
```

The record includes source hashes, environment, every call's outcome/latency,
public reservations (null when redacted), exact quotes/membership, per-scenario
timeout/partial/invalidation rates, 50-ms process-tree RSS samples plus OS
high-water marks, and logical SQLite/WAL/SHM sizes before/after checkpoint.
Induced public-budget failures, model stalls and actual E4 heartbeat invalidation
are separate scenarios, not failures removed from a favorable pooled average.

Every call spawns fresh controlled providers. Repetition is **not warm-model
search**, and filesystem-cache warmth is not controlled. Canonical/vector rows
share SQLite files; no fictional disk split is reported. This smaller workload is
not the 1,000-document foundation workload and cannot establish its targets,
real-model quality, checkpoint integration or wider V1 acceptance. Resource
measurements include setup/count production as labeled in each record.

The [2026-09-22 controlled record](q1-controlled-composition.json) contains 20
successful cold-process searches (nearest-rank p95 606.6 ms), each with one exact
hit and five reservations. The 25/1,001 submitted-decision counts and inspected
ID sets matched actual producer receipts. All four induced public limits failed
without data, the injected model stall timed out, and the heartbeat case returned
`state_changed`. Conservative measured parent-plus-child peak was 137,199,616
bytes; combined managed SQLite/WAL/SHM after checkpoint was 4,792,320 bytes.
These are observations on the labeled smaller controlled workload, **not passes
of the reference workload or real-model targets**. The record identifies the
working-tree source hashes used; it is not a claim that its baseline commit alone
contains the composed implementation.

The [review-fix and passage-support reconciliation run](q1-controlled-composition-reviewed.json)
preserves a separate 20-sample record rather than overwriting the initial run.
Its cold-process search p95 was 654.0 ms, with the same exact-hit/five-reservation,
25/1,001 membership and injected-failure outcomes. Conservative peak was
141,656,064 bytes and post-checkpoint managed disk was 4,780,032 bytes. Its baseline
commit includes the reconciled runtime; source hashes identify the measured files.
Neither run is a reference-workload target result.
