---
name: work-package
description: Organize a Local Knowledge Graph package issue into a file-based implementation spec, independent critic review, and a full approved spec published on the issue. Use when starting or resuming a package design, preparing a spec for criticism, or handing an approved design to implementation. Stop for approval before coding; do not treat skill invocation as permission to implement or merge.
---

# Work package

Follow the [contributor workflow](../../../CONTRIBUTING.md#starting-work-issue-file-spec-critic-implementation).
This skill is the agent checklist, not a second roadmap.

## Input and boundaries

- Accept a package issue number or URL and, when resuming, its spec file and
  published spec comment.
  Example: `Use /work-package for issue #24. Design and critic review only.`
- If no issue is selected, inspect the
  [tracking issue](https://github.com/markgar/local-knowledge-graph/issues/28),
  present ready choices, and ask the user to select one. Do not start every package.
- Default to design and review only. Do not change runtime code, schemas, tests,
  dependencies or repository planning files, start implementation, close issues,
  commit, or merge merely because this skill was invoked.
- Issues own scope, dependencies and progress. Session-local Markdown files are
  working designs. The full approved spec on the issue is the durable design record.
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

One work session can own design, revisions and authorized implementation end to
end: use the current session or one app subsession if the work is delegated.
Do not create separate app sessions for each phase. Independent criticism needs
fresh agent context, not a separate app session or code worktree: use a
general-purpose subagent with a read-only review assignment. Any delegated work
inherits the same approval boundaries; a design request does not authorize
implementation.

Verify the repository and owning issue before publishing. An ordinary issue
comment does not require an issue-linked app session or the special issue-artifact
feature; do not create another session or block on enabling that feature.

## 2. Write or resume the spec file

Use the actual session artifact directory outside the tracked repository, with
a package-based filename such as `files/E1-spec.md`. Resolve and report its
absolute path; do not assume `files/` means a directory under the repo. If no
artifact directory is provided, ask for an appropriate external location.
Resume the existing file rather than creating competing drafts.

Include these sections, using `Not applicable` with a reason where appropriate:

| Section | Required content |
| --- | --- |
| Identity and status | Package/issue link, revision, inspected commit, draft/reviewed/approved status, published spec comment URL/ID if one exists. |
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

Use a general-purpose subagent in fresh context to review the file, not an
author's summary. Instruct it to work read-only in the current worktree; do not
create an app session or worktree just for critique.
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

After approval, publish the **full spec**, including revision, inspected baseline,
critic findings/dispositions and user approval record, as an ordinary comment on
the owning package issue. This is the default workflow, not an optional extra:
do not ask again whether the approved spec should go on the issue. A summary,
local path or promise to publish later is not a substitute for the full content.

Retain the comment URL and ID in the local working file. For later approved
revisions, update that same designated spec comment rather than creating competing
current versions; retain a concise revision/approval history. Read the existing
comment before editing and preserve any concurrent changes. Never label an
unapproved revision approved or replace the approved record with a draft.

Read the published comment back to verify the full approved content is present
on the correct issue before implementation or handoff. If publication is blocked,
retain the local file and report the blocker; do not claim a durable handoff.
Do not archive the work session before its design and review record are preserved.

## 5. Stop or hand off

Default completion is a reviewed spec awaiting approval, or an approved spec
published to its issue, with implementation not started. Report the file/revision,
issue/spec-comment reference, review state and blockers concisely.

Only after an explicit instruction to implement the approved slice, continue in
the same work session using its issue, approved spec comment, inspected baseline,
acceptance criteria and stopping point. If a handoff is actually needed, provide
that same context; a new session is not required. Reconcile changes on main first.
Follow `CONTRIBUTING.md` for tests, independent complete-diff review, fix review
and merge checks. Dispatch CI only
for code-affecting changes, not documentation, skill text or instructions alone.
Update package progress after each delivered slice; close only on actual acceptance evidence
and update the tracker. Do not infer merge or issue-closure authority from a
design-only request.

## 6. Clean up the issue

Before handing back work, make the package issue reflect its actual state: link
the current spec comment and relevant PRs, record review outcomes and decisions,
update completed checklist items, and state remaining work or blockers clearly.
Correct stale status and superseded guidance without deleting useful history or
another contributor's work. Keep one clearly identified current design record.

After implementation, record delivered behavior and acceptance evidence. Close
the issue only when its completion criteria are met and closure is authorized;
partial work stays open. Update the tracking issue's package status and affected
dependencies so the two agree. A design-only handoff must not mark implementation
complete.
