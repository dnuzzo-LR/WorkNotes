---
issue: 3862
repo: lightriversoftware/netflex
state: OPEN
status: Ready for QA
sprint: Sprint 14
title: Lumen Local TELLABS 532/530 NE type number 5 in nF is sending the incorrect login commands to the NE
customer: Lumen
priority: Required this Release
type: Support (bug)
process_group: LAN
release: 5.3.0
build: "29"
milestone: 5.4.0
author: hborreli-LR
assignee: dnuzzo-LR, hborreli-LR
created: 2026-05-05
updated: 2026-06-18
url: https://github.com/lightriversoftware/netflex/issues/3862
---

# Issue 3862 — Lumen Local TELLABS 532/530 NE type number 5 in nF is sending the incorrect login commands to the NE

[View on GitHub](https://github.com/lightriversoftware/netflex/issues/3862)

## Summary

Enrolled Tellabs 532 NEs (NE type 5) in production receive the wrong login commands from netFLEX. nF sends the NE-type-6 (532L) style login (`UTL:FRM 01, SEQ 48:LOGIN, USER PROV, PASSWORD ...`), which the 532/530 rejects with "NOT VALID LOGIN RESPONSE." The correct sequence for the 532/530 is `UTL::PRIVATE, PROV!` → password → `UTL::PUBLIC!`, which works manually. Investigation concluded nF does not support the Tellabs 532 login process at all today, so this becomes a feature enhancement. (An "INS 2025 AAA/TACACS+ migration notice" banner on the device was initially suspected but ruled out — ATT's standard 532/530s use no login while only the 532L logs in.)

## Reproduction Steps

- Simulated on r9dev03/r9dev04 as NE 5.
- Enable a 532/530 link; nF repeatedly resends the 532L-style login and the NE returns "NOT VALID LOGIN RESPONSE."

## Observed Behavior

`watch` on NE 596 shows nF attempting login with the 532L command form and the NE rejecting it (`NOT VALID LOGIN RESPONSE`, then `RESENT LOGIN REQUEST. CNT: 1`), while manual login via `UTL::PRIVATE, PROV!` / `UTL::PUBLIC!` succeeds.

## Resolution / Notes

No closure comment yet — issue is **OPEN** and currently **Ready for QA**.

- **tmasse-LR (2026-05-06):** netFLEX does not support the Tellabs 532 login process today; this must be a feature enhancement targeted at 5.4.0 (may slip to a 5.4.0 patch). Milestone set to 5.4.0; customer/AIR to be updated; then into the Lumen lab for certification.
- **efondren-LR (2026-05-06):** On ATT, only the Tellabs 532L has a login; standard 532/530s are set up with no login (ruled out the TACACS+ banner theory).
- **hborreli-LR (2026-05-06):** Provided the correct 532/530 security-level commands — enter with `UTL::PRIVATE, mode!` (mode = TEST/PROV/ADMN), log off with `UTL::PUBLIC!`. Acknowledged and updated the AIR.
- **netflex-workflow-bot (2026-06-08):** "Ready for testing."
- **hborreli-LR (2026-06-18):** "Waiting on patch to test in production."

## Attachments

- watch log of NE 596 login attempts; login screenshots (GitHub user-attachments images):
  - https://github.com/user-attachments/assets/2bb7c4e7-b679-4408-a425-f006c0c6dd4d
  - https://github.com/user-attachments/assets/8ff214d7-3956-4160-b99e-2e57d143f5f9

## Metadata

- **Issue Type:** Support (bug) → feature enhancement (labeled `enhancement`)
- **Process Group:** LAN
- **Software Release Detected:** 5.3.0 (Build 29); target 5.4.0
- **Customer Affected:** Lumen
- **Priority:** Required this Release
- **Author:** hborreli-LR
- **Assignees:** dnuzzo-LR, hborreli-LR
- **Milestone:** 5.4.0
- **Project Status:** Ready for QA (Sprint 14)
- **Created:** 2026-05-05 · **Updated:** 2026-06-18

## My Notes
* c17a219309f2dde66e55477fa505afeca2ad527e
