---
title: JuniperCloud decision record 27
date: 2026-07-07
---

# JuniperCloud decision record 27

JuniperCloud's handover review work uses batch junipercloud-27 from the central staging environment. Lee inspected 76 on-call runbook examples; 5 entries were set aside for manual inspection because of unmapped escalation contacts. These are rehearsal observations, not a production approval or a change to another team's checklist.

## Rationale

For JuniperCloud, preserving the raw on-call runbook examples allows reviewers to distinguish unmapped escalation contacts from changes introduced by the comparison tool. The summary worksheet alone cannot explain records that were omitted during normalization. Storage use was measured separately from review completeness.

The review packet identifies batch junipercloud-27, the central environment, and the operator who exported it. Prior weekly notes remain useful historical evidence even when a later explicit record replaces their operating choice.

## Decisions

- Retain JuniperCloud rehearsal bundles for 14 days. [key:: junipercloud-decision-27] [supersedes:: junipercloud-decision-23]
