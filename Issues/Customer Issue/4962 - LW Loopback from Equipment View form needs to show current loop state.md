---
issue: 4962
repo: lightriversoftware/netflex
state: OPEN
status: Customer Issue
title: "[QA Testing] LW Loopback from Equipment View form needs to show current loop state"
customer: LightRiver Internal
priority: Important
type: QA Testing (bug)
process_group: Other
release: 5.4.0
build: 27
milestone: 5.4.0
author: jsloop-LR
assignee: dnuzzo-LR
created: 2026-06-17
updated: 2026-06-17
url: https://github.com/lightriversoftware/netflex/issues/4962
---

# Issue 4962 — [QA Testing] LW Loopback from Equipment View form needs to show current loop state

[View on GitHub](https://github.com/lightriversoftware/netflex/issues/4962)

## Summary

The Loopback form launched from the **Equipment View** does not reflect the port's current loop state. When you place a loop, you first choose a direction (Facility or terminal) and operate it — this works fine. The problem is what happens afterward:

- After the loop is placed, the form's Action still defaults to **Operate loopback** instead of switching to **Release loopback**. There's no way to tear the loop down from this same screen.
- Even re-opening the form from the Equipment View doesn't recognize that a loop is already placed.
- The form has a **Retrieve** button (to pull the current state) but it is always grayed out and unusable.

The Loopback dialog launched from the **Trace** form already behaves correctly: when a loop is already placed, it defaults to **Release**. The Equipment View form should match that behavior and support state retrieval.

## Reproduction Steps

1. From the Equipment View, place a loop on a port — the Loopback form launches.
2. Choose a direction (Facility or terminal) and operate the loopback (works).
3. Re-open / observe the loopback form for that same port.
4. Note the Action still defaults to "Operate loopback" with no release option, and the Retrieve button is grayed out.
5. Compare with the Trace form's loop dialog, which defaults to "Release" when a loop is already placed.

## Observed Behavior

- Equipment View loopback form keeps Action at "Operate loopback" even after a loop is placed.
- No option to remove/release the loop from the Equipment View form.
- Re-navigating from Equipment View does not detect an existing loop.
- The **Retrieve** button on the form is permanently grayed out.
- By contrast, the Trace form's loop dialog correctly defaults to "Release" when a loop exists.

## Resolution / Notes

No engineer resolution posted yet. The issue is **OPEN**. (An automated RCA-bot draft analysis was posted but is unverified workflow-bot content and is not treated as a resolution.)

## Attachments

- Screenshot — Equipment View loopback form after a loop is placed (Action stuck on Operate): https://github.com/user-attachments/assets/9b275f16-c4d3-45b5-94aa-591eaca2e8a1
- Screenshot — Trace form loop dialog defaulting to "Release" when a loop is already placed: https://github.com/user-attachments/assets/da871b48-e7d9-4caa-bd00-26d51de0f7b0
- Screenshot — Equipment View loopback form showing grayed-out Retrieve button: https://github.com/user-attachments/assets/98ed5e6b-63d0-48e8-aec3-3c4d42091575

## Metadata

- **Status:** Customer Issue
- **State:** OPEN
- **Priority:** Important
- **Type:** QA Testing (bug)
- **Customer:** LightRiver Internal
- **Process Group:** Other
- **Software Release Detected:** 5.4.0
- **Build Number:** 27
- **Milestone:** 5.4.0
- **Hostname:** linc12vm3
- **Issue Simulated:** No
- **Author:** jsloop-LR (Jimmy Sloop)
- **Assignee:** dnuzzo-LR (Dan Nuzzo)
- **Created:** 2026-06-17
- **Updated:** 2026-06-17

## My Notes

<!-- Your notes below are preserved across syncs. -->
