---
issue: 4835
repo: lightriversoftware/netflex
state: OPEN
title: "Ciena Core Director link flaps DOWN — RTRV-NETYPE DENY leaves LAN_GET_LINK flag stuck (lanalive.c)"
customer: Lumen
priority: TOP PRIORITY
type: Support (bug)
process_group: DNO
release: 5.3.0
build: 32
milestone: 5.3.0
author: dholcomb-LR
created: 2026-06-15
updated: 2026-06-15
url: https://github.com/lightriversoftware/netflex/issues/4835
---

# Issue 4835 — Ciena Core Director link flaps DOWN — RTRV-NETYPE DENY leaves LAN_GET_LINK flag stuck (lanalive.c)

[View on GitHub](https://github.com/lightriversoftware/netflex/issues/4835)

## Summary

netFLEX's LAN keepalive (`lanalive.c`) probes a Ciena **Core Director** (dtype `CIENA_CORE_DIRECTOR` = 242) after login with `RTRV-NETYPE` — a command the Core Director does not support. The NE answers `DENY` (`IICM / Input: Unknown error in VMM`). Because the Core Director shares the `CASE_CIENA_5400_LIKE` case label with the 5400, and the response branch only clears the in-flight `LAN_GET_LINK` flag on a `COMPL` substring, a `DENY` reply leaves the flag stuck. About 60 s later the get-link timeout fires, raising a recurring `COMM_ERR` / "NO RESPONSE RECEIVED FOR GET LINK REQUEST" alarm, and the NE flaps DOWN. The NE's software generic (`VRSN`) is never learned.

The reporter verified the split in production: **96/96 Core Directors fail with `RTRV-NETYPE`** but **96/96 succeed with `RTRV-EQPT::COM:;`** (parsing the `VRSN=` field for the SWFR generic). The DNO subsystem (`dno_release.c` / `set_ciena_cd_release`) already does this correctly; only the SDI/lan path lags behind.

## Reproduction Steps

1. On a GNE that fronts a Ciena Core Director, bring the link to the NE UP.
2. Observe the post-login probe send `RTRV-NETYPE`.
3. The NE responds `DENY` (`IICM / Input: Unknown error in VMM`).
4. ~60 s later the get-link timeout fires; the NE goes DOWN.

## Observed Behavior

- `RTRV-NETYPE` is denied:
  ```
  X0DL5P DENY
     IICM
     /* Input: Unknown error in VMM */
  ```
- A few moments later the NE goes down; the `RTRV-NETYPE` denial appears causally linked.
- `RTRV-EQPT::COM:;` succeeds and returns the version:
  ```
  RTRV-EQPT:LSANCA54K01197002:COM:CJS11;
  ...
  "COM,EQPTNAME=LSANCA54K01197002,...,VRSN=6.3.2.cn810667,PRDCTNAME=CoreDirector,..."
  ```
- Production scope: 96 Core Directors all failed with `RTRV-NETYPE`; all 96 succeeded with `RTRV-EQPT::COM`.

## Resolution / Notes

No resolution posted yet — issue is **OPEN**. An automated (DRAFT/UNVERIFIED) RCA bot comment proposes the fix; engineer review still required.

**Reporter's proposed fix (Dennis Holcomb, 2026-06-15):**
> Replace the post-login probe for `CIENA_CORE_DIRECTOR` with a command the NE actually supports — `RTRV-EQPT:<tid>:COM:<ctag>;` — and parse the `VRSN=` field from the COMPLD response.

**Root cause (per reporter and corroborated by RCA bot):**
1. In `lanalive.c`, the `CASE_CIENA_5400_LIKE` label folds `CIENA_54XX` and `CIENA_CORE_DIRECTOR` together, so both get sent `RTRV-NETYPE`. The Core Director rejects it.
2. The response branch clears `LAN_GET_LINK` only inside the `strstr(ptr, "COMPL")` block. A `DENY` never contains `COMPL`, so the flag stays set, the get-link timeout fires, and a recurring `COMM_ERR` alarm is raised; the NE eventually flaps DOWN.

> Note: the reporter cited `lanalive.c:5739-5740`, but the RCA bot found those lines are actually the Optelian `sscanf` in the same branch — line numbers drift between switch branches. Cite by symbol: the `CASE_CIENA_5400_LIKE` branch in the `LAN_GET_LINK` response switch, where `AND_STATUS_STRUCT(..., ~LAN_GET_LINK)` is gated by `ptr2 = "COMPL"`.

**RCA bot recommended fix (DRAFT / UNVERIFIED — `netflex-workflow-bot`, 2026-06-15):**
> In `cnc/sdi/src/lanalive.c`, stop using the combined `CASE_CIENA_5400_LIKE` label for the Core Director in the two get-link sites, and give `CIENA_CORE_DIRECTOR` its own `RTRV-EQPT:...:COM:...` send and `VRSN=`-based parse — mirroring `dno_release.c` / `set_ciena_cd_release`.

- **Send side** (~line 3037): split out a `case CIENA_CORE_DIRECTOR:` that emits `RTRV-EQPT:<tid>:COM:RE<ctag>;` and arms `LAN_GET_LINK`.
- **Receive side** (~line 5710): add a dedicated `case CIENA_CORE_DIRECTOR:` that clears `LAN_GET_LINK` as soon as **our ctag is echoed** (whether `COMPL` or `DENY`), and parses `VRSN=` via `sscanf(cptr, "VRSN=%[0-9.]", vrsn)` into `Frm[dacsid].release`.
- Robustness: clearing on ctag echo (not only on `COMPL`) mirrors the existing `GENERIC_TL1` / `MICROSEMI_REF_SOURCE` branch and prevents future stuck-probe loops. Consider applying the same hardening to the shared `CIENA_54XX` branch.

**Patchbuild notes (from RCA bot):**
- Affected source: `cnc/sdi/src/lanalive.c` only (no header changes; `CIENA_CORE_DIRECTOR` and `CASE_CIENA_5400_LIKE` already exist).
- Rebuild the SDI/LAN keepalive (`lan`/`sdi`) process binary; restart the per-NE LAN keepalive/poller process for affected GNE(s) (or recycle SDI). Full INC restart should not be required.
- No DNO/`.dno` schema change, no `dumpdno` impact. `dno_release.c` is already correct and is not rebuilt.

**Confidence:** HIGH on root cause (wrong probe command, verified in source + 96/96 production split). MEDIUM only on the precise mechanism by which `COMM_ERR` propagates to the link-DOWN flap — the get-link timeout arms log the alarm but do not themselves call `set_link_status`, so that last hop relies on the attached `lan.72767.txt` trace rather than static proof. The fix is correct either way.

## Attachments

- [lan.72767.txt](https://github.com/user-attachments/files/28961419/lan.72767.txt) — LAN trace showing the link come UP, the `RTRV-NETYPE` DENY, and the subsequent DOWN.
- [NE_72767_analysis.md](https://github.com/user-attachments/files/28961421/NE_72767_analysis.md) — Claude Code analysis of the trace plus `lan.c` / `lanalive.c`.

## Metadata

- **Issue:** #4835
- **State:** OPEN
- **Customer:** Lumen
- **Priority:** TOP PRIORITY
- **Type:** Support (bug)
- **Product:** netFLEX
- **Process Group:** DNO
- **Software Release Detected:** 5.3.0
- **Build Number:** 32
- **Milestone:** 5.3.0
- **Author:** dholcomb-LR (Dennis Holcomb)
- **Assignee:** (none)
- **Created:** 2026-06-15
- **Updated:** 2026-06-15
- **Labels:** bug
