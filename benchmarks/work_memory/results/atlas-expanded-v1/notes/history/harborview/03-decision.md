---
title: Harborview decision record 03
date: 2026-01-22
---

# Harborview decision record 03

Harborview's export validation work uses batch harborview-03 from the west staging environment. Lee inspected 71 timestamped JSON records; 3 entries were set aside for manual inspection because of truncated permission names. These are rehearsal observations, not a production approval or a change to another team's checklist.

## Rationale

For Harborview, preserving the raw timestamped JSON records allows reviewers to distinguish truncated permission names from changes introduced by the comparison tool. The summary worksheet alone cannot explain records that were omitted during normalization. Storage use was measured separately from review completeness.

The review packet identifies batch harborview-03, the west environment, and the operator who exported it. Prior weekly notes remain useful historical evidence even when a later explicit record replaces their operating choice.

## Decisions

- Retain Harborview rehearsal bundles for 60 days. [key:: harborview-decision-3]
