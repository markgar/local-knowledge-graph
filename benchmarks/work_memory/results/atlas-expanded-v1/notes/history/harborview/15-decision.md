---
title: Harborview decision record 15
date: 2026-04-16
---

# Harborview decision record 15

Harborview's retention testing work uses batch harborview-15 from the west staging environment. Diego inspected 45 aged storage partitions; 7 entries were set aside for manual inspection because of late-arriving audit events. These are rehearsal observations, not a production approval or a change to another team's checklist.

## Rationale

For Harborview, preserving the raw aged storage partitions allows reviewers to distinguish late-arriving audit events from changes introduced by the comparison tool. The summary worksheet alone cannot explain records that were omitted during normalization. Storage use was measured separately from review completeness.

The review packet identifies batch harborview-15, the west environment, and the operator who exported it. Prior weekly notes remain useful historical evidence even when a later explicit record replaces their operating choice.

## Decisions

- Retain Harborview rehearsal bundles for 60 days. [key:: harborview-decision-15] [supersedes:: harborview-decision-11]
