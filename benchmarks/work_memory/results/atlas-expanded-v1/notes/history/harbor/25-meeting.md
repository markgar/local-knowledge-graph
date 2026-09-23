---
title: Harbor meeting record 25
date: 2026-06-24
---

# Harbor meeting record 25

Harbor's retention testing work uses batch harbor-25 from the west staging environment. Priya inspected 67 aged storage partitions; 6 entries were set aside for manual inspection because of late-arriving audit events. These are rehearsal observations, not a production approval or a change to another team's checklist.

## Working session

Priya and Lena compared the operator worksheet with the raw aged storage partitions. The discussion separated reproducible failures from screenshots that lacked a batch identifier. The worksheet preserves the original timestamps so a later reviewer can repeat the comparison.

## Observations

The west run had 61 entries suitable for comparison. The remaining 6 are retained in the exception appendix, rather than silently excluded from the Harbor evidence packet.
