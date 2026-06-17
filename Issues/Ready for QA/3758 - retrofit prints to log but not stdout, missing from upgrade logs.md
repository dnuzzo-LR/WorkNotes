---
issue: 3758
repo: lightriversoftware/netflex
state: OPEN
status: Ready for QA
sprint: Sprint 13
title: retrofit prints to a log but not stdout, so it's missing from the upgrade logs
customer: LightRiver Internal
priority: Time Permitting
type: Development (enhancement)
process_group: Other
release: 5.4.0
build: "13"
milestone: 5.4.0
author: dnuzzo-LR
assignee: dnuzzo-LR
created: 2026-04-29
updated: 2026-05-27
url: https://github.com/lightriversoftware/netflex/issues/3758
---

# Issue 3758 — retrofit will print to a log, but not to stdout, so it's missing from the upgrade logs

[View on GitHub](https://github.com/lightriversoftware/netflex/issues/3758)

## Summary

During an upgrade, the `retrofit` step writes its output to its own log file but not to stdout. Because the upgrade logs capture stdout, retrofit's output is missing from them — so when an upgrade fails, the relevant retrofit detail isn't visible in the upgrade logs. The fix is to also echo retrofit output to stdout so it is captured.

## Observed Behavior

While testing an upgrade, it was failing but no retrofit output appeared in the upgrade logs (simulated on holmvm76).

## Resolution / Notes

No resolution posted yet.

## Attachments

None.

## Metadata

- **Issue Type:** Development (enhancement)
- **Process Group:** Other
- **Software Release Detected:** 5.4.0 (Build 13)
- **Customer Affected:** LightRiver Internal
- **Priority:** Time Permitting
- **Project Status:** Ready for QA (Sprint 13)
