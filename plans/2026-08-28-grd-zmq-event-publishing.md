# grd ZMQ Event Publishing Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `grd` publish its state as `app.gr.*` events onto niimxd's local ZMQ pub/sub bus, and give niimxd a loopback TCP XPUB endpoint so external subscribers can attach — without changing any existing GR behavior.

**Architecture:** A new self-contained `gr_zmq_pub` module owns a `ZMQ_PUB` socket connected to niimxd's XSUB IPC endpoint. All pure logic (config resolution, YAML escaping, transition detection, heartbeat gating) lives in that module as functions over plain structs, so it is unit-testable without `gr_daemon2.o`, without niimxd, and without the CNC databases. `gr_daemon2.cpp` gains one choke-point function plus a small number of in-place call sites. Publishing is opt-in and defaults off.

**Tech Stack:** C++11 (gcc 4.8.5 baseline), libzmq, Lucent/AT&T nmake, project `trace.h` / `utillibinc.h`.

**Spec:** `~/WorkNotes/specs/2026-08-28-grd-zmq-event-publishing-design.md`

---

## Environment Prerequisite — READ FIRST

`BASE` and `VPATH` must match this worktree before **any** nmake command in this plan.
Builds will silently pull headers and sources from the wrong tree otherwise.

```bash
export BASE=/home/dan/Git/hack-netflex
export VPATH=$BASE:<other-paths-from-your-normal-setup>
```

Verify before starting:

```bash
git rev-parse --show-toplevel   # must equal $BASE
echo "$BASE"
echo "$VPATH" | cut -d: -f1     # must equal $BASE
```

If these do not match, **stop** and fix the environment. Do not reconstruct or hack
around missing `globaldefs.nmk` / `global_*.nmk` paths.

All nmake commands below are run from `/home/dan/Git/hack-netflex/cnc/gr/src` unless
stated otherwise.

---

## Deviations From the Spec (decided while planning — carried into this plan)

These four points refine the approved spec. They are additive and do not change its
scope or intent.

1. **`GRD_ZMQ_ENDPOINT` environment override.** The spec had the XSUB endpoint
   hardcoded, following `otn_port_zmq_pub.c`. That makes the module untestable: a test
   cannot write to `/usr/cnc/data/nfdb_sub.sock` and cannot start a real niimxd (see
   point 3). An environment-variable override, checked before the compiled-in default,
   costs three lines and makes every test in this plan runnable. Env-only — no sysdef —
   so no string-sysdef API is needed.

2. **`GRD_ZMQ_PUB` and `GRD_ZMQ_HEARTBEAT` also honor the environment**, checked before
   the sysdef. This lets tests run without the `FEAT_SYSTEM` database attached. When the
   env var is present the sysdef lookup is skipped entirely, so no test touches the DB.

3. **Tests bind their own SUB socket rather than standing up a broker.** niimxd cannot
   be used as a test fixture: its `main()` starts with `(void)argc; (void)argv;`, it
   hardcodes its endpoints, and it requires `ddb_init()`, `atch_aal()`, ctree, and
   `/usr/cnc/.CNC_UP`. (Relatedly, `cnc/niimx/src/test_nfdb.sh` invokes `$BINDIR/nfd`,
   which the niimx Makefile no longer builds — that script is stale and does not run.
   Noted in the final task; not fixed here.) ZMQ permits either peer to bind, so a
   test-only binary that **binds** `ZMQ_SUB` at a temp `ipc://` path and dumps frames is
   a complete harness for a `ZMQ_PUB` that connects to it.

4. **Transition detection is a pure function in the module**, not a static in
   `gr_daemon2.cpp`. The spec placed `grd_publish_state()` in `gr_daemon2.cpp` reading
   file-static globals, which cannot be unit-tested. Splitting it — `grpub_state_delta()`
   as a pure function over a plain struct in the module, and a thin
   `grd_publish_state()` in `gr_daemon2.cpp` that populates the struct from globals and
   formats the events — keeps the logic testable while leaving the wiring where it
   belongs.

---

## File Structure

**Created:**

| Path | Responsibility |
|---|---|
| `cnc/gr/src/gr_zmq_pub.h` | Public API of the publisher module. |
| `cnc/gr/src/gr_zmq_pub.cpp` | Socket lifecycle, config resolution, YAML escaping, two-frame send, transition detection, heartbeat gating. No knowledge of GR globals. |
| `cnc/gr/src/grsub.cpp` | Test-only tool: binds `ZMQ_SUB` at a given endpoint, prints `topic|body` lines. |
| `cnc/gr/src/test_gr_zmq_pub.cpp` | Unit tests for the module's pure functions. Standalone `main()` with PASS/FAIL counters. |
| `cnc/gr/src/test_gr_zmq_pub.sh` | Integration test: runs `grsub`, drives the publisher, asserts wire format. |

**Modified:**

| Path | Change |
|---|---|
| `cnc/gr/src/grdsrc.mk` | Add `gr_zmq_pub.o` and `-lzmq` to the `grd` target; add `grsub` and `test_gr_zmq_pub` targets. |
| `cnc/gr/src/gr_daemon2.cpp` | `grpub_init`/`grpub_close` lifecycle; `grd_publish_state()` choke point; call sites for `data`, `bep`, `transfer`; SIGCHLD exit deferral. |
| `cnc/niimx/src/niimxd.cpp` | Second (TCP) bind on the existing XPUB socket; `pub_tcp_endpoint` config field. |

`cnc/gr` has no `include/` directory (unlike `cnc/otn_port`), so the header lives beside
the sources.

---

## Task 1: Baseline build

Establishes that `grd` builds cleanly **before** any change, so a later failure is
unambiguously caused by this work.

**Files:** none modified.

- [ ] **Step 1: Verify the build environment**

```bash
cd /home/dan/Git/hack-netflex/cnc/gr/src
git rev-parse --show-toplevel
echo "BASE=$BASE"
echo "VPATH[0]=$(echo "$VPATH" | cut -d: -f1)"
```

Expected: all three print `/home/dan/Git/hack-netflex`. If not, stop and fix the
environment.

- [ ] **Step 2: Build grd unchanged**

```bash
nmake -f grdsrc.mk ../../../3b2/bin/grd
```

Expected: builds without error. Record the last few lines of output; you will compare
against this later.

- [ ] **Step 3: Confirm libzmq is linkable in this environment**

```bash
nmake -f grdsrc.mk ../../../3b2/bin/grsvc
```

Expected: builds without error. `grsvc` already links `-lzmq -lzreq` (grdsrc.mk:33), so
success here proves libzmq and its headers are available before you depend on them.

- [ ] **Step 4: Commit nothing**

No changes yet. Do not commit.

---

## Task 2: `grsub` test subscriber tool

The harness every later test depends on. Binds a SUB socket so a connecting PUB has a
peer, and prints each two-frame message as one line.

**Files:**
- Create: `cnc/gr/src/grsub.cpp`
- Modify: `cnc/gr/src/grdsrc.mk`

- [ ] **Step 1: Write `grsub.cpp`**

```cpp
/**
 * @file grsub.cpp
 * @brief Test-only ZMQ subscriber that BINDS its endpoint.
 *
 * The niimxd broker normally binds the XPUB/XSUB pair, but niimxd cannot be
 * used as a test fixture: it takes no arguments, hardcodes its endpoints, and
 * requires the CNC databases and /usr/cnc/.CNC_UP. ZMQ allows either peer to
 * bind, so this tool binds a SUB socket and a ZMQ_PUB under test connects to
 * it -- giving a complete harness with no broker involved.
 *
 * Prints one line per received message: "<topic>|<body-with-newlines-escaped>".
 * Line-buffered so a shell test can poll the output file.
 *
 * Usage: grsub <endpoint> [topic-prefix]
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <errno.h>
#include <signal.h>
#include <zmq.h>

/** @brief Set by SIGINT/SIGTERM to break the receive loop. */
static volatile sig_atomic_t s_stop = 0;

/**
 * @brief Signal handler: request loop exit.
 *
 * @param sig  Unused.
 */
static void grsub_on_signal(int sig)
{
    (void)sig;
    s_stop = 1;
}

/**
 * @brief Print a frame with newlines and pipes escaped so one message is one line.
 *
 * @param data  Frame bytes (not null-terminated).
 * @param len   Frame length in bytes.
 */
static void grsub_print_escaped(const char *data, size_t len)
{
    for (size_t i = 0; i < len; i++) {
        switch (data[i]) {
        case '\n': fputs("\\n", stdout); break;
        case '\r': fputs("\\r", stdout); break;
        case '|':  fputs("\\|", stdout); break;
        default:   fputc(data[i], stdout); break;
        }
    }
}

int main(int argc, char **argv)
{
    if (argc < 2) {
        fprintf(stderr, "usage: %s <endpoint> [topic-prefix]\n", argv[0]);
        return 2;
    }
    const char *endpoint = argv[1];
    const char *prefix   = (argc > 2) ? argv[2] : "";

    signal(SIGINT,  grsub_on_signal);
    signal(SIGTERM, grsub_on_signal);

    void *ctx = zmq_ctx_new();
    if (!ctx) {
        fprintf(stderr, "zmq_ctx_new failed: %s\n", zmq_strerror(errno));
        return 1;
    }

    void *sock = zmq_socket(ctx, ZMQ_SUB);
    if (!sock) {
        fprintf(stderr, "zmq_socket(SUB) failed: %s\n", zmq_strerror(errno));
        zmq_ctx_destroy(ctx);
        return 1;
    }

    if (zmq_setsockopt(sock, ZMQ_SUBSCRIBE, prefix, strlen(prefix)) != 0) {
        fprintf(stderr, "ZMQ_SUBSCRIBE failed: %s\n", zmq_strerror(errno));
        zmq_close(sock);
        zmq_ctx_destroy(ctx);
        return 1;
    }

    if (zmq_bind(sock, endpoint) != 0) {
        fprintf(stderr, "zmq_bind(%s) failed: %s\n", endpoint, zmq_strerror(errno));
        zmq_close(sock);
        zmq_ctx_destroy(ctx);
        return 1;
    }

    /* Unbuffered-ish: the shell test polls this output while we run. */
    setvbuf(stdout, NULL, _IOLBF, 0);
    fprintf(stderr, "grsub: bound %s prefix=\"%s\"\n", endpoint, prefix);

    while (!s_stop) {
        zmq_msg_t msg;
        zmq_msg_init(&msg);

        /* Frame 0: topic. */
        if (zmq_msg_recv(&msg, sock, 0) < 0) {
            zmq_msg_close(&msg);
            if (errno == EINTR) continue;
            break;
        }
        grsub_print_escaped((const char *)zmq_msg_data(&msg), zmq_msg_size(&msg));
        int more = zmq_msg_more(&msg);
        zmq_msg_close(&msg);

        fputc('|', stdout);

        /* Frame 1: body (if the sender framed one). */
        if (more) {
            zmq_msg_init(&msg);
            if (zmq_msg_recv(&msg, sock, 0) >= 0) {
                grsub_print_escaped((const char *)zmq_msg_data(&msg),
                                    zmq_msg_size(&msg));
            }
            zmq_msg_close(&msg);
        }

        fputc('\n', stdout);
        fflush(stdout);
    }

    int linger = 0;
    zmq_setsockopt(sock, ZMQ_LINGER, &linger, sizeof(linger));
    zmq_close(sock);
    zmq_ctx_destroy(ctx);
    return 0;
}
```

- [ ] **Step 2: Add the `grsub` target to `grdsrc.mk`**

Insert after the `$(PBIN)/lmap` rule (grdsrc.mk:39-40). `grsub` links only libzmq — it
deliberately does not pull in `$(CORELIBS)`, so it builds and runs without the CNC
databases.

```
$(PBIN)/grsub : grsub.o
	$(CPLUS_CC)  $(LDFLAGS) -o $(<) $(*) -lzmq
```

Note: `grsub` is intentionally **not** added to the `.ALL` target list — it is a test
tool, not shipped.

- [ ] **Step 3: Build it**

```bash
nmake -f grdsrc.mk ../../../3b2/bin/grsub
```

Expected: builds without error.

- [ ] **Step 4: Verify it works against a hand-rolled publisher**

```bash
../../../3b2/bin/grsub "ipc:///tmp/grsub_smoke_$$.sock" "" > /tmp/grsub_smoke_out 2>/dev/null &
SUBPID=$!
sleep 1
../../../3b2/bin/nfdb -e "ipc:///tmp/grsub_smoke_$$.sock" -P app.gr.smoke "k: v" || \
  echo "nfdb -P unavailable; skip this cross-check"
sleep 1
kill $SUBPID 2>/dev/null
cat /tmp/grsub_smoke_out
```

Expected: `grsub` starts and prints its `bound` line to stderr. The `nfdb -P` cross-check
is best-effort — `nfdb` publishes through a REQ socket to a broker, so it may not reach a
bare bound SUB. The authoritative verification of `grsub` is Task 5, where the real
publisher connects to it. If `grsub` bound successfully, that is enough to proceed.

- [ ] **Step 5: Commit**

```bash
cd /home/dan/Git/hack-netflex
git add cnc/gr/src/grsub.cpp cnc/gr/src/grdsrc.mk
git commit -m "test: add grsub, a binding ZMQ SUB dumper for gr publisher tests

niimxd cannot serve as a test fixture (no argv, hardcoded endpoints, requires
ddb_init/atch_aal/ctree/.CNC_UP), so tests bind their own SUB socket and let
the publisher under test connect to it.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

## Task 3: Module skeleton — config resolution and socket lifecycle

**Files:**
- Create: `cnc/gr/src/gr_zmq_pub.h`
- Create: `cnc/gr/src/gr_zmq_pub.cpp`
- Create: `cnc/gr/src/test_gr_zmq_pub.cpp`
- Modify: `cnc/gr/src/grdsrc.mk`

- [ ] **Step 1: Write the failing test**

Create `cnc/gr/src/test_gr_zmq_pub.cpp`:

```cpp
/**
 * @file test_gr_zmq_pub.cpp
 * @brief Unit tests for the pure functions in gr_zmq_pub.cpp.
 *
 * Standalone main() with PASS/FAIL counters, matching the style of
 * cnc/niimx/src/test_nfdb.sh. Exits 0 when every test passes.
 *
 * These tests never touch the FEAT_SYSTEM database: every config value is
 * supplied through the environment, and grpub_cfg_int() skips the sysdef
 * lookup when the environment variable is present.
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include "gr_zmq_pub.h"

/** @brief Count of passed assertions. */
static int s_pass = 0;
/** @brief Count of failed assertions. */
static int s_fail = 0;

/**
 * @brief Assert two ints are equal.
 *
 * @param desc  Test description.
 * @param got   Observed value.
 * @param want  Expected value.
 */
static void eq_int(const char *desc, int got, int want)
{
    if (got == want) {
        s_pass++;
        printf("  PASS: %s\n", desc);
    } else {
        s_fail++;
        printf("  FAIL: %s (got %d, want %d)\n", desc, got, want);
    }
}

/**
 * @brief Assert two strings are equal.
 *
 * @param desc  Test description.
 * @param got   Observed value (may be NULL).
 * @param want  Expected value.
 */
static void eq_str(const char *desc, const char *got, const char *want)
{
    if (got && strcmp(got, want) == 0) {
        s_pass++;
        printf("  PASS: %s\n", desc);
    } else {
        s_fail++;
        printf("  FAIL: %s (got \"%s\", want \"%s\")\n",
               desc, got ? got : "(null)", want);
    }
}

/**
 * @brief grpub_cfg_int(): environment wins and the sysdef is never consulted.
 */
static void test_cfg_int(void)
{
    printf("grpub_cfg_int:\n");

    setenv("GRPUB_TEST_VAL", "7", 1);
    eq_int("env value used", grpub_cfg_int("GRPUB_TEST_VAL", "NOSUCH_SYSDEF=", 3), 7);

    setenv("GRPUB_TEST_VAL", "0", 1);
    eq_int("env zero honored, not treated as unset",
           grpub_cfg_int("GRPUB_TEST_VAL", "NOSUCH_SYSDEF=", 3), 0);

    setenv("GRPUB_TEST_VAL", "notanumber", 1);
    eq_int("non-numeric env falls back to default",
           grpub_cfg_int("GRPUB_TEST_VAL", "NOSUCH_SYSDEF=", 3), 3);

    unsetenv("GRPUB_TEST_VAL");
}

/**
 * @brief grpub_endpoint(): environment override, else the compiled-in default.
 */
static void test_endpoint(void)
{
    printf("grpub_endpoint:\n");

    unsetenv("GRD_ZMQ_ENDPOINT");
    eq_str("default endpoint", grpub_endpoint(), GRPUB_DEF_ENDPOINT);

    setenv("GRD_ZMQ_ENDPOINT", "ipc:///tmp/x.sock", 1);
    eq_str("env override", grpub_endpoint(), "ipc:///tmp/x.sock");

    setenv("GRD_ZMQ_ENDPOINT", "", 1);
    eq_str("empty env override ignored", grpub_endpoint(), GRPUB_DEF_ENDPOINT);

    unsetenv("GRD_ZMQ_ENDPOINT");
}

/**
 * @brief Disabled module: init succeeds, publishing is a silent no-op.
 */
static void test_disabled_is_noop(void)
{
    printf("disabled path:\n");

    setenv("GRD_ZMQ_PUB", "0", 1);
    eq_int("init returns 0 when disabled", grpub_init(), 0);
    eq_int("enabled reports 0", grpub_enabled(), 0);
    eq_int("event is a no-op returning 0",
           grpub_event("app.gr.test", "k: %d", 1), 0);
    grpub_close();
    grpub_close();   /* idempotent */
    s_pass++;
    printf("  PASS: double close is safe\n");

    unsetenv("GRD_ZMQ_PUB");
}

int main(void)
{
    test_cfg_int();
    test_endpoint();
    test_disabled_is_noop();

    printf("\nResults: %d passed, %d failed\n", s_pass, s_fail);
    return s_fail == 0 ? 0 : 1;
}
```

- [ ] **Step 2: Add the test target to `grdsrc.mk`**

Insert after the `$(PBIN)/grsub` rule from Task 2:

```
$(PBIN)/test_gr_zmq_pub : test_gr_zmq_pub.o gr_zmq_pub.o $(CORELIBS) $(UTILLIB) -lnelib -latm -linc -lgr -lrest++ -lgr++ -lcncdb
	$(CPLUS_CC)  $(LDFLAGS) -o $(<) $(*) $(ACCLIBS) -lpthread -lzmq
```

The test links the same library set as `grd` because `gr_zmq_pub.o` calls `TRACE()` and
`get_sysdef_int()`, which live in `$(UTILLIB)`.

- [ ] **Step 3: Run the test to verify it fails**

```bash
nmake -f grdsrc.mk ../../../3b2/bin/test_gr_zmq_pub
```

Expected: **build FAILS** — `gr_zmq_pub.h: No such file or directory`. That is the
failing state for this task; there is no compiled artifact to run yet.

- [ ] **Step 4: Write `gr_zmq_pub.h`**

```c
/**
 * @file gr_zmq_pub.h
 * @brief Opt-in ZMQ publisher for grd state events.
 *
 * When enabled via the GRD_ZMQ_PUB system_define (or the same-named
 * environment variable), grd publishes its state transitions to niimxd's
 * XSUB endpoint (ipc:///usr/cnc/data/nfdb_sub.sock). Subscribers on the XPUB
 * side (nfdb_pub.sock) receive a two-frame message:
 *   frame 0: app.gr.<subtopic>
 *   frame 1: <YAML body>
 *
 * GR failover must never depend on niimxd. Every function here is
 * best-effort: a disabled or broken publisher is a silent no-op, and no
 * failure propagates to a caller.
 *
 * The pure functions (grpub_cfg_int, grpub_endpoint, grpub_yaml_escape,
 * grpub_state_delta, grpub_heartbeat_due) hold no socket state and are
 * unit-tested in test_gr_zmq_pub.cpp.
 */
#ifndef GR_ZMQ_PUB_H
#define GR_ZMQ_PUB_H

#include <time.h>

#ifdef __cplusplus
extern "C" {
#endif

/**
 * @brief niimxd XSUB endpoint. Publishers connect here; niimxd forwards to
 * the XPUB side (nfdb_pub.sock) that subscribers use.
 */
#define GRPUB_DEF_ENDPOINT "ipc:///usr/cnc/data/nfdb_sub.sock"

/** @brief system_defines toggle (and env var name). Nonzero = enabled. */
#define GRPUB_SYSDEF_ENABLE "GRD_ZMQ_PUB="

/** @brief Environment variable name for the enable toggle. */
#define GRPUB_ENV_ENABLE "GRD_ZMQ_PUB"

/** @brief system_defines heartbeat floor in seconds (and env var name). */
#define GRPUB_SYSDEF_HEARTBEAT "GRD_ZMQ_HEARTBEAT="

/** @brief Environment variable name for the heartbeat floor. */
#define GRPUB_ENV_HEARTBEAT "GRD_ZMQ_HEARTBEAT"

/** @brief Environment variable overriding the XSUB endpoint (test/debug hook). */
#define GRPUB_ENV_ENDPOINT "GRD_ZMQ_ENDPOINT"

/** @brief Default heartbeat floor in seconds. */
#define GRPUB_DEF_HEARTBEAT 30

/** @brief Outbound queue depth; messages beyond this are dropped, never blocked. */
#define GRPUB_SNDHWM 1000

/**
 * @brief Resolve an int config value: environment first, then sysdef, then default.
 *
 * When the environment variable is set and parses as an integer, its value is
 * used and the sysdef database is NOT consulted. This keeps tests independent
 * of FEAT_SYSTEM. A set-but-unparseable environment value yields @p dflt.
 *
 * @param env     Environment variable name (no trailing '='). May be NULL.
 * @param sysdef  system_defines key including its trailing '='. May be NULL.
 * @param dflt    Value used when neither source supplies one.
 * @return The resolved value.
 */
int grpub_cfg_int(const char *env, const char *sysdef, int dflt);

/**
 * @brief Resolve the XSUB endpoint: GRD_ZMQ_ENDPOINT, else GRPUB_DEF_ENDPOINT.
 *
 * An unset or empty environment variable yields the default.
 *
 * @return Endpoint string. Never NULL. Points to either the environment block
 *         or a string literal; the caller must not free it.
 */
const char *grpub_endpoint(void);

/**
 * @brief Initialize the publisher.
 *
 * Reads the enable toggle. If 0 or absent, the module stays disabled and
 * grpub_event() becomes a cheap no-op. If enabled, creates a ZMQ context,
 * opens a ZMQ_PUB socket, connects to the endpoint from grpub_endpoint(),
 * and sleeps 1s to cover the PUB slow-joiner problem.
 *
 * MUST be called AFTER any fork that daemonizes the process. A ZMQ context
 * created before such a fork is unusable in the child.
 *
 * @return 0 on success or when disabled; -1 on hard ZMQ failure (module left
 *         disabled, which is not a fatal condition for the caller).
 */
int grpub_init(void);

/**
 * @brief Report whether publishing is active.
 *
 * @return Nonzero when the socket is open, 0 when disabled or torn down.
 */
int grpub_enabled(void);

/**
 * @brief Tear down the publisher. Idempotent.
 *
 * Call before any execl() that replaces the process image, and via atexit().
 */
void grpub_close(void);

/**
 * @brief Publish one event as a two-frame ZMQ message.
 *
 * No-op returning 0 when the module is disabled. Best-effort: failures are
 * logged at TRACE(D3) and do NOT propagate. Callers must not check the return
 * value to decide control flow.
 *
 * @param topic     Full topic, e.g. "app.gr.mate.state". Must not be NULL.
 * @param yaml_fmt  printf-style format for the YAML body. Must not be NULL.
 *                  Interpolated strings must be passed through
 *                  grpub_yaml_escape() first.
 * @return 0 on send (or when disabled); -1 on transient send failure.
 */
int grpub_event(const char *topic, const char *yaml_fmt, ...)
    __attribute__((format(printf, 2, 3)));

#ifdef __cplusplus
}
#endif

#endif /* GR_ZMQ_PUB_H */
```

- [ ] **Step 5: Write `gr_zmq_pub.cpp` — config and lifecycle only**

`grpub_event()` gets a minimal body here (enough for the disabled-path test to pass);
Task 5 fills in the send. Escaping and state helpers come in Tasks 4 and 6.

```cpp
/**
 * @file gr_zmq_pub.cpp
 * @brief Opt-in ZMQ publisher for grd state events.
 *
 * See gr_zmq_pub.h for the wire format and the enable toggle. Modeled on
 * cnc/otn_port/src/otn_port_zmq_pub.c, minus its pthread mutex: grd publishes
 * only from its main loop, single-threaded, whereas otn_portd publishes from a
 * pg_listen thread.
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <errno.h>
#include <stdarg.h>
#include <zmq.h>
#include <trace.h>
#include <utillibinc.h>
#include "gr_zmq_pub.h"

/** @brief Module-local ZMQ context. NULL when disabled. */
static void *s_ctx  = NULL;
/** @brief Module-local ZMQ_PUB socket. NULL when disabled. */
static void *s_sock = NULL;

int grpub_cfg_int(const char *env, const char *sysdef, int dflt)
{
    if (env) {
        const char *v = getenv(env);
        if (v && *v) {
            char *end = NULL;
            long n = strtol(v, &end, 10);
            /* Require the whole value to be numeric; otherwise fall through
             * to the default rather than silently accepting a partial parse. */
            if (end && *end == '\0')
                return (int)n;
            return dflt;
        }
    }

    if (sysdef) {
        int n = dflt;
        get_sysdef_int((char *)sysdef, &n, dflt);
        return n;
    }

    return dflt;
}

const char *grpub_endpoint(void)
{
    const char *v = getenv(GRPUB_ENV_ENDPOINT);
    if (v && *v)
        return v;
    return GRPUB_DEF_ENDPOINT;
}

int grpub_enabled(void)
{
    return s_sock != NULL;
}

int grpub_init(void)
{
    int enabled = grpub_cfg_int(GRPUB_ENV_ENABLE, GRPUB_SYSDEF_ENABLE, 0);
    if (!enabled) {
        TRACE(D1, "grpub: disabled (%s not set)\n", GRPUB_SYSDEF_ENABLE);
        return 0;
    }

    const char *endpoint = grpub_endpoint();

    s_ctx = zmq_ctx_new();
    if (!s_ctx) {
        TRACE(D0, "grpub: zmq_ctx_new failed: %s\n", zmq_strerror(errno));
        return -1;
    }

    s_sock = zmq_socket(s_ctx, ZMQ_PUB);
    if (!s_sock) {
        TRACE(D0, "grpub: zmq_socket(PUB) failed: %s\n", zmq_strerror(errno));
        zmq_ctx_destroy(s_ctx);
        s_ctx = NULL;
        return -1;
    }

    /* SNDHWM before connect: bound the outbound queue so an absent niimxd
     * cannot grow it without limit. PUB drops past the HWM rather than
     * blocking, which is the behavior GR needs. */
    int hwm = GRPUB_SNDHWM;
    (void)zmq_setsockopt(s_sock, ZMQ_SNDHWM, &hwm, sizeof(hwm));

    /* LINGER=0: on shutdown, drop pending outbound messages instead of
     * blocking zmq_ctx_destroy() waiting to hand them off. */
    int linger = 0;
    (void)zmq_setsockopt(s_sock, ZMQ_LINGER, &linger, sizeof(linger));

    if (zmq_connect(s_sock, endpoint) != 0) {
        TRACE(D0, "grpub: zmq_connect(%s) failed: %s\n",
              endpoint, zmq_strerror(errno));
        zmq_close(s_sock);
        zmq_ctx_destroy(s_ctx);
        s_sock = NULL;
        s_ctx  = NULL;
        return -1;
    }

    /* PUB slow-joiner: give subscribers time to complete subscription.
     * Same 1s pattern used in cnc/otn_port/src/otn_port_zmq_pub.c. */
    sleep(1);

    TRACE(D1, "grpub: enabled, connected to %s\n", endpoint);
    return 0;
}

void grpub_close(void)
{
    void *sock = s_sock;
    void *ctx  = s_ctx;
    s_sock = NULL;
    s_ctx  = NULL;
    if (sock) zmq_close(sock);
    if (ctx)  zmq_ctx_destroy(ctx);
}

int grpub_event(const char *topic, const char *yaml_fmt, ...)
{
    if (!s_sock)
        return 0;
    (void)topic;
    (void)yaml_fmt;
    return 0;
}
```

- [ ] **Step 6: Run the test to verify it passes**

```bash
nmake -f grdsrc.mk ../../../3b2/bin/test_gr_zmq_pub
../../../3b2/bin/test_gr_zmq_pub
```

Expected: builds, then prints `Results: 10 passed, 0 failed` and exits 0.

- [ ] **Step 7: Commit**

```bash
cd /home/dan/Git/hack-netflex
git add cnc/gr/src/gr_zmq_pub.h cnc/gr/src/gr_zmq_pub.cpp \
        cnc/gr/src/test_gr_zmq_pub.cpp cnc/gr/src/grdsrc.mk
git commit -m "feat(gr): add gr_zmq_pub module skeleton, default disabled

Config resolution (env before sysdef, so tests need no FEAT_SYSTEM DB),
socket lifecycle with SNDHWM and LINGER=0, and a disabled-path no-op.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

## Task 4: YAML escaping

Event bodies embed hostnames, alarm text, and `LastGrCmdOutput`. Unescaped, any of those
can produce a body that is not valid YAML — or that forges additional keys.

**Files:**
- Modify: `cnc/gr/src/gr_zmq_pub.h`
- Modify: `cnc/gr/src/gr_zmq_pub.cpp`
- Modify: `cnc/gr/src/test_gr_zmq_pub.cpp`

- [ ] **Step 1: Write the failing test**

Add to `test_gr_zmq_pub.cpp`, above `main()`:

```cpp
/**
 * @brief grpub_yaml_escape(): produces a valid double-quoted YAML scalar.
 */
static void test_yaml_escape(void)
{
    printf("grpub_yaml_escape:\n");
    char buf[128];

    grpub_yaml_escape(buf, sizeof(buf), "plain");
    eq_str("plain string is quoted", buf, "\"plain\"");

    grpub_yaml_escape(buf, sizeof(buf), "say \"hi\"");
    eq_str("double quotes escaped", buf, "\"say \\\"hi\\\"\"");

    grpub_yaml_escape(buf, sizeof(buf), "back\\slash");
    eq_str("backslash escaped", buf, "\"back\\\\slash\"");

    grpub_yaml_escape(buf, sizeof(buf), "two\nlines");
    eq_str("newline escaped", buf, "\"two\\nlines\"");

    grpub_yaml_escape(buf, sizeof(buf), "-leading");
    eq_str("leading dash safe inside quotes", buf, "\"-leading\"");

    grpub_yaml_escape(buf, sizeof(buf), "tab\there");
    eq_str("tab escaped", buf, "\"tab\\there\"");

    grpub_yaml_escape(buf, sizeof(buf), NULL);
    eq_str("NULL becomes empty quoted scalar", buf, "\"\"");

    grpub_yaml_escape(buf, sizeof(buf), "");
    eq_str("empty becomes empty quoted scalar", buf, "\"\"");

    /* Truncation must still yield a closed quote, never a dangling escape. */
    char small[8];
    grpub_yaml_escape(small, sizeof(small), "abcdefghijklmnop");
    eq_int("truncated output stays within bounds",
           (int)(strlen(small) < sizeof(small)), 1);
    eq_int("truncated output ends with a closing quote",
           (int)(small[strlen(small) - 1] == '"'), 1);

    /* An injection attempt must not escape the scalar. */
    grpub_yaml_escape(buf, sizeof(buf), "x\nrole: ACTIVE");
    eq_str("newline injection neutralized", buf, "\"x\\nrole: ACTIVE\"");
}
```

Register it in `main()`, before the results line:

```cpp
    test_yaml_escape();
```

- [ ] **Step 2: Declare it in `gr_zmq_pub.h`**

Add before the `grpub_event()` declaration:

```c
/**
 * @brief Render a string as a double-quoted YAML scalar, escaped.
 *
 * Escapes backslash, double quote, newline, carriage return, and tab. Wrapping
 * the result in double quotes makes leading '-', ':', '#', and other YAML
 * sigils inert, so callers need no per-character knowledge.
 *
 * Always writes a well-formed, null-terminated, closed-quote scalar, even when
 * the input must be truncated to fit -- truncation never leaves a dangling
 * escape or an unterminated quote.
 *
 * @param dst  Destination buffer. Must not be NULL; must be at least 3 bytes.
 * @param dsz  Size of @p dst in bytes.
 * @param src  Source string. NULL is treated as empty.
 * @return @p dst, for use directly as a printf "%s" argument.
 */
const char *grpub_yaml_escape(char *dst, size_t dsz, const char *src);
```

Add `#include <stddef.h>` beside the existing `#include <time.h>` for `size_t`.

- [ ] **Step 3: Run the test to verify it fails**

```bash
nmake -f grdsrc.mk ../../../3b2/bin/test_gr_zmq_pub
```

Expected: **link FAILS** with an undefined reference to `grpub_yaml_escape`.

- [ ] **Step 4: Implement it in `gr_zmq_pub.cpp`**

Add after `grpub_endpoint()`:

```cpp
const char *grpub_yaml_escape(char *dst, size_t dsz, const char *src)
{
    if (!dst || dsz < 3)
        return "\"\"";

    size_t w = 0;
    dst[w++] = '"';

    /* Reserve two bytes: the closing quote and the null terminator. */
    const size_t limit = dsz - 2;

    for (const char *p = src; p && *p && w < limit; p++) {
        const char *esc = NULL;
        switch (*p) {
        case '\\': esc = "\\\\"; break;
        case '"':  esc = "\\\""; break;
        case '\n': esc = "\\n";  break;
        case '\r': esc = "\\r";  break;
        case '\t': esc = "\\t";  break;
        default: break;
        }

        if (esc) {
            /* Only emit a two-byte escape if both bytes fit. Emitting one
             * byte of an escape sequence would produce invalid YAML. */
            if (w + 2 > limit)
                break;
            dst[w++] = esc[0];
            dst[w++] = esc[1];
        } else {
            dst[w++] = *p;
        }
    }

    dst[w++] = '"';
    dst[w]   = '\0';
    return dst;
}
```

- [ ] **Step 5: Run the test to verify it passes**

```bash
nmake -f grdsrc.mk ../../../3b2/bin/test_gr_zmq_pub
../../../3b2/bin/test_gr_zmq_pub
```

Expected: `Results: 21 passed, 0 failed`, exit 0.

- [ ] **Step 6: Commit**

```bash
cd /home/dan/Git/hack-netflex
git add cnc/gr/src/gr_zmq_pub.h cnc/gr/src/gr_zmq_pub.cpp cnc/gr/src/test_gr_zmq_pub.cpp
git commit -m "feat(gr): add grpub_yaml_escape for event body interpolation

Double-quoted scalars with backslash/quote/newline/CR/tab escaped. Truncation
always closes the quote so a clipped body is still parseable, and a newline in
alarm text or LastGrCmdOutput cannot forge additional YAML keys.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

## Task 5: Two-frame send

**Files:**
- Modify: `cnc/gr/src/gr_zmq_pub.cpp`
- Create: `cnc/gr/src/test_gr_zmq_pub.sh`

- [ ] **Step 1: Write the failing integration test**

Create `cnc/gr/src/test_gr_zmq_pub.sh`:

```bash
#!/bin/bash
# Integration test for the grd ZMQ publisher.
#
# Binds a SUB socket via grsub, points the publisher at it with
# GRD_ZMQ_ENDPOINT, drives grpub_event() through test_gr_zmq_pub's --emit
# mode, and asserts the two-frame wire format.
#
# No niimxd and no FEAT_SYSTEM database are involved: niimxd cannot be used as
# a test fixture (no argv, hardcoded endpoints, requires ddb_init/atch_aal/
# ctree/.CNC_UP), and every config value comes from the environment.
#
# Usage: ./test_gr_zmq_pub.sh [path_to_bindir]

set -u

BINDIR="${1:-../../../3b2/bin}"
ENDPOINT="ipc:///tmp/test_grpub_$$.sock"
OUTFILE="/tmp/test_grpub_out_$$"
PASS=0
FAIL=0
SUB_PID=0

cleanup() {
    if [ "$SUB_PID" -gt 0 ] 2>/dev/null; then
        kill "$SUB_PID" 2>/dev/null || true
        wait "$SUB_PID" 2>/dev/null || true
    fi
    rm -f "/tmp/test_grpub_$$.sock" "$OUTFILE"
    echo ""
    echo "Results: $PASS passed, $FAIL failed"
    [ "$FAIL" -eq 0 ] && exit 0 || exit 1
}
trap cleanup EXIT

assert_line() {
    local desc="$1"
    local needle="$2"
    if grep -qF -- "$needle" "$OUTFILE" 2>/dev/null; then
        PASS=$((PASS + 1))
        echo "  PASS: $desc"
    else
        FAIL=$((FAIL + 1))
        echo "  FAIL: $desc (expected a line containing: $needle)"
        echo "    captured output:"
        sed 's/^/      /' "$OUTFILE" 2>/dev/null || echo "      (empty)"
    fi
}

assert_absent() {
    local desc="$1"
    local needle="$2"
    if grep -qF -- "$needle" "$OUTFILE" 2>/dev/null; then
        FAIL=$((FAIL + 1))
        echo "  FAIL: $desc (unexpectedly found: $needle)"
    else
        PASS=$((PASS + 1))
        echo "  PASS: $desc"
    fi
}

# ---- Start the binding subscriber ----
echo "Starting grsub on $ENDPOINT ..."
"$BINDIR/grsub" "$ENDPOINT" "app.gr." > "$OUTFILE" 2>/dev/null &
SUB_PID=$!
sleep 1

if ! kill -0 "$SUB_PID" 2>/dev/null; then
    echo "ERROR: grsub failed to start"
    exit 1
fi

# ---- Drive the publisher ----
echo "Publishing events:"
GRD_ZMQ_PUB=1 GRD_ZMQ_ENDPOINT="$ENDPOINT" \
    "$BINDIR/test_gr_zmq_pub" --emit
sleep 1

assert_line "topic and body arrive as two frames" "app.gr.test|k: 1"
assert_line "escaped body is delivered intact"    'text: "a\\nb"'
assert_absent "unsubscribed topic filtered out"   "app.other.test"

echo ""
echo "Done."
```

Make it executable:

```bash
chmod +x cnc/gr/src/test_gr_zmq_pub.sh
```

- [ ] **Step 2: Add `--emit` mode to `test_gr_zmq_pub.cpp`**

Replace the existing `main()` with:

```cpp
/**
 * @brief Publish a fixed set of events, for the shell integration test.
 *
 * Driven by test_gr_zmq_pub.sh with GRD_ZMQ_PUB=1 and GRD_ZMQ_ENDPOINT
 * pointing at a grsub instance.
 *
 * @return 0 when the publisher initialized, 1 otherwise.
 */
static int emit_mode(void)
{
    if (grpub_init() != 0) {
        fprintf(stderr, "emit: grpub_init failed\n");
        return 1;
    }
    if (!grpub_enabled()) {
        fprintf(stderr, "emit: publisher not enabled (set GRD_ZMQ_PUB=1)\n");
        return 1;
    }

    char esc[64];
    grpub_event("app.gr.test", "k: 1");
    grpub_event("app.gr.escaped", "text: %s",
                grpub_yaml_escape(esc, sizeof(esc), "a\nb"));
    grpub_event("app.other.test", "should: be filtered");

    /* Let the messages drain before LINGER=0 discards them at close. */
    sleep(1);
    grpub_close();
    return 0;
}

int main(int argc, char **argv)
{
    if (argc > 1 && strcmp(argv[1], "--emit") == 0)
        return emit_mode();

    test_cfg_int();
    test_endpoint();
    test_yaml_escape();
    test_disabled_is_noop();

    printf("\nResults: %d passed, %d failed\n", s_pass, s_fail);
    return s_fail == 0 ? 0 : 1;
}
```

Add `#include <unistd.h>` at the top of the file for `sleep()`.

- [ ] **Step 3: Run the integration test to verify it fails**

```bash
nmake -f grdsrc.mk ../../../3b2/bin/test_gr_zmq_pub
./test_gr_zmq_pub.sh
```

Expected: **FAILS** — `grpub_event()` is still the Task 3 stub that sends nothing, so
`Results: 1 passed, 2 failed` (the "unsubscribed topic filtered out" assertion passes
trivially because nothing is sent at all).

- [ ] **Step 4: Implement the send in `gr_zmq_pub.cpp`**

Add the EINTR-retry helper before `grpub_event()`:

```cpp
/**
 * @brief zmq_send wrapper that retries transparently on EINTR.
 *
 * ZMQ returns EINTR when a blocking call is interrupted by a signal before any
 * bytes are handed to the transport, so retrying is safe and cannot double-send.
 * grd takes SIGALRM and SIGCHLD routinely, so this is a live path, not
 * defensive padding. All other errors are returned unchanged.
 *
 * @param sock   ZMQ socket.
 * @param data   Bytes to send.
 * @param len    Byte count.
 * @param flags  zmq_send flags.
 * @return Bytes sent, or -1 with errno set.
 */
static int grpub_send_frame(void *sock, const void *data, size_t len, int flags)
{
    for (;;) {
        int rc = zmq_send(sock, data, len, flags);
        if (rc >= 0)
            return rc;
        if (errno != EINTR)
            return rc;
    }
}
```

Replace the stub `grpub_event()` with:

```cpp
int grpub_event(const char *topic, const char *yaml_fmt, ...)
{
    if (!s_sock)
        return 0;

    char body[4096];
    va_list ap;
    va_start(ap, yaml_fmt);
    int n = vsnprintf(body, sizeof(body), yaml_fmt, ap);
    va_end(ap);

    if (n < 0) {
        TRACE(D3, "grpub: vsnprintf failed for topic=%s\n", topic);
        return -1;
    }
    size_t blen = ((size_t)n < sizeof(body)) ? (size_t)n : sizeof(body) - 1;

    /* ZMQ_DONTWAIT: a PUB socket at its HWM must drop, never stall the GR
     * main loop. Frame 0 = topic, frame 1 = YAML body. */
    if (grpub_send_frame(s_sock, topic, strlen(topic),
                         ZMQ_SNDMORE | ZMQ_DONTWAIT) < 0) {
        TRACE(D3, "grpub: topic frame send failed topic=%s: %s\n",
              topic, zmq_strerror(errno));
        return -1;
    }
    if (grpub_send_frame(s_sock, body, blen, ZMQ_DONTWAIT) < 0) {
        TRACE(D3, "grpub: body frame send failed topic=%s: %s\n",
              topic, zmq_strerror(errno));
        return -1;
    }

    TRACE(D4, "grpub: sent topic=%s body_len=%d\n", topic, (int)blen);
    return 0;
}
```

- [ ] **Step 5: Run both tests to verify they pass**

```bash
nmake -f grdsrc.mk ../../../3b2/bin/test_gr_zmq_pub
../../../3b2/bin/test_gr_zmq_pub
./test_gr_zmq_pub.sh
```

Expected: unit tests `Results: 21 passed, 0 failed`; shell test
`Results: 3 passed, 0 failed`. Both exit 0.

- [ ] **Step 6: Commit**

```bash
cd /home/dan/Git/hack-netflex
git add cnc/gr/src/gr_zmq_pub.cpp cnc/gr/src/test_gr_zmq_pub.cpp \
        cnc/gr/src/test_gr_zmq_pub.sh
git commit -m "feat(gr): implement two-frame event send with DONTWAIT

Topic frame plus YAML body frame, EINTR-retried. ZMQ_DONTWAIT so a full HWM
drops the event instead of stalling the GR main loop. Integration test drives
it against a bound grsub with no niimxd involved.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

## Task 6: Transition detection and heartbeat gating

Pure functions, no sockets, no GR globals — so they are unit-testable and the two
duplicated ladders in `gr_daemon2.cpp` cannot drift apart.

**Files:**
- Modify: `cnc/gr/src/gr_zmq_pub.h`
- Modify: `cnc/gr/src/gr_zmq_pub.cpp`
- Modify: `cnc/gr/src/test_gr_zmq_pub.cpp`

- [ ] **Step 1: Write the failing test**

Add to `test_gr_zmq_pub.cpp` above `main()`:

```cpp
/**
 * @brief grpub_state_delta(): reports exactly the fields that changed.
 */
static void test_state_delta(void)
{
    printf("grpub_state_delta:\n");

    grpub_state_t a;
    grpub_state_t b;
    memset(&a, 0, sizeof(a));
    memset(&b, 0, sizeof(b));

    a.rmt_working = 1; a.local_working = 1; a.mode = 1; a.rdb_ok = 1;
    b = a;

    eq_int("identical states yield no change",
           (int)grpub_state_delta(&a, &b), 0);

    b = a; b.rmt_working = 0;
    eq_int("mate change detected",
           (int)grpub_state_delta(&a, &b), GRPUB_CHG_MATE);

    b = a; b.local_working = 0;
    eq_int("local change detected",
           (int)grpub_state_delta(&a, &b), GRPUB_CHG_LOCAL);

    b = a; b.rdb_ok = 0;
    eq_int("rdb change detected",
           (int)grpub_state_delta(&a, &b), GRPUB_CHG_RDB);

    b = a; b.mode = 2;
    eq_int("role change detected",
           (int)grpub_state_delta(&a, &b), GRPUB_CHG_ROLE);

    b = a; b.rmt_working = 0; b.mode = 2;
    eq_int("simultaneous changes both reported",
           (int)grpub_state_delta(&a, &b),
           GRPUB_CHG_MATE | GRPUB_CHG_ROLE);

    /* check_result is carried for event bodies but must not itself be a
     * transition -- it changes on paths where no latched state moved. */
    b = a; b.check_result = 255;
    eq_int("check_result alone is not a transition",
           (int)grpub_state_delta(&a, &b), 0);

    eq_int("NULL prev reports everything changed",
           (int)grpub_state_delta(NULL, &b),
           GRPUB_CHG_MATE | GRPUB_CHG_LOCAL | GRPUB_CHG_RDB | GRPUB_CHG_ROLE);
}

/**
 * @brief grpub_heartbeat_due(): fires on the first call, then rate-limits.
 */
static void test_heartbeat_due(void)
{
    printf("grpub_heartbeat_due:\n");

    time_t last = 0;

    eq_int("first call is due", grpub_heartbeat_due(1000, &last, 30), 1);
    eq_int("first call records the time", (int)last, 1000);

    eq_int("immediate repeat is not due",
           grpub_heartbeat_due(1001, &last, 30), 0);
    eq_int("not-due call leaves last untouched", (int)last, 1000);

    eq_int("one second short is not due",
           grpub_heartbeat_due(1029, &last, 30), 0);
    eq_int("exactly at the interval is due",
           grpub_heartbeat_due(1030, &last, 30), 1);
    eq_int("due call advances last", (int)last, 1030);

    /* A backwards clock (NTP step, manual set) must not wedge the heartbeat
     * off for the remainder of the process lifetime. */
    eq_int("clock stepping backwards fires and resyncs",
           grpub_heartbeat_due(500, &last, 30), 1);
    eq_int("backwards step resets last", (int)last, 500);

    /* Interval 0 means every call. */
    time_t l2 = 0;
    eq_int("interval 0 always due (first)", grpub_heartbeat_due(10, &l2, 0), 1);
    eq_int("interval 0 always due (repeat)", grpub_heartbeat_due(10, &l2, 0), 1);
}
```

Register both in `main()` before the results line:

```cpp
    test_state_delta();
    test_heartbeat_due();
```

- [ ] **Step 2: Declare them in `gr_zmq_pub.h`**

Add before the `grpub_init()` declaration:

```c
/** @brief grpub_state_delta bit: mate reachability changed. */
#define GRPUB_CHG_MATE  0x01
/** @brief grpub_state_delta bit: local health changed. */
#define GRPUB_CHG_LOCAL 0x02
/** @brief grpub_state_delta bit: RDB path reachability changed. */
#define GRPUB_CHG_RDB   0x04
/** @brief grpub_state_delta bit: role (ACTIVE/STANDBY) changed. */
#define GRPUB_CHG_ROLE  0x08

/**
 * @brief Snapshot of the grd state that drives event publishing.
 *
 * Deliberately a plain struct of scalars copied out of gr_daemon2's globals,
 * so transition detection is a pure comparison that can be unit-tested without
 * the daemon.
 */
typedef struct {
    int rmt_working;    /**< Mirrors gr_daemon2 RmtWorking. */
    int local_working;  /**< Mirrors gr_daemon2 LocalWorking. */
    int mode;           /**< Mirrors gr_daemon2 Mode. */
    int rdb_ok;         /**< Last gr_rdb_path_check() result. */
    int conn_error;     /**< Mirrors gr_daemon2 ConnError. */
    int check_result;   /**< Last grd_check_remote_host() return; carried for
                             event bodies, NOT compared as a transition. */
} grpub_state_t;

/**
 * @brief Compare two state snapshots and report which fields changed.
 *
 * @p check_result is excluded from comparison: it varies on paths where no
 * latched state moved, and treating it as a transition would publish on every
 * loop pass.
 *
 * @param prev  Previously published snapshot, or NULL to mean "nothing
 *              published yet", which reports every field as changed.
 * @param cur   Current snapshot. Must not be NULL.
 * @return Bitwise OR of GRPUB_CHG_* bits; 0 when nothing changed.
 */
unsigned grpub_state_delta(const grpub_state_t *prev, const grpub_state_t *cur);

/**
 * @brief Decide whether a periodic heartbeat is due, and record it if so.
 *
 * The grd outer loop runs at GR_WAKEUP (20-300s) normally but drops to
 * GR_RDB_RETRY_INTERVAL (2s) during an RDB outage -- exactly when event volume
 * should not spike. Gating on elapsed time decouples heartbeat rate from loop
 * rate.
 *
 * Takes @p last by pointer rather than holding static state so the function is
 * pure with respect to its arguments and directly testable.
 *
 * A @p now earlier than @p *last (a backwards clock step) is treated as due and
 * resynchronizes @p *last, so an NTP correction cannot suppress heartbeats for
 * the rest of the process lifetime.
 *
 * @param now       Current time.
 * @param last      In/out: time of the last heartbeat. Updated only when this
 *                  returns nonzero. Initialize to 0 for "never".
 * @param interval  Minimum seconds between heartbeats. 0 means every call.
 * @return Nonzero when a heartbeat should be published now.
 */
int grpub_heartbeat_due(time_t now, time_t *last, int interval);
```

- [ ] **Step 3: Run the test to verify it fails**

```bash
nmake -f grdsrc.mk ../../../3b2/bin/test_gr_zmq_pub
```

Expected: **link FAILS** with undefined references to `grpub_state_delta` and
`grpub_heartbeat_due`.

- [ ] **Step 4: Implement them in `gr_zmq_pub.cpp`**

Add after `grpub_yaml_escape()`:

```cpp
unsigned grpub_state_delta(const grpub_state_t *prev, const grpub_state_t *cur)
{
    if (!cur)
        return 0;

    if (!prev)
        return GRPUB_CHG_MATE | GRPUB_CHG_LOCAL | GRPUB_CHG_RDB | GRPUB_CHG_ROLE;

    unsigned chg = 0;
    if (prev->rmt_working   != cur->rmt_working)   chg |= GRPUB_CHG_MATE;
    if (prev->local_working != cur->local_working) chg |= GRPUB_CHG_LOCAL;
    if (prev->rdb_ok        != cur->rdb_ok)        chg |= GRPUB_CHG_RDB;
    if (prev->mode          != cur->mode)          chg |= GRPUB_CHG_ROLE;
    return chg;
}

int grpub_heartbeat_due(time_t now, time_t *last, int interval)
{
    if (!last)
        return 0;

    if (interval <= 0) {
        *last = now;
        return 1;
    }

    /* now < *last means the clock stepped backwards. Fire and resync rather
     * than waiting out a bogus future deadline. */
    if (now < *last || now - *last >= interval) {
        *last = now;
        return 1;
    }
    return 0;
}
```

- [ ] **Step 5: Run the test to verify it passes**

```bash
nmake -f grdsrc.mk ../../../3b2/bin/test_gr_zmq_pub
../../../3b2/bin/test_gr_zmq_pub
```

Expected: `Results: 40 passed, 0 failed`, exit 0.

- [ ] **Step 6: Commit**

```bash
cd /home/dan/Git/hack-netflex
git add cnc/gr/src/gr_zmq_pub.h cnc/gr/src/gr_zmq_pub.cpp cnc/gr/src/test_gr_zmq_pub.cpp
git commit -m "feat(gr): add pure transition detection and heartbeat gating

grpub_state_delta compares snapshots so the two duplicated result ladders in
ACTIVE_mode/STANDBY_mode cannot drift. grpub_heartbeat_due decouples heartbeat
rate from the loop rate, which drops to 2s during an RDB outage, and tolerates
a backwards clock step.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

## Task 7: Wire the module into grd

Lifecycle plus the choke-point function covering `mate.state`, `local.state`,
`rdb.state`, `role`, and `heartbeat`.

**Files:**
- Modify: `cnc/gr/src/gr_daemon2.cpp`
- Modify: `cnc/gr/src/grdsrc.mk`

- [ ] **Step 1: Add the include and forward declarations**

In `gr_daemon2.cpp`, add after `#include "utillibinc.h"` (gr_daemon2.cpp:42):

```cpp
#include "gr_zmq_pub.h"
```

Add to the forward-declaration block near gr_daemon2.cpp:128:

```cpp
static void grd_publish_state(GRGLOBAL& g,int check_result,int rdb_ok);
static const char *grd_mate_state_str(int check_result);
```

- [ ] **Step 2: Add the file-static publish state**

Add beside the other file statics, after the `alarm_time` declaration
(gr_daemon2.cpp:183):

```cpp
//Last state snapshot published to the ZMQ bus, and whether one ever was.
//grpub_state_delta() compares against this so a transition publishes exactly
//once regardless of which mode loop or which ladder branch observed it.
static grpub_state_t GrPubLast;
static int GrPubLastValid = 0;
//Last heartbeat time, gated by grpub_heartbeat_due() so heartbeat cadence is
//independent of the outer loop rate.
static time_t GrPubHeartbeatLast = 0;
//Last RDB probe result, so STANDBY_mode (which does not probe RDB) publishes a
//consistent snapshot rather than flapping the rdb field.
static int GrPubRdbOk = 1;
```

- [ ] **Step 3: Implement the choke point**

Add before `ACTIVE_mode` (gr_daemon2.cpp:2112):

```cpp
/**
 * @brief Map a grd_check_remote_host() return to a mate state string.
 *
 * @param check_result  Value returned by grd_check_remote_host().
 * @return Stable lowercase token for the app.gr.mate.state event.
 */
static const char *grd_mate_state_str(int check_result)
{
	switch (check_result) {
	case 0:               return "up";
	case GR_CONN_ERROR:   return "conn_error";
	case GR_APP_DOWN:     return "app_down";
	case GR_LOCAL_ERROR:  return "local_error";
	case GR_SINGLE_SWITCH:return "single_switch";
	case GR_FATAL_SIGNAL: return "fatal_signal";
	default:              return "unknown";
	}
}

/**
 * @brief Publish grd state transitions and the periodic heartbeat.
 *
 * Called by both ACTIVE_mode and STANDBY_mode immediately after
 * grd_check_remote_host(). Snapshots the current state, compares against the
 * last published snapshot, and emits only what changed.
 *
 * Comparing snapshots -- rather than publishing inside the result ladders --
 * is deliberate. ACTIVE_mode (gr_daemon2.cpp) and STANDBY_mode carry
 * near-duplicate ladders that differ: STANDBY_mode returns on GR_CONN_ERROR
 * and GR_APP_DOWN instead of latching RmtWorking. A publish call inside a
 * ladder branch would fire on some paths and not others, and the two copies
 * would drift as either ladder is edited.
 *
 * A no-op when publishing is disabled: every grpub_event() call returns
 * immediately. GR failover must never depend on niimxd, so nothing here
 * affects control flow and no return value is checked.
 *
 * @param g             Daemon global state, for the mate hostname.
 * @param check_result  Value just returned by grd_check_remote_host().
 * @param rdb_ok        Most recent gr_rdb_path_check() result.
 */
static void grd_publish_state(GRGLOBAL& g,int check_result,int rdb_ok)
{
	if (!grpub_enabled()) return;

	grpub_state_t cur;
	memset(&cur,0,sizeof(cur));
	cur.rmt_working   = RmtWorking;
	cur.local_working = LocalWorking;
	cur.mode          = Mode;
	cur.rdb_ok        = rdb_ok;
	cur.conn_error    = ConnError;
	cur.check_result  = check_result;

	char host[128], mate[128], reason[256];
	grpub_yaml_escape(host,sizeof(host),LclSysname);
	grpub_yaml_escape(mate,sizeof(mate),RmtSysname);

	//Envelope shared by every event. seq lets a subscriber detect gaps, which
	//plain ZMQ pub/sub cannot otherwise report.
	static unsigned long seq = 0;
	char env[512];
	snprintf(env,sizeof(env),
		"host: %s\nrole: %s\npid: %d\nseq: %lu\nts: %ld",
		host,MODE_TO_STR(Mode),(int)GR_pid,++seq,(long)time(0));

	unsigned chg = grpub_state_delta(GrPubLastValid?&GrPubLast:NULL,&cur);

	if (chg & GRPUB_CHG_MATE) {
		grpub_yaml_escape(reason,sizeof(reason),grd_mate_state_str(check_result));
		grpub_event("app.gr.mate.state",
			"%s\nmate: %s\nstate: %s\nreason: %s\ncheck_result: %d",
			env,mate,RmtWorking?"\"up\"":"\"down\"",reason,check_result);
	}

	if (chg & GRPUB_CHG_LOCAL) {
		grpub_event("app.gr.local.state",
			"%s\nstate: %s\ncheck_result: %d",
			env,LocalWorking?"\"up\"":"\"error\"",check_result);
	}

	if (chg & GRPUB_CHG_RDB) {
		grpub_event("app.gr.rdb.state",
			"%s\nstate: %s\nfail_timeout: %d",
			env,rdb_ok?"\"up\"":"\"down\"",gr_rdb_fail_timeout);
	}

	//NOTE: app.gr.role is deliberately NOT published here. This function only
	//observes that Mode changed; it cannot know WHY, and the spec requires a
	//cause. Task 7b publishes app.gr.role from main(), which has the mode-loop
	//return code and therefore the cause. GRPUB_CHG_ROLE is still tracked so
	//the snapshot and heartbeat stay accurate.
	(void)(chg & GRPUB_CHG_ROLE);

	GrPubLast      = cur;
	GrPubLastValid = 1;

	int hb_interval = grpub_cfg_int(GRPUB_ENV_HEARTBEAT,
					GRPUB_SYSDEF_HEARTBEAT,
					GRPUB_DEF_HEARTBEAT);
	if (grpub_heartbeat_due(time(0),&GrPubHeartbeatLast,hb_interval)) {
		grpub_event("app.gr.heartbeat",
			"%s\nmate: %s\nmate_working: %d\nlocal_working: %d\n"
			"rdb_ok: %d\nconn_error: %d\ncheck_result: %d\nwakeup: %d",
			env,mate,RmtWorking,LocalWorking,rdb_ok,ConnError,
			check_result,gr_wakeup);
	}
}
```

Note: `MODE_TO_STR(Mode)` yields a bare token, so the `role:` and `to:` fields are
unquoted YAML scalars. That is valid for these fixed tokens (`ACTIVE`, `STANDBY`); the
quoted literals above (`"up"`, `"down"`) keep the surrounding fields consistent.

- [ ] **Step 4: Initialize and tear down the publisher**

In `main()`, immediately after the `begin_gr_daemon();` call (gr_daemon2.cpp:1642):

```cpp
	//AFTER begin_gr_daemon(): it forks to daemonize, and a ZMQ context created
	//before that fork is unusable in the child.
	if (grpub_init() != 0) {
		TRACE(0,"WARNING: grpub_init failed; GR event publishing disabled\n");
	}
	atexit(grpub_close);
```

Before the self-restart `execl()` (gr_daemon2.cpp:1583), add:

```cpp
		grpub_close();
```

- [ ] **Step 5: Call the choke point from both loops**

In `ACTIVE_mode`, immediately after the `grd_check_remote_host()` call and its
`TRACE` (gr_daemon2.cpp:2301-2304), add:

```cpp
		GrPubRdbOk = rdb_ok ? 1 : 0;
		grd_publish_state(g,result,GrPubRdbOk);
```

In `STANDBY_mode`, immediately after its `grd_check_remote_host()` call and `TRACE`
(gr_daemon2.cpp:2606-2607), add:

```cpp
		//STANDBY_mode does not probe RDB; carry the last known value so the
		//rdb field does not flap as the daemon changes roles.
		grd_publish_state(g,result,GrPubRdbOk);
```

Both calls sit **before** the ladders, so they run on every pass including the ones
where a ladder returns early.

- [ ] **Step 6: Add the module to the grd link line**

In `grdsrc.mk`, change the `grd` target (grdsrc.mk:30-31) from:

```
$(PBIN)/grd : gr_daemon2.o $(CORELIBS) $(UTILLIB)  -lnelib -latm -linc -lgr -lrest++ -lgr++ -lcncdb
	$(CPLUS_CC)  $(LDFLAGS) -o $(<) $(*) $(ACCLIBS) -lpthread
```

to:

```
$(PBIN)/grd : gr_daemon2.o gr_zmq_pub.o $(CORELIBS) $(UTILLIB)  -lnelib -latm -linc -lgr -lrest++ -lgr++ -lcncdb
	$(CPLUS_CC)  $(LDFLAGS) -o $(<) $(*) $(ACCLIBS) -lpthread -lzmq
```

Also add `gr_zmq_pub.o` to `GRD_OBJS` (grdsrc.mk:27) so `lint` covers it:

```
GRD_OBJS = gr_daemon2.o gr_zmq_pub.o
```

- [ ] **Step 7: Build and verify**

```bash
nmake -f grdsrc.mk ../../../3b2/bin/grd
```

Expected: builds without error.

- [ ] **Step 8: Verify the disabled path is inert**

```bash
GRD_ZMQ_PUB=0 ../../../3b2/bin/grd -V 2>&1 | head -5 || true
```

Expected: whatever `grd -V` normally prints, with no ZMQ-related error. The point is
that a `grd` built with the module still starts. If `grd` has no `-V` flag, skip this
step and rely on Task 13's runtime verification.

- [ ] **Step 9: Commit**

```bash
cd /home/dan/Git/hack-netflex
git add cnc/gr/src/gr_daemon2.cpp cnc/gr/src/grdsrc.mk
git commit -m "feat(gr): publish mate/local/rdb/role state and heartbeat from grd

One choke point called by both ACTIVE_mode and STANDBY_mode before their
result ladders, so transitions publish exactly once even where STANDBY_mode
returns early instead of latching RmtWorking. grpub_init runs after the
daemonize fork; grpub_close runs before the self-restart execl and via atexit.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

## Task 7b: `app.gr.role` with a real cause

The choke point in Task 7 can see that `Mode` changed but not why, and the spec requires
`cause: mode_switch|single_switch|forceswitch|rdb_timeout`. Every switch path in both
mode loops `return`s a code that `main()` then acts on — roughly eleven `return`
statements funnel into one place. Publishing from `main()` needs one call site instead of
eleven and is the only place that knows both the code and the resulting role change.

**Files:**
- Modify: `cnc/gr/src/gr_daemon2.cpp`

- [ ] **Step 1: Add the cause mapping and publish helper**

Add before `main()` (gr_daemon2.cpp:1573):

```cpp
/**
 * @brief Map a mode-loop return code to an app.gr.role cause token.
 *
 * @param rc  Value returned by ACTIVE_mode() or STANDBY_mode().
 * @return Stable lowercase cause token.
 */
static const char *grd_role_cause_str(int rc)
{
	switch (rc) {
	case GR_MODE_SWITCH:   return "mode_switch";
	case GR_SINGLE_SWITCH: return "single_switch";
	case GR_CONN_ERROR:    return "conn_error";
	case GR_APP_DOWN:      return "app_down";
	case GR_FATAL_SIGNAL:  return "fatal_signal";
	case GR_LOCAL_ERROR:   return "local_error";
	default:               return "unknown";
	}
}

/**
 * @brief Publish an app.gr.role event for a role change, with its cause.
 *
 * Called from main() where the mode-loop return code is available. Task 7's
 * grd_publish_state() deliberately does not publish this topic: it observes
 * only that Mode changed, and cannot supply a cause.
 *
 * A no-op when publishing is disabled.
 *
 * @param from_mode  Mode before the change.
 * @param to_mode    Mode after the change.
 * @param rc         Mode-loop return code that caused it.
 */
static void grd_publish_role(int from_mode,int to_mode,int rc)
{
	if (!grpub_enabled()) return;

	char host[128], cause[64];
	grpub_yaml_escape(host,sizeof(host),LclSysname);
	grpub_yaml_escape(cause,sizeof(cause),grd_role_cause_str(rc));

	grpub_event("app.gr.role",
		"host: %s\npid: %d\nts: %ld\n"
		"from: %s\nto: %s\ncause: %s\nreturn_code: %d\nswitch_enabled: %d",
		host,(int)GR_pid,(long)time(0),
		MODE_TO_STR(from_mode),MODE_TO_STR(to_mode),cause,rc,
		strncmp(Tokens[GR_ENABLE_SWITCH_INDEX],"false",5)==0?0:1);
}
```

- [ ] **Step 2: Publish at the two role-change branches in `main()`**

`main()` handles the loop return at gr_daemon2.cpp:1830-2090. Two branches change role.
The mode value constants are `GR_ACTIVE` and `GR_STANDBY` (include/geo_redun.h:58-59).

In the STANDBY-to-ACTIVE branch — after `TRACE(0,"Switching from Standby to Active\n");`
(gr_daemon2.cpp:1841):

```cpp
				grd_publish_role(Mode,GR_ACTIVE,result);
```

In the ACTIVE-to-STANDBY branch — after
`TRACE(0,"Changing From Active to Standby\n");` (gr_daemon2.cpp:1849):

```cpp
				grd_publish_role(Mode,GR_STANDBY,result);
```

Both read `Mode` before it is reassigned, so `from` is the outgoing role. Verify that
ordering against the surrounding code: if `Mode` is already updated by the time you reach
the `TRACE`, capture it into a local before the reassignment and pass that instead.

One `MODE_TO_STR` caveat: it expands to an unparenthesized ternary
(`MODE_IS_ACTIVE(mode)?ACTIVE_STR:MODE_IS_STANDBY(mode)?STANDBY_STR:"ERROR"`,
include/geo_redun.h:64). As a bare function argument that is safe — the comma operator
binds looser than `?:` — but never embed it in a larger expression without wrapping it
in parentheses.

- [ ] **Step 3: Publish the RDB-forced switch cause**

The RDB outage path forces a switch through a different route — it shells out to
`gr --op forceswitch` and returns `GR_SINGLE_SWITCH` (gr_daemon2.cpp:2280-2287), which
would otherwise be reported as `single_switch` rather than the more specific
`rdb_timeout`. Publish it at the source, immediately after the
`TRACE(0, "RDB unreachable for %d s; forcing switch\n", gr_rdb_fail_timeout);` line:

```cpp
					if (grpub_enabled()) {
						char h[128];
						grpub_yaml_escape(h,sizeof(h),LclSysname);
						grpub_event("app.gr.role",
							"host: %s\npid: %d\nts: %ld\n"
							"from: %s\nto: \"unknown\"\ncause: \"rdb_timeout\"\n"
							"return_code: %d\nfail_timeout: %d",
							h,(int)GR_pid,(long)time(0),MODE_TO_STR(Mode),
							GR_SINGLE_SWITCH,gr_rdb_fail_timeout);
					}
```

`to: "unknown"` is accurate here: `gr --op forceswitch` decides the resulting role, and
this code does not learn it before returning. The subsequent `main()` event from Step 2
reports the actual transition.

- [ ] **Step 4: Build**

```bash
nmake -f grdsrc.mk ../../../3b2/bin/grd
```

Expected: builds without error.

- [ ] **Step 5: Commit**

```bash
cd /home/dan/Git/hack-netflex
git add cnc/gr/src/gr_daemon2.cpp
git commit -m "feat(gr): publish app.gr.role with the switch cause

Published from main(), where the mode-loop return code is available -- eleven
switch return sites funnel there, so one call site covers them all. The Task 7
choke point deliberately skips this topic: it sees that Mode changed but not
why, and the event requires a cause. The RDB-forced switch publishes at its
source to report rdb_timeout rather than the generic single_switch.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

## Task 8: `app.gr.data` call sites

These outcomes are distinct branches of `grd_check_remote_host()` with no shared
variable to snapshot, so they need in-place calls.

**Files:**
- Modify: `cnc/gr/src/gr_daemon2.cpp`

- [ ] **Step 1: Add a local helper**

Add immediately before `grd_check_remote_host()` (gr_daemon2.cpp:3395):

```cpp
/**
 * @brief Publish an app.gr.data event describing a .GR_DATA arbitration outcome.
 *
 * A no-op when publishing is disabled.
 *
 * @param action     Outcome token, e.g. "in_sync", "sent", "pulled".
 * @param lcl_mtime  Local .GR_DATA mtime, or 0 when not determined.
 * @param rmt_mtime  Remote .GR_DATA mtime, or 0 when not determined.
 * @param fail_index Differing token index, or -1 when the files matched.
 */
static void grd_publish_data(const char *action,time_t lcl_mtime,
			     time_t rmt_mtime,int fail_index)
{
	if (!grpub_enabled()) return;

	char host[128], mate[128], act[64];
	grpub_yaml_escape(host,sizeof(host),LclSysname);
	grpub_yaml_escape(mate,sizeof(mate),RmtSysname);
	grpub_yaml_escape(act,sizeof(act),action);

	grpub_event("app.gr.data",
		"host: %s\nrole: %s\npid: %d\nts: %ld\n"
		"mate: %s\naction: %s\nlcl_mtime: %ld\nrmt_mtime: %ld\nfail_index: %d",
		host,MODE_TO_STR(Mode),(int)GR_pid,(long)time(0),
		mate,act,(long)lcl_mtime,(long)rmt_mtime,fail_index);
}
```

- [ ] **Step 2: Add the six call sites**

Each goes immediately after the existing `TRACE` at that outcome, so the event and the
trace line always agree.

1. Remote has no `.GR_DATA`, ours was sent — after the `scp` result check inside the
   `WEXITSTATUS(result) == 1` branch (gr_daemon2.cpp:3455-3461):

```cpp
			grd_publish_data("created_remote",0,0,-1);
```

2. Tokens matched, nothing to do — at the `return 0;` reached when `FailFlag == -1`
   (the final `return 0;` of the `else` block, gr_daemon2.cpp:3711). Insert before it:

```cpp
		if (FailFlag == -1) grd_publish_data("in_sync",lclbuf_mtime,rmtbuf.st_mtime,-1);
```

   This needs the local mtime in scope. Add near the top of the `else` block, right
   after the successful `stat(GR_DATA_RMT_COPY, &rmtbuf)` check (gr_daemon2.cpp:3533):

```cpp
		//Captured here so the in_sync event can report both mtimes; the
		//existing local stat() happens only on the differing-data path.
		time_t lclbuf_mtime = 0;
		{
			struct stat lb;
			memset(&lb,0,sizeof(lb));
			if (stat(GR_DATA,&lb) == 0) lclbuf_mtime = lb.st_mtime;
		}
```

3. Remote data newer, pulled — after the `grLog(...)` following the successful
   `cp` (gr_daemon2.cpp:3655):

```cpp
					grd_publish_data("pulled",lclbuf.st_mtime,rmtbuf.st_mtime,FailFlag);
```

4. Local data newer, sent — after the `grLog(...)` following the `scp`
   (gr_daemon2.cpp:3690):

```cpp
					grd_publish_data("sent",lclbuf.st_mtime,rmtbuf.st_mtime,FailFlag);
```

5. Connection-error arbitration touched our file — after each
   `system("touch /usr/cnc/.GR_DATA");` inside the `if (ConnError)` block. There are
   four such calls (gr_daemon2.cpp:3583, 3596, 3612, 3628); add after each:

```cpp
						grd_publish_data("touched_primary",0,rmtbuf.st_mtime,FailFlag);
```

6. Difference detected but no branch acted — immediately before the closing brace of
   the `if (FailFlag != -1)` block, so a silent fall-through is still observable:

```cpp
			grd_publish_data("diff_unresolved",0,rmtbuf.st_mtime,FailFlag);
```

- [ ] **Step 3: Build**

```bash
nmake -f grdsrc.mk ../../../3b2/bin/grd
```

Expected: builds without error. If `lclbuf` is not in scope at call sites 3 and 4,
substitute `lclbuf_mtime` — verify against the actual declaration at
gr_daemon2.cpp:3634 before choosing.

- [ ] **Step 4: Commit**

```bash
cd /home/dan/Git/hack-netflex
git add cnc/gr/src/gr_daemon2.cpp
git commit -m "feat(gr): publish app.gr.data for .GR_DATA arbitration outcomes

Six call sites in grd_check_remote_host, each beside the existing TRACE so the
event and the trace line cannot disagree. Includes a diff_unresolved event so a
silent fall-through through the ConnError ladder is observable.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

## Task 9: `app.gr.bep` call sites

**Files:**
- Modify: `cnc/gr/src/gr_daemon2.cpp`

- [ ] **Step 1: Add a local helper**

Add immediately before `grd_sync_data()` (gr_daemon2.cpp:791):

```cpp
/**
 * @brief Publish an app.gr.bep event describing a BEP flip decision.
 *
 * A no-op when publishing is disabled.
 *
 * @param bep     BEP name being made active.
 * @param from    Previously active BEP name.
 * @param cause   Reason token: "up_cnt", "active_down", or "rdr_down".
 * @param detail  Numeric detail for the cause (up_cnt difference, or state).
 */
static void grd_publish_bep(const char *bep,const char *from,
			    const char *cause,int detail)
{
	if (!grpub_enabled()) return;

	char b[128], f[128], c[64];
	grpub_yaml_escape(b,sizeof(b),bep);
	grpub_yaml_escape(f,sizeof(f),from);
	grpub_yaml_escape(c,sizeof(c),cause);

	grpub_event("app.gr.bep",
		"pid: %d\nts: %ld\nbep: %s\nfrom: %s\ncause: %s\ndetail: %d",
		(int)GR_pid,(long)time(0),b,f,c,detail);
}
```

`grd_sync_data()` runs before `LclSysname` is necessarily populated in every path, so
this event omits the host field rather than risk publishing an empty one.

- [ ] **Step 2: Add the three call sites in `grd_sync_data()`**

1. Flip because of `up_cnt` — after the `TRACE(0,"INFO: Flip to BEP[%s] because of up_cnt\n",...)` (gr_daemon2.cpp:877):

```cpp
							grd_publish_bep(bp->second.standby.name.c_str(),
								bp->second.active.name.c_str(),
								"up_cnt",diff);
```

2. Flip because the active BEP is down — after the
   `TRACE(0,"INFO: Flip to BEP[%s] because [%s] is down \n",...)` (gr_daemon2.cpp:885):

```cpp
						grd_publish_bep(bp->second.standby.name.c_str(),
							bp->second.active.name.c_str(),
							"active_down",bp->second.active.state);
```

3. Flip because the active MSG RDR is down — after the
   `TRACE(0,"INFO: Flip to BEP[%s] because [%s] MSG RDR is down \n",...)`
   (gr_daemon2.cpp:891):

```cpp
						grd_publish_bep(bp->second.standby.name.c_str(),
							bp->second.active.name.c_str(),
							"rdr_down",active_rdr_status);
```

- [ ] **Step 3: Build**

```bash
nmake -f grdsrc.mk ../../../3b2/bin/grd
```

Expected: builds without error.

- [ ] **Step 4: Commit**

```bash
cd /home/dan/Git/hack-netflex
git add cnc/gr/src/gr_daemon2.cpp
git commit -m "feat(gr): publish app.gr.bep for BEP flip decisions

Three call sites in grd_sync_data, beside the existing INFO traces. Omits the
host field because LclSysname is not reliably populated on every path into
grd_sync_data.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

## Task 10: `app.gr.transfer` with SIGCHLD deferral

Children are reaped from two places that race for the same PIDs: the main-loop
`while ((kid = wait(&status)) > 0)` (gr_daemon2.cpp:2504) and the `waitpid` loop inside
the **SIGCHLD handler** (gr_daemon2.cpp:2860). Publishing from the handler is not safe —
`grpub_event()` calls `zmq_send` and `vsnprintf`, neither async-signal-safe. Publishing
only from the main loop would silently drop every exit the handler reaped first.

So the handler records the exit into a signal-safe slot and the main loop publishes it.

**Files:**
- Modify: `cnc/gr/src/gr_daemon2.cpp`

- [ ] **Step 1: Add the deferral slots**

Add beside the `PidTab` declaration (gr_daemon2.cpp:175):

```cpp
//Exits reaped by the SIGCHLD handler, deferred for the main loop to publish.
//grpub_event() calls vsnprintf() and zmq_send(), neither async-signal-safe, so
//the handler may only record. Parallel arrays indexed like PidTab.
static volatile sig_atomic_t GrPubExitPid[GR_MAX_PASSES];
static volatile sig_atomic_t GrPubExitStatus[GR_MAX_PASSES];
static volatile sig_atomic_t GrPubExitPending;
```

- [ ] **Step 2: Add the publish helper**

Add before `ACTIVE_mode` (gr_daemon2.cpp:2112), next to `grd_publish_state()`:

```cpp
/**
 * @brief Publish one app.gr.transfer event.
 *
 * A no-op when publishing is disabled.
 *
 * @param phase      "start" or "end".
 * @param pass       Pass index into PidTab / GR_groups.
 * @param pid        Child pid.
 * @param exit_code  Child exit status, or -1 for a start event.
 */
static void grd_publish_transfer(const char *phase,int pass,pid_t pid,int exit_code)
{
	if (!grpub_enabled()) return;

	char host[128], ph[32], grp[64];
	grpub_yaml_escape(host,sizeof(host),LclSysname);
	grpub_yaml_escape(ph,sizeof(ph),phase);
	grpub_yaml_escape(grp,sizeof(grp),
		(pass >= 0 && pass < GR_MAX_PASSES) ? GR_groups[pass] : "");

	grpub_event("app.gr.transfer",
		"host: %s\nrole: %s\nts: %ld\n"
		"phase: %s\npass: %d\ngroup: %s\npid: %d\nexit_code: %d",
		host,MODE_TO_STR(Mode),(long)time(0),
		ph,pass,grp,(int)pid,exit_code);
}

/**
 * @brief Publish any child exits recorded by the SIGCHLD handler.
 *
 * Called from the main loop. The handler can only record into
 * GrPubExitPid/GrPubExitStatus; formatting and sending happen here, outside
 * signal context.
 */
static void grd_publish_deferred_exits()
{
	if (!GrPubExitPending) return;
	GrPubExitPending = 0;

	for (int i = 0; i < GR_MAX_PASSES; i++) {
		pid_t pid = (pid_t)GrPubExitPid[i];
		if (pid == 0) continue;
		int status = (int)GrPubExitStatus[i];
		GrPubExitPid[i] = 0;
		grd_publish_transfer("end",i,pid,
			WIFEXITED(status)?WEXITSTATUS(status):-1);
	}
}
```

- [ ] **Step 3: Record exits in the SIGCHLD handler**

Inside the handler's `while ((pid = waitpid(-1, &status, WNOHANG)) > 0)` loop
(gr_daemon2.cpp:2860), add:

```cpp
			//Record only -- grpub_event() is not async-signal-safe.
			//grd_publish_deferred_exits() sends these from the main loop.
			for (int i = 0; i < GR_MAX_PASSES; i++) {
				if (PidTab[i] == pid) {
					GrPubExitPid[i]    = (sig_atomic_t)pid;
					GrPubExitStatus[i] = (sig_atomic_t)status;
					GrPubExitPending   = 1;
					break;
				}
			}
```

- [ ] **Step 4: Publish the start event**

After `PidTab[iidx] = pid;` (gr_daemon2.cpp:2488), add:

```cpp
							grd_publish_transfer("start",iidx,pid,-1);
```

- [ ] **Step 5: Publish main-loop reaps and drain the deferred queue**

Inside the main-loop reap at gr_daemon2.cpp:2504-2506, within the
`if (PidTab[iidx] == kid && status != 0)` region, add — placed so it fires for any
matched child, not only nonzero status. Immediately after the
`while ((kid = wait(&status)) > 0) {` line, add:

```cpp
			for (int pidx = 0; pidx < GR_MAX_PASSES; pidx++) {
				if (PidTab[pidx] == kid) {
					grd_publish_transfer("end",pidx,kid,
						WIFEXITED(status)?WEXITSTATUS(status):-1);
					//Claimed here; keep the handler's deferral from
					//publishing the same exit twice.
					GrPubExitPid[pidx] = 0;
					break;
				}
			}
```

Then add the drain call in both loops. In `ACTIVE_mode`, immediately after the
`grd_publish_state(...)` call added in Task 7:

```cpp
		grd_publish_deferred_exits();
```

And the same line in `STANDBY_mode` after its `grd_publish_state(...)` call.

- [ ] **Step 6: Build**

```bash
nmake -f grdsrc.mk ../../../3b2/bin/grd
```

Expected: builds without error.

- [ ] **Step 7: Commit**

```bash
cd /home/dan/Git/hack-netflex
git add cnc/gr/src/gr_daemon2.cpp
git commit -m "feat(gr): publish app.gr.transfer with SIGCHLD deferral

Children are reaped from both the main loop and the SIGCHLD handler, racing
for the same pids. grpub_event calls vsnprintf and zmq_send, neither
async-signal-safe, so the handler records into volatile sig_atomic_t slots and
the main loop publishes. Both sites clear the slot so an exit publishes once.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

## Task 11: niimxd loopback TCP XPUB endpoint

**Files:**
- Modify: `cnc/niimx/src/niimxd.cpp`

- [ ] **Step 1: Add the default endpoint macro**

After `#define NIIMX_DEF_NFDB_SUB_ENDPOINT ...` (niimxd.cpp:101):

```cpp
/**
 * @brief Additional TCP bind for the nfdb XPUB socket, for subscribers that
 * cannot use ipc:// transport.
 *
 * Loopback only. The XPUB bus carries db.* record events with field-level
 * diffs and app.* application state; ZMQ provides no authentication or
 * encryption unless CURVE is configured, and subscription filtering is
 * advisory -- a subscriber sending SUBSCRIBE "" receives every topic. Binding
 * this to a routable address would publish all of it to the network, so
 * widening it must be evaluated together with CURVE, not done by editing this
 * default.
 *
 * Setting NIIMX.PUB_TCP_ENDPOINT to the literal "none" disables the bind.
 */
#define NIIMX_DEF_NFDB_PUB_TCP_ENDPOINT "tcp://127.0.0.1:5560"
```

- [ ] **Step 2: Add the config field**

In `struct NiimxConfig`, beside `endpoint` (niimxd.cpp:162):

```cpp
    char pub_tcp_endpoint[256] = NIIMX_DEF_NFDB_PUB_TCP_ENDPOINT;  /**< NIIMX.PUB_TCP_ENDPOINT: extra TCP bind for nfdb XPUB; "none" disables. */
```

Add the matching `@var` line to the struct's Doxygen block (near niimxd.cpp:149):

```
 * @var NiimxConfig::pub_tcp_endpoint  NIIMX.PUB_TCP_ENDPOINT: extra TCP bind for the nfdb XPUB socket; "none" disables.
```

- [ ] **Step 3: Load it in `niimx_load_config()`**

`get_sysdef_str()` leaves the buffer empty when the key is absent — that is why the
existing code reads `if (!cfg.endpoint[0]) strcpy(cfg.endpoint, NIIMX_DEF_ENDPOINT);`.
So "key absent" and "key explicitly empty" are indistinguishable, and empty cannot mean
"disabled". The disable value is therefore the literal string `none`.

In `niimx_load_config()`, add after the `NIIMX.ENDPOINT=` line:

```cpp
    get_sysdef_str("NIIMX.PUB_TCP_ENDPOINT=", cfg.pub_tcp_endpoint, sizeof(cfg.pub_tcp_endpoint)-1);
```

And after the existing `if (!cfg.endpoint[0]) strcpy(cfg.endpoint, NIIMX_DEF_ENDPOINT);`:

```cpp
    /* get_sysdef_str leaves the buffer empty when the key is absent, so empty
     * cannot distinguish "unset" from "deliberately disabled". Absent means
     * take the default; the literal "none" disables the TCP bind. */
    if (!cfg.pub_tcp_endpoint[0])
        strcpy(cfg.pub_tcp_endpoint, NIIMX_DEF_NFDB_PUB_TCP_ENDPOINT);
    if (0 == strcmp(cfg.pub_tcp_endpoint, "none"))
        cfg.pub_tcp_endpoint[0] = '\0';
```

Add it to the config `Trc` at the end of the function, inside the existing chain:

```cpp
           << " pub_tcp_endpoint=[" << cfg.pub_tcp_endpoint << "]"
```

Do **not** add `pub_tcp_endpoint` to `niimx_reload_config()` (niimxd.cpp:2161-2251).
Socket binds happen once at startup, and rebinding a live XPUB would drop every
connected subscriber. Add this comment there instead:

```cpp
    //pub_tcp_endpoint is deliberately NOT reloaded: rebinding a live XPUB
    //drops connected subscribers. Changing it requires a niimxd restart.
```

- [ ] **Step 4: Add the second bind**

In `niimx_setup_infrastructure()`, immediately after the existing successful IPC bind
and its `Trc` (niimxd.cpp:872-876):

```cpp
    //A ZMQ socket may bind multiple endpoints, so subscribers arriving over
    //TCP are served by the same XPUB, the same relay, and the same ZMQ_FD
    //epoll registration -- no new fd, no broker change.
    if (Cfg.pub_tcp_endpoint[0]) {
        if (0 != zmq_bind(G.nfdb_xpub, Cfg.pub_tcp_endpoint)) {
            //NOT fatal: the TCP endpoint is a convenience. Losing it must
            //never cost us the local IPC bus, which is already bound above.
            Err("nfdb XPUB TCP bind to " << Cfg.pub_tcp_endpoint
                << " failed errno=" << errno
                << "; continuing with ipc only" << endl);
        } else {
            Trc(2, "nfdb XPUB also bound to " << Cfg.pub_tcp_endpoint << endl);
        }
    }
```

- [ ] **Step 5: Add it to the interactive config dump**

In `main()`, beside the other `printf` lines in the `isatty` block (niimxd.cpp:31 of
that block, after the `NIIMX.ENDPOINT` line):

```cpp
        printf("  NIIMX.PUB_TCP_ENDPOINT=%s  — extra TCP bind for nfdb XPUB (\"none\"=disabled)\n", Cfg.pub_tcp_endpoint);
```

- [ ] **Step 6: Build**

```bash
cd /home/dan/Git/hack-netflex/cnc/niimx/src
nmake -f Makefile ../../../3b2/bin/niimxd
```

Expected: builds without error.

- [ ] **Step 7: Verify the config is visible**

```bash
../../../3b2/bin/niimxd
```

Run from a terminal: `main()` prints its config and exits when `isatty(STDIN_FILENO)`.

Expected: the output includes
`NIIMX.PUB_TCP_ENDPOINT=tcp://127.0.0.1:5560  — extra TCP bind for nfdb XPUB ("none"=disabled)`.

- [ ] **Step 8: Commit**

```bash
cd /home/dan/Git/hack-netflex
git add cnc/niimx/src/niimxd.cpp
git commit -m "feat(niimx): bind nfdb XPUB to loopback TCP as well as ipc

A ZMQ socket may bind multiple endpoints, so TCP subscribers are served by the
existing XPUB, relay, and epoll registration with no broker change. Bind
failure is non-fatal so losing the convenience endpoint never costs the local
IPC bus. Loopback only: the bus carries db.* record diffs and ZMQ has no auth
unless CURVE is configured. Startup-only; rebinding a live XPUB would drop
subscribers.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

## Task 12: Full test sweep

**Files:** none modified.

- [ ] **Step 1: Run the unit tests**

```bash
cd /home/dan/Git/hack-netflex/cnc/gr/src
nmake -f grdsrc.mk ../../../3b2/bin/test_gr_zmq_pub
../../../3b2/bin/test_gr_zmq_pub
echo "exit=$?"
```

Expected: `Results: 40 passed, 0 failed`, `exit=0`.

- [ ] **Step 2: Run the integration test**

```bash
./test_gr_zmq_pub.sh
echo "exit=$?"
```

Expected: `Results: 3 passed, 0 failed`, `exit=0`.

- [ ] **Step 3: Rebuild every gr binary**

Confirms nothing else in `cnc/gr` broke.

```bash
nmake -f grdsrc.mk
```

Expected: all seven `.ALL` targets build — `grd`, `grsvc`, `gr`, `grcheck`,
`gr_restore`, `gr_transfer`, `lmap`.

- [ ] **Step 4: Run lint**

```bash
nmake -f grdsrc.mk lint
```

Expected: no new warnings attributable to `gr_zmq_pub.cpp` or the `gr_daemon2.cpp`
additions. Pre-existing warnings elsewhere are not in scope.

- [ ] **Step 5: Rebuild niimx**

```bash
cd /home/dan/Git/hack-netflex/cnc/niimx/src
nmake -f Makefile
```

Expected: `niimxd`, `niimx`, `nfdb`, and `libnfdb` all build.

- [ ] **Step 6: Commit nothing**

Verification only.

---

## Task 13: Runtime verification and documentation

**Files:**
- Modify: `cnc/gr/src/test_gr_zmq_pub.sh` (header comment only)

- [ ] **Step 1: Verify the disabled default on a live system**

On a system where `grd` runs, with `GRD_ZMQ_PUB` unset or 0:

```bash
grep -c grpub /usr/cnc/trace/grd
```

Expected: the trace contains the single `grpub: disabled` line from `grpub_init()` and
nothing further. No events, no errors.

- [ ] **Step 2: Verify publishing against the live niimxd bus**

With niimxd running and `GRD_ZMQ_PUB=1` set for `grd`:

```bash
/usr/cnc/bin/nfdb -e ipc:///usr/cnc/data/nfdb_pub.sock -S app.gr.
```

Expected: within one heartbeat interval (default 30 s), an `app.gr.heartbeat` event
appears with a populated envelope — `host`, `role`, `pid`, `seq`, `ts` — and `seq`
increments on each subsequent event.

- [ ] **Step 3: Verify the TCP endpoint serves subscribers**

```bash
/usr/cnc/bin/nfdb -e tcp://127.0.0.1:5560 -S app.gr.
```

Expected: the same events as Step 2. This is what proves the second bind works.

- [ ] **Step 4: Verify a transition publishes once, not per loop pass**

Break the mate connection (pull its network, or stop sshd on it) and watch the
subscriber from Step 2.

Expected: exactly **one** `app.gr.mate.state` event with `state: "down"` at the
transition, then only heartbeats — not one `mate.state` per loop pass. Restore the
connection: exactly one `state: "up"` event. This is the behavior
`grpub_state_delta()` exists to produce, and the main thing worth confirming by hand.

- [ ] **Step 5: Verify niimxd absence is harmless**

Stop niimxd while `grd` runs with `GRD_ZMQ_PUB=1`.

Expected: `grd` continues normally. Its trace shows no new errors beyond at most
`TRACE(D3)` send failures, and failover behavior is unchanged. Restart niimxd:
`grd` resumes publishing with no intervention, because ZMQ reconnects on its own.

- [ ] **Step 6: Note the stale niimx test script**

Add to the header comment of `cnc/gr/src/test_gr_zmq_pub.sh`:

```bash
# Note: cnc/niimx/src/test_nfdb.sh, the closest existing precedent, invokes
# "$BINDIR/nfd" -- a binary cnc/niimx/src/Makefile no longer builds (nfdb was
# merged into niimxd). That script cannot run as written. Not fixed here; it is
# unrelated to this change. Mentioned so the next reader does not assume it is
# a working model to copy.
```

- [ ] **Step 7: Commit**

```bash
cd /home/dan/Git/hack-netflex
git add cnc/gr/src/test_gr_zmq_pub.sh
git commit -m "docs(gr): note that niimx test_nfdb.sh references a removed binary

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

## Definition of Done

- [ ] `nmake -f grdsrc.mk` builds all seven gr targets.
- [ ] `nmake -f Makefile` builds all four niimx targets.
- [ ] `test_gr_zmq_pub` exits 0 with 40 passing assertions.
- [ ] `test_gr_zmq_pub.sh` exits 0 with 3 passing assertions.
- [ ] With `GRD_ZMQ_PUB` unset, `grd` behavior and trace output are unchanged apart from
      one `grpub: disabled` line.
- [ ] With `GRD_ZMQ_PUB=1`, all 8 `app.gr.*` topics are reachable from both
      `ipc:///usr/cnc/data/nfdb_pub.sock` and `tcp://127.0.0.1:5560`.
- [ ] A mate up/down transition publishes exactly one `app.gr.mate.state` event per
      edge.
- [ ] Stopping niimxd does not affect GR failover.
- [ ] No SSH call was removed and no existing GR control flow was changed.
