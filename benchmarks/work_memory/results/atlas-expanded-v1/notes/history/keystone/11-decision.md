---
title: Keystone decision record 11
date: 2026-03-16
---

# Keystone decision record 11

Keystone's export validation work uses batch keystone-11 from the west staging environment. Diego inspected 101 timestamped JSON records; 6 entries were set aside for manual inspection because of truncated permission names. These are rehearsal observations, not a production approval or a change to another team's checklist.

## Rationale

For Keystone, preserving the raw timestamped JSON records allows reviewers to distinguish truncated permission names from changes introduced by the comparison tool. The summary worksheet alone cannot explain records that were omitted during normalization. Storage use was measured separately from review completeness.

The review packet identifies batch keystone-11, the west environment, and the operator who exported it. Prior weekly notes remain useful historical evidence even when a later explicit record replaces their operating choice.

## Decisions

- Retain Keystone rehearsal bundles for 60 days. [key:: keystone-decision-11] [supersedes:: keystone-decision-7]
