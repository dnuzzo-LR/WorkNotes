---
issue: 4381
repo: lightriversoftware/netflex
state: OPEN
title: "[DEV][Charter]Batch large number of NE Login's [#3614]"
customer: Charter
type: Development (enhancement)
milestone: 5.4.0
author: tmasse-LR
assignee: dnuzzo-LR
created: 2026-05-26
updated: 2026-05-26
url: https://github.com/lightriversoftware/netflex/issues/4381
---

# Issue 4381 — [DEV][Charter]Batch large number of NE Login's [#3614]

[View on GitHub](https://github.com/lightriversoftware/netflex/issues/4381)

## Summary

Charter's TACACS authentication server is being overwhelmed when a BEP (Backbone Edge Platform) reboots and all of its nodes come online simultaneously. The resulting flood of `ACT-USER` login commands across GNE links resembles a scaled security attack, which causes TACACS to lock the users out.

The request is to throttle NE logins when bringing GNE links up — the same approach previously applied to the Infinera RT nodes. Logins should be batched (e.g. ~250 at a time), with a delay between batches (e.g. 2 seconds), then the next group enabled, and so on. Both parameters should be user-adjustable: the **number of nodes per batch** and the **delay timer** between batches.

This is a child enhancement of parent issue **#3614** ([SUPPORT] Charter: Infinera ADP Not Building RT).

## Observed Behavior

- When a BEP reboots, all nodes come up at once.
- This produces a large simultaneous burst of `ACT-USER` login commands.
- TACACS interprets the flood as a scaled security attack and locks users out.

## Resolution / Notes

No resolution posted yet.

The author requested: *"If possible, we would like to see this in r5.4.0 in a patch"* — **Tim Masse (tmasse-LR), 2026-05-26**.

## Attachments

None.

## Metadata

- **Issue:** 4381
- **State:** OPEN
- **Customer:** Charter
- **Type:** Development (enhancement)
- **Parent Issue:** #3614 — [SUPPORT] Charter: Infinera ADP Not Building RT
- **Milestone:** 5.4.0 (due 2026-06-01)
- **Labels:** enhancement
- **Author:** Tim Masse (tmasse-LR)
- **Assignee:** Dan Nuzzo (dnuzzo-LR)
- **Created:** 2026-05-26
- **Updated:** 2026-05-26
- **Requested target:** r5.4.0 (in a patch)


## Resolution

Host-wide rate limiter for the TL1 `ACT-USER` login command. Caps the
aggregate rate at which any combination of `lan`/`lanalive` processes
on a single host may issue `ACT-USER` to network elements.

- **Header:** `include/ne_throttle.h`
- **Source:** `cnc/utility/src/ne_actuser_throttle.c`
- **Library:** linked into `libnelib.so`

**Problem:** when the BEP reboots, every managed NE comes up at once.
Every `lan`/`lanalive` process opens its TL1 session and fires
`ACT-USER` more-or-less simultaneously. Charter's TACACS server treats
the resulting login flood as a credential-stuffing attack and locks the
account out, blocking all subsequent logins until manual recovery.

**Original ask:** batch logins (e.g., 250 at a time, 2 second delay
between batches), with both batch size and delay tunable.

**This implementation:** a continuous host-wide rate cap on
`ACT-USER` sends, configured as **R tokens per W-second window**
(`ACTUSER_RATE_PER_WINDOW` per `ACTUSER_WINDOW_SECS`). This is
functionally equivalent to the batch+delay design — at rate=R per
window=W the average inter-login spacing is W/R seconds, with bursts
capped at R inside any W-second window — and is simpler to reason
about, with fewer moving parts (no per-process scheduler, no group
coordination).

78b2db0e02957498f9548843cc55e8572a26ce41
c7accadb806af3ec1055b3df8ade55280e9161ce
