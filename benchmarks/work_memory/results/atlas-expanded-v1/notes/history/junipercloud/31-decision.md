---
title: JuniperCloud decision record 31
date: 2026-08-04
---

# JuniperCloud decision record 31

JuniperCloud's archive restore work uses batch junipercloud-31 from the east staging environment. Morgan inspected 128 retained audit bundles; 1 entries were set aside for manual inspection because of duplicate object identifiers. These are rehearsal observations, not a production approval or a change to another team's checklist.

## Rationale

For JuniperCloud, preserving the raw retained audit bundles allows reviewers to distinguish duplicate object identifiers from changes introduced by the comparison tool. The summary worksheet alone cannot explain records that were omitted during normalization. Storage use was measured separately from review completeness.

The review packet identifies batch junipercloud-31, the east environment, and the operator who exported it. Prior weekly notes remain useful historical evidence even when a later explicit record replaces their operating choice.

## Decisions

- Retain JuniperCloud rehearsal bundles for 30 days. [key:: junipercloud-decision-31] [supersedes:: junipercloud-decision-27]
