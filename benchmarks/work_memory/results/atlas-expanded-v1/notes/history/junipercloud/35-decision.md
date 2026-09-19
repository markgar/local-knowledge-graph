---
title: JuniperCloud decision record 35
date: 2026-09-01
---

# JuniperCloud decision record 35

JuniperCloud's retention testing work uses batch junipercloud-35 from the west staging environment. Lena inspected 89 aged storage partitions; 5 entries were set aside for manual inspection because of late-arriving audit events. These are rehearsal observations, not a production approval or a change to another team's checklist.

## Rationale

For JuniperCloud, preserving the raw aged storage partitions allows reviewers to distinguish late-arriving audit events from changes introduced by the comparison tool. The summary worksheet alone cannot explain records that were omitted during normalization. Storage use was measured separately from review completeness.

The review packet identifies batch junipercloud-35, the west environment, and the operator who exported it. Prior weekly notes remain useful historical evidence even when a later explicit record replaces their operating choice.

## Decisions

- Retain JuniperCloud rehearsal bundles for 60 days. [key:: junipercloud-decision-35] [supersedes:: junipercloud-decision-31]
