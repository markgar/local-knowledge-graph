---
title: Harbor decision record 31
date: 2026-08-05
---

# Harbor decision record 31

Harbor's export validation work uses batch harbor-31 from the west staging environment. Lena inspected 54 timestamped JSON records; 4 entries were set aside for manual inspection because of truncated permission names. These are rehearsal observations, not a production approval or a change to another team's checklist.

## Rationale

For Harbor, preserving the raw timestamped JSON records allows reviewers to distinguish truncated permission names from changes introduced by the comparison tool. The summary worksheet alone cannot explain records that were omitted during normalization. Storage use was measured separately from review completeness.

The review packet identifies batch harbor-31, the west environment, and the operator who exported it. Prior weekly notes remain useful historical evidence even when a later explicit record replaces their operating choice.

## Decisions

- Retain Harbor rehearsal bundles for 60 days. [key:: harbor-decision-31] [supersedes:: harbor-decision-27]
