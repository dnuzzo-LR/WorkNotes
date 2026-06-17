---
issue: 168
repo: lightriversoftware/netflex
state: OPEN
title: "[PROJECT] netFLEX Penetration Test Jan 20-30 Items"
type: enhancement
milestone: "5.4.0"
author: tmasse-LR
assignee: [dnuzzo-LR, riweh-LR]
created: 2025-10-02
updated: 2026-04-16
url: https://github.com/lightriversoftware/netflex/issues/168
---

# Issue 168 — [PROJECT] netFLEX Penetration Test Jan 20-30 Items

[View on GitHub](https://github.com/lightriversoftware/netflex/issues/168)

## Summary

Project-tracking issue for the January 20–30, 2026 netFLEX penetration test (GUI and API). It coordinates the setup, scheduling, and remediation tracking for both the UI and API pen tests, including provisioning a dedicated test VM, creating secure GUI/API user accounts (admin + read-only), and assessing the effectiveness of fixes made for issues found in the prior year's test. The body holds the password references for the resulting UI and API pen-test report PDFs.

## Reproduction Steps

Not applicable — this is a project/coordination issue, not a bug report.

## Observed Behavior

Not applicable.

## Resolution / Notes

No resolution posted yet (issue is OPEN). Key planning recap from the team:

> **Internal Recap**
> - Setup ready to go by end of week, Friday 16th
> - GUI Testing starts Tuesday 20th
> - API Testing starts Monday 26th
>
> **Reuben to:** upgrade the netflexapp VM to r5.3.0, update the URL, provide web URL and Swagger URL (tracked from #167).
> **Dan to:** provide updated Swagger command list, identify which APIs work for the read-only user, provide code snippets for issues remediated last year to assess fix effectiveness (tracked from this issue).
> **Dan & Reuben to:** create 2 API aliases (admin with full API access; read-only with inventory-only access), set up secure GUI user/password (no defaults), set up 2 secure API user/passwords (no defaults), and share credentials via 1Password share (not raw email).
>
> — @tmasse-LR, 2026-01-13

A follow-up comment from @dnuzzo-LR (2026-01-30) flagged a **"First Failure"** and attached the critical API pen-test update PDF.

## Attachments

- `depth.security.lightriver.netFLEX.API.jan2026.critical.update.pdf` — attached by @dnuzzo-LR, 2026-01-30 ([link](https://github.com/user-attachments/files/24967595/depth.security.lightriver.netFLEX.API.jan2026.critical.update.pdf))
- Report PDF passwords are recorded in the issue body (UI and API).

## Metadata

- **Issue:** 168
- **State:** OPEN
- **Type:** enhancement
- **Milestone:** 5.4.0 (due 2026-06-01)
- **Author:** tmasse-LR (Tim Masse)
- **Assignees:** dnuzzo-LR (Dan Nuzzo), riweh-LR (Reuben Iweh)
- **Created:** 2025-10-02
- **Updated:** 2026-04-16
