# ncland #4 — POSIX mqueue Inbound + `qwrite()` Outbound (replacing the ZMQ ctl)

**Status:** design approved 2026-06-16. Implementation plan to follow.
**Predecessors:** `2026-06-11-ncland-stepper-3b-design.md` (#3b stepper, retypes here), `2026-06-09-ncland-stepper-3a-design.md` (#3a result field, retypes here).
**Branch:** `ncland-start`.
**Memory references:** `project_rdb_notify_zmq` (zmq stays linked for the planned notify pub/sub).

---

## 1. Scope & Constraints

Replace ncland's external ZMQ ROUTER control boundary with a legacy-style fan-out using POSIX message queues inbound and the existing `qwrite()` → SysV `ScreenMsqid` outbound. Drop the RPC reply model. Retire `nclan-cmd` and the pending-stash machinery.

### In scope

- Inbound `POSIX mqueue` named `/ncland_ctl` (configurable). Daemon owns it; clients `mq_send` to it.
- `mqd_t` integrated into the existing epoll loop on Linux (mqd_t IS an fd).
- Per-cmd payload = `struct dacs_msg` (from `msgscreen.h`) — matches every existing TL1/REST/MTOSI client.
- Outbound via existing `qwrite(struct dacs_msg *)` from `-lutillib` — SysV `msgsnd` to `ScreenMsqid` (per `dm_to_screen=1`).
- `ncland_cmd_msg_t` and `ncland_rsp_msg_t` retired. Field-for-field mapping into `dacs_msg`.
- `nclan_cmd.cpp` deleted (clients now talk to ncland via mq, no debug-RPC tool).
- Pending-stash table (`wh->pending[]` + `warehouse_pending_stash`/`_take`) deleted — no per-cmd correlation needed.
- Silent-drop semantics for unknown-neid / busy-conn / shutdown / malformed (no reply path exists for failure messages).
- Test isolation via a link-time stub `qwrite` / `qopen` / `scrnq_overflow` in `ncland_test_qwrite.cpp`.

### Out of scope

- `librdb_notify` integration — separate work; ZMQ stays linked for that.
- Alarm-handler integration (`AlrmMsqid`) — wired the same way as screen, deferred until ncland publishes autonomous alarms.
- Per-client client-side helpers — every existing client already builds `dacs_msg`; the only change is the destination (`/ncland_ctl` POSIX mq vs. clan's SysV `Dacsq`). Per-client migration tracked separately.
- A new operator-debug tool to replace `nclan-cmd`. A one-off `mqsend`-style script can be written ad-hoc.

---

## 2. Architecture

```
                     /ncland_ctl  (POSIX mq, ncland owns)
                            │
   TL1/REST/MTOSI ──mq_send─▶
                            ▼
                      ┌──────────────────────────┐
                      │ ncland (epoll loop)      │
                      │   • mqd_t in epoll set   │
                      │   • mq_receive(dacs_msg) │
                      │   • talks to NEs (ssh)   │
                      └──┬───────────────────────┘
                         │ qwrite(&dacs_msg)   ← from -lutillib
                         ▼
              ScreenMsqid (SysV) ───or──→ AlrmMsqid (SysV, future)
                         │                       │
                         ▼                       ▼
                    screen process          alarm-handler
                         │
                         ▼
                   originating client
```

**Asymmetric transports by design:**

- **Inbound = POSIX mqueue.** ncland is the *consumer* and needs epoll integration. `mqd_t` is an fd on Linux; `epoll_ctl(EPOLL_CTL_ADD, mqd, ...)` works directly. SysV msgQs (which legacy clan uses for inbound) cannot integrate with epoll — that's the root cause of the migration.
- **Outbound = SysV via `qwrite()`.** ncland is the *producer*; `msgsnd` is fine because there's no per-call event loop on the producer side. SysV is what `screen` and the alarm handler already read from via `msgrcv`. ncland slots into the existing pipeline with zero new infrastructure.

**No pending-stash table.** Today's `warehouse_pending[seq]` exists to remember "who do I reply to" so the ZMQ ROUTER can route the rsp back to the right DEALER identity. With responses going to a fixed downstream (`ScreenMsqid`), there's no per-caller routing — the `dacs_msg` carries `dm_dacsid` + `dm_tty` so screen knows the logical client to forward to. The pending table is deleted.

**Epoll integration:** `wh->ctl_mqd` joins `wh->pool.event_fd` and the per-conn `net_fd`s in the same epoll set. No `ZMQ_FD` indirection, no `ncland_zmq_drain` edge-trigger dance.

**Files retired:** `ncland_zmq.cpp`, `nclan_cmd.cpp`, `wh->pending[]`, `warehouse_pending_stash`/`_take`, `NCLAND_MAX_PENDING`, `ncland_cmd_msg_t`, `ncland_rsp_msg_t`.

**Files added:** `ncland_mq.{h,cpp}` (inbound), `ncland_test_qwrite.cpp` (test-only stub).

**Build wiring:** add `-lrt` to ncland's link. `-lzmq` stays (notify pub/sub future). `nclan-cmd` target removed entirely.

---

## 3. Types & `ncland_wh_t` field changes

### 3.1 Wire-format struct: `struct dacs_msg` (existing)

From `include/msgscreen.h`:

```c
struct dacs_msg {
    long          dm_tty;          /* SysV msgtype; logical client TTY */
    int           dm_type;         /* DMTYPE_TEXT_MSG | DMTYPE_SEQ_MSG | ... */
    int           dm_old_tty;      /* original tty (forwarded msgs) */
    int           dm_slot;         /* SLOT_UNUSED or SNMP slot */
    int           dm_slot_tp;
    unsigned int  dm_up:1;
    unsigned int  dm_active:1;
    unsigned int  dm_to_screen:1;  /* 1 = ScreenMsqid; 0 = AlrmMsqid */
    unsigned int  dm_reason:9;
    unsigned int  dm_dacsid:20;    /* NE id */
    union {
        char  dm_text[MAXDACSMSG+1];   /* MAXDACSMSG = 10240 */
        int   dm_seq;
    } u;
};
```

`DACS_MSG_HDR_SZ` = `offsetof(struct dacs_msg, u)` — used to size sends so short text doesn't pay the full 10 KB.

### 3.2 Field-by-field mapping

**`ncland_cmd_msg_t` → `dacs_msg` (inbound):**

| old field | new field | notes |
|---|---|---|
| `msg_type` | — | gone. Only CMD was ever used; OPEN/CLOSE were never honored by real callers. |
| `conn_id` | — | gone. Routing is by `dm_dacsid` (= neid). |
| `seq` | — | gone. No pending-stash; no caller-side correlation. |
| `orig_cnc` | — | gone. Caller correlation is by `dm_tty` (screen-side). |
| `tty` | `dm_tty` | direct. |
| `user_priv` | — | gone. Never honored downstream. |
| `is_rtrv` | — | auto-derived from `dm_text` parse (`"show "` prefix), unchanged from today. |
| `neid` | `dm_dacsid` | direct. |
| `check_errors` | — | **dropped feature.** Always-scan, always-set `dm_reason`. No production caller ever set the opt-out. |
| `quiet` | — | gone. |
| `tmout` | embedded in `dm_text` | already in colon-delim `"ctag:tmout:is_rtrv:quiet:command"` prefix. |
| `deadline` | — | gone. |
| `ctag[32]` | embedded in `dm_text` | colon-delim, position 1. |
| `data[2560]` | `dm_text[10241]` | larger. |

**`ncland_rsp_msg_t` → `dacs_msg` (outbound):**

| old field | new field | notes |
|---|---|---|
| `conn_id` | — | gone. |
| `seq` | — | gone. |
| `orig_cnc` | — | gone. |
| `tty` | `dm_tty` | echoed from inbound. |
| `dm_old_tty` (new) | `dm_old_tty` | echoed from inbound (its standard role: forwarded-msg original tty). |
| `dm_type` | `dm_type` | typically `DMTYPE_TEXT_MSG`. |
| `result` | `dm_reason` | 0 = SUCCESS, 1 = FAILURE. 9-bit field has room for future codes. |
| `text[]` | `dm_text[]` | direct. |

`dm_to_screen = 1` for command responses; `0` for autonomous alarms (future).

### 3.3 `ncland_wh_t` field changes

**Retired:**
```c
void          *zmq_ctx;
void          *zmq_ctl;
int            zmq_ctl_fd;

struct {                            /* pending-stash table */
    int      seq;
    size_t   identity_len;
    uint8_t  identity[256];
} pending[NCLAND_MAX_PENDING];
```

**Added:**
```c
mqd_t          ctl_mqd;             /**< POSIX mq descriptor for /ncland_ctl; -1 when not open. */
char           ctl_mq_name[64];     /**< Name passed to mq_open; remembered for mq_unlink at shutdown. */
```

`notify_fd` (the future ZMQ pub/sub subscriber for `librdb_notify`) stays — per `project_rdb_notify_zmq`, that path remains ZMQ.

`NCLAND_MAX_PENDING` retired. The cap is now implicit — POSIX mq's `mq_maxmsg` attr (default from `/proc/sys/fs/mqueue/msg_max`, typically 10) bounds in-flight cmds.

### 3.4 `conn_t` field changes

`pending_rsp` (added in #3b) retypes:

```c
struct dacs_msg  pending_rsp;       /* was: ncland_rsp_msg_t */
```

Still POD; still per-conn; still set at prompt-match and shipped via `worker_send_rsp` after `post_command` completes. Size grows from ~3 KB to ~10.3 KB; per-conn cost at `MAX_CONNS = 256` adds ~1.9 MB to the conn array — fine for the daemon's footprint; `ncland_wh_t` stays stack-allocated.

**New POD fields for outbound routing** (set in `warehouse_handle_dacs_msg`, read in `worker_send_rsp`):

```c
long    orig_tty;        /**< dm_tty of the cmd that owns the conn right now */
int     orig_old_tty;    /**< dm_old_tty echoed back */
int     orig_slot;       /**< dm_slot echoed back */
int     orig_slot_tp;    /**< dm_slot_tp echoed back */
```

### 3.5 Test seam retypes

| old seam | new seam |
|---|---|
| `g_test_last_rsp_result` (int) | `g_test_last_rsp_reason` (int; mirrors `dm_reason`) |
| `g_test_last_sent_rsp_text` (char[]) | `g_test_last_sent_dacs_text` (char[MAXDACSMSG+1]) |
| `g_test_last_direct_rsp_result` | **deleted** — no direct-reply path |
| `g_test_worker_dispatched_seq` | `g_test_worker_dispatched_dacsid` |

### 3.6 New API in `ncland_mq.h`

```c
int  ncland_mq_init(ncland_wh_t *wh, const char *queue_name);
void ncland_mq_close(ncland_wh_t *wh);
int  ncland_mq_drain(ncland_wh_t *wh);
```

Outbound is the existing `qwrite(struct dacs_msg *)` from `-lutillib` — no new public API.

### 3.7 Function renames

| old | new |
|---|---|
| `warehouse_handle_cmd_from_zmq(wh, identity, len, &cmd_msg)` | `warehouse_handle_dacs_msg(wh, &dacs_msg)` |
| `dispatch_cmd(c, &cmd_msg)` | `dispatch_cmd(c, &dacs_msg)` |
| `worker_send_rsp(wh, &rsp_msg)` | `worker_send_rsp(wh, &dacs_msg)` |
| `warehouse_pending_stash` / `_take` | **deleted** |
| `ncland_zmq_init` / `_close` / `_recv_cmd` / `_send_rsp` / `_drain` | **deleted** |

---

## 4. Inbound — `mq_open`, epoll integration, drain, dispatch

### 4.1 Queue setup at startup

In `warehouse_init`, after `qopen(1)`:

```c
struct mq_attr attr = {0};
attr.mq_maxmsg  = 10;                          /* matches default /proc cap; tune via -m */
attr.mq_msgsize = sizeof(struct dacs_msg);
attr.mq_flags   = 0;                           /* non-blocking set via O_NONBLOCK */

snprintf(wh->ctl_mq_name, sizeof(wh->ctl_mq_name), "%s", cfg->ctl_mq_name);
wh->ctl_mqd = mq_open(wh->ctl_mq_name,
                      O_CREAT | O_RDONLY | O_NONBLOCK,
                      0660, &attr);
if (wh->ctl_mqd == (mqd_t)-1) {
    if (errno == EINVAL)
        LOG_ERROR("warehouse_init: mq_open(%s) EINVAL — likely fs.mqueue.msgsize_max < %zu",
                  wh->ctl_mq_name, sizeof(struct dacs_msg));
    else
        LOG_ERROR("warehouse_init: mq_open(%s) failed: %s",
                  wh->ctl_mq_name, strerror(errno));
    return -1;
}
```

Default `cfg->ctl_mq_name` = `"/ncland_ctl"`; CLI flag `-c <name>` overrides (replaces the old `-c <zmq_endpoint>`).

### 4.2 The msgsize-max gotcha

`sizeof(struct dacs_msg)` is **~10.3 KB**. Default Linux per-queue cap `/proc/sys/fs/mqueue/msgsize_max` is **8192**. `mq_open` fails with `EINVAL` on a stock system.

**Deployment requirement** (the only new ops touch):
```
/etc/sysctl.d/99-ncland.conf:
    fs.mqueue.msgsize_max = 16384
    fs.mqueue.msg_max     = 64
```

### 4.3 Adding the mqd to epoll

```c
struct epoll_event ev = { .events = EPOLLIN, .data.fd = wh->ctl_mqd };
if (epoll_ctl(epfd, EPOLL_CTL_ADD, wh->ctl_mqd, &ev) < 0) {
    LOG_ERROR("warehouse_main_loop: epoll_ctl ctl_mqd: %s", strerror(errno));
}
```

`mqd_t` is level-triggered by default — no `EPOLLET`. Matches the rest of ncland's per-conn fd model.

### 4.4 Drain loop

`ncland_mq_drain(wh)` lives in `ncland_mq.cpp`. Called from `warehouse_main_loop` when epoll reports `EPOLLIN` on `wh->ctl_mqd`:

```c
int ncland_mq_drain(ncland_wh_t *wh)
{
    if (!wh || wh->ctl_mqd == (mqd_t)-1) return -1;

    for (;;) {
        struct dacs_msg m;
        unsigned int prio = 0;
        ssize_t n = mq_receive(wh->ctl_mqd, (char *)&m, sizeof(m), &prio);
        if (n < 0) {
            if (errno == EAGAIN) return 0;
            LOG_WARN("ncland_mq_drain: mq_receive: %s", strerror(errno));
            return -1;
        }
        if (n < (ssize_t)DACS_MSG_HDR_SZ) {
            LOG_WARN("ncland_mq_drain: short msg %zd bytes (< hdr %zu); dropped",
                     n, DACS_MSG_HDR_SZ);
            continue;
        }
        /* NUL-terminate dm_text based on actual byte count. */
        if (n < (ssize_t)sizeof(m)) {
            size_t text_off = offsetof(struct dacs_msg, u.dm_text);
            if ((size_t)n > text_off) m.u.dm_text[n - text_off] = '\0';
            else                       m.u.dm_text[0] = '\0';
        }
        warehouse_handle_dacs_msg(wh, &m);
    }
}
```

POSIX mq is message-bounded — no partial-read framing. One `mq_receive` = one whole `dacs_msg`.

### 4.5 Dispatch

```c
void warehouse_handle_dacs_msg(ncland_wh_t *wh, struct dacs_msg *m)
{
    if (!wh || !m) return;

    if (wh->shutting_down) {
        LOG_DEBUG("mq cmd dropped: shutting_down (neid=%u tty=%ld)",
                  m->dm_dacsid, m->dm_tty);
        return;
    }

    int id = ncland_find_conn_by_neid(wh, (int)m->dm_dacsid);
    if (id < 0) {
        LOG_WARN("mq cmd: no session for neid=%u tty=%ld; dropped",
                 m->dm_dacsid, m->dm_tty);
        return;
    }
    conn_t *c = &wh->conn[id];
    if (c->state != CS_READY) {
        LOG_WARN("mq cmd: neid=%u busy (state %d) tty=%ld; dropped",
                 m->dm_dacsid, (int)c->state, m->dm_tty);
        return;
    }

    if (ncland_parse_cmd_data(m) < 0) {
        LOG_WARN("mq cmd: malformed dm_text for neid=%u; dropped", m->dm_dacsid);
        return;
    }

    c->orig_tty     = m->dm_tty;
    c->orig_old_tty = m->dm_old_tty;
    c->orig_slot    = m->dm_slot;
    c->orig_slot_tp = m->dm_slot_tp;

    if (dispatch_cmd(c, m) < 0)
        LOG_WARN("mq cmd: dispatch_cmd failed for neid=%u", m->dm_dacsid);
}
```

`ncland_parse_cmd_data(m)` operates on `m->u.dm_text` (was `m->data`).

### 4.6 Main-loop wiring

```c
} else if (wh->ctl_mqd != (mqd_t)-1 && fd == wh->ctl_mqd) {
    if (ncland_mq_drain(wh) < 0)
        LOG_WARN("warehouse_main_loop: ncland_mq_drain failed");
}
```

### 4.7 Shutdown

```c
if (wh->ctl_mqd != (mqd_t)-1) {
    mq_close(wh->ctl_mqd);
    wh->ctl_mqd = (mqd_t)-1;
}
if (wh->ctl_mq_name[0]) {
    mq_unlink(wh->ctl_mq_name);
    wh->ctl_mq_name[0] = '\0';
}
```

---

## 5. Outbound — `worker_send_rsp` via `qwrite`

### 5.1 Body

```c
extern "C" int qwrite(struct dacs_msg *inmsg);
extern "C" int scrnq_overflow(void);

int worker_send_rsp(ncland_wh_t *wh, struct dacs_msg *m)
{
    if (!m) return -1;

    /* Capture for tests before qwrite. */
    snprintf(g_test_last_sent_dacs_text, sizeof(g_test_last_sent_dacs_text),
             "%s", m->u.dm_text);
    g_test_last_rsp_reason = (int)m->dm_reason;

    if (scrnq_overflow() == SUCCESS) {
        LOG_WARN("worker_send_rsp: ScreenMsq overflow; dropping rsp neid=%u tty=%ld",
                 m->dm_dacsid, m->dm_tty);
        return -1;
    }
    if (qwrite(m) != SUCCESS) {
        LOG_WARN("worker_send_rsp: qwrite failed neid=%u tty=%ld",
                 m->dm_dacsid, m->dm_tty);
        return -1;
    }
    return 0;
}
```

### 5.2 Construction site — `handle_ne_data` prompt-match branch

```c
struct dacs_msg out;
memset(&out, 0, sizeof(out));
out.dm_tty       = c->orig_tty;
out.dm_old_tty   = c->orig_old_tty;
out.dm_slot      = c->orig_slot;
out.dm_slot_tp   = c->orig_slot_tp;
out.dm_dacsid    = (unsigned)c->neid;
out.dm_type      = DMTYPE_TEXT_MSG;
out.dm_to_screen = 1;
out.dm_reason    = NCLAND_RESULT_SUCCESS;
snprintf(out.u.dm_text, sizeof(out.u.dm_text), "%.*s",
         (int)(sizeof(out.u.dm_text) - 1), c->rbuf);

const ne_entry_t *e = ncland_registry_find(&wh->registry, c->dtype);
if (e) {
    for (size_t i = 0; i < e->error_res.size(); i++) {
        if (regexec(&e->error_res[i], c->rbuf, 0, NULL, 0) == 0) {
            out.dm_reason = NCLAND_RESULT_FAILURE;
            LOG_WARN("neid=%d cmd failed: matched error pattern", c->neid);
            break;
        }
    }
}
```

### 5.3 #3b stepper field renames

`conn_t::pending_rsp` is `struct dacs_msg`. `step_finish`'s POST_CMD branch:

```c
if (which == STEP_BLOCK_POST_CMD) {
    if (c->step_failed) c->pending_rsp.dm_reason = NCLAND_RESULT_FAILURE;
    worker_send_rsp(wh, &c->pending_rsp);
    c->pending_rsp.dm_tty = 0;            /* freshness sentinel (was: seq = 0) */
    c->rlen = 0; c->rbuf[0] = '\0';
    c->step_failed = 0;
    conn_set_state(c, CS_READY);
    return;
}
```

`step_abort_for_eof` similarly: write `"NE disconnected mid-post_command"` into `dm_text`, set `dm_reason = NCLAND_RESULT_FAILURE`, `worker_send_rsp`.

### 5.4 `qopen(1)` at init

```c
if (qopen(1) != SUCCESS) {
    LOG_WARN("warehouse_init: qopen(screen) failed; responses will not reach screen");
    /* Continue — useful for unit tests that don't have screen. */
}
```

Non-fatal: ncland still runs; responses just don't reach `screen`. Tests intercept via the seam regardless.

---

## 6. Error Semantics & Edge Cases

| # | Failure | Detected by | Effect |
|---|---------|-------------|--------|
| 1 | `mq_open` EINVAL | `warehouse_init` | LOG_ERROR cites `fs.mqueue.msgsize_max`; init -1; daemon exits. |
| 2 | `mq_open` EACCES | `warehouse_init` | LOG_ERROR; init -1. Recover: `mq_unlink` from privileged shell. |
| 3 | `mq_open` ENOSPC | `warehouse_init` | LOG_ERROR; init -1. Recover: raise `fs.mqueue.queues_max` or unlink stale queues. |
| 4 | `qopen(1)` fails | `warehouse_init` | **Non-fatal** WARN; daemon continues. Responses don't reach screen. |
| 5 | `mq_receive` short message | `ncland_mq_drain` | LOG_WARN; drop; continue. |
| 6 | `mq_receive` EAGAIN | `ncland_mq_drain` | Normal end-of-drain; return 0. |
| 7 | `mq_receive` other error | `ncland_mq_drain` | LOG_WARN; return -1; main loop continues. |
| 8 | Unknown `dm_dacsid` | `warehouse_handle_dacs_msg` | LOG_WARN; **silently drop**. Sender times out. |
| 9 | Target conn not `CS_READY` | `warehouse_handle_dacs_msg` | LOG_WARN; **silently drop**. |
| 10 | `ncland_parse_cmd_data` rejects | `warehouse_handle_dacs_msg` | LOG_WARN; **silently drop**. |
| 11 | `qwrite` FAILURE | `worker_send_rsp` | LOG_WARN; return -1. NE state unaffected; response lost. |
| 12 | `scrnq_overflow()` SUCCESS | `worker_send_rsp` | LOG_WARN; skip qwrite; return -1. |
| 13 | SIGTERM mid-cmd | Existing #3b drain | `shutting_down` set; new inbound silently dropped (§4.5); in-flight cmds finish via existing path; daemon exits inside `SHUTDOWN_GRACE_S`. |
| 14 | Two ncland procs against same queue | None | Both compete on `mq_receive`. Pathological; documented as ops constraint. |
| 15 | Queue persists after crash | None | Next start attaches via `O_CREAT`. Buffered cmds drain on first epoll iteration. |

### 6.1 Silent-drop is the new normal

Three rejection paths that previously returned a FAILURE reply now silently drop:

- Unknown neid
- Busy conn
- Shutting-down refuse

This is the **direct consequence of fan-out architecture** — no caller-private reply channel exists. Operators debug via the daemon log (same as legacy clan + screen).

### 6.2 Feature losses

- **`check_errors` opt-out** (#3a): dropped. Always scan; `dm_reason` always set. No production caller used the opt-out.
- **`seq` field**: dropped. Pending-stash is gone; seq served no other purpose.
- **`direct rsp`**: dropped. No direct-reply path; nclan-cmd is retired.

### 6.3 `mq_unlink` ordering

`warehouse_shutdown` ordering:

1. Set `shutting_down`, drain in-flight conns (existing #3b path).
2. `epoll_ctl(EPOLL_CTL_DEL, ctl_mqd)`.
3. `mq_close(ctl_mqd); mq_unlink(ctl_mq_name);`.
4. Existing teardown (lua_shutdown, registry_free, conn cleanup).

`mq_close` safe on `(mqd_t)-1`. `mq_unlink` of an already-removed name returns ENOENT (log DEBUG). `ctl_mq_name[0] = '\0'` after unlink prevents double-unlink on shutdown re-entry.

### 6.4 Idempotency

- `ncland_mq_init` twice: second call sees `ctl_mqd != -1`, returns -1 with WARN.
- `ncland_mq_close` twice: second call sees `(mqd_t)-1`, returns.

### 6.5 `qwrite` blocks if screen is wedged

Mitigation in §5.1: `scrnq_overflow()` guard before write. If screen is permanently wedged, ncland's rsps drop with WARN but the main loop continues — no daemon stall.

### 6.6 Expired-cmd path

`check_expired_cmds` (#3a) builds a synthetic FAILURE `dacs_msg` and ships via `qwrite` — same path as normal NE responses. Shutdown silent-drop (§6.1) is the only path that diverges.

---

## 7. Testing Strategy

### 7.1 Test seams (replacing the ZMQ-era ones)

```c
extern int  g_test_last_rsp_reason;
extern char g_test_last_sent_dacs_text[MAXDACSMSG+1];
extern int  g_test_worker_dispatched_dacsid;
```

`g_test_last_direct_rsp_result` deleted.

### 7.2 `qwrite` interception

`ncland_test_qwrite.cpp` (linked into `ncland_unit_tests` only):

```cpp
extern "C" int qwrite(struct dacs_msg *m)
{
    snprintf(g_test_last_sent_dacs_text, sizeof(g_test_last_sent_dacs_text),
             "%s", m->u.dm_text);
    g_test_last_rsp_reason = (int)m->dm_reason;
    return 0;
}
extern "C" int scrnq_overflow(void) { return 1; /* FAILURE = no overflow */ }
extern "C" int qopen(int screen)    { (void)screen; return 0; }
```

Linked into the test binary; the daemon uses real `-lutillib` `qwrite`.

### 7.3 Suite `mq` — inbound self-tests

| Test | Asserts |
|------|---------|
| `M-mq init creates and unlinks the queue` | `ncland_mq_init(wh, "/ncland-test-XYZ")` returns 0; `wh->ctl_mqd != -1`; close + unlink clean. |
| `M-mq drain dispatches a well-formed dacs_msg` | Send dacs_msg via `mq_send`; drain; assert `g_test_worker_dispatched_dacsid == N`. |
| `M-mq drain rejects short message` | Buffer < `DACS_MSG_HDR_SZ`; assert WARN, no dispatch. |
| `M-mq drain returns 0 on EAGAIN` | No data; drain returns 0. |
| `M-mq init fails clearly on EINVAL` (skip-marked) | Documented manual check; not run in CI. |

### 7.4 Suite `warehouse` — rewrites

| Existing | New form |
|----------|----------|
| `T9.7 zmq cmd dispatches to in-process conn and stashes identity` | `T9.7 mq cmd dispatches to in-process conn` — drop identity-stash assertion; assert `c->orig_tty == m->dm_tty`. |
| `T-3a cmd to unknown neid replies FAILURE` | `T-3a cmd to unknown neid silently dropped` — log assertion; no rsp check. |
| `T-3a cmd to busy (non-READY) neid replies FAILURE` | `T-3a cmd to busy neid silently dropped` — same. |
| `T-3a cmd with wrong msg_type replies FAILURE` | **deleted** — no msg_type field. |
| `T9.8 response from in-process I/O is sent on ROUTER to stashed identity` | `T9.8 response goes through qwrite with originator coords echoed` — extend seam to record `dm_tty`; assert it matches. |
| `W-3b shutting_down rejects new cmds with FAILURE` | `W-3b shutting_down silently drops new cmds` — log assertion only. |

`T9.5`/`T9.6` (pending-stash) deleted entirely.

### 7.5 Suite `worker` — rewrites

#3a tests touched (mechanical):
- Remove `c->check_errors` setup (field gone).
- Rename `g_test_last_rsp_result` → `g_test_last_rsp_reason`.
- Collapse the `check_errors off` variant of `T-3a clean response` (no opt-out).

~12 tests touched.

### 7.6 Suite `stepper` — rewrites

#3b/`I-sim` tests touched (mechanical):
- `g_test_last_sent_rsp_text` → `g_test_last_sent_dacs_text`.
- `c->pending_rsp.result` → `c->pending_rsp.dm_reason`.
- `c->pending_rsp.seq` → `c->pending_rsp.dm_tty`.

~13 tests touched.

### 7.7 `nclan_cmd` suite — deleted

Goes away with `nclan_cmd.cpp` itself. −1 test.

### 7.8 Projected suite count

| | tests |
|---|---|
| Baseline (`ncland-start` HEAD) | 141 |
| Removed (nclan_cmd, pending-stash, wrong-msg_type, check_errors-off) | −6 |
| Added (`mq` suite) | +5 |
| Net | **140** |

### 7.9 Integration / live test

No automated integration test ships with this change. The lin07n live test mirrors #3b's: deploy ncland, send a real `dacs_msg` from a real client (TL1 front-end or a `/tmp/send_mq_cmd.sh` helper), confirm the daemon log shows mq_receive → dispatch → NE response → qwrite to screen.

---

## 8. Files Touched / Created

| File | Responsibility |
|------|----------------|
| `ncland_mq.h` (new) | `ncland_mq_init`, `ncland_mq_close`, `ncland_mq_drain` decls. |
| `ncland_mq.cpp` (new) | mq_open/close/unlink, drain loop, dispatch to `warehouse_handle_dacs_msg`. |
| `ncland_zmq.cpp` | **deleted**. |
| `nclan_cmd.cpp` | **deleted**. |
| `ncland.h` | `wh->ctl_mqd`/`ctl_mq_name` fields added; `zmq_*`/`pending[]`/`NCLAND_MAX_PENDING` removed. `ncland_cmd_msg_t` and `ncland_rsp_msg_t` deleted. `conn_t.pending_rsp` retyped to `struct dacs_msg`. New `conn_t.orig_tty`/`orig_old_tty`/`orig_slot`/`orig_slot_tp`. `g_test_*` seam decls updated per §7.1. |
| `ncland_warehouse.cpp` | `warehouse_handle_cmd_from_zmq` → `warehouse_handle_dacs_msg`. Pending-stash deleted. `qopen(1)` added to init; `ncland_zmq_init`/`_close` replaced with `ncland_mq_init`/`_close`. Main-loop epoll branch switched. Shutdown adds `ncland_mq_close` + `mq_unlink`. |
| `ncland_worker.cpp` | `worker_send_rsp` body rewritten per §5.1. Construction site in `handle_ne_data` rewritten per §5.2. `g_test_worker_dispatched_seq` → `_dacsid`. |
| `ncland_stepper.cpp` | `pending_rsp` field renames in `step_finish` and `step_abort_for_eof` per §5.3. |
| `ncland_proto.cpp` | `ncland_parse_cmd_data` signature: `(ncland_cmd_msg_t *)` → `(struct dacs_msg *)`; body operates on `m->u.dm_text`. |
| `ncland_test_qwrite.cpp` (new) | Test-only stub for `qwrite`/`scrnq_overflow`/`qopen` per §7.2. Linked into `ncland_unit_tests` only. |
| `Makefile` | Drop `ncland_zmq.o` and `nclan_cmd.o` from NCLAND_OBJS. Drop `nclan-cmd` target. Add `ncland_mq.o` to NCLAND_OBJS. Add `ncland_test_qwrite.o` to `ncland_unit_tests` prereqs. Add `-lrt` to LDFLAGS for `mq_*`. |
| `ne/*.yaml`, `lua/*.lua` | Unchanged. |

---

## 9. Open Questions / Deferred

- **Real screen queue name.** ncland's outbound uses `qopen(1)` which calls `msgget(SCREEN_DI_PASSWD, ...)` — the existing screen msqid. Confirm the deployed `screen` process is actually live and reading from that msqid before declaring the live test passed.
- **Per-client migration.** Existing TL1/REST/MTOSI clients send `dacs_msg` to clan's SysV `Dacsq`. To talk to ncland, they need a parallel `mq_send` path targeting `/ncland_ctl`. Per-client migration is tracked outside this design.
- **Operator debug tool.** With `nclan-cmd` retired, ad-hoc CLI testing requires a one-off `mq_send`-based helper (out of scope; trivial to write).
- **Alarm-handler integration.** `AlrmMsqid` outbound is wired the same way as screen, deferred until ncland publishes autonomous alarms.
- **librdb_notify integration.** ZMQ stays linked for this; subscriber is still the same stub.
