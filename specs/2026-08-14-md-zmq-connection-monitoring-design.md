# MD rdr/wtr connection monitoring — ZMQ mode redesign

**Date:** 2026-08-14
**Branch context:** `cp/54.1/md-rdr-wtr-zmq-issues`
**Component:** `cnc/md/src` (FEP↔BEP message and framelink readers/writers)
**Status:** Design approved, pending spec review → implementation plan

---

## Problem

After the TCP→ZMQ (PUSH/PULL) migration of the MD rdr/wtr link processes, a link
that goes quiet for the reader's timeout window triggers a **destructive teardown
storm** instead of a graceful recover:

1. Reader `md_zmqrecv` returns EAGAIN after `2×ping` (60s) of no data.
2. Reader logs "Lost Connection", ssh's `md_lostconn` to the remote (drops a
   `.lostconn` flag file), and calls `fbmr_exit(15)`.
3. `fbmr_exit` `psGrepKill(SIGTERM)`s the co-located writer(s).
4. Writer catches SIGTERM → `fbmw_exit(0)`; it also self-exits if it sees the
   `.lostconn` file.
5. Everything respawns on a ~60s beat.

Observed failure characteristics and root findings from the investigation:

- **The teardown is a vestigial TCP-era reflex.** ZMQ PUSH/PULL already
  auto-reconnects; killing and respawning processes fights ZMQ's own reconnection
  rather than helping.
- **Blast radius is too wide** — one inbound timeout kills writers (a *different*
  pipe than the one that starved), ssh-flags the remote, and respawns the set.
- **No backoff** — `exit(15)` collides numerically with `SIGTERM` (15), so
  `fbmr_exit`'s intended `RESPAWN_SLEEP` is skipped → tight 60s hammering.
- **State persists across restarts** — `BEP_KEY` / `MUX_KEY` shm are zeroed only
  on creation (`del_shm()` is a no-op), so a poisoned/stale state survives an
  app restart; only a full boot / `ipcrm` / reboot clears it.

**Detection is not the problem.** Because the writer sends a PING every
`mdPingFreq` (30s), an alive-and-correctly-connected peer always delivers within
the 60s window, so a timeout is a *real* signal (peer down, not connected, wrong
endpoint, or a >60s transport gap). The problem is the **reaction**.

## Goal

In ZMQ mode, **let ZMQ own the connection lifecycle**. A recv-timeout becomes an
*observability + status* event, not a control action: warn, mark the link down in
shared memory, and keep going. When traffic resumes, mark it back up. No process
kill, no exit, no `md_lostconn` ssh, no respawn.

## Non-goals

- No change to the legacy TCP path (`MdUseZmq != 1`) — it still needs kill/respawn
  because TCP does not self-reconnect. All changes are guarded by `MdUseZmq == 1`.
- No change to the GR active/standby election, the shm-init bug, or the wire
  protocol. (The shm-persistence and GR-role issues are real but tracked
  separately; this design makes them *moot on the ZMQ hot path* by not exiting.)
- No new ZMQ heartbeat wiring — the app-level PING already serves as the
  heartbeat.

## Scope — 8 files

Both directions × {msg, frm} × {rdr, wtr}, all under `cnc/md/src`:

```
l_fep_bep_msg_rdr.cpp   l_fep_bep_msg_wtr.cpp
l_bep_fep_msg_rdr.cpp   l_bep_fep_msg_wtr.cpp
l_fep_bep_frm_rdr.cpp   l_fep_bep_frm_wtr.cpp
l_bep_fep_frm_rdr.cpp   l_bep_fep_frm_wtr.cpp
```

There is no `fep_fep` variant in `md/src`. Shared socket helpers in `l_md_sock.c`
are unchanged.

## Design

All new behavior is inside `if (MdUseZmq == 1)` branches.

### Reader — recv-timeout path

Today (e.g. `l_fep_bep_msg_rdr.cpp:423`): on `md_zmqrecv` returning 0/-1 →
`bulk_links_down` → "Lost Connection" → ssh `md_lostconn` → `fbmr_exit(15)`.

New, ZMQ mode:
- Maintain a per-reader `link_down` flag (local static/state), initialized "up".
- On recv timeout (EAGAIN):
  - If `link_down` is false (down-edge):
    - Emit a **warning** (`TRACE` + `MD_LOG` + raise a standing alarm),
      e.g. `WARN: no data from <peer> in <N>s; awaiting ZMQ reconnect`.
    - `set_bep_socket_stat_idx(idx, BEP_SOCKET_DOWN, <RDR_DATA>, Port)`.
    - Set `link_down = true`.
  - Else (still down): optionally emit a periodic "still down" reminder every
    Nth timeout (default: quiet; reminder cadence is a tunable, off unless set).
  - **Continue the loop** — call `md_zmqrecv` again. The PULL socket stays bound;
    ZMQ reconnects the peer's PUSH underneath.
  - **Do NOT** ssh `md_lostconn`, `psGrepKill` the writer, or `fbmr_exit`.

### Reader — recovery path

On the next successful recv while `link_down` is true (up-edge):
- `set_bep_socket_stat_idx(idx, BEP_SOCKET_UP, <RDR_DATA>, Port)`.
- Log a **recovery** message and clear the standing alarm.
- Set `link_down = false`.
- Process the message normally.

### Writer — send-failure path

Today (e.g. `l_fep_bep_msg_wtr.cpp:454`): `md_zmqsend` returns −1 →
`fbmw_exit(1)`. Writer also self-exits on seeing the `.lostconn` file
(`l_fep_bep_msg_wtr.cpp:445`).

New, ZMQ mode:
- On `md_zmqsend` failure: **warn**, keep the socket, and retry on the next ping
  cycle (ZMQ requeues / reconnects). **Do not exit.**
- **Ignore the `.lostconn` file** in ZMQ mode — nothing creates it on this path
  anymore.
- Optionally mirror the reader's status transitions on repeated send failure
  (`set_bep_socket_stat_idx(..., BEP_SOCKET_DOWN/UP, WTR_DATA, Port)`) using the
  same edge-based flag. (Primary status source remains the reader; writer status
  is secondary/optional.)

## Behavior after the change

- **Transient drop** (network blip, peer briefly restarts): warning + status DOWN,
  ZMQ reconnects, traffic resumes, status UP, recovery logged. No process churn.
- **Genuinely wedged remote** (peer writer alive but mis-connected, or down):
  link stays DOWN with a standing alarm + periodic warnings; the local processes
  keep running and keep trying. Recovering the remote is the remote's own
  responsibility (its local supervision), consistent with "pure reconnect, never
  self-restart in ZMQ mode."
- The `exit(15)==SIGTERM` backoff bug and the shm-never-reinit poisoning are moot
  on the ZMQ hot path because nothing exits.

## Shared-memory contract

`set_bep_socket_stat_idx(idx, status, dtype, port)` (`cnc/utility/src/atch_bep.c`)
continues to be the single writer of `BepSocketStatus[idx-1].{rdr,wtr}_status`.
GR / routing / `dacstat` read it as before; they now see DOWN/UP transitions
driven purely by data flow, not by process life/death.

## Testing

- **Unit-ish / harness:** with a controllable PUSH peer, verify the reader:
  (a) marks DOWN + warns exactly once on the down-edge, (b) does not exit / kill /
  ssh, (c) marks UP + logs recovery on the up-edge, (d) survives repeated
  down/up cycles without leaking or exiting.
- **Writer:** verify send-failure warns and retries without exiting, and ignores
  a stale `.lostconn` file in ZMQ mode.
- **Regression:** with `MdUseZmq != 1`, confirm behavior is byte-for-byte the old
  path (kill/respawn/lostconn intact).
- **Soak:** run a link through repeated peer restarts and a >60s network gap;
  confirm no restart storm, correct DOWN→UP status, and clean logs.

## Open / deferred

- Periodic "still down" reminder cadence — default off; expose as a tunable
  (`MD_*` sysdef) if operators want it.
- The `BEP_KEY`/`MUX_KEY` shm re-init-on-restart bug and the GR failover-detection
  gap are **separate** work items; this design intentionally sidesteps them on the
  hot path rather than fixing them here.
