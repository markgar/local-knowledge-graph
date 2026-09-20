# Foundation value fixtures

These are synthetic contract fixtures, not permission to connect to any source.
They are parsed by the implemented
[`foundation/1` value models](../../CONTRACTS.md):

| Fixture | Value |
| --- | --- |
| [`enrichment.json`](enrichment.json) | A multi-document enrichment request with request-local entity references and source-state dependencies. |
| [`query.json`](query.json) | An exact-ID resolve/records/count plan. |
| [`query-ambiguous.json`](query-ambiguous.json) | The same dependent plan with a name selector for the workload's two distinct Sam entities. |

Contract tests parse, reject mutations and round-trip these values. They do
**not** commit or execute them; no generic write service or query executor is
implemented. Namespaced predicates demonstrate syntax, not a registered business
ontology or authorization policy.

The [workload generator](../../benchmarks/foundation/README.md) supplies 1,000
deterministic documents and operation selections, including the distinct Sam
identities. Its labels describe synthetic setup, not enforced access or observed
service outcomes.

Pending integration scenarios and their owners live in the
[A01-A17 issue inventory](https://github.com/markgar/local-knowledge-graph/issues/28#acceptance-recipes),
not in these fixture files.
