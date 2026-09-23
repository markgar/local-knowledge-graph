---
title: Juniper decision record 07
date: 2026-02-16
---

# Juniper decision record 07

Juniper's deployment rehearsal work uses batch juniper-07 from the central staging environment. Samira inspected 72 rollback checkpoint logs; 6 entries were set aside for manual inspection because of out-of-order health probes. These are rehearsal observations, not a production approval or a change to another team's checklist.

## Rationale

For Juniper, preserving the raw rollback checkpoint logs allows reviewers to distinguish out-of-order health probes from changes introduced by the comparison tool. The summary worksheet alone cannot explain records that were omitted during normalization. Storage use was measured separately from review completeness.

The review packet identifies batch juniper-07, the central environment, and the operator who exported it. Prior weekly notes remain useful historical evidence even when a later explicit record replaces their operating choice.

## Decisions

- Retain Juniper rehearsal bundles for 14 days. [key:: juniper-decision-7] [supersedes:: juniper-decision-3]
