# ncland #3c — Lua-Driven NE Simulator (`nclansim`) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a test-only in-process Lua-driven NE simulator (`nclansim`) so unit tests can drive ncland end-to-end (login, command, post_command, disconnect_steps, EOF) against scripted NE behavior without real hardware.

**Architecture:** Socketpair-based: `sv[0]` becomes ncland's `conn->net_fd`; `sv[1]` is read/written by the sim. Per-sim `lua_State` (separate from `wh->L`) hosts one coroutine running a script that calls `sim.send`/`sim.expect` (yielding on expect). `nclansim_step` is a fixed-point pump that drains both directions and resumes the sim coroutine when its pending pattern matches. Built into `ncland_unit_tests` only — never into the daemon binary.

**Tech Stack:** C++17 via Lucent/AT&T **nmake** (`ncland.mk`), Lua 5.3 (`-llua`), POSIX `regex.h`, `nfunit-test.hpp` (`./ncland_unit_tests`).

**Design doc:** `~/docs/superpowers/specs/2026-06-12-ncland-sim-tool-design.md` (read first).

---

## Critical context for the implementer (read first)

- **Build:** Lucent/AT&T **nmake**, not GNU make. From `cnc/ncland/src`: `nmake -f ncland.mk <target>`. Ensure `BASE = git rev-parse --show-toplevel` (`/home/dan/Git/netflex`) and `VPATH`'s first segment == `$BASE`. If a build fails on missing `global*.nmk` / project headers, STOP and report.
- **Tests:** `nmake -f ncland.mk ncland_unit_tests && ./ncland_unit_tests` — baseline **141 passed / 0 failed / 6 skipped**. Keep it green (plus new tests). `nfunit-test.hpp` API: `TEST("suite","name") { REQUIRE(...); REQUIRE_EQ(a,b); }`. Run one suite: `./ncland_unit_tests sim`.
- **Commit trailer (every commit):** `Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>`. Work on branch `ncland-start`; no new branches/worktrees.
- **Doxygen** on new/modified functions and structs (project rule).
- **`conn_t` POD discipline** does NOT apply to `nclansim_t` — the sim struct is heap-allocated by tests and never memcpy'd. Use `std::string` freely.
- **Test-only code:** `ncland_sim.o` goes into the `ncland_unit_tests` prereq list ONLY. **NOT** in `NCLAND_OBJS` (daemon list). This is the canonical "test fixture, not production" pattern.
- **Existing test fixture pattern:** `wh_with_step_entry(wh, dtype, prompt, lua_src, post_cmd_yaml, disconnect_yaml)` in `ncland_stepper_tests.cpp:19` uses `ncland_lua_inject_module(wh, mod_name, lua_src)` to register a synthetic Lua module without writing files. The sim's test fixture follows the same pattern (no tmp file IO for tests).
- **`ncland_lua_inject_module(wh, mod_name, lua_src)`** loads a Lua module body into the wh's module cache keyed by `mod_name` (bare filename like `cisco_ios.lua`). The NE's `ne_entry_t.lua_module` field is set to the same bare name. Returns 0 on success.
- **`ncland_login_start(wh, c)`** kicks off the ncland-side login coroutine. Returns `NCLAND_LOGIN_PENDING` if the coroutine yielded (the normal case) or `NCLAND_LOGIN_READY` if `login()` returned synchronously.
- **`handle_ne_data(c, wh)` in `ncland_worker.cpp`** is the I/O dispatcher; safe to call repeatedly on a conn in CS_AUTHENTICATING / CS_READY / CS_POST_CMD / CS_DISCONNECTING.
- **`conn_alloc(wh)` / `warehouse_apply_registry(c, e)` / `warehouse_drop_conn(wh, c)`** are the standard conn-slot lifecycle helpers used elsewhere in the codebase.
- **Lua 5.3 coroutine drive:** `lua_newthread(L)` creates a coroutine on the parent state; `lua_resume(co, from, narg)` runs it; `lua_yieldk(co, nresults, ctx, k)` suspends. Reference implementation: `ncland_lua_coroutine_drive` in `ncland_lua.cpp` (extracted in #3b Task 2).
- **`NCLAND_NE_DIR`** (`/usr/cnc/lib/data/ncland`) is the production install dir. For sim tests, the sim's Lua script path does NOT need to live there — we pass an absolute path (e.g. from `mkstemps`) or use a string-based loader. Spec §3.2 says the API takes a `const char *lua_script_path` — we use `luaL_loadfile` for files; tests can use `mkstemps` to drop a tmp `.lua` file with an absolute path.

---

## File Structure

| File | Responsibility in #3c |
|------|----------------------|
| `ncland_sim.h` (new) | Public C API: `nclansim_t` struct, six functions (`open`/`step`/`step_until`/`received`/`received_matches`/`close`). |
| `ncland_sim.cpp` (new) | Implementation: socketpair setup, sim's `lua_State` + coroutine, `sim.*` Lua functions, step algorithm, expect matching, error handling. |
| `ncland_sim_tests.cpp` (new) | 5 self-tests (`S-sim`) + 4 integration tests (`I-sim`), both in suite `sim`. |
| `ncland.mk` | Add `ncland_sim.o` + `ncland_sim_tests.o` to `ncland_unit_tests` prereqs. **Not** to `NCLAND_OBJS`. |

Tasks are sequential. Each task builds and stays green on its own.

---

## Task 1: Skeleton — `ncland_sim.{h,cpp}` + build wiring + `nclansim_open`/`_close`

**Files:** `ncland_sim.h` (new), `ncland_sim.cpp` (new), `ncland_sim_tests.cpp` (new), `ncland.mk`

Establish the module, wire it into the unit-test build, and ship a minimal `open`/`close` that creates a socketpair and slots a conn — no Lua VM, no login_start yet.

- [ ] **Step 1: Wire `ncland.mk`.** Add `ncland_sim.o` and `ncland_sim_tests.o` to the `ncland_unit_tests` prerequisite list (find the line that lists `ncland_stepper_tests.o` and add the two new objs next to it). Do **NOT** add `ncland_sim.o` to `NCLAND_OBJS` (the daemon link list).

- [ ] **Step 2: Create `ncland_sim.h`:**

```c
/** @file ncland_sim.h
 *  Lua-driven NE simulator for ncland unit tests (#3c).
 *  Test-only: NEVER linked into the daemon. See
 *  ~/docs/superpowers/specs/2026-06-12-ncland-sim-tool-design.md */
#pragma once
#include "ncland.h"
#include <string>

extern "C" { struct lua_State; }
typedef struct lua_State lua_State;

typedef struct nclansim {
    int            sv[2];           /**< sv[0] = ncland's net_fd; sv[1] = sim's own end */
    int            conn_id;         /**< Slot in wh->conn */
    ncland_wh_t   *wh;              /**< Parent warehouse */
    lua_State     *L;               /**< Sim's own Lua VM (separate from wh->L); NULL until Task 2 */
    /* Fields below populated in later tasks; declared up front so the struct
     * doesn't churn between tasks. */
    int            coro_ref;        /**< LUA_REGISTRYINDEX ref to the sim coroutine */
    int            coro_started;    /**< 1 once the script's first resume has run */
    int            coro_done;       /**< 1 once the script returned or raised */
    int            got_eof;         /**< 1 once ncland closed sv[0] */
    int            expect_active;   /**< 1 when yielded inside sim.expect() */
    regex_t        expect_re;       /**< Compiled pattern from current sim.expect() */
    int            expect_re_valid;
    time_t         expect_deadline; /**< 0 = no deadline */
    std::string    rbuf;            /**< Bytes from ncland not yet consumed by sim.expect */
    std::string    last_match;      /**< Match text of most recent satisfied sim.expect */
    std::string    history;         /**< Every byte ever received from ncland */
} nclansim_t;

/** @brief Create a sim, slot it as wh->conn[id]. Lua script is loaded in later tasks;
 *  for now, lua_script_path may be NULL. Returns sim handle or NULL on failure. */
nclansim_t *nclansim_open(ncland_wh_t *wh, int neid, int dtype,
                          const char *lua_script_path);

/** @brief Release the sim and drop its conn slot. Safe on NULL. */
void nclansim_close(nclansim_t *sim);
```

Add `#include <regex.h>` and `#include <time.h>` if not already pulled by `ncland.h`.

`LUA_NOREF` is from `<lua.h>` — to avoid pulling Lua into the header, initialize `coro_ref` via `nclansim_open` rather than as a default. The struct field is `int`; the file defining it `#include`s `<lua.h>`.

- [ ] **Step 3: Create `ncland_sim_tests.cpp` skeleton with the first failing test:**

```cpp
#include "ncland.h"
#include "ncland_sim.h"
#include "nfunit-test.hpp"
#include <fcntl.h>
#include <unistd.h>

TEST("sim", "T-3c open returns a valid sim and slots a conn") {
    ncland_wh_t wh{};
    for (int i = 0; i < MAX_CONNS; i++) conn_init(&wh.conn[i], i);
    /* No registry entry needed for skeleton (Task 1). lua_script_path is NULL. */
    nclansim_t *sim = nclansim_open(&wh, /*neid=*/1234, /*dtype=*/0, /*script=*/NULL);
    REQUIRE(sim != NULL);
    REQUIRE(sim->sv[0] >= 0);
    REQUIRE(sim->sv[1] >= 0);
    REQUIRE(sim->conn_id >= 0 && sim->conn_id < MAX_CONNS);
    REQUIRE_EQ(wh.conn[sim->conn_id].neid, 1234);
    REQUIRE_EQ(wh.conn[sim->conn_id].net_fd, sim->sv[0]);
    nclansim_close(sim);
    REQUIRE_EQ((int)wh.conn[1234 % MAX_CONNS].state, (int)CS_IDLE);
}

TEST("sim", "T-3c close is NULL-safe") {
    nclansim_close(NULL);  /* must not crash */
}
```

- [ ] **Step 4: Build (fails) — link error: `nclansim_open` / `nclansim_close` undefined.**

```bash
cd /home/dan/Git/netflex/cnc/ncland/src
nmake -f ncland.mk ncland_unit_tests
```

Expected: link error citing `nclansim_open` and `nclansim_close`.

- [ ] **Step 5: Implement `ncland_sim.cpp`:**

```cpp
/** @file ncland_sim.cpp
 *  Lua-driven NE simulator for ncland unit tests (#3c). Test-only.
 *  Design: ~/docs/superpowers/specs/2026-06-12-ncland-sim-tool-design.md */
#include "ncland_sim.h"
#include "nflog.hpp"

#include <sys/socket.h>
#include <fcntl.h>
#include <unistd.h>
#include <string.h>
#include <stdlib.h>

extern "C" { #include <lua.h> }  /* for LUA_NOREF; the rest comes in Task 2 */

extern int conn_alloc(ncland_wh_t *wh);
extern void warehouse_drop_conn(ncland_wh_t *wh, conn_t *c);

/**
 * @brief Create a sim, allocate a conn slot, install sv[0] as the conn's net_fd.
 *
 * This skeleton form does not yet create a Lua VM or start ncland's login —
 * those land in later tasks.
 */
nclansim_t *nclansim_open(ncland_wh_t *wh, int neid, int dtype,
                          const char *lua_script_path)
{
    (void)lua_script_path;  /* used from Task 2 */
    if (!wh) return NULL;

    nclansim_t *sim = new nclansim_t();
    sim->wh              = wh;
    sim->L               = NULL;
    sim->coro_ref        = LUA_NOREF;
    sim->coro_started    = 0;
    sim->coro_done       = 0;
    sim->got_eof         = 0;
    sim->expect_active   = 0;
    sim->expect_re_valid = 0;
    sim->expect_deadline = 0;

    if (socketpair(AF_UNIX, SOCK_STREAM, 0, sim->sv) != 0) {
        LOG_WARN("nclansim_open: socketpair failed: %s", strerror(errno));
        delete sim;
        return NULL;
    }
    fcntl(sim->sv[0], F_SETFL, O_NONBLOCK);
    fcntl(sim->sv[1], F_SETFL, O_NONBLOCK);

    int id = conn_alloc(wh);
    if (id < 0) {
        LOG_WARN("nclansim_open: no free conn slot");
        close(sim->sv[0]); close(sim->sv[1]);
        delete sim;
        return NULL;
    }
    sim->conn_id = id;
    conn_t *c = &wh->conn[id];
    c->neid    = neid;
    c->dtype   = dtype;
    c->net_fd  = sim->sv[0];
    c->state   = CS_READY;          /* Task 7 will set this via ncland_login_start */
    wh->nconns++;

    return sim;
}

/**
 * @brief Release the sim and drop its conn slot. Safe on NULL.
 */
void nclansim_close(nclansim_t *sim)
{
    if (!sim) return;
    /* sv[0] is closed by warehouse_drop_conn via conn_free. */
    if (sim->sv[1] >= 0) close(sim->sv[1]);
    if (sim->conn_id >= 0 && sim->wh)
        warehouse_drop_conn(sim->wh, &sim->wh->conn[sim->conn_id]);
    delete sim;
}
```

- [ ] **Step 6: Build + run tests.** `nmake -f ncland.mk ncland_unit_tests && ./ncland_unit_tests sim` → 2 new tests pass. Full suite → `143 passed, 0 failed, 6 skipped`.

- [ ] **Step 7: Commit**

```bash
cd /home/dan/Git/netflex
git add cnc/ncland/src/ncland_sim.h cnc/ncland/src/ncland_sim.cpp cnc/ncland/src/ncland_sim_tests.cpp cnc/ncland/src/ncland.mk
git commit -m "$(cat <<'EOF'
[ncland] #3c sim skeleton: nclansim_open/_close + socketpair + conn slot

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 2: Per-sim `lua_State` + `sim` table + script load

**Files:** `ncland_sim.cpp`, `ncland_sim_tests.cpp`

Add a fresh `lua_State` per sim with an empty `sim` table (Lua-side global), and load+run a script at open. No coroutine yet — the script's top-level chunk runs synchronously here.

- [ ] **Step 1: Write the failing test** in `ncland_sim_tests.cpp`. Helper for tmp lua script writing:

```cpp
/* Test helper: write a Lua source to a tmp .lua file; caller unlinks. */
static std::string write_tmp_lua_3c(const char *body) {
    char path[] = "/tmp/ncland-sim-3c-XXXXXX.lua";
    int fd = mkstemps(path, 4);
    REQUIRE(fd >= 0);
    REQUIRE_EQ((ssize_t)strlen(body), write(fd, body, strlen(body)));
    close(fd);
    return std::string(path);
}

TEST("sim", "T-3c open with a valid script creates lua_State and runs chunk") {
    ncland_wh_t wh{};
    for (int i = 0; i < MAX_CONNS; i++) conn_init(&wh.conn[i], i);
    std::string lua = write_tmp_lua_3c("-- empty script, returns nothing\n");
    nclansim_t *sim = nclansim_open(&wh, 1234, 0, lua.c_str());
    REQUIRE(sim != NULL);
    REQUIRE(sim->L != NULL);
    nclansim_close(sim);
    unlink(lua.c_str());
}

TEST("sim", "T-3c open with missing file returns NULL") {
    ncland_wh_t wh{};
    for (int i = 0; i < MAX_CONNS; i++) conn_init(&wh.conn[i], i);
    nclansim_t *sim = nclansim_open(&wh, 1234, 0, "/tmp/does-not-exist-xyz.lua");
    REQUIRE(sim == NULL);
}

TEST("sim", "T-3c open with syntax-error script returns NULL") {
    ncland_wh_t wh{};
    for (int i = 0; i < MAX_CONNS; i++) conn_init(&wh.conn[i], i);
    std::string lua = write_tmp_lua_3c("this is not valid lua @@@");
    nclansim_t *sim = nclansim_open(&wh, 1234, 0, lua.c_str());
    REQUIRE(sim == NULL);
    unlink(lua.c_str());
}
```

- [ ] **Step 2: Run (fails)** — `./ncland_unit_tests sim` — new tests fail (`sim->L == NULL` after open).

- [ ] **Step 3: Add Lua VM setup to `nclansim_open` in `ncland_sim.cpp`.**

Add full Lua includes at the top:

```cpp
extern "C" {
#include <lua.h>
#include <lauxlib.h>
#include <lualib.h>
}
```

After the conn-slot setup and BEFORE the final `return sim;`, add:

```cpp
    /* Per-sim Lua VM (separate from wh->L). */
    sim->L = luaL_newstate();
    if (!sim->L) {
        LOG_WARN("nclansim_open: luaL_newstate failed");
        warehouse_drop_conn(wh, c);
        close(sim->sv[1]);
        delete sim;
        return NULL;
    }
    luaL_openlibs(sim->L);

    /* Install empty `sim` table; per-function entries are added in later tasks. */
    lua_newtable(sim->L);
    lua_pushinteger(sim->L, neid);
    lua_setfield(sim->L, -2, "neid");
    lua_setglobal(sim->L, "sim");

    /* Load the script if provided. The top-level chunk runs synchronously here
     * — in Task 3 we'll convert it to a coroutine and defer execution. */
    if (lua_script_path) {
        if (luaL_loadfile(sim->L, lua_script_path) != LUA_OK) {
            LOG_WARN("nclansim_open: loadfile %s failed: %s",
                     lua_script_path, lua_tostring(sim->L, -1));
            lua_close(sim->L); sim->L = NULL;
            warehouse_drop_conn(wh, c);
            close(sim->sv[1]);
            delete sim;
            return NULL;
        }
        if (lua_pcall(sim->L, 0, 0, 0) != LUA_OK) {
            LOG_WARN("nclansim_open: pcall %s failed: %s",
                     lua_script_path, lua_tostring(sim->L, -1));
            lua_close(sim->L); sim->L = NULL;
            warehouse_drop_conn(wh, c);
            close(sim->sv[1]);
            delete sim;
            return NULL;
        }
    }
```

- [ ] **Step 4: Close the Lua VM in `nclansim_close`.** Insert before the `warehouse_drop_conn` call:

```c
    if (sim->L) { lua_close(sim->L); sim->L = NULL; }
```

- [ ] **Step 5: Run tests (pass)** — `./ncland_unit_tests sim` → 5 tests pass. Full suite → `146 passed, 0 failed, 6 skipped`.

- [ ] **Step 6: Commit**

```bash
cd /home/dan/Git/netflex
git add cnc/ncland/src/ncland_sim.cpp cnc/ncland/src/ncland_sim_tests.cpp
git commit -m "$(cat <<'EOF'
[ncland] #3c sim: per-sim lua_State + sim.neid + script load

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 3: Convert script to coroutine + `sim.send` + `sim.log` + history buffer + `nclansim_received` / `_received_matches` + minimal `nclansim_step`

**Files:** `ncland_sim.cpp`, `ncland_sim.h`, `ncland_sim_tests.cpp`

This is the largest task — it ships the basic pump. Subsequent tasks add features on top.

- [ ] **Step 1: Declare new public API** in `ncland_sim.h` (extend the existing decls):

```c
/** @brief One round of bidirectional I/O. (Full algorithm grows over Tasks 3-9.)
 *  Task 3 form: drain ncland→sim into history, kick sim coroutine if not started. */
void nclansim_step(nclansim_t *sim);

/** @brief Copy received history into buf; returns total bytes available
 *  (truncated to maxlen on write). */
int  nclansim_received(const nclansim_t *sim, char *buf, int maxlen);

/** @brief 1 if regex (POSIX ERE) matches anywhere in the received history. */
int  nclansim_received_matches(const nclansim_t *sim, const char *regex);
```

- [ ] **Step 2: Write the failing tests:**

```cpp
TEST("sim", "T-3c sim.send writes bytes to sv[0]") {
    ncland_wh_t wh{};
    for (int i = 0; i < MAX_CONNS; i++) conn_init(&wh.conn[i], i);
    std::string lua = write_tmp_lua_3c("sim.send('hello')\n");
    nclansim_t *sim = nclansim_open(&wh, 1234, 0, lua.c_str());
    REQUIRE(sim != NULL);

    nclansim_step(sim);

    char buf[16] = {0};
    int n = (int)read(sim->sv[0], buf, sizeof(buf)-1);
    REQUIRE_EQ(n, 5);
    REQUIRE_EQ(strcmp(buf, "hello"), 0);
    nclansim_close(sim); unlink(lua.c_str());
}

TEST("sim", "T-3c received history accumulates and matches regex") {
    ncland_wh_t wh{};
    for (int i = 0; i < MAX_CONNS; i++) conn_init(&wh.conn[i], i);
    /* Sim does nothing — just exists to receive bytes from sv[1] side. */
    std::string lua = write_tmp_lua_3c("-- passive\n");
    nclansim_t *sim = nclansim_open(&wh, 1234, 0, lua.c_str());

    /* Test writes to sv[0] (impersonating ncland); sim drains via sv[1]. */
    REQUIRE_EQ((ssize_t)3, write(sim->sv[0], "abc", 3));
    nclansim_step(sim);
    REQUIRE_EQ((ssize_t)3, write(sim->sv[0], "def", 3));
    nclansim_step(sim);

    char buf[16] = {0};
    int n = nclansim_received(sim, buf, sizeof(buf));
    REQUIRE_EQ(n, 6);
    REQUIRE_EQ(strcmp(buf, "abcdef"), 0);
    REQUIRE_EQ(nclansim_received_matches(sim, "bcde"), 1);
    REQUIRE_EQ(nclansim_received_matches(sim, "xyz"), 0);
    nclansim_close(sim); unlink(lua.c_str());
}

TEST("sim", "T-3c sim.log emits a log line (smoke test)") {
    ncland_wh_t wh{};
    for (int i = 0; i < MAX_CONNS; i++) conn_init(&wh.conn[i], i);
    std::string lua = write_tmp_lua_3c("sim.log('hello from script')\n");
    nclansim_t *sim = nclansim_open(&wh, 1234, 0, lua.c_str());
    REQUIRE(sim != NULL);
    nclansim_step(sim);   /* kicks the coroutine */
    /* No assertion on log line content (would require log redirection); just no-crash. */
    nclansim_close(sim); unlink(lua.c_str());
}
```

- [ ] **Step 3: Run (fails)** — `sim.send` undefined / `nclansim_step` undefined.

- [ ] **Step 4: Convert script load to coroutine creation.** In `nclansim_open`, change the script-load block (replace the existing `luaL_loadfile` + `lua_pcall` with):

```c
    /* Load the script onto a fresh coroutine; the coroutine body IS the top-level chunk. */
    if (lua_script_path) {
        if (luaL_loadfile(sim->L, lua_script_path) != LUA_OK) {
            LOG_WARN("nclansim_open: loadfile %s failed: %s",
                     lua_script_path, lua_tostring(sim->L, -1));
            lua_close(sim->L); sim->L = NULL;
            warehouse_drop_conn(wh, c);
            close(sim->sv[1]);
            delete sim;
            return NULL;
        }
        /* lua_newthread shares wh->L's globals — but sim->L IS the parent, so the
         * coroutine inherits sim->L's globals (which include sim.* table). */
        lua_State *co = lua_newthread(sim->L);
        lua_pushvalue(sim->L, -2);          /* the loaded chunk */
        lua_xmove(sim->L, co, 1);            /* move chunk onto coroutine */
        lua_pop(sim->L, 1);                  /* drop original chunk from sim->L */
        sim->coro_ref = luaL_ref(sim->L, LUA_REGISTRYINDEX);
    }
```

- [ ] **Step 5: Install `sim.send` and `sim.log` C functions** in `ncland_sim.cpp`. Define them near the top of the file (after includes):

```cpp
/* Forward decl: per-sim handle stashed in the lua_State's extra-space.
 * Using lua_getextraspace gives O(1) access without a registry lookup. */
static nclansim_t *sim_from_L(lua_State *L)
{
    nclansim_t **slot = (nclansim_t **)lua_getextraspace(L);
    return *slot;
}

/* sim.send(text) -> writes text to sv[1] (which sv[0] reader will see). */
static int l_sim_send(lua_State *L)
{
    nclansim_t *sim = sim_from_L(L);
    size_t len = 0;
    const char *s = luaL_checklstring(L, 1, &len);
    if (sim && sim->sv[1] >= 0)
        (void)write(sim->sv[1], s, len);
    return 0;
}

/* sim.log(msg) -> daemon log at INFO. */
static int l_sim_log(lua_State *L)
{
    nclansim_t *sim = sim_from_L(L);
    const char *msg = luaL_checkstring(L, 1);
    int neid = (sim && sim->conn_id >= 0) ? sim->wh->conn[sim->conn_id].neid : 0;
    LOG_INFO("sim[neid=%d]: %s", neid, msg);
    return 0;
}
```

In `nclansim_open`, BEFORE the script load, stash the sim handle in extra-space and add functions to the `sim` table:

```c
    /* Stash the sim handle in the lua_State extra-space for O(1) lookup
     * from C callbacks. (Same coroutines share parent's extra-space.) */
    *((nclansim_t **)lua_getextraspace(sim->L)) = sim;

    /* sim table: neid (already added) + send + log. expect/last_received in Tasks 5-6. */
    lua_getglobal(sim->L, "sim");
    lua_pushcfunction(sim->L, l_sim_send);  lua_setfield(sim->L, -2, "send");
    lua_pushcfunction(sim->L, l_sim_log);   lua_setfield(sim->L, -2, "log");
    lua_pop(sim->L, 1);
```

- [ ] **Step 6: Implement `sim_read_from_ncland` (file-static helper)** and `nclansim_step` (Task 3 form: kick once + drain ncland-side):

```c
static int sim_read_from_ncland(nclansim_t *sim)
{
    char tmp[2048];
    int n = (int)read(sim->sv[1], tmp, sizeof(tmp));
    if (n > 0) {
        sim->rbuf.append(tmp, n);
        sim->history.append(tmp, n);
        return n;
    }
    if (n == 0) { sim->got_eof = 1; return -1; }
    return 0;  /* EAGAIN / EWOULDBLOCK / other transient */
}

/* Resume the sim coroutine once. Task 3 form: just resume; Tasks 5-6 add
 * narg handling and expect resolution. */
static void sim_resume(nclansim_t *sim)
{
    if (sim->coro_done || sim->coro_ref == LUA_NOREF) return;
    lua_rawgeti(sim->L, LUA_REGISTRYINDEX, sim->coro_ref);
    lua_State *co = lua_tothread(sim->L, -1);
    lua_pop(sim->L, 1);
    int rc = lua_resume(co, sim->L, 0);
    if (rc == LUA_YIELD) { sim->coro_started = 1; return; }
    if (rc == LUA_OK)    { sim->coro_started = 1; sim->coro_done = 1; return; }
    const char *msg = lua_tostring(co, -1);
    LOG_WARN("sim[neid=%d]: script error: %s",
             sim->wh->conn[sim->conn_id].neid, msg ? msg : "(no message)");
    sim->coro_done = 1;
}

void nclansim_step(nclansim_t *sim)
{
    if (!sim) return;
    /* Task 3 form: simple two-phase. Full fixed-point pump arrives in Task 7. */
    if (!sim->coro_started && !sim->coro_done) sim_resume(sim);
    sim_read_from_ncland(sim);
}
```

- [ ] **Step 7: Implement `nclansim_received` and `nclansim_received_matches`:**

```c
int nclansim_received(const nclansim_t *sim, char *buf, int maxlen)
{
    if (!sim || !buf || maxlen <= 0) return 0;
    int copy = (int)sim->history.size();
    if (copy > maxlen - 1) copy = maxlen - 1;
    memcpy(buf, sim->history.data(), copy);
    buf[copy] = '\0';
    return (int)sim->history.size();
}

int nclansim_received_matches(const nclansim_t *sim, const char *regex)
{
    if (!sim || !regex) return 0;
    regex_t re;
    if (regcomp(&re, regex, REG_EXTENDED) != 0) return 0;
    /* history may contain embedded NULs; copy into a NUL-terminated buf for regexec. */
    std::string h = sim->history;
    int rc = regexec(&re, h.c_str(), 0, NULL, 0);
    regfree(&re);
    return (rc == 0) ? 1 : 0;
}
```

- [ ] **Step 8: Release coroutine in `nclansim_close`.** Insert before `lua_close(sim->L)`:

```c
    if (sim->L && sim->coro_ref != LUA_NOREF) {
        luaL_unref(sim->L, LUA_REGISTRYINDEX, sim->coro_ref);
        sim->coro_ref = LUA_NOREF;
    }
    if (sim->expect_re_valid) { regfree(&sim->expect_re); sim->expect_re_valid = 0; }
```

- [ ] **Step 9: Run tests (pass)** — `./ncland_unit_tests sim` → 3 new tests pass. Full suite → `149 passed, 0 failed, 6 skipped`.

- [ ] **Step 10: Commit**

```bash
cd /home/dan/Git/netflex
git add cnc/ncland/src/ncland_sim.h cnc/ncland/src/ncland_sim.cpp cnc/ncland/src/ncland_sim_tests.cpp
git commit -m "$(cat <<'EOF'
[ncland] #3c sim: coroutine + sim.send/sim.log + history buffer + minimal step

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 4: `sim.expect` — synchronous path + `sim_try_match`

**Files:** `ncland_sim.cpp`, `ncland_sim_tests.cpp`

Add `sim.expect` that matches against already-buffered data synchronously (no yield required if the data is already present). Async yield comes in Task 5.

- [ ] **Step 1: Write the failing test:**

```cpp
TEST("sim", "T-3c sim.expect matches data already in rbuf (sync path)") {
    ncland_wh_t wh{};
    for (int i = 0; i < MAX_CONNS; i++) conn_init(&wh.conn[i], i);
    /* Script: receive line, echo. Test pre-loads sv[0]→sv[1] before step. */
    const char *body =
        "local line = sim.expect('([a-z]+)\\n', 5)\n"
        "sim.send('got: '..tostring(line))\n";
    std::string lua = write_tmp_lua_3c(body);
    nclansim_t *sim = nclansim_open(&wh, 1234, 0, lua.c_str());
    REQUIRE(sim != NULL);
    /* Pre-load data the sim's expect will find immediately. */
    REQUIRE_EQ((ssize_t)6, write(sim->sv[0], "hello\n", 6));
    nclansim_step(sim);     /* kicks coro: expect sync-matches, then sim.send */
    /* Drain output (sv[1] → sv[0] reader). */
    char buf[32] = {0};
    int n = (int)read(sim->sv[0], buf, sizeof(buf)-1);
    REQUIRE(n >= 11 && strncmp(buf, "got: hello\n", 11) == 0);
    nclansim_close(sim); unlink(lua.c_str());
}
```

- [ ] **Step 2: Run (fails)** — `sim.expect` undefined in the Lua table.

- [ ] **Step 3: Implement `sim_try_match` and `l_sim_expect`** in `ncland_sim.cpp`:

```c
/** Try to satisfy a pending sim.expect against current rbuf.
 *  Returns true if the script should be resumed (match or timeout/EOF). */
static bool sim_try_match(nclansim_t *sim)
{
    if (!sim->expect_active || !sim->expect_re_valid) return false;
    sim->rbuf.push_back('\0');
    regmatch_t m;
    int rc = regexec(&sim->expect_re, sim->rbuf.data(), 1, &m, 0);
    sim->rbuf.pop_back();
    if (rc != 0) {
        bool timed_out = sim->expect_deadline && time(NULL) >= sim->expect_deadline;
        if (timed_out || sim->got_eof) {
            sim->last_match.clear();
            regfree(&sim->expect_re); sim->expect_re_valid = 0;
            sim->expect_active = 0;
            return true;
        }
        return false;
    }
    sim->last_match.assign(sim->rbuf.data() + m.rm_so, m.rm_eo - m.rm_so);
    sim->rbuf.erase(0, m.rm_eo);
    regfree(&sim->expect_re); sim->expect_re_valid = 0;
    sim->expect_active = 0;
    return true;
}

/* Continuation: simply returns 1 — the resumed value is already on the
 * coroutine's stack (pushed by sim_resume; see Task 5). */
static int l_sim_expect_k(lua_State *L, int status, lua_KContext ctx)
{
    (void)L; (void)status; (void)ctx;
    return 1;
}

/* sim.expect(pattern, timeout_s) — sync path only in Task 4. */
static int l_sim_expect(lua_State *L)
{
    nclansim_t *sim = sim_from_L(L);
    const char *pat = luaL_checkstring(L, 1);
    lua_Number tmout = luaL_optnumber(L, 2, 5.0);

    if (sim->expect_re_valid) { regfree(&sim->expect_re); sim->expect_re_valid = 0; }
    if (regcomp(&sim->expect_re, pat, REG_EXTENDED) != 0)
        return luaL_error(L, "sim.expect: bad regex '%s'", pat);
    sim->expect_re_valid = 1;
    sim->expect_active   = 1;
    sim->expect_deadline = (tmout > 0) ? time(NULL) + (time_t)tmout : 0;

    /* Already-EOF? Resolve nil immediately. */
    if (sim->got_eof) {
        regfree(&sim->expect_re); sim->expect_re_valid = 0;
        sim->expect_active = 0;
        lua_pushnil(L);
        return 1;
    }

    /* Synchronous match attempt — for Task 4 the loop has not drained sv[1]
     * yet, but tests pre-fill ncland's side; the rbuf might already hold the
     * data. If not, Task 4 returns nil (a temporary stand-in for "yield"). */
    if (sim_try_match(sim)) {
        if (sim->last_match.empty()) lua_pushnil(L);
        else lua_pushlstring(L, sim->last_match.data(), sim->last_match.size());
        sim->last_match.clear();
        sim->expect_deadline = 0;
        return 1;
    }

    /* Task 4 stub: no yield support yet — return nil so the script can
     * continue without hanging. The async path lands in Task 5. */
    sim->expect_active = 0;
    regfree(&sim->expect_re); sim->expect_re_valid = 0;
    lua_pushnil(L);
    return 1;
}
```

Wire `sim.expect` into the table in `nclansim_open`, alongside `send`/`log`:

```c
    lua_pushcfunction(sim->L, l_sim_expect); lua_setfield(sim->L, -2, "expect");
```

Update `nclansim_step` to drain ncland-side BEFORE the coro kick, so pre-loaded data is in rbuf before the first `sim.expect` call:

```c
void nclansim_step(nclansim_t *sim)
{
    if (!sim) return;
    sim_read_from_ncland(sim);
    if (!sim->coro_started && !sim->coro_done) sim_resume(sim);
    sim_read_from_ncland(sim);
}
```

- [ ] **Step 4: Run tests (pass)** — `./ncland_unit_tests sim` → 1 new test passes. Full suite → `150 passed, 0 failed, 6 skipped`.

- [ ] **Step 5: Commit**

```bash
cd /home/dan/Git/netflex
git add cnc/ncland/src/ncland_sim.cpp cnc/ncland/src/ncland_sim_tests.cpp
git commit -m "$(cat <<'EOF'
[ncland] #3c sim: sim.expect synchronous path + sim_try_match

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 5: `sim.expect` async path — `lua_yieldk` + `sim_resume` with `narg=1`

**Files:** `ncland_sim.cpp`, `ncland_sim_tests.cpp`

Replace the Task 4 stub (return-nil-when-no-sync-match) with a real `lua_yieldk` that suspends the coroutine until `sim_try_match` resolves.

- [ ] **Step 1: Write the failing test:**

```cpp
TEST("sim", "T-3c sim.expect yields, resumes when data arrives") {
    ncland_wh_t wh{};
    for (int i = 0; i < MAX_CONNS; i++) conn_init(&wh.conn[i], i);
    const char *body =
        "local line = sim.expect('([a-z]+)\\n', 5)\n"
        "sim.send('got: '..tostring(line))\n";
    std::string lua = write_tmp_lua_3c(body);
    nclansim_t *sim = nclansim_open(&wh, 1234, 0, lua.c_str());

    nclansim_step(sim);    /* coro yields on expect, no data yet */
    REQUIRE_EQ(sim->expect_active, 1);
    REQUIRE_EQ(sim->coro_done, 0);

    REQUIRE_EQ((ssize_t)6, write(sim->sv[0], "world\n", 6));
    nclansim_step(sim);    /* match → resume → sim.send → return */
    REQUIRE_EQ(sim->coro_done, 1);
    char buf[32] = {0};
    int n = (int)read(sim->sv[0], buf, sizeof(buf)-1);
    REQUIRE(n >= 11 && strncmp(buf, "got: world\n", 11) == 0);
    nclansim_close(sim); unlink(lua.c_str());
}
```

- [ ] **Step 2: Run (fails)** — current Task 4 stub returns nil immediately; `expect_active` is 0 after step 1, not 1; "got: nil" landed in sv[0] instead.

- [ ] **Step 3: Replace the Task 4 stub at the bottom of `l_sim_expect` with a real yield:**

Find this block (Task 4 stub):
```c
    /* Task 4 stub: no yield support yet — return nil so the script can
     * continue without hanging. The async path lands in Task 5. */
    sim->expect_active = 0;
    regfree(&sim->expect_re); sim->expect_re_valid = 0;
    lua_pushnil(L);
    return 1;
```

Replace with:
```c
    /* Async path: yield. Continuation will return the value pushed by sim_resume. */
    return lua_yieldk(L, 0, 0, l_sim_expect_k);
```

- [ ] **Step 4: Update `sim_resume`** to push the match value on resume when an expect was just satisfied:

Replace the existing `sim_resume` body with:

```c
static void sim_resume(nclansim_t *sim)
{
    if (sim->coro_done || sim->coro_ref == LUA_NOREF) return;
    lua_rawgeti(sim->L, LUA_REGISTRYINDEX, sim->coro_ref);
    lua_State *co = lua_tothread(sim->L, -1);
    lua_pop(sim->L, 1);

    int narg = 0;
    /* If we're resuming from a satisfied expect (Task 4's sim_try_match set
     * expect_active=0 and populated last_match), push the match/nil for the
     * continuation to return. */
    if (sim->coro_started && !sim->expect_active && !sim->expect_re_valid) {
        if (sim->last_match.empty()) lua_pushnil(co);
        else lua_pushlstring(co, sim->last_match.data(), sim->last_match.size());
        sim->last_match.clear();
        sim->expect_deadline = 0;
        narg = 1;
    }

    int rc = lua_resume(co, sim->L, narg);
    if (rc == LUA_YIELD) { sim->coro_started = 1; return; }
    if (rc == LUA_OK)    { sim->coro_started = 1; sim->coro_done = 1; return; }
    const char *msg = lua_tostring(co, -1);
    LOG_WARN("sim[neid=%d]: script error: %s",
             sim->wh->conn[sim->conn_id].neid, msg ? msg : "(no message)");
    sim->coro_done = 1;
}
```

- [ ] **Step 5: Update `nclansim_step`** to attempt a `sim_try_match` + resume on each step:

```c
void nclansim_step(nclansim_t *sim)
{
    if (!sim) return;
    /* Task 5: simple pump. Full fixed-point lands in Task 7 once handle_ne_data
     * is wired in. */
    sim_read_from_ncland(sim);
    if (!sim->coro_started && !sim->coro_done) {
        sim_resume(sim);
    } else if (sim->expect_active && sim_try_match(sim)) {
        sim_resume(sim);
    }
    sim_read_from_ncland(sim);
}
```

- [ ] **Step 6: Run tests (pass)** — `./ncland_unit_tests sim` → 1 new test passes; Task 4's sync test must still pass too. Full suite → `151 passed, 0 failed, 6 skipped`.

- [ ] **Step 7: Commit**

```bash
cd /home/dan/Git/netflex
git add cnc/ncland/src/ncland_sim.cpp cnc/ncland/src/ncland_sim_tests.cpp
git commit -m "$(cat <<'EOF'
[ncland] #3c sim: sim.expect async (lua_yieldk + sim_resume with narg=1)

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 6: `sim.last_received`

**Files:** `ncland_sim.cpp`, `ncland_sim_tests.cpp`

Trivial add: return bytes since the most recent satisfied expect (or since open if none).

- [ ] **Step 1: Add a field** `last_received_bookmark` (size_t) to `nclansim_t` in `ncland_sim.h`:

```c
    size_t         last_received_bookmark;  /**< history offset of last expect completion */
```

- [ ] **Step 2: Bookmark on each satisfied expect.** In `sim_try_match`, after the successful-match branch:

```c
    sim->last_match.assign(sim->rbuf.data() + m.rm_so, m.rm_eo - m.rm_so);
    sim->rbuf.erase(0, m.rm_eo);
    sim->last_received_bookmark = sim->history.size();   /* NEW */
    regfree(&sim->expect_re); sim->expect_re_valid = 0;
    sim->expect_active = 0;
    return true;
```

Initialize `last_received_bookmark = 0` in `nclansim_open`.

- [ ] **Step 3: Add `l_sim_last_received` C function and wire it:**

```c
static int l_sim_last_received(lua_State *L)
{
    nclansim_t *sim = sim_from_L(L);
    if (!sim) { lua_pushlstring(L, "", 0); return 1; }
    size_t bk = sim->last_received_bookmark;
    if (bk >= sim->history.size()) { lua_pushlstring(L, "", 0); return 1; }
    lua_pushlstring(L, sim->history.data() + bk, sim->history.size() - bk);
    return 1;
}
```

Wire in `nclansim_open` next to other `sim.*` setters:
```c
    lua_pushcfunction(sim->L, l_sim_last_received); lua_setfield(sim->L, -2, "last_received");
```

- [ ] **Step 4: Write the test:**

```cpp
TEST("sim", "T-3c sim.last_received returns bytes since last expect") {
    ncland_wh_t wh{};
    for (int i = 0; i < MAX_CONNS; i++) conn_init(&wh.conn[i], i);
    const char *body =
        "sim.expect('A', 5)\n"
        "sim.expect('B', 5)\n"
        "sim.send('got: '..sim.last_received())\n";
    std::string lua = write_tmp_lua_3c(body);
    nclansim_t *sim = nclansim_open(&wh, 1234, 0, lua.c_str());

    nclansim_step(sim);                      /* yields on expect 'A' */
    REQUIRE_EQ((ssize_t)1, write(sim->sv[0], "A", 1));
    nclansim_step(sim);                      /* matches A; bookmark set; yields on 'B' */
    REQUIRE_EQ((ssize_t)4, write(sim->sv[0], "xyzB", 4));
    nclansim_step(sim);                      /* matches B; resumes; sim.last_received()=="xyzB" */

    char buf[32] = {0};
    int n = (int)read(sim->sv[0], buf, sizeof(buf)-1);
    REQUIRE(n >= 9 && strncmp(buf, "got: xyzB", 9) == 0);
    nclansim_close(sim); unlink(lua.c_str());
}
```

- [ ] **Step 5: Run + commit.** Full suite → `152 passed, 0 failed, 6 skipped`.

```bash
cd /home/dan/Git/netflex
git add cnc/ncland/src/ncland_sim.h cnc/ncland/src/ncland_sim.cpp cnc/ncland/src/ncland_sim_tests.cpp
git commit -m "$(cat <<'EOF'
[ncland] #3c sim: sim.last_received + history bookmark

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 7: Full `nclansim_step` fixed-point pump + `nclansim_step_until` + `handle_ne_data` integration

**Files:** `ncland_sim.cpp`, `ncland_sim.h`, `ncland_sim_tests.cpp`

Wire ncland's `handle_ne_data` into the step loop and convert `nclansim_step` into the fixed-point pump from spec §5.1. Also start ncland's login coroutine inside `nclansim_open` so we can drive a real login dialogue end-to-end.

- [ ] **Step 1: Add `nclansim_step_until` decl** in `ncland_sim.h`:

```c
/** @brief Repeatedly nclansim_step until c->state == target_state, or max_iters
 *  elapses. Returns 1 on match, 0 on timeout. */
int nclansim_step_until(nclansim_t *sim, conn_state_t target_state, int max_iters);
```

- [ ] **Step 2: Make `nclansim_open` start ncland's login.**

Add `#include "ncland_lua.h"` to `ncland_sim.cpp`. Then in `nclansim_open`, AFTER the script is loaded onto its coroutine but BEFORE returning, add:

```c
    /* Apply registry-compiled regexes (prompt, paging, errors) if a dtype is registered.
     * Without this, handle_ne_data has no prompt to match. */
    const ne_entry_t *e = ncland_registry_find(&wh->registry, dtype);
    if (e) {
        extern void warehouse_apply_registry(conn_t *c, const ne_entry_t *e);
        warehouse_apply_registry(c, e);
    }

    /* Kick ncland's login coroutine so the conn enters CS_AUTHENTICATING. The
     * NE's lua_module must define M.login. If no module / no login → conn
     * goes CS_READY immediately (no harm; tests that need a real login provide
     * a module). */
    if (e && !e->lua_module.empty()) {
        int rc = ncland_login_start(wh, c);
        if (rc != NCLAND_LOGIN_PENDING && rc != NCLAND_LOGIN_READY) {
            LOG_WARN("nclansim_open: ncland_login_start failed (rc=%d)", rc);
            /* Continue anyway — the sim is still usable; tests can observe the failure. */
        }
    }
```

- [ ] **Step 3: Rewrite `nclansim_step`** to the full fixed-point pump:

```c
/* Drain conn state forward by one round; returns true if state or rbuf changed. */
static bool sim_drive_ncland(nclansim_t *sim)
{
    conn_t *c = &sim->wh->conn[sim->conn_id];
    int pre_rlen = c->rlen;
    conn_state_t pre_state = c->state;
    extern void handle_ne_data(conn_t *c, ncland_wh_t *wh);
    handle_ne_data(c, sim->wh);
    return (c->rlen != pre_rlen) || (c->state != pre_state);
}

void nclansim_step(nclansim_t *sim)
{
    if (!sim) return;
    for (;;) {
        bool progressed = false;

        /* Phase A: kick the sim coroutine if it has never run. */
        if (!sim->coro_started && !sim->coro_done) {
            sim_resume(sim);
            progressed = true;
        }

        /* Phase B: drain ncland → sim. */
        int r = sim_read_from_ncland(sim);
        if (r > 0 || r < 0) progressed = true;   /* any data OR EOF this round */

        /* Phase C: try to satisfy a pending sim.expect. */
        if (sim->expect_active && sim_try_match(sim)) {
            sim_resume(sim);
            progressed = true;
        }

        /* Phase D: drive ncland's handle_ne_data. */
        if (sim_drive_ncland(sim)) progressed = true;

        if (!progressed) return;
    }
}

int nclansim_step_until(nclansim_t *sim, conn_state_t target_state, int max_iters)
{
    if (!sim) return 0;
    for (int i = 0; i < max_iters; i++) {
        nclansim_step(sim);
        if (sim->wh->conn[sim->conn_id].state == target_state) return 1;
    }
    return 0;
}
```

- [ ] **Step 4: Write the integration test.** This needs an NE lua module + registry entry to exercise the full pipe:

```cpp
/* Set up a wh with a registered dtype + a synthetic lua_module that does a
 * tiny login dialogue. Mirrors wh_with_step_entry from stepper tests. */
static void wh_with_sim_entry(ncland_wh_t &wh, int dtype, const char *prompt,
                              const char *lua_src)
{
    for (int i = 0; i < MAX_CONNS; i++) conn_init(&wh.conn[i], i);
    REQUIRE_EQ(ncland_lua_init(&wh), 0);
    static int seq = 0;
    char mod_name[64];
    snprintf(mod_name, sizeof(mod_name), "sim_test_%d.lua", dtype + seq++);
    REQUIRE_EQ(ncland_lua_inject_module(&wh, mod_name, lua_src), 0);

    ne_entry_t e;
    e.dtype = dtype; e.ne_type = "TEST_NE"; e.lua_module = mod_name;
    e.primary_regex = prompt;
    REQUIRE_EQ(regcomp(&e.prmpt1re, prompt, REG_EXTENDED), 0); e.prmpt1_valid = true;
    wh.registry.by_dtype[dtype] = e;
}

TEST("sim", "T-3c full login dialogue: sim sends prompts, ncland's login matches them") {
    ncland_wh_t wh{};
    /* NE module: login() reads 'Username:' from NE then sends user; reads
     * 'Password:' then sends password; reads prompt and returns. */
    const char *ne_lua =
        "local M={}\n"
        "function M.login(s)\n"
        "  s:expect('Username:', 5); s:send('admin\\n')\n"
        "  s:expect('Password:', 5); s:send('secret\\n')\n"
        "  s:expect('device#',   5)\n"
        "end\n"
        "return M\n";
    wh_with_sim_entry(wh, 9501, "device#", ne_lua);

    /* Sim script: a fake Cisco-ish login dialogue. */
    const char *sim_lua =
        "sim.send('Username: ')\n"
        "sim.expect('admin\\n', 5)\n"
        "sim.send('Password: ')\n"
        "sim.expect('secret\\n', 5)\n"
        "sim.send('device# ')\n";
    std::string sim_path = write_tmp_lua_3c(sim_lua);

    nclansim_t *sim = nclansim_open(&wh, 9501, 9501, sim_path.c_str());
    REQUIRE(sim != NULL);
    int reached = nclansim_step_until(sim, CS_READY, 50);
    REQUIRE_EQ(reached, 1);
    REQUIRE_EQ(nclansim_received_matches(sim, "admin"), 1);
    REQUIRE_EQ(nclansim_received_matches(sim, "secret"), 1);
    nclansim_close(sim);
    ncland_lua_shutdown(&wh);
    unlink(sim_path.c_str());
}
```

- [ ] **Step 5: Run (may fail)** — possible issues:
- Login uses `wh->L`'s session userdata; sim uses its own L. They're independent and should not interfere.
- ncland_login_start expects `wh->L` to be initialized — the fixture calls `ncland_lua_init(&wh)`.
- If a stub registry doesn't have a `lua_module` field populated, the login won't run; this test's fixture sets it.

If the test fails, diagnose via `LOG_DEBUG` traces on both coroutines. Most likely cause: `e->lua_module` field isn't being read because the fixture didn't init it properly.

- [ ] **Step 6: Run tests (pass)** — full suite → `153 passed, 0 failed, 6 skipped`.

- [ ] **Step 7: Commit**

```bash
cd /home/dan/Git/netflex
git add cnc/ncland/src/ncland_sim.h cnc/ncland/src/ncland_sim.cpp cnc/ncland/src/ncland_sim_tests.cpp
git commit -m "$(cat <<'EOF'
[ncland] #3c sim: fixed-point step pump + step_until + login_start integration

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 8: Timeout handling for `sim.expect`

**Files:** `ncland_sim.cpp`, `ncland_sim_tests.cpp`

`sim_try_match` already checks `expect_deadline` and resolves with `nil` on timeout. Add a test that verifies the timeout path works end-to-end.

- [ ] **Step 1: Write the failing test:**

```cpp
TEST("sim", "T-3c sim.expect timeout returns nil; script continues") {
    ncland_wh_t wh{};
    for (int i = 0; i < MAX_CONNS; i++) conn_init(&wh.conn[i], i);
    const char *body =
        "local x = sim.expect('NEVERSEEN', 1)\n"
        "if x == nil then sim.send('TIMED_OUT')\n"
        "else             sim.send('UNEXPECTED:'..x) end\n";
    std::string lua = write_tmp_lua_3c(body);
    nclansim_t *sim = nclansim_open(&wh, 1234, 0, lua.c_str());

    nclansim_step(sim);                  /* coro yields on expect */
    REQUIRE_EQ(sim->expect_active, 1);
    sleep(2);                             /* exceed the 1s deadline */
    nclansim_step(sim);                  /* sim_try_match: timed_out=true → resume nil */
    REQUIRE_EQ(sim->coro_done, 1);

    char buf[32] = {0};
    int n = (int)read(sim->sv[0], buf, sizeof(buf)-1);
    REQUIRE(n >= 9 && strncmp(buf, "TIMED_OUT", 9) == 0);
    nclansim_close(sim); unlink(lua.c_str());
}
```

- [ ] **Step 2: Run** — expected to pass because `sim_try_match` (Task 4) already handles `expect_deadline`. If it fails, the most likely cause is the timeout path doesn't trigger because `sim_try_match` isn't being called in `nclansim_step` for the no-new-data case. Audit Phase C: `if (sim->expect_active && sim_try_match(sim))` — this should run even when no data arrived. Verify by reading the implementation.

- [ ] **Step 3: Suite → `154 passed, 0 failed, 6 skipped`. Commit:**

```bash
cd /home/dan/Git/netflex
git add cnc/ncland/src/ncland_sim_tests.cpp
git commit -m "$(cat <<'EOF'
[ncland] #3c sim: sim.expect timeout test (covers the deadline path)

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 9: EOF handling (`got_eof`)

**Files:** `ncland_sim_tests.cpp`

`got_eof` plumbing was already added to `sim_read_from_ncland`, `sim_try_match`, and `l_sim_expect` in Tasks 3-5. Add a test that exercises an EOF arriving during a pending expect.

- [ ] **Step 1: Write the test:**

```cpp
TEST("sim", "T-3c EOF during pending expect: expect returns nil, script can exit") {
    ncland_wh_t wh{};
    for (int i = 0; i < MAX_CONNS; i++) conn_init(&wh.conn[i], i);
    const char *body =
        "local x = sim.expect('NEVERSEEN', 30)\n"
        "if x == nil then sim.send('GOT_EOF') end\n";
    std::string lua = write_tmp_lua_3c(body);
    nclansim_t *sim = nclansim_open(&wh, 1234, 0, lua.c_str());

    nclansim_step(sim);                  /* coro yields on expect */
    REQUIRE_EQ(sim->expect_active, 1);

    /* Simulate ncland closing its end. close(sv[0]) directly — bypasses
     * warehouse_drop_conn since we want sv[0] dead but sim still alive. */
    close(sim->sv[0]);                   /* ncland-side close */
    sim->wh->conn[sim->conn_id].net_fd = -1;
    nclansim_step(sim);                  /* sim_read_from_ncland sets got_eof; try_match resolves nil */
    REQUIRE_EQ(sim->got_eof, 1);
    REQUIRE_EQ(sim->coro_done, 1);

    /* sim.send writes to sv[1] which is still open even though sv[0] is closed;
     * the bytes are buffered until sim->sv[1] is closed (by nclansim_close).
     * History assertion is the easier check: history doesn't grow further. */
    REQUIRE_EQ(nclansim_received_matches(sim, "."), 0);   /* nothing received */
    nclansim_close(sim); unlink(lua.c_str());
}
```

- [ ] **Step 2: Run** — should pass if Task 5's `sim_resume` correctly pushes `nil` on resume after a `got_eof`-triggered `sim_try_match`. If it fails, the resume's narg-handling needs a tweak: ensure the `sim->got_eof` case in `sim_try_match` (which sets `last_match.clear()` and `expect_active=0`) is followed by a `sim_resume` that pushes `nil` (the empty `last_match` should trigger the `lua_pushnil(co)` branch).

- [ ] **Step 3: Suite → `155 passed, 0 failed, 6 skipped`. Commit:**

```bash
cd /home/dan/Git/netflex
git add cnc/ncland/src/ncland_sim_tests.cpp
git commit -m "$(cat <<'EOF'
[ncland] #3c sim: EOF-during-expect test (got_eof path)

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 10: Integration tests (`I-sim`) against #3b code

**Files:** `ncland_sim_tests.cpp`

Four end-to-end tests that prove the sim is fit for testing the #3b step engine. No new sim code.

- [ ] **Step 1: I-sim full login → command → response.** A small mock Cisco NE; sim returns canned data; test injects a command via `dispatch_cmd` directly (bypasses ZMQ — tests inject straight into the conn) and verifies the response.

```cpp
TEST("sim", "I-sim full login + show command produces expected response") {
    ncland_wh_t wh{};
    const char *ne_lua =
        "local M={}\n"
        "function M.login(s) s:expect('device#', 5) end\n"
        "return M\n";
    wh_with_sim_entry(wh, 9601, "device#", ne_lua);

    const char *sim_lua =
        "sim.send('device# ')\n"                                      /* login prompt */
        "while true do\n"
        "  local line = sim.expect('([^\\r\\n]+)\\n', 30)\n"
        "  if line == nil then return end\n"
        "  if line:match('show version') then sim.send('IOS XR 7.5\\ndevice# ')\n"
        "  else sim.send('% Invalid input\\ndevice# ') end\n"
        "end\n";
    std::string sim_path = write_tmp_lua_3c(sim_lua);

    nclansim_t *sim = nclansim_open(&wh, 9601, 9601, sim_path.c_str());
    REQUIRE_EQ(nclansim_step_until(sim, CS_READY, 50), 1);

    /* Inject a CMD via dispatch_cmd directly (avoids the ZMQ frame parsing). */
    ncland_cmd_msg_t cmd; memset(&cmd, 0, sizeof(cmd));
    cmd.conn_id = sim->conn_id; cmd.seq = 1; cmd.tmout = 10;
    snprintf(cmd.data, sizeof(cmd.data), "show version");
    extern int dispatch_cmd(conn_t *c, ncland_cmd_msg_t *msg);
    REQUIRE_EQ(dispatch_cmd(&wh.conn[sim->conn_id], &cmd), 0);

    /* Drive both sides until conn returns to CS_READY. */
    REQUIRE_EQ(nclansim_step_until(sim, CS_READY, 100), 1);
    /* Confirm the sim received the cmd. */
    REQUIRE_EQ(nclansim_received_matches(sim, "show version"), 1);

    nclansim_close(sim); ncland_lua_shutdown(&wh); unlink(sim_path.c_str());
}
```

- [ ] **Step 2: I-sim post_command sees NE output.** Drives a post_command lua step against a sim that returns recognizable text:

```cpp
TEST("sim", "I-sim post_command op:lua sees NE output via session:last_output") {
    ncland_wh_t wh{};
    const char *ne_lua =
        "local M={}\n"
        "function M.login(s) s:expect('device#', 5) end\n"
        "function M.assert_foo(s) s:set_var('saw_foo', s:last_output():match('foo') and 'yes' or 'no') end\n"
        "return M\n";
    wh_with_sim_entry(wh, 9602, "device#", ne_lua);
    /* Override post_command on this dtype */
    wh.registry.by_dtype[9602].post_command = YAML::Load("[{op: lua, func: assert_foo}]");

    const char *sim_lua =
        "sim.send('device# ')\n"
        "sim.expect('([^\\r\\n]+)\\n', 30)\n"
        "sim.send('foo\\ndevice# ')\n";
    std::string sim_path = write_tmp_lua_3c(sim_lua);
    nclansim_t *sim = nclansim_open(&wh, 9602, 9602, sim_path.c_str());
    REQUIRE_EQ(nclansim_step_until(sim, CS_READY, 50), 1);

    ncland_cmd_msg_t cmd; memset(&cmd, 0, sizeof(cmd));
    cmd.conn_id = sim->conn_id; cmd.seq = 1; cmd.tmout = 10;
    snprintf(cmd.data, sizeof(cmd.data), "show foo");
    extern int dispatch_cmd(conn_t *c, ncland_cmd_msg_t *msg);
    dispatch_cmd(&wh.conn[sim->conn_id], &cmd);

    REQUIRE_EQ(nclansim_step_until(sim, CS_READY, 100), 1);
    REQUIRE_EQ(strcmp(ncland_lua_get_var(&wh, &wh.conn[sim->conn_id], "saw_foo"), "yes"), 0);

    nclansim_close(sim); ncland_lua_shutdown(&wh); unlink(sim_path.c_str());
}
```

- [ ] **Step 3: I-sim disconnect_steps fires on SIGTERM-equivalent.** Calls `ncland_warehouse_begin_drain` and asserts the sim received the disconnect lua's output:

```cpp
TEST("sim", "I-sim disconnect_steps fires on begin_drain and sim receives the bye") {
    ncland_wh_t wh{};
    const char *ne_lua =
        "local M={}\n"
        "function M.login(s) s:expect('device#', 5) end\n"
        "function M.bye(s)   s:send('write memory\\n'); s:expect('device#', 5) end\n"
        "return M\n";
    wh_with_sim_entry(wh, 9603, "device#", ne_lua);
    wh.registry.by_dtype[9603].disconnect_steps = YAML::Load("[{op: lua, func: bye}]");

    const char *sim_lua =
        "sim.send('device# ')\n"
        "while true do\n"
        "  local cmd = sim.expect('([^\\r\\n]+)\\n', 30)\n"
        "  if cmd == nil then return end\n"
        "  if cmd:match('write memory') then sim.send('[OK]\\ndevice# ') end\n"
        "end\n";
    std::string sim_path = write_tmp_lua_3c(sim_lua);
    nclansim_t *sim = nclansim_open(&wh, 9603, 9603, sim_path.c_str());
    REQUIRE_EQ(nclansim_step_until(sim, CS_READY, 50), 1);

    extern void ncland_warehouse_begin_drain(ncland_wh_t *wh);
    ncland_warehouse_begin_drain(&wh);
    REQUIRE_EQ(nclansim_step_until(sim, CS_IDLE, 100), 1);
    REQUIRE_EQ(nclansim_received_matches(sim, "write memory"), 1);

    nclansim_close(sim); ncland_lua_shutdown(&wh); unlink(sim_path.c_str());
}
```

- [ ] **Step 4: I-sim script-return → coro_done; sim still usable.**

```cpp
TEST("sim", "I-sim script return → coro_done; further steps are no-op") {
    ncland_wh_t wh{};
    for (int i = 0; i < MAX_CONNS; i++) conn_init(&wh.conn[i], i);
    std::string lua = write_tmp_lua_3c("sim.send('hi')\n");   /* return immediately */
    nclansim_t *sim = nclansim_open(&wh, 1234, 0, lua.c_str());
    nclansim_step(sim);   REQUIRE_EQ(sim->coro_done, 1);
    nclansim_step(sim); nclansim_step(sim); nclansim_step(sim);   /* no-op */
    char buf[8] = {0}; int n = (int)read(sim->sv[0], buf, sizeof(buf)-1);
    REQUIRE_EQ(n, 2); REQUIRE_EQ(strcmp(buf, "hi"), 0);
    nclansim_close(sim); unlink(lua.c_str());
}
```

- [ ] **Step 5: Run all tests.** Full suite → `159 passed, 0 failed, 6 skipped`.

- [ ] **Step 6: Commit**

```bash
cd /home/dan/Git/netflex
git add cnc/ncland/src/ncland_sim_tests.cpp
git commit -m "$(cat <<'EOF'
[ncland] #3c sim: 4 integration tests (login+cmd, post_command, drain, script-return)

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Self-Review (against the spec)

**Spec coverage:**
- §1 Scope (in-process socketpair, 6-fn C API, 5-fn Lua API, separate VM, explicit tick, history buffer) → Tasks 1-7 cover all. ✓
- §2 Architecture (socketpair + conn slot + lua_State + login_start) → Tasks 1 (slot), 2 (VM), 3 (coroutine), 7 (login_start). ✓
- §3.1 Struct → Task 1 (all fields declared up front; Task 6 adds `last_received_bookmark`). ✓
- §3.2 6 C functions → `open`/`close` Task 1, `step`+`received`+`received_matches` Task 3, `step_until` Task 7. ✓
- §4 5 Lua functions → `neid` Task 2, `send`+`log` Task 3, `expect` Tasks 4-5, `last_received` Task 6. ✓
- §5 Step algorithm (Phases A-D, sim_read, sim_try_match, sim_resume, expect yield) → Tasks 3-7. ✓
- §6 Error semantics → spread across Tasks 2 (script load failures), 5 (yield/resume), 8 (timeout), 9 (EOF). ✓
- §7 Testing → §7.1 self-tests in Tasks 1-9; §7.2 integration in Task 10. ✓
- §8 Files touched → matches the File Structure table at top. ✓
- §9 Deferred items → not implemented per spec. ✓

**Placeholder scan:** no "TBD" / "TODO" / "fill in" / "similar to Task N" — every code step shows complete code with exact placement.

**Type consistency:** `nclansim_t`, `nclansim_open` / `_close` / `_step` / `_step_until` / `_received` / `_received_matches`, `sim_resume`, `sim_try_match`, `sim_read_from_ncland`, `sim_drive_ncland`, `l_sim_send` / `_log` / `_expect` / `_expect_k` / `_last_received`, `sim_from_L`, `last_received_bookmark`, `coro_ref`/`coro_started`/`coro_done`/`got_eof`/`expect_active`/`expect_re`/`expect_re_valid`/`expect_deadline`/`last_match`/`history`/`rbuf` — used identically across tasks.

---

## Execution note

Tasks 1→10 are sequential. Tasks 1-6 build the sim primitive bottom-up; Task 7 wires it into ncland's main flow; Tasks 8-9 cover the harder error paths; Task 10 is purely additive integration tests. Recommended: subagent-driven, two-stage review per task.
