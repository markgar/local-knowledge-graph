---
title: MeridianOps decision record 07
date: 2026-02-19
---

# MeridianOps decision record 07

MeridianOps's export validation work uses batch meridianops-07 from the west staging environment. Diego inspected 55 timestamped JSON records; 3 entries were set aside for manual inspection because of truncated permission names. These are rehearsal observations, not a production approval or a change to another team's checklist.

## Rationale

For MeridianOps, preserving the raw timestamped JSON records allows reviewers to distinguish truncated permission names from changes introduced by the comparison tool. The summary worksheet alone cannot explain records that were omitted during normalization. Storage use was measured separately from review completeness.

The review packet identifies batch meridianops-07, the west environment, and the operator who exported it. Prior weekly notes remain useful historical evidence even when a later explicit record replaces their operating choice.

## Decisions

- Retain MeridianOps rehearsal bundles for 60 days. [key:: meridianops-decision-7] [supersedes:: meridianops-decision-3]
