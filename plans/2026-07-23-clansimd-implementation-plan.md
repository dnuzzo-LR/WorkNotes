# clansimd Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build `clansimd`, a single standalone daemon that hosts many lua-scripted network-element simulators, each reachable on its own TCP port, all started/stopped/inspected at runtime over a zmq REQ/REP control socket.

**Architecture:** Single-threaded epoll loop owns a zmq REP control socket, N per-sim TCP listeners, and M accepted client connections. Each running sim owns a private `lua_State`; each connection is a lua coroutine inside it. The daemon overrides `io.read`/`io.write`/`print`/`os.exit` per state so the *existing, unchanged* `.clansim` + `clansim.lua` scripts run verbatim: `io.read` yields the coroutine when the socket has no full line and resumes on `EPOLLIN`.

**Tech Stack:** C++17 (g++ 8.5, RHEL 8 baseline), Lua 5.3 (`-llua`), ZeroMQ (`-lzmq`), nlohmann::json header-only (`include/json.hpp`), `nflog.hpp` logging, `nfunit-test.hpp` unit tests, Lucent/AT&T **nmake** build. All new files live in `cnc/ncland/src`.

**Reference spec:** `~/WorkNotes/specs/2026-07-23-clansimd-simulator-design.md`

---

## Preconditions (verify once before starting)

- `echo $BASE` == `git rev-parse --show-toplevel` (must be `/home/dan/Git/netflex`).
- First colon-segment of `$VPATH` == `$BASE`.
- Build a target with: `nmake <target>` from `cnc/ncland/src`. If nmake fails on missing `globaldefs.nmk` / `global_*.nmk` / project headers, **STOP** and tell the user (do not reconstruct paths) — see project memory `feedback_vpath_halt`.
- Use **nmake**, never `make`/`gmake` (memory `feedback_use_nmake`). Makefile block comments use `/* */` (memory `feedback_nmake_c_comments`).

---

## File Structure

| File | Responsibility |
|---|---|
| `csim_types.h` | Shared structs/enums: `csim_conn`, `csim_instance`, `csim_daemon`, `csim_config`, pump/status enums. No logic. |
| `csim_control.h` / `.cpp` | Parse a JSON control request into a typed struct; format JSON replies. Pure functions, no I/O. |
| `csim_luabridge.h` / `.cpp` | Per-sim `lua_State` setup (`inc` shim, `io`/`print`/`os.exit` overrides), script load, connection coroutine open/feed/pump/close. |
| `csim_instance.h` / `.cpp` | Sim lifecycle: script-path resolution, start (listener), accept, stop, reset, reload, inject; per-sim overlay dir. |
| `csim_daemon.h` / `.cpp` | epoll loop, fd→sim routing, zmq REP socket, control dispatch. |
| `clansimd.cpp` | `main()`: parse args, build `csim_daemon`, run loop. |
| `csim_control_tests.cpp`, `csim_luabridge_tests.cpp`, `csim_instance_tests.cpp` | Unit tests (compiled into `ncland_unit_tests`). |
| `test/fixtures/clansim.lua`, `test/fixtures/sim/*.clansim`, `test/fixtures/lansim/...` | Test fixtures: a copy of `clansim.lua`, sample scripts, canned-response tree. |
| `test/integration/it_clansimd.sh` | End-to-end: launch daemon, START a sim over zmq, TCP-connect, assert responses, STOP. |
| `Makefile` | Add `clansimd` build target and test objects. |

**Naming:** symbols prefixed `csim_`, macros `CSIM_` (global instruction: component prefixes).

---

## Task 1: Build skeleton + Makefile target

**Files:**
- Create: `cnc/ncland/src/clansimd.cpp`
- Modify: `cnc/ncland/src/Makefile`

- [ ] **Step 1: Write the minimal entry point**

Create `cnc/ncland/src/clansimd.cpp`:

```cpp
// clansimd.cpp -- standalone lua NE-simulator daemon entry point.
#include "nflog.hpp"
#include <cstdio>
#include <cstring>

/**
 * @brief clansimd entry point (skeleton; real loop wired in Task 9).
 * @param argc Argument count.
 * @param argv Argument vector.
 * @return Process exit status.
 */
int main(int argc, char **argv)
{
    (void)argc; (void)argv;
    LOG_INFO("clansimd: starting (skeleton)");
    return 0;
}
```

- [ ] **Step 2: Add the build target to the Makefile**

In `cnc/ncland/src/Makefile`, add `clansimd` to `.MAIN` and add its recipe. Change line 31 from:

```
.MAIN : $(PBIN)/ncland $(PBIN)/nclan-seed
```
to:
```
.MAIN : $(PBIN)/ncland $(PBIN)/nclan-seed $(PBIN)/clansimd
```

Add after the `nclan-seed` recipe (after line 43). `CSIM_OBJS` is grown by later tasks; start it with just the entry object:

```
CSIM_OBJS = csim_control.o csim_luabridge.o csim_instance.o csim_daemon.o

$(PBIN)/clansimd :: clansimd.o $(CSIM_OBJS) $(INCLIBS) -latm -linc -lxmlparse -lssh -ljson -lnetclan
	$(CPLUS_CC) $(CCFLAGS:M!=-AC99) $(LDFLAGS) -o $(<) $(*) -lpthread -lssh -lzmq -lyaml-cpp -llua -lrt
```

> Note: the four `.o` in `CSIM_OBJS` are created in Tasks 2–9. Until they exist the link will fail; build only `clansimd.o` (`nmake clansimd.o`) to check compilation until Task 9. This matches how ncland grows its object list.

- [ ] **Step 3: Verify the entry object compiles**

Run: `nmake clansimd.o`
Expected: compiles cleanly, produces `clansimd.o`. (If nmake errors on missing globaldefs/headers, STOP per Preconditions.)

- [ ] **Step 4: Commit**

```bash
git add cnc/ncland/src/clansimd.cpp cnc/ncland/src/Makefile
git commit -m "clansimd: add daemon skeleton and nmake target"
```

---

## Task 2: Shared types header

**Files:**
- Create: `cnc/ncland/src/csim_types.h`

- [ ] **Step 1: Write the header**

Create `cnc/ncland/src/csim_types.h`:

```cpp
#ifndef CSIM_TYPES_H
#define CSIM_TYPES_H

#include <string>
#include <vector>
#include <unordered_map>

extern "C" { struct lua_State; }

/** @brief Result of pumping (resuming) a connection's lua coroutine. */
typedef enum {
    CSIM_PUMP_YIELD = 0,   /**< Coroutine yielded (blocked in io.read). */
    CSIM_PUMP_DONE  = 1,   /**< Script logged out / returned; close connection. */
    CSIM_PUMP_ERROR = -1   /**< Lua error; close connection. */
} csim_pump_t;

/** @brief Lifecycle state of a simulator instance. */
typedef enum {
    CSIM_IDLE      = 0,    /**< Created, no listener yet. */
    CSIM_LISTENING = 1,    /**< Listener open, no client. */
    CSIM_CONNECTED = 2,    /**< One client connected. */
    CSIM_ERR       = 3     /**< Failed state. */
} csim_status_t;

/** @brief Per-client-connection state (one active connection per sim). */
struct csim_conn {
    int          fd       = -1;      /**< Accepted client socket (nonblocking), -1 if idle. */
    int          coro_ref = -2;      /**< LUA_NOREF sentinel until set; ref to the coroutine. */
    lua_State   *co       = nullptr; /**< The connection's lua coroutine (thread of inst->L). */
    std::string  rbuf;               /**< Bytes read from client, consumed line-wise by io.read. */
    std::string  txbuf;              /**< Bytes queued by io.write/print, flushed after each pump. */
};

/** @brief One running simulator: its own lua_State plus at most one connection. */
struct csim_instance {
    int           id           = 0;    /**< Control-assigned sim id. */
    int           dtype        = -1;   /**< Device type, or -1 if started by explicit script. */
    int           neid         = 0;    /**< Network-element id (passed to scripts as arg[1]). */
    std::string   tid;                 /**< Target id (arg[2]). */
    std::string   script_path;         /**< Resolved .clansim path. */
    std::string   sim_data_root;       /**< Per-sim SIMDATA root (base + overlay for INJECT). */
    int           listen_fd    = -1;   /**< TCP listener fd. */
    int           port         = 0;    /**< Bound port. */
    lua_State    *L            = nullptr; /**< Per-sim interpreter (device state). */
    csim_conn     conn;                /**< Single active connection. */
    csim_status_t status       = CSIM_IDLE;
    bool          logout       = false; /**< Set by the os.exit override; read by the pump. */
    long          bytes_in     = 0;
    long          bytes_out    = 0;
    std::string   last_cmd;            /**< Last full line consumed by io.read. */
};

/** @brief Daemon-wide configuration. */
struct csim_config {
    std::vector<std::string> sim_dirs;      /**< Script search roots (START resolution order). */
    std::string              lua_lib_dir;   /**< Dir containing clansim.lua (added to package.path). */
    std::string              sim_data_root; /**< Base for canned-file tree + per-sim overlays. */
    int                      port_base = 20000; /**< Auto-port allocation base. */
    std::string              ctl_endpoint = "ipc:///tmp/clansimd.ctl"; /**< zmq REP bind endpoint. */
};

/** @brief Whole-daemon state. */
struct csim_daemon {
    int          epoll_fd = -1;
    void        *zmq_ctx  = nullptr;
    void        *rep_sock = nullptr;
    int          rep_fd   = -1;      /**< ZMQ_FD of rep_sock, registered in epoll. */
    int          next_id  = 1;
    int          next_port;          /**< Initialized from cfg.port_base. */
    csim_config  cfg;
    std::unordered_map<int, csim_instance*> sims;    /**< by id. */
    std::unordered_map<int, csim_instance*> by_fd;   /**< listener fd & conn fd -> owning sim. */
    bool         running = true;
};

#endif /* CSIM_TYPES_H */
```

- [ ] **Step 2: Verify it compiles standalone**

Create a throwaway check: `echo '#include "csim_types.h"' > /tmp/csim_hdr_check.cpp && g++ -std=c++17 -I. -Ithe-repo-include -fsyntax-only /tmp/csim_hdr_check.cpp` — but the canonical check is that Task 3 compiles against it. Skip a standalone compile; proceed.

- [ ] **Step 3: Commit**

```bash
git add cnc/ncland/src/csim_types.h
git commit -m "clansimd: add shared types header"
```

---

## Task 3: Control-message parse/format (TDD)

**Files:**
- Create: `cnc/ncland/src/csim_control.h`, `cnc/ncland/src/csim_control.cpp`
- Test: `cnc/ncland/src/csim_control_tests.cpp`

- [ ] **Step 1: Write the header**

Create `cnc/ncland/src/csim_control.h`:

```cpp
#ifndef CSIM_CONTROL_H
#define CSIM_CONTROL_H

#include <string>

/** @brief Control command verbs. */
typedef enum {
    CSIM_CMD_UNKNOWN = 0,
    CSIM_CMD_START,
    CSIM_CMD_STOP,
    CSIM_CMD_LIST,
    CSIM_CMD_STATUS,
    CSIM_CMD_RESET,
    CSIM_CMD_RELOAD,
    CSIM_CMD_INJECT
} csim_cmd_t;

/** @brief Parsed control request (union of all command args; unused fields default). */
struct csim_request {
    csim_cmd_t  cmd    = CSIM_CMD_UNKNOWN;
    std::string req_id;               /**< Echoed back in the reply. */
    bool        ok     = false;       /**< False if the envelope failed to parse. */
    std::string err;                  /**< Parse error text when ok == false. */
    /* args */
    int         dtype  = -1;
    int         neid   = 0;
    std::string tid;
    std::string script;               /**< Explicit script path (alt to dtype). */
    int         port   = 0;           /**< 0 == auto. */
    int         id     = 0;           /**< Target sim id for STOP/STATUS/RESET/RELOAD/INJECT. */
    std::string inj_cmd;              /**< INJECT: command string. */
    std::string inj_reply;            /**< INJECT: reply text. */
};

/**
 * @brief Parse a JSON control-request envelope into a typed struct.
 *
 * Envelope: {"v":1,"cmd":"START","args":{...},"req_id":"..."}. On any error
 * the returned struct has ok == false and err set; cmd is best-effort.
 *
 * @param json_text Raw request bytes.
 * @return Parsed request (never throws).
 */
csim_request csim_parse_request(const std::string &json_text);

/**
 * @brief Format a success reply: {"ok":true,"req_id":...,"result":<result_json>}.
 *
 * @param req_id      Request id to echo (may be empty).
 * @param result_json A JSON document string for the "result" field ("null" if none).
 * @return Serialized reply.
 */
std::string csim_format_ok(const std::string &req_id, const std::string &result_json);

/**
 * @brief Format an error reply: {"ok":false,"req_id":...,"error":<msg>}.
 *
 * @param req_id Request id to echo (may be empty).
 * @param msg    Human-readable error.
 * @return Serialized reply.
 */
std::string csim_format_err(const std::string &req_id, const std::string &msg);

#endif /* CSIM_CONTROL_H */
```

- [ ] **Step 2: Write the failing test**

Create `cnc/ncland/src/csim_control_tests.cpp`:

```cpp
#include "nfunit-test.hpp"
#include "csim_control.h"

TEST("CsimControl", "parses START with all args") {
    auto r = csim_parse_request(
        R"({"v":1,"cmd":"START","req_id":"abc",)"
        R"("args":{"dtype":153,"neid":7,"tid":"NE7","port":0}})");
    REQUIRE(r.ok);
    REQUIRE_EQ(r.cmd, CSIM_CMD_START);
    REQUIRE_EQ(r.req_id, std::string("abc"));
    REQUIRE_EQ(r.dtype, 153);
    REQUIRE_EQ(r.neid, 7);
    REQUIRE_EQ(r.tid, std::string("NE7"));
    REQUIRE_EQ(r.port, 0);
}

TEST("CsimControl", "parses STOP id") {
    auto r = csim_parse_request(R"({"v":1,"cmd":"STOP","args":{"id":4}})");
    REQUIRE(r.ok);
    REQUIRE_EQ(r.cmd, CSIM_CMD_STOP);
    REQUIRE_EQ(r.id, 4);
}

TEST("CsimControl", "parses INJECT") {
    auto r = csim_parse_request(
        R"({"cmd":"INJECT","args":{"id":2,"cmd":"RTRV-EQPT","reply":"OK\n"}})");
    REQUIRE(r.ok);
    REQUIRE_EQ(r.cmd, CSIM_CMD_INJECT);
    REQUIRE_EQ(r.id, 2);
    REQUIRE_EQ(r.inj_cmd, std::string("RTRV-EQPT"));
    REQUIRE_EQ(r.inj_reply, std::string("OK\n"));
}

TEST("CsimControl", "rejects malformed json") {
    auto r = csim_parse_request("{not json");
    REQUIRE(!r.ok);
    REQUIRE(!r.err.empty());
}

TEST("CsimControl", "rejects unknown cmd") {
    auto r = csim_parse_request(R"({"cmd":"FLY"})");
    REQUIRE(!r.ok);
    REQUIRE_EQ(r.cmd, CSIM_CMD_UNKNOWN);
}

TEST("CsimControl", "format_ok embeds result and echoes req_id") {
    std::string s = csim_format_ok("abc", R"({"id":7,"port":20007})");
    /* round-trip via a fresh parse is overkill; check substrings. */
    REQUIRE(s.find("\"ok\":true") != std::string::npos);
    REQUIRE(s.find("\"req_id\":\"abc\"") != std::string::npos);
    REQUIRE(s.find("20007") != std::string::npos);
}

TEST("CsimControl", "format_err sets ok false") {
    std::string s = csim_format_err("", "no such sim");
    REQUIRE(s.find("\"ok\":false") != std::string::npos);
    REQUIRE(s.find("no such sim") != std::string::npos);
}
```

- [ ] **Step 3: Register the test object in the Makefile**

In `cnc/ncland/src/Makefile`, add `csim_control_tests.o` and `csim_control.o` to the `ncland_unit_tests` target's object list (line 36), right after `ncland_test_qwrite.o`:

```
ncland_unit_tests :: ncland_unit_tests.o ncland_notify_parse_tests.o nclan_seed_fmt.o nclan_seed_tests.o ncland_registry_tests.o ncland_connpool_tests.o ncland_lua_tests.o ncland_stepper_tests.o ncland_mq_tests.o ncland_test_qwrite.o csim_control.o csim_control_tests.o $(NCLAND_OBJS) $(INCLIBS) -latm -linc -lxmlparse -lssh -ljson -lnetclan
```

- [ ] **Step 4: Run the test to verify it fails**

Run: `nmake ncland_unit_tests`
Expected: FAIL — link error, undefined reference to `csim_parse_request` / `csim_format_ok` / `csim_format_err` (implementation not written yet).

- [ ] **Step 5: Write the implementation**

Create `cnc/ncland/src/csim_control.cpp`:

```cpp
// csim_control.cpp -- JSON control-envelope parse/format for clansimd.
#include "csim_control.h"
#include "json.hpp"

using nlohmann::json;

/** @brief Map a verb string to csim_cmd_t (CSIM_CMD_UNKNOWN if unrecognized). */
static csim_cmd_t verb_of(const std::string &v)
{
    if (v == "START")  return CSIM_CMD_START;
    if (v == "STOP")   return CSIM_CMD_STOP;
    if (v == "LIST")   return CSIM_CMD_LIST;
    if (v == "STATUS") return CSIM_CMD_STATUS;
    if (v == "RESET")  return CSIM_CMD_RESET;
    if (v == "RELOAD") return CSIM_CMD_RELOAD;
    if (v == "INJECT") return CSIM_CMD_INJECT;
    return CSIM_CMD_UNKNOWN;
}

csim_request csim_parse_request(const std::string &json_text)
{
    csim_request r;
    json root = json::parse(json_text, nullptr, /*allow_exceptions=*/false);
    if (root.is_discarded()) { r.err = "malformed JSON"; return r; }

    if (root.contains("req_id") && root["req_id"].is_string())
        r.req_id = root["req_id"].get<std::string>();

    if (!root.contains("cmd") || !root["cmd"].is_string()) {
        r.err = "missing cmd"; return r;
    }
    r.cmd = verb_of(root["cmd"].get<std::string>());
    if (r.cmd == CSIM_CMD_UNKNOWN) { r.err = "unknown cmd"; return r; }

    const json a = root.contains("args") && root["args"].is_object()
                       ? root["args"] : json::object();

    auto get_int = [&](const char *k, int def) -> int {
        return (a.contains(k) && a[k].is_number_integer()) ? a[k].get<int>() : def;
    };
    auto get_str = [&](const char *k) -> std::string {
        return (a.contains(k) && a[k].is_string()) ? a[k].get<std::string>() : std::string();
    };

    r.dtype     = get_int("dtype", -1);
    r.neid      = get_int("neid", 0);
    r.tid       = get_str("tid");
    r.script    = get_str("script");
    r.port      = get_int("port", 0);
    r.id        = get_int("id", 0);
    r.inj_cmd   = get_str("cmd");     /* INJECT nests a "cmd" inside args */
    r.inj_reply = get_str("reply");
    r.ok        = true;
    return r;
}

std::string csim_format_ok(const std::string &req_id, const std::string &result_json)
{
    json out;
    out["ok"] = true;
    if (!req_id.empty()) out["req_id"] = req_id;
    out["result"] = json::parse(result_json, nullptr, false);
    if (out["result"].is_discarded()) out["result"] = nullptr;
    return out.dump();
}

std::string csim_format_err(const std::string &req_id, const std::string &msg)
{
    json out;
    out["ok"] = false;
    if (!req_id.empty()) out["req_id"] = req_id;
    out["error"] = msg;
    return out.dump();
}
```

- [ ] **Step 6: Run the tests to verify they pass**

Run: `nmake ncland_unit_tests && ./ncland_unit_tests -s CsimControl`
Expected: PASS — all 7 `CsimControl` tests green.

- [ ] **Step 7: Commit**

```bash
git add cnc/ncland/src/csim_control.h cnc/ncland/src/csim_control.cpp cnc/ncland/src/csim_control_tests.cpp cnc/ncland/src/Makefile
git commit -m "clansimd: JSON control envelope parse/format with tests"
```

---

## Task 4: Script-path resolution (TDD)

Resolves START's script using clan's order: `ne<neid>.clansim` → `<dtype>.clansim` → `def.<dtype>.clansim`, searched across `cfg.sim_dirs`.

**Files:**
- Create: `cnc/ncland/src/csim_instance.h`, `cnc/ncland/src/csim_instance.cpp`
- Modify: `cnc/ncland/src/csim_instance_tests.cpp` (created here)

- [ ] **Step 1: Write the header (resolution API only; lifecycle added in later tasks)**

Create `cnc/ncland/src/csim_instance.h`:

```cpp
#ifndef CSIM_INSTANCE_H
#define CSIM_INSTANCE_H

#include "csim_types.h"
#include <string>
#include <vector>

/**
 * @brief Resolve a .clansim script path for (neid, dtype) using clan's order.
 *
 * Search order within each dir in @p sim_dirs (dirs tried in list order):
 *   1. ne<neid>.clansim
 *   2. <dtype>.clansim
 *   3. def.<dtype>.clansim
 * The first existing, readable file wins.
 *
 * @param sim_dirs Ordered script search roots.
 * @param neid     Network-element id.
 * @param dtype    Device type (ignored for the ne<neid> form; <0 skips dtype forms).
 * @param out      Set to the resolved absolute/relative path on success.
 * @return true if a script was found, false otherwise.
 */
bool csim_resolve_script(const std::vector<std::string> &sim_dirs,
                         int neid, int dtype, std::string &out);

#endif /* CSIM_INSTANCE_H */
```

- [ ] **Step 2: Write the failing test**

Create `cnc/ncland/src/csim_instance_tests.cpp`:

```cpp
#include "nfunit-test.hpp"
#include "csim_instance.h"
#include <cstdio>
#include <cstdlib>
#include <string>
#include <sys/stat.h>
#include <unistd.h>

/* Build a unique temp dir under /tmp for fixture files. */
static std::string mktmpdir() {
    char tmpl[] = "/tmp/csim_test_XXXXXX";
    char *d = mkdtemp(tmpl);
    return std::string(d ? d : "/tmp");
}
static void touch(const std::string &path) {
    FILE *f = fopen(path.c_str(), "w"); if (f) fclose(f);
}

TEST("CsimResolve", "prefers ne<neid> over dtype") {
    std::string dir = mktmpdir();
    touch(dir + "/ne7.clansim");
    touch(dir + "/153.clansim");
    std::string out;
    REQUIRE(csim_resolve_script({dir}, 7, 153, out));
    REQUIRE_EQ(out, dir + "/ne7.clansim");
}

TEST("CsimResolve", "falls back to dtype then def") {
    std::string dir = mktmpdir();
    touch(dir + "/def.153.clansim");
    std::string out;
    REQUIRE(csim_resolve_script({dir}, 7, 153, out));
    REQUIRE_EQ(out, dir + "/def.153.clansim");
}

TEST("CsimResolve", "searches dirs in order") {
    std::string a = mktmpdir(), b = mktmpdir();
    touch(b + "/153.clansim");
    std::string out;
    REQUIRE(csim_resolve_script({a, b}, 7, 153, out));
    REQUIRE_EQ(out, b + "/153.clansim");
}

TEST("CsimResolve", "returns false when nothing found") {
    std::string dir = mktmpdir();
    std::string out;
    REQUIRE(!csim_resolve_script({dir}, 7, 153, out));
}
```

- [ ] **Step 3: Register objects in the Makefile**

Add `csim_instance.o` and `csim_instance_tests.o` to the `ncland_unit_tests` object list (line 36), after `csim_control_tests.o`.

- [ ] **Step 4: Run to verify failure**

Run: `nmake ncland_unit_tests`
Expected: FAIL — undefined reference to `csim_resolve_script`.

- [ ] **Step 5: Write the implementation**

Create `cnc/ncland/src/csim_instance.cpp` (lifecycle functions appended in Tasks 6–8; this step adds only resolution + includes it will need later):

```cpp
// csim_instance.cpp -- simulator instance lifecycle + script resolution.
#include "csim_instance.h"
#include "nflog.hpp"
#include <unistd.h>
#include <cstdio>

/** @brief True if @p path exists and is readable. */
static bool readable(const std::string &path) {
    return access(path.c_str(), R_OK) == 0;
}

bool csim_resolve_script(const std::vector<std::string> &sim_dirs,
                         int neid, int dtype, std::string &out)
{
    char buf[64];
    for (const std::string &dir : sim_dirs) {
        std::snprintf(buf, sizeof(buf), "/ne%d.clansim", neid);
        std::string p = dir + buf;
        if (readable(p)) { out = p; return true; }

        if (dtype >= 0) {
            std::snprintf(buf, sizeof(buf), "/%d.clansim", dtype);
            p = dir + buf;
            if (readable(p)) { out = p; return true; }

            std::snprintf(buf, sizeof(buf), "/def.%d.clansim", dtype);
            p = dir + buf;
            if (readable(p)) { out = p; return true; }
        }
    }
    return false;
}
```

- [ ] **Step 6: Run to verify pass**

Run: `nmake ncland_unit_tests && ./ncland_unit_tests -s CsimResolve`
Expected: PASS — 4 tests green.

- [ ] **Step 7: Commit**

```bash
git add cnc/ncland/src/csim_instance.h cnc/ncland/src/csim_instance.cpp cnc/ncland/src/csim_instance_tests.cpp cnc/ncland/src/Makefile
git commit -m "clansimd: script-path resolution with tests"
```

---

## Task 5: Lua bridge — state setup + shims (TDD)

Build a per-sim `lua_State` that can `require('clansim')`, with the `inc` shim and `io`/`print`/`os.exit` overrides installed. This task proves the state builds and the shims exist; the coroutine drive is Task 6.

**Files:**
- Create: `cnc/ncland/src/csim_luabridge.h`, `cnc/ncland/src/csim_luabridge.cpp`
- Create fixtures: `cnc/ncland/src/test/fixtures/clansim.lua` (copy), `cnc/ncland/src/test/fixtures/sim/echo.clansim`
- Test: `cnc/ncland/src/csim_luabridge_tests.cpp`

- [ ] **Step 1: Create fixtures**

Copy the real shared library verbatim:

```bash
cp /home/dan/Git/netflex/3b2/data/clansim.lua cnc/ncland/src/test/fixtures/clansim.lua
```

Create a minimal self-contained sim script `cnc/ncland/src/test/fixtures/sim/echo.clansim` that does NOT depend on the canned-file tree (so the bridge can be tested in isolation). It logs in with one prompt, then echoes each command:

```lua
#!/usr/cnc/bin/ilua
-- echo.clansim: minimal test sim. Prompts once, then echoes commands.
-- arg[1]=neid, arg[2]=tid (set by the daemon before the chunk runs).
io.write('login: ')
local u = io.read('*l')          -- consume the username line
io.write('prompt> ')
while true do
    local s = io.read('*l')
    if s == nil or s == 'logout' then os.exit(0) end
    io.write('you said: ' .. s .. '\n')
    io.write('prompt> ')
end
```

- [ ] **Step 2: Write the header**

Create `cnc/ncland/src/csim_luabridge.h`:

```cpp
#ifndef CSIM_LUABRIDGE_H
#define CSIM_LUABRIDGE_H

#include "csim_types.h"
#include <string>

/**
 * @brief Create and configure a per-sim lua_State for @p inst.
 *
 * Opens stdlib, adds @p lua_lib_dir and the script's directory to package.path,
 * installs the `inc` shim (app_setup/trace/sysdef) and overrides io.read /
 * io.write / print / os.exit so unchanged .clansim scripts run against the
 * connection buffers. Stashes @p inst in the state's extraspace. On success
 * inst->L is set.
 *
 * @param inst        Instance to attach the state to (fields id/neid/tid/sim_data_root used).
 * @param lua_lib_dir Directory containing clansim.lua (added to package.path).
 * @return 0 on success, -1 on failure (inst->L left null).
 */
int csim_lua_open_state(csim_instance *inst, const std::string &lua_lib_dir);

/**
 * @brief Destroy inst->L (if any) and clear the connection coroutine handle.
 *
 * @param inst Instance whose lua_State is closed.
 */
void csim_lua_close_state(csim_instance *inst);

/**
 * @brief Open a fresh connection coroutine: load inst->script_path as the
 * coroutine body, set the global `arg`={neid,tid}, and pump once.
 *
 * The first pump runs the script until its first io.read yield, emitting the
 * initial prompt into inst->conn.txbuf.
 *
 * @param inst Instance with a live L, a resolved script_path, and conn.fd set
 *             (fd may be -1 in unit tests that drive buffers directly).
 * @return CSIM_PUMP_YIELD on the normal first-yield, CSIM_PUMP_DONE/ERROR otherwise.
 */
csim_pump_t csim_conn_open(csim_instance *inst);

/**
 * @brief Append bytes to the connection read buffer and resume the coroutine.
 *
 * @param inst  Instance with an open connection coroutine.
 * @param data  Bytes received from the client.
 * @param len   Byte count.
 * @return Pump result (YIELD = still waiting for more input; DONE/ERROR = close).
 */
csim_pump_t csim_conn_feed(csim_instance *inst, const char *data, size_t len);

/**
 * @brief Release the connection coroutine (unref) and clear conn buffers.
 *
 * Leaves inst->L intact (sim keeps its device state; reconnectable).
 *
 * @param inst Instance whose connection is being torn down.
 */
void csim_conn_close(csim_instance *inst);

#endif /* CSIM_LUABRIDGE_H */
```

- [ ] **Step 3: Write the failing test (state build only)**

Create `cnc/ncland/src/csim_luabridge_tests.cpp`:

```cpp
#include "nfunit-test.hpp"
#include "csim_luabridge.h"
#include "csim_types.h"
#include <string>

/* Fixtures live relative to the test binary's CWD (cnc/ncland/src). */
static const char *FIX_LIB = "test/fixtures";
static const char *FIX_SIM = "test/fixtures/sim/echo.clansim";

TEST("CsimLua", "open_state builds a state that can require clansim") {
    csim_instance inst;
    inst.neid = 7; inst.tid = "NE7"; inst.sim_data_root = "/tmp";
    REQUIRE_EQ(csim_lua_open_state(&inst, FIX_LIB), 0);
    REQUIRE(inst.L != nullptr);
    csim_lua_close_state(&inst);
    REQUIRE(inst.L == nullptr);
}
```

- [ ] **Step 4: Register objects + run to verify failure**

Add `csim_luabridge.o` and `csim_luabridge_tests.o` to the `ncland_unit_tests` object list (line 36).

Run: `nmake ncland_unit_tests`
Expected: FAIL — undefined reference to `csim_lua_open_state` / `csim_lua_close_state`.

- [ ] **Step 5: Write the implementation (state + shims; coroutine funcs stubbed to compile)**

Create `cnc/ncland/src/csim_luabridge.cpp`:

```cpp
// csim_luabridge.cpp -- per-sim lua_State setup + connection coroutine drive.
//
// Runs unchanged .clansim / clansim.lua scripts by overriding io.read (yields
// on empty socket), io.write/print (buffer to txbuf), and os.exit (logout,
// never kills the daemon). Mirrors the coroutine/yieldk pattern in
// ncland_lua.cpp (see l_session_expect / expect_k).
#include "csim_luabridge.h"
#include "nflog.hpp"

extern "C" {
#include <lua.h>
#include <lauxlib.h>
#include <lualib.h>
}

#include <cstring>
#include <string>

/* Retrieve the instance stashed in the state's (global) extraspace. Works from
 * the main state or any of its coroutines — extraspace is per global_State. */
static csim_instance *inst_of(lua_State *L) {
    return *reinterpret_cast<csim_instance **>(lua_getextraspace(L));
}

/* ---- inc.* shim -------------------------------------------------------- */

/** @brief inc.app_setup(name): log the sim's app name; no-op otherwise. */
static int l_inc_app_setup(lua_State *L) {
    const char *name = luaL_optstring(L, 1, "clansim");
    LOG_INFO("clansim app_setup: %s", name);
    return 0;
}
/** @brief inc.trace(level, msg): route script traces to nflog at DEBUG. */
static int l_inc_trace(lua_State *L) {
    lua_Integer lvl = luaL_optinteger(L, 1, 0);
    const char *msg = luaL_optstring(L, 2, "");
    LOG_DEBUG("clansim trace[%d]: %s", (int)lvl, msg);
    return 0;
}
/** @brief inc.sysdef(key, default): return this sim's SIMDATA root for
 *  'SIMDATA=', else the provided default. */
static int l_inc_sysdef(lua_State *L) {
    const char *key = luaL_optstring(L, 1, "");
    const char *def = luaL_optstring(L, 2, "");
    csim_instance *inst = inst_of(L);
    if (std::strcmp(key, "SIMDATA=") == 0 && inst && !inst->sim_data_root.empty())
        lua_pushstring(L, inst->sim_data_root.c_str());
    else
        lua_pushstring(L, def);
    return 1;
}

/* ---- io / print / os.exit overrides ------------------------------------ */

static int io_read_k(lua_State *L, int status, lua_KContext ctx);

/* io.read("*l"): return a buffered line, or yield until one arrives. Only line
 * mode is supported (all .clansim scripts use io.read('*l')). */
static int l_io_read(lua_State *L) {
    return io_read_k(L, 0, 0);   /* first attempt shares the continuation body */
}
static int io_read_k(lua_State *L, int status, lua_KContext ctx) {
    (void)status; (void)ctx;
    csim_instance *inst = inst_of(L);
    std::string &rb = inst->conn.rbuf;
    size_t nl = rb.find('\n');
    if (nl != std::string::npos) {
        std::string line = rb.substr(0, nl);
        /* strip a trailing CR (telnet sends CRLF). */
        if (!line.empty() && line.back() == '\r') line.pop_back();
        rb.erase(0, nl + 1);
        inst->last_cmd = line;
        lua_pushlstring(L, line.data(), line.size());
        return 1;
    }
    /* No full line yet: yield; the daemon resumes us after more bytes arrive. */
    return lua_yieldk(L, 0, 0, io_read_k);
}

/* io.write(...): append all string/number args to txbuf. */
static int l_io_write(lua_State *L) {
    csim_instance *inst = inst_of(L);
    int n = lua_gettop(L);
    for (int i = 1; i <= n; i++) {
        size_t l = 0;
        const char *s = luaL_tolstring(L, i, &l);   /* accepts strings & numbers */
        inst->conn.txbuf.append(s, l);
        lua_pop(L, 1);
    }
    return 0;
}

/* print(...): tab-separated args + newline into txbuf (stock print semantics). */
static int l_print(lua_State *L) {
    csim_instance *inst = inst_of(L);
    int n = lua_gettop(L);
    for (int i = 1; i <= n; i++) {
        size_t l = 0;
        const char *s = luaL_tolstring(L, i, &l);
        if (i > 1) inst->conn.txbuf.push_back('\t');
        inst->conn.txbuf.append(s, l);
        lua_pop(L, 1);
    }
    inst->conn.txbuf.push_back('\n');
    return 0;
}

/* os.exit([code]): request logout; raise a sentinel error to unwind the
 * coroutine. NEVER exits the daemon process. */
static int l_os_exit(lua_State *L) {
    csim_instance *inst = inst_of(L);
    inst->logout = true;
    return luaL_error(L, "__csim_logout__");
}

/* Install overrides into an already-opened standard-lib state. */
static void install_overrides(lua_State *L) {
    /* inc = { app_setup=, trace=, sysdef= } */
    lua_newtable(L);
    lua_pushcfunction(L, l_inc_app_setup); lua_setfield(L, -2, "app_setup");
    lua_pushcfunction(L, l_inc_trace);     lua_setfield(L, -2, "trace");
    lua_pushcfunction(L, l_inc_sysdef);    lua_setfield(L, -2, "sysdef");
    lua_setglobal(L, "inc");

    /* print */
    lua_pushcfunction(L, l_print); lua_setglobal(L, "print");

    /* io.read / io.write */
    lua_getglobal(L, "io");
    lua_pushcfunction(L, l_io_read);  lua_setfield(L, -2, "read");
    lua_pushcfunction(L, l_io_write); lua_setfield(L, -2, "write");
    lua_pop(L, 1);

    /* os.exit */
    lua_getglobal(L, "os");
    lua_pushcfunction(L, l_os_exit); lua_setfield(L, -2, "exit");
    lua_pop(L, 1);
}

/* Append a directory to package.path as "<dir>/?.lua". */
static void add_package_path(lua_State *L, const std::string &dir) {
    lua_getglobal(L, "package");
    lua_getfield(L, -1, "path");
    std::string cur = lua_tostring(L, -1) ? lua_tostring(L, -1) : "";
    cur += ";" + dir + "/?.lua";
    lua_pop(L, 1);
    lua_pushlstring(L, cur.data(), cur.size());
    lua_setfield(L, -2, "path");
    lua_pop(L, 1);
}

int csim_lua_open_state(csim_instance *inst, const std::string &lua_lib_dir)
{
    if (!inst) return -1;
    lua_State *L = luaL_newstate();
    if (!L) { LOG_ERROR("csim_lua_open_state: luaL_newstate failed"); return -1; }
    luaL_openlibs(L);
    *reinterpret_cast<csim_instance **>(lua_getextraspace(L)) = inst;
    add_package_path(L, lua_lib_dir);
    install_overrides(L);
    inst->L = L;
    LOG_INFO("csim sim id=%d neid=%d: lua state ready (%s)", inst->id, inst->neid, LUA_RELEASE);
    return 0;
}

void csim_lua_close_state(csim_instance *inst)
{
    if (!inst || !inst->L) return;
    lua_close(inst->L);          /* frees coroutines/refs too */
    inst->L = nullptr;
    inst->conn.co = nullptr;
    inst->conn.coro_ref = -2;
}

/* Coroutine open/feed/close implemented in Task 6. Provide temporary stubs so
 * this task links; REPLACE them in Task 6. */
csim_pump_t csim_conn_open(csim_instance *)              { return CSIM_PUMP_YIELD; }
csim_pump_t csim_conn_feed(csim_instance *, const char *, size_t) { return CSIM_PUMP_YIELD; }
void        csim_conn_close(csim_instance *)             {}
```

- [ ] **Step 6: Run to verify pass**

Run: `nmake ncland_unit_tests && ./ncland_unit_tests -s CsimLua`
Expected: PASS — the `open_state builds a state that can require clansim` test is green.

- [ ] **Step 7: Commit**

```bash
git add cnc/ncland/src/csim_luabridge.h cnc/ncland/src/csim_luabridge.cpp cnc/ncland/src/csim_luabridge_tests.cpp cnc/ncland/src/test/fixtures cnc/ncland/src/Makefile
git commit -m "clansimd: lua state setup with inc/io/print/os.exit shims"
```

---

## Task 6: Lua bridge — connection coroutine drive (TDD)

Replace the Task-5 stubs with the real coroutine open/feed/pump/close. This is the "unit-test the sims" seam: tests drive `csim_conn_open`/`csim_conn_feed` against the buffers with **no socket**.

**Files:**
- Modify: `cnc/ncland/src/csim_luabridge.cpp` (replace stubs)
- Modify: `cnc/ncland/src/csim_luabridge_tests.cpp` (add drive tests)

- [ ] **Step 1: Write the failing tests**

Append to `cnc/ncland/src/csim_luabridge_tests.cpp`:

```cpp
/* Helper: open a sim on echo.clansim with no socket, return its instance. */
static void open_echo(csim_instance &inst) {
    inst.neid = 7; inst.tid = "NE7"; inst.sim_data_root = "/tmp";
    inst.script_path = FIX_SIM;
    REQUIRE_EQ(csim_lua_open_state(&inst, FIX_LIB), 0);
}

TEST("CsimLua", "first pump emits the initial prompt and yields") {
    csim_instance inst;
    open_echo(inst);
    csim_pump_t r = csim_conn_open(&inst);
    REQUIRE_EQ(r, CSIM_PUMP_YIELD);
    /* echo.clansim writes 'login: ' then reads -> yields. */
    REQUIRE_EQ(inst.conn.txbuf, std::string("login: "));
    csim_lua_close_state(&inst);
}

TEST("CsimLua", "feeding a line drives login then command echo") {
    csim_instance inst;
    open_echo(inst);
    csim_conn_open(&inst);
    inst.conn.txbuf.clear();

    /* Send username -> script writes 'prompt> ' and yields. */
    csim_pump_t r = csim_conn_feed(&inst, "admin\n", 6);
    REQUIRE_EQ(r, CSIM_PUMP_YIELD);
    REQUIRE_EQ(inst.conn.txbuf, std::string("prompt> "));
    inst.conn.txbuf.clear();

    /* Send a command -> echoed with a fresh prompt. */
    r = csim_conn_feed(&inst, "hello world\n", 12);
    REQUIRE_EQ(r, CSIM_PUMP_YIELD);
    REQUIRE_EQ(inst.conn.txbuf, std::string("you said: hello world\nprompt> "));
    REQUIRE_EQ(inst.last_cmd, std::string("hello world"));
    csim_lua_close_state(&inst);
}

TEST("CsimLua", "partial line does not resume until newline arrives") {
    csim_instance inst;
    open_echo(inst);
    csim_conn_open(&inst);
    csim_conn_feed(&inst, "admin\n", 6);
    inst.conn.txbuf.clear();

    csim_pump_t r = csim_conn_feed(&inst, "par", 3);   /* no newline yet */
    REQUIRE_EQ(r, CSIM_PUMP_YIELD);
    REQUIRE(inst.conn.txbuf.empty());                  /* still blocked in io.read */

    r = csim_conn_feed(&inst, "tial\n", 5);
    REQUIRE_EQ(r, CSIM_PUMP_YIELD);
    REQUIRE_EQ(inst.conn.txbuf, std::string("you said: partial\nprompt> "));
    csim_lua_close_state(&inst);
}

TEST("CsimLua", "logout closes cleanly without killing the process") {
    csim_instance inst;
    open_echo(inst);
    csim_conn_open(&inst);
    csim_conn_feed(&inst, "admin\n", 6);
    csim_pump_t r = csim_conn_feed(&inst, "logout\n", 7);
    REQUIRE_EQ(r, CSIM_PUMP_DONE);       /* os.exit override -> clean logout */
    REQUIRE(inst.logout);
    csim_lua_close_state(&inst);
    /* Reaching here proves the daemon/test process survived os.exit. */
    REQUIRE(true);
}
```

- [ ] **Step 2: Run to verify failure**

Run: `nmake ncland_unit_tests && ./ncland_unit_tests -s CsimLua`
Expected: FAIL — the four new tests fail (stubs return YIELD with empty txbuf / never emit output).

- [ ] **Step 3: Replace the stubs with the real implementation**

In `cnc/ncland/src/csim_luabridge.cpp`, delete the three stub definitions at the bottom and replace with:

```cpp
/* Drive the coroutine once; flush nothing here (caller/daemon flushes txbuf to
 * the socket). Maps lua_resume status to csim_pump_t, honoring the os.exit
 * logout sentinel. */
static csim_pump_t pump(csim_instance *inst, int narg)
{
    lua_State *co = inst->conn.co;
    int st = lua_resume(co, inst->L, narg);
    if (st == LUA_YIELD) return CSIM_PUMP_YIELD;
    if (st == LUA_OK)    return CSIM_PUMP_DONE;   /* script returned (rare). */
    /* Error path: distinguish the intentional logout sentinel from real errors. */
    if (inst->logout) return CSIM_PUMP_DONE;
    const char *e = lua_tostring(co, -1);
    LOG_WARN("csim sim id=%d neid=%d: lua error: %s",
             inst->id, inst->neid, e ? e : "(error)");
    return CSIM_PUMP_ERROR;
}

csim_pump_t csim_conn_open(csim_instance *inst)
{
    if (!inst || !inst->L) return CSIM_PUMP_ERROR;

    /* Set global arg = { [1]=neid (as string, matching ilua argv), [2]=tid }. */
    lua_newtable(inst->L);
    char nbuf[32]; std::snprintf(nbuf, sizeof(nbuf), "%d", inst->neid);
    lua_pushstring(inst->L, nbuf);        lua_rawseti(inst->L, -2, 1);
    lua_pushstring(inst->L, inst->tid.c_str()); lua_rawseti(inst->L, -2, 2);
    lua_setglobal(inst->L, "arg");

    /* Load the script chunk onto L. */
    if (luaL_loadfile(inst->L, inst->script_path.c_str()) != LUA_OK) {
        LOG_ERROR("csim: load %s failed: %s", inst->script_path.c_str(),
                  lua_tostring(inst->L, -1));
        lua_pop(inst->L, 1);
        return CSIM_PUMP_ERROR;
    }
    /* Create the coroutine and move the chunk into it. */
    inst->conn.co = lua_newthread(inst->L);
    inst->conn.coro_ref = luaL_ref(inst->L, LUA_REGISTRYINDEX);  /* pops the thread */
    lua_xmove(inst->L, inst->conn.co, 1);                        /* move chunk fn */
    inst->logout = false;
    return pump(inst, 0);
}

csim_pump_t csim_conn_feed(csim_instance *inst, const char *data, size_t len)
{
    if (!inst || !inst->conn.co) return CSIM_PUMP_ERROR;
    inst->conn.rbuf.append(data, len);
    inst->bytes_in += (long)len;
    return pump(inst, 0);
}

void csim_conn_close(csim_instance *inst)
{
    if (!inst) return;
    if (inst->conn.coro_ref != -2 && inst->L)
        luaL_unref(inst->L, LUA_REGISTRYINDEX, inst->conn.coro_ref);
    inst->conn.co = nullptr;
    inst->conn.coro_ref = -2;
    inst->conn.rbuf.clear();
    inst->conn.txbuf.clear();
    if (inst->conn.fd >= 0) inst->conn.fd = -1;
    inst->logout = false;
}
```

- [ ] **Step 4: Run to verify pass**

Run: `nmake ncland_unit_tests && ./ncland_unit_tests -s CsimLua`
Expected: PASS — all `CsimLua` tests green (initial prompt, login+echo, partial-line, logout).

- [ ] **Step 5: Commit**

```bash
git add cnc/ncland/src/csim_luabridge.cpp cnc/ncland/src/csim_luabridge_tests.cpp
git commit -m "clansimd: connection coroutine drive (open/feed/pump/close)"
```

---

## Task 7: Instance lifecycle — start/stop + TCP listener (TDD where practical)

Add sim start (create state + overlay dir + TCP listener), accept, and stop. Port binding is real; a unit test binds on an ephemeral port and connects a client socket end-to-end through the coroutine.

**Files:**
- Modify: `cnc/ncland/src/csim_instance.h`, `cnc/ncland/src/csim_instance.cpp`
- Modify: `cnc/ncland/src/csim_instance_tests.cpp`

- [ ] **Step 1: Extend the header**

Append to `cnc/ncland/src/csim_instance.h` (before `#endif`):

```cpp
#include "csim_luabridge.h"

/**
 * @brief Create the per-sim overlay data dir <sim_data_root>. Idempotent.
 * @param inst Instance whose sim_data_root is created (mkdir -p semantics).
 * @return 0 on success, -1 on failure.
 */
int csim_instance_make_datadir(csim_instance *inst);

/**
 * @brief Open a listening TCP socket on inst->port (0 = OS-assigned) bound to
 * 127.0.0.1, set nonblocking. Sets inst->listen_fd, inst->port, status.
 *
 * @param inst Instance to open a listener for.
 * @return listen_fd on success, -1 on failure.
 */
int csim_instance_listen(csim_instance *inst);

/**
 * @brief Accept one pending client on inst->listen_fd, open its coroutine, and
 * flush the initial prompt. Rejects (closes) a second client while one is active.
 *
 * @param inst Instance whose listener is readable.
 * @return 0 on a successful accept, -1 on error / rejection.
 */
int csim_instance_accept(csim_instance *inst);

/**
 * @brief Read available bytes from the connection, feed the coroutine, flush
 * any response to the socket. Closes the connection on pump DONE/ERROR/EOF.
 *
 * @param inst Instance whose connection fd is readable.
 * @return 0 normally; 1 if the connection was closed.
 */
int csim_instance_on_readable(csim_instance *inst);

/**
 * @brief Stop a sim: close connection, listener, lua state, remove overlay dir.
 * @param inst Instance to stop (safe to call repeatedly).
 */
void csim_instance_stop(csim_instance *inst);
```

- [ ] **Step 2: Write the failing end-to-end test**

Append to `cnc/ncland/src/csim_instance_tests.cpp`:

```cpp
#include "csim_luabridge.h"
#include <arpa/inet.h>
#include <sys/socket.h>
#include <netinet/in.h>
#include <fcntl.h>
#include <string>

/* Connect a blocking client to 127.0.0.1:port; return the fd (or -1). */
static int dial(int port) {
    int fd = socket(AF_INET, SOCK_STREAM, 0);
    struct sockaddr_in a; std::memset(&a, 0, sizeof(a));
    a.sin_family = AF_INET; a.sin_port = htons((uint16_t)port);
    inet_pton(AF_INET, "127.0.0.1", &a.sin_addr);
    if (connect(fd, (struct sockaddr *)&a, sizeof(a)) != 0) { close(fd); return -1; }
    return fd;
}
static std::string slurp(int fd, size_t want) {
    std::string out; char b[256];
    while (out.size() < want) {
        int n = (int)recv(fd, b, sizeof(b), 0);
        if (n <= 0) break;
        out.append(b, n);
    }
    return out;
}

TEST("CsimInstance", "listen + accept + echo over a real socket") {
    csim_instance inst;
    inst.neid = 7; inst.tid = "NE7"; inst.sim_data_root = "/tmp";
    inst.script_path = "test/fixtures/sim/echo.clansim";
    REQUIRE_EQ(csim_lua_open_state(&inst, "test/fixtures"), 0);
    inst.port = 0;
    REQUIRE(csim_instance_listen(&inst) >= 0);
    REQUIRE(inst.port > 0);

    int cfd = dial(inst.port);
    REQUIRE(cfd >= 0);
    REQUIRE_EQ(csim_instance_accept(&inst), 0);

    /* Initial prompt was flushed on accept. */
    REQUIRE_EQ(slurp(cfd, 7), std::string("login: "));

    /* Login line. */
    send(cfd, "admin\n", 6, 0);
    csim_instance_on_readable(&inst);
    REQUIRE_EQ(slurp(cfd, 8), std::string("prompt> "));

    /* Command. */
    send(cfd, "ping\n", 5, 0);
    csim_instance_on_readable(&inst);
    REQUIRE_EQ(slurp(cfd, 10), std::string("you said: ping\nprompt> ").substr(0, 10));

    close(cfd);
    csim_instance_stop(&inst);
}
```

- [ ] **Step 3: Run to verify failure**

Run: `nmake ncland_unit_tests && ./ncland_unit_tests -s CsimInstance -t "listen + accept + echo over a real socket"`
Expected: FAIL — undefined references to `csim_instance_listen`/`_accept`/`_on_readable`/`_stop`.

- [ ] **Step 4: Implement lifecycle in csim_instance.cpp**

Append to `cnc/ncland/src/csim_instance.cpp` (add includes at top: `<sys/socket.h>`, `<netinet/in.h>`, `<arpa/inet.h>`, `<fcntl.h>`, `<sys/stat.h>`, `<cerrno>`, `<cstring>`):

```cpp
#include <sys/socket.h>
#include <netinet/in.h>
#include <arpa/inet.h>
#include <fcntl.h>
#include <sys/stat.h>
#include <cerrno>
#include <cstring>

/** @brief Set a fd nonblocking; returns 0 on success. */
static int set_nonblock(int fd) {
    int fl = fcntl(fd, F_GETFL, 0);
    return (fl < 0) ? -1 : fcntl(fd, F_SETFL, fl | O_NONBLOCK);
}

int csim_instance_make_datadir(csim_instance *inst) {
    if (!inst || inst->sim_data_root.empty()) return -1;
    /* mkdir -p the root and its lansim/ subdir (where read_file/INJECT live). */
    std::string p = inst->sim_data_root;
    ::mkdir(p.c_str(), 0700);           /* ignore EEXIST */
    ::mkdir((p + "/lansim").c_str(), 0700);
    return 0;
}

int csim_instance_listen(csim_instance *inst) {
    int fd = socket(AF_INET, SOCK_STREAM, 0);
    if (fd < 0) { LOG_ERROR("csim listen: socket: %s", strerror(errno)); return -1; }
    int one = 1;
    setsockopt(fd, SOL_SOCKET, SO_REUSEADDR, &one, sizeof(one));
    struct sockaddr_in a; std::memset(&a, 0, sizeof(a));
    a.sin_family = AF_INET;
    a.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
    a.sin_port = htons((uint16_t)inst->port);   /* 0 => OS assigns */
    if (bind(fd, (struct sockaddr *)&a, sizeof(a)) != 0) {
        LOG_ERROR("csim listen: bind(%d): %s", inst->port, strerror(errno));
        close(fd); return -1;
    }
    socklen_t al = sizeof(a);
    if (getsockname(fd, (struct sockaddr *)&a, &al) == 0)
        inst->port = ntohs(a.sin_port);
    if (listen(fd, 1) != 0) {
        LOG_ERROR("csim listen: listen: %s", strerror(errno));
        close(fd); return -1;
    }
    set_nonblock(fd);
    inst->listen_fd = fd;
    inst->status = CSIM_LISTENING;
    LOG_INFO("csim sim id=%d neid=%d listening on 127.0.0.1:%d", inst->id, inst->neid, inst->port);
    return fd;
}

/* Flush inst->conn.txbuf to the socket (best-effort; small login/response
 * payloads). Clears what was written. */
static void flush_tx(csim_instance *inst) {
    std::string &tx = inst->conn.txbuf;
    size_t off = 0;
    while (off < tx.size()) {
        int n = (int)send(inst->conn.fd, tx.data() + off, tx.size() - off, 0);
        if (n <= 0) break;
        off += (size_t)n;
        inst->bytes_out += n;
    }
    tx.clear();
}

int csim_instance_accept(csim_instance *inst) {
    struct sockaddr_in a; socklen_t al = sizeof(a);
    int cfd = accept(inst->listen_fd, (struct sockaddr *)&a, &al);
    if (cfd < 0) return -1;
    if (inst->conn.fd >= 0) {           /* already have a client: reject the 2nd. */
        LOG_WARN("csim sim id=%d: rejecting 2nd client", inst->id);
        close(cfd);
        return -1;
    }
    set_nonblock(cfd);
    inst->conn.fd = cfd;
    inst->status = CSIM_CONNECTED;
    csim_pump_t r = csim_conn_open(inst);
    flush_tx(inst);
    if (r == CSIM_PUMP_ERROR) { csim_instance_on_readable(inst); return -1; }
    return 0;
}

int csim_instance_on_readable(csim_instance *inst) {
    char buf[4096];
    int n = (int)recv(inst->conn.fd, buf, sizeof(buf), 0);
    if (n <= 0) {                        /* EOF or error: drop the connection. */
        close(inst->conn.fd);
        csim_conn_close(inst);
        inst->status = CSIM_LISTENING;
        return 1;
    }
    csim_pump_t r = csim_conn_feed(inst, buf, (size_t)n);
    flush_tx(inst);
    if (r != CSIM_PUMP_YIELD) {          /* logout or error: close, keep listener. */
        close(inst->conn.fd);
        csim_conn_close(inst);
        inst->status = CSIM_LISTENING;
        return 1;
    }
    return 0;
}

void csim_instance_stop(csim_instance *inst) {
    if (!inst) return;
    if (inst->conn.fd >= 0) { close(inst->conn.fd); }
    csim_conn_close(inst);
    if (inst->listen_fd >= 0) { close(inst->listen_fd); inst->listen_fd = -1; }
    csim_lua_close_state(inst);
    inst->status = CSIM_IDLE;
    /* Overlay dir removal is best-effort and non-recursive here; Task 10's
     * daemon path uses a dedicated per-sim overlay under sim_data_root and
     * removes its files on STOP. */
}
```

- [ ] **Step 5: Run to verify pass**

Run: `nmake ncland_unit_tests && ./ncland_unit_tests -s CsimInstance`
Expected: PASS — resolution tests plus the socket end-to-end test all green.

- [ ] **Step 6: Commit**

```bash
git add cnc/ncland/src/csim_instance.h cnc/ncland/src/csim_instance.cpp cnc/ncland/src/csim_instance_tests.cpp
git commit -m "clansimd: instance listen/accept/readable/stop over TCP"
```

---

## Task 8: INJECT via overlay dir (TDD)

INJECT writes the reply as the canned file `clansim.read_file()` already searches for, under the sim's `sim_data_root`. Command→filename uses the same transform as `clansim.lua`: spaces→`_`, `/` and `|`→`_`.

**Files:**
- Modify: `cnc/ncland/src/csim_instance.h`, `cnc/ncland/src/csim_instance.cpp`
- Modify: `cnc/ncland/src/csim_instance_tests.cpp`

- [ ] **Step 1: Extend the header**

Append to `cnc/ncland/src/csim_instance.h` (before `#endif`):

```cpp
/**
 * @brief Write an injected canned reply for @p cmd under the sim's overlay.
 *
 * Path: <sim_data_root>/lansim/<neid:06d>/<tid>/CLI/<mangled-cmd>, matching the
 * first entry clansim.read_file() searches. The command is mangled exactly like
 * clansim.lua: spaces -> '_', then '/' and '|' -> '_'.
 *
 * @param inst  Instance whose overlay receives the file.
 * @param cmd   Command string the sim should answer.
 * @param reply Reply bytes to write.
 * @return 0 on success, -1 on failure.
 */
int csim_instance_inject(csim_instance *inst, const std::string &cmd,
                         const std::string &reply);
```

- [ ] **Step 2: Write the failing test**

Append to `cnc/ncland/src/csim_instance_tests.cpp`:

```cpp
#include <fstream>
#include <sstream>

TEST("CsimInject", "writes reply to the read_file search path") {
    csim_instance inst;
    inst.neid = 7; inst.tid = "NE7";
    inst.sim_data_root = mktmpdir();       /* fresh empty overlay */
    REQUIRE_EQ(csim_instance_inject(&inst, "show interfaces xe-0/0/9", "IF UP\n"), 0);

    /* Expected path mirrors clansim.lua read_file filelist[1]. */
    char zn[16]; std::snprintf(zn, sizeof(zn), "%06d", 7);
    std::string path = inst.sim_data_root + "/lansim/" + zn +
                       "/NE7/CLI/show_interfaces_xe-0_0_9";
    std::ifstream in(path);
    REQUIRE(in.good());
    std::stringstream ss; ss << in.rdbuf();
    REQUIRE_EQ(ss.str(), std::string("IF UP\n"));
}
```

- [ ] **Step 3: Run to verify failure**

Run: `nmake ncland_unit_tests && ./ncland_unit_tests -s CsimInject`
Expected: FAIL — undefined reference to `csim_instance_inject`.

- [ ] **Step 4: Implement**

Append to `cnc/ncland/src/csim_instance.cpp`:

```cpp
/** @brief Mangle a command into a filename exactly like clansim.lua read_file:
 *  whitespace runs -> '_', then '/' and '|' -> '_'. */
static std::string mangle_cmd(const std::string &cmd) {
    std::string s;
    bool in_ws = false;
    for (char c : cmd) {                 /* collapse whitespace runs to one '_' */
        if (c == ' ' || c == '\t') { if (!in_ws) s.push_back('_'); in_ws = true; }
        else { in_ws = false; s.push_back(c); }
    }
    for (char &c : s) if (c == '/' || c == '|') c = '_';
    return s;
}

/** @brief mkdir -p for a slash-separated path (best effort). */
static void mkpath(const std::string &dir) {
    std::string acc;
    for (size_t i = 0; i < dir.size(); ) {
        size_t j = dir.find('/', i);
        if (j == std::string::npos) j = dir.size();
        acc = dir.substr(0, j);
        if (!acc.empty()) ::mkdir(acc.c_str(), 0700);   /* ignore EEXIST */
        i = j + 1;
    }
}

int csim_instance_inject(csim_instance *inst, const std::string &cmd,
                         const std::string &reply) {
    if (!inst || inst->sim_data_root.empty()) return -1;
    char zn[16]; std::snprintf(zn, sizeof(zn), "%06d", inst->neid);
    std::string dir = inst->sim_data_root + "/lansim/" + zn + "/" + inst->tid + "/CLI";
    mkpath(dir);
    std::string path = dir + "/" + mangle_cmd(cmd);
    FILE *f = fopen(path.c_str(), "wb");
    if (!f) { LOG_ERROR("csim inject: open %s: %s", path.c_str(), strerror(errno)); return -1; }
    fwrite(reply.data(), 1, reply.size(), f);
    fclose(f);
    LOG_INFO("csim sim id=%d: injected reply for '%s'", inst->id, cmd.c_str());
    return 0;
}
```

- [ ] **Step 5: Run to verify pass**

Run: `nmake ncland_unit_tests && ./ncland_unit_tests -s CsimInject`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add cnc/ncland/src/csim_instance.h cnc/ncland/src/csim_instance.cpp cnc/ncland/src/csim_instance_tests.cpp
git commit -m "clansimd: INJECT writes canned reply into per-sim overlay"
```

---

## Task 9: Daemon — epoll loop, zmq REP, control dispatch

Wire the pieces into the running daemon: create the zmq REP socket, register its `ZMQ_FD` plus all listener/conn fds in epoll, and dispatch control commands. No new unit test framework here (integration test in Task 10); a `--self-test` smoke path validates startup.

**Files:**
- Create: `cnc/ncland/src/csim_daemon.h`, `cnc/ncland/src/csim_daemon.cpp`
- Modify: `cnc/ncland/src/clansimd.cpp`

- [ ] **Step 1: Write the daemon header**

Create `cnc/ncland/src/csim_daemon.h`:

```cpp
#ifndef CSIM_DAEMON_H
#define CSIM_DAEMON_H

#include "csim_types.h"
#include <string>

/**
 * @brief Initialize the daemon: epoll fd, zmq ctx + REP socket bound to
 * cfg.ctl_endpoint, ZMQ_FD registered in epoll.
 * @param d   Daemon to initialize (d->cfg must be filled first).
 * @return 0 on success, -1 on failure.
 */
int csim_daemon_init(csim_daemon *d);

/**
 * @brief Run the epoll event loop until d->running is cleared.
 * @param d Initialized daemon.
 * @return 0 on clean shutdown.
 */
int csim_daemon_run(csim_daemon *d);

/**
 * @brief Tear down all sims, the REP socket, the zmq context, and epoll.
 * @param d Daemon to shut down.
 */
void csim_daemon_shutdown(csim_daemon *d);

/**
 * @brief Handle one JSON control request, returning the JSON reply.
 *
 * Exposed (non-static) so an integration harness / test can call it directly.
 * @param d        Daemon state (mutated: sims created/removed).
 * @param req_text Raw request bytes.
 * @return JSON reply bytes.
 */
std::string csim_daemon_dispatch(csim_daemon *d, const std::string &req_text);

#endif /* CSIM_DAEMON_H */
```

- [ ] **Step 2: Write the daemon implementation**

Create `cnc/ncland/src/csim_daemon.cpp`:

```cpp
// csim_daemon.cpp -- epoll loop, zmq REP control, dispatch for clansimd.
#include "csim_daemon.h"
#include "csim_control.h"
#include "csim_instance.h"
#include "csim_luabridge.h"
#include "nflog.hpp"
#include "json.hpp"

#include <zmq.h>
#include <sys/epoll.h>
#include <unistd.h>
#include <cstring>
#include <cerrno>

using nlohmann::json;

/* ---- epoll helpers ----------------------------------------------------- */

static int ep_add(int epfd, int fd, void *ptr) {
    struct epoll_event ev; std::memset(&ev, 0, sizeof(ev));
    ev.events = EPOLLIN;
    ev.data.ptr = ptr;
    return epoll_ctl(epfd, EPOLL_CTL_ADD, fd, &ev);
}
static void ep_del(int epfd, int fd) { epoll_ctl(epfd, EPOLL_CTL_DEL, fd, nullptr); }

/* Tag pointers so epoll data.ptr can distinguish the REP socket, a listener,
 * or a connection. We register listener/conn fds with a small tagged wrapper. */
enum { TAG_REP = 1, TAG_LISTEN = 2, TAG_CONN = 3 };
struct fd_tag { int kind; csim_instance *inst; };

/* ---- dispatch ---------------------------------------------------------- */

static csim_instance *find_sim(csim_daemon *d, int id) {
    auto it = d->sims.find(id);
    return it == d->sims.end() ? nullptr : it->second;
}

/* START: resolve script, build state, overlay dir, listener; register in epoll. */
static std::string do_start(csim_daemon *d, const csim_request &r) {
    std::string script;
    if (!r.script.empty()) script = r.script;
    else if (!csim_resolve_script(d->cfg.sim_dirs, r.neid, r.dtype, script))
        return csim_format_err(r.req_id, "no script for neid/dtype");

    csim_instance *inst = new csim_instance();
    inst->id = d->next_id++;
    inst->dtype = r.dtype;
    inst->neid = r.neid;
    inst->tid = r.tid.empty() ? ("NE" + std::to_string(r.neid)) : r.tid;
    inst->script_path = script;
    inst->sim_data_root = d->cfg.sim_data_root + "/sim" + std::to_string(inst->id);
    inst->port = r.port;   /* 0 => auto */

    csim_instance_make_datadir(inst);
    if (csim_lua_open_state(inst, d->cfg.lua_lib_dir) != 0) {
        delete inst; return csim_format_err(r.req_id, "lua state init failed");
    }
    if (csim_instance_listen(inst) < 0) {
        csim_lua_close_state(inst); delete inst;
        return csim_format_err(r.req_id, "listen failed");
    }
    /* register listener in epoll with a tagged wrapper. */
    fd_tag *t = new fd_tag{TAG_LISTEN, inst};
    ep_add(d->epoll_fd, inst->listen_fd, t);
    d->by_fd[inst->listen_fd] = inst;
    d->sims[inst->id] = inst;

    json res; res["id"] = inst->id; res["port"] = inst->port;
    return csim_format_ok(r.req_id, res.dump());
}

static std::string do_stop(csim_daemon *d, const csim_request &r) {
    csim_instance *inst = find_sim(d, r.id);
    if (!inst) return csim_format_err(r.req_id, "no such sim");
    if (inst->conn.fd >= 0) { ep_del(d->epoll_fd, inst->conn.fd); d->by_fd.erase(inst->conn.fd); }
    if (inst->listen_fd >= 0) { ep_del(d->epoll_fd, inst->listen_fd); d->by_fd.erase(inst->listen_fd); }
    csim_instance_stop(inst);
    d->sims.erase(inst->id);
    delete inst;
    return csim_format_ok(r.req_id, "null");
}

static std::string do_list(csim_daemon *d, const csim_request &r) {
    json arr = json::array();
    for (auto &kv : d->sims) {
        csim_instance *s = kv.second;
        arr.push_back({{"id", s->id}, {"dtype", s->dtype}, {"neid", s->neid},
                       {"tid", s->tid}, {"port", s->port}, {"status", (int)s->status}});
    }
    return csim_format_ok(r.req_id, arr.dump());
}

static std::string do_status(csim_daemon *d, const csim_request &r) {
    csim_instance *s = find_sim(d, r.id);
    if (!s) return csim_format_err(r.req_id, "no such sim");
    json res = {{"id", s->id}, {"status", (int)s->status}, {"port", s->port},
                {"bytes_in", s->bytes_in}, {"bytes_out", s->bytes_out},
                {"last_cmd", s->last_cmd}};
    return csim_format_ok(r.req_id, res.dump());
}

/* RESET: rebuild the lua state (device reboots); keep listener/port. Drops any
 * live connection. */
static std::string do_reset(csim_daemon *d, const csim_request &r) {
    csim_instance *s = find_sim(d, r.id);
    if (!s) return csim_format_err(r.req_id, "no such sim");
    if (s->conn.fd >= 0) { ep_del(d->epoll_fd, s->conn.fd); d->by_fd.erase(s->conn.fd); close(s->conn.fd); }
    csim_conn_close(s);
    csim_lua_close_state(s);
    if (csim_lua_open_state(s, d->cfg.lua_lib_dir) != 0)
        return csim_format_err(r.req_id, "reset: lua re-init failed");
    s->status = CSIM_LISTENING;
    return csim_format_ok(r.req_id, "null");
}

/* RELOAD == RESET for v1 (re-reads the script on the next connection since the
 * chunk is loaded per-connection in csim_conn_open). */
static std::string do_reload(csim_daemon *d, const csim_request &r) {
    return do_reset(d, r);
}

static std::string do_inject(csim_daemon *d, const csim_request &r) {
    csim_instance *s = find_sim(d, r.id);
    if (!s) return csim_format_err(r.req_id, "no such sim");
    if (csim_instance_inject(s, r.inj_cmd, r.inj_reply) != 0)
        return csim_format_err(r.req_id, "inject failed");
    return csim_format_ok(r.req_id, "null");
}

std::string csim_daemon_dispatch(csim_daemon *d, const std::string &req_text) {
    csim_request r = csim_parse_request(req_text);
    if (!r.ok) return csim_format_err(r.req_id, r.err.empty() ? "bad request" : r.err);
    switch (r.cmd) {
        case CSIM_CMD_START:  return do_start(d, r);
        case CSIM_CMD_STOP:   return do_stop(d, r);
        case CSIM_CMD_LIST:   return do_list(d, r);
        case CSIM_CMD_STATUS: return do_status(d, r);
        case CSIM_CMD_RESET:  return do_reset(d, r);
        case CSIM_CMD_RELOAD: return do_reload(d, r);
        case CSIM_CMD_INJECT: return do_inject(d, r);
        default:              return csim_format_err(r.req_id, "unknown cmd");
    }
}

/* ---- zmq REP drain (edge-triggered ZMQ_FD, same idiom as ncland_notify) -- */

static void drain_rep(csim_daemon *d) {
    int events; size_t esz = sizeof(events);
    while (true) {
        if (zmq_getsockopt(d->rep_sock, ZMQ_EVENTS, &events, &esz) != 0) break;
        if (!(events & ZMQ_POLLIN)) break;

        zmq_msg_t m; zmq_msg_init(&m);
        if (zmq_msg_recv(&m, d->rep_sock, ZMQ_DONTWAIT) < 0) { zmq_msg_close(&m); break; }
        std::string req((char *)zmq_msg_data(&m), zmq_msg_size(&m));
        /* drain any extra frames defensively. */
        while (zmq_msg_more(&m)) { zmq_msg_close(&m); zmq_msg_init(&m);
            if (zmq_msg_recv(&m, d->rep_sock, ZMQ_DONTWAIT) < 0) break; }
        zmq_msg_close(&m);

        std::string rep = csim_daemon_dispatch(d, req);
        zmq_send(d->rep_sock, rep.data(), rep.size(), 0);
    }
}

/* ---- init / run / shutdown -------------------------------------------- */

int csim_daemon_init(csim_daemon *d) {
    d->next_port = d->cfg.port_base;
    d->epoll_fd = epoll_create1(0);
    if (d->epoll_fd < 0) { LOG_FATAL("epoll_create1: %s", strerror(errno)); return -1; }

    d->zmq_ctx = zmq_ctx_new();
    d->rep_sock = zmq_socket(d->zmq_ctx, ZMQ_REP);
    if (!d->rep_sock) { LOG_FATAL("zmq_socket(REP): %s", zmq_strerror(errno)); return -1; }
    if (zmq_bind(d->rep_sock, d->cfg.ctl_endpoint.c_str()) != 0) {
        LOG_FATAL("zmq_bind(%s): %s", d->cfg.ctl_endpoint.c_str(), zmq_strerror(errno));
        return -1;
    }
    size_t fsz = sizeof(d->rep_fd);
    if (zmq_getsockopt(d->rep_sock, ZMQ_FD, &d->rep_fd, &fsz) != 0) {
        LOG_FATAL("zmq_getsockopt(ZMQ_FD): %s", zmq_strerror(errno)); return -1;
    }
    static fd_tag rep_tag{TAG_REP, nullptr};
    ep_add(d->epoll_fd, d->rep_fd, &rep_tag);
    LOG_INFO("clansimd: control REP bound %s (fd=%d)", d->cfg.ctl_endpoint.c_str(), d->rep_fd);
    return 0;
}

int csim_daemon_run(csim_daemon *d) {
    struct epoll_event evs[64];
    while (d->running) {
        int n = epoll_wait(d->epoll_fd, evs, 64, 1000);
        if (n < 0) { if (errno == EINTR) continue; break; }
        for (int i = 0; i < n; i++) {
            fd_tag *t = (fd_tag *)evs[i].data.ptr;
            if (t->kind == TAG_REP) { drain_rep(d); continue; }
            csim_instance *inst = t->inst;
            if (t->kind == TAG_LISTEN) {
                if (csim_instance_accept(inst) == 0 && inst->conn.fd >= 0) {
                    fd_tag *ct = new fd_tag{TAG_CONN, inst};
                    ep_add(d->epoll_fd, inst->conn.fd, ct);
                    d->by_fd[inst->conn.fd] = inst;
                }
            } else if (t->kind == TAG_CONN) {
                int fd = inst->conn.fd;
                if (csim_instance_on_readable(inst) == 1) {  /* connection closed */
                    ep_del(d->epoll_fd, fd);
                    d->by_fd.erase(fd);
                    delete t;   /* free the conn tag */
                }
            }
        }
    }
    return 0;
}

void csim_daemon_shutdown(csim_daemon *d) {
    for (auto &kv : d->sims) { csim_instance_stop(kv.second); delete kv.second; }
    d->sims.clear();
    if (d->rep_sock) { zmq_close(d->rep_sock); d->rep_sock = nullptr; }
    if (d->zmq_ctx)  { zmq_ctx_destroy(d->zmq_ctx); d->zmq_ctx = nullptr; }
    if (d->epoll_fd >= 0) { close(d->epoll_fd); d->epoll_fd = -1; }
}
```

> **Note (listener tag leak):** `do_stop`/`do_reset` `ep_del` the listener fd but do not `delete` its `fd_tag`. Track the listener's `fd_tag*` on the instance (add `void *listen_tag;` to `csim_instance` if you want clean frees) — acceptable minor leak for v1; note it in code with a `TODO`. Do not silently ignore: add the `TODO` comment so it is visible.

- [ ] **Step 3: Wire main() in clansimd.cpp**

Replace `cnc/ncland/src/clansimd.cpp` with:

```cpp
// clansimd.cpp -- standalone lua NE-simulator daemon entry point.
#include "csim_daemon.h"
#include "csim_types.h"
#include "nflog.hpp"
#include <csignal>
#include <cstdlib>
#include <cstring>
#include <string>

static csim_daemon *g_d = nullptr;
static void on_term(int) { if (g_d) g_d->running = false; }

/**
 * @brief Parse args, build the daemon config, run the loop.
 *
 * Flags: --ctl <endpoint>, --sim-dir <dir> (repeatable), --lua-dir <dir>,
 *        --data-root <dir>, --port-base <n>.
 */
int main(int argc, char **argv)
{
    csim_daemon d;
    d.cfg.lua_lib_dir   = "/usr/cnc/lua";
    d.cfg.sim_data_root = "/tmp/clansimd";
    d.cfg.sim_dirs      = {"/usr/cnc/data", "/usr/cnc/lib/clan"};

    for (int i = 1; i < argc; i++) {
        if (!std::strcmp(argv[i], "--ctl") && i + 1 < argc)        d.cfg.ctl_endpoint = argv[++i];
        else if (!std::strcmp(argv[i], "--sim-dir") && i + 1 < argc) d.cfg.sim_dirs.push_back(argv[++i]);
        else if (!std::strcmp(argv[i], "--lua-dir") && i + 1 < argc)  d.cfg.lua_lib_dir = argv[++i];
        else if (!std::strcmp(argv[i], "--data-root") && i + 1 < argc) d.cfg.sim_data_root = argv[++i];
        else if (!std::strcmp(argv[i], "--port-base") && i + 1 < argc) d.cfg.port_base = atoi(argv[++i]);
    }

    if (csim_daemon_init(&d) != 0) { LOG_FATAL("clansimd: init failed"); return 1; }
    g_d = &d;
    std::signal(SIGTERM, on_term);
    std::signal(SIGINT,  on_term);
    std::signal(SIGPIPE, SIG_IGN);

    LOG_INFO("clansimd: running");
    csim_daemon_run(&d);
    csim_daemon_shutdown(&d);
    LOG_INFO("clansimd: stopped");
    return 0;
}
```

- [ ] **Step 4: Add csim_daemon.o to the build objects**

`CSIM_OBJS` in the Makefile already lists `csim_daemon.o` (Task 1). Confirm all four objects exist now so the `clansimd` link succeeds.

- [ ] **Step 5: Build the daemon**

Run: `nmake clansimd`
Expected: links cleanly, produces `$(PBIN)/clansimd`.

- [ ] **Step 6: Verify the full unit suite still passes**

Run: `nmake ncland_unit_tests && ./ncland_unit_tests -s Csim`
Expected: PASS — all `Csim*` suites (Control, Resolve, Lua, Instance, Inject) green; no regressions in other suites when run without `-s`.

- [ ] **Step 7: Commit**

```bash
git add cnc/ncland/src/csim_daemon.h cnc/ncland/src/csim_daemon.cpp cnc/ncland/src/clansimd.cpp cnc/ncland/src/Makefile
git commit -m "clansimd: epoll loop, zmq REP control, full command dispatch"
```

---

## Task 10: Integration test (end-to-end over zmq + TCP)

**Files:**
- Create: `cnc/ncland/src/test/integration/it_clansimd.sh`
- Create fixtures: `cnc/ncland/src/test/fixtures/sim/def.999.clansim` (uses the real `clansim.lua` + a canned tree)

- [ ] **Step 1: Create a dtype fixture script + canned response**

Create `cnc/ncland/src/test/fixtures/sim/def.999.clansim`:

```lua
#!/usr/cnc/bin/ilua
require('clansim')
clansim.default_sim(arg, {'login: ', 'prompt> '})
```

Create the canned response file the script will serve for command `RTRV-HDR`:

```bash
mkdir -p cnc/ncland/src/test/fixtures/data/lansim/000007/NE7/CLI
printf 'HEADER OK\n' > cnc/ncland/src/test/fixtures/data/lansim/000007/NE7/CLI/RTRV-HDR
```

- [ ] **Step 2: Write the integration script**

Create `cnc/ncland/src/test/integration/it_clansimd.sh`:

```bash
#!/bin/sh
# it_clansimd.sh -- end-to-end: start daemon, START a sim, connect, assert.
# Requires: clansimd on PATH or $PBIN, python3 (for the zmq REQ client + TCP),
#           built with the fixtures under test/fixtures.
set -e
HERE=$(cd "$(dirname "$0")/.." && pwd)          # .../cnc/ncland/src/test
SRC=$(cd "$HERE/.." && pwd)                      # .../cnc/ncland/src
CTL="ipc:///tmp/it_clansimd.$$.ctl"
DATA="/tmp/it_clansimd.$$.data"
CLANSIMD="${PBIN:-/usr/cnc/bin}/clansimd"

mkdir -p "$DATA/lansim"
cp -r "$SRC/test/fixtures/data/lansim/." "$DATA/lansim/"   # seed canned tree

"$CLANSIMD" --ctl "$CTL" \
    --sim-dir "$SRC/test/fixtures/sim" \
    --lua-dir "$SRC/test/fixtures" \
    --data-root "$DATA" &
DPID=$!
trap 'kill $DPID 2>/dev/null; rm -rf "$DATA"' EXIT
sleep 1

python3 - "$CTL" <<'PY'
import sys, zmq, socket, json, time
ctl = sys.argv[1]
c = zmq.Context()
s = c.socket(zmq.REQ); s.connect(ctl)

# START a sim on dtype 999, neid 7.
s.send_string(json.dumps({"v":1,"cmd":"START","req_id":"1",
                          "args":{"dtype":999,"neid":7,"tid":"NE7","port":0}}))
r = json.loads(s.recv_string()); assert r["ok"], r
port = r["result"]["port"]; sid = r["result"]["id"]
print("started sim", sid, "port", port)

# Connect as the "NE client".
t = socket.create_connection(("127.0.0.1", port)); t.settimeout(3)
assert t.recv(64).startswith(b"login: ")
t.sendall(b"admin\n"); assert t.recv(64).startswith(b"prompt> ")

# Command served from the canned tree.
t.sendall(b"RTRV-HDR\n")
data = t.recv(256)
assert b"HEADER OK" in data, data
print("canned response OK:", data)

# INJECT overrides a command's reply.
s.send_string(json.dumps({"cmd":"INJECT","args":{"id":sid,"cmd":"PING","reply":"PONG\n"}}))
assert json.loads(s.recv_string())["ok"]
t.sendall(b"PING\n")
assert b"PONG" in t.recv(64)
print("inject OK")

# STOP.
s.send_string(json.dumps({"cmd":"STOP","args":{"id":sid}}))
assert json.loads(s.recv_string())["ok"]
print("ALL OK")
PY
echo "it_clansimd: PASS"
```

Make it executable:

```bash
chmod +x cnc/ncland/src/test/integration/it_clansimd.sh
```

- [ ] **Step 3: Build and run the integration test**

Run:
```bash
nmake clansimd
PBIN=$(dirname $(find /home/dan/Git/netflex -name clansimd -type f 2>/dev/null | head -1)) \
  ./cnc/ncland/src/test/integration/it_clansimd.sh
```
Expected: prints `started sim …`, `canned response OK`, `inject OK`, `ALL OK`, then `it_clansimd: PASS`. If `python3`+`pyzmq` are unavailable in the environment, note that and run the test where they are; do not silently skip — report the skip.

- [ ] **Step 4: Commit**

```bash
git add cnc/ncland/src/test/integration/it_clansimd.sh cnc/ncland/src/test/fixtures/sim/def.999.clansim cnc/ncland/src/test/fixtures/data
git commit -m "clansimd: end-to-end integration test over zmq + TCP"
```

---

## Task 11: Docs + final verification

**Files:**
- Create: `cnc/ncland/src/CLANSIMD.adoc` (brief operator/dev doc)

- [ ] **Step 1: Write a short doc**

Create `cnc/ncland/src/CLANSIMD.adoc` describing: purpose, how to start (`clansimd --ctl … --sim-dir … --lua-dir … --data-root …`), the control JSON verbs with one example each, the script search order, and the INJECT overlay path convention. Keep it to ~1 page. (Content mirrors this plan's spec §7 and §10.)

- [ ] **Step 2: Full clean build + full test run**

Run:
```bash
nmake ncland_unit_tests && ./ncland_unit_tests
nmake clansimd
```
Expected: entire unit suite PASS (no regressions across non-Csim suites); `clansimd` links.

- [ ] **Step 3: Commit**

```bash
git add cnc/ncland/src/CLANSIMD.adoc
git commit -m "clansimd: add operator/developer doc"
```

---

## Self-Review notes (author checklist — completed)

- **Spec coverage:** transport TCP-per-sim (Task 7/9), verbatim lua reuse + `inc.*` shim + io bridge (Tasks 5–6), rich control API START/STOP/LIST/STATUS/RESET/RELOAD/INJECT (Tasks 8–9), both test goals — unit drive of sims (Task 6) + integration fixture (Task 10), single-threaded epoll+coroutine (Tasks 6/9), JSON via nlohmann (Task 3), ipc endpoint default (Task 9/main), one-connection-per-sim (Task 7 accept rejects 2nd), INJECT overlay dir (Task 8). clan integration correctly deferred (not in plan).
- **Type consistency:** `csim_pump_t`, `csim_instance`, `csim_conn`, `csim_config`, `csim_daemon` defined once in Task 2 and used unchanged. Function names stable: `csim_conn_open/feed/close`, `csim_instance_listen/accept/on_readable/stop/inject`, `csim_daemon_init/run/shutdown/dispatch`, `csim_parse_request`, `csim_format_ok/err`, `csim_resolve_script`, `csim_lua_open_state/close_state`.
- **Known v1 caveats (flagged in-code, not placeholders):** listener `fd_tag` free on STOP/RESET (TODO in Task 9); `clansim.lua` `read_file` prefers `/usr/cnc/lansim` if that dir exists — tests use `--data-root`/SIMDATA and a non-existent `/usr/cnc/lansim`, so the SIMDATA path wins.
