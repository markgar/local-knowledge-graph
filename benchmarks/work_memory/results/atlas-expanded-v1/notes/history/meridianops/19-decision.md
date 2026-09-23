---
title: MeridianOps decision record 19
date: 2026-05-14
---

# MeridianOps decision record 19

MeridianOps's retention testing work uses batch meridianops-19 from the west staging environment. Morgan inspected 120 aged storage partitions; 7 entries were set aside for manual inspection because of late-arriving audit events. These are rehearsal observations, not a production approval or a change to another team's checklist.

## Rationale

For MeridianOps, preserving the raw aged storage partitions allows reviewers to distinguish late-arriving audit events from changes introduced by the comparison tool. The summary worksheet alone cannot explain records that were omitted during normalization. Storage use was measured separately from review completeness.

The review packet identifies batch meridianops-19, the west environment, and the operator who exported it. Prior weekly notes remain useful historical evidence even when a later explicit record replaces their operating choice.

## Decisions

- Retain MeridianOps rehearsal bundles for 60 days. [key:: meridianops-decision-19] [supersedes:: meridianops-decision-15]
