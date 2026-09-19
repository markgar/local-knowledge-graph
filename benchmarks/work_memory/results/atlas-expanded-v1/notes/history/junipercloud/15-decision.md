---
title: JuniperCloud decision record 15
date: 2026-04-14
---

# JuniperCloud decision record 15

JuniperCloud's deployment rehearsal work uses batch junipercloud-15 from the central staging environment. Morgan inspected 102 rollback checkpoint logs; 1 entries were set aside for manual inspection because of out-of-order health probes. These are rehearsal observations, not a production approval or a change to another team's checklist.

## Rationale

For JuniperCloud, preserving the raw rollback checkpoint logs allows reviewers to distinguish out-of-order health probes from changes introduced by the comparison tool. The summary worksheet alone cannot explain records that were omitted during normalization. Storage use was measured separately from review completeness.

The review packet identifies batch junipercloud-15, the central environment, and the operator who exported it. Prior weekly notes remain useful historical evidence even when a later explicit record replaces their operating choice.

## Decisions

- Retain JuniperCloud rehearsal bundles for 14 days. [key:: junipercloud-decision-15] [supersedes:: junipercloud-decision-11]
