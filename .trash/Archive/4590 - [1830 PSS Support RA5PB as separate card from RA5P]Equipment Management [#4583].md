---
issue: 4590
repo: lightriversoftware/netflex
state: OPEN
title: "[DEV][1830 PSS Support RA5PB as separate card from RA5P]Equipment Management [#4583]"
milestone: "5.4.0"
author: app/netflex-workflow-bot
assignee: dnuzzo-LR
created: 2026-06-03
updated: 2026-06-03
url: https://github.com/lightriversoftware/netflex/issues/4590
---
# Issue 4590 — [DEV][1830 PSS Support RA5PB as separate card from RA5P]Equipment Management [#4583]

[View on GitHub](https://github.com/lightriversoftware/netflex/issues/4590)

## Summary

This is a sub-task for #4583 **Parent Issue:** [FEATURE] 1830 PSS Support RA5PB as separate card from RA5P **Support Level:** ODC **Targeted Release:** 5.4.0

## Resolution / Notes

**PR:** [#4949 — Map RA5PB to RA5PB_CARD in equipment management](https://github.com/lightriversoftware/netflex/pull/4949) (branch `ra5pb-equipment-mgmt-4590`, base `main`)

### Finding
The only equipment-management gap left in `cnc/userproc/src/portprov_pss32.c`:
`ppp32_cardType_char2int()` (~line 27361) mapped only `"RA5P"` → `RA5P_CARD` via exact-match `STRCASEEQ`. An RA5PB card (type/AID prefix resolves to `"RA5PB"`) fell through to `UNTRCKD_CARD` instead of the separate `RA5PB_CARD` dno type.

### Change (one line)
```c
else if (STRCASEEQ(type, "RA5P"))  rc = RA5P_CARD;
else if (STRCASEEQ(type, "RA5PB")) rc = RA5PB_CARD;   // added
```
This enum is stamped into slot/line equipment records (called from `portprov_pss32lib.c`).

### Already in place (closed DNO #4585 / Assign #4586)
- `RA5PB_CARD` (1430) + `IS_1830_RA5P_CARD()` in `common/include/card_type.h`
- `include/dno_card.ext` + `cnc/rdb/src/rdb_card_type.c` entries
- slotaid/AID + ENT-EQPT ltypeid handling in `portprov_pss32.c` (RA5PB checked before RA5P)
- HSC mapping — RA5PB shares `HSC_RA5P` (same port-prov behavior; no new HSC class needed)

### TODO before merge
- Build userproc (`portprov_pss32.o`) — not yet compiled
- DNO → equip an RA5PB on PSS32 / PSI-L sim; confirm equipment record shows `RA5PB_CARD`

## Metadata

- **State:** OPEN
- **Created:** 2026-06-03
- **Updated:** 2026-06-03
- **Author:** app/netflex-workflow-bot
- **Assignee(s):** dnuzzo-LR
- **Milestone:** 5.4.0