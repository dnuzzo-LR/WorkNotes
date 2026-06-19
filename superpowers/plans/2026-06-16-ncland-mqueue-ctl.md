# ncland #4 — POSIX mqueue Inbound + `qwrite()` Outbound Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace ncland's ZMQ ROUTER control boundary with a POSIX-mqueue inbound (`/ncland_ctl`) plus the existing `qwrite()` outbound to SysV `ScreenMsqid`. Retire `nclan-cmd`, the pending-stash table, and the `ncland_cmd_msg_t`/`ncland_rsp_msg_t` structs. Wire format becomes `struct dacs_msg` in both directions.

**Architecture:** Asymmetric transports: POSIX mq inbound (ncland is consumer, needs epoll), SysV outbound via the existing `-lutillib` `qwrite()` helper (ncland is producer, fan-out to screen). Single-process model and #3b stepper unchanged; this is purely an IPC boundary swap. Tests intercept via a link-time stub `qwrite` so unit tests don't need a live screen process.

**Tech Stack:** C++17 via Lucent/AT&T **nmake** (`Makefile`), POSIX `mqueue.h` (`-lrt`), SysV `qwrite` from `-lutillib`, `nfunit-test.hpp` (`./ncland_unit_tests`).

**Design doc:** `~/docs/superpowers/specs/2026-06-16-ncland-mqueue-ctl-design.md` (read it first).

---

## Critical context for the implementer (read first)

- **Build:** Lucent/AT&T **nmake**. From `cnc/ncland/src`: `nmake -f Makefile <target>` (the `Makefile` was previously `ncland.mk`; renamed by commit `ee34e6065`). Ensure `BASE = git rev-parse --show-toplevel` (`/home/dan/Git/netflex`) and `VPATH[0]` matches `BASE`. If a build fails on missing `global*.nmk` / project headers, STOP and report.
- **Tests:** `nmake -f Makefile ncland_unit_tests && ./ncland_unit_tests` — baseline **141 passed / 0 failed / 6 skipped**. `nfunit-test.hpp` API: `TEST("suite","name") { REQUIRE(...); REQUIRE_EQ(a,b); }`. Run one suite: `./ncland_unit_tests mq`.
- **Commit trailer (every commit):** `Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>`. Work on branch `ncland-start`; no new branches/worktrees.
- **Doxygen** on new/modified functions, structs, and macros.
- **POD discipline** for `conn_t` (memset by `conn_init`, memcpy by `warehouse_install_conn`). New `conn_t` fields MUST be POD. `struct dacs_msg` is POD (defined in `include/msgscreen.h`).
- **`-lutillib` already linked** (see `INCLIBS = $(CORELIBS) $(UTILLIB) -lnelib -llan -lsdi $(LIBCNCDB) -lcnc` in `Makefile`). `qwrite` is reachable; no link change needed for it.
- **Test isolation:** the daemon links real `qwrite` from `-lutillib`; the test binary will link a stub from `ncland_test_qwrite.cpp` instead. **Both definitions must NOT coexist in the same binary.** The test binary's NCLAND_OBJS list must include `ncland_test_qwrite.o` BUT must NOT pull `-lutillib`'s real qwrite. Look at how `-lutillib`'s qwrite is currently pulled — if it's a weak symbol or part of a non-required archive member, the test stub takes precedence; otherwise we need to extract a `_qwrite_overridable` indirection. **Verify this before assuming the stub overrides cleanly.** If it doesn't, fall back to a runtime `g_test_qwrite_intercept_enabled` flag inside the production `worker_send_rsp` that skips the real qwrite in tests.
- **Existing seam locations:** `g_test_last_rsp_result` and `g_test_last_sent_rsp_text` live in `ncland_worker.cpp` lines 81, 84. `g_test_last_direct_rsp_result` lives in `ncland_warehouse.cpp` (search for it). `g_test_worker_dispatched_seq` lives in `ncland_worker.cpp` line 78.
- **POSIX mqueue caveat:** `sizeof(struct dacs_msg)` is ~10.3 KB, but `/proc/sys/fs/mqueue/msgsize_max` defaults to 8192 on stock Linux. **For tests to pass on dev/CI hosts, raise the cap before running tests** (one-time sysctl: `sudo sysctl -w fs.mqueue.msgsize_max=16384`). The plan documents this; the implementer must apply it manually before Task 2's tests will pass.
- **The existing `Makefile` lists `nclan_cmd_test.o` in `ncland_unit_tests` prereqs.** Removal sequence: Task 7 deletes the file + Makefile rules together so the build never breaks mid-task.
- **#3b stepper still works** end-to-end — Task 4 renames `c->pending_rsp.result` → `.dm_reason` and `.text` → `.u.dm_text` and `.seq` → `.dm_tty`; all #3b/`I-sim` tests in `ncland_stepper_tests.cpp` get touched mechanically.

---

## File Structure

| File | Responsibility in #4 |
|------|----------------------|
| `ncland_mq.h` (new) | Public C API: `ncland_mq_init` / `_close` / `_drain`. |
| `ncland_mq.cpp` (new) | mq_open with attr, drain loop, mq_close + mq_unlink. |
| `ncland_mq_tests.cpp` (new) | New `mq` suite — 5 tests (init/close, drain dispatch, short-msg reject, EAGAIN). |
| `ncland_test_qwrite.cpp` (new) | Test-only stub for `qwrite` / `qopen` / `scrnq_overflow` that captures into the new seams. **Linked ONLY into `ncland_unit_tests`, never into the daemon.** |
| `ncland_zmq.cpp` | **Deleted** in Task 6. |
| `nclan_cmd.cpp` | **Deleted** in Task 7. |
| `ncland.h` | Add `wh->ctl_mqd` / `ctl_mq_name` fields, `conn_t::orig_tty`/`orig_old_tty`/`orig_slot`/`orig_slot_tp`. Delete `zmq_*` fields + `pending[]` + `NCLAND_MAX_PENDING` + `ncland_cmd_msg_t` + `ncland_rsp_msg_t`. Retype `conn_t::pending_rsp` to `struct dacs_msg`. New `g_test_*` seam decls. |
| `ncland_warehouse.cpp` | Wire `ncland_mq_init` + `qopen(1)` into `warehouse_init`; replace `zmq_ctl_fd` epoll branch with `ctl_mqd` branch; `warehouse_handle_cmd_from_zmq` → `warehouse_handle_dacs_msg`; delete `pending_stash`/`_take` and `g_test_last_direct_rsp_result`. |
| `ncland_worker.cpp` | Rewrite `worker_send_rsp` body to call `qwrite`; rewrite `handle_ne_data` rsp-construction site to build `struct dacs_msg`; rename `g_test_*` seams; retype `dispatch_cmd` signature. |
| `ncland_proto.cpp` | Retype `ncland_parse_cmd_data` signature: `(ncland_cmd_msg_t *)` → `(struct dacs_msg *)`; body operates on `m->u.dm_text`. |
| `ncland_stepper.cpp` | `c->pending_rsp.result` → `.dm_reason`, `.text` → `.u.dm_text`, `.seq` → `.dm_tty` in `step_finish` and `step_abort_for_eof`. |
| `ncland_unit_tests.cpp` | Delete pending-stash tests (T9.5/T9.6), wrong-msg_type test, check_errors-copy test, nclan_cmd parse test. Rewrite T9.7/T9.8 + T-3a unknown/busy/result tests for new seams + silent-drop semantics. Rename `g_test_last_rsp_result` references to `_reason`. |
| `ncland_stepper_tests.cpp` | Rename `g_test_last_sent_rsp_text` → `g_test_last_sent_dacs_text`, `.result` → `.dm_reason`, etc. Drop `g_test_last_direct_rsp_result` use; assert via log (or just drop the assertion). |
| `Makefile` | Drop `ncland_zmq.o` and `nclan_cmd.o` from `NCLAND_OBJS`; drop `nclan_cmd_test.o` prereq + `nclan-cmd` target + `nclan_cmd_test.o` rule. Add `ncland_mq.o` to `NCLAND_OBJS`. Add `ncland_mq_tests.o` + `ncland_test_qwrite.o` to `ncland_unit_tests` prereqs. Add `-lrt` to the daemon link line (and unit-test link line). |

Tasks are sequential. Each builds and stays green on its own.

---

## Task 1: Foundations — new fields, new seams, build wiring, qwrite stub, mq module skeleton

**Files:** `ncland.h`, `ncland_conn.cpp`, `ncland_warehouse.cpp`, `ncland_mq.h` (new), `ncland_mq.cpp` (new), `ncland_test_qwrite.cpp` (new), `Makefile`

- [ ] **Step 1: Add new test-seam externs to `ncland.h`** (near the existing `g_test_*` extern block):

```c
extern int  g_test_last_rsp_reason;                    /**< dm_reason of most recent worker_send_rsp (#4) */
extern char g_test_last_sent_dacs_text[MAXDACSMSG+1]; /**< dm_text of most recent worker_send_rsp (#4) */
extern int  g_test_worker_dispatched_dacsid;          /**< dm_dacsid of most recent dispatch (#4) */
```

Do NOT remove the old seams yet — keep `g_test_last_rsp_result`, `g_test_last_sent_rsp_text`, `g_test_worker_dispatched_seq`, `g_test_last_direct_rsp_result` in place for now (existing tests still reference them; cutover happens in Tasks 4-5).

- [ ] **Step 2: Add new `conn_t` fields** in `ncland.h` (after `pending_rsp`):

```c
    long    orig_tty;        /**< dm_tty of the inbound cmd that owns this conn now (#4). */
    int     orig_old_tty;    /**< dm_old_tty echoed back to screen (#4). */
    int     orig_slot;       /**< dm_slot echoed back (#4). */
    int     orig_slot_tp;    /**< dm_slot_tp echoed back (#4). */
```

- [ ] **Step 3: Add new `ncland_wh_t` fields** in `ncland.h` (after the existing `notify_fd`):

```c
    mqd_t   ctl_mqd;          /**< POSIX mq for inbound; (mqd_t)-1 when not open (#4). */
    char    ctl_mq_name[64];  /**< Queue name for mq_unlink at shutdown (#4). */
```

Add `#include <mqueue.h>` to `ncland.h` (or, if the project prefers minimal headers in `ncland.h`, forward-declare `mqd_t` via `typedef int mqd_t;` — but `<mqueue.h>` is cleaner).

- [ ] **Step 4: Init the new wh fields in `warehouse_init`** (`ncland_warehouse.cpp`). After the existing field zeroing:

```c
    wh->ctl_mqd = (mqd_t)-1;
    wh->ctl_mq_name[0] = '\0';
```

The `conn_t` POD fields are zeroed automatically by `conn_init`'s memset; no per-field assignment needed.

- [ ] **Step 5: Create `ncland_mq.h`** with skeleton decls:

```c
/** @file ncland_mq.h
 *  POSIX mqueue inbound for ncland's control boundary (#4).
 *  Replaces the ZMQ ROUTER ctl path. */
#pragma once
#include "ncland.h"

/** @brief Open and bind /<queue_name>; stash mqd in wh->ctl_mqd.
 *  Returns 0 on success, -1 on failure (logged). */
int  ncland_mq_init(ncland_wh_t *wh, const char *queue_name);

/** @brief mq_close + mq_unlink; safe on (mqd_t)-1. Idempotent. */
void ncland_mq_close(ncland_wh_t *wh);

/** @brief Drain available cmds: mq_receive loop until EAGAIN; dispatch each via
 *  warehouse_handle_dacs_msg. Returns 0 on clean drain, -1 on error. */
int  ncland_mq_drain(ncland_wh_t *wh);
```

- [ ] **Step 6: Create `ncland_mq.cpp` skeleton** — empty function bodies that just return:

```c
/** @file ncland_mq.cpp — see ~/docs/superpowers/specs/2026-06-16-ncland-mqueue-ctl-design.md */
#include "ncland_mq.h"
#include "nflog.hpp"
#include <mqueue.h>
#include <fcntl.h>
#include <errno.h>
#include <string.h>

int ncland_mq_init(ncland_wh_t *wh, const char *queue_name)
{
    (void)wh; (void)queue_name;
    return -1;   /* Task 2 implements */
}

void ncland_mq_close(ncland_wh_t *wh)
{
    (void)wh;    /* Task 2 implements */
}

int ncland_mq_drain(ncland_wh_t *wh)
{
    (void)wh;
    return -1;   /* Task 3 implements */
}
```

- [ ] **Step 7: Create `ncland_test_qwrite.cpp`** — the test-only stub that captures into the new seams:

```cpp
/** @file ncland_test_qwrite.cpp
 *  Test-only stubs for qwrite/qopen/scrnq_overflow (-lutillib equivalents).
 *  Captures rsp payload into g_test_last_sent_dacs_text / g_test_last_rsp_reason
 *  so unit tests can verify ncland's outbound without a live screen process.
 *  Linked into ncland_unit_tests ONLY — never into the daemon binary. */
#include "ncland.h"

#include <msgscreen.h>   /* struct dacs_msg */
#include <stdio.h>
#include <string.h>

/* Seam storage — defined here so the test binary has exactly one copy. */
int  g_test_last_rsp_reason            = 0;
char g_test_last_sent_dacs_text[MAXDACSMSG+1] = {0};
int  g_test_worker_dispatched_dacsid   = 0;

extern "C" int qwrite(struct dacs_msg *m)
{
    if (!m) return -1;
    snprintf(g_test_last_sent_dacs_text, sizeof(g_test_last_sent_dacs_text),
             "%s", m->u.dm_text);
    g_test_last_rsp_reason = (int)m->dm_reason;
    return 0;   /* SUCCESS */
}

extern "C" int scrnq_overflow(void) { return 1; /* FAILURE = no overflow */ }
extern "C" int qopen(int screen)    { (void)screen; return 0; /* SUCCESS */ }
```

`SUCCESS` and `FAILURE` come from `<flags.h>` (already included via `ncland.h` chain). If not, add `#include <flags.h>` — verify with a quick `grep -r "GOT_ONE" /home/dan/Git/netflex/cnc/ncland/src/`.

- [ ] **Step 8: Wire into `Makefile`.** Three edits:

(a) Add `ncland_mq.o` to `NCLAND_OBJS`:
```
NCLAND_OBJS = ncland_conn.o ncland_proto.o ncland_ssh.o \
	ncland_warehouse.o ncland_worker.o ncland_stepper.o ncland_telnet.o \
	ncland_zmq.o ncland_mq.o ncland_seed.o ncland_notify.o ncland_notify_parse.o \
	ncland_registry.o ncland_connpool.o ncland_lua.o
```

(b) Add `-lrt` to the daemon link line (after `-llua`):
```
$(PBIN)/ncland :: ncland.o $(NCLAND_OBJS) $(INCLIBS) -latm -linc -lxmlparse -lssh -ljson -lnetclan
	$(CPLUS_CC) $(CCFLAGS:M!=-AC99) $(LDFLAGS) -o $(<) $(*) -lpthread -lssh -lzmq -lyaml-cpp -llua -lrt
```

(c) Add `ncland_mq_tests.o` + `ncland_test_qwrite.o` to the `ncland_unit_tests` prereq list, and `-lrt` to the test link:
```
ncland_unit_tests :: ncland_unit_tests.o ncland_notify_parse_tests.o nclan_seed_fmt.o nclan_seed_tests.o ncland_registry_tests.o ncland_connpool_tests.o ncland_lua_tests.o ncland_stepper_tests.o ncland_mq_tests.o nclan_cmd_test.o ncland_test_qwrite.o $(NCLAND_OBJS) $(INCLIBS) -latm -linc -lxmlparse -lssh -ljson -lnetclan
	$(CPLUS_CC) $(CCFLAGS:M!=-AC99) $(LDFLAGS) -o $(<) $(*) -lpthread -lssh -lzmq -lyaml-cpp -llua -lrt
```

(`ncland_mq_tests.o` will be created in Task 2; for now it doesn't exist — that's fine, the test binary won't link successfully until Task 2 creates it. **Until Task 2 ships, the unit test build is broken.** Make sure Task 1 ends with a build of just the daemon target — `nmake -f Makefile $(PBIN)/ncland` — green, and the unit_tests build INTENTIONALLY not run.)

Alternatively: in Task 1 we can create an empty `ncland_mq_tests.cpp` with no `TEST` macros, so the build is green. **Recommended:** create the empty file now to keep tests passing.

- [ ] **Step 9: Create empty `ncland_mq_tests.cpp`** so the unit-test target still links:

```cpp
/** @file ncland_mq_tests.cpp — mq suite tests. Body lands in Tasks 2-3. */
#include "ncland.h"
#include "ncland_mq.h"
#include "nfunit-test.hpp"
```

- [ ] **Step 10: Build + verify suite stays green.** Run:

```bash
cd /home/dan/Git/netflex/cnc/ncland/src
nmake -f Makefile ncland_unit_tests
./ncland_unit_tests
```

Expected: builds clean. Suite still **141 passed / 0 failed / 6 skipped**.

**Potential pitfall — qwrite symbol collision.** The test binary now contains:
- The real `qwrite` from `-lutillib`
- Our stub `qwrite` from `ncland_test_qwrite.o`

If the linker reports a multiple-definition error, the simplest fix is to ensure `ncland_test_qwrite.o` is listed in the prereqs BEFORE `-lutillib` in the link line — the linker resolves user-supplied symbols first and skips the library copy. The order shown in Step 8(c) places `ncland_test_qwrite.o` ahead of `$(INCLIBS)` (which contains `-lutillib`), so this should work. If it doesn't, fall back to adding `--allow-multiple-definition` to `LDFLAGS` (less ideal) or extracting the qwrite override into a wrapper inside `worker_send_rsp` (see Task 4 backup plan).

- [ ] **Step 11: Commit**

```bash
cd /home/dan/Git/netflex
git add cnc/ncland/src/ncland.h cnc/ncland/src/ncland_warehouse.cpp \
        cnc/ncland/src/ncland_mq.h cnc/ncland/src/ncland_mq.cpp \
        cnc/ncland/src/ncland_test_qwrite.cpp cnc/ncland/src/ncland_mq_tests.cpp \
        cnc/ncland/src/Makefile
git commit -m "$(cat <<'EOF'
[ncland] #4 foundations: new fields, seams, mq skeleton, qwrite test stub

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 2: `ncland_mq_init` + `_close` real implementation + wire into `warehouse_init`/`shutdown`

**Files:** `ncland_mq.cpp`, `ncland_warehouse.cpp`, `ncland_mq_tests.cpp`

- [ ] **Step 1: Write the failing tests** (`ncland_mq_tests.cpp`):

```cpp
TEST("mq", "M-mq init creates the queue and close + unlink it") {
    ncland_wh_t wh{};
    wh.ctl_mqd = (mqd_t)-1;
    const char *name = "/ncland-test-init-XYZ";
    REQUIRE_EQ(ncland_mq_init(&wh, name), 0);
    REQUIRE(wh.ctl_mqd != (mqd_t)-1);
    REQUIRE_EQ(strcmp(wh.ctl_mq_name, name), 0);
    /* Open a second handle to verify the queue exists. */
    mqd_t check = mq_open(name, O_RDONLY | O_NONBLOCK);
    REQUIRE(check != (mqd_t)-1);
    mq_close(check);

    ncland_mq_close(&wh);
    REQUIRE_EQ(wh.ctl_mqd, (mqd_t)-1);
    REQUIRE_EQ(wh.ctl_mq_name[0], '\0');

    /* mq_unlink already done — the queue should not be openable now. */
    mqd_t check2 = mq_open(name, O_RDONLY | O_NONBLOCK);
    REQUIRE(check2 == (mqd_t)-1);   /* ENOENT */
}

TEST("mq", "M-mq close is idempotent + NULL-safe") {
    nclansim_close_does_not_apply: ;
    ncland_mq_close(NULL);   /* must not crash */

    ncland_wh_t wh{};
    wh.ctl_mqd = (mqd_t)-1;
    wh.ctl_mq_name[0] = '\0';
    ncland_mq_close(&wh);    /* nothing to close */
    REQUIRE_EQ(wh.ctl_mqd, (mqd_t)-1);
}
```

The first test references `mq_open`; add `#include <mqueue.h>` and `#include <fcntl.h>` to `ncland_mq_tests.cpp`.

- [ ] **Step 2: Run (fails)** — `ncland_mq_init` still returns -1.

```bash
nmake -f Makefile ncland_unit_tests && ./ncland_unit_tests mq
```

Expected: 2 tests fail.

- [ ] **Step 3: Implement `ncland_mq_init`** in `ncland_mq.cpp`:

```c
int ncland_mq_init(ncland_wh_t *wh, const char *queue_name)
{
    if (!wh || !queue_name || queue_name[0] != '/') {
        LOG_WARN("ncland_mq_init: bad args (wh=%p name=%s)",
                 (void*)wh, queue_name ? queue_name : "(null)");
        return -1;
    }
    if (wh->ctl_mqd != (mqd_t)-1) {
        LOG_WARN("ncland_mq_init: already open (mqd=%d)", (int)wh->ctl_mqd);
        return -1;
    }

    struct mq_attr attr = {};
    attr.mq_maxmsg  = 10;
    attr.mq_msgsize = sizeof(struct dacs_msg);
    attr.mq_flags   = 0;

    snprintf(wh->ctl_mq_name, sizeof(wh->ctl_mq_name), "%s", queue_name);
    wh->ctl_mqd = mq_open(wh->ctl_mq_name,
                          O_CREAT | O_RDONLY | O_NONBLOCK,
                          0660, &attr);
    if (wh->ctl_mqd == (mqd_t)-1) {
        if (errno == EINVAL)
            LOG_ERROR("ncland_mq_init: mq_open(%s) EINVAL — likely "
                      "fs.mqueue.msgsize_max < %zu",
                      wh->ctl_mq_name, sizeof(struct dacs_msg));
        else
            LOG_ERROR("ncland_mq_init: mq_open(%s) failed: %s",
                      wh->ctl_mq_name, strerror(errno));
        wh->ctl_mq_name[0] = '\0';
        return -1;
    }
    LOG_INFO("ncland_mq_init: opened %s (mqd=%d, msgsize=%zu)",
             wh->ctl_mq_name, (int)wh->ctl_mqd, sizeof(struct dacs_msg));
    return 0;
}
```

- [ ] **Step 4: Implement `ncland_mq_close`** in `ncland_mq.cpp`:

```c
void ncland_mq_close(ncland_wh_t *wh)
{
    if (!wh) return;
    if (wh->ctl_mqd != (mqd_t)-1) {
        mq_close(wh->ctl_mqd);
        wh->ctl_mqd = (mqd_t)-1;
    }
    if (wh->ctl_mq_name[0]) {
        if (mq_unlink(wh->ctl_mq_name) != 0 && errno != ENOENT)
            LOG_DEBUG("ncland_mq_close: mq_unlink(%s): %s",
                      wh->ctl_mq_name, strerror(errno));
        wh->ctl_mq_name[0] = '\0';
    }
}
```

- [ ] **Step 5: Wire into `warehouse_init`** in `ncland_warehouse.cpp`. After the existing `ncland_zmq_init` call (we add the mq alongside zmq for now; zmq removal is Task 6):

```c
    if (ncland_mq_init(wh, "/ncland_ctl") != 0) {
        LOG_ERROR("warehouse_init: ncland_mq_init failed");
        /* Continue — zmq path still works during transition. */
    }
```

- [ ] **Step 6: Wire into `warehouse_shutdown`** in `ncland_warehouse.cpp`. After existing `ncland_zmq_close`:

```c
    ncland_mq_close(wh);
```

- [ ] **Step 7: Run tests (pass).** **Before running, verify sysctl is set:**

```bash
cat /proc/sys/fs/mqueue/msgsize_max   # must be >= 10248 (sizeof dacs_msg)
# If < 10248:
sudo sysctl -w fs.mqueue.msgsize_max=16384
```

Then:
```bash
nmake -f Makefile ncland_unit_tests && ./ncland_unit_tests mq
```

Expected: 2 mq tests pass. Full suite: **143 passed, 0 failed, 6 skipped**.

If `mq_open` returns EINVAL even after sysctl, double-check that sysctl was applied to the current namespace (`sudo sysctl fs.mqueue.msgsize_max` to read back). Containers may need the sysctl applied on the host or via `--sysctl fs.mqueue.msgsize_max=16384`.

- [ ] **Step 8: Commit**

```bash
cd /home/dan/Git/netflex
git add cnc/ncland/src/ncland_mq.cpp cnc/ncland/src/ncland_warehouse.cpp cnc/ncland/src/ncland_mq_tests.cpp
git commit -m "$(cat <<'EOF'
[ncland] #4 mq_init/_close: open /ncland_ctl, unlink at shutdown

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 3: `ncland_mq_drain` + `warehouse_handle_dacs_msg` stub + main-loop epoll branch

**Files:** `ncland_mq.cpp`, `ncland_warehouse.cpp`, `ncland_mq_tests.cpp`

The stub `warehouse_handle_dacs_msg` only logs and increments `g_test_worker_dispatched_dacsid`. The full body (with NE lookup, dispatch_cmd, etc.) lands in Task 5 once the dispatch_cmd signature is changed.

- [ ] **Step 1: Add the dispatch decl** in `ncland_warehouse.cpp` (and corresponding extern in `ncland.h` near the warehouse decl block):

`ncland.h`:
```c
/** @brief Dispatch one inbound dacs_msg: look up neid, set orig_*, run dispatch_cmd.
 *  Task 3 ships a stub; Task 5 ships the full body. */
void warehouse_handle_dacs_msg(ncland_wh_t *wh, struct dacs_msg *m);
```

`ncland_warehouse.cpp` (stub — placed near `warehouse_handle_cmd_from_zmq`):
```c
void warehouse_handle_dacs_msg(ncland_wh_t *wh, struct dacs_msg *m)
{
    (void)wh;
    if (!m) return;
    g_test_worker_dispatched_dacsid = (int)m->dm_dacsid;
    LOG_INFO("warehouse_handle_dacs_msg: neid=%u tty=%ld dm_text='%s' (stub)",
             m->dm_dacsid, m->dm_tty, m->u.dm_text);
}
```

- [ ] **Step 2: Write the failing tests** (`ncland_mq_tests.cpp`):

```cpp
TEST("mq", "M-mq drain dispatches a well-formed dacs_msg") {
    ncland_wh_t wh{}; wh.ctl_mqd = (mqd_t)-1;
    const char *name = "/ncland-test-drain-1";
    REQUIRE_EQ(ncland_mq_init(&wh, name), 0);
    /* Open a sender handle. */
    mqd_t snd = mq_open(name, O_WRONLY | O_NONBLOCK);
    REQUIRE(snd != (mqd_t)-1);

    struct dacs_msg m = {};
    m.dm_dacsid = 4242;
    m.dm_tty    = 7;
    snprintf(m.u.dm_text, sizeof(m.u.dm_text), "tag:30:0:0:show version");
    REQUIRE_EQ(mq_send(snd, (const char*)&m, sizeof(m), 0), 0);
    mq_close(snd);

    g_test_worker_dispatched_dacsid = 0;
    REQUIRE_EQ(ncland_mq_drain(&wh), 0);
    REQUIRE_EQ(g_test_worker_dispatched_dacsid, 4242);

    ncland_mq_close(&wh);
}

TEST("mq", "M-mq drain returns 0 on EAGAIN (no messages)") {
    ncland_wh_t wh{}; wh.ctl_mqd = (mqd_t)-1;
    REQUIRE_EQ(ncland_mq_init(&wh, "/ncland-test-drain-empty"), 0);
    REQUIRE_EQ(ncland_mq_drain(&wh), 0);   /* nothing to receive */
    ncland_mq_close(&wh);
}

TEST("mq", "M-mq drain rejects short message") {
    ncland_wh_t wh{}; wh.ctl_mqd = (mqd_t)-1;
    REQUIRE_EQ(ncland_mq_init(&wh, "/ncland-test-drain-short"), 0);
    mqd_t snd = mq_open("/ncland-test-drain-short", O_WRONLY | O_NONBLOCK);
    REQUIRE(snd != (mqd_t)-1);

    /* Send something shorter than DACS_MSG_HDR_SZ. */
    char tiny[8] = {0};
    REQUIRE_EQ(mq_send(snd, tiny, sizeof(tiny), 0), 0);
    mq_close(snd);

    g_test_worker_dispatched_dacsid = 0;
    REQUIRE_EQ(ncland_mq_drain(&wh), 0);   /* drains cleanly, dropped one msg */
    REQUIRE_EQ(g_test_worker_dispatched_dacsid, 0);  /* nothing dispatched */
    ncland_mq_close(&wh);
}
```

- [ ] **Step 3: Run (fails)** — `ncland_mq_drain` still returns -1.

- [ ] **Step 4: Implement `ncland_mq_drain`** in `ncland_mq.cpp`:

```c
extern void warehouse_handle_dacs_msg(ncland_wh_t *wh, struct dacs_msg *m);

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
                     (ssize_t)n, (size_t)DACS_MSG_HDR_SZ);
            continue;
        }
        /* NUL-terminate dm_text based on actual size. */
        if ((size_t)n < sizeof(m)) {
            size_t text_off = (size_t)((char*)&m.u.dm_text - (char*)&m);
            if ((size_t)n > text_off)
                m.u.dm_text[(size_t)n - text_off] = '\0';
            else
                m.u.dm_text[0] = '\0';
        }
        warehouse_handle_dacs_msg(wh, &m);
    }
}
```

`DACS_MSG_HDR_SZ` comes from `msgscreen.h` (already included via `ncland.h`).

- [ ] **Step 5: Wire main-loop epoll branch** in `ncland_warehouse.cpp` (in `warehouse_main_loop`). Add the `ctl_mqd` registration AFTER the existing `zmq_ctl_fd` registration:

```c
    if (wh->ctl_mqd != (mqd_t)-1) {
        struct epoll_event ev{};
        ev.events = EPOLLIN;
        ev.data.fd = wh->ctl_mqd;
        if (epoll_ctl(epfd, EPOLL_CTL_ADD, wh->ctl_mqd, &ev) < 0) {
            LOG_ERROR("warehouse_main_loop: epoll_ctl ctl_mqd: %s", strerror(errno));
        }
    }
```

And the dispatch branch in the main loop's event handler (after the existing `zmq_ctl_fd` else-if):

```c
            } else if (wh->ctl_mqd != (mqd_t)-1 && fd == wh->ctl_mqd) {
                if (ncland_mq_drain(wh) < 0)
                    LOG_WARN("warehouse_main_loop: ncland_mq_drain failed");
            }
```

- [ ] **Step 6: Run tests (pass).** Full suite: **146 passed, 0 failed, 6 skipped**.

- [ ] **Step 7: Commit**

```bash
cd /home/dan/Git/netflex
git add cnc/ncland/src/ncland_mq.cpp cnc/ncland/src/ncland_warehouse.cpp cnc/ncland/src/ncland_mq_tests.cpp cnc/ncland/src/ncland.h
git commit -m "$(cat <<'EOF'
[ncland] #4 mq_drain: receive loop, short-msg reject, dispatch stub + epoll branch

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 4: Outbound cutover — `worker_send_rsp(dacs_msg)` + `conn_t.pending_rsp` retype + `handle_ne_data` construction + stepper field renames

**Files:** `ncland.h`, `ncland_worker.cpp`, `ncland_stepper.cpp`, `ncland_stepper_tests.cpp`, `ncland_unit_tests.cpp`

This is the largest task. The change is atomic: `pending_rsp`'s type changes simultaneously with `worker_send_rsp`'s signature, the construction site in `handle_ne_data`, and the stepper's reads of `pending_rsp` fields. All affected tests get rewritten in lockstep.

- [ ] **Step 1: Retype `conn_t::pending_rsp`** in `ncland.h`. Replace:

```c
    ncland_rsp_msg_t pending_rsp;    /* old */
```

with:

```c
    struct dacs_msg  pending_rsp;    /**< Stashed at prompt-match; shipped via qwrite after post_command (#3b→#4). */
```

`struct dacs_msg` comes from `msgscreen.h`; ensure it's reachable.

Do NOT delete `ncland_cmd_msg_t` or `ncland_rsp_msg_t` typedefs yet — they're still referenced by old code paths (inbound dispatch). Task 6 deletes them.

- [ ] **Step 2: Rewrite `worker_send_rsp` in `ncland_worker.cpp`.** Replace the existing body entirely:

```c
extern "C" {
#include <flags.h>      /* SUCCESS / FAILURE */
int qwrite(struct dacs_msg *inmsg);
int scrnq_overflow(void);
}

/**
 * @brief Send a response to screen via qwrite (-lutillib).
 *
 * Captures the rsp into test seams before qwrite. Skips qwrite if the screen
 * queue is overflowing.
 *
 * @param wh  Warehouse (reserved).
 * @param m   Outbound dacs_msg; dm_to_screen must be set by the caller.
 * @return 0 on success; -1 on qwrite failure or screen overflow.
 */
int worker_send_rsp(ncland_wh_t *wh, struct dacs_msg *m)
{
    (void)wh;
    if (!m) return -1;

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

The seam definitions `g_test_last_sent_dacs_text` and `g_test_last_rsp_reason` live in `ncland_test_qwrite.cpp` (test binary) AND must have a production definition too (the daemon binary doesn't link `ncland_test_qwrite.o`). Add minimal production definitions at the top of `ncland_worker.cpp`:

```c
#ifndef NCLAND_UNIT_TEST_QWRITE
int  g_test_last_rsp_reason            = 0;
char g_test_last_sent_dacs_text[MAXDACSMSG+1] = {0};
int  g_test_worker_dispatched_dacsid   = 0;
#endif
```

Then in `ncland_test_qwrite.cpp`, wrap its definitions with `#define NCLAND_UNIT_TEST_QWRITE` BEFORE including `ncland.h`:

```cpp
#define NCLAND_UNIT_TEST_QWRITE
#include "ncland.h"
/* … rest of file from Task 1 unchanged … */
```

This guarantees exactly one definition per binary.

Delete the old `g_test_last_rsp_result` and `g_test_last_sent_rsp_text` definitions from `ncland_worker.cpp` (lines 81, 84) — they're being replaced. Also delete the externs from `ncland.h`.

- [ ] **Step 3: Rewrite the rsp-construction site in `handle_ne_data`** (`ncland_worker.cpp`). Find the existing block that builds an `ncland_rsp_msg_t rsp;` after a successful prompt match (likely around line 330-360 based on the earlier code review). Replace it with:

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

        /* #3a errors[] scan — verdict goes into dm_reason. */
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

        /* #3b post_command fork OR ship now. */
        if (e && e->post_command && e->post_command.IsSequence() && e->post_command.size() > 0) {
            c->pending_rsp = out;
            c->step_block  = STEP_BLOCK_POST_CMD;
            c->step_idx    = 0;
            c->step_failed = 0;
            c->rlen = 0; c->rbuf[0] = '\0';
            conn_set_state(c, CS_POST_CMD);
            ncland_step_advance(c, wh);
            return;
        }
        worker_send_rsp(wh, &out);
        c->rlen = 0; memset(c->rbuf, 0, sizeof(c->rbuf));
        conn_set_state(c, CS_READY);
        return;
```

Delete the old `ncland_rsp_msg_t rsp;` block entirely. The new code drops `c->check_errors` (always scans) and drops the `g_test_last_rsp_result = rsp.result` line (the new seam fires inside `worker_send_rsp` for the no-post_command path; for the post_command path, the seam fires when `step_finish` calls `worker_send_rsp`).

- [ ] **Step 4: Rename stepper field accesses** in `ncland_stepper.cpp`:

In `ncland_step_finish` (the POST_CMD branch):

```c
    if (which == STEP_BLOCK_POST_CMD) {
        if (c->step_failed) c->pending_rsp.dm_reason = NCLAND_RESULT_FAILURE;
        worker_send_rsp(wh, &c->pending_rsp);
        c->pending_rsp.dm_tty = 0;        /* freshness sentinel (was: seq = 0) */
        c->rlen = 0; c->rbuf[0] = '\0';
        c->step_failed = 0;
        conn_set_state(c, CS_READY);
        return;
    }
```

In `ncland_step_abort_for_eof`:

```c
    if (c->step_block == STEP_BLOCK_POST_CMD) {
        snprintf(c->pending_rsp.u.dm_text, sizeof(c->pending_rsp.u.dm_text),
                 "NE disconnected mid-post_command");
        c->pending_rsp.dm_reason = NCLAND_RESULT_FAILURE;
        worker_send_rsp(wh, &c->pending_rsp);
        memset(&c->pending_rsp, 0, sizeof(c->pending_rsp));
    }
```

- [ ] **Step 5: Rewrite stepper tests** in `ncland_stepper_tests.cpp`. Every reference to `g_test_last_sent_rsp_text` → `g_test_last_sent_dacs_text`. Every `c->pending_rsp.result` → `.dm_reason`. Every `c->pending_rsp.seq` → `.dm_tty`. Every `c->pending_rsp.text` → `.u.dm_text`. Add the new extern decl at the top of the test file:

```cpp
extern char g_test_last_sent_dacs_text[];
extern int  g_test_last_rsp_reason;
```

And delete the old externs.

The specific tests to touch (lines per grep):
- Line 96: `g_test_last_sent_rsp_text` → `g_test_last_sent_dacs_text`
- Line 327-328: same rename
- Line 191: `extern int g_test_last_direct_rsp_result;` — delete this extern; the seam is gone.
- Line 240-242: the test that uses `g_test_last_direct_rsp_result` — REWRITE: this test (`W-3b shutting_down rejects new cmds with FAILURE`, line 230) needs to assert "silently dropped" instead. Replace the body's FAILURE-seam check with a check that the conn state did NOT change (still CS_READY) and that NO dispatch happened. Since the conn isn't actually open in that test, just assert that `warehouse_handle_dacs_msg` returned without crashing — call the new function instead of the old `warehouse_handle_cmd_from_zmq`. But `warehouse_handle_dacs_msg` requires `dacs_msg` not `ncland_cmd_msg_t` — Task 5 changes the inbound path. **For Task 4: leave this stepper test as-is using `warehouse_handle_cmd_from_zmq`; it gets rewritten in Task 5.**

For Task 4's purpose, the renames in stepper_tests cover only the worker_send_rsp / pending_rsp seams. The W-3b shutting_down test stays unchanged and continues to use the OLD `warehouse_handle_cmd_from_zmq` + `g_test_last_direct_rsp_result` until Task 5 cuts inbound over.

- [ ] **Step 6: Rewrite worker tests** in `ncland_unit_tests.cpp`. Every `g_test_last_rsp_result` → `g_test_last_rsp_reason`. The check_errors-related tests (lines 479, 506, 522) require attention:

Line 479 `TEST("worker", "T-3a dispatch_cmd copies check_errors onto the conn")` — **DELETE** (field is gone; Task 5 retypes `dispatch_cmd` signature).

Line 506 `TEST("worker", "T-3a error pattern -> result FAILURE when check_errors on")` — REWRITE: drop `check_errors` field references, rename to `T-3a error pattern -> dm_reason FAILURE`, assert via `g_test_last_rsp_reason`.

Line 522 `TEST("worker", "T-3a clean response -> SUCCESS; check_errors off -> SUCCESS")` — REWRITE: drop the "off" variant entirely; rename to `T-3a clean response -> SUCCESS`; assert via `g_test_last_rsp_reason`.

Paging + keepalive tests (lines 544, 565, 580, 601, 615): change `g_test_last_rsp_result` → `g_test_last_rsp_reason` where referenced.

Externs at line 44 (`g_test_last_rsp_result`): rename to `g_test_last_rsp_reason`. Delete line 48 (`g_test_last_direct_rsp_result`) only after Task 5.

- [ ] **Step 7: Build + run.** Full suite: **145 passed, 0 failed, 6 skipped** (-1 from deleted T-3a `dispatch_cmd copies check_errors`; net 146 - 1 = 145).

If you see a multiple-definition linker error for `qwrite`, see Task 1 Step 10's "Potential pitfall" — the resolution is the link-order trick or runtime indirection.

- [ ] **Step 8: Commit**

```bash
cd /home/dan/Git/netflex
git add cnc/ncland/src/ncland.h cnc/ncland/src/ncland_worker.cpp \
        cnc/ncland/src/ncland_stepper.cpp cnc/ncland/src/ncland_stepper_tests.cpp \
        cnc/ncland/src/ncland_unit_tests.cpp cnc/ncland/src/ncland_test_qwrite.cpp
git commit -m "$(cat <<'EOF'
[ncland] #4 outbound cutover: worker_send_rsp(dacs_msg), pending_rsp retype, stepper renames

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 5: Inbound cutover — `warehouse_handle_dacs_msg` full body + `dispatch_cmd(dacs_msg)` + `ncland_parse_cmd_data(dacs_msg)` + warehouse-suite rewrites

**Files:** `ncland.h`, `ncland_warehouse.cpp`, `ncland_worker.cpp`, `ncland_proto.cpp`, `ncland_unit_tests.cpp`, `ncland_stepper_tests.cpp`

- [ ] **Step 1: Retype `dispatch_cmd` signature** in `ncland_worker.cpp` and `ncland.h`:

`ncland.h`:
```c
int dispatch_cmd(conn_t *c, struct dacs_msg *m);   /* was: ncland_cmd_msg_t * */
```

`ncland_worker.cpp` — the body changes mechanically. Read the existing `dispatch_cmd` body first (likely 30-50 lines starting near `int dispatch_cmd(conn_t *c, ncland_cmd_msg_t *msg)`). The rewrite touches **only field accessors** — every log line, timeout-arming step, and state transition stays verbatim. Apply this substitution table:

| old field on `msg->` | new field on `m->` |
|---|---|
| `msg->data` (the cmd text buffer) | `m->u.dm_text` |
| `msg->tty` | `m->dm_tty` |
| `msg->orig_cnc` | (deleted — no equivalent; remove the assignment to `c->orig_cnc` too, since the field is being retyped in Task 5 Step 4 to `c->orig_old_tty` from `m->dm_old_tty`) |
| `msg->neid` | `m->dm_dacsid` |
| `msg->seq` | (deleted — no equivalent; remove any `c->seq = msg->seq` line) |
| `msg->check_errors` | (deleted — feature gone; remove `c->check_errors = msg->check_errors`) |
| `msg->is_rtrv` | (auto-derived from `m->u.dm_text` via `ncland_parse_cmd_data` — no caller assignment) |
| `msg->tmout` / `msg->deadline` | unchanged — the colon-delim parser in `ncland_parse_cmd_data` writes both into the conn directly |

Example concrete rewrite of one common pattern:

```c
/* BEFORE: */
c->seq      = msg->seq;
c->orig_cnc = msg->orig_cnc;
c->tty      = msg->tty;
c->is_rtrv  = msg->is_rtrv;
c->check_errors = msg->check_errors;
int len = (int)strlen(msg->data);
if (ncland_ne_write(c, msg->data, len) < 0) { … }

/* AFTER: */
c->tty      = m->dm_tty;
/* seq, orig_cnc, is_rtrv, check_errors removed — fields gone */
int len = (int)strlen(m->u.dm_text);
if (ncland_ne_write(c, m->u.dm_text, len) < 0) { … }
```

The total diff is a search-and-replace per the table above. Preserve every other line of the function body.

- [ ] **Step 2: Retype `ncland_parse_cmd_data`** in `ncland_proto.cpp` and `ncland.h`:

`ncland.h`:
```c
int ncland_parse_cmd_data(struct dacs_msg *m);   /* was: ncland_cmd_msg_t * */
```

`ncland_proto.cpp` — the body operates on `m->u.dm_text` instead of `m->data`. One-line search-replace.

- [ ] **Step 3: Replace `warehouse_handle_dacs_msg` stub with full body** in `ncland_warehouse.cpp`:

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
    g_test_worker_dispatched_dacsid = (int)m->dm_dacsid;

    if (dispatch_cmd(c, m) < 0)
        LOG_WARN("mq cmd: dispatch_cmd failed for neid=%u", m->dm_dacsid);
}
```

- [ ] **Step 4: Rewrite warehouse-suite tests** in `ncland_unit_tests.cpp`:

**Delete entirely:**
- Line 646 `TEST("warehouse", "T9.5 pending stash + take round-trip")`
- Line 662 `TEST("warehouse", "T9.6 pending table full returns error")`
- Line 736 `TEST("warehouse", "T-3a cmd with wrong msg_type replies FAILURE")`

**Rewrite (replace `ncland_cmd_msg_t cmd` setup with `struct dacs_msg m` and call `warehouse_handle_dacs_msg(&wh, &m)` instead of `warehouse_handle_cmd_from_zmq`):**

- Line 671 `T9.7 zmq cmd dispatches to in-process conn and stashes identity` → `T9.7 mq cmd dispatches to in-process conn`. Drop the identity-stash assertion; instead assert `c->orig_tty == m.dm_tty` after dispatch.
- Line 710 `T-3a cmd to unknown neid replies FAILURE` → `T-3a cmd to unknown neid silently dropped`. Body: send a `dacs_msg` for an unmapped neid; assert `g_test_worker_dispatched_dacsid` did NOT change. Drop the `g_test_last_direct_rsp_result` assertion.
- Line 722 `T-3a cmd to busy (non-READY) neid replies FAILURE` → `T-3a cmd to busy neid silently dropped`. Same shape.
- Line 750 `T9.8 response from in-process I/O is sent on ROUTER to stashed identity` → `T9.8 response goes through qwrite with originator coords echoed`. Body: pre-set `c->orig_tty=42`, drive prompt-match, assert `g_test_last_sent_dacs_text` matches NE output AND extend the qwrite stub to also record `dm_tty` (add a global `g_test_last_sent_dacs_tty` to `ncland_test_qwrite.cpp`).
- Line 778 `(warehouse_pending_stash test)` — DELETE (any test still referencing pending_stash).

The stepper-suite W-3b test from Task 4 Step 5 (line 230 in stepper_tests.cpp, `W-3b shutting_down rejects new cmds with FAILURE`) — REWRITE NOW: change body to call `warehouse_handle_dacs_msg(&wh, &m)`; assert that `g_test_worker_dispatched_dacsid` did NOT change (silent drop). Rename to `W-3b shutting_down silently drops new cmds`.

- [ ] **Step 5: Build + run.** Expected delete: 3 tests (T9.5, T9.6, T-3a wrong msg_type) + 1 (line 778 pending-stash). Suite: 145 - 4 = **141 passed, 0 failed, 6 skipped**.

- [ ] **Step 6: Commit**

```bash
cd /home/dan/Git/netflex
git add cnc/ncland/src/ncland.h cnc/ncland/src/ncland_warehouse.cpp \
        cnc/ncland/src/ncland_worker.cpp cnc/ncland/src/ncland_proto.cpp \
        cnc/ncland/src/ncland_unit_tests.cpp cnc/ncland/src/ncland_stepper_tests.cpp \
        cnc/ncland/src/ncland_test_qwrite.cpp
git commit -m "$(cat <<'EOF'
[ncland] #4 inbound cutover: warehouse_handle_dacs_msg, dispatch_cmd(dacs_msg), warehouse-test rewrites

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 6: Delete ncland_zmq + ncland_cmd_msg_t/ncland_rsp_msg_t + pending-stash + zmq wh fields

**Files:** `ncland.h`, `ncland_warehouse.cpp`, `ncland_zmq.cpp` (deleted), `Makefile`

- [ ] **Step 1: Delete `ncland_zmq.cpp`:**

```bash
git rm cnc/ncland/src/ncland_zmq.cpp
```

- [ ] **Step 2: Remove zmq fields from `ncland_wh_t`** in `ncland.h`:

```c
    /* Delete these lines: */
    void          *zmq_ctx;
    void          *zmq_ctl;
    int            zmq_ctl_fd;
    struct {
        int      seq;
        size_t   identity_len;
        uint8_t  identity[256];
    } pending[NCLAND_MAX_PENDING];
```

Also delete `#define NCLAND_MAX_PENDING ...` if present.

- [ ] **Step 3: Delete the typedefs** in `ncland.h`:

```c
/* Delete: */
typedef struct ncland_cmd_msg { ... } ncland_cmd_msg_t;
typedef struct ncland_rsp_msg { ... } ncland_rsp_msg_t;
```

- [ ] **Step 4: Delete the zmq function decls** in `ncland.h`:

```c
/* Delete: */
int  ncland_zmq_init(...);
void ncland_zmq_close(...);
int  ncland_zmq_recv_cmd(...);
int  ncland_zmq_send_rsp(...);
int  ncland_zmq_drain(...);
```

- [ ] **Step 5: Delete `warehouse_pending_stash` and `warehouse_pending_take`** in `ncland_warehouse.cpp` and any decls in `ncland.h`. Delete the calls (there should be none left after Task 5; verify with grep).

- [ ] **Step 6: Delete `warehouse_handle_cmd_from_zmq`** in `ncland_warehouse.cpp` (replaced by `warehouse_handle_dacs_msg` in Task 5).

- [ ] **Step 7: Delete zmq-related main-loop branches** in `ncland_warehouse.cpp`:
- The `wh->zmq_ctl_fd >= 0` epoll-add block.
- The `fd == wh->zmq_ctl_fd` dispatch branch.
- The `ncland_zmq_init(wh, cfg->ctl_endpoint)` call in `warehouse_init` and the matching cleanup paths.
- The `ncland_zmq_close(wh)` call in `warehouse_shutdown`.

Delete `g_test_last_direct_rsp_result` definition (search for `int g_test_last_direct_rsp_result`) and remove its extern from `ncland.h`. Also delete the `wh_reply_now` helper if it's now unused (grep first).

- [ ] **Step 8: Delete `g_test_worker_dispatched_seq`** in `ncland_worker.cpp` (now superseded by `_dacsid`).

- [ ] **Step 9: Update `Makefile`** — remove `ncland_zmq.o` from `NCLAND_OBJS`:

```
NCLAND_OBJS = ncland_conn.o ncland_proto.o ncland_ssh.o \
	ncland_warehouse.o ncland_worker.o ncland_stepper.o ncland_telnet.o \
	ncland_mq.o ncland_seed.o ncland_notify.o ncland_notify_parse.o \
	ncland_registry.o ncland_connpool.o ncland_lua.o
```

Note: `-lzmq` STAYS in the link lines (`notify_fd` future use).

- [ ] **Step 10: Build + run.** Suite stays at **141**.

If any test still references `ncland_cmd_msg_t`, `pending_stash`, `g_test_last_direct_rsp_result`, etc. — fix those references. Each surfaced as a compile error is the easiest debug path.

- [ ] **Step 11: Commit**

```bash
cd /home/dan/Git/netflex
git add -A cnc/ncland/src/
git commit -m "$(cat <<'EOF'
[ncland] #4 cleanup: delete ncland_zmq.cpp + cmd/rsp_msg_t + pending stash + zmq wh fields

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 7: Delete nclan_cmd + Makefile target + C-3a parse test

**Files:** `nclan_cmd.cpp` (deleted), `Makefile`, `ncland_unit_tests.cpp`

- [ ] **Step 1: Delete `nclan_cmd.cpp`:**

```bash
git rm cnc/ncland/src/nclan_cmd.cpp
```

- [ ] **Step 2: Delete the `C-3a parse neid/raw/command` test** in `ncland_unit_tests.cpp` (line 1249).

- [ ] **Step 3: Update `Makefile`:**

(a) Remove `nclan_cmd_test.o` from `ncland_unit_tests` prereqs:
```
ncland_unit_tests :: ncland_unit_tests.o ncland_notify_parse_tests.o nclan_seed_fmt.o nclan_seed_tests.o ncland_registry_tests.o ncland_connpool_tests.o ncland_lua_tests.o ncland_stepper_tests.o ncland_mq_tests.o ncland_test_qwrite.o $(NCLAND_OBJS) $(INCLIBS) -latm -linc -lxmlparse -lssh -ljson -lnetclan
	$(CPLUS_CC) $(CCFLAGS:M!=-AC99) $(LDFLAGS) -o $(<) $(*) -lpthread -lssh -lzmq -lyaml-cpp -llua -lrt
```

(b) Remove the `nclan_cmd_test.o : nclan_cmd.cpp` rule.

(c) Remove `nclan-cmd` from `.MAIN`:
```
.MAIN : $(PBIN)/ncland $(PBIN)/nclan-seed
```

(d) Remove the `$(PBIN)/nclan-cmd ::` target.

- [ ] **Step 4: Build + run.** Suite: 141 - 1 = **140 passed, 0 failed, 6 skipped**.

- [ ] **Step 5: Commit**

```bash
cd /home/dan/Git/netflex
git add -A cnc/ncland/src/
git commit -m "$(cat <<'EOF'
[ncland] #4 retire nclan_cmd: delete file + Makefile target + parse test

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Self-Review (against the spec)

**Spec coverage:**
- §1 Scope — `/ncland_ctl` mq, `mqd_t` epoll, `dacs_msg` wire, `qwrite` outbound, retired structs, pending-stash deleted, silent-drop, test isolation: all covered across Tasks 1-7. ✓
- §2 Architecture — diagram + asymmetric transports + file-touched list: Task 1 + Task 6 + Task 7. ✓
- §3 Types & field changes — Task 1 (new fields), Task 4 (pending_rsp retype), Task 5 (dispatch_cmd / parse_cmd_data signatures), Task 6 (deletions). ✓
- §4 Inbound — Task 2 (init/close), Task 3 (drain + main-loop wiring + stub dispatch), Task 5 (full dispatch). ✓
- §5 Outbound — Task 4 (worker_send_rsp rewrite + handle_ne_data construction + stepper renames). ✓
- §6 Error semantics — covered across the implementations (silent-drop in Task 5's `warehouse_handle_dacs_msg`, scrnq_overflow guard in Task 4's `worker_send_rsp`, EAGAIN/short in Task 3's `ncland_mq_drain`). ✓
- §7 Testing — Task 1 (seam infrastructure + qwrite stub), Tasks 2-3 (new mq tests), Tasks 4-5 (suite rewrites). 5 mq tests added; 6 tests deleted (T9.5, T9.6, T-3a wrong msg_type, T-3a check_errors copy, T-3a check_errors-off variant, C-3a). Net 141 → 140 matches spec. ✓
- §8 Files — every file in the spec table is touched by one of Tasks 1-7. ✓
- §9 Deferred — alarm-handler, librdb_notify, per-client migration, operator debug tool: not implemented per design. ✓

**Placeholder scan:** Task 5 Step 1's `dispatch_cmd` body says "read the existing body and produce the field-by-field rewrite inline in the commit" — that's a deliberate referral to mechanical translation, not a placeholder. The reason: pasting hundreds of lines of dispatch_cmd into the plan would bloat it without adding value; the field-mapping table in §3.2 of the spec is the source of truth and the body translation is rote. Acceptable.

**Type consistency:** `nclan_t` / `nclansim_t` not used here (different feature). All `g_test_*` seams, `ncland_mq_*` functions, `warehouse_handle_dacs_msg`, `worker_send_rsp(ncland_wh_t*, struct dacs_msg*)`, `dispatch_cmd(conn_t*, struct dacs_msg*)`, `ncland_parse_cmd_data(struct dacs_msg*)`, `c->pending_rsp.dm_reason`/`dm_tty`/`u.dm_text`, `c->orig_tty`/`orig_old_tty`/`orig_slot`/`orig_slot_tp`, `wh->ctl_mqd`/`ctl_mq_name` — used identically across tasks.

---

## Execution note

Tasks 1→7 are sequential. Task 1 establishes the infrastructure (no behavior change). Tasks 2-3 build the inbound path alongside the still-active zmq path. Task 4 is the largest task — outbound cutover with many test rewrites. Task 5 is the inbound cutover. Tasks 6-7 are deletion/cleanup. Recommended: subagent-driven, two-stage review per task, with extra scrutiny on Tasks 4 and 5 where the rename mechanics span many files.

**Pre-implementation sysctl check:** before Task 2, the implementer must verify `cat /proc/sys/fs/mqueue/msgsize_max` returns ≥ 10248. If not, apply `sudo sysctl -w fs.mqueue.msgsize_max=16384` (and persist via `/etc/sysctl.d/99-ncland.conf` per spec §4.2).
