# clansimctl CLI Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship `clansimctl`, a single-file C++ CLI client that speaks the `clansimd` zmq REQ/REP JSON protocol so operators and scripts can start/stop/list/inspect/reset/reload/inject sims without hand-rolling JSON.

**Architecture:** One `.cpp` at `cnc/ncland/src/clansimctl.cpp`. Subcommand dispatch on `argv[1]`. Each subcommand builds an `nlohmann::json` request envelope, calls a shared `rpc_call()` helper (fresh `zmq_ctx_new()` + `ZMQ_REQ` socket per invocation, `ZMQ_SNDTIMEO`/`ZMQ_RCVTIMEO`/`ZMQ_LINGER=0`), and formats the reply as either a table/one-liner or raw JSON. Pure helpers (arg parse, envelope build, table format, status label) live in the same file, guarded by `#ifdef CLANSIMCTL_TESTING` for unit-test linkage. New Makefile target `$(PBIN)/clansimctl`; new unit-test file added to `ncland_unit_tests`.

**Tech Stack:** C++17, Lucent nmake, `nlohmann::json` (`include/json.hpp`), `libzmq` C API, `nflog.hpp`, existing `nfunit-test.hpp` framework.

**Spec:** `~/WorkNotes/specs/2026-07-24-clansimctl-cli-design.md`

---

## File Structure

- **Create** `cnc/ncland/src/clansimctl.cpp` — CLI entry + all helpers (arg parse, envelope build, RPC, formatting, dispatch, `main`).
- **Create** `cnc/ncland/src/clansimctl_tests.cpp` — unit tests for the pure helpers, added to `ncland_unit_tests`.
- **Modify** `cnc/ncland/src/Makefile` — add `$(PBIN)/clansimctl` to `.MAIN`, add link rule, add `clansimctl_tests.o` to `ncland_unit_tests` line.
- **Modify** `cnc/ncland/src/CLANSIMD.md` — append a `Client` section with `clansimctl` example invocations.

## Testing Strategy

TDD, one behavior per test. Tests target *pure* helpers only — no zmq round-trip in the unit-test binary. The functions we want to test (`parse_globals`, `build_start_req`, `build_stop_req`, `build_inject_req`, `format_list_table`, `format_status_block`, `status_label`) are file-scope `static` in `clansimctl.cpp`; we expose them to tests by wrapping their declarations in a header-less way — the test file `#include`s `clansimctl.cpp` directly with `#define CLANSIMCTL_TESTING` set so `main` is `#ifdef`-guarded away. This is the smallest-possible surface for unit-testability (no separate header, no over-engineered lib split).

End-to-end coverage against the daemon is deferred — the existing `CsimDaemon` suite in `csim_daemon_tests.cpp` already covers the daemon dispatch path.

## Commit Cadence

One commit per task. Every commit builds cleanly (`nmake`) and passes `./ncland_unit_tests -s Clansimctl`.

---

### Task 1: Skeleton `clansimctl.cpp` with usage + build

**Files:**
- Create: `cnc/ncland/src/clansimctl.cpp`
- Modify: `cnc/ncland/src/Makefile`

- [ ] **Step 1: Create `clansimctl.cpp` skeleton**

```cpp
// clansimctl.cpp -- CLI client for clansimd (zmq REQ/REP JSON control).
#include "nflog.hpp"
#include "json.hpp"

#include <zmq.h>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <string>
#include <vector>

using nlohmann::json;

namespace {

/** @brief Global options parsed from the command line. */
struct globals {
    std::string ctl_endpoint;   /**< zmq REQ endpoint. */
    int         timeout_secs = 5;
    bool        json_out     = false;
};

/** @brief Print usage to stderr. */
static void usage()
{
    std::fprintf(stderr,
        "Usage: clansimctl [--ctl ENDPOINT] [--timeout SECS] [--json] <cmd> [args]\n"
        "\n"
        "Global flags:\n"
        "  --ctl ENDPOINT     zmq REQ endpoint (default: $CLANSIMD_CTL or\n"
        "                     ipc:///tmp/clansimd.ctl)\n"
        "  --timeout SECS     REQ send/recv timeout, 0 = wait forever (default: 5)\n"
        "  --json             Print raw JSON reply instead of formatted output\n"
        "  -h, --help         This help\n"
        "\n"
        "Commands:\n"
        "  start   --dtype N [--neid N] [--tid STR] [--port N]\n"
        "  start   --script PATH [--neid N] [--tid STR] [--port N]\n"
        "  stop    <id>\n"
        "  list\n"
        "  status  <id>\n"
        "  reset   <id>\n"
        "  reload  <id>\n"
        "  inject  <id> --cmd STR (--reply STR | --reply-file PATH)\n");
}

} /* namespace */

#ifndef CLANSIMCTL_TESTING
int main(int argc, char **argv)
{
    (void)argc; (void)argv;
    usage();
    return 2;
}
#endif
```

- [ ] **Step 2: Add build rule to Makefile**

Locate the `.MAIN` line in `cnc/ncland/src/Makefile` and change it, and add the new link rule below the existing `$(PBIN)/clansimd` rule:

```make
.MAIN : $(PBIN)/ncland $(PBIN)/nclan-seed $(PBIN)/clansimd $(PBIN)/clansimctl
```

```make
$(PBIN)/clansimctl :: clansimctl.o $(INCLIBS) -latm -linc -lxmlparse -lssh -ljson -lnetclan
	$(CPLUS_CC) $(CCFLAGS:M!=-AC99) $(LDFLAGS) -o $(<) $(*) -lpthread -lzmq -lrt
```

- [ ] **Step 3: Build and verify**

Run: `nmake $(PBIN)/clansimctl`
Expected: `clansimctl.o` compiles, link succeeds, binary lives at `$PBIN/clansimctl`.

Run: `$PBIN/clansimctl`
Expected: exit code 2, usage printed on stderr.

- [ ] **Step 4: Commit**

```bash
git add cnc/ncland/src/clansimctl.cpp cnc/ncland/src/Makefile
git commit -m "clansimctl: skeleton binary + build rule"
```

---

### Task 2: `parse_globals` — strip global flags, honor env

**Files:**
- Modify: `cnc/ncland/src/clansimctl.cpp`
- Create: `cnc/ncland/src/clansimctl_tests.cpp`
- Modify: `cnc/ncland/src/Makefile`

- [ ] **Step 1: Write failing tests**

Create `cnc/ncland/src/clansimctl_tests.cpp`:

```cpp
// clansimctl_tests.cpp -- unit tests for clansimctl pure helpers.
#define CLANSIMCTL_TESTING
#include "clansimctl.cpp"

#include "nfunit-test.hpp"
#include <cstdlib>

static std::vector<char *> mkargv(std::vector<std::string> &store)
{
    std::vector<char *> out;
    for (auto &s : store) out.push_back(&s[0]);
    return out;
}

TEST("Clansimctl", "parse_globals: defaults when no flags") {
    unsetenv("CLANSIMD_CTL");
    std::vector<std::string> store = {"clansimctl", "list"};
    auto argv = mkargv(store);
    globals g;
    std::vector<char *> rest;
    int rc = parse_globals((int)argv.size(), argv.data(), g, rest);
    REQUIRE_EQ(rc, 0);
    REQUIRE_EQ(g.ctl_endpoint, std::string("ipc:///tmp/clansimd.ctl"));
    REQUIRE_EQ(g.timeout_secs, 5);
    REQUIRE(!g.json_out);
    REQUIRE_EQ(rest.size(), (size_t)1);
    REQUIRE_EQ(std::string(rest[0]), std::string("list"));
}

TEST("Clansimctl", "parse_globals: env var supplies default") {
    setenv("CLANSIMD_CTL", "tcp://h:9", 1);
    std::vector<std::string> store = {"clansimctl", "list"};
    auto argv = mkargv(store);
    globals g;
    std::vector<char *> rest;
    parse_globals((int)argv.size(), argv.data(), g, rest);
    REQUIRE_EQ(g.ctl_endpoint, std::string("tcp://h:9"));
    unsetenv("CLANSIMD_CTL");
}

TEST("Clansimctl", "parse_globals: --ctl overrides env") {
    setenv("CLANSIMD_CTL", "tcp://h:9", 1);
    std::vector<std::string> store = {
        "clansimctl", "--ctl", "ipc:///tmp/x.sock", "list"};
    auto argv = mkargv(store);
    globals g;
    std::vector<char *> rest;
    parse_globals((int)argv.size(), argv.data(), g, rest);
    REQUIRE_EQ(g.ctl_endpoint, std::string("ipc:///tmp/x.sock"));
    REQUIRE_EQ(rest.size(), (size_t)1);
    unsetenv("CLANSIMD_CTL");
}

TEST("Clansimctl", "parse_globals: --timeout + --json") {
    unsetenv("CLANSIMD_CTL");
    std::vector<std::string> store = {
        "clansimctl", "--timeout", "12", "--json", "list"};
    auto argv = mkargv(store);
    globals g;
    std::vector<char *> rest;
    parse_globals((int)argv.size(), argv.data(), g, rest);
    REQUIRE_EQ(g.timeout_secs, 12);
    REQUIRE(g.json_out);
    REQUIRE_EQ(rest.size(), (size_t)1);
    REQUIRE_EQ(std::string(rest[0]), std::string("list"));
}

TEST("Clansimctl", "parse_globals: --help returns 2") {
    unsetenv("CLANSIMD_CTL");
    std::vector<std::string> store = {"clansimctl", "--help"};
    auto argv = mkargv(store);
    globals g;
    std::vector<char *> rest;
    int rc = parse_globals((int)argv.size(), argv.data(), g, rest);
    REQUIRE_EQ(rc, 2);
}

TEST("Clansimctl", "parse_globals: unknown flag returns 2") {
    unsetenv("CLANSIMD_CTL");
    std::vector<std::string> store = {"clansimctl", "--nope", "list"};
    auto argv = mkargv(store);
    globals g;
    std::vector<char *> rest;
    int rc = parse_globals((int)argv.size(), argv.data(), g, rest);
    REQUIRE_EQ(rc, 2);
}
```

- [ ] **Step 2: Add test file to `ncland_unit_tests`**

Modify the `ncland_unit_tests ::` recipe in `cnc/ncland/src/Makefile` — append `clansimctl_tests.o` to the object list.

Original snippet:
```make
ncland_unit_tests :: ncland_unit_tests.o ncland_notify_parse_tests.o nclan_seed_fmt.o nclan_seed_tests.o ncland_registry_tests.o ncland_connpool_tests.o ncland_lua_tests.o ncland_stepper_tests.o ncland_mq_tests.o ncland_test_qwrite.o csim_control.o csim_control_tests.o csim_instance.o csim_instance_tests.o csim_luabridge.o csim_luabridge_tests.o csim_daemon.o csim_daemon_tests.o $(NCLAND_OBJS) $(INCLIBS) -latm -linc -lxmlparse -lssh -ljson -lnetclan
```

New snippet (add `clansimctl_tests.o` just before `$(NCLAND_OBJS)`):
```make
ncland_unit_tests :: ncland_unit_tests.o ncland_notify_parse_tests.o nclan_seed_fmt.o nclan_seed_tests.o ncland_registry_tests.o ncland_connpool_tests.o ncland_lua_tests.o ncland_stepper_tests.o ncland_mq_tests.o ncland_test_qwrite.o csim_control.o csim_control_tests.o csim_instance.o csim_instance_tests.o csim_luabridge.o csim_luabridge_tests.o csim_daemon.o csim_daemon_tests.o clansimctl_tests.o $(NCLAND_OBJS) $(INCLIBS) -latm -linc -lxmlparse -lssh -ljson -lnetclan
```

- [ ] **Step 3: Run tests to confirm failure**

Run: `nmake ncland_unit_tests`
Expected: FAIL — `parse_globals` is undeclared.

- [ ] **Step 4: Implement `parse_globals` in `clansimctl.cpp`**

Inside the anonymous namespace, above `usage()`, add:

```cpp
/** @brief Default control endpoint when neither --ctl nor $CLANSIMD_CTL is set. */
static const char *CLANSIMCTL_DEFAULT_CTL = "ipc:///tmp/clansimd.ctl";
```

Inside the anonymous namespace, below `usage()`, add:

```cpp
/**
 * @brief Consume global flags from argv into @p g; append the remaining
 *        (subcommand + args) tokens into @p rest.
 *
 * Precedence: --ctl beats $CLANSIMD_CTL beats the compiled-in default.
 * `--help`/`-h` sets rc=2 (usage printed by caller); unknown global flags
 * also return 2.
 *
 * @param argc Full argv count.
 * @param argv Full argv (argv[0] is the program name).
 * @param g    Output: parsed global options.
 * @param rest Output: remaining tokens starting with the subcommand.
 * @return 0 on success, 2 on usage error / help.
 */
static int parse_globals(int argc, char **argv, globals &g,
                         std::vector<char *> &rest)
{
    const char *env = std::getenv("CLANSIMD_CTL");
    g.ctl_endpoint  = env && *env ? env : CLANSIMCTL_DEFAULT_CTL;
    g.timeout_secs  = 5;
    g.json_out      = false;

    int i = 1;
    for (; i < argc; i++) {
        const char *a = argv[i];
        if (!std::strcmp(a, "--ctl") && i + 1 < argc) {
            g.ctl_endpoint = argv[++i];
        } else if (!std::strcmp(a, "--timeout") && i + 1 < argc) {
            g.timeout_secs = std::atoi(argv[++i]);
        } else if (!std::strcmp(a, "--json")) {
            g.json_out = true;
        } else if (!std::strcmp(a, "-h") || !std::strcmp(a, "--help")) {
            return 2;
        } else if (a[0] == '-' && a[1] == '-') {
            std::fprintf(stderr, "clansimctl: unknown flag: %s\n", a);
            return 2;
        } else {
            break;  /* first non-flag token = subcommand */
        }
    }
    for (; i < argc; i++) rest.push_back(argv[i]);
    return 0;
}
```

- [ ] **Step 5: Run tests to confirm they pass**

Run: `nmake ncland_unit_tests && ./ncland_unit_tests -s Clansimctl`
Expected: 6 tests pass.

- [ ] **Step 6: Wire `parse_globals` into `main`**

Replace the `main` body (still under `#ifndef CLANSIMCTL_TESTING`):

```cpp
int main(int argc, char **argv)
{
    globals g;
    std::vector<char *> rest;
    if (parse_globals(argc, argv, g, rest) != 0) { usage(); return 2; }
    if (rest.empty()) { usage(); return 2; }
    /* Subcommand dispatch added in later tasks. */
    std::fprintf(stderr, "clansimctl: unknown command: %s\n", rest[0]);
    return 2;
}
```

Run: `nmake $(PBIN)/clansimctl && $PBIN/clansimctl --ctl foo`
Expected: exit 2, usage printed (no subcommand).

Run: `$PBIN/clansimctl foo`
Expected: exit 2, "unknown command: foo" on stderr.

- [ ] **Step 7: Commit**

```bash
git add cnc/ncland/src/clansimctl.cpp cnc/ncland/src/clansimctl_tests.cpp cnc/ncland/src/Makefile
git commit -m "clansimctl: parse global flags (--ctl, --timeout, --json)"
```

---

### Task 3: `rpc_call` helper + `req_id_gen`

**Files:**
- Modify: `cnc/ncland/src/clansimctl.cpp`

This helper touches the zmq wire, so we skip a fake-zmq unit test and instead verify behavior end-to-end when the first command (`list`) lands in Task 4.

- [ ] **Step 1: Add `req_id_gen`**

Inside the anonymous namespace of `clansimctl.cpp`, add:

```cpp
/**
 * @brief Generate a unique request id string.
 *
 * Format: "<pid>.<counter>". Deterministic within a single process; readable
 * in daemon logs when correlating.
 *
 * @return Fresh id string.
 */
static std::string req_id_gen()
{
    static long counter = 0;
    char buf[64];
    std::snprintf(buf, sizeof(buf), "%ld.%ld", (long)::getpid(), ++counter);
    return std::string(buf);
}
```

Add `#include <unistd.h>` to the top of `clansimctl.cpp`.

- [ ] **Step 2: Add `rpc_call`**

Inside the anonymous namespace, add:

```cpp
/** @brief RPC exit codes. */
enum {
    RPC_OK      = 0,
    RPC_ERR     = 1,   /**< Daemon returned ok:false OR transport failure. */
    RPC_USAGE   = 2,
    RPC_TIMEOUT = 3
};

/**
 * @brief Send @p req over a fresh zmq REQ socket and wait for the reply.
 *
 * Creates a per-call context + socket (short-lived; no shared state between
 * commands). ZMQ_LINGER=0 avoids blocking on close if the daemon vanished.
 *
 * @param endpoint    zmq endpoint (any bind string).
 * @param timeout_ms  Send/recv timeout; -1 (from timeout_secs==0) waits forever.
 * @param req         Request envelope to send.
 * @param reply_out   Output: parsed JSON reply on success.
 * @return RPC_OK on parsed reply, RPC_TIMEOUT on EAGAIN, RPC_ERR on other
 *         transport/parse failures. Error text goes to stderr.
 */
static int rpc_call(const std::string &endpoint, int timeout_ms,
                    const json &req, json &reply_out)
{
    void *ctx = zmq_ctx_new();
    if (!ctx) { std::fprintf(stderr, "clansimctl: zmq_ctx_new failed\n"); return RPC_ERR; }

    int rc = RPC_ERR;
    void *sock = zmq_socket(ctx, ZMQ_REQ);
    if (!sock) {
        std::fprintf(stderr, "clansimctl: zmq_socket: %s\n", zmq_strerror(errno));
        zmq_ctx_term(ctx);
        return RPC_ERR;
    }

    int linger = 0;
    zmq_setsockopt(sock, ZMQ_LINGER, &linger, sizeof(linger));
    if (timeout_ms > 0) {
        zmq_setsockopt(sock, ZMQ_SNDTIMEO, &timeout_ms, sizeof(timeout_ms));
        zmq_setsockopt(sock, ZMQ_RCVTIMEO, &timeout_ms, sizeof(timeout_ms));
    }

    if (zmq_connect(sock, endpoint.c_str()) != 0) {
        std::fprintf(stderr, "clansimctl: zmq_connect(%s): %s\n",
                     endpoint.c_str(), zmq_strerror(errno));
        goto done;
    }

    {
        std::string body = req.dump();
        if (zmq_send(sock, body.data(), body.size(), 0) < 0) {
            if (errno == EAGAIN) { rc = RPC_TIMEOUT; goto done; }
            std::fprintf(stderr, "clansimctl: zmq_send: %s\n", zmq_strerror(errno));
            goto done;
        }
    }

    {
        zmq_msg_t m; zmq_msg_init(&m);
        int n = zmq_msg_recv(&m, sock, 0);
        if (n < 0) {
            if (errno == EAGAIN) { zmq_msg_close(&m); rc = RPC_TIMEOUT; goto done; }
            std::fprintf(stderr, "clansimctl: zmq_recv: %s\n", zmq_strerror(errno));
            zmq_msg_close(&m);
            goto done;
        }
        std::string wire((char *)zmq_msg_data(&m), zmq_msg_size(&m));
        zmq_msg_close(&m);
        try {
            reply_out = json::parse(wire);
            rc = RPC_OK;
        } catch (const std::exception &e) {
            std::fprintf(stderr, "clansimctl: reply parse: %s\n", e.what());
            rc = RPC_ERR;
        }
    }

done:
    zmq_close(sock);
    zmq_ctx_term(ctx);
    return rc;
}
```

- [ ] **Step 3: Build**

Run: `nmake $(PBIN)/clansimctl`
Expected: clean build.

- [ ] **Step 4: Commit**

```bash
git add cnc/ncland/src/clansimctl.cpp
git commit -m "clansimctl: rpc_call + req_id_gen helpers"
```

---

### Task 4: `list` subcommand — table + `--json`

**Files:**
- Modify: `cnc/ncland/src/clansimctl.cpp`
- Modify: `cnc/ncland/src/clansimctl_tests.cpp`

- [ ] **Step 1: Write failing tests for `status_label` and `format_list_table`**

Append to `clansimctl_tests.cpp`:

```cpp
TEST("Clansimctl", "status_label maps enum to string") {
    REQUIRE_EQ(status_label(0), std::string("idle"));
    REQUIRE_EQ(status_label(1), std::string("listening"));
    REQUIRE_EQ(status_label(2), std::string("connected"));
    REQUIRE_EQ(status_label(3), std::string("err"));
}

TEST("Clansimctl", "status_label unknown falls back to ?N") {
    REQUIRE_EQ(status_label(42), std::string("?42"));
}

TEST("Clansimctl", "format_list_table renders header + rows") {
    json rows = json::parse(R"([
        {"id":1,"dtype":999,"neid":7,"tid":"NE7","port":20000,"status":2},
        {"id":2,"dtype":103,"neid":88,"tid":"NE88","port":20001,"status":1}
    ])");
    std::string out = format_list_table(rows);
    REQUIRE(out.find("ID") != std::string::npos);
    REQUIRE(out.find("DTYPE") != std::string::npos);
    REQUIRE(out.find("NEID") != std::string::npos);
    REQUIRE(out.find("TID") != std::string::npos);
    REQUIRE(out.find("PORT") != std::string::npos);
    REQUIRE(out.find("STATUS") != std::string::npos);
    REQUIRE(out.find("NE7") != std::string::npos);
    REQUIRE(out.find("NE88") != std::string::npos);
    REQUIRE(out.find("connected") != std::string::npos);
    REQUIRE(out.find("listening") != std::string::npos);
}

TEST("Clansimctl", "format_list_table empty result prints header only") {
    json rows = json::array();
    std::string out = format_list_table(rows);
    REQUIRE(out.find("ID") != std::string::npos);
    REQUIRE(out.find("NE7") == std::string::npos);
}
```

- [ ] **Step 2: Run tests, confirm failure**

Run: `nmake ncland_unit_tests`
Expected: FAIL — `status_label` and `format_list_table` undeclared.

- [ ] **Step 3: Implement `status_label`**

In the anonymous namespace of `clansimctl.cpp`:

```cpp
/**
 * @brief Map csim_status_t integer to a human label.
 *
 * Mirrors csim_types.h enum (idle=0, listening=1, connected=2, err=3).
 * Unknown values fall back to "?<n>" so operators can spot new states.
 *
 * @param s Status value from the daemon reply.
 * @return Label string.
 */
static std::string status_label(int s)
{
    switch (s) {
        case 0: return "idle";
        case 1: return "listening";
        case 2: return "connected";
        case 3: return "err";
        default: {
            char b[16]; std::snprintf(b, sizeof(b), "?%d", s);
            return std::string(b);
        }
    }
}
```

- [ ] **Step 4: Implement `format_list_table`**

In the anonymous namespace:

```cpp
/**
 * @brief Format a LIST result (array of {id,dtype,neid,tid,port,status}) as
 *        an aligned text table.
 *
 * Column widths grow to the widest cell. Header is always emitted.
 *
 * @param arr JSON array of sim records.
 * @return Multi-line string ending with '\n'.
 */
static std::string format_list_table(const json &arr)
{
    struct row { std::string id, dtype, neid, tid, port, status; };
    std::vector<row> rows;
    row hdr{"ID","DTYPE","NEID","TID","PORT","STATUS"};
    rows.push_back(hdr);

    if (arr.is_array()) {
        for (const auto &r : arr) {
            row x;
            x.id     = std::to_string(r.value("id",    0));
            x.dtype  = std::to_string(r.value("dtype", 0));
            x.neid   = std::to_string(r.value("neid",  0));
            x.tid    = r.value("tid", std::string(""));
            x.port   = std::to_string(r.value("port",  0));
            x.status = status_label(r.value("status", -1));
            rows.push_back(x);
        }
    }

    size_t w[6] = {0,0,0,0,0,0};
    for (const auto &x : rows) {
        const std::string *c[6] = {&x.id,&x.dtype,&x.neid,&x.tid,&x.port,&x.status};
        for (int i = 0; i < 6; i++) if (c[i]->size() > w[i]) w[i] = c[i]->size();
    }

    std::string out;
    char buf[256];
    for (const auto &x : rows) {
        std::snprintf(buf, sizeof(buf),
            "%-*s  %-*s  %-*s  %-*s  %-*s  %-*s\n",
            (int)w[0], x.id.c_str(), (int)w[1], x.dtype.c_str(),
            (int)w[2], x.neid.c_str(), (int)w[3], x.tid.c_str(),
            (int)w[4], x.port.c_str(), (int)w[5], x.status.c_str());
        out += buf;
    }
    return out;
}
```

- [ ] **Step 5: Run tests, confirm pass**

Run: `nmake ncland_unit_tests && ./ncland_unit_tests -s Clansimctl`
Expected: all Clansimctl tests pass.

- [ ] **Step 6: Wire the `list` subcommand into `main`**

Add above `main` (still inside the anonymous namespace):

```cpp
/**
 * @brief Execute the `list` subcommand.
 *
 * @param g Global options (endpoint, timeout, json).
 * @return Process exit code.
 */
static int cmd_list(const globals &g)
{
    json req = {
        {"v", 1},
        {"cmd", "LIST"},
        {"req_id", req_id_gen()}
    };
    json reply;
    int rc = rpc_call(g.ctl_endpoint,
                      g.timeout_secs > 0 ? g.timeout_secs * 1000 : -1,
                      req, reply);
    if (rc != RPC_OK) return rc;
    if (!reply.value("ok", false)) {
        std::fprintf(stderr, "clansimctl: %s\n",
                     reply.value("error", std::string("(no error text)")).c_str());
        return RPC_ERR;
    }
    if (g.json_out) {
        std::fputs(reply.dump().c_str(), stdout);
        std::fputc('\n', stdout);
    } else {
        std::fputs(format_list_table(reply.value("result", json::array())).c_str(),
                   stdout);
    }
    return RPC_OK;
}
```

Replace the trailing "unknown command" branch in `main` with dispatch:

```cpp
    std::string sub = rest[0];
    if      (sub == "list")   return cmd_list(g);
    else {
        std::fprintf(stderr, "clansimctl: unknown command: %s\n", sub.c_str());
        return 2;
    }
```

- [ ] **Step 7: Smoke test against a running daemon**

Start a daemon in one shell (adjust flags to your setup):
```bash
$PBIN/clansimd --ctl ipc:///tmp/clansimd.ctl --sim-dir cnc/ncland/src/test/fixtures/sim --lua-dir cnc/ncland/src/test/fixtures --data-root /tmp/clansimd-smoke --port-base 20500 &
```

Then:
```bash
$PBIN/clansimctl list
$PBIN/clansimctl --json list
$PBIN/clansimctl --ctl ipc:///tmp/nosuch.sock --timeout 1 list ; echo "exit=$?"
```

Expected:
- First: header row only (no sims), exit 0.
- Second: `{"ok":true,"req_id":"...","result":[]}` + newline, exit 0.
- Third: connect error to stderr, `exit=1` (zmq REQ silently connects; the send succeeds; the recv hits `--timeout 1s` → `exit=3`). Either 1 or 3 is acceptable — the important thing is a non-zero exit and no hang.

Kill daemon: `kill %1`.

- [ ] **Step 8: Commit**

```bash
git add cnc/ncland/src/clansimctl.cpp cnc/ncland/src/clansimctl_tests.cpp
git commit -m "clansimctl: list subcommand (table + --json)"
```

---

### Task 5: `stop` / `reset` / `reload` — id-only verbs

**Files:**
- Modify: `cnc/ncland/src/clansimctl.cpp`
- Modify: `cnc/ncland/src/clansimctl_tests.cpp`

- [ ] **Step 1: Write failing test for envelope builder**

Append to `clansimctl_tests.cpp`:

```cpp
TEST("Clansimctl", "build_id_req builds envelope with cmd + id") {
    json r = build_id_req("STOP", 7, "rid-1");
    REQUIRE_EQ(r["cmd"], "STOP");
    REQUIRE_EQ(r["v"],   1);
    REQUIRE_EQ(r["req_id"], "rid-1");
    REQUIRE_EQ(r["args"]["id"], 7);
}
```

- [ ] **Step 2: Run tests, confirm failure**

Run: `nmake ncland_unit_tests`
Expected: FAIL — `build_id_req` undeclared.

- [ ] **Step 3: Implement `build_id_req` + `cmd_id_verb`**

In the anonymous namespace, add:

```cpp
/**
 * @brief Build an envelope for verbs whose only arg is @p id (STOP/RESET/RELOAD/STATUS).
 *
 * @param verb   One of "STOP","RESET","RELOAD","STATUS".
 * @param id     Sim id.
 * @param req_id Request id to echo.
 * @return JSON envelope.
 */
static json build_id_req(const char *verb, int id, const std::string &req_id)
{
    return json{
        {"v", 1},
        {"cmd", verb},
        {"req_id", req_id},
        {"args", {{"id", id}}}
    };
}

/**
 * @brief Execute an id-only verb (STOP/RESET/RELOAD).
 *
 * Success prints `ok` (or raw JSON with --json). No result formatting.
 *
 * @param g       Globals.
 * @param verb    Uppercase protocol verb.
 * @param argc    Remaining argc after globals stripped.
 * @param argv    Remaining argv (argv[0] = subcommand name, argv[1] = id).
 * @return exit code.
 */
static int cmd_id_verb(const globals &g, const char *verb,
                       int argc, char **argv)
{
    if (argc != 2) {
        std::fprintf(stderr, "clansimctl: %s requires <id>\n", argv[0]);
        return RPC_USAGE;
    }
    int id = std::atoi(argv[1]);
    json reply;
    int rc = rpc_call(g.ctl_endpoint,
                      g.timeout_secs > 0 ? g.timeout_secs * 1000 : -1,
                      build_id_req(verb, id, req_id_gen()), reply);
    if (rc != RPC_OK) return rc;
    if (!reply.value("ok", false)) {
        std::fprintf(stderr, "clansimctl: %s\n",
                     reply.value("error", std::string("(no error text)")).c_str());
        return RPC_ERR;
    }
    if (g.json_out) {
        std::fputs(reply.dump().c_str(), stdout);
        std::fputc('\n', stdout);
    } else {
        std::fputs("ok\n", stdout);
    }
    return RPC_OK;
}
```

- [ ] **Step 4: Wire dispatch**

Update the `if/else` chain in `main`:

```cpp
    std::string sub = rest[0];
    int rc_argc = (int)rest.size();
    char **rc_argv = rest.data();
    if      (sub == "list")   return cmd_list(g);
    else if (sub == "stop")   return cmd_id_verb(g, "STOP",   rc_argc, rc_argv);
    else if (sub == "reset")  return cmd_id_verb(g, "RESET",  rc_argc, rc_argv);
    else if (sub == "reload") return cmd_id_verb(g, "RELOAD", rc_argc, rc_argv);
    else {
        std::fprintf(stderr, "clansimctl: unknown command: %s\n", sub.c_str());
        return 2;
    }
```

- [ ] **Step 5: Run tests, confirm pass**

Run: `nmake ncland_unit_tests && ./ncland_unit_tests -s Clansimctl`
Expected: all Clansimctl tests pass.

Run: `nmake $(PBIN)/clansimctl && $PBIN/clansimctl stop`
Expected: stderr `clansimctl: stop requires <id>`, exit 2.

- [ ] **Step 6: Commit**

```bash
git add cnc/ncland/src/clansimctl.cpp cnc/ncland/src/clansimctl_tests.cpp
git commit -m "clansimctl: stop/reset/reload (id-only verbs)"
```

---

### Task 6: `status` subcommand — key/value block

**Files:**
- Modify: `cnc/ncland/src/clansimctl.cpp`
- Modify: `cnc/ncland/src/clansimctl_tests.cpp`

- [ ] **Step 1: Write failing test**

Append to `clansimctl_tests.cpp`:

```cpp
TEST("Clansimctl", "format_status_block prints key=value lines") {
    json r = json::parse(R"({
        "id":1,"status":2,"port":20000,
        "bytes_in":42,"bytes_out":17,"last_cmd":"show ver"
    })");
    std::string out = format_status_block(r);
    REQUIRE(out.find("id=1")            != std::string::npos);
    REQUIRE(out.find("status=connected")!= std::string::npos);
    REQUIRE(out.find("port=20000")      != std::string::npos);
    REQUIRE(out.find("bytes_in=42")     != std::string::npos);
    REQUIRE(out.find("bytes_out=17")    != std::string::npos);
    REQUIRE(out.find("last_cmd=show ver") != std::string::npos);
}
```

- [ ] **Step 2: Run tests, confirm failure**

Run: `nmake ncland_unit_tests`
Expected: FAIL — `format_status_block` undeclared.

- [ ] **Step 3: Implement `format_status_block`**

In the anonymous namespace:

```cpp
/**
 * @brief Format a STATUS result as key=value lines (one per known field).
 *
 * Emits fields in a stable order: id, status, port, bytes_in, bytes_out,
 * last_cmd. Missing fields are omitted.
 *
 * @param r JSON object.
 * @return Multi-line string ending with '\n'.
 */
static std::string format_status_block(const json &r)
{
    std::string out;
    auto add_int = [&](const char *k) {
        if (r.contains(k)) {
            out += k; out += "="; out += std::to_string(r.value(k, 0)); out += "\n";
        }
    };
    auto add_str = [&](const char *k) {
        if (r.contains(k)) {
            out += k; out += "="; out += r.value(k, std::string("")); out += "\n";
        }
    };
    add_int("id");
    if (r.contains("status")) {
        out += "status="; out += status_label(r.value("status", -1)); out += "\n";
    }
    add_int("port");
    add_int("bytes_in");
    add_int("bytes_out");
    add_str("last_cmd");
    return out;
}
```

- [ ] **Step 4: Implement `cmd_status` and wire dispatch**

In the anonymous namespace:

```cpp
/**
 * @brief Execute the `status` subcommand.
 *
 * @param g    Globals.
 * @param argc Remaining argc (subcommand + args).
 * @param argv Remaining argv.
 * @return exit code.
 */
static int cmd_status(const globals &g, int argc, char **argv)
{
    if (argc != 2) {
        std::fprintf(stderr, "clansimctl: status requires <id>\n");
        return RPC_USAGE;
    }
    int id = std::atoi(argv[1]);
    json reply;
    int rc = rpc_call(g.ctl_endpoint,
                      g.timeout_secs > 0 ? g.timeout_secs * 1000 : -1,
                      build_id_req("STATUS", id, req_id_gen()), reply);
    if (rc != RPC_OK) return rc;
    if (!reply.value("ok", false)) {
        std::fprintf(stderr, "clansimctl: %s\n",
                     reply.value("error", std::string("(no error text)")).c_str());
        return RPC_ERR;
    }
    if (g.json_out) {
        std::fputs(reply.dump().c_str(), stdout);
        std::fputc('\n', stdout);
    } else {
        std::fputs(format_status_block(reply.value("result", json::object())).c_str(),
                   stdout);
    }
    return RPC_OK;
}
```

Extend dispatch:

```cpp
    else if (sub == "status") return cmd_status(g, rc_argc, rc_argv);
```

- [ ] **Step 5: Run tests + smoke**

Run: `nmake ncland_unit_tests && ./ncland_unit_tests -s Clansimctl`
Expected: all pass.

Run: `nmake $(PBIN)/clansimctl && $PBIN/clansimctl status`
Expected: stderr `clansimctl: status requires <id>`, exit 2.

- [ ] **Step 6: Commit**

```bash
git add cnc/ncland/src/clansimctl.cpp cnc/ncland/src/clansimctl_tests.cpp
git commit -m "clansimctl: status subcommand (key=value block)"
```

---

### Task 7: `start` subcommand

**Files:**
- Modify: `cnc/ncland/src/clansimctl.cpp`
- Modify: `cnc/ncland/src/clansimctl_tests.cpp`

- [ ] **Step 1: Write failing test**

Append to `clansimctl_tests.cpp`:

```cpp
TEST("Clansimctl", "build_start_req with dtype") {
    json r = build_start_req(/*dtype*/999, /*script*/"", /*neid*/7,
                             /*tid*/"NE7", /*port*/0, "rid-a");
    REQUIRE_EQ(r["cmd"], "START");
    REQUIRE_EQ(r["req_id"], "rid-a");
    REQUIRE_EQ(r["args"]["dtype"], 999);
    REQUIRE_EQ(r["args"]["neid"],  7);
    REQUIRE_EQ(r["args"]["tid"],   "NE7");
    REQUIRE_EQ(r["args"]["port"],  0);
    REQUIRE(!r["args"].contains("script"));
}

TEST("Clansimctl", "build_start_req with script skips dtype") {
    json r = build_start_req(/*dtype*/-1, /*script*/"/tmp/x.clansim",
                             /*neid*/0, /*tid*/"", /*port*/20500, "rid-b");
    REQUIRE_EQ(r["args"]["script"], "/tmp/x.clansim");
    REQUIRE_EQ(r["args"]["port"],   20500);
    REQUIRE(!r["args"].contains("dtype"));
    REQUIRE(!r["args"].contains("tid"));
    REQUIRE(!r["args"].contains("neid"));
}
```

- [ ] **Step 2: Run tests, confirm failure**

Run: `nmake ncland_unit_tests`
Expected: FAIL — `build_start_req` undeclared.

- [ ] **Step 3: Implement `build_start_req`**

In the anonymous namespace:

```cpp
/**
 * @brief Build a START envelope.
 *
 * If @p script is non-empty, it wins over @p dtype (matches daemon
 * resolution: explicit script bypasses dtype lookup). Empty tid / zero neid
 * are omitted so the daemon sees only supplied fields.
 *
 * @param dtype  Device type, -1 = omit (when using script).
 * @param script Explicit script path, "" = omit.
 * @param neid   Network element id, 0 = omit.
 * @param tid    Terminal id (label), "" = omit.
 * @param port   TCP port hint, 0 = auto (always emitted).
 * @param req_id Request id to echo.
 * @return JSON envelope.
 */
static json build_start_req(int dtype, const std::string &script,
                            int neid, const std::string &tid, int port,
                            const std::string &req_id)
{
    json args = json::object();
    if (!script.empty())      args["script"] = script;
    else if (dtype >= 0)      args["dtype"]  = dtype;
    if (neid != 0)            args["neid"]   = neid;
    if (!tid.empty())         args["tid"]    = tid;
    args["port"] = port;
    return json{
        {"v", 1},
        {"cmd", "START"},
        {"req_id", req_id},
        {"args", args}
    };
}
```

- [ ] **Step 4: Implement `cmd_start`**

```cpp
/**
 * @brief Execute the `start` subcommand.
 *
 * @param g    Globals.
 * @param argc Remaining argc (argv[0] = "start", followed by flags).
 * @param argv Remaining argv.
 * @return exit code.
 */
static int cmd_start(const globals &g, int argc, char **argv)
{
    int dtype = -1, neid = 0, port = 0;
    std::string script, tid;

    for (int i = 1; i < argc; i++) {
        const char *a = argv[i];
        if      (!std::strcmp(a, "--dtype")  && i + 1 < argc) dtype  = std::atoi(argv[++i]);
        else if (!std::strcmp(a, "--script") && i + 1 < argc) script = argv[++i];
        else if (!std::strcmp(a, "--neid")   && i + 1 < argc) neid   = std::atoi(argv[++i]);
        else if (!std::strcmp(a, "--tid")    && i + 1 < argc) tid    = argv[++i];
        else if (!std::strcmp(a, "--port")   && i + 1 < argc) port   = std::atoi(argv[++i]);
        else {
            std::fprintf(stderr, "clansimctl: start: bad flag %s\n", a);
            return RPC_USAGE;
        }
    }
    if (dtype < 0 && script.empty()) {
        std::fprintf(stderr, "clansimctl: start: --dtype or --script required\n");
        return RPC_USAGE;
    }

    json reply;
    int rc = rpc_call(g.ctl_endpoint,
                      g.timeout_secs > 0 ? g.timeout_secs * 1000 : -1,
                      build_start_req(dtype, script, neid, tid, port, req_id_gen()),
                      reply);
    if (rc != RPC_OK) return rc;
    if (!reply.value("ok", false)) {
        std::fprintf(stderr, "clansimctl: %s\n",
                     reply.value("error", std::string("(no error text)")).c_str());
        return RPC_ERR;
    }
    if (g.json_out) {
        std::fputs(reply.dump().c_str(), stdout);
        std::fputc('\n', stdout);
    } else {
        const auto &res = reply["result"];
        std::printf("id=%d port=%d\n",
                    res.value("id", 0), res.value("port", 0));
    }
    return RPC_OK;
}
```

Extend dispatch:

```cpp
    else if (sub == "start")  return cmd_start(g, rc_argc, rc_argv);
```

- [ ] **Step 5: Run tests + smoke**

Run: `nmake ncland_unit_tests && ./ncland_unit_tests -s Clansimctl`
Expected: all Clansimctl tests pass.

Run: `nmake $(PBIN)/clansimctl && $PBIN/clansimctl start`
Expected: `clansimctl: start: --dtype or --script required`, exit 2.

- [ ] **Step 6: Commit**

```bash
git add cnc/ncland/src/clansimctl.cpp cnc/ncland/src/clansimctl_tests.cpp
git commit -m "clansimctl: start subcommand (dtype or script)"
```

---

### Task 8: `inject` subcommand — reply string or file

**Files:**
- Modify: `cnc/ncland/src/clansimctl.cpp`
- Modify: `cnc/ncland/src/clansimctl_tests.cpp`

- [ ] **Step 1: Write failing tests**

Append to `clansimctl_tests.cpp`:

```cpp
TEST("Clansimctl", "build_inject_req with inline reply") {
    json r = build_inject_req(3, "show ver", "IOS 12\n", "rid-i");
    REQUIRE_EQ(r["cmd"], "INJECT");
    REQUIRE_EQ(r["args"]["id"], 3);
    REQUIRE_EQ(r["args"]["cmd"],   "show ver");
    REQUIRE_EQ(r["args"]["reply"], "IOS 12\n");
}

TEST("Clansimctl", "read_reply_file slurps file contents") {
    char path[] = "/tmp/clansimctl_reply_XXXXXX";
    int fd = mkstemp(path);
    const char *body = "hello world\n";
    write(fd, body, std::strlen(body));
    close(fd);
    std::string got;
    REQUIRE_EQ(read_reply_file(path, got), 0);
    REQUIRE_EQ(got, std::string("hello world\n"));
    unlink(path);
}

TEST("Clansimctl", "read_reply_file missing file returns -1") {
    std::string got;
    REQUIRE_EQ(read_reply_file("/tmp/clansimctl_no_such_file_zzz", got), -1);
}
```

- [ ] **Step 2: Run tests, confirm failure**

Run: `nmake ncland_unit_tests`
Expected: FAIL — `build_inject_req` and `read_reply_file` undeclared.

- [ ] **Step 3: Implement helpers**

Add `#include <cerrno>` and `#include <sys/stat.h>` to the top of `clansimctl.cpp` (in addition to what's already there).

In the anonymous namespace:

```cpp
/**
 * @brief Read an entire file into a string.
 *
 * @param path Filesystem path.
 * @param out  Output: file contents (unchanged on error).
 * @return 0 on success, -1 on any error (message logged to stderr).
 */
static int read_reply_file(const std::string &path, std::string &out)
{
    FILE *f = std::fopen(path.c_str(), "rb");
    if (!f) {
        std::fprintf(stderr, "clansimctl: open %s: %s\n",
                     path.c_str(), std::strerror(errno));
        return -1;
    }
    char buf[4096];
    while (size_t n = std::fread(buf, 1, sizeof(buf), f)) out.append(buf, n);
    std::fclose(f);
    return 0;
}

/**
 * @brief Build an INJECT envelope.
 *
 * @param id     Target sim id.
 * @param cmd    Command string the sim receives (subject to daemon-side mangling).
 * @param reply  Canned reply body.
 * @param req_id Request id.
 * @return JSON envelope.
 */
static json build_inject_req(int id, const std::string &cmd,
                             const std::string &reply,
                             const std::string &req_id)
{
    return json{
        {"v", 1},
        {"cmd", "INJECT"},
        {"req_id", req_id},
        {"args", {{"id", id}, {"cmd", cmd}, {"reply", reply}}}
    };
}
```

- [ ] **Step 4: Implement `cmd_inject`**

```cpp
/**
 * @brief Execute the `inject` subcommand.
 *
 * Usage: inject <id> --cmd STR (--reply STR | --reply-file PATH)
 *
 * @param g    Globals.
 * @param argc Remaining argc.
 * @param argv Remaining argv.
 * @return exit code.
 */
static int cmd_inject(const globals &g, int argc, char **argv)
{
    if (argc < 4) {
        std::fprintf(stderr, "clansimctl: inject <id> --cmd STR (--reply STR | --reply-file PATH)\n");
        return RPC_USAGE;
    }
    int id = std::atoi(argv[1]);
    std::string cmd, reply, reply_file;
    for (int i = 2; i < argc; i++) {
        const char *a = argv[i];
        if      (!std::strcmp(a, "--cmd")        && i + 1 < argc) cmd        = argv[++i];
        else if (!std::strcmp(a, "--reply")      && i + 1 < argc) reply      = argv[++i];
        else if (!std::strcmp(a, "--reply-file") && i + 1 < argc) reply_file = argv[++i];
        else {
            std::fprintf(stderr, "clansimctl: inject: bad flag %s\n", a);
            return RPC_USAGE;
        }
    }
    if (cmd.empty()) {
        std::fprintf(stderr, "clansimctl: inject: --cmd required\n");
        return RPC_USAGE;
    }
    if (reply.empty() == reply_file.empty()) {
        std::fprintf(stderr, "clansimctl: inject: exactly one of --reply or --reply-file required\n");
        return RPC_USAGE;
    }
    if (!reply_file.empty() && read_reply_file(reply_file, reply) != 0) {
        return RPC_ERR;
    }

    json out;
    int rc = rpc_call(g.ctl_endpoint,
                      g.timeout_secs > 0 ? g.timeout_secs * 1000 : -1,
                      build_inject_req(id, cmd, reply, req_id_gen()), out);
    if (rc != RPC_OK) return rc;
    if (!out.value("ok", false)) {
        std::fprintf(stderr, "clansimctl: %s\n",
                     out.value("error", std::string("(no error text)")).c_str());
        return RPC_ERR;
    }
    if (g.json_out) {
        std::fputs(out.dump().c_str(), stdout);
        std::fputc('\n', stdout);
    } else {
        std::fputs("ok\n", stdout);
    }
    return RPC_OK;
}
```

Extend dispatch:

```cpp
    else if (sub == "inject") return cmd_inject(g, rc_argc, rc_argv);
```

- [ ] **Step 5: Run tests, confirm pass**

Run: `nmake ncland_unit_tests && ./ncland_unit_tests -s Clansimctl`
Expected: all Clansimctl tests pass.

Run: `nmake $(PBIN)/clansimctl && $PBIN/clansimctl inject 1 --cmd 'x'`
Expected: `clansimctl: inject: exactly one of --reply or --reply-file required`, exit 2.

- [ ] **Step 6: Commit**

```bash
git add cnc/ncland/src/clansimctl.cpp cnc/ncland/src/clansimctl_tests.cpp
git commit -m "clansimctl: inject subcommand (--reply / --reply-file)"
```

---

### Task 9: Docs — append Client section to CLANSIMD.md

**Files:**
- Modify: `cnc/ncland/src/CLANSIMD.md`

- [ ] **Step 1: Locate the `Example session` heading**

Confirm it is at line ~126 with `== Example session` (asciidoc).

- [ ] **Step 2: Append a new `== Client` section after `== Example session`**

Add this content immediately after the closing of the `Example session` code block and before `== Architecture`:

```
== Client

`clansimctl` is the companion CLI. Subcommands map 1:1 to daemon verbs; human-
readable output by default, `--json` for scripting. See
`~/WorkNotes/specs/2026-07-24-clansimctl-cli-design.md` for the design.

[source,sh]
----
# Global flags: --ctl ENDPOINT (default $CLANSIMD_CTL or ipc:///tmp/clansimd.ctl),
#               --timeout SECS (default 5), --json.

clansimctl start  --dtype 999 --neid 7 --tid NE7
# id=1 port=20000

clansimctl list
# ID  DTYPE  NEID  TID  PORT   STATUS
# 1   999    7     NE7  20000  connected

clansimctl status 1
# id=1
# status=connected
# port=20000

clansimctl inject 1 --cmd 'PING' --reply 'PONG'
# ok

clansimctl stop   1
# ok
----

Exit codes: `0` ok, `1` daemon error or transport failure, `2` usage, `3` recv timeout.
```

- [ ] **Step 3: Commit**

```bash
git add cnc/ncland/src/CLANSIMD.md
git commit -m "clansimctl: document CLI in CLANSIMD.md"
```

---

### Task 10: End-to-end smoke against a live daemon

**Files:** none modified — this task is manual verification.

- [ ] **Step 1: Prepare a fresh scratch dir**

```bash
rm -rf /tmp/clansimctl-smoke
mkdir  /tmp/clansimctl-smoke
```

- [ ] **Step 2: Start clansimd in the background**

```bash
$PBIN/clansimd \
    --ctl ipc:///tmp/clansimctl-smoke.ctl \
    --sim-dir cnc/ncland/src/test/fixtures/sim \
    --lua-dir cnc/ncland/src/test/fixtures \
    --data-root /tmp/clansimctl-smoke \
    --port-base 20800 &
DPID=$!
sleep 0.3
```

- [ ] **Step 3: Exercise every subcommand**

```bash
export CLANSIMD_CTL=ipc:///tmp/clansimctl-smoke.ctl

$PBIN/clansimctl list                                       # header row only
$PBIN/clansimctl start --dtype 999 --neid 7 --tid NE7       # id=1 port=<...>
$PBIN/clansimctl list                                       # one row
$PBIN/clansimctl status 1                                   # id=1 status=listening ...
$PBIN/clansimctl inject 1 --cmd 'PING' --reply 'PONG'       # ok
$PBIN/clansimctl reset  1                                   # ok
$PBIN/clansimctl reload 1                                   # ok
$PBIN/clansimctl --json list                                # raw JSON
$PBIN/clansimctl stop   1                                   # ok
$PBIN/clansimctl list                                       # header row only again
```

All should exit 0. `$?` = 0 after each.

- [ ] **Step 4: Failure paths**

```bash
$PBIN/clansimctl stop 999          ; echo "expect 1, got $?"   # daemon error: no sim 999
$PBIN/clansimctl start             ; echo "expect 2, got $?"   # usage
$PBIN/clansimctl --ctl ipc:///nosuch.sock --timeout 1 list
echo "expect non-zero (1 or 3), got $?"
```

- [ ] **Step 5: Cleanup**

```bash
kill $DPID
wait $DPID 2>/dev/null
rm -f /tmp/clansimctl-smoke.ctl
rm -rf /tmp/clansimctl-smoke
```

- [ ] **Step 6: No commit needed**

If anything failed, file a follow-up task and fix before proceeding.

---

## Self-Review

**Spec coverage:**
- CLI surface (all subcommands + global flags) → Tasks 4/5/6/7/8.
- Exit codes → Task 3 defines the enum; each cmd_* returns them.
- `status` label mapping → Task 4 defines `status_label` used by both `list` and `status`.
- `--json` behavior → each cmd_* branches on `g.json_out`.
- Build wiring → Task 1.
- Tests → each pure helper has a test in Tasks 2/4/6/7/8.
- Docs → Task 9.
- End-to-end smoke → Task 10.

**Placeholder scan:** none — every step has concrete code or exact commands.

**Type consistency:** `globals` struct defined in Task 1, referenced by every `cmd_*`. `rpc_call` signature stable from Task 3. `build_id_req` covers STOP/RESET/RELOAD/STATUS (all four use `{args:{id:N}}`). Function names: `status_label`, `format_list_table`, `format_status_block`, `build_id_req`, `build_start_req`, `build_inject_req`, `read_reply_file`, `rpc_call`, `req_id_gen`, `parse_globals`, `usage`, `cmd_list/stop/reset/reload/status/start/inject` — no drift between tasks.
