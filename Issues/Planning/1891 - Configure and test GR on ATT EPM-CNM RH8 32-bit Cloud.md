---
issue: 1891
repo: lightriversoftware/netflex
state: OPEN
status: Planning
title: "[DEV][ATT - EPM/CNM RH8 32-bit Cloud Support]Configure and test Geographic Redundancy on AT&T EPM/CNM RH8 32-bit Holmdel VMs"
type: Development (enhancement)
milestone: 5.4.0
author: bmead-LR
assignee: dnuzzo-LR, efondren-LR, dallen-LR
created: 2026-02-10
updated: 2026-06-18
url: https://github.com/lightriversoftware/netflex/issues/1891
---

# Issue 1891 — [DEV][ATT - EPM/CNM RH8 32-bit Cloud Support]Configure and test Geographic Redundancy on AT&T EPM/CNM RH8 32-bit Holmdel VMs

[View on GitHub](https://github.com/lightriversoftware/netflex/issues/1891)

## Summary

Development sub-task of parent #353. Stand up and test Geographic Redundancy (GR) on the AT&T EPM/CNM RH8 32-bit cloud VMs.

## Resolution / Notes

- **efondren-LR (2026-05-28):** "I installed a database from holmvm67 on core32-dev07 & dev11. I installed the db from holmvm68 on core32-dev08 & dev 12. I enabled GR in the license file on core32-dev07, 8, 11 & 12. I copied the system_defines from holmvm67 platform to their respective servers. I had to remove the /usr/cnc/features/ems touch file on all servers to run in epm mode. I setup gr in the gui on dev07 & dev08. I also had to copy the cmm pub key from dev08 to the authorized keys on dev 12 so the gr would work."

## Attachments

None.

## Metadata

- **Issue Type:** Development (enhancement) (sub-task)
- **Parent Issue:** #353 (ATT - EPM/CNM RH8 32-bit Cloud Support)
- **Customer:** AT&T (INC)
- **Priority:** Required this Release
- **Process Group:** GR
- **Milestone:** 5.4.0
- **Project Status:** Planning (no sprint assigned)

## My Notes

<!-- Your notes below are preserved across syncs. -->
