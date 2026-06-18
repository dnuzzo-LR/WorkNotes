# ncland #3a — Command Path Made Config-Driven Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make ncland's steady-state command path honor each NE's YAML (paging, keepalive, adapted prompt, error→pass/fail) and add `nclan-cmd` to drive a command to an NE by neid and print the response.

**Architecture:** Per-NE-type patterns (`more_pattern`, `errors[]`) compile once into the registry entry (`ne_entry_t`, beside the existing `prmpt1re/2re`); the adapted prompt stays per-conn. `handle_ne_data` drains paging from `more_re`/`more_response`, scans responses against `error_res` to set a SUCCESS/FAILURE `result` (opt-out per request via `check_errors`), and handles a new `CS_KEEPALIVE` drain state. `check_keepalives` becomes `ka_*`-driven with a liveness teardown. Commands are addressed by neid (ncland maps neid→conn). `nclan-cmd` is a ZMQ DEALER speaking the existing raw-struct ctl protocol.

**Tech Stack:** C++17 via Lucent/AT&T **nmake** (`ncland.mk`), POSIX `regex.h`, ZeroMQ (`-lzmq`), `nfunit-test.hpp` (run via `./ncland_unit_tests`).

---

## Critical context for the implementer (read first)

- **Build:** Lucent/AT&T **nmake**, not GNU make. From `cnc/ncland/src`: `nmake -f ncland.mk <target>`. Ensure `BASE`=`git rev-parse --show-toplevel` (`/home/dan/Git/netflex`) and `VPATH`'s first segment == `$BASE`. If a build fails on missing `global*.nmk`/project headers, STOP and report — don't hack include paths.
- **Tests:** `nmake -f ncland.mk ncland_unit_tests && ./ncland_unit_tests` — baseline **93 passed, 0 failed, 6 skipped**. Keep it green (plus new tests). `nfunit-test.hpp`: `TEST("suite","name"){ REQUIRE(...); REQUIRE_EQ(a,b); }`. Run one suite: `./ncland_unit_tests <suite>`.
- **Commit trailer (every commit):** `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`. Work on branch `ncland-start`; no new branches/worktrees.
- **Doxygen** on new/modified functions & structs (project rule).
- **conn_t is POD** (`conn_init` memsets it; `warehouse_install_conn` memcpys it). New `conn_t` fields MUST be POD (int/time_t/regex_t). Do NOT add std:: members to `conn_t`. Per-conn var store is already wh-side.
- **Registry entries & regex_t copy discipline:** `ncland_registry_build(node, &entry, &err)` compiles into a caller entry; the loader then stores it via `by_dtype[dtype] = entry` (a bitwise copy — the compiled `regex_t` internals are shared, and the transient source is abandoned WITHOUT `regfree`, so only the map copy is ever freed). This is how `prmpt1re` already works. Follow it: compile in `build`, never `regfree` a transient entry, and free only the live map entries once (Task 1's `ncland_registry_free`).
- **Wire format (ctl):** `wh->zmq_ctl` is a ZMQ_ROUTER bound to `cfg.ctl_endpoint`. A command is 3 frames `[identity][empty][ncland_cmd_msg_t bytes]`; a response is `[identity][empty][ncland_rsp_msg_t bytes]` (see `ncland_zmq_recv_cmd`/`ncland_zmq_send_rsp`). Structs are sent raw, so a client built from the same `ncland.h` is automatically compatible; adding fields just grows the struct (rebuild both).
- **Current command path (`ncland_worker.cpp`):**
  - `dispatch_cmd(conn_t*, ncland_cmd_msg_t*)`: writes `msg->data` + `"\n"`, arms `cmd_deadline`, copies `seq/orig_cnc/tty/is_rtrv`, clears `rbuf`/`rlen`, sets `CS_CMD_PENDING`.
  - `handle_ne_data(conn_t*, ncland_wh_t*)`: appends to `rbuf`; hardcoded `--More--` block; if `state==CS_CMD_PENDING` and `prmpt1re`/`prmpt2re` matches → builds `ncland_rsp_msg_t {conn_id,seq,orig_cnc,tty,dm_type,text}`, `worker_send_rsp(wh,&rsp)`, clears buffer, `CS_READY`.
  - `check_keepalives(ncland_wh_t*)`: for `CS_READY` conns, `conn_needs_keepalive` → `ncland_ne_write(c,"\n",1)`, bump `next_keepalive`.
  - `check_expired_cmds`: `CS_CMD_PENDING` past `cmd_deadline` → DENY response, `CS_READY`.
  - `next_timeout_ms`: min over `CS_CMD_PENDING` `cmd_deadline` and `CS_AUTHENTICATING` deadlines.
- **`warehouse_handle_cmd_from_zmq(wh, identity, identity_len, cmd)`** (`ncland_warehouse.cpp`): validates `cmd->conn_id`, `warehouse_pending_stash(wh, seq, identity, len)`, `ncland_parse_cmd_data(cmd)`, `dispatch_cmd`. On failure rolls back the stash.
- **`ncland_zmq_send_rsp(wh, identity, identity_len, rsp)`** sends `[id][empty][rsp]` directly. **`worker_send_rsp(wh, rsp)`** does `pending_take(seq)`→identity then `ncland_zmq_send_rsp`.
- **`find_conn_by_neid`** is currently `static` in `ncland_notify.cpp` (`for i: state!=CS_IDLE && neid==…`). Task 7 promotes it.
- **`warehouse_drop_conn(wh, conn_t*)`** (from #2): `EPOLL_CTL_DEL` + `conn_free` + `nconns--`. Use it for keepalive liveness teardown.
- **`ncland_registry_find(&wh->registry, dtype)`** → `const ne_entry_t*` or NULL.

---

## File Structure

| File | Responsibility in 3a |
|------|----------------------|
| `ncland_registry.h` / `.cpp` | `ne_entry_t`: compiled `more_re`/`more_valid`/`error_res`; compile in `ncland_registry_build`; new `ncland_registry_free`. |
| `ncland.h` | New struct fields (`cmd.neid`/`cmd.check_errors`, `rsp.result`+`NCLAND_RESULT_*`, `conn.check_errors`/`conn.ka_deadline`), `CS_KEEPALIVE`, `KA_RESPONSE_TMOUT_S`; decl `ncland_find_conn_by_neid`. |
| `ncland_worker.cpp` | `dispatch_cmd` (copy check_errors), `handle_ne_data` (registry paging, error scan→result, `CS_KEEPALIVE` drain), `check_keepalives` (ka_*), `check_expired_cmds` (result=FAILURE), `next_timeout_ms` (ka_deadline). |
| `ncland_lua.cpp` | `resume_login` LUA_OK: apply `adapted_primary_regex` to `prmpt1re`. |
| `ncland_warehouse.cpp` | `warehouse_handle_cmd_from_zmq`: neid resolution + not-ready/busy FAILURE; keepalive liveness teardown in the main-loop sweep; call `ncland_registry_free` at shutdown; promote `ncland_find_conn_by_neid`. |
| `nclan_cmd.cpp` | **new** CLI sender. |
| `ncland.mk` | `nclan-cmd` target. |
| `ncland_lua_tests.cpp` / `ncland_unit_tests.cpp` / `ncland_registry_tests.cpp` | the unit tests. |

Tasks are sequential (later tasks use earlier fields). Each builds and stays green on its own.

---

## Task 1: Registry — compile `more_re` + `error_res`; add `ncland_registry_free`

**Files:** `ncland_registry.h`, `ncland_registry.cpp`, `ncland_registry_tests.cpp`

- [ ] **Step 1: Add the compiled fields to `ne_entry_t`** (`ncland_registry.h`), right after the existing `prmpt2re`/`prmpt2_valid`:

```cpp
    /* compiled paging + error patterns (#3a; compiled in ncland_registry_build,
     * freed by ncland_registry_free). Per-type, shared by all conns of dtype. */
    regex_t              more_re;          /**< compiled more_pattern. */
    bool                 more_valid = false;
    std::vector<regex_t> error_res;        /**< compiled errors[] (one per entry). */
```

Add `#include <vector>` and `#include <regex.h>` to `ncland_registry.h` if not already present.

- [ ] **Step 2: Declare `ncland_registry_free`** in `ncland_registry.h` (near `ncland_registry_load`):

```cpp
/** @brief Free all compiled regexes in a registry (prompt/more/errors) and clear it.
 *  Call once at shutdown. Safe on an empty/default registry. */
void ncland_registry_free(ncland_registry_t *reg);
```

- [ ] **Step 3: Write the failing test** (`ncland_registry_tests.cpp`): a YAML with paging + errors compiles them.

```cpp
TEST("registry", "R-3a more_re and error_res compile from YAML") {
    ne_entry_t e;
    std::string err;
    const char *yaml =
        "ne_type: CIENA_RLS\n"           /* a known dtype name */
        "vendor: x\nschema_version: 1\n"
        "transport: {protocol: [ssh]}\n"
        "prompt: {primary_regex: '[a-z]+#'}\n"
        "paging: {more_pattern: '--More--', more_response: ' '}\n"
        "errors: ['% Invalid', 'ERROR']\n"
        "lua_module: x.lua\n";
    REQUIRE_EQ(ncland_registry_build(YAML::Load(yaml), &e, &err), 0);
    REQUIRE(e.more_valid);
    REQUIRE_EQ((int)e.error_res.size(), 2);
    /* the compiled regexes actually match */
    REQUIRE_EQ(regexec(&e.more_re, "xx--More--", 0, nullptr, 0), 0);
    REQUIRE_EQ(regexec(&e.error_res[0], "% Invalid input", 0, nullptr, 0), 0);
    /* free directly (single transient entry; mirrors registry teardown) */
    regfree(&e.prmpt1re);
    if (e.prmpt2_valid) regfree(&e.prmpt2re);
    regfree(&e.more_re);
    for (auto &r : e.error_res) regfree(&r);
}

TEST("registry", "R-3a empty paging leaves more_valid false") {
    ne_entry_t e; std::string err;
    const char *yaml =
        "ne_type: CIENA_RLS\nvendor: x\nschema_version: 1\n"
        "transport: {protocol: [ssh]}\n"
        "prompt: {primary_regex: '[a-z]+#'}\n"
        "lua_module: x.lua\n";
    REQUIRE_EQ(ncland_registry_build(YAML::Load(yaml), &e, &err), 0);
    REQUIRE(!e.more_valid);
    REQUIRE_EQ((int)e.error_res.size(), 0);
    regfree(&e.prmpt1re);
    if (e.prmpt2_valid) regfree(&e.prmpt2re);
}
```

- [ ] **Step 4: Run it (fails)** — `nmake -f ncland.mk ncland_unit_tests && ./ncland_unit_tests registry`. Expected: the two R-3a tests fail (`more_valid` not set / `error_res` empty) since build doesn't compile them yet.

- [ ] **Step 5: Compile the patterns in `ncland_registry_build`** (`ncland_registry.cpp`). After the existing `errors` list is read (`if (n["errors"]) reg_to_list(...)`) and before the final `return 0`, add:

```cpp
    /* Compile paging more_pattern (optional). */
    out->more_valid = false;
    if (!out->more_pattern.empty()) {
        if (regcomp(&out->more_re, out->more_pattern.c_str(), REG_EXTENDED) != 0) {
            regfree(&out->prmpt1re); out->prmpt1_valid = false;
            if (out->prmpt2_valid) { regfree(&out->prmpt2re); out->prmpt2_valid = false; }
            return fail("more_pattern failed to compile");
        }
        out->more_valid = true;
    }

    /* Compile errors[] (each entry an ERE). */
    out->error_res.clear();
    for (size_t i = 0; i < out->errors.size(); i++) {
        regex_t re;
        if (regcomp(&re, out->errors[i].c_str(), REG_EXTENDED) != 0) {
            for (auto &r : out->error_res) regfree(&r);
            out->error_res.clear();
            if (out->more_valid) { regfree(&out->more_re); out->more_valid = false; }
            regfree(&out->prmpt1re); out->prmpt1_valid = false;
            if (out->prmpt2_valid) { regfree(&out->prmpt2re); out->prmpt2_valid = false; }
            return fail("errors[" + std::to_string(i) + "] failed to compile");
        }
        out->error_res.push_back(re);
    }
```

- [ ] **Step 6: Implement `ncland_registry_free`** (`ncland_registry.cpp`):

```cpp
void ncland_registry_free(ncland_registry_t *reg)
{
    if (!reg) return;
    for (auto &kv : reg->by_dtype) {
        ne_entry_t &e = kv.second;
        if (e.prmpt1_valid) { regfree(&e.prmpt1re); e.prmpt1_valid = false; }
        if (e.prmpt2_valid) { regfree(&e.prmpt2re); e.prmpt2_valid = false; }
        if (e.more_valid)   { regfree(&e.more_re);  e.more_valid  = false; }
        for (auto &r : e.error_res) regfree(&r);
        e.error_res.clear();
    }
    reg->by_dtype.clear();
}
```

- [ ] **Step 7: Run tests (pass)** — `./ncland_unit_tests registry` → R-3a tests pass. Then full `./ncland_unit_tests` → `95 passed, 0 failed, 6 skipped`.

- [ ] **Step 8: Commit**
```bash
cd /home/dan/Git/netflex
git add cnc/ncland/src/ncland_registry.h cnc/ncland/src/ncland_registry.cpp cnc/ncland/src/ncland_registry_tests.cpp
git commit -m "[ncland] registry: compile more_re/error_res; add ncland_registry_free

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 2: Message/conn fields — `neid`, `check_errors`, `result`

**Files:** `ncland.h`, `ncland_worker.cpp`, `ncland_unit_tests.cpp`

- [ ] **Step 1: Add the result enum + struct fields** (`ncland.h`).

In the constants area (near `CLAN2_MSG_*`):
```c
/* Command result, returned to the caller in ncland_rsp_msg_t.result (#3a). */
#define NCLAND_RESULT_SUCCESS 0   /**< Command completed; no error pattern matched (or not checked). */
#define NCLAND_RESULT_FAILURE 1   /**< An errors[] pattern matched the response. */
```

In `ncland_cmd_msg_t` (after `is_rtrv`):
```c
    int     neid;         /**< Target NE id; ncland maps it to the live conn (#3a). */
    int     check_errors; /**< 1 = scan response against errors[] for SUCCESS/FAILURE; 0 = raw, no verdict (#3a). */
```

In `ncland_rsp_msg_t` (after `dm_type`):
```c
    int   result;             /**< NCLAND_RESULT_SUCCESS | NCLAND_RESULT_FAILURE (#3a). */
```

In `conn_t` (after `is_rtrv`):
```c
    int            check_errors;   /**< Copied from the command msg; gates the errors[] scan (#3a). */
```

- [ ] **Step 2: Write the failing test** (`ncland_unit_tests.cpp`, suite `worker`): dispatch copies `check_errors`.

```cpp
TEST("worker", "T-3a dispatch_cmd copies check_errors onto the conn") {
    int sv[2]; REQUIRE_EQ(socketpair(AF_UNIX, SOCK_STREAM, 0, sv), 0);
    ncland_wh_t wh{};
    for (int i = 0; i < MAX_CONNS; i++) conn_init(&wh.conn[i], i);
    conn_t *c = &wh.conn[5]; c->net_fd = sv[0]; c->state = CS_READY;
    ncland_cmd_msg_t cmd; memset(&cmd, 0, sizeof(cmd));
    cmd.conn_id = 5; cmd.seq = 1; cmd.tmout = 30; cmd.check_errors = 1;
    snprintf(cmd.data, sizeof(cmd.data), "X");
    REQUIRE_EQ(dispatch_cmd(c, &cmd), 0);
    REQUIRE_EQ(c->check_errors, 1);
    close(sv[0]); close(sv[1]);
}
```

- [ ] **Step 3: Run it (fails)** — `./ncland_unit_tests worker` → fails (`c->check_errors` not set; it is 0 from conn_init).

- [ ] **Step 4: Copy the flag in `dispatch_cmd`** (`ncland_worker.cpp`), alongside the other `c->… = msg->…` copies (`c->seq`, `c->is_rtrv`, …):

```c
    c->check_errors = msg->check_errors;
```

- [ ] **Step 5: Run tests (pass)** — `./ncland_unit_tests worker` passes; full suite `96 passed, 0 failed, 6 skipped`.

- [ ] **Step 6: Commit**
```bash
cd /home/dan/Git/netflex
git add cnc/ncland/src/ncland.h cnc/ncland/src/ncland_worker.cpp cnc/ncland/src/ncland_unit_tests.cpp
git commit -m "[ncland] add cmd.neid/check_errors, rsp.result, conn.check_errors

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 3: Error scan → `result` in `handle_ne_data`

**Files:** `ncland_worker.cpp`, `ncland_unit_tests.cpp`

The response-build block in `handle_ne_data` (the `if (matched) { … }` branch) currently sets `rsp.conn_id/seq/orig_cnc/tty/dm_type/text`. Add the error scan and `result`.

- [ ] **Step 1: Write the failing tests** (`ncland_unit_tests.cpp`, suite `worker`). These drive `handle_ne_data` to completion with a registry entry carrying an error pattern. Use a helper that builds a wh with one dtype entry and a prompt regex.

```cpp
/* Build wh with dtype 'd': prompt '[a-z0-9]+#', optional error pattern. */
static void wh_with_entry(ncland_wh_t &wh, int d, const char *prompt,
                          const char *errpat /*nullptr for none*/) {
    for (int i = 0; i < MAX_CONNS; i++) conn_init(&wh.conn[i], i);
    ne_entry_t e; e.dtype = d; e.ne_type = "TEST_NE"; e.lua_module = "x.lua";
    e.primary_regex = prompt;
    REQUIRE_EQ(regcomp(&e.prmpt1re, prompt, REG_EXTENDED), 0); e.prmpt1_valid = true;
    if (errpat) {
        regex_t r; REQUIRE_EQ(regcomp(&r, errpat, REG_EXTENDED), 0);
        e.error_res.push_back(r); e.errors.push_back(errpat);
    }
    wh.registry.by_dtype[d] = e;
}

/* Drive a CS_CMD_PENDING conn to a response; return the captured rsp via a seam. */
TEST("worker", "T-3a error pattern -> result FAILURE when check_errors on") {
    int sv[2]; REQUIRE_EQ(socketpair(AF_UNIX, SOCK_STREAM, 0, sv), 0);
    fcntl(sv[0], F_SETFL, O_NONBLOCK);
    ncland_wh_t wh{};
    wh_with_entry(wh, 9001, "[a-z0-9]+#", "% Invalid");
    conn_t *c = &wh.conn[6];
    c->net_fd = sv[0]; c->dtype = 9001; c->state = CS_CMD_PENDING;
    c->seq = 0; c->check_errors = 1;
    /* compile the conn prompt (handle_ne_data matches on c->prmpt1re) */
    REQUIRE_EQ(regcomp(&c->prmpt1re, "[a-z0-9]+#", REG_EXTENDED), 0); c->prmpt1re_valid = 1;
    write(sv[1], "% Invalid input\nsw1#", 20);
    handle_ne_data(c, &wh);
    REQUIRE_EQ((int)c->state, (int)CS_READY);     /* response delivered, back to ready */
    REQUIRE_EQ(g_test_last_rsp_result, NCLAND_RESULT_FAILURE);
    close(sv[0]); close(sv[1]);
}

TEST("worker", "T-3a clean response -> SUCCESS; check_errors off -> SUCCESS") {
    int sv[2]; REQUIRE_EQ(socketpair(AF_UNIX, SOCK_STREAM, 0, sv), 0);
    fcntl(sv[0], F_SETFL, O_NONBLOCK);
    ncland_wh_t wh{}; wh_with_entry(wh, 9001, "[a-z0-9]+#", "% Invalid");
    conn_t *c = &wh.conn[7]; c->net_fd = sv[0]; c->dtype = 9001;
    c->state = CS_CMD_PENDING; c->seq = 0; c->check_errors = 1;
    REQUIRE_EQ(regcomp(&c->prmpt1re, "[a-z0-9]+#", REG_EXTENDED), 0); c->prmpt1re_valid = 1;
    write(sv[1], "all good\nsw1#", 13);
    handle_ne_data(c, &wh);
    REQUIRE_EQ(g_test_last_rsp_result, NCLAND_RESULT_SUCCESS);

    /* now with the error text but check_errors OFF -> SUCCESS */
    conn_t *c2 = &wh.conn[8]; int sv2[2]; socketpair(AF_UNIX, SOCK_STREAM, 0, sv2);
    fcntl(sv2[0], F_SETFL, O_NONBLOCK);
    c2->net_fd = sv2[0]; c2->dtype = 9001; c2->state = CS_CMD_PENDING;
    c2->seq = 0; c2->check_errors = 0;
    REQUIRE_EQ(regcomp(&c2->prmpt1re, "[a-z0-9]+#", REG_EXTENDED), 0); c2->prmpt1re_valid = 1;
    write(sv2[1], "% Invalid input\nsw1#", 20);
    handle_ne_data(c2, &wh);
    REQUIRE_EQ(g_test_last_rsp_result, NCLAND_RESULT_SUCCESS);
    close(sv[0]); close(sv[1]); close(sv2[0]); close(sv2[1]);
}
```

These reference a test seam `g_test_last_rsp_result` (the last response's result). Add it next to the existing `worker_send_rsp` seam.

- [ ] **Step 2: Add the seam + error scan.** In `ncland_worker.cpp`, near the top with the other test seam (`g_test_worker_dispatched_seq`):

```c
/* Test seam: result of the most recent response built by handle_ne_data. */
int g_test_last_rsp_result = NCLAND_RESULT_SUCCESS;
```

In `handle_ne_data`'s `if (matched)` block, after `snprintf(rsp.text, …)` and before `worker_send_rsp(wh, &rsp)`, insert the scan:

```c
        /* Error scan -> SUCCESS/FAILURE (#3a), gated per request by check_errors. */
        rsp.result = NCLAND_RESULT_SUCCESS;
        if (c->check_errors) {
            const ne_entry_t *e = ncland_registry_find(&wh->registry, c->dtype);
            if (e) {
                for (size_t i = 0; i < e->error_res.size(); i++) {
                    if (regexec(&e->error_res[i], c->rbuf, 0, nullptr, 0) == 0) {
                        rsp.result = NCLAND_RESULT_FAILURE;
                        LOG_WARN("neid=%d cmd failed: matched error pattern", c->neid);
                        break;
                    }
                }
            }
        }
        g_test_last_rsp_result = rsp.result;
```

(`rsp` is already `memset` to 0, so `result` defaults SUCCESS even if the block is skipped; the explicit set documents intent.)

- [ ] **Step 3: Run tests (pass)** — `./ncland_unit_tests worker`. Full suite `98 passed, 0 failed, 6 skipped`.

- [ ] **Step 4: Commit**
```bash
cd /home/dan/Git/netflex
git add cnc/ncland/src/ncland_worker.cpp cnc/ncland/src/ncland_unit_tests.cpp
git commit -m "[ncland] handle_ne_data: errors[] scan -> rsp.result (gated by check_errors)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 4: Registry-driven paging in `handle_ne_data`

**Files:** `ncland_worker.cpp`, `ncland_unit_tests.cpp`

Replace the hardcoded `--More--` block with the registry `more_re`/`more_response`.

- [ ] **Step 1: Write the failing tests** (suite `worker`):

```cpp
TEST("worker", "T-3a paging uses more_pattern/more_response") {
    int sv[2]; REQUIRE_EQ(socketpair(AF_UNIX, SOCK_STREAM, 0, sv), 0);
    fcntl(sv[0], F_SETFL, O_NONBLOCK); fcntl(sv[1], F_SETFL, O_NONBLOCK);
    ncland_wh_t wh{}; wh_with_entry(wh, 9001, "[a-z0-9]+#", nullptr);
    ne_entry_t &e = wh.registry.by_dtype[9001];
    e.more_pattern = "--More--"; e.more_response = " ";
    REQUIRE_EQ(regcomp(&e.more_re, "--More--", REG_EXTENDED), 0); e.more_valid = true;
    conn_t *c = &wh.conn[9]; c->net_fd = sv[0]; c->dtype = 9001;
    c->state = CS_CMD_PENDING; c->seq = 0; c->check_errors = 0;
    REQUIRE_EQ(regcomp(&c->prmpt1re, "[a-z0-9]+#", REG_EXTENDED), 0); c->prmpt1re_valid = 1;
    /* page 1 ends with the more prompt */
    write(sv[1], "line1\n--More--", 14);
    handle_ne_data(c, &wh);
    char b[16] = {0}; int n = (int)read(sv[1], b, sizeof(b)-1);
    REQUIRE(n >= 1 && b[0] == ' ');               /* more_response sent */
    REQUIRE_EQ((int)c->state, (int)CS_CMD_PENDING); /* still reading, not done */
    /* page 2 ends with the prompt */
    write(sv[1], "line2\nsw1#", 10);
    handle_ne_data(c, &wh);
    REQUIRE_EQ((int)c->state, (int)CS_READY);
    close(sv[0]); close(sv[1]);
}

TEST("worker", "T-3a no paging when more_pattern empty") {
    int sv[2]; REQUIRE_EQ(socketpair(AF_UNIX, SOCK_STREAM, 0, sv), 0);
    fcntl(sv[0], F_SETFL, O_NONBLOCK); fcntl(sv[1], F_SETFL, O_NONBLOCK);
    ncland_wh_t wh{}; wh_with_entry(wh, 9001, "[a-z0-9]+#", nullptr); /* more_valid=false */
    conn_t *c = &wh.conn[10]; c->net_fd = sv[0]; c->dtype = 9001;
    c->state = CS_CMD_PENDING; c->seq = 0; c->check_errors = 0;
    REQUIRE_EQ(regcomp(&c->prmpt1re, "[a-z0-9]+#", REG_EXTENDED), 0); c->prmpt1re_valid = 1;
    write(sv[1], "data\nsw1#", 9);
    handle_ne_data(c, &wh);
    char b[8] = {0}; int n = (int)read(sv[1], b, sizeof(b)-1);
    REQUIRE(n <= 0);                              /* nothing written back */
    REQUIRE_EQ((int)c->state, (int)CS_READY);
    close(sv[0]); close(sv[1]);
}
```

- [ ] **Step 2: Run (fails)** — the first test fails because the hardcoded `--More--` block sends `" "` only for the literal `--More--` token but does not consult the registry (it would actually still pass for `--More--`!). To make the test meaningful and the behavior correct, **replace** the hardcoded block. Run after the replacement.

- [ ] **Step 3: Replace the hardcoded paging block** in `handle_ne_data`. Find the existing block:

```c
    /* ---- --More-- pagination ---- */
    if (strstr(c->rbuf, "--More--") != NULL) {
        ne_write(c, " ", 1);
        ...strip "--More--"...
        return;
    }
```

Replace it entirely with registry-driven paging:

```c
    /* ---- paging (registry-driven, design §3) ---- */
    {
        const ne_entry_t *e = ncland_registry_find(&wh->registry, c->dtype);
        if (e && e->more_valid) {
            regmatch_t mm;
            if (regexec(&e->more_re, c->rbuf, 1, &mm, 0) == 0) {
                if (!e->more_response.empty())
                    ncland_ne_write(c, e->more_response.c_str(),
                                    (int)e->more_response.size());
                /* strip the matched token so it doesn't pollute the response */
                int tok = mm.rm_eo - mm.rm_so;
                int tail = c->rlen - mm.rm_eo;
                if (tail > 0)
                    memmove(c->rbuf + mm.rm_so, c->rbuf + mm.rm_eo, (size_t)tail);
                c->rlen -= tok;
                if (c->rlen < 0) c->rlen = 0;
                c->rbuf[c->rlen] = '\0';
                return; /* wait for more data */
            }
        }
    }
```

(Uses `ncland_ne_write` — the promoted name from #2. Confirm the function uses that name in this file.)

- [ ] **Step 4: Run tests (pass)** — `./ncland_unit_tests worker`. Full `100 passed, 0 failed, 6 skipped`.

- [ ] **Step 5: Commit**
```bash
cd /home/dan/Git/netflex
git add cnc/ncland/src/ncland_worker.cpp cnc/ncland/src/ncland_unit_tests.cpp
git commit -m "[ncland] handle_ne_data: registry-driven paging (more_pattern/more_response)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 5: Apply `adapted_primary_regex` at `CS_READY`

**Files:** `ncland_lua.cpp`, `ncland_lua_tests.cpp`

When the login coroutine finishes, if `login()` published `adapted_primary_regex`, recompile the conn's `prmpt1re` from it so command prompt-matching uses the live prompt.

- [ ] **Step 1: Write the failing test** (`ncland_lua_tests.cpp`, suite `lua`):

```cpp
TEST("lua", "L14 adapted_primary_regex is applied to prmpt1re at CS_READY") {
    int sv[2]; REQUIRE_EQ(socketpair(AF_UNIX, SOCK_STREAM, 0, sv), 0);
    fcntl(sv[0], F_SETFL, O_NONBLOCK);
    /* login publishes an adapted regex then returns immediately. */
    const char *src =
        "local M={} function M.login(s)\n"
        "  s:set_var('adapted_primary_regex', 'node-9[*^]*#')\n"
        "end return M\n";
    ncland_wh_t wh{}; setup_login_wh(wh, 9001, src);
    conn_t *c = &wh.conn[16]; c->net_fd = sv[0]; c->dtype = 9001;
    REQUIRE_EQ(ncland_login_start(&wh, c), NCLAND_LOGIN_READY);  /* no expect -> finishes */
    REQUIRE_EQ((int)c->state, (int)CS_READY);
    REQUIRE(c->prmpt1re_valid);
    /* the recompiled regex matches the live prompt */
    REQUIRE_EQ(regexec(&c->prmpt1re, "node-9#", 0, nullptr, 0), 0);
    ncland_lua_shutdown(&wh); close(sv[0]); close(sv[1]);
}
```

- [ ] **Step 2: Run (fails)** — `prmpt1re` is whatever `conn_init` left (invalid) so `prmpt1re_valid` is 0 / regexec fails.

- [ ] **Step 3: Apply the adapted regex in `resume_login`** (`ncland_lua.cpp`), in the `LUA_OK` branch, before `conn_set_state(c, CS_READY)`:

```c
        /* If login() published an adapted prompt, recompile it into prmpt1re so
         * the command path matches the live prompt (design §4). */
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
```

- [ ] **Step 4: Run tests (pass)** — `./ncland_unit_tests lua`. Full `101 passed, 0 failed, 6 skipped`.

- [ ] **Step 5: Commit**
```bash
cd /home/dan/Git/netflex
git add cnc/ncland/src/ncland_lua.cpp cnc/ncland/src/ncland_lua_tests.cpp
git commit -m "[ncland] apply login()'s adapted_primary_regex to prmpt1re at CS_READY

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 6: Registry-driven keepalive + `CS_KEEPALIVE` drain + liveness

**Files:** `ncland.h`, `ncland_worker.cpp`, `ncland_warehouse.cpp`, `ncland_unit_tests.cpp`

- [ ] **Step 1: Add the state, field, and constant** (`ncland.h`).

In `conn_state_t`, add after `CS_CMD_PENDING`:
```c
    CS_KEEPALIVE,        /**< Keepalive sent; draining the echo to the prompt (#3a). */
```
In `conn_t`, after `next_keepalive`:
```c
    time_t         ka_deadline;    /**< CS_KEEPALIVE response deadline; 0 when not keepalive-pending (#3a). */
```
Near `KEEPALIVE_INTERVAL_MS`:
```c
#define KA_RESPONSE_TMOUT_S 30   /**< Max seconds to wait for a keepalive echo before declaring the link dead (#3a). */
```

- [ ] **Step 2: Write the failing tests** (suite `worker`):

```cpp
TEST("worker", "T-3a keepalive sends ka_command and enters CS_KEEPALIVE") {
    int sv[2]; REQUIRE_EQ(socketpair(AF_UNIX, SOCK_STREAM, 0, sv), 0);
    fcntl(sv[0], F_SETFL, O_NONBLOCK); fcntl(sv[1], F_SETFL, O_NONBLOCK);
    ncland_wh_t wh{}; wh_with_entry(wh, 9001, "[a-z0-9]+#", nullptr);
    ne_entry_t &e = wh.registry.by_dtype[9001];
    e.ka_enabled = true; e.ka_interval_s = 600; e.ka_command = "\n";
    e.ka_expect_response = true;
    conn_t *c = &wh.conn[11]; c->net_fd = sv[0]; c->dtype = 9001;
    c->state = CS_READY; c->next_keepalive = time(NULL) - 1;  /* due now */
    REQUIRE_EQ(regcomp(&c->prmpt1re, "[a-z0-9]+#", REG_EXTENDED), 0); c->prmpt1re_valid = 1;
    check_keepalives(&wh);
    char b[8] = {0}; int n = (int)read(sv[1], b, sizeof(b)-1);
    REQUIRE(n >= 1 && b[0] == '\n');
    REQUIRE_EQ((int)c->state, (int)CS_KEEPALIVE);
    /* echo the prompt -> handle_ne_data drains, no caller response, back to READY */
    write(sv[1], "sw1#", 4);
    handle_ne_data(c, &wh);
    REQUIRE_EQ((int)c->state, (int)CS_READY);
    REQUIRE_EQ(c->rlen, 0);
    close(sv[0]); close(sv[1]);
}

TEST("worker", "T-3a keepalive disabled sends nothing") {
    int sv[2]; REQUIRE_EQ(socketpair(AF_UNIX, SOCK_STREAM, 0, sv), 0);
    fcntl(sv[0], F_SETFL, O_NONBLOCK); fcntl(sv[1], F_SETFL, O_NONBLOCK);
    ncland_wh_t wh{}; wh_with_entry(wh, 9001, "[a-z0-9]+#", nullptr);
    wh.registry.by_dtype[9001].ka_enabled = false;
    conn_t *c = &wh.conn[12]; c->net_fd = sv[0]; c->dtype = 9001;
    c->state = CS_READY; c->next_keepalive = time(NULL) - 1;
    check_keepalives(&wh);
    char b[8] = {0}; int n = (int)read(sv[1], b, sizeof(b)-1);
    REQUIRE(n <= 0);
    REQUIRE_EQ((int)c->state, (int)CS_READY);
    close(sv[0]); close(sv[1]);
}
```

- [ ] **Step 3: Run (fails)** — `CS_KEEPALIVE` undefined until Step 1; after Step 1, `check_keepalives` still uses the hardcoded `"\n"` and never sets `CS_KEEPALIVE`, so the first test fails on state.

- [ ] **Step 4: Rewrite `check_keepalives`** (`ncland_worker.cpp`):

```c
void check_keepalives(ncland_wh_t *wh)
{
    time_t now = time(NULL);
    for (int i = 0; i < MAX_CONNS; i++) {
        conn_t *c = &wh->conn[i];
        if (c->state != CS_READY) continue;
        const ne_entry_t *e = ncland_registry_find(&wh->registry, c->dtype);
        if (!e || !e->ka_enabled) continue;
        if (now < c->next_keepalive) continue;

        if (!e->ka_command.empty())
            ncland_ne_write(c, e->ka_command.c_str(), (int)e->ka_command.size());
        c->next_keepalive = now + (e->ka_interval_s > 0 ? e->ka_interval_s
                                                        : (KEEPALIVE_INTERVAL_MS / 1000));
        if (e->ka_expect_response) {
            c->rlen = 0; c->rbuf[0] = '\0';        /* fresh buffer for the echo */
            c->ka_deadline = now + KA_RESPONSE_TMOUT_S;
            conn_set_state(c, CS_KEEPALIVE);
        }
        LOG_DEBUG("conn[%d]: keepalive sent (expect_response=%d)", c->id, e->ka_expect_response);
    }
}
```

- [ ] **Step 5: Drain the keepalive echo in `handle_ne_data`.** The prompt-match guard currently is `if (c->state != CS_CMD_PENDING) return;`. Change it so `CS_KEEPALIVE` is also processed, and on a match in `CS_KEEPALIVE` just clear and return to `CS_READY` with **no** response. Replace the guard + match-build with:

```c
    /* prompt matching applies to CS_CMD_PENDING (deliver response) and
     * CS_KEEPALIVE (drain echo, no response). */
    if (c->state != CS_CMD_PENDING && c->state != CS_KEEPALIVE)
        return;

    regmatch_t pmatch;
    int matched = 0;
    if (c->prmpt1re_valid && regexec(&c->prmpt1re, c->rbuf, 1, &pmatch, 0) == 0) matched = 1;
    if (!matched && c->prmpt2re_valid && regexec(&c->prmpt2re, c->rbuf, 1, &pmatch, 0) == 0) matched = 1;
    if (!matched) return;

    if (c->state == CS_KEEPALIVE) {
        /* keepalive echo drained: discard, no caller response. */
        c->rlen = 0; c->rbuf[0] = '\0';
        c->ka_deadline = 0;
        conn_set_state(c, CS_READY);
        return;
    }

    /* CS_CMD_PENDING: build + deliver the response (error scan from Task 3). */
    ncland_rsp_msg_t rsp;
    memset(&rsp, 0, sizeof(rsp));
    rsp.conn_id  = c->id;
    rsp.seq      = c->seq;
    rsp.orig_cnc = c->orig_cnc;
    rsp.tty      = c->tty;
    rsp.dm_type  = c->is_rtrv;
    snprintf(rsp.text, sizeof(rsp.text), "%.*s", (int)(sizeof(rsp.text) - 1), c->rbuf);
    /* (error scan -> rsp.result; inserted in Task 3) */
    rsp.result = NCLAND_RESULT_SUCCESS;
    if (c->check_errors) {
        const ne_entry_t *e = ncland_registry_find(&wh->registry, c->dtype);
        if (e) for (size_t i = 0; i < e->error_res.size(); i++)
            if (regexec(&e->error_res[i], c->rbuf, 0, nullptr, 0) == 0) {
                rsp.result = NCLAND_RESULT_FAILURE;
                LOG_WARN("neid=%d cmd failed: matched error pattern", c->neid);
                break;
            }
    }
    g_test_last_rsp_result = rsp.result;
    worker_send_rsp(wh, &rsp);
    c->rlen = 0; memset(c->rbuf, 0, sizeof(c->rbuf));
    conn_set_state(c, CS_READY);
```

(This consolidates Task 3's scan into the unified match block. If Task 3 left a separate block, fold it here — the net behavior is identical; ensure no duplicate response is sent.)

- [ ] **Step 6: `next_timeout_ms` accounts for keepalive timers** (`ncland_worker.cpp`). Extend the per-conn deadline pick:

```c
        } else if (c->state == CS_KEEPALIVE) {
            dl = c->ka_deadline;
        } else if (c->state == CS_READY) {
            const ne_entry_t *e = ncland_registry_find(&wh->registry, c->dtype);
            if (e && e->ka_enabled) dl = c->next_keepalive;
        }
```
(add alongside the existing `CS_CMD_PENDING` / `CS_AUTHENTICATING` cases; keeps the loop waking for due keepalives.)

- [ ] **Step 7: Liveness teardown in the main-loop sweep** (`ncland_warehouse.cpp`). In the per-iteration timer area (where the login-deadline sweep from #2 lives), add a keepalive-liveness sweep:

```c
        /* Keepalive liveness: a CS_KEEPALIVE conn whose echo never arrived is dead. */
        {
            time_t now = time(NULL);
            for (int i = 0; i < MAX_CONNS; i++) {
                conn_t *c = &wh->conn[i];
                if (c->state == CS_KEEPALIVE && c->ka_deadline && now >= c->ka_deadline) {
                    LOG_WARN("neid=%d conn=%d keepalive timeout; dropping (dead link)",
                             c->neid, c->id);
                    warehouse_drop_conn(wh, c);
                }
            }
        }
```

- [ ] **Step 8: Run tests (pass)** — `./ncland_unit_tests worker`. Full suite `103 passed, 0 failed, 6 skipped`. Build the daemon: `nmake -f ncland.mk` → rc=0.

- [ ] **Step 9: Commit**
```bash
cd /home/dan/Git/netflex
git add cnc/ncland/src/ncland.h cnc/ncland/src/ncland_worker.cpp cnc/ncland/src/ncland_warehouse.cpp cnc/ncland/src/ncland_unit_tests.cpp
git commit -m "[ncland] registry-driven keepalive (CS_KEEPALIVE drain + liveness teardown)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 7: neid addressing + not-ready/busy FAILURE in `warehouse_handle_cmd_from_zmq`

**Files:** `ncland.h`, `ncland_notify.cpp`, `ncland_warehouse.cpp`, `ncland_unit_tests.cpp`

- [ ] **Step 1: Promote `find_conn_by_neid`.** In `ncland_notify.cpp`, change `static int find_conn_by_neid(...)` to `int ncland_find_conn_by_neid(...)` and update its call sites in that file. Declare in `ncland.h`:

```c
/** @brief Index of the live (non-idle) conn for neid, or -1 if none (#3a). */
int ncland_find_conn_by_neid(ncland_wh_t *wh, int neid);
```

- [ ] **Step 2: Write the failing test** (suite `warehouse`). It checks that an unknown neid yields an immediate FAILURE response and a busy conn yields FAILURE. Use a test seam capturing the last directly-sent rsp result. Add seam `g_test_last_direct_rsp_result` set in the new immediate-reply helper.

```cpp
TEST("warehouse", "T-3a cmd to unknown neid replies FAILURE") {
    ncland_wh_t wh{};
    for (int i = 0; i < MAX_CONNS; i++) conn_init(&wh.conn[i], i);
    ncland_cmd_msg_t cmd; memset(&cmd, 0, sizeof(cmd));
    cmd.msg_type = CLAN2_MSG_CMD; cmd.neid = 777; cmd.seq = 5; cmd.check_errors = 1;
    snprintf(cmd.data, sizeof(cmd.data), "show x");
    uint8_t id[1] = {7};
    g_test_last_direct_rsp_result = -1;
    warehouse_handle_cmd_from_zmq(&wh, id, 1, &cmd);
    REQUIRE_EQ(g_test_last_direct_rsp_result, NCLAND_RESULT_FAILURE);
}

TEST("warehouse", "T-3a cmd to busy neid replies FAILURE") {
    ncland_wh_t wh{};
    for (int i = 0; i < MAX_CONNS; i++) conn_init(&wh.conn[i], i);
    conn_t *c = &wh.conn[3]; c->neid = 42; c->state = CS_CMD_PENDING;
    ncland_cmd_msg_t cmd; memset(&cmd, 0, sizeof(cmd));
    cmd.msg_type = CLAN2_MSG_CMD; cmd.neid = 42; cmd.seq = 6;
    snprintf(cmd.data, sizeof(cmd.data), "show x");
    uint8_t id[1] = {7};
    g_test_last_direct_rsp_result = -1;
    warehouse_handle_cmd_from_zmq(&wh, id, 1, &cmd);
    REQUIRE_EQ(g_test_last_direct_rsp_result, NCLAND_RESULT_FAILURE);
    REQUIRE_EQ((int)c->state, (int)CS_CMD_PENDING);  /* unchanged */
}
```

- [ ] **Step 3: Rework `warehouse_handle_cmd_from_zmq`** (`ncland_warehouse.cpp`) to resolve by neid and reply FAILURE immediately when unroutable/busy. Add a small static helper + seam at the top of the file:

```c
/* Test seam: result of the most recent immediate (direct) failure reply. */
int g_test_last_direct_rsp_result = -1;

/* Send an immediate one-frame result reply (no pending stash) to a caller. */
static void wh_reply_now(ncland_wh_t *wh, const uint8_t *identity, size_t identity_len,
                         const ncland_cmd_msg_t *cmd, int result, const char *text)
{
    ncland_rsp_msg_t rsp; memset(&rsp, 0, sizeof(rsp));
    rsp.seq      = cmd->seq;
    rsp.orig_cnc = cmd->orig_cnc;
    rsp.tty      = cmd->tty;
    rsp.result   = result;
    snprintf(rsp.text, sizeof(rsp.text), "%s", text ? text : "");
    g_test_last_direct_rsp_result = result;
    if (wh->zmq_ctl && identity && identity_len)
        ncland_zmq_send_rsp(wh, identity, identity_len, &rsp);
}
```

Replace the body's conn resolution (the `cmd->conn_id` checks) with neid resolution:

```c
    if (!wh || !cmd) return;

    int id = ncland_find_conn_by_neid(wh, cmd->neid);
    if (id < 0) {
        char m[64]; snprintf(m, sizeof(m), "no session for neid %d", cmd->neid);
        LOG_WARN("zmq cmd: %s", m);
        wh_reply_now(wh, identity, identity_len, cmd, NCLAND_RESULT_FAILURE, m);
        return;
    }
    conn_t *c = &wh->conn[id];
    if (c->state != CS_READY) {
        char m[64]; snprintf(m, sizeof(m), "neid %d busy (state %d)", cmd->neid, (int)c->state);
        LOG_WARN("zmq cmd: %s", m);
        wh_reply_now(wh, identity, identity_len, cmd, NCLAND_RESULT_FAILURE, m);
        return;
    }

    if (warehouse_pending_stash(wh, cmd->seq, identity, identity_len) != 0) {
        LOG_ERROR("pending stash failed for seq=%d", cmd->seq);
        return;
    }
    if (ncland_parse_cmd_data(cmd) < 0) {
        LOG_WARN("zmq cmd malformed for neid=%d", cmd->neid);
        uint8_t dump[256]; size_t dl = sizeof(dump);
        warehouse_pending_take(wh, cmd->seq, dump, &dl);
        wh_reply_now(wh, identity, identity_len, cmd, NCLAND_RESULT_FAILURE, "malformed command");
        return;
    }
    if (dispatch_cmd(c, cmd) < 0) {
        LOG_ERROR("dispatch_cmd failed for neid=%d seq=%d", cmd->neid, cmd->seq);
        uint8_t dump[256]; size_t dl = sizeof(dump);
        warehouse_pending_take(wh, cmd->seq, dump, &dl);
        wh_reply_now(wh, identity, identity_len, cmd, NCLAND_RESULT_FAILURE, "dispatch failed");
        return;
    }
```

(Keep the rest of the function — the trailing braces — intact. `ncland_parse_cmd_data` is unchanged.)

- [ ] **Step 4: Run tests (pass)** — `./ncland_unit_tests warehouse`. Full `105 passed, 0 failed, 6 skipped`.

- [ ] **Step 5: Commit**
```bash
cd /home/dan/Git/netflex
git add cnc/ncland/src/ncland.h cnc/ncland/src/ncland_notify.cpp cnc/ncland/src/ncland_warehouse.cpp cnc/ncland/src/ncland_unit_tests.cpp
git commit -m "[ncland] route commands by neid; immediate FAILURE for unknown/busy neid

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 8: `nclan-cmd` CLI sender

**Files:** `nclan_cmd.cpp` (new), `ncland.mk`, `ncland_unit_tests.cpp`

- [ ] **Step 1: Factor a testable arg parser.** To unit-test arg handling without spawning a process, put parsing in a small struct + function inside `nclan_cmd.cpp`, and expose it via a header-free `extern` used only by the test. Define in `nclan_cmd.cpp`:

```cpp
// nclan_cmd.cpp — CLI: send one CLAN2_MSG_CMD to ncland by neid, print the response.
#include "ncland.h"
#include <zmq.h>
#include <stdio.h>
#include <string.h>
#include <stdlib.h>
#include <unistd.h>

struct nclan_cmd_opts {
    int  neid;
    int  check_errors;
    int  tmout;
    char endpoint[256];
    char data[2560];
    int  valid;
};

/* Parse argv into opts. Returns 0 on success, -1 on usage error. */
int nclan_cmd_parse(int argc, char **argv, struct nclan_cmd_opts *o)
{
    memset(o, 0, sizeof(*o));
    o->check_errors = 1;
    o->tmout = 30;
    o->neid = -1;
    snprintf(o->endpoint, sizeof(o->endpoint),
             "ipc:///usr/cnc/lib/data/ncland/ctl.sock");

    int opt;
    optind = 1;
    while ((opt = getopt(argc, argv, "n:c:t:r")) != -1) {
        switch (opt) {
        case 'n': o->neid = atoi(optarg); break;
        case 'c': snprintf(o->endpoint, sizeof(o->endpoint), "%s", optarg); break;
        case 't': o->tmout = atoi(optarg); break;
        case 'r': o->check_errors = 0; break;
        default:  return -1;
        }
    }
    if (o->neid < 0 || optind >= argc) return -1;   /* need -n and a command */
    /* join remaining argv into the command body */
    size_t off = 0;
    for (int i = optind; i < argc && off < sizeof(o->data) - 1; i++) {
        int n = snprintf(o->data + off, sizeof(o->data) - off,
                         "%s%s", (i > optind) ? " " : "", argv[i]);
        if (n < 0) break;
        off += (size_t)n;
    }
    o->valid = 1;
    return 0;
}
```

- [ ] **Step 2: Write the failing arg-parse test** (`ncland_unit_tests.cpp`, suite `nclan_cmd`). Declare the parser extern in the test:

```cpp
struct nclan_cmd_opts {            /* must match nclan_cmd.cpp */
    int neid; int check_errors; int tmout; char endpoint[256]; char data[2560]; int valid;
};
extern int nclan_cmd_parse(int argc, char **argv, struct nclan_cmd_opts *o);

TEST("nclan_cmd", "C-3a parse neid/raw/command") {
    struct nclan_cmd_opts o;
    char *av1[] = {(char*)"nclan-cmd", (char*)"-n", (char*)"518",
                   (char*)"show", (char*)"shelf"};
    REQUIRE_EQ(nclan_cmd_parse(5, av1, &o), 0);
    REQUIRE_EQ(o.neid, 518);
    REQUIRE_EQ(o.check_errors, 1);
    REQUIRE_EQ(strcmp(o.data, "show shelf"), 0);

    char *av2[] = {(char*)"nclan-cmd", (char*)"-r", (char*)"-n", (char*)"7", (char*)"x"};
    REQUIRE_EQ(nclan_cmd_parse(5, av2, &o), 0);
    REQUIRE_EQ(o.check_errors, 0);

    char *av3[] = {(char*)"nclan-cmd", (char*)"show"};  /* missing -n */
    REQUIRE_EQ(nclan_cmd_parse(2, av3, &o), -1);
}
```

But the test target must link `nclan_cmd.o`. Rather than pull the whole tool (with its own `main`) into the test binary, **move `main` into a separate guard** so the parser object is reusable. In `nclan_cmd.cpp`, guard `main` with `#ifndef NCLAN_CMD_NO_MAIN`, and compile a test-only object. Simpler: keep `main` in `nclan_cmd.cpp` but compile the test against the parser by adding `nclan_cmd.o` to the test target **and** guarding `main` with `#ifndef NCLAND_UNIT_TEST`. The unit-test build already defines its own `main` in `ncland_unit_tests.cpp`; define `NCLAND_UNIT_TEST` for the test target so `nclan_cmd.cpp`'s `main` is excluded.

In `nclan_cmd.cpp`:
```cpp
#ifndef NCLAND_UNIT_TEST
int main(int argc, char **argv) { /* Step 3 body */ }
#endif
```

- [ ] **Step 3: Implement `main`** (`nclan_cmd.cpp`):

```cpp
#ifndef NCLAND_UNIT_TEST
int main(int argc, char **argv)
{
    struct nclan_cmd_opts o;
    if (nclan_cmd_parse(argc, argv, &o) != 0) {
        fprintf(stderr,
            "Usage: %s -n <neid> [-r] [-t secs] [-c endpoint] <command...>\n"
            "  -r  raw output (no SUCCESS/FAILURE verdict)\n", argv[0]);
        return 2;
    }

    void *ctx = zmq_ctx_new();
    void *s   = zmq_socket(ctx, ZMQ_DEALER);
    if (zmq_connect(s, o.endpoint) != 0) {
        fprintf(stderr, "connect %s failed: %s\n", o.endpoint, zmq_strerror(zmq_errno()));
        return 2;
    }

    ncland_cmd_msg_t cmd; memset(&cmd, 0, sizeof(cmd));
    cmd.msg_type = CLAN2_MSG_CMD;
    cmd.neid = o.neid;
    cmd.seq = 1;
    cmd.tmout = o.tmout;
    cmd.check_errors = o.check_errors;
    snprintf(cmd.data, sizeof(cmd.data), "%s", o.data);

    /* DEALER->ROUTER envelope: [empty][body]; the daemon's recv expects
     * [identity][empty][body] and the ROUTER prepends identity. */
    zmq_send(s, "", 0, ZMQ_SNDMORE);
    zmq_send(s, &cmd, sizeof(cmd), 0);

    /* wait up to tmout+5s for the reply */
    zmq_pollitem_t pi = { s, 0, ZMQ_POLLIN, 0 };
    int rc = zmq_poll(&pi, 1, (o.tmout + 5) * 1000);
    if (rc <= 0) { fprintf(stderr, "timeout waiting for response\n"); return 2; }

    zmq_msg_t empty, body;
    zmq_msg_init(&empty); zmq_msg_recv(&empty, s, 0); zmq_msg_close(&empty);
    zmq_msg_init(&body);
    int n = zmq_msg_recv(&body, s, 0);
    int result = NCLAND_RESULT_FAILURE;
    if (n == (int)sizeof(ncland_rsp_msg_t)) {
        ncland_rsp_msg_t *rsp = (ncland_rsp_msg_t *)zmq_msg_data(&body);
        fputs(rsp->text, stdout);
        if (rsp->text[0] && rsp->text[strlen(rsp->text)-1] != '\n') fputc('\n', stdout);
        result = rsp->result;
    } else {
        fprintf(stderr, "bad response (%d bytes)\n", n);
    }
    zmq_msg_close(&body);

    if (!o.check_errors) { /* raw: print no verdict */ }
    else printf("RESULT: %s\n", result == NCLAND_RESULT_SUCCESS ? "SUCCESS" : "FAILURE");

    zmq_close(s); zmq_ctx_term(ctx);
    return result == NCLAND_RESULT_SUCCESS ? 0 : 1;
}
#endif
```

- [ ] **Step 4: Wire `ncland.mk`.** Add the tool target (mirrors the `nclan-seed` rule) and add `nclan_cmd.o` (compiled with `-DNCLAND_UNIT_TEST`) to the `ncland_unit_tests` prerequisites.

Add a `nclan-cmd` build rule after the `nclan-seed` rule:
```
$(PBIN)/nclan-cmd :: nclan_cmd.o $(INCLIBS) -latm -linc -lxmlparse -lssh -ljson -lnetclan
	$(CPLUS_CC) $(CCFLAGS:M!=-AC99) $(LDFLAGS) -o $(<) $(*) -lpthread -lssh -lzmq
```
Add `$(PBIN)/nclan-cmd` to `.MAIN` (so a bare build produces it) alongside `$(PBIN)/ncland`.

For the test build of the parser without `main`, add a dedicated object rule and include it in the test target:
```
nclan_cmd_test.o : nclan_cmd.cpp
	$(CPLUS_CC) $(CPP_STANDARD) $(CCFLAGS:M!=-AC99|-std=gnu99) -DNCLAND_UNIT_TEST -c $(ACCFLAGS) nclan_cmd.cpp -o nclan_cmd_test.o
```
and append `nclan_cmd_test.o` to the `ncland_unit_tests ::` prerequisite list. (This compiles the parser with `main` excluded so the test's own `main` is the only one.)

- [ ] **Step 5: Build + run** — `nmake -f ncland.mk ncland_unit_tests && ./ncland_unit_tests nclan_cmd` → the parse test passes. Full suite `106 passed, 0 failed, 6 skipped`. Build the tool: `nmake -f ncland.mk` → `$(PBIN)/nclan-cmd` produced.

- [ ] **Step 6: Commit**
```bash
cd /home/dan/Git/netflex
git add cnc/ncland/src/nclan_cmd.cpp cnc/ncland/src/ncland.mk cnc/ncland/src/ncland_unit_tests.cpp
git commit -m "[ncland] nclan-cmd: CLI sender (neid-addressed CLAN2_MSG_CMD, prints response + verdict)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 9: Wire registry teardown + live test

**Files:** `ncland_warehouse.cpp`, memory

- [ ] **Step 1: Free the registry at shutdown.** In `warehouse_shutdown` (the teardown that already frees conns + calls `ncland_lua_shutdown`), after the conn-free loop and lua shutdown, add:
```c
    ncland_registry_free(&wh->registry);
```
Run the full suite (`./ncland_unit_tests` → still green) and a clean rebuild (`nmake -f ncland.mk` rc=0) to confirm no double-free at shutdown (the unit tests that build local `ne_entry_t` and `regfree` them directly are unaffected; only the live `wh->registry` map is freed here).

- [ ] **Step 2: Commit**
```bash
cd /home/dan/Git/netflex
git add cnc/ncland/src/ncland_warehouse.cpp
git commit -m "[ncland] free registry compiled regexes at warehouse shutdown

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

- [ ] **Step 3: Live test (manual, on lin07n).** Redeploy `ncland` + the new `nclan-cmd` to `/usr/cnc/bin`. With a logged-in PSS (neid 518):
```
nclan-cmd -n 518 -r 'show shelf'     # raw output, no verdict
nclan-cmd -n 518 'show shelf'        # prints RESULT: SUCCESS
```
Expect the daemon log to show the command written, prompt matched, response captured; `nclan-cmd` prints the NE output. Capture the log for review.

- [ ] **Step 4: Update memory.** In `/home/dan/.claude/projects/-home-dan-Git-netflex/memory/project_ncland_is_clan_rewrite.md`, note #3a done (commands config-driven + nclan-cmd), #3b (step engine) pending.

---

## Self-Review (against the spec)

**Spec coverage:**
- §2 registry compile (`more_re`/`error_res`) + free → Task 1. ✓
- §3 registry paging → Task 4. ✓
- §4 apply `adapted_primary_regex` → Task 5. ✓
- §5 errors→result, `check_errors` opt-out → Tasks 2 (fields) + 3 (scan) + folded into Task 6's unified match block. ✓
- §6 keepalive `ka_*` + `CS_KEEPALIVE` drain + liveness → Task 6. ✓
- §7 neid addressing + not-ready/busy FAILURE + `nclan-cmd` → Tasks 7 + 8. ✓
- §8 one-command-per-conn (busy→FAILURE), timeout result → Task 7 (busy) + the DENY path already carries result default; **note:** Task 6's match block sets `result` only on the delivered path — `check_expired_cmds`'s DENY rsp should also set `rsp.result = NCLAND_RESULT_FAILURE` (add that one line in Task 6 Step 5's vicinity / `check_expired_cmds`). **Added to Task 6.**
- §9 tests → Tasks 1–8 each include their tests. ✓
- §10 live test → Task 9. ✓
- §11 files → all mapped. ✓

**Gap fix (apply now):** add to `check_expired_cmds` (Task 6) the line `rsp.result = NCLAND_RESULT_FAILURE;` when it builds the DENY response, so a timed-out command reports FAILURE.

**Placeholder scan:** none — every code step shows complete code; the one judgment item (registry copy discipline) is documented with the rule, not left vague.

**Type consistency:** `NCLAND_RESULT_SUCCESS/FAILURE`, `CS_KEEPALIVE`, `ka_deadline`, `check_errors`, `neid`, `ncland_find_conn_by_neid`, `ncland_registry_free`, `g_test_last_rsp_result`, `g_test_last_direct_rsp_result`, `nclan_cmd_parse`/`nclan_cmd_opts` are used identically across tasks. `ncland_ne_write` (the #2-promoted name) is used in Tasks 4 & 6.

---

## Execution note

Tasks 1→9 are sequential. Task 9 Step 3 (live test) pauses for the user. Recommended: subagent-driven, two-stage review per task; fold the §8 `check_expired_cmds` result line into Task 6.
