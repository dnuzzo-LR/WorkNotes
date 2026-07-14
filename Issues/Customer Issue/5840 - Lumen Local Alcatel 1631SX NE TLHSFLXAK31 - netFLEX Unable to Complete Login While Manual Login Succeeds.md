---
issue: 5840
repo: lightriversoftware/netflex
state: OPEN
status: Customer Issue
title: "[SUPPORT] Lumen Local Alcatel 1631SX NE TLHSFLXAK31 - netFLEX Unable to Complete Login While Manual Login Succeeds"
customer: Lumen
priority: Required this Release
type: Support (bug)
process_group: LAN
release: 5.3.0
build: 32
milestone: 5.3.0
author: hborreli-LR
assignee: dnuzzo-LR
created: 2026-07-13
updated: 2026-07-13
url: https://github.com/lightriversoftware/netflex/issues/5840
---

# Issue 5840 — [SUPPORT] Lumen Local Alcatel 1631SX NE TLHSFLXAK31 - netFLEX Unable to Complete Login While Manual Login Succeeds

[View on GitHub](https://github.com/lightriversoftware/netflex/issues/5840)

## Summary

The NE **TLHSFLXAK31** (NEID 71470, TID TLHSFLXAK31), an **Alcatel 1631 SMC** at `10.61.160.42`, will not log in through netFLEX. The LAN port shows `DSBL` / link `DOWN` in `nestat`, and the `watch -l 71470` log shows netFLEX repeatedly re-sending the `ACT-USER` login and logging `Communication Errors - RESENT LOGIN REQUEST` with no successful login response ever coming back.

A **manual telnet** login from the application server (`usddclvenlp4`) to the same device/IP succeeds using the same connection — `ACT-USER`, `RTRV-HDR`, and `CANC-USER` all complete. Notably, during the manual session the NE did not echo the entered commands back to the terminal (only NE responses appeared). No "friendlies" are present on production (Rocky Linux 9.7).

This is a Lumen customer issue, priority **Required this Release**, detected on release 5.3.0 build 32. It was **not** simulated, and per the author it **cannot be reproduced in NJ**.

## Reproduction Steps

1. On production (`usddclvenlp*`), have NEID 71470 / TLHSFLXAK31 provisioned as a LAN NE.
2. Observe netFLEX cannot complete login: `nestat -n 71470` shows the `lan` port `DSBL`, link `DOWN`.
3. `watch -l 71470` shows repeated `ACT-USER:TLHSFLXAK31:emsadmin:ZG1J5A::#########;` TO-NE messages followed by `RESENT LOGIN REQUEST. CNT: 1` communication errors.
4. Manually `telnet 10.61.160.42` and issue `ACT-USER` — login succeeds, confirming the device and credentials work outside netFLEX.

## Observed Behavior

- netFLEX endlessly re-transmits the login request; the NE never completes login through the app.
- Manual telnet login to the same device/IP succeeds.
- During the manual session, entered commands were not echoed back by the NE.
- Connection record: `71470,TLHSFLXAK31,,GENERIC TL1 -,GNE,10.61.160.42/23,DOWN,51/0,TL1,3/USDDCLVENLP4,` — i.e. the NE is provisioned as device type **GENERIC TL1**.

## Resolution / Notes

No confirmed engineering resolution yet. Two relevant threads:

- **Automated RCA (DRAFT / UNVERIFIED, `netflex-workflow-bot`, 2026-07-13):** MEDIUM confidence. Attributes the failure to a **device-type / provisioning mismatch** — the NE is provisioned as `GENERIC TL1` but is physically a 1631 SMC (`ALC_31_SMC`). The generic branch of `lan_login()` sends a **TID-qualified** `ACT-USER:TLHSFLXAK31:emsadmin:...` form, whereas the NE only answers the **TID-less** form `ACT-USER::emsadmin:...;` (which the `ALC_31_SMC` branch produces and which the manual login's command echo `/* ACT-USER::emsadmin:::***** */` confirms). Because the login response is never recognized, `LAN_LOGGING_IN` never clears and the resend loop never resolves.
  > **Recommended fix:** Re-provision NEID 71470 with the correct netFLEX device type **`TELMAR 1631 SMC` (`ALC_31_SMC`)** instead of `GENERIC TL1`, then re-enable the LAN link (data/provisioning change, no rebuild). Code-level alternative: make the generic `ACT-USER` builder omit/blank the TID for 1631-class endpoints. The RCA notes the "no command echo" observation is a red herring.

- **Human discussion:**
  > **@bmead-LR (2026-07-13):** "Is this reproduced in NJ where a developer can investigate?"
  >
  > **@hborreli-LR (2026-07-13):** "Sir, I apologize. I can not reproduce the issue in NJ. I did attach the lan file 'lan71470.txt'."

## Attachments

- [lan71470.txt](https://github.com/user-attachments/files/29978855/lan71470.txt) — SDI/LAN trace for NE 71470 (referenced in the body and by the author).

## Metadata

- **Issue:** 5840
- **State:** OPEN
- **Status (project 16):** Customer Issue
- **Priority:** Required this Release
- **Customer Affected:** Lumen
- **Issue Type:** Support (bug)
- **Process Group:** LAN
- **Software Release Detected:** 5.3.0
- **Build Number:** 32
- **Milestone:** 5.3.0
- **Issue Simulated:** No
- **NE:** TLHSFLXAK31 (NEID 71470), Alcatel 1631 SMC, `10.61.160.42`
- **Author:** hborreli-LR (Henry J Borreli)
- **Assignee:** dnuzzo-LR (Dan Nuzzo)
- **Created:** 2026-07-13
- **Updated:** 2026-07-13
- **Labels:** bug, rca-cause:config, rca-complete

## My Notes

<!-- Your notes below are preserved across syncs. -->
