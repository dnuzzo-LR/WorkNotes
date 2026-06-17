---
issue: 4039
repo: lightriversoftware/netflex
state: OPEN
status: Customer Issue
sprint: Sprint 14
title: Lumen Local ADTRAN TOTAL ACCESS 3000 RT Login Failure (manual login works)
customer: Lumen
priority: Required this Release
type: Support (bug)
process_group: LAN
release: 5.3.0
build: "29"
milestone: 5.3.0
author: hborreli-LR
assignee: dnuzzo-LR
created: 2026-05-13
updated: 2026-06-01
url: https://github.com/lightriversoftware/netflex/issues/4039
---

# Issue 4039 — Lumen Local ADTRAN TOTAL ACCESS 3000 RT Login Failure – Debug Logs with RT/GNE Watch + GNE Trace (Manual Login Works)

[View on GitHub](https://github.com/lightriversoftware/netflex/issues/4039)

## Summary

An ADTRAN Total Access 3000 RT (behind a Generic TL1 GNE, NE 8850) fails to log in automatically through netFLEX, while manual telnet login to the NE succeeds. The failure is specific to the automated/system login path and reproduces consistently. Detailed RT/GNE watch logs, a debug-level-9 GNE trace, and a LAN-file snapshot were captured.

## Reproduction Steps

1. Save and empty the active LAN file to start fresh logging.
2. Start a GNE trace from the beginning; set debug level 9.
3. Start watches on both the RT and the GNE.
4. Allow the system to attempt login 3 times, then disable the RT.
5. Collect the RT watch log, GNE watch log, LAN file, and GNE trace.

## Observed Behavior

- RT fails to log in after multiple automated attempts, consistently.
- Manual telnet login to the NE (ACT-USER / RTRV-HDR / CANC-USER sequence) works without issue.

## Resolution / Notes

- **hborreli-LR (2026-06-01):** Set the `RTLOGINDELAY` variable in the ping file for the dtype to **3**.

## Attachments

- lan8850.txt, ne008850.txt, ne024316.txt (GitHub attachments).

## Metadata

- **Issue Type:** Support (bug)
- **Process Group:** LAN
- **Software Release Detected:** 5.3.0 (Build 29)
- **Customer Affected:** Lumen
- **Priority:** Required this Release
- **Project Status:** Customer Issue (Sprint 14)
