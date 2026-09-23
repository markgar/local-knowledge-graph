---
title: Borealiscope decision record 03
date: 2026-01-20
---

# Borealiscope decision record 03

Borealiscope's retention testing work uses batch borealiscope-03 from the west staging environment. Lee inspected 60 aged storage partitions; 1 entries were set aside for manual inspection because of late-arriving audit events. These are rehearsal observations, not a production approval or a change to another team's checklist.

## Rationale

For Borealiscope, preserving the raw aged storage partitions allows reviewers to distinguish late-arriving audit events from changes introduced by the comparison tool. The summary worksheet alone cannot explain records that were omitted during normalization. Storage use was measured separately from review completeness.

The review packet identifies batch borealiscope-03, the west environment, and the operator who exported it. Prior weekly notes remain useful historical evidence even when a later explicit record replaces their operating choice.

## Decisions

- Retain Borealiscope rehearsal bundles for 60 days. [key:: borealiscope-decision-3]
