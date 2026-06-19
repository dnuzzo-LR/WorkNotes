# ncland #3b — Step Engine (post_command + disconnect_steps) Design

**Status:** design approved 2026-06-11. Implementation plan to follow.
**Predecessor:** `2026-06-09-ncland-stepper-3a-design.md` (command path config-driven).
**Master schema spec:** `2026-05-27-clan-yaml-design.md` §8 (Step Language), §9.5 (Lua entry points).
**Branch:** `ncland-start`.

---

## 1. Scope & Constraints

#3b makes ncland honor the YAML `post_command` and `disconnect_steps` blocks defined in master spec §8. Scope is deliberately minimal:

- **Only `op: lua` is implemented.** Master spec §8.1 defines eight ops (`expect`, `send`, `expect_send`, `branch`, `label`, `sleep`, `set`, `lua`). All current NE files use either an empty block or a single `op: lua` call (`alc_1850_tss5.yaml` → `graceful_close`; `generic_cisco_rtr.yaml` → `optional_writemem`). The other seven ops are deferred until a real NE author needs them; each can be added incrementally as a new case in `step_advance`.
- **`disconnect_steps` runs on SIGTERM/SIGHUP only.** No per-NE close command is added to the wire protocol. Conns in non-`CS_READY` states at signal time are left to finish naturally or time-out.
- **`post_command_hook` / `disconnect_hook` (§9.5) are not implemented.** Redundant under op-lua-only scope: a single `op: lua` step does the same job.
- **Errors inside a step do not redact `rsp.text`.** Client always receives the NE output that was collected at prompt-match time; step failure only flips `rsp.result` to `FAILURE`.
- **The conn stays alive on `post_command` failure.** Only a hard transport error (EOF, keepalive-dead, login-fail) drops the conn during a post_command run.

Out of scope for #3b: `op: expect`/`send`/`expect_send`/`set`/`branch`/`label`/`sleep`/`goto`; step-language regex pre-compile (no regexes are used by `op: lua`); `max_steps:` budget (loop ops don't exist yet); `op: branch_var` (deferred — master spec §14 Open Q 11).

---

## 2. Architecture

**One stepper, two trigger points.** A single C function `step_advance(c, wh)` drives both blocks. The two paths differ only in (a) what conn state is active while running and (b) what `step_finish` does on completion.

### 2.1 Trigger 1 — `post_command`

Spliced into `handle_ne_data` at the end of the existing `CS_CMD_PENDING` match block (post-#3a: text captured, `errors[]` scanned, `rsp.result` set). New fork point is right before `worker_send_rsp`:

- If `ne_entry.post_command` is an empty or absent sequence → ship rsp immediately, same as #3a.
- Otherwise: stash the rsp into a new conn field `pending_rsp`, transition `CS_CMD_PENDING → CS_POST_CMD`, set `step_block = STEP_BLOCK_POST_CMD`, `step_idx = 0`, `rbuf` cleared, then call `step_advance`. The captured rsp is delivered when `step_finish` runs (with `result` possibly downgraded to `FAILURE`).

### 2.2 Trigger 2 — `disconnect_steps` (SIGTERM-driven)

The signalfd handler for `SIGTERM`/`SIGHUP` no longer breaks the main loop directly. Instead:

1. Set `wh->shutting_down = time(NULL)` and `wh->shutdown_deadline = shutting_down + SHUTDOWN_GRACE_S` (`#define SHUTDOWN_GRACE_S 10`).
2. Call `warehouse_begin_drain(wh)`: for each `CS_READY` conn, transition to `CS_DISCONNECTING` and `step_advance`. Conns whose dtype has an empty `disconnect_steps` block are `warehouse_drop_conn`'d immediately (no state transition).
3. Conns in `CS_AUTHENTICATING`/`CS_CMD_PENDING`/`CS_KEEPALIVE`/`CS_POST_CMD` are **not** disturbed; their existing deadlines apply. A per-tick drain sweep picks them up once they reach `CS_READY` (or hard-closes them at the deadline).

The main loop continues running normally; its exit condition becomes:

```
shutdown_done = wh->shutting_down
             && (active_conn_count(wh) == 0 || now >= wh->shutdown_deadline)
```

At loop break, the existing `warehouse_shutdown` runs and hard-closes any leftover conns (logged as WARN with count).

### 2.3 New states

Added to `conn_state_t` after `CS_KEEPALIVE`:

```c
CS_POST_CMD       /**< Running post_command coroutine. */
CS_DISCONNECTING  /**< Running disconnect_steps coroutine. */
```

### 2.4 New module

`ncland_stepper.{h,cpp}` holds `step_advance`, `step_finish`, `warehouse_begin_drain`, and the `check_step_deadlines` sweep. The Lua-coroutine plumbing (`ncland_lua_step_start`, `ncland_lua_coroutine_drive`) lives in `ncland_lua.cpp` alongside the existing login bridge.

---

## 3. Registry Validation (load time)

The registry already stores `post_command` and `disconnect_steps` as raw `YAML::Node` in `ne_entry_t` (#3a). #3b adds a `ncland_registry_validate_steps(entry, lua_module_path, &err)` call from `ncland_registry_build`, after regex compiles, before return.

For each block (`post_command`, `disconnect_steps`):

1. Must be a YAML sequence (or absent). Scalar/map at block level → fail.
2. For each step:
   - Must be a YAML map.
   - Must contain exactly one of the known op keys. Today the set is `{ "lua" }`. Unknown key → fail with `"step[N] unknown op '<key>' (known: lua)"`.
   - For `op: lua`: sibling `func:` required, must be non-empty string. `args:` optional, must be a sequence if present.
3. Verify each referenced `func` exists in the NE's Lua module. Done via a new helper `ncland_lua_module_has_func(wh, lua_module_path, func_name)` that uses the existing module cache: opens the module, `lua_getfield(L, -1, func)`, checks `lua_isfunction`.

**Failure semantics.** Same as existing regex-compile failures: WARN log with file path + step index + reason; the NE type is skipped (`by_dtype` does not get the entry). Daemon stays up; other NE types load.

**Storage.** Blocks remain `YAML::Node`. No tagged-union vector. Bytes-per-step cost is negligible at one op type; the struct is the deliberate punt to add later if/when other ops land in bulk.

---

## 4. Per-conn Stepper

### 4.1 New `conn_t` fields

`conn_t` is POD (memset by `conn_init`, memcpy by `warehouse_install_conn`). All new fields are trivially copyable:

```c
typedef enum {
    STEP_BLOCK_NONE        = 0,
    STEP_BLOCK_POST_CMD    = 1,
    STEP_BLOCK_DISCONNECT  = 2,
} step_block_t;

step_block_t       step_block;        /**< Which YAML block is executing, or NONE. */
int                step_idx;          /**< Index of the current step within the block. */
int                step_failed;       /**< 1 if any step raised/timed out — propagates to rsp.result. */
time_t             step_deadline;     /**< Wall-clock deadline for the current step; 0 if none. */
ncland_rsp_msg_t   pending_rsp;       /**< Cmd-rsp captured at prompt-match, deferred until post_command finishes. */
```

`pending_rsp` is ~3 KB (full `ncland_rsp_msg_t`); per-conn cost is acceptable at `MAX_CONNS`.

### 4.2 State machine — post_command

```
CS_CMD_PENDING (prompt matched in handle_ne_data; rsp built per #3a)
        ├── post_command empty → worker_send_rsp → CS_READY              (unchanged from #3a)
        └── post_command non-empty
                  stash rsp into c->pending_rsp
                  set step_block=POST_CMD, step_idx=0, step_failed=0
                  clear rbuf/rlen
                  → CS_POST_CMD
                  → step_advance(c, wh)
                          ├── coroutine LUA_OK    → step_idx++ → step_advance
                          ├── coroutine LUA_YIELD → return; resume on EPOLLIN
                          ├── coroutine LUA_ERR   → step_failed=1 → step_finish
                          └── step_idx == block.size() → step_finish
                                  if step_failed: rsp.result = FAILURE
                                  worker_send_rsp(wh, &c->pending_rsp)
                                  clear stepper fields, release coroutine
                                  → CS_READY
```

### 4.3 State machine — disconnect

```
CS_READY (warehouse_begin_drain picks up this conn)
        ├── disconnect_steps empty → warehouse_drop_conn                 (no state transition)
        └── disconnect_steps non-empty
                  set step_block=DISCONNECT, step_idx=0, step_failed=0
                  clear rbuf/rlen
                  → CS_DISCONNECTING
                  → step_advance
                          ├── …same coroutine cases as above…
                          └── step_idx == block.size() → step_finish
                                  if step_failed: WARN log
                                  warehouse_drop_conn(wh, c)             (always)
```

### 4.4 Coroutine reuse

The existing login bridge (`ncland_lua.cpp`) already drives a Lua coroutine via `lua_resume`, with `session:expect()` yielding through `lua_yieldk`, and the main loop resuming on `EPOLLIN`. The bridge is split into two pieces:

- **Extract** `ncland_lua_coroutine_drive(c, wh)` from `resume_login`. Does the actual `lua_resume` and interprets `LUA_OK`/`LUA_YIELD`/`LUA_ERRRUN`. The completion path branches on `c->state` (`CS_AUTHENTICATING` → login completion; `CS_POST_CMD`/`CS_DISCONNECTING` → `step_idx++ → step_advance`).
- **Add** `ncland_lua_step_start(c, wh, func_name, args_node)` mirroring `ncland_login_start`: looks up `M[func]` in the per-conn lua module thread, pushes session userdata + global table + args (YAML list → Lua table, 1-indexed; absent → `nil`), creates the coroutine, `lua_resume`s. Returns `NCLAND_LUA_OK` / `NCLAND_LUA_YIELDED` / `NCLAND_LUA_FAILED`.

`handle_ne_data` already has a `CS_AUTHENTICATING` branch that calls `resume_login` on incoming data. #3b adds the parallel branch:

```c
if (c->state == CS_POST_CMD || c->state == CS_DISCONNECTING) {
    ncland_lua_coroutine_drive(c, wh);  /* completion calls step_advance/step_finish */
    return;
}
```

The `read(2)` at the top of `handle_ne_data` is unchanged — same buffer feeds both login and step `session:expect()` paths via `session:last_output()`.

### 4.5 Per-step timeout

Each `op: lua` step gets a default deadline `step_deadline = now + STEP_DEFAULT_TMOUT_S` (`#define STEP_DEFAULT_TMOUT_S 60`). `next_timeout_ms` extends to include `step_deadline` for `CS_POST_CMD`/`CS_DISCONNECTING`. A new main-loop sweep `check_step_deadlines(wh)` marks `step_failed=1` and calls `step_finish` for any conn past its `step_deadline`. The orphaned Lua coroutine is unref'd by `step_finish` via the existing `ncland_lua_release_thread(wh, c)` helper.

(`STEP_DEFAULT_TMOUT_S` is a single per-step bound, not a block-total bound. A 3-step block can theoretically run up to 180s; in practice steps are quick. Configurable via step args in a future revision if needed.)

### 4.6 `step_advance` pseudo-code

```c
void step_advance(conn_t *c, ncland_wh_t *wh)
{
    const ne_entry_t *e = ncland_registry_find(&wh->registry, c->dtype);
    if (!e) { c->step_failed = 1; step_finish(c, wh); return; }

    const YAML::Node &block = (c->step_block == STEP_BLOCK_POST_CMD)
                              ? e->post_command : e->disconnect_steps;

    while (c->step_idx < (int)block.size()) {
        const YAML::Node &step = block[c->step_idx];
        /* Validation at load (§3) guarantees exactly one known op. */
        if (step["op"].as<std::string>() == "lua") {
            std::string func = step["func"].as<std::string>();
            const YAML::Node &args = step["args"];   /* nil-safe in step_start */
            int rc = ncland_lua_step_start(c, wh, func.c_str(), args);
            if (rc == NCLAND_LUA_YIELDED) {
                c->step_deadline = time(NULL) + STEP_DEFAULT_TMOUT_S;
                return;   /* wait for EPOLLIN → coroutine_drive re-enters step_advance on LUA_OK */
            }
            if (rc == NCLAND_LUA_FAILED) {
                c->step_failed = 1;
                LOG_WARN("conn[%d] neid=%d: step[%d] op:lua func='%s' failed",
                         c->id, c->neid, c->step_idx, func.c_str());
                break;   /* abort block; jump to finish */
            }
            /* NCLAND_LUA_OK — synchronous completion, advance immediately */
        }
        c->step_idx++;
    }
    step_finish(c, wh);
}
```

### 4.7 `step_finish` pseudo-code

```c
static void step_finish(conn_t *c, ncland_wh_t *wh)
{
    if (c->step_block == STEP_BLOCK_NONE) return;   /* idempotent — see §6.2 */
    step_block_t which = c->step_block;
    c->step_block = STEP_BLOCK_NONE;
    c->step_idx = 0;
    c->step_deadline = 0;
    ncland_lua_release_thread(wh, c);

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
```

---

## 5. SIGTERM-driven Disconnect Flow

### 5.1 New `ncland_wh_t` fields

```c
time_t  shutting_down;     /**< 0 = running; nonzero = SIGTERM timestamp; refuse new cmds, drain conns. */
time_t  shutdown_deadline; /**< shutting_down + SHUTDOWN_GRACE_S; main loop breaks at or past this. */
```

### 5.2 Signal handler

In the existing signalfd handler for SIGTERM/SIGHUP:

```c
case SIGTERM:
case SIGHUP:
    if (wh->shutting_down) break;     /* idempotent — second signal still waits the grace */
    wh->shutting_down     = time(NULL);
    wh->shutdown_deadline = wh->shutting_down + SHUTDOWN_GRACE_S;
    LOG_INFO("signal: graceful shutdown initiated (grace=%ds)", SHUTDOWN_GRACE_S);
    warehouse_begin_drain(wh);
    break;
```

### 5.3 `warehouse_begin_drain`

```c
/* Returns 1 if the conn entered CS_DISCONNECTING, 0 if it was hard-dropped immediately. */
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
    step_advance(c, wh);
    return 1;
}

static void warehouse_begin_drain(ncland_wh_t *wh)
{
    int kicked = 0, dropped = 0;
    for (int i = 0; i < MAX_CONNS; i++) {
        conn_t *c = &wh->conn[i];
        if (c->state != CS_READY) continue;  /* mid-cmd/mid-login: let in-flight finish */
        if (drain_one_ready_conn(wh, c)) kicked++; else dropped++;
    }
    LOG_INFO("drain: %d conn(s) running disconnect_steps; %d dropped immediately", kicked, dropped);
}
```

### 5.4 Per-tick drain sweep

Each main-loop tick, after the existing keepalive/expired-cmd sweeps:

```c
if (wh->shutting_down) {
    for (int i = 0; i < MAX_CONNS; i++) {
        conn_t *c = &wh->conn[i];
        if (c->state == CS_READY) drain_one_ready_conn(wh, c);
    }
}
```

This picks up conns that just finished an in-flight op and transitioned to `CS_READY`.

### 5.5 New-command refusal

`warehouse_handle_cmd_from_zmq` adds an early-reject:

```c
if (wh->shutting_down) {
    wh_reply_now(wh, identity, identity_len, cmd, NCLAND_RESULT_FAILURE,
                 "ncland shutting down");
    return;
}
```

### 5.6 Loop exit & hard-close

```c
bool shutdown_done = wh->shutting_down
                  && (warehouse_active_conn_count(wh) == 0
                      || time(NULL) >= wh->shutdown_deadline);
if (shutdown_done) break;
```

After the break, the existing `warehouse_shutdown` runs. Any conn still in `CS_DISCONNECTING`/`CS_POST_CMD`/`CS_AUTHENTICATING`/`CS_CMD_PENDING` past the deadline is hard-closed via the existing teardown paths (SSH/telnet disconnect + conn_free). A WARN logs the count:

```c
int leftover = warehouse_active_conn_count(wh);
if (leftover > 0) LOG_WARN("shutdown: hard-closing %d conn(s) past grace", leftover);
```

### 5.7 `next_timeout_ms` during shutdown

Cap the computed wait at `(shutdown_deadline - now) * 1000` so the loop wakes up at the deadline even if nothing else is scheduled.

---

## 6. Error Handling & Edge Cases

| # | Failure | Detected by | Effect on conn | Effect on client rsp |
|---|---------|-------------|----------------|----------------------|
| 1 | YAML step missing `op:` / unknown op | Load-time validation (§3) | NE type not registered; daemon stays up | N/A (rejected at load) |
| 2 | `op: lua` references nonexistent `func` | Load-time validation (§3) | NE type not registered | N/A |
| 3 | Step lua raises (`error()`/`session:fail()`/runtime) | `lua_resume` returns `LUA_ERRRUN` in `coroutine_drive` | `step_failed=1` → `step_finish` → CS_READY (post_cmd) or `drop_conn` (disconnect) | post_cmd: `result=FAILURE`, NE text preserved. disconnect: WARN log, conn dropped anyway. |
| 4 | Step lua coroutine times out (no `expect` match within `STEP_DEFAULT_TMOUT_S`) | `check_step_deadlines` sweep | Same as #3 | Same as #3 |
| 5 | NE disconnects (EOF on net_fd) mid-step | `handle_ne_data` read returns 0 | `warehouse_drop_conn` (existing path) — bypasses `step_finish` | post_cmd: client gets `result=FAILURE` text `"NE disconnected mid-post_command"` via new `step_abort_for_eof(c, wh)` called from the EOF branch. disconnect: no client to notify. |
| 6 | Registry lookup fails (entry unloaded mid-flight) | `step_advance` `if (!e)` | `step_failed=1` → `step_finish` | Same as #3 |
| 7 | Multi-step: step N fails | `step_advance` loop `break` on `NCLAND_LUA_FAILED` | Remaining steps skipped — block aborts | post_cmd: `result=FAILURE`; partial side-effects from earlier steps already applied to NE |
| 8 | `disconnect_steps` runs past `shutdown_deadline` | Loop exit condition (§5.6) | `warehouse_shutdown` hard-closes the conn | N/A |
| 9 | New client cmd arrives during shutdown | `warehouse_handle_cmd_from_zmq` early-reject (§5.5) | Conn unaffected | Immediate `result=FAILURE`, text `"ncland shutting down"` |
| 10 | Second SIGTERM during drain | `shutting_down` already set; signalfd handler no-ops | Conns continue draining | N/A — operator must `SIGKILL` to short-circuit |

### 6.1 Resource discipline

`step_finish` is the single funnel for all step-block exits (success, lua-failure, timeout, registry-miss). It always:

1. Clears `c->step_block`/`step_idx`/`step_deadline`/`step_failed`.
2. Zeros `c->pending_rsp`.
3. Clears `c->rbuf`/`rlen`.
4. Releases the per-conn Lua coroutine via `ncland_lua_release_thread(wh, c)`.

### 6.2 Idempotency

`step_advance` is safe to call when `step_idx >= block.size()` (falls through to `step_finish`). `step_finish` is safe to call twice (subsequent calls see `step_block == STEP_BLOCK_NONE` and return early) — necessary because the EOF path and step-timeout path can race.

### 6.3 Autonomous NE data during `CS_POST_CMD`

Unsolicited bytes from the NE (alarms, banners) arrive in `c->rbuf` while a step is running and feed into `session:last_output()`. If they happen to satisfy a step's `session:expect()`, that is a false-positive consumption — the step author's responsibility, identical to the existing login constraint. Documented here, not engineered around.

### 6.4 No partial response on failure

If post_command step 3 of 4 fails, the client still receives the full NE output (captured at prompt-match time into `pending_rsp.text`). Failure does not redact; only `rsp.result` flips. This matches §8.3's "additional processing" framing — post_command analyzes, it doesn't censor.

---

## 7. Testing Strategy

Baseline: 108 passed / 0 failed / 6 skipped. Projected after #3b: ≈125 passed / 0 failed / 6 skipped.

### 7.1 Test seams

```c
int g_test_last_step_block_finished;   /* enum step_block_t cast to int; -1 if none yet */
int g_test_last_step_failed;            /* 0/1 */
int g_test_active_conn_count_at_break;  /* set by main loop break in shutdown */
```

`step_finish` sets the first two; the shutdown loop-exit sets the third. No production behavior depends on them.

### 7.2 Suite `registry` — load-time validation

| Test | Input | Assertion |
|------|-------|-----------|
| R-3b post_command op:lua compiles when func exists | one `op: lua, func: graceful_close` against module defining it | `registry_build` returns 0; entry stored |
| R-3b unknown op rejected | `op: bogus` | nonzero return; error contains `"unknown op"` + step index |
| R-3b op:lua missing func rejected | `op: lua` with no `func:` | nonzero return; error contains `"func"` |
| R-3b op:lua func not in module rejected | `op: lua, func: nonexistent_fn` | nonzero return; error contains `"no such function"` + module path |
| R-3b block must be sequence | `post_command: foo` (scalar) | nonzero return |
| R-3b empty block accepted | `post_command: []` | returns 0; entry stored, block size 0 |

### 7.3 Suite `stepper` — post_command execution

| Test | Assertion |
|------|-----------|
| S-3b empty post_command path unchanged | rsp ships immediately; CS_READY; `g_test_last_step_block_finished == -1` |
| S-3b single op:lua step runs to completion | step ran; rsp shipped; CS_READY; `g_test_last_step_failed == 0` |
| S-3b op:lua with session:expect that matches | yields, EPOLLIN drives resume, matches, completes; rsp shipped |
| S-3b op:lua raising error → FAILURE | `step_failed == 1`; rsp shipped with `result == FAILURE`; original NE text preserved |
| S-3b op:lua timeout → FAILURE | deadline fires via `check_step_deadlines`; FAILURE; CS_READY |
| S-3b multi-step: step 2 fails, step 3 skipped | counter shows 2 attempts; rsp FAILURE; CS_READY |
| S-3b post_command preserves rsp.text from prompt match | `rsp.text` holds pre-prompt output, not step's last_output |
| S-3b NE EOF mid-post_command → FAILURE | client gets FAILURE rsp mentioning EOF; conn dropped |
| S-3b op:lua args list passed as Lua table | step `args: [foo, 42]` → Lua `args[1]=="foo"`, `args[2]==42` |

### 7.4 Suite `stepper` — disconnect-block specifics

| Test | Assertion |
|------|-----------|
| S-3b disconnect_steps runs on CS_READY drain | step ran; `g_test_last_step_block_finished == DISCONNECT`; conn CS_IDLE |
| S-3b disconnect_steps failure still drops conn | conn dropped; WARN logged; no client side-effects |
| S-3b empty disconnect_steps hard-closes immediately | conn CS_IDLE without entering CS_DISCONNECTING |
| S-3b disconnect skipped for non-READY conns | conn in CS_CMD_PENDING untouched by `begin_drain` |

### 7.5 Suite `warehouse` — shutdown integration

| Test | Assertion |
|------|-----------|
| W-3b shutting_down rejects new cmds | `g_test_last_direct_rsp_result == FAILURE` with text `"shutting down"` |
| W-3b drain sweep picks up newly-READY conn | conn transitions to CS_DISCONNECTING on next tick |
| W-3b shutdown deadline forces loop exit | loop break fires; `g_test_active_conn_count_at_break == 1`; WARN about hard-close |

### 7.6 Manual / live test

Tracked as a step in the implementation plan, not the unit suite. Deploy `ncland` + an `alc_1850_tss5.lua::graceful_close` that returns quickly. With a TSS5 conn open: `nclan-cmd -n <neid> 'rtrv-eqpt'` → confirm normal rsp. `systemctl stop ncland` → confirm log shows drain begin, `graceful_close` invoked, conn dropped, daemon exits inside 10s.

### 7.7 Coverage targets

- Every `step_finish` exit path (success, lua-failure, lua-timeout, EOF, registry-miss).
- Every state transition added by #3b (CS_CMD_PENDING→CS_POST_CMD→CS_READY; CS_READY→CS_DISCONNECTING→CS_IDLE).
- Both validation rejection cases at load.

---

## 8. Files Touched

| File | Responsibility in #3b |
|------|----------------------|
| `ncland.h` | `step_block_t` enum; 5 new `conn_t` fields; `CS_POST_CMD` / `CS_DISCONNECTING`; `SHUTDOWN_GRACE_S` / `STEP_DEFAULT_TMOUT_S`; `wh->shutting_down` / `shutdown_deadline`; decls for `step_advance` / `step_finish` / `warehouse_begin_drain`. |
| `ncland_stepper.h` / `.cpp` (new) | `step_advance`, `step_finish`, `step_abort_for_eof`, `drain_one_ready_conn`, `warehouse_begin_drain`, `check_step_deadlines`. |
| `ncland_registry.cpp` | `ncland_registry_validate_steps` called from `ncland_registry_build`. |
| `ncland_lua.h` / `.cpp` | Extract `ncland_lua_coroutine_drive` from `resume_login`; add `ncland_lua_step_start`, `ncland_lua_module_has_func`, `ncland_lua_release_thread` (the last may already exist — verify). |
| `ncland_worker.cpp` | `handle_ne_data`: splice post_command fork before `worker_send_rsp`; add `CS_POST_CMD` / `CS_DISCONNECTING` dispatch branch; EOF branch calls `step_abort_for_eof` when in those states; `next_timeout_ms` accounts for `step_deadline` and `shutdown_deadline`. |
| `ncland_warehouse.cpp` | Signalfd handler kicks off drain instead of breaking; main loop adds drain sweep + new exit condition; `warehouse_handle_cmd_from_zmq` shutdown-reject; `warehouse_active_conn_count`. |
| `ncland_stepper_tests.cpp` (new) / `ncland_registry_tests.cpp` / `ncland_warehouse_tests.cpp` | Unit tests per §7. |
| `ncland.mk` | Add `ncland_stepper.o` to daemon + unit-test targets; add `ncland_stepper_tests.o` to unit-test prerequisites. |

---

## 9. Open Questions / Deferred

- `op: expect`/`send`/`expect_send`/`set`/`branch`/`label`/`sleep` — deferred until a real NE needs them. Each adds one case in `step_advance`.
- `max_steps:` budget (master spec §8.2) — not needed yet; only `op: lua` exists and it cannot loop.
- `op: branch_var` (master spec §14 Open Q 11 / GAPS Gap 3) — remains deferred.
- Per-step custom timeout via `timeout_s:` step arg — out of scope; revisit if 60s default proves wrong.
- `post_command_hook` / `disconnect_hook` (master spec §9.5) — dropped under op-lua-only scope.
- Per-NE close command on the wire — out of scope; SIGTERM is the only graceful-close trigger.
