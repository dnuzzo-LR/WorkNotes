# Route iimx traffic to niimxd — design

Date: 2026-09-11
Repo: `~/Git/netflex`, branch `route-iimx-traffic-to-niimxd`
Author: Dan Nuzzo

## Goal

Add a single `system_defines` toggle that routes all iimx command traffic away from
the legacy transports (SysV message queue for local, `mcmd ssh` fork-per-command for
remote) and into `niimxd` over ZMQ.

## Background

`cnc/utility/src/iimx_snd.c` is the client side of iimx. It has two legacy transports:

- **Local** — SysV message queue keyed `IIMX_IKEY`, reply selected by
  `mtype = getpid()`, multi-segment replies signalled by `imsg.mcont`.
- **Remote** — `iimx_sendx_remote()` (`:69`) shells out to
  `mcmd ssh -o BatchMode=yes <host> '<cmd>'` via `vPopen`, one fork plus one SSH
  handshake per command. `iimx_sendx_remote_old()` (`:87`) is the dead HTTP/HTTPS
  variant. `iimx_connect()` (`:1799`) opens port-80 sockets for the batch/swarm paths.

There are ~271 call sites across ~30 files, so the toggle must live inside
`iimx_snd.c`, never at the callers.

`niimxd` (`cnc/niimx/src/niimxd.cpp`) is **not** REQ/REP. It binds a `ZMQ_ROUTER`
(`:820`) feeding a global `requestQ` drained by N bchannels. Nothing keys on "one
request in flight per identity", so a `ZMQ_DEALER` client may pipeline N commands and
read N replies. Replies carry the echoed `msgid` (`:1214-1226`) and arrive in
*completion* order, not send order — the same async fan-out the msgq pool paths
already rely on. The PUSH/PULL-shaped callers therefore map onto DEALER/ROUTER with
no change to the async model.

`niimxd` also already owns persistent libssh bchannels per remote host
(`bchannel::m_ssh_session`, `m_remote_host`), so routing remote traffic through it
removes the per-command ssh spawn entirely.

## Decisions

| # | Decision |
|---|---|
| 1 | Toggle covers **everything** — local msgq paths and remote paths alike. |
| 2 | `remote_req()` (`:281`) is **excluded**. It is generic `/fcgi/<service>`, single caller `cnc/rcmd/src/iisnd.c:334`, carries non-iimx services. Stays on HTTP. |
| 3 | `rspfile` parity via a **new protocol frame** (option b), not by ignoring it. |
| 4 | Toggle-on failure is a **hard fail**. No fallback to msgq, ever. |
| 5 | **One switch**, `USE_NIIMXD=`. No bitmask, no per-class defines. |

## Current state of `niimxlib.c`

`cnc/utility/src/niimxlib.c` already exists and is already listed in `OBJ_INC`
(`util.mk:395`). It provides a single-shot
`niimx(host, command, char **rsp, int *nbytes, int tmout)` with the correct request
frame layout and a correct `mcont` reassembly loop. It has no callers yet.

It cannot be used as-is:

| line | defect |
|---|---|
| 33, 132-133 | `zmq_ctx_new`/`zmq_ctx_destroy` per call; ctx teardown blocks, unusable for pool/batch |
| 59 | `msgid` hardcoded to `1` — no demux, so no pipelining |
| 55 | connects to the `NIIMX_ENDPOINT` literal, ignoring the `NIIMX.ENDPOINT=` sysdef read at `:37` — dead override |
| 46 | identity `niimxlib-<pid>`; two concurrent sockets in one process collide and the ROUTER misroutes |
| — | no `rspfile` frame |
| — | no `"Service Unavailable"` hard-fail mapping |

The work is to grow this file into the real client rather than add a new one.

## Design

### 1. Transport client — `niimxlib.c` / `niimxlib.h`

All externally visible symbols take the `niimx_` prefix.

```c
int  niimx_enabled(void);                     /* cached get_sysdef_int("USE_NIIMXD=") */
int  niimx_avail(void);                       /* access() on endpoint -> fast hard fail */
int  niimx_send(const char *host, const char *cmd, int32_t msgid,
                int32_t tmout, int32_t rspfile);
int  niimx_recv(int32_t *msgid, char **rsp, int *nbytes, int tmout_ms);
int  niimx_xact(const char *host, const char *cmd, int rspfile,
                char **rsp, int *nbytes, int tmout);
```

- Singleton `ctx` plus one `ZMQ_DEALER`, created on first use and never torn down
  per call.
- Identity `niimxlib-<pid>-<seq>`, unique per socket within a process.
- Endpoint resolved once from `NIIMX.ENDPOINT=`, falling back to
  `ipc:///usr/cnc/data/niimx.ipc`.
- Monotonic msgid counter, independent of `_IIMXmyid`.
- `niimx_recv()` drains one complete `mcont` chain and reports the echoed msgid.
  A msgid absent from the pending table is dropped and reading continues — this is
  how a stale reply from an already-timed-out request is discarded instead of being
  misattributed to a later command.
- The async and pool paths reuse the existing `iimx_q_entry` / `iimx_q_find` pending
  table shape (`iimx_snd.c:820-857`), keyed off `niimx_recv` instead of `msgrcv`.

Socket lifetime note: a msgq is kernel-global, a ZMQ socket is process-local heap.
`iimx_q()` → `iimx_poll()` therefore requires the singleton to persist across calls.
Verified safe: those two are called only from `cnc/rcmd/src/ilua.c:1570,1537`, same
process, no `fork()` between them.

### 2. Entry points in `iimx_snd.c`

`static int Use_Niimxd = -1;` with `niimx_enabled()` performing
`get_sysdef_int("USE_NIIMXD=", &Use_Niimxd, 0)` once. Each entry point gains a guard
at the top:

| function | line | maps to |
|---|---|---|
| `iimx_sendx_call` | 451 | `niimx_xact`, carries `rspfile` |
| `iimx_sendxn` | 650 | `niimx_xact`, truncate into caller buffer |
| `iimx_q` / `iimx_poll` | 932 / 858 | `niimx_send` N, `niimx_recv` + pending table |
| `iimx_local` | 2539 | pipelined send-N / recv-N |
| `iimx_sendx_pool` | 2941 | pipelined send-N / recv-N |
| `iimx_sendx_pool_v2` | 2700 | pipelined send-N / recv-N |
| `iimx_sendx_remote` | 69 | `niimx_xact` with `host` frame; replaces `mcmd ssh` fork |
| `iimx_sendx_batch_wk` | 1850 | `host` frame per entry; replaces `iimx_connect` port-80 socket |

Three functions need no divert of their own:

- `iimx_multi_fp` (`:1459`) is a thin wrapper over `iimx_sendx_batch` (`:2680`),
  which forwards to `iimx_sendx_batch_wk`. Covered.
- `iimx_swarm` (`:2168`) spawns `/usr/cnc/bin/swarm` running `iisnd -c '<cmd>'`
  (`:2199-2202`), and `iisnd` calls `iimx_sendx` (`cnc/rcmd/src/iisnd.c:342`). It
  inherits the toggle transitively once `libinc.so` ships, locally and — via
  `ssh <host> -- iisnd` — on the far end too.
- `remote_req` (`:281`) is excluded by decision 2.

### 3. Protocol change

Request frame layout gains `rspfile`:

```
identity | empty | host | msgid(4B) | mpid(4B) | tmout(4B) | rspfile(4B) | command
```

Response layout is unchanged: `identity | empty | msgid(4B) | mcont(4B) | payload`.

In `niimxd.cpp`:

- parse the new frame at `:965` and store it on `Request`;
- carry it to `bchannel::m_rspfile` at dispatch (`:1595`);
- consume it only on the success completion at `:1999` — write `bc.m_response` to
  `/usr/cnc/tmp/iimx-rsp<pid>.<msgid>` and send that path as the body.

All reject and error responses (`:1000, 1008, 1019, 1025, 1687, 1784, 1819, 2389,
2432`) stay inline regardless of `rspfile`; they are short, and the client matches
error prefixes against the body.

This is a breaking wire change. `niimxd` and `libinc.so` must deploy together.
`cnc/niimx/src/niimx.cpp` gains a `-R` option to exercise the new frame.

Client side, `rspfile=1` behaviour matches imsgx exactly: read the returned path,
`unlink` it, hand the contents back as `*rsp` (`iimx_snd.c:604-620`). The three live
callers are `cnc/restjson/src/rj_gui_cgi.cpp:321, 382, 578` (`RMTdump`,
`RMTalm_list`).

### 4. Error and congestion mapping

`iimx_sendx_call` currently branches on `"IMSGX^Internal Error. Core Dump"`,
`"IMSGX^Internal Error."` and `"FAIL:Service Unavail"` (`:578-600`). `niimxd` emits
`"NIIMX^..."` (`niimxd.cpp:1000-1027`), which would otherwise fall through and reach
callers as a *successful* response body.

- `niimx_xact` maps any `NIIMX^`-prefixed body to `rc = -1` with the body preserved,
  matching the existing `IMSGX^Internal Error.` branch.
- The congestion pre-check at `:474` tests `/usr/cnc/data/niimx.congested` when the
  toggle is on, rather than `/usr/cnc/data/iimx.congested`.
- Unreachable daemon → `rc = -1`, `*rsp = "Service Unavailable"`. `zmq_connect` to an
  IPC endpoint succeeds lazily even with nothing bound, so liveness is established by
  `access()` on the socket path plus a short `ZMQ_RCVTIMEO` on the first response —
  never by waiting out the caller's `tmout`, which can be 600s. `niimx_avail()` strips
  the `ipc://` scheme prefix before the `access()` call, and returns "available"
  without checking for any non-`ipc://` endpoint, since only IPC endpoints have a
  filesystem path.

### 5. Build

`libinc.so` is built by the stock `mklib` rule (`global.nmk:318`) with no `-lzmq`.
`niimxlib.o` is already in `OBJ_INC` but unreferenced, so its undefined `zmq_*`
symbols sit harmlessly in the `.so` today. Once `iimx_snd.c` calls into it, every
executable linking `-linc` would need `-lzmq`.

Fix: a local `mklib_inc : .USE` clone in `util.mk` carrying `-lzmq`, used by the
`libinc` target at `:397`, so `DT_NEEDED` on libzmq propagates and no consumer
makefile changes. This is the sanctioned per-library link-flag pattern in this tree.

### 6. Test

A/B harness running each case under `USE_NIIMXD=0` and `USE_NIIMXD=1` and diffing:

1. single sync command via `iimx_sendx`;
2. `rspfile=1` against the three `rj_gui_cgi.cpp` payloads;
3. pool fan-out with deliberately staggered completion, asserting out-of-order
   replies are matched by msgid;
4. a deliberate timeout, asserting the late reply is dropped and not misattributed to
   the next command on the same socket;
5. remote command via the `host` frame;
6. daemon stopped, asserting `"Service Unavailable"` returns promptly rather than
   after `tmout`.

## Risks

- **Remote config parity.** `niimxd` rejects any host not in `G.remote_host_list`
  and honours `G.host_available` (`niimxd.cpp:1013-1027`). With a single switch,
  remote goes live with everything else, so a gap in that list turns every remote
  call into `"Unknown host"`. This is the first thing that will bite.
- **Segment count on large payloads.** `mcont` segments are 25KB
  (`niimxd.cpp:1205`); a multi-MB `RMTdump` becomes many round-trips over one DEALER.
- **Deploy is not partial-safe.** Old `libinc.so` against new `niimxd`, or the
  reverse, mismatches the frame count and hangs. Binary and daemon must ship
  together.

## Out of scope

- `remote_req()` and its `/fcgi/<service>` transport.
- `iimx_sendx_remote_old()` — dead code, left alone.
- Removing the msgq paths. The toggle defaults off and both transports stay in the
  binary.
