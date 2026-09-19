---
title: Borealis decision record 11
date: 2026-03-16
---

# Borealis decision record 11

Borealis's deployment rehearsal work uses batch borealis-11 from the central staging environment. Morgan inspected 56 rollback checkpoint logs; 6 entries were set aside for manual inspection because of out-of-order health probes. These are rehearsal observations, not a production approval or a change to another team's checklist.

## Rationale

For Borealis, preserving the raw rollback checkpoint logs allows reviewers to distinguish out-of-order health probes from changes introduced by the comparison tool. The summary worksheet alone cannot explain records that were omitted during normalization. Storage use was measured separately from review completeness.

The review packet identifies batch borealis-11, the central environment, and the operator who exported it. Prior weekly notes remain useful historical evidence even when a later explicit record replaces their operating choice.

## Decisions

- Retain Borealis rehearsal bundles for 14 days. [key:: borealis-decision-11] [supersedes:: borealis-decision-7]
