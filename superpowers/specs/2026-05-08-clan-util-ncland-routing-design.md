# clan_util: route CLI to ncland over ZeroMQ for owned dtypes

**Date:** 2026-05-08
**Status:** Draft (awaiting review)
**Component:** `cnc/utility/src/clan_util.c`
**Related design:** `docs/superpowers/specs/2026-05-04-ncland-zeromq-seeding-design.md`

---

## 1. Background

`ncland` (the new C++ multi-NE CLI agent) replaces legacy `clan` for a subset
of dtypes listed in `/usr/cnc/lib/data/ncland/ncland.json`. `m_clan` already
consults that file and skips per-slot CLAN spawn for ncland-owned dtypes
(`cnc/sdi/src/m_clan.c:148`).

The caller side is the missing half. `clan_write()` /
`clan_write_ctag()` /`clan_read()` in `cnc/utility/src/clan_util.c` still send
every command through the SysV MQ that fans out to clan / netclan / netcland.
For ncland-owned dtypes those messages have no consumer.

This change routes CLI commands for ncland-owned dtypes to the warehouse over
its existing ZeroMQ ROUTER endpoint, and reads responses back over the same
DEALER. Other dtypes are untouched.

---

## 2. Goals

- `clan_write_ctag()` sends ncland-owned-dtype CLI commands to the warehouse
  ROUTER (`ipc:///usr/cnc/lib/data/ncland/ctl.sock`) instead of `msgsnd()`.
- `clan_read()` reads responses for ncland-owned dtypes off the same DEALER
  socket instead of `msgrcv()`.
- Allowlist is read once per caller process from
  `/usr/cnc/lib/data/ncland/ncland.json` using the same hand-rolled scanner
  already in `m_clan.c` (no new JSON-parser dependency in `cnc/utility`).
- One ZMQ context + one DEALER socket per caller process, lazy-initialised.

---

## 3. Non-Goals

- `DST_NETCONF` requests. They continue to flow to `netclan` / `netcland`.
- Multibox / `remote != 0`. Cross-box CLI continues through the legacy path.
- ncland.json hot reload. Read once at first use; restart caller to pick up
  changes (matches m_clan’s policy).
- Per-NE OPEN/CLOSE issued from the caller. The warehouse seeds connections
  at startup and reacts to otn_portd events; misses are warehouse business.
- Removing the legacy MQ path. Both paths coexist until every dtype migrates.

---

## 4. Architecture

```
caller (clanfe / fep / etc.)        ncland_warehouse
┌──────────────────────────┐         ┌─────────────────────────┐
│ clan_write_ctag()        │         │  ROUTER                 │
│  ├─ load_ncland_allowlist│         │  ipc:///.../ctl.sock    │
│  ├─ dtype_owned_by_ncland│         │  by_neid lookup         │
│  ├─ ncland_zmq_ensure()  │  zmq    │   → conn_id             │
│  └─ ncland_send_cmd() ───┼────────▶│   → worker ctl_sock     │
│                          │ DEALER  │                         │
│ clan_read()              │         │  worker → R-frame ──┐   │
│  └─ ncland_recv_rsp() ◀──┼─────────┼──────────────────── │   │
└──────────────────────────┘         └─────────────────────┘
```

ZMQ socket is process-singleton: one `zmq_ctx_new()` and one
`zmq_socket(ZMQ_DEALER)`, created on the first ncland-routed call and reused
for the life of the process.

---

## 5. Components

| File | Change |
|---|---|
| `cnc/utility/src/clan_util.c` | Add file-static allowlist + ZMQ singleton, `load_ncland_allowlist()`, `dtype_owned_by_ncland()`, `ncland_zmq_ensure()`, `ncland_send_cmd()`, `ncland_recv_rsp()`. Gate early in `clan_write_ctag()` and `clan_read()`. |
| `cnc/ncland/src/ncland.h` | Add `int neid;` field to `ncland_cmd_msg_t`. |
| `cnc/ncland/src/ncland_warehouse.cpp` | In `warehouse_handle_cmd_from_zmq()`, when `cmd->conn_id == -1`, resolve via new `by_neid` map; if not found, log + drop. Map built in seed (`ncland_seed.cpp`) and updated on `NE_CREATE`/`NE_DELETE`/`NE_DTYPE_CHANGE`/`NE_IP_CHANGE` in `ncland_notify.cpp`. |
| Caller `*.mk` files that link `clan_util.o` | Append `-lzmq`. |

---

## 6. Caller-side state and helpers

```c
/* file-static in clan_util.c */
#define NCLAND_ALLOW_MAX 64
static int   NclandAllow[NCLAND_ALLOW_MAX];
static int   NclandAllowCount  = 0;
static int   NclandAllowLoaded = 0;
static void *NclandZmqCtx      = NULL;
static void *NclandZmqDealer   = NULL;

static const char *NCLAND_JSON_PATH    =
    "/usr/cnc/lib/data/ncland/ncland.json";
static const char *NCLAND_CTL_ENDPOINT =
    "ipc:///usr/cnc/lib/data/ncland/ctl.sock";

static void load_ncland_allowlist(void);     /* mirrors m_clan.c:99 */
static int  dtype_owned_by_ncland(int dtype);
static int  ncland_zmq_ensure(void);         /* lazy ctx + DEALER */
static int  ncland_send_cmd(CLAN *c, int tmout, const char *cmd,
                            const char *ctagstr, int rtrv, int quiet);
static int  ncland_recv_rsp(CLAN *c, char *buffer, int bufsiz, int flag);
```

`load_ncland_allowlist()` is the same hand-rolled scanner already in
`m_clan.c` — copied verbatim to keep both consumers byte-identical and to
avoid pulling `<json.hpp>` / `cJSON` into the C-only `clan_util.c`. Loaded
once on first call (`NclandAllowLoaded` guard).

`ncland_zmq_ensure()`:
- `NclandZmqCtx = zmq_ctx_new();`
- `NclandZmqDealer = zmq_socket(NclandZmqCtx, ZMQ_DEALER);`
- `ZMQ_LINGER = 0`
- `ZMQ_IDENTITY = "<pname>:<pid>"` so warehouse logs the caller cleanly
- `zmq_connect(NclandZmqDealer, NCLAND_CTL_ENDPOINT)`
- All errors → log, leave singleton NULL, return -1 (caller falls back to
  FAILURE; no automatic legacy-path fallback because the dtype is
  warehouse-owned by config).

---

## 7. clan_write_ctag flow

Insert the gate immediately after `neType` is computed
(`clan_util.c:258`), before the existing netclan / netcland routing:

```c
int neType = frm_dtype(c->dacsid);

load_ncland_allowlist();
if (dtype_owned_by_ncland(neType)
    && dst_type == DST_CLI
    && !remote)
{
    if (setCtag)
        strcpy(ctagstr,
               ctag(neType, verb, c->dacsid, c->seqnum, NULL));
    return ncland_send_cmd(c, tmout, cmd, ctagstr, rtrv, quiet);
}
```

`ncland_send_cmd()`:

```c
ncland_cmd_msg_t msg;
memset(&msg, 0, sizeof(msg));
msg.msg_type  = CLAN2_MSG_CMD;
msg.conn_id   = -1;                        /* "look up by neid" */
msg.neid      = c->dacsid;
msg.seq       = c->seqnum;
msg.orig_cnc  = CncNum;
msg.tty       = set_clan_port(c->dacsid, c->slot, 0);
msg.user_priv = get_user_ne_cmd_priv_id();
msg.is_rtrv   = rtrv;
msg.quiet     = quiet;
msg.tmout     = tmout;
strncpy(msg.ctag, ctagstr, sizeof(msg.ctag) - 1);
snprintf(msg.data, sizeof(msg.data),
         "%s:%d:%d:%d:%s", ctagstr, tmout, rtrv, quiet, cmd);

if (ncland_zmq_ensure() != 0) return FAILURE;
if (zmq_send(NclandZmqDealer, "",   0,           ZMQ_SNDMORE) < 0
 || zmq_send(NclandZmqDealer, &msg, sizeof(msg), 0)           < 0)
    return FAILURE;
return SUCCESS;
```

`cmd` length check (`>= DACS_MSG_SZ - 20` → "Command to long") is preserved
upstream of the gate; same length budget applies.

---

## 8. clan_read flow

`clan_read()` gate at the top of the function:

```c
int neType = frm_dtype(c->dacsid);
if (dtype_owned_by_ncland(neType))
    return ncland_recv_rsp(c, buffer, bufsiz, flag);
```

`ncland_recv_rsp()`:

```c
if (ncland_zmq_ensure() != 0) {
    snprintf(buffer, bufsiz, "syserror:\"ncland zmq init\"");
    return FAILURE;
}

zmq_msg_t m;
int recv_flags = (flag == IPC_NOWAIT) ? ZMQ_DONTWAIT : 0;

zmq_msg_init(&m);
if (zmq_msg_recv(&m, NclandZmqDealer, recv_flags) < 0) {
    zmq_msg_close(&m);
    if (errno == EAGAIN) {
        snprintf(buffer, bufsiz, "No Message");
        return FAILURE;
    }
    snprintf(buffer, bufsiz, "syserror:\"%s\" errno:%d",
             strerror(errno), errno);
    return FAILURE;
}
zmq_msg_close(&m);                     /* empty delimiter */

zmq_msg_init(&m);
int n = zmq_msg_recv(&m, NclandZmqDealer, 0);
if (n != (int)sizeof(ncland_rsp_msg_t)) {
    zmq_msg_close(&m);
    snprintf(buffer, bufsiz, "syserror:\"bad ncland rsp size\"");
    return FAILURE;
}
ncland_rsp_msg_t rsp = *(ncland_rsp_msg_t *)zmq_msg_data(&m);
zmq_msg_close(&m);

snprintf(buffer, bufsiz, "%s", rsp.text);
fileName[0] = '\0';
return SUCCESS;
```

The `mtype`-filtering semantics of `msgrcv()` do not exist on the DEALER:
the warehouse multiplexes by ROUTER identity, so this DEALER only receives
responses destined for this caller. `seq` correlation is preserved in
`rsp.seq` for callers that want to assert it.

---

## 9. Wire change to ncland_cmd_msg_t

`ncland.h`:

```c
typedef struct ncland_cmd_msg {
    int     msg_type;
    int     conn_id;     /**< Slot index, or -1 to look up by neid. */
    int     neid;        /**< Used when conn_id == -1. */
    /* …existing fields unchanged… */
} ncland_cmd_msg_t;
```

`warehouse_handle_cmd_from_zmq()`:

```c
int conn_id = cmd->conn_id;
if (conn_id < 0) {
    auto it = wh->by_neid.find(cmd->neid);
    if (it == wh->by_neid.end()) {
        LOG_WARN("zmq cmd: no live conn for neid=%d", cmd->neid);
        return;
    }
    conn_id = it->second;
}
/* unchanged from here */
```

`ncland_wh_t::by_neid` is a `std::unordered_map<int, int>` (neid → conn_id),
maintained by:
- `ncland_seed_run()` after each successful `warehouse_open_conn()`.
- `ncland_notify` event handlers on `NE_CREATE` / `NE_ENABLE` (insert) and
  `NE_DELETE` / `NE_DISABLE` / `NE_DTYPE_CHANGE` (remove).
- `NE_IP_CHANGE` (close+reopen) re-inserts with the same neid → new
  conn_id.

---

## 10. Error handling & edge cases

| Scenario | Behaviour |
|---|---|
| `ncland.json` missing or malformed | Allowlist empty; gate never fires; legacy path serves every dtype. Log at INFO once. (Same as m_clan today.) |
| `ipc:///…/ctl.sock` not yet bound (warehouse not running) | `zmq_connect()` succeeds (ZMQ buffers); `zmq_send()` queues to HWM. If HWM reached, `EAGAIN` → return `FAILURE` from `clan_write_ctag`. |
| Warehouse crashes mid-flight | Sent message is lost; caller's `clan_read` blocks (or `EAGAIN` if `IPC_NOWAIT`). Retry is the caller's responsibility, same as today's MQ path. |
| Allowlist contains a dtype the warehouse hasn't actually opened | `warehouse_handle_cmd_from_zmq` logs + drops; no response ever arrives; caller times out via existing `tmout` semantics in `clan_read`. |
| `DST_NETCONF` request for an ncland-owned dtype | Gate skipped (`dst_type == DST_CLI` check); legacy netcland path runs. Today no dtype is both ncland-owned and DST_NETCONF; if that ever holds, this design needs revisiting. |
| `remote != 0` (multibox cross-box) | Gate skipped; legacy path. Warehouse runs only on the local box. |
| Caller process forks after init | ZMQ context survives `fork()` only if user code doesn't touch it in the child; safe assumption here is "do not use ncland zmq across fork". Child must `ncland_zmq_ensure()` again — handled by the lazy-init guard plus a `pthread_atfork` reset hook is **out of scope**; if caller processes fork after first ncland-routed write, that is a future-work item. |

---

## 11. Testing

Unit suites land in `cnc/utility/test/` (or wherever existing util tests
live — confirm at plan time):

- T-cu-1: `load_ncland_allowlist()` parses the m_clan fixture.
- T-cu-2: `dtype_owned_by_ncland()` lookup.
- T-cu-3: `ncland_zmq_ensure()` lazy init succeeds against a stub ROUTER on
  `ipc:///tmp/test_clanutil.sock`; a second call returns the same socket.
- T-cu-4: `clan_write_ctag()` with allowlisted dtype + `DST_CLI` + `!remote`
  produces a `[empty][cmd_msg]` frame at the test ROUTER; `cmd_msg` fields
  match expectations (neid, seq, ctag, data string format).
- T-cu-5: `clan_write_ctag()` with non-allowlisted dtype skips the gate and
  reaches `msgsnd` (existing path).
- T-cu-6: `clan_read()` with allowlisted dtype recovers a stubbed
  `ncland_rsp_msg_t.text` into `buffer` and returns SUCCESS.
- T-cu-7: `clan_read()` with `flag == IPC_NOWAIT` and no message returns
  FAILURE + "No Message".

Integration: pair with the existing T-int-1 in the parent design.

CI static check: `grep -n 'msgsnd\|msgrcv' clan_util.c` still finds the
legacy paths but the caller-fanout report shows ncland dtypes never hit
them.

---

## 12. Migration

1. Land the `ncland_cmd_msg_t::neid` wire change and warehouse `by_neid`
   resolver. Existing callers send `conn_id >= 0` so the new branch is
   dead until step 2. Update `ncland_unit_tests` for the new field.
2. Land the `clan_util.c` caller changes. With an empty
   `/usr/cnc/lib/data/ncland/ncland.json`, behaviour is identical to today.
3. Populate the allowlist in lab — verify a single dtype routes through
   ncland end-to-end.
4. Roll allowlist forward dtype-by-dtype.

Steps 1 and 2 must ship in the same release; otherwise callers send a
field the warehouse ignores and `conn_id == -1` is rejected with
`"bad conn_id"`.

---

## 13. Open questions

1. Which makefiles link `clan_util.o`? Need the full list to add `-lzmq`.
   Candidates seen so far: `cnc/utility/utility.mk`, plus any consumer that
   pulls `clan_util.o` directly. Confirmed at plan time.
2. Process-fork semantics in known callers (clanfe, fep, restclanfe). If
   any of these forks after a first ncland-routed call, we need a
   `pthread_atfork` reset of `NclandZmqCtx`/`NclandZmqDealer`. Audit at
   plan time; out of scope until the audit shows it's needed.
3. Confirm the warehouse `by_neid` map is the right primitive (vs. a flat
   array indexed by neid) — depends on neid range. Defer to the
   warehouse-side change.
