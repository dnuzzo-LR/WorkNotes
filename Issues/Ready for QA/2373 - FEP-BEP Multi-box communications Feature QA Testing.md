---
issue: 2373
repo: lightriversoftware/netflex
state: OPEN
status: Ready for QA
sprint: Sprint 11
title: "[QA][Modernize FEP/BEP Multi-box communications]Feature QA Testing [#360]"
type: QA (bug)
milestone: 5.4.1
author: bmead-LR
assignee: bmead-LR, dnuzzo-LR, jsloop-LR
created: 2026-03-04
updated: 2026-06-10
url: https://github.com/lightriversoftware/netflex/issues/2373
---

# Issue 2373 — [QA][Modernize FEP/BEP Multi-box communications]Feature QA Testing [#360]

[View on GitHub](https://github.com/lightriversoftware/netflex/issues/2373)

## Summary

QA-testing sub-task of parent feature #360. Test the FEP→BEP / BEP→FEP communication processes that now run over **ZeroMQ (zmq)**. The feature replaces raw-socket reader/writer connection management with a zmq layer for greater stability on imperfect networks.

## Reproduction Steps

1. Set `NFMD=1` in `system_defines` on **both** the FEP and the BEP.
2. Run `systemctl restart netFLEX-md` on all FEPs and BEPs.
3. Exercise the multi-box rdr/wtr communication.

## Resolution / Notes

- **fsantitoro-LR (2026-03-25):** "This issue for testing the fep_bep, bep_fep communication processes with zmq. The system_define for enabling this is NFMD=1. Dan Nuzzo would know any more details about zmq."
- **jsloop-LR (2026-05-11):** "To turn on the new 'zeromq' Multibox feature for the fep/bep frm/wtr. You need to set this variable to 1 in the system_defines NFMD=1 This goes on both FEP and BEP" and "do an 'systemctl restart netFLEX-md' on all the fep's and bep's." Per Dan: "by default the fep/bep rdr/wtr use raw sockets, and have logic to keep the connection up and functioning ... zeromq is a layer on top of sockets, that manages the connections, so it should be way more stable in environments where the network is less then perfect."
- **jsloop-LR (2026-06-10):** "Have turned this on in GR environement. holmvm39-44."

## Attachments

None.

## Metadata

- **Issue Type:** QA (bug) (sub-task)
- **Parent Issue:** #360 (Modernize FEP/BEP Multi-box communications)
- **Process Group:** Other
- **Priority:** Required this Release
- **Milestone:** 5.4.1
- **Project Status:** Ready for QA (Sprint 11)

## My Notes

<!-- Your notes below are preserved across syncs. -->
