# Route iimx traffic to niimxd — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a single `system_defines` toggle, `USE_NIIMXD=1`, that routes every iimx command in `cnc/utility/src/iimx_snd.c` away from SysV message queues and per-command `ssh` forks, and into `niimxd` over ZMQ.

**Architecture:** `cnc/utility/src/niimxlib.c` (an existing single-shot stub, already in `OBJ_INC`) grows into a real pipelined `ZMQ_DEALER` client with a singleton context, monotonic message IDs, and msgid-keyed demultiplexing. Each entry point in `iimx_snd.c` gains a one-line guard that diverts to it when the toggle is on. `niimxd`'s wire protocol gains an `rspfile` frame so large-response spill-to-file keeps parity with imsgx.

**Tech Stack:** C (gcc 4.8.5 baseline), C++11 for `niimxd`, ZeroMQ (`libzmq`), Lucent/AT&T nmake.

**Design spec:** `~/WorkNotes/plans/2026-09-11-iimx-route-to-niimxd-design.md`

**Build environment:** every `nmake` invocation below requires `BASE` set to the repo root and `VPATH` whose first segment equals `BASE`. Verify before starting:

```bash
test "$BASE" = "$(git rev-parse --show-toplevel)" && echo BASE_OK
test "${VPATH%%:*}" = "$BASE" && echo VPATH_OK
```

If either fails, stop and tell the user. Do not reconstruct the paths.

**Naming nmake targets:** `nmake` expands `$(PBIN)` and `$(LDIR)` to the *relative*
paths `../../../3b2/bin` and `../../../3b2/lib`, so a target must be named the same
way. `nmake ../../../3b2/bin/niimx_t` works; `nmake $BASE/3b2/bin/niimx_t` fails with
"don't know how to make". Use `$BASE` only for `cd` and for non-nmake commands.

---

## Background you need

`iimx` is the command transport between netFLEX processes and the `imsgx` back end. Today `iimx_snd.c` has two legacy transports:

- **Local** — a SysV message queue keyed `IIMX_IKEY`. The sender stamps `imsg.msgid` from the file-static counter `_IIMXmyid` (`iimx_snd.c:449`) and reads its reply back with `msgrcv(..., mtype = getpid(), ...)`. Multi-part replies are signalled by `imsg.mcont` being non-zero on every segment but the last.
- **Remote** — `iimx_sendx_remote()` (`:69`) builds a shell string and runs it through `vPopen`: one `fork` plus one full SSH handshake per command.

`niimxd` (`cnc/niimx/src/niimxd.cpp`) binds a `ZMQ_ROUTER` at `ipc:///usr/cnc/data/niimx.ipc` (`:820`). It is **not** request/reply — inbound requests land in a global `requestQ` drained by N `bchannel` worker slots, and nothing limits a client to one request in flight. A `ZMQ_DEALER` may therefore send N commands and read N replies, each tagged with the echoed `msgid`, arriving in *completion* order rather than send order.

Wire format today:

```
request:   identity | empty | host | msgid(4B) | mpid(4B) | tmout(4B) | command
response:  identity | empty | msgid(4B) | mcont(4B) | payload
```

`mcont` counts down from `segments-1` to `0`; `0` is the final segment. Segment size is 25 KB (`niimxd.cpp:1204`).

There are ~271 call sites of the iimx APIs across ~30 files. **The toggle must never appear at a call site** — every divert lives inside `iimx_snd.c`.

---

## File Structure

**Created:**

| File | Responsibility |
|---|---|
| `include/niimxlib.h` | Public `niimx_*` API — the only thing `iimx_snd.c` includes |
| `cnc/niimx/src/niimx_stub.cpp` | Test double: a ROUTER that speaks the wire format and echoes, so the client can be tested without a real `niimxd`, PTY, or network element |
| `cnc/niimx/src/niimx_t.cpp` | Test driver: calls the `iimx_snd.c` entry points and prints results in a greppable form |
| `cnc/niimx/src/test_niimx.sh` | Integration harness, modelled on the existing `test_nfdb.sh` |

**Modified:**

| File | Change |
|---|---|
| `cnc/utility/src/niimxlib.c` | Stub → full pipelined client |
| `cnc/utility/src/iimx_snd.c` | Nine divert guards + congestion-file switch |
| `cnc/utility/src/util.mk:397` | `libinc.so` links `-lzmq` via a local `mklib_inc` clone |
| `cnc/niimx/src/niimxd.cpp` | Parse, carry, and honour the `rspfile` frame |
| `cnc/niimx/src/niimx.cpp` | Send the `rspfile` frame; new `-R` flag |
| `cnc/niimx/src/Makefile` | Build the two new test binaries |

**Deliberately untouched:** `remote_req()` (`iimx_snd.c:281`) is generic `/fcgi/<service>` with one caller and carries non-iimx services. `iimx_swarm()` (`:2168`) shells out to `swarm` running `iisnd`, and `iisnd` calls `iimx_sendx` (`cnc/rcmd/src/iisnd.c:342`), so it inherits the toggle transitively. `iimx_multi_fp()` (`:1459`) is a thin wrapper over `iimx_sendx_batch` → `iimx_sendx_batch_wk`, covered by Task 16. `iimx_sendx_remote_old()` (`:87`) is dead code.

---

### Task 1: Test scaffolding

Nothing downstream can be tested without a fake `niimxd`. A real one needs PTY logins and live network elements, so build a double that speaks only the wire format.

**Files:**
- Create: `cnc/niimx/src/niimx_stub.cpp`
- Create: `cnc/niimx/src/test_niimx.sh`
- Modify: `cnc/niimx/src/Makefile`

- [ ] **Step 1: Write the stub server**

Create `cnc/niimx/src/niimx_stub.cpp`:

```cpp
// -*- compile-command: "nmake ../../../3b2/bin/niimx_stub"; -*-
/**
 * @file niimx_stub.cpp
 * @brief Test double for niimxd: a ROUTER speaking the niimx wire format.
 *
 * Exists so the client side can be exercised without PTY logins or live
 * network elements. Behaviour is driven entirely by the command text:
 *   echo:<text>     reply with <text>
 *   big:<n>         reply with <n> bytes of 'x' (exercises mcont segmenting)
 *   sleep:<n>       reply after <n> seconds (exercises client timeouts)
 *   drop            never reply (exercises hard-fail and stale-reply paths)
 * Any other command echoes itself back.
 */
#include <zmq.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <stdint.h>
#include <string>

static const size_t SEG_SZ = 25 * 1024;

/** @brief Receive one frame, reporting whether more follow. */
static bool recv_frame(void *sock, std::string &out, int &more)
{
    zmq_msg_t m;
    zmq_msg_init(&m);
    if (zmq_msg_recv(&m, sock, 0) < 0) { zmq_msg_close(&m); return false; }
    out.assign((char *)zmq_msg_data(&m), zmq_msg_size(&m));
    size_t sz = sizeof(more);
    zmq_getsockopt(sock, ZMQ_RCVMORE, &more, &sz);
    zmq_msg_close(&m);
    return true;
}

/** @brief Send one frame. */
static void send_frame(void *sock, const void *d, size_t n, bool more)
{
    zmq_msg_t m;
    zmq_msg_init_size(&m, n);
    if (n) memcpy(zmq_msg_data(&m), d, n);
    zmq_msg_send(&m, sock, more ? ZMQ_SNDMORE : 0);
}

/** @brief Send a response, segmented exactly as niimxd segments it. */
static void reply(void *sock, const std::string &id, int32_t msgid,
                  const std::string &body)
{
    size_t total = body.size(), off = 0;
    int segs = (int)((total + SEG_SZ - 1) / SEG_SZ);
    if (segs == 0) segs = 1;
    for (int s = 0; s < segs; s++) {
        size_t len = total - off < SEG_SZ ? total - off : SEG_SZ;
        int32_t mcont = segs - 1 - s;
        send_frame(sock, id.data(), id.size(), true);
        send_frame(sock, "", 0, true);
        send_frame(sock, &msgid, 4, true);
        send_frame(sock, &mcont, 4, true);
        send_frame(sock, body.data() + off, len, false);
        off += len;
    }
}

int main(int argc, char **argv)
{
    const char *ep = (argc > 1) ? argv[1] : "ipc:///tmp/niimx_stub.ipc";

    void *ctx = zmq_ctx_new();
    void *sock = zmq_socket(ctx, ZMQ_ROUTER);
    if (0 != zmq_bind(sock, ep)) {
        fprintf(stderr, "niimx_stub: bind(%s) failed\n", ep);
        return 1;
    }
    fprintf(stderr, "niimx_stub: bound %s\n", ep);

    while (1) {
        std::string id, empty, host, s_msgid, s_mpid, s_tmout, cmd;
        int more = 0;

        if (!recv_frame(sock, id, more)      || !more) continue;
        if (!recv_frame(sock, empty, more)   || !more) continue;
        if (!recv_frame(sock, host, more)    || !more) continue;
        if (!recv_frame(sock, s_msgid, more) || !more) continue;
        if (!recv_frame(sock, s_mpid, more)  || !more) continue;
        if (!recv_frame(sock, s_tmout, more) || !more) continue;
        if (!recv_frame(sock, cmd, more))             continue;
        while (more) { std::string x; recv_frame(sock, x, more); }

        int32_t msgid = 0;
        if (s_msgid.size() >= 4) memcpy(&msgid, s_msgid.data(), 4);

        if (cmd == "drop") continue;

        if (cmd.compare(0, 6, "sleep:") == 0) {
            sleep(atoi(cmd.c_str() + 6));
            reply(sock, id, msgid, "slept");
            continue;
        }
        if (cmd.compare(0, 4, "big:") == 0) {
            reply(sock, id, msgid, std::string(atoi(cmd.c_str() + 4), 'x'));
            continue;
        }
        if (cmd.compare(0, 5, "echo:") == 0) {
            reply(sock, id, msgid, cmd.substr(5));
            continue;
        }
        reply(sock, id, msgid, cmd);
    }
    return 0;
}
```

- [ ] **Step 2: Add the stub to the build**

In `cnc/niimx/src/Makefile`, extend `.ALL` and add a target. Change:

```
.ALL :  $(PBIN)/niimxd $(PBIN)/niimx $(PBIN)/nfdb $(LDIR)/libnfdb.$(LIBTYPE)
```

to:

```
.ALL :  $(PBIN)/niimxd $(PBIN)/niimx $(PBIN)/nfdb $(LDIR)/libnfdb.$(LIBTYPE) $(PBIN)/niimx_stub
```

and append at end of file:

```
$(PBIN)/niimx_stub : niimx_stub.o
	$(CPLUS_CC) $(LDFLAGS) -o $(<) $(*) -lzmq
```

Note nmake, not GNU make: `$(<)` is the target, `$(*)` all prerequisites. Actions are tab-indented.

- [ ] **Step 3: Build it and verify it runs**

```bash
cd $BASE/cnc/niimx/src && nmake ../../../3b2/bin/niimx_stub
$BASE/3b2/bin/niimx_stub ipc:///tmp/t.ipc &
sleep 1
$BASE/3b2/bin/niimx -e ipc:///tmp/t.ipc -c 'echo:hello' -w 5 -x hello && echo STUB_OK
kill %1
```

Expected: `STUB_OK`. (`niimx` still sends the pre-`rspfile` frame layout here; the stub tolerates it because Task 7 has not landed yet. After Task 7 both move together.)

- [ ] **Step 4: Write the harness skeleton**

Create `cnc/niimx/src/test_niimx.sh`:

```bash
#!/bin/bash
# Integration tests for the niimx client library and the iimx_snd.c diverts.
# Usage: ./test_niimx.sh [path_to_bindir]
set -u

BINDIR="${1:-../../../3b2/bin}"
ENDPOINT="ipc:///tmp/test_niimx_$$.sock"
PASS=0
FAIL=0
STUB_PID=0

cleanup() {
    [ "$STUB_PID" -gt 0 ] 2>/dev/null && kill "$STUB_PID" 2>/dev/null
    rm -f "/tmp/test_niimx_$$.sock"
    echo ""
    echo "Results: $PASS passed, $FAIL failed"
    [ "$FAIL" -eq 0 ] && exit 0 || exit 1
}
trap cleanup EXIT

start_stub() {
    "$BINDIR/niimx_stub" "$ENDPOINT" 2>/dev/null &
    STUB_PID=$!
    sleep 1
}

stop_stub() {
    [ "$STUB_PID" -gt 0 ] 2>/dev/null && kill "$STUB_PID" 2>/dev/null
    STUB_PID=0
    sleep 1
}

# assert_contains <desc> <needle> <command...>
assert_contains() {
    local desc="$1" needle="$2"; shift 2
    local out
    out=$("$@" 2>&1)
    if echo "$out" | grep -q -- "$needle"; then
        PASS=$((PASS + 1)); echo "  PASS: $desc"
    else
        FAIL=$((FAIL + 1)); echo "  FAIL: $desc"; echo "    wanted: $needle"; echo "    got: $out"
    fi
}

echo "=== niimx stub reachable ==="
start_stub
assert_contains "stub echoes" "hello" \
    "$BINDIR/niimx" -e "$ENDPOINT" -c 'echo:hello' -w 5
stop_stub
```

- [ ] **Step 5: Run the harness**

```bash
chmod +x $BASE/cnc/niimx/src/test_niimx.sh
cd $BASE/cnc/niimx/src && ./test_niimx.sh
```

Expected: `Results: 1 passed, 0 failed`.

- [ ] **Step 6: Commit**

```bash
cd $BASE
git add cnc/niimx/src/niimx_stub.cpp cnc/niimx/src/test_niimx.sh cnc/niimx/src/Makefile
git commit -m "niimx: add wire-protocol test stub and integration harness

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 2: `niimxlib` — public header, endpoint resolution, singleton socket

The existing `niimxlib.c` builds a whole ZMQ context per call (`:33`, `:132-133`) and connects to a hardcoded literal while ignoring the `NIIMX.ENDPOINT=` sysdef it just read (`:37` vs `:55`). Fix both.

**Files:**
- Create: `include/niimxlib.h`
- Modify: `cnc/utility/src/niimxlib.c`
- Modify: `cnc/niimx/src/test_niimx.sh`
- Create: `cnc/niimx/src/niimx_t.cpp`
- Modify: `cnc/niimx/src/Makefile`

- [ ] **Step 1: Write the failing test**

Create `cnc/niimx/src/niimx_t.cpp` — the driver that lets the shell harness reach into `libinc`:

```cpp
// -*- compile-command: "nmake ../../../3b2/bin/niimx_t"; -*-
/**
 * @file niimx_t.cpp
 * @brief Test driver exercising the niimx client library and iimx_snd.c diverts.
 *
 * Each subcommand prints a single greppable line so test_niimx.sh can assert
 * on it. Subcommands are added by the task that needs them.
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

extern "C" {
#include <ioprint.h>     /* struct iob, used by struct iimx_ri */
#include <iimx_snd.h>    /* struct iimx_ri, iimx_batch2_ri, iimx_pool_ri */
#include <niimxlib.h>
}

static int cmd_endpoint()
{
    printf("ENDPOINT=%s\n", niimx_endpoint());
    return 0;
}

int main(int argc, char **argv)
{
    if (argc < 2) { fprintf(stderr, "usage: niimx_t <subcommand> [args]\n"); return 2; }
    if (0 == strcmp(argv[1], "endpoint")) return cmd_endpoint();
    fprintf(stderr, "niimx_t: unknown subcommand %s\n", argv[1]);
    return 2;
}
```

Add to `cnc/niimx/src/Makefile` — extend `.ALL` with `$(PBIN)/niimx_t` and append:

```
$(PBIN)/niimx_t : niimx_t.o $(CORELIBS) -linc
	$(CPLUS_CC) $(LDFLAGS) -o $(<) $(*) -lzmq
```

Append to `cnc/niimx/src/test_niimx.sh`:

```bash
echo "=== endpoint resolution ==="
assert_contains "endpoint honours NIIMX.ENDPOINT sysdef" "ENDPOINT=$ENDPOINT" \
    env NIIMX_ENDPOINT_OVERRIDE="$ENDPOINT" "$BINDIR/niimx_t" endpoint
```

- [ ] **Step 2: Run it to verify it fails**

```bash
cd $BASE/cnc/niimx/src && nmake ../../../3b2/bin/niimx_t
```

Expected: FAIL — `fatal error: niimxlib.h: No such file or directory`.

- [ ] **Step 3: Write the header**

Create `include/niimxlib.h`:

```c
#ifndef niimxlib_include
#define niimxlib_include

#include <stdint.h>

/** @brief Default ZMQ endpoint of the niimxd ROUTER socket. */
#define NIIMX_ENDPOINT_DEFAULT "ipc:///usr/cnc/data/niimx.ipc"

/** @brief Flag file niimxd writes when it is shedding load. */
#define NIIMX_CONGESTED_FILE "/usr/cnc/data/niimx.congested"

/** @brief niimx_recv() return code: no response arrived before the deadline. */
#define NIIMX_TIMEDOUT (-2)

#ifdef __cplusplus
extern "C" {
#endif

/**
 * @brief Resolved niimxd endpoint for this process.
 *
 * Read once from the NIIMX_ENDPOINT_OVERRIDE environment variable, then the
 * NIIMX.ENDPOINT= system define, then NIIMX_ENDPOINT_DEFAULT. The environment
 * override exists so the test harness can point at a stub.
 *
 * @return Endpoint string owned by the library; never NULL.
 */
const char *niimx_endpoint(void);

/**
 * @brief Report whether niimxd appears to be listening.
 *
 * zmq_connect() to an IPC endpoint succeeds even with nothing bound, so
 * liveness is established by the presence of the socket file. Endpoints that
 * are not ipc:// have no filesystem path and are reported available.
 *
 * @return Non-zero if the daemon appears reachable, 0 otherwise.
 */
int niimx_avail(void);

#ifdef __cplusplus
}
#endif

#endif /* niimxlib_include */
```

- [ ] **Step 4: Implement endpoint resolution and the singleton socket**

Replace the whole body of `cnc/utility/src/niimxlib.c` with:

```c
// -*- compile-command: "nmake -f util.mk  ../../../3b2/lib/libinc.so"; -*-
/**
 * @file niimxlib.c
 * @brief ZMQ DEALER client for the niimxd command daemon.
 *
 * One context and one DEALER socket per process, created on first use and
 * never torn down, so that pipelined callers (the iimx pool and batch paths)
 * do not pay context setup per command.
 */

#include <zmq.h>
#include <stdint.h>
#include <string.h>
#include <stdlib.h>
#include <unistd.h>
#include <fcntl.h>
#include <utillibinc.h>
#include <niimxlib.h>

static char  Endpoint[256] = {0};
static void *Ctx  = NULL;
static void *Sock = NULL;

const char *niimx_endpoint(void)
{
	if (Endpoint[0]) return Endpoint;

	const char *env = getenv("NIIMX_ENDPOINT_OVERRIDE");
	if (env && env[0]) {
		strncpy(Endpoint, env, sizeof(Endpoint) - 1);
		return Endpoint;
	}

	get_sysdef_str("NIIMX.ENDPOINT=", Endpoint, sizeof(Endpoint) - 1);

	if (!Endpoint[0])
		strncpy(Endpoint, NIIMX_ENDPOINT_DEFAULT, sizeof(Endpoint) - 1);

	return Endpoint;
}

int niimx_avail(void)
{
	const char *ep = niimx_endpoint();

	/* Only ipc:// endpoints have a filesystem path to probe. */
	if (0 != strncmp(ep, "ipc://", 6)) return 1;

	return (0 == access(ep + 6, F_OK));
}

/**
 * @brief Lazily create the process-wide DEALER socket.
 *
 * The identity must be unique per socket, not merely per process: two sockets
 * sharing one identity make the ROUTER misroute replies.
 *
 * @return The connected socket, or NULL on failure.
 */
static void *niimx_sock(void)
{
	static int seq = 0;
	char identity[64];

	if (Sock) return Sock;

	if (!Ctx && !(Ctx = zmq_ctx_new())) return NULL;

	if (!(Sock = zmq_socket(Ctx, ZMQ_DEALER))) return NULL;

	snprintf(identity, sizeof(identity), "niimxlib-%d-%d", (int)getpid(), seq++);
	zmq_setsockopt(Sock, ZMQ_IDENTITY, identity, strlen(identity));

	if (0 != zmq_connect(Sock, niimx_endpoint())) {
		zmq_close(Sock);
		Sock = NULL;
		return NULL;
	}

	return Sock;
}
```

The old `niimx()` function is removed. It had no callers anywhere in the tree — confirm with `grep -rn '\bniimx(' --include=*.c --include=*.cpp $BASE` before deleting.

- [ ] **Step 5: Build and run the test**

```bash
cd $BASE/cnc/utility/src && nmake -f util.mk ../../../3b2/lib/libinc.so
cd $BASE/cnc/niimx/src && nmake ../../../3b2/bin/niimx_t && ./test_niimx.sh
```

Expected: `Results: 2 passed, 0 failed`.

`niimx_sock()` is `static` and unused at this point, so expect a `-Wunused-function` warning. Task 3 uses it.

- [ ] **Step 6: Commit**

```bash
cd $BASE
git add include/niimxlib.h cnc/utility/src/niimxlib.c cnc/niimx/src/niimx_t.cpp cnc/niimx/src/Makefile cnc/niimx/src/test_niimx.sh
git commit -m "niimxlib: singleton DEALER socket and working endpoint override

The NIIMX.ENDPOINT= sysdef was read but discarded in favour of a hardcoded
literal, and every call built and destroyed its own ZMQ context.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 3: `niimxlib` — pipelined send and receive

Split the transaction so callers can send N commands and then collect N replies, matching what the pool and batch paths already do with `msgsnd`/`msgrcv`.

**Files:**
- Modify: `include/niimxlib.h`
- Modify: `cnc/utility/src/niimxlib.c`
- Modify: `cnc/niimx/src/niimx_t.cpp`
- Modify: `cnc/niimx/src/test_niimx.sh`

- [ ] **Step 1: Write the failing test**

Add to `cnc/niimx/src/niimx_t.cpp`, above `main`:

```cpp
/** @brief Fire N commands, then collect N replies, printing completion order. */
static int cmd_pipeline(int n)
{
    int32_t *ids = (int32_t *)calloc(n, sizeof(int32_t));
    char cmd[64];

    for (int i = 0; i < n; i++) {
        ids[i] = niimx_next_msgid();
        snprintf(cmd, sizeof(cmd), "echo:cmd%d", i);
        if (0 != niimx_send("", cmd, ids[i], 30, 0)) {
            printf("SEND_FAIL=%d\n", i);
            free(ids);
            return 1;
        }
    }

    for (int got = 0; got < n; got++) {
        int32_t rid = 0;
        char *rsp = NULL;
        int nbytes = 0;

        if (0 != niimx_recv(&rid, &rsp, &nbytes, 5000)) {
            printf("RECV_FAIL=%d\n", got);
            free(ids);
            return 1;
        }
        for (int i = 0; i < n; i++)
            if (ids[i] == rid) printf("MATCH=%d:%s\n", i, rsp);
        free(rsp);
    }

    free(ids);
    printf("PIPELINE_OK\n");
    return 0;
}

/** @brief Reassemble a large segmented response and report its length. */
static int cmd_big(int n)
{
    int32_t id = niimx_next_msgid(), rid = 0;
    char *rsp = NULL;
    int nbytes = 0;
    char cmd[64];

    snprintf(cmd, sizeof(cmd), "big:%d", n);
    if (0 != niimx_send("", cmd, id, 30, 0)) { printf("SEND_FAIL\n"); return 1; }
    if (0 != niimx_recv(&rid, &rsp, &nbytes, 10000)) { printf("RECV_FAIL\n"); return 1; }

    printf("BIG_LEN=%d MSGID_MATCH=%d\n", nbytes, (rid == id));
    free(rsp);
    return 0;
}

/** @brief Send a doomed command, let it time out, prove the late reply is dropped. */
static int cmd_stale()
{
    int32_t slow = niimx_next_msgid(), rid = 0;
    char *rsp = NULL;
    int nbytes = 0;

    /* This one will answer in 3s, but we only wait 1s for it. */
    niimx_send("", "sleep:3", slow, 30, 0);
    if (NIIMX_TIMEDOUT != niimx_recv(&rid, &rsp, &nbytes, 1000)) {
        printf("STALE_FAIL=no_timeout\n");
        return 1;
    }

    /* Now a fresh command. Its reply must be the one we get back. */
    int32_t fresh = niimx_next_msgid();
    niimx_send("", "echo:fresh", fresh, 30, 0);

    for (int tries = 0; tries < 10; tries++) {
        rsp = NULL;
        if (0 != niimx_recv(&rid, &rsp, &nbytes, 5000)) continue;
        if (rid == slow) { free(rsp); continue; }   /* stale — library may surface it */
        printf("STALE_OK rid_is_fresh=%d body=%s\n", (rid == fresh), rsp);
        free(rsp);
        return 0;
    }
    printf("STALE_FAIL=no_fresh_reply\n");
    return 1;
}
```

and in `main`, before the unknown-subcommand line:

```cpp
    if (0 == strcmp(argv[1], "pipeline")) return cmd_pipeline(argc > 2 ? atoi(argv[2]) : 4);
    if (0 == strcmp(argv[1], "big"))      return cmd_big(argc > 2 ? atoi(argv[2]) : 100000);
    if (0 == strcmp(argv[1], "stale"))    return cmd_stale();
```

Append to `cnc/niimx/src/test_niimx.sh`:

```bash
echo "=== pipelined send/recv ==="
start_stub
assert_contains "four commands pipelined" "PIPELINE_OK" \
    env NIIMX_ENDPOINT_OVERRIDE="$ENDPOINT" "$BINDIR/niimx_t" pipeline 4
assert_contains "each reply matched to its sender" "MATCH=3:cmd3" \
    env NIIMX_ENDPOINT_OVERRIDE="$ENDPOINT" "$BINDIR/niimx_t" pipeline 4
assert_contains "segmented response reassembled" "BIG_LEN=100000 MSGID_MATCH=1" \
    env NIIMX_ENDPOINT_OVERRIDE="$ENDPOINT" "$BINDIR/niimx_t" big 100000
assert_contains "late reply not misattributed" "rid_is_fresh=1" \
    env NIIMX_ENDPOINT_OVERRIDE="$ENDPOINT" "$BINDIR/niimx_t" stale
stop_stub
```

- [ ] **Step 2: Run to verify it fails**

```bash
cd $BASE/cnc/niimx/src && nmake ../../../3b2/bin/niimx_t
```

Expected: FAIL — `niimx_next_msgid` / `niimx_send` / `niimx_recv` were not declared.

- [ ] **Step 3: Declare the API**

Add to `include/niimxlib.h`, inside the `extern "C"` block:

```c
/**
 * @brief Allocate the next message ID for this process.
 *
 * IDs are monotonic and independent of the legacy _IIMXmyid counter so that
 * a process running both transports cannot collide them.
 *
 * @return A message ID unique within this process.
 */
int32_t niimx_next_msgid(void);

/**
 * @brief Send one command to niimxd without waiting for its reply.
 *
 * @param host     Target host; "" or NULL for local.
 * @param cmd      Command text.
 * @param msgid    ID from niimx_next_msgid(), echoed back on the reply.
 * @param tmout    Command deadline in seconds, passed to niimxd.
 * @param rspfile  Non-zero to ask niimxd to spill the response to a file.
 * @return 0 on success, -1 on failure.
 */
int niimx_send(const char *host, const char *cmd, int32_t msgid,
               int32_t tmout, int32_t rspfile);

/**
 * @brief Receive one complete response, reassembling all mcont segments.
 *
 * @param msgid   Out: the echoed message ID identifying which command replied.
 * @param rsp     Out: malloc'd NUL-terminated body; caller frees.
 * @param nbytes  Out: body length excluding the NUL.
 * @param tmout_ms  Milliseconds to wait; <= 0 blocks indefinitely.
 * @return 0 on success, NIIMX_TIMEDOUT if nothing arrived, -1 on error.
 */
int niimx_recv(int32_t *msgid, char **rsp, int *nbytes, int tmout_ms);
```

- [ ] **Step 4: Implement them**

Append to `cnc/utility/src/niimxlib.c`:

```c
/** @brief Send one ZMQ frame on @p sock. */
static void niimx_send_frame(void *sock, const void *data, size_t len, int more)
{
	zmq_msg_t m;
	zmq_msg_init_size(&m, len);
	if (len) memcpy(zmq_msg_data(&m), data, len);
	zmq_msg_send(&m, sock, more ? ZMQ_SNDMORE : 0);
}

int32_t niimx_next_msgid(void)
{
	static int32_t next = 0;

	/* Seed from the pid so concurrent processes do not share a low ID space
	   in traces; uniqueness on the wire comes from the ROUTER identity. */
	if (0 == next) next = ((int32_t)getpid() << 8) + 1;

	return next++;
}

int niimx_send(const char *host, const char *cmd, int32_t msgid,
               int32_t tmout, int32_t rspfile)
{
	void *sock = niimx_sock();
	const char *h = host ? host : "";
	int32_t mpid = (int32_t)getpid();

	if (!sock || !cmd) return -1;

	niimx_send_frame(sock, "",      0,               1);
	niimx_send_frame(sock, h,       strlen(h),       1);
	niimx_send_frame(sock, &msgid,  sizeof(msgid),   1);
	niimx_send_frame(sock, &mpid,   sizeof(mpid),    1);
	niimx_send_frame(sock, &tmout,  sizeof(tmout),   1);
	niimx_send_frame(sock, &rspfile, sizeof(rspfile), 1);
	niimx_send_frame(sock, cmd,     strlen(cmd),     0);

	return 0;
}

int niimx_recv(int32_t *msgid, char **rsp, int *nbytes, int tmout_ms)
{
	void  *sock  = niimx_sock();
	char  *buf   = NULL;
	size_t bufsz = 0;
	int32_t mcont = 0;
	int    ms = (tmout_ms > 0) ? tmout_ms : -1;

	if (!sock) return -1;

	zmq_setsockopt(sock, ZMQ_RCVTIMEO, &ms, sizeof(ms));

	do {
		zmq_msg_t frame;

		/* empty delimiter — the first recv of a segment is where a timeout shows up */
		zmq_msg_init(&frame);
		if (zmq_msg_recv(&frame, sock, 0) < 0) {
			int e = zmq_errno();
			zmq_msg_close(&frame);
			free(buf);
			return (EAGAIN == e) ? NIIMX_TIMEDOUT : -1;
		}
		zmq_msg_close(&frame);

		/* msgid */
		zmq_msg_init(&frame);
		if (zmq_msg_recv(&frame, sock, 0) < 0 || zmq_msg_size(&frame) < 4) {
			zmq_msg_close(&frame);
			free(buf);
			return -1;
		}
		memcpy(msgid, zmq_msg_data(&frame), 4);
		zmq_msg_close(&frame);

		/* mcont */
		zmq_msg_init(&frame);
		if (zmq_msg_recv(&frame, sock, 0) < 0 || zmq_msg_size(&frame) < 4) {
			zmq_msg_close(&frame);
			free(buf);
			return -1;
		}
		memcpy(&mcont, zmq_msg_data(&frame), 4);
		zmq_msg_close(&frame);

		/* payload */
		zmq_msg_init(&frame);
		if (zmq_msg_recv(&frame, sock, 0) < 0) {
			zmq_msg_close(&frame);
			free(buf);
			return -1;
		}
		{
			size_t sz = zmq_msg_size(&frame);
			char *nb = (char *)realloc(buf, bufsz + sz + 1);
			if (!nb) {
				zmq_msg_close(&frame);
				free(buf);
				return -1;
			}
			buf = nb;
			memcpy(buf + bufsz, zmq_msg_data(&frame), sz);
			bufsz += sz;
			buf[bufsz] = '\0';
		}
		zmq_msg_close(&frame);

	} while (mcont > 0);

	*rsp    = buf;
	*nbytes = (int)bufsz;

	return 0;
}
```

A note on the segment loop: once the first frame of a multi-segment response has arrived, the remaining segments are already queued locally, so the per-frame `ZMQ_RCVTIMEO` cannot strand a half-assembled body.

- [ ] **Step 5: Build and run the tests**

```bash
cd $BASE/cnc/utility/src && nmake -f util.mk ../../../3b2/lib/libinc.so
cd $BASE/cnc/niimx/src && nmake ../../../3b2/bin/niimx_t && ./test_niimx.sh
```

Expected: `Results: 6 passed, 0 failed`.

Note the stub does not yet read the `rspfile` frame this sends — it drains trailing frames after `command`, so the extra frame lands harmlessly in the drain loop but shifts the command into the `rspfile` position. **Update `niimx_stub.cpp` in this step**: add `std::string s_rspfile;` and a matching `recv_frame` call between `s_tmout` and `cmd`.

- [ ] **Step 6: Commit**

```bash
cd $BASE
git add include/niimxlib.h cnc/utility/src/niimxlib.c cnc/niimx/src/niimx_t.cpp cnc/niimx/src/niimx_stub.cpp cnc/niimx/src/test_niimx.sh
git commit -m "niimxlib: pipelined send/recv with msgid demux and mcont reassembly

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 4: `niimxlib` — single transaction, error mapping, hard fail

`iimx_sendx_call` branches on `"IMSGX^Internal Error..."` and `"FAIL:Service Unavail"` (`iimx_snd.c:578-600`). `niimxd` emits `"NIIMX^..."` (`niimxd.cpp:1000-1027`), which would otherwise reach callers as a *successful* body. Map it.

**Files:**
- Modify: `include/niimxlib.h`
- Modify: `cnc/utility/src/niimxlib.c`
- Modify: `cnc/niimx/src/niimx_t.cpp`
- Modify: `cnc/niimx/src/test_niimx.sh`

- [ ] **Step 1: Write the failing test**

Add to `cnc/niimx/src/niimx_t.cpp`:

```cpp
/** @brief One round-trip through niimx_xact, printing rc and body. */
static int cmd_xact(const char *cmd, int tmout)
{
    char *rsp = NULL;
    int nbytes = 0;
    int rc = niimx_xact("", cmd, 0, &rsp, &nbytes, tmout);

    printf("XACT rc=%d body=%s\n", rc, rsp ? rsp : "(null)");
    free(rsp);
    return 0;
}
```

and in `main`:

```cpp
    if (0 == strcmp(argv[1], "xact"))
        return cmd_xact(argc > 2 ? argv[2] : "echo:hi", argc > 3 ? atoi(argv[3]) : 10);
```

Append to `cnc/niimx/src/test_niimx.sh`:

```bash
echo "=== transaction and error mapping ==="
start_stub
assert_contains "successful xact returns rc=0" "XACT rc=0 body=hi" \
    env NIIMX_ENDPOINT_OVERRIDE="$ENDPOINT" "$BINDIR/niimx_t" xact 'echo:hi'
assert_contains "NIIMX^ body maps to rc=-1" "XACT rc=-1 body=NIIMX^System congestion" \
    env NIIMX_ENDPOINT_OVERRIDE="$ENDPOINT" "$BINDIR/niimx_t" xact 'echo:NIIMX^System congestion'
stop_stub

echo "=== hard fail when daemon absent ==="
assert_contains "no daemon returns Service Unavailable" "XACT rc=-1 body=Service Unavailable" \
    env NIIMX_ENDPOINT_OVERRIDE="ipc:///tmp/test_niimx_absent_$$.sock" "$BINDIR/niimx_t" xact 'echo:hi' 600
```

The last case also asserts speed implicitly: with `tmout` 600 it must still return at once. Make that explicit by wrapping it in `timeout 10`.

- [ ] **Step 2: Run to verify it fails**

```bash
cd $BASE/cnc/niimx/src && nmake ../../../3b2/bin/niimx_t
```

Expected: FAIL — `niimx_xact` was not declared.

- [ ] **Step 3: Declare it**

Add to `include/niimxlib.h`, inside the `extern "C"` block:

```c
/**
 * @brief Send one command and wait for its complete response.
 *
 * A body prefixed "NIIMX^" is a daemon-level rejection and is reported as a
 * failure with the body preserved, matching how the message-queue transport
 * treats "IMSGX^Internal Error." bodies. When the daemon is not reachable the
 * call fails immediately rather than waiting out @p tmout.
 *
 * @param host     Target host; "" or NULL for local.
 * @param cmd      Command text.
 * @param rspfile  Non-zero to have niimxd spill the response to a file, which
 *                 this function then reads and unlinks.
 * @param rsp      Out: malloc'd NUL-terminated body; caller frees. Always set.
 * @param nbytes   Out: body length; may be NULL.
 * @param tmout    Seconds to wait; <= 0 means 600.
 * @return 0 on success, -1 on any failure.
 */
int niimx_xact(const char *host, const char *cmd, int rspfile,
               char **rsp, int *nbytes, int tmout);
```

- [ ] **Step 4: Implement it**

Append to `cnc/utility/src/niimxlib.c`:

```c
int niimx_xact(const char *host, const char *cmd, int rspfile,
               char **rsp, int *nbytes, int tmout)
{
	int32_t msgid, rid = 0;
	char *body = NULL;
	int   len = 0, rc;

	if (!rsp) return -1;
	*rsp = NULL;
	if (nbytes) *nbytes = 0;

	if (tmout < 1) tmout = 600;

	if (!niimx_avail()) {
		*rsp = strdup("Service Unavailable");
		return -1;
	}

	msgid = niimx_next_msgid();

	if (0 != niimx_send(host, cmd, msgid, (int32_t)tmout, (int32_t)rspfile)) {
		*rsp = strdup("Service Unavailable");
		return -1;
	}

	/* Discard replies to commands that already timed out under us. */
	while (1) {
		rc = niimx_recv(&rid, &body, &len, tmout * 1000);

		if (0 != rc) {
			*rsp = strdup((NIIMX_TIMEDOUT == rc) ? "Timeout" : "Service Unavailable");
			return -1;
		}
		if (rid == msgid) break;

		free(body);
		body = NULL;
	}

	*rsp = body;
	if (nbytes) *nbytes = len;

	if (0 == strncmp(body, "NIIMX^", 6)) return -1;

	return 0;
}
```

- [ ] **Step 5: Build and run**

```bash
cd $BASE/cnc/utility/src && nmake -f util.mk ../../../3b2/lib/libinc.so
cd $BASE/cnc/niimx/src && nmake ../../../3b2/bin/niimx_t && ./test_niimx.sh
```

Expected: `Results: 9 passed, 0 failed`.

- [ ] **Step 6: Commit**

```bash
cd $BASE
git add include/niimxlib.h cnc/utility/src/niimxlib.c cnc/niimx/src/niimx_t.cpp cnc/niimx/src/test_niimx.sh
git commit -m "niimxlib: add niimx_xact with NIIMX^ error mapping and fast hard fail

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 5: `niimxlib` — the toggle itself

**Files:**
- Modify: `include/niimxlib.h`
- Modify: `cnc/utility/src/niimxlib.c`
- Modify: `cnc/niimx/src/niimx_t.cpp`
- Modify: `cnc/niimx/src/test_niimx.sh`

- [ ] **Step 1: Write the failing test**

Add to `cnc/niimx/src/niimx_t.cpp`:

```cpp
/** @brief Print the resolved state of the USE_NIIMXD toggle. */
static int cmd_enabled()
{
    printf("ENABLED=%d\n", niimx_enabled());
    return 0;
}
```

and in `main`:

```cpp
    if (0 == strcmp(argv[1], "enabled")) return cmd_enabled();
```

Append to `cnc/niimx/src/test_niimx.sh`:

```bash
echo "=== USE_NIIMXD toggle ==="
assert_contains "toggle defaults off" "ENABLED=0" \
    "$BINDIR/niimx_t" enabled
assert_contains "toggle honours env override" "ENABLED=1" \
    env USE_NIIMXD_OVERRIDE=1 "$BINDIR/niimx_t" enabled
```

The env override exists only so the harness can flip the toggle without writing to `/usr/cnc/features/system_defines` on a live box. Production uses the system define.

- [ ] **Step 2: Run to verify it fails**

```bash
cd $BASE/cnc/niimx/src && nmake ../../../3b2/bin/niimx_t
```

Expected: FAIL — `niimx_enabled` was not declared.

- [ ] **Step 3: Declare it**

Add to `include/niimxlib.h`, inside the `extern "C"` block:

```c
/**
 * @brief Report whether iimx traffic should be routed to niimxd.
 *
 * Resolved once per process from the USE_NIIMXD_OVERRIDE environment variable,
 * then the USE_NIIMXD= system define, defaulting to off. The environment
 * override exists for testing; production flips the system define.
 *
 * @return Non-zero when niimxd routing is enabled.
 */
int niimx_enabled(void);
```

- [ ] **Step 4: Implement it**

Append to `cnc/utility/src/niimxlib.c`:

```c
int niimx_enabled(void)
{
	static int enabled = -1;
	const char *env;

	if (enabled >= 0) return enabled;

	if ((env = getenv("USE_NIIMXD_OVERRIDE")) && env[0]) {
		enabled = atoi(env);
		return enabled;
	}

	get_sysdef_int("USE_NIIMXD=", &enabled, 0);

	return enabled;
}
```

- [ ] **Step 5: Build and run**

```bash
cd $BASE/cnc/utility/src && nmake -f util.mk ../../../3b2/lib/libinc.so
cd $BASE/cnc/niimx/src && nmake ../../../3b2/bin/niimx_t && ./test_niimx.sh
```

Expected: `Results: 11 passed, 0 failed`.

- [ ] **Step 6: Commit**

```bash
cd $BASE
git add include/niimxlib.h cnc/utility/src/niimxlib.c cnc/niimx/src/niimx_t.cpp cnc/niimx/src/test_niimx.sh
git commit -m "niimxlib: add USE_NIIMXD toggle reader

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 6: Build — link `libinc.so` against `libzmq`

`libinc.so` is built by the stock `mklib` rule (`global.nmk:318`) with no `-lzmq`. `niimxlib.o` is already in `OBJ_INC` (`util.mk:395`) but nothing calls it, so its undefined `zmq_*` symbols sit harmlessly in the shared object. The moment `iimx_snd.c` calls in (Task 9), every one of the ~30 executables linking `-linc` would need `-lzmq` of its own. Fix it once, at the library.

This is **Lucent/AT&T nmake**, not GNU make: `.USE` defines a reusable action template, `$(<)` is the target, `$(*)` all prerequisites, `silent` suppresses echo and `ignore` suppresses exit status. Actions are tab-indented.

**Files:**
- Modify: `cnc/utility/src/util.mk`

- [ ] **Step 1: Write the failing test**

```bash
cd $BASE/cnc/utility/src && nmake -f util.mk ../../../3b2/lib/libinc.so
ldd $BASE/3b2/lib/libinc.so | grep -q libzmq && echo ZMQ_LINKED || echo ZMQ_MISSING
```

Expected: `ZMQ_MISSING`.

- [ ] **Step 2: Add the clone**

In `cnc/utility/src/util.mk`, immediately above the `OBJ_INC` line at `:395`, add a local clone of the stock `mklib` rule carrying the extra flag:

```
/* libinc.so calls into libzmq via niimxlib.c. Carrying -lzmq here puts libzmq
   in libinc's DT_NEEDED, so the ~30 executables that link -linc need no change. */
mklib_inc : .USE
	set -x
	silent ignore chmod 777 $(<) 2>/dev/null
	$(CC) $(LIBFLAGS) -o $(<) $(*) -lzmq
	silent ignore chmod 555 $(<) 2>/dev/null
	set +x
```

Then change `:397` from:

```
$(LDIR)/libinc.$(LIBTYPE): mklib $(OBJ_INC:B:S=.o)
```

to:

```
$(LDIR)/libinc.$(LIBTYPE): mklib_inc $(OBJ_INC:B:S=.o)
```

Leave every other `mklib` user in the file alone.

- [ ] **Step 3: Rebuild and verify**

```bash
cd $BASE/cnc/utility/src && nmake -f util.mk ../../../3b2/lib/libinc.so
ldd $BASE/3b2/lib/libinc.so | grep -q libzmq && echo ZMQ_LINKED || echo ZMQ_MISSING
```

Expected: `ZMQ_LINKED`.

- [ ] **Step 4: Verify no consumer regressed**

Pick a binary that links `-linc` and rebuild it:

```bash
cd $BASE/cnc/rcmd/src && nmake ../../../3b2/bin/iisnd && echo CONSUMER_OK
```

Expected: `CONSUMER_OK`, with no undefined-symbol errors.

- [ ] **Step 5: Commit**

```bash
cd $BASE
git add cnc/utility/src/util.mk
git commit -m "build: link libinc.so against libzmq via mklib_inc clone

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 7: `niimxd` — the `rspfile` frame

Three live callers pass `rspfile=1` for large payloads: `cnc/restjson/src/rj_gui_cgi.cpp:321, 382, 578` (`RMTdump`, `RMTalm_list`). Give niimxd the same capability imsgx has.

New request layout — note `rspfile` sits between `tmout` and `command`:

```
identity | empty | host | msgid(4B) | mpid(4B) | tmout(4B) | rspfile(4B) | command
```

This is a breaking wire change. `niimxd` and `libinc.so` must deploy together.

**Files:**
- Modify: `cnc/niimx/src/niimxd.cpp:965-995` (intake), `:189-201` (`Request`), `:304` region (`bchannel`), `:1595` (dispatch), `:1999` (completion)
- Modify: `cnc/niimx/src/niimx.cpp`
- Modify: `cnc/niimx/src/test_niimx.sh`

- [ ] **Step 1: Write the failing test**

Add `-R` to `cnc/niimx/src/niimx.cpp`. In the option string and switch:

```cpp
    int32_t     rspfile   = 0;
```

```cpp
    while ((opt = getopt(argc, argv, "e:c:i:t:w:x:H:R:qTh")) != -1) {
```

```cpp
            case 'R': rspfile   = (int32_t)atoi(optarg); break;
```

and in the send block, between the `tmout` and `command` frames:

```cpp
    send_frame(sock, &rspfile, 4,                true);
```

plus the usage line:

```cpp
        "  -R 0|1        Ask niimxd to spill the response to a file\n"
```

Append to `cnc/niimx/src/test_niimx.sh`:

```bash
echo "=== rspfile frame ==="
start_stub
assert_contains "rspfile frame is carried, not mistaken for the command" "hello" \
    "$BINDIR/niimx" -e "$ENDPOINT" -c 'echo:hello' -R 1 -w 5
stop_stub
```

- [ ] **Step 2: Run to verify it fails**

```bash
cd $BASE/cnc/niimx/src && nmake ../../../3b2/bin/niimx && ./test_niimx.sh
```

Expected: FAIL before the `-R` edit (`invalid option`); after the edit it passes only because the stub already reads the frame (added in Task 3, Step 5). Confirm the stub has `s_rspfile` in its recv chain before continuing.

- [ ] **Step 3: Carry the field through niimxd**

In `struct Request` (`niimxd.cpp:194-201`) add the member and its Doxygen line:

```cpp
    int32_t rspfile = 0;   /**< Non-zero: spill the response to a file and return its path. */
```

In `struct bchannel`, beside `m_tmout` (`:304`):

```cpp
    int32_t    m_rspfile = 0;   /**< rspfile flag of the in-flight request. */
```

In `niimx_handle_zmq()` (`:965-995`), add the frame to the receive chain, between `s_tmout` and `command`:

```cpp
        string identity, empty, host, s_msgid, s_mpid, s_tmout, s_rspfile, command;
```

```cpp
        if (!niimx_zmq_recv_frame(G.router, s_tmout,   more) || !more) continue;
        if (!niimx_zmq_recv_frame(G.router, s_rspfile, more) || !more) continue;
        if (!niimx_zmq_recv_frame(G.router, command,   more))          continue;
```

and populate it beside the other fields:

```cpp
        req.rspfile  = parse_i32(s_rspfile);
```

At dispatch, beside `bc.m_tmout = req.tmout;` (`:1595`):

```cpp
    bc.m_rspfile = req.rspfile;
```

- [ ] **Step 4: Honour it at completion**

At the success completion (`:1999`), replace:

```cpp
            niimx_zmq_send_response(bc.m_zmq_identity, bc.m_msgid, bc.m_response);
```

with:

```cpp
            if (bc.m_rspfile) {
                char path[256];
                snprintf(path, sizeof(path), "/usr/cnc/tmp/iimx-rsp%d.%d",
                         (int)bc.m_mpid, (int)bc.m_msgid);

                FILE *fp = fopen(path, "w");
                if (fp) {
                    fwrite(bc.m_response.data(), 1, bc.m_response.size(), fp);
                    fclose(fp);
                    niimx_zmq_send_response(bc.m_zmq_identity, bc.m_msgid, path);
                } else {
                    Trc(0, "rspfile: fopen(" << path << ") failed errno=" << errno << endl);
                    niimx_zmq_send_response(bc.m_zmq_identity, bc.m_msgid, bc.m_response);
                }
            } else {
                niimx_zmq_send_response(bc.m_zmq_identity, bc.m_msgid, bc.m_response);
            }
```

The path prefix must stay `/usr/cnc/tmp/iimx-rsp` — `iimx_snd.c:610` recognises a file response by `strncmp(imsg.mtext, "/usr/cnc/tmp/iimx-rsp", 13)`. A `fopen` failure falls back to an inline body rather than failing the command.

Leave every other `niimx_zmq_send_response` call site inline: the reject and error paths (`:1000, 1008, 1019, 1025, 1687, 1784, 1819, 2389, 2432`) send short strings that the client matches on by prefix.

- [ ] **Step 5: Build and run**

```bash
cd $BASE/cnc/niimx/src && nmake && ./test_niimx.sh
```

Expected: `Results: 12 passed, 0 failed`, and `niimxd` compiles clean.

- [ ] **Step 6: Commit**

```bash
cd $BASE
git add cnc/niimx/src/niimxd.cpp cnc/niimx/src/niimx.cpp cnc/niimx/src/test_niimx.sh
git commit -m "niimxd: add rspfile frame for spill-to-file response parity with imsgx

Breaking wire change: request frames gain rspfile between tmout and command.
niimxd and libinc.so must deploy together.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 8: `niimxlib` — client side of `rspfile`

**Files:**
- Modify: `cnc/utility/src/niimxlib.c`
- Modify: `cnc/niimx/src/niimx_stub.cpp`
- Modify: `cnc/niimx/src/niimx_t.cpp`
- Modify: `cnc/niimx/src/test_niimx.sh`

- [ ] **Step 1: Write the failing test**

Teach the stub to honour `rspfile`. In `niimx_stub.cpp`, parse the flag:

```cpp
        int32_t rspfile = 0;
        if (s_rspfile.size() >= 4) memcpy(&rspfile, s_rspfile.data(), 4);
```

and just before the command dispatch chain, add:

```cpp
        if (rspfile) {
            char path[256];
            snprintf(path, sizeof(path), "/usr/cnc/tmp/iimx-rsp%d.%d",
                     (int)getpid(), (int)msgid);
            std::string body = (cmd.compare(0, 5, "echo:") == 0) ? cmd.substr(5) : cmd;
            FILE *fp = fopen(path, "w");
            if (fp) { fwrite(body.data(), 1, body.size(), fp); fclose(fp); }
            reply(sock, id, msgid, path);
            continue;
        }
```

Add `#include <stdio.h>` if not already present. Add to `niimx_t.cpp`:

```cpp
/** @brief Round-trip with rspfile=1, printing the body and whether the file is gone. */
static int cmd_xact_rspfile(const char *cmd)
{
    char *rsp = NULL;
    int nbytes = 0;
    int rc = niimx_xact("", cmd, 1, &rsp, &nbytes, 10);

    printf("RSPFILE rc=%d body=%s\n", rc, rsp ? rsp : "(null)");
    free(rsp);
    return 0;
}
```

and in `main`:

```cpp
    if (0 == strcmp(argv[1], "xact_rspfile"))
        return cmd_xact_rspfile(argc > 2 ? argv[2] : "echo:spilled");
```

Append to `cnc/niimx/src/test_niimx.sh`:

```bash
echo "=== rspfile round trip ==="
mkdir -p /usr/cnc/tmp 2>/dev/null || true
start_stub
assert_contains "rspfile body read back from file" "RSPFILE rc=0 body=spilled" \
    env NIIMX_ENDPOINT_OVERRIDE="$ENDPOINT" "$BINDIR/niimx_t" xact_rspfile 'echo:spilled'
assert_contains "spill file is unlinked after reading" "SPILL_GONE" \
    bash -c 'ls /usr/cnc/tmp/iimx-rsp* >/dev/null 2>&1 && echo SPILL_LEFT || echo SPILL_GONE'
stop_stub
```

- [ ] **Step 2: Run to verify it fails**

```bash
cd $BASE/cnc/niimx/src && nmake && ./test_niimx.sh
```

Expected: FAIL — `RSPFILE rc=0 body=/usr/cnc/tmp/iimx-rsp...`, the raw path rather than the contents.

- [ ] **Step 3: Read and unlink the spill file**

In `cnc/utility/src/niimxlib.c`, in `niimx_xact`, replace:

```c
	*rsp = body;
	if (nbytes) *nbytes = len;
```

with:

```c
	/* niimxd spilled the response: the body is the path, not the payload.
	   Match the prefix the message-queue transport uses (iimx_snd.c:610). */
	if (rspfile && 0 == strncmp(body, "/usr/cnc/tmp/iimx-rsp", 21)) {
		char *fbuf = NULL;

		if (-1 == readInFile(&fbuf, body)) {
			free(body);
			*rsp = strdup("System Failure");
			return -1;
		}
		unlink(body);
		free(body);
		body = fbuf;
		len  = (int)strlen(fbuf);
	}

	*rsp = body;
	if (nbytes) *nbytes = len;
```

`readInFile` is declared in `utillibinc.h`, already included.

- [ ] **Step 4: Build and run**

```bash
cd $BASE/cnc/utility/src && nmake -f util.mk ../../../3b2/lib/libinc.so
cd $BASE/cnc/niimx/src && nmake && ./test_niimx.sh
```

Expected: `Results: 14 passed, 0 failed`.

- [ ] **Step 5: Commit**

```bash
cd $BASE
git add cnc/utility/src/niimxlib.c cnc/niimx/src/niimx_stub.cpp cnc/niimx/src/niimx_t.cpp cnc/niimx/src/test_niimx.sh
git commit -m "niimxlib: read and unlink rspfile spill files in niimx_xact

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 9: Divert `iimx_sendx_call`

The single sync path, backing `iimx_sendx`, `iimx_send`, `iimx_check`, and `gbl_cgi` — the bulk of the ~271 call sites.

**Files:**
- Modify: `cnc/utility/src/iimx_snd.c:451-476`
- Modify: `cnc/niimx/src/niimx_t.cpp`
- Modify: `cnc/niimx/src/test_niimx.sh`

- [ ] **Step 1: Write the failing test**

Add to `cnc/niimx/src/niimx_t.cpp` — note this calls the **iimx** API, not the niimx one, so it proves the divert:

```cpp
extern "C" {
int iimx_sendx(const char *host, const char *cmd, char **rsp, int tmout);
}

/** @brief Call iimx_sendx and print what came back. */
static int cmd_sendx(const char *host, const char *cmd)
{
    char *rsp = NULL;
    int rc = iimx_sendx(host, cmd, &rsp, 10);

    printf("SENDX rc=%d body=%s\n", rc, rsp ? rsp : "(null)");
    free(rsp);
    return 0;
}
```

and in `main`:

```cpp
    if (0 == strcmp(argv[1], "sendx"))
        return cmd_sendx(argc > 2 ? argv[2] : "localhost", argc > 3 ? argv[3] : "echo:hi");
```

Append to `cnc/niimx/src/test_niimx.sh`:

```bash
echo "=== iimx_sendx divert ==="
start_stub
assert_contains "toggle on routes iimx_sendx to niimxd" "SENDX rc=0 body=hi" \
    env USE_NIIMXD_OVERRIDE=1 NIIMX_ENDPOINT_OVERRIDE="$ENDPOINT" \
        "$BINDIR/niimx_t" sendx localhost 'echo:hi'
stop_stub
```

- [ ] **Step 2: Run to verify it fails**

```bash
cd $BASE/cnc/niimx/src && nmake ../../../3b2/bin/niimx_t && ./test_niimx.sh
```

Expected: FAIL — with no `imsgx` running, `iimx_sendx` times out or returns a message-queue error, not `body=hi`.

- [ ] **Step 3: Add the include and the divert**

In `cnc/utility/src/iimx_snd.c`, beside the other includes near `:29`:

```c
#include <niimxlib.h>
```

Then in `iimx_sendx_call`, replace the opening of the function body (`:453-476`, from `time_t sent, nnow;` down to and including the `iimx.congested` block) so the divert comes first:

```c
int iimx_sendx_call(const char *host,const char *cmd,int rspfile,char **rsp,int tmout)
{

	time_t sent, nnow;
	char me[31];
	int tries=0;

	gethostname(me,sizeof(me)-1);

	int local = (0==strcmp(host,"localhost")||0==strcmp(host,"2iimx")||
		         0==strcasecmp(me,host));

	if (niimx_enabled())
		return niimx_xact(local ? "" : host, cmd, rspfile, rsp, 0, tmout);

	if (!local)
	{
		return iimx_sendx_remote(host,cmd,rsp,tmout);
	}

	int msgid, mtype, iq, oq, usecs, rc, bytes;

	char path[256], ebuf[256], **vars, *ikey, *okey, *tb;
	struct imsgx_m imsg;

	if (0==access("/usr/cnc/data/iimx.congested",R_OK)) {
		*rsp = strdup("iimx is busy right now. try again later");
		trace(0,"ERROR: %s\n",ebuf);
		return (-1);
	}
```

Two things to notice. The `local` test is lifted out of the original `if` so both transports share one definition of "is this host me". And the divert passes `""` for local, because niimxd treats an empty `host` frame as local and would reject the literal string `localhost` as an unknown host.

- [ ] **Step 4: Build and run**

```bash
cd $BASE/cnc/utility/src && nmake -f util.mk ../../../3b2/lib/libinc.so
cd $BASE/cnc/niimx/src && nmake ../../../3b2/bin/niimx_t && ./test_niimx.sh
```

Expected: `Results: 15 passed, 0 failed`.

- [ ] **Step 5: Verify the toggle-off path is untouched**

```bash
cd $BASE/cnc/niimx/src
env USE_NIIMXD_OVERRIDE=0 $BASE/3b2/bin/niimx_t sendx localhost 'echo:hi'
```

Expected: a message-queue error or timeout, **not** `body=hi` — proving the legacy path still runs when the toggle is off.

- [ ] **Step 6: Commit**

```bash
cd $BASE
git add cnc/utility/src/iimx_snd.c cnc/niimx/src/niimx_t.cpp cnc/niimx/src/test_niimx.sh
git commit -m "iimx_snd: route iimx_sendx_call to niimxd when USE_NIIMXD is set

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 10: Divert `iimx_sendxn`

Same single command, but the caller supplies a fixed buffer instead of receiving a `malloc`'d one.

**Files:**
- Modify: `cnc/utility/src/iimx_snd.c:650-660`
- Modify: `cnc/niimx/src/niimx_t.cpp`
- Modify: `cnc/niimx/src/test_niimx.sh`

- [ ] **Step 1: Write the failing test**

Add to `cnc/niimx/src/niimx_t.cpp`:

```cpp
extern "C" {
int iimx_sendxn(char *host, char *cmd, int n, char *rsp, int tmout);
}

/** @brief Call iimx_sendxn with a deliberately small buffer. */
static int cmd_sendxn(char *host, char *cmd, int n)
{
    char buf[4096];
    memset(buf, 0, sizeof(buf));
    if (n > (int)sizeof(buf)) n = (int)sizeof(buf);

    int rc = iimx_sendxn(host, cmd, n, buf, 10);
    printf("SENDXN rc=%d len=%d body=%s\n", rc, (int)strlen(buf), buf);
    return 0;
}
```

and in `main`:

```cpp
    if (0 == strcmp(argv[1], "sendxn"))
        return cmd_sendxn(argc > 2 ? argv[2] : (char *)"localhost",
                          argc > 3 ? argv[3] : (char *)"echo:hi",
                          argc > 4 ? atoi(argv[4]) : 256);
```

Append to `cnc/niimx/src/test_niimx.sh`:

```bash
echo "=== iimx_sendxn divert ==="
start_stub
assert_contains "sendxn fills caller buffer" "SENDXN rc=0 len=2 body=hi" \
    env USE_NIIMXD_OVERRIDE=1 NIIMX_ENDPOINT_OVERRIDE="$ENDPOINT" \
        "$BINDIR/niimx_t" sendxn localhost 'echo:hi' 256
assert_contains "sendxn truncates to n-1 without overrun" "SENDXN rc=0 len=9" \
    env USE_NIIMXD_OVERRIDE=1 NIIMX_ENDPOINT_OVERRIDE="$ENDPOINT" \
        "$BINDIR/niimx_t" sendxn localhost 'big:100000' 10
stop_stub
```

- [ ] **Step 2: Run to verify it fails**

Expected: FAIL — no divert yet, so the message-queue path runs and no stub reply arrives.

- [ ] **Step 3: Add the divert**

In `cnc/utility/src/iimx_snd.c`, at the top of `iimx_sendxn` (`:650`), after `memset(rsp,0,n);`:

```c
	if (niimx_enabled()) {
		int local = (0==strcmp(host,"localhost")||0==strcmp(host,"2iimx")||
		             0==strcasecmp(me,host));
		char *xrsp = 0;
		int rc = niimx_xact(local ? "" : host, cmd, 0, &xrsp, 0, tmout);

		if (xrsp) {
			strncpy(rsp, xrsp, n - 1);
			rsp[n - 1] = 0;
			free(xrsp);
		}
		return rc;
	}
```

Place it after the existing `gethostname(me,sizeof(me)-1);` so `me` is populated.

- [ ] **Step 4: Build and run**

```bash
cd $BASE/cnc/utility/src && nmake -f util.mk ../../../3b2/lib/libinc.so
cd $BASE/cnc/niimx/src && nmake ../../../3b2/bin/niimx_t && ./test_niimx.sh
```

Expected: `Results: 17 passed, 0 failed`.

- [ ] **Step 5: Commit**

```bash
cd $BASE
git add cnc/utility/src/iimx_snd.c cnc/niimx/src/niimx_t.cpp cnc/niimx/src/test_niimx.sh
git commit -m "iimx_snd: route iimx_sendxn to niimxd when USE_NIIMXD is set

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 11: Divert `iimx_q` and `iimx_poll`

The async pair. `iimx_q` fires a command and registers a callback in the file-static `iimxQ` table; `iimx_poll` drains replies and dispatches callbacks. Only caller is the Lua binding (`cnc/rcmd/src/ilua.c:1570` and `:1537`), same process, no `fork` between the two — so the process-local socket is safe here.

**Files:**
- Modify: `cnc/utility/src/iimx_snd.c:858-1002`
- Modify: `cnc/niimx/src/niimx_t.cpp`
- Modify: `cnc/niimx/src/test_niimx.sh`

- [ ] **Step 1: Write the failing test**

Add to `cnc/niimx/src/niimx_t.cpp`:

```cpp
extern "C" {
int iimx_q(char *cmd, void (*callback)(char *, void *), void *data, int tmout);
int iimx_poll(int tmout);
}

/** @brief Callback recording which slot answered. */
static void qcb(char *rsp, void *data)
{
    printf("QCB slot=%d body=%s\n", (int)(long)data, rsp ? rsp : "(null)");
}

/** @brief Queue n commands, then poll them all out. */
static int cmd_queue(int n)
{
    char cmd[64];

    for (int i = 0; i < n; i++) {
        snprintf(cmd, sizeof(cmd), "echo:q%d", i);
        if (0 != iimx_q(cmd, qcb, (void *)(long)i, 30)) {
            printf("QUEUE_FAIL=%d\n", i);
            return 1;
        }
    }

    int left = iimx_poll(20);
    printf("QUEUE_DONE left=%d\n", left);
    return 0;
}
```

and in `main`:

```cpp
    if (0 == strcmp(argv[1], "queue")) return cmd_queue(argc > 2 ? atoi(argv[2]) : 3);
```

Append to `cnc/niimx/src/test_niimx.sh`:

```bash
echo "=== iimx_q / iimx_poll divert ==="
start_stub
assert_contains "queued callbacks all fire" "QUEUE_DONE left=0" \
    env USE_NIIMXD_OVERRIDE=1 NIIMX_ENDPOINT_OVERRIDE="$ENDPOINT" \
        "$BINDIR/niimx_t" queue 3
assert_contains "callback receives its own reply" "QCB slot=2 body=q2" \
    env USE_NIIMXD_OVERRIDE=1 NIIMX_ENDPOINT_OVERRIDE="$ENDPOINT" \
        "$BINDIR/niimx_t" queue 3
stop_stub
```

- [ ] **Step 2: Run to verify it fails**

Expected: FAIL — `QUEUE_DONE left=3`, the legacy poll never receiving anything.

- [ ] **Step 3: Divert `iimx_q`**

In `cnc/utility/src/iimx_snd.c`, in `iimx_q` (`:932`), replace the message-queue block from `if (-1 == (iq=msgget(...)))` through the closing `}` of the `msgsnd` failure check with a branch. The final shape:

```c
	iimx_q_entry *e=iimxQ+idx;

	e->tmout=tmout;
	e->data=data;
	e->callback=callback;
	e->status=2; // 2 == running

	if (niimx_enabled()) {
		e->msgid = niimx_next_msgid();
		time(&(e->start));

		if (0 != niimx_send("", cmd, e->msgid, tmout, 0)) {
			TRACE(0,"ERROR: niimx_send() failed\n");
			memset(e,0,sizeof(*e));
			return -1;
		}
		return 0;
	}

	if (-1 == (iq=msgget(IIMX_IKEY,IPC_CREAT|0666)))
	{
		...unchanged...
```

On failure the slot is cleared so it is not left marked running forever — the legacy path has the same bug in reverse, but do not fix that here.

- [ ] **Step 4: Divert `iimx_poll`**

In `iimx_poll` (`:858`), add a niimxd branch. Insert right after `time(&start);`:

```c
	if (niimx_enabled()) {
		while (1) {
			int32_t rid = 0;
			char *rsp = NULL;
			int nbytes = 0;

			count=0;
			for(int iidx=0; iidx < IIMXQ_SZ; iidx+=1)
				if (iimxQ[iidx].status!=0) count +=1;

			if (count <1) break;

			time(&now);
			if (tmout>0 && now-start > tmout) break;

			if (0 != niimx_recv(&rid, &rsp, &nbytes, 500)) continue;

			int idx=iimx_q_find(iimxQ,IIMXQ_SZ,rid);
			if (idx == -1) {
				/* A reply to a command that already gave up. Drop it. */
				TRACE(0,"Message id does not match any in queue. Ignore\n");
				free(rsp);
				continue;
			}

			iimx_q_entry *e=iimxQ+idx;
			if (e->callback) e->callback(rsp,e->data);

			free(rsp);
			memset(e,0,sizeof(*e));
		}
		return count;
	}

	if (-1 == (oq=msgget(IIMX_IKEY,IPC_CREAT|0666)))
	{
		...unchanged...
```

Move the declarations of `now` and `start` above this block if the compiler complains — they are already declared at the top of the function (`:862`).

- [ ] **Step 5: Build and run**

```bash
cd $BASE/cnc/utility/src && nmake -f util.mk ../../../3b2/lib/libinc.so
cd $BASE/cnc/niimx/src && nmake ../../../3b2/bin/niimx_t && ./test_niimx.sh
```

Expected: `Results: 19 passed, 0 failed`.

- [ ] **Step 6: Commit**

```bash
cd $BASE
git add cnc/utility/src/iimx_snd.c cnc/niimx/src/niimx_t.cpp cnc/niimx/src/test_niimx.sh
git commit -m "iimx_snd: route iimx_q/iimx_poll to niimxd when USE_NIIMXD is set

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 12: Divert `iimx_local`

Batch2: N commands out, replies matched back into `ri[].b` by msgid.

**Files:**
- Modify: `cnc/utility/src/iimx_snd.c:2539-2560`
- Modify: `cnc/niimx/src/niimx_t.cpp`
- Modify: `cnc/niimx/src/test_niimx.sh`

- [ ] **Step 1: Write the failing test**

Add to `cnc/niimx/src/niimx_t.cpp`:

```cpp
extern "C" {
int iimx_local(int ncmd, struct iimx_batch2_ri *ri, int tmout, int gbltmout);
}

/** @brief Run n commands through iimx_local and print each result. */
static int cmd_local(int n)
{
    struct iimx_batch2_ri *ri =
        (struct iimx_batch2_ri *)calloc(n, sizeof(struct iimx_batch2_ri));

    for (int i = 0; i < n; i++)
        snprintf(ri[i].cmd, sizeof(ri[i].cmd), "echo:L%d", i);

    int rc = iimx_local(n, ri, 10, 20);

    for (int i = 0; i < n; i++)
        printf("LOCAL i=%d state=%d body=%s\n", i, (int)ri[i].state,
               ri[i].b.buf ? ri[i].b.buf : "(null)");

    printf("LOCAL_DONE rc=%d\n", rc);
    free(ri);
    return 0;
}
```

and in `main`:

```cpp
    if (0 == strcmp(argv[1], "local")) return cmd_local(argc > 2 ? atoi(argv[2]) : 3);
```

Append to `cnc/niimx/src/test_niimx.sh`:

```bash
echo "=== iimx_local divert ==="
start_stub
assert_contains "each batch2 entry gets its own reply" "LOCAL i=2 state=2 body=L2" \
    env USE_NIIMXD_OVERRIDE=1 NIIMX_ENDPOINT_OVERRIDE="$ENDPOINT" \
        "$BINDIR/niimx_t" local 3
stop_stub
```

`state=2` is `IIMX_RFINISHED` (`include/iimx_snd.h:8`).

- [ ] **Step 2: Run to verify it fails**

Expected: FAIL — `body=(null)`, nothing having arrived.

- [ ] **Step 3: Add the divert**

In `cnc/utility/src/iimx_snd.c`, at the top of `iimx_local` (`:2539`), after the `if (ncmd == 0) return 0;` guard:

```c
	if (niimx_enabled()) {
		int queued = 0;

		for (int iidx = 0; iidx < ncmd; iidx++) {
			ri[iidx].msgid = niimx_next_msgid();
			ri[iidx].sfd   = (-1);
			ri[iidx].state = IIMX_RINIT;
			iob_free(&(ri[iidx].b));

			if (0 != niimx_send("", ri[iidx].cmd, ri[iidx].msgid, tmout, 0)) {
				ri[iidx].state = IIMX_RQERROR;
				continue;
			}
			ri[iidx].state = IIMX_RACTIVE;
			queued += 1;
		}

		time_t nstart;
		time(&nstart);

		while (queued > 0) {
			int32_t rid = 0;
			char *nrsp = NULL;
			int nbytes = 0;
			time_t nnow2;

			time(&nnow2);
			if (nnow2 > nstart + gbltmout) {
				TRACE(0, "ERROR: timed out waiting for niimxd responses.\n");
				return -1;
			}

			if (0 != niimx_recv(&rid, &nrsp, &nbytes, 500)) continue;

			for (int iidx = 0; iidx < ncmd; iidx++) {
				if (ri[iidx].state == IIMX_RACTIVE && ri[iidx].msgid == rid) {
					iob_append(&(ri[iidx].b), nrsp, nbytes + 1);
					ri[iidx].state = IIMX_RFINISHED;
					queued -= 1;
					break;
				}
			}
			free(nrsp);
		}

		return 0;
	}
```

The `nbytes + 1` matches the legacy path at `:2650`, which appends the terminating NUL so `ri[].b.buf` is a usable C string.

- [ ] **Step 4: Build and run**

```bash
cd $BASE/cnc/utility/src && nmake -f util.mk ../../../3b2/lib/libinc.so
cd $BASE/cnc/niimx/src && nmake ../../../3b2/bin/niimx_t && ./test_niimx.sh
```

Expected: `Results: 20 passed, 0 failed`.

- [ ] **Step 5: Commit**

```bash
cd $BASE
git add cnc/utility/src/iimx_snd.c cnc/niimx/src/niimx_t.cpp cnc/niimx/src/test_niimx.sh
git commit -m "iimx_snd: route iimx_local to niimxd when USE_NIIMXD is set

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 13: Divert `iimx_sendx_pool`

**Files:**
- Modify: `cnc/utility/src/iimx_snd.c:2941-2960`
- Modify: `cnc/niimx/src/niimx_t.cpp`
- Modify: `cnc/niimx/src/test_niimx.sh`

- [ ] **Step 1: Write the failing test**

Add to `cnc/niimx/src/niimx_t.cpp`:

```cpp
extern "C" {
int iimx_sendx_pool(char *host, int ncmd, struct iimx_ri *ri, int tmout);
}

/** @brief Run n commands through iimx_sendx_pool and print each result. */
static int cmd_pool(int n)
{
    struct iimx_ri *ri = (struct iimx_ri *)calloc(n, sizeof(struct iimx_ri));
    char buf[64];

    for (int i = 0; i < n; i++) {
        snprintf(buf, sizeof(buf), "echo:P%d", i);
        ri[i].cmd = strdup(buf);
    }

    int rc = iimx_sendx_pool((char *)"localhost", n, ri, 10);

    for (int i = 0; i < n; i++) {
        printf("POOL i=%d state=%d body=%s\n", i, (int)ri[i].state,
               ri[i].b.buf ? ri[i].b.buf : "(null)");
        free(ri[i].cmd);
    }

    printf("POOL_DONE rc=%d\n", rc);
    free(ri);
    return 0;
}
```

and in `main`:

```cpp
    if (0 == strcmp(argv[1], "pool")) return cmd_pool(argc > 2 ? atoi(argv[2]) : 3);
```

Append to `cnc/niimx/src/test_niimx.sh`:

```bash
echo "=== iimx_sendx_pool divert ==="
start_stub
assert_contains "each pool entry gets its own reply" "POOL i=2 state=2 body=P2" \
    env USE_NIIMXD_OVERRIDE=1 NIIMX_ENDPOINT_OVERRIDE="$ENDPOINT" \
        "$BINDIR/niimx_t" pool 3
stop_stub
```

- [ ] **Step 2: Run to verify it fails**

Expected: FAIL — `body=(null)`.

- [ ] **Step 3: Add the divert**

In `cnc/utility/src/iimx_snd.c`, at the top of `iimx_sendx_pool` (`:2941`), immediately after the `if (ncmd <= 0)` guard:

```c
	if (niimx_enabled()) {
		int queued = 0;
		time_t nstart, nnow2;

		if (tmout < 1) tmout = 600;

		for (int iidx = 0; iidx < ncmd; iidx++) {
			ri[iidx].state = IIMX_RINIT;
			ri[iidx].sfd   = (-1);
			iob_init(&(ri[iidx].b), 0);
			ri[iidx].msgid = niimx_next_msgid();

			if (0 != niimx_send("", ri[iidx].cmd, ri[iidx].msgid, tmout, 0)) {
				ri[iidx].state = IIMX_RQERROR;
				continue;
			}
			ri[iidx].state = IIMX_RACTIVE;
			queued += 1;
		}

		time(&nstart);

		while (queued > 0) {
			int32_t rid = 0;
			char *nrsp = NULL;
			int nbytes = 0;

			time(&nnow2);
			if (nnow2 > nstart + tmout) {
				trace(0,"ERROR: timed out waiting for niimxd responses. host[%s]\n",host);
				return -1;
			}

			if (0 != niimx_recv(&rid, &nrsp, &nbytes, 500)) continue;

			for (int iidx = 0; iidx < ncmd; iidx++) {
				if (ri[iidx].state != IIMX_RACTIVE || ri[iidx].msgid != rid) continue;

				if (0 == strncmp(nrsp, "NIIMX^", 6)) {
					iob_printf(&(ri[iidx].b), "Service Unavailable", host);
					ri[iidx].state = IIMX_RERROR;
				} else {
					iob_printf(&(ri[iidx].b), nrsp, host);
					ri[iidx].state = IIMX_RFINISHED;
				}
				queued -= 1;
				break;
			}
			free(nrsp);
		}

		return 0;
	}
```

The `NIIMX^` branch mirrors the legacy `FAIL:Service Unavail` handling at `:3145`.

- [ ] **Step 4: Build and run**

```bash
cd $BASE/cnc/utility/src && nmake -f util.mk ../../../3b2/lib/libinc.so
cd $BASE/cnc/niimx/src && nmake ../../../3b2/bin/niimx_t && ./test_niimx.sh
```

Expected: `Results: 21 passed, 0 failed`.

- [ ] **Step 5: Commit**

```bash
cd $BASE
git add cnc/utility/src/iimx_snd.c cnc/niimx/src/niimx_t.cpp cnc/niimx/src/test_niimx.sh
git commit -m "iimx_snd: route iimx_sendx_pool to niimxd when USE_NIIMXD is set

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 14: Divert `iimx_sendx_pool_v2`

Same shape as Task 13, but `struct iimx_pool_ri` has a fixed `char iob_buf[4096]` and a `char cmd[128]` instead of an `iob` and a pointer (`include/iimx_snd.h:53-64`).

**Files:**
- Modify: `cnc/utility/src/iimx_snd.c:2700-2720`
- Modify: `cnc/niimx/src/niimx_t.cpp`
- Modify: `cnc/niimx/src/test_niimx.sh`

- [ ] **Step 1: Write the failing test**

Add to `cnc/niimx/src/niimx_t.cpp`:

```cpp
extern "C" {
int iimx_sendx_pool_v2(char *host, int ncmd, struct iimx_pool_ri *ri, int tmout);
}

/** @brief Run n commands through iimx_sendx_pool_v2 and print each result. */
static int cmd_pool_v2(int n)
{
    struct iimx_pool_ri *ri =
        (struct iimx_pool_ri *)calloc(n, sizeof(struct iimx_pool_ri));

    for (int i = 0; i < n; i++)
        snprintf(ri[i].cmd, sizeof(ri[i].cmd), "echo:V%d", i);

    int rc = iimx_sendx_pool_v2((char *)"localhost", n, ri, 10);

    for (int i = 0; i < n; i++)
        printf("POOLV2 i=%d state=%d body=%s\n", i, (int)ri[i].state, ri[i].iob_buf);

    printf("POOLV2_DONE rc=%d\n", rc);
    free(ri);
    return 0;
}
```

and in `main`:

```cpp
    if (0 == strcmp(argv[1], "pool_v2")) return cmd_pool_v2(argc > 2 ? atoi(argv[2]) : 3);
```

Append to `cnc/niimx/src/test_niimx.sh`:

```bash
echo "=== iimx_sendx_pool_v2 divert ==="
start_stub
assert_contains "each pool_v2 entry gets its own reply" "POOLV2 i=2 state=2 body=V2" \
    env USE_NIIMXD_OVERRIDE=1 NIIMX_ENDPOINT_OVERRIDE="$ENDPOINT" \
        "$BINDIR/niimx_t" pool_v2 3
stop_stub
```

- [ ] **Step 2: Run to verify it fails**

Expected: FAIL — empty body.

- [ ] **Step 3: Add the divert**

`struct iimx_pool_ri` carries no `msgid` member, so hold the IDs in a local array. At the top of `iimx_sendx_pool_v2` (`:2700`), after the `ncmd <= 0` guard:

```c
	if (niimx_enabled()) {
		int32_t *nids = (int32_t *)malloc(sizeof(int32_t) * ncmd);
		int queued = 0, nrc = 0;
		time_t nstart, nnow2;

		if (!nids) {
			trace(0, "ERROR: Could not allocate memory for niimx id list\n");
			return -1;
		}

		if (tmout < 1) tmout = 600;

		for (int iidx = 0; iidx < ncmd; iidx++) {
			ri[iidx].state = IIMX_RINIT;
			ri[iidx].sfd   = (-1);
			ri[iidx].iob_buf[0] = 0;
			nids[iidx] = niimx_next_msgid();

			if (0 != niimx_send("", ri[iidx].cmd, nids[iidx], tmout, 0)) {
				ri[iidx].state = IIMX_RQERROR;
				nids[iidx] = -1;
				continue;
			}
			ri[iidx].state = IIMX_RACTIVE;
			queued += 1;
		}

		time(&nstart);

		while (queued > 0) {
			int32_t rid = 0;
			char *nrsp = NULL;
			int nbytes = 0;

			time(&nnow2);
			if (nnow2 > nstart + tmout) {
				trace(0,"ERROR: timed out waiting for niimxd responses. host[%s]\n",host);
				nrc = -1;
				break;
			}

			if (0 != niimx_recv(&rid, &nrsp, &nbytes, 500)) continue;

			for (int iidx = 0; iidx < ncmd; iidx++) {
				if (ri[iidx].state != IIMX_RACTIVE || nids[iidx] != rid) continue;

				if (0 == strncmp(nrsp, "NIIMX^", 6)) {
					strncpy(ri[iidx].iob_buf, "Service Unavailable",
					        sizeof(ri[iidx].iob_buf) - 1);
					ri[iidx].state = IIMX_RERROR;
				} else {
					strncpy(ri[iidx].iob_buf, nrsp, sizeof(ri[iidx].iob_buf) - 1);
					ri[iidx].iob_buf[sizeof(ri[iidx].iob_buf) - 1] = 0;
					ri[iidx].state = IIMX_RFINISHED;
				}
				queued -= 1;
				break;
			}
			free(nrsp);
		}

		free(nids);
		return nrc;
	}
```

`iob_buf` is a fixed 4096 bytes, so responses larger than that truncate. That matches the legacy behaviour of this function — the v2 pool was always capped.

- [ ] **Step 4: Build and run**

```bash
cd $BASE/cnc/utility/src && nmake -f util.mk ../../../3b2/lib/libinc.so
cd $BASE/cnc/niimx/src && nmake ../../../3b2/bin/niimx_t && ./test_niimx.sh
```

Expected: `Results: 22 passed, 0 failed`.

- [ ] **Step 5: Commit**

```bash
cd $BASE
git add cnc/utility/src/iimx_snd.c cnc/niimx/src/niimx_t.cpp cnc/niimx/src/test_niimx.sh
git commit -m "iimx_snd: route iimx_sendx_pool_v2 to niimxd when USE_NIIMXD is set

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 15: Divert `iimx_sendx_remote`

Today this forks `mcmd ssh` per command (`:74-76`). niimxd holds persistent libssh bchannels per remote host, so the fork disappears.

**Files:**
- Modify: `cnc/utility/src/iimx_snd.c:69-86`
- Modify: `cnc/niimx/src/niimx_stub.cpp`
- Modify: `cnc/niimx/src/test_niimx.sh`

- [ ] **Step 1: Write the failing test**

Teach the stub to echo the host back so the test can prove the `host` frame was populated. In `niimx_stub.cpp`, in the default echo branch at the end, change:

```cpp
        reply(sock, id, msgid, cmd);
```

to:

```cpp
        reply(sock, id, msgid, host.empty() ? cmd : ("host=" + host + " " + cmd));
```

and likewise in the `echo:` branch:

```cpp
        if (cmd.compare(0, 5, "echo:") == 0) {
            std::string body = cmd.substr(5);
            reply(sock, id, msgid, host.empty() ? body : ("host=" + host + " " + body));
            continue;
        }
```

Append to `cnc/niimx/src/test_niimx.sh`:

```bash
echo "=== remote host frame ==="
start_stub
assert_contains "remote host reaches the host frame" "SENDX rc=0 body=host=faraway hi" \
    env USE_NIIMXD_OVERRIDE=1 NIIMX_ENDPOINT_OVERRIDE="$ENDPOINT" \
        "$BINDIR/niimx_t" sendx faraway 'echo:hi'
assert_contains "local host sends an empty host frame" "SENDX rc=0 body=hi" \
    env USE_NIIMXD_OVERRIDE=1 NIIMX_ENDPOINT_OVERRIDE="$ENDPOINT" \
        "$BINDIR/niimx_t" sendx localhost 'echo:hi'
stop_stub
```

The first case already passes through the Task 9 divert in `iimx_sendx_call`, which routes non-local hosts straight to `niimx_xact`. This task makes `iimx_sendx_remote` correct for its own direct callers.

- [ ] **Step 2: Run to verify the direct path still forks**

```bash
cd $BASE/cnc/niimx/src && nmake && ./test_niimx.sh
grep -n "vPopen" $BASE/cnc/utility/src/iimx_snd.c | head -3
```

Expected: the two assertions pass via Task 9's divert, and `vPopen` is still the only transport inside `iimx_sendx_remote`.

- [ ] **Step 3: Add the divert**

In `cnc/utility/src/iimx_snd.c`, at the top of `iimx_sendx_remote` (`:69`):

```c
int iimx_sendx_remote(char *host,char *cmd,char **rsp,int tmout)
{
	char sshcmd[BUFSIZ+1];
	int bytes;

	if (niimx_enabled())
		return niimx_xact(host, cmd, 0, rsp, 0, tmout);

	sprintf(sshcmd,
		"/usr/cnc/bin/mcmd ssh -q -o StrictHostKeyChecking=no -o ConnectTimeout=%d -o BatchMode=yes %s '%s'",
		tmout,host,cmd);
```

- [ ] **Step 4: Build and run**

```bash
cd $BASE/cnc/utility/src && nmake -f util.mk ../../../3b2/lib/libinc.so
cd $BASE/cnc/niimx/src && nmake && ./test_niimx.sh
```

Expected: `Results: 24 passed, 0 failed`.

- [ ] **Step 5: Commit**

```bash
cd $BASE
git add cnc/utility/src/iimx_snd.c cnc/niimx/src/niimx_stub.cpp cnc/niimx/src/test_niimx.sh
git commit -m "iimx_snd: route iimx_sendx_remote to niimxd, replacing per-command ssh fork

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 16: Divert `iimx_sendx_batch_wk`

Covers `iimx_sendx_batch` (`:2680`) and, through it, `iimx_multi_fp` (`:1459`). Legacy transport is `iimx_connect` to port 80 per entry (`:1984-1996`), with an optional per-response callback.

**Files:**
- Modify: `cnc/utility/src/iimx_snd.c:1850-1870`
- Modify: `cnc/niimx/src/niimx_t.cpp`
- Modify: `cnc/niimx/src/test_niimx.sh`

- [ ] **Step 1: Write the failing test**

Add to `cnc/niimx/src/niimx_t.cpp`:

```cpp
extern "C" {
int iimx_sendx_batch(char *host, int ncmd, struct iimx_ri *ri, int tmout);
}

/** @brief Run n commands through iimx_sendx_batch, each against its own host. */
static int cmd_batch(int n)
{
    struct iimx_ri *ri = (struct iimx_ri *)calloc(n, sizeof(struct iimx_ri));
    char buf[64];

    for (int i = 0; i < n; i++) {
        snprintf(buf, sizeof(buf), "echo:B%d", i);
        ri[i].cmd = strdup(buf);
        snprintf(ri[i].host, sizeof(ri[i].host), "h%d", i);
    }

    int rc = iimx_sendx_batch((char *)"", n, ri, 10);

    for (int i = 0; i < n; i++) {
        printf("BATCH i=%d state=%d body=%s\n", i, (int)ri[i].state,
               ri[i].b.buf ? ri[i].b.buf : "(null)");
        free(ri[i].cmd);
    }

    printf("BATCH_DONE rc=%d\n", rc);
    free(ri);
    return 0;
}
```

and in `main`:

```cpp
    if (0 == strcmp(argv[1], "batch")) return cmd_batch(argc > 2 ? atoi(argv[2]) : 3);
```

Append to `cnc/niimx/src/test_niimx.sh`:

```bash
echo "=== iimx_sendx_batch divert ==="
start_stub
assert_contains "batch entry keeps its own host" "BATCH i=2 state=2 body=host=h2 B2" \
    env USE_NIIMXD_OVERRIDE=1 NIIMX_ENDPOINT_OVERRIDE="$ENDPOINT" \
        "$BINDIR/niimx_t" batch 3
stop_stub
```

- [ ] **Step 2: Run to verify it fails**

Expected: FAIL — the legacy path tries to open port 80 on hosts `h0`..`h2` and every entry errors.

- [ ] **Step 3: Add the divert**

In `cnc/utility/src/iimx_snd.c`, at the top of `iimx_sendx_batch_wk` (`:1850`):

```c
	if (niimx_enabled()) {
		int queued = 0, nrc = 0;
		time_t nstart, nnow2;

		if (tmout < 1) tmout = 600;

		for (int iidx = 0; iidx < ncmd; iidx++) {
			const char *rhost = ri[iidx].host[0] ? ri[iidx].host
			                  : (host && host[0] ? host : "");

			ri[iidx].state = IIMX_RINIT;
			ri[iidx].sfd   = (-1);
			iob_init(&(ri[iidx].b), 0);
			ri[iidx].msgid = niimx_next_msgid();

			if (0 != niimx_send(rhost, ri[iidx].cmd, ri[iidx].msgid, tmout, 0)) {
				ri[iidx].state = IIMX_RQERROR;
				continue;
			}
			ri[iidx].state = IIMX_RACTIVE;
			queued += 1;
		}

		time(&nstart);

		while (queued > 0) {
			int32_t rid = 0;
			char *nrsp = NULL;
			int nbytes = 0;

			time(&nnow2);
			if (nnow2 > nstart + tmout) {
				TRACE(0,"ERROR: timed out waiting for niimxd responses.\n");
				nrc = -1;
				break;
			}

			if (0 != niimx_recv(&rid, &nrsp, &nbytes, 500)) continue;

			for (int iidx = 0; iidx < ncmd; iidx++) {
				if (ri[iidx].state != IIMX_RACTIVE || ri[iidx].msgid != rid) continue;

				iob_append(&(ri[iidx].b), nrsp, nbytes + 1);
				ri[iidx].state = (0 == strncmp(nrsp, "NIIMX^", 6))
				               ? IIMX_RERROR : IIMX_RFINISHED;
				if (cb) cb(nrsp);
				queued -= 1;
				break;
			}
			free(nrsp);
		}

		return nrc;
	}
```

The `rhost` precedence — per-entry host first, then the function-wide `host`, then local — matches the legacy selection at `:1984-1996`. The callback fires per response, as it does today.

- [ ] **Step 4: Build and run**

```bash
cd $BASE/cnc/utility/src && nmake -f util.mk ../../../3b2/lib/libinc.so
cd $BASE/cnc/niimx/src && nmake ../../../3b2/bin/niimx_t && ./test_niimx.sh
```

Expected: `Results: 25 passed, 0 failed`.

- [ ] **Step 5: Commit**

```bash
cd $BASE
git add cnc/utility/src/iimx_snd.c cnc/niimx/src/niimx_t.cpp cnc/niimx/src/test_niimx.sh
git commit -m "iimx_snd: route iimx_sendx_batch_wk to niimxd when USE_NIIMXD is set

Also covers iimx_sendx_batch and iimx_multi_fp, which wrap it.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 17: Divert `iimx_sendx_multi`

Direct sockets, called from `cnc/rcmd/src/iisnd.c:237`.

**Files:**
- Modify: `cnc/utility/src/iimx_snd.c:1520-1540`
- Modify: `cnc/niimx/src/niimx_t.cpp`
- Modify: `cnc/niimx/src/test_niimx.sh`

- [ ] **Step 1: Write the failing test**

Add to `cnc/niimx/src/niimx_t.cpp`:

```cpp
extern "C" {
int iimx_sendx_multi(char *host, int ncmd, struct iimx_ri *ri, int tmout);
}

/** @brief Run n commands through iimx_sendx_multi and print each result. */
static int cmd_multi(int n)
{
    struct iimx_ri *ri = (struct iimx_ri *)calloc(n, sizeof(struct iimx_ri));
    char buf[64];

    for (int i = 0; i < n; i++) {
        snprintf(buf, sizeof(buf), "echo:M%d", i);
        ri[i].cmd = strdup(buf);
    }

    int rc = iimx_sendx_multi((char *)"localhost", n, ri, 10);

    for (int i = 0; i < n; i++) {
        printf("MULTI i=%d state=%d body=%s\n", i, (int)ri[i].state,
               ri[i].b.buf ? ri[i].b.buf : "(null)");
        free(ri[i].cmd);
    }

    printf("MULTI_DONE rc=%d\n", rc);
    free(ri);
    return 0;
}
```

and in `main`:

```cpp
    if (0 == strcmp(argv[1], "multi")) return cmd_multi(argc > 2 ? atoi(argv[2]) : 3);
```

Append to `cnc/niimx/src/test_niimx.sh`:

```bash
echo "=== iimx_sendx_multi divert ==="
start_stub
assert_contains "each multi entry gets its own reply" "MULTI i=2 state=2 body=M2" \
    env USE_NIIMXD_OVERRIDE=1 NIIMX_ENDPOINT_OVERRIDE="$ENDPOINT" \
        "$BINDIR/niimx_t" multi 3
stop_stub
```

- [ ] **Step 2: Run to verify it fails**

Expected: FAIL — `body=(null)`.

- [ ] **Step 3: Add the divert**

`iimx_sendx_multi` has the same signature and the same `struct iimx_ri` array as `iimx_sendx_batch_wk`, so delegate rather than duplicate the loop. At the top of `iimx_sendx_multi` (`:1520`):

```c
	if (niimx_enabled())
		return iimx_sendx_batch_wk(host, ncmd, ri, NULL, tmout);
```

`iimx_sendx_batch_wk` is declared later in the file, so add a forward declaration beside the existing one at `:43`:

```c
int iimx_sendx_batch_wk(char *host,int ncmd,struct iimx_ri *ri,int (cb)(char *),int tmout);
```

- [ ] **Step 4: Build and run**

```bash
cd $BASE/cnc/utility/src && nmake -f util.mk ../../../3b2/lib/libinc.so
cd $BASE/cnc/niimx/src && nmake ../../../3b2/bin/niimx_t && ./test_niimx.sh
```

Expected: `Results: 26 passed, 0 failed`.

- [ ] **Step 5: Commit**

```bash
cd $BASE
git add cnc/utility/src/iimx_snd.c cnc/niimx/src/niimx_t.cpp cnc/niimx/src/test_niimx.sh
git commit -m "iimx_snd: route iimx_sendx_multi to niimxd when USE_NIIMXD is set

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 18: Congestion flag file

`iimx_snd.c` pre-checks `/usr/cnc/data/iimx.congested` in three places (`:474`, `:2550`, `:2982`). `niimxd` writes `/usr/cnc/data/niimx.congested` (`niimxd.cpp:1003`, `:1013`). Under the toggle, check the right one.

**Files:**
- Modify: `cnc/utility/src/iimx_snd.c`
- Modify: `cnc/niimx/src/test_niimx.sh`

- [ ] **Step 1: Write the failing test**

Append to `cnc/niimx/src/test_niimx.sh`:

```bash
echo "=== congestion flag file ==="
start_stub
touch /usr/cnc/data/niimx.congested
assert_contains "niimx congestion flag is honoured" "SENDX rc=-1" \
    env USE_NIIMXD_OVERRIDE=1 NIIMX_ENDPOINT_OVERRIDE="$ENDPOINT" \
        "$BINDIR/niimx_t" sendx localhost 'echo:hi'
rm -f /usr/cnc/data/niimx.congested
assert_contains "traffic resumes once the flag is cleared" "SENDX rc=0 body=hi" \
    env USE_NIIMXD_OVERRIDE=1 NIIMX_ENDPOINT_OVERRIDE="$ENDPOINT" \
        "$BINDIR/niimx_t" sendx localhost 'echo:hi'
stop_stub
```

- [ ] **Step 2: Run to verify it fails**

Expected: FAIL on the first assertion — `SENDX rc=0`, the flag being ignored.

- [ ] **Step 3: Check the flag in `niimx_xact`**

Put the check in the library rather than in three places in `iimx_snd.c`. In `cnc/utility/src/niimxlib.c`, in `niimx_xact`, immediately after the `if (tmout < 1) tmout = 600;` line:

```c
	if (0 == access(NIIMX_CONGESTED_FILE, R_OK)) {
		*rsp = strdup("iimx is busy right now. try again later");
		return -1;
	}
```

The message text matches the legacy one at `iimx_snd.c:476` so callers that surface it to a user see no change.

- [ ] **Step 4: Check it in the pipelined paths too**

The batch entry points do not go through `niimx_xact`. Add the same guard at the top of each divert block added in Tasks 12, 13, 14, and 16, immediately before the send loop:

```c
		if (0 == access(NIIMX_CONGESTED_FILE, R_OK)) {
			TRACE(0, "ERROR: niimx.congested\n");
			return -1;
		}
```

- [ ] **Step 5: Build and run**

```bash
cd $BASE/cnc/utility/src && nmake -f util.mk ../../../3b2/lib/libinc.so
cd $BASE/cnc/niimx/src && nmake && ./test_niimx.sh
```

Expected: `Results: 28 passed, 0 failed`.

- [ ] **Step 6: Commit**

```bash
cd $BASE
git add cnc/utility/src/niimxlib.c cnc/utility/src/iimx_snd.c cnc/niimx/src/test_niimx.sh
git commit -m "niimxlib: honour niimx.congested rather than iimx.congested under the toggle

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 19: Documentation and full-suite verification

**Files:**
- Modify: `include/feat_path.h` (comment only)
- Modify: `cnc/niimx/src/test_niimx.sh`

- [ ] **Step 1: Document the system defines**

`include/feat_path.h:356` defines `FEAT_SYSTEM` as the system-defines path. Other toggles are documented as comments beside the code that reads them (see `include/alm_db.h:321`). Follow that pattern — add above the `niimx_enabled` implementation in `cnc/utility/src/niimxlib.c`:

```c
/*
 * system_defines tunables consumed here:
 *
 *   USE_NIIMXD=1        Route all iimx traffic through niimxd instead of the
 *                       SysV message queue and per-command ssh. Off by default.
 *                       When on there is no fallback: if niimxd is unreachable
 *                       commands fail with "Service Unavailable".
 *   NIIMX.ENDPOINT=...  Override the niimxd ZMQ endpoint. Defaults to
 *                       ipc:///usr/cnc/data/niimx.ipc
 *
 * niimxd and libinc.so carry a matching wire format and must deploy together.
 */
```

- [ ] **Step 2: Add the toggle-off regression sweep**

Append to `cnc/niimx/src/test_niimx.sh`:

```bash
echo "=== toggle off leaves the legacy transport in place ==="
start_stub
for sub in sendx sendxn local pool pool_v2 batch multi; do
    out=$(env USE_NIIMXD_OVERRIDE=0 NIIMX_ENDPOINT_OVERRIDE="$ENDPOINT" \
              timeout 30 "$BINDIR/niimx_t" "$sub" 2>&1)
    if echo "$out" | grep -q 'body=hi\|body=L2\|body=P2\|body=V2\|body=B2\|body=M2'; then
        FAIL=$((FAIL + 1)); echo "  FAIL: $sub reached the stub with the toggle off"
    else
        PASS=$((PASS + 1)); echo "  PASS: $sub stayed on the legacy transport"
    fi
done
stop_stub
```

- [ ] **Step 3: Run the whole suite**

```bash
cd $BASE/cnc/niimx/src && ./test_niimx.sh
```

Expected: `Results: 35 passed, 0 failed`.

- [ ] **Step 4: Rebuild everything that links `-linc`**

```bash
cd $BASE/cnc/utility/src && nmake -f util.mk
cd $BASE/cnc/niimx/src   && nmake
cd $BASE/cnc/rcmd/src    && nmake
cd $BASE/cnc/restjson/src && nmake
```

Expected: all four complete with no undefined-symbol errors. `cnc/restjson/src` matters specifically because it holds the three `rspfile=1` callers.

- [ ] **Step 5: Commit**

```bash
cd $BASE
git add cnc/utility/src/niimxlib.c cnc/niimx/src/test_niimx.sh
git commit -m "niimxlib: document USE_NIIMXD and NIIMX.ENDPOINT system defines

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

## Verification before calling this done

Run all of these and paste the output. Do not claim completion on any of them without it.

```bash
cd $BASE/cnc/niimx/src && ./test_niimx.sh
ldd $BASE/3b2/lib/libinc.so | grep libzmq
grep -c 'niimx_enabled()' $BASE/cnc/utility/src/iimx_snd.c   # expect 10
```

The ten diverted functions, one `niimx_enabled()` call each: `iimx_sendx_remote`,
`iimx_sendx_call`, `iimx_sendxn`, `iimx_poll`, `iimx_q`, `iimx_sendx_multi`,
`iimx_sendx_batch_wk`, `iimx_local`, `iimx_sendx_pool`, `iimx_sendx_pool_v2`.

## Deployment note

`niimxd` and `libinc.so` share a wire format that this plan changes. Deploying one without the other mismatches the frame count: the daemon blocks waiting for a frame that never comes, and the client waits out its full timeout. Ship both, or neither.
