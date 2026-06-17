---
issue: 4039
repo: lightriversoftware/netflex
state: OPEN
title: "[SUPPORT] Lumen Local ADTRAN TOTAL ACCESS 3000 RT Login Failure"
customer: Lumen
priority: Required this Release
type: Support (bug)
process_group: LAN
release: 5.3.0
build: 29
milestone: 5.3.0
author: hborreli-LR (Henry J Borreli)
assignee: dnuzzo-LR (Dan Nuzzo)
created: 2026-05-13
updated: 2026-06-01
url: https://github.com/lightriversoftware/netflex/issues/4039
---

# Issue 4039 — Lumen ADTRAN TOTAL ACCESS 3000 RT Login Failure

[View on GitHub](https://github.com/lightriversoftware/netflex/issues/4039)

## Summary

The automated/system login from the RT to the GNE fails on a Lumen ADTRAN Total Access 3000 (process group LAN). **Manual login to the NE works without issue** — the failure is specific to the automated login process. Detailed debug logs (debug level 9), RT/GNE watches, and a GNE trace were collected to capture the login behavior.

## Reproduction Steps (per reporter)

1. **Prepared LAN file** — copied/saved the current LAN file, then emptied the active one for fresh logging.
2. **Started debugging/tracing** — started a GNE trace from the beginning; set debug level to **9**.
3. **Started watches** — watch on RT and watch on GNE.
4. **Reproduced** — allowed the system to attempt login **3 times**, then disabled the RT again; left both watches running throughout.
5. **Collected logs** — RT watch log, GNE watch log, LAN file snapshot, and GNE trace file.

## Observed Behavior

- RT fails to log in after multiple attempts; behavior is consistent across repeated tests.
- Manual login to the NE succeeds — only the automated login fails.

Manual TL1 session (works) captured in the issue showed `ACT-USER` / `CANC-USER` completing normally against `MRRYUTEXD0101010801`.

## Resolution / Notes

> **Fix (hborreli-LR, 2026-06-01):** Set the variable **`RTLOGINDELAY`** in the ping file for the dtype. Set it to **3**.

Believed to be the same root cause as the DTN RT logging in too quickly — plan is to have Henry try friendlies with that fix.

Affected elements: **GNE 8850**, **RT 24316**.

## Attachments

- `lan8850.txt` — LAN file snapshot
- `ne008850.txt`, `ne024316.txt` — NE watch logs

## Metadata

- **Repo:** lightriversoftware/netflex
- **State:** OPEN
- **Customer:** Lumen
- **Priority:** Required this Release
- **Process Group:** LAN
- **Software Release Detected:** 5.3.0 (Build 29)
- **Milestone:** 5.3.0
- **Reporter:** Henry J Borreli (hborreli-LR)
- **Assignee:** Dan Nuzzo (dnuzzo-LR)
