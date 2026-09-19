---
title: Keystone decision record 23
date: 2026-06-08
---

# Keystone decision record 23

Keystone's retention testing work uses batch keystone-23 from the west staging environment. Morgan inspected 75 aged storage partitions; 2 entries were set aside for manual inspection because of late-arriving audit events. These are rehearsal observations, not a production approval or a change to another team's checklist.

## Rationale

For Keystone, preserving the raw aged storage partitions allows reviewers to distinguish late-arriving audit events from changes introduced by the comparison tool. The summary worksheet alone cannot explain records that were omitted during normalization. Storage use was measured separately from review completeness.

The review packet identifies batch keystone-23, the west environment, and the operator who exported it. Prior weekly notes remain useful historical evidence even when a later explicit record replaces their operating choice.

## Decisions

- Retain Keystone rehearsal bundles for 60 days. [key:: keystone-decision-23] [supersedes:: keystone-decision-19]
