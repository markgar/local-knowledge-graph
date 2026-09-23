---
title: Juniper decision record 27
date: 2026-07-06
---

# Juniper decision record 27

Juniper's retention testing work uses batch juniper-27 from the west staging environment. Samira inspected 59 aged storage partitions; 2 entries were set aside for manual inspection because of late-arriving audit events. These are rehearsal observations, not a production approval or a change to another team's checklist.

## Rationale

For Juniper, preserving the raw aged storage partitions allows reviewers to distinguish late-arriving audit events from changes introduced by the comparison tool. The summary worksheet alone cannot explain records that were omitted during normalization. Storage use was measured separately from review completeness.

The review packet identifies batch juniper-27, the west environment, and the operator who exported it. Prior weekly notes remain useful historical evidence even when a later explicit record replaces their operating choice.

## Decisions

- Retain Juniper rehearsal bundles for 60 days. [key:: juniper-decision-27] [supersedes:: juniper-decision-23]
