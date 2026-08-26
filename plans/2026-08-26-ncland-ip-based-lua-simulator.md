# ncland IP-based Lua Simulator Startup Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Restore clan's behavior of spawning a Lua `.clansim` simulator child (over a PTY) when an NE address is a loopback/simulator address, in the `ncland` rewrite.

**Architecture:** A sim-address NE resolves a `.clansim` script (clan's 3-path order); if found, ncland forks it on a PTY via `inc_subproc` and hands the PTY master fd to the existing connect-pool → completion → login → worker-I/O → teardown pipeline. Only the connect step (`ncland_sim_connect`) and child reap (`ncland_sim_disconnect`) are new; everything downstream is transport-agnostic (`ncland_ne_read`/`ncland_ne_write` already fall back to raw `read`/`write` when `c->chan == NULL`). PTY stays in default cooked/echo mode to match clan exactly. No script found → drop with a log (clansimd fallback deferred).

**Tech Stack:** C++17, Lucent/AT&T nmake, libssh/lua/yaml-cpp/zmq (already linked), `inc_subproc` PTY spawn from `-linc` (`cnc/utility/src/misc2.c`), nfunit-test harness (`include/nfunit-test.hpp`).

---

## Prerequisites (read before starting)

- **BASE/VPATH must match this repo before any nmake build.** The session shows a
  mismatch. Fix in the shell first:
  ```bash
  export BASE=/home/dan/Git/support-netflex
  export VPATH=$BASE:$VPATH
  ```
  If an nmake build later fails on a missing `globaldefs.nmk` / `global_*.nmk` /
  project header, **stop and tell the user** — do not reconstruct build paths.
- All Makefile edits use **Lucent/AT&T nmake**, not GNU make. Invoke the
  `writing-nmake-makefiles` skill for the Makefile task.
- Work happens in `cnc/ncland/src`. Paths below are relative to the repo root
  `/home/dan/Git/support-netflex`.

## File Structure

| File | Responsibility |
|------|----------------|
| `cnc/ncland/src/ncland.h` | add `sim`, `sim_pid`, `sim_script[]` to `conn_t` |
| `cnc/ncland/src/ncland_sim.h` (new) | prototypes for the sim transport |
| `cnc/ncland/src/ncland_sim.cpp` (new) | script resolver + `ncland_sim_connect` + `ncland_sim_disconnect` |
| `cnc/ncland/src/ncland_conn.cpp` | init/reset new fields; reap on `conn_free` |
| `cnc/ncland/src/ncland_warehouse.cpp` | sim branch in `open_conn_by_ne`; sim dispatch in `connect_cb`; reap in `carrier_free` |
| `cnc/ncland/src/ncland_sim_tests.cpp` (new) | unit tests for resolver / disconnect / connect / field-clearing |
| `cnc/ncland/src/Makefile` | add `ncland_sim.o` to `NCLAND_OBJS`; add `ncland_sim_tests.o` to the test target |

---

## Task 1: conn_t sim fields + init/reset

**Files:**
- Modify: `cnc/ncland/src/ncland.h` (conn_t, near line 193 — after `neid`/`slot`)
- Modify: `cnc/ncland/src/ncland_conn.cpp` (`conn_reset`, lines 27-45)
- Create/Modify test: `cnc/ncland/src/ncland_sim_tests.cpp`

- [ ] **Step 1: Add the fields to `conn_t`**

In `ncland.h`, inside `typedef struct conn { ... }`, immediately after the
`frame_link identity` block (after `int slot;` at line 192), add:

```c
    /* --- Simulator transport (clan .clansim spawn) ------------------ */
    int            sim;            /**< 1 = spawned .clansim child on a PTY (not ssh/telnet). */
    int            sim_pid;        /**< PID of the .clansim child to reap; 0 when none. */
    char           sim_script[256];/**< Resolved .clansim path (logging / spawn). */
```

- [ ] **Step 2: Clear the fields in `conn_reset`**

`conn_init` already `memset`s the whole struct, so the new fields zero
automatically there. `conn_reset` (which does NOT memset) must clear them so a
reused idle slot carries no stale sim state. In `ncland_conn.cpp`, in
`conn_reset`, after the `c->orig_slot_tp = 0;` line (line 43), add:

```c
    c->sim          = 0;
    c->sim_pid      = 0;
    c->sim_script[0] = '\0';
```

- [ ] **Step 3: Write the failing tests**

Create `cnc/ncland/src/ncland_sim_tests.cpp` with:

```cpp
#include "../../../include/nfunit-test.hpp"
#include "ncland.h"
#include "ncland_sim.h"
#include <cstring>
#include <cstdio>
#include <cstdlib>
#include <fstream>
#include <unistd.h>
#include <fcntl.h>
#include <sys/wait.h>
#include <sys/stat.h>

/* Unit tests must never perform live NE connects. */
static int _ncland_sim_test_no_connect = (g_warehouse_no_connect = 1, 0);

TEST("sim", "S1 conn_init zeroes sim fields") {
    conn_t c;
    memset(&c, 0xFF, sizeof(c)); /* poison */
    conn_init(&c, 5);
    REQUIRE_EQ(c.sim, 0);
    REQUIRE_EQ(c.sim_pid, 0);
    REQUIRE_EQ(c.sim_script[0], '\0');
}

TEST("sim", "S2 conn_reset clears sim fields") {
    conn_t c;
    conn_init(&c, 6);
    c.sim = 1;
    c.sim_pid = 4242;
    strcpy(c.sim_script, "/usr/cnc/data/ne1.clansim");
    conn_reset(&c);
    REQUIRE_EQ(c.sim, 0);
    REQUIRE_EQ(c.sim_pid, 0);
    REQUIRE_EQ(c.sim_script[0], '\0');
}
```

Note: `ncland_sim.h` does not exist yet (Task 2 creates it). This test file will
not compile until Task 2 — that is expected; the build/run happens in Task 6.
Steps 4-5 below verify only the field/reset logic by temporarily commenting the
`#include "ncland_sim.h"` line if you want an early check; otherwise proceed and
these tests are exercised in the Task 6 build.

- [ ] **Step 4: (optional early check) verify reset logic in isolation**

If checking now, temporarily comment out `#include "ncland_sim.h"` and the sim
tests that need it (none yet), build just this concern is deferred — the canonical
run is Task 6. Skip if working task-by-task with a subagent.

- [ ] **Step 5: Commit**

```bash
cd /home/dan/Git/support-netflex
git add cnc/ncland/src/ncland.h cnc/ncland/src/ncland_conn.cpp cnc/ncland/src/ncland_sim_tests.cpp
git commit -m "ncland: add conn_t sim fields (sim, sim_pid, sim_script) + init/reset"
```

---

## Task 2: `.clansim` script resolver (with testable root seam)

**Files:**
- Create: `cnc/ncland/src/ncland_sim.h`
- Create: `cnc/ncland/src/ncland_sim.cpp`
- Test: `cnc/ncland/src/ncland_sim_tests.cpp`

- [ ] **Step 1: Write the header**

Create `cnc/ncland/src/ncland_sim.h`:

```cpp
// ncland_sim.h — clan-style .clansim simulator transport (fork + PTY)
#ifndef NCLAND_SIM_H
#define NCLAND_SIM_H

#include "ncland.h"
#include <stddef.h>

/**
 * @brief Resolve a .clansim script for (neid, dtype) using clan's search order,
 *        rooted at caller-supplied dirs (testable seam).
 *
 * Search order, each checked with access(R_OK|X_OK):
 *   1. <data_dir>/ne<neid>.clansim
 *   2. <lib_dir>/<dtype>.clansim
 *   3. <lib_dir>/def.<dtype>.clansim
 *
 * @param data_dir Directory for the per-NE script (clan: /usr/cnc/data).
 * @param lib_dir  Directory for the per-dtype scripts (clan: /usr/cnc/lib/clan).
 * @param neid     Network-element id.
 * @param dtype    Device-type code.
 * @param out      Output buffer for the resolved path.
 * @param outsz    Size of @p out.
 * @return 1 and fills @p out on hit; 0 on miss (out[0] set to '\0').
 */
int ncland_sim_resolve_script_root(const char *data_dir, const char *lib_dir,
                                   int neid, int dtype, char *out, size_t outsz);

/**
 * @brief Resolve a .clansim script under clan's production roots
 *        (/usr/cnc/data and /usr/cnc/lib/clan).
 * @return 1 on hit (out filled), 0 on miss.
 */
int ncland_sim_resolve_script(int neid, int dtype, char *out, size_t outsz);

/**
 * @brief Fork+exec c->sim_script on a PTY (via inc_subproc), clan-style.
 *
 * On success: c->net_fd = PTY master (set non-blocking), c->sim_pid = child pid,
 * c->ssh = NULL, c->chan = NULL, c->state = CS_READY. No termios changes — the
 * PTY stays in its default cooked/echo mode to match legacy clan exactly.
 *
 * @param c Carrier with sim_script, neid populated.
 * @return 0 on success, -1 on failure (c->state = CS_DISCONNECTED).
 */
int ncland_sim_connect(conn_t *c);

/**
 * @brief Reap a spawned .clansim child and release its PTY fd.
 *
 * No-op when c->sim_pid <= 0. Otherwise kill(SIGTERM) + waitpid (bounded), close
 * net_fd, and clear sim/sim_pid/net_fd. Safe to call on any conn.
 *
 * @param c Connection (NULL-safe).
 */
void ncland_sim_disconnect(conn_t *c);

#endif /* NCLAND_SIM_H */
```

- [ ] **Step 2: Write the resolver implementation (only)**

Create `cnc/ncland/src/ncland_sim.cpp` with just the resolver for now:

```cpp
// ncland_sim.cpp — clan-style .clansim simulator transport (fork + PTY)
#include "ncland_sim.h"
#include "nflog.hpp"

#include <stdio.h>
#include <string.h>
#include <unistd.h>

/* Production roots, matching legacy clan (clan.c:1589-1605). */
#define NCLAND_SIM_DATA_DIR "/usr/cnc/data"
#define NCLAND_SIM_LIB_DIR  "/usr/cnc/lib/clan"

/**
 * @brief Return non-zero if @p path is present and executable (R_OK|X_OK).
 */
static int sim_path_ok(const char *path)
{
    return access(path, R_OK | X_OK) == 0;
}

int ncland_sim_resolve_script_root(const char *data_dir, const char *lib_dir,
                                   int neid, int dtype, char *out, size_t outsz)
{
    if (!out || outsz == 0)
        return 0;
    out[0] = '\0';

    /* 1. <data_dir>/ne<neid>.clansim */
    snprintf(out, outsz, "%s/ne%d.clansim", data_dir, neid);
    if (sim_path_ok(out))
        return 1;

    /* 2. <lib_dir>/<dtype>.clansim */
    snprintf(out, outsz, "%s/%d.clansim", lib_dir, dtype);
    if (sim_path_ok(out))
        return 1;

    /* 3. <lib_dir>/def.<dtype>.clansim */
    snprintf(out, outsz, "%s/def.%d.clansim", lib_dir, dtype);
    if (sim_path_ok(out))
        return 1;

    out[0] = '\0';
    return 0;
}

int ncland_sim_resolve_script(int neid, int dtype, char *out, size_t outsz)
{
    return ncland_sim_resolve_script_root(NCLAND_SIM_DATA_DIR, NCLAND_SIM_LIB_DIR,
                                          neid, dtype, out, outsz);
}
```

- [ ] **Step 3: Write the failing tests**

Append to `cnc/ncland/src/ncland_sim_tests.cpp`:

```cpp
static std::string sim_mk_tmpdir() {
    char tmpl[] = "/tmp/ncsim_XXXXXX";
    char *d = mkdtemp(tmpl);
    return d ? std::string(d) : std::string();
}
/* Create an executable stub script (content is irrelevant to the resolver). */
static void sim_put_exec(const std::string &path) {
    std::ofstream f(path); f << "#!/bin/sh\nexit 0\n"; f.close();
    chmod(path.c_str(), 0755);
}

TEST("sim", "S3 resolve prefers ne<neid> in data_dir") {
    std::string data = sim_mk_tmpdir();
    std::string lib  = sim_mk_tmpdir();
    REQUIRE(!data.empty()); REQUIRE(!lib.empty());
    sim_put_exec(data + "/ne7.clansim");
    sim_put_exec(lib  + "/33.clansim");        /* lower-priority also present */
    char out[256];
    int hit = ncland_sim_resolve_script_root(data.c_str(), lib.c_str(), 7, 33, out, sizeof(out));
    REQUIRE_EQ(hit, 1);
    REQUIRE(std::string(out) == data + "/ne7.clansim");
}

TEST("sim", "S4 resolve falls back to <dtype> then def.<dtype>") {
    std::string data = sim_mk_tmpdir();
    std::string lib  = sim_mk_tmpdir();
    char out[256];

    /* only <dtype>.clansim present */
    sim_put_exec(lib + "/33.clansim");
    REQUIRE_EQ(ncland_sim_resolve_script_root(data.c_str(), lib.c_str(), 7, 33, out, sizeof(out)), 1);
    REQUIRE(std::string(out) == lib + "/33.clansim");

    /* only def.<dtype>.clansim present */
    std::string lib2 = sim_mk_tmpdir();
    sim_put_exec(lib2 + "/def.33.clansim");
    REQUIRE_EQ(ncland_sim_resolve_script_root(data.c_str(), lib2.c_str(), 7, 33, out, sizeof(out)), 1);
    REQUIRE(std::string(out) == lib2 + "/def.33.clansim");
}

TEST("sim", "S5 resolve miss returns 0 and empties out") {
    std::string data = sim_mk_tmpdir();
    std::string lib  = sim_mk_tmpdir();
    char out[256];
    strcpy(out, "poison");
    REQUIRE_EQ(ncland_sim_resolve_script_root(data.c_str(), lib.c_str(), 7, 33, out, sizeof(out)), 0);
    REQUIRE_EQ(out[0], '\0');
}

TEST("sim", "S6 resolve requires executable bit (not just readable)") {
    std::string data = sim_mk_tmpdir();
    std::string lib  = sim_mk_tmpdir();
    /* readable but NOT executable -> must be skipped */
    std::string p = data + "/ne7.clansim";
    std::ofstream f(p); f << "#!/bin/sh\n"; f.close();
    chmod(p.c_str(), 0644);
    char out[256];
    REQUIRE_EQ(ncland_sim_resolve_script_root(data.c_str(), lib.c_str(), 7, 33, out, sizeof(out)), 0);
}
```

- [ ] **Step 4: Verify (build + run) — deferred to Task 6**

The unit-test binary links the whole `NCLAND_OBJS` set and requires the Makefile
change (Task 6). Do not attempt a standalone compile of one TU. The resolver tests
run green in Task 6 Step 3. Expected there: `S3`–`S6` PASS.

- [ ] **Step 5: Commit**

```bash
cd /home/dan/Git/support-netflex
git add cnc/ncland/src/ncland_sim.h cnc/ncland/src/ncland_sim.cpp cnc/ncland/src/ncland_sim_tests.cpp
git commit -m "ncland: add .clansim script resolver (clan search order) + tests"
```

---

## Task 3: `ncland_sim_disconnect` (reap child)

**Files:**
- Modify: `cnc/ncland/src/ncland_sim.cpp`
- Test: `cnc/ncland/src/ncland_sim_tests.cpp`

- [ ] **Step 1: Implement `ncland_sim_disconnect`**

Append to `cnc/ncland/src/ncland_sim.cpp`. Add these includes at the top with the
others first:

```cpp
#include <signal.h>
#include <sys/wait.h>
#include <errno.h>
```

Then the function:

```cpp
void ncland_sim_disconnect(conn_t *c)
{
    if (!c)
        return;

    if (c->sim_pid > 0) {
        NFTRACE(NF_D2, "sim: reaping .clansim child pid=%d neid=%d", c->sim_pid, c->neid);
        kill(c->sim_pid, SIGTERM);

        /* Bounded reap: poll for exit for up to ~2s, then give up so a wedged
         * child can never hang the warehouse. WNOHANG loop with short sleeps. */
        int waited_ms = 0;
        for (;;) {
            int st;
            pid_t r = waitpid(c->sim_pid, &st, WNOHANG);
            if (r == c->sim_pid || (r < 0 && errno == ECHILD))
                break;                       /* reaped, or not our child */
            if (waited_ms >= 2000) {
                kill(c->sim_pid, SIGKILL);   /* escalate; final reap below */
                waitpid(c->sim_pid, &st, 0);
                break;
            }
            usleep(50 * 1000);
            waited_ms += 50;
        }
        c->sim_pid = 0;
    }

    if (c->net_fd >= 0) {
        close(c->net_fd);
        c->net_fd = -1;
    }
    c->sim = 0;
}
```

- [ ] **Step 2: Write the failing tests**

Append to `cnc/ncland/src/ncland_sim_tests.cpp`:

```cpp
TEST("sim", "S7 disconnect is a no-op when sim_pid <= 0") {
    conn_t c;
    conn_init(&c, 1);
    /* net_fd == -1, sim_pid == 0 -> nothing to do, must not crash */
    ncland_sim_disconnect(&c);
    REQUIRE_EQ(c.sim_pid, 0);
    REQUIRE_EQ(c.net_fd, -1);
    ncland_sim_disconnect(NULL); /* NULL-safe */
}

TEST("sim", "S8 disconnect reaps a real child and closes fd") {
    /* Fork a child that just pauses; give the conn a live fd via a pipe. */
    int pfd[2];
    REQUIRE_EQ(pipe(pfd), 0);
    pid_t kid = fork();
    REQUIRE(kid >= 0);
    if (kid == 0) {
        /* child: block until killed */
        pause();
        _exit(0);
    }
    conn_t c;
    conn_init(&c, 2);
    c.sim = 1;
    c.sim_pid = kid;
    c.net_fd = pfd[0];   /* the fd disconnect will close */

    ncland_sim_disconnect(&c);

    REQUIRE_EQ(c.sim_pid, 0);
    REQUIRE_EQ(c.net_fd, -1);
    REQUIRE_EQ(c.sim, 0);
    /* fd really closed: fcntl now fails with EBADF */
    REQUIRE_EQ(fcntl(pfd[0], F_GETFD), -1);
    /* child really reaped: a second waitpid yields ECHILD */
    int st; pid_t r = waitpid(kid, &st, WNOHANG);
    REQUIRE(r < 0 && errno == ECHILD);
    close(pfd[1]);
}
```

- [ ] **Step 3: Verify — deferred to Task 6** (expected there: `S7`, `S8` PASS).

- [ ] **Step 4: Commit**

```bash
cd /home/dan/Git/support-netflex
git add cnc/ncland/src/ncland_sim.cpp cnc/ncland/src/ncland_sim_tests.cpp
git commit -m "ncland: add ncland_sim_disconnect (kill+reap child, close PTY) + tests"
```

---

## Task 4: `ncland_sim_connect` (fork .clansim on a PTY)

**Files:**
- Modify: `cnc/ncland/src/ncland_sim.cpp`
- Test: `cnc/ncland/src/ncland_sim_tests.cpp`

- [ ] **Step 1: Implement `ncland_sim_connect`**

`inc_subproc` has no header prototype (legacy clan relies on an implicit C
declaration, which is illegal in C++), so declare it ourselves. It is a variadic
C function that lives in `-linc` (`cnc/utility/src/misc2.c`). `Frmlnk` and
`GET_TID` come from the frame_link layer; guard against a NULL `Frmlnk` so the
function is crash-safe under unit tests.

Add near the top of `ncland_sim.cpp` (after the existing includes):

```cpp
#include <fcntl.h>
extern "C" {
#include <d1compool.h>            /* struct frame_link, GET_TID */
    int inc_subproc(int *info, char *path, ...);  /* misc2.c, -linc (no header) */
}

extern struct frame_link *Frmlnk;  /* defined in ncland.cpp / warehouse TU */
```

Then the function:

```cpp
int ncland_sim_connect(conn_t *c)
{
    if (!c || !c->sim_script[0]) {
        if (c) c->state = CS_DISCONNECTED;
        return -1;
    }

    char s_neid[24];
    snprintf(s_neid, sizeof(s_neid), "%d", c->neid);

    /* TID from frame_link, clan-style (clan.c:1610). Crash-safe if Frmlnk is
     * NULL (unit tests) or neid is unset. */
    const char *s_tid = "";
    if (Frmlnk && c->neid > 0) {
        const char *t = (const char *)GET_TID(&Frmlnk[c->neid]);
        if (t) s_tid = t;
    }

    int info[2] = { -1, -1 };
    /* argv passed to the script: {"clansim", <neid>, <tid>} — matches clan. */
    if (inc_subproc(info, c->sim_script, (char *)"clansim",
                    s_neid, (char *)s_tid, (char *)NULL) == -1) {
        NFTRACE(NF_D1, "sim: inc_subproc failed for %s (neid=%d)", c->sim_script, c->neid);
        c->state = CS_DISCONNECTED;
        return -1;
    }

    c->net_fd  = info[0];   /* PTY master */
    c->sim_pid = info[1];   /* child pid  */

    /* Non-blocking so the PTY master lives in the warehouse epoll like any fd.
     * NO termios changes: match clan — the PTY stays cooked/echo-on. */
    int fl = fcntl(c->net_fd, F_GETFL, 0);
    if (fl >= 0)
        fcntl(c->net_fd, F_SETFL, fl | O_NONBLOCK);

    c->ssh   = NULL;
    c->chan  = NULL;
    c->state = CS_READY;
    NFTRACE(NF_D2, "sim: spawned %s neid=%d pid=%d fd=%d",
            c->sim_script, c->neid, c->sim_pid, c->net_fd);
    return 0;
}
```

- [ ] **Step 2: Write the failing test**

Append to `cnc/ncland/src/ncland_sim_tests.cpp`. Uses a real executable stub that
prints a banner then blocks, so we can observe spawn + read + reap end-to-end.
`Frmlnk` is NULL in the test binary, so the tid guard keeps this crash-safe.

```cpp
TEST("sim", "S9 connect spawns script, sets fd/pid/state, reads banner") {
    std::string dir = sim_mk_tmpdir();
    REQUIRE(!dir.empty());
    std::string script = dir + "/ne1.clansim";
    {
        std::ofstream f(script);
        f << "#!/bin/sh\n"
             "echo READY\n"
             "sleep 30\n";
        f.close();
        chmod(script.c_str(), 0755);
    }

    conn_t c;
    conn_init(&c, 0);
    c.neid = 1;
    c.sim  = 1;
    strncpy(c.sim_script, script.c_str(), sizeof(c.sim_script) - 1);

    REQUIRE_EQ(ncland_sim_connect(&c), 0);
    REQUIRE(c.net_fd >= 0);
    REQUIRE(c.sim_pid > 0);
    REQUIRE(c.ssh == NULL);
    REQUIRE(c.chan == NULL);
    REQUIRE_EQ(c.state, CS_READY);
    /* fd is non-blocking */
    REQUIRE((fcntl(c.net_fd, F_GETFL) & O_NONBLOCK) != 0);

    /* Read the banner (poll briefly since the child needs a moment to exec). */
    char buf[64]; std::string got;
    for (int i = 0; i < 40 && got.find("READY") == std::string::npos; i++) {
        ssize_t n = read(c.net_fd, buf, sizeof(buf));
        if (n > 0) got.append(buf, (size_t)n);
        else usleep(50 * 1000);
    }
    REQUIRE(got.find("READY") != std::string::npos);

    ncland_sim_disconnect(&c);
    REQUIRE_EQ(c.sim_pid, 0);
    REQUIRE_EQ(c.net_fd, -1);
}

TEST("sim", "S10 connect fails cleanly with empty sim_script") {
    conn_t c;
    conn_init(&c, 0);
    c.sim = 1;                 /* sim_script left empty */
    REQUIRE_EQ(ncland_sim_connect(&c), -1);
    REQUIRE_EQ(c.state, CS_DISCONNECTED);
}
```

- [ ] **Step 3: Verify — deferred to Task 6** (expected there: `S9`, `S10` PASS).

- [ ] **Step 4: Commit**

```bash
cd /home/dan/Git/support-netflex
git add cnc/ncland/src/ncland_sim.cpp cnc/ncland/src/ncland_sim_tests.cpp
git commit -m "ncland: add ncland_sim_connect (fork .clansim on PTY via inc_subproc) + tests"
```

---

## Task 5: Wire dispatch + teardown reap

**Files:**
- Modify: `cnc/ncland/src/ncland_sim.cpp` (`ncland_sim_disconnect` — gate on `c->sim`)
- Modify: `cnc/ncland/src/ncland_sim_tests.cpp` (add guard test S11)
- Modify: `cnc/ncland/src/ncland_warehouse.cpp` (`warehouse_connect_cb` line 124; `warehouse_carrier_free` line 162)
- Modify: `cnc/ncland/src/ncland_conn.cpp` (`conn_free` line 82; add include)

> **Teardown-ordering hazard (fixed here — read first).** `ncland_ssh_disconnect`
> sets `c->net_fd = -1` **unconditionally** (libssh owns the ssh fd, so it does
> not `close()`), and `ncland_telnet_disconnect` only closes when `net_fd >= 0`.
> So if `ncland_sim_disconnect` runs *after* the ssh/telnet disconnects, `net_fd`
> is already `-1` and the **PTY master fd leaks** on every sim teardown.
> Conversely, the current `ncland_sim_disconnect` closes `net_fd` whenever it is
> `>= 0` — so calling it *first* on an **ssh** conn would `close()` the
> libssh-owned fd (double-close / use-after-free). The fix is BOTH: (a) make
> `ncland_sim_disconnect` a strict no-op on non-sim conns by gating on `c->sim`,
> and (b) call it **before** the ssh/telnet disconnects. Steps 1-2 do (a) + a
> guard test; Steps 4-5 do (b).

- [ ] **Step 1: Gate `ncland_sim_disconnect` on `c->sim`**

In `ncland_sim.cpp`, change the guard at the top of `ncland_sim_disconnect` so it
returns immediately for any non-sim conn (this makes it safe to call first, and a
true no-op for ssh/telnet slots whose `net_fd` it must never touch). Replace:

```cpp
    if (!c)
        return;
```
with:
```cpp
    /* Only ever act on sim conns. For ssh/telnet slots net_fd is owned by the
     * transport layer (libssh does not close it; telnet closes it itself), so a
     * non-sim conn must be left entirely untouched — see the teardown-ordering
     * note in Task 5. */
    if (!c || !c->sim)
        return;
```

The reap (`if (c->sim_pid > 0)`) and the `close(net_fd)` that follow are unchanged
— for a real sim conn `c->sim == 1`, so both still run.

- [ ] **Step 2: Add guard test S11 (non-sim conn is untouched)**

Append to `cnc/ncland/src/ncland_sim_tests.cpp`:

```cpp
TEST("sim", "S11 disconnect leaves a non-sim conn's fd untouched") {
    /* A telnet/ssh slot (sim == 0) must not have its net_fd closed by the sim
     * reaper — that fd is owned by the transport layer. */
    int pfd[2];
    REQUIRE_EQ(pipe(pfd), 0);
    conn_t c;
    conn_init(&c, 3);
    c.sim = 0;            /* NOT a sim conn */
    c.net_fd = pfd[0];    /* a live fd the sim reaper must leave alone */

    ncland_sim_disconnect(&c);

    REQUIRE_EQ(c.net_fd, pfd[0]);            /* fd field untouched */
    REQUIRE_NE(fcntl(pfd[0], F_GETFD), -1);  /* fd still open (no EBADF) */
    close(pfd[0]);
    close(pfd[1]);
}
```

Note: existing S7 still passes — `conn_init` leaves `sim == 0`, so the call is a
no-op and its assertions (`sim_pid == 0`, `net_fd == -1`) still hold. S8/S9 set
`c.sim = 1`, so their reap path is unaffected.

- [ ] **Step 3: Add the include to warehouse**

In `ncland_warehouse.cpp`, with the other `#include "ncland_*.h"` lines near the
top (after `#include "ncland_stepper.h"`), add:

```cpp
#include "ncland_sim.h"
```

- [ ] **Step 4: Branch the connect dispatch**

Replace the body of `warehouse_connect_cb` (line 124-129):

```cpp
static int warehouse_connect_cb(void *user, void *item)
{
    (void)user;
    conn_t *c = (conn_t *)item;
    if (c->sim)
        return ncland_sim_connect(c);
    return (c->ssh_flag == 1) ? ncland_ssh_connect(c) : ncland_telnet_connect(c);
}
```

- [ ] **Step 5: Reap on carrier teardown (BEFORE ssh/telnet)**

In `warehouse_carrier_free` (line 162), add the sim reap **before** the ssh/telnet
disconnects, so it still sees a valid `net_fd` to close (see the ordering note
above). Immediately after the `if (!c) return;` guard and before
`ncland_ssh_disconnect(c);` (line 165), add:

```cpp
    ncland_sim_disconnect(c);
```

- [ ] **Step 6: Reap on slot teardown (BEFORE ssh/telnet)**

In `ncland_conn.cpp`, add the include near the top (after `#include "nflog.hpp"`):

```cpp
#include "ncland_sim.h"
```

Then in `conn_free` (line 82), add the sim reap **before** the existing disconnect
calls — immediately before `ncland_ssh_disconnect(c);` (line 86):

```cpp
    ncland_sim_disconnect(c);
```

(Gated on `c->sim` from Step 1, this is a strict no-op for ssh/telnet slots, so
the ordering is safe for every conn type.)

- [ ] **Step 7: Commit**

```bash
cd /home/dan/Git/support-netflex
git add cnc/ncland/src/ncland_sim.cpp cnc/ncland/src/ncland_sim_tests.cpp \
        cnc/ncland/src/ncland_warehouse.cpp cnc/ncland/src/ncland_conn.cpp
git commit -m "ncland: dispatch sim connect in connect_cb; reap .clansim child first on teardown"
```

---

## Task 6: Trigger in open_conn_by_ne + Makefile + build + run tests

**Files:**
- Modify: `cnc/ncland/src/ncland_warehouse.cpp` (`warehouse_open_conn_by_ne` line 423)
- Modify: `cnc/ncland/src/Makefile`

- [ ] **Step 1: Add the sim-address branch in `warehouse_open_conn_by_ne`**

The sim decision goes in the **live path** (the carrier path), after the existing
`-X` skip block and the `g_warehouse_no_connect` test-path block, and after the
carrier is allocated and `ip`/`neid`/`dtype` are set on it. Concretely: in
`ncland_warehouse.cpp`, immediately after the carrier's `c->ipAddr` is populated
(after line 474 `c->ipAddr[sizeof(c->ipAddr) - 1] = '\0';`) and before the
registry lookup at line 476, insert:

```cpp
    /* Simulator address (not -X): spawn a clan-style .clansim child instead of
     * a network connect. Resolve the script now; if none exists, drop the open
     * (the legacy clansim TCP-daemon fallback is deferred with clansimd). */
    if (ncland_addr_is_simulator(ip)) {
        char script[sizeof(c->sim_script)];
        if (!ncland_sim_resolve_script(neid, dtype, script, sizeof(script))) {
            NFTRACE(NF_D1, "open: neid=%d dtype=%d addr=%s is a simulator but no "
                    ".clansim script found; dropping", neid, dtype, ip);
            warehouse_carrier_free(c);
            return -1;
        }
        c->sim = 1;
        strncpy(c->sim_script, script, sizeof(c->sim_script) - 1);
        c->sim_script[sizeof(c->sim_script) - 1] = '\0';
        NFTRACE(NF_D2, "open: neid=%d dtype=%d addr=%s -> sim script %s",
                neid, dtype, ip, c->sim_script);
        /* Skip the es64-credential lookup: the .clansim script handles its own
         * login dialogue. Apply registry (for prompts/steps) then submit. */
        const ne_entry_t *se = ncland_registry_find(&wh->registry, dtype);
        if (se) warehouse_apply_registry(c, se);
        c->state = CS_CONNECTING;
        connpool_submit(&wh->pool, c);
        return 0;
    }
```

Rationale for placement: the `-X` block (line 430) already returns early for sim
addresses when `skip_sim` is set, so this branch only runs when `-X` is off. It
sits before the credential/registry block so the es64 lookup (lines 480-527) is
skipped for sims. It reuses `warehouse_carrier_free` (drops the carrier + reaps
nothing, since `sim_pid` is still 0) on the no-script path.

- [ ] **Step 2: Update the Makefile (nmake — use the writing-nmake-makefiles skill)**

Two edits in `cnc/ncland/src/Makefile`:

(a) Add `ncland_sim.o` to `NCLAND_OBJS`. Change:

```
	ncland_registry.o ncland_connpool.o ncland_lua.o \
	ncland_router.o ncland_trace.o
```
to:
```
	ncland_registry.o ncland_connpool.o ncland_lua.o \
	ncland_router.o ncland_trace.o ncland_sim.o
```

(b) Add `ncland_sim_tests.o` to the `ncland_unit_tests` target's object list.
Change the start of that line:
```
ncland_unit_tests :: ncland_unit_tests.o ncland_notify_parse_tests.o ...
```
to include `ncland_sim_tests.o` (append it right after `ncland_unit_tests.o`):
```
ncland_unit_tests :: ncland_unit_tests.o ncland_sim_tests.o ncland_notify_parse_tests.o ...
```
Leave the rest of that recipe line unchanged. `ncland_sim.o` is pulled into the
test binary automatically via `$(NCLAND_OBJS)` already on that line.

- [ ] **Step 3: Build and run the unit tests**

First ensure BASE/VPATH are correct (see Prerequisites), then:

```bash
cd /home/dan/Git/support-netflex/cnc/ncland/src
nmake ncland_unit_tests && ./ncland_unit_tests
```
Expected: build succeeds; test run reports all suites passing, including the new
`sim` suite `S1`–`S10`. If the build fails on a missing `globaldefs.nmk` /
`global_*.nmk` / project header, **stop and tell the user** (do not hand-fix build
paths). If `inc_subproc` is an unresolved symbol at link time, the carrying lib is
not on the link line — report it; the expected fix is that `-linc` already
provides it (same lib clan uses), so a link failure means BASE/VPATH or the lib
set is wrong, not the code.

- [ ] **Step 4: Build the daemon itself**

```bash
cd /home/dan/Git/support-netflex/cnc/ncland/src
nmake
```
Expected: `ncland` (and the other targets: `nclan-seed`, `clansimd`,
`clansimctl`) link cleanly with `ncland_sim.o` included.

- [ ] **Step 5: Commit**

```bash
cd /home/dan/Git/support-netflex
git add cnc/ncland/src/ncland_warehouse.cpp cnc/ncland/src/Makefile
git commit -m "ncland: spawn .clansim on simulator-address opens; build ncland_sim"
```

---

## Task 7: Manual integration verification

**Files:** none (runtime verification on a box with a real `.clansim` script).

No code changes. Perform these checks and record results; they confirm the
end-to-end behavior the unit tests cannot (real seed → spawn → TL1 round-trip →
reap).

- [ ] **Step 1: Sim NE with a script present**

Seed/trigger an NE whose address is a simulator address (e.g. `127.0.0.1` or a
`*lansim*` host) and for which a matching `.clansim` script exists under
`/usr/cnc/data/ne<neid>.clansim` or `/usr/cnc/lib/clan/<dtype>.clansim`.

Expected (watch the trace at NF_D2): `open: ... -> sim script <path>`, then
`sim: spawned <path> ... pid=<n>`, then `transport up ... starting login`. Confirm
a TL1 command round-trips. `ps` shows the `.clansim` child while the session is up.

- [ ] **Step 2: Teardown reaps the child**

Drop the NE session (or stop ncland). Expected: `sim: reaping .clansim child
pid=<n>` in the trace, and `ps` shows the child is gone (no zombie / no orphan).

- [ ] **Step 3: Sim NE with no script**

Same as Step 1 but with no matching `.clansim` present. Expected: `open: ... is a
simulator but no .clansim script found; dropping`, no child spawned, no session.

- [ ] **Step 4: `-X` regression**

Start ncland with `-X` and a sim-address NE. Expected (unchanged behavior):
`skip: ... (simulator/loopback, -X)`, no resolve, no spawn.

- [ ] **Step 5: Echo watch (cooked-PTY known risk)**

During Step 1's login dialogue, watch for the login Lua matching its own echoed
command (cooked PTY echoes input). If observed, note it — the documented minimal
fix is clearing `ECHO` only on the PTY master; per the approved design we ship
cooked to match clan and only revisit if this actually occurs.

---

## Notes / accepted risk

- **Threaded fork:** `warehouse_connect_cb` runs on a connpool thread and
  `inc_subproc` does `fdopen`/`trace` between `fork` and `execv` — technically
  unsafe in a multithreaded process. Accepted (clan's exact mechanism; tiny
  window; pool threads normally blocked in `recv`, not `malloc`). Escape hatch if
  it ever manifests: replace the `inc_subproc` call with an async-signal-safe PTY
  spawner (open ptsname / dup2 / execv only). Not implemented now.
- **PTY mode:** intentionally cooked/echo-on to match legacy clan; no `termios`
  calls anywhere in the sim path.
