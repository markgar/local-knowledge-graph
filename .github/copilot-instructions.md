# Local Knowledge Graph

## Default: lean personal-use MVP

- This is a personal-use MVP. Optimize for getting a useful end-to-end capability into the user's hands with the smallest coherent change.
- Solve the observed problem and its immediate correctness requirements. Do not add production-scale orchestration, generalized frameworks, speculative recovery systems, diagnostics redesigns, storage-format changes, or new public abstractions unless the requested capability cannot work correctly without them.
- Prefer extending an existing path over replacing adjacent systems. A natural one-item case using the same batch implementation is not compatibility scaffolding.
- Keep the blast radius minimal. Every changed subsystem, public contract, durable format, command, and operational mechanism must be necessary for the requested outcome. If it is merely useful, defensive, or potentially needed later, leave it out.
- Treat critic findings as questions about the requested scope, not automatic requirements to expand it. Fix in-scope correctness defects; reject or defer findings that require unrelated capabilities.
- During design and review, actively remove speculative scope. State the simplest measurable acceptance test first and stop when it is satisfied.
- If the smallest correct solution requires a broader redesign, explain exactly why and obtain explicit user approval before expanding the package.

## Default: no backward compatibility

- This is experimental software with no production consumers. Prefer the clearest direct replacement over compatibility layers.
- Do not add migrations, dual reads/writes, legacy aliases, deprecated paths, parallel old/new output shapes, or opt-in compatibility modes unless the user explicitly requests them for a named consumer.
- Treat compatibility work as a scope expansion that requires explicit approval. Update tests, fixtures, examples, and docs to the new behavior instead.
- During design and code review, actively look for compatibility scaffolding. Report any unapproved compatibility effort explicitly and treat it as a regression or design defect, not defensive engineering.
- Fresh-store reload is the normal recovery path and is cheap. Prefer it over compatibility code.
- No backward compatibility does not authorize unrelated redesign. Change or remove an existing interface only when the requested capability requires it; otherwise keep useful behavior on the same implementation path without aliases, fallbacks, or duplicate old/new machinery.

## Start here

- Read `README.md` for usage, `SPEC.md` for architecture, `CONTRACTS.md` for executable APIs, and `CONTRIBUTING.md` for development and validation rules.
- Follow the selected open issue and its approved requirements. Issues own scope and acceptance; milestones only group work.
- Keep repository documentation about behavior that exists. Do not advertise planned APIs as shipped.

## Architecture

- SQLite `evidence-store/5` is authoritative for exact text and revisions, identities, schema revisions, entities, assertions, evidence, receipts, and history.
- Ladybug is an optional, rebuildable graph projection. It contains no unique authored truth, and leftover graph files never prove freshness.
- Ownership:
  - `evidence/`: intake, authorization, exact evidence.
  - `knowledge/`: schema and authored knowledge.
  - `indexing/`: passages, vectors, and search.
  - `processing/`: control plane.
  - `query/`: supported canonical queries.
  - `graph/`: private exact-scope Ladybug projection and fixed native operations.
  - `diagnostics/`: bounded reports.
  - `models/`: values, not execution.
- The Markdown demonstration is separate from the canonical service. Do not route canonical schema or graph work through its manifests, database, or `src/kg/schema.sql`.

## Current capability boundaries

- Supplied-document intake, indexing, search, and explicitly authored knowledge exist.
- Automatic knowledge extraction, a natural-language query planner, live connectors, public retained graph inspection, and general graph joins do not.
- Native graph queries are typed, cited, fixed one-hop operations. Preserve exact scopes, full association proofs, distinct submitted-ID counts, observer/final fencing, budgets, and canonical receipts even when display is truncated or graph execution fails.
- The optional runtime is `ladybug==0.20.4` on macOS 15+ ARM64 with CPython 3.12. Its 256 MiB buffer and two threads are not an RSS or crash boundary; native failures can terminate the host. Base imports and search must work without Ladybug.

## Data and compatibility

- Preserve deterministic behavior, immutable provenance, exact anchors, corpus isolation, and configuration-driven domain logic.
- Never silently alter or delete an incompatible store.
- Use an explicit fresh store or reload when a canonical format change requires it. Preserve data safety within a supported store; do not preserve obsolete interfaces by default.

## Agent and package workflows

- When operating the KG, load `.github/skills/use-knowledge-graph/SKILL.md`. Update both matching skill copies whenever public behavior changes.
- When designing a package, load `.github/skills/work-package/SKILL.md`.
- Store working specs outside the repository. Obtain independent criticism and explicit approval before coding.
- Publish the full approved spec and review record on the owning issue, verify it, and update that same comment for approved revisions. Do not block on the special issue-artifact feature.

## Dependencies and validation

- Installing declared dependencies, including dev and needed extras, is pre-approved. Use configured sources and existing constraints; never bypass network controls.
- Expensive model, native, capacity, or full-suite workloads need separate approval.
- GitHub Actions is manual-only. Do not dispatch it unless explicitly authorized.
- Do not run the full Python suite for documentation, skill-text, or instruction-only changes. Inspect the diff and relevant links instead.
- `tests/` contains unit, CLI, ingestion, acceptance, and benchmark coverage. `corpora/` contains manifests and synthetic fixtures; `examples/` contains client usage; `benchmarks/` contains evaluation tooling and results.
