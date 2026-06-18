# ncland #3a — Command Path Made Config-Driven (Design)

**Date:** 2026-06-09
**Status:** Approved (design); ready for implementation plan
**Component:** `cnc/ncland/src`
**Builds on:** the completed #2 Lua login bridge (commit `c9334d4e8`) — sessions
reach `CS_READY`; the session API, shared `global`, per-conn coroutine engine,
and YAML prompt regexes are in place. Companion to
`2026-05-27-clan-yaml-design.md` (the step language in its §8 is **deferred to
#3b**, not this spec).

---

## 1. Goal & Scope

Make the existing steady-state command path honor each NE's YAML configuration
and return a clear pass/fail result, and add a tool to drive commands so the
path is testable against real NEs.

**In scope (#3a):**
1. Registry-driven **paging** (`more_pattern` / `more_response`) — replaces the
   hardcoded `--More--` handling.
2. Registry-driven **keepalive** (`ka_enabled` / `ka_interval_s` / `ka_command`
   / `ka_expect_response`) with a liveness teardown.
3. **Apply `adapted_primary_regex`** (which `login()` publishes) to the conn's
   live command-prompt matching.
4. **Error-pattern → SUCCESS/FAILURE** result for the client, **opt-out per
   request**.
5. **`nclan-cmd`** — a CLI sender that drives a command to an NE by neid and
   prints the response.

**Out of scope (→ #3b):** the step-language interpreter (`post_command`,
`disconnect_steps`, the `lua` op, `post_command_hook`/`disconnect_hook`),
`last_command`, and `inter_command_ms` multi-command sequencing.

---

## 2. Registry: Compile Per-Type Regexes Once

Paging and error patterns are identical for every connection of a given dtype,
so they are compiled **into the registry entry** (`ne_entry_t`, which already
holds the compiled `prmpt1re`/`prmpt2re`), not per-conn. Add to `ne_entry_t`:

```cpp
regex_t              more_re;        /* compiled more_pattern */
bool                 more_valid = false;
std::vector<regex_t> error_res;      /* compiled errors[] */
```

- Compiled in `ncland_registry_build`, after required-key validation, from
  `more_pattern` (skipped when empty) and each `errors[]` string.
- Freed at registry teardown (a small `ncland_registry_free`/clear that
  `regfree`s `prmpt1re/2re/more_re/error_res` for every entry; wire it into
  `ncland_lua_shutdown`'s sibling teardown or `warehouse_shutdown`).
- **Copy hazard:** a `regcomp`'d `regex_t` must not be value-copied after
  compilation (the copy shares heap internals → double `regfree`). Compile into
  the entry's final slot in `by_dtype` (after the entry is inserted/merged), the
  same discipline the existing `prmpt1re` compilation already follows. The build
  must not copy an entry after its regexes are compiled.
- Runtime lookup is `ncland_registry_find(&wh->registry, c->dtype)`.

---

## 3. Paging — `handle_ne_data` (replaces hardcoded `--More--`)

On each NE read, before prompt-matching, drain paging automatically (spec §8.3):

- Look up the entry by `c->dtype`. If `entry && entry->more_valid` and
  `regexec(&entry->more_re, rbuf+scan, ...)` matches:
  - write `entry->more_response` to the NE,
  - strip the matched token from `rbuf` (so it doesn't pollute the response),
  - return (keep reading; do not prompt-match this pass).
- Empty `more_pattern` → `more_valid == false` → no paging (the PSS case;
  paging is disabled at login).

This generalizes the current single hardcoded `--More--` rule to any vendor
pattern (Cisco `--More--`, Ciena `] (q,g,space,enter)`, etc.) without code
changes.

---

## 4. Apply `adapted_primary_regex` at `CS_READY`

`login()` may publish `adapted_primary_regex` (a regex string derived from the
live prompt — see CIENA_RLS / 1830 PSS `.lua`). When the login coroutine
finishes (`resume_login`, `LUA_OK`, in `ncland_lua.cpp`), before transitioning
to `CS_READY`:

- read the session var `adapted_primary_regex`;
- if set and non-empty, `regfree` the conn's current `prmpt1re` (if valid) and
  `regcomp` the adapted string into `c->prmpt1re` (`REG_EXTENDED`),
  `prmpt1re_valid = 1`;
- leave `c->prmpt2re` as the YAML `secondary_regex` (fallback).

So command prompt-matching uses the **actual** device prompt. This is essential
for NEs whose real prompt the generic YAML `primary_regex` would not match; it
is a no-op when `login()` published nothing.

---

## 5. Error → SUCCESS/FAILURE Result (opt-out per request)

The legacy `errors[]` mechanism gives the client a simple pass/fail verdict on a
command. ncland reproduces this, **gated per request** so a client that just
wants to view output is never told "FAILURE" merely because the output contains
an error-looking pattern.

- Add a per-request flag to `ncland_cmd_msg_t`: `int check_errors;` (default 1).
- Add a result field to `ncland_rsp_msg_t`: `int result;` with
  `NCLAND_RESULT_SUCCESS = 0`, `NCLAND_RESULT_FAILURE = 1`.
- On prompt-match in `handle_ne_data`, when `c->check_errors` is set: scan the
  full response against `entry->error_res`; if any matches → `result = FAILURE`
  and `LOG_WARN("neid=%d cmd failed: matched error pattern", ...)`; else
  `result = SUCCESS`.
- When `check_errors` is **off**: skip the scan; `result = SUCCESS` ("not
  evaluated").
- Either way the full NE text is returned in `rsp.text`.

`c->check_errors` is copied from the command msg in `dispatch_cmd` (a POD int on
`conn_t`).

---

## 6. Keepalive — `check_keepalives` (config-driven + liveness)

Replaces the hardcoded `"\n"` / fixed interval. For each `CS_READY` conn whose
dtype entry has `ka_enabled`:

- When `now >= c->next_keepalive`: write `entry->ka_command`; set
  `c->next_keepalive = now + entry->ka_interval_s`.
- If `entry->ka_expect_response`:
  - transition the conn to a new state `CS_KEEPALIVE` and set
    `c->ka_deadline = now + KA_RESPONSE_TMOUT_S` (a bounded wait; default 30 s);
  - the echo arrives via `EPOLLIN` → `handle_ne_data` handles `CS_KEEPALIVE`:
    drain paging as usual, and on prompt-match **clear the buffer and return to
    `CS_READY`** with **no caller response** (keepalive has no `seq`);
  - if `c->ka_deadline` passes with no prompt (the timer sweep), the link is
    dead → **teardown** the conn (`warehouse_drop_conn`).
- If `!ka_expect_response`: fire-and-forget — stay `CS_READY`, send nothing
  further. Any incidental echo is harmless: it accumulates in `rbuf` but the
  next `dispatch_cmd` clears `rbuf` before issuing a command.
- `ka_enabled == false` → no keepalive traffic at all (the PSS case).

New POD `conn_t` fields: `time_t ka_deadline;`. New state `CS_KEEPALIVE` in
`conn_state_t`. `next_timeout_ms` accounts for `ka_deadline` (when
`CS_KEEPALIVE`) and `next_keepalive` (when `CS_READY` and `ka_enabled`).

---

## 7. Addressing & the `nclan-cmd` Sender

Callers know **neids, not conn slots**.

- Add `int neid;` to `ncland_cmd_msg_t`. `warehouse_handle_cmd_from_zmq`
  resolves it to a live conn via `find_conn_by_neid` (promote/whatever the
  notify path already uses), then `dispatch_cmd` to that conn.
- If no conn matches, or the conn is not `CS_READY` (e.g. still connecting /
  logging in / busy), reply immediately with `result = FAILURE` and an
  explanatory `text` ("no ready session for neid N" / "neid N busy").

New tool **`cnc/ncland/src/nclan_cmd.cpp`** (+ `ncland.mk` target `nclan-cmd`):
- ZMQ `DEALER` connected to ncland's ctl ROUTER endpoint (default
  `ipc:///usr/cnc/lib/data/ncland/ctl.sock`; `-c <endpoint>` to override).
- Flags: `-n <neid>` (required), `-r`/`--raw` (send `check_errors=0`),
  `-t <secs>` (command timeout, default 30), `-c <endpoint>`.
- Sends `CLAN2_MSG_CMD{ neid, data=<argv tail>, tmout, check_errors }`, waits
  for the response, prints the `text`, then a final line `RESULT: SUCCESS` or
  `RESULT: FAILURE` (suppressed under `-r`). Exit code 0 on SUCCESS, 1 on
  FAILURE/error.
- Links like the other ncland tools (`-lzmq`, the proto objects); reuses
  `ncland_proto.*` framing so client and daemon stay in lockstep.

Usage:
```
nclan-cmd -n 518 'show shelf'        # judged pass/fail
nclan-cmd -n 518 -r 'show shelf'     # raw output, no verdict
```

---

## 8. Command Lifecycle & Edge Cases

- **One outstanding command per conn.** A `CLAN2_MSG_CMD` for a conn already in
  `CS_CMD_PENDING` or `CS_KEEPALIVE` replies immediately `FAILURE` ("neid N
  busy"). Queuing multiple commands is deferred. A keepalive in flight
  (`CS_KEEPALIVE`) similarly blocks a command until it drains or times out.
- **Command timeout** is already handled by `check_expired_cmds` (DENY on
  `cmd_deadline`); that DENY response now also carries `result = FAILURE`.
- **`dispatch_cmd`** clears `rbuf`/`rlen` before writing (already does), copies
  `check_errors` from the msg, arms `cmd_deadline`, sets `CS_CMD_PENDING`.
- **Buffer hygiene:** `handle_ne_data` continues to cap `rbuf` at
  `CLAN2_MAX_RBUF`; the paging strip keeps `rlen` consistent.

---

## 9. Testing

Socketpair unit tests (no real NE), suite `"worker"`/`"stepper"`:

1. **Paging:** entry with `more_pattern='--More--'`, `more_response=' '`; feed
   `"line\n--More--"`, assert `' '` written to the peer and the token stripped;
   then feed the prompt, assert one response with the paged content joined.
2. **Paging disabled:** empty `more_pattern`; feed output + prompt; assert no
   more_response written, response returned once.
3. **Error→FAILURE:** entry `errors=['ERROR','% Invalid']`; command with
   `check_errors=1`; response containing `"% Invalid input"` → `result ==
   FAILURE`. Clean response → `SUCCESS`.
4. **Error opt-out:** same error-containing response with `check_errors=0` →
   `result == SUCCESS`, scan skipped.
5. **Adapted prompt:** set session var `adapted_primary_regex='node-9[*^]*#'`,
   drive login→`CS_READY`, assert `c->prmpt1re` recompiled and a response ending
   in `node-9#` matches; a response matching only the old generic regex behaves
   per the new pattern.
6. **Keepalive send + drain:** `ka_enabled`, `ka_command='\n'`,
   `ka_expect_response=true`; advance `next_keepalive`; run `check_keepalives` →
   assert `ka_command` written and state `CS_KEEPALIVE`; feed the prompt →
   `handle_ne_data` returns conn to `CS_READY` with no caller response and a
   cleared buffer.
7. **Keepalive liveness:** as (6) but never feed a prompt; advance past
   `ka_deadline`; the sweep tears the conn down (`CS_IDLE`, `nconns--`).
8. **Keepalive disabled:** `ka_enabled=false`; `check_keepalives` writes nothing
   and leaves state `CS_READY`.
9. **Addressing:** `warehouse_handle_cmd_from_zmq` for an unknown neid → an
   immediate `FAILURE` response; for a non-`CS_READY` neid → `FAILURE` ("busy").
10. **`nclan-cmd`:** arg-parsing unit (neid required; `-r` sets `check_errors=0`;
    timeout/endpoint parsed). Full round-trip is the live PSS test (§10).

The existing suite stays green. `g_warehouse_no_connect` conns are unaffected
(they never enter `CS_CMD_PENDING`/`CS_KEEPALIVE`).

---

## 10. Live Test (the payoff)

Redeploy `ncland` + `nclan-cmd`. With a logged-in PSS (neid 518):
```
nclan-cmd -n 518 -r 'show shelf'
```
Expect in the daemon log: the command written, prompt matched (paging is off for
the PSS), response captured; and `nclan-cmd` prints the NE output. Then a
judged command (`nclan-cmd -n 518 'show shelf'`) prints `RESULT: SUCCESS`.

---

## 11. Files

| File | Change |
|------|--------|
| `ncland_registry.h` / `.cpp` | `ne_entry_t`: add `more_re`/`more_valid`/`error_res`; compile in `ncland_registry_build`; add a registry-free that `regfree`s all compiled regexes. |
| `ncland.h` | `ncland_cmd_msg_t`: add `int neid; int check_errors;`. `ncland_rsp_msg_t`: add `int result;` + `NCLAND_RESULT_*`. `conn_t`: add `int check_errors; time_t ka_deadline;`. `conn_state_t`: add `CS_KEEPALIVE`. Decls for any promoted helpers (`find_conn_by_neid`). |
| `ncland_worker.cpp` | `handle_ne_data`: registry paging, `CS_KEEPALIVE` drain, error scan→`result`. `dispatch_cmd`: copy `check_errors`. `check_keepalives`: registry-driven + `CS_KEEPALIVE`/liveness. `check_expired_cmds`: set `result=FAILURE`. `next_timeout_ms`: `ka_deadline`. |
| `ncland_lua.cpp` | `resume_login` (LUA_OK): apply `adapted_primary_regex` to `prmpt1re`. |
| `ncland_warehouse.cpp` | `warehouse_handle_cmd_from_zmq`: neid→conn resolution + not-ready/busy `FAILURE`; `CS_KEEPALIVE` liveness teardown in the timer sweep. |
| `ncland_proto.{h,cpp}` | (de)serialize the new `neid`/`check_errors`/`result` fields. |
| `nclan_cmd.cpp` | **new** CLI sender. |
| `ncland.mk` | add `nclan-cmd` target; `nclan_cmd.o`. |
| `*_tests.cpp` | the §9 unit tests. |

---

## 12. Reconciliation note

This implements the runtime half of the older spec's §7.1 (paging), §7.4
(keepalive), and §7.6 (errors) and the `adapt_from_actual` apply promised by
§7.7 — none of which the daemon consumed before. The step language (§8) and Lua
hooks (§9.5) remain for #3b.
