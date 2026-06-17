---
issue: 360
repo: lightriversoftware/netflex
state: OPEN
status: Ready for QA
sprint: Sprint 7
title: Modernize FEP/BEP Multi-box communications
type: Development (enhancement) / Feature
process_group: Core Software
release: 5.3.1
milestone: 5.4.1
author: tmasse-LR
assignee: dnuzzo-LR, jsloop-LR
created: 2025-12-02
updated: 2026-06-10
url: https://github.com/lightriversoftware/netflex/issues/360
---

# Issue 360 — [FEATURE] Modernize FEP/BEP Multi-box communications

[View on GitHub](https://github.com/lightriversoftware/netflex/issues/360)

## Summary

Feature to modernize the FEP↔BEP multi-box communication path. Today the FEP/BEP reader/writer use raw sockets with custom logic to keep the connection alive; this feature layers **ZeroMQ (zmq)** on top of sockets to manage the connections, making multi-box communication far more stable on imperfect networks. Enabled via the `NFMD=1` system_define. Category Core Software; broken into QA Testing (#2373) and Documentation (#2375) sub-tasks.

## Resolution / Notes

- Feature is functional and being tested (see #2373). Enable with `NFMD=1` in `system_defines` on **both** FEP and BEP, then `systemctl restart netFLEX-md` on all FEPs/BEPs.
- **jsloop-LR (2026-06-10):** Turned on in the GR environment (holmvm39–44).

## Attachments

None.

## Metadata

- **Issue Type:** Development (enhancement) / Feature
- **Category:** Core Software
- **Support Level:** General
- **Targeted Release:** 5.3.1 (milestone 5.4.1)
- **Sub-tasks:** #2373 (Feature QA Testing), #2375 (Documentation)
- **Project Status:** Ready for QA (Sprint 7)
