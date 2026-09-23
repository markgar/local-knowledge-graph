---
title: JuniperCloud decision record 19
date: 2026-05-12
---

# JuniperCloud decision record 19

JuniperCloud's access inventory work uses batch junipercloud-19 from the east staging environment. Diego inspected 63 service-account permissions; 5 entries were set aside for manual inspection because of missing role annotations. These are rehearsal observations, not a production approval or a change to another team's checklist.

## Rationale

For JuniperCloud, preserving the raw service-account permissions allows reviewers to distinguish missing role annotations from changes introduced by the comparison tool. The summary worksheet alone cannot explain records that were omitted during normalization. Storage use was measured separately from review completeness.

The review packet identifies batch junipercloud-19, the east environment, and the operator who exported it. Prior weekly notes remain useful historical evidence even when a later explicit record replaces their operating choice.

## Decisions

- Retain JuniperCloud rehearsal bundles for 30 days. [key:: junipercloud-decision-19] [supersedes:: junipercloud-decision-15]
