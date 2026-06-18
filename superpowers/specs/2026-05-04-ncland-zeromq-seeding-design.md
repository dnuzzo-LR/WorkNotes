# ncland: ZeroMQ Transport + Warehouse Seeding & otn_portd Subscription

**Date:** 2026-05-04
**Status:** Draft (awaiting review)
**Component:** `cnc/ncland`
**Related design doc:** `cnc/ncland/src/NCLAND-DESIGN.adoc`
**Related TODO:** `cnc/ncland/src/TODO.adoc`

---

## 1. Background

`ncland` is the new C++ replacement for `clan.c` — a multi-NE CLI agent built on
libssh + epoll + a two-tier (warehouse + workers) process model. The original
design (see `NCLAND-DESIGN.adoc`) used POSIX message queues for caller ↔ ncland
IPC and assumed an externally-managed NE list.

Two changes are required:

1. **Transport swap.** Replace POSIX MQ (`<mqueue.h>` + `-lrt`) with **ZeroMQ
   over Unix-domain `ipc://`** for the external caller boundary. Workers stay on
   their existing Unix-socket (`SCM_RIGHTS`) channel to the warehouse — ZeroMQ
   does not cross the warehouse-to-worker boundary.
2. **Self-seeding warehouse + live event subscription.** The warehouse must
   determine its own NE set at startup and react to runtime NE-state changes:
   - **Seed:** iterate `frame_link` + aal shm (mirroring `cnc/sdi/src/m_clan.c`'s
     iteration), filter by **`/usr/cnc/features/ncland.json`** (a dtype
     allowlist), and open a connection per matched NE.
   - **Runtime:** subscribe to **Postgres `rdb_notify`** channels published by
     `cnc/otn_port/src/otn_portd.c` and react to NE provisioning / equipment
     state events.

`m_clan` continues to fork legacy `clan` processes for dtypes **not** in
`ncland.json`. Both `m_clan` and `ncland` consult the same file to partition the
NE population.

---

## 2. Goals

- Drop POSIX MQ entirely from `ncland`. No `mq_*` symbols, no `<mqueue.h>`, no
  `-lrt` in the link.
- Caller-facing IPC uses ZeroMQ ROUTER bound on `ipc:///var/run/ncland/ctl.sock`.
  Workers receive commands and emit responses via existing Unix-socket framed
  protocol with the warehouse — ZeroMQ awareness is confined to the warehouse.
- Warehouse self-seeds at startup: parses `ncland.json`, iterates frame_link +
  aal shm, opens connections for matched NEs.
- Warehouse subscribes to `rdb_notify` (via `librdb_notify`) for six NE
  lifecycle events: CREATE, DELETE, ENABLE, DISABLE, DTYPE_CHANGE, IP_CHANGE.
- `m_clan` modified to skip dtypes in `ncland.json`; legacy `clan` continues to
  serve all other dtypes unchanged.

---

## 3. Non-Goals

- No changes to `otn_portd` itself. It continues to publish via PG NOTIFY
  through the existing `librdb_notify` machinery.
- No new ZeroMQ usage beyond the caller-to-warehouse boundary. No PUB/SUB bus,
  no inproc broker, no per-worker ZMQ endpoints.
- No persistence of warehouse state. On restart, the warehouse re-seeds from
  scratch; this is idempotent because `warehouse_open_conn(neid)` is.
- No hot reload of `ncland.json`. Read once at startup; changes require
  warehouse restart.
- No fallback to MQ. Cutover is complete; the abstraction-shim alternative
  (Approach 2 in brainstorming) is rejected as YAGNI.

---

## 4. Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                    ncland_warehouse  (parent)                    │
│                                                                  │
│   Startup sequence:                                              │
│   1. Load /usr/cnc/features/ncland.json → dtype allowlist set    │
│   2. zmq_ctx_new() → bind ROUTER on ipc:///var/run/ncland/ctl    │
│   3. rdb_notify_subscribe(NE_PROVISION channel)                  │
│   4. SEED PASS — iterate frame_link + aal shm; for each NE       │
│      whose dtype ∈ allowlist (and domain/IP filters pass),       │
│      ssh_connect(), assign to worker                             │
│   5. Enter epoll loop                                            │
│                                                                  │
│   Steady-state event sources (epoll):                            │
│   • signalfd      — SIGCHLD / SIGTERM / SIGHUP                   │
│   • zmq_ctl FD    — caller commands (OPEN / CMD / CLOSE)         │
│   • rdb_notify FD — otn_portd NE lifecycle events                │
│   • worker ctl_socks — response forwarding from workers          │
└──────────────────────────┬──────────────────────────────────────┘
                           │ SCM_RIGHTS + framed control
              ┌────────────┴─────────────┐
              ▼                          ▼
        ncland_wrkr  …                ncland_wrkr
       (epoll over assigned net_fds; per-conn libssh I/O)
```

`m_clan` continues to fork `clan` for dtypes **not** in `ncland.json`. Both
processes consult the same file.

---

## 5. Components

| File | Role |
|---|---|
| `ncland_zmq.cpp` | ZMQ context lifecycle. Bind ROUTER on `ipc:///var/run/ncland/ctl.sock`. Wrap `zmq_send`/`zmq_recv` (`ZMQ_DONTWAIT`). Expose the `ZMQ_FD` for epoll. Multipart frame protocol: `[caller_identity][empty][ncland_cmd_msg_t bytes]`. Sets `ZMQ_SNDHWM` / `ZMQ_RCVHWM` = 4096. Replaces all `ncland_mq_*` symbols. |
| `ncland_seed.cpp` | One-shot startup pass. `ncland_seed_load_filter(path, dtype_allow)` parses `ncland.json` into `std::unordered_set<int>`. `ncland_seed_run(wh)` walks frame_link + aal shm (mirroring m_clan's iteration), filters by `dtype_allow + LocalDomainName + isIPaddrSim`, calls `warehouse_open_conn()` per matched NE. |
| `ncland_notify.cpp` | rdb_notify subscriber. `ncland_notify_init()` opens an RDB connection via `librdb_notify`, subscribes to NE provisioning channel(s), returns FD for epoll. `ncland_notify_drain(wh)` reads pending notifies and dispatches via a table-driven event handler (see §7). |
| `ncland_warehouse.cpp` | Existing file; gains `zmq_ctx`/`zmq_ctl`/notify-FD members in `ncland_wh_t`, seed call in `warehouse_init()`, new epoll branches in `warehouse_main_loop()` for ZMQ + notify FDs, response-forwarding path from worker `ctl_sock` back to caller via stashed ROUTER identity. |
| `ncland_worker.cpp` | Existing file; existing `mq_in`/`mq_out` calls deleted. Worker keeps using its existing Unix-socket (`ctl_sock`) framed cmd/rsp channel to the warehouse. Worker has zero ZMQ awareness. |
| `ncland.h` | Add `dtype_allow`, `zmq_ctx`, `zmq_ctl`, `notify_fd`, `pending_t pending[]` (caller-identity stash) to `ncland_wh_t`. Drop `mqd_t mq_*` members from `ncland_wh_t` and `ncland_wrkr_t`. |
| `cnc/sdi/src/m_clan.c` | Add `ncland.json` parse (dtype allowlist load); skip NEs whose dtype ∈ allowlist. Single `if (dtype_in_ncland(r.dtype)) continue;` guard before `start_clan_proc`. |
| `Makefile` (`sdisrc.mk` etc.) | Add `-lzmq -lrdb_notify -lcjson`. Remove `-lrt`. |
| `/usr/cnc/features/ncland.json` | New runtime config file. Schema: `{ "version": 1, "dtypes": [<int>, ...] }`. |
| `test/fixtures/ncland.json`, `test/fixtures/frame_link_mock.c`, `test/fixtures/rdb_notify_mock.c` | Test fixtures (see §10). |

---

## 6. Data Flow

### 6.1 Startup seed

```
ncland_warehouse start
  → ncland_seed_load_filter("/usr/cnc/features/ncland.json", dtype_allow)
       /* fatal exit if missing or malformed */
  → zmq_ctx = zmq_ctx_new()
  → zmq_ctl = bind ipc:///var/run/ncland/ctl.sock (ROUTER)
  → notify_fd = ncland_notify_init()
       /* librdb_notify subscribe; returns FD usable in epoll */
  → ncland_seed_run(wh):
       for each NE entry in frame_link/aal shm:
         if dtype ∈ dtype_allow
            && domain == LocalDomainName
            && isIPaddrSim(ip):
           conn_id = warehouse_open_conn(neid, slot)   /* SSH/Telnet connect */
           warehouse_assign_conn(conn_id)              /* SCM_RIGHTS to worker */
  → enter warehouse_main_loop
```

### 6.2 Caller command flow (steady state)

```
caller (DEALER)
  └── zmq_send([conn_id_hint, ncland_cmd_msg_t]) ──▶ warehouse ROUTER
                                                         │
                              identity frame stashed     │
                              keyed by msg.seq           │
                                                         ▼
                              worker_slot = conn_to_worker[msg.conn_id]
                              send(ctl_sock[worker_slot], cmd_frame)
                                                         │
                                                         ▼
                              worker: ssh_channel_write → NE
                              NE response → ssh_channel_read
                              worker builds ncland_rsp_msg_t
                              send(ctl_sock, rsp_frame) ──▶ warehouse
                                                         │
                              warehouse looks up identity by seq
                              zmq_send([identity, rsp_msg_t]) ──▶ caller
```

### 6.3 otn_portd event flow

```
DB write commits NE row → PG NOTIFY → librdb_notify FD ready
  → epoll wakes warehouse → ncland_notify_drain()
  → for each pending notify:
       parse → event_t { type, neid, dtype?, ip?, port? }
       dispatch via event handler table (see §7)
```

---

## 7. Event Dispatch Table (otn_portd → warehouse)

Implemented as a function-pointer table keyed by event type so adding a future
event is a one-row change.

| Event | Action |
|---|---|
| `NE_CREATE` | If `dtype ∈ dtype_allow`, `warehouse_open_conn(neid)` + `warehouse_assign_conn`. |
| `NE_DELETE` | `warehouse_close_conn(conn_id)`; free slot. |
| `NE_ENABLE` | `warehouse_open_conn(neid)` for an already-provisioned NE. New conn slot. Idempotent if already open. |
| `NE_DISABLE` | `warehouse_close_conn(conn_id)`; keep NE-side metadata for possible re-enable. Allowlist membership unchanged. |
| `NE_DTYPE_CHANGE` | If new dtype ∉ allowlist: close conn. If new dtype ∈ allowlist (and was not before): open conn. Otherwise: no-op. |
| `NE_IP_CHANGE` / `NE_PORT_CHANGE` | Close + reopen with new endpoint (sequential `close_conn` then `open_conn`). |

Only two warehouse primitives are needed: `warehouse_open_conn(neid)` and
`warehouse_close_conn(conn_id)`. Each event maps to a sequence of those calls.
`warehouse_open_conn` is idempotent — if `neid` is already mapped to a live
conn slot, the call returns the existing id and is a no-op.

---

## 8. Error Handling & Edge Cases

| Scenario | Behavior |
|---|---|
| `ncland.json` missing or malformed at startup | Fatal — `LOG_FATAL`, exit non-zero. m_clan applies the same check. Supervisor decides whether to retry. |
| `ncland.json` references a dtype unknown to the system | Allowlist accepts it; harmless — no NE in frame_link will ever match. Logged at INFO. |
| Seed pass: SSH connect fails for one NE | Per-NE failure logged at WARN, slot freed, seed continues. otn_portd `NE_ENABLE` retries reopen later. No retry loop in seed itself — keeps startup bounded. |
| Seed pass: shm / frame_link unavailable | Fatal — same as m_clan today; warehouse exits, supervisor restarts. |
| `librdb_notify` connection drops | epoll detects FD EOF / read error. Reconnect with exponential backoff (1 s → 30 s cap). During the reconnect window NE events are missed — accepted gap; warehouse does not re-seed automatically. Logged at WARN; metric counter incremented. |
| ZMQ caller crashes during pending CMD | Worker still completes the CMD. Warehouse's `zmq_send` to dead identity returns `EHOSTUNREACH` → response dropped + logged. Worker conn returns to `CS_READY`. |
| Duplicate `NE_CREATE` (e.g. seed already opened, then notify fires) | `warehouse_open_conn` checks if `neid` is already mapped; idempotent — second call returns existing id, no-op. |
| `NE_DISABLE` arrives mid-CMD | Worker drains the in-flight response, then warehouse closes the conn. No mid-write abort. |
| `NE_IP_CHANGE` arrives while conn live | Treated as `DISABLE` then `ENABLE` — sequential close + open, same neid. |
| Warehouse restart while workers alive | Warehouse SIGTERMs all workers at shutdown, re-seeds from scratch on next start, fresh workers spawn. Caller-visible: brief outage. |
| `dtype_allow` empty | Warehouse runs with no conns, only handles future `NE_*` events that will never match. Logged at WARN. |
| ZMQ ROUTER backlog at HWM | Excess `zmq_send` returns `EAGAIN` to the caller; caller retries. HWM = 4096 on `ZMQ_SNDHWM` / `ZMQ_RCVHWM` for the ctl socket. |

---

## 9. Schema: `/usr/cnc/features/ncland.json`

```json
{
  "version": 1,
  "dtypes": [1, 7, 12, 23]
}
```

- `version` (integer, required) — schema version. Code rejects any version it
  does not recognize.
- `dtypes` (array of integers, required) — device-type IDs ncland owns.
  Anything not in this list is left to legacy `clan` via `m_clan`.

Any unknown top-level key is rejected (strict parse).

---

## 10. Testing

### 10.1 New unit suites

| Step | File | Coverage |
|---|---|---|
| 3-rev | `test_zmq.cpp` | T3.1 ROUTER bind+unbind round-trip. T3.2 send/recv multipart frame `[id][empty][cmd_msg]`. T3.3 `ZMQ_FD` registers in epoll and fires `EPOLLIN`. T3.4 drain-until-`EAGAIN` loop terminates. T3.5 HWM enforced — sender sees `EAGAIN` after fill. |
| 15 | `test_seed.cpp` | T15.1 `load_filter` parses valid JSON. T15.2 missing file returns fatal. T15.3 malformed JSON returns fatal. T15.4 `dtype_allow.contains()` correct. T15.5 seed pass with mock frame_link iterates only allowed dtypes. T15.6 seed skips IP-mismatch / domain-mismatch entries. T15.7 idempotent re-seed — second call no-ops on existing neids. |
| 16 | `test_notify.cpp` | T16.1 subscribe returns valid FD. T16.2 mock notify dispatches `NE_CREATE` → `warehouse_open_conn` called with right args. T16.3 `NE_DELETE` path calls `warehouse_close_conn`. T16.4 `NE_ENABLE` / `NE_DISABLE` / `NE_DTYPE_CHANGE` / `NE_IP_CHANGE` dispatch table coverage (one test per event). T16.5 unknown event type logged + dropped, no crash. T16.6 reconnect-after-drop path retries with backoff. |

### 10.2 Integration tests (tagged `[integration]`)

- **T-int-1:** end-to-end caller → ZMQ ctl → warehouse → worker → loopback SSH
  → response → caller. Verify ROUTER identity round-trip.
- **T-int-2:** live PG NOTIFY emitted by a test fixture; warehouse opens a
  conn; verify worker receives the fd.
- **T-int-3:** full restart cycle — kill warehouse mid-flight, restart, verify
  seed re-runs and conns re-establish.

### 10.3 Removed tests

POSIX MQ tests T3.1–T3.4 (current TODO) deleted. Step 3 retitled "ZeroMQ Layer".

### 10.4 Static checks in CI

- `grep -rn 'mq_open\|mq_send\|mq_receive\|<mqueue.h>' src/ncland*.cpp` must
  return empty after migration.
- `grep -rn 'SIGALRM\|alarm(' src/ncland*.cpp` already empty (existing
  step 11 check).

### 10.5 Test fixtures

- `test/fixtures/ncland.json` — sample allowlist for unit tests.
- `test/fixtures/frame_link_mock.c` — in-memory stub for seed tests; real shm
  symbol replaced via link-time interpose.
- `test/fixtures/rdb_notify_mock.c` — in-memory event injector for notify
  tests.

---

## 11. Migration Plan

1. **Land transport swap first** — replace `ncland_mq_*` with `ncland_zmq_*`
   end-to-end; `test_zmq.cpp` green; static check confirms zero MQ symbols
   remain. Ship behind feature flag if preferred (otherwise direct cutover —
   ncland is not yet in production per `TODO.adoc`).
2. **Land seed module** — `ncland_seed.cpp` + `ncland.json` parser.
   `test_seed.cpp` green. Warehouse can boot with NEs from frame_link.
3. **Land notify module** — `ncland_notify.cpp` + dispatch table.
   `test_notify.cpp` green. Warehouse reacts to live events.
4. **Modify `m_clan`** to consult `ncland.json` and skip allowlisted dtypes.
   Land in same release as the warehouse seed module; do **not** ship the
   m_clan change before warehouse is functional, or NEs will be orphaned.
5. **Integration suite** runs in CI with fixtures; one manual smoke test on a
   lab NE per release.

---

## 12. Open Questions

The following must be resolved during plan-writing or step-1 implementation:

1. **rdb_notify channel name.** Exact `rdb_notify_type` enum values (and PG
   channel names) for the six events listed in §7. `cnc/otn_port/src/otn_portd.c`
   and `<rdb_notify.h>` are the source of truth — read at plan time.
2. **`librdb_notify` API surface for non-RDB-server clients.** `otn_portd` is
   itself the notify *server* and uses the API for serving subscribers.
   Confirm the same library is usable from `ncland_warehouse` as a *subscribing
   client*, or whether a thinner client variant is needed.
3. **frame_link / aal shm access from warehouse.** m_clan attaches via
   `atch_aal()` and `atch_frm()`. Confirm these calls are safe to invoke from
   a second long-running process (ncland warehouse) concurrent with m_clan.
4. **JSON parser choice.** `cJSON` is named in §5; verify it is already
   linked into the build environment, or pick the in-tree alternative
   (`jansson`, hand-rolled parser, etc.).
5. **`ncland.json` deployment path & ownership.** Who installs it (RPM
   package?), what mode (likely `0644 root:root`), and what happens during
   upgrade if it is absent on first boot — supervisor restart loop, or
   warehouse runs in "no-op" mode until file appears? §8 currently says fatal
   exit; confirm supervisor behavior matches.
