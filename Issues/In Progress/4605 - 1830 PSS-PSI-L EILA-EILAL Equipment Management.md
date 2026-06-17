---
issue: 4605
repo: lightriversoftware/netflex
state: OPEN
status: In Progress
sprint: Sprint 14
title: 1830 PSS/PSI-L new equipment at ODC (MLFSB, EILA, EILAL) — Equipment Management EILA/EILAL
type: Development (enhancement)
release: 5.4.0
milestone: 5.4.0
author: netflex-workflow-bot
assignee: dnuzzo-LR
created: 2026-06-03
updated: 2026-06-03
url: https://github.com/lightriversoftware/netflex/issues/4605
---

# Issue 4605 — [DEV][1830 PSS/PSI-L new equipment at ODC: MLFSB, EILA, EILAL] Equipment Management - EILA/EILAL [#3053]

[View on GitHub](https://github.com/lightriversoftware/netflex/issues/4605)

## Summary

Equipment Management sub-task of parent feature #3053 (1830 PSS/PSI-L new equipment at ODC: MLFSB, EILA, EILAL). Scope is the equipment-management support for the **EILA / EILAL** cards. Support level ODC, targeted at release 5.4.0.

## Resolution / Notes

No resolution posted yet.

## ⚠️ Blocked — needs OEM documentation

The code plumbing exists, but the equipment-management **forms** (which forms apply to an EILA/EILAL amp, and per form the fields + allowed/enumerated values) are **OEM ground truth not present in the repo or anywhere in the #3053 issue tree** (checked all 16 sub-issues; only attachment is the Oracle Material Submittal parts list on #3053 — not a form spec).

**Need one of:**
- **Nokia 1830 PSS R23.12 docs** (cards-file tags these cards `R23.12`):
  - *TL1 Commands and Messages Reference* — `ENT/ED/RTRV-EQPT` parameters + enumerations for EILA/EILAL (maps most directly to form fields + values).
  - *Equipment / Card Description* guide — EILA/EILAL **Equalized In-Line Amplifier** chapter (modes, gain/tilt ranges, OSC vs OSCSFP).
  - Search: `1830 PSS R23.12 TL1 Commands Messages EILA` / Nokia customer portal R23.12 doc set.
- **or** capture from the OEM EMS / raw TL1 against the lab shelves `10.9.101.165 / .168 / .171` (TX FBN) — `RTRV-EQPT` etc. and walk the OEM GUI forms recording each field's options.

→ Action: Dan to locate the Nokia R23.12 equipment/TL1 doc. Once obtained, extract per-form field + value tables for EILA/EILAL.

### Code context already in place (from sibling sub-tasks)
- Enums `EILA_CARD 1411` / `EILAL_CARD 1412` + `IS_EILA(x)` macro — `common/include/card_type.h`.
- Cards file entries — `3b2/data/alc1830_card` (#3992), `nokia_psi_4l_8l_card` (#3990).
- rdb mapping `RDB_CARD_CLASS_AMP` — `cnc/rdb/src/rdb_card_type.c:282-283`.
- DNO #4593 / Assign #4551 / otn_port #4618 / EV+LV #4657,#4681 — merged.
- Remaining one-line gap (template = your RA5PB commit `f510b3cc`, #4590): add `EILA`/`EILAL` branches in `ppp32_cardType_char2int()` — `cnc/userproc/src/portprov_pss32.c` (~L27343–27366).
- Facility/AID model (from #4546): `EILA-[1-30]-[2-17]-LINE[1,2]IN/OUT` (OTS), `OSC[1,2]`, `OSCSFP[1,2]` (LINE).

## Attachments

None in the issue tree relevant to forms. (Parent #3053 has the Oracle Material Submittal spreadsheet — parts list only.)

## Metadata

- **Issue Type:** Development (enhancement), sub-task
- **Parent Issue:** #3053 (1830 PSS/PSI-L new equipment at ODC)
- **Support Level:** ODC
- **Targeted Release:** 5.4.0
- **Project Status:** In Progress (Sprint 14)
