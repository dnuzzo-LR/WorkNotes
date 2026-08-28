# grd ZMQ Event Publishing — Design (Step 1)

**Date:** 2026-08-28
**Repo:** netflex (`hack-netflex` worktree, branch `grd-niimx-events`)
**Components:** `cnc/gr` (grd), `cnc/niimx` (niimxd)

## Problem

`gr_daemon2.cpp` monitors and exchanges data with its mate entirely over SSH. Every
mate interaction funnels through `grSystem()` (gr_daemon2.cpp:1451), which shells out
to `/usr/cnc/bin/scmd ssh ... -o ConnectTimeout=N` and bounds itself with `SIGALRM`.

Four categories of mate traffic exist today:

1. **Liveness probes** — `grd_remote_cnc_up_check()` (`test -f /usr/cnc/.CNC_UP`,
   gr_daemon2.cpp:3257) and `grd_remote_start_check()` (`incps -m /bin/start`,
   gr_daemon2.cpp:3303).
2. **State file exchange** — `.GR_DATA` `test -f` / `scp` both directions with mtime
   arbitration (gr_daemon2.cpp:3421-3712).
3. **Remote commands** — `gr --active <bep>` on the mate (gr_daemon2.cpp:917),
   `multi_gr check`.
4. **Remote DB pull** — `obtain_remote_db()` (gr_daemon2.cpp:3797).

The long-term goal is to move this onto the libzmq pub/sub bus that niimxd already
brokers. This spec covers **step 1 only**.

## Scope of Step 1

**In scope:**

- `grd` publishes its state as events onto niimxd's local IPC bus.
- niimxd gains a loopback TCP XPUB endpoint so external subscribers can attach.

**Explicitly out of scope:**

- No SSH call is removed. No behavior changes.
- No cross-host ZMQ traffic. The bus stays local to each host.
- No subscriber consumes these events yet.
- No remote publishers (`NIIMX.SUB_TCP_ENDPOINT` not added).

With `GRD_ZMQ_PUB=0` (the default), `grd` behavior is identical to today.

## Existing Infrastructure

niimxd binds an XPUB/XSUB pair and relays between them with a hand-rolled proxy:

- XPUB `ipc:///usr/cnc/data/nfdb_pub.sock` — subscribers connect here (niimxd.cpp:872)
- XSUB `ipc:///usr/cnc/data/nfdb_sub.sock` — publishers connect here (niimxd.cpp:883)
- Relay: `niimx_handle_nfdb_xsub()` (niimxd.cpp:1132), `niimx_handle_nfdb_xpub()` (niimxd.cpp:1161)

Wire format is two frames: `[topic][YAML body]`. The `db.` topic prefix is reserved
for nfdb record events; `app.` is for application events (nfdb_cmd.cpp:2506).

`cnc/otn_port/src/otn_port_zmq_pub.c` is an existing, documented, opt-in publisher
using exactly this pattern. The new grd module is a clone-and-adapt of it.

## Design

### Component 1: `gr_zmq_pub` module

**New files:**

- `cnc/gr/src/gr_zmq_pub.h`
- `cnc/gr/src/gr_zmq_pub.cpp`

`cnc/gr` has no `include/` directory (unlike `otn_port`); headers live beside the
sources and are found via the compiler's current-directory search plus the
`.SOURCE.h` entries in `grdsrc.mk`.

**API:**

```c
int  grpub_init(void);                 /* honors GRD_ZMQ_PUB sysdef; no-op when 0/absent */
void grpub_close(void);                /* idempotent */
int  grpub_event(const char *topic, const char *yaml_fmt, ...);  /* printf-style body */
```

`grpub_event()` is a no-op returning 0 when the module is disabled, so call sites are
unconditional — no `if (zmq_enabled)` guards scattered through `gr_daemon2.cpp`.

**Inherited from `otn_port_zmq_pub.c`:**

- Module-local `static` context and `ZMQ_PUB` socket
- `zmq_connect` to `ipc:///usr/cnc/data/nfdb_sub.sock`
- `ZMQ_LINGER = 0` so shutdown never blocks
- 1 s slow-joiner `sleep()` at init
- EINTR-retry `zmq_send` wrapper
- Best-effort: send failures log at `TRACE(D3)` and never propagate

**Deliberate differences:**

- **No `pthread_mutex`.** `grd` publishes only from the main loop, single-threaded.
  otn_port needs the lock because `pg_listen` runs on its own thread.
- **`ZMQ_SNDHWM = 1000` set explicitly** rather than relying on the ZMQ default.
- **Init ordering:** `grpub_init()` is called from `main()` *after*
  `begin_gr_daemon()` (gr_daemon2.cpp:2783) daemonizes. A ZMQ context created before
  that fork is unusable in the child.
- **Teardown:** `grpub_close()` is called before the self-restart `execl()` at
  gr_daemon2.cpp:1583, and registered via `atexit()`.

**Build** — `grdsrc.mk`: add `gr_zmq_pub.o` to the `grd` target and append `-lzmq`.
`grsvc` already links `-lzmq -lzreq` (grdsrc.mk:33), so the library is available.

### Component 2: transition detection

**Problem it solves.** `ACTIVE_mode` (gr_daemon2.cpp:2301) and `STANDBY_mode`
(gr_daemon2.cpp:2606) each contain a near-duplicate ladder handling the
`grd_check_remote_host()` result. The two ladders differ: `STANDBY_mode` *returns* on
`GR_CONN_ERROR` and `GR_APP_DOWN` rather than latching `RmtWorking`. A publish call
placed inside a ladder branch would fire on some paths and not others, and the two
copies would drift.

**Solution.** One function owns transition detection:

```c
static void grd_publish_state(GRGLOBAL& g, int check_result);
```

Both loops call it immediately after `grd_check_remote_host()`. It holds `static`
copies of the last-published `RmtWorking`, `LocalWorking`, `Mode`, and `rdb_ok`,
compares against current values, and publishes only what changed.

Snapshot comparison is immune to which ladder branch ran or whether the ladder
returned early. Neither loop's control flow is modified.

### Component 3: event schema

**Common envelope**, present on every event:

```yaml
host: nfx-a
role: ACTIVE
pid: 12345
seq: 4471
ts: 1756425600
```

`seq` increments monotonically per publish. ZMQ pub/sub has no delivery guarantee and
no replay; `seq` is how a subscriber detects it missed events.

**Topics:**

| Topic | Trigger | Body beyond envelope |
|---|---|---|
| `app.gr.mate.state` | `RmtWorking` change | `mate`, `state: up\|conn_error\|app_down`, `reason`, `check_result` |
| `app.gr.local.state` | `LocalWorking` change | `state: up\|error`, `reason` |
| `app.gr.rdb.state` | probe result change / outage confirmed | `state: up\|down\|confirmed_down`, `fail_secs`, `fail_timeout` |
| `app.gr.role` | `Mode` change or switch return | `from`, `to`, `cause: mode_switch\|single_switch\|forceswitch\|rdb_timeout`, `switch_enabled` |
| `app.gr.data` | `.GR_DATA` arbitration outcome | `action: in_sync\|sent\|pulled\|created_remote\|diff_index`, `lcl_mtime`, `rmt_mtime`, `fail_index` |
| `app.gr.bep` | BEP flip decision | `bep`, `from`, `to`, `cause: up_cnt\|active_down\|rdr_down`, `up_cnt_diff` |
| `app.gr.transfer` | pass fork / reap | `pass`, `group`, `phase: start\|end`, `pid`, `exit_code` |
| `app.gr.heartbeat` | periodic | full snapshot of all current values, plus `wakeup`, `conn_error`, `wait_timers` |

**Call sites outside the choke point.** Three topics cannot be derived from snapshot
comparison and need in-place calls:

- `app.gr.data` — `grd_check_remote_host()` (gr_daemon2.cpp:3421-3712). The outcomes
  are distinct branches with no shared variable to snapshot. Six call sites.
- `app.gr.bep` — `grd_sync_data()` (gr_daemon2.cpp:834-925), at the existing
  `TRACE(0,"INFO: Flip to BEP...")` points.
- `app.gr.transfer` — the `fork()` at gr_daemon2.cpp:2955 and the `PidTab` reap site.

**Heartbeat cadence.** The outer loop runs at `GR_WAKEUP` (20-300 s, default 20) in
steady state but drops to `GR_RDB_RETRY_INTERVAL` (2 s) during an RDB outage —
precisely when event volume should not spike. The heartbeat is therefore gated on its
own timer: publish only if at least `GRD_ZMQ_HEARTBEAT` seconds have elapsed since the
last heartbeat, default 30. Transitions always publish immediately regardless.

**YAML escaping.** Bodies embed hostnames, alarm text, and `LastGrCmdOutput`. A helper
quotes and escapes interpolated strings rather than trusting callers. Must handle `"`,
`\`, newline, and a leading `-`.

### Component 4: niimxd TCP XPUB endpoint

A ZMQ socket may bind multiple endpoints, so the existing XPUB takes a second bind
alongside the IPC one at niimxd.cpp:872:

```c
#define NIIMX_DEF_NFDB_PUB_TCP_ENDPOINT "tcp://127.0.0.1:5560"
```

Config field `pub_tcp_endpoint` in `NiimxConfig`, loaded from sysdef
`NIIMX.PUB_TCP_ENDPOINT`, defaulting to the loopback endpoint above. Empty string
disables the bind. Port 5560 is free — the only ZMQ TCP port currently in the tree is
`tcp://*:5000`.

Three properties:

- **Bind failure is non-fatal.** If the port is taken, log at `Err` and continue with
  IPC only. The TCP endpoint is a convenience; nothing may lose the local bus because
  of it. Existing IPC binds keep their current fatal-on-failure behavior.
- **The broker is untouched.** `niimx_handle_nfdb_xsub()` and
  `niimx_handle_nfdb_xpub()` operate on the socket, not the endpoint. Subscribers
  arriving over TCP are served by the same relay and the same `ZMQ_FD` epoll
  registration. No new fd, no new epoll entry.
- **Startup-only.** `niimx_reload_config()` (niimxd.cpp:2141) will not rebind.
  Rebinding a live XPUB would drop connected subscribers, which is worse than
  requiring a restart. Documented in the config comment; no unbind/rebind logic.

### Security posture

Loopback binding is deliberate. The XPUB bus carries `db.*` events — every nfdb record
add/update/delete with field-level diffs (nfdb_cmd.cpp:1230-1362) — and now `app.gr.*`
state. ZMQ provides no authentication or encryption unless CURVE is configured, and
subscription filtering is advisory: a subscriber sending `SUBSCRIBE ""` receives every
topic on the socket. There is no per-topic access control.

Binding to `127.0.0.1` grants external *process* access without external *host*
access. When step 2 needs real cross-host subscribers, that change must be evaluated
together with CURVE authentication — not by simply widening this bind to `*`.

## Configuration

| Key | Default | Effect |
|---|---|---|
| `GRD_ZMQ_PUB=` | `0` (off) | Master switch for grd publishing |
| `GRD_ZMQ_HEARTBEAT=` | `30` | Heartbeat floor, seconds |
| `NIIMX.PUB_TCP_ENDPOINT` | `tcp://127.0.0.1:5560` | Loopback TCP XPUB; empty disables |

`GRD_ZMQ_PUB` defaults off, matching `OTN_PORTD_ZMQ_PUB`. An install taking this build
sees no behavior change until an operator opts in.

## Failure Modes

The load-bearing requirement: **GR failover must never acquire a dependency on
niimxd.**

| Condition | Behavior |
|---|---|
| niimxd absent at grd start | `zmq_connect` to an IPC path with no listener succeeds in ZMQ. Messages queue to `ZMQ_SNDHWM` then drop. No error, no block. |
| niimxd dies mid-run | Same — PUB sockets discard when no peer is connected. |
| niimxd restarts | ZMQ reconnects automatically; grd notices nothing. |
| Any `grpub_event()` failure | Logs at `TRACE(D3)`, returns without propagating. No call site checks the return value. |
| TCP port 5560 already bound | niimxd logs and continues with IPC only. |

The 1 s slow-joiner `sleep()` runs once inside `grpub_init()` after daemonization. It
delays grd startup by one second and has no other effect.

## Testing

- **Integration:** `cnc/gr/src/test_gr_zmq_pub.sh`, modeled on the existing
  `cnc/niimx/src/test_nfdb.sh`. Subscribes to `app.gr.` and asserts topic and YAML
  shape.
- **Disabled path:** `grpub_event()` with the module disabled must be a no-op
  returning 0.
- **YAML escaping:** strings containing `"`, `\`, newline, and a leading `-`.
- **Transition detection:** `grd_publish_state()` is pure snapshot comparison. Testable
  by calling it with sequences of `check_result` values and asserting the resulting
  publish set — no sockets involved.
- **Manual:** set `GRD_ZMQ_PUB=1`, subscribe, pull the mate's network, and confirm
  `app.gr.mate.state` fires once on the transition rather than once per loop pass.

## Build Environment Note

`BASE` and `VPATH` must match the worktree root before any nmake run:

```
export BASE=/home/dan/Git/hack-netflex
export VPATH=$BASE:<other-paths>
```

## Future Steps (not this spec)

- **Step 2:** cross-host transport. Options surveyed: niimxd binds a routable TCP
  XPUB; niimxd-to-niimxd bridge re-injecting mate events under a `peer.` prefix; or
  grd owning a direct cross-host socket. The bridge is the cleanest for future
  consumers. CURVE authentication belongs with whichever is chosen.
- **Step 3:** replace SSH liveness probes with subscription-based mate liveness.
- **Step 4:** replace `.GR_DATA` scp arbitration with event-driven state exchange.
