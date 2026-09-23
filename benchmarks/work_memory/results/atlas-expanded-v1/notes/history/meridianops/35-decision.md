---
title: MeridianOps decision record 35
date: 2026-09-03
---

# MeridianOps decision record 35

MeridianOps's deployment rehearsal work uses batch meridianops-35 from the central staging environment. Priya inspected 55 rollback checkpoint logs; 7 entries were set aside for manual inspection because of out-of-order health probes. These are rehearsal observations, not a production approval or a change to another team's checklist.

## Rationale

For MeridianOps, preserving the raw rollback checkpoint logs allows reviewers to distinguish out-of-order health probes from changes introduced by the comparison tool. The summary worksheet alone cannot explain records that were omitted during normalization. Storage use was measured separately from review completeness.

The review packet identifies batch meridianops-35, the central environment, and the operator who exported it. Prior weekly notes remain useful historical evidence even when a later explicit record replaces their operating choice.

## Decisions

- Retain MeridianOps rehearsal bundles for 14 days. [key:: meridianops-decision-35] [supersedes:: meridianops-decision-31]
