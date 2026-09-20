---
name: work-package
description: Organize a Local Knowledge Graph package issue into a file-based implementation spec, independent critic review, and durable issue artifact. Use when starting or resuming a package design, preparing a spec for criticism, or handing an approved design to implementation. Stop for approval before coding; do not treat skill invocation as permission to implement or merge.
---

# Work package

Follow the [contributor workflow](../../../CONTRIBUTING.md#starting-work-issue-file-spec-critic-implementation).
This skill is the agent checklist, not a second roadmap.

## Input and boundaries

- Accept a package issue number or URL and, when resuming, its spec file/artifact.
  Example: `Use /work-package for issue #24. Design and critic review only.`
- If no issue is selected, inspect the
  [tracking issue](https://github.com/markgar/local-knowledge-graph/issues/28),
  present ready choices, and ask the user to select one. Do not start every package.
- Default to design and review only. Do not change runtime code, schemas, tests,
  dependencies or repository planning files, start implementation, close issues,
  commit, or merge merely because this skill was invoked.
- Issues own scope, dependencies and progress. Session-local Markdown files are
  working designs. The approved issue artifact is the durable design record.
  Repository docs describe delivered behavior and development procedures.

## 1. Establish the work

Read the live package issue, its dependencies, the tracking issue's shared
requirements/acceptance cases, and `CONTRIBUTING.md`. Read relevant current code,
`SPEC.md` and `CONTRACTS.md`; do not assume proposed services exist.

Confirm which prerequisites block implementation versus which allow design in
parallel. Report missing decisions or blocked dependencies rather than marking
the package ready yourself. Inspect worktree changes and refresh the main ref
without overwriting work or switching to the main checkout. Record the inspected
commit and any relevant unmerged changes in the spec.

Use app subsessions for scoped design, independent criticism and, when authorized,
implementation work. Keep the parent session coordinating results and user
decisions. Reuse suitable existing subsessions rather than duplicating work; give
each the issue, relevant files/artifacts, scope, expected output and stop condition.
Use the orchestration workflow for session creation and handoffs, without a fixed
session hierarchy or unnecessary fan-out. Subsessions inherit the same approval
boundaries; a design request does not authorize implementation.

Use an issue-linked session when publishing issue artifacts, and verify it targets
the selected issue. If session tools are unavailable, report that limitation;
never attach to an unrelated issue.

## 2. Write or resume the spec file

Use the actual session artifact directory outside the tracked repository, with
a package-based filename such as `files/E1-spec.md`. Resolve and report its
absolute path; do not assume `files/` means a directory under the repo. If no
artifact directory is provided, ask for an appropriate external location.
Resume the existing file rather than creating competing drafts.

Include these sections, using `Not applicable` with a reason where appropriate:

| Section | Required content |
| --- | --- |
| Identity and status | Package/issue link, revision, inspected commit, draft/reviewed/approved status, existing artifact reference. |
| Goal and boundaries | Deliverable, non-goals, dependencies and unresolved prerequisites; links to shared requirements. |
| Current behavior | Relevant code paths, existing contracts and concrete gaps; distinguish validation from service enforcement. |
| Proposed design | Public APIs, types, storage/schema, ownership and service boundaries. |
| Compatibility and migration | Existing IDs, exact evidence, metadata/history, public behavior and migration/recovery effects. |
| State and failure handling | Atomicity, concurrency, retries, stale work, authorization, partial results and explicit error behavior. |
| Acceptance and validation | Applicable issue cases, actual service tests, negative/race/recovery cases and validation commands. |
| Delivery slices | Reviewable PR-sized outcomes, owned surfaces, dependencies and stopping points. |
| Decisions and review | Alternatives, open questions, critic findings/dispositions and approval tied to the exact spec revision. |

Link the shared requirements instead of copying the entire roadmap. Do not invent
permissions, performance results, passing acceptance evidence or completed work.
Preserve exact provenance, corpus isolation, authored gold and benchmark results.

## 3. Obtain independent criticism

Use an independent, read-only critic subsession to review the file, not an
author's summary.
Provide the absolute path, revision, issue and shared requirements, code baseline,
scope, and an explicit stop after reporting findings. If the critic cannot access
the file, supply that exact revision through supported artifact/file transfer.

Request concrete correctness, compatibility, migration, transaction/concurrency,
failure-handling, scope and testability gaps, with spec-section/code references
and severity. The critic must not implement or edit the spec.

Address findings in the same file and record dispositions. Reuse the critic for
fix review when possible. Re-review material revisions; do not claim an old review
clears a changed design. If independent review is unavailable, report that gate
as incomplete rather than substituting self-review. Ask the user to resolve
material choices or persistent disagreements; avoid endless review loops.

## 4. Approval and durable publication

Present the spec path/revision, review outcome and remaining decisions. Ask for
explicit approval of the reviewed revision. A critic's approval is not user
authorization to code, and approving a design is not permission to merge.

After approval, publish the full spec and review/approval record as a named
artifact on the selected package issue. Use the issue-artifact tools when
available and verify the session is linked to that issue before writing. Retain
the returned artifact identity; update that same artifact for later approved
revisions, rather than creating duplicates. Never label an unapproved revision
approved or overwrite the approved artifact with a draft.

Verify publication and record the artifact reference on the issue. An absolute
local path alone is not durable publication. If publishing is blocked, retain the
file and report the blocker. Do not archive the design session before its design
and review record are preserved.

## 5. Stop or hand off

Default completion is a reviewed spec awaiting approval, or an approved spec
published to its issue, with implementation not started. Report the file/revision,
issue/artifact reference, review state and blockers concisely.

Only after an explicit instruction to implement the approved slice, hand off its
issue, approved artifact, inspected baseline, acceptance criteria and stopping
point. Reconcile changes on main first. Follow `CONTRIBUTING.md` for tests,
independent complete-diff review, fix review and merge checks. Update package
progress after each delivered slice; close only on actual acceptance evidence
and update the tracker. Do not infer merge or issue-closure authority from a
design-only request.

## 6. Clean up the issue

Before handing back work, make the package issue reflect its actual state: link
the current spec artifact and relevant PRs, record review outcomes and decisions,
update completed checklist items, and state remaining work or blockers clearly.
Correct stale status and superseded guidance without deleting useful history or
another contributor's work. Keep one clearly identified current design artifact.

After implementation, record delivered behavior and acceptance evidence. Close
the issue only when its completion criteria are met and closure is authorized;
partial work stays open. Update the tracking issue's package status and affected
dependencies so the two agree. A design-only handoff must not mark implementation
complete.
