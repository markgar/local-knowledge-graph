---
title: Harborview decision record 31
date: 2026-08-06
---

# Harborview decision record 31

Harborview's deployment rehearsal work uses batch harborview-31 from the central staging environment. Diego inspected 71 rollback checkpoint logs; 7 entries were set aside for manual inspection because of out-of-order health probes. These are rehearsal observations, not a production approval or a change to another team's checklist.

## Rationale

For Harborview, preserving the raw rollback checkpoint logs allows reviewers to distinguish out-of-order health probes from changes introduced by the comparison tool. The summary worksheet alone cannot explain records that were omitted during normalization. Storage use was measured separately from review completeness.

The review packet identifies batch harborview-31, the central environment, and the operator who exported it. Prior weekly notes remain useful historical evidence even when a later explicit record replaces their operating choice.

## Decisions

- Retain Harborview rehearsal bundles for 14 days. [key:: harborview-decision-31] [supersedes:: harborview-decision-27]
