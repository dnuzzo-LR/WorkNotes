---
issue: 5265
repo: lightriversoftware/netflex
state: OPEN
status: Customer Issue
title: "[SUPPORT] Description ATT: EPM RH8 New NE created does not copy down to bep [#1890]"
customer: LightRiver Internal
priority: Important
type: QA Testing (bug)
process_group: Other
release: 5.4.0
build: 26
milestone: 5.4.1
author: efondren-LR
assignee: dnuzzo-LR
created: 2026-06-25
updated: 2026-07-09
url: https://github.com/lightriversoftware/netflex/issues/5265
---

# Issue 5265 — [SUPPORT] Description ATT: EPM RH8 New NE created does not copy down to bep [#1890]

[View on GitHub](https://github.com/lightriversoftware/netflex/issues/5265)

## Summary

On the AT&T EPM RH8 32-bit environment (host `holmvm22`), when a new Network Element (NE) is created on the primary application, it is not automatically synchronized down to the BEP (back-end processor / backup). The new NE only appears on the BEP after the application on the BEP is rebooted, which forces it to pull the configuration. Running the upload command did not help. The same workflow works correctly on CB production, so the behavior appears specific to this environment/release. This issue was simulated (reproduced) and is tracked as a sub-issue of #1890.

## Reproduction Steps

1. On the primary application, create a new NE.
2. Observe the BEP — the newly created NE is not copied down to it.
3. (Workaround) Reboot the application on the BEP; it then pulls the new NE down.

## Observed Behavior

- Newly created NEs are not automatically propagated to the BEP.
- Rebooting the BEP application forces it to pull the new NE down.
- Existing behavior otherwise: rebooting pulls things down, but not the new NE without a reboot.
- Issuing the upload command produced no change.
- The same scenario works fine on CB production.

## Resolution / Notes

No resolution posted yet. (The only comments on the issue are automated workflow-bot messages — project-board automation, create-subissues instructions, and an RCA-failed notice — with no engineering resolution.)

## Attachments

None found in the body or comments.

## Metadata

- **Issue:** 5265
- **State:** OPEN
- **Status (project 16):** Customer Issue
- **Priority:** Important
- **Customer Affected:** LightRiver Internal
- **Issue Type:** QA Testing (bug)
- **Process Group:** Other
- **Software Release Detected:** 5.4.0
- **Build Number:** 26
- **Milestone:** 5.4.1
- **Related / Parent Issue:** #1890
- **Hostname:** holmvm22
- **Issue Simulated:** Yes
- **Author:** efondren-LR (Eric Fondren)
- **Assignee:** dnuzzo-LR (Dan Nuzzo)
- **Created:** 2026-06-25
- **Updated:** 2026-07-09
- **Labels:** bug

## My Notes

<!-- Your notes below are preserved across syncs. -->
