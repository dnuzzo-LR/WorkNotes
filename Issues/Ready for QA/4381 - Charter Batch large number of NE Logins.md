---
issue: 4381
repo: lightriversoftware/netflex
state: OPEN
status: Ready for QA
sprint: Sprint 14
title: Charter — Batch large number of NE Logins
customer: Charter
type: Development (enhancement)
milestone: 5.4.0
author: tmasse-LR
assignee: tmasse-LR, dnuzzo-LR
created: 2026-05-26
updated: 2026-06-17
url: https://github.com/lightriversoftware/netflex/issues/4381
---

# Issue 4381 — [DEV][Charter] Batch large number of NE Login's [#3614]

[View on GitHub](https://github.com/lightriversoftware/netflex/issues/4381)

## Summary

Development sub-task of parent #3614. When a Charter BEP reboots, all nodes come up at once and the flood of simultaneous `ACT-USER` logins looks to Charter's TACACS like a scaled security attack, locking users out. The request is to throttle GNE link-up the same way RT logins were throttled for Infinera RT nodes — e.g. enable ~250 nodes per batch, wait ~2 seconds, then the next group. Both the **batch size** and the **delay timer** should be adjustable.

## Resolution / Notes

- **tmasse-LR (2026-05-26):** Would like this delivered in an r5.4.0 patch if possible.

## Attachments

None.

## Metadata

- **Issue Type:** Development (enhancement)
- **Parent Issue:** #3614 (Charter)
- **Milestone:** 5.4.0
- **Customer Affected:** Charter
- **Project Status:** Ready for QA (Sprint 14)
