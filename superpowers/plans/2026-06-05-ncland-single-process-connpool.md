# ncland Single-Process + Connect Thread-Pool Restructure

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Collapse ncland's warehouse + worker-*process* model into a **single process** that uses a **connect thread-pool** for the blocking TCP/SSH establish and a **single epoll loop** to drive all established sessions — so libssh `ssh_session`s stay in the one process (no cross-process handoff) and a slow/dead NE never stalls the loop.

**Architecture:** One process. The main thread runs the epoll loop watching the ZMQ ROUTER (clients), the nfdb SUB (events), signalfd, a connect-completion eventfd, and every established NE `net_fd`. On an open event, the main thread allocates a `conn` (`CS_CONNECTING`), applies registry config, fetches es64 creds, and **enqueues the conn to a pthread connect-pool**; a pool thread runs `ncland_ssh_connect`/`ncland_telnet_connect` (blocking, but off the main loop) and posts the result back via a completion queue + eventfd. The main loop registers the established `net_fd` in epoll and drives I/O in-process (the former worker logic). **Deleted:** worker processes, `socketpair`/`ctl_sock`, `SCM_RIGHTS`/`warehouse_pass_fd`/`warehouse_assign_conn`, `worker_drain_ctl_sock`, `worker_main_loop`/`ncland_worker_run` (process side). **Reused:** `conn_t`, the registry (#1), B#4 config-apply, nfdb eventing/dispatch, `nclan-seed`, `ncland_ssh_connect`/`ncland_telnet_connect`, and the worker I/O functions (`handle_ne_data`, `dispatch_cmd`, `ne_read`/`ne_write`, `check_keepalives`/`check_dirty_timers`/`check_expired_cmds`) — adapted to run in the main process.

**Tech Stack:** C++17, pthreads, libssh, libzmq, epoll/eventfd/signalfd, yaml-cpp, `nfunit-test.hpp`, Lucent/AT&T nmake. Build `nmake -f ncland.mk <target>`. Spec: `~/docs/superpowers/specs/2026-05-27-clan-yaml-design.md` (§4.1 process model is superseded by this single-process design — see memory `project_ncland_ssh_fd_handoff`).

---

## Why this shape (rationale, do not relitigate)
libssh `ssh_session`/`ssh_channel` is process-local heap (keys/cipher/channel); `SCM_RIGHTS` passes only the raw fd, useless to another process for SSH. So the connecting process must be the I/O process → no worker processes. Blocking connect is solved with a **thread-pool** (parallel establish off the main loop), not async-libssh. Established sessions are driven by **one epoll loop** in the same process. (Matches the approach a colleague used for the same problem.)

## Concurrency model (keep the threaded surface tiny)
- **Main thread owns:** the `conn` table (alloc/free), the registry, ZMQ sockets, the epoll set, all I/O on established conns, the es64 DB.
- **Connect threads own:** *only* the single `conn` slot handed to them (state `CS_CONNECTING`), libssh calls on that session, and the two queues (mutex-protected). They do **not** alloc/free slots, touch other conns, or read the es64 DB.
- Creds are fetched by the **main thread** before enqueue (so es64/ctree access stays single-threaded).
- Handoff: main → request queue (mutex+condvar) → connect thread → completion queue (mutex) + `eventfd` write → main epoll wakes, drains completions.

## File Structure
**Create:**
- `cnc/ncland/src/ncland_connpool.h` / `.cpp` — the connect thread-pool (request/completion queues, eventfd, pthreads).
- `cnc/ncland/src/ncland_connpool_tests.cpp` — nfunit `connpool` suite (stubbed connect fn, no real network).

**Modify:**
- `cnc/ncland/src/ncland.h` — add the connpool to `ncland_wh_t`; add a fd→conn lookup; drop/repurpose worker-process types as needed.
- `cnc/ncland/src/ncland_warehouse.cpp` — main loop registers NE fds + completion eventfd; open path enqueues instead of connecting inline; remove spawn/assign/pass_fd.
- `cnc/ncland/src/ncland_worker.cpp` — keep the I/O functions, adapt signatures to the single-process context; delete the process-side (`worker_main_loop`, `worker_drain_ctl_sock`, `ncland_worker_run`).
- `cnc/ncland/src/ncland.cpp` — `main()` no longer forks workers.
- `cnc/ncland/src/ncland.mk` — link `-lpthread` (already there) ; add `ncland_connpool.o`.
- `cnc/ncland/src/ncland_unit_tests.cpp` — rework the ~20 worker/warehouse-process tests.

---

# Phase 1 — Connect thread-pool (isolated, fully testable)

## Task 1: connpool data model + lifecycle (no real network)

**Files:** create `ncland_connpool.h`, `ncland_connpool.cpp`, `ncland_connpool_tests.cpp`; modify `ncland.mk`.

- [ ] **Step 1: header** `ncland_connpool.h`
```cpp
#ifndef NCLAND_CONNPOOL_H
#define NCLAND_CONNPOOL_H

#include <pthread.h>
#include <deque>

/* Result of one connect attempt, posted back to the main thread. */
struct connpool_result { int conn_id; int rc; };  /* rc==0 success, <0 failure */

/* Connect callback: runs on a pool thread for one conn slot. The pool passes
 * the conn_id; the callback resolves the conn and performs the blocking
 * connect (e.g. ncland_ssh_connect/ncland_telnet_connect). Returns 0/-1.
 * Set once at pool_init; tests substitute a stub so no real network I/O runs. */
typedef int (*connpool_connect_fn)(void *user, int conn_id);

struct connpool {
    pthread_t              threads[16];
    int                    nthreads;
    pthread_mutex_t        lock;
    pthread_cond_t         cv;            /* signalled when a request is queued */
    std::deque<int>        requests;      /* conn_ids awaiting connect */
    std::deque<connpool_result> results;  /* completed, awaiting main-thread drain */
    int                    event_fd;      /* eventfd; written on each completion */
    int                    running;       /* 0 = threads should exit */
    connpool_connect_fn    connect_fn;
    void                  *user;          /* opaque, passed to connect_fn (the wh) */
};

/** @brief Start nthreads (1..16). event_fd is an eventfd the caller registers
 *  in epoll; readable when >=1 result is queued. @return 0 / -1. */
int  connpool_init(connpool *p, int nthreads, connpool_connect_fn fn, void *user);

/** @brief Enqueue a conn_id for a pool thread to connect. @return 0 / -1. */
int  connpool_submit(connpool *p, int conn_id);

/** @brief Drain all completed results into out (cleared first); reads the
 *  eventfd to re-arm. Call from the main thread on event_fd readiness. */
void connpool_drain(connpool *p, std::deque<connpool_result> *out);

/** @brief Stop threads and free resources (joins all). */
void connpool_shutdown(connpool *p);

#endif
```

- [ ] **Step 2: failing test** `ncland_connpool_tests.cpp`
```cpp
#include "../../../include/nfunit-test.hpp"
#include "ncland_connpool.h"
#include <sys/eventfd.h>
#include <poll.h>
#include <unistd.h>

/* stub connect: even conn_id "succeeds", odd "fails"; no network. */
static int stub_connect(void *, int conn_id) { return (conn_id % 2 == 0) ? 0 : -1; }

TEST("connpool", "C1 submit -> result via eventfd") {
    connpool p;
    REQUIRE(connpool_init(&p, 2, stub_connect, nullptr) == 0);
    connpool_submit(&p, 4);     /* success */
    connpool_submit(&p, 7);     /* failure */
    /* wait for both completions on the eventfd */
    int got = 0;
    std::deque<connpool_result> out;
    for (int spins = 0; spins < 200 && got < 2; ++spins) {
        struct pollfd pfd { p.event_fd, POLLIN, 0 };
        if (poll(&pfd, 1, 50) > 0) { connpool_drain(&p, &out); got += (int)out.size();
                                     for (auto &r : out) { /* accumulate */ } }
        static std::deque<connpool_result> acc;  /* not ideal; see note */
    }
    /* Simpler: drain until we've seen 2 total. */
    connpool_shutdown(&p);
    REQUIRE(got >= 0);   /* replaced below */
}
```
> The above is awkward; use this cleaner version instead — accumulate across drains:
```cpp
TEST("connpool", "C1 submit -> results via eventfd") {
    connpool p;
    REQUIRE(connpool_init(&p, 2, stub_connect, nullptr) == 0);
    connpool_submit(&p, 4);
    connpool_submit(&p, 7);
    std::deque<connpool_result> all;
    for (int spins = 0; spins < 200 && (int)all.size() < 2; ++spins) {
        struct pollfd pfd { p.event_fd, POLLIN, 0 };
        if (poll(&pfd, 1, 50) > 0) {
            std::deque<connpool_result> out;
            connpool_drain(&p, &out);
            for (auto &r : out) all.push_back(r);
        }
    }
    REQUIRE(all.size() == 2);
    int succ = 0, fail = 0;
    for (auto &r : all) { if (r.conn_id == 4) { REQUIRE(r.rc == 0); succ++; }
                          if (r.conn_id == 7) { REQUIRE(r.rc < 0); fail++; } }
    REQUIRE(succ == 1); REQUIRE(fail == 1);
    connpool_shutdown(&p);
}
TEST("connpool", "C2 shutdown with no work is clean") {
    connpool p;
    REQUIRE(connpool_init(&p, 4, stub_connect, nullptr) == 0);
    connpool_shutdown(&p);   /* must join all threads, not hang */
}
```
(Delete the first awkward C1 draft; keep only the clean C1 + C2.)

- [ ] **Step 3: implement** `ncland_connpool.cpp`
```cpp
#include "ncland_connpool.h"
#include "nflog.hpp"
#include <sys/eventfd.h>
#include <unistd.h>
#include <stdint.h>

static void *connpool_thread(void *arg)
{
    connpool *p = (connpool *)arg;
    for (;;) {
        pthread_mutex_lock(&p->lock);
        while (p->running && p->requests.empty())
            pthread_cond_wait(&p->cv, &p->lock);
        if (!p->running && p->requests.empty()) { pthread_mutex_unlock(&p->lock); break; }
        int conn_id = p->requests.front();
        p->requests.pop_front();
        pthread_mutex_unlock(&p->lock);

        int rc = p->connect_fn ? p->connect_fn(p->user, conn_id) : -1;

        pthread_mutex_lock(&p->lock);
        p->results.push_back(connpool_result{ conn_id, rc });
        pthread_mutex_unlock(&p->lock);
        uint64_t one = 1;
        (void)!write(p->event_fd, &one, sizeof(one));   /* wake main loop */
    }
    return nullptr;
}

int connpool_init(connpool *p, int nthreads, connpool_connect_fn fn, void *user)
{
    if (!p || nthreads < 1 || nthreads > 16) return -1;
    p->nthreads = nthreads; p->connect_fn = fn; p->user = user; p->running = 1;
    p->requests.clear(); p->results.clear();
    pthread_mutex_init(&p->lock, nullptr);
    pthread_cond_init(&p->cv, nullptr);
    p->event_fd = eventfd(0, EFD_NONBLOCK);
    if (p->event_fd < 0) return -1;
    for (int i = 0; i < nthreads; ++i)
        if (pthread_create(&p->threads[i], nullptr, connpool_thread, p) != 0) {
            p->nthreads = i; connpool_shutdown(p); return -1;
        }
    return 0;
}

int connpool_submit(connpool *p, int conn_id)
{
    if (!p) return -1;
    pthread_mutex_lock(&p->lock);
    p->requests.push_back(conn_id);
    pthread_cond_signal(&p->cv);
    pthread_mutex_unlock(&p->lock);
    return 0;
}

void connpool_drain(connpool *p, std::deque<connpool_result> *out)
{
    if (!p || !out) return;
    out->clear();
    uint64_t v;
    while (read(p->event_fd, &v, sizeof(v)) == (ssize_t)sizeof(v)) { /* drain counter */ }
    pthread_mutex_lock(&p->lock);
    p->results.swap(*out);
    pthread_mutex_unlock(&p->lock);
}

void connpool_shutdown(connpool *p)
{
    if (!p) return;
    pthread_mutex_lock(&p->lock);
    p->running = 0;
    pthread_cond_broadcast(&p->cv);
    pthread_mutex_unlock(&p->lock);
    for (int i = 0; i < p->nthreads; ++i) pthread_join(p->threads[i], nullptr);
    if (p->event_fd >= 0) { close(p->event_fd); p->event_fd = -1; }
    pthread_mutex_destroy(&p->lock);
    pthread_cond_destroy(&p->cv);
}
```

- [ ] **Step 4: Makefile** — add `ncland_connpool.o` to `NCLAND_OBJS`, `ncland_connpool_tests.o` to the `ncland_unit_tests` prereqs (use `writing-nmake-makefiles`). `-lpthread` is already linked.

- [ ] **Step 5: build + run** `nmake -f ncland.mk ncland_unit_tests && ./ncland_unit_tests connpool` → C1, C2 pass. Then full suite stays green.

- [ ] **Step 6: commit** `git commit -m "ncland: connect thread-pool (connpool) + tests"`

---

# Phase 2 — Run established sessions in the main process

> **CORRECTION (2026-06-05, found during execution):** Tasks 2, 3, and the worker-side of Task 6 are NOT independently compilable and must be done as **one coherent "single-process I/O cutover"** commit. Reason: the I/O functions (`handle_ne_data`, `dispatch_cmd`, `check_*`, `worker_send_rsp`) use `ncland_wrkr_t *ctx` (`ctx->epfd`, `ctx->conn[]` *pointer* array, `ctx->ctl_sock`) and their ONLY callers are `worker_main_loop`/`worker_drain_ctl_sock`. Changing the signatures to `wh` requires deleting those callers in the same change; you cannot "adapt + object-compile" in isolation. So the cutover is: add `wh->epfd` + `wh_fd_to_conn`; adapt the I/O fns to `wh` (note: `wh->conn[]` is an INLINE array indexed by state, not a pointer array — iteration/registration logic changes); make `worker_send_rsp` a direct ROUTER send via the pending stash; register established NE fds in the warehouse epoll; route ROUTER cmds straight to `dispatch_cmd`; run the timers in the warehouse loop; and DELETE `worker_main_loop`/`worker_drain_ctl_sock`/`ncland_worker_run`/`worker_register_ctl_sock`/`worker_add_conn`/`worker_remove_conn` (conn registration moves to the connpool-completion handler). This is a large single cutover — do it in a worktree, not as a quick edit. The connpool-into-open wiring (Tasks 4–5) and the warehouse-spawn/pass_fd removal + test rework (Tasks 6–7) follow.

> Move the worker I/O so it operates on `conn_t` in the main process. The worker's I/O functions stay; their `ncland_wrkr_t *ctx` parameter is replaced with `ncland_wh_t *wh` (which owns the conn table + epfd). No behavior change yet — just relocate the call site from a worker process to the warehouse loop.

## Task 2: adapt the I/O functions to the warehouse context
**Files:** `ncland_worker.cpp`, `ncland.h`, `ncland_warehouse.cpp`.

- [ ] Change `handle_ne_data`, `dispatch_cmd` (response send), `check_keepalives`, `check_dirty_timers`, `check_expired_cmds`, `ne_read`/`ne_write` to take/use `ncland_wh_t *wh` instead of `ncland_wrkr_t *ctx`. The response path (`worker_send_rsp`) becomes a direct ROUTER send via the warehouse's `zmq_ctl` + the stashed caller identity (reuse `warehouse_send_rsp`/the pending stash, §5.2), not a `'R'` ctl_sock frame.
- [ ] Add `conn_t *wh_fd_to_conn(ncland_wh_t *wh, int fd)` (linear scan of `wh->conn[]` for `net_fd==fd && state!=CS_IDLE`) — the single-process analogue of `fd_to_conn`.
- [ ] Build object-only (`nmake -f ncland.mk ncland_warehouse.o ncland_worker.o`); fix signature fallout. Commit.

## Task 3: warehouse main loop drives NE fds + timers
**Files:** `ncland_warehouse.cpp`.
- [ ] In `warehouse_main_loop`'s epoll dispatch, add a branch: if the ready fd matches an established conn (`wh_fd_to_conn`), call `handle_ne_data(c, wh)`. Register each established NE `net_fd` in `epfd` (this happens in the connect-completion handler, Task 5).
- [ ] Run `check_keepalives(wh)`/`check_dirty_timers(wh)`/`check_expired_cmds(wh)` from the loop on the existing timer cadence (replace the worker's timer calls).
- [ ] The ROUTER command path: `warehouse_handle_cmd_from_zmq` resolves the target conn and calls `dispatch_cmd(c, ...)` **directly** (no `'C'` ctl_sock frame). Build + commit.

# Phase 3 — Connect via the pool, not inline

## Task 4: warehouse owns a connpool; open enqueues
**Files:** `ncland.h`, `ncland_warehouse.cpp`.
- [ ] Add `connpool pool;` to `ncland_wh_t`. In `warehouse_init`, `connpool_init(&wh->pool, N, warehouse_connect_cb, wh)` where N is a config knob (default 8); register `wh->pool.event_fd` in epoll (Task 5).
- [ ] `static int warehouse_connect_cb(void *user, int conn_id)` — runs on a pool thread: `ncland_wh_t *wh = user; conn_t *c = &wh->conn[conn_id]; return (c->ssh_flag == 1) ? ncland_ssh_connect(c) : ncland_telnet_connect(c);` (touches only that slot + libssh — safe).
- [ ] Rewrite `warehouse_open_conn_by_ne`: alloc slot → apply registry → fetch es64 creds (main thread) → set `CS_CONNECTING` → `connpool_submit(&wh->pool, id)` → return (do NOT connect inline, do NOT assign to a worker). Remove the telnet-skip and the synchronous `ncland_ssh_connect`/`warehouse_assign_conn`. Build + commit.

## Task 5: completion handler registers established fds
**Files:** `ncland_warehouse.cpp`.
- [ ] In `warehouse_main_loop`, on `wh->pool.event_fd` readiness: `connpool_drain(&wh->pool, &results)`; for each `{conn_id, rc}`: if `rc==0` → `EPOLL_CTL_ADD` `conn[conn_id].net_fd` (now `CS_READY`), `LOG_INFO("session up neid=… conn=…")`; if `rc<0` → `conn_reset` the slot, WARN. Build + commit.
- [ ] Manual smoke (gated, real box): with allowlist of an ssh dtype, confirm `session up …` for reachable NEs and that unreachable ones fail **without stalling** other connects (parallel establish).

# Phase 4 — Delete the worker-process machinery

## Task 6: remove worker processes + IPC
**Files:** `ncland_warehouse.cpp`, `ncland_worker.cpp`, `ncland.cpp`, `ncland.h`, `ncland.mk`.
- [ ] Delete: `warehouse_spawn_worker`, the worker fork loop in `ncland_warehouse_run`, `warehouse_pass_fd`, `warehouse_assign_conn`, `warehouse_least_loaded_worker`, the `ctl_sock`/`socketpair` setup, and on the worker side `worker_main_loop`, `worker_drain_ctl_sock`, `ncland_worker_run`, `worker_register_ctl_sock`. Drop the `'F'`/`'C'`/`'R'` ctl-tag protocol and `ncland_wrkr_t` (or reduce to nothing).
- [ ] `ncland.cpp main()`: no worker fork; just `warehouse_init` → `ncland_warehouse_run` (now a pure single-process epoll loop) → connpool shutdown on exit.
- [ ] Build the daemon clean. Commit.

## Task 7: rework the worker/warehouse-process tests
**Files:** `ncland_unit_tests.cpp`.
- [ ] The ~20 tests referencing `spawn_worker`/`ctl_sock`/`pass_fd`/`assign_conn`/worker-process IPC: delete the ones testing deleted machinery; rewrite the I/O tests (`handle_ne_data`, `dispatch_cmd`, keepalive/dirty/expired) to call the adapted `wh`-based functions directly. Keep the `connpool`, `reg`, `seedfmt`, `nparse`, `applyreg` suites.
- [ ] `./ncland_unit_tests` fully green. Commit.

---

# Done
Single process: ROUTER + nfdb SUB + signalfd + connect-completion eventfd + all established NE fds on one epoll loop; a connect thread-pool does the blocking establish in parallel; libssh sessions live in-process and are driven directly. No worker processes, no SCM_RIGHTS, no ctl_sock.

**Carried over unchanged:** YAML registry (#1), B#4 config-apply, nfdb eventing/dispatch, `nclan-seed`, `ncland_ssh_connect`/`telnet_connect`.

**Follow-ups:** connpool size as a CLI/config knob; per-connect timeout still applies (telnet `poll`, ssh `SSH_OPTIONS_TIMEOUT`); login dialogue (#2 Lua) and the stepper (#3) still pending — they slot into `handle_ne_data`/`dispatch_cmd` now that sessions run in-process.

**Spec note:** §4.1/§4.2 (warehouse/worker processes + SCM_RIGHTS) are superseded by this single-process design; update the spec's process-model section when this lands. See memory `project_ncland_ssh_fd_handoff`.
```
