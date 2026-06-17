---
issue: 4835
repo: lightriversoftware/netflex
state: OPEN
status: Customer Issue
sprint: Sprint 14
title: Ciena Core Director link flaps DOWN — RTRV-NETYPE DENY leaves LAN_GET_LINK flag stuck (lanalive.c)
customer: Lumen
priority: TOP PRIORITY
type: Support (bug)
process_group: DNO
release: 5.3.0
build: "32"
milestone: 5.3.0
author: dholcomb-LR
assignee: dnuzzo-LR
created: 2026-06-15
updated: 2026-06-15
url: https://github.com/lightriversoftware/netflex/issues/4835
---

# Issue 4835 — Ciena Core Director link flaps DOWN — RTRV-NETYPE DENY leaves LAN_GET_LINK flag stuck (lanalive.c)

[View on GitHub](https://github.com/lightriversoftware/netflex/issues/4835)

## Summary

For Ciena Core Directors, the link comes UP, netFLEX sends `RTRV-NETYPE`, and the NE always responds `DENY` (IICM / "Unknown error in VMM"). Moments later the NE goes DOWN, and the `RTRV-NETYPE` denial is the trigger. Across 96 Core Directors tested in production, **all** failed with `RTRV-NETYPE` and **all** succeeded with `RTRV-EQPT::COM:;` (whose COMPLD response carries `VRSN=` for the SWFR generic). TOP PRIORITY for Lumen.

## Reproduction Steps

- Bring a Ciena Core Director link up; netFLEX issues `RTRV-NETYPE` → NE returns `DENY`. ~60 s later the link drops. Reproduced on 96 production Core Directors.

## Observed Behavior

- `RTRV-NETYPE` → `X0DL5P DENY / IICM / Input: Unknown error in VMM`.
- `RTRV-EQPT:<tid>:COM:<ctag>;` → COMPLD, e.g. `VRSN=6.3.2.cn810667, PRDCTNAME=CoreDirector`.

## Resolution / Notes

No resolution posted yet (issue just opened). Root-cause analysis and proposed fix are in the issue body (and attached `NE_72767_analysis.md`):

- **Root cause:** At `lanalive.c:5739-5740`, for `CIENA_5400_LIKE` the `AND_STATUS_STRUCT(..., ~LAN_GET_LINK)` clear is gated by `if (strstr(ptr, "COMPL"))`. Because a Core Director always answers `RTRV-NETYPE` with `DENY`, the `LAN_GET_LINK` flag never clears. ~60 s later the GET-LINK-REQUEST timeout (`lanalive.c:494-507`) fires a `COMM_ERR` alarm; a downstream consumer sets `link_status[0] &= ~DI_STAT` in shared memory, so `get_link_status` then returns `NE_LINK_DOWN` and the GNE-up check at `lanalive.c:819` fails on every poll.
- **Proposed fix:** Replace the post-login probe for `CIENA_CORE_DIRECTOR` with `RTRV-EQPT:<tid>:COM:<ctag>;` and parse the `VRSN=` field from the COMPLD response.

## Attachments

- `lan.72767.txt` (LAN trace)
- `NE_72767_analysis.md` (root-cause analysis)

## Metadata

- **Issue Type:** Support (bug)
- **Process Group:** DNO
- **Software Release Detected:** 5.3.0 (Build 32)
- **Customer Affected:** Lumen
- **Priority:** TOP PRIORITY
- **Project Status:** Customer Issue (Sprint 14)
