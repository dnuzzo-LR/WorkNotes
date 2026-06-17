---
issue: 2373
repo: lightriversoftware/netflex
state: OPEN
status: Ready for QA
sprint: Sprint 11
title: Modernize FEP/BEP Multi-box communications — Feature QA Testing
type: QA (bug)
milestone: 5.4.1
author: bmead-LR
assignee: bmead-LR, dnuzzo-LR, jsloop-LR
created: 2026-03-04
updated: 2026-06-10
url: https://github.com/lightriversoftware/netflex/issues/2373
---

# Issue 2373 — [QA][Modernize FEP/BEP Multi-box communications] Feature QA Testing [#360]

[View on GitHub](https://github.com/lightriversoftware/netflex/issues/2373)

## Summary

QA-testing sub-task of parent feature #360. Test the FEP→BEP / BEP→FEP communication processes that now run over **ZeroMQ (zmq)**. The feature replaces raw-socket reader/writer connection management with a zmq layer for stability on imperfect networks.

## Reproduction / Enablement Steps

1. Set `NFMD=1` in `system_defines` on **both** the FEP and the BEP.
2. Run `systemctl restart netFLEX-md` on all FEPs and BEPs.
3. Exercise the multi-box rdr/wtr communication.

## Resolution / Notes

- **fsantitoro-LR (2026-03-25):** Enabling system_define is `NFMD=1`; zmq details from Dan Nuzzo.
- **jsloop-LR (2026-05-11):** Confirmed enablement steps (NFMD=1 on FEP and BEP + `systemctl restart netFLEX-md`). Per Dan: the fep/bep rdr/wtr default to raw sockets with keep-alive logic; zmq is a layer on top of sockets that manages connections, much more stable on less-than-perfect networks.
- **jsloop-LR (2026-06-10):** Turned this on in the GR environment (holmvm39–44).

## Attachments

None.

## Metadata

- **Issue Type:** QA (bug), sub-task
- **Parent Issue:** #360 (Modernize FEP/BEP Multi-box communications)
- **Milestone:** 5.4.1
- **Project Status:** Ready for QA (Sprint 11)
