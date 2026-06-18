# ncland: ZeroMQ Transport + Warehouse Seeding Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace POSIX MQ with ZeroMQ ROUTER on the caller boundary, add warehouse self-seeding from `frame_link` + `/usr/cnc/features/ncland.json`, and subscribe the warehouse to `otn_portd` `rdb_notify` events for runtime NE lifecycle changes.

**Architecture:** ZeroMQ touches only the external caller boundary; warehouse↔worker IPC stays on the existing Unix-domain `ctl_sock` (extended to carry framed cmd/rsp in addition to `SCM_RIGHTS`). Warehouse iterates `frame_link` once at startup to seed allowed-dtype connections, then reacts to PG NOTIFY events from `otn_portd` for the rest of its lifetime. `m_clan` skips allowlisted dtypes so the two processes partition the NE population without overlap.

**Tech Stack:** C++17, libzmq (`-lzmq`), librdb_notify (`-lrdb_notify`), cJSON (`-ljson` already linked), libssh, epoll, signalfd, `nfunit-test.hpp`. Targets g++ 8.5.0 (RHEL 8) and g++ 11.x (RHEL 9).

**Spec:** `docs/superpowers/specs/2026-05-04-ncland-zeromq-seeding-design.md`

---

## File Plan

**New files:**
- `cnc/ncland/src/ncland_zmq.cpp` — ZMQ context + ROUTER + send/recv wrappers + ZMQ_FD helper.
- `cnc/ncland/src/ncland_seed.cpp` — `ncland.json` parser + `frame_link` iterator.
- `cnc/ncland/src/ncland_notify.cpp` — `librdb_notify` subscriber + event dispatch table.
- `cnc/ncland/src/test/fixtures/ncland_good.json` — valid sample for unit tests.
- `cnc/ncland/src/test/fixtures/ncland_bad.json` — malformed sample for unit tests.

**Modified files:**
- `cnc/ncland/src/ncland.h` — drop `mqd_t mq_*` from `ncland_wh_t` / `ncland_wrkr_t` / `ncland_config_t`; add `void *zmq_ctx`, `void *zmq_ctl`, `int zmq_ctl_fd`, `int notify_fd`, `std::unordered_set<int> dtype_allow`, `pending_call_t pending[NCLAND_MAX_PENDING]` to `ncland_wh_t`. Drop `mq_in_name` / `mq_out_name` from `ncland_config_t`; add `ctl_endpoint`. Drop `mq_in` / `mq_out` from `ncland_wrkr_t`.
- `cnc/ncland/src/ncland.cpp` — replace `-q` / `-Q` flag handling with `-c <ctl_endpoint>` (default `ipc:///var/run/ncland/ctl.sock`) and `-f <ncland_json_path>` (default `/usr/cnc/features/ncland.json`).
- `cnc/ncland/src/ncland_warehouse.cpp` — replace `mq_ctl` open/epoll branch with ZMQ ROUTER bind + `ZMQ_FD` epoll branch; add seed call and notify subscribe in `warehouse_init`; add identity-stash and response-forwarding path; add worker-ctl_sock epoll branch.
- `cnc/ncland/src/ncland_worker.cpp` — replace `mq_in` epoll branch with `ctl_sock` framed-recv; replace `ncland_mq_send_rsp` with framed-send back to warehouse over `ctl_sock`.
- `cnc/ncland/src/ncland_proto.cpp` — delete `ncland_mq_*` and the `mq_open_helper`/`MaxMsgsForFile`; keep `parse_open_msg`, `route_cmd`, etc.
- `cnc/ncland/src/ncland_unit_tests.cpp` — delete T3.x MQ tests; register new test files (T15.x seed, T16.x notify, T17.x ZMQ).
- `cnc/ncland/src/ncland.mk` — add `-lzmq -lrdb_notify`; remove `-lrt`; add new objects (`ncland_zmq.o`, `ncland_seed.o`, `ncland_notify.o`).
- `cnc/sdi/src/m_clan.c` — load `ncland.json` allowlist at startup; skip NEs whose dtype is in the allowlist.
- `cnc/sdi/src/sdisrc.mk` — add `-ljson` if not already present.

---

## Task 1: Wire-format types + struct changes in `ncland.h`

**Why first:** every later task depends on the new struct shape. Doing this once up front avoids a cascade of edits per task.

**Files:**
- Modify: `cnc/ncland/src/ncland.h`

- [ ] **Step 1: Locate the existing struct definitions**

Run: `grep -n -E 'struct ncland_wh|struct ncland_wrkr|struct ncland_config|mqd_t' cnc/ncland/src/ncland.h`
Note the line ranges containing `ncland_wh_t`, `ncland_wrkr_t`, `ncland_config_t`.

- [ ] **Step 2: Add new ZMQ + seeding members; remove MQ members**

In `ncland_wh_t`, replace `mqd_t mq_ctl;` with:

```cpp
    /* ZeroMQ caller boundary */
    void          *zmq_ctx;          /* zmq_ctx_new() */
    void          *zmq_ctl;          /* ROUTER bound to ctl endpoint */
    int            zmq_ctl_fd;       /* ZMQ_FD readiness fd */

    /* otn_portd subscription */
    int            notify_fd;        /* librdb_notify fd */

    /* Startup-loaded dtype allowlist (read-only after seed) */
    std::unordered_set<int> dtype_allow;

    /* Pending calls awaiting worker response — keyed by msg.seq.
       identity_len == 0 means slot free. */
    struct pending_call_t {
        int      seq;
        size_t   identity_len;
        uint8_t  identity[256];
    }              pending[NCLAND_MAX_PENDING];
```

In `ncland_wrkr_t`, delete:

```cpp
    mqd_t      mq_in;
    mqd_t      mq_out;
```

(Worker only needs `ctl_sock`, which is already in the struct.)

In `ncland_config_t`, replace `mq_in_name[256]` / `mq_out_name[256]` with:

```cpp
    char ctl_endpoint[256];   /* zmq endpoint, e.g. ipc:///var/run/ncland/ctl.sock */
    char ncland_json_path[256];
```

Add at top of header (under existing includes):

```cpp
#include <unordered_set>
#include <stdint.h>
```

Add macro near other limits:

```cpp
#define NCLAND_MAX_PENDING 256
```

- [ ] **Step 3: Add new prototype declarations**

After existing `warehouse_*` prototypes, add:

```cpp
/* ncland_zmq.cpp */
int  ncland_zmq_init(ncland_wh_t *wh, const char *endpoint);
void ncland_zmq_close(ncland_wh_t *wh);
int  ncland_zmq_recv_cmd(ncland_wh_t *wh,
                         uint8_t *identity, size_t *identity_len,
                         ncland_cmd_msg_t *cmd_out);
int  ncland_zmq_send_rsp(ncland_wh_t *wh,
                         const uint8_t *identity, size_t identity_len,
                         const ncland_rsp_msg_t *rsp);
int  ncland_zmq_drain(ncland_wh_t *wh);   /* loop until EAGAIN */

/* ncland_seed.cpp */
int  ncland_seed_load_filter(const char *path,
                             std::unordered_set<int> *out);
int  ncland_seed_run(ncland_wh_t *wh);

/* ncland_notify.cpp */
int  ncland_notify_init(ncland_wh_t *wh);
void ncland_notify_close(ncland_wh_t *wh);
int  ncland_notify_drain(ncland_wh_t *wh);

/* Pending-call table (warehouse) */
int  warehouse_pending_stash(ncland_wh_t *wh, int seq,
                             const uint8_t *identity, size_t identity_len);
int  warehouse_pending_take (ncland_wh_t *wh, int seq,
                             uint8_t *identity_out, size_t *identity_len_out);
```

Delete the prototypes for `ncland_mq_open_in`, `ncland_mq_open_out`, `ncland_mq_close_all`, `ncland_mq_send_rsp`, `ncland_mq_recv_cmd`.

- [ ] **Step 4: Verify it still parses**

Run: `cd cnc/ncland/src && nmake -f ncland.mk ncland.o`
Expected: compile errors about missing `mq_*` symbols and `mqd_t` references in `ncland_warehouse.cpp` / `ncland_worker.cpp` / `ncland.cpp` / `ncland_proto.cpp`.

This is intentional — those errors are the worklist for the next tasks.

- [ ] **Step 5: Commit**

```bash
git add cnc/ncland/src/ncland.h
git commit -m "ncland: add ZMQ + seed types to ncland.h; remove MQ types"
```

---

## Task 2: ZeroMQ wrapper module — context + bind

**Files:**
- Create: `cnc/ncland/src/ncland_zmq.cpp`
- Modify: `cnc/ncland/src/ncland_unit_tests.cpp`

- [ ] **Step 1: Write the failing test for `ncland_zmq_init` / `ncland_zmq_close` round-trip**

Add to `ncland_unit_tests.cpp` (after existing T-blocks):

```cpp
#include <zmq.h>
#include <unistd.h>

TEST("zmq", "T17.1 init binds ROUTER and exposes ZMQ_FD") {
    ncland_wh_t wh;
    memset(&wh, 0, sizeof(wh));
    const char *ep = "ipc:///tmp/ncland_test_t17_1.sock";
    unlink("/tmp/ncland_test_t17_1.sock");

    int rc = ncland_zmq_init(&wh, ep);
    REQUIRE_EQ(rc, 0);
    REQUIRE(wh.zmq_ctx != nullptr);
    REQUIRE(wh.zmq_ctl != nullptr);
    REQUIRE_GT(wh.zmq_ctl_fd, 0);

    ncland_zmq_close(&wh);
    REQUIRE_EQ(wh.zmq_ctx, nullptr);
    REQUIRE_EQ(wh.zmq_ctl, nullptr);
    unlink("/tmp/ncland_test_t17_1.sock");
}
```

- [ ] **Step 2: Run the test; expect link/build failure**

Run: `cd cnc/ncland/src && nmake -f ncland.mk ncland_unit_tests`
Expected: linker errors (`undefined reference to ncland_zmq_init`).

- [ ] **Step 3: Implement minimal `ncland_zmq_init` / `ncland_zmq_close`**

Create `cnc/ncland/src/ncland_zmq.cpp`:

```cpp
// ncland_zmq.cpp — ZeroMQ ROUTER on the caller boundary

#include "ncland.h"
#include "nflog.hpp"

#include <zmq.h>
#include <errno.h>
#include <string.h>

/**
 * @brief Create a ZMQ context and bind the ROUTER socket.
 *
 * @param wh        Warehouse state.
 * @param endpoint  ZeroMQ endpoint (e.g. "ipc:///var/run/ncland/ctl.sock").
 * @return 0 on success, -1 on error (wh fields rolled back).
 */
int ncland_zmq_init(ncland_wh_t *wh, const char *endpoint)
{
    LOG_DEBUG("ncland_zmq_init: wh=%p endpoint=%s", (void *)wh, endpoint);
    if (!wh || !endpoint) return -1;

    wh->zmq_ctx = zmq_ctx_new();
    if (!wh->zmq_ctx) {
        LOG_ERROR("zmq_ctx_new failed: %s", zmq_strerror(errno));
        return -1;
    }

    wh->zmq_ctl = zmq_socket(wh->zmq_ctx, ZMQ_ROUTER);
    if (!wh->zmq_ctl) {
        LOG_ERROR("zmq_socket(ROUTER) failed: %s", zmq_strerror(errno));
        zmq_ctx_term(wh->zmq_ctx);
        wh->zmq_ctx = nullptr;
        return -1;
    }

    int hwm = 4096;
    zmq_setsockopt(wh->zmq_ctl, ZMQ_SNDHWM, &hwm, sizeof(hwm));
    zmq_setsockopt(wh->zmq_ctl, ZMQ_RCVHWM, &hwm, sizeof(hwm));

    if (zmq_bind(wh->zmq_ctl, endpoint) != 0) {
        LOG_ERROR("zmq_bind(%s) failed: %s", endpoint, zmq_strerror(errno));
        zmq_close(wh->zmq_ctl);
        zmq_ctx_term(wh->zmq_ctx);
        wh->zmq_ctl = nullptr;
        wh->zmq_ctx = nullptr;
        return -1;
    }

    int    fd = -1;
    size_t sz = sizeof(fd);
    if (zmq_getsockopt(wh->zmq_ctl, ZMQ_FD, &fd, &sz) != 0 || fd < 0) {
        LOG_ERROR("zmq_getsockopt(ZMQ_FD) failed: %s", zmq_strerror(errno));
        ncland_zmq_close(wh);
        return -1;
    }
    wh->zmq_ctl_fd = fd;
    return 0;
}

/**
 * @brief Tear down ROUTER and context. Idempotent.
 */
void ncland_zmq_close(ncland_wh_t *wh)
{
    LOG_DEBUG("ncland_zmq_close: wh=%p", (void *)wh);
    if (!wh) return;
    if (wh->zmq_ctl) { zmq_close(wh->zmq_ctl); wh->zmq_ctl = nullptr; }
    if (wh->zmq_ctx) { zmq_ctx_term(wh->zmq_ctx); wh->zmq_ctx = nullptr; }
    wh->zmq_ctl_fd = -1;
}
```

Add `ncland_zmq.o` to `NCLAND_OBJS` in `ncland.mk`:

```
NCLAND_OBJS = ncland_conn.o ncland_proto.o ncland_ssh.o \
	ncland_warehouse.o ncland_worker.o ncland_telnet.o \
	ncland_zmq.o
```

Add `-lzmq` to both link rules in `ncland.mk`:

```
$(PBIN)/ncland :: ncland.o $(NCLAND_OBJS) $(INCLIBS) -latm -linc -lxmlparse -lssh -ljson -lnetclan
	$(CPLUS_CC) $(CCFLAGS:M!=-AC99) $(LDFLAGS) -o $(<) $(*) -lpthread -lssh -llua -lzmq

ncland_unit_tests :: ncland_unit_tests.o $(NCLAND_OBJS)
	$(CPLUS_CC) $(CCFLAGS:M!=-AC99) $(LDFLAGS) -o $(<) $(*) -lpthread -lssh -llua -lzmq
```

(`-lrt` removed — no MQ.)

- [ ] **Step 4: Run the test; expect pass**

Run: `cd cnc/ncland/src && nmake -f ncland.mk ncland_unit_tests && ./ncland_unit_tests -t 'T17.1'`
Expected: `1 passed, 0 failed`.

- [ ] **Step 5: Commit**

```bash
git add cnc/ncland/src/ncland_zmq.cpp cnc/ncland/src/ncland.mk cnc/ncland/src/ncland_unit_tests.cpp
git commit -m "ncland: add ncland_zmq module with ROUTER bind/close"
```

---

## Task 3: ZMQ recv/send multipart frames

**Files:**
- Modify: `cnc/ncland/src/ncland_zmq.cpp`
- Modify: `cnc/ncland/src/ncland_unit_tests.cpp`

Wire format: ROUTER receives `[identity][empty][cmd_msg_bytes]`. When responding, send `[identity][empty][rsp_msg_bytes]`.

- [ ] **Step 1: Write the failing recv/send test**

Add to `ncland_unit_tests.cpp`:

```cpp
TEST("zmq", "T17.2 recv_cmd / send_rsp round-trip via DEALER client") {
    ncland_wh_t wh;
    memset(&wh, 0, sizeof(wh));
    const char *ep = "ipc:///tmp/ncland_test_t17_2.sock";
    unlink("/tmp/ncland_test_t17_2.sock");
    REQUIRE_EQ(ncland_zmq_init(&wh, ep), 0);

    void *cctx = zmq_ctx_new();
    void *dealer = zmq_socket(cctx, ZMQ_DEALER);
    REQUIRE_EQ(zmq_connect(dealer, ep), 0);

    /* Client sends a cmd_msg (one frame). DEALER prepends an empty
       delimiter automatically when the peer is a ROUTER. */
    ncland_cmd_msg_t in;
    memset(&in, 0, sizeof(in));
    in.conn_id = 5;
    in.seq     = 99;
    snprintf(in.data, sizeof(in.data), "PING");

    REQUIRE_EQ(zmq_send(dealer, "", 0, ZMQ_SNDMORE), 0);
    REQUIRE_GT(zmq_send(dealer, &in, sizeof(in), 0), 0);

    /* Warehouse receives. */
    uint8_t identity[256];
    size_t  identity_len = sizeof(identity);
    ncland_cmd_msg_t out;
    int rc;
    for (int tries = 0; tries < 50; tries++) {
        rc = ncland_zmq_recv_cmd(&wh, identity, &identity_len, &out);
        if (rc == 0) break;
        usleep(20000);
        identity_len = sizeof(identity);
    }
    REQUIRE_EQ(rc, 0);
    REQUIRE_EQ(out.conn_id, 5);
    REQUIRE_EQ(out.seq,     99);
    REQUIRE(strcmp(out.data, "PING") == 0);
    REQUIRE_GT(identity_len, 0u);

    /* Warehouse sends back a response. */
    ncland_rsp_msg_t rsp;
    memset(&rsp, 0, sizeof(rsp));
    rsp.conn_id = 5;
    rsp.seq     = 99;
    snprintf(rsp.text, sizeof(rsp.text), "PONG");
    REQUIRE_EQ(ncland_zmq_send_rsp(&wh, identity, identity_len, &rsp), 0);

    /* DEALER receives [empty][rsp_bytes] */
    zmq_msg_t m;
    zmq_msg_init(&m); zmq_msg_recv(&m, dealer, 0); zmq_msg_close(&m);
    zmq_msg_init(&m);
    int n = zmq_msg_recv(&m, dealer, 0);
    REQUIRE_EQ((size_t)n, sizeof(rsp));
    ncland_rsp_msg_t got = *(ncland_rsp_msg_t *)zmq_msg_data(&m);
    zmq_msg_close(&m);
    REQUIRE_EQ(got.conn_id, 5);
    REQUIRE_EQ(got.seq,     99);
    REQUIRE(strcmp(got.text, "PONG") == 0);

    zmq_close(dealer);
    zmq_ctx_term(cctx);
    ncland_zmq_close(&wh);
    unlink("/tmp/ncland_test_t17_2.sock");
}
```

- [ ] **Step 2: Run; expect failure (functions undefined)**

Run: `cd cnc/ncland/src && nmake -f ncland.mk ncland_unit_tests`
Expected: link error for `ncland_zmq_recv_cmd` / `ncland_zmq_send_rsp`.

- [ ] **Step 3: Implement recv/send**

Append to `ncland_zmq.cpp`:

```cpp
/**
 * @brief Receive one ROUTER frame triplet [identity][empty][cmd_msg].
 *
 * @param wh             Warehouse state.
 * @param identity       Output buffer for caller identity bytes.
 * @param identity_len   In: capacity. Out: bytes written.
 * @param cmd_out        Output cmd_msg.
 * @return 0 on success, -1 on EAGAIN/error (errno preserved on EAGAIN).
 */
int ncland_zmq_recv_cmd(ncland_wh_t *wh,
                        uint8_t *identity, size_t *identity_len,
                        ncland_cmd_msg_t *cmd_out)
{
    if (!wh || !wh->zmq_ctl || !identity || !identity_len || !cmd_out)
        return -1;

    zmq_msg_t id_msg, empty_msg, body_msg;
    zmq_msg_init(&id_msg);
    int n = zmq_msg_recv(&id_msg, wh->zmq_ctl, ZMQ_DONTWAIT);
    if (n < 0) { zmq_msg_close(&id_msg); return -1; }

    size_t id_sz = zmq_msg_size(&id_msg);
    if (id_sz > *identity_len) { zmq_msg_close(&id_msg); errno = EMSGSIZE; return -1; }
    memcpy(identity, zmq_msg_data(&id_msg), id_sz);
    *identity_len = id_sz;
    zmq_msg_close(&id_msg);

    zmq_msg_init(&empty_msg);
    if (zmq_msg_recv(&empty_msg, wh->zmq_ctl, 0) < 0) {
        zmq_msg_close(&empty_msg); return -1;
    }
    zmq_msg_close(&empty_msg);

    zmq_msg_init(&body_msg);
    n = zmq_msg_recv(&body_msg, wh->zmq_ctl, 0);
    if (n != (int)sizeof(*cmd_out)) {
        LOG_WARN("zmq recv body size=%d expected=%zu", n, sizeof(*cmd_out));
        zmq_msg_close(&body_msg);
        errno = EBADMSG;
        return -1;
    }
    memcpy(cmd_out, zmq_msg_data(&body_msg), sizeof(*cmd_out));
    zmq_msg_close(&body_msg);
    return 0;
}

/**
 * @brief Send a response back to a stashed identity.
 */
int ncland_zmq_send_rsp(ncland_wh_t *wh,
                        const uint8_t *identity, size_t identity_len,
                        const ncland_rsp_msg_t *rsp)
{
    if (!wh || !wh->zmq_ctl || !identity || identity_len == 0 || !rsp)
        return -1;
    if (zmq_send(wh->zmq_ctl, identity, identity_len, ZMQ_SNDMORE) < 0) return -1;
    if (zmq_send(wh->zmq_ctl, "", 0, ZMQ_SNDMORE) < 0)                  return -1;
    if (zmq_send(wh->zmq_ctl, rsp, sizeof(*rsp), 0) < 0)                return -1;
    return 0;
}
```

- [ ] **Step 4: Run; expect pass**

Run: `./ncland_unit_tests -t 'T17.2'`
Expected: pass.

- [ ] **Step 5: Commit**

```bash
git add cnc/ncland/src/ncland_zmq.cpp cnc/ncland/src/ncland_unit_tests.cpp
git commit -m "ncland_zmq: add multipart recv_cmd / send_rsp"
```

---

## Task 4: ZMQ drain loop (handle ZMQ_FD edge-trigger semantics)

**Why:** `ZMQ_FD` only signals readiness, not bytes-ready. After `EPOLLIN` fires, must loop `recv` until `EAGAIN` and re-check `ZMQ_EVENTS`.

**Files:**
- Modify: `cnc/ncland/src/ncland_zmq.cpp`
- Modify: `cnc/ncland/src/ncland_unit_tests.cpp`

- [ ] **Step 1: Write the failing test (drain handles two queued frames)**

```cpp
TEST("zmq", "T17.3 drain processes all queued frames before returning") {
    ncland_wh_t wh; memset(&wh, 0, sizeof(wh));
    const char *ep = "ipc:///tmp/ncland_test_t17_3.sock";
    unlink("/tmp/ncland_test_t17_3.sock");
    REQUIRE_EQ(ncland_zmq_init(&wh, ep), 0);

    void *cctx = zmq_ctx_new();
    void *dealer = zmq_socket(cctx, ZMQ_DEALER);
    zmq_connect(dealer, ep);

    for (int i = 0; i < 3; i++) {
        ncland_cmd_msg_t cmd; memset(&cmd, 0, sizeof(cmd));
        cmd.seq = 1000 + i;
        zmq_send(dealer, "", 0, ZMQ_SNDMORE);
        zmq_send(dealer, &cmd, sizeof(cmd), 0);
    }
    usleep(50000);

    /* Stub dispatcher counts how many cmds the drain delivered. */
    extern int g_test_zmq_drain_count;
    g_test_zmq_drain_count = 0;
    int rc = ncland_zmq_drain(&wh);
    REQUIRE_EQ(rc, 0);
    REQUIRE_EQ(g_test_zmq_drain_count, 3);

    zmq_close(dealer); zmq_ctx_term(cctx);
    ncland_zmq_close(&wh);
    unlink("/tmp/ncland_test_t17_3.sock");
}
```

- [ ] **Step 2: Build; expect undefined `ncland_zmq_drain` and missing test counter**

Run: `nmake -f ncland.mk ncland_unit_tests`
Expected: link errors.

- [ ] **Step 3: Implement `ncland_zmq_drain` and test hook**

Append to `ncland_zmq.cpp`:

```cpp
#ifdef NCLAND_TEST
int g_test_zmq_drain_count = 0;
#endif

/**
 * @brief Drain ROUTER until EAGAIN. For each cmd, dispatch via
 * warehouse_handle_cmd_from_zmq() (defined in ncland_warehouse.cpp).
 *
 * @return 0 on success (drained), -1 on hard error.
 */
int ncland_zmq_drain(ncland_wh_t *wh)
{
    if (!wh || !wh->zmq_ctl) return -1;

    for (;;) {
        uint32_t events = 0;
        size_t   sz = sizeof(events);
        if (zmq_getsockopt(wh->zmq_ctl, ZMQ_EVENTS, &events, &sz) != 0) return -1;
        if ((events & ZMQ_POLLIN) == 0) return 0;

        uint8_t identity[256];
        size_t  identity_len = sizeof(identity);
        ncland_cmd_msg_t cmd;
        int rc = ncland_zmq_recv_cmd(wh, identity, &identity_len, &cmd);
        if (rc != 0) {
            if (errno == EAGAIN) return 0;
            return -1;
        }
#ifdef NCLAND_TEST
        g_test_zmq_drain_count++;
        (void)warehouse_handle_cmd_from_zmq;  /* avoid unused warning */
#else
        warehouse_handle_cmd_from_zmq(wh, identity, identity_len, &cmd);
#endif
    }
}
```

Add a forward declaration `void warehouse_handle_cmd_from_zmq(ncland_wh_t*, const uint8_t*, size_t, ncland_cmd_msg_t*);` to `ncland.h` (next to existing warehouse prototypes). Provide a weak stub in `ncland_warehouse.cpp` (real body comes in Task 6):

```cpp
__attribute__((weak)) void warehouse_handle_cmd_from_zmq(
    ncland_wh_t *wh, const uint8_t *id, size_t id_len, ncland_cmd_msg_t *cmd)
{
    (void)wh; (void)id; (void)id_len; (void)cmd;
}
```

- [ ] **Step 4: Run; expect pass**

Run: `./ncland_unit_tests -t 'T17.3'`
Expected: pass.

- [ ] **Step 5: Commit**

```bash
git add cnc/ncland/src/ncland_zmq.cpp cnc/ncland/src/ncland.h cnc/ncland/src/ncland_warehouse.cpp cnc/ncland/src/ncland_unit_tests.cpp
git commit -m "ncland_zmq: add drain loop honoring ZMQ_FD edge-trigger"
```

---

## Task 5: Pending-call identity stash

**Files:**
- Create: stash functions in `cnc/ncland/src/ncland_warehouse.cpp` (new section)
- Modify: `cnc/ncland/src/ncland_unit_tests.cpp`

- [ ] **Step 1: Write the failing tests for stash/take**

```cpp
TEST("warehouse", "T9.5 pending stash + take round-trip") {
    ncland_wh_t wh; memset(&wh, 0, sizeof(wh));
    uint8_t id[] = {1,2,3,4,5};
    REQUIRE_EQ(warehouse_pending_stash(&wh, 42, id, sizeof(id)), 0);

    uint8_t  out[256];
    size_t   out_len = sizeof(out);
    REQUIRE_EQ(warehouse_pending_take(&wh, 42, out, &out_len), 0);
    REQUIRE_EQ(out_len, sizeof(id));
    REQUIRE_EQ(memcmp(out, id, sizeof(id)), 0);

    /* Second take of same seq must fail. */
    out_len = sizeof(out);
    REQUIRE_NE(warehouse_pending_take(&wh, 42, out, &out_len), 0);
}

TEST("warehouse", "T9.6 pending table full returns error") {
    ncland_wh_t wh; memset(&wh, 0, sizeof(wh));
    uint8_t id[] = {0xAA};
    for (int i = 0; i < NCLAND_MAX_PENDING; i++) {
        REQUIRE_EQ(warehouse_pending_stash(&wh, i+1, id, sizeof(id)), 0);
    }
    REQUIRE_NE(warehouse_pending_stash(&wh, 9999, id, sizeof(id)), 0);
}
```

- [ ] **Step 2: Run; expect undefined**

Run: `nmake -f ncland.mk ncland_unit_tests`
Expected: link errors.

- [ ] **Step 3: Implement stash/take in `ncland_warehouse.cpp`**

Append to `ncland_warehouse.cpp`:

```cpp
/**
 * @brief Stash caller identity for later response forwarding.
 *
 * @return 0 on success, -1 if table full.
 */
int warehouse_pending_stash(ncland_wh_t *wh, int seq,
                            const uint8_t *identity, size_t identity_len)
{
    if (!wh || !identity || identity_len == 0
        || identity_len > sizeof(wh->pending[0].identity))
        return -1;

    int i;
    for (i = 0; i < NCLAND_MAX_PENDING; i++) {
        if (wh->pending[i].identity_len == 0) {
            wh->pending[i].seq          = seq;
            wh->pending[i].identity_len = identity_len;
            memcpy(wh->pending[i].identity, identity, identity_len);
            return 0;
        }
    }
    LOG_WARN("pending table full (seq=%d)", seq);
    return -1;
}

/**
 * @brief Retrieve and remove an identity by seq.
 */
int warehouse_pending_take(ncland_wh_t *wh, int seq,
                           uint8_t *identity_out, size_t *identity_len_out)
{
    if (!wh || !identity_out || !identity_len_out) return -1;

    int i;
    for (i = 0; i < NCLAND_MAX_PENDING; i++) {
        if (wh->pending[i].identity_len > 0 && wh->pending[i].seq == seq) {
            if (*identity_len_out < wh->pending[i].identity_len) return -1;
            memcpy(identity_out, wh->pending[i].identity, wh->pending[i].identity_len);
            *identity_len_out = wh->pending[i].identity_len;
            wh->pending[i].identity_len = 0;
            return 0;
        }
    }
    return -1;
}
```

- [ ] **Step 4: Run; expect pass**

Run: `./ncland_unit_tests -t 'T9.5' -t 'T9.6'`
Expected: 2 passed.

- [ ] **Step 5: Commit**

```bash
git add cnc/ncland/src/ncland_warehouse.cpp cnc/ncland/src/ncland_unit_tests.cpp
git commit -m "warehouse: add pending-call identity stash"
```

---

## Task 6: Warehouse cmd-from-ZMQ handler + worker ctl_sock framed protocol

**Goal:** Replace `warehouse_init` MQ open with ZMQ init. Replace warehouse epoll branch on `mq_ctl` with a branch on `zmq_ctl_fd` calling `ncland_zmq_drain`. Implement `warehouse_handle_cmd_from_zmq` (the real body): stash identity, look up worker for `cmd.conn_id`, forward `cmd` over `worker[].ctl_sock` using a 1-byte tag (`'C'` = command frame, `'F'` = SCM_RIGHTS fd transfer, `'R'` = response frame from worker) followed by the raw `ncland_cmd_msg_t`.

**Files:**
- Modify: `cnc/ncland/src/ncland_warehouse.cpp`
- Modify: `cnc/ncland/src/ncland_worker.cpp`
- Modify: `cnc/ncland/src/ncland.h` (add `WH_CTL_TAG_*` enum)

- [ ] **Step 1: Add tag enum to `ncland.h`**

```cpp
enum {
    NCLAND_CTL_TAG_FD       = 'F',  /* SCM_RIGHTS fd transfer */
    NCLAND_CTL_TAG_CMD      = 'C',  /* warehouse → worker: ncland_cmd_msg_t */
    NCLAND_CTL_TAG_RSP      = 'R',  /* worker → warehouse: ncland_rsp_msg_t */
    NCLAND_CTL_TAG_SHUTDOWN = 'X'   /* warehouse → worker: graceful exit */
};
```

- [ ] **Step 2: Write failing test for warehouse → worker forward via socketpair**

```cpp
TEST("warehouse", "T9.7 forward zmq cmd over worker ctl_sock with tag C") {
    ncland_wh_t wh; memset(&wh, 0, sizeof(wh));
    int sv[2]; socketpair(AF_UNIX, SOCK_STREAM, 0, sv);
    wh.nworkers = 1;
    wh.worker[0].ctl_sock = sv[0];
    wh.worker[0].nconns   = 1;

    /* Map conn_id 7 → worker 0 (test stub). */
    wh.conn[7].state = CS_READY;
    wh.conn[7].id    = 7;
    /* (real conn_to_worker map populated by warehouse_assign_conn; for the
       test we just rely on least_loaded_worker / direct lookup) */

    uint8_t identity[] = {0xDE, 0xAD};
    ncland_cmd_msg_t cmd; memset(&cmd, 0, sizeof(cmd));
    cmd.conn_id = 7;
    cmd.seq     = 11;
    snprintf(cmd.data, sizeof(cmd.data), "show ver");

    warehouse_handle_cmd_from_zmq(&wh, identity, sizeof(identity), &cmd);

    /* Read the framed message off sv[1]. */
    char tag = 0;
    REQUIRE_EQ(read(sv[1], &tag, 1), 1);
    REQUIRE_EQ(tag, NCLAND_CTL_TAG_CMD);
    ncland_cmd_msg_t got;
    REQUIRE_EQ(read(sv[1], &got, sizeof(got)), (ssize_t)sizeof(got));
    REQUIRE_EQ(got.conn_id, 7);
    REQUIRE_EQ(got.seq,     11);

    /* Identity stashed under seq 11. */
    uint8_t out[256]; size_t out_len = sizeof(out);
    REQUIRE_EQ(warehouse_pending_take(&wh, 11, out, &out_len), 0);
    REQUIRE_EQ(out_len, sizeof(identity));

    close(sv[0]); close(sv[1]);
}
```

- [ ] **Step 3: Run; expect failure (current weak stub does nothing)**

Run: `./ncland_unit_tests -t 'T9.7'`
Expected: fail (no read available).

- [ ] **Step 4: Implement real `warehouse_handle_cmd_from_zmq`**

Replace the weak stub in `ncland_warehouse.cpp`:

```cpp
/**
 * @brief Handle a single cmd received over ZMQ ROUTER.
 *
 * Stashes identity by seq, looks up which worker owns conn_id, and
 * forwards [tag=C][cmd_msg bytes] over that worker's ctl_sock.
 */
void warehouse_handle_cmd_from_zmq(ncland_wh_t *wh,
                                   const uint8_t *identity, size_t identity_len,
                                   ncland_cmd_msg_t *cmd)
{
    if (!wh || !cmd) return;

    if (cmd->conn_id < 0 || cmd->conn_id >= MAX_CONNS) {
        LOG_WARN("zmq cmd with bad conn_id=%d", cmd->conn_id);
        return;
    }

    /* For a fresh OPEN, conn_id may be -1 to mean "warehouse picks slot".
       That code path is added in Task 7; for now route only existing conns. */
    int worker = wh->conn[cmd->conn_id].owning_worker;
    if (worker < 0 || worker >= wh->nworkers) {
        LOG_WARN("no worker assigned for conn_id=%d", cmd->conn_id);
        return;
    }

    if (warehouse_pending_stash(wh, cmd->seq, identity, identity_len) != 0) {
        LOG_ERROR("pending stash failed for seq=%d", cmd->seq);
        return;
    }

    char tag = NCLAND_CTL_TAG_CMD;
    int sock = wh->worker[worker].ctl_sock;
    if (write(sock, &tag, 1) != 1
        || write(sock, cmd, sizeof(*cmd)) != (ssize_t)sizeof(*cmd)) {
        LOG_ERROR("write to worker ctl_sock failed: %s", strerror(errno));
        size_t dummy = 256; uint8_t dump[256];
        warehouse_pending_take(wh, cmd->seq, dump, &dummy);  /* roll back stash */
    }
}
```

Add `int owning_worker;` member to `conn_t` in `ncland.h` (default `-1` in `conn_init`).

- [ ] **Step 5: Run; expect pass**

Run: `./ncland_unit_tests -t 'T9.7'`
Expected: pass.

- [ ] **Step 6: Commit**

```bash
git add cnc/ncland/src/ncland.h cnc/ncland/src/ncland_warehouse.cpp cnc/ncland/src/ncland_conn.cpp cnc/ncland/src/ncland_unit_tests.cpp
git commit -m "warehouse: forward ZMQ cmds over worker ctl_sock with framing tag"
```

---

## Task 7: Worker side — read framed cmd from ctl_sock; write framed rsp back

**Files:**
- Modify: `cnc/ncland/src/ncland_worker.cpp`
- Modify: `cnc/ncland/src/ncland_unit_tests.cpp`

- [ ] **Step 1: Write failing test for worker frame parser**

```cpp
TEST("worker", "T8.9 worker reads C-tagged cmd from ctl_sock") {
    int sv[2]; socketpair(AF_UNIX, SOCK_STREAM, 0, sv);
    ncland_wrkr_t w; memset(&w, 0, sizeof(w));
    w.ctl_sock = sv[1];

    /* Inject a C-tagged frame on sv[0]. */
    char tag = NCLAND_CTL_TAG_CMD;
    ncland_cmd_msg_t cmd; memset(&cmd, 0, sizeof(cmd));
    cmd.conn_id = 4;
    cmd.seq     = 77;
    snprintf(cmd.data, sizeof(cmd.data), "PING");
    write(sv[0], &tag, 1);
    write(sv[0], &cmd, sizeof(cmd));

    extern int g_test_worker_dispatched_seq;
    g_test_worker_dispatched_seq = 0;
    int rc = worker_drain_ctl_sock(&w);
    REQUIRE_EQ(rc, 0);
    REQUIRE_EQ(g_test_worker_dispatched_seq, 77);

    close(sv[0]); close(sv[1]);
}
```

- [ ] **Step 2: Run; expect undefined**

Run: `nmake -f ncland.mk ncland_unit_tests`
Expected: link errors.

- [ ] **Step 3: Implement `worker_drain_ctl_sock`**

Append to `ncland_worker.cpp`:

```cpp
#ifdef NCLAND_TEST
int g_test_worker_dispatched_seq = 0;
#endif

/**
 * @brief Read framed messages off ctl_sock until EAGAIN.
 *
 * Frame layout: 1-byte tag (NCLAND_CTL_TAG_*) + payload of fixed size
 * derived from the tag. C-tag carries ncland_cmd_msg_t; F-tag carries an
 * SCM_RIGHTS-passed fd; X-tag carries no payload.
 */
int worker_drain_ctl_sock(ncland_wrkr_t *w)
{
    if (!w || w->ctl_sock < 0) return -1;

    for (;;) {
        char tag;
        ssize_t n = recv(w->ctl_sock, &tag, 1, MSG_DONTWAIT);
        if (n == 0)               return 0;                 /* peer closed */
        if (n < 0 && errno == EAGAIN) return 0;
        if (n != 1)               return -1;

        switch (tag) {
        case NCLAND_CTL_TAG_CMD: {
            ncland_cmd_msg_t cmd;
            ssize_t got = 0;
            while (got < (ssize_t)sizeof(cmd)) {
                ssize_t r = recv(w->ctl_sock, ((char*)&cmd)+got,
                                 sizeof(cmd)-got, 0);
                if (r <= 0) return -1;
                got += r;
            }
#ifdef NCLAND_TEST
            g_test_worker_dispatched_seq = cmd.seq;
#else
            worker_route_cmd(w, &cmd);  /* existing dispatch fn */
#endif
            break;
        }
        case NCLAND_CTL_TAG_SHUTDOWN:
            w->running = 0;
            return 0;
        default:
            LOG_WARN("worker_drain_ctl_sock: unknown tag '%c'", tag);
            return -1;
        }
    }
}

/**
 * @brief Send a response frame back to the warehouse over ctl_sock.
 */
int worker_send_rsp(ncland_wrkr_t *w, const ncland_rsp_msg_t *rsp)
{
    if (!w || w->ctl_sock < 0 || !rsp) return -1;
    char tag = NCLAND_CTL_TAG_RSP;
    if (write(w->ctl_sock, &tag, 1) != 1)                       return -1;
    if (write(w->ctl_sock, rsp, sizeof(*rsp)) != (ssize_t)sizeof(*rsp)) return -1;
    return 0;
}
```

Replace every existing worker call to `ncland_mq_send_rsp(ctx->mq_out, &rsp);` with `worker_send_rsp(ctx, &rsp);`.

In the worker main loop, replace the `mq_in` epoll branch with a `ctl_sock` branch that calls `worker_drain_ctl_sock(ctx)`. Remove `worker_register_mq` / its caller. Add `worker_register_ctl_sock` that does `epoll_ctl(ADD, ctx->ctl_sock, EPOLLIN | EPOLLET)`.

- [ ] **Step 4: Run; expect pass**

Run: `./ncland_unit_tests -t 'T8.9'`
Expected: pass. Run full step 8 suite to confirm no regression: `./ncland_unit_tests -s 8`.

- [ ] **Step 5: Commit**

```bash
git add cnc/ncland/src/ncland_worker.cpp cnc/ncland/src/ncland_unit_tests.cpp
git commit -m "worker: replace mq_in/mq_out with ctl_sock framed cmd/rsp"
```

---

## Task 8: Warehouse — drain worker ctl_sock for R-tag and forward to caller

**Files:**
- Modify: `cnc/ncland/src/ncland_warehouse.cpp`
- Modify: `cnc/ncland/src/ncland_unit_tests.cpp`

- [ ] **Step 1: Failing test for response forwarding**

```cpp
TEST("warehouse", "T9.8 R-tagged rsp from worker is sent on ROUTER to stashed identity") {
    ncland_wh_t wh; memset(&wh, 0, sizeof(wh));
    const char *ep = "ipc:///tmp/ncland_test_t9_8.sock";
    unlink("/tmp/ncland_test_t9_8.sock");
    REQUIRE_EQ(ncland_zmq_init(&wh, ep), 0);

    /* Stash an identity for seq=55. */
    void *cctx = zmq_ctx_new();
    void *dealer = zmq_socket(cctx, ZMQ_DEALER);
    zmq_setsockopt(dealer, ZMQ_IDENTITY, "test_caller", 11);
    zmq_connect(dealer, ep);
    /* Round-trip a dummy cmd so ROUTER has the identity registered. */
    ncland_cmd_msg_t poke; memset(&poke, 0, sizeof(poke));
    poke.seq = 55;
    zmq_send(dealer, "", 0, ZMQ_SNDMORE);
    zmq_send(dealer, &poke, sizeof(poke), 0);
    usleep(50000);
    uint8_t id[256]; size_t id_len = sizeof(id);
    ncland_cmd_msg_t got;
    REQUIRE_EQ(ncland_zmq_recv_cmd(&wh, id, &id_len, &got), 0);
    REQUIRE_EQ(warehouse_pending_stash(&wh, 55, id, id_len), 0);

    /* Inject an R-frame from worker ctl_sock. */
    int sv[2]; socketpair(AF_UNIX, SOCK_STREAM, 0, sv);
    wh.nworkers = 1;
    wh.worker[0].ctl_sock = sv[0];
    char tag = NCLAND_CTL_TAG_RSP;
    ncland_rsp_msg_t rsp; memset(&rsp, 0, sizeof(rsp));
    rsp.seq = 55;
    snprintf(rsp.text, sizeof(rsp.text), "OK");
    write(sv[1], &tag, 1);
    write(sv[1], &rsp, sizeof(rsp));

    REQUIRE_EQ(warehouse_drain_worker_ctl_sock(&wh, 0), 0);

    /* DEALER should now have the response. */
    zmq_msg_t m; zmq_msg_init(&m);
    int n = zmq_msg_recv(&m, dealer, 0); zmq_msg_close(&m);  /* empty */
    zmq_msg_init(&m);
    n = zmq_msg_recv(&m, dealer, 0);
    REQUIRE_EQ((size_t)n, sizeof(rsp));
    ncland_rsp_msg_t out = *(ncland_rsp_msg_t*)zmq_msg_data(&m);
    zmq_msg_close(&m);
    REQUIRE_EQ(out.seq, 55);
    REQUIRE(strcmp(out.text, "OK") == 0);

    close(sv[0]); close(sv[1]);
    zmq_close(dealer); zmq_ctx_term(cctx);
    ncland_zmq_close(&wh);
    unlink("/tmp/ncland_test_t9_8.sock");
}
```

- [ ] **Step 2: Run; expect link error**

- [ ] **Step 3: Implement `warehouse_drain_worker_ctl_sock`**

Append to `ncland_warehouse.cpp`:

```cpp
/**
 * @brief Read framed messages from a single worker's ctl_sock until EAGAIN.
 *
 * R-tagged frames are forwarded to the original caller via ROUTER.
 */
int warehouse_drain_worker_ctl_sock(ncland_wh_t *wh, int worker_idx)
{
    if (!wh || worker_idx < 0 || worker_idx >= wh->nworkers) return -1;
    int sock = wh->worker[worker_idx].ctl_sock;
    if (sock < 0) return -1;

    for (;;) {
        char tag;
        ssize_t n = recv(sock, &tag, 1, MSG_DONTWAIT);
        if (n == 0)                   return 0;
        if (n < 0 && errno == EAGAIN) return 0;
        if (n != 1)                   return -1;

        switch (tag) {
        case NCLAND_CTL_TAG_RSP: {
            ncland_rsp_msg_t rsp;
            ssize_t got = 0;
            while (got < (ssize_t)sizeof(rsp)) {
                ssize_t r = recv(sock, ((char*)&rsp)+got, sizeof(rsp)-got, 0);
                if (r <= 0) return -1;
                got += r;
            }
            uint8_t id[256]; size_t id_len = sizeof(id);
            if (warehouse_pending_take(wh, rsp.seq, id, &id_len) == 0) {
                if (ncland_zmq_send_rsp(wh, id, id_len, &rsp) != 0)
                    LOG_WARN("zmq send_rsp failed for seq=%d", rsp.seq);
            } else {
                LOG_WARN("response for unknown seq=%d (caller gone?)", rsp.seq);
            }
            break;
        }
        default:
            LOG_WARN("warehouse_drain_worker_ctl_sock: unknown tag '%c'", tag);
            return -1;
        }
    }
}
```

In `warehouse_main_loop`, register each `worker[i].ctl_sock` in epoll (`EPOLLIN | EPOLLET`) and dispatch to `warehouse_drain_worker_ctl_sock(wh, i)`. Replace existing `mq_ctl` epoll registration with `wh->zmq_ctl_fd`; on `EPOLLIN` for that fd, call `ncland_zmq_drain(wh)`.

In `warehouse_init`, replace `ncland_mq_open_in(&wh->mq_ctl)` with `ncland_zmq_init(wh, cfg->ctl_endpoint)`. (Caller change in Task 12.)

In `warehouse_shutdown`, replace `mq_close`/`mq_unlink` with `ncland_zmq_close(wh)`.

- [ ] **Step 4: Run; expect pass**

Run: `./ncland_unit_tests -t 'T9.8' && ./ncland_unit_tests -s 9`
Expected: T9.x suite passes (T9.4 reap test still passes).

- [ ] **Step 5: Commit**

```bash
git add cnc/ncland/src/ncland_warehouse.cpp cnc/ncland/src/ncland_unit_tests.cpp
git commit -m "warehouse: replace mq_ctl with zmq_ctl_fd; forward worker rsps via ROUTER"
```

---

## Task 9: Delete dead MQ code in `ncland_proto.cpp` and unit-test cleanup

**Files:**
- Modify: `cnc/ncland/src/ncland_proto.cpp`
- Modify: `cnc/ncland/src/ncland_unit_tests.cpp`

- [ ] **Step 1: Delete `mq_open_helper`, `ncland_mq_open_in`, `ncland_mq_open_out`, `ncland_mq_close_all`, `ncland_mq_send_rsp`, `ncland_mq_recv_cmd` from `ncland_proto.cpp`**

Also remove `#include <mqueue.h>` and `#include <fcntl.h>` if only used by the removed helpers, and remove the `static_assert` on `mqd_t` size.

- [ ] **Step 2: Delete T3.1 / T3.2 / T3.3 / T3.4 MQ test blocks from `ncland_unit_tests.cpp`**

(They tested the removed helpers.)

- [ ] **Step 3: Build**

Run: `cd cnc/ncland/src && nmake -f ncland.mk ncland_unit_tests`
Expected: clean build.

- [ ] **Step 4: Static check — no MQ symbols remain**

Run: `grep -rn 'mq_open\|mq_send\|mq_receive\|<mqueue.h>\|mqd_t' cnc/ncland/src/*.cpp cnc/ncland/src/*.h`
Expected: empty output.

Run: `grep -rn '\-lrt' cnc/ncland/src/ncland.mk`
Expected: empty output.

- [ ] **Step 5: Run all existing tests**

Run: `./ncland_unit_tests -u`
Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add cnc/ncland/src/ncland_proto.cpp cnc/ncland/src/ncland_unit_tests.cpp
git commit -m "ncland: delete dead POSIX MQ code and tests"
```

---

## Task 10: Seed module — `ncland.json` parser

**Files:**
- Create: `cnc/ncland/src/ncland_seed.cpp`
- Create: `cnc/ncland/src/test/fixtures/ncland_good.json`
- Create: `cnc/ncland/src/test/fixtures/ncland_bad.json`
- Modify: `cnc/ncland/src/ncland_unit_tests.cpp`
- Modify: `cnc/ncland/src/ncland.mk` (add `ncland_seed.o`)

- [ ] **Step 1: Create fixtures**

`cnc/ncland/src/test/fixtures/ncland_good.json`:

```json
{
  "version": 1,
  "dtypes": [1, 7, 12, 23]
}
```

`cnc/ncland/src/test/fixtures/ncland_bad.json`:

```json
{
  "version": 1,
  "dtypes":
```

- [ ] **Step 2: Failing tests**

```cpp
TEST("seed", "T15.1 load_filter parses good JSON") {
    std::unordered_set<int> allow;
    int rc = ncland_seed_load_filter(
        "cnc/ncland/src/test/fixtures/ncland_good.json", &allow);
    REQUIRE_EQ(rc, 0);
    REQUIRE_EQ(allow.size(), 4u);
    REQUIRE(allow.count(1)  == 1);
    REQUIRE(allow.count(7)  == 1);
    REQUIRE(allow.count(12) == 1);
    REQUIRE(allow.count(23) == 1);
    REQUIRE(allow.count(99) == 0);
}

TEST("seed", "T15.2 missing file returns -1") {
    std::unordered_set<int> allow;
    REQUIRE_EQ(ncland_seed_load_filter("/no/such/path.json", &allow), -1);
}

TEST("seed", "T15.3 malformed JSON returns -1") {
    std::unordered_set<int> allow;
    REQUIRE_EQ(ncland_seed_load_filter(
        "cnc/ncland/src/test/fixtures/ncland_bad.json", &allow), -1);
}

TEST("seed", "T15.4 unknown version rejected") {
    /* Reuse good fixture but with version 999 — write inline tmp file. */
    char tmp[] = "/tmp/ncland_seed_v999_XXXXXX";
    int fd = mkstemp(tmp);
    REQUIRE_GT(fd, 0);
    const char *body = "{\"version\":999,\"dtypes\":[1]}";
    write(fd, body, strlen(body));
    close(fd);
    std::unordered_set<int> allow;
    REQUIRE_EQ(ncland_seed_load_filter(tmp, &allow), -1);
    unlink(tmp);
}
```

- [ ] **Step 3: Run; expect undefined**

Run: `nmake -f ncland.mk ncland_unit_tests`
Expected: link error.

- [ ] **Step 4: Implement `ncland_seed_load_filter`**

`cnc/ncland/src/ncland_seed.cpp`:

```cpp
// ncland_seed.cpp — startup NE-list seeding for ncland_warehouse

#include "ncland.h"
#include "nflog.hpp"

#include <cjson/cJSON.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#define NCLAND_JSON_VERSION 1

/**
 * @brief Read a file fully into a malloc'd buffer.
 */
static char *slurp(const char *path)
{
    FILE *f = fopen(path, "rb");
    if (!f) return nullptr;
    fseek(f, 0, SEEK_END);
    long sz = ftell(f);
    fseek(f, 0, SEEK_SET);
    if (sz < 0) { fclose(f); return nullptr; }
    char *buf = (char *)malloc((size_t)sz + 1);
    if (!buf) { fclose(f); return nullptr; }
    size_t got = fread(buf, 1, (size_t)sz, f);
    fclose(f);
    buf[got] = '\0';
    return buf;
}

/**
 * @brief Parse /usr/cnc/features/ncland.json into a dtype allowlist.
 */
int ncland_seed_load_filter(const char *path, std::unordered_set<int> *out)
{
    LOG_DEBUG("ncland_seed_load_filter: path=%s out=%p", path, (void *)out);
    if (!path || !out) return -1;

    char *buf = slurp(path);
    if (!buf) { LOG_ERROR("seed: cannot read %s", path); return -1; }

    cJSON *root = cJSON_Parse(buf);
    free(buf);
    if (!root) { LOG_ERROR("seed: malformed JSON in %s", path); return -1; }

    cJSON *ver = cJSON_GetObjectItemCaseSensitive(root, "version");
    if (!cJSON_IsNumber(ver) || ver->valueint != NCLAND_JSON_VERSION) {
        LOG_ERROR("seed: unsupported version in %s", path);
        cJSON_Delete(root);
        return -1;
    }

    cJSON *arr = cJSON_GetObjectItemCaseSensitive(root, "dtypes");
    if (!cJSON_IsArray(arr)) {
        LOG_ERROR("seed: dtypes is not an array in %s", path);
        cJSON_Delete(root);
        return -1;
    }

    out->clear();
    cJSON *e;
    cJSON_ArrayForEach(e, arr) {
        if (!cJSON_IsNumber(e)) {
            LOG_ERROR("seed: non-integer dtype in %s", path);
            cJSON_Delete(root);
            return -1;
        }
        out->insert(e->valueint);
    }
    cJSON_Delete(root);
    LOG_INFO("seed: loaded %zu dtypes from %s", out->size(), path);
    return 0;
}
```

Add `ncland_seed.o` to `NCLAND_OBJS` in `ncland.mk`.

- [ ] **Step 5: Run; expect 4 pass**

Run: `./ncland_unit_tests -t 'T15.1' -t 'T15.2' -t 'T15.3' -t 'T15.4'`
Expected: 4 passed.

- [ ] **Step 6: Commit**

```bash
git add cnc/ncland/src/ncland_seed.cpp cnc/ncland/src/test/ cnc/ncland/src/ncland.mk cnc/ncland/src/ncland_unit_tests.cpp
git commit -m "ncland_seed: add ncland.json filter loader"
```

---

## Task 11: Seed module — `frame_link` iteration

**Why:** This is the runtime equivalent of m_clan's main loop. Walks `frame_link` + aal shm, applies `dtype_allow + LocalDomainName + isIPaddrSim` filters, and calls `warehouse_open_conn(neid, slot)` per match.

**Files:**
- Modify: `cnc/ncland/src/ncland_seed.cpp`
- Modify: `cnc/ncland/src/ncland_unit_tests.cpp`

- [ ] **Step 1: Failing test using a mock frame_link iterator**

The real iterator uses `atch_frm()` + `atch_aal()` from libsdi. For unit tests, expose a seam: `int ncland_seed_for_each_ne(void (*cb)(ncland_seed_ne_t*, void*), void *u);` whose default implementation walks the real frame_link, but which the test can replace via an `extern "C" __attribute__((weak))` override.

```cpp
struct ncland_seed_ne_t {
    int  neid;
    int  slot;
    int  dtype;
    char ip[64];
};

TEST("seed", "T15.5 seed_run opens only NEs whose dtype is in allowlist") {
    ncland_wh_t wh; memset(&wh, 0, sizeof(wh));
    wh.dtype_allow = {7, 12};

    /* Inject a 4-NE mock list via test seam. */
    extern std::vector<ncland_seed_ne_t> g_test_seed_nes;
    g_test_seed_nes = {
        {1001, 1,  7,  "10.0.0.1"},
        {1002, 1,  3,  "10.0.0.2"},   /* dtype 3 not in allowlist */
        {1003, 1, 12,  "10.0.0.3"},
        {1004, 1, 99,  "10.0.0.4"}
    };

    extern int g_test_seed_open_count;
    g_test_seed_open_count = 0;
    int rc = ncland_seed_run(&wh);
    REQUIRE_EQ(rc, 0);
    REQUIRE_EQ(g_test_seed_open_count, 2);  /* 1001 and 1003 */
}

TEST("seed", "T15.6 idempotent re-run no-ops on already-open neids") {
    ncland_wh_t wh; memset(&wh, 0, sizeof(wh));
    wh.dtype_allow = {7};
    extern std::vector<ncland_seed_ne_t> g_test_seed_nes;
    g_test_seed_nes = { {1001, 1, 7, "10.0.0.1"} };

    extern int g_test_seed_open_count;
    g_test_seed_open_count = 0;
    REQUIRE_EQ(ncland_seed_run(&wh), 0);
    REQUIRE_EQ(g_test_seed_open_count, 1);
    REQUIRE_EQ(ncland_seed_run(&wh), 0);
    REQUIRE_EQ(g_test_seed_open_count, 1);  /* second pass is idempotent */
}
```

- [ ] **Step 2: Run; expect undefined**

- [ ] **Step 3: Implement `ncland_seed_run` with test seam**

Append to `ncland_seed.cpp`:

```cpp
#include <vector>

#ifdef NCLAND_TEST
std::vector<ncland_seed_ne_t> g_test_seed_nes;
int                           g_test_seed_open_count;
#endif

/**
 * @brief Iterate every NE in frame_link/aal shm, calling cb per entry.
 *
 * Default implementation uses libsdi (atch_frm + atch_aal). Tests
 * override via a weak symbol that pulls from g_test_seed_nes.
 */
__attribute__((weak))
void ncland_seed_for_each_ne(void (*cb)(const ncland_seed_ne_t*, void*), void *u)
{
#ifdef NCLAND_TEST
    for (const auto &ne : g_test_seed_nes) cb(&ne, u);
#else
    extern int NumCLAN;                              /* defined in libsdi */
    extern struct frame_link *Frmlnk;
    /* Mirror m_clan: walk Frmlnk, then per-slot read aal record. */
    for (int neid = 1; neid <= NumCLAN; neid++) {
        struct frame_link *f = &Frmlnk[neid];
        for (int slot_idx = 0; slot_idx < MAX_CLAN_PER_NE; slot_idx++) {
            int slot = ((int*)&f->link_slot_1)[slot_idx];   /* same slot ordering as m_clan */
            if (slot <= 0) continue;
            aal_record_t r;
            if (read_aal(neid, slot, &r) != 0) continue;
            ncland_seed_ne_t ne = { neid, slot, r.dtype, {0} };
            strncpy(ne.ip, r.ipAddr, sizeof(ne.ip)-1);
            cb(&ne, u);
        }
    }
#endif
}

static int seed_find_existing(ncland_wh_t *wh, int neid)
{
    for (int i = 0; i < MAX_CONNS; i++) {
        if (wh->conn[i].state != CS_IDLE && wh->conn[i].neid == neid)
            return i;
    }
    return -1;
}

static void seed_cb(const ncland_seed_ne_t *ne, void *u)
{
    ncland_wh_t *wh = (ncland_wh_t *)u;
    if (wh->dtype_allow.count(ne->dtype) == 0) return;

    /* Idempotent: skip if a live conn for this neid already exists. */
    if (seed_find_existing(wh, ne->neid) >= 0) return;

#ifndef NCLAND_TEST
    extern char LocalDomainName[];
    extern const char *LrsDomain;
    extern int  CheckValidIP;
    extern "C" int isIPaddrSim(const char *);

    if (LrsDomain && strcmp(LocalDomainName, LrsDomain) == 0
        && CheckValidIP && !isIPaddrSim(ne->ip))
        return;
#endif

    int conn_id = warehouse_open_conn_by_ne(wh, ne->neid, ne->slot, ne->dtype, ne->ip);
    if (conn_id < 0) {
        LOG_WARN("seed: failed to open neid=%d slot=%d (continuing)", ne->neid, ne->slot);
        return;
    }
#ifdef NCLAND_TEST
    g_test_seed_open_count++;
#else
    warehouse_assign_conn(wh, conn_id);
#endif
}

int ncland_seed_run(ncland_wh_t *wh)
{
    if (!wh) return -1;
    LOG_INFO("seed: starting iteration (allowlist size=%zu)", wh->dtype_allow.size());
    ncland_seed_for_each_ne(seed_cb, wh);
    LOG_INFO("seed: complete (%d conns)", wh->nconns);
    return 0;
}
```

Add a stub `warehouse_open_conn_by_ne` that returns 0 in test mode and calls into `ncland_ssh_connect`/`conn_alloc` in the real build. Real implementation lives in `ncland_warehouse.cpp`:

```cpp
int warehouse_open_conn_by_ne(ncland_wh_t *wh, int neid, int slot,
                              int dtype, const char *ip)
{
    int id = conn_alloc(wh);
    if (id < 0) return -1;
    conn_t *c = &wh->conn[id];
    c->neid  = neid;
    c->slot  = slot;
    c->dtype = dtype;
    strncpy(c->ipAddr, ip, sizeof(c->ipAddr)-1);
#ifdef NCLAND_TEST
    /* Tests don't have a real NE — just mark the slot used so the
       seed_find_existing idempotency check works. */
    c->state = CS_READY;
    return id;
#else
    if (ncland_ssh_connect(c) != 0) {  /* or telnet, by dtype rule */
        conn_free(wh, id);
        return -1;
    }
    return id;
#endif
}
```

Add `int neid;` and `int slot;` to `conn_t` if not already present.

- [ ] **Step 4: Run; expect 2 pass**

Run: `./ncland_unit_tests -t 'T15.5' -t 'T15.6'`
Expected: 2 passed.

- [ ] **Step 5: Commit**

```bash
git add cnc/ncland/src/ncland_seed.cpp cnc/ncland/src/ncland_warehouse.cpp cnc/ncland/src/ncland.h cnc/ncland/src/ncland_unit_tests.cpp
git commit -m "ncland_seed: add frame_link iterator with test seam"
```

---

## Task 12: Wire seed into `warehouse_init` + new CLI flags

**Files:**
- Modify: `cnc/ncland/src/ncland.cpp`
- Modify: `cnc/ncland/src/ncland_warehouse.cpp`
- Modify: `cnc/ncland/src/ncland_unit_tests.cpp`

- [ ] **Step 1: Add new CLI flags**

In `ncland.cpp::config_defaults`:

```cpp
strncpy(cfg->ctl_endpoint, "ipc:///var/run/ncland/ctl.sock",
        sizeof(cfg->ctl_endpoint) - 1);
strncpy(cfg->ncland_json_path, "/usr/cnc/features/ncland.json",
        sizeof(cfg->ncland_json_path) - 1);
```

In `ncland_parse_args`, replace the `q:Q:` getopt with `c:f:`:

```cpp
while ((opt = getopt(argc, argv, "w:n:c:f:l:")) != -1) {
    switch (opt) {
    case 'w': /* unchanged */ break;
    case 'n': /* unchanged */ break;
    case 'c':
        strncpy(cfg->ctl_endpoint, optarg, sizeof(cfg->ctl_endpoint) - 1);
        break;
    case 'f':
        strncpy(cfg->ncland_json_path, optarg, sizeof(cfg->ncland_json_path) - 1);
        break;
    case 'l': /* unchanged */ break;
    default:  return -1;
    }
}
```

Update usage string accordingly.

- [ ] **Step 2: Wire seed + zmq init into warehouse**

In `warehouse_init(ncland_wh_t *wh, const ncland_config_t *cfg)` (signature changes — pass cfg through):

```cpp
int warehouse_init(ncland_wh_t *wh, const ncland_config_t *cfg)
{
    if (!wh || !cfg || cfg->nworkers < 1 || cfg->nworkers > MAX_WORKERS)
        return -1;
    memset(wh, 0, sizeof(*wh));
    wh->nworkers = cfg->nworkers;
    wh->zmq_ctl_fd = -1;
    wh->notify_fd  = -1;
    for (int i = 0; i < MAX_CONNS;   i++) conn_init(&wh->conn[i], i);
    for (int i = 0; i < MAX_WORKERS; i++) {
        wh->worker[i].ctl_sock = -1;
        wh->worker[i].pid      = 0;
        wh->worker[i].nconns   = 0;
    }
    if (ncland_seed_load_filter(cfg->ncland_json_path, &wh->dtype_allow) != 0)
        return -1;
    if (ncland_zmq_init(wh, cfg->ctl_endpoint) != 0) return -1;
    if (ncland_seed_run(wh) != 0) {
        ncland_zmq_close(wh);
        return -1;
    }
    wh->running = 1;
    return 0;
}
```

In `ncland.cpp::main`, replace `wh.nworkers = cfg.nworkers;` + bare `ncland_warehouse_run(&wh)` with:

```cpp
ncland_wh_t wh;
if (warehouse_init(&wh, &cfg) != 0) return 1;
return ncland_warehouse_run(&wh);
```

- [ ] **Step 3: Update existing `T9.1 warehouse_init` test**

The test now needs a `ncland_config_t` and a fixtures path. Update:

```cpp
TEST("warehouse", "T9.1 warehouse_init succeeds with valid config") {
    ncland_config_t cfg; memset(&cfg, 0, sizeof(cfg));
    cfg.nworkers = 1;
    strncpy(cfg.ctl_endpoint, "ipc:///tmp/ncland_t9_1.sock",
            sizeof(cfg.ctl_endpoint)-1);
    strncpy(cfg.ncland_json_path,
            "cnc/ncland/src/test/fixtures/ncland_good.json",
            sizeof(cfg.ncland_json_path)-1);
    ncland_wh_t wh;
    REQUIRE_EQ(warehouse_init(&wh, &cfg), 0);
    REQUIRE_GT(wh.zmq_ctl_fd, 0);
    REQUIRE_EQ(wh.dtype_allow.size(), 4u);
    warehouse_shutdown(&wh);
    unlink("/tmp/ncland_t9_1.sock");
}
```

Update the existing `T12.x` parse-args tests to use `-c` / `-f` instead of `-q` / `-Q`.

- [ ] **Step 4: Run; expect pass**

Run: `./ncland_unit_tests -s 9 -s 12`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add cnc/ncland/src/ncland.cpp cnc/ncland/src/ncland_warehouse.cpp cnc/ncland/src/ncland_unit_tests.cpp
git commit -m "ncland: wire ncland.json seed into warehouse_init; new CLI flags -c/-f"
```

---

## Task 13: Notify module — librdb_notify subscribe + drain skeleton

**Files:**
- Create: `cnc/ncland/src/ncland_notify.cpp`
- Modify: `cnc/ncland/src/ncland.mk` (add `ncland_notify.o`, `-lrdb_notify`)
- Modify: `cnc/ncland/src/ncland_unit_tests.cpp`

> Open question 1 (spec §12): the exact `rdb_notify_type` enum values for the six events. Read `/home/dan/Git/netflex/cnc/rdb/include/rdb_notify.h` (or wherever the enum lives) and pick the right type IDs for `NE_CREATE`, `NE_DELETE`, `NE_ENABLE`, `NE_DISABLE`, `NE_DTYPE_CHANGE`, `NE_IP_CHANGE`. Update the dispatch table in this task accordingly.

- [ ] **Step 1: Failing test for `ncland_notify_init` returning a usable fd**

```cpp
TEST("notify", "T16.1 init returns a valid fd or graceful error") {
    ncland_wh_t wh; memset(&wh, 0, sizeof(wh));
    int rc = ncland_notify_init(&wh);
    if (rc == 0) {
        REQUIRE_GT(wh.notify_fd, 0);
        ncland_notify_close(&wh);
    } else {
        /* No DB available in this CI lane — accepted. */
        SKIP("no rdb available");
    }
}
```

- [ ] **Step 2: Run; expect undefined**

- [ ] **Step 3: Implement init / close**

`cnc/ncland/src/ncland_notify.cpp`:

```cpp
// ncland_notify.cpp — subscribe to otn_portd PG NOTIFY events

#include "ncland.h"
#include "nflog.hpp"

#include <rdb_notify.h>
#include <errno.h>
#include <string.h>

/**
 * @brief Connect to RDB and subscribe to NE-lifecycle notify channels.
 *
 * Channels subscribed: NE_CREATE / NE_DELETE / NE_ENABLE / NE_DISABLE /
 * NE_DTYPE_CHANGE / NE_IP_CHANGE (real enum values resolved from
 * rdb_notify.h — see open question §12 of design spec).
 *
 * @return 0 + sets wh->notify_fd on success, -1 on error.
 */
int ncland_notify_init(ncland_wh_t *wh)
{
    if (!wh) return -1;
    int fd = rdb_notify_subscribe_fd();   /* assumed API; adjust to real signature */
    if (fd < 0) {
        LOG_WARN("rdb_notify_subscribe_fd failed: %s", strerror(errno));
        return -1;
    }

    static const int channels[] = {
        RDB_NOTIFY_NE_CREATE, RDB_NOTIFY_NE_DELETE,
        RDB_NOTIFY_NE_ENABLE, RDB_NOTIFY_NE_DISABLE,
        RDB_NOTIFY_NE_DTYPE_CHANGE, RDB_NOTIFY_NE_IP_CHANGE,
    };
    for (size_t i = 0; i < sizeof(channels)/sizeof(channels[0]); i++) {
        if (rdb_notify_listen(fd, channels[i]) != 0) {
            LOG_ERROR("rdb_notify_listen(%d) failed", channels[i]);
            rdb_notify_close(fd);
            return -1;
        }
    }
    wh->notify_fd = fd;
    LOG_INFO("notify: subscribed to 6 NE channels (fd=%d)", fd);
    return 0;
}

void ncland_notify_close(ncland_wh_t *wh)
{
    if (wh && wh->notify_fd >= 0) {
        rdb_notify_close(wh->notify_fd);
        wh->notify_fd = -1;
    }
}
```

Add `-lrdb_notify` to the link rules (or include it transitively if `$(RDB_LIBS)` already covers it; verify via `grep RDB_LIBS cnc/ncland/src/ncland.mo` if unsure).

- [ ] **Step 4: Build and run**

Run: `nmake -f ncland.mk ncland_unit_tests && ./ncland_unit_tests -t 'T16.1'`
Expected: pass or graceful skip if no DB.

- [ ] **Step 5: Commit**

```bash
git add cnc/ncland/src/ncland_notify.cpp cnc/ncland/src/ncland.mk cnc/ncland/src/ncland_unit_tests.cpp
git commit -m "ncland_notify: add rdb_notify subscriber skeleton"
```

---

## Task 14: Notify module — event dispatch table

**Files:**
- Modify: `cnc/ncland/src/ncland_notify.cpp`
- Modify: `cnc/ncland/src/ncland_unit_tests.cpp`

- [ ] **Step 1: Failing tests — one per event**

```cpp
TEST("notify", "T16.2 NE_CREATE dispatches warehouse_open_conn_by_ne when dtype in allowlist") {
    ncland_wh_t wh; memset(&wh, 0, sizeof(wh));
    wh.dtype_allow = {7};
    extern int g_test_open_calls;
    g_test_open_calls = 0;
    ncland_notify_event_t e = {NCLAND_EVT_NE_CREATE, /*neid*/100, /*slot*/1, /*dtype*/7, "10.0.0.7"};
    ncland_notify_dispatch(&wh, &e);
    REQUIRE_EQ(g_test_open_calls, 1);
}

TEST("notify", "T16.3 NE_CREATE skipped when dtype not in allowlist") {
    ncland_wh_t wh; memset(&wh, 0, sizeof(wh));
    wh.dtype_allow = {7};
    extern int g_test_open_calls;
    g_test_open_calls = 0;
    ncland_notify_event_t e = {NCLAND_EVT_NE_CREATE, 100, 1, /*dtype*/3, "10.0.0.7"};
    ncland_notify_dispatch(&wh, &e);
    REQUIRE_EQ(g_test_open_calls, 0);
}

TEST("notify", "T16.4 NE_DELETE dispatches warehouse_close_conn") {
    ncland_wh_t wh; memset(&wh, 0, sizeof(wh));
    /* preload one conn for neid=100 */
    wh.conn[0].state = CS_READY;
    wh.conn[0].neid  = 100;
    extern int g_test_close_calls;
    g_test_close_calls = 0;
    ncland_notify_event_t e = {NCLAND_EVT_NE_DELETE, 100, 0, 0, ""};
    ncland_notify_dispatch(&wh, &e);
    REQUIRE_EQ(g_test_close_calls, 1);
}

TEST("notify", "T16.5 NE_IP_CHANGE = close + open") {
    ncland_wh_t wh; memset(&wh, 0, sizeof(wh));
    wh.dtype_allow = {7};
    wh.conn[0].state = CS_READY; wh.conn[0].neid = 100; wh.conn[0].dtype = 7;
    extern int g_test_open_calls, g_test_close_calls;
    g_test_open_calls = g_test_close_calls = 0;
    ncland_notify_event_t e = {NCLAND_EVT_NE_IP_CHANGE, 100, 1, 7, "10.0.0.99"};
    ncland_notify_dispatch(&wh, &e);
    REQUIRE_EQ(g_test_close_calls, 1);
    REQUIRE_EQ(g_test_open_calls,  1);
}

TEST("notify", "T16.6 unknown event dropped, no crash") {
    ncland_wh_t wh; memset(&wh, 0, sizeof(wh));
    ncland_notify_event_t e = {(ncland_notify_evt_kind_t)9999, 0, 0, 0, ""};
    REQUIRE_NOTHROW(ncland_notify_dispatch(&wh, &e));
}
```

Add to `ncland.h`:

```cpp
typedef enum {
    NCLAND_EVT_NE_CREATE = 1,
    NCLAND_EVT_NE_DELETE,
    NCLAND_EVT_NE_ENABLE,
    NCLAND_EVT_NE_DISABLE,
    NCLAND_EVT_NE_DTYPE_CHANGE,
    NCLAND_EVT_NE_IP_CHANGE
} ncland_notify_evt_kind_t;

typedef struct {
    ncland_notify_evt_kind_t kind;
    int  neid;
    int  slot;
    int  dtype;     /* new dtype for DTYPE_CHANGE; 0 otherwise */
    char ip[64];    /* new ip for IP_CHANGE; empty otherwise */
} ncland_notify_event_t;

void ncland_notify_dispatch(ncland_wh_t *wh, const ncland_notify_event_t *e);
```

- [ ] **Step 2: Run; expect undefined**

- [ ] **Step 3: Implement dispatch**

Append to `ncland_notify.cpp`:

```cpp
#ifdef NCLAND_TEST
int g_test_open_calls  = 0;
int g_test_close_calls = 0;
#endif

static int find_conn_by_neid(ncland_wh_t *wh, int neid)
{
    for (int i = 0; i < MAX_CONNS; i++) {
        if (wh->conn[i].state != CS_IDLE && wh->conn[i].neid == neid)
            return i;
    }
    return -1;
}

static int try_open(ncland_wh_t *wh, int neid, int slot, int dtype, const char *ip)
{
    if (wh->dtype_allow.count(dtype) == 0) return -1;
    if (find_conn_by_neid(wh, neid) >= 0) return 0;   /* already open: idempotent */
#ifdef NCLAND_TEST
    g_test_open_calls++;
    return 0;
#else
    int id = warehouse_open_conn_by_ne(wh, neid, slot, dtype, ip);
    if (id < 0) return -1;
    warehouse_assign_conn(wh, id);
    return 0;
#endif
}

static int try_close(ncland_wh_t *wh, int neid)
{
    int id = find_conn_by_neid(wh, neid);
    if (id < 0) return 0;
#ifdef NCLAND_TEST
    g_test_close_calls++;
    wh->conn[id].state = CS_IDLE;
    return 0;
#else
    warehouse_close_conn(wh, id);
    return 0;
#endif
}

void ncland_notify_dispatch(ncland_wh_t *wh, const ncland_notify_event_t *e)
{
    if (!wh || !e) return;
    switch (e->kind) {
    case NCLAND_EVT_NE_CREATE:
    case NCLAND_EVT_NE_ENABLE:
        try_open(wh, e->neid, e->slot, e->dtype, e->ip);
        break;
    case NCLAND_EVT_NE_DELETE:
    case NCLAND_EVT_NE_DISABLE:
        try_close(wh, e->neid);
        break;
    case NCLAND_EVT_NE_DTYPE_CHANGE: {
        int id = find_conn_by_neid(wh, e->neid);
        bool was_open = (id >= 0);
        bool now_allowed = wh->dtype_allow.count(e->dtype) > 0;
        if (was_open && !now_allowed)         try_close(wh, e->neid);
        else if (!was_open && now_allowed)    try_open(wh, e->neid, e->slot, e->dtype, e->ip);
        break;
    }
    case NCLAND_EVT_NE_IP_CHANGE:
        try_close(wh, e->neid);
        try_open (wh, e->neid, e->slot, e->dtype, e->ip);
        break;
    default:
        LOG_WARN("notify: unknown event kind=%d", (int)e->kind);
        break;
    }
}

/**
 * @brief Drain notify_fd until empty.
 *
 * Each pending notify is parsed into an ncland_notify_event_t and
 * dispatched. Implementation depends on librdb_notify API — see open
 * question §12. Pseudo-code:
 *
 *    while (rdb_notify_poll(wh->notify_fd, &raw) == 0) {
 *        ncland_notify_event_t e;
 *        if (parse_raw(&raw, &e) == 0) ncland_notify_dispatch(wh, &e);
 *    }
 */
int ncland_notify_drain(ncland_wh_t *wh)
{
    if (!wh || wh->notify_fd < 0) return -1;
    /* TODO: replace with real librdb_notify polling once enum mapping is confirmed */
    return 0;
}
```

- [ ] **Step 4: Run; expect 5 pass**

Run: `./ncland_unit_tests -t 'T16.2' -t 'T16.3' -t 'T16.4' -t 'T16.5' -t 'T16.6'`
Expected: 5 passed.

- [ ] **Step 5: Commit**

```bash
git add cnc/ncland/src/ncland_notify.cpp cnc/ncland/src/ncland.h cnc/ncland/src/ncland_unit_tests.cpp
git commit -m "ncland_notify: add event dispatch table for 6 NE lifecycle kinds"
```

---

## Task 15: Wire notify into `warehouse_init` + epoll loop

**Files:**
- Modify: `cnc/ncland/src/ncland_warehouse.cpp`

- [ ] **Step 1: Add notify init to `warehouse_init`**

After `ncland_zmq_init` succeeds and before `ncland_seed_run`, add:

```cpp
if (ncland_notify_init(wh) != 0)
    LOG_WARN("warehouse_init: notify subscribe failed; runtime events disabled");
```

(Non-fatal — warehouse can still serve seed-loaded NEs and incoming ZMQ commands. Reconnect logic added in a follow-up if/when the failure surfaces in real ops.)

- [ ] **Step 2: Register notify_fd in epoll**

In `warehouse_main_loop`, where `zmq_ctl_fd` is registered:

```cpp
if (wh->notify_fd >= 0) {
    struct epoll_event ev;
    ev.events  = EPOLLIN;
    ev.data.fd = wh->notify_fd;
    epoll_ctl(epfd, EPOLL_CTL_ADD, wh->notify_fd, &ev);
}
```

In the dispatch switch, add:

```cpp
} else if (wh->notify_fd >= 0 && fd == wh->notify_fd) {
    ncland_notify_drain(wh);
}
```

In `warehouse_shutdown`, add `ncland_notify_close(wh);` before `ncland_zmq_close`.

- [ ] **Step 3: Build clean**

Run: `nmake -f ncland.mk ncland`
Expected: clean build.

- [ ] **Step 4: Run all unit tests**

Run: `./ncland_unit_tests -u`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add cnc/ncland/src/ncland_warehouse.cpp
git commit -m "warehouse: subscribe to notify_fd in main loop"
```

---

## Task 16: Modify `m_clan` to skip allowlisted dtypes

**Files:**
- Modify: `cnc/sdi/src/m_clan.c`
- Modify: `cnc/sdi/src/sdisrc.mk` (if `-ljson` not already linked)

- [ ] **Step 1: Add allowlist loader at startup**

After the existing `set_tunables();` call near line 119, add:

```c
/* Load /usr/cnc/features/ncland.json into a static int[] dtype allowlist. */
static int  NclandAllow[64];
static int  NclandAllowCount = 0;

static void load_ncland_allowlist(void)
{
    FILE *f = fopen("/usr/cnc/features/ncland.json", "rb");
    if (!f) {
        TRACE(D1, "%s: ncland.json not present; m_clan will own all NEs\n", __func__);
        return;
    }
    fseek(f, 0, SEEK_END); long sz = ftell(f); fseek(f, 0, SEEK_SET);
    if (sz <= 0) { fclose(f); return; }
    char *buf = malloc((size_t)sz + 1);
    if (!buf) { fclose(f); return; }
    fread(buf, 1, (size_t)sz, f); buf[sz] = '\0'; fclose(f);

    cJSON *root = cJSON_Parse(buf);
    free(buf);
    if (!root) {
        TRACE(FL, "%s: ncland.json malformed; aborting\n", __func__);
        exit(EXIT_FAILURE);
    }
    cJSON *arr = cJSON_GetObjectItemCaseSensitive(root, "dtypes");
    if (!cJSON_IsArray(arr)) {
        TRACE(FL, "%s: ncland.json missing dtypes array\n", __func__);
        cJSON_Delete(root);
        exit(EXIT_FAILURE);
    }
    cJSON *e;
    cJSON_ArrayForEach(e, arr) {
        if (!cJSON_IsNumber(e)) continue;
        if (NclandAllowCount >= (int)(sizeof(NclandAllow)/sizeof(NclandAllow[0]))) break;
        NclandAllow[NclandAllowCount++] = e->valueint;
    }
    cJSON_Delete(root);
    TRACE(D0, "%s: loaded %d ncland-owned dtypes\n", __func__, NclandAllowCount);
}

static int dtype_owned_by_ncland(int dtype)
{
    int i;
    for (i = 0; i < NclandAllowCount; i++)
        if (NclandAllow[i] == dtype) return 1;
    return 0;
}
```

Add `#include <cjson/cJSON.h>` at top of file.

Call `load_ncland_allowlist();` once after `set_tunables();`.

- [ ] **Step 2: Skip allowlisted dtypes in the main loop**

In `m_clan.c`, every place that reads an aal record `r` and checks `LocalDomainName / CheckValidIP` (lines around 332, 364, 397, 430, 463, 496 — the per-slot blocks), add immediately after the existing domain/IP guard but before the spawn:

```c
if (dtype_owned_by_ncland(r.dtype)) {
    TRACE(D7, "%s: NE [%d.%d] dtype %d owned by ncland; skipping clan spawn\n",
          __func__, loop, frmlnk->link_slot_X, r.dtype);
    continue;
}
```

(Replace `link_slot_X` with the matching slot field per block — the existing code has six near-identical blocks, one per slot.)

- [ ] **Step 3: Verify it builds**

Run: `cd cnc/sdi/src && nmake -f sdisrc.mk ../../../3b2/bin/m_clan`
Expected: clean build. If `cJSON.h` not found, add `-ljson` to the relevant link rule (already in `INCLIBS` per the ncland.mk pattern).

- [ ] **Step 4: Smoke test (manual, in lab)**

Place a sample `/usr/cnc/features/ncland.json` with a single dtype that exists in `aal`. Start `m_clan`. Trace output should show "owned by ncland; skipping" for matching slots, normal spawn for everything else.

- [ ] **Step 5: Commit**

```bash
git add cnc/sdi/src/m_clan.c cnc/sdi/src/sdisrc.mk
git commit -m "m_clan: skip NEs whose dtype is owned by ncland"
```

---

## Task 17: Final integration test (manual + smoke automation)

**Files:**
- Create: `cnc/ncland/src/test/integration/it_smoke.sh`

- [ ] **Step 1: Write smoke script**

```bash
#!/bin/bash
# Integration smoke for ncland warehouse: bring up, send a CMD via DEALER,
# verify response.
set -euo pipefail

WH_PID=
cleanup() { [ -n "$WH_PID" ] && kill -TERM "$WH_PID" 2>/dev/null || true; rm -f /tmp/ncland_smoke.sock; }
trap cleanup EXIT

cat >/tmp/ncland_smoke.json <<EOF
{"version":1,"dtypes":[$DTYPE_FOR_SMOKE]}
EOF

/usr/cnc/bin/ncland \
  -w 1 -n 4 \
  -c ipc:///tmp/ncland_smoke.sock \
  -f /tmp/ncland_smoke.json &
WH_PID=$!
sleep 1

python3 - <<'PY'
import zmq, struct, sys, time
ctx = zmq.Context()
d   = ctx.socket(zmq.DEALER)
d.setsockopt(zmq.IDENTITY, b"smoke")
d.connect("ipc:///tmp/ncland_smoke.sock")
# Build a minimal cmd_msg — adjust struct layout to match ncland_cmd_msg_t
# (kept opaque here; in real test, link to a small C helper that fills it).
print("smoke: connected", flush=True)
PY

echo "smoke: ok"
```

- [ ] **Step 2: Run static checks**

```bash
grep -rn 'mq_open\|mq_send\|mq_receive\|<mqueue.h>\|mqd_t' \
    cnc/ncland/src/*.cpp cnc/ncland/src/*.h
```
Expected: empty.

```bash
grep -rn '\-lrt' cnc/ncland/src/ncland.mk
```
Expected: empty.

```bash
grep -rn 'SIGALRM\|alarm(' cnc/ncland/src/*.cpp
```
Expected: empty.

- [ ] **Step 3: Full test suite**

Run: `cd cnc/ncland/src && nmake -f ncland.mk ncland_unit_tests && ./ncland_unit_tests -u`
Expected: every test passes.

- [ ] **Step 4: Commit**

```bash
git add cnc/ncland/src/test/integration/it_smoke.sh
git commit -m "ncland: add integration smoke harness + static check enforcement"
```

---

## Open Items (defer or follow-up)

These mirror the spec §12 list. Resolve before / during implementation:

1. **Real `rdb_notify` API names** — `rdb_notify_subscribe_fd`, `rdb_notify_listen`, `rdb_notify_poll`, `rdb_notify_close` are placeholders. Read `cnc/rdb/include/rdb_notify.h` and adjust Task 13 / 14 to the real signatures. The PG channel-name strings published by `otn_portd` for the six events also need confirming — grep `otn_portd.c` for `NOTIFY` / `pg_notify` / `rdb_notify_send` calls and map them to `NCLAND_EVT_*`.
2. **Concurrent `atch_aal` / `atch_frm` access** — Task 11's real iterator assumes the warehouse can attach to the same shm m_clan attaches to. Confirm with the SDI maintainer; if not safe, add a small notification helper that proxies through m_clan or use the RDB directly.
3. **`/usr/cnc/features/ncland.json` deployment** — confirm it's packaged in the ncland RPM, owned by `root:root`, mode `0644`. If absent on first install, the warehouse currently exits fatally; supervisor restart loop should be benign once the file appears.
4. **`-lrdb_notify` vs existing `$(RDB_LIBS)`** — `cnc/ncland/src/ncland.mo` shows `RDB_LIBS = -lrdb -lpq`. If the notify symbols ship in `librdb`, no link change needed. Otherwise add `-lrdb_notify` explicitly.
5. **`isIPaddrSim` linkage** — symbol comes from libsdi. Verify `INCLIBS` already includes `-lsdi` (it does per `ncland.mo`).
