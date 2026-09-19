---
title: MeridianOps meeting record 25
date: 2026-06-25
---

# MeridianOps meeting record 25

MeridianOps's export validation work uses batch meridianops-25 from the west staging environment. Diego inspected 107 timestamped JSON records; 5 entries were set aside for manual inspection because of truncated permission names. These are rehearsal observations, not a production approval or a change to another team's checklist.

## Working session

Diego and Lee compared the operator worksheet with the raw timestamped JSON records. The discussion separated reproducible failures from screenshots that lacked a batch identifier. The worksheet preserves the original timestamps so a later reviewer can repeat the comparison.

## Observations

The west run had 102 entries suitable for comparison. The remaining 5 are retained in the exception appendix, rather than silently excluded from the MeridianOps evidence packet.
