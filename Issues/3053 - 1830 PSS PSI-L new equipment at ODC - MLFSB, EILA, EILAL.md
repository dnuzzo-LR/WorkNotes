---
issue: 3053
repo: lightriversoftware/netflex
state: OPEN
title: "[79][FEATURE] 1830 PSS/PSI-L new equipment at ODC: MLFSB, EILA, EILAL"
customer: Oracle
type: Development (enhancement)
category: New Hardware
support_level: ODC
release: "5.4.0"
milestone: "5.4.0"
author: tmasse-LR
created: 2026-03-31
updated: 2026-06-03
url: https://github.com/lightriversoftware/netflex/issues/3053
---

# Issue 3053 — [79][FEATURE] 1830 PSS/PSI-L new equipment at ODC: MLFSB, EILA, EILAL

[View on GitHub](https://github.com/lightriversoftware/netflex/issues/3053)

## Summary

LightRiver Services needs netFLEX to support provisioning of three new Nokia 1830 PSS / PSI-L cards — **MLFSB**, **EILA**, and **EILAL** — for the Oracle network. LR is the customer and will use netFLEX to manage the Oracle deployment, so upgrades and "friendly" handling can be coordinated as needed.

The equipment is being staged in the lab during Q2 so development and QA can happen before the deployment is installed at Oracle. Tim Masse requested this land in **5.4.0** (ideally a patch), with **pre-GA 5.4.1** as a fallback.

Several related 1830 PSS cards are already supported by netFLEX in the Oracle plan: IRDM32, IRDM32L, OMDCL, AWBILA, RA5PB.

## Observed Behavior

This is an enhancement / new-hardware request, not a defect — no broken behavior is reported. The work scope (from the checked Common Tasks) covers: Investigation, otn_port, Trace/CAT, match_wvl, PM, Logical View, Equipment View, Equipment Management, Wavelength Services, QA Testing, and Documentation.

## Resolution / Notes

No resolution posted yet — issue is OPEN and actively in progress under milestone 5.4.0.

Progress notes from the comments:

> "per Grant, the FBN team is turning up the network in the lab this week. We should get access later this week. Specifically the EILA/EILAL cards"
> — **tmasse-LR**, 2026-05-12

> "The 1830 PSS Shelves with the EILA and EILAL cards are also on and connected to the lab network. The IP's of those devices are: 10.9.101.165, 10.9.101.168, 10.9.101.171. these are in the TX FBN. idk if we can reach them yet but please look into that"
> — **tmasse-LR**, 2026-05-19

> "Other cards already supported by nF in the Oracle plan: PSS IRDM32, PSS IRDM32L, PSS OMDCL, PSS AWBILA, PSS RA5PB"
> — **tmasse-LR**, 2026-03-31

### Sub-issues
This feature was decomposed into sub-issues (parent #3053):
- #3054 — Investigation (DEV, open)
- #3055 — QA Testing (open)
- #3056 — Documentation (open)
- #3983 — Add 1830 EILA and EILAL cards to cards file, etc (closed/completed)
- #4546 — EILA, EILAL Assign (closed/completed)
- #4570 — DNO — EILA / EILAL (open)
- #4598 — DNO — MLFSB (open)
- #4599 — otn_port
- #4600 — Trace/CAT
- #4601 — match_wvl
- #4602 — PM
- #4603 — Logical View
- #4604 — Equipment View
- #4605 — Equipment Management
- #4606 — Wavelength Services

## Attachments

- [Material Submittal Oracle PRE-ORDER LGA-ORD Route 1 0326 REVISED 033126.xlsm](https://github.com/user-attachments/files/26389411/Material.Submittal.Oracle.PRE-ORDER.LGA-ORD.Route.1.0326.REVISED.033126.xlsm) — posted by tmasse-LR, 2026-03-31

## Metadata

- **Issue:** #3053
- **State:** OPEN
- **Title:** [79][FEATURE] 1830 PSS/PSI-L new equipment at ODC: MLFSB, EILA, EILAL
- **Customer:** Oracle (via LR Services)
- **Type:** Development (enhancement)
- **Category:** New Hardware
- **Support Level:** ODC
- **Targeted Release:** 5.4.0
- **Milestone:** 5.4.0 (due 2026-06-01)
- **Author:** Tim Masse (tmasse-LR)
- **Assignee:** _none_
- **Created:** 2026-03-31
- **Updated:** 2026-06-03
- **Labels:** enhancement
