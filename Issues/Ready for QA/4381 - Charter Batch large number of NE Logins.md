---
issue: 4381
repo: lightriversoftware/netflex
state: OPEN
status: Ready for QA
sprint: Sprint 14
title: "[DEV][Charter]Batch large number of NE Login's [#3614]"
customer: Charter
type: Development (enhancement)
milestone: 5.4.0
author: tmasse-LR
assignee: tmasse-LR, dnuzzo-LR
created: 2026-05-26
updated: 2026-06-17
url: https://github.com/lightriversoftware/netflex/issues/4381
---

# Issue 4381 — [DEV][Charter]Batch large number of NE Login's [#3614]

[View on GitHub](https://github.com/lightriversoftware/netflex/issues/4381)

## Summary

Development sub-task of parent #3614 (Charter: Infinera ADP Not Building RT). When a Charter BEP reboots and all nodes come up simultaneously, the flood of `ACT-USER` logins looks to Charter's TACACS like a scaled security attack and locks users out. The request is to throttle GNE link-up the same way it was done for Infinera RT nodes — e.g. enable ~250 nodes per batch, wait ~2 seconds, then bring up the next group. Both the **batch size (nodes per batch)** and the **delay timer** should be adjustable.

## Resolution / Notes

- **tmasse-LR (2026-05-26):** "If possible, we would like to see this in r5.4.0 in a patch."
- **netflex-workflow-bot (2026-06-17):** Posted "Ready for testing @tmasse-LR" — development complete, handed to QA.

## Attachments

None.

## Metadata

- **Issue Type:** Development (enhancement)
- **Parent Issue:** #3614 — [SUPPORT] Charter: Infinera ADP Not Building RT
- **Customer Affected:** Charter
- **Priority:** TOP PRIORITY (copied from parent)
- **Process Group:** DNO
- **Milestone:** 5.4.0
- **Project Status:** Ready for QA (Sprint 14)

## My Notes

<!-- Your notes below are preserved across syncs. -->
