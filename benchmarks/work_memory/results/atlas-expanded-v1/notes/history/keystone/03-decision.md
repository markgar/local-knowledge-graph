---
title: Keystone decision record 03
date: 2026-01-19
---

# Keystone decision record 03

Keystone's deployment rehearsal work uses batch keystone-03 from the central staging environment. Priya inspected 88 rollback checkpoint logs; 6 entries were set aside for manual inspection because of out-of-order health probes. These are rehearsal observations, not a production approval or a change to another team's checklist.

## Rationale

For Keystone, preserving the raw rollback checkpoint logs allows reviewers to distinguish out-of-order health probes from changes introduced by the comparison tool. The summary worksheet alone cannot explain records that were omitted during normalization. Storage use was measured separately from review completeness.

The review packet identifies batch keystone-03, the central environment, and the operator who exported it. Prior weekly notes remain useful historical evidence even when a later explicit record replaces their operating choice.

## Decisions

- Retain Keystone rehearsal bundles for 14 days. [key:: keystone-decision-3]
