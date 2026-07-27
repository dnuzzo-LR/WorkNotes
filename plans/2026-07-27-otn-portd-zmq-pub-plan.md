# otn_portd ZMQ pub Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add opt-in ZMQ publisher in `otn_portd` that forwards every postgres `NOTIFY` (channel + payload) to niimxd's XSUB endpoint, toggled by `system_defines` key `OTN_PORTD_ZMQ_PUB`. The existing RdbSock notify fanout is preserved unchanged.

**Architecture:** New self-contained module `cnc/otn_port/src/otn_port_zmq_pub.c` (+ header) exposes `_init/_close/_notify`. Init reads the sysdef, opens `ZMQ_PUB` connected to `ipc:///usr/cnc/data/nfdb_sub.sock`, sleeps 1s (PUB slow-joiner). Notify publishes a two-frame message: frame 0 = topic `db.otnport.<channel>`, frame 1 = flow-YAML body `{channel: <relname>, extra: '<escaped>'}`. `otn_portd.c` calls init once in `main()` and calls notify() inside `pg_listen()` after the existing RdbSock fanout.

**Tech Stack:** C, Lucent nmake, `libzmq`, project TRACE facility, `get_sysdef_int`. Build: `nmake` from `cnc/otn_port/src/`. Runtime deps: `niimxd` for XSUB endpoint.

**Spec:** `~/WorkNotes/specs/2026-07-27-otn-portd-zmq-pub-design.md`.

**Branch:** `otn-portd-zmq` (already current).

**Test strategy note:** This project has no unit-test harness for otn_portd. "Test" per task = clean build + manual bench verification with expected trace/wire output. Each task ends with a commit so any regression is easy to revert.

---

## File Structure

**Create:**
- `cnc/otn_port/include/otn_port_zmq_pub.h` — public API (3 functions).
- `cnc/otn_port/src/otn_port_zmq_pub.c` — implementation.

**Modify:**
- `cnc/otn_port/src/otn_portd.c` — call `_init()` in `main()`; call `_notify()` inside `pg_listen()`.
- `cnc/otn_port/src/otn_port.mk` — add object to `otn_portd` link line, add `-lzmq`.

**Do not touch:**
- `cnc/otn_port/src/otn_port_listen.c` (unrelated listener binary).
- Any other `otn_port_*` translation unit (they belong to `otn_port` and `otn_port_l2` binaries, not `otn_portd`).
- `librdb_notify` or `niimxd`.

---

## Environment Preflight

Before starting Task 1, verify build env in the shell that will run `nmake`:

```
git rev-parse --show-toplevel     # must equal $BASE
echo $BASE                        # must equal repo root
echo $VPATH | cut -d: -f1         # must equal $BASE
echo $L64_SETUP                   # must be 1 (64-bit build) — per project memory
```

If any check fails, stop and fix before proceeding. This is a hard requirement from `~/.claude/CLAUDE.md`.

---

## Task 1: Create module skeleton (disabled behavior only) + wire into otn_portd

Goal: introduce the new .c/.h with no-op bodies, wire them into `main()` and `pg_listen()`, add to Makefile, confirm otn_portd builds and runs exactly as before. This isolates the "does it still work" check from any ZMQ behavior.

**Files:**
- Create: `cnc/otn_port/include/otn_port_zmq_pub.h`
- Create: `cnc/otn_port/src/otn_port_zmq_pub.c`
- Modify: `cnc/otn_port/src/otn_portd.c`
- Modify: `cnc/otn_port/src/otn_port.mk`

- [ ] **Step 1: Create the header**

Create `cnc/otn_port/include/otn_port_zmq_pub.h`:

```c
/**
 * @file otn_port_zmq_pub.h
 * @brief Opt-in ZMQ publisher for postgres NOTIFY forwarding.
 *
 * When enabled via the OTN_PORTD_ZMQ_PUB system_define, otn_portd
 * publishes every postgres NOTIFY it receives to niimxd's XSUB
 * endpoint (ipc:///usr/cnc/data/nfdb_sub.sock). Subscribers on the
 * XPUB side (nfdb_pub.sock) receive a two-frame message:
 *   frame 0: db.otnport.<channel>
 *   frame 1: {channel: <relname>, extra: '<escaped>'}
 */
#ifndef OTN_PORT_ZMQ_PUB_H
#define OTN_PORT_ZMQ_PUB_H

#ifdef __cplusplus
extern "C" {
#endif

/**
 * @brief Initialize the ZMQ publisher.
 *
 * Reads system_define OTN_PORTD_ZMQ_PUB. If 0 or absent, the module
 * stays disabled and otn_port_zmq_pub_notify() becomes a cheap no-op.
 * If enabled, creates a ZMQ context, opens a ZMQ_PUB socket, connects
 * to niimxd's XSUB endpoint, and sleeps 1s to cover the PUB
 * slow-joiner problem.
 *
 * @return 0 on success or when disabled; -1 on hard ZMQ failure
 *         (module left disabled).
 */
int otn_port_zmq_pub_init(void);

/**
 * @brief Tear down the publisher. Idempotent.
 */
void otn_port_zmq_pub_close(void);

/**
 * @brief Publish one postgres NOTIFY to the niimxd bus.
 *
 * No-op when the module is disabled. Best-effort: failures are logged
 * at TRACE(D3) and do NOT propagate.
 *
 * @param channel  postgres relname (topic tail). Must not be NULL.
 * @param extra    postgres NOTIFY payload; may be NULL or empty.
 * @return 0 on send (or disabled); -1 on transient send failure.
 */
int otn_port_zmq_pub_notify(const char *channel, const char *extra);

#ifdef __cplusplus
}
#endif

#endif /* OTN_PORT_ZMQ_PUB_H */
```

- [ ] **Step 2: Create the stub implementation**

Create `cnc/otn_port/src/otn_port_zmq_pub.c`:

```c
/**
 * @file otn_port_zmq_pub.c
 * @brief Stub for Task 1. Real ZMQ init/publish lands in later tasks.
 *
 * Task 1 keeps this file a pure no-op so we can confirm otn_portd
 * still builds and runs with the module wired in.
 */
#include <stdio.h>
#include <otn_port_zmq_pub.h>

int otn_port_zmq_pub_init(void)
{
    return 0;
}

void otn_port_zmq_pub_close(void)
{
}

int otn_port_zmq_pub_notify(const char *channel, const char *extra)
{
    (void)channel;
    (void)extra;
    return 0;
}
```

- [ ] **Step 3: Wire into otn_portd.c main()**

Modify `cnc/otn_port/src/otn_portd.c`.

Add near the other project headers (after line 40 `#include <rdb_notify.h>`):

```c
#include <otn_port_zmq_pub.h>
```

Then in `main()` right after `mm_cmm();` (currently line 92) add:

```c
    otn_port_zmq_pub_init();   /* opt-in ZMQ pub; safe when disabled */
```

- [ ] **Step 4: Wire into pg_listen()**

Modify `cnc/otn_port/src/otn_portd.c`, inside `pg_listen()`. The current inner block (line 351 → 397) is:

```c
while ((notify = PQnotifies(con)) != NULL)
{
    RdbNotify rdbNotify;
    memset((void *) &rdbNotify, 0, sizeof(rdbNotify));
    strncpy(rdbNotify.extra, notify->extra, RDB_NOTIFY_EXTRA_STR_LEN);
    rdbNotify.type = rdb_notify_channel_to_type(notify->relname);
    ...  /* existing RdbSock fanout */
    PQfreemem(notify);
}
```

Add the ZMQ publish call immediately **before** `PQfreemem(notify);`. The final tail of the loop should look like:

```c
        }

        otn_port_zmq_pub_notify(notify->relname, notify->extra);

        PQfreemem(notify);
    }
```

Placement rationale: after the existing RdbSock fanout (so RdbSock is never delayed by ZMQ), before `PQfreemem` (while `notify->relname/extra` are still valid).

- [ ] **Step 5: Add to Makefile**

Modify `cnc/otn_port/src/otn_port.mk`.

Change the `otn_portd` target line (currently line 159):

```
$(PBIN)/otn_portd:: otn_portd.o repopulate.o $(CORELIBS) $(UTILLIB) $(LIBCNCDB) $(OTN_PORT_LIBS)
	$(CC) $(CFLAGS) $(LDFLAGS) -o $(<) $(*)
```

to:

```
$(PBIN)/otn_portd:: otn_portd.o repopulate.o otn_port_zmq_pub.o $(CORELIBS) $(UTILLIB) $(LIBCNCDB) $(OTN_PORT_LIBS)
	$(CC) $(CFLAGS) $(LDFLAGS) -o $(<) $(*)
```

Do NOT add `-lzmq` yet — the stub has no ZMQ calls, so the link would succeed without it. Adding the flag before we need it hides which change actually pulls in libzmq.

- [ ] **Step 6: Build**

From `cnc/otn_port/src/`:

```
nmake
```

Expected: clean build, `otn_portd` binary produced under `$PBIN`. If `nmake` fails on missing `globaldefs.nmk` / `global_*.nmk` / project headers, STOP and tell the user (project memory `feedback_vpath_halt`) — do not attempt path reconstruction.

- [ ] **Step 7: Smoke-run otn_portd (optional but recommended)**

If a bench with the full stack is available: install the new binary, start otn_portd, tail `/usr/cnc/trace/otn_portd`, trigger a postgres NOTIFY on a listened channel, and confirm the existing RdbSock consumers still receive it. The stub `_notify()` is a no-op so behavior should be identical to before this task.

If no bench is available, skip and rely on Task 3's end-to-end check to catch regressions.

- [ ] **Step 8: Commit**

```
git add cnc/otn_port/include/otn_port_zmq_pub.h \
        cnc/otn_port/src/otn_port_zmq_pub.c \
        cnc/otn_port/src/otn_portd.c \
        cnc/otn_port/src/otn_port.mk
git commit -m "otn_portd: add zmq pub module skeleton, wire into pg_listen"
```

---

## Task 2: Implement init() (sysdef read + ZMQ socket)

Goal: turn the stub `_init()` into the real thing — read `OTN_PORTD_ZMQ_PUB`, open ctx+socket, connect, sleep, log. `_notify()` and `_close()` stay stubs for now.

**Files:**
- Modify: `cnc/otn_port/src/otn_port_zmq_pub.c`
- Modify: `cnc/otn_port/src/otn_port.mk`

- [ ] **Step 1: Implement init() and close()**

Replace the contents of `cnc/otn_port/src/otn_port_zmq_pub.c` with:

```c
/**
 * @file otn_port_zmq_pub.c
 * @brief Opt-in ZMQ publisher for postgres NOTIFY forwarding.
 *
 * See otn_port_zmq_pub.h for the wire format. Enabled by the
 * OTN_PORTD_ZMQ_PUB system_define (int, nonzero = enabled).
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <errno.h>
#include <zmq.h>
#include <trace.h>            /* project TRACE macro + Trace/DebugLvl */
#include <otn_port_zmq_pub.h>

/**
 * @brief niimxd XSUB endpoint. Publishers connect here; niimxd
 * forwards to the XPUB side (nfdb_pub.sock) that subscribers use.
 */
#define OTN_PORTD_ZMQ_XSUB_ENDPOINT "ipc:///usr/cnc/data/nfdb_sub.sock"

/**
 * @brief system_defines toggle. Nonzero = enabled.
 */
#define OTN_PORTD_ZMQ_SYSDEF "OTN_PORTD_ZMQ_PUB="

/** @brief Module-local ZMQ context. NULL when disabled. */
static void *g_ctx  = NULL;
/** @brief Module-local ZMQ_PUB socket. NULL when disabled. */
static void *g_sock = NULL;

int otn_port_zmq_pub_init(void)
{
    int enabled = 0;

    /* External API from cnc/utility/src/system_defines64.c */
    extern int get_sysdef_int(const char *string, int *value, int defval);

    get_sysdef_int(OTN_PORTD_ZMQ_SYSDEF, &enabled, 0);
    if (!enabled) {
        TRACE(D1, "otn_port_zmq_pub: disabled (%s not set)\n",
              OTN_PORTD_ZMQ_SYSDEF);
        return 0;
    }

    g_ctx = zmq_ctx_new();
    if (!g_ctx) {
        TRACE(D0, "otn_port_zmq_pub: zmq_ctx_new failed: %s\n",
              strerror(errno));
        return -1;
    }

    g_sock = zmq_socket(g_ctx, ZMQ_PUB);
    if (!g_sock) {
        TRACE(D0, "otn_port_zmq_pub: zmq_socket(PUB) failed: %s\n",
              zmq_strerror(errno));
        zmq_ctx_destroy(g_ctx);
        g_ctx = NULL;
        return -1;
    }

    if (zmq_connect(g_sock, OTN_PORTD_ZMQ_XSUB_ENDPOINT) != 0) {
        TRACE(D0, "otn_port_zmq_pub: zmq_connect(%s) failed: %s\n",
              OTN_PORTD_ZMQ_XSUB_ENDPOINT, zmq_strerror(errno));
        zmq_close(g_sock);
        zmq_ctx_destroy(g_ctx);
        g_sock = NULL;
        g_ctx  = NULL;
        return -1;
    }

    /* PUB slow-joiner: give subscribers time to complete subscription.
     * Same 1s pattern used in cnc/md/src/nfsend.cpp:164. */
    sleep(1);

    TRACE(D1, "otn_port_zmq_pub: enabled, connected to %s\n",
          OTN_PORTD_ZMQ_XSUB_ENDPOINT);
    return 0;
}

void otn_port_zmq_pub_close(void)
{
    if (g_sock) { zmq_close(g_sock); g_sock = NULL; }
    if (g_ctx)  { zmq_ctx_destroy(g_ctx); g_ctx = NULL; }
}

int otn_port_zmq_pub_notify(const char *channel, const char *extra)
{
    (void)channel;
    (void)extra;
    /* Real body lands in Task 3. */
    return 0;
}
```

If `<trace.h>` is not the correct project header for the `TRACE` macro in this daemon, use whatever `otn_portd.c` uses. Confirm by grepping: `grep -l "define TRACE" /home/dan/Git/netflex/include/`. The macro is available anywhere `otn_portd.c` compiles.

- [ ] **Step 2: Add -lzmq to the link line**

Modify `cnc/otn_port/src/otn_port.mk`. The LINUX branch of `OTN_PORT_LIBS` (line 68) currently reads:

```
	OTN_PORT_LIBS = -lnelib -ltl1 -lsnmpne -latm -lcncudb -lm -lcnc -lretro -linc -lring -lsodium -lpthread -lpq -lrdb -lpcre -lwvlsvc -lotnport
```

Change to:

```
	OTN_PORT_LIBS = -lnelib -ltl1 -lsnmpne -latm -lcncudb -lm -lcnc -lretro -linc -lring -lsodium -lpthread -lpq -lrdb -lpcre -lwvlsvc -lotnport -lzmq
```

Do NOT modify the non-LINUX branch (line 70). Per project memory `feedback_linux_only`, non-LINUX branches are dead code and Solaris/HP configs will never link libzmq.

- [ ] **Step 3: Build**

From `cnc/otn_port/src/`:

```
nmake
```

Expected: clean build. If the link fails with unresolved `zmq_*` symbols, `-lzmq` wasn't picked up — recheck Step 2.

- [ ] **Step 4: Verify disabled path (sysdef absent)**

Install the built `otn_portd`. Ensure `OTN_PORTD_ZMQ_PUB` is NOT set in the system_defines file. Start otn_portd. In `/usr/cnc/trace/otn_portd` expect this line (verbosity D1 or higher):

```
otn_port_zmq_pub: disabled (OTN_PORTD_ZMQ_PUB= not set)
```

Also verify no unix socket file descriptor to `nfdb_sub.sock` is open by otn_portd:

```
ls -l /proc/$(pidof otn_portd)/fd/ | grep nfdb_sub
```

Expected: no output.

- [ ] **Step 5: Verify enabled path (sysdef=1)**

Add to system_defines: `OTN_PORTD_ZMQ_PUB=1`. Ensure `niimxd` is running (its XSUB endpoint must exist). Restart otn_portd. Expect trace:

```
otn_port_zmq_pub: enabled, connected to ipc:///usr/cnc/data/nfdb_sub.sock
```

Check the socket is open:

```
ls -l /proc/$(pidof otn_portd)/fd/ | grep -E "socket|nfdb_sub"
```

Expected: at least one unix domain socket owned by otn_portd's ZMQ side.

If `niimxd` is not running, expect the failure branch:

```
otn_port_zmq_pub: zmq_connect(...) failed: ...
```

and otn_portd stays alive (Task 1 wired init to be non-fatal by discarding its return code).

- [ ] **Step 6: Commit**

```
git add cnc/otn_port/src/otn_port_zmq_pub.c cnc/otn_port/src/otn_port.mk
git commit -m "otn_portd: implement zmq pub init/close with OTN_PORTD_ZMQ_PUB sysdef"
```

---

## Task 3: Implement notify() (frame build + escape + publish)

Goal: real publish. Two-frame ZMQ message per NOTIFY. YAML-escape single quotes and CR/LF in the `extra` payload. End-to-end subscribe check confirms wire format.

**Files:**
- Modify: `cnc/otn_port/src/otn_port_zmq_pub.c`

- [ ] **Step 1: Replace the stub notify() with the real implementation**

In `cnc/otn_port/src/otn_port_zmq_pub.c`, replace the current stub:

```c
int otn_port_zmq_pub_notify(const char *channel, const char *extra)
{
    (void)channel;
    (void)extra;
    /* Real body lands in Task 3. */
    return 0;
}
```

with:

```c
/**
 * @brief YAML-escape a payload string in-place into caller buffer.
 *
 * Doubles single quotes ('' escape), and replaces CR/LF with a space
 * so the body stays on one line (flow-YAML requirement).
 *
 * @param src  input string (may be NULL, treated as "").
 * @param dst  output buffer (must be non-NULL).
 * @param cap  output buffer capacity in bytes (must be >= 1).
 * @return length of the escaped string (excluding NUL), truncated to
 *         cap-1 if it did not fit.
 */
static size_t yaml_escape_extra(const char *src, char *dst, size_t cap)
{
    size_t o = 0;
    const char *p = src ? src : "";
    while (*p && o + 1 < cap) {
        char c = *p++;
        if (c == '\'') {
            if (o + 2 >= cap) break;
            dst[o++] = '\'';
            dst[o++] = '\'';
        } else if (c == '\n' || c == '\r') {
            dst[o++] = ' ';
        } else {
            dst[o++] = c;
        }
    }
    dst[o] = '\0';
    return o;
}

int otn_port_zmq_pub_notify(const char *channel, const char *extra)
{
    if (!g_sock || !channel) return 0;

    char topic[256];
    char body[4096];
    char escaped[3072];

    yaml_escape_extra(extra, escaped, sizeof(escaped));

    int tlen = snprintf(topic, sizeof(topic), "db.otnport.%s", channel);
    if (tlen < 0 || (size_t)tlen >= sizeof(topic)) {
        TRACE(D3, "otn_port_zmq_pub: topic overflow, channel=%s\n", channel);
        return -1;
    }

    int blen = snprintf(body, sizeof(body),
                        "{channel: %s, extra: '%s'}",
                        channel, escaped);
    if (blen < 0 || (size_t)blen >= sizeof(body)) {
        TRACE(D3, "otn_port_zmq_pub: body overflow, channel=%s\n", channel);
        return -1;
    }

    /* Frame 0: topic (SNDMORE) */
    if (zmq_send(g_sock, topic, (size_t)tlen, ZMQ_SNDMORE) < 0) {
        TRACE(D3, "otn_port_zmq_pub: send(topic) failed: %s\n",
              zmq_strerror(errno));
        return -1;
    }
    /* Frame 1: body */
    if (zmq_send(g_sock, body, (size_t)blen, 0) < 0) {
        TRACE(D3, "otn_port_zmq_pub: send(body) failed: %s\n",
              zmq_strerror(errno));
        return -1;
    }

    TRACE(D5, "otn_port_zmq_pub: PUB %s | %s\n", topic, body);
    return 0;
}
```

- [ ] **Step 2: Build**

From `cnc/otn_port/src/`:

```
nmake
```

Expected: clean build.

- [ ] **Step 3: End-to-end verify (sysdef enabled)**

Preconditions: `OTN_PORTD_ZMQ_PUB=1`, niimxd running, new otn_portd installed and started, at least one RdbSock client subscribed via the existing path (so `pg_listen` reaches the notify block; without any subscribers the notify path still runs — subscribers only gate whether RdbSock is fanned out).

Terminal A — subscribe to the bus:

```
nfdb -S db.otnport.
```

Terminal B — trigger a NOTIFY through postgres. Pick any channel the daemon LISTENs on (see `RdbNotifyTypeToChannelTable`). Example using `psql` against the otn_port database:

```
psql -h $RDB_DIRECT_HOST -p $RDB_DIRECT_PORT -U $RDB_USERNAME -d $RDB_OTNPORT_DB \
     -c "NOTIFY otn_port_alarm_change, '17,test-payload';"
```

(Channel name is illustrative — replace with a real one from `RdbNotifyTypeToChannelTable`.)

Expected in Terminal A:

```
db.otnport.otn_port_alarm_change
{channel: otn_port_alarm_change, extra: '17,test-payload'}
```

Expected in `/usr/cnc/trace/otn_portd` at D5:

```
otn_port_zmq_pub: PUB db.otnport.otn_port_alarm_change | {channel: otn_port_alarm_change, extra: '17,test-payload'}
```

Also verify the existing RdbSock consumers still received the notify (their behavior must be unchanged).

- [ ] **Step 4: Escape verification**

Trigger a NOTIFY whose payload contains a single quote and a newline:

```
psql ... -c $'NOTIFY otn_port_alarm_change, \'a\\\'b\nc\';'
```

Expected wire body:

```
{channel: otn_port_alarm_change, extra: 'a''b c'}
```

(Two adjacent single quotes for the escaped quote; the LF collapsed to a space.)

- [ ] **Step 5: Disabled path regression**

Set `OTN_PORTD_ZMQ_PUB=0` (or remove the line), restart otn_portd. Trigger a NOTIFY. Confirm:
- Terminal A subscriber receives nothing (good).
- Trace still logs the existing D5 line `%s: NOTIFY %s, ...`.
- RdbSock consumers still receive the notify.

- [ ] **Step 6: Commit**

```
git add cnc/otn_port/src/otn_port_zmq_pub.c
git commit -m "otn_portd: publish two-frame NOTIFY events to niimxd XSUB"
```

---

## Task 4: Shutdown hook

Goal: call `otn_port_zmq_pub_close()` on clean shutdown so the socket is torn down before the process exits.

`otn_portd.c::sighandler()` is called on SIGINT/SIGHUP/SIGTERM. Add the close call there.

**Files:**
- Modify: `cnc/otn_port/src/otn_portd.c`

- [ ] **Step 1: Locate sighandler**

Find `void sighandler(int signum)` in `cnc/otn_port/src/otn_portd.c` (declared line 83; definition is elsewhere in the same file — locate it).

- [ ] **Step 2: Add the close call**

At the top of the sighandler body, before whatever cleanup it currently does, add:

```c
    otn_port_zmq_pub_close();
```

If sighandler currently `exit()`s without any prior cleanup, this line is enough. If it does other cleanup already, add the close call as the first cleanup step so ZMQ is torn down before postgres/rdb connections are dropped (ordering matches init order: last-in-first-out).

- [ ] **Step 3: Build**

From `cnc/otn_port/src/`:

```
nmake
```

Expected: clean build.

- [ ] **Step 4: Verify graceful shutdown**

With `OTN_PORTD_ZMQ_PUB=1` and niimxd running, start otn_portd, then send SIGTERM:

```
kill $(pidof otn_portd)
```

Confirm `/usr/cnc/trace/otn_portd` shows the sighandler ran (existing behavior) and there is no ZMQ-related error message. `ls -l /proc/<pid>/fd/` should not be inspectable (process is gone). Restart otn_portd — the reconnect to `nfdb_sub.sock` should succeed.

- [ ] **Step 5: Commit**

```
git add cnc/otn_port/src/otn_portd.c
git commit -m "otn_portd: close zmq pub socket on shutdown"
```

---

## Self-Review Checklist

Before handing off to the executor:

- [ ] Every spec section has at least one task covering it:
  - Architecture / new module → Task 1, Task 2, Task 3.
  - Integration points in otn_portd.c → Task 1 (init + notify call), Task 4 (close).
  - Wire format (frames, topic, body) → Task 3.
  - Toggle (sysdef read once at init) → Task 2.
  - Failure handling (init disables gracefully, notify best-effort) → Task 2 (init), Task 3 (notify).
  - Build (add object, `-lzmq`, LINUX-only) → Task 1 (object), Task 2 (link flag).
  - Testing (manual bench matrix) → Task 2 Steps 4-5, Task 3 Steps 3-5, Task 4 Step 4.
- [ ] No placeholders (`TODO`, `TBD`, "similar to Task N without code") — grep this document to confirm.
- [ ] Function signatures consistent across tasks: `otn_port_zmq_pub_init` / `_close` / `_notify` used identically in header, .c, and callers.
- [ ] Endpoint constant `ipc:///usr/cnc/data/nfdb_sub.sock` matches spec + `ENDPOINT_SUB_DEFAULT` in `nfdb_proto.h`.
- [ ] Sysdef key `OTN_PORTD_ZMQ_PUB` matches spec exactly (with trailing `=` in the lookup call).
- [ ] Topic format `db.otnport.<channel>` matches spec.
- [ ] Body format `{channel: <relname>, extra: '<escaped>'}` matches spec.
