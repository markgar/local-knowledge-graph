---
title: JuniperCloud decision record 11
date: 2026-03-17
---

# JuniperCloud decision record 11

JuniperCloud's certificate provisioning work uses batch junipercloud-11 from the west staging environment. Priya inspected 50 staging signing requests; 5 entries were set aside for manual inspection because of renewal queue delays. These are rehearsal observations, not a production approval or a change to another team's checklist.

## Rationale

For JuniperCloud, preserving the raw staging signing requests allows reviewers to distinguish renewal queue delays from changes introduced by the comparison tool. The summary worksheet alone cannot explain records that were omitted during normalization. Storage use was measured separately from review completeness.

The review packet identifies batch junipercloud-11, the west environment, and the operator who exported it. Prior weekly notes remain useful historical evidence even when a later explicit record replaces their operating choice.

## Decisions

- Retain JuniperCloud rehearsal bundles for 60 days. [key:: junipercloud-decision-11] [supersedes:: junipercloud-decision-7]
