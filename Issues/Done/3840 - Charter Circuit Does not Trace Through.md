---
issue: 3840
repo: lightriversoftware/netflex
state: OPEN
status: Done
sprint: Sprint 14
title: Charter — Circuit Does not Trace Through
customer: Charter
priority: Important
type: Support (bug)
process_group: Trace
release: 5.2.1
build: "41"
milestone: 5.2.1
author: JGarner-LR
assignee: dnuzzo-LR, tgibbia-LR
created: 2026-05-04
updated: 2026-05-18
url: https://github.com/lightriversoftware/netflex/issues/3840
---

# Issue 3840 — Charter - Circuit Does not Trace Through

[View on GitHub](https://github.com/lightriversoftware/netflex/issues/3840)

## Summary

A Charter circuit fails to trace through at the OMS level between ESAM cards. Starting the trace at NEID 6865 on AGGR-22-6-7-1, it stops at NEID 19; the path should run ESAM↔LIM↔WSS↔WSS↔LIM↔ESAM. All four beginning AIDs on NEID 6865 fail at the same node between the ESAM cards.

## Reproduction Steps

1. Begin the trace at NEID 6865 on AGGR-22-6-7-1 (also -8-1, -9-1, -10-1).
2. Trace fails to go through ESAM-2-5 → ESAM-1-5 in NEID 19; it stops on NEID 19.

## Observed Behavior

Trace stops on a PTP not in `otn_port`; neighbor/tie for NE 19 OPTMON-2-5-7 ↔ OPTMON-2-6-8 could not be brought into the neighbor DB (`populate`/`chknetie` errors), initially suspected as a ctree/tie DB error.

## Resolution / Notes

- **tgibbia-LR (2026-05-06):** After Dan confirmed there was no DB issue, the root cause was found to be a **conflicting topology provisioned on NEs 19 and 22** — both have topology provisioned to 2-6-8 on NE 19. It was hard to spot because of how the unassigned DSCM card is handled (no neighbors are normally created to it). NE 19 internal adjacency 2-5-7→2-6-8 conflicts with NE 22 external adjacency 1-91-1 (DSCM)→2-6-8 on NE 19.
- **JGarner-LR (2026-05-18):** Referred to Charter to resolve the conflicting provisioning on their side.

## Attachments

- Trace screenshots (GitHub user-attachments images in body and comments).

## Metadata

- **Issue Type:** Support (bug)
- **Process Group:** Trace
- **Software Release Detected:** 5.2.1 (Build 41)
- **Customer Affected:** Charter
- **Priority:** Important
- **Project Status:** Done (Sprint 14)
