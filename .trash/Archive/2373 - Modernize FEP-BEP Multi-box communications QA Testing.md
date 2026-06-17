---
issue: 2373
repo: lightriversoftware/netflex
state: OPEN
title: "[QA][Modernize FEP/BEP Multi-box communications]Feature QA Testing [#360]"
type: "QA (bug)"
milestone: "5.4.1"
author: bmead-LR
assignee: [bmead-LR, dnuzzo-LR, jsloop-LR]
created: 2026-03-04
updated: 2026-06-10
url: https://github.com/lightriversoftware/netflex/issues/2373
---

# Issue 2373 — [QA][Modernize FEP/BEP Multi-box communications] Feature QA Testing [#360]

[View on GitHub](https://github.com/lightriversoftware/netflex/issues/2373)

## Summary

QA sub-task (under parent feature #360) to test the modernized FEP/BEP multi-box communications. The feature replaces the legacy raw-socket FEP/BEP reader/writer communication with a ZeroMQ (zmq) transport layer, which manages connections on top of sockets for far greater stability on imperfect networks. QA needs to enable the feature and verify the fep_bep / bep_fep processes function correctly under zmq.

## Reproduction Steps

To enable and test the new ZeroMQ multibox feature for the fep/bep frm/wtr:

1. Set `NFMD=1` in the `system_defines` on **both** FEP and BEP.
2. Run `systemctl restart netFLEX-md` on all FEPs and BEPs.

## Observed Behavior

Not applicable — this is a QA verification task for a new feature, not a defect report.

## Resolution / Notes

No formal closure yet (issue is OPEN). Enablement details and background were provided across several comments:

> This issue for testing the fep_bep, bep_fep communication processes with zmq. The system_define for enabling this is NFMD=1. Dan Nuzzo would know any more details about zmq.
>
> — @fsantitoro-LR, 2026-03-25

> To turn on the new 'zeromq' Multibox feature for the fep/bep frm/wtr. You need to set this variable to 1 in the system_defines: NFMD=1 ... and do a `systemctl restart netFLEX-md` on all the fep's and bep's. NFMD=1 goes on both FEP and BEP.
>
> From Dan: by default the fep/bep rdr/wtr use raw sockets, and have logic to keep the connection up and functioning, but it's not the best ... zeromq is a layer on top of sockets, that manages the connections, so it should be way more stable in environments where the network is less than perfect.
>
> — @jsloop-LR, 2026-05-11

> Have turned this on in GR environment. holmvm39-44
>
> — @jsloop-LR, 2026-06-10

## Attachments

None.

## Metadata

- **Issue:** 2373
- **State:** OPEN
- **Type:** QA (bug); labels: enhancement, sub-task
- **Parent Issue:** #360 — [FEATURE] Modernize FEP/BEP Multi-box communications
- **Enablement:** `NFMD=1` in system_defines (FEP + BEP), then `systemctl restart netFLEX-md`
- **Milestone:** 5.4.1 (due 2026-08-31)
- **Author:** bmead-LR (Bob Mead)
- **Assignees:** bmead-LR (Bob Mead), dnuzzo-LR (Dan Nuzzo), jsloop-LR (Jimmy Sloop)
- **Created:** 2026-03-04
- **Updated:** 2026-06-10
