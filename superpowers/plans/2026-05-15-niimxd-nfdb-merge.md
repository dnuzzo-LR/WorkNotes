# niimxd / nfd Merge Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fold the `nfd` daemon into `niimxd` so that `niimxd` owns the three `nfdb*.sock` ZMQ endpoints, the Ctree DB lifecycle, and the DB command dispatch loop. The `nfd` binary is deleted. The `nfdb` CLI and `libnfdb.so` are unchanged.

**Architecture:** Add three ZMQ sockets (ROUTER on `nfdb.sock`, XPUB on `nfdb_pub.sock`, XSUB on `nfdb_sub.sock`) to `niimxd`'s existing epoll-with-ZMQ_FD event loop. Move the per-client state map and DB command dispatch into niimxd. Link `nfdb_cmd.o` and `nfdb_proto.o` into the `niimxd` binary. Cut over `test_nfdb.sh` to start `niimxd`. Delete `nfd.cpp` and drop its Makefile target.

**Tech Stack:** C++ (gcc 4.8.5 baseline), ZeroMQ (libzmq), Lucent/AT&T nmake, Ctree (`com_db.h` / `gen_db.h` / `inc_db.h`), epoll/signalfd/timerfd.

**Spec:** `docs/superpowers/specs/2026-05-15-niimxd-nfdb-merge-design.md`

---

## File Map

**Modified:**
- `cnc/niimx/src/niimxd.cpp` — main integration point: state, init, handlers, shutdown.
- `cnc/niimx/src/nfdb_cmd.cpp` — rename `g_xpub` → `nfdb_g_xpub`, add `nfdb_send_frame` (helper hosted here so niimxd and command handlers share one definition).
- `cnc/niimx/src/nfdb_cmd.h` — rename extern, declare `nfdb_send_frame`.
- `cnc/niimx/src/nfd.cpp` — updated incrementally during prep tasks, then **deleted** in the cutover task.
- `cnc/niimx/src/Makefile` — drop `nfd` target, link `nfdb_cmd.o nfdb_proto.o` into `niimxd`.
- `cnc/niimx/src/test_nfdb.sh` — start `niimxd` instead of `nfd`.

**Unchanged:**
- `cnc/niimx/src/nfdb_proto.cpp` / `.h`
- `cnc/niimx/src/nfdb_client.cpp` (still builds `nfdb` CLI)
- `cnc/niimx/src/nfdb_clientlib.cpp` (still builds `libnfdb.so`)
- `cnc/niimx/src/niimx.cpp` (the `niimx` CLI client)

**Deleted:**
- `cnc/niimx/src/nfd.cpp` (final cutover task)

---

## Build & Test Commands

All builds run from `cnc/niimx/src/` with the project's nmake:

```bash
cd /home/dan/Git/netflex/cnc/niimx/src
nmake ../../../3b2/bin/niimxd
nmake ../../../3b2/bin/nfd          # only used in prep tasks; removed in cutover
nmake ../../../3b2/bin/nfdb
nmake $(LDIR)/nfdb.$(LIBTYPE)
```

Integration test:
```bash
cd /home/dan/Git/netflex/cnc/niimx/src
./test_nfdb.sh
```

Expected: "Results: N passed, 0 failed".

Verify `BASE` / `VPATH`:
```bash
git rev-parse --show-toplevel    # must equal $BASE
echo $VPATH | cut -d: -f1        # must equal $BASE
```

---

# Phase A — Symbol-Prefix Prep

These tasks keep `nfd` building and `test_nfdb.sh` passing against `nfd`. The goal is to land the cross-file rename in a stand-alone commit before touching `niimxd`.

## Task 1: Rename `g_xpub` → `nfdb_g_xpub`

**Files:**
- Modify: `cnc/niimx/src/nfd.cpp` — change three references: declaration near top, assignments in `main()`, and the `g_xpub = NULL` after close.
- Modify: `cnc/niimx/src/nfdb_cmd.h` — change `extern void *g_xpub;` to `extern void *nfdb_g_xpub;`.
- Modify: `cnc/niimx/src/nfdb_cmd.cpp` — change every reference to `g_xpub` to `nfdb_g_xpub` (used inside `nfdb_event_publish` and possibly `cmd_publish`).

- [ ] **Step 1: Find all references**

```bash
cd /home/dan/Git/netflex/cnc/niimx/src
grep -n 'g_xpub' nfd.cpp nfdb_cmd.cpp nfdb_cmd.h
```

Expected: hits in all three files. Note every line.

- [ ] **Step 2: Apply rename in `nfd.cpp`**

In `nfd.cpp` change:

```cpp
void *g_xpub = NULL;
```
to:
```cpp
void *nfdb_g_xpub = NULL;
```

And in `main()` change:
```cpp
g_xpub = zmq_socket(ctx, ZMQ_XPUB);
if (!g_xpub) {
```
to:
```cpp
nfdb_g_xpub = zmq_socket(ctx, ZMQ_XPUB);
if (!nfdb_g_xpub) {
```

Apply the same substitution to every other `g_xpub` line in `nfd.cpp` (the `zmq_bind`, the cleanup `zmq_close(g_xpub)` and `g_xpub = NULL`).

- [ ] **Step 3: Apply rename in `nfdb_cmd.h`**

Change:
```c
extern void *g_xpub;
```
to:
```c
extern void *nfdb_g_xpub;
```

- [ ] **Step 4: Apply rename in `nfdb_cmd.cpp`**

Search for `g_xpub` inside `nfdb_cmd.cpp` (likely inside `nfdb_event_publish` and `cmd_publish`); replace every occurrence with `nfdb_g_xpub`.

- [ ] **Step 5: Build nfd and nfdb**

```bash
cd /home/dan/Git/netflex/cnc/niimx/src
nmake ../../../3b2/bin/nfd
nmake ../../../3b2/bin/nfdb
```

Expected: clean compile, no warnings about undefined `g_xpub` / `nfdb_g_xpub`.

- [ ] **Step 6: Run integration test**

```bash
cd /home/dan/Git/netflex/cnc/niimx/src
./test_nfdb.sh
```

Expected: all tests pass, including the event-delivery test (which exercises `nfdb_g_xpub`).

- [ ] **Step 7: Commit**

```bash
cd /home/dan/Git/netflex
git add cnc/niimx/src/nfd.cpp cnc/niimx/src/nfdb_cmd.cpp cnc/niimx/src/nfdb_cmd.h
GIT_EDITOR=mg git commit --edit --file=- <<'EOF'
nfdb: prefix g_xpub global as nfdb_g_xpub

Prep for niimxd/nfd merge: avoid bare globals and satisfy the
project's per-component prefix rule before nfdb_cmd.o is linked
into niimxd alongside other ZMQ code.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
```

---

## Task 2: Rename `send_frame` → `nfdb_send_frame` and move declaration to `nfdb_cmd.h`

**Files:**
- Modify: `cnc/niimx/src/nfd.cpp` — remove the local non-static `send_frame` definition (replace with a call to the renamed helper) and update all callers in `main()` to use `nfdb_send_frame`.
- Modify: `cnc/niimx/src/nfdb_cmd.cpp` — add the definition of `nfdb_send_frame` (copy the body of the current `send_frame` from `nfd.cpp`). Update internal callers (`cmd_publish`, `nfdb_event_publish`) to use the new name.
- Modify: `cnc/niimx/src/nfdb_cmd.h` — replace `int send_frame(...)` declaration with `int nfdb_send_frame(...)`.

The motivation: niimxd's own code already uses the identifier `send_frame` in scope (lower-case helpers in its ZMQ paths). Hosting the helper in `nfdb_cmd.cpp` lets both `niimxd.cpp` and the command handlers share one definition once we link `nfdb_cmd.o` into niimxd.

- [ ] **Step 1: Find all references**

```bash
cd /home/dan/Git/netflex/cnc/niimx/src
grep -n 'send_frame' nfd.cpp nfdb_cmd.cpp nfdb_cmd.h
```

Note every call site and the current definition location.

- [ ] **Step 2: Update `nfdb_cmd.h`**

Replace the existing declaration:

```c
int send_frame(void *sock, const void *data, size_t len, int flags);
```

with:

```c
/**
 * @brief Send a single ZMQ message frame from a data buffer.
 *
 * @param sock  ZMQ socket to send on.
 * @param data  Pointer to data.
 * @param len   Length of data.
 * @param flags ZMQ send flags (e.g., ZMQ_SNDMORE).
 * @return 0 on success, -1 on error.
 */
int nfdb_send_frame(void *sock, const void *data, size_t len, int flags);
```

- [ ] **Step 3: Move the definition into `nfdb_cmd.cpp`**

In `nfdb_cmd.cpp`, add (near the top, after the existing includes):

```cpp
int nfdb_send_frame(void *sock, const void *data, size_t len, int flags)
{
    zmq_msg_t msg;
    zmq_msg_init_size(&msg, len);
    memcpy(zmq_msg_data(&msg), data, len);
    int rc = zmq_msg_send(&msg, sock, flags);
    if (rc < 0) {
        zmq_msg_close(&msg);
        return -1;
    }
    return 0;
}
```

Then search `nfdb_cmd.cpp` for any internal call to `send_frame(` and change it to `nfdb_send_frame(`.

- [ ] **Step 4: Update `nfd.cpp`**

Delete the existing `send_frame` definition in `nfd.cpp` (the function body between roughly lines 106–117 of `nfd.cpp`). Then in `main()` replace all three sender calls:

```cpp
send_frame(router, identity.data(), identity.size(), ZMQ_SNDMORE);
send_frame(router, "", 0, ZMQ_SNDMORE);
send_frame(router, c.wbuf.data(), c.wbuf.size(), 0);
```

with:

```cpp
nfdb_send_frame(router, identity.data(), identity.size(), ZMQ_SNDMORE);
nfdb_send_frame(router, "", 0, ZMQ_SNDMORE);
nfdb_send_frame(router, c.wbuf.data(), c.wbuf.size(), 0);
```

- [ ] **Step 5: Build nfd and nfdb**

```bash
cd /home/dan/Git/netflex/cnc/niimx/src
nmake ../../../3b2/bin/nfd
nmake ../../../3b2/bin/nfdb
```

Expected: clean compile. If the linker reports a duplicate definition of `send_frame`, you missed deleting the body from `nfd.cpp`. If it reports an undefined `nfdb_send_frame`, you missed adding the body to `nfdb_cmd.cpp`.

- [ ] **Step 6: Run integration test**

```bash
./test_nfdb.sh
```

Expected: all tests pass. The event-delivery test exercises the renamed path on both ends.

- [ ] **Step 7: Commit**

```bash
cd /home/dan/Git/netflex
git add cnc/niimx/src/nfd.cpp cnc/niimx/src/nfdb_cmd.cpp cnc/niimx/src/nfdb_cmd.h
GIT_EDITOR=mg git commit --edit --file=- <<'EOF'
nfdb: rename send_frame to nfdb_send_frame and host in nfdb_cmd.cpp

Prep for niimxd/nfd merge: avoid collision with niimxd's own helpers
and give nfdb_cmd.o callers a shared definition once it links into
niimxd.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
```

---

# Phase B — Port nfd Logic into niimxd

After Phase B, both `niimxd` and `nfd` exist on disk and serve nfdb traffic — `nfd` is left intact as a fallback while we validate niimxd. Cutover happens in Phase C.

## Task 3: Add Ctree DB lifecycle and nfdb includes to niimxd

**Files:**
- Modify: `cnc/niimx/src/niimxd.cpp` — add includes, default endpoint constants, and `ct_init` / `CloseISAM` calls into the existing startup and shutdown paths.

- [ ] **Step 1: Add includes near the top of `niimxd.cpp`**

After the existing C++ standard-library includes and before `using namespace std;`, add:

```cpp
#include <map>          // per-client state keyed by ZMQ identity

extern "C" {
#include "com_db.h"
#include "gen_db.h"
}

#include "nfdb_proto.h"
#include "nfdb_cmd.h"
```

If `<map>` is already included, skip that line.

- [ ] **Step 2: Add default endpoint constants**

After the existing `NIIMX_DEF_ENDPOINT` line:

```cpp
#define NIIMX_DEF_NFDB_ENDPOINT      "ipc:///usr/cnc/data/nfdb.sock"
#define NIIMX_DEF_NFDB_PUB_ENDPOINT  "ipc:///usr/cnc/data/nfdb_pub.sock"
#define NIIMX_DEF_NFDB_SUB_ENDPOINT  "ipc:///usr/cnc/data/nfdb_sub.sock"
```

- [ ] **Step 3: Initialise the DB during niimxd startup**

Find the main initialisation block in `niimxd.cpp` (the function that calls `niimx_setup_zmq` or, if startup runs inline in `main()`, the spot just before that). Add, before the call to `niimx_setup_zmq()`:

```cpp
if (ct_init(0, 0, 0) == FAILURE) {
    Err("ct_init() failed — Ctree DB unavailable" << endl);
    return 1;
}
Trc(2, "Ctree DB initialised" << endl);
```

(If `niimxd`'s `main` uses a different return-value style, match it. The error message and trace level should match existing surrounding code.)

- [ ] **Step 4: Add `CloseISAM()` to graceful shutdown**

In `niimx_graceful_shutdown()` (around line 2260 of niimxd.cpp at the time of writing), after the existing ZMQ cleanup block:

```cpp
if (G.router)  { zmq_close(G.router);      G.router = nullptr; }
if (G.zmq_ctx) { zmq_ctx_destroy(G.zmq_ctx); G.zmq_ctx = nullptr; }
```

…add (immediately after):

```cpp
// Close Ctree DB after all sockets are torn down
extern "C" int CloseISAM(void);
CloseISAM();
Trc(2, "Ctree DB closed" << endl);
```

(If `CloseISAM` is already prototyped via an included header, drop the inline `extern "C"` line.)

- [ ] **Step 5: Compile niimxd object only**

```bash
cd /home/dan/Git/netflex/cnc/niimx/src
nmake niimxd.o
```

Expected: clean compile. We deliberately stop short of linking the binary until Task 11 wires `nfdb_cmd.o`/`nfdb_proto.o` into the Makefile. If compilation fails referencing `ct_init`, `CloseISAM`, `Client`, or `Command`, fix the include before continuing.

- [ ] **Step 6: Commit**

```bash
cd /home/dan/Git/netflex
git add cnc/niimx/src/niimxd.cpp
GIT_EDITOR=mg git commit --edit --file=- <<'EOF'
niimxd: add Ctree DB lifecycle and nfdb endpoint constants

First step toward absorbing nfd: open the DB at startup and close
it during graceful shutdown. Endpoint constants will be consumed
by the nfdb socket init in a follow-up task.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
```

---

## Task 4: Extend `NiimxGlobalState` with nfdb socket fields

**Files:**
- Modify: `cnc/niimx/src/niimxd.cpp` — extend `NiimxGlobalState` (around line 347) and update its Doxygen.

- [ ] **Step 1: Add fields to the struct**

In the `NiimxGlobalState` definition, after the existing `zmq_fd` line:

```cpp
  void* nfdb_router = nullptr;   /**< nfdb ROUTER socket on nfdb.sock. */
  void* nfdb_xpub   = nullptr;   /**< nfdb XPUB socket for event subscribers. */
  void* nfdb_xsub   = nullptr;   /**< nfdb XSUB socket for event publishers. */
  int   nfdb_router_fd = -1;     /**< ZMQ_FD for nfdb_router. */
  int   nfdb_xpub_fd   = -1;     /**< ZMQ_FD for nfdb_xpub. */
  int   nfdb_xsub_fd   = -1;     /**< ZMQ_FD for nfdb_xsub. */
  std::map<std::string, Client> nfdb_clients;  /**< Per-client state keyed by ZMQ identity. */
```

- [ ] **Step 2: Update the Doxygen `@var` block above the struct**

Add three `@var` entries matching the new sockets and fds, plus one for `nfdb_clients`. Mirror the style of the existing entries (one-line each).

- [ ] **Step 3: Compile niimxd object**

```bash
nmake niimxd.o
```

Expected: clean compile. The new fields are not yet used.

- [ ] **Step 4: Commit**

```bash
git add cnc/niimx/src/niimxd.cpp
GIT_EDITOR=mg git commit --edit --file=- <<'EOF'
niimxd: add nfdb socket state to NiimxGlobalState

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
```

---

## Task 5: Add `niimx_setup_nfdb()`

**Files:**
- Modify: `cnc/niimx/src/niimxd.cpp` — new static function alongside `niimx_setup_zmq()`.

- [ ] **Step 1: Add the function**

Place this immediately after `niimx_setup_zmq()` (after the closing `}` of that function, around the current line 815):

```cpp
/**
 * @brief Initialise the three nfdb ZMQ sockets.
 *
 * Binds a ROUTER for DB commands and an XPUB/XSUB pair forming the
 * application-event broker. Each socket's ZMQ_FD is registered with the global
 * epoll instance edge-triggered, matching the convention used by the niimx
 * ROUTER socket.
 *
 * @return true on success, false on any ZMQ create/bind failure.
 */
static bool niimx_setup_nfdb() {
    G.nfdb_router = zmq_socket(G.zmq_ctx, ZMQ_ROUTER);
    if (!G.nfdb_router) {
        Err("zmq_socket(nfdb ROUTER) failed errno=" << errno << endl);
        return false;
    }
    if (0 != zmq_bind(G.nfdb_router, NIIMX_DEF_NFDB_ENDPOINT)) {
        Err("zmq_bind(" << NIIMX_DEF_NFDB_ENDPOINT << ") failed errno=" << errno << endl);
        return false;
    }
    Trc(2, "nfdb ROUTER bound to " << NIIMX_DEF_NFDB_ENDPOINT << endl);

    G.nfdb_xpub = zmq_socket(G.zmq_ctx, ZMQ_XPUB);
    if (!G.nfdb_xpub) {
        Err("zmq_socket(nfdb XPUB) failed errno=" << errno << endl);
        return false;
    }
    if (0 != zmq_bind(G.nfdb_xpub, NIIMX_DEF_NFDB_PUB_ENDPOINT)) {
        Err("zmq_bind(" << NIIMX_DEF_NFDB_PUB_ENDPOINT << ") failed errno=" << errno << endl);
        return false;
    }
    Trc(2, "nfdb XPUB bound to " << NIIMX_DEF_NFDB_PUB_ENDPOINT << endl);

    G.nfdb_xsub = zmq_socket(G.zmq_ctx, ZMQ_XSUB);
    if (!G.nfdb_xsub) {
        Err("zmq_socket(nfdb XSUB) failed errno=" << errno << endl);
        return false;
    }
    if (0 != zmq_bind(G.nfdb_xsub, NIIMX_DEF_NFDB_SUB_ENDPOINT)) {
        Err("zmq_bind(" << NIIMX_DEF_NFDB_SUB_ENDPOINT << ") failed errno=" << errno << endl);
        return false;
    }
    Trc(2, "nfdb XSUB bound to " << NIIMX_DEF_NFDB_SUB_ENDPOINT << endl);

    // Publish handlers expect this symbol (declared in nfdb_cmd.h)
    extern void *nfdb_g_xpub;
    nfdb_g_xpub = G.nfdb_xpub;

    // Register all three ZMQ_FDs with epoll, edge-triggered
    size_t fd_size = sizeof(int);
    zmq_getsockopt(G.nfdb_router, ZMQ_FD, &G.nfdb_router_fd, &fd_size);
    zmq_getsockopt(G.nfdb_xpub,   ZMQ_FD, &G.nfdb_xpub_fd,   &fd_size);
    zmq_getsockopt(G.nfdb_xsub,   ZMQ_FD, &G.nfdb_xsub_fd,   &fd_size);

    struct epoll_event ev{};
    ev.events = EPOLLIN | EPOLLET;

    ev.data.fd = G.nfdb_router_fd;
    if (epoll_ctl(G.epfd, EPOLL_CTL_ADD, G.nfdb_router_fd, &ev) < 0) {
        Err("epoll_ctl ADD nfdb_router_fd errno=" << errno << endl);
        return false;
    }

    ev.data.fd = G.nfdb_xpub_fd;
    if (epoll_ctl(G.epfd, EPOLL_CTL_ADD, G.nfdb_xpub_fd, &ev) < 0) {
        Err("epoll_ctl ADD nfdb_xpub_fd errno=" << errno << endl);
        return false;
    }

    ev.data.fd = G.nfdb_xsub_fd;
    if (epoll_ctl(G.epfd, EPOLL_CTL_ADD, G.nfdb_xsub_fd, &ev) < 0) {
        Err("epoll_ctl ADD nfdb_xsub_fd errno=" << errno << endl);
        return false;
    }

    return true;
}
```

- [ ] **Step 2: Compile niimxd object**

```bash
nmake niimxd.o
```

Expected: clean compile. (Link not yet attempted — Makefile prerequisite update lands in Task 11.) Also remove the inline `extern void *nfdb_g_xpub;` if `nfdb_cmd.h` already provides it; the inline `extern` shown in step 1 is belt-and-suspenders and can be deleted once `#include "nfdb_cmd.h"` from Task 3 is in place.

- [ ] **Step 3: Commit**

```bash
git add cnc/niimx/src/niimxd.cpp
GIT_EDITOR=mg git commit --edit --file=- <<'EOF'
niimxd: add niimx_setup_nfdb() socket init

Binds ROUTER/XPUB/XSUB on the three nfdb IPC paths and registers
their ZMQ_FDs in the existing epoll edge-triggered. Not yet
called by main; wired in a follow-up task.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
```

---

## Task 6: Wire `niimx_setup_nfdb()` into startup

**Files:**
- Modify: `cnc/niimx/src/niimxd.cpp` — call the new setup right after the existing `niimx_setup_zmq()` call.

- [ ] **Step 1: Find the existing call site**

```bash
grep -n 'niimx_setup_zmq' /home/dan/Git/netflex/cnc/niimx/src/niimxd.cpp
```

Identify the call site in `main()` (or whichever init function calls it).

- [ ] **Step 2: Add the nfdb setup call**

Immediately after the existing call (and its error-return check):

```cpp
if (!niimx_setup_zmq()) return 1;

if (!niimx_setup_nfdb()) {
    Err("niimx_setup_nfdb() failed — exiting" << endl);
    return 1;
}
```

(Match the surrounding error-return idiom — if the function returns `void` and uses a different error path, use that path instead.)

- [ ] **Step 3: Compile niimxd object**

```bash
nmake niimxd.o
```

Expected: clean compile.

- [ ] **Step 4: Commit**

```bash
git add cnc/niimx/src/niimxd.cpp
GIT_EDITOR=mg git commit --edit --file=- <<'EOF'
niimxd: call niimx_setup_nfdb() during startup

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
```

---

## Task 7: Add nfdb ROUTER drain handler

**Files:**
- Modify: `cnc/niimx/src/niimxd.cpp` — new static `niimx_handle_nfdb_router()`.

- [ ] **Step 1: Add the handler**

Place this after `niimx_handle_zmq()` (the existing ROUTER drain for niimx.ipc):

```cpp
/**
 * @brief Drain pending messages on the nfdb ROUTER socket and dispatch them.
 *
 * Edge-triggered: must loop until ZMQ_EVENTS no longer reports ZMQ_POLLIN.
 * Each request is the three-frame ZMQ form [identity][empty][data]; the
 * response is sent back in the same shape. Per-client state lives in
 * G.nfdb_clients keyed by the binary identity frame.
 */
static void niimx_handle_nfdb_router() {
    int events;
    size_t evsize = sizeof(events);

    while (true) {
        zmq_getsockopt(G.nfdb_router, ZMQ_EVENTS, &events, &evsize);
        if (!(events & ZMQ_POLLIN)) break;

        // Receive [identity][empty][data]
        zmq_msg_t msg;
        std::string identity, empty, data;

        zmq_msg_init(&msg);
        if (zmq_msg_recv(&msg, G.nfdb_router, ZMQ_DONTWAIT) < 0) {
            zmq_msg_close(&msg);
            continue;
        }
        identity.assign(static_cast<char*>(zmq_msg_data(&msg)), zmq_msg_size(&msg));
        zmq_msg_close(&msg);

        zmq_msg_init(&msg);
        if (zmq_msg_recv(&msg, G.nfdb_router, ZMQ_DONTWAIT) < 0) {
            zmq_msg_close(&msg);
            continue;
        }
        empty.assign(static_cast<char*>(zmq_msg_data(&msg)), zmq_msg_size(&msg));
        zmq_msg_close(&msg);

        zmq_msg_init(&msg);
        if (zmq_msg_recv(&msg, G.nfdb_router, ZMQ_DONTWAIT) < 0) {
            zmq_msg_close(&msg);
            continue;
        }
        data.assign(static_cast<char*>(zmq_msg_data(&msg)), zmq_msg_size(&msg));
        zmq_msg_close(&msg);

        Trc(5, "nfdb RX id_len=" << identity.size() << " data_len=" << data.size() << endl);

        // Look up or create per-client state
        Client &c = G.nfdb_clients[identity];
        if (c.wbuf.capacity() == 0) {
            client_init(&c);
            Trc(5, "nfdb new client session id_len=" << identity.size() << endl);
        }
        c.wbuf.clear();

        // Strip trailing CR/LF
        while (!data.empty() &&
               (data[data.size()-1] == '\n' || data[data.size()-1] == '\r')) {
            data.resize(data.size() - 1);
        }

        Command cmd;
        int quit = 0;
        if (tokenize(data.c_str(), &cmd) > 0) {
            quit = dispatch_command(&c, &cmd);
        }

        // Send [identity][empty][wbuf]
        nfdb_send_frame(G.nfdb_router, identity.data(), identity.size(), ZMQ_SNDMORE);
        nfdb_send_frame(G.nfdb_router, "", 0, ZMQ_SNDMORE);
        nfdb_send_frame(G.nfdb_router, c.wbuf.data(), c.wbuf.size(), 0);

        if (quit < 0) {
            Trc(5, "nfdb client QUIT — removing session" << endl);
            G.nfdb_clients.erase(identity);
        }
    }
}
```

- [ ] **Step 2: Compile niimxd object**

```bash
nmake niimxd.o
```

Expected: clean compile. Handler isn't called yet — dispatch wires in Task 9.

- [ ] **Step 3: Commit**

```bash
git add cnc/niimx/src/niimxd.cpp
GIT_EDITOR=mg git commit --edit --file=- <<'EOF'
niimxd: add nfdb ROUTER drain handler

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
```

---

## Task 8: Add XSUB→XPUB relay handler and XPUB subscription drain

**Files:**
- Modify: `cnc/niimx/src/niimxd.cpp` — two new static functions.

- [ ] **Step 1: Add the relay handler**

Place this after `niimx_handle_nfdb_router()`:

```cpp
/**
 * @brief Relay all available XSUB-side frames to XPUB.
 *
 * Implements the application-event broker: any publisher writing to the
 * XSUB endpoint has its frames forwarded to all matching subscribers on
 * the XPUB endpoint. Preserves multipart boundaries via ZMQ_SNDMORE.
 * Edge-triggered: loop until ZMQ_EVENTS clears.
 */
static void niimx_handle_nfdb_xsub() {
    int events;
    size_t evsize = sizeof(events);

    while (true) {
        zmq_getsockopt(G.nfdb_xsub, ZMQ_EVENTS, &events, &evsize);
        if (!(events & ZMQ_POLLIN)) break;

        zmq_msg_t msg;
        zmq_msg_init(&msg);
        while (true) {
            if (zmq_msg_recv(&msg, G.nfdb_xsub, ZMQ_DONTWAIT) < 0) break;
            int more = zmq_msg_more(&msg);
            zmq_msg_send(&msg, G.nfdb_xpub, more ? ZMQ_SNDMORE : 0);
            if (!more) break;
        }
        zmq_msg_close(&msg);
    }
}
```

- [ ] **Step 2: Add the XPUB drain**

Immediately after:

```cpp
/**
 * @brief Drain subscription/unsubscription frames from the XPUB socket.
 *
 * XPUB emits one message per subscriber connect/disconnect. We don't act
 * on them today, but the ZMQ_FD is edge-triggered so they must be read or
 * the fd will stop firing.
 */
static void niimx_handle_nfdb_xpub() {
    int events;
    size_t evsize = sizeof(events);

    while (true) {
        zmq_getsockopt(G.nfdb_xpub, ZMQ_EVENTS, &events, &evsize);
        if (!(events & ZMQ_POLLIN)) break;

        zmq_msg_t msg;
        zmq_msg_init(&msg);
        while (true) {
            if (zmq_msg_recv(&msg, G.nfdb_xpub, ZMQ_DONTWAIT) < 0) break;
            if (!zmq_msg_more(&msg)) break;
        }
        zmq_msg_close(&msg);
    }
}
```

- [ ] **Step 3: Compile niimxd object**

```bash
nmake niimxd.o
```

Expected: clean compile.

- [ ] **Step 4: Commit**

```bash
git add cnc/niimx/src/niimxd.cpp
GIT_EDITOR=mg git commit --edit --file=- <<'EOF'
niimxd: add nfdb XSUB->XPUB relay and XPUB subscription drain

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
```

---

## Task 9: Dispatch the three new fds from the main epoll loop

**Files:**
- Modify: `cnc/niimx/src/niimxd.cpp` — extend the fd-match cascade inside the main `epoll_wait` loop.

- [ ] **Step 1: Locate the dispatch site**

```bash
grep -n 'epoll_wait' /home/dan/Git/netflex/cnc/niimx/src/niimxd.cpp
```

There are two `epoll_wait` sites: the main loop (around line 2447 in the original file) and the drain loop inside `niimx_graceful_shutdown()` (around line 2278). You need to extend the **main loop** only.

- [ ] **Step 2: Add the fd matches**

Inside the main loop, where existing code dispatches by `events[i].data.fd` (the pattern is `if (fd == G.tfd) ... else if (fd == G.zmq_fd) ...` etc.), add three new branches:

```cpp
else if (fd == G.nfdb_router_fd) { niimx_handle_nfdb_router(); }
else if (fd == G.nfdb_xsub_fd)   { niimx_handle_nfdb_xsub();   }
else if (fd == G.nfdb_xpub_fd)   { niimx_handle_nfdb_xpub();   }
```

Order matters only for readability — put them next to the existing `G.zmq_fd` branch so all ZMQ_FDs cluster together.

- [ ] **Step 3: Compile niimxd object**

```bash
nmake niimxd.o
```

Expected: clean compile.

- [ ] **Step 4: Commit**

```bash
git add cnc/niimx/src/niimxd.cpp
GIT_EDITOR=mg git commit --edit --file=- <<'EOF'
niimxd: dispatch nfdb router/xsub/xpub fds from main epoll loop

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
```

---

## Task 10: Tear down nfdb sockets in `niimx_graceful_shutdown()`

**Files:**
- Modify: `cnc/niimx/src/niimxd.cpp` — extend the shutdown block.

- [ ] **Step 1: Locate the cleanup block**

In `niimx_graceful_shutdown()`, find the block:

```cpp
if (G.router)  { zmq_close(G.router);      G.router = nullptr; }
if (G.zmq_ctx) { zmq_ctx_destroy(G.zmq_ctx); G.zmq_ctx = nullptr; }
```

- [ ] **Step 2: Insert nfdb teardown before the ctx destroy**

Replace the two lines above with:

```cpp
if (G.nfdb_xsub)   { zmq_close(G.nfdb_xsub);    G.nfdb_xsub   = nullptr; }
if (G.nfdb_xpub)   { zmq_close(G.nfdb_xpub);    G.nfdb_xpub   = nullptr; }
if (G.nfdb_router) { zmq_close(G.nfdb_router);  G.nfdb_router = nullptr; }
if (G.router)      { zmq_close(G.router);       G.router      = nullptr; }
if (G.zmq_ctx)     { zmq_ctx_destroy(G.zmq_ctx); G.zmq_ctx     = nullptr; }
```

The existing `CloseISAM()` call added in Task 3 should already follow — verify it's still positioned after the ctx destroy.

- [ ] **Step 3: Compile niimxd object**

```bash
nmake niimxd.o
```

Expected: clean compile.

- [ ] **Step 4: Commit**

```bash
git add cnc/niimx/src/niimxd.cpp
GIT_EDITOR=mg git commit --edit --file=- <<'EOF'
niimxd: close nfdb sockets during graceful shutdown

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
```

---

## Task 11: Link `nfdb_cmd.o` and `nfdb_proto.o` into niimxd

**Files:**
- Modify: `cnc/niimx/src/Makefile` — add prerequisites to the `niimxd` target.

- [ ] **Step 1: Edit `Makefile`**

Locate the existing `niimxd` target:

```
$(PBIN)/niimxd : niimxd.o $(CORELIBS) $(UTILLIB)  -lnelib
	$(CPLUS_CC)  $(LDFLAGS) -o $(<) $(*) $(ACCLIBS) -lzmq
```

Change to:

```
$(PBIN)/niimxd : niimxd.o nfdb_cmd.o nfdb_proto.o $(CORELIBS) $(UTILLIB)  -lnelib
	$(CPLUS_CC)  $(LDFLAGS) -o $(<) $(*) $(ACCLIBS) -lzmq
```

Do not remove the `nfd`, `nfdb`, or `nfdb.$(LIBTYPE)` targets at this point — they stay until Task 14.

- [ ] **Step 2: Build niimxd — this is the first successful link**

```bash
cd /home/dan/Git/netflex/cnc/niimx/src
nmake ../../../3b2/bin/niimxd
```

Expected: **clean link**. If you still see unresolved symbols, audit which file owns them and adjust the prerequisite list. Common diagnoses:

- `tokenize`, `client_init`, `client_cleanup`, `buf_printf`, `write_ok`, `write_err`, `write_end` → `nfdb_proto.o`
- `dispatch_command`, `cmd_*`, `nfdb_event_publish`, `nfdb_send_frame`, `nfdb_g_xpub` → `nfdb_cmd.o`

- [ ] **Step 3: Verify other targets still build**

```bash
nmake ../../../3b2/bin/nfd
nmake ../../../3b2/bin/nfdb
nmake $(LDIR)/nfdb.$(LIBTYPE)
```

All four targets (niimxd, nfd, nfdb, nfdb shared lib) should now build clean. `nfd` is still intact and serves as a fallback during the next task.

- [ ] **Step 4: Commit**

```bash
cd /home/dan/Git/netflex
git add cnc/niimx/src/Makefile
GIT_EDITOR=mg git commit --edit --file=- <<'EOF'
niimx: link nfdb_cmd.o and nfdb_proto.o into niimxd

niimxd now binds and serves nfdb traffic alongside its existing
duties. The standalone nfd binary still builds and continues to be
a viable fallback until the test harness is repointed.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
```

---

## Task 12: Run niimxd manually and verify nfdb sockets respond

**Files:** none modified — this is a smoke-test gate before retargeting `test_nfdb.sh`.

- [ ] **Step 1: Start niimxd in foreground using ephemeral endpoints**

Check whether niimxd accepts a `-s` or equivalent flag. If it does, run with a private endpoint and let the nfdb defaults bind their normal paths. If not, accept that niimxd will bind its normal endpoint too — make sure no other niimxd is running.

```bash
pkill -x nfd 2>/dev/null            # avoid socket collision
pkill -x niimxd 2>/dev/null
rm -f /usr/cnc/data/nfdb*.sock
../../../3b2/bin/niimxd -f &
NIIMXD_PID=$!
sleep 1
```

- [ ] **Step 2: Sanity-check from the nfdb CLI**

```bash
../../../3b2/bin/nfdb PING
../../../3b2/bin/nfdb DBS
../../../3b2/bin/nfdb DESC ne
```

Expected:
- PING → `+OK PONG`
- DBS → `+OK` followed by a list of database names
- DESC ne → `+OK` followed by schema lines

- [ ] **Step 3: Confirm event broker round-trip**

In one terminal:
```bash
../../../3b2/bin/nfdb -e ipc:///usr/cnc/data/nfdb_pub.sock -S app.
```

In another:
```bash
../../../3b2/bin/nfdb PUBLISH app.test.smoke "hello: niimxd"
```

The subscriber should print the event. Stop both with Ctrl-C.

- [ ] **Step 4: Stop niimxd**

```bash
kill $NIIMXD_PID
wait $NIIMXD_PID 2>/dev/null
```

If any step fails, fix and rerun before continuing. Do **not** proceed to Task 13 with a broken smoke test.

- [ ] **Step 5: Commit (no code change — empty checkpoint, skip)**

No commit. This task is verification only.

---

## Task 13: Repoint `test_nfdb.sh` and run

**Files:**
- Modify: `cnc/niimx/src/test_nfdb.sh` — start `niimxd` instead of `nfd`.

- [ ] **Step 1: Update the start block**

In `test_nfdb.sh`, change the server-start block:

```bash
# ---- Start server ----
echo "Starting nfd on $ENDPOINT ..."
"$BINDIR/nfd" -f -s "$ENDPOINT" -p "$PUB_ENDPOINT" -u "$SUB_ENDPOINT" &
```

to:

```bash
# ---- Start server ----
echo "Starting niimxd on $ENDPOINT ..."
"$BINDIR/niimxd" -f -s "$ENDPOINT" -p "$PUB_ENDPOINT" -u "$SUB_ENDPOINT" &
```

If `niimxd` does not accept those exact flags, two options:

- **Preferred:** add the flags (parse `-s`/`-p`/`-u` in niimxd's `getopt` to override `NIIMX_DEF_NFDB_*_ENDPOINT`). This is consistent with how nfd worked and lets tests use ephemeral sockets.
- **Fallback:** drop the `-s/-p/-u` flags from the test invocation and rely on the default nfdb endpoints. The test will then require exclusive use of `/usr/cnc/data/nfdb*.sock`.

Pick one and apply.

- [ ] **Step 2: Run the test**

```bash
cd /home/dan/Git/netflex/cnc/niimx/src
./test_nfdb.sh
```

Expected: "Results: N passed, 0 failed". The full assert list (PING, FORMAT, DBS, DESC, GET, pipe mode, PUBLISH, app-event delivery) should all pass.

- [ ] **Step 3: Fix any failures**

Failure modes and diagnoses:
- "Server failed to start" → check `getopt` accepts the flags you passed; check that nothing else is bound to the endpoint.
- PING fails → ROUTER drain handler (Task 7) likely wrong; check edge-trigger drain and reply framing.
- PUBLISH "subscriber did not receive app event" → relay handler (Task 8) likely wrong, or `nfdb_g_xpub` not assigned in Task 5.
- DBS / DESC fail → `ct_init()` (Task 3) probably not called or failed silently; check niimxd's trace log.

Iterate until the test fully passes.

- [ ] **Step 4: Commit**

```bash
cd /home/dan/Git/netflex
git add cnc/niimx/src/test_nfdb.sh cnc/niimx/src/niimxd.cpp
GIT_EDITOR=mg git commit --edit --file=- <<'EOF'
test_nfdb: drive niimxd instead of nfd

niimxd now serves the nfdb endpoints; integration test repointed.
Any niimxd CLI changes required to accept the test's -s/-p/-u
overrides are included.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
```

---

# Phase C — Cutover

## Task 14: Delete `nfd.cpp` and drop its Makefile target

**Files:**
- Delete: `cnc/niimx/src/nfd.cpp`
- Modify: `cnc/niimx/src/Makefile` — remove `nfd` from `.ALL` and remove its target stanza.

- [ ] **Step 1: Edit `Makefile`**

In `.ALL`, remove `$(PBIN)/nfd`:

```
.ALL :  $(PBIN)/niimxd $(PBIN)/niimx $(PBIN)/nfd $(PBIN)/nfdb $(LDIR)/nfdb.$(LIBTYPE)
```

becomes:

```
.ALL :  $(PBIN)/niimxd $(PBIN)/niimx $(PBIN)/nfdb $(LDIR)/nfdb.$(LIBTYPE)
```

Delete the entire `nfd` target stanza:

```
$(PBIN)/nfd : nfd.o nfdb_cmd.o nfdb_proto.o $(CORELIBS) $(UTILLIB) -lnelib
	$(CPLUS_CC) $(LDFLAGS) -o $(<) $(*) $(ACCLIBS) -lzmq
```

- [ ] **Step 2: Delete the source**

```bash
cd /home/dan/Git/netflex
git rm cnc/niimx/src/nfd.cpp
```

- [ ] **Step 3: Remove the old binary**

```bash
rm -f /home/dan/Git/netflex/3b2/bin/nfd
```

(Just for hygiene — the next packaging build will not regenerate it.)

- [ ] **Step 4: Build everything**

```bash
cd /home/dan/Git/netflex/cnc/niimx/src
nmake ../../../3b2/bin/niimxd
nmake ../../../3b2/bin/niimx
nmake ../../../3b2/bin/nfdb
nmake $(LDIR)/nfdb.$(LIBTYPE)
```

Expected: all four targets build clean. There should be no remaining reference to `nfd.o` or `nfd` anywhere.

- [ ] **Step 5: Re-run integration tests**

```bash
./test_nfdb.sh
```

Expected: all tests still pass.

- [ ] **Step 6: Commit**

```bash
cd /home/dan/Git/netflex
git add cnc/niimx/src/Makefile
git status   # should show "deleted: cnc/niimx/src/nfd.cpp" plus the Makefile change
GIT_EDITOR=mg git commit --edit --file=- <<'EOF'
niimx: retire standalone nfd daemon

niimxd has owned the nfdb endpoints for one commit cycle and the
integration test runs against it cleanly. Delete nfd.cpp and drop
its Makefile target.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
```

---

## Task 15: Smoke test niimxd's existing functions (regression check)

**Files:** none modified.

The merge must not regress niimxd's pre-existing duties (SSH PTY pool, alarm/utilization monitoring, ZMQ on `niimx.ipc`, signal handling).

- [ ] **Step 1: Start niimxd in foreground**

```bash
pkill -x niimxd 2>/dev/null
rm -f /usr/cnc/data/nfdb*.sock
../../../3b2/bin/niimxd -f &
NIIMXD_PID=$!
sleep 2
```

- [ ] **Step 2: Verify niimx CLI still works against `niimx.ipc`**

```bash
../../../3b2/bin/niimx --help 2>&1 | head
```

Run one or two no-op commands that exercise the niimx-side ROUTER. Confirm responses come back in the expected timeframe.

- [ ] **Step 3: Watch trace log for the first minute**

```bash
tail -F /usr/cnc/trace/niimxd
```

Look for: signalfd ticks, timer ticks, utilization sampling at the 5s/10min cadences mentioned in the niimxd code. No crashes, no "epoll_wait errno=" lines other than EINTR.

- [ ] **Step 4: Send SIGTERM and verify graceful shutdown**

```bash
kill -TERM $NIIMXD_PID
wait $NIIMXD_PID 2>/dev/null
```

Trace should show "Graceful shutdown: waiting for working bchannels...", "Ctree DB closed", "Shutdown complete." in order. No hang.

- [ ] **Step 5: Re-run `test_nfdb.sh` one final time**

```bash
cd /home/dan/Git/netflex/cnc/niimx/src
./test_nfdb.sh
```

Expected: all pass.

- [ ] **Step 6: No commit — verification only.**

If anything regressed, fix in a targeted follow-up commit on this branch before considering the merge complete.

---

# Done

Final branch state should show:
- `niimxd` binary serving four ZMQ endpoints (its original `niimx.ipc`, plus the three nfdb sockets).
- `nfd` binary and `nfd.cpp` gone.
- `nfdb` CLI and `libnfdb.so` unchanged.
- `test_nfdb.sh` driving `niimxd`.
- One commit per task above, in order.

Next branch-level action is up to the user (PR, additional manual testing, etc.). Not part of this plan.
