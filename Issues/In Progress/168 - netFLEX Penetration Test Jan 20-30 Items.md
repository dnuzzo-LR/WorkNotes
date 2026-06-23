---
issue: 168
repo: lightriversoftware/netflex
state: OPEN
status: In Progress
sprint: Sprint 6
title: "[PROJECT] netFLEX Penetration Test Jan 20-30 Items"
type: Project
milestone: 5.4.0
author: tmasse-LR
assignee: dnuzzo-LR, riweh-LR
created: 2025-10-02
updated: 2026-04-16
url: https://github.com/lightriversoftware/netflex/issues/168
---

# Issue 168 — [PROJECT] netFLEX Penetration Test Jan 20-30 Items

[View on GitHub](https://github.com/lightriversoftware/netflex/issues/168)

> **Note:** The issue body contains the UI and API pen-test PDF passwords in cleartext. They are intentionally **not** copied here — retrieve them from the GitHub issue if needed.

## Summary

Tracking item for netFLEX's January 2026 penetration test (GUI testing starting Jan 20, API testing starting Jan 26). Dan's work items focus on the API/Swagger side; Reuben (riweh) tracks the environment setup (companion item #167).

## Reproduction Steps

**Dan (this issue, #168):**
- Provide updated Swagger command list.
- Identify which commands will work for the read-only user.
- Provide code snippets for issues remediated last year to assess effectiveness of the fixes.

**Dan & Reuben (together):**
- Create 2 API aliases — an admin user with access to every API, and a read-only user limited to the inventory set (plus other required APIs).
- Set up secure (non-default) user & password for the GUI.
- Set up 2 secure (non-default) user & passwords for the API users.
- Share credentials via 1Password share (not raw email).

**Reuben (#167):** upgrade the netflexapp VM to r5.3.0, update the URL, provide web + Swagger URLs.

## Resolution / Notes

- **dnuzzo-LR (2026-01-30):** Posted "First Failure" — attached the depth/Praetorian API critical-update PDF (`depth.security.lightriver.netFLEX.API.jan2026.critical.update.pdf`).

## Attachments

- `depth.security.lightriver.netFLEX.API.jan2026.critical.update.pdf` (API pen-test critical update).

## Metadata

- **Issue Type:** Project tracking (enhancement)
- **Milestone:** 5.4.0
- **Related:** #167 (Reuben's environment-setup tracking item)
- **Project Status:** In Progress (Sprint 6)

## My Notes

<!-- Your notes below are preserved across syncs. -->
