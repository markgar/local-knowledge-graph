---
title: JuniperCloud decision record 03
date: 2026-01-20
---

# JuniperCloud decision record 03

JuniperCloud's calendar reconciliation work uses batch junipercloud-03 from the central staging environment. Diego inspected 128 review invitation exports; 5 entries were set aside for manual inspection because of timezone conversion. These are rehearsal observations, not a production approval or a change to another team's checklist.

## Rationale

For JuniperCloud, preserving the raw review invitation exports allows reviewers to distinguish timezone conversion from changes introduced by the comparison tool. The summary worksheet alone cannot explain records that were omitted during normalization. Storage use was measured separately from review completeness.

The review packet identifies batch junipercloud-03, the central environment, and the operator who exported it. Prior weekly notes remain useful historical evidence even when a later explicit record replaces their operating choice.

## Decisions

- Retain JuniperCloud rehearsal bundles for 14 days. [key:: junipercloud-decision-3]
