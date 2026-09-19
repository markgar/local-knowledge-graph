---
title: JuniperCloud decision record 23
date: 2026-06-09
---

# JuniperCloud decision record 23

JuniperCloud's export validation work uses batch junipercloud-23 from the west staging environment. Samira inspected 115 timestamped JSON records; 1 entries were set aside for manual inspection because of truncated permission names. These are rehearsal observations, not a production approval or a change to another team's checklist.

## Rationale

For JuniperCloud, preserving the raw timestamped JSON records allows reviewers to distinguish truncated permission names from changes introduced by the comparison tool. The summary worksheet alone cannot explain records that were omitted during normalization. Storage use was measured separately from review completeness.

The review packet identifies batch junipercloud-23, the west environment, and the operator who exported it. Prior weekly notes remain useful historical evidence even when a later explicit record replaces their operating choice.

## Decisions

- Retain JuniperCloud rehearsal bundles for 60 days. [key:: junipercloud-decision-23] [supersedes:: junipercloud-decision-19]
