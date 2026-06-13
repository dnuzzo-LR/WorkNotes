---
issue: 4409
repo: lightriversoftware/netflex
state: OPEN
title: "[65][FEATURE] Convert CLI process to use EPOLL"
type: Development (enhancement)
process_group: Core Software
release: 5.4.1
milestone: 5.4.1
author: tmasse-LR
assignee: dnuzzo-LR
created: 2026-05-27
updated: 2026-05-27
url: https://github.com/lightriversoftware/netflex/issues/4409
---

# Issue 4409 — [65][FEATURE] Convert CLI process to use EPOLL

[View on GitHub](https://github.com/lightriversoftware/netflex/issues/4409)

## Summary

Enhancement request (Product rank #65) to convert the netFLEX CLI process to use `epoll` for its I/O event handling. This is a core-software development task targeted at the 5.4.1 release. The work is scoped to investigation, QA testing, and documentation, with no customer-facing defect attached.

## Observed Behavior

Not a defect report — this is a feature/enhancement. The current CLI process does not use `epoll` for I/O multiplexing; the request is to migrate it to an `epoll`-based model.

## Resolution / Notes

No resolution posted yet.

The issue is **OPEN**. Three sub-issues were spun off from the Common Tasks checklist:

- **#4412** — Investigation
- **#4410** — QA Testing
- **#4411** — Documentation

Common Tasks checked on the parent: Investigation, QA Testing, Documentation.

## Attachments

None.

## Metadata

- **Issue:** #4409
- **State:** OPEN
- **Type:** Development (enhancement)
- **Product:** netFLEX
- **Category / Process Group:** Core Software
- **Support Level:** General
- **Targeted Release:** 5.4.1
- **Milestone:** 5.4.1
- **Label:** enhancement
- **Author:** tmasse-LR (Tim Masse)
- **Assignee:** dnuzzo-LR (Dan Nuzzo)
- **Created:** 2026-05-27
- **Updated:** 2026-05-27
- **Sub-issues:** #4410 (QA), #4411 (DOC), #4412 (Investigation)
