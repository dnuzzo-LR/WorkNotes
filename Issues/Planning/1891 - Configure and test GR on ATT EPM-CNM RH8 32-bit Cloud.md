---
issue: 1891
repo: lightriversoftware/netflex
state: OPEN
status: Planning
title: Configure and test Geographic Redundancy on AT&T EPM/CNM RH8 32-bit Cloud VMs
type: Development (enhancement)
milestone: 5.4.0
author: bmead-LR
assignee: dnuzzo-LR, efondren-LR, dallen-LR
created: 2026-02-10
updated: 2026-05-28
url: https://github.com/lightriversoftware/netflex/issues/1891
---

# Issue 1891 — [DEV][ATT - EPM/CNM RH8 32-bit Cloud Support] Configure and test Geographic Redundancy on AT&T EPM/CNM RH8 32-bit Cloud VMs

[View on GitHub](https://github.com/lightriversoftware/netflex/issues/1891)

## Summary

Development sub-task of parent #353. Stand up and test Geographic Redundancy (GR) on the AT&T EPM/CNM RH8 32-bit cloud VMs.

## Resolution / Notes

- **efondren-LR (2026-05-28):** GR configured and brought up on the core32-dev VMs:
  - Installed the DB from `holmvm67` on `core32-dev07` & `dev11`; installed the DB from `holmvm68` on `core32-dev08` & `dev12`.
  - Enabled GR in the license file on core32-dev07, 08, 11 & 12.
  - Copied `system_defines` from the holmvm67 platform to the respective servers.
  - Removed the `/usr/cnc/features/ems` touch file on all servers to run in **EPM mode**.
  - Set up GR in the GUI on dev07 & dev08.
  - Copied the cmm public key from dev08 to authorized_keys on dev12 so GR would work.

## Attachments

None.

## Metadata

- **Issue Type:** Development (enhancement), sub-task
- **Parent Issue:** #353 (ATT - EPM/CNM RH8 32-bit Cloud Support)
- **Milestone:** 5.4.0
- **Project Status:** Planning (no sprint assigned)
