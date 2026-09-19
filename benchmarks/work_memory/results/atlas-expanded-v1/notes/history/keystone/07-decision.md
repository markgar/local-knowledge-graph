---
title: Keystone decision record 07
date: 2026-02-16
---

# Keystone decision record 07

Keystone's access inventory work uses batch keystone-07 from the east staging environment. Lena inspected 49 service-account permissions; 2 entries were set aside for manual inspection because of missing role annotations. These are rehearsal observations, not a production approval or a change to another team's checklist.

## Rationale

For Keystone, preserving the raw service-account permissions allows reviewers to distinguish missing role annotations from changes introduced by the comparison tool. The summary worksheet alone cannot explain records that were omitted during normalization. Storage use was measured separately from review completeness.

The review packet identifies batch keystone-07, the east environment, and the operator who exported it. Prior weekly notes remain useful historical evidence even when a later explicit record replaces their operating choice.

## Decisions

- Retain Keystone rehearsal bundles for 30 days. [key:: keystone-decision-7] [supersedes:: keystone-decision-3]
