---
issue: 3144
repo: lightriversoftware/netflex
state: OPEN
status: In Progress
sprint: Sprint 10
title: Praetorian Security Audit — Insufficient Password Policies
type: Development (enhancement) / Project
milestone: 5.4.0
author: fsantitoro-LR
assignee: dnuzzo-LR, riweh-LR
created: 2026-04-02
updated: 2026-05-08
url: https://github.com/lightriversoftware/netflex/issues/3144
---

# Issue 3144 — [DEV][PROJECT Praetorian Security Audit Findings] Insufficient Password Policies [#3139]

[View on GitHub](https://github.com/lightriversoftware/netflex/issues/3144)

## Summary

Sub-task of the Praetorian Security Audit (#3139). Praetorian found that LightRiver did not enforce sufficient password policies across the `lrsdev.local` Active Directory domain and Linux infrastructure: AD enforced only a 7-character minimum with no account-lockout threshold, and multiple Linux servers accepted username-as-password credentials. This enabled password spraying, lateral movement, and privilege escalation. Severity: Medium.

## Observed Behavior / Findings

- **Active Directory (`lrsdev.local`, DC 10.18.0.10 / DEV-AD1):** 7-char minimum password length; no account-lockout threshold (unlimited failed attempts).
- **Linux servers:** anonymous SMB/Samba enumeration exposed usernames; password spraying with password == username succeeded on multiple hosts (verified via SMB/SSH sessions).

## Resolution / Notes

- **fsantitoro-LR (2026-05-06):** Plan to enforce 14-character passwords (Reuben to produce the plan). Anonymous SAMBA enumeration has already been disabled. The 14-char password implementation will address the weak-password finding.
- **fsantitoro-LR (2026-05-08, from Reuben's email):** Proposed centralized **LDAP**-based enforcement (migrate NIS users/servers → LDAP, centralize logging, enforce strong policy). Phased rollout: Phase 0 lab validation → Phase 1 NIS→LDAP migration → Phase 2 centralized logging → Phase 3 enforce policy. Proposed standard: min length 14; ≥1 upper/lower/number/special; prevent reuse of last 10; max age 90 days; min age 1 day; lockout after 5 failed attempts; lockout duration 15 min; expiration warning 14 days prior.
- **Division of work:** LightRiver IT makes the AD changes (coordinate timing with dev); the dev team remediates the Linux-server controls. Work with Dan Nuzzo to determine the best approach first.

## Attachments

- Praetorian audit document (referenced for the full list of impacted systems).

## Metadata

- **Issue Type:** Development (enhancement), sub-task / Project
- **Parent Issue:** #3139 (Praetorian Security Audit Findings)
- **Severity:** Medium
- **Milestone:** 5.4.0
- **Project Status:** In Progress (Sprint 10)
