---
title: Atlascope decision record 23
date: 2026-06-09
---

# Atlascope decision record 23

Atlascope's deployment rehearsal work uses batch atlascope-23 from the central staging environment. Lena inspected 70 rollback checkpoint logs; 1 entries were set aside for manual inspection because of out-of-order health probes. These are rehearsal observations, not a production approval or a change to another team's checklist.

## Rationale

For Atlascope, preserving the raw rollback checkpoint logs allows reviewers to distinguish out-of-order health probes from changes introduced by the comparison tool. The summary worksheet alone cannot explain records that were omitted during normalization. Storage use was measured separately from review completeness.

The review packet identifies batch atlascope-23, the central environment, and the operator who exported it. Prior weekly notes remain useful historical evidence even when a later explicit record replaces their operating choice.

## Decisions

- Retain Atlascope rehearsal bundles for 14 days. [key:: atlascope-decision-23] [supersedes:: atlascope-decision-19]
