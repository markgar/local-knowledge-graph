# Foundation acceptance inputs

These are synthetic contract fixtures, not permission to connect to any source.
`enrichment.json` is a multi-document request value; `query.json` is an
exact-ID resolve/records/count plan value; `query-ambiguous.json` uses the same
plan with a name selector over two distinct Sam entities. Contract tests parse,
reject mutations and round-trip these values. They do **not** commit or execute
them; no generic write service or query executor is implemented.

The generator in `benchmarks/foundation/workload.py` supplies 1,000 deterministic
documents and operation selections. Index below means zero-based generator index.
This is the single A01-A17 acceptance inventory. Each recipe records its
integration owners; [CONTRACTS.md](../../CONTRACTS.md) defines the shared semantics
and [ROADMAP.md](../../ROADMAP.md) tracks package dependencies and ready issues.
All integration outcomes are **pending**; contract validation alone does not
complete a service scenario.
Implement real integration tests with the owning package, not skipped tests or
passing mock substitutes here.

| Case | Integration owners | Deterministic setup/action | Required integration observation |
| --- | --- | --- | --- |
| A01 | E1 | Submit workload indices 0/1 (same external ID, different namespace) and copy 0 into `isolated`; retry each with original key. | Three distinct document identities, one committed write each, no cross-corpus lookup. |
| A02 | E1, E3 | Supply `A\r\nCafe\u0301 \U0001f680`, code-point slice `[3,10)` with quote `Cafe\u0301 \U0001f680`; try end=11 and composed `Café`. | Exact bytes/hash/text survive; invalid ranges/quotes leave no rows. Shape-only counterparts run now. |
| A03 | E1, E2 | Upgrade a pre-E1 database with one retained revision/anchor but no full text; then offer both mismatching bytes and verified original bytes. | Existing IDs/history unchanged; unavailable until verified backfill. Never reconstruct content from quotes. |
| A04 | E1, E2, K1 | Co-locate indices 0/1, manifest seed Sam and agent alias/mention/assertion on Markdown evidence. Change manifest aliases and parser/config version to force record rebuild; remove seed and omit email from Markdown enumeration. | Before coexistence, mixed-writer rejection or scoped mutations. Once enabled, email and agent contributions survive; re-extraction cannot erase agent mentions/relationships or aliases. |
| A05 | E1, E2, E4 | In the same namespace/sync scope, start generation g1 then g2; finish g2, then g1. Separately stop enumeration after index 175, or mark failed. | Old/partial/failed runs deactivate nothing; current complete snapshot deactivates only its owned absent documents. |
| A06 | E1, K1, E4 | Commit index 0, drop the response, retry; reuse key with appended text. Race two updates expecting s1. Advance retry clock beyond 30 days. | Same receipt on authorized retry; changed-key payload conflicts; only one raced update commits; expired key never re-executes. |
| A07 | E1, K1 | One batch: valid create 0, stale update 1, valid create 2. Separately submit a malformed operation tag. | Ordered applied/conflict/applied, durable successes, zero failed-unit rows; malformed envelope prevents any execution. |
| A08 | E1, K4, K5 | Index 0: original s1/r1, edit s2/r2, deactivate s3, restore s4/r1; submit enrichment expecting s1. | Conflict despite reused r1; no stale enrichment committed or automatically revived. |
| A09 | K1, K2, K3, K4 | Create two distinct Sam entities; add a passage mentioning both, then explicit/inferred competing `work:owns` assertions. | Separate identities and contributions; mention creates no factual edge; no write-order truth winner. |
| A10 | X1, K2, Q1, Q2, Q4 | Deny workload access group `denied`, then revoke email access; exercise evidence/history/alias/path/count/diagnostics and continuation. | No unauthorized content, identifiers or totals; conjunctive contribution hidden if any support denied. |
| A11 | Q1, Q2, Q3 | All 240 workload tasks belong to work; 24 are denied, leaving 216, search limit 20. Execute `query.json` selecting `sam-primary` by ID; `query-ambiguous.json` selects name Sam over both generated Sam entities. Insert an update between resolve and count. | Count 216, not 20; inspect complete eligible support; ambiguous variant stops; interleaved execution observes one state or returns state_changed without data. |
| A12 | E3, E4, K5 | Commit 0 before enrichment; interrupt index/enrichment jobs, update source, restart old jobs. | Independent index readiness; stale jobs never mark new state ready; retry converges without duplicate knowledge. |
| A13 | Q4, X1 | Page all tied workload task results with limit 20; repeat cursor, expire it, change data, revoke access. | No duplicate/omitted records in supported result set; explicit invalidation/expiry; candidate-pool exhaustion distinguished from eligible-set exhaustion. |
| A14 | X1, E4, K5, Q4 | Purge 0 while job/retry/continuation data exist; restart/retry and simulate a failed derived-store purge. | Enumerated managed stores cannot disclose or resurrect content; explicit recoverable failure, never a secure-erasure claim. |
| A15 | E2, Q1, R1 | Run existing example/Atlas/state corpora and existing acceptance/product/CLI tests unchanged. | Existing IDs, quotes, results and authored gold preserved. Existing suite runs now; future adapters must rerun it. |
| A16 | E1, E3, X1 | On index 0 change only title/location then access policy; retain original citation and start a job against old state. | Fresh state/metadata snapshots, old citation context intact, stale job not ready. |
| A17 | E1, K1, K2 | Use `enrichment.json`; retry success; then independently change each supporting state before commit and try a missing local entity reference. Remove one source after commit. | Complete durable mapping stable on retry; any stale/invalid dependency leaves zero new changes. Conjunctive support becomes ineligible; separate alternative contribution remains eligible if all its own supports survive. |

Examples use namespaced predicates only as syntax. A future integration fixture
must register `work:owns` with person subject/project entity object. Its existence
here is not a generic hard-coded business ontology or authorization policy.
