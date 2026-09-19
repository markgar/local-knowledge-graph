---
title: Harbor decision record 27
date: 2026-07-08
---

# Harbor decision record 27

Harbor's access inventory work uses batch harbor-27 from the east staging environment. Priya inspected 93 service-account permissions; 0 entries were set aside for manual inspection because of missing role annotations. These are rehearsal observations, not a production approval or a change to another team's checklist.

## Rationale

For Harbor, preserving the raw service-account permissions allows reviewers to distinguish missing role annotations from changes introduced by the comparison tool. The summary worksheet alone cannot explain records that were omitted during normalization. Storage use was measured separately from review completeness.

The review packet identifies batch harbor-27, the east environment, and the operator who exported it. Prior weekly notes remain useful historical evidence even when a later explicit record replaces their operating choice.

## Decisions

- Retain Harbor rehearsal bundles for 30 days. [key:: harbor-decision-27] [supersedes:: harbor-decision-23]
