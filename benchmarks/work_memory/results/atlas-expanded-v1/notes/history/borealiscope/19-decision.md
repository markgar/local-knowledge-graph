---
title: Borealiscope decision record 19
date: 2026-05-12
---

# Borealiscope decision record 19

Borealiscope's deployment rehearsal work uses batch borealiscope-19 from the central staging environment. Lee inspected 86 rollback checkpoint logs; 1 entries were set aside for manual inspection because of out-of-order health probes. These are rehearsal observations, not a production approval or a change to another team's checklist.

## Rationale

For Borealiscope, preserving the raw rollback checkpoint logs allows reviewers to distinguish out-of-order health probes from changes introduced by the comparison tool. The summary worksheet alone cannot explain records that were omitted during normalization. Storage use was measured separately from review completeness.

The review packet identifies batch borealiscope-19, the central environment, and the operator who exported it. Prior weekly notes remain useful historical evidence even when a later explicit record replaces their operating choice.

## Decisions

- Retain Borealiscope rehearsal bundles for 14 days. [key:: borealiscope-decision-19] [supersedes:: borealiscope-decision-15]
