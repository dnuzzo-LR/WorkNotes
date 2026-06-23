---
issue: 4143
repo: lightriversoftware/netflex
state: OPEN
status: Ready for QA
sprint: Sprint 14
title: "[DEV][Lumen Local TELLABS 532/530 NE type number 5 in nF is sending the incorrect login commands to the NE]Enhance lansim to support the login process on Tellabs 532/530 PDS NEs [#3862]"
customer: Lumen
type: Development (enhancement)
process_group: LAN
milestone: 5.4.0
author: bmead-LR
assignee: bmead-LR, dnuzzo-LR
created: 2026-05-15
updated: 2026-06-08
url: https://github.com/lightriversoftware/netflex/issues/4143
---

# Issue 4143 — Enhance lansim to support the login process on Tellabs 532/530 PDS NEs [#3862]

[View on GitHub](https://github.com/lightriversoftware/netflex/issues/4143)

## Summary

Development sub-task of parent #3862 (Lumen Local Tellabs 532/530 NE type 5 sending incorrect login commands). Enhance `lansim` to simulate the Tellabs 532/530 PDS login handshake: after netFLEX sends `UTL::PRIVATE, PROV!`, the NE prompts for a password and, once entered, responds with the completion message. lansim must support this exchange so the corrected 532/530 login can be tested.

## Resolution / Notes

- **netflex-workflow-bot (2026-06-08):** Posted "Ready for testing @bmead-LR" — development complete, handed to QA.

## Attachments

None.

## Metadata

- **Issue Type:** Development (enhancement)
- **Parent Issue:** #3862 — [SUPPORT] Lumen Local TELLABS 532/530 NE type number 5 in nF is sending the incorrect login commands to the NE
- **Customer Affected:** Lumen
- **Priority:** Required this Release (copied from parent)
- **Process Group:** LAN
- **Feature Set:** MDH
- **Milestone:** 5.4.0
- **Labels:** enhancement, sub-task
- **Project Status:** Ready for QA (Sprint 14)

## My Notes

<!-- Your notes below are preserved across syncs. -->
