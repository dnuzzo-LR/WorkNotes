# Merge `nfd` Daemon into `niimxd`

**Status:** Approved design
**Date:** 2026-05-15
**Branch:** `niimx-nfdb-merge`

## Summary

Fold the `nfd` daemon's functionality into `niimxd`. The standalone `nfd` binary is deleted; its three ZMQ sockets, command dispatch loop, and Ctree DB lifecycle move into `niimxd`'s existing epoll-based event loop. The `nfdb` CLI binary and `libnfdb.so` shared library are unchanged — they keep talking to the same `ipc:///usr/cnc/data/nfdb*.sock` paths, which are now bound by `niimxd` instead of `nfd`.

## Goal & Non-Goals

### Goal
- One fewer process to run, supervise, and trace.
- Single owner of the Ctree DB handle on this host, opening the door (separately, not in this work) to direct event publication from internal niimxd state changes via the existing XPUB socket.

### Non-Goals
- No protocol change. Wire format on `nfdb*.sock` is identical to today.
- No endpoint consolidation. `nfdb.sock`, `nfdb_pub.sock`, `nfdb_sub.sock` stay as separate IPC paths (do **not** multiplex over `niimx.ipc`).
- No removal of the `nfdb` CLI or `libnfdb.so`.
- No wiring of `rdb_notify` to the new XPUB socket. That is tracked separately and intentionally out of scope here.

## Architecture

```
                        niimxd (single process)
  ┌──────────────────────────────────────────────────────────────┐
  │  epoll loop                                                  │
  │    ├── existing fds: signalfd, timerfd(s), inotify, ssh,     │
  │    │   ROUTER on niimx.ipc, …                                │
  │    ├── ZMQ_FD( nfdb_router  on nfdb.sock )     [new]         │
  │    ├── ZMQ_FD( nfdb_xpub    on nfdb_pub.sock ) [new]         │
  │    └── ZMQ_FD( nfdb_xsub    on nfdb_sub.sock ) [new]         │
  │                                                              │
  │  std::map<identity, Client>                  [moved from nfd]│
  │  ct_init() at startup, CloseISAM() at shutdown               │
  └──────────────────────────────────────────────────────────────┘
```

niimxd already uses `epoll_wait` with ZMQ sockets registered via `ZMQ_FD` (see `niimxd.cpp` around lines 789–813, 853). The new sockets follow that same pattern.

## File-Level Changes

| File | Action |
|---|---|
| `cnc/niimx/src/niimxd.cpp` | Extend `NiimxGlobalState` with three socket pointers and their ZMQ_FDs. Bind sockets in init. Add ZMQ_FDs to epoll. Add dispatcher that drains each socket on epoll wake. Add `ct_init()` at startup and `CloseISAM()` at shutdown. Add the per-client `std::map<std::string, Client>`. |
| `cnc/niimx/src/nfd.cpp` | **Delete.** All useful logic is moved; `daemonize()`, signal handling, and the main loop are already provided by niimxd. |
| `cnc/niimx/src/nfdb_cmd.cpp` | Keep and link into niimxd. The bare global `g_xpub` is renamed to `nfdb_g_xpub` (or hidden behind a setter/getter in `nfdb_cmd.h`) so it follows the project's prefix convention. |
| `cnc/niimx/src/nfdb_cmd.h` | Keep. Update `extern` for the renamed XPUB global. |
| `cnc/niimx/src/nfdb_proto.cpp` / `.h` | Keep. Link into niimxd. |
| `cnc/niimx/src/nfdb_client.cpp` | Keep. Still builds the `nfdb` CLI; nothing changes on the wire. |
| `cnc/niimx/src/nfdb_clientlib.cpp` | Keep. Still builds `libnfdb.so`. |
| `cnc/niimx/src/Makefile` | Drop `$(PBIN)/nfd` from `.ALL` and remove its target. Add `nfdb_cmd.o nfdb_proto.o` to `niimxd`'s prerequisite list. Keep `nfdb`, `niimx`, and `libnfdb` targets. |
| `cnc/niimx/src/test_nfdb.sh` | Re-point harness to start `niimxd` instead of `nfd`. Protocol unchanged so the rest of the script stays as-is. |

## Data Flow

ZMQ_FD is edge-triggered. On any epoll wake for these fds the handler must loop until `zmq_getsockopt(ZMQ_EVENTS)` is empty for that socket.

- **`nfdb_router` POLLIN**: receive `[identity][empty][data]` frames; look up or create `Client` in the map; strip trailing CR/LF; `tokenize()` + `dispatch_command()`; send `[identity][empty][wbuf]`; on `quit < 0` erase the entry.
- **`nfdb_xsub` POLLIN**: relay all frames (preserving `ZMQ_SNDMORE`) to `nfdb_xpub` — the same XPUB/XSUB broker behavior nfd has today.
- **`nfdb_xpub` POLLIN**: drain subscription frames (XPUB delivers these whenever a subscriber connects/disconnects). Today nfd does not act on them; we keep that behavior, but we must read them to satisfy edge-trigger semantics.

## Lifecycle & Error Handling

- **DB init**: `ct_init(0, 0, 0)` runs once during niimxd startup, before the event loop begins. Failure is fatal (matches today's nfd behavior).
- **Socket bind**: bind failure on any of the three nfdb sockets is fatal (matches today's nfd behavior).
- **Shutdown**: when the existing niimxd shutdown signal fires, after epoll exit, close `nfdb_xsub`, `nfdb_xpub`, `nfdb_router` (in that order), continue existing niimxd cleanup, then `CloseISAM()`, then `zmq_ctx_destroy()`.
- **Signal handling**: niimxd's signalfd already covers SIGTERM/SIGINT; the nfd `signal_handler` / `g_shutdown` global is dropped.

## Naming & Symbol Collisions

- nfd.cpp defines a non-static `send_frame()`; niimxd has its own ZMQ helpers with the same name. Resolution: rename the nfd-side helper to `nfdb_send_frame`, define it in `nfdb_cmd.cpp`, declare it in `nfdb_cmd.h`. Both niimxd's new router drain handler (response writes) and command handlers (event publish) will call it.
- `g_xpub` becomes `nfdb_g_xpub`.
- `g_shutdown`, `g_foreground`, `g_endpoint`, `g_pub_endpoint`, `g_sub_endpoint` from nfd.cpp are deleted — niimxd already owns shutdown state, daemonization, and endpoint configuration; the three new endpoints become niimxd config (constants or CLI flags, matching how niimxd handles its existing `NIIMX_DEF_ENDPOINT`).
- `daemonize()` from nfd.cpp is deleted (niimxd provides this).

## Configuration

The three nfdb endpoints become niimxd-side constants alongside `NIIMX_DEF_ENDPOINT`, with the same default values nfd uses today:

```
ipc:///usr/cnc/data/nfdb.sock
ipc:///usr/cnc/data/nfdb_pub.sock
ipc:///usr/cnc/data/nfdb_sub.sock
```

If niimxd already accepts CLI flags for its endpoint, three matching flags (or one combined override mechanism) should be added so operators can relocate IPC paths without recompiling. If niimxd does not currently accept such flags, defaults-only is acceptable for this change.

## Testing

- `cnc/niimx/src/test_nfdb.sh`: start `niimxd` (foreground), exercise PING/DBS/DESC/GET/SCAN/SET/PUBLISH against `nfdb.sock`, verify XPUB delivery on `nfdb_pub.sock`. Protocol is unchanged so all existing assertions should pass.
- Manual smoke: confirm niimxd's existing functions (SSH PTY pool, alarm/utilization monitoring, ZMQ on `niimx.ipc`) still work — no regression.

## Build Verification

Using the project's `nmake`:

```
nmake ../../../3b2/bin/niimxd
nmake ../../../3b2/bin/nfdb
nmake $(LDIR)/nfdb.$(LIBTYPE)
```

Confirm `nfd` is no longer produced and is removed from `$(PBIN)`.

## Risks

- **DB blocking inside the event loop.** Ctree calls inside `dispatch_command()` are synchronous and will pause niimxd's whole loop. This matches today's nfd; if profiling shows real latency impact on niimxd's other duties (SSH heartbeats, timers), a worker thread can be introduced in a follow-up — not in this change.
- **Symbol collisions** between nfd helpers and niimxd's existing code. Mitigated by the renames above; a clean compile is the gate.
- **Backwards compatibility** for the `nfd` binary itself: no init scripts or package manifests reference `nfd` today (verified by grep across `*.sh`, `Makefile*`, `*.conf`, `*.nmk`), so its removal is safe.

## Open Items

None at design time. Items to revisit when writing the implementation plan:

- Whether niimxd should accept endpoint-override CLI flags for the three new sockets, or stick with compiled-in defaults.
- Whether `nfdb_cmd.cpp` should expose its XPUB pointer via a setter (cleaner) or a renamed extern (smaller diff).
