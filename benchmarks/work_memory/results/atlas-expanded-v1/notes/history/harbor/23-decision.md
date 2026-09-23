---
title: Harbor decision record 23
date: 2026-06-10
---

# Harbor decision record 23

Harbor's deployment rehearsal work uses batch harbor-23 from the central staging environment. Lee inspected 41 rollback checkpoint logs; 4 entries were set aside for manual inspection because of out-of-order health probes. These are rehearsal observations, not a production approval or a change to another team's checklist.

## Rationale

For Harbor, preserving the raw rollback checkpoint logs allows reviewers to distinguish out-of-order health probes from changes introduced by the comparison tool. The summary worksheet alone cannot explain records that were omitted during normalization. Storage use was measured separately from review completeness.

The review packet identifies batch harbor-23, the central environment, and the operator who exported it. Prior weekly notes remain useful historical evidence even when a later explicit record replaces their operating choice.

## Decisions

- Retain Harbor rehearsal bundles for 14 days. [key:: harbor-decision-23] [supersedes:: harbor-decision-19]
