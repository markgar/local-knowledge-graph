---
title: Borealiscope decision record 27
date: 2026-07-07
---

# Borealiscope decision record 27

Borealiscope's export validation work uses batch borealiscope-27 from the west staging environment. Morgan inspected 99 timestamped JSON records; 1 entries were set aside for manual inspection because of truncated permission names. These are rehearsal observations, not a production approval or a change to another team's checklist.

## Rationale

For Borealiscope, preserving the raw timestamped JSON records allows reviewers to distinguish truncated permission names from changes introduced by the comparison tool. The summary worksheet alone cannot explain records that were omitted during normalization. Storage use was measured separately from review completeness.

The review packet identifies batch borealiscope-27, the west environment, and the operator who exported it. Prior weekly notes remain useful historical evidence even when a later explicit record replaces their operating choice.

## Decisions

- Retain Borealiscope rehearsal bundles for 60 days. [key:: borealiscope-decision-27] [supersedes:: borealiscope-decision-23]
