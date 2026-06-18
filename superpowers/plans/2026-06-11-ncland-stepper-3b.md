# ncland #3b — Step Engine (op:lua post_command + SIGTERM disconnect) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make ncland honor each NE's YAML `post_command` and `disconnect_steps` blocks (today: `op: lua` only), and turn SIGTERM/SIGHUP into a graceful per-NE drain instead of an immediate process exit.

**Architecture:** One C stepper (`step_advance` / `step_finish` in `ncland_stepper.{h,cpp}`) drives both blocks. Each `op: lua` step is a Lua coroutine resumed by the existing login bridge's machinery (extracted as `ncland_lua_coroutine_drive`). Two new conn states (`CS_POST_CMD`, `CS_DISCONNECTING`) sandwich the step block between `CS_CMD_PENDING`→`CS_READY` (post_command) and `CS_READY`→`CS_IDLE` (disconnect). SIGTERM sets `wh->shutting_down`, kicks `warehouse_begin_drain`, refuses new commands, and the main loop runs for up to `SHUTDOWN_GRACE_S` (10s) before hard-closing leftovers.

**Tech Stack:** C++17 via Lucent/AT&T **nmake** (`ncland.mk`), POSIX `regex.h`, Lua 5.3 (`-llua`), yaml-cpp (`-lyaml-cpp`), ZeroMQ (`-lzmq`), `nfunit-test.hpp` (run via `./ncland_unit_tests`).

**Design doc:** `~/docs/superpowers/specs/2026-06-11-ncland-stepper-3b-design.md` (read it first).

---

## Critical context for the implementer (read first)

- **Build:** Lucent/AT&T **nmake**, not GNU make. From `cnc/ncland/src`: `nmake -f ncland.mk <target>`. Ensure `BASE = git rev-parse --show-toplevel` (`/home/dan/Git/netflex`) and `VPATH`'s first segment == `$BASE`. If a build fails on missing `global*.nmk` / project headers, STOP and report — do not hack include paths.
- **Tests:** `nmake -f ncland.mk ncland_unit_tests && ./ncland_unit_tests` — baseline **108 passed / 0 failed / 6 skipped**. Keep it green (plus new tests). `nfunit-test.hpp` API: `TEST("suite","name") { REQUIRE(...); REQUIRE_EQ(a,b); }`. Run one suite: `./ncland_unit_tests <suite>`.
- **Commit trailer (every commit):** `Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>`. Work on branch `ncland-start`; no new branches/worktrees.
- **Doxygen** on new/modified functions, structs, and enums (project rule).
- **`conn_t` is POD** (`conn_init` memsets it; `warehouse_install_conn` memcpys it). New `conn_t` fields MUST be POD (int/time_t/enum int/byte arrays). `ncland_rsp_msg_t` is already POD (used by ZMQ raw-struct wire format). Do NOT add std:: members to `conn_t`.
- **Naming:** new public functions in this work use the `ncland_` prefix (`ncland_step_*`, `ncland_lua_*`). File-static helpers can drop the prefix. Macros use uppercase.
- **Lua coroutine state:** the per-conn coroutine reference is `c->lua_ref` (a `LUA_REGISTRYINDEX` ref; `LUA_NOREF` = none). Existing teardown is `if (c->lua_ref != LUA_NOREF) { luaL_unref(wh->L, LUA_REGISTRYINDEX, c->lua_ref); c->lua_ref = LUA_NOREF; }` — duplicated in `ncland_lua.cpp:586` and `ncland_conn.cpp:94`. Task 3 wraps it in `ncland_lua_release_thread`.
- **Existing test helper:** `wh_with_entry(ncland_wh_t &wh, int dtype, const char *prompt, const char *errpat)` at `ncland_unit_tests.cpp:490` builds a wh with one dtype entry (compiled prompt regex + optional error pattern). Reuse it. Extend (don't duplicate) for step-block tests in Task 5.
- **Existing login bridge:** `resume_login(ncland_wh_t *wh, conn_t *c, int narg)` at `ncland_lua.cpp:604`. Called from three call sites (login start at line 677, `on_data` at 728, `on_timeout` at 753). Task 2 extracts the generic coroutine-drive body out of `resume_login` so it can be shared by step coroutines; `resume_login` becomes a thin wrapper that preserves the existing CS_READY-on-LUA_OK behavior.
- **Existing signal handler:** signalfd loop in `ncland_warehouse.cpp:787` currently sets `wh->running = 0` on SIGTERM/SIGINT and breaks out. Task 9 changes this to kick `warehouse_begin_drain` instead, and adds a separate shutdown-deadline exit condition in the main loop.
- **`wh_with_entry` does NOT compile post_command/disconnect_steps** — those are `YAML::Node` members already on `ne_entry_t` from the registry. Tests that need a step block assign the `YAML::Node` directly. Helper `wh_with_step_entry` introduced in Task 5.
- **Spec coverage map** is in §8 of the design doc — refer to it whenever uncertain which file a piece of code belongs in.

---

## File Structure

| File | Responsibility in 3b |
|------|----------------------|
| `ncland.h` | `step_block_t` enum; 5 new `conn_t` fields; `CS_POST_CMD` / `CS_DISCONNECTING`; `SHUTDOWN_GRACE_S` / `STEP_DEFAULT_TMOUT_S`; `wh->shutting_down` / `shutdown_deadline`; decls. |
| `ncland_stepper.h` / `.cpp` (new) | `ncland_step_advance`, `ncland_step_finish`, `ncland_step_abort_for_eof`, `drain_one_ready_conn`, `ncland_warehouse_begin_drain`, `ncland_check_step_deadlines`, `ncland_warehouse_active_conn_count`. |
| `ncland_lua.h` / `.cpp` | Extract `ncland_lua_coroutine_drive` from `resume_login`; add `ncland_lua_step_start`, `ncland_lua_module_has_func`, `ncland_lua_release_thread`. |
| `ncland_registry.cpp` | `validate_steps` called from `ncland_registry_build`. |
| `ncland_worker.cpp` | `handle_ne_data`: post_command fork before `worker_send_rsp`; `CS_POST_CMD` / `CS_DISCONNECTING` dispatch branch; EOF branch calls `ncland_step_abort_for_eof` for those states; `next_timeout_ms` accounts for `step_deadline` and `shutdown_deadline`. |
| `ncland_warehouse.cpp` | Signalfd handler kicks off drain instead of breaking; main loop adds drain sweep + new exit condition; `warehouse_handle_cmd_from_zmq` shutdown-reject. |
| `ncland_stepper_tests.cpp` (new) | Unit tests for stepper + drain. |
| `ncland_registry_tests.cpp` | R-3b validation tests. |
| `ncland_unit_tests.cpp` | W-3b shutdown integration tests; shared `g_test_last_step_*` seam definitions. |
| `ncland.mk` | Add `ncland_stepper.o` to daemon target; add `ncland_stepper.o` + `ncland_stepper_tests.o` to `ncland_unit_tests` prerequisites. |

Tasks are sequential. Each builds and stays green on its own.

---

## Task 1: Foundations — states, conn_t fields, wh fields, constants

**Files:** `ncland.h`, `ncland_unit_tests.cpp`

- [ ] **Step 1: Add the enum, states, constants, conn fields, wh fields** in `ncland.h`.

Near the other `#define`s for shutdown/timeouts:

```c
#define SHUTDOWN_GRACE_S    10   /**< Seconds to drain disconnect_steps before hard-close on SIGTERM (#3b). */
#define STEP_DEFAULT_TMOUT_S 60  /**< Default per-step deadline for an op:lua step (#3b). */
```

In `conn_state_t`, append after `CS_KEEPALIVE`:

```c
    CS_POST_CMD,         /**< Running post_command coroutine; awaits step completion (#3b). */
    CS_DISCONNECTING,    /**< Running disconnect_steps coroutine; conn dropped on completion (#3b). */
```

Near `conn_state_t`, add the step-block tag enum:

```c
/** @brief Which step block is executing on a conn, or NONE if none (#3b). */
typedef enum {
    STEP_BLOCK_NONE        = 0,
    STEP_BLOCK_POST_CMD    = 1,
    STEP_BLOCK_DISCONNECT  = 2,
} step_block_t;
```

In `conn_t`, after the #3a `ka_deadline` field:

```c
    step_block_t     step_block;     /**< Which YAML step block is running, or STEP_BLOCK_NONE (#3b). */
    int              step_idx;       /**< Index of the current step within the block (#3b). */
    int              step_failed;    /**< 1 if any step raised/timed out — propagates to rsp.result (#3b). */
    time_t           step_deadline;  /**< Per-step deadline; 0 if not in a step (#3b). */
    ncland_rsp_msg_t pending_rsp;    /**< Cmd rsp captured at prompt-match, deferred until post_command finishes (#3b). */
```

In `ncland_wh_t`, after the other timer fields:

```c
    time_t  shutting_down;     /**< 0 = running; nonzero = SIGTERM timestamp; refuse new cmds, drain conns (#3b). */
    time_t  shutdown_deadline; /**< shutting_down + SHUTDOWN_GRACE_S; main loop breaks at or past this (#3b). */
```

Near the existing `g_test_*` seam externs:

```c
extern int g_test_last_step_block_finished;  /**< step_block_t cast to int; -1 if no step block has finished yet (#3b). */
extern int g_test_last_step_failed;          /**< 0/1 — step_failed at the last step_finish (#3b). */
extern int g_test_active_conn_count_at_break; /**< active conn count at main-loop graceful-shutdown break (#3b). */
```

- [ ] **Step 2: Add a tiny sanity test** (`ncland_unit_tests.cpp`, new suite `stepper`):

```cpp
TEST("stepper", "F-3b new states have distinct values") {
    REQUIRE(CS_POST_CMD != CS_DISCONNECTING);
    REQUIRE(CS_POST_CMD != CS_READY);
    REQUIRE(CS_POST_CMD != CS_CMD_PENDING);
    REQUIRE(CS_POST_CMD != CS_KEEPALIVE);
    REQUIRE(STEP_BLOCK_NONE == 0);
    REQUIRE(STEP_BLOCK_POST_CMD != STEP_BLOCK_DISCONNECT);
}

TEST("stepper", "F-3b conn_init zeros new step fields") {
    conn_t c; conn_init(&c, 7);
    REQUIRE_EQ((int)c.step_block, (int)STEP_BLOCK_NONE);
    REQUIRE_EQ(c.step_idx, 0);
    REQUIRE_EQ(c.step_failed, 0);
    REQUIRE_EQ((long)c.step_deadline, 0L);
    /* pending_rsp is memset by conn_init via the per-field zero — verify one byte */
    REQUIRE_EQ(c.pending_rsp.seq, 0);
}
```

- [ ] **Step 3: Define the test seams** in `ncland_unit_tests.cpp` (file-scope), so the link doesn't fail:

```cpp
int g_test_last_step_block_finished = -1;
int g_test_last_step_failed         = 0;
int g_test_active_conn_count_at_break = 0;
```

- [ ] **Step 4: Build + run.** `nmake -f ncland.mk ncland_unit_tests && ./ncland_unit_tests stepper` → 2 new tests pass. Full suite → `110 passed, 0 failed, 6 skipped`.

- [ ] **Step 5: Commit**

```bash
cd /home/dan/Git/netflex
git add cnc/ncland/src/ncland.h cnc/ncland/src/ncland_unit_tests.cpp
git commit -m "$(cat <<'EOF'
[ncland] #3b foundations: CS_POST_CMD/CS_DISCONNECTING + step_block_t + conn/wh fields

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 2: Extract `ncland_lua_coroutine_drive` from `resume_login` (refactor, no behavior change)

**Files:** `ncland_lua.h`, `ncland_lua.cpp`

The current `resume_login` does three things: (a) calls `lua_resume`, (b) interprets the return code, (c) on `LUA_OK` transitions the conn to `CS_READY`, applies adapted prompt, etc. (b) is generic and reusable by step coroutines; (a) needs the narg parameter on the initial call only; (c) is login-specific. Split: `ncland_lua_coroutine_drive(c, wh, narg)` does (a) + (b) and returns one of three return codes; `resume_login` becomes a thin caller that runs the login-specific (c) on `NCLAND_LUA_OK`.

- [ ] **Step 1: Define the return codes** in `ncland_lua.h`:

```c
/** @brief Result of driving a Lua coroutine one step (#3b). */
#define NCLAND_LUA_OK       0  /**< Coroutine returned cleanly. */
#define NCLAND_LUA_YIELDED  1  /**< Coroutine yielded (waiting for I/O / timer); resume on next event. */
#define NCLAND_LUA_FAILED  -1  /**< Coroutine raised an error; reason logged. */

/** @brief Resume the conn's Lua coroutine once. narg passed through to lua_resume.
 *  Returns NCLAND_LUA_OK / _YIELDED / _FAILED. Does NOT transition conn state — the caller
 *  decides what to do based on the return value (#3b). */
int ncland_lua_coroutine_drive(ncland_wh_t *wh, conn_t *c, int narg);
```

- [ ] **Step 2: Implement `ncland_lua_coroutine_drive`** in `ncland_lua.cpp`. Lift the body of the current `resume_login` (everything from `lua_State *co = ...` through the return-code switch, minus the login-specific CS_READY transition / adapted-prompt application). Keep behavior identical for the YIELD/ERR cases; on OK, return `NCLAND_LUA_OK` and let the caller decide.

The exact lift target depends on the current shape of `resume_login` — preserve every existing log line and every existing teardown call. The login-specific code on `LUA_OK` (`adapted_primary_regex` apply, `conn_set_state(c, CS_READY)`, `ncland_lua_release_thread` call, `LOG_INFO("session ready ...")`) stays in `resume_login`.

- [ ] **Step 3: Rewrite `resume_login`** as a thin wrapper:

```c
static int resume_login(ncland_wh_t *wh, conn_t *c, int narg)
{
    int rc = ncland_lua_coroutine_drive(wh, c, narg);
    if (rc == NCLAND_LUA_OK) {
        /* Login-specific: apply adapted prompt, drop coroutine ref, transition CS_READY.
         * (Move the existing post-LUA_OK body verbatim here.) */
        const char *adapted = ncland_lua_get_var(wh, c, "adapted_primary_regex");
        if (adapted && adapted[0]) {
            regex_t re;
            if (regcomp(&re, adapted, REG_EXTENDED) == 0) {
                if (c->prmpt1re_valid) regfree(&c->prmpt1re);
                c->prmpt1re = re;
                c->prmpt1re_valid = 1;
                LOG_INFO("neid=%d: applied adapted prompt /%s/", c->neid, adapted);
            } else {
                LOG_WARN("neid=%d: adapted_primary_regex /%s/ failed to compile (keeping prior)",
                         c->neid, adapted);
            }
        }
        /* Existing release_thread / LOG_INFO("session ready ...") / conn_set_state(CS_READY) lines */
    }
    return rc;
}
```

The exact mechanics of the existing release-thread / state-transition lines stay verbatim; this step is mechanical extract-and-rename, not a redesign.

- [ ] **Step 4: Build + full suite.** `nmake -f ncland.mk ncland_unit_tests && ./ncland_unit_tests` → still `110 passed, 0 failed, 6 skipped`. Login tests must remain green (they exercise the wrapper end-to-end).

- [ ] **Step 5: Commit**

```bash
cd /home/dan/Git/netflex
git add cnc/ncland/src/ncland_lua.h cnc/ncland/src/ncland_lua.cpp
git commit -m "$(cat <<'EOF'
[ncland] lua: extract ncland_lua_coroutine_drive from resume_login (no behavior change)

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 3: Add `ncland_lua_release_thread` + `ncland_lua_module_has_func` + `ncland_lua_step_start`

**Files:** `ncland_lua.h`, `ncland_lua.cpp`, `ncland_lua_tests.cpp`

- [ ] **Step 1: Declare the three helpers** in `ncland_lua.h`:

```c
/** @brief Release the conn's Lua coroutine ref (idempotent — safe if already NOREF) (#3b). */
void ncland_lua_release_thread(ncland_wh_t *wh, conn_t *c);

/** @brief 1 if the NE's lua module exports `func` as a function, 0 otherwise.
 *  Uses the module cache (same loader as the login bridge). lua_module_path is
 *  the path stored in ne_entry_t.lua_module — pass it verbatim (#3b). */
int ncland_lua_module_has_func(ncland_wh_t *wh, const char *lua_module_path, const char *func);

/** @brief Start a new step coroutine: load M[func] from the NE's module, push session
 *  userdata + global table + args, lua_resume. args_node is the YAML args: list (may be
 *  Null/absent → passed as Lua nil). Returns NCLAND_LUA_OK / _YIELDED / _FAILED (#3b). */
int ncland_lua_step_start(ncland_wh_t *wh, conn_t *c, const char *func, const YAML::Node &args_node);
```

(`YAML::Node` is already known to `ncland_lua.h` via the registry header; if not, include `<yaml-cpp/yaml.h>`.)

- [ ] **Step 2: Write the failing tests** (`ncland_lua_tests.cpp`, suite `lua`):

```cpp
TEST("lua", "L-3b release_thread is idempotent and clears lua_ref") {
    int sv[2]; REQUIRE_EQ(socketpair(AF_UNIX, SOCK_STREAM, 0, sv), 0);
    const char *src = "local M={} function M.login(s) end return M\n";
    ncland_wh_t wh{}; setup_login_wh(wh, 9101, src);
    conn_t *c = &wh.conn[3]; c->net_fd = sv[0]; c->dtype = 9101;
    REQUIRE_EQ(ncland_login_start(&wh, c), NCLAND_LOGIN_READY);  /* runs through */
    /* After login completion, lua_ref should already be NOREF — call release again, must not crash */
    ncland_lua_release_thread(&wh, c);
    REQUIRE_EQ(c->lua_ref, LUA_NOREF);
    ncland_lua_release_thread(&wh, c);  /* second call - still safe */
    REQUIRE_EQ(c->lua_ref, LUA_NOREF);
    ncland_lua_shutdown(&wh); close(sv[0]); close(sv[1]);
}

TEST("lua", "L-3b module_has_func detects present vs absent functions") {
    const char *src =
        "local M = {}\n"
        "function M.login(s) end\n"
        "function M.graceful_close(s) end\n"
        "M.not_a_function = 42\n"
        "return M\n";
    /* Write the source to a tmp file so module_has_func can load it as a path. */
    char path[] = "/tmp/ncland-lua-mhf-XXXXXX.lua";
    int fd = mkstemps(path, 4);  /* .lua suffix */
    REQUIRE(fd >= 0);
    REQUIRE_EQ((ssize_t)strlen(src), write(fd, src, strlen(src)));
    close(fd);

    ncland_wh_t wh{}; REQUIRE_EQ(ncland_lua_init(&wh), 0);
    REQUIRE_EQ(ncland_lua_module_has_func(&wh, path, "login"), 1);
    REQUIRE_EQ(ncland_lua_module_has_func(&wh, path, "graceful_close"), 1);
    REQUIRE_EQ(ncland_lua_module_has_func(&wh, path, "missing"), 0);
    REQUIRE_EQ(ncland_lua_module_has_func(&wh, path, "not_a_function"), 0);  /* present but not callable */
    ncland_lua_shutdown(&wh);
    unlink(path);
}

TEST("lua", "L-3b step_start runs a synchronous Lua function to completion") {
    int sv[2]; REQUIRE_EQ(socketpair(AF_UNIX, SOCK_STREAM, 0, sv), 0);
    const char *src =
        "local M={}\n"
        "function M.login(s) end\n"
        "function M.mark(s) s:set_var('ran', 'yes') end\n"
        "return M\n";
    ncland_wh_t wh{}; setup_login_wh(wh, 9102, src);
    conn_t *c = &wh.conn[4]; c->net_fd = sv[0]; c->dtype = 9102;
    REQUIRE_EQ(ncland_login_start(&wh, c), NCLAND_LOGIN_READY);
    /* After login, run a step that synchronously sets a var */
    YAML::Node empty;  /* no args */
    int rc = ncland_lua_step_start(&wh, c, "mark", empty);
    REQUIRE_EQ(rc, NCLAND_LUA_OK);
    const char *v = ncland_lua_get_var(&wh, c, "ran");
    REQUIRE(v != NULL);
    REQUIRE_EQ(strcmp(v, "yes"), 0);
    ncland_lua_release_thread(&wh, c);
    ncland_lua_shutdown(&wh); close(sv[0]); close(sv[1]);
}

TEST("lua", "L-3b step_start with args list arrives as Lua table") {
    int sv[2]; REQUIRE_EQ(socketpair(AF_UNIX, SOCK_STREAM, 0, sv), 0);
    const char *src =
        "local M={}\n"
        "function M.login(s) end\n"
        "function M.check_args(s, g, args)\n"
        "  s:set_var('a1', tostring(args[1]))\n"
        "  s:set_var('a2', tostring(args[2]))\n"
        "end\n"
        "return M\n";
    ncland_wh_t wh{}; setup_login_wh(wh, 9103, src);
    conn_t *c = &wh.conn[5]; c->net_fd = sv[0]; c->dtype = 9103;
    REQUIRE_EQ(ncland_login_start(&wh, c), NCLAND_LOGIN_READY);
    YAML::Node args = YAML::Load("[foo, 42]");
    REQUIRE_EQ(ncland_lua_step_start(&wh, c, "check_args", args), NCLAND_LUA_OK);
    REQUIRE_EQ(strcmp(ncland_lua_get_var(&wh, c, "a1"), "foo"), 0);
    REQUIRE_EQ(strcmp(ncland_lua_get_var(&wh, c, "a2"), "42"), 0);
    ncland_lua_release_thread(&wh, c);
    ncland_lua_shutdown(&wh); close(sv[0]); close(sv[1]);
}

TEST("lua", "L-3b step_start with raising function returns NCLAND_LUA_FAILED") {
    int sv[2]; REQUIRE_EQ(socketpair(AF_UNIX, SOCK_STREAM, 0, sv), 0);
    const char *src =
        "local M={}\n"
        "function M.login(s) end\n"
        "function M.boom(s) error('nope') end\n"
        "return M\n";
    ncland_wh_t wh{}; setup_login_wh(wh, 9104, src);
    conn_t *c = &wh.conn[6]; c->net_fd = sv[0]; c->dtype = 9104;
    REQUIRE_EQ(ncland_login_start(&wh, c), NCLAND_LOGIN_READY);
    YAML::Node empty;
    REQUIRE_EQ(ncland_lua_step_start(&wh, c, "boom", empty), NCLAND_LUA_FAILED);
    ncland_lua_release_thread(&wh, c);
    ncland_lua_shutdown(&wh); close(sv[0]); close(sv[1]);
}
```

- [ ] **Step 3: Run (fails)** — `./ncland_unit_tests lua` → 5 new tests fail (functions not defined / link error).

- [ ] **Step 4: Implement `ncland_lua_release_thread`** in `ncland_lua.cpp`:

```c
void ncland_lua_release_thread(ncland_wh_t *wh, conn_t *c)
{
    if (!wh || !c) return;
    if (c->lua_ref == LUA_NOREF) return;
    if (wh->L) luaL_unref(wh->L, LUA_REGISTRYINDEX, c->lua_ref);
    c->lua_ref = LUA_NOREF;
}
```

Replace the inline `luaL_unref(...); c->lua_ref = LUA_NOREF;` pairs at `ncland_lua.cpp:586` and `ncland_conn.cpp:94` with calls to this helper.

- [ ] **Step 5: Implement `ncland_lua_module_has_func`** in `ncland_lua.cpp`:

```c
int ncland_lua_module_has_func(ncland_wh_t *wh, const char *lua_module_path, const char *func)
{
    if (!wh || !wh->L || !lua_module_path || !func) return 0;
    /* Load + cache via the same path the login bridge uses. The existing module-cache
     * helper is ncland_lua_load_module(wh, path) — returns 0 on success and leaves the
     * module table on the stack at index -1, or returns nonzero on failure. */
    int top = lua_gettop(wh->L);
    if (ncland_lua_load_module(wh, lua_module_path) != 0) {
        lua_settop(wh->L, top);
        return 0;
    }
    lua_getfield(wh->L, -1, func);            /* push M[func] */
    int is_func = lua_isfunction(wh->L, -1);
    lua_settop(wh->L, top);                   /* clean stack */
    return is_func ? 1 : 0;
}
```

(If the existing module-loader helper is named differently, adjust — match the name used by `ncland_login_start`.)

- [ ] **Step 6: Implement `ncland_lua_step_start`** in `ncland_lua.cpp`. Pattern mirrors `ncland_login_start`:

```c
int ncland_lua_step_start(ncland_wh_t *wh, conn_t *c, const char *func, const YAML::Node &args_node)
{
    if (!wh || !wh->L || !c || !func) return NCLAND_LUA_FAILED;
    /* Resolve module path: from the ne_entry for c->dtype, .lua_module field. */
    const ne_entry_t *e = ncland_registry_find(&wh->registry, c->dtype);
    if (!e || e->lua_module.empty()) {
        LOG_WARN("step_start: no lua_module for dtype %d", c->dtype);
        return NCLAND_LUA_FAILED;
    }
    /* Build the resolved path (same logic the login bridge uses — likely NCLAND_NE_DIR + "/" + e->lua_module). */
    char path[512];
    snprintf(path, sizeof(path), "%s/%s", NCLAND_NE_DIR, e->lua_module.c_str());

    /* Create a fresh coroutine and stash it in the registry (replacing any prior). */
    ncland_lua_release_thread(wh, c);
    lua_State *co = lua_newthread(wh->L);
    c->lua_ref = luaL_ref(wh->L, LUA_REGISTRYINDEX);

    /* Load the module on the coroutine stack, push M[func]. */
    if (ncland_lua_load_module_into(co, path) != 0) {
        LOG_WARN("step_start: failed to load module %s", path);
        ncland_lua_release_thread(wh, c);
        return NCLAND_LUA_FAILED;
    }
    lua_getfield(co, -1, func);
    if (!lua_isfunction(co, -1)) {
        LOG_WARN("step_start: %s: no function '%s'", path, func);
        ncland_lua_release_thread(wh, c);
        return NCLAND_LUA_FAILED;
    }
    lua_remove(co, -2);  /* drop M; leave function on top */

    /* Push session userdata, global table, args table. */
    ncland_lua_push_session_ud(co, wh, c);    /* existing helper used by login_start */
    ncland_lua_push_global(co, wh);
    if (args_node && args_node.IsSequence()) {
        lua_newtable(co);
        int i = 1;
        for (auto it = args_node.begin(); it != args_node.end(); ++it, ++i) {
            std::string v = it->as<std::string>();
            /* Best-effort number conversion: if value parses as integer, push as integer; else string. */
            char *end;
            long n = strtol(v.c_str(), &end, 10);
            if (*end == '\0') lua_pushinteger(co, n);
            else              lua_pushlstring(co, v.c_str(), v.size());
            lua_rawseti(co, -2, i);
        }
    } else {
        lua_pushnil(co);
    }

    return ncland_lua_coroutine_drive(wh, c, 3);
}
```

The exact names of `ncland_lua_load_module` / `ncland_lua_load_module_into` / `ncland_lua_push_session_ud` / `ncland_lua_push_global` should match what `ncland_login_start` already calls. If they're file-static, either expose them in the header or hand-roll the equivalent here (same as login does).

- [ ] **Step 7: Run tests (pass)** — `./ncland_unit_tests lua` → 5 new tests pass. Full suite → `115 passed, 0 failed, 6 skipped`.

- [ ] **Step 8: Commit**

```bash
cd /home/dan/Git/netflex
git add cnc/ncland/src/ncland_lua.h cnc/ncland/src/ncland_lua.cpp cnc/ncland/src/ncland_lua_tests.cpp cnc/ncland/src/ncland_conn.cpp
git commit -m "$(cat <<'EOF'
[ncland] lua: release_thread helper + module_has_func + step_start

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 4: Registry validation — `validate_steps`

**Files:** `ncland_registry.cpp`, `ncland_registry_tests.cpp`

- [ ] **Step 1: Write the failing tests** (`ncland_registry_tests.cpp`, suite `registry`):

```cpp
/* Helper: emit a tmp Lua module so validation can confirm func existence. */
static std::string write_tmp_lua(const char *body) {
    char path[] = "/tmp/ncland-reg-3b-XXXXXX.lua";
    int fd = mkstemps(path, 4);
    REQUIRE(fd >= 0);
    REQUIRE_EQ((ssize_t)strlen(body), write(fd, body, strlen(body)));
    close(fd);
    return std::string(path);
}

TEST("registry", "R-3b op:lua compiles when func exists") {
    std::string lua = write_tmp_lua(
        "local M={} function M.login(s) end function M.graceful_close(s) end return M\n");
    char yaml[2048];
    snprintf(yaml, sizeof(yaml),
        "ne_type: CIENA_RLS\nvendor: x\nschema_version: 1\n"
        "transport: {protocol: [ssh]}\n"
        "prompt: {primary_regex: '[a-z]+#'}\n"
        "disconnect_steps:\n  - op: lua\n    func: graceful_close\n"
        "lua_module: %s\n", lua.c_str());
    ne_entry_t e; std::string err;
    REQUIRE_EQ(ncland_registry_build(YAML::Load(yaml), &e, &err), 0);
    /* free the compiled regexes the build left behind */
    if (e.prmpt1_valid) regfree(&e.prmpt1re);
    unlink(lua.c_str());
}

TEST("registry", "R-3b unknown op rejected") {
    std::string lua = write_tmp_lua("local M={} function M.login(s) end return M\n");
    char yaml[2048];
    snprintf(yaml, sizeof(yaml),
        "ne_type: CIENA_RLS\nvendor: x\nschema_version: 1\n"
        "transport: {protocol: [ssh]}\n"
        "prompt: {primary_regex: '[a-z]+#'}\n"
        "post_command:\n  - op: bogus\n"
        "lua_module: %s\n", lua.c_str());
    ne_entry_t e; std::string err;
    REQUIRE(ncland_registry_build(YAML::Load(yaml), &e, &err) != 0);
    REQUIRE(err.find("unknown op") != std::string::npos);
    REQUIRE(err.find("step[0]") != std::string::npos);
    unlink(lua.c_str());
}

TEST("registry", "R-3b op:lua missing func rejected") {
    std::string lua = write_tmp_lua("local M={} function M.login(s) end return M\n");
    char yaml[2048];
    snprintf(yaml, sizeof(yaml),
        "ne_type: CIENA_RLS\nvendor: x\nschema_version: 1\n"
        "transport: {protocol: [ssh]}\n"
        "prompt: {primary_regex: '[a-z]+#'}\n"
        "post_command:\n  - op: lua\n"
        "lua_module: %s\n", lua.c_str());
    ne_entry_t e; std::string err;
    REQUIRE(ncland_registry_build(YAML::Load(yaml), &e, &err) != 0);
    REQUIRE(err.find("func") != std::string::npos);
    unlink(lua.c_str());
}

TEST("registry", "R-3b op:lua func not in module rejected") {
    std::string lua = write_tmp_lua("local M={} function M.login(s) end return M\n");
    char yaml[2048];
    snprintf(yaml, sizeof(yaml),
        "ne_type: CIENA_RLS\nvendor: x\nschema_version: 1\n"
        "transport: {protocol: [ssh]}\n"
        "prompt: {primary_regex: '[a-z]+#'}\n"
        "post_command:\n  - op: lua\n    func: nonexistent\n"
        "lua_module: %s\n", lua.c_str());
    ne_entry_t e; std::string err;
    REQUIRE(ncland_registry_build(YAML::Load(yaml), &e, &err) != 0);
    REQUIRE(err.find("no such function") != std::string::npos);
    unlink(lua.c_str());
}

TEST("registry", "R-3b block must be sequence") {
    std::string lua = write_tmp_lua("local M={} function M.login(s) end return M\n");
    char yaml[2048];
    snprintf(yaml, sizeof(yaml),
        "ne_type: CIENA_RLS\nvendor: x\nschema_version: 1\n"
        "transport: {protocol: [ssh]}\n"
        "prompt: {primary_regex: '[a-z]+#'}\n"
        "post_command: notalist\n"
        "lua_module: %s\n", lua.c_str());
    ne_entry_t e; std::string err;
    REQUIRE(ncland_registry_build(YAML::Load(yaml), &e, &err) != 0);
    unlink(lua.c_str());
}

TEST("registry", "R-3b empty block accepted") {
    std::string lua = write_tmp_lua("local M={} function M.login(s) end return M\n");
    char yaml[2048];
    snprintf(yaml, sizeof(yaml),
        "ne_type: CIENA_RLS\nvendor: x\nschema_version: 1\n"
        "transport: {protocol: [ssh]}\n"
        "prompt: {primary_regex: '[a-z]+#'}\n"
        "post_command: []\ndisconnect_steps: []\n"
        "lua_module: %s\n", lua.c_str());
    ne_entry_t e; std::string err;
    REQUIRE_EQ(ncland_registry_build(YAML::Load(yaml), &e, &err), 0);
    if (e.prmpt1_valid) regfree(&e.prmpt1re);
    unlink(lua.c_str());
}
```

Note: `ncland_registry_build` today takes a YAML::Node and an `ne_entry_t *`, plus an `std::string *` for errors. The existing tests in `ncland_registry_tests.cpp` show the pattern — match it.

- [ ] **Step 2: Run (fails)** — `./ncland_unit_tests registry` → all 6 new tests fail (validation not implemented; bad YAML is currently accepted silently).

- [ ] **Step 3: Implement `validate_steps`** in `ncland_registry.cpp`. Add a static function and call it from `ncland_registry_build` after the existing regex compiles, before `return 0`:

```c
static int validate_step_block(const YAML::Node &block, const char *block_name,
                               const std::string &lua_module_path, ncland_wh_t *wh,
                               std::string *err)
{
    if (!block) return 0;                              /* absent → ok */
    if (!block.IsSequence()) {
        *err = std::string(block_name) + ": must be a YAML sequence";
        return -1;
    }
    for (size_t i = 0; i < block.size(); i++) {
        const YAML::Node &step = block[i];
        if (!step.IsMap()) {
            *err = std::string(block_name) + " step[" + std::to_string(i) + "]: must be a map";
            return -1;
        }
        if (!step["op"]) {
            *err = std::string(block_name) + " step[" + std::to_string(i) + "]: missing 'op'";
            return -1;
        }
        std::string op = step["op"].as<std::string>();
        if (op != "lua") {
            *err = std::string(block_name) + " step[" + std::to_string(i)
                 + "]: unknown op '" + op + "' (known: lua)";
            return -1;
        }
        if (!step["func"] || !step["func"].IsScalar() || step["func"].as<std::string>().empty()) {
            *err = std::string(block_name) + " step[" + std::to_string(i)
                 + "]: op:lua requires non-empty 'func'";
            return -1;
        }
        std::string func = step["func"].as<std::string>();
        if (step["args"] && !step["args"].IsSequence()) {
            *err = std::string(block_name) + " step[" + std::to_string(i)
                 + "]: 'args' must be a sequence";
            return -1;
        }
        /* Module presence check is only possible if wh is available — at registry_build
         * time we have lua_module_path; validate function existence on the wh's Lua state. */
        if (wh && !ncland_lua_module_has_func(wh, lua_module_path.c_str(), func.c_str())) {
            *err = std::string(block_name) + " step[" + std::to_string(i)
                 + "]: no such function '" + func + "' in " + lua_module_path;
            return -1;
        }
    }
    return 0;
}
```

`ncland_registry_build`'s signature today doesn't take a `wh*`. The simplest extension: add an optional `ncland_wh_t *wh` parameter (default `nullptr`); registry-load callers pass the live wh, the existing tests can pass `nullptr` to skip the func-existence check. **However**, R-3b tests need the func-existence check to fire — give them a wh: extend `ncland_registry_build`'s signature to take `ncland_wh_t *wh`:

```c
int ncland_registry_build(const YAML::Node &node, ne_entry_t *out, std::string *err, ncland_wh_t *wh = nullptr);
```

Update the existing call in `ncland_registry_load` to pass the live `wh`. Pre-existing tests keep working with the default `nullptr`. R-3b tests build a wh with a minimum-viable Lua VM:

```cpp
/* In ncland_registry_tests.cpp, at file scope: */
static ncland_wh_t *get_test_wh() {
    static ncland_wh_t wh;
    static bool inited = false;
    if (!inited) { ncland_lua_init(&wh); inited = true; }
    return &wh;
}
/* …and pass get_test_wh() to ncland_registry_build calls that need func-existence checks. */
```

Then add the four `validate_step_block` calls inside `ncland_registry_build`:

```c
    if (validate_step_block(out->post_command,     "post_command",     out->lua_module, wh, err) != 0) {
        /* unwind any partial state — same regex-cleanup pattern as the existing fail() helper */
        return -1;
    }
    if (validate_step_block(out->disconnect_steps, "disconnect_steps", out->lua_module, wh, err) != 0) {
        return -1;
    }
```

- [ ] **Step 4: Run tests (pass)** — `./ncland_unit_tests registry` → 6 new tests pass. Full suite → `121 passed, 0 failed, 6 skipped`.

- [ ] **Step 5: Commit**

```bash
cd /home/dan/Git/netflex
git add cnc/ncland/src/ncland_registry.h cnc/ncland/src/ncland_registry.cpp cnc/ncland/src/ncland_registry_tests.cpp
git commit -m "$(cat <<'EOF'
[ncland] registry: validate post_command/disconnect_steps op:lua + func existence

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 5: Stepper module — `ncland_step_advance` + `ncland_step_finish`

**Files:** `ncland_stepper.h` (new), `ncland_stepper.cpp` (new), `ncland_stepper_tests.cpp` (new), `ncland.mk`

- [ ] **Step 1: Wire the new files into the build** (`ncland.mk`).

Add `ncland_stepper.o` to the daemon target's object list (alongside `ncland_worker.o` / `ncland_warehouse.o`). Add `ncland_stepper.o` and `ncland_stepper_tests.o` to `ncland_unit_tests`'s prerequisite list. Mirror the rule shape used by other `*.cpp` files in `ncland.mk`:

```
ncland_stepper.o : ncland_stepper.cpp ncland_stepper.h ncland.h
ncland_stepper_tests.o : ncland_stepper_tests.cpp ncland.h ncland_stepper.h
```

(nmake handles the compile via the implicit `.cpp.o` rule already present.)

- [ ] **Step 2: Create `ncland_stepper.h`:**

```c
/** @file ncland_stepper.h
 *  Per-conn step engine for ncland (#3b). Drives op:lua step blocks
 *  (post_command, disconnect_steps) via the shared Lua coroutine bridge. */
#pragma once
#include "ncland.h"

/** @brief Start (or continue, from step_idx) running c's current step_block.
 *  Calls step_finish on completion or fatal failure. Idempotent at block end. */
void ncland_step_advance(conn_t *c, ncland_wh_t *wh);

/** @brief Finish the current step block: ship pending_rsp (post_cmd) or drop conn
 *  (disconnect), clear stepper fields, release Lua coroutine. Idempotent. */
void ncland_step_finish(conn_t *c, ncland_wh_t *wh);

/** @brief Called from the EOF branch in handle_ne_data when c->state is
 *  CS_POST_CMD or CS_DISCONNECTING. Marks failed, ships FAILURE rsp (post_cmd),
 *  drops conn. Caller should not call warehouse_drop_conn separately. */
void ncland_step_abort_for_eof(conn_t *c, ncland_wh_t *wh);

/** @brief Mark step_failed and finish for any conn past step_deadline. */
void ncland_check_step_deadlines(ncland_wh_t *wh);
```

- [ ] **Step 3: Write the failing tests** (`ncland_stepper_tests.cpp`):

```cpp
#include "ncland.h"
#include "ncland_stepper.h"
#include "ncland_lua.h"
#include "ncland_registry.h"
#include "nfunit-test.hpp"
#include <sys/socket.h>
#include <fcntl.h>
#include <unistd.h>

/* Build a wh with one dtype entry that has a step block.
 * lua_src is the Lua module body (must define login + the named func(s)).
 * post_cmd_yaml / disconnect_yaml: YAML fragments for the blocks ([] for empty). */
static void wh_with_step_entry(ncland_wh_t &wh, int dtype, const char *prompt,
                               const char *lua_src,
                               const char *post_cmd_yaml,
                               const char *disconnect_yaml)
{
    for (int i = 0; i < MAX_CONNS; i++) conn_init(&wh.conn[i], i);
    REQUIRE_EQ(ncland_lua_init(&wh), 0);
    /* Write the Lua source to a tmp file and register the path as lua_module. */
    char path[] = "/tmp/ncland-stepper-XXXXXX.lua";
    int fd = mkstemps(path, 4);
    REQUIRE(fd >= 0);
    REQUIRE_EQ((ssize_t)strlen(lua_src), write(fd, lua_src, strlen(lua_src)));
    close(fd);
    ne_entry_t e;
    e.dtype = dtype; e.ne_type = "TEST_NE"; e.lua_module = path;
    e.primary_regex = prompt;
    REQUIRE_EQ(regcomp(&e.prmpt1re, prompt, REG_EXTENDED), 0); e.prmpt1_valid = true;
    e.post_command     = YAML::Load(post_cmd_yaml);
    e.disconnect_steps = YAML::Load(disconnect_yaml);
    wh.registry.by_dtype[dtype] = e;
}

TEST("stepper", "S-3b empty post_command finishes immediately, no step ran") {
    int sv[2]; REQUIRE_EQ(socketpair(AF_UNIX, SOCK_STREAM, 0, sv), 0); fcntl(sv[0], F_SETFL, O_NONBLOCK);
    ncland_wh_t wh{};
    wh_with_step_entry(wh, 9201, "[a-z0-9]+#",
        "local M={} function M.login(s) end return M\n",
        "[]", "[]");
    conn_t *c = &wh.conn[3]; c->net_fd = sv[0]; c->dtype = 9201; c->state = CS_POST_CMD;
    c->step_block = STEP_BLOCK_POST_CMD; c->step_idx = 0; c->step_failed = 0;
    memset(&c->pending_rsp, 0, sizeof(c->pending_rsp));
    c->pending_rsp.seq = 42; c->pending_rsp.result = NCLAND_RESULT_SUCCESS;
    snprintf(c->pending_rsp.text, sizeof(c->pending_rsp.text), "data");

    g_test_last_step_block_finished = -1;
    ncland_step_advance(c, &wh);
    REQUIRE_EQ(g_test_last_step_block_finished, (int)STEP_BLOCK_POST_CMD);
    REQUIRE_EQ(g_test_last_step_failed, 0);
    REQUIRE_EQ((int)c->state, (int)CS_READY);
    ncland_lua_shutdown(&wh); close(sv[0]); close(sv[1]);
}

TEST("stepper", "S-3b single op:lua step runs synchronously and ships rsp") {
    int sv[2]; REQUIRE_EQ(socketpair(AF_UNIX, SOCK_STREAM, 0, sv), 0); fcntl(sv[0], F_SETFL, O_NONBLOCK);
    ncland_wh_t wh{};
    wh_with_step_entry(wh, 9202, "[a-z0-9]+#",
        "local M={} function M.login(s) end function M.mark(s) s:set_var('ran','yes') end return M\n",
        "[{op: lua, func: mark}]", "[]");
    conn_t *c = &wh.conn[4]; c->net_fd = sv[0]; c->dtype = 9202; c->state = CS_POST_CMD;
    c->step_block = STEP_BLOCK_POST_CMD; c->step_idx = 0; c->step_failed = 0;
    memset(&c->pending_rsp, 0, sizeof(c->pending_rsp));
    c->pending_rsp.seq = 1; c->pending_rsp.result = NCLAND_RESULT_SUCCESS;

    g_test_last_step_block_finished = -1; g_test_last_step_failed = -1;
    ncland_step_advance(c, &wh);
    REQUIRE_EQ(g_test_last_step_block_finished, (int)STEP_BLOCK_POST_CMD);
    REQUIRE_EQ(g_test_last_step_failed, 0);
    REQUIRE_EQ((int)c->state, (int)CS_READY);
    const char *v = ncland_lua_get_var(&wh, c, "ran");
    REQUIRE(v != NULL); REQUIRE_EQ(strcmp(v, "yes"), 0);
    ncland_lua_shutdown(&wh); close(sv[0]); close(sv[1]);
}

TEST("stepper", "S-3b raising step -> FAILURE, original rsp.text preserved") {
    int sv[2]; REQUIRE_EQ(socketpair(AF_UNIX, SOCK_STREAM, 0, sv), 0); fcntl(sv[0], F_SETFL, O_NONBLOCK);
    ncland_wh_t wh{};
    wh_with_step_entry(wh, 9203, "[a-z0-9]+#",
        "local M={} function M.login(s) end function M.boom(s) error('nope') end return M\n",
        "[{op: lua, func: boom}]", "[]");
    conn_t *c = &wh.conn[5]; c->net_fd = sv[0]; c->dtype = 9203; c->state = CS_POST_CMD;
    c->step_block = STEP_BLOCK_POST_CMD; c->step_idx = 0; c->step_failed = 0;
    memset(&c->pending_rsp, 0, sizeof(c->pending_rsp));
    c->pending_rsp.seq = 1; c->pending_rsp.result = NCLAND_RESULT_SUCCESS;
    snprintf(c->pending_rsp.text, sizeof(c->pending_rsp.text), "captured-text");

    ncland_step_advance(c, &wh);
    REQUIRE_EQ(g_test_last_step_failed, 1);
    REQUIRE_EQ(c->pending_rsp.result, NCLAND_RESULT_FAILURE);
    /* Text NOT redacted by step failure (§6.4 of spec) — verified via worker_send_rsp seam: */
    REQUIRE_EQ(strcmp(g_test_last_sent_rsp_text, "captured-text"), 0);
    REQUIRE_EQ((int)c->state, (int)CS_READY);
    ncland_lua_shutdown(&wh); close(sv[0]); close(sv[1]);
}

TEST("stepper", "S-3b multi-step: step 2 fails, step 3 skipped") {
    int sv[2]; REQUIRE_EQ(socketpair(AF_UNIX, SOCK_STREAM, 0, sv), 0); fcntl(sv[0], F_SETFL, O_NONBLOCK);
    ncland_wh_t wh{};
    const char *src =
        "local M={}\n"
        "function M.login(s) end\n"
        "function M.a(s) s:set_var('a','1') end\n"
        "function M.b(s) error('boom') end\n"
        "function M.c(s) s:set_var('c','1') end\n"
        "return M\n";
    wh_with_step_entry(wh, 9204, "[a-z0-9]+#", src,
        "[{op: lua, func: a}, {op: lua, func: b}, {op: lua, func: c}]", "[]");
    conn_t *c = &wh.conn[6]; c->net_fd = sv[0]; c->dtype = 9204; c->state = CS_POST_CMD;
    c->step_block = STEP_BLOCK_POST_CMD; c->step_idx = 0; c->step_failed = 0;
    memset(&c->pending_rsp, 0, sizeof(c->pending_rsp));

    ncland_step_advance(c, &wh);
    REQUIRE_EQ(g_test_last_step_failed, 1);
    REQUIRE_EQ(c->pending_rsp.result, NCLAND_RESULT_FAILURE);
    /* a ran, c did not */
    const char *va = ncland_lua_get_var(&wh, c, "a");
    REQUIRE(va != NULL); REQUIRE_EQ(strcmp(va, "1"), 0);
    REQUIRE(ncland_lua_get_var(&wh, c, "c") == NULL);
    ncland_lua_shutdown(&wh); close(sv[0]); close(sv[1]);
}

TEST("stepper", "S-3b op:lua args list arrives as Lua table") {
    int sv[2]; REQUIRE_EQ(socketpair(AF_UNIX, SOCK_STREAM, 0, sv), 0); fcntl(sv[0], F_SETFL, O_NONBLOCK);
    ncland_wh_t wh{};
    wh_with_step_entry(wh, 9205, "[a-z0-9]+#",
        "local M={} function M.login(s) end\n"
        "function M.ck(s,g,a) s:set_var('x1',tostring(a[1])); s:set_var('x2',tostring(a[2])) end\n"
        "return M\n",
        "[{op: lua, func: ck, args: [hello, 7]}]", "[]");
    conn_t *c = &wh.conn[7]; c->net_fd = sv[0]; c->dtype = 9205; c->state = CS_POST_CMD;
    c->step_block = STEP_BLOCK_POST_CMD; c->step_idx = 0; c->step_failed = 0;
    memset(&c->pending_rsp, 0, sizeof(c->pending_rsp));

    ncland_step_advance(c, &wh);
    REQUIRE_EQ(strcmp(ncland_lua_get_var(&wh, c, "x1"), "hello"), 0);
    REQUIRE_EQ(strcmp(ncland_lua_get_var(&wh, c, "x2"), "7"), 0);
    ncland_lua_shutdown(&wh); close(sv[0]); close(sv[1]);
}

TEST("stepper", "S-3b disconnect_steps runs and drops conn at end") {
    int sv[2]; REQUIRE_EQ(socketpair(AF_UNIX, SOCK_STREAM, 0, sv), 0); fcntl(sv[0], F_SETFL, O_NONBLOCK);
    ncland_wh_t wh{};
    wh_with_step_entry(wh, 9206, "[a-z0-9]+#",
        "local M={} function M.login(s) end function M.bye(s) s:set_var('bye','y') end return M\n",
        "[]", "[{op: lua, func: bye}]");
    conn_t *c = &wh.conn[8]; c->net_fd = sv[0]; c->dtype = 9206; c->state = CS_DISCONNECTING;
    c->step_block = STEP_BLOCK_DISCONNECT; c->step_idx = 0; c->step_failed = 0;

    ncland_step_advance(c, &wh);
    REQUIRE_EQ(g_test_last_step_block_finished, (int)STEP_BLOCK_DISCONNECT);
    REQUIRE_EQ((int)c->state, (int)CS_IDLE);          /* warehouse_drop_conn ran */
    ncland_lua_shutdown(&wh); close(sv[0]); close(sv[1]);
}

TEST("stepper", "S-3b disconnect_steps failure still drops conn") {
    int sv[2]; REQUIRE_EQ(socketpair(AF_UNIX, SOCK_STREAM, 0, sv), 0); fcntl(sv[0], F_SETFL, O_NONBLOCK);
    ncland_wh_t wh{};
    wh_with_step_entry(wh, 9207, "[a-z0-9]+#",
        "local M={} function M.login(s) end function M.bye(s) error('oh no') end return M\n",
        "[]", "[{op: lua, func: bye}]");
    conn_t *c = &wh.conn[9]; c->net_fd = sv[0]; c->dtype = 9207; c->state = CS_DISCONNECTING;
    c->step_block = STEP_BLOCK_DISCONNECT; c->step_idx = 0; c->step_failed = 0;

    ncland_step_advance(c, &wh);
    REQUIRE_EQ(g_test_last_step_failed, 1);
    REQUIRE_EQ((int)c->state, (int)CS_IDLE);          /* still dropped */
    ncland_lua_shutdown(&wh); close(sv[0]); close(sv[1]);
}

TEST("stepper", "S-3b step_finish is idempotent") {
    ncland_wh_t wh{};
    for (int i = 0; i < MAX_CONNS; i++) conn_init(&wh.conn[i], i);
    conn_t *c = &wh.conn[10];
    /* No step block — finish must be a no-op */
    g_test_last_step_block_finished = -1;
    ncland_step_finish(c, &wh);
    REQUIRE_EQ(g_test_last_step_block_finished, -1);  /* never set */
    REQUIRE_EQ((int)c->step_block, (int)STEP_BLOCK_NONE);
}
```

This test file references `g_test_last_sent_rsp_text` — that's a new test seam to add inside `worker_send_rsp` (Task 5 Step 5). Also, the suite uses `ncland_step_advance` to drive the engine; the EPOLLIN-driven YIELDED-then-resume path is covered in Task 6's tests (because it needs `handle_ne_data` plumbing).

- [ ] **Step 4: Run (fails)** — `./ncland_unit_tests stepper` → link errors (`ncland_step_advance` / `ncland_step_finish` undefined).

- [ ] **Step 5: Add the rsp-text seam** in `ncland_worker.cpp`. Near the other `g_test_*` seams:

```c
char g_test_last_sent_rsp_text[NCLAND_RSP_TEXT_LEN] = {0};
```

In `worker_send_rsp`, right after the function body completes the send (before `return`), copy the text:

```c
    snprintf(g_test_last_sent_rsp_text, sizeof(g_test_last_sent_rsp_text), "%s", rsp ? rsp->text : "");
```

Declare in `ncland.h` near the other test seams:

```c
extern char g_test_last_sent_rsp_text[NCLAND_RSP_TEXT_LEN]; /**< Text of most recent rsp shipped (#3b). */
```

(If `NCLAND_RSP_TEXT_LEN` isn't a macro today, use `sizeof(ncland_rsp_msg_t::text)` or the literal size used by the struct.)

- [ ] **Step 6: Implement `ncland_stepper.cpp`:**

```cpp
/** @file ncland_stepper.cpp
 *  Per-conn step engine (#3b). See ~/docs/superpowers/specs/2026-06-11-ncland-stepper-3b-design.md */
#include "ncland_stepper.h"
#include "ncland_lua.h"
#include "ncland_registry.h"
#include "nflog.hpp"
#include <string.h>
#include <time.h>

/* Forward-declare warehouse symbols we call. */
extern void warehouse_drop_conn(ncland_wh_t *wh, conn_t *c);
extern void worker_send_rsp(ncland_wh_t *wh, const ncland_rsp_msg_t *rsp);

/* Test seams defined in ncland_unit_tests.cpp / ncland.h */
extern int g_test_last_step_block_finished;
extern int g_test_last_step_failed;

void ncland_step_finish(conn_t *c, ncland_wh_t *wh)
{
    if (!c) return;
    if (c->step_block == STEP_BLOCK_NONE) return;  /* idempotent */

    step_block_t which = c->step_block;
    c->step_block    = STEP_BLOCK_NONE;
    c->step_idx      = 0;
    c->step_deadline = 0;
    ncland_lua_release_thread(wh, c);

    g_test_last_step_block_finished = (int)which;
    g_test_last_step_failed         = c->step_failed;

    if (which == STEP_BLOCK_POST_CMD) {
        if (c->step_failed) c->pending_rsp.result = NCLAND_RESULT_FAILURE;
        worker_send_rsp(wh, &c->pending_rsp);
        memset(&c->pending_rsp, 0, sizeof(c->pending_rsp));
        c->rlen = 0; c->rbuf[0] = '\0';
        c->step_failed = 0;
        conn_set_state(c, CS_READY);
        return;
    }
    if (which == STEP_BLOCK_DISCONNECT) {
        if (c->step_failed)
            LOG_WARN("conn[%d] neid=%d: disconnect_steps failed; dropping anyway",
                     c->id, c->neid);
        warehouse_drop_conn(wh, c);
        return;
    }
}

void ncland_step_advance(conn_t *c, ncland_wh_t *wh)
{
    if (!c || !wh) return;
    const ne_entry_t *e = ncland_registry_find(&wh->registry, c->dtype);
    if (!e) {
        LOG_WARN("conn[%d] dtype=%d: no registry entry; aborting step block", c->id, c->dtype);
        c->step_failed = 1;
        ncland_step_finish(c, wh);
        return;
    }

    const YAML::Node &block = (c->step_block == STEP_BLOCK_POST_CMD)
                              ? e->post_command : e->disconnect_steps;

    while (c->step_idx < (int)block.size()) {
        const YAML::Node &step = block[c->step_idx];
        /* Validation at load (Task 4) guarantees op:lua. Defensive check anyway. */
        std::string op = step["op"] ? step["op"].as<std::string>() : std::string();
        if (op == "lua") {
            std::string func = step["func"].as<std::string>();
            const YAML::Node &args = step["args"];
            int rc = ncland_lua_step_start(wh, c, func.c_str(), args);
            if (rc == NCLAND_LUA_YIELDED) {
                c->step_deadline = time(NULL) + STEP_DEFAULT_TMOUT_S;
                return;  /* resume on EPOLLIN via coroutine_drive in handle_ne_data */
            }
            if (rc == NCLAND_LUA_FAILED) {
                c->step_failed = 1;
                LOG_WARN("conn[%d] neid=%d: step[%d] op:lua func='%s' failed",
                         c->id, c->neid, c->step_idx, func.c_str());
                break;
            }
            /* NCLAND_LUA_OK — synchronous completion; advance */
        } else {
            LOG_WARN("conn[%d] step[%d] unknown op '%s' (validation should have caught)",
                     c->id, c->step_idx, op.c_str());
            c->step_failed = 1;
            break;
        }
        c->step_idx++;
    }
    ncland_step_finish(c, wh);
}

void ncland_step_abort_for_eof(conn_t *c, ncland_wh_t *wh)
{
    if (!c) return;
    if (c->step_block == STEP_BLOCK_NONE) return;
    LOG_WARN("conn[%d] neid=%d: NE EOF during %s; aborting",
             c->id, c->neid,
             c->step_block == STEP_BLOCK_POST_CMD ? "post_command" : "disconnect_steps");
    if (c->step_block == STEP_BLOCK_POST_CMD) {
        snprintf(c->pending_rsp.text, sizeof(c->pending_rsp.text),
                 "NE disconnected mid-post_command");
        c->pending_rsp.result = NCLAND_RESULT_FAILURE;
        worker_send_rsp(wh, &c->pending_rsp);
        memset(&c->pending_rsp, 0, sizeof(c->pending_rsp));
    }
    c->step_block    = STEP_BLOCK_NONE;
    c->step_idx      = 0;
    c->step_deadline = 0;
    c->step_failed   = 0;
    ncland_lua_release_thread(wh, c);
    warehouse_drop_conn(wh, c);
}

void ncland_check_step_deadlines(ncland_wh_t *wh)
{
    if (!wh) return;
    time_t now = time(NULL);
    for (int i = 0; i < MAX_CONNS; i++) {
        conn_t *c = &wh->conn[i];
        if (c->step_block == STEP_BLOCK_NONE) continue;
        if (c->step_deadline == 0 || now < c->step_deadline) continue;
        LOG_WARN("conn[%d] neid=%d: step[%d] timed out", c->id, c->neid, c->step_idx);
        c->step_failed = 1;
        ncland_step_finish(c, wh);
    }
}
```

- [ ] **Step 7: Run tests (pass)** — `./ncland_unit_tests stepper` → 8 new tests pass. Full suite → `129 passed, 0 failed, 6 skipped`.

- [ ] **Step 8: Commit**

```bash
cd /home/dan/Git/netflex
git add cnc/ncland/src/ncland_stepper.h cnc/ncland/src/ncland_stepper.cpp cnc/ncland/src/ncland_stepper_tests.cpp cnc/ncland/src/ncland.h cnc/ncland/src/ncland_worker.cpp cnc/ncland/src/ncland.mk
git commit -m "$(cat <<'EOF'
[ncland] stepper: ncland_step_advance/finish/abort_for_eof + check_step_deadlines

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 6: Splice into `handle_ne_data` — post_command fork + CS_POST_CMD/CS_DISCONNECTING dispatch

**Files:** `ncland_worker.cpp`, `ncland_stepper_tests.cpp`

This is the wiring task: prompt-match branch forks based on `post_command` presence; new state branch routes EPOLLIN to the step coroutine; coroutine_drive's completion callback advances the stepper.

- [ ] **Step 1: Write the failing tests** that exercise the splice end-to-end (`ncland_stepper_tests.cpp`):

```cpp
TEST("stepper", "S-3b end-to-end: prompt match -> post_command yields on expect -> resumes -> rsp") {
    int sv[2]; REQUIRE_EQ(socketpair(AF_UNIX, SOCK_STREAM, 0, sv), 0);
    fcntl(sv[0], F_SETFL, O_NONBLOCK); fcntl(sv[1], F_SETFL, O_NONBLOCK);
    ncland_wh_t wh{};
    wh_with_step_entry(wh, 9301, "[a-z0-9]+#",
        "local M={} function M.login(s) end\n"
        "function M.ping(s) s:send('ping\\n'); local m=s:expect('pong', 5)\n"
        "  s:set_var('got', m or 'nil') end\n"
        "return M\n",
        "[{op: lua, func: ping}]", "[]");
    conn_t *c = &wh.conn[3]; c->net_fd = sv[0]; c->dtype = 9301;
    c->state = CS_CMD_PENDING; c->seq = 11;
    REQUIRE_EQ(regcomp(&c->prmpt1re, "[a-z0-9]+#", REG_EXTENDED), 0); c->prmpt1re_valid = 1;

    /* NE sends prompt -> handle_ne_data matches, enters CS_POST_CMD, runs `ping` */
    write(sv[1], "out\nsw1#", 8);
    handle_ne_data(c, &wh);
    REQUIRE_EQ((int)c->state, (int)CS_POST_CMD);
    /* Step sent "ping\n" — verify it landed */
    char b[8] = {0}; int n = (int)read(sv[1], b, sizeof(b)-1);
    REQUIRE(n >= 5 && strncmp(b, "ping\n", 5) == 0);
    /* NE responds with "pong" -> handle_ne_data drives coroutine_drive */
    write(sv[1], "pong\n", 5);
    handle_ne_data(c, &wh);
    REQUIRE_EQ((int)c->state, (int)CS_READY);
    REQUIRE_EQ(g_test_last_step_failed, 0);
    REQUIRE_EQ(strcmp(ncland_lua_get_var(&wh, c, "got"), "pong"), 0);
    ncland_lua_shutdown(&wh); close(sv[0]); close(sv[1]);
}

TEST("stepper", "S-3b empty post_command: rsp ships immediately, no CS_POST_CMD") {
    int sv[2]; REQUIRE_EQ(socketpair(AF_UNIX, SOCK_STREAM, 0, sv), 0);
    fcntl(sv[0], F_SETFL, O_NONBLOCK); fcntl(sv[1], F_SETFL, O_NONBLOCK);
    ncland_wh_t wh{};
    wh_with_step_entry(wh, 9302, "[a-z0-9]+#",
        "local M={} function M.login(s) end return M\n",
        "[]", "[]");
    conn_t *c = &wh.conn[4]; c->net_fd = sv[0]; c->dtype = 9302;
    c->state = CS_CMD_PENDING; c->seq = 12;
    REQUIRE_EQ(regcomp(&c->prmpt1re, "[a-z0-9]+#", REG_EXTENDED), 0); c->prmpt1re_valid = 1;

    g_test_last_step_block_finished = -1;
    write(sv[1], "data\nsw1#", 9);
    handle_ne_data(c, &wh);
    REQUIRE_EQ((int)c->state, (int)CS_READY);
    REQUIRE_EQ(g_test_last_step_block_finished, -1);   /* step path skipped */
    ncland_lua_shutdown(&wh); close(sv[0]); close(sv[1]);
}

TEST("stepper", "S-3b NE EOF mid-post_command -> FAILURE rsp + drop") {
    int sv[2]; REQUIRE_EQ(socketpair(AF_UNIX, SOCK_STREAM, 0, sv), 0);
    fcntl(sv[0], F_SETFL, O_NONBLOCK); fcntl(sv[1], F_SETFL, O_NONBLOCK);
    ncland_wh_t wh{};
    wh_with_step_entry(wh, 9303, "[a-z0-9]+#",
        "local M={} function M.login(s) end\n"
        "function M.wait(s) s:expect('never', 30) end\n"
        "return M\n",
        "[{op: lua, func: wait}]", "[]");
    conn_t *c = &wh.conn[5]; c->net_fd = sv[0]; c->dtype = 9303;
    c->state = CS_CMD_PENDING; c->seq = 13;
    REQUIRE_EQ(regcomp(&c->prmpt1re, "[a-z0-9]+#", REG_EXTENDED), 0); c->prmpt1re_valid = 1;
    /* Enter CS_POST_CMD */
    write(sv[1], "sw1#", 4);
    handle_ne_data(c, &wh);
    REQUIRE_EQ((int)c->state, (int)CS_POST_CMD);
    /* NE disconnects */
    close(sv[1]);
    handle_ne_data(c, &wh);
    REQUIRE_EQ((int)c->state, (int)CS_IDLE);
    REQUIRE(strstr(g_test_last_sent_rsp_text, "EOF") != NULL ||
            strstr(g_test_last_sent_rsp_text, "disconnected") != NULL);
    ncland_lua_shutdown(&wh); close(sv[0]);
}
```

- [ ] **Step 2: Run (fails)** — first test fails because `handle_ne_data` does not enter `CS_POST_CMD` (it ships rsp synchronously regardless of post_command). EOF test fails for the same reason.

- [ ] **Step 3: Modify `handle_ne_data` in `ncland_worker.cpp` — three changes:**

(a) **Fork before `worker_send_rsp`** in the `CS_CMD_PENDING` match branch. Locate the existing block (post-#3a) that builds `rsp` (sets `conn_id`/`seq`/`orig_cnc`/`tty`/`dm_type`/`text`/`result`) and currently calls `worker_send_rsp(wh, &rsp); c->rlen=0; conn_set_state(c, CS_READY);`. Replace the ship-now suffix with:

```c
        /* #3b post_command fork */
        const ne_entry_t *e = ncland_registry_find(&wh->registry, c->dtype);
        if (e && e->post_command && e->post_command.IsSequence() && e->post_command.size() > 0) {
            c->pending_rsp = rsp;
            c->step_block  = STEP_BLOCK_POST_CMD;
            c->step_idx    = 0;
            c->step_failed = 0;
            c->rlen = 0; c->rbuf[0] = '\0';
            conn_set_state(c, CS_POST_CMD);
            ncland_step_advance(c, wh);
            return;
        }
        /* No post_command — ship and finish, same as #3a */
        worker_send_rsp(wh, &rsp);
        c->rlen = 0; memset(c->rbuf, 0, sizeof(c->rbuf));
        conn_set_state(c, CS_READY);
        return;
```

(b) **Add the CS_POST_CMD / CS_DISCONNECTING dispatch branch.** Locate the existing `CS_AUTHENTICATING` branch (which calls into the login bridge on EPOLLIN). Add immediately after — or before, depending on existing layout:

```c
    if (c->state == CS_POST_CMD || c->state == CS_DISCONNECTING) {
        int rc = ncland_lua_coroutine_drive(wh, c, 0);
        if (rc == NCLAND_LUA_OK) {
            c->step_idx++;
            ncland_step_advance(c, wh);
        } else if (rc == NCLAND_LUA_FAILED) {
            c->step_failed = 1;
            ncland_step_finish(c, wh);
        }
        /* NCLAND_LUA_YIELDED: still waiting for more data */
        return;
    }
```

(c) **Handle EOF in those states.** Locate the existing EOF branch (the read-returns-0 path that calls `warehouse_drop_conn`). Add an early case:

```c
        if (c->state == CS_POST_CMD || c->state == CS_DISCONNECTING) {
            ncland_step_abort_for_eof(c, wh);
            return;
        }
```

This must run **before** the generic EOF drop.

- [ ] **Step 4: Update `next_timeout_ms`** to account for `step_deadline` when state is `CS_POST_CMD` / `CS_DISCONNECTING`:

```c
        } else if (c->state == CS_POST_CMD || c->state == CS_DISCONNECTING) {
            dl = c->step_deadline;
        }
```

Add alongside the existing per-state branches (`CS_CMD_PENDING`, `CS_AUTHENTICATING`, `CS_KEEPALIVE`, `CS_READY` keepalive-due).

- [ ] **Step 5: Run tests (pass)** — `./ncland_unit_tests stepper` → 3 new tests pass; existing 8 stepper tests still pass. Full suite → `132 passed, 0 failed, 6 skipped`.

- [ ] **Step 6: Commit**

```bash
cd /home/dan/Git/netflex
git add cnc/ncland/src/ncland_worker.cpp cnc/ncland/src/ncland_stepper_tests.cpp
git commit -m "$(cat <<'EOF'
[ncland] handle_ne_data: post_command fork + CS_POST_CMD/CS_DISCONNECTING dispatch + EOF abort

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 7: Per-step deadline sweep wired into the main loop

**Files:** `ncland_warehouse.cpp`, `ncland_stepper_tests.cpp`

The stepper module already exposes `ncland_check_step_deadlines` (Task 5). The main loop needs to call it each tick.

- [ ] **Step 1: Write the failing test:**

```cpp
TEST("stepper", "S-3b step deadline fires -> FAILURE rsp + CS_READY") {
    int sv[2]; REQUIRE_EQ(socketpair(AF_UNIX, SOCK_STREAM, 0, sv), 0);
    fcntl(sv[0], F_SETFL, O_NONBLOCK); fcntl(sv[1], F_SETFL, O_NONBLOCK);
    ncland_wh_t wh{};
    wh_with_step_entry(wh, 9401, "[a-z0-9]+#",
        "local M={} function M.login(s) end\n"
        "function M.wait(s) s:expect('never', 30) end\n"
        "return M\n",
        "[{op: lua, func: wait}]", "[]");
    conn_t *c = &wh.conn[6]; c->net_fd = sv[0]; c->dtype = 9401; c->state = CS_POST_CMD;
    c->step_block = STEP_BLOCK_POST_CMD; c->step_idx = 0;
    memset(&c->pending_rsp, 0, sizeof(c->pending_rsp));
    c->pending_rsp.seq = 1;
    /* Manually start the step to put coroutine in yield state */
    YAML::Node args;
    REQUIRE_EQ(ncland_lua_step_start(&wh, c, "wait", args), NCLAND_LUA_YIELDED);
    /* Set deadline to past */
    c->step_deadline = time(NULL) - 1;
    ncland_check_step_deadlines(&wh);
    REQUIRE_EQ((int)c->state, (int)CS_READY);
    REQUIRE_EQ(c->pending_rsp.seq, 0);              /* cleared by step_finish */
    REQUIRE_EQ(g_test_last_step_failed, 1);
    ncland_lua_shutdown(&wh); close(sv[0]); close(sv[1]);
}
```

- [ ] **Step 2: Run (fails)** — the test passes the `ncland_check_step_deadlines` call directly, so it actually depends only on Task 5 wiring. Verify it passes (already implemented). If it does → skip Step 3. If it doesn't → fix Task 5's `check_step_deadlines` first.

- [ ] **Step 3: Wire into the main loop.** In `ncland_warehouse.cpp`'s main loop (the `while (wh->running)` body), find the existing sweep section (where `check_keepalives(wh)`, `check_expired_cmds(wh)`, and the keepalive-liveness sweep live). Add:

```c
        ncland_check_step_deadlines(wh);
```

- [ ] **Step 4: Build + full suite** — `./ncland_unit_tests` → `133 passed, 0 failed, 6 skipped`. The daemon still builds (`nmake -f ncland.mk`).

- [ ] **Step 5: Commit**

```bash
cd /home/dan/Git/netflex
git add cnc/ncland/src/ncland_warehouse.cpp cnc/ncland/src/ncland_stepper_tests.cpp
git commit -m "$(cat <<'EOF'
[ncland] main loop: per-tick ncland_check_step_deadlines sweep

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 8: SIGTERM drain — `warehouse_begin_drain` + `drain_one_ready_conn` + drain sweep + cmd refusal

**Files:** `ncland_stepper.cpp`, `ncland_stepper.h`, `ncland_warehouse.cpp`, `ncland_unit_tests.cpp`

This task does NOT add the loop exit condition (that's Task 9). Here we just install the drain primitives and the per-tick sweep, and refuse new commands while `shutting_down` is set.

- [ ] **Step 1: Declare the new helpers** in `ncland_stepper.h`:

```c
/** @brief Iterate CS_READY conns and kick disconnect_steps (or hard-drop on empty block).
 *  Called from the signalfd handler when SIGTERM/SIGHUP arrives. */
void ncland_warehouse_begin_drain(ncland_wh_t *wh);

/** @brief Count conns whose state != CS_IDLE. Used by the main loop's shutdown exit
 *  condition (Task 9) and by W-3b tests. */
int  ncland_warehouse_active_conn_count(const ncland_wh_t *wh);
```

- [ ] **Step 2: Write the failing tests** (`ncland_unit_tests.cpp`, suite `warehouse`):

```cpp
TEST("warehouse", "W-3b begin_drain kicks disconnect_steps on CS_READY conns") {
    int sv[2]; REQUIRE_EQ(socketpair(AF_UNIX, SOCK_STREAM, 0, sv), 0); fcntl(sv[0], F_SETFL, O_NONBLOCK);
    ncland_wh_t wh{};
    wh_with_step_entry(wh, 9501, "[a-z0-9]+#",
        "local M={} function M.login(s) end function M.bye(s) end return M\n",
        "[]", "[{op: lua, func: bye}]");
    conn_t *c = &wh.conn[4]; c->net_fd = sv[0]; c->dtype = 9501; c->state = CS_READY;
    ncland_warehouse_begin_drain(&wh);
    /* synchronous bye -> conn dropped */
    REQUIRE_EQ((int)c->state, (int)CS_IDLE);
    ncland_lua_shutdown(&wh); close(sv[0]); close(sv[1]);
}

TEST("warehouse", "W-3b begin_drain hard-drops CS_READY conn with empty disconnect_steps") {
    int sv[2]; REQUIRE_EQ(socketpair(AF_UNIX, SOCK_STREAM, 0, sv), 0); fcntl(sv[0], F_SETFL, O_NONBLOCK);
    ncland_wh_t wh{};
    wh_with_step_entry(wh, 9502, "[a-z0-9]+#",
        "local M={} function M.login(s) end return M\n",
        "[]", "[]");
    conn_t *c = &wh.conn[5]; c->net_fd = sv[0]; c->dtype = 9502; c->state = CS_READY;
    ncland_warehouse_begin_drain(&wh);
    REQUIRE_EQ((int)c->state, (int)CS_IDLE);
    ncland_lua_shutdown(&wh); close(sv[0]); close(sv[1]);
}

TEST("warehouse", "W-3b begin_drain skips non-READY conns") {
    int sv[2]; REQUIRE_EQ(socketpair(AF_UNIX, SOCK_STREAM, 0, sv), 0); fcntl(sv[0], F_SETFL, O_NONBLOCK);
    ncland_wh_t wh{};
    wh_with_step_entry(wh, 9503, "[a-z0-9]+#",
        "local M={} function M.login(s) end function M.bye(s) end return M\n",
        "[]", "[{op: lua, func: bye}]");
    conn_t *c = &wh.conn[6]; c->net_fd = sv[0]; c->dtype = 9503; c->state = CS_CMD_PENDING;
    ncland_warehouse_begin_drain(&wh);
    REQUIRE_EQ((int)c->state, (int)CS_CMD_PENDING);  /* untouched */
    ncland_lua_shutdown(&wh); close(sv[0]); close(sv[1]);
}

TEST("warehouse", "W-3b shutting_down rejects new cmds with FAILURE") {
    ncland_wh_t wh{};
    for (int i = 0; i < MAX_CONNS; i++) conn_init(&wh.conn[i], i);
    /* set a conn so neid resolution doesn't fail first */
    conn_t *c = &wh.conn[3]; c->neid = 99; c->state = CS_READY;
    wh.shutting_down = time(NULL);
    ncland_cmd_msg_t cmd; memset(&cmd, 0, sizeof(cmd));
    cmd.msg_type = CLAN2_MSG_CMD; cmd.neid = 99; cmd.seq = 1;
    snprintf(cmd.data, sizeof(cmd.data), "nclancmd:30:0:0:show");
    uint8_t id[1] = {7};
    g_test_last_direct_rsp_result = -1;
    warehouse_handle_cmd_from_zmq(&wh, id, 1, &cmd);
    REQUIRE_EQ(g_test_last_direct_rsp_result, NCLAND_RESULT_FAILURE);
}

TEST("warehouse", "W-3b active_conn_count counts non-IDLE") {
    ncland_wh_t wh{};
    for (int i = 0; i < MAX_CONNS; i++) conn_init(&wh.conn[i], i);
    wh.conn[1].state = CS_READY;
    wh.conn[2].state = CS_CMD_PENDING;
    wh.conn[3].state = CS_IDLE;
    REQUIRE_EQ(ncland_warehouse_active_conn_count(&wh), 2);
}
```

- [ ] **Step 3: Run (fails)** — link errors for `ncland_warehouse_begin_drain` / `ncland_warehouse_active_conn_count`; the cmd-refusal test fails because the shutdown check isn't there yet.

- [ ] **Step 4: Implement the helpers** in `ncland_stepper.cpp`:

```c
int ncland_warehouse_active_conn_count(const ncland_wh_t *wh)
{
    if (!wh) return 0;
    int n = 0;
    for (int i = 0; i < MAX_CONNS; i++)
        if (wh->conn[i].state != CS_IDLE) n++;
    return n;
}

/* Returns 1 if the conn entered CS_DISCONNECTING, 0 if hard-dropped immediately. */
static int drain_one_ready_conn(ncland_wh_t *wh, conn_t *c)
{
    const ne_entry_t *e = ncland_registry_find(&wh->registry, c->dtype);
    if (!e || !e->disconnect_steps || !e->disconnect_steps.IsSequence()
                                   || e->disconnect_steps.size() == 0) {
        warehouse_drop_conn(wh, c);
        return 0;
    }
    c->step_block  = STEP_BLOCK_DISCONNECT;
    c->step_idx    = 0;
    c->step_failed = 0;
    c->rlen = 0; c->rbuf[0] = '\0';
    conn_set_state(c, CS_DISCONNECTING);
    ncland_step_advance(c, wh);
    return 1;
}

void ncland_warehouse_begin_drain(ncland_wh_t *wh)
{
    if (!wh) return;
    int kicked = 0, dropped = 0;
    for (int i = 0; i < MAX_CONNS; i++) {
        conn_t *c = &wh->conn[i];
        if (c->state != CS_READY) continue;
        if (drain_one_ready_conn(wh, c)) kicked++; else dropped++;
    }
    LOG_INFO("drain: %d conn(s) running disconnect_steps; %d dropped immediately",
             kicked, dropped);
}
```

- [ ] **Step 5: Per-tick drain sweep** in the main loop. In `ncland_warehouse.cpp`, after `ncland_check_step_deadlines(wh)` (added Task 7):

```c
        if (wh->shutting_down) {
            for (int i = 0; i < MAX_CONNS; i++) {
                conn_t *c = &wh->conn[i];
                if (c->state == CS_READY) drain_one_ready_conn(wh, c);
            }
        }
```

`drain_one_ready_conn` is file-static in `ncland_stepper.cpp`. Expose it via the header for the sweep call:

```c
/* in ncland_stepper.h, near begin_drain */
int ncland_drain_one_ready_conn(ncland_wh_t *wh, conn_t *c);
```

Rename the file-static accordingly and update `begin_drain` to call the public name.

- [ ] **Step 6: Cmd refusal** in `warehouse_handle_cmd_from_zmq`. Insert at the top of the function, before any other validation:

```c
    if (wh->shutting_down) {
        wh_reply_now(wh, identity, identity_len, cmd, NCLAND_RESULT_FAILURE, "ncland shutting down");
        return;
    }
```

- [ ] **Step 7: Run tests (pass)** — `./ncland_unit_tests warehouse` → 5 new tests pass. Full suite → `138 passed, 0 failed, 6 skipped`.

- [ ] **Step 8: Commit**

```bash
cd /home/dan/Git/netflex
git add cnc/ncland/src/ncland_stepper.h cnc/ncland/src/ncland_stepper.cpp cnc/ncland/src/ncland_warehouse.cpp cnc/ncland/src/ncland_unit_tests.cpp
git commit -m "$(cat <<'EOF'
[ncland] drain: begin_drain + per-tick sweep + refuse cmds while shutting_down

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 9: SIGTERM/SIGHUP handler + shutdown deadline + loop exit + hard-close

**Files:** `ncland_warehouse.cpp`, `ncland_unit_tests.cpp`

- [ ] **Step 1: Write the failing tests** (suite `warehouse`):

```cpp
TEST("warehouse", "W-3b shutdown deadline expired -> loop should exit") {
    ncland_wh_t wh{};
    for (int i = 0; i < MAX_CONNS; i++) conn_init(&wh.conn[i], i);
    /* One CS_DISCONNECTING conn that will never finish — simulates a stuck disconnect */
    wh.conn[2].state = CS_DISCONNECTING;
    wh.conn[2].step_block = STEP_BLOCK_DISCONNECT;
    wh.shutting_down     = time(NULL) - 20;
    wh.shutdown_deadline = wh.shutting_down + SHUTDOWN_GRACE_S;  /* already past */
    /* Compute exit condition directly (helper introduced by this task): */
    REQUIRE(ncland_warehouse_shutdown_done(&wh));
}

TEST("warehouse", "W-3b shutdown not done while drain in progress and within grace") {
    ncland_wh_t wh{};
    for (int i = 0; i < MAX_CONNS; i++) conn_init(&wh.conn[i], i);
    wh.conn[2].state = CS_DISCONNECTING; wh.conn[2].step_block = STEP_BLOCK_DISCONNECT;
    wh.shutting_down     = time(NULL);
    wh.shutdown_deadline = wh.shutting_down + SHUTDOWN_GRACE_S;
    REQUIRE(!ncland_warehouse_shutdown_done(&wh));
}

TEST("warehouse", "W-3b shutdown done immediately when no active conns") {
    ncland_wh_t wh{};
    for (int i = 0; i < MAX_CONNS; i++) conn_init(&wh.conn[i], i);
    wh.shutting_down     = time(NULL);
    wh.shutdown_deadline = wh.shutting_down + SHUTDOWN_GRACE_S;
    REQUIRE(ncland_warehouse_shutdown_done(&wh));
}
```

- [ ] **Step 2: Run (fails)** — `ncland_warehouse_shutdown_done` undefined.

- [ ] **Step 3: Add the helper** in `ncland_warehouse.cpp` (and declare in `ncland.h` near the other warehouse decls):

```c
/** @brief Compute the graceful-shutdown exit condition (#3b).
 *  Returns true iff shutting_down is set AND (no active conns OR past deadline). */
bool ncland_warehouse_shutdown_done(const ncland_wh_t *wh)
{
    if (!wh || !wh->shutting_down) return false;
    if (ncland_warehouse_active_conn_count(wh) == 0) return true;
    return time(NULL) >= wh->shutdown_deadline;
}
```

- [ ] **Step 4: Rework the signalfd handler** in `ncland_warehouse.cpp`. Locate the SIGTERM/SIGHUP/SIGINT block in the main loop (currently sets `wh->running = 0`). Replace SIGTERM/SIGHUP with the graceful path; keep SIGINT as immediate exit (developer ctrl-C):

```c
                    if ((int)si.ssi_signo == SIGTERM ||
                        (int)si.ssi_signo == SIGHUP) {
                        if (wh->shutting_down) break;  /* idempotent */
                        wh->shutting_down     = time(NULL);
                        wh->shutdown_deadline = wh->shutting_down + SHUTDOWN_GRACE_S;
                        LOG_INFO("signal: graceful shutdown initiated (grace=%ds)",
                                 SHUTDOWN_GRACE_S);
                        ncland_warehouse_begin_drain(wh);
                        break;
                    }
                    if ((int)si.ssi_signo == SIGINT) {
                        LOG_INFO("signal: SIGINT — immediate exit");
                        wh->running = 0;
                        break;
                    }
```

- [ ] **Step 5: New loop exit condition.** Locate the main loop header `while (wh->running)`. Add the shutdown-done check at the bottom of each iteration (after sweeps + drain sweep):

```c
        if (ncland_warehouse_shutdown_done(wh)) {
            int leftover = ncland_warehouse_active_conn_count(wh);
            if (leftover > 0)
                LOG_WARN("shutdown: hard-closing %d conn(s) past grace", leftover);
            g_test_active_conn_count_at_break = leftover;
            wh->running = 0;
        }
```

`warehouse_shutdown` (the existing teardown after the loop) already iterates conns and frees them — it will hard-close any CS_DISCONNECTING/CS_POST_CMD leftovers via `conn_free` + the existing ssh/telnet disconnect calls. No additional teardown code needed here.

- [ ] **Step 6: Cap `next_timeout_ms` at `shutdown_deadline`** when shutting down. In `next_timeout_ms`, after computing the per-conn min:

```c
    if (wh->shutting_down) {
        time_t now = time(NULL);
        long left_ms = (long)(wh->shutdown_deadline - now) * 1000;
        if (left_ms < 0) left_ms = 0;
        if (next_ms < 0 || left_ms < next_ms) next_ms = left_ms;
    }
```

- [ ] **Step 7: Run tests (pass)** — `./ncland_unit_tests warehouse` → 3 new tests pass. Full suite → `141 passed, 0 failed, 6 skipped`. Build the daemon: `nmake -f ncland.mk` → rc=0.

- [ ] **Step 8: Commit**

```bash
cd /home/dan/Git/netflex
git add cnc/ncland/src/ncland.h cnc/ncland/src/ncland_warehouse.cpp cnc/ncland/src/ncland_unit_tests.cpp
git commit -m "$(cat <<'EOF'
[ncland] signalfd: SIGTERM/SIGHUP -> graceful drain; main loop exits at deadline or zero active

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 10: Live test + memory update

**Files:** `/home/dan/.claude/projects/-home-dan-Git-netflex/memory/project_ncland_is_clan_rewrite.md`

- [ ] **Step 1: Deploy + smoke test (manual, on lin07n).** Build the daemon clean, deploy `ncland` to `/usr/cnc/bin/ncland`. Either: (a) edit `alc_1850_tss5.yaml` and `alc_1850_tss5.lua` to add a real `graceful_close` that logs + sends `LOGOUT\n` and waits briefly for the prompt; OR (b) edit `nokia_1830_pss.yaml` to add a no-op `disconnect_steps: [{op: lua, func: bye}]` plus a `function M.bye(s) s:log('bye!') end` in `nokia_1830_pss.lua`, just to exercise the path.

Sequence:
1. `systemctl start ncland` (or run in foreground).
2. `nclan-cmd -n 518 'show shelf'` → confirm rsp arrives, RESULT SUCCESS.
3. `systemctl stop ncland` (or `kill -TERM $(pgrep ncland)`).
4. Confirm the log shows `drain: 1 conn(s) running disconnect_steps`, the `bye` function ran, conn dropped, daemon exited inside 10s. Capture the log lines.

- [ ] **Step 2: Update memory** at `/home/dan/.claude/projects/-home-dan-Git-netflex/memory/project_ncland_is_clan_rewrite.md`. Append the #3b entry alongside the existing #3a line:

```markdown
- **#3b step engine — DONE** (plan `~/docs/.../plans/2026-06-11-ncland-stepper-3b.md`,
  spec `2026-06-11-ncland-stepper-3b-design.md`): `op:lua` post_command + disconnect_steps
  honored via shared Lua coroutine bridge (`ncland_lua_coroutine_drive` extracted from
  `resume_login`). New states `CS_POST_CMD` / `CS_DISCONNECTING`; new module
  `ncland_stepper.{h,cpp}` (`ncland_step_advance`/`_finish`/`_abort_for_eof`/
  `_check_step_deadlines`). Load-time `validate_steps` rejects unknown ops + missing
  funcs. SIGTERM/SIGHUP triggers `ncland_warehouse_begin_drain` (10s grace via
  `SHUTDOWN_GRACE_S`); new cmds during shutdown reply FAILURE with "ncland shutting
  down". Other `op:` types (expect/send/branch/...) deferred — each is one new case
  in `ncland_step_advance`. Suite ~141/0/6.
```

Bump the "as of" date in the heading above the bullets. Update the "Still pending" line — remove #3b, keep the tracked follow-ups (dead `warehouse_handle_open/close`, etc.).

- [ ] **Step 3: Commit memory update** (memory files live outside the repo — no git commit needed; the file is the canonical store).

---

## Self-Review (against the spec)

**Spec coverage:**
- §1 Scope (op:lua only, SIGTERM-only disconnect, no hooks, FAILURE doesn't redact text) → enforced in Tasks 4 (validation rejects non-lua ops), 8/9 (drain SIGTERM only), N/A (hooks not implemented), Task 5 (`step_finish` preserves `pending_rsp.text`). ✓
- §2 Architecture (CS_POST_CMD/CS_DISCONNECTING; one stepper, two triggers) → Tasks 1 + 5 + 6 + 8. ✓
- §3 Registry validation → Task 4. ✓
- §4 Per-conn stepper (5 conn_t fields, state machines, coroutine reuse, per-step deadline) → Tasks 1 + 5 + 7. ✓
- §4.4 `ncland_lua_coroutine_drive` extraction + `ncland_lua_step_start` → Tasks 2 + 3. ✓
- §5 SIGTERM drain (begin_drain, drain_one_ready_conn, drain sweep, cmd refusal, loop exit, hard-close, next_timeout_ms cap) → Tasks 8 + 9. ✓
- §6 Error handling (10-row table; resource discipline, idempotency, EOF, no partial response) → Tasks 5 (`step_finish` early-return + `step_abort_for_eof` + text-preserve) + 6 (EOF wiring) + 7 (deadline sweep). ✓
- §7 Testing strategy → Tasks 4 (R-3b), 5+6+7 (S-3b), 8+9 (W-3b). All ~17 tests covered. ✓
- §8 Files Touched → matches the File Structure table at the top of this plan. ✓
- §9 Open Questions (deferred items) → not implemented; documented in spec as deferred. ✓

**Placeholder scan:** none — every code step shows complete code; the only judgment items are (a) the exact name of the existing Lua module-loader (`ncland_lua_load_module` / `ncland_lua_load_module_into`) which Task 3 Step 6 notes must be matched against the current code, and (b) `NCLAND_RSP_TEXT_LEN` which may need to be defined if absent (Task 5 Step 5 notes the fallback). Both are flagged inline.

**Type consistency:** `STEP_BLOCK_NONE/POST_CMD/DISCONNECT`, `CS_POST_CMD/CS_DISCONNECTING`, `NCLAND_LUA_OK/YIELDED/FAILED`, `SHUTDOWN_GRACE_S`, `STEP_DEFAULT_TMOUT_S`, `ncland_step_advance/finish/abort_for_eof`, `ncland_warehouse_begin_drain/active_conn_count/shutdown_done`, `ncland_drain_one_ready_conn`, `ncland_check_step_deadlines`, `ncland_lua_coroutine_drive/step_start/release_thread/module_has_func`, `g_test_last_step_block_finished/last_step_failed/last_sent_rsp_text/active_conn_count_at_break` are used identically across tasks. `conn_t` fields `step_block/step_idx/step_failed/step_deadline/pending_rsp` and wh fields `shutting_down/shutdown_deadline` likewise.

---

## Execution note

Tasks 1→10 are sequential. Task 2 is a pure refactor (must stay green with no new tests). Task 10 Step 1 (live test) pauses for the user. Recommended: subagent-driven, two-stage review per task.
