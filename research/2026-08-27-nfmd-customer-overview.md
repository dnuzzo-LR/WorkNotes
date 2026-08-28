# Setting `NFMD=1`: Modernized Mediation Communications

## Overview

The `NFMD` setting in your `system_defines` file controls **how the mediation
processes communicate between machines**. Setting `NFMD=1` switches these
communications from the traditional networking method to a modern, more robust
messaging library (ZeroMQ).

This document explains, in plain terms, what changes when you enable this
setting and why it matters.

## What this affects

netFLEX uses a set of internal "mediation" processes to move information
between machines in a redundant pair and out toward the network elements. These
processes maintain the network connections that carry alarms, provisioning, and
status traffic.

`NFMD` selects the underlying communications technology those processes use:

- **`NFMD` not set (default):** traditional networking — a direct, hand-managed
  network connection for each link.
- **`NFMD=1`:** modern messaging library — the same links are carried over an
  industry-standard messaging layer (ZeroMQ).

No other configuration changes are required. The setting is read once when the
processes start.

## What changes with `NFMD=1`

The information exchanged is the same; only the *transport underneath* changes.
The benefits of the modern transport are:

- **More reliable failure detection.** The modern transport uses built-in
  "heartbeat" signals that quickly and reliably notice when the far end has gone
  away — even in cases where a connection dies silently. The system then
  re-establishes the link automatically.

- **No indefinite hangs.** Sends and receives are bounded by timeouts, so a
  stalled or missing peer is reported promptly rather than causing a process to
  wait forever.

- **Cleaner reconnection.** Connections re-establish themselves in the
  background, reducing manual recovery and restart churn.

- **A modern, maintainable foundation.** Moving off the legacy, hand-built
  socket code onto a well-supported messaging library improves long-term
  supportability and positions the platform for further communication
  enhancements.

From an operator's perspective, the observable behavior of the system is
unchanged — alarms, provisioning, and status continue to flow exactly as
before. The improvements are in resilience and recoverability of the underlying
links.

## How to enable

Add the following line to your `system_defines` file:

```
NFMD=1
```

Then restart (or allow the mediation processes to cycle) so the setting takes
effect. To return to the traditional transport, remove the line (or set it to a
value other than `1`).

## Things to know

- **All-or-nothing per system.** The setting applies to the mediation link
  processes as a group; you do not enable it per individual link.
- **Both machines in a pair should match.** For a redundant pair, both sides
  should use the same `NFMD` setting so the two ends speak the same transport.
- **Firewall / ports.** The modern transport still communicates over standard
  TCP to the same peers, so no new firewall rules are typically required beyond
  the existing mediation ports.

## Summary

| Setting        | Transport                        | Result                                   |
|----------------|----------------------------------|------------------------------------------|
| `NFMD` not set | Traditional network connections  | Existing behavior (default)              |
| `NFMD=1`       | Modern messaging library (ZeroMQ)| More robust failure detection & recovery |

Enabling `NFMD=1` upgrades the communication layer beneath netFLEX's mediation
processes to a more resilient, modern foundation — with no change to the
information carried or to day-to-day operation.
