# ncland #3c — Lua-Driven NE Simulator (`nclansim`) for Unit Tests

**Status:** design approved 2026-06-12. Implementation plan to follow.
**Predecessors:** `2026-06-11-ncland-stepper-3b-design.md` (step engine, which the sim is built to test).
**Reference:** `cnc/sdi/src/xlansim.c` (clan's existing external-process sim) and `3b2/data/clansim.lua` (existing prompts+files-style Lua simulator). The new tool reuses neither directly — it's a purpose-built test fixture.
**Branch:** `ncland-start`.

---

## 1. Goal & Scope

`nclansim` is a **test-only** Lua-driven NE simulator that lives in-process with `ncland_unit_tests`. It lets a test author write a single Lua script that emulates an NE's CLI dialogue, then drive ncland against it end-to-end (login, command, post_command, disconnect_steps, EOF) without touching real hardware.

### In scope

- In-process socketpair-based sim — `sv[0]` becomes ncland's `conn->net_fd`; `sv[1]` is the sim's own end.
- One C API for tests (`nclansim_open` / `_step` / `_step_until` / `_received` / `_received_matches` / `_close`).
- One Lua API for scripts (`sim.send`, `sim.expect`, `sim.last_received`, `sim.log`, `sim.neid`).
- A per-sim `lua_State` separate from `wh->L`, so sim scripts cannot collide with ncland's NE Lua modules.
- Explicit tick model: `nclansim_step(sim)` pumps both halves until quiescence. No background threads.
- Sim-side history buffer for assertions (`nclansim_received` / `_received_matches`).

### Out of scope

- Real TCP listeners — the existing socketpair pattern is sufficient; no port management needed.
- SSH simulation — sim is telnet-shape only. Login Lua scripts for SSH NEs (e.g. PSS) can use the same sim because ncland's transport layer is irrelevant to the post-connect dialogue.
- Sub-process spawning — the entire sim lives in the test thread.
- Asynchronous out-of-band injection (e.g. unsolicited alarms during a command). Tests that need this can write to `sv[1]` directly; not worth a dedicated API.
- A `clansim.lua`-style declarative prompts-and-canned-responses helper. The `sim.expect`/`sim.send` primitives are flexible enough; a higher-level helper can be added later if a pattern emerges.

---

## 2. Architecture

```
                ┌─────────────────────────────┐
                │  Test (nfunit-test C++17)   │
                └────────────┬────────────────┘
                             │ nclansim_open(wh, neid, dtype, "sim.lua")
                             ▼
   ┌─────────────────────────────────────────────────┐
   │  socketpair(AF_UNIX, SOCK_STREAM, …, sv)        │
   │  sv[0] ◄────────────────────────────────► sv[1] │
   └────┬─────────────────────────────────────┬──────┘
        │                                     │
        │ assigned to wh->conn[id].net_fd     │ owned by nclansim_t
        ▼                                     ▼
   ┌──────────────────┐                ┌─────────────────────────┐
   │  ncland          │                │   sim's Lua VM          │
   │  conn[id]        │                │   (own lua_State, not   │
   │  + login         │                │    wh->L)               │
   │    coroutine     │                │   one coroutine running │
   │    on wh->L      │                │   the loaded script     │
   └──────────────────┘                └─────────────────────────┘
```

**Lifecycle (one sim instance):**

1. `nclansim_open` creates the socketpair, allocates a conn slot, installs the sim end of the socketpair into `wh->conn[id].net_fd`, applies the dtype's compiled registry entry (prompt regex, more_pattern, etc.), and starts ncland's login via `ncland_login_start`. Ncland's login coroutine immediately yields on its first `session:expect`.
2. The test calls `nclansim_step(sim)` (typically inside `nclansim_step_until`) to alternate-pump both sides.
3. The script runs until it returns (clean exit) or the test calls `nclansim_close(sim)`.
4. `nclansim_close` releases the sim coroutine, `lua_close`s the sim VM, closes `sv[1]` (`sv[0]` is closed by ncland's normal conn teardown), and `warehouse_drop_conn`s the slot.

**Single funnel for teardown:** `nclansim_close` is the only place a sim is freed. If the script returns mid-test, the sim is `coro_done` but otherwise alive until close — letting the test inspect history and assert.

**Build wiring:** `ncland_sim.o` and `ncland_sim_tests.o` are added to the `ncland_unit_tests` prereq list in `ncland.mk`. **Not** added to `NCLAND_OBJS` — the sim is test-only and must not link into `3b2/bin/ncland`.

---

## 3. `nclansim_t` Struct & C API

### 3.1 Struct (`ncland_sim.h`)

```c
typedef struct nclansim {
    int            sv[2];           /**< sv[0] = ncland's net_fd; sv[1] = sim's own end */
    int            conn_id;         /**< Slot in wh->conn (managed via standard install path) */
    ncland_wh_t   *wh;              /**< Parent warehouse */

    lua_State     *L;               /**< Sim's own Lua VM (separate from wh->L) */
    int            coro_ref;        /**< LUA_REGISTRYINDEX ref to the sim coroutine; LUA_NOREF when done */
    int            coro_started;    /**< 1 once the script's first resume has run */
    int            coro_done;       /**< 1 once the script returned or raised; further ticks are no-ops */
    int            got_eof;         /**< 1 once ncland closed sv[0] (read returned 0); used to break a pending expect */

    int            expect_active;   /**< 1 when sim's coroutine is yielded inside sim.expect() */
    regex_t        expect_re;       /**< Compiled pattern from the current sim.expect() call */
    int            expect_re_valid; /**< 1 if expect_re holds a valid compiled regex */
    time_t         expect_deadline; /**< 0 = no deadline; otherwise wall-clock seconds */

    std::string    rbuf;            /**< Bytes from ncland not yet consumed by sim.expect */
    std::string    last_match;      /**< Match text of the most recent satisfied sim.expect */
    std::string    history;         /**< Every byte ever received from ncland (for assertions) */
} nclansim_t;
```

`std::string` is used freely because `nclansim_t` is allocated by the test (heap, not in `conn_t`) — POD discipline does not apply.

### 3.2 Public C API (`ncland_sim.h`)

```c
/** @brief Create a sim, slot it as wh->conn[id], start ncland's login.
 *  @return Sim handle on success, NULL on any setup failure (logged). */
nclansim_t *nclansim_open(ncland_wh_t *wh, int neid, int dtype,
                          const char *lua_script_path);

/** @brief One round of bidirectional pumping. Returns when neither side has
 *  data to consume AND the sim coroutine is yielded without a satisfiable
 *  expect AND ncland's handle_ne_data is idle. */
void nclansim_step(nclansim_t *sim);

/** @brief Repeatedly nclansim_step until wh->conn[sim->conn_id].state ==
 *  target_state, or max_iters elapses. Returns 1 if reached, 0 on timeout. */
int  nclansim_step_until(nclansim_t *sim, conn_state_t target_state, int max_iters);

/** @brief Copy the full received history into buf; returns bytes available
 *  (may be larger than maxlen, in which case buf is truncated). */
int  nclansim_received(const nclansim_t *sim, char *buf, int maxlen);

/** @brief 1 if `regex` (POSIX ERE) matches anywhere in the received history. */
int  nclansim_received_matches(const nclansim_t *sim, const char *regex);

/** @brief Release coroutine, close socketpair, drop conn from wh, free sim. */
void nclansim_close(nclansim_t *sim);
```

---

## 4. Sim-Side Lua API

A single global table `sim` is installed into the sim's `lua_State` before the script runs.

| Lua call | Returns | Behavior |
|----------|---------|----------|
| `sim.send(text)` | nothing | Write `text` (raw bytes, no substitution) to `sv[1]` immediately. |
| `sim.expect(pattern, timeout_s)` | matched string or `nil` on timeout | Yield until `pattern` (POSIX ERE) matches `sim->rbuf`. `timeout_s` is wall-clock seconds; default 5 if omitted; 0 = no deadline. Returns the full match text. Consumes everything up to and including `rm_eo` from rbuf. |
| `sim.last_received()` | string | Bytes since the most recent `sim.expect` resolved (or since `nclansim_open` if none has). Doesn't consume. |
| `sim.log(msg)` | nothing | One INFO log line: `"sim[neid=N]: <msg>"`. |
| `sim.neid` | integer | Read-only field. The neid the sim is impersonating. |

### 4.1 Script entry point

The script is loaded with `luaL_loadfile` into a new coroutine (`lua_newthread`); the loaded chunk IS the coroutine body. No `M.run(sim)` or similar wrapper. Example:

```lua
-- sim/fake_cisco.lua
sim.send("\r\nUser Access Verification\r\n\r\n")
sim.send("Username: ")
local user = sim.expect("[^\r\n]+\r?\n", 5)
sim.send("Password: ")
sim.expect("[^\r\n]+\r?\n", 5)
sim.send("device# ")

while true do
  local cmd = sim.expect("([^\r\n]+)\r?\n", 30)
  if cmd == nil then return end                      -- timeout / EOF
  cmd = cmd:gsub("[\r\n]+$", "")
  if     cmd:match("^show version")  then sim.send("Cisco IOS XR 7.5.2\r\ndevice# ")
  elseif cmd:match("^quit")          then sim.send("Goodbye\r\n"); return
  elseif cmd:match("^write memory")  then sim.send("Building configuration...\r\n[OK]\r\ndevice# ")
  else                                    sim.send("% Invalid input\r\ndevice# ")
  end
end
```

The script's natural top-level return ends the simulation cleanly (`coro_done = 1`). The test can still `nclansim_received_matches` afterwards to verify what the NE side saw.

---

## 5. The `nclansim_step` Algorithm

### 5.1 Fixed-point pump

```c
void nclansim_step(nclansim_t *sim)
{
    if (!sim) return;
    conn_t *c = &sim->wh->conn[sim->conn_id];

    for (;;) {
        bool progressed = false;

        /* Phase A: kick the sim coroutine if it has never run yet. */
        if (!sim->coro_started && !sim->coro_done) {
            sim_resume(sim);
            sim->coro_started = 1;
            progressed = true;
        }

        /* Phase B: drain ncland→sim. Append any bytes from sv[1] into
         * sim->rbuf and sim->history. */
        if (sim_read_from_ncland(sim) > 0) progressed = true;

        /* Phase C: try to satisfy a pending sim.expect. */
        if (sim->expect_active && sim_try_match(sim)) {
            sim_resume(sim);
            progressed = true;
        }

        /* Phase D: drive ncland's handle_ne_data; it reads from sv[0]
         * and runs the state machine for this conn. */
        int          pre_rlen  = c->rlen;
        conn_state_t pre_state = c->state;
        handle_ne_data(c, sim->wh);
        if (c->rlen != pre_rlen || c->state != pre_state) progressed = true;

        if (!progressed) return;     /* both sides stalled */
    }
}
```

`nclansim_step_until(sim, target_state, max_iters)` repeatedly calls `nclansim_step` and checks `c->state == target_state` after each round, returning early on match or 0 after the cap.

### 5.2 `sim_read_from_ncland`

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
    if (n == 0) {                  /* ncland closed sv[0] = EOF */
        sim->got_eof = 1;
        return -1;                  /* signal EOF to the loop, distinct from EAGAIN */
    }
    /* errno == EAGAIN/EWOULDBLOCK → no data, no error. Other errno → real error,
     * but we treat it the same: nothing to consume right now. */
    return 0;
}
```

`sv[1]` is non-blocking. `n == 0` (orderly peer close) sets `got_eof` and returns `-1`; the step loop reads that as "EOF arrived this round" and treats it as progress so phase C runs and resolves any pending `expect` with `nil`.

### 5.3 `sim.expect` — yielding side

The C function backing `sim.expect`:

1. Read `pattern` (string) and `timeout_s` (number, default 5) from the Lua stack.
2. `regcomp` the pattern with `REG_EXTENDED`. On failure: `luaL_error("sim.expect: bad regex '%s'", pattern)`.
3. **If `sim->got_eof` is already set**: `regfree`, push `nil`, return 1 — no point yielding when ncland has already closed the connection.
4. Set `sim->expect_re_valid = 1`, `sim->expect_active = 1`, `sim->expect_deadline = (timeout_s > 0) ? time(NULL) + timeout_s : 0`.
5. Try a synchronous match against `sim->rbuf` immediately (the requested data may already be present).
6. If matched synchronously: consume, set `last_match`, clear `expect_*` flags, push the match string, return 1.
7. Otherwise: `lua_yieldk(L, 0, 0, sim_expect_continuation)`. The continuation simply returns 1 (the resumed value already on the stack — pushed by `sim_resume` — is the script's `sim.expect` return).

### 5.4 `sim_try_match` — resolve a pending expect from the C side

```c
static bool sim_try_match(nclansim_t *sim)
{
    if (!sim->expect_active || !sim->expect_re_valid) return false;

    sim->rbuf.push_back('\0');  /* NUL-terminate for regexec */
    regmatch_t m;
    int rc = regexec(&sim->expect_re, sim->rbuf.data(), 1, &m, 0);
    sim->rbuf.pop_back();

    if (rc != 0) {
        bool timed_out = sim->expect_deadline && time(NULL) >= sim->expect_deadline;
        if (timed_out || sim->got_eof) {
            sim->last_match.clear();
            regfree(&sim->expect_re); sim->expect_re_valid = 0;
            sim->expect_active = 0;
            return true;  /* resume; sim_resume will push nil */
        }
        return false;
    }
    /* Match: capture and consume up through rm_eo */
    sim->last_match.assign(sim->rbuf.data() + m.rm_so, m.rm_eo - m.rm_so);
    sim->rbuf.erase(0, m.rm_eo);
    regfree(&sim->expect_re); sim->expect_re_valid = 0;
    sim->expect_active = 0;
    return true;
}
```

### 5.5 `sim_resume`

```c
static void sim_resume(nclansim_t *sim)
{
    if (sim->coro_done) return;
    lua_State *co = lua_tothread_from_ref(sim->L, sim->coro_ref);
    int narg = 0;

    /* If we're resuming from a satisfied expect, push its return value (match
     * string or nil) so sim.expect's continuation can return it to the script. */
    if (sim->coro_started && !sim->expect_active && !sim->expect_re_valid &&
        (!sim->last_match.empty() || sim->got_eof || sim->expect_deadline)) {
        if (sim->last_match.empty()) lua_pushnil(co);
        else                         lua_pushlstring(co, sim->last_match.data(),
                                                         sim->last_match.size());
        sim->last_match.clear();
        sim->expect_deadline = 0;   /* consume the trigger */
        narg = 1;
    }

    int rc = lua_resume(co, sim->L, narg);
    if (rc == LUA_YIELD) { sim->coro_started = 1; return; }
    if (rc == LUA_OK)    { sim->coro_started = 1; sim->coro_done = 1; return; }
    /* LUA_ERRRUN */
    const char *msg = lua_tostring(co, -1);
    LOG_WARN("sim[neid=%d]: script error: %s",
             sim->wh->conn[sim->conn_id].neid, msg ? msg : "(no message)");
    sim->coro_done = 1;
}
```

`sim.expect`'s C body uses `lua_yieldk(L, 0, 0, k)` where `k` is a tiny continuation that returns `1` — the resumed value pushed by `sim_resume` becomes the script-side return of `sim.expect`.

---

## 6. Error Semantics & Edge Cases

| # | Failure | Detected by | Effect |
|---|---------|-------------|--------|
| 1 | Lua script file missing / syntax error | `luaL_loadfile` / `lua_pcall` returns nonzero | `nclansim_open` logs WARN, frees partial state, returns `NULL`. Test fails fast. |
| 2 | Script runtime error (`nil[1]`, etc.) | `lua_resume` returns `LUA_ERRRUN` | LOG_WARN with error; `coro_done = 1`. Further steps are no-ops. Test can still drive ncland. |
| 3 | `sim.expect` invalid regex | `regcomp` returns nonzero | `luaL_error` (script raises) — caught by #2. |
| 4 | `sim.expect` times out | `expect_deadline` reached in `sim_try_match` | Resumes with `nil`; script branches on it. Not a hard error. |
| 5 | ncland closes `sv[0]` mid-step | `read(sv[1])` returns 0 → `got_eof = 1` | If an `expect` is pending, `sim_try_match` resolves it with `nil` on the next step round; the script can branch and exit. If no expect is pending, the script's next `sim.expect` call resolves with `nil` immediately. `coro_done` is set only when the script subsequently returns (or raises). |
| 6 | `nclansim_step` runs unbounded (script loops without expect/return) | None — not enforced | YAGNI; tests use `nclansim_step_until(..., max_iters)` to cap. Inner-loop quiescence check bounds one `_step` call. |
| 7 | `conn_alloc` returns -1 (table full) | `nclansim_open` | Frees state, returns `NULL` with LOG_WARN. |
| 8 | dtype has no registry entry | `ncland_registry_find` returns NULL | LOG_WARN, `nclansim_open` returns `NULL` — sim can't drive login without a prompt regex. |
| 9 | `nclansim_close` on a sim mid-yield | — | Resumes with `nil` once (so script's `expect` returns and any `finally`-style code runs), then closes regardless. Bounded: single resume. |

### 6.1 Resource discipline

`nclansim_close` is the single funnel for sim teardown. It always:
1. Resumes the coroutine one final time with `nil` (if not already `coro_done`).
2. `regfree`s `expect_re` if still valid.
3. `luaL_unref` the coro_ref.
4. `lua_close` the sim's `lua_State`.
5. `close(sv[1])` (sv[0] is closed by `warehouse_drop_conn`).
6. `warehouse_drop_conn` the slot.
7. `free(sim)`.

Safe on a NULL sim. Safe to call twice (second call sees `L == NULL` and bails).

### 6.2 No autonomous behavior

The sim never advances on its own. The test must call `nclansim_step` (or `_step_until`) to make either side progress. This is the deliberate trade-off for determinism — no race against background threads, no need for locks.

### 6.3 Autonomous data from ncland

Bytes that arrive in `sim->rbuf` while no `expect` is active accumulate. The next `sim.expect` will see them. This matches the design of ncland's own `session:expect` semantics.

---

## 7. Testing the Sim

Two layers of tests live in a new file `ncland_sim_tests.cpp`.

### 7.1 Suite `sim` — self-tests (the sim is correct in isolation)

| Test | Asserts |
|------|---------|
| `S-sim sim.send delivers to ncland` | Script: `sim.send("Username: ")`. Step once. Read sv[0] (or read `c->rbuf` after `handle_ne_data` runs) and confirm `"Username: "` is there. |
| `S-sim sim.expect matches data ncland wrote` | Script: `sim.send("U:"); local u = sim.expect("admin\n", 5); sim.log("got "..u)`. Test injects `"admin\n"` to sv[0] via direct write. Step. Assert `nclansim_received_matches(sim, "admin")` returns 1 and the log line fired. |
| `S-sim sim.expect timeout returns nil` | Script: `local x = sim.expect("never", 1); assert(x == nil)`. Step (advance time by 2s, or use a synthetic time helper). Script must reach its assertion without raising. |
| `S-sim received history accumulates across steps` | Sim is passive (one expect that never matches). Test writes `"abc"`, steps, writes `"def"`, steps. `nclansim_received` returns `"abcdef"`. |
| `S-sim script return → coro_done; further steps are no-ops` | Script: `sim.send("hi")` and return. Step. Verify `"hi"` received. Step 3 more times; assert no crash, history unchanged, `coro_done == 1`. |

### 7.2 Suite `sim` — integration tests (drives ncland end-to-end)

Same file, also tagged `sim` for selection. These prove the sim is fit for testing #3b code.

| Test | Scenario |
|------|----------|
| `I-sim full login → command → response` | Sim plays a fake Cisco; test fixture configures a dtype + lua_module that does the matching login dialogue. After `nclansim_step_until(sim, CS_READY, 50)`, inject a CMD; step until conn returns to CS_READY; assert response text. |
| `I-sim post_command op:lua sees NE output` | Dtype has `post_command: [{op: lua, func: assert_foo}]`; sim returns `"foo\ndevice# "` to the command. Assert the post_command lua step ran and saw `"foo"` via `session:last_output()`. |
| `I-sim disconnect_steps fires on SIGTERM-equivalent` | After login, call `ncland_warehouse_begin_drain(wh)`. Step until conn is CS_IDLE. Assert `nclansim_received_matches(sim, "write memory")` (or whatever the dtype's disconnect lua sends). |
| `I-sim NE-side EOF drops the conn` | Script: `sim.send("hello"); return`. Step. `nclansim_close` (which closes sv[1]). Step ncland's `handle_ne_data` directly. Assert conn dropped (CS_IDLE). |

### 7.3 Coverage targets

Every `nclansim_close` exit path, every `sim_try_match` outcome (sync hit, async hit, timeout, EOF), and every state transition introduced by `nclansim_step` driving login + cmd + disconnect.

Projected suite delta: ≈9 new tests on top of the 141 baseline → ~150/0/6 after the sim ships.

---

## 8. Files Touched / Created

| File | Responsibility in #3c |
|------|-----------------------|
| `ncland_sim.h` (new) | Public C API: `nclansim_t`, six functions. |
| `ncland_sim.cpp` (new) | Implementation: socketpair setup, sim's lua_State + coroutine, the `sim.*` Lua functions, step algorithm, expect matching, error handling. |
| `ncland_sim_tests.cpp` (new) | 5 self-tests + 4 integration tests. |
| `ncland.mk` | Add `ncland_sim.o` and `ncland_sim_tests.o` to `ncland_unit_tests` prereqs. **Not** to `NCLAND_OBJS`. |

No existing files are modified beyond `ncland.mk`.

---

## 9. Open Questions / Deferred

- **Higher-level scripting helper** (e.g. `nclansim.lua` library with `default_sim(prompts, patterns)` that mirrors clansim.lua's familiar pattern) — deferred. The raw `sim.expect`/`sim.send` primitives are flexible enough for the initial use cases; a helper can be layered on top with no changes to the C core if a real pattern emerges.
- **Localhost TCP variant** — deferred. Useful if a future need to exercise ncland's `ncland_telnet_connect` path appears; the current socketpair design covers everything after the connect. The C API would gain `nclansim_open_tcp(...)` returning a `(sim, port)` pair; the rest of the API is unchanged.
- **Time mocking** — `sim.expect` timeouts use wall-clock `time(NULL)`. Tests that need millisecond control can `sleep(2)` to cross a 1s deadline; finer control requires injecting a time function, which is YAGNI today.
- **SSH transport simulation** — out of scope; ncland's SSH layer is below the post-connect dialogue the sim emulates.
- **Out-of-band injection from the test** — tests that want to inject bytes mid-script can write to `sv[1]` directly via the sim handle; no dedicated API.
