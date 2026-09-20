# Foundation workload and initial budgets

Status: implemented V0 inputs and **proposed engineering targets**.
No service-performance baseline, model quality, ACL enforcement, recovery result
or production readiness is claimed. V1 owns measurements and reviewed revisions.

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

The [A01-A17 recipes](../../corpora/foundation/README.md) specify namespace
collisions, denied sources, competing identities, retry/restore/concurrency and
failure injection. The generator's labels are expected setup, not access checks.
Future runners must interpret operation recipes through real service boundaries;
they must not count these flags directly as evidence of service correctness.

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

| Metric | Initial target | Method / enforcing owner | Rationale |
| --- | --- | --- | --- |
| Text intake (no embeddings/enrichment) | >=100 documents/s | Fresh DB, 1,000 creates, 5 runs; total commits/time per run, report median and worst; E1/V1 | Ten-second local import goal for small supplied inputs. |
| Structured reads | p95 <=100 ms | 100 warm-up then 1,000 measured calls, nearest-rank p95; Q1/Q2/V1 | Interactive exact reads should not be model-bound. |
| Graph reads (<=3 hops) | p95 <=250 ms | Same timing schedule, generated 243-entity/480-edge graph; report eligible node/edge totals; K1/Q2/V1 | Allow bounded joins without seconds-long navigation. |
| Warm product search | p95 <=2,000 ms | Ready real local providers, matching index, 100 warm-up and 1,000 measured queries; Q1/V1 | Human-facing local search goal, not achieved baseline. |
| Cold product search | p95 <=15,000 ms | 20 new processes with approved local model cache and ready index, no downloads; process start to first response; Q1/V1 | Separates provider initialization from steady-state cost. |
| Peak RSS | <=2 GiB mechanical; <=8 GiB including local search models | Sample entire process tree at 50 ms plus OS high-water mark, report larger; E1/E3/Q1/V1 | Leave headroom on a 16-GiB laptop. |
| Managed disk | <=100 MiB canonical + lexical; <=250 MiB vector projections | Total logical file sizes including WAL/SHM after workload, before/after checkpoint; exclude/report model caches separately; E1/E3/V1 | Small workload should not require large retained stores. |
| Recovery | <=30 s | Kill process after canonical commit and before index/job acknowledgment at deterministic fault points, restart until explicit ready/failed state; E4/K5/V1 | Short, observable local recovery; no endless retry loop. |
| Query planner | <=5 s, <=2 calls, <=8,000 input + 2,000 output tokens per request | Count total across attempts, monotonic deadline; Q3/V1 | Bounded planning rather than autonomous investigation. |
| Enrichment agent per document | <=30 s, <=4 calls, <=16,000 input + 4,000 output tokens | Total across attempts, monotonic deadline; I2/K5/V1 | Enough room for evidence lookup, not open-ended loops. |
| External inference cost | $0 authorized; proposed future caps $0.02/query, $0.05/document | Current local-only execution; if separately authorized, record provider/pricing/date and all charged attempts; Q3/I2/V1 | Cost ceiling is not permission for hosted inference. |

Report successes, failures, timeouts and correctness separately; never discard
failed samples to improve p95. Repeat timed runs five times except the stated
cold-process experiment. Report each run's p95 and the worst run against target,
not a favorable pooled average. For throughput, any run under target is a miss.
No wall-clock threshold is enforced in routine contract tests.

Correctness targets are zero cross-scope disclosures, zero duplicate committed
retry writes, zero lost/repointed historical citations outside explicit purge,
100% exact authored quotes/counts, and explicit ambiguity in every authored case.
These become enforceable under their A01-A17 integration owners, not in shape
tests. Preserve all existing relevance/answerability thresholds and frozen gold:
see [historical gates](../history/evidence-mvp.md#retrieval-acceptance-gates) and
[QASPER results](../qasper/RESULTS.md). Synthetic correctness does not establish
real-world recall, answerability or production quality.

Record misses as misses. Changes to workload/hardware/model/limits require a new
labeled run and reviewed rationale; changing a target never retroactively passes
a prior run. V1 supplements this synthetic workload with permission-approved
sources, real models and agents; S1/S2/S3 live acceptance stays separate and open.
