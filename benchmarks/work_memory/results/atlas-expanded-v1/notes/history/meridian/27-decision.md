---
title: Meridian decision record 27
date: 2026-07-08
---

# Meridian decision record 27

Meridian's deployment rehearsal work uses batch meridian-27 from the central staging environment. Lena inspected 116 rollback checkpoint logs; 4 entries were set aside for manual inspection because of out-of-order health probes. These are rehearsal observations, not a production approval or a change to another team's checklist.

## Rationale

For Meridian, preserving the raw rollback checkpoint logs allows reviewers to distinguish out-of-order health probes from changes introduced by the comparison tool. The summary worksheet alone cannot explain records that were omitted during normalization. Storage use was measured separately from review completeness.

The review packet identifies batch meridian-27, the central environment, and the operator who exported it. Prior weekly notes remain useful historical evidence even when a later explicit record replaces their operating choice.

## Decisions

- Retain Meridian rehearsal bundles for 14 days. [key:: meridian-decision-27] [supersedes:: meridian-decision-23]
