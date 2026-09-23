---
title: Meridian decision record 11
date: 2026-03-18
---

# Meridian decision record 11

Meridian's retention testing work uses batch meridian-11 from the west staging environment. Diego inspected 90 aged storage partitions; 4 entries were set aside for manual inspection because of late-arriving audit events. These are rehearsal observations, not a production approval or a change to another team's checklist.

## Rationale

For Meridian, preserving the raw aged storage partitions allows reviewers to distinguish late-arriving audit events from changes introduced by the comparison tool. The summary worksheet alone cannot explain records that were omitted during normalization. Storage use was measured separately from review completeness.

The review packet identifies batch meridian-11, the west environment, and the operator who exported it. Prior weekly notes remain useful historical evidence even when a later explicit record replaces their operating choice.

## Decisions

- Retain Meridian rehearsal bundles for 60 days. [key:: meridian-decision-11] [supersedes:: meridian-decision-7]
