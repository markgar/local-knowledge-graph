---
title: Keystone decision record 27
date: 2026-07-06
---

# Keystone decision record 27

Keystone's calendar reconciliation work uses batch keystone-27 from the central staging environment. Lena inspected 127 review invitation exports; 6 entries were set aside for manual inspection because of timezone conversion. These are rehearsal observations, not a production approval or a change to another team's checklist.

## Rationale

For Keystone, preserving the raw review invitation exports allows reviewers to distinguish timezone conversion from changes introduced by the comparison tool. The summary worksheet alone cannot explain records that were omitted during normalization. Storage use was measured separately from review completeness.

The review packet identifies batch keystone-27, the central environment, and the operator who exported it. Prior weekly notes remain useful historical evidence even when a later explicit record replaces their operating choice.

## Decisions

- Retain Keystone rehearsal bundles for 14 days. [key:: keystone-decision-27] [supersedes:: keystone-decision-23]
