# ncland #2 — Lua login() Bridge Design

**Date:** 2026-06-05
**Status:** Approved (design); ready for implementation plan
**Component:** `cnc/ncland/src`
**Builds on:** the completed slot-on-success connect path (commit `831f0e3fb`) and
the YAML registry (`ncland_registry.*`). Companion to the broader
`2026-05-27-clan-yaml-design.md` spec — this document is the authoritative,
post-single-process-restructure design for the login bridge and **supersedes
that spec's §9.3** (`global` table model) and refines its §9 intro and §6 Lua
path. See "Spec reconciliation" at the end.

---

## 1. Goal

Embed a Lua VM in the single-process `ncland` daemon and drive each
transport-established NE session through its per-dtype CLI login dialogue
(banner → username → password → enable → steady-state prompt) until the session
is sitting at the prompt described by `ne/<name>.yaml`'s `prompt.primary_regex`
— i.e. command-ready. Login behavior lives in each NE's companion `.lua`
module's required `login(session, global, args)` entry point (spec §9.5); adding
or changing an NE's login requires no C recompilation.

**In scope (#2):** Lua VM embedding, the `session` API (§9.2), the shared
`global` table, the module loader/cache, and `login()` invocation driven by the
epoll loop (coroutine yield/resume on `expect`), with login-success → `CS_READY`
and login-failure → teardown.

**Out of scope (deferred to #3, the stepper):** the declarative step language
(`post_command`, `disconnect_steps`), the `lua` step op, the optional
`post_command_hook` / `disconnect_hook`, and keepalive integration. The
`session` API and `global` table built here are the foundation #3 reuses.

---

## 2. Execution Model

One shared `lua_State *L` for the whole process, created in `warehouse_init` and
freed at shutdown. **Single-threaded:** only the main epoll loop ever touches
Lua. Connect-pool threads remain transport-only and never call into Lua. Each
connection runs its `login()` as a **coroutine** (`lua_newthread(L)`), anchored
in the registry (`luaL_ref`) so the GC cannot collect it mid-login.

Because everything Lua-side runs on the single main-loop thread, there is **no
locking** anywhere in the bridge: `global` is a plain shared table, and per-conn
coroutines never run concurrently. (This is the key simplification over the
pre-restructure design, which assumed separate worker processes — see §9.3
reconciliation.)

Per-NE lifecycle:

```
CS_CONNECTING        pool thread: TCP connect + (ssh) protocol auth + open channel
   │ connect ok
   ▼
[install slot]       main loop installs the established carrier (unchanged path)
   │
   ▼
CS_AUTHENTICATING    main loop: lua_newthread(L); push module.login,
   │                 session userdata, global table, args; lua_resume
   │
   │  login() runs straight-line imperative code. session:expect() checks the
   │  conn's read buffer: match → returns the matched string; no match → records
   │  the expect deadline and lua_yields. On the next EPOLLIN the loop reads,
   │  appends to the buffer, and lua_resumes; expect re-scans. On the expect
   │  deadline with no match, the loop resumes such that expect returns nil.
   │
   ├─ login() returns normally ............ ▶ CS_READY   (LOG_INFO "session ready")
   └─ coroutine error / session:fail() /
      overall login_s deadline exceeded .... ▶ conn_free + LOG_WARN, no retry
```

A conn-table slot **is** held during login. This is acceptable: the connection
is a live, transport-established NE, and login is bounded by `login_s` (default
60 s). The flooding problem solved by slot-on-success was *dead connects* (10 s
telnet timeouts) squatting slots — those now fail at the transport stage and are
never installed, so they never reach login.

### Why coroutine-backed (not pool-thread-blocking, not reactive-handler)

- **Not pool-thread blocking:** would hold a pool thread for the whole login and
  duplicate the transport teardown logic; the main-loop coroutine approach keeps
  all session I/O in one place and never blocks the loop.
- **Not a reactive state-machine handler:** that would force every `.lua` author
  to hand-roll a state machine and would change the §9.1/§9.5 contract. The
  coroutine approach lets authors write straight-line `expect`/`send` code while
  the runtime makes it non-blocking via `lua_yield`/`lua_resume`.

---

## 3. Components / Files

### Create `ncland_lua.h` / `ncland_lua.cpp`

The bridge. Public surface (prefix `ncland_lua_` / `ncland_login_`):

| Function | Purpose |
|----------|---------|
| `int  ncland_lua_init(ncland_wh_t *wh)` | Create `L`, open base libs, create the `global` table, register the `session` metatable. Returns 0 / -1. |
| `void ncland_lua_shutdown(ncland_wh_t *wh)` | Close `L`, clear the module cache. |
| `int  ncland_login_start(ncland_wh_t *wh, conn_t *c)` | Resolve+load the dtype's module, create the coroutine, pre-populate reserved vars, do the first `lua_resume`. Returns 0 (in progress / done) or -1 (could not start → caller tears down). |
| `int  ncland_login_on_data(ncland_wh_t *wh, conn_t *c)` | Called by the main loop on EPOLLIN while `CS_AUTHENTICATING`: read into the buffer, resume the coroutine. Returns a status (pending / ready / failed). |
| `int  ncland_login_on_timeout(ncland_wh_t *wh, conn_t *c, int which)` | Called when an expect or the overall login deadline trips. `which` ∈ {expect, login}. Resumes with nil (expect) or tears down (login). |

Internal: module load/cache (keyed by `lua_module` filename → a Lua registry
ref to the module table; negative-cached on failure), the `session` userdata +
metatable methods, and the resume helper that distinguishes
resumed-by-data from resumed-by-timeout.

### Modify `ncland.h`

`conn_t` additions (per-connection login state):

```c
lua_State *lua;            /* coroutine for this conn's login; NULL when none. (EXISTS) */
int        lua_ref;        /* luaL_ref anchor for `lua` in L's registry; LUA_NOREF when none. */
regex_t    login_expect_re; /* compiled pattern of the pending expect(). */
int        login_expect_valid;
time_t     login_deadline;  /* overall login_s deadline (0 = not logging in). */
time_t     expect_deadline; /* current pending expect()'s deadline (0 = none). */
size_t     out_consumed;    /* rbuf offset marking the end of the last expect()'s match;
                               last_output() returns rbuf[out_consumed .. rlen). */
```

`ncland_wh_t` additions:

```c
lua_State *L;              /* shared Lua VM; NULL until ncland_lua_init. */
int        lua_global_ref; /* registry ref to the shared `global` table. */
/* module cache: filename -> registry ref (or negative marker). std::unordered_map. */
```

### Modify `ncland_warehouse.cpp`

- **Install path** (pool-completion success handler): set `CS_AUTHENTICATING`
  and call `ncland_login_start` instead of going straight to `CS_READY`. If
  `ncland_login_start` returns -1, tear the just-installed slot down.
- **NE-data epoll branch:** dispatch by state — `CS_AUTHENTICATING` →
  `ncland_login_on_data`; otherwise the existing `handle_ne_data`
  (`CS_CMD_PENDING`).
- **Timer path:** for `CS_AUTHENTICATING` conns, fire `ncland_login_on_timeout`
  on expect/login deadlines.
- **`ncland_lua_init`** called from `warehouse_init`; `ncland_lua_shutdown` on
  loop exit.

### Modify `ncland_worker.cpp`

`next_timeout_ms` includes `expect_deadline` / `login_deadline` for
`CS_AUTHENTICATING` conns (alongside the existing `cmd_deadline` logic).

### Modify `ncland.mk`

Add `-llua` to the daemon link line and the `ncland_unit_tests` link line (today
only `nclan-seed` links it). Add `ncland_lua.o` to `NCLAND_OBJS` and
`ncland_lua_tests.o` to the test objects.

### Create `ncland_lua_tests.cpp`

Unit tests (§7).

---

## 4. session API (mechanics)

`session` is a Lua userdata wrapping `conn_t *c` and `ncland_wh_t *wh`. Its
metatable implements (spec §9.2):

| Method | Behavior |
|--------|----------|
| `send(str)` | `ne_write(c, str, len)`. Login writes are small; a short write is treated as success. Returns bytes written. |
| `expect(regex, timeout_s)` | Compile (and cache on the conn) `regex`. Scan `c->rbuf` from `out_consumed`. **Match:** set `out_consumed` to the end of the match, clear the pending-expect state, return the matched substring. **No match:** store `login_expect_re` + `expect_deadline = now + min(timeout_s, remaining login_s)`, then `lua_yield(L, 0)`. On resume-by-data the method re-runs its scan; on resume-by-timeout it returns `nil`. |
| `last_output()` | Returns `c->rbuf[out_consumed .. rlen)` as a string. |
| `get_var(name)` / `set_var(name, value)` | C-backed **per-conn string→string** store (so vars persist independent of the VM and are reused by #3's stepper). |
| `log(msg)` | `LOG_DEBUG("lua[neid=%d]: %s", c->neid, msg)`. |
| `fail(reason)` | `luaL_error(L, "%s", reason)` — aborts the coroutine; the loop tears the session down. |
| `ne_type` / `host` / `user` (fields) | Read-only, via the metatable `__index`. |

**Reserved session vars**, pre-populated by `ncland_login_start` from
`conn_t` / es64 data before the first resume (spec §9.2 reserved set): `user`,
`password`, `p_enable`, `p_read`, `p_write`, `card_type`, `host`, `ne_type`,
`slot`, `neid`, `primary_regex` / `secondary_regex` (the NE's YAML prompt
regexes, so `login()` matches against the single source of truth — added
2026-06-09), plus `adapt_from_actual` (string `"true"`/`"false"`, §7.7 — the
bridge sets it; it has no C-side behavior). A var with no source value is unset
(`get_var` returns `nil`). Authors may `set_var` any other name freely.

**`global` table:** created once in `L` at init, passed as the 2nd argument to
`login()`. A plain shared Lua table, process-lifetime, **no lock** (single
threaded). Persists across all NE types and all sessions.

### The resume helper

A single internal function performs every `lua_resume` and, before resuming,
records *why* (new-data vs timeout) so the `expect` continuation knows whether to
re-scan or return `nil`. After `lua_resume` returns it inspects the status:

- `LUA_YIELD` → login still in progress; arm timers from `expect_deadline`.
- `LUA_OK` (coroutine finished) → `login()` returned → transition to `CS_READY`
  (`out_consumed` reset, `lua_ref` released, `lua` cleared), `LOG_INFO`.
- error (`LUA_ERRRUN` etc.) → `LOG_WARN("login failed neid=%d: %s", …, msg)` →
  teardown.

---

## 5. Error Handling & Edge Cases

- **Module missing / no `login`:** the module is loaded lazily on first use per
  `lua_module` filename and cached; a load error or a module that does not export
  a `login` function is **negative-cached** (so it warns once per dtype). At
  `login_start` a negative-cached or unloadable module → `LOG_WARN` + return -1 →
  the caller tears down the just-installed slot.
- **Coroutine runtime error / `fail()`:** captured message → `LOG_WARN` →
  teardown.
- **Write to a dead fd:** `ne_write` < 0 surfaces as a Lua error → teardown.
- **Partial prompt split across reads:** handled naturally — `rbuf` accumulates
  and `expect` re-scans the whole unconsumed region on each resume.
- **Buffer overflow during login:** the unconsumed region is capped at
  `max_buffer_bytes` (registry field, default 1 MB); on overflow the login is
  failed (`LOG_WARN` + teardown) rather than growing unbounded.
- **expect timeout vs overall login timeout:** both honored. Each `expect`
  arms `expect_deadline = now + min(timeout_s, remaining login_s)`; the overall
  `login_deadline = login_start_time + login_s` is a hard ceiling that tears the
  session down regardless of what the coroutine is doing.
- **No retry:** a failed login is torn down and not retried, consistent with the
  connect-failure path. A later seed/notify event for the same NE will attempt
  again.

---

## 6. Data Flow Summary

```
pool thread        main loop (epoll)                    Lua (coroutine on L)
-----------        -----------------                    --------------------
connect() ───────▶ install slot, CS_AUTHENTICATING
                   ncland_login_start ────────────────▶ login(session, global, args)
                                                          session:expect("login:")
                   ◀──────────── lua_yield (no match) ──   (buffer empty)
   (EPOLLIN) ─────▶ read → append rbuf
                   ncland_login_on_data → resume ──────▶   expect re-scans → match
                                                          session:send(user)
                   ◀──────────── ne_write("user\n") ────
                                                          session:expect("password:")
                   ◀──────────── lua_yield ─────────────
   (EPOLLIN) ─────▶ read → append → resume ────────────▶   match; send(pass); expect(prompt)
                   ◀──────────── lua_yield ─────────────
   (EPOLLIN) ─────▶ read → append → resume ────────────▶   prompt match → login() returns
                   CS_READY ("session ready") ◀─────────  LUA_OK
```

---

## 7. Testing

All tests drive the coroutine with **synthetic bytes over a `socketpair`** — no
real NE. `ne_read`/`ne_write` already work on plain fds, so a test conn whose
`net_fd` is one end of a socketpair, with the test writing the "NE side" on the
other end, exercises the full path.

Test cases (`ncland_lua_tests.cpp`, suite `"lua"`):

1. **Happy path:** load a test module
   (`login`: `expect "login:"` → send `user` → `expect "password:"` → send
   `password` → `expect "#"`). Feed `"login: "`, assert `user\n` appears on the
   peer; feed `"password: "`, assert `password\n`; feed `"sw1# "`, assert the
   conn reaches `CS_READY`.
2. **Yield/resume across partial reads:** deliver the prompt in two chunks
   (`"sw"` then `"1# "`); assert the match only completes after the second feed.
3. **expect timeout → nil:** a module whose `login` does
   `if not session:expect("X", 1) then session:log("timed out"); ... end`;
   advance the expect deadline; assert `expect` returns `nil` and the script's
   timeout branch runs.
4. **Overall login_s timeout:** a module that never matches; trip
   `login_deadline`; assert teardown (`CS_DISCONNECTED`/freed) + WARN.
5. **Module missing `login`:** a `.lua` exporting no `login` → `login_start`
   returns -1; negative-cached (second call does not re-load).
6. **`global` shared across conns:** conn A's `login` does
   `global.n = (global.n or 0) + 1`; run two conns; assert the second sees
   `global.n == 2`.
7. **Reserved vars populated:** assert `session:get_var("user")`,
   `"host"`, `"ne_type"`, `"neid"` match the conn's record inside `login`.
8. **Buffer overflow:** feed more than `max_buffer_bytes` without a match →
   teardown + WARN.

Existing suites must stay green (75/0/6). The connect-path and registry tests are
unaffected (login only engages once a transport is established; `g_warehouse_no_connect`
test conns never enter `CS_AUTHENTICATING`).

---

## 8. Chosen Defaults (decided, not open)

- **Lua module path:** `NCLAND_NE_DIR/<lua_module>` — flat, same directory as the
  YAML files (`/usr/cnc/lib/data/ncland`). The pre-restructure spec §6 showed a
  `lua/` subdir; this design uses a flat layout to match how the registry already
  loads YAML from `NCLAND_NE_DIR`. (Spec §6 to be reconciled to flat.)
- **`get_var`/`set_var` value type:** strings (sufficient for creds, prompt
  strings, and author state in #2). Richer types can come with #3 if needed.
- **Both** the per-`expect` `timeout_s` and the overall `login_s` deadline are
  enforced (the expect deadline is clamped to the remaining login budget).
- **Lua version:** whatever `-llua` resolves to on the build hosts (Lua 5.x);
  the bridge uses only stable C-API calls (`lua_newthread`, `lua_resume`,
  `lua_yield`, `luaL_ref`, userdata + metatables) available across 5.1–5.4.
  `lua_resume`'s arity differs across versions — wrapped in one compat shim.

---

## 9. Spec Reconciliation (changes to `2026-05-27-clan-yaml-design.md`)

This design supersedes parts of the older spec that predate the single-process
restructure. Alongside implementation, patch the older spec:

- **§9.3 `global` table:** replace the per-worker-process description (mutexed
  proxy, "not shared across workers") with: a single process-wide shared Lua
  table, no lock, since the daemon is single-process / single-Lua-thread.
- **§9 intro:** add a note that `login()` runs in the main epoll loop as a
  coroutine — `session:expect()` yields and is resumed on epoll readiness; it
  never blocks the loop.
- **§6 Lua path:** the `lua/` subdir becomes a flat layout under
  `NCLAND_NE_DIR` (companion `.lua` beside the `.yaml`).
- **§4.1/§4.2 process model:** already known-superseded by the single-process
  design (connect thread-pool + one epoll loop); note that login is part of that
  loop.

The `login(session, global, args)` contract (§9.1), the `session` method set
(§9.2), the required-`login` rule (§9.5), and the reserved-var list remain
authoritative and unchanged.
