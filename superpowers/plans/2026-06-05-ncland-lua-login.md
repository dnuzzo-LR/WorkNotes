# ncland #2 — Lua login() Bridge Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Embed the vendored Lua 5.3 VM in the single-process `ncland` daemon and drive each transport-established NE session through its per-dtype `login(session, global, args)` dialogue — using coroutine yield/resume fed by the epoll loop — until the session reaches its `primary_regex` prompt (`CS_READY`).

**Architecture:** One shared `lua_State *L` owned by the main loop (no threads touch Lua, so no locking). Each connection runs `login()` as a coroutine (`lua_newthread`). `session:expect()` scans the conn's read buffer and, on no match, yields via `lua_yieldk` with a continuation; the epoll loop reads bytes on `EPOLLIN`, appends them, and `lua_resume`s. `login()` returning → `CS_READY`; error / `fail()` / `login_s` timeout → teardown. Scope is the login bridge only; the step language (post_command/disconnect/keepalive) is #3.

**Tech Stack:** C++17 (compiled via Lucent/AT&T **nmake**, `ncland.mk`), vendored Lua 5.3 (headers in `include/`, lib `-llua`), POSIX `regex.h`, libssh, `nfunit-test.hpp` (run via `./ncland_unit_tests`).

---

## Critical context for the implementer (read before starting)

- **Build:** This project uses **Lucent/AT&T nmake**, not GNU make. Build from `cnc/ncland/src` with `nmake -f ncland.mk <target>`. Before building, ensure `BASE` = `git rev-parse --show-toplevel` (= `/home/dan/Git/netflex`) and `VPATH`'s first segment equals `$BASE`. If a build fails on missing `global*.nmk` or project headers, **stop and report** — do not hack include paths.
- **Lua version:** vendored **Lua 5.3** (`include/lua.h`, `include/lauxlib.h`, `include/lualib.h`; already on the `-I../../../include` path). Key API facts:
  - `lua_resume(lua_State *co, lua_State *from, int narg)` — **3 args** in 5.3.
  - **Yielding from a C function** uses `lua_yieldk(L, nresults, ctx, k)`; on resume Lua invokes the continuation `int k(lua_State*, int status, lua_KContext ctx)` — **not** the code after `lua_yieldk`. `expect()` MUST use this idiom.
  - `lua_newthread(L)` pushes a coroutine (a `lua_State*`) onto `L`; anchor it with `luaL_ref(L, LUA_REGISTRYINDEX)` to stop GC, and `luaL_unref` to release.
- **`conn_t` is POD-moved:** `conn_init` does `memset(c, 0, sizeof(*c))` and `warehouse_install_conn` does `memcpy(slot, carrier, sizeof(conn_t))`. **Never add a non-trivial C++ type (e.g. `std::map`) to `conn_t`.** New fields must be POD. The per-conn session-var store therefore lives **wh-side, keyed by conn id** (`ncland_wh_t::session_vars`), not in `conn_t`.
- **Login starts post-install:** `ncland_login_start` runs in the main loop *after* `warehouse_install_conn` (the memcpy). At that point the conn is a stable `&wh->conn[id]` and is never moved again, so it's safe to set its `lua`/`lua_ref`/deadlines and to key `session_vars` by `c->id`.
- **Test idiom (socketpair stands in for the NE):**
  ```c
  int sv[2]; socketpair(AF_UNIX, SOCK_STREAM, 0, sv);
  c->net_fd = sv[0];                    // daemon side
  write(sv[1], "login: ", 7);           // "NE" sends to daemon
  ncland_login_on_data(&wh, c);         // daemon reads sv[0], drives login
  char buf[64]={0}; read(sv[1], buf, sizeof(buf)-1);  // assert daemon's send()
  ```
  Set the socketpair non-blocking in tests so `ne_read` never blocks: `fcntl(sv[0], F_SETFL, O_NONBLOCK)`.
- **Existing helpers you will reuse:** `ne_read`/`ne_write` are `static` in `ncland_worker.cpp`. The bridge needs them, so Task 2 promotes them to non-static with declarations in `ncland.h` (`ncland_ne_read`/`ncland_ne_write`). `conn_set_state`, `conn_alloc`, `conn_free`, `conn_init` are in `ncland_conn.cpp`.
- **Run tests:** `nmake -f ncland.mk ncland_unit_tests && ./ncland_unit_tests` — baseline today is **75 passed, 0 failed, 6 skipped**. Each task must keep that green (plus its own new tests).
- **Commit trailer:** end every commit message with
  `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`.

---

## File Structure

| File | Responsibility |
|------|----------------|
| `cnc/ncland/src/ncland_lua.h` | **Create.** Public bridge API: VM lifecycle, session push, login driver entry points, status enum. |
| `cnc/ncland/src/ncland_lua.cpp` | **Create.** All bridge implementation: `L` lifecycle, `global` table, module load/cache, `session` userdata + metatable (`send`/`expect`/`last_output`/`get_var`/`set_var`/`log`/`fail` + fields), the `expect` yieldk continuation, `login_start`/`on_data`/`on_timeout`, the resume helper, regex scan helper. |
| `cnc/ncland/src/ncland_lua_tests.cpp` | **Create.** `nfunit` tests, suite `"lua"`. |
| `cnc/ncland/src/ncland.h` | **Modify.** New `conn_t` POD fields; new `ncland_wh_t` fields (`L`, `lua_global_ref`, module cache, `session_vars`); promote `ne_read`/`ne_write` decls; include guard for the Lua bridge header where needed. |
| `cnc/ncland/src/ncland_conn.cpp` | **Modify.** `conn_free` also regfrees `login_expect_re` and clears the conn's wh-side session vars + releases the coroutine ref. |
| `cnc/ncland/src/ncland_worker.cpp` | **Modify.** Promote `ne_read`/`ne_write` to non-static (`ncland_ne_read`/`ncland_ne_write`); `next_timeout_ms` accounts for `login_deadline`/`expect_deadline`. |
| `cnc/ncland/src/ncland_warehouse.cpp` | **Modify.** Call `ncland_lua_init`/`shutdown`; install path → `CS_AUTHENTICATING` + `ncland_login_start`; NE-data epoll branch dispatches by state; timer path fires `ncland_login_on_timeout`. |
| `cnc/ncland/src/ncland.mk` | **Modify.** Link `-llua` into daemon + test targets; add `ncland_lua.o` to `NCLAND_OBJS`; add `ncland_lua_tests.o` to the test objects. |

---

## Task 1: Build wiring + VM lifecycle

**Files:**
- Create: `cnc/ncland/src/ncland_lua.h`
- Create: `cnc/ncland/src/ncland_lua.cpp`
- Create: `cnc/ncland/src/ncland_lua_tests.cpp`
- Modify: `cnc/ncland/src/ncland.h` (add `L`, `lua_global_ref`, `lua_modules`, `session_vars` to `ncland_wh_t`)
- Modify: `cnc/ncland/src/ncland.mk`

- [ ] **Step 1: Add the bridge header skeleton**

Create `cnc/ncland/src/ncland_lua.h`:

```c
#ifndef NCLAND_LUA_H
#define NCLAND_LUA_H

/* Lua login bridge (design: 2026-06-05-ncland-lua-login-design.md).
 * One shared lua_State owned by the main loop; per-conn login() coroutines. */

struct ncland_wh;     /* fwd: defined in ncland.h */
struct conn_t;        /* fwd: conn_t is a typedef'd anonymous struct; see note */
typedef struct conn_t conn_t;   /* matches ncland.h's typedef name */

/** @brief login driver status, returned by on_data/on_timeout/start. */
typedef enum {
    NCLAND_LOGIN_PENDING = 0, /**< coroutine yielded; still logging in. */
    NCLAND_LOGIN_READY   = 1, /**< login() returned; conn is CS_READY. */
    NCLAND_LOGIN_FAILED  = -1 /**< error/fail/timeout; caller must tear down. */
} ncland_login_status_t;

/** @brief Create the shared Lua VM, base libs, and the global table.
 *  @return 0 on success, -1 on failure. */
int  ncland_lua_init(struct ncland_wh *wh);

/** @brief Destroy the shared Lua VM and clear caches. NULL-safe. */
void ncland_lua_shutdown(struct ncland_wh *wh);

#endif /* NCLAND_LUA_H */
```

> Note on the forward declaration: `conn_t` in `ncland.h` is `typedef struct { ... } conn_t;` (anonymous). The clean fix is in Step 3: have `ncland_lua.cpp` include `ncland.h` (which fully defines `conn_t`) rather than relying on the forward declaration, and drop the `conn_t` fwd-decl lines from this header — the header only needs `struct ncland_wh`. Replace the two `conn_t` fwd lines with nothing; functions taking `conn_t*` are declared in `ncland.h` (Task 4), not here. Keep this header limited to `ncland_lua_init`/`shutdown` + the status enum.

Final header (use this — supersedes the block above):

```c
#ifndef NCLAND_LUA_H
#define NCLAND_LUA_H

struct ncland_wh;

typedef enum {
    NCLAND_LOGIN_PENDING = 0,
    NCLAND_LOGIN_READY   = 1,
    NCLAND_LOGIN_FAILED  = -1
} ncland_login_status_t;

int  ncland_lua_init(struct ncland_wh *wh);
void ncland_lua_shutdown(struct ncland_wh *wh);

#endif /* NCLAND_LUA_H */
```

- [ ] **Step 2: Add wh fields**

In `cnc/ncland/src/ncland.h`, add to the `ncland_wh_t` struct (near the other C++ members like `dtype_allow`/`registry`, which prove the struct already holds non-POD members because `wh` is value-initialized `ncland_wh_t wh{}`, never `memset`):

```c
    /* --- Lua login bridge (#2) -------------------------------------- */
    lua_State                                         *L;              /**< Shared Lua VM; NULL until ncland_lua_init. */
    int                                                lua_global_ref; /**< Registry ref to the shared `global` table; LUA_NOREF when none. */
    std::unordered_map<std::string, int>               lua_modules;    /**< lua_module filename -> registry ref of its table; ref == LUA_NOREF marks a negative-cached (bad) module. */
    std::unordered_map<int, std::map<std::string, std::string>> session_vars; /**< conn id -> session var store (string->string). */
```

Ensure these headers are included at the top of `ncland.h` (add any missing): `#include <string>`, `#include <map>`, `#include <unordered_map>`. `lua_State` is already forward-declared in `ncland.h` (confirmed: `struct lua_State; typedef struct lua_State lua_State;`).

- [ ] **Step 3: Implement VM lifecycle**

Create `cnc/ncland/src/ncland_lua.cpp`:

```cpp
// ncland_lua.cpp — Lua 5.3 login bridge (single shared VM, per-conn coroutines).

#include "ncland.h"
#include "ncland_lua.h"
#include "nflog.hpp"

extern "C" {
#include <lua.h>
#include <lauxlib.h>
#include <lualib.h>
}

#include <string.h>

/* ------------------------------------------------------------------ */
/* VM lifecycle                                                        */
/* ------------------------------------------------------------------ */

int ncland_lua_init(struct ncland_wh *wh)
{
    if (!wh) return -1;
    if (wh->L) return 0;   /* already initialized */

    lua_State *L = luaL_newstate();
    if (!L) {
        LOG_ERROR("ncland_lua_init: luaL_newstate failed");
        return -1;
    }
    luaL_openlibs(L);

    /* Create the process-wide shared `global` table and anchor it. */
    lua_newtable(L);
    wh->lua_global_ref = luaL_ref(L, LUA_REGISTRYINDEX);

    wh->L = L;
    LOG_INFO("ncland_lua_init: Lua %s ready", LUA_RELEASE);
    return 0;
}

void ncland_lua_shutdown(struct ncland_wh *wh)
{
    if (!wh || !wh->L) return;
    lua_close(wh->L);           /* frees everything, including coroutines/refs */
    wh->L = nullptr;
    wh->lua_global_ref = LUA_NOREF;
    wh->lua_modules.clear();
}
```

- [ ] **Step 4: Wire the build**

In `cnc/ncland/src/ncland.mk`:

1. Add `ncland_lua.o` to `NCLAND_OBJS` (append to the existing list):
```
NCLAND_OBJS = ncland_conn.o ncland_proto.o ncland_ssh.o \
	ncland_warehouse.o ncland_worker.o ncland_telnet.o \
	ncland_zmq.o ncland_seed.o ncland_notify.o ncland_notify_parse.o \
	ncland_registry.o ncland_connpool.o ncland_lua.o
```
2. The daemon link line already ends with `-lyaml-cpp`; it already lists `-lssh` etc. Add `-llua` to the daemon line:
```
$(PBIN)/ncland :: ncland.o $(NCLAND_OBJS) $(INCLIBS) -latm -linc -lxmlparse -lssh -ljson -lnetclan
	$(CPLUS_CC) $(CCFLAGS:M!=-AC99) $(LDFLAGS) -o $(<) $(*) -lpthread -lssh -lzmq -lyaml-cpp -llua
```
3. Add `ncland_lua_tests.o` to the test target objects and `-llua` to its link line:
```
ncland_unit_tests :: ncland_unit_tests.o ncland_notify_parse_tests.o nclan_seed_fmt.o nclan_seed_tests.o ncland_registry_tests.o ncland_connpool_tests.o ncland_lua_tests.o $(NCLAND_OBJS) $(INCLIBS) -latm -linc -lxmlparse -lssh -ljson -lnetclan
	$(CPLUS_CC) $(CCFLAGS:M!=-AC99) $(LDFLAGS) -o $(<) $(*) -lpthread -lssh -lzmq -lyaml-cpp -llua
```

- [ ] **Step 5: Write the failing test**

Create `cnc/ncland/src/ncland_lua_tests.cpp`:

```cpp
#include "../../../include/nfunit-test.hpp"
#include "ncland.h"
#include "ncland_lua.h"

TEST("lua", "L1 init/shutdown creates and frees the VM") {
    ncland_wh_t wh{};
    wh.lua_global_ref = -2;   /* LUA_NOREF is -2 in 5.3; sentinel check below */
    REQUIRE_EQ(ncland_lua_init(&wh), 0);
    REQUIRE(wh.L != nullptr);
    REQUIRE(wh.lua_global_ref != -2 || wh.lua_global_ref >= 0); /* a real ref was assigned */
    ncland_lua_shutdown(&wh);
    REQUIRE(wh.L == nullptr);
}
```

- [ ] **Step 6: Run the test — verify it fails to link/compile first, then passes**

Run: `cd cnc/ncland/src && nmake -f ncland.mk ncland_unit_tests && ./ncland_unit_tests lua`
Expected after Steps 1-5: builds, and the `L1` test passes. (If you run it before Step 3's implementation exists, it fails to link with an undefined `ncland_lua_init` — that's the TDD red.)

- [ ] **Step 7: Run the full suite**

Run: `./ncland_unit_tests`
Expected: `76 passed, 0 failed, 6 skipped` (baseline 75 + L1).

- [ ] **Step 8: Commit**

```bash
cd /home/dan/Git/netflex
git add cnc/ncland/src/ncland_lua.h cnc/ncland/src/ncland_lua.cpp cnc/ncland/src/ncland_lua_tests.cpp cnc/ncland/src/ncland.h cnc/ncland/src/ncland.mk
git commit -m "[ncland] lua bridge: shared VM lifecycle + build wiring

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 2: session userdata — fields, log, get_var/set_var

**Files:**
- Modify: `cnc/ncland/src/ncland_lua.cpp` (session metatable + push + var store)
- Modify: `cnc/ncland/src/ncland.h` (declare `ncland_lua_push_session`; promote `ne_read`/`ne_write`)
- Modify: `cnc/ncland/src/ncland_worker.cpp` (rename `ne_read`→`ncland_ne_read`, `ne_write`→`ncland_ne_write`, drop `static`)
- Modify: `cnc/ncland/src/ncland_lua_tests.cpp`

- [ ] **Step 1: Promote ne_read/ne_write**

In `cnc/ncland/src/ncland_worker.cpp`, change the two helper definitions from `static int ne_read(...)` / `static int ne_write(...)` to `int ncland_ne_read(...)` / `int ncland_ne_write(...)`, and update their call sites within that file (`handle_ne_data`, `dispatch_cmd`, `check_keepalives`, `check_dirty_timers`) to the new names.

In `cnc/ncland/src/ncland.h`, near the other worker declarations, add:

```c
/** @brief Read available bytes from a conn's transport (ssh chan or fd). Non-blocking; returns >=0 or -1. */
int ncland_ne_read(conn_t *c, char *buf, int maxlen);
/** @brief Write bytes to a conn's transport. Returns bytes written or -1. */
int ncland_ne_write(conn_t *c, const char *buf, int len);
```

- [ ] **Step 2: Add session-var helpers + the session metatable (no expect/send yet)**

Append to `cnc/ncland/src/ncland_lua.cpp`. The session userdata stores a `conn_t*` and `ncland_wh_t*`:

```cpp
/* ------------------------------------------------------------------ */
/* session userdata                                                    */
/* ------------------------------------------------------------------ */

#define NCLAND_SESSION_MT "ncland.session"

struct session_ud {
    ncland_wh_t *wh;
    conn_t      *c;
};

static session_ud *check_session(lua_State *Ls)
{
    return (session_ud *)luaL_checkudata(Ls, 1, NCLAND_SESSION_MT);
}

/* wh-side var store accessors (string->string), keyed by conn id. */
const char *ncland_lua_get_var(ncland_wh_t *wh, conn_t *c, const char *name)
{
    auto it = wh->session_vars.find(c->id);
    if (it == wh->session_vars.end()) return nullptr;
    auto vit = it->second.find(name);
    return (vit == it->second.end()) ? nullptr : vit->second.c_str();
}

void ncland_lua_set_var(ncland_wh_t *wh, conn_t *c, const char *name, const char *value)
{
    wh->session_vars[c->id][name] = value ? value : "";
}

/* --- methods --- */

static int l_session_log(lua_State *Ls)
{
    session_ud *s = check_session(Ls);
    const char *msg = luaL_checkstring(Ls, 2);
    LOG_DEBUG("lua[neid=%d]: %s", s->c->neid, msg);
    return 0;
}

static int l_session_get_var(lua_State *Ls)
{
    session_ud *s = check_session(Ls);
    const char *name = luaL_checkstring(Ls, 2);
    const char *v = ncland_lua_get_var(s->wh, s->c, name);
    if (v) lua_pushstring(Ls, v); else lua_pushnil(Ls);
    return 1;
}

static int l_session_set_var(lua_State *Ls)
{
    session_ud *s = check_session(Ls);
    const char *name  = luaL_checkstring(Ls, 2);
    const char *value = luaL_optstring(Ls, 3, "");
    ncland_lua_set_var(s->wh, s->c, name, value);
    return 0;
}

static int l_session_fail(lua_State *Ls)
{
    const char *reason = luaL_optstring(Ls, 2, "login failed");
    return luaL_error(Ls, "%s", reason);   /* longjmp; aborts the coroutine */
}

/* __index: methods first, then read-only fields ne_type/host/user. */
static int l_session_index(lua_State *Ls)
{
    session_ud *s = (session_ud *)luaL_checkudata(Ls, 1, NCLAND_SESSION_MT);
    const char *key = luaL_checkstring(Ls, 2);

    static const luaL_Reg methods[] = {
        {"log",     l_session_log},
        {"get_var", l_session_get_var},
        {"set_var", l_session_set_var},
        {"fail",    l_session_fail},
        /* send/expect/last_output added in Task 3 */
        {nullptr, nullptr}
    };
    for (const luaL_Reg *m = methods; m->name; ++m) {
        if (strcmp(key, m->name) == 0) { lua_pushcfunction(Ls, m->func); return 1; }
    }

    if (strcmp(key, "ne_type") == 0) {
        const char *v = ncland_lua_get_var(s->wh, s->c, "ne_type");
        lua_pushstring(Ls, v ? v : ""); return 1;
    }
    if (strcmp(key, "host") == 0) { lua_pushstring(Ls, s->c->ipAddr); return 1; }
    if (strcmp(key, "user") == 0) { lua_pushstring(Ls, s->c->user);   return 1; }

    lua_pushnil(Ls);
    return 1;
}

static void ensure_session_mt(lua_State *Ls)
{
    if (luaL_newmetatable(Ls, NCLAND_SESSION_MT)) {
        lua_pushcfunction(Ls, l_session_index);
        lua_setfield(Ls, -2, "__index");
    }
    lua_pop(Ls, 1);
}

/* Push a fresh session userdata for (wh,c) onto Ls (which must be wh->L or a
 * coroutine of it — they share the registry, so the metatable is visible). */
void ncland_lua_push_session(ncland_wh_t *wh, lua_State *Ls, conn_t *c)
{
    ensure_session_mt(Ls);
    session_ud *s = (session_ud *)lua_newuserdata(Ls, sizeof(session_ud));
    s->wh = wh;
    s->c  = c;
    luaL_setmetatable(Ls, NCLAND_SESSION_MT);
}
```

In `cnc/ncland/src/ncland.h`, declare (near the wh decls):

```c
/* Lua bridge internals shared with tests / warehouse (defined in ncland_lua.cpp). */
void        ncland_lua_push_session(ncland_wh_t *wh, lua_State *Ls, conn_t *c);
const char *ncland_lua_get_var(ncland_wh_t *wh, conn_t *c, const char *name);
void        ncland_lua_set_var(ncland_wh_t *wh, conn_t *c, const char *name, const char *value);
```

(`lua_State` is already forward-declared in `ncland.h`.)

- [ ] **Step 3: Write the failing test**

Add to `cnc/ncland/src/ncland_lua_tests.cpp`. This test reaches into `wh.L` to push a session and exercises the methods via a tiny Lua chunk. Include the Lua headers in the test file:

```cpp
extern "C" {
#include <lua.h>
#include <lauxlib.h>
#include <lualib.h>
}

TEST("lua", "L2 session get/set_var round-trip and fields") {
    ncland_wh_t wh{};
    REQUIRE_EQ(ncland_lua_init(&wh), 0);
    for (int i = 0; i < MAX_CONNS; i++) conn_init(&wh.conn[i], i);
    conn_t *c = &wh.conn[3];
    c->neid = 518;
    strncpy(c->ipAddr, "10.0.0.5", sizeof(c->ipAddr)-1);
    strncpy(c->user,   "admin",    sizeof(c->user)-1);
    ncland_lua_set_var(&wh, c, "ne_type", "CIENA_RLS");

    /* push session as a global `s`, then run a chunk that uses it. */
    ncland_lua_push_session(&wh, wh.L, c);
    lua_setglobal(wh.L, "s");

    const char *chunk =
        "s:set_var('x', 'hello')\n"
        "assert(s:get_var('x') == 'hello')\n"
        "assert(s:get_var('missing') == nil)\n"
        "assert(s.host == '10.0.0.5')\n"
        "assert(s.user == 'admin')\n"
        "assert(s.ne_type == 'CIENA_RLS')\n"
        "return 1\n";
    REQUIRE_EQ(luaL_dostring(wh.L, chunk), 0);   /* 0 == LUA_OK */
    /* var also visible C-side */
    REQUIRE(ncland_lua_get_var(&wh, c, "x") != nullptr);
    REQUIRE_EQ(strcmp(ncland_lua_get_var(&wh, c, "x"), "hello"), 0);

    ncland_lua_shutdown(&wh);
}
```

- [ ] **Step 4: Build and run**

Run: `cd cnc/ncland/src && nmake -f ncland.mk ncland_unit_tests && ./ncland_unit_tests lua`
Expected: `L1` and `L2` pass.

- [ ] **Step 5: Run the full suite**

Run: `./ncland_unit_tests`
Expected: `77 passed, 0 failed, 6 skipped`.

- [ ] **Step 6: Commit**

```bash
cd /home/dan/Git/netflex
git add cnc/ncland/src/ncland_lua.cpp cnc/ncland/src/ncland.h cnc/ncland/src/ncland_worker.cpp cnc/ncland/src/ncland_lua_tests.cpp
git commit -m "[ncland] lua bridge: session userdata (vars, fields, log, fail)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 3: send(), last_output(), and the regex scan helper

**Files:**
- Modify: `cnc/ncland/src/ncland_lua.cpp`
- Modify: `cnc/ncland/src/ncland_lua_tests.cpp`

**Background:** `expect()` (Task 4) and `last_output()` both need to scan `c->rbuf[0..rlen)` from `c->out_consumed`. Extract that as a pure helper now, tested directly, before wiring the coroutine. Also add the conn_t fields the helper and Task 4 use.

- [ ] **Step 1: Add the POD conn_t login fields**

In `cnc/ncland/src/ncland.h`, inside `conn_t`, after the `lua_State *lua;` member, add:

```c
    int            lua_ref;            /**< luaL_ref anchor for `lua` in L's registry; LUA_NOREF (-2) when none. */
    regex_t        login_expect_re;    /**< Compiled pattern of the pending expect(). */
    int            login_expect_valid; /**< Non-zero when login_expect_re is compiled. */
    int            expect_timed_out;   /**< Set by on_timeout before resume so expect() returns nil. */
    time_t         login_deadline;     /**< Overall login_s hard deadline (0 = not logging in). */
    time_t         expect_deadline;    /**< Current pending expect()'s deadline (0 = none). */
    size_t         out_consumed;       /**< rbuf offset: end of the last expect() match; last_output() = rbuf[out_consumed..rlen). */
```

All POD — safe under `memset`/`memcpy`. `conn_init`'s `memset` zeroes them; set `lua_ref = LUA_NOREF` explicitly in Task 4's `login_start` (don't rely on 0, since `LUA_NOREF` is -2).

- [ ] **Step 2: Add the scan helper, send, last_output**

Append to `cnc/ncland/src/ncland_lua.cpp`:

```cpp
/* ------------------------------------------------------------------ */
/* buffer scan                                                         */
/* ------------------------------------------------------------------ */

/* Scan c->rbuf[c->out_consumed .. c->rlen) for `re`. On match, set *mend to the
 * rbuf offset just past the match and return 1; else return 0. The unconsumed
 * region is NUL-terminated for regexec (rbuf always has a spare byte: rlen <
 * sizeof(rbuf)). */
static int scan_unconsumed(conn_t *c, const regex_t *re, size_t *mend)
{
    if (c->out_consumed > (size_t)c->rlen) c->out_consumed = (size_t)c->rlen;
    const char *base = c->rbuf + c->out_consumed;
    /* rbuf is kept NUL-terminated by the reader; ensure it here defensively. */
    c->rbuf[c->rlen] = '\0';
    regmatch_t m;
    if (regexec(re, base, 1, &m, 0) != 0) return 0;
    *mend = c->out_consumed + (size_t)m.rm_eo;
    return 1;
}

/* ------------------------------------------------------------------ */
/* send / last_output                                                  */
/* ------------------------------------------------------------------ */

static int l_session_send(lua_State *Ls)
{
    session_ud *s = check_session(Ls);
    size_t len = 0;
    const char *str = luaL_checklstring(Ls, 2, &len);
    int n = ncland_ne_write(s->c, str, (int)len);
    if (n < 0) return luaL_error(Ls, "send failed (neid=%d)", s->c->neid);
    lua_pushinteger(Ls, n);
    return 1;
}

static int l_session_last_output(lua_State *Ls)
{
    session_ud *s = check_session(Ls);
    conn_t *c = s->c;
    if (c->out_consumed > (size_t)c->rlen) c->out_consumed = (size_t)c->rlen;
    lua_pushlstring(Ls, c->rbuf + c->out_consumed, (size_t)c->rlen - c->out_consumed);
    return 1;
}
```

Register `send` and `last_output` in `l_session_index`'s `methods[]` (add the two rows):

```cpp
        {"send",        l_session_send},
        {"last_output", l_session_last_output},
```

- [ ] **Step 3: Write the failing test (scan + send over socketpair)**

Add to `cnc/ncland/src/ncland_lua_tests.cpp` (ensure `#include <fcntl.h>`, `#include <sys/socket.h>`, `#include <unistd.h>`):

```cpp
TEST("lua", "L3 send writes to the transport fd") {
    int sv[2]; REQUIRE_EQ(socketpair(AF_UNIX, SOCK_STREAM, 0, sv), 0);
    ncland_wh_t wh{};
    REQUIRE_EQ(ncland_lua_init(&wh), 0);
    for (int i = 0; i < MAX_CONNS; i++) conn_init(&wh.conn[i], i);
    conn_t *c = &wh.conn[2];
    c->net_fd = sv[0];

    ncland_lua_push_session(&wh, wh.L, c);
    lua_setglobal(wh.L, "s");
    REQUIRE_EQ(luaL_dostring(wh.L, "return s:send('hello\\n')"), 0);

    char buf[32] = {0};
    int n = (int)read(sv[1], buf, sizeof(buf)-1);
    REQUIRE(n >= 6);
    REQUIRE(strncmp(buf, "hello\n", 6) == 0);

    ncland_lua_shutdown(&wh);
    close(sv[0]); close(sv[1]);
}

TEST("lua", "L4 last_output returns unconsumed buffer") {
    ncland_wh_t wh{};
    REQUIRE_EQ(ncland_lua_init(&wh), 0);
    for (int i = 0; i < MAX_CONNS; i++) conn_init(&wh.conn[i], i);
    conn_t *c = &wh.conn[1];
    snprintf(c->rbuf, sizeof(c->rbuf), "abcDEF");
    c->rlen = 6;
    c->out_consumed = 3;   /* "abc" already consumed */

    ncland_lua_push_session(&wh, wh.L, c);
    lua_setglobal(wh.L, "s");
    REQUIRE_EQ(luaL_dostring(wh.L, "assert(s:last_output() == 'DEF'); return 1"), 0);

    ncland_lua_shutdown(&wh);
}
```

- [ ] **Step 4: Build and run**

Run: `cd cnc/ncland/src && nmake -f ncland.mk ncland_unit_tests && ./ncland_unit_tests lua`
Expected: `L1`-`L4` pass.

- [ ] **Step 5: Full suite**

Run: `./ncland_unit_tests`
Expected: `79 passed, 0 failed, 6 skipped`.

- [ ] **Step 6: Commit**

```bash
cd /home/dan/Git/netflex
git add cnc/ncland/src/ncland_lua.cpp cnc/ncland/src/ncland.h cnc/ncland/src/ncland_lua_tests.cpp
git commit -m "[ncland] lua bridge: send/last_output + buffer scan helper + conn login fields

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 4: login coroutine drive — expect (yieldk), login_start, on_data, on_timeout

**Files:**
- Modify: `cnc/ncland/src/ncland_lua.cpp`
- Modify: `cnc/ncland/src/ncland.h` (declare driver entry points)
- Modify: `cnc/ncland/src/ncland_lua_tests.cpp`

This is the core task. `expect()` yields with a continuation; the driver resumes the coroutine on data/timeout and maps the coroutine status to `ncland_login_status_t`.

- [ ] **Step 1: Declare the driver entry points**

In `cnc/ncland/src/ncland.h`, add (after the session-var decls):

```c
/** @brief Begin login() for a freshly-installed, transport-connected conn.
 *  Loads the dtype's lua_module, creates the coroutine, pre-populates reserved
 *  session vars, arms login_deadline, and does the first resume.
 *  @return NCLAND_LOGIN_PENDING / NCLAND_LOGIN_READY / NCLAND_LOGIN_FAILED. */
int ncland_login_start(ncland_wh_t *wh, conn_t *c);

/** @brief Feed newly-readable NE bytes into the in-progress login coroutine. */
int ncland_login_on_data(ncland_wh_t *wh, conn_t *c);

/** @brief Fire when expect_deadline or login_deadline trips. which: 0=expect, 1=login. */
int ncland_login_on_timeout(ncland_wh_t *wh, conn_t *c, int which);
```

Include `ncland_lua.h` for `ncland_login_status_t`, or note the int return uses the enum values. (`ncland.h` should `#include "ncland_lua.h"` once near the top so the enum is visible to all `.cpp` that include `ncland.h`.)

- [ ] **Step 2: Implement expect with yieldk + continuation, the resume helper, and module loading**

Append to `cnc/ncland/src/ncland_lua.cpp`. Add `#include <time.h>` and `#include <stdlib.h>` at the top of the file if not present.

```cpp
/* ------------------------------------------------------------------ */
/* expect (yields across the C boundary via lua_yieldk)                */
/* ------------------------------------------------------------------ */

/* Forward: the continuation re-checks the buffer after each resume. */
static int expect_k(lua_State *Ls, int status, lua_KContext ctx);

static int l_session_expect(lua_State *Ls)
{
    session_ud *s = check_session(Ls);
    conn_t *c = s->c;
    const char *pat = luaL_checkstring(Ls, 2);
    lua_Integer tmo = luaL_optinteger(Ls, 3, 0);   /* 0 => clamp to login budget */

    /* Compile (replace any stale pending expect). */
    if (c->login_expect_valid) { regfree(&c->login_expect_re); c->login_expect_valid = 0; }
    if (regcomp(&c->login_expect_re, pat, REG_EXTENDED) != 0)
        return luaL_error(Ls, "expect: bad regex '%s'", pat);
    c->login_expect_valid = 1;

    /* Immediate match? */
    size_t mend = 0;
    if (scan_unconsumed(c, &c->login_expect_re, &mend)) {
        size_t start = c->out_consumed;
        lua_pushlstring(Ls, c->rbuf + start, mend - start);
        c->out_consumed = mend;
        regfree(&c->login_expect_re); c->login_expect_valid = 0;
        return 1;
    }

    /* Arm the per-expect deadline (clamped to remaining login budget). */
    time_t now = time(NULL);
    time_t per = (tmo > 0) ? now + (time_t)tmo : c->login_deadline;
    if (c->login_deadline && per > c->login_deadline) per = c->login_deadline;
    c->expect_deadline  = per;
    c->expect_timed_out = 0;

    return lua_yieldk(Ls, 0, (lua_KContext)c, expect_k);
}

static int expect_k(lua_State *Ls, int status, lua_KContext ctx)
{
    (void)status;
    conn_t *c = (conn_t *)ctx;

    if (c->expect_timed_out) {
        c->expect_timed_out = 0;
        c->expect_deadline  = 0;
        if (c->login_expect_valid) { regfree(&c->login_expect_re); c->login_expect_valid = 0; }
        lua_pushnil(Ls);
        return 1;   /* expect() returns nil on timeout (spec §9.2) */
    }

    size_t mend = 0;
    if (c->login_expect_valid && scan_unconsumed(c, &c->login_expect_re, &mend)) {
        size_t start = c->out_consumed;
        lua_pushlstring(Ls, c->rbuf + start, mend - start);
        c->out_consumed = mend;
        c->expect_deadline = 0;
        regfree(&c->login_expect_re); c->login_expect_valid = 0;
        return 1;
    }

    /* Still no match and not timed out → keep waiting. */
    return lua_yieldk(Ls, 0, ctx, expect_k);
}
```

Register `expect` in `l_session_index`'s `methods[]`:

```cpp
        {"expect",      l_session_expect},
```

Module loading + the resume helper + the driver:

```cpp
/* ------------------------------------------------------------------ */
/* module load/cache                                                   */
/* ------------------------------------------------------------------ */

/* Resolve lua_module for `dtype`, load+cache it, and push its `login` function
 * onto wh->L. Returns 1 on success (login pushed), 0 on failure (nothing
 * pushed; negative-cached). */
static int push_login_fn(ncland_wh_t *wh, int dtype)
{
    const ne_entry_t *e = ncland_registry_find(&wh->registry, dtype);
    if (!e || e->lua_module.empty()) {
        LOG_WARN("login: dtype=%d has no lua_module", dtype);
        return 0;
    }
    const std::string &mod = e->lua_module;

    /* Cache hit? */
    auto it = wh->lua_modules.find(mod);
    if (it != wh->lua_modules.end()) {
        if (it->second == LUA_NOREF) return 0;   /* negative-cached */
        lua_rawgeti(wh->L, LUA_REGISTRYINDEX, it->second);   /* module table */
        lua_getfield(wh->L, -1, "login");
        lua_remove(wh->L, -2);                    /* drop module table, keep fn */
        return 1;
    }

    /* Load NCLAND_NE_DIR/<lua_module> as a chunk, run it to get the module table. */
    std::string path = std::string(NCLAND_NE_DIR) + "/" + mod;
    if (luaL_loadfile(wh->L, path.c_str()) != LUA_OK) {
        LOG_WARN("login: load %s failed: %s", path.c_str(), lua_tostring(wh->L, -1));
        lua_pop(wh->L, 1);
        wh->lua_modules[mod] = LUA_NOREF;
        return 0;
    }
    if (lua_pcall(wh->L, 0, 1, 0) != LUA_OK) {
        LOG_WARN("login: run %s failed: %s", path.c_str(), lua_tostring(wh->L, -1));
        lua_pop(wh->L, 1);
        wh->lua_modules[mod] = LUA_NOREF;
        return 0;
    }
    if (!lua_istable(wh->L, -1)) {
        LOG_WARN("login: %s did not return a module table", path.c_str());
        lua_pop(wh->L, 1);
        wh->lua_modules[mod] = LUA_NOREF;
        return 0;
    }
    lua_getfield(wh->L, -1, "login");
    if (!lua_isfunction(wh->L, -1)) {
        LOG_WARN("login: %s exports no login() function", path.c_str());
        lua_pop(wh->L, 2);
        wh->lua_modules[mod] = LUA_NOREF;
        return 0;
    }
    lua_pop(wh->L, 1);   /* drop the login fn we just probed */
    int ref = luaL_ref(wh->L, LUA_REGISTRYINDEX);   /* cache the module table */
    wh->lua_modules[mod] = ref;

    lua_rawgeti(wh->L, LUA_REGISTRYINDEX, ref);
    lua_getfield(wh->L, -1, "login");
    lua_remove(wh->L, -2);
    return 1;
}

/* ------------------------------------------------------------------ */
/* reserved vars                                                       */
/* ------------------------------------------------------------------ */

static void populate_reserved_vars(ncland_wh_t *wh, conn_t *c)
{
    char tmp[64];
    ncland_lua_set_var(wh, c, "user",     c->user);
    ncland_lua_set_var(wh, c, "password", c->password);
    ncland_lua_set_var(wh, c, "host",     c->ipAddr);
    snprintf(tmp, sizeof(tmp), "%d", c->neid); ncland_lua_set_var(wh, c, "neid", tmp);
    snprintf(tmp, sizeof(tmp), "%d", c->slot); ncland_lua_set_var(wh, c, "slot", tmp);
    const ne_entry_t *e = ncland_registry_find(&wh->registry, c->dtype);
    if (e) {
        ncland_lua_set_var(wh, c, "ne_type", e->ne_type.c_str());
        ncland_lua_set_var(wh, c, "adapt_from_actual", e->adapt_from_actual ? "true" : "false");
    }
    /* p_enable/p_read/p_write/card_type: not sourced in #2 (left unset). */
}

/* ------------------------------------------------------------------ */
/* resume helper + driver                                              */
/* ------------------------------------------------------------------ */

/* Resume c->lua with `narg` args already pushed on it. Map the coroutine
 * outcome to ncland_login_status_t and update conn state. */
static int resume_login(ncland_wh_t *wh, conn_t *c, int narg)
{
    int st = lua_resume(c->lua, wh->L, narg);
    if (st == LUA_YIELD) {
        return NCLAND_LOGIN_PENDING;
    }
    if (st == LUA_OK) {
        LOG_INFO("session ready neid=%d conn=%d", c->neid, c->id);
        conn_set_state(c, CS_READY);
        c->login_deadline = 0;
        c->expect_deadline = 0;
        if (c->login_expect_valid) { regfree(&c->login_expect_re); c->login_expect_valid = 0; }
        /* Release the coroutine anchor; the thread is now done. */
        if (c->lua_ref != LUA_NOREF) { luaL_unref(wh->L, LUA_REGISTRYINDEX, c->lua_ref); c->lua_ref = LUA_NOREF; }
        c->lua = nullptr;
        return NCLAND_LOGIN_READY;
    }
    /* error */
    LOG_WARN("login failed neid=%d conn=%d: %s", c->neid, c->id,
             lua_tostring(c->lua, -1) ? lua_tostring(c->lua, -1) : "(error)");
    return NCLAND_LOGIN_FAILED;
}

int ncland_login_start(ncland_wh_t *wh, conn_t *c)
{
    if (!wh || !wh->L || !c) return NCLAND_LOGIN_FAILED;

    /* Reset login buffer view. */
    c->out_consumed       = 0;
    c->login_expect_valid = 0;
    c->expect_timed_out   = 0;
    c->expect_deadline    = 0;
    c->lua_ref            = LUA_NOREF;
    int login_s = (c->login_tmout > 0) ? c->login_tmout : 60;
    c->login_deadline     = time(NULL) + login_s;

    populate_reserved_vars(wh, c);

    /* Create the coroutine and anchor it. */
    c->lua = lua_newthread(wh->L);
    c->lua_ref = luaL_ref(wh->L, LUA_REGISTRYINDEX);   /* pops the thread, keeps it alive */

    /* Push login fn + (session, global, nil) onto the coroutine. */
    if (!push_login_fn(wh, c->dtype)) {
        /* nothing pushed on wh->L; tear the coroutine down */
        luaL_unref(wh->L, LUA_REGISTRYINDEX, c->lua_ref); c->lua_ref = LUA_NOREF; c->lua = nullptr;
        return NCLAND_LOGIN_FAILED;
    }
    lua_xmove(wh->L, c->lua, 1);                         /* move login fn to coroutine */
    ncland_lua_push_session(wh, c->lua, c);              /* arg1: session */
    lua_rawgeti(c->lua, LUA_REGISTRYINDEX, wh->lua_global_ref); /* arg2: global */
    lua_pushnil(c->lua);                                 /* arg3: args (nil in #2) */

    conn_set_state(c, CS_AUTHENTICATING);
    int rc = resume_login(wh, c, 3);
    if (rc == NCLAND_LOGIN_FAILED) {
        if (c->lua_ref != LUA_NOREF) { luaL_unref(wh->L, LUA_REGISTRYINDEX, c->lua_ref); c->lua_ref = LUA_NOREF; }
        c->lua = nullptr;
    }
    return rc;
}

int ncland_login_on_data(ncland_wh_t *wh, conn_t *c)
{
    if (!wh || !c || !c->lua) return NCLAND_LOGIN_FAILED;

    /* Read available bytes; append to rbuf (cap at max_buffer_bytes via rbuf size). */
    char tmp[CLAN2_MAX_RBUF];
    int n = ncland_ne_read(c, tmp, sizeof(tmp) - 1);
    if (n > 0) {
        int space = (int)sizeof(c->rbuf) - c->rlen - 1;
        if (n > space) {
            LOG_WARN("login: neid=%d buffer overflow, failing", c->neid);
            return NCLAND_LOGIN_FAILED;
        }
        memcpy(c->rbuf + c->rlen, tmp, (size_t)n);
        c->rlen += n;
        c->rbuf[c->rlen] = '\0';
        c->last_activity = time(NULL);
    } else if (n < 0) {
        return NCLAND_LOGIN_FAILED;   /* hard read error / EOF */
    }

    int rc = resume_login(wh, c, 0);
    if (rc == NCLAND_LOGIN_FAILED) {
        if (c->lua_ref != LUA_NOREF) { luaL_unref(wh->L, LUA_REGISTRYINDEX, c->lua_ref); c->lua_ref = LUA_NOREF; }
        c->lua = nullptr;
    }
    return rc;
}

int ncland_login_on_timeout(ncland_wh_t *wh, conn_t *c, int which)
{
    if (!wh || !c || !c->lua) return NCLAND_LOGIN_FAILED;
    if (which == 1) {   /* overall login deadline: hard fail */
        LOG_WARN("login timed out neid=%d conn=%d (login_s)", c->neid, c->id);
        if (c->lua_ref != LUA_NOREF) { luaL_unref(wh->L, LUA_REGISTRYINDEX, c->lua_ref); c->lua_ref = LUA_NOREF; }
        c->lua = nullptr;
        return NCLAND_LOGIN_FAILED;
    }
    /* expect deadline: resume so expect() returns nil and the script decides. */
    c->expect_timed_out = 1;
    int rc = resume_login(wh, c, 0);
    if (rc == NCLAND_LOGIN_FAILED) {
        if (c->lua_ref != LUA_NOREF) { luaL_unref(wh->L, LUA_REGISTRYINDEX, c->lua_ref); c->lua_ref = LUA_NOREF; }
        c->lua = nullptr;
    }
    return rc;
}
```

> Note: `push_login_fn` references `ne_entry_t::lua_module`, `ne_type`, `adapt_from_actual` and `ncland_registry_find` — all already defined in `ncland_registry.h` (included via `ncland.h`). Confirm `ncland.h` includes `ncland_registry.h`; if not, add it.

- [ ] **Step 3: Write the failing tests — happy path, partial reads, module-missing**

Add to `cnc/ncland/src/ncland_lua_tests.cpp`. These write a temp `.lua` into `NCLAND_NE_DIR` — but that's a fixed system path; instead, drive login with an **inline module** by pre-seeding the cache. To keep tests hermetic, add a tiny test-only helper to the bridge that injects a module from a string:

In `ncland_lua.cpp` add (guarded comment — it's a test seam, kept tiny and public):

```cpp
/* Test seam: load a module from a source string and cache it under `name`. */
int ncland_lua_inject_module(ncland_wh_t *wh, const char *name, const char *src)
{
    if (luaL_loadstring(wh->L, src) != LUA_OK || lua_pcall(wh->L, 0, 1, 0) != LUA_OK) {
        LOG_WARN("inject_module %s: %s", name, lua_tostring(wh->L, -1));
        lua_pop(wh->L, 1);
        return -1;
    }
    if (!lua_istable(wh->L, -1)) { lua_pop(wh->L, 1); return -1; }
    lua_getfield(wh->L, -1, "login");
    int ok = lua_isfunction(wh->L, -1);
    lua_pop(wh->L, 1);
    if (!ok) { lua_pop(wh->L, 1); wh->lua_modules[name] = LUA_NOREF; return -1; }
    wh->lua_modules[name] = luaL_ref(wh->L, LUA_REGISTRYINDEX);
    return 0;
}
```

Declare it in `ncland.h`: `int ncland_lua_inject_module(ncland_wh_t *wh, const char *name, const char *src);`

The test registers a dtype→lua_module mapping by inserting a minimal `ne_entry_t` into `wh.registry.by_dtype`, then injects the module under that filename. Test code:

```cpp
#include <fcntl.h>

/* Build a wh with a test dtype 9001 -> module "t.lua", and inject `src`. */
static void setup_login_wh(ncland_wh_t &wh, int dtype, const char *src) {
    REQUIRE_EQ(ncland_lua_init(&wh), 0);
    for (int i = 0; i < MAX_CONNS; i++) conn_init(&wh.conn[i], i);
    ne_entry_t e;
    e.dtype = dtype;
    e.ne_type = "TEST_NE";
    e.lua_module = "t.lua";
    wh.registry.by_dtype[dtype] = e;
    REQUIRE_EQ(ncland_lua_inject_module(&wh, "t.lua", src), 0);
}

TEST("lua", "L5 happy-path login reaches CS_READY") {
    int sv[2]; REQUIRE_EQ(socketpair(AF_UNIX, SOCK_STREAM, 0, sv), 0);
    fcntl(sv[0], F_SETFL, O_NONBLOCK);

    const char *src =
        "local M={}\n"
        "function M.login(s)\n"
        "  s:expect('[Ll]ogin:'); s:send(s:get_var('user')..'\\n')\n"
        "  s:expect('[Pp]assword:'); s:send(s:get_var('password')..'\\n')\n"
        "  s:expect('#')\n"
        "end\n"
        "return M\n";
    ncland_wh_t wh{};
    setup_login_wh(wh, 9001, src);
    conn_t *c = &wh.conn[5];
    c->net_fd = sv[0]; c->dtype = 9001; c->neid = 42;
    strncpy(c->user, "adm", sizeof(c->user)-1);
    strncpy(c->password, "pw", sizeof(c->password)-1);

    /* First resume yields waiting for "login:". */
    REQUIRE_EQ(ncland_login_start(&wh, c), NCLAND_LOGIN_PENDING);

    write(sv[1], "login: ", 7);
    REQUIRE_EQ(ncland_login_on_data(&wh, c), NCLAND_LOGIN_PENDING);
    char b[64]={0}; int n=(int)read(sv[1], b, sizeof(b)-1);
    REQUIRE(n>0 && strstr(b,"adm")!=NULL);

    write(sv[1], "password: ", 10);
    REQUIRE_EQ(ncland_login_on_data(&wh, c), NCLAND_LOGIN_PENDING);
    memset(b,0,sizeof(b)); n=(int)read(sv[1], b, sizeof(b)-1);
    REQUIRE(n>0 && strstr(b,"pw")!=NULL);

    write(sv[1], "sw1# ", 5);
    REQUIRE_EQ(ncland_login_on_data(&wh, c), NCLAND_LOGIN_READY);
    REQUIRE_EQ((int)c->state, (int)CS_READY);

    ncland_lua_shutdown(&wh);
    close(sv[0]); close(sv[1]);
}

TEST("lua", "L6 prompt split across two reads") {
    int sv[2]; REQUIRE_EQ(socketpair(AF_UNIX, SOCK_STREAM, 0, sv), 0);
    fcntl(sv[0], F_SETFL, O_NONBLOCK);
    const char *src =
        "local M={} function M.login(s) s:expect('sw1#') end return M\n";
    ncland_wh_t wh{}; setup_login_wh(wh, 9001, src);
    conn_t *c=&wh.conn[6]; c->net_fd=sv[0]; c->dtype=9001;
    REQUIRE_EQ(ncland_login_start(&wh, c), NCLAND_LOGIN_PENDING);
    write(sv[1], "sw", 2);
    REQUIRE_EQ(ncland_login_on_data(&wh, c), NCLAND_LOGIN_PENDING);
    write(sv[1], "1# ", 3);
    REQUIRE_EQ(ncland_login_on_data(&wh, c), NCLAND_LOGIN_READY);
    ncland_lua_shutdown(&wh); close(sv[0]); close(sv[1]);
}

TEST("lua", "L7 module without login() is rejected and negative-cached") {
    ncland_wh_t wh{};
    REQUIRE_EQ(ncland_lua_init(&wh), 0);
    for (int i = 0; i < MAX_CONNS; i++) conn_init(&wh.conn[i], i);
    ne_entry_t e; e.dtype=9002; e.ne_type="BAD"; e.lua_module="bad.lua";
    wh.registry.by_dtype[9002]=e;
    /* a module table with no login: inject returns -1 and negative-caches. */
    REQUIRE_EQ(ncland_lua_inject_module(&wh, "bad.lua", "return {}\n"), -1);
    conn_t *c=&wh.conn[7]; c->dtype=9002;
    REQUIRE_EQ(ncland_login_start(&wh, c), NCLAND_LOGIN_FAILED);
    ncland_lua_shutdown(&wh);
}
```

- [ ] **Step 4: Build and run**

Run: `cd cnc/ncland/src && nmake -f ncland.mk ncland_unit_tests && ./ncland_unit_tests lua`
Expected: `L1`-`L7` pass.

- [ ] **Step 5: Full suite**

Run: `./ncland_unit_tests`
Expected: `82 passed, 0 failed, 6 skipped`.

- [ ] **Step 6: Commit**

```bash
cd /home/dan/Git/netflex
git add cnc/ncland/src/ncland_lua.cpp cnc/ncland/src/ncland.h cnc/ncland/src/ncland_lua_tests.cpp
git commit -m "[ncland] lua bridge: login coroutine drive (expect/yieldk, start, on_data, on_timeout, module cache)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 5: warehouse integration + timeout/teardown wiring

**Files:**
- Modify: `cnc/ncland/src/ncland_warehouse.cpp`
- Modify: `cnc/ncland/src/ncland_conn.cpp`
- Modify: `cnc/ncland/src/ncland_worker.cpp`
- Modify: `cnc/ncland/src/ncland_lua_tests.cpp`

- [ ] **Step 1: Init/shutdown the VM in the warehouse**

In `cnc/ncland/src/ncland_warehouse.cpp`, `warehouse_init`: after `connpool_init` (and before/after registry load is fine — order independent), add:

```c
    if (ncland_lua_init(wh) != 0)
        LOG_WARN("warehouse_init: lua init failed; logins will fail");
```

In `warehouse_main_loop`, just before the function returns (after the `while (wh->running)` loop ends), add:

```c
    ncland_lua_shutdown(wh);
```

Ensure `ncland_warehouse.cpp` includes `"ncland_lua.h"` (or relies on `ncland.h` including it).

- [ ] **Step 2: Install path → CS_AUTHENTICATING + login_start**

In `ncland_warehouse.cpp`, the pool-completion success branch currently does `conn_set_state(slot, CS_READY); LOG_INFO("session up …")`. Replace the state-set + log with a login kickoff:

```c
                    conn_t *slot = &wh->conn[cid];
                    struct epoll_event nev;
                    memset(&nev, 0, sizeof(nev));
                    nev.events  = EPOLLIN;
                    nev.data.fd = slot->net_fd;
                    if (slot->net_fd >= 0 &&
                        epoll_ctl(epfd, EPOLL_CTL_ADD, slot->net_fd, &nev) < 0)
                        LOG_WARN("warehouse_main_loop: epoll_ctl add net_fd=%d: %s",
                                 slot->net_fd, strerror(errno));
                    LOG_INFO("transport up neid=%d conn=%d; starting login", slot->neid, cid);
                    int lst = ncland_login_start(wh, slot);
                    if (lst == NCLAND_LOGIN_FAILED) {
                        if (wh->epfd >= 0 && slot->net_fd >= 0)
                            epoll_ctl(epfd, EPOLL_CTL_DEL, slot->net_fd, NULL);
                        conn_free(wh, cid);
                        if (wh->nconns > 0) wh->nconns--;
                    }
                    /* PENDING → driven by EPOLLIN; READY (rare: login matched on
                     * banner already buffered) → nothing more to do here. */
```

- [ ] **Step 3: NE-data branch dispatches by state**

In the same loop, the existing "else" branch routes readable NE fds to `handle_ne_data`. Make it state-aware:

```c
            } else {
                conn_t *c = wh_fd_to_conn(wh, fd);
                if (!c) {
                    LOG_WARN("warehouse_main_loop: unknown fd %d in epoll event", fd);
                } else if (c->state == CS_AUTHENTICATING) {
                    int lst = ncland_login_on_data(wh, c);
                    if (lst == NCLAND_LOGIN_FAILED) {
                        if (wh->epfd >= 0 && c->net_fd >= 0)
                            epoll_ctl(epfd, EPOLL_CTL_DEL, c->net_fd, NULL);
                        int id = c->id;
                        conn_free(wh, id);
                        if (wh->nconns > 0) wh->nconns--;
                    }
                } else {
                    handle_ne_data(c, wh);
                }
            }
```

- [ ] **Step 4: Timeout wiring in the timer path**

`next_timeout_ms` (in `ncland_worker.cpp`) currently scans `CS_CMD_PENDING` deadlines. Extend it to also consider `login_deadline`/`expect_deadline` for `CS_AUTHENTICATING` conns:

```c
int next_timeout_ms(ncland_wh_t *wh)
{
    time_t now = time(NULL);
    int    min_ms = KEEPALIVE_INTERVAL_MS;
    for (int i = 0; i < MAX_CONNS; i++) {
        conn_t *c = &wh->conn[i];
        time_t dl = 0;
        if (c->state == CS_CMD_PENDING) dl = c->cmd_deadline;
        else if (c->state == CS_AUTHENTICATING) {
            dl = c->expect_deadline ? c->expect_deadline : c->login_deadline;
            if (c->login_deadline && (!dl || c->login_deadline < dl)) {
                /* login_deadline is the hard cap; pick the nearer of the two */
            }
        }
        if (!dl) continue;
        time_t diff = dl - now;
        if (diff <= 0) return 0;
        int ms = (int)(diff * 1000);
        if (ms < min_ms) min_ms = ms;
    }
    return min_ms;
}
```

In `ncland_warehouse.cpp`, after the per-iteration timer calls (`check_keepalives`, `check_dirty_timers`, `check_expired_cmds`), add a login-deadline sweep:

```c
        /* Login deadline sweep over CS_AUTHENTICATING conns. */
        {
            time_t now = time(NULL);
            for (int i = 0; i < MAX_CONNS; i++) {
                conn_t *c = &wh->conn[i];
                if (c->state != CS_AUTHENTICATING) continue;
                int which = -1;
                if (c->login_deadline && now >= c->login_deadline) which = 1;       /* hard */
                else if (c->expect_deadline && now >= c->expect_deadline) which = 0; /* expect */
                if (which < 0) continue;
                int lst = ncland_login_on_timeout(wh, c, which);
                if (lst == NCLAND_LOGIN_FAILED) {
                    if (wh->epfd >= 0 && c->net_fd >= 0)
                        epoll_ctl(wh->epfd, EPOLL_CTL_DEL, c->net_fd, NULL);
                    int id = c->id;
                    conn_free(wh, id);
                    if (wh->nconns > 0) wh->nconns--;
                }
            }
        }
```

- [ ] **Step 5: conn_free cleans login state**

In `cnc/ncland/src/ncland_conn.cpp`, `conn_free`, after the existing regex frees, add login-state cleanup:

```c
    /* Login-state cleanup (#2 Lua bridge). */
    if (c->login_expect_valid) { regfree(&c->login_expect_re); c->login_expect_valid = 0; }
    if (c->lua_ref != LUA_NOREF && wh->L) {
        luaL_unref(wh->L, LUA_REGISTRYINDEX, c->lua_ref);
    }
    c->lua_ref = LUA_NOREF;
    c->lua = NULL;
    c->login_deadline = c->expect_deadline = 0;
    c->out_consumed = 0;
    wh->session_vars.erase(c->id);
```

`ncland_conn.cpp` must include the Lua headers for `luaL_unref`/`LUA_NOREF`/`LUA_REGISTRYINDEX`:

```c
extern "C" {
#include <lua.h>
#include <lauxlib.h>
}
```

> Note: `conn_init` sets all-zero, so `lua_ref` starts at 0, not `LUA_NOREF` (-2). A slot that never logged in has `lua_ref == 0`, which is a *valid* registry ref index. To avoid `luaL_unref`-ing ref 0 spuriously, `conn_init` must set `c->lua_ref = LUA_NOREF;` explicitly. Add that line to `conn_init` in `ncland_conn.cpp` (after the `memset`).

- [ ] **Step 6: Write the failing tests — expect-timeout, login_s timeout, global-shared, overflow**

Add to `cnc/ncland/src/ncland_lua_tests.cpp`:

```cpp
TEST("lua", "L8 expect timeout returns nil to the script") {
    int sv[2]; REQUIRE_EQ(socketpair(AF_UNIX, SOCK_STREAM, 0, sv), 0);
    fcntl(sv[0], F_SETFL, O_NONBLOCK);
    /* login: expect with 1s; on nil, set a var and finish OK. */
    const char *src =
        "local M={} function M.login(s)\n"
        "  if s:expect('NEVER', 1) == nil then s:set_var('to','yes') end\n"
        "end return M\n";
    ncland_wh_t wh{}; setup_login_wh(wh, 9001, src);
    conn_t *c=&wh.conn[8]; c->net_fd=sv[0]; c->dtype=9001;
    REQUIRE_EQ(ncland_login_start(&wh, c), NCLAND_LOGIN_PENDING);
    /* Force the expect deadline into the past, then fire the expect timeout. */
    c->expect_deadline = time(NULL) - 1;
    REQUIRE_EQ(ncland_login_on_timeout(&wh, c, 0), NCLAND_LOGIN_READY);
    REQUIRE(ncland_lua_get_var(&wh, c, "to") != nullptr);
    ncland_lua_shutdown(&wh); close(sv[0]); close(sv[1]);
}

TEST("lua", "L9 overall login timeout tears down") {
    int sv[2]; REQUIRE_EQ(socketpair(AF_UNIX, SOCK_STREAM, 0, sv), 0);
    fcntl(sv[0], F_SETFL, O_NONBLOCK);
    const char *src = "local M={} function M.login(s) s:expect('NEVER') end return M\n";
    ncland_wh_t wh{}; setup_login_wh(wh, 9001, src);
    conn_t *c=&wh.conn[9]; c->net_fd=sv[0]; c->dtype=9001;
    REQUIRE_EQ(ncland_login_start(&wh, c), NCLAND_LOGIN_PENDING);
    REQUIRE_EQ(ncland_login_on_timeout(&wh, c, 1), NCLAND_LOGIN_FAILED);
    REQUIRE(c->lua == nullptr);
    ncland_lua_shutdown(&wh); close(sv[0]); close(sv[1]);
}

TEST("lua", "L10 global table shared across two conns") {
    int sv0[2], sv1[2];
    socketpair(AF_UNIX, SOCK_STREAM, 0, sv0); fcntl(sv0[0], F_SETFL, O_NONBLOCK);
    socketpair(AF_UNIX, SOCK_STREAM, 0, sv1); fcntl(sv1[0], F_SETFL, O_NONBLOCK);
    const char *src =
        "local M={} function M.login(s,g)\n"
        "  g.n = (g.n or 0) + 1; s:set_var('seen', tostring(g.n)); s:expect('#')\n"
        "end return M\n";
    ncland_wh_t wh{}; setup_login_wh(wh, 9001, src);
    conn_t *a=&wh.conn[10]; a->net_fd=sv0[0]; a->dtype=9001;
    conn_t *b=&wh.conn[11]; b->net_fd=sv1[0]; b->dtype=9001;
    REQUIRE_EQ(ncland_login_start(&wh, a), NCLAND_LOGIN_PENDING);
    REQUIRE_EQ(ncland_login_start(&wh, b), NCLAND_LOGIN_PENDING);
    REQUIRE_EQ(strcmp(ncland_lua_get_var(&wh,a,"seen"),"1"), 0);
    REQUIRE_EQ(strcmp(ncland_lua_get_var(&wh,b,"seen"),"2"), 0);
    ncland_lua_shutdown(&wh);
    close(sv0[0]); close(sv0[1]); close(sv1[0]); close(sv1[1]);
}

TEST("lua", "L11 buffer overflow during login fails cleanly") {
    int sv[2]; REQUIRE_EQ(socketpair(AF_UNIX, SOCK_STREAM, 0, sv), 0);
    fcntl(sv[0], F_SETFL, O_NONBLOCK); fcntl(sv[1], F_SETFL, O_NONBLOCK);
    const char *src = "local M={} function M.login(s) s:expect('ZZZ') end return M\n";
    ncland_wh_t wh{}; setup_login_wh(wh, 9001, src);
    conn_t *c=&wh.conn[12]; c->net_fd=sv[0]; c->dtype=9001;
    REQUIRE_EQ(ncland_login_start(&wh, c), NCLAND_LOGIN_PENDING);
    /* Pre-fill rbuf to near-full so the next read overflows. */
    c->rlen = (int)sizeof(c->rbuf) - 2;
    memset(c->rbuf, 'x', c->rlen);
    char big[256]; memset(big, 'y', sizeof(big));
    write(sv[1], big, sizeof(big));
    REQUIRE_EQ(ncland_login_on_data(&wh, c), NCLAND_LOGIN_FAILED);
    ncland_lua_shutdown(&wh); close(sv[0]); close(sv[1]);
}
```

- [ ] **Step 7: Build and run**

Run: `cd cnc/ncland/src && nmake -f ncland.mk ncland_unit_tests && ./ncland_unit_tests lua`
Expected: `L1`-`L11` pass.

- [ ] **Step 8: Full suite + daemon build**

Run: `./ncland_unit_tests`
Expected: `86 passed, 0 failed, 6 skipped`.
Run: `nmake -f ncland.mk`
Expected: `rc=0`, `../../../3b2/bin/ncland` relinks.

- [ ] **Step 9: Commit**

```bash
cd /home/dan/Git/netflex
git add cnc/ncland/src/ncland_warehouse.cpp cnc/ncland/src/ncland_conn.cpp cnc/ncland/src/ncland_worker.cpp cnc/ncland/src/ncland_lua_tests.cpp
git commit -m "[ncland] wire Lua login into the epoll loop (install->CS_AUTHENTICATING, data/timeout drive, teardown)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 6: Spec reconciliation + a sample login .lua + memory

**Files:**
- Modify: `~/docs/superpowers/specs/2026-05-27-clan-yaml-design.md` (§9.3, §9 intro, §6 path)
- Create: an example `*.lua` for one real dtype, committed under the netFLEX repo's NE-config source dir (ask the user where the source `ne/*.yaml` live; the runtime dir `/usr/cnc/lib/data/ncland` is a deploy target, not source).
- Modify: memory file `project_ncland_is_clan_rewrite.md` (mark #2 done)

- [ ] **Step 1: Patch the older spec**

In `~/docs/superpowers/specs/2026-05-27-clan-yaml-design.md`:
- §9.3: replace the per-worker-process `global` description with: a single process-wide shared Lua table, no lock (single-process / single-Lua-thread daemon).
- §9 intro: add a sentence that `login()` runs in the main epoll loop as a coroutine — `session:expect()` yields and is resumed on epoll readiness; it never blocks the loop.
- §6: change the `lua/` subdir to a flat layout (companion `.lua` beside the `.yaml` under `NCLAND_NE_DIR`).

- [ ] **Step 2: Commit the spec patch (in the docs repo)**

```bash
cd /home/dan/docs
git add superpowers/specs/2026-05-27-clan-yaml-design.md
git commit -m "spec: reconcile clan-yaml §9.3/§9/§6 to single-process Lua login (see 2026-06-05 login design)"
```

- [ ] **Step 3: Write one real login .lua (paired with an existing ne yaml)**

Ask the user which dtype to start with (likely `CIENA_RLS`, dtype 89, since its `lua_module: ciena_rls.lua` already appears in registry tests). Write `ciena_rls.lua` exporting `M.login` that drives that NE's real dialogue, using `session:expect`/`send`/`get_var`. Place it beside the `ciena_rls.yaml` source file (path per the user's answer). This is the first end-to-end artifact for live testing.

> This step has a genuine unknown (the source dir for NE config + the real CIENA_RLS dialogue), so it ends with a user question rather than fabricated content. Do not invent the dialogue — confirm the prompt sequence with the user or the legacy `setupCienaRLSConnection` (clan.c L1306) before writing it.

- [ ] **Step 4: Update memory**

Edit `/home/dan/.claude/projects/-home-dan-Git-netflex/memory/project_ncland_is_clan_rewrite.md`: note #2 (Lua login bridge) is implemented (commit range from this plan), #3 stepper still pending. Add a one-line pointer if a new memory is warranted.

- [ ] **Step 5: Redeploy + live test**

```bash
cp /home/dan/Git/netflex/3b2/bin/ncland /usr/cnc/bin/ncland   # or the deploy path the user uses
# copy the .lua beside the deployed .yaml under /usr/cnc/lib/data/ncland
```
Restart ncland; in the new log expect, for a reachable CIENA_RLS NE:
`transport up … starting login` → (login dialogue) → `session ready neid=… conn=…`.
Capture the log and review with the user.

---

## Self-Review (against the spec)

**1. Spec coverage:**
- §1 goal (drive to command-ready) → Tasks 4–5. ✓
- §2 execution model (shared `L`, per-conn coroutine, no lock, install→CS_AUTHENTICATING→READY/teardown) → Tasks 1, 4, 5. ✓
- §3 components/files → all files mapped to tasks. ✓
- §4 session API (send/expect/last_output/get_var/set_var/log/fail + ne_type/host/user; reserved vars; global) → Tasks 2 (vars/fields/log/fail), 3 (send/last_output), 4 (expect, reserved vars, global passed to login). ✓
- §5 error/edge (module missing→neg-cache, coroutine error, dead-fd write, partial prompt, buffer overflow, expect vs login timeout, no retry) → Task 4 (module, error, partial, expect/login timeout), Task 5 (overflow path in on_data is Task 4; teardown in Task 5), Task 3 (send error). ✓
- §6 data flow → realized by Task 4/5. ✓
- §7 testing (8 cases) → L5 (happy), L6 (partial), L8 (expect timeout), L9 (login timeout), L7 (module missing), L10 (global shared), L2/L4 (reserved vars/last_output), L11 (overflow). ✓
- §8 defaults (flat lua path, string vars, both timeouts, Lua 5.3 yieldk shim) → Task 4 (`NCLAND_NE_DIR/<mod>`, yieldk), Task 2 (string vars), Task 4 (clamp expect to login budget). ✓
- §9 reconciliation → Task 6. ✓

**2. Placeholder scan:** No "TBD"/"add error handling"/"similar to". The one genuine unknown (Task 6 Step 3, the real CIENA_RLS dialogue + source dir) is explicitly flagged as a user question, not fabricated — correct handling per the no-placeholder rule (don't invent domain facts).

**3. Type consistency:** `ncland_login_status_t` values used identically across `ncland.h` decls and `ncland_lua.cpp`. `ncland_ne_read`/`ncland_ne_write` names consistent (Task 2 rename → Task 4 use). `conn_t` fields (`lua_ref`, `login_expect_re`, `login_expect_valid`, `expect_timed_out`, `login_deadline`, `expect_deadline`, `out_consumed`) declared once (Task 3 Step 1) and used in Task 4/5 with matching names/types. `session_vars`/`lua_modules`/`L`/`lua_global_ref` wh fields declared once (Task 1) and used consistently. `LUA_NOREF` handling reconciled in Task 5 (conn_init sets it). ✓

---

## Execution note

Tasks 1→5 are strictly sequential (each builds on the prior file state). Task 6 is docs + one domain artifact and depends on a user answer. Recommended: subagent-driven execution, reviewing between tasks; Task 6 Step 3 must pause for the user.
