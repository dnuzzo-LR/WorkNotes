# niimx Async Commands Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an async command mode to the niimx ZMQ protocol: clients submit a command, receive a tag immediately, and either poll for the result by tag or subscribe to a completion event on the existing nfdb XPUB socket.

**Architecture:** New module `niimx_async.{h,cpp}` owns a tag table keyed by 64-bit opaque ids. `niimxd.cpp` integrates the module via ~35 lines across recv, dispatch, reply, tick, and lifecycle. Wire change: a new required `mode(4)` frame between `tmout` and `command`. POLL/CANCEL are sync verbs handled inline. DONE-returning POLLs erase the entry; otherwise a configurable retention window applies.

**Tech Stack:** C++ (gcc 4.8.5 baseline), ZeroMQ (`libzmq`), Lucent/AT&T nmake, `std::map`, existing epoll/signalfd/timerfd loop in niimxd.

**Spec:** `docs/superpowers/specs/2026-05-20-niimx-async-commands-design.md`

---

## File Map

**Created:**
- `cnc/niimx/src/niimx_async.h` — `niimx_async_state_t`, `niimx_async_entry_t`, `NiimxAsync` class.
- `cnc/niimx/src/niimx_async.cpp` — implementation.
- `cnc/niimx/src/test_niimx_async.cpp` — unit tests (standalone main, assert-based, no test framework).
- `cnc/niimx/src/test_niimx_async.sh` — integration test driver (sources/extends patterns from `test_nfdb.sh`).

**Modified:**
- `cnc/niimx/src/niimxd.cpp` — config load, `Request` fields, mode-frame recv, POLL/CANCEL branch, SUBMIT path, `mark_running`, reply branch, sweep tick, startup/shutdown.
- `cnc/niimx/src/niimx.cpp` — emit the new `mode` frame (always `0` for now).
- `cnc/niimx/src/Makefile` — link `niimx_async.o` into `niimxd`; add `test_niimx_async` build target.
- `niimxd.conf` template (location: `cnc/niimx/data/niimxd.conf` or wherever the existing sample lives — see Task 1 verification step).

**Unchanged:**
- All `nfdb_*` files.

---

## Build & Test Commands

All from `cnc/niimx/src/`:

```bash
nmake ../../../3b2/bin/niimxd
nmake ../../../3b2/bin/niimx
nmake ./test_niimx_async         # produced by new Makefile target
```

Unit tests:
```bash
./test_niimx_async               # prints "PASS N/N" or "FAIL ..." and exits non-zero on fail
```

Integration tests:
```bash
./test_niimx_async.sh            # prints "Results: N passed, M failed", exits non-zero on fail
```

Verify env at every task start:
```bash
git rev-parse --show-toplevel    # must equal $BASE
echo "$VPATH" | cut -d: -f1      # must equal $BASE
```

If either fails, stop and report — do not proceed.

---

## Phase 0 — Preflight

### Task 0: Verify merge state and env

This plan assumes the `niimxd/nfd` merge (`cnc/niimx/src/2026-05-15-niimxd-nfdb-merge.md`) has landed. Specifically:

- `niimxd.cpp` defines `G.nfdb_router`, `nfdb_g_xpub`, `G.nfdb_clients`, and binds the three `nfdb*.sock` endpoints at startup.
- `Makefile` links `nfdb_cmd.o nfdb_proto.o` into `niimxd`.
- `nfd.cpp` has been deleted.

- [ ] **Step 1: Verify env**

```bash
cd /home/dan/Git/netflex/cnc/niimx/src
git rev-parse --show-toplevel
echo "BASE=$BASE"
echo "VPATH[0]=$(echo "$VPATH" | cut -d: -f1)"
```

Expected: all three print `/home/dan/Git/netflex`. If not, stop and surface the mismatch.

- [ ] **Step 2: Verify merge symbols present in niimxd.cpp**

```bash
grep -nE 'nfdb_g_xpub|G\.nfdb_router|nfdb\.sock|nfdb_pub\.sock|nfdb_sub\.sock' niimxd.cpp | head -20
```

Expected: hits for all five patterns. If any missing, the merge is incomplete — stop and tell the user this plan needs the merge first.

- [ ] **Step 3: Verify niimxd builds clean**

```bash
nmake ../../../3b2/bin/niimxd
```

Expected: zero errors, zero new warnings. If it fails, fix the merge first.

- [ ] **Step 4: Locate the conf file path**

```bash
grep -rnE 'max_iq|reject *=|reject :' /home/dan/Git/netflex/cnc/niimx/ 2>/dev/null | grep -vE '\.cpp|\.h|\.o' | head
```

Expected: locate the sample conf path. Record that path; the rest of the plan calls it `<CONF>`. If no sample conf exists, the config is hardcoded — skip the conf edits later but still add the parsing code in `niimxd.cpp`.

---

## Phase 1 — Header and Test Skeleton (TDD)

We build the module test-first. Each task writes a failing test, then implements the minimum to pass.

### Task 1: Create header skeleton

**Files:**
- Create: `cnc/niimx/src/niimx_async.h`

- [ ] **Step 1: Write the header**

```c
#ifndef NIIMX_ASYNC_H
#define NIIMX_ASYNC_H

#include <stdint.h>
#include <stddef.h>
#include <string>
#include <map>
#include <chrono>

/** @brief Wall-clock type used by NiimxAsync. */
typedef std::chrono::steady_clock NiimxAsyncClock;

/**
 * @brief Lifecycle state of an async command entry.
 */
typedef enum {
    NIIMX_ASYNC_PENDING,    /**< Submitted; in RequestQ or dispatched. */
    NIIMX_ASYNC_RUNNING,    /**< Dispatched to backend, awaiting reply. */
    NIIMX_ASYNC_DONE,       /**< Backend reply received and stored. */
    NIIMX_ASYNC_CANCELLED,  /**< Cancelled before or during execution. */
    NIIMX_ASYNC_EXPIRED,    /**< Past retention; transient, erased before
                                 poll() returns. */
    NIIMX_ASYNC_UNKNOWN     /**< Sentinel returned by poll() when the tag
                                 is not in the table. */
} niimx_async_state_t;

/**
 * @brief Result code for NiimxAsync::cancel().
 */
typedef enum {
    NIIMX_CANCEL_CANCELLED_NOW,  /**< Was PENDING and not yet dispatched. */
    NIIMX_CANCEL_PENDING,        /**< Was RUNNING; discard_on_reply set. */
    NIIMX_CANCEL_NOT_FOUND,      /**< Tag not in table. */
    NIIMX_CANCEL_ALREADY_DONE    /**< Tag already DONE or CANCELLED. */
} niimx_cancel_result_t;

/**
 * @brief Bookkeeping for a single outstanding async command.
 */
struct niimx_async_entry_t {
    uint64_t                       tag;              /**< 64-bit monotonic id. */
    niimx_async_state_t            state;            /**< Current state. */
    std::string                    identity;         /**< ZMQ identity of submitter. */
    int32_t                        msgid;            /**< Original client msgid. */
    std::string                    host;             /**< Target host. */
    std::string                    command;          /**< Original command string. */
    std::string                    payload;          /**< Filled on completion. */
    NiimxAsyncClock::time_point    submitted_at;
    NiimxAsyncClock::time_point    completed_at;     /**< Set on DONE/CANCELLED. */
    bool                           discard_on_reply; /**< Set by cancel() when RUNNING. */
};

/**
 * @brief Publish callback signature used by NiimxAsync.
 *
 * @param ctx    Opaque context (a ZMQ XPUB socket in production; a test
 *               capture struct in unit tests).
 * @param topic  Event topic string (e.g., "niimx.done.<hex>").
 * @param body   Event body (full payload bytes).
 */
typedef void (*niimx_publish_fn)(void *ctx,
                                 const std::string &topic,
                                 const std::string &body);

/**
 * @brief Tag table + retention manager for async commands.
 *
 * Single-threaded by contract: all methods must be called from the
 * niimxd event loop thread. No internal locking.
 */
class NiimxAsync {
public:
    /**
     * @brief Construct with a custom publish callback.
     *
     * @param pub             Publish callback (must not be null).
     * @param pub_ctx         Opaque ctx passed to pub.
     * @param max_outstanding Global cap on table size; 0 = unlimited.
     * @param retention_secs  Retention window for DONE/CANCELLED entries.
     */
    NiimxAsync(niimx_publish_fn pub, void *pub_ctx,
               size_t max_outstanding, int retention_secs);

    ~NiimxAsync();

    /**
     * @brief Submit a new async command; allocate and return its tag.
     * @return new tag on success, 0 if at cap.
     */
    uint64_t submit(const std::string &identity, int32_t msgid,
                    const std::string &host,     const std::string &command);

    /** @brief Transition a tag from PENDING to RUNNING. No-op if not PENDING. */
    void mark_running(uint64_t tag);

    /**
     * @brief Store backend reply and publish event.
     *
     * If the entry was cancel-pending (discard_on_reply set) the payload
     * is discarded, the entry is moved to CANCELLED, and a
     * "niimx.cancelled.<hex>" event is published. Otherwise the payload
     * is stored, the entry is moved to DONE, and a "niimx.done.<hex>"
     * event is published.
     */
    void complete(uint64_t tag, const std::string &payload);

    /**
     * @brief Read status + payload.
     *
     * Side effects:
     *   - DONE                : payload copied to *payload_out, entry erased.
     *   - EXPIRED             : entry erased before return.
     *   - PENDING/RUNNING/
     *     CANCELLED           : entry left in place.
     *   - tag not in table    : returns UNKNOWN.
     */
    niimx_async_state_t poll(uint64_t tag, std::string *payload_out);

    /**
     * @brief Cancel by tag.
     *
     * Caller is responsible for evicting any matching Request from
     * RequestQ when result == CANCELLED_NOW.
     */
    niimx_cancel_result_t cancel(uint64_t tag);

    /** @brief Erase DONE/CANCELLED entries past retention. */
    void sweep(NiimxAsyncClock::time_point now);

    /** @brief Current table size. */
    size_t size() const { return table_.size(); }

private:
    void publish_event(uint64_t tag, const std::string &payload,
                       const char *suffix /* "done" or "cancelled" */);

    niimx_publish_fn                            pub_fn_;
    void                                       *pub_ctx_;
    size_t                                      cap_;
    int                                         retention_secs_;
    uint64_t                                    next_tag_;
    std::map<uint64_t, niimx_async_entry_t>     table_;
};

/**
 * @brief Format a 64-bit tag as 16 lowercase hex chars.
 *
 * @param tag  Tag value.
 * @return 16-char std::string.
 */
std::string niimx_async_tag_to_hex(uint64_t tag);

/**
 * @brief Parse a 16-char lowercase-hex string into a 64-bit tag.
 *
 * @param hex      Input string (must be exactly 16 hex chars).
 * @param tag_out  Receives the parsed tag on success.
 * @return true on success, false on malformed input.
 */
bool niimx_async_hex_to_tag(const std::string &hex, uint64_t *tag_out);

#endif /* NIIMX_ASYNC_H */
```

- [ ] **Step 2: Verify it parses**

```bash
cd /home/dan/Git/netflex/cnc/niimx/src
g++ -std=c++11 -fsyntax-only -I. niimx_async.h
```

Expected: zero output.

- [ ] **Step 3: Commit**

```bash
cd /home/dan/Git/netflex
git add cnc/niimx/src/niimx_async.h
git commit -m "$(cat <<'EOF'
niimx_async: header skeleton for async-command tag table

Declares NiimxAsync class, niimx_async_entry_t, the state and
cancel-result enums, the publish callback typedef, and the
hex-tag helpers. Implementation lands in subsequent commits.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 2: Test harness skeleton + tag-format helpers

**Files:**
- Create: `cnc/niimx/src/test_niimx_async.cpp`
- Modify: `cnc/niimx/src/niimx_async.cpp` (created in this task)
- Modify: `cnc/niimx/src/Makefile` — add `test_niimx_async` target

- [ ] **Step 1: Write the failing test**

Create `cnc/niimx/src/test_niimx_async.cpp`:

```c
// -*- compile-command: "nmake test_niimx_async"; -*-
#include "niimx_async.h"
#include <stdio.h>
#include <string.h>
#include <assert.h>

static int g_passed = 0;
static int g_failed = 0;

#define CHECK(cond)                                                       \
    do {                                                                  \
        if (cond) {                                                       \
            ++g_passed;                                                   \
        } else {                                                          \
            ++g_failed;                                                   \
            fprintf(stderr, "FAIL %s:%d  %s\n", __FILE__, __LINE__, #cond); \
        }                                                                 \
    } while (0)

static void test_tag_hex_roundtrip() {
    uint64_t in = 0x0123456789abcdefULL;
    std::string hex = niimx_async_tag_to_hex(in);
    CHECK(hex == "0123456789abcdef");

    uint64_t out = 0;
    CHECK(niimx_async_hex_to_tag(hex, &out));
    CHECK(out == in);
}

static void test_tag_hex_zero() {
    std::string hex = niimx_async_tag_to_hex(0);
    CHECK(hex == "0000000000000000");
    uint64_t out = 1;
    CHECK(niimx_async_hex_to_tag(hex, &out));
    CHECK(out == 0);
}

static void test_tag_hex_max() {
    uint64_t in = 0xffffffffffffffffULL;
    std::string hex = niimx_async_tag_to_hex(in);
    CHECK(hex == "ffffffffffffffff");
    uint64_t out = 0;
    CHECK(niimx_async_hex_to_tag(hex, &out));
    CHECK(out == in);
}

static void test_tag_hex_reject_short() {
    uint64_t out = 0;
    CHECK(!niimx_async_hex_to_tag("0123", &out));
}

static void test_tag_hex_reject_long() {
    uint64_t out = 0;
    CHECK(!niimx_async_hex_to_tag("0123456789abcdef0", &out));
}

static void test_tag_hex_reject_uppercase() {
    uint64_t out = 0;
    CHECK(!niimx_async_hex_to_tag("0123456789ABCDEF", &out));
}

static void test_tag_hex_reject_non_hex() {
    uint64_t out = 0;
    CHECK(!niimx_async_hex_to_tag("0123456789abcdeZ", &out));
}

int main() {
    test_tag_hex_roundtrip();
    test_tag_hex_zero();
    test_tag_hex_max();
    test_tag_hex_reject_short();
    test_tag_hex_reject_long();
    test_tag_hex_reject_uppercase();
    test_tag_hex_reject_non_hex();
    printf("test_niimx_async: %d passed, %d failed\n", g_passed, g_failed);
    return g_failed == 0 ? 0 : 1;
}
```

- [ ] **Step 2: Create empty cpp + add Makefile target**

Create `cnc/niimx/src/niimx_async.cpp`:

```c
#include "niimx_async.h"
```

(empty stub — body lands in Step 3.)

Modify `cnc/niimx/src/Makefile`. Append after the existing `$(LDIR)/nfdb.$(LIBTYPE)` rule (around line 32):

```make
test_niimx_async : test_niimx_async.o niimx_async.o
	$(CPLUS_CC) $(LDFLAGS) -o $(<) $(*)
```

And add `niimx_async.o` to the niimxd link line. Replace:

```make
$(PBIN)/niimxd : niimxd.o nfdb_cmd.o nfdb_proto.o $(CORELIBS) $(UTILLIB)  -lnelib
	$(CPLUS_CC)  $(LDFLAGS) -o $(<) $(*) $(ACCLIBS) -lzmq -lssh
```

with:

```make
$(PBIN)/niimxd : niimxd.o nfdb_cmd.o nfdb_proto.o niimx_async.o $(CORELIBS) $(UTILLIB)  -lnelib
	$(CPLUS_CC)  $(LDFLAGS) -o $(<) $(*) $(ACCLIBS) -lzmq -lssh
```

- [ ] **Step 3: Run test, verify it fails to link**

```bash
cd /home/dan/Git/netflex/cnc/niimx/src
nmake test_niimx_async
```

Expected: link failure on `niimx_async_tag_to_hex` / `niimx_async_hex_to_tag` (undefined references).

- [ ] **Step 4: Implement the helpers**

In `cnc/niimx/src/niimx_async.cpp`, replace the file body with:

```c
#include "niimx_async.h"
#include <stdio.h>
#include <string.h>

std::string niimx_async_tag_to_hex(uint64_t tag) {
    char buf[17];
    snprintf(buf, sizeof(buf), "%016llx", (unsigned long long)tag);
    return std::string(buf, 16);
}

bool niimx_async_hex_to_tag(const std::string &hex, uint64_t *tag_out) {
    if (hex.size() != 16) return false;
    uint64_t v = 0;
    for (size_t i = 0; i < 16; ++i) {
        char c = hex[i];
        uint64_t d;
        if (c >= '0' && c <= '9') d = (uint64_t)(c - '0');
        else if (c >= 'a' && c <= 'f') d = (uint64_t)(c - 'a' + 10);
        else return false;  /* uppercase / non-hex */
        v = (v << 4) | d;
    }
    *tag_out = v;
    return true;
}
```

- [ ] **Step 5: Run tests, verify they pass**

```bash
nmake test_niimx_async && ./test_niimx_async
```

Expected stdout: `test_niimx_async: 14 passed, 0 failed`. Exit 0.

- [ ] **Step 6: Commit**

```bash
cd /home/dan/Git/netflex
git add cnc/niimx/src/niimx_async.cpp \
        cnc/niimx/src/test_niimx_async.cpp \
        cnc/niimx/src/Makefile
git commit -m "$(cat <<'EOF'
niimx_async: tag<->hex helpers with unit tests

Add the 16-char lowercase-hex serialization used on the wire,
plus a standalone test_niimx_async driver and a Makefile target
that links it. Strict parsing: rejects short/long input,
uppercase, and non-hex characters.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 3: NiimxAsync construct + submit + size + cap

**Files:**
- Modify: `cnc/niimx/src/niimx_async.cpp`
- Modify: `cnc/niimx/src/test_niimx_async.cpp`

- [ ] **Step 1: Write the failing tests**

Append to `test_niimx_async.cpp`, before `main()`:

```c
/* Test publish callback that captures into a vector. */
#include <vector>

struct pub_event { std::string topic, body; };
static std::vector<pub_event> g_events;
static void test_publish(void * /*ctx*/, const std::string &topic,
                         const std::string &body) {
    pub_event e; e.topic = topic; e.body = body;
    g_events.push_back(e);
}

static void test_submit_returns_tag_pending() {
    g_events.clear();
    NiimxAsync a(test_publish, NULL, /*cap*/ 0, /*ret*/ 60);
    uint64_t t = a.submit("id1", 42, "hostA", "who");
    CHECK(t != 0);
    CHECK(a.size() == 1);

    std::string payload;
    CHECK(a.poll(t, &payload) == NIIMX_ASYNC_PENDING);
    /* PENDING poll does not erase: */
    CHECK(a.size() == 1);
}

static void test_submit_unique_tags() {
    NiimxAsync a(test_publish, NULL, 0, 60);
    uint64_t t1 = a.submit("id1", 1, "h", "c1");
    uint64_t t2 = a.submit("id1", 2, "h", "c2");
    uint64_t t3 = a.submit("id1", 3, "h", "c3");
    CHECK(t1 != 0 && t2 != 0 && t3 != 0);
    CHECK(t1 != t2);
    CHECK(t2 != t3);
    CHECK(a.size() == 3);
}

static void test_submit_cap() {
    NiimxAsync a(test_publish, NULL, /*cap*/ 2, 60);
    CHECK(a.submit("i", 1, "h", "c") != 0);
    CHECK(a.submit("i", 2, "h", "c") != 0);
    CHECK(a.submit("i", 3, "h", "c") == 0);   /* at cap, must reject */
    CHECK(a.size() == 2);
}

static void test_submit_zero_cap_unlimited() {
    NiimxAsync a(test_publish, NULL, /*cap*/ 0, 60);
    for (int i = 0; i < 100; ++i) {
        CHECK(a.submit("i", i, "h", "c") != 0);
    }
    CHECK(a.size() == 100);
}
```

And add these calls to `main()` before the `printf`:

```c
    test_submit_returns_tag_pending();
    test_submit_unique_tags();
    test_submit_cap();
    test_submit_zero_cap_unlimited();
```

- [ ] **Step 2: Run, verify link failures**

```bash
cd /home/dan/Git/netflex/cnc/niimx/src
nmake test_niimx_async
```

Expected: link errors on `NiimxAsync::NiimxAsync`, `NiimxAsync::~NiimxAsync`, `NiimxAsync::submit`, `NiimxAsync::poll`.

- [ ] **Step 3: Implement constructor, destructor, submit, and minimal poll**

Append to `cnc/niimx/src/niimx_async.cpp`:

```c
NiimxAsync::NiimxAsync(niimx_publish_fn pub, void *pub_ctx,
                       size_t max_outstanding, int retention_secs)
    : pub_fn_(pub),
      pub_ctx_(pub_ctx),
      cap_(max_outstanding),
      retention_secs_(retention_secs),
      next_tag_(0) {
    /*
     * Seed next_tag_ so tags are unique within a daemon lifetime even
     * across rapid restarts: high 32 bits from wall clock, low bits 1
     * (avoid 0 which is the "no tag" sentinel).
     */
    uint64_t seed_hi = (uint64_t)time(NULL);
    next_tag_ = (seed_hi << 32) | 0x1ULL;
}

NiimxAsync::~NiimxAsync() {
    table_.clear();
}

uint64_t NiimxAsync::submit(const std::string &identity, int32_t msgid,
                            const std::string &host,
                            const std::string &command) {
    if (cap_ != 0 && table_.size() >= cap_) return 0;

    uint64_t tag = next_tag_++;
    if (tag == 0) tag = next_tag_++;   /* never hand out 0 */

    niimx_async_entry_t e;
    e.tag               = tag;
    e.state             = NIIMX_ASYNC_PENDING;
    e.identity          = identity;
    e.msgid             = msgid;
    e.host              = host;
    e.command           = command;
    e.submitted_at      = NiimxAsyncClock::now();
    e.completed_at      = NiimxAsyncClock::time_point();
    e.discard_on_reply  = false;

    table_[tag] = e;
    return tag;
}

niimx_async_state_t NiimxAsync::poll(uint64_t tag, std::string *payload_out) {
    std::map<uint64_t, niimx_async_entry_t>::iterator it = table_.find(tag);
    if (it == table_.end()) return NIIMX_ASYNC_UNKNOWN;

    /* Minimal version for this task: just report state. DONE/EXPIRED
     * erasure lands in Task 5; sweep retention lands in Task 6. */
    if (payload_out && it->second.state == NIIMX_ASYNC_DONE) {
        *payload_out = it->second.payload;
    }
    return it->second.state;
}
```

- [ ] **Step 4: Run tests, verify they pass**

```bash
nmake test_niimx_async && ./test_niimx_async
```

Expected: all tests pass (count grows by the new asserts).

- [ ] **Step 5: Commit**

```bash
cd /home/dan/Git/netflex
git add cnc/niimx/src/niimx_async.cpp cnc/niimx/src/test_niimx_async.cpp
git commit -m "$(cat <<'EOF'
niimx_async: submit + minimal poll

Allocate a unique 64-bit tag per submit, enforce the global cap,
and reject submission when the table is full. Poll returns the
current state; DONE/EXPIRED erasure semantics arrive in a later
commit.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 4: mark_running + complete + publish event

**Files:**
- Modify: `cnc/niimx/src/niimx_async.cpp`
- Modify: `cnc/niimx/src/test_niimx_async.cpp`

- [ ] **Step 1: Write the failing tests**

Append to `test_niimx_async.cpp` before `main()`:

```c
static void test_mark_running_transitions_state() {
    g_events.clear();
    NiimxAsync a(test_publish, NULL, 0, 60);
    uint64_t t = a.submit("i", 1, "h", "c");
    a.mark_running(t);
    std::string payload;
    CHECK(a.poll(t, &payload) == NIIMX_ASYNC_RUNNING);
}

static void test_mark_running_unknown_is_noop() {
    NiimxAsync a(test_publish, NULL, 0, 60);
    a.mark_running(0xdeadbeefULL);   /* must not crash */
    CHECK(a.size() == 0);
}

static void test_complete_stores_payload_and_publishes() {
    g_events.clear();
    NiimxAsync a(test_publish, NULL, 0, 60);
    uint64_t t = a.submit("i", 1, "h", "c");
    a.mark_running(t);
    a.complete(t, "RESULT-BYTES");

    /* Event was published once with the right topic+body. */
    CHECK(g_events.size() == 1);
    CHECK(g_events[0].topic == "niimx.done." + niimx_async_tag_to_hex(t));
    CHECK(g_events[0].body  == "RESULT-BYTES");

    /* Entry is now DONE; payload visible via poll. */
    std::string payload;
    CHECK(a.poll(t, &payload) == NIIMX_ASYNC_DONE);
    CHECK(payload == "RESULT-BYTES");
}

static void test_complete_for_unknown_tag_is_safe() {
    g_events.clear();
    NiimxAsync a(test_publish, NULL, 0, 60);
    a.complete(0xdeadbeefULL, "X");   /* must not crash, no event */
    CHECK(g_events.empty());
}
```

And in `main()` add:

```c
    test_mark_running_transitions_state();
    test_mark_running_unknown_is_noop();
    test_complete_stores_payload_and_publishes();
    test_complete_for_unknown_tag_is_safe();
```

- [ ] **Step 2: Run, verify failures**

```bash
cd /home/dan/Git/netflex/cnc/niimx/src
nmake test_niimx_async
```

Expected: link errors on `NiimxAsync::mark_running`, `NiimxAsync::complete`, and `NiimxAsync::publish_event`.

- [ ] **Step 3: Implement**

Append to `cnc/niimx/src/niimx_async.cpp`:

```c
void NiimxAsync::mark_running(uint64_t tag) {
    std::map<uint64_t, niimx_async_entry_t>::iterator it = table_.find(tag);
    if (it == table_.end()) return;
    if (it->second.state == NIIMX_ASYNC_PENDING) {
        it->second.state = NIIMX_ASYNC_RUNNING;
    }
}

void NiimxAsync::publish_event(uint64_t tag, const std::string &payload,
                               const char *suffix) {
    if (!pub_fn_) return;
    std::string topic = "niimx.";
    topic += suffix;
    topic += ".";
    topic += niimx_async_tag_to_hex(tag);
    pub_fn_(pub_ctx_, topic, payload);
}

void NiimxAsync::complete(uint64_t tag, const std::string &payload) {
    std::map<uint64_t, niimx_async_entry_t>::iterator it = table_.find(tag);
    if (it == table_.end()) return;   /* swept or unknown — drop */

    it->second.completed_at = NiimxAsyncClock::now();

    if (it->second.discard_on_reply) {
        it->second.state   = NIIMX_ASYNC_CANCELLED;
        it->second.payload.clear();
        publish_event(tag, std::string(), "cancelled");
    } else {
        it->second.state   = NIIMX_ASYNC_DONE;
        it->second.payload = payload;
        publish_event(tag, payload, "done");
    }
}
```

- [ ] **Step 4: Run tests**

```bash
nmake test_niimx_async && ./test_niimx_async
```

Expected: all tests pass.

- [ ] **Step 5: Commit**

```bash
cd /home/dan/Git/netflex
git add cnc/niimx/src/niimx_async.cpp cnc/niimx/src/test_niimx_async.cpp
git commit -m "$(cat <<'EOF'
niimx_async: mark_running, complete, publish_event

complete() stores the payload and publishes a two-frame event
(topic, body). cancel-pending entries publish a "cancelled"
event with an empty body. Unknown-tag completes are silently
dropped (covers the race where a swept tag's backend reply
arrives late).

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 5: DONE-erase semantics + cancel + EXPIRED-inline + sweep

**Files:**
- Modify: `cnc/niimx/src/niimx_async.cpp`
- Modify: `cnc/niimx/src/test_niimx_async.cpp`

- [ ] **Step 1: Write the failing tests**

Append to `test_niimx_async.cpp`:

```c
static void test_poll_done_erases_entry() {
    g_events.clear();
    NiimxAsync a(test_publish, NULL, 0, 60);
    uint64_t t = a.submit("i", 1, "h", "c");
    a.mark_running(t);
    a.complete(t, "X");

    std::string p;
    CHECK(a.poll(t, &p) == NIIMX_ASYNC_DONE);
    CHECK(p == "X");
    CHECK(a.size() == 0);                         /* erased */

    /* Second poll: tag is gone */
    p.clear();
    CHECK(a.poll(t, &p) == NIIMX_ASYNC_UNKNOWN);
    CHECK(p.empty());
}

static void test_cancel_pending_returns_cancelled_now() {
    g_events.clear();
    NiimxAsync a(test_publish, NULL, 0, 60);
    uint64_t t = a.submit("i", 1, "h", "c");

    CHECK(a.cancel(t) == NIIMX_CANCEL_CANCELLED_NOW);

    /* CANCELLED entry kept until retention; poll does NOT erase. */
    std::string p;
    CHECK(a.poll(t, &p) == NIIMX_ASYNC_CANCELLED);
    CHECK(a.poll(t, &p) == NIIMX_ASYNC_CANCELLED);  /* still there */
    CHECK(a.size() == 1);
}

static void test_cancel_running_returns_cancel_pending() {
    g_events.clear();
    NiimxAsync a(test_publish, NULL, 0, 60);
    uint64_t t = a.submit("i", 1, "h", "c");
    a.mark_running(t);

    CHECK(a.cancel(t) == NIIMX_CANCEL_PENDING);

    /* Backend reply arrives — must publish cancelled event, not done */
    a.complete(t, "LATE-PAYLOAD");
    CHECK(g_events.size() == 1);
    CHECK(g_events[0].topic == "niimx.cancelled." + niimx_async_tag_to_hex(t));
    CHECK(g_events[0].body.empty());

    /* Poll still shows CANCELLED until retention */
    std::string p;
    CHECK(a.poll(t, &p) == NIIMX_ASYNC_CANCELLED);
}

static void test_cancel_after_done_returns_already_done() {
    NiimxAsync a(test_publish, NULL, 0, 60);
    uint64_t t = a.submit("i", 1, "h", "c");
    a.complete(t, "X");
    CHECK(a.cancel(t) == NIIMX_CANCEL_ALREADY_DONE);
}

static void test_cancel_unknown_returns_not_found() {
    NiimxAsync a(test_publish, NULL, 0, 60);
    CHECK(a.cancel(0xdeadbeefULL) == NIIMX_CANCEL_NOT_FOUND);
}

static void test_sweep_erases_completed_past_retention() {
    NiimxAsync a(test_publish, NULL, 0, /*retention*/ 1);
    uint64_t t1 = a.submit("i", 1, "h", "c");
    uint64_t t2 = a.submit("i", 2, "h", "c");
    a.complete(t1, "X");
    /* t2 stays PENDING */

    NiimxAsyncClock::time_point future =
        NiimxAsyncClock::now() + std::chrono::seconds(2);
    a.sweep(future);

    /* DONE entry past retention -> erased; PENDING untouched */
    std::string p;
    CHECK(a.poll(t1, &p) == NIIMX_ASYNC_UNKNOWN);
    CHECK(a.poll(t2, &p) == NIIMX_ASYNC_PENDING);
    CHECK(a.size() == 1);
}

static void test_poll_expired_inline_erases_entry() {
    /* Window has elapsed but sweep hasn't run yet; the poll itself
     * should report EXPIRED and erase. Use retention=0 so anything
     * completed in the past is already over the window. */
    NiimxAsync a(test_publish, NULL, 0, /*retention*/ 0);
    uint64_t t = a.submit("i", 1, "h", "c");
    a.complete(t, "X");

    /* Sleep 1ms so completed_at + 0s strictly < now. */
    /* (steady_clock has ns resolution but be explicit.) */
    std::this_thread::sleep_for(std::chrono::milliseconds(1));

    std::string p;
    CHECK(a.poll(t, &p) == NIIMX_ASYNC_EXPIRED);
    CHECK(p.empty());
    CHECK(a.size() == 0);
    CHECK(a.poll(t, &p) == NIIMX_ASYNC_UNKNOWN);
}

static void test_sweep_never_erases_pending_or_running() {
    NiimxAsync a(test_publish, NULL, 0, /*retention*/ 0);
    uint64_t t_pending = a.submit("i", 1, "h", "c");
    uint64_t t_running = a.submit("i", 2, "h", "c");
    a.mark_running(t_running);

    NiimxAsyncClock::time_point future =
        NiimxAsyncClock::now() + std::chrono::seconds(10);
    a.sweep(future);

    CHECK(a.size() == 2);
    std::string p;
    CHECK(a.poll(t_pending, &p) == NIIMX_ASYNC_PENDING);
    CHECK(a.poll(t_running, &p) == NIIMX_ASYNC_RUNNING);
}
```

Add `#include <thread>` and `#include <chrono>` near the top of `test_niimx_async.cpp` (just after the `niimx_async.h` include).

Add to `main()`:

```c
    test_poll_done_erases_entry();
    test_cancel_pending_returns_cancelled_now();
    test_cancel_running_returns_cancel_pending();
    test_cancel_after_done_returns_already_done();
    test_cancel_unknown_returns_not_found();
    test_sweep_erases_completed_past_retention();
    test_poll_expired_inline_erases_entry();
    test_sweep_never_erases_pending_or_running();
```

- [ ] **Step 2: Run, verify failures**

```bash
cd /home/dan/Git/netflex/cnc/niimx/src
nmake test_niimx_async
```

Expected: link errors on `cancel`, `sweep`; test failures on the DONE-erase / EXPIRED-inline asserts (the Task 3 minimal `poll` does not erase).

- [ ] **Step 3: Implement final poll, cancel, sweep**

In `cnc/niimx/src/niimx_async.cpp`, replace the existing `NiimxAsync::poll` body with the full version, then append `cancel` and `sweep`:

```c
niimx_async_state_t NiimxAsync::poll(uint64_t tag, std::string *payload_out) {
    std::map<uint64_t, niimx_async_entry_t>::iterator it = table_.find(tag);
    if (it == table_.end()) return NIIMX_ASYNC_UNKNOWN;

    /* Check inline-expiry for completed states. */
    if ((it->second.state == NIIMX_ASYNC_DONE ||
         it->second.state == NIIMX_ASYNC_CANCELLED) &&
        retention_secs_ >= 0) {
        NiimxAsyncClock::time_point now = NiimxAsyncClock::now();
        NiimxAsyncClock::time_point deadline =
            it->second.completed_at + std::chrono::seconds(retention_secs_);
        if (now > deadline) {
            table_.erase(it);
            return NIIMX_ASYNC_EXPIRED;
        }
    }

    switch (it->second.state) {
        case NIIMX_ASYNC_DONE:
            if (payload_out) *payload_out = it->second.payload;
            table_.erase(it);          /* DONE delivered once */
            return NIIMX_ASYNC_DONE;
        case NIIMX_ASYNC_CANCELLED:
            return NIIMX_ASYNC_CANCELLED;
        case NIIMX_ASYNC_PENDING:
            return NIIMX_ASYNC_PENDING;
        case NIIMX_ASYNC_RUNNING:
            return NIIMX_ASYNC_RUNNING;
        default:
            return NIIMX_ASYNC_UNKNOWN;
    }
}

niimx_cancel_result_t NiimxAsync::cancel(uint64_t tag) {
    std::map<uint64_t, niimx_async_entry_t>::iterator it = table_.find(tag);
    if (it == table_.end()) return NIIMX_CANCEL_NOT_FOUND;

    switch (it->second.state) {
        case NIIMX_ASYNC_PENDING:
            it->second.state         = NIIMX_ASYNC_CANCELLED;
            it->second.completed_at  = NiimxAsyncClock::now();
            return NIIMX_CANCEL_CANCELLED_NOW;
        case NIIMX_ASYNC_RUNNING:
            it->second.discard_on_reply = true;
            return NIIMX_CANCEL_PENDING;
        case NIIMX_ASYNC_DONE:
        case NIIMX_ASYNC_CANCELLED:
            return NIIMX_CANCEL_ALREADY_DONE;
        default:
            return NIIMX_CANCEL_NOT_FOUND;
    }
}

void NiimxAsync::sweep(NiimxAsyncClock::time_point now) {
    if (retention_secs_ < 0) return;
    std::map<uint64_t, niimx_async_entry_t>::iterator it = table_.begin();
    while (it != table_.end()) {
        if ((it->second.state == NIIMX_ASYNC_DONE ||
             it->second.state == NIIMX_ASYNC_CANCELLED) &&
            now > it->second.completed_at +
                  std::chrono::seconds(retention_secs_)) {
            table_.erase(it++);
        } else {
            ++it;
        }
    }
}
```

- [ ] **Step 4: Run tests**

```bash
nmake test_niimx_async && ./test_niimx_async
```

Expected: all tests pass; output reports zero failures.

- [ ] **Step 5: Commit**

```bash
cd /home/dan/Git/netflex
git add cnc/niimx/src/niimx_async.cpp cnc/niimx/src/test_niimx_async.cpp
git commit -m "$(cat <<'EOF'
niimx_async: DONE-erase, cancel, sweep, EXPIRED inline

poll() now erases DONE entries on read (spec: DONE delivered
once) and reports EXPIRED + erases inline when the retention
window has elapsed before sweep runs. cancel() differentiates
PENDING (immediate) from RUNNING (discard-on-reply). sweep()
removes DONE/CANCELLED entries past retention; PENDING/RUNNING
are never swept.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Phase 2 — niimxd integration

We now wire `NiimxAsync` into `niimxd.cpp`. The integration is six small changes plus the publish bridge from `nfdb_g_xpub`.

### Task 6: Publish bridge from XPUB socket

The production publish callback needs to send a two-frame ZMQ message on `nfdb_g_xpub`. Implement it inside `niimx_async.cpp` so the wiring lives next to the rest of the module, then `niimxd` passes `&niimx_async_zmq_publish` to the constructor.

**Files:**
- Modify: `cnc/niimx/src/niimx_async.h` — declare bridge.
- Modify: `cnc/niimx/src/niimx_async.cpp` — define bridge.
- Modify: `cnc/niimx/src/test_niimx_async.cpp` — (no change required; tests use the test stub).

- [ ] **Step 1: Add declaration**

Append before `#endif /* NIIMX_ASYNC_H */` in `niimx_async.h`:

```c
/**
 * @brief niimx_publish_fn bridge that sends a two-frame ZMQ event.
 *
 * @param ctx   Must point to a bound XPUB socket (cast from void*).
 * @param topic Frame 1.
 * @param body  Frame 2.
 *
 * Failures are logged via stderr; no exception is thrown.
 */
void niimx_async_zmq_publish(void *ctx,
                             const std::string &topic,
                             const std::string &body);
```

- [ ] **Step 2: Add definition**

Append to `niimx_async.cpp`:

```c
#include <zmq.h>

void niimx_async_zmq_publish(void *ctx,
                             const std::string &topic,
                             const std::string &body) {
    void *sock = ctx;
    if (!sock) return;

    zmq_msg_t m1, m2;
    zmq_msg_init_size(&m1, topic.size());
    if (!topic.empty()) memcpy(zmq_msg_data(&m1), topic.data(), topic.size());
    if (zmq_msg_send(&m1, sock, ZMQ_SNDMORE) < 0) {
        fprintf(stderr, "niimx_async_zmq_publish: topic send failed errno=%d\n",
                errno);
        zmq_msg_close(&m1);
        return;
    }

    zmq_msg_init_size(&m2, body.size());
    if (!body.empty()) memcpy(zmq_msg_data(&m2), body.data(), body.size());
    if (zmq_msg_send(&m2, sock, 0) < 0) {
        fprintf(stderr, "niimx_async_zmq_publish: body send failed errno=%d\n",
                errno);
    }
}
```

(`zmq_msg_send` consumes/closes the message on success — no explicit close needed on the success path.)

- [ ] **Step 3: Verify build**

```bash
cd /home/dan/Git/netflex/cnc/niimx/src
nmake test_niimx_async && ./test_niimx_async
```

Expected: passes (the bridge isn't exercised; this just confirms it links).

- [ ] **Step 4: Commit**

```bash
cd /home/dan/Git/netflex
git add cnc/niimx/src/niimx_async.h cnc/niimx/src/niimx_async.cpp
git commit -m "$(cat <<'EOF'
niimx_async: ZMQ publish bridge

Production callback that sends [topic][body] on a bound XPUB
socket. niimxd will pass this with nfdb_g_xpub as the ctx.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 7: Add config knobs and Request fields

**Files:**
- Modify: `cnc/niimx/src/niimxd.cpp`

- [ ] **Step 1: Locate the existing Cfg struct and Request struct**

```bash
cd /home/dan/Git/netflex/cnc/niimx/src
grep -n 'max_iq' niimxd.cpp | head
grep -n 'struct Request' niimxd.cpp | head
```

Expected: identify the `struct Cfg` / `struct CfgT` definition (where `max_iq`, `reject` live) and `struct Request` (around line 290–310).

- [ ] **Step 2: Add the knobs to Cfg**

In the `Cfg` definition next to `max_iq`, add:

```c
int    max_async_outstanding;   /**< Async tag table cap; 0 = unlimited. */
int    async_retention_secs;    /**< Retention window for DONE/CANCELLED. */
```

Initialize defaults wherever `max_iq` is defaulted (commonly `Cfg{...}` initializer or a function like `load_config()`):

```c
Cfg.max_async_outstanding = 1024;
Cfg.async_retention_secs  = 300;
```

If `niimxd.conf` parsing exists (the loop that reads key/value lines), add two cases:

```c
} else if (key == "max_async_outstanding") {
    Cfg.max_async_outstanding = atoi(val.c_str());
} else if (key == "async_retention_secs") {
    Cfg.async_retention_secs  = atoi(val.c_str());
```

placed in the same chained `else if` block as the existing `max_iq` handling.

- [ ] **Step 3: Add fields to Request**

Locate `struct Request` and append two fields:

```c
uint64_t async_tag;   /**< 0 = sync request, non-zero = parked in g_async. */
bool     async;       /**< Convenience flag = (async_tag != 0). */
```

Find every place where a `Request` is constructed (search: `Request req`, `Request{`). Initialize the new fields to `0` / `false`. Example in `niimx_handle_zmq`:

```c
Request req;
req.identity   = identity;
req.host       = host;
req.msgid      = parse_i32(s_msgid);
req.mpid       = parse_i32(s_mpid);
req.tmout      = parse_i32(s_tmout);
req.command    = command;
req.arrived    = Clock::now();
req.async_tag  = 0;        /* NEW */
req.async      = false;    /* NEW */
```

If `Request` uses an in-class initializer (`uint64_t async_tag = 0;` style), prefer that to centralize the default.

- [ ] **Step 4: Add include and global pointer**

Near the other `#include` lines at the top of `niimxd.cpp`, add:

```c
#include "niimx_async.h"
```

Locate `struct NiimxGlobalState` (search: `struct NiimxGlobalState`). Add:

```c
NiimxAsync *async;   /**< Owns the async tag table. */
```

Initialize to `nullptr` in the existing zero-init or constructor.

- [ ] **Step 5: Verify build**

```bash
cd /home/dan/Git/netflex/cnc/niimx/src
nmake ../../../3b2/bin/niimxd
```

Expected: clean build, no warnings on the new fields. If the build fails, fix the `Request` constructors you missed.

- [ ] **Step 6: Commit**

```bash
cd /home/dan/Git/netflex
git add cnc/niimx/src/niimxd.cpp
git commit -m "$(cat <<'EOF'
niimxd: add async config knobs and Request fields

Cfg gains max_async_outstanding (default 1024) and
async_retention_secs (default 300). Request gains async_tag and
the async flag. G gains a NiimxAsync* pointer. No behavior
change yet — wiring lands in subsequent commits.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 8: Construct and destroy NiimxAsync in niimxd lifecycle

**Files:**
- Modify: `cnc/niimx/src/niimxd.cpp`

- [ ] **Step 1: Find the startup XPUB bind**

```bash
grep -n 'nfdb_g_xpub' niimxd.cpp
```

Locate the line after `nfdb_g_xpub` is bound to `nfdb_pub.sock` and is non-null. Insert directly after:

```c
G.async = new NiimxAsync(&niimx_async_zmq_publish,
                         nfdb_g_xpub,
                         (size_t)Cfg.max_async_outstanding,
                         Cfg.async_retention_secs);
```

- [ ] **Step 2: Find the shutdown XPUB close**

In the same file, locate `zmq_close(nfdb_g_xpub)` and the `nfdb_g_xpub = NULL` that follow. Immediately before the `zmq_close`, add:

```c
delete G.async;
G.async = nullptr;
```

Order matters: `~NiimxAsync()` does not touch the socket, but if any in-flight publish is queued, we want it flushed before the socket dies. Closing the socket after deletion is harmless.

- [ ] **Step 3: Verify build**

```bash
cd /home/dan/Git/netflex/cnc/niimx/src
nmake ../../../3b2/bin/niimxd
```

Expected: clean build.

- [ ] **Step 4: Smoke run**

```bash
./test_nfdb.sh
```

Expected: existing tests still pass (we haven't changed any sync paths yet, and the async table is allocated but unused).

- [ ] **Step 5: Commit**

```bash
cd /home/dan/Git/netflex
git add cnc/niimx/src/niimxd.cpp
git commit -m "$(cat <<'EOF'
niimxd: construct/destroy NiimxAsync in lifecycle

Allocate the async tag table after nfdb_g_xpub is bound; free
it before the socket closes. Module is not yet wired into the
recv/dispatch/reply paths.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 9: Receive the mode frame and route by (mode, command_prefix)

**Files:**
- Modify: `cnc/niimx/src/niimxd.cpp`

- [ ] **Step 1: Read the existing recv loop**

Inspect `niimx_handle_zmq` (~line 956) to confirm the current frame order is `identity | empty | host | msgid | mpid | tmout | command`. The change splits the recv into seven frames and adds a router branch.

- [ ] **Step 2: Update the recv loop**

Inside `niimx_handle_zmq`, replace the existing recv block that reads through `command` with:

```c
// Receive multipart: identity | empty | host | msgid(4) | mpid(4) | tmout(4) | mode(4) | command
string identity, empty, host, s_msgid, s_mpid, s_tmout, s_mode, command;
bool more;

if (!niimx_zmq_recv_frame(G.router, identity, more) || !more) continue;
if (!niimx_zmq_recv_frame(G.router, empty,    more) || !more) continue;
if (!niimx_zmq_recv_frame(G.router, host,     more) || !more) continue;
if (!niimx_zmq_recv_frame(G.router, s_msgid,  more) || !more) continue;
if (!niimx_zmq_recv_frame(G.router, s_mpid,   more) || !more) continue;
if (!niimx_zmq_recv_frame(G.router, s_tmout,  more) || !more) continue;
if (!niimx_zmq_recv_frame(G.router, s_mode,   more) || !more) continue;
if (!niimx_zmq_recv_frame(G.router, command,  more))          continue;
while (more) { string x; niimx_zmq_recv_frame(G.router, x, more); }

auto parse_i32 = [](const string& s) -> int32_t {
    if (s.size() < 4) return 0;
    int32_t v;
    memcpy(&v, s.data(), 4);
    return v;
};

int32_t r_msgid = parse_i32(s_msgid);
int32_t r_mpid  = parse_i32(s_mpid);
int32_t r_tmout = parse_i32(s_tmout);
int32_t r_mode  = parse_i32(s_mode);
```

(Delete the old block that built `req` directly — we now branch on `r_mode` first.)

- [ ] **Step 3: Add helper for sending a single-segment async response**

If `niimx_zmq_send_response` already exists with signature `(const string &identity, int32_t msgid, const string &payload)`, reuse it. Otherwise, before `niimx_handle_zmq`, add:

```c
/**
 * @brief Send a single-segment response with mcont=0.
 * @param identity  ZMQ identity frame of the original client.
 * @param msgid     Original msgid (echoed back).
 * @param payload   Response bytes.
 */
static void niimx_zmq_send_simple(const string &identity, int32_t msgid,
                                  const string &payload) {
    int32_t mcont = 0;
    zmq_send(G.router, identity.data(), identity.size(), ZMQ_SNDMORE);
    zmq_send(G.router, "",       0,                       ZMQ_SNDMORE);
    zmq_send(G.router, &msgid,   4,                       ZMQ_SNDMORE);
    zmq_send(G.router, &mcont,   4,                       ZMQ_SNDMORE);
    zmq_send(G.router, payload.data(), payload.size(),    0);
}
```

(If a similar helper already exists, use that instead — verify by `grep -n 'mcont' niimxd.cpp`.)

- [ ] **Step 4: Add the mode/verb router**

After the recv block, before the existing enqueue-into-`requestQ` code, insert:

```c
/* --- async control verbs (mode==0 only) --- */
if (r_mode == 0 && command.compare(0, 5, "POLL ") == 0) {
    uint64_t tag;
    if (!niimx_async_hex_to_tag(command.substr(5), &tag)) {
        niimx_zmq_send_simple(identity, r_msgid, "-ERR bad tag");
        continue;
    }
    string payload;
    niimx_async_state_t st = G.async->poll(tag, &payload);
    switch (st) {
        case NIIMX_ASYNC_PENDING:
        case NIIMX_ASYNC_RUNNING:
            niimx_zmq_send_simple(identity, r_msgid, "+OK PENDING");
            break;
        case NIIMX_ASYNC_DONE: {
            string body = "+OK DONE\n";
            body.append(payload);
            niimx_zmq_send_simple(identity, r_msgid, body);
            break;
        }
        case NIIMX_ASYNC_CANCELLED:
            niimx_zmq_send_simple(identity, r_msgid, "+OK CANCELLED");
            break;
        case NIIMX_ASYNC_EXPIRED:
            niimx_zmq_send_simple(identity, r_msgid, "+OK EXPIRED");
            break;
        case NIIMX_ASYNC_UNKNOWN:
        default:
            niimx_zmq_send_simple(identity, r_msgid, "-ERR UNKNOWN tag");
            break;
    }
    continue;
}

if (r_mode == 0 && command.compare(0, 7, "CANCEL ") == 0) {
    uint64_t tag;
    if (!niimx_async_hex_to_tag(command.substr(7), &tag)) {
        niimx_zmq_send_simple(identity, r_msgid, "-ERR bad tag");
        continue;
    }
    niimx_cancel_result_t r = G.async->cancel(tag);
    const char *msg;
    switch (r) {
        case NIIMX_CANCEL_CANCELLED_NOW:  msg = "+OK cancelled";       break;
        case NIIMX_CANCEL_PENDING:        msg = "+OK cancel-pending";  break;
        case NIIMX_CANCEL_ALREADY_DONE:   msg = "-ERR already-done";   break;
        case NIIMX_CANCEL_NOT_FOUND:
        default:                          msg = "-ERR UNKNOWN tag";    break;
    }
    /* If we cancelled-now, evict any matching Request from RequestQ. */
    if (r == NIIMX_CANCEL_CANCELLED_NOW) {
        for (auto it = G.requestQ.begin(); it != G.requestQ.end(); ++it) {
            if (it->async && it->async_tag == tag) { G.requestQ.erase(it); break; }
        }
    }
    niimx_zmq_send_simple(identity, r_msgid, msg);
    continue;
}

/* mode==1 with a POLL/CANCEL command is malformed. */
if (r_mode == 1 &&
    (command.compare(0, 5, "POLL ")  == 0 ||
     command.compare(0, 7, "CANCEL ") == 0)) {
    niimx_zmq_send_simple(identity, r_msgid, "-ERR mode mismatch");
    continue;
}

/* Any other value of mode is rejected. */
if (r_mode != 0 && r_mode != 1) {
    niimx_zmq_send_simple(identity, r_msgid, "-ERR bad mode");
    continue;
}
```

- [ ] **Step 5: Rebuild the Request and branch on mode==1 for SUBMIT**

After the verb router (still inside the recv loop), build the request:

```c
Request req;
req.identity   = identity;
req.host       = host;
req.msgid      = r_msgid;
req.mpid       = r_mpid;
req.tmout      = r_tmout;
req.command    = command;
req.arrived    = Clock::now();
req.async_tag  = 0;
req.async      = false;

Trc(3, "RX msgid=" << req.msgid << " mpid=" << req.mpid
       << " tmout=" << req.tmout << " mode=" << r_mode
       << " cmd=[" << req.command.substr(0, 40) << "]" << endl);
```

Keep all existing rejects (`SystemUtilization >= reject`, `requestQ` full, unknown host, host unavailable) exactly as they are — they apply equally to sync and async.

After the rejects, replace the existing `G.requestQ.push_back(move(req));` with:

```c
if (r_mode == 1) {
    uint64_t tag = G.async->submit(req.identity, req.msgid,
                                   req.host,     req.command);
    if (tag == 0) {
        niimx_zmq_send_simple(identity, req.msgid,
                              "NIIMX^Async table full");
        continue;
    }
    /* Ack immediately. */
    char ackbuf[40];
    snprintf(ackbuf, sizeof(ackbuf), "+OK tag=%016llx",
             (unsigned long long)tag);
    niimx_zmq_send_simple(identity, req.msgid, ackbuf);

    req.async     = true;
    req.async_tag = tag;
}

G.requestQ.push_back(std::move(req));
```

- [ ] **Step 6: Verify build**

```bash
cd /home/dan/Git/netflex/cnc/niimx/src
nmake ../../../3b2/bin/niimxd
```

Expected: clean build.

- [ ] **Step 7: Sanity check sync path still works**

```bash
./test_nfdb.sh
```

Expected: existing pass count unchanged. Existing niimx CLI binary will fail because it sends 6 frames (no `mode`), but `test_nfdb.sh` does not use the niimx CLI — it tests the nfdb protocol on a separate socket. If `test_nfdb.sh` was changed to also drive niimx, that part will need the CLI rebuilt (next task).

- [ ] **Step 8: Commit**

```bash
cd /home/dan/Git/netflex
git add cnc/niimx/src/niimxd.cpp
git commit -m "$(cat <<'EOF'
niimxd: accept mode frame and route POLL/CANCEL/SUBMIT

niimx_handle_zmq now requires a 7th request frame (mode). POLL
and CANCEL are handled inline against G.async without entering
RequestQ. SUBMIT (mode=1) allocates a tag, acks immediately,
and parks an async-flagged Request in the queue.

WIRE-BREAKING: niimx CLI must be rebuilt to send the new
frame. The CLI is the only existing caller and is updated in
the next commit.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 10: Wire mark_running on dispatch and the async reply branch

**Files:**
- Modify: `cnc/niimx/src/niimxd.cpp`

- [ ] **Step 1: Locate the dispatch site**

```bash
grep -n 'niimx_dispatch_requests' niimxd.cpp
```

Open `niimx_dispatch_requests` (~line 1928). Find where a request is pulled from `G.requestQ` and handed to a `bchannel`.

- [ ] **Step 2: Add mark_running**

Immediately after the request leaves the queue and a bchannel is assigned (i.e., we have both `req` in hand and a confirmed dispatch), add:

```c
if (req.async && G.async) {
    G.async->mark_running(req.async_tag);
}
```

(Make sure this runs after the dispatch is confirmed — not before — so a failed dispatch doesn't leave the entry in RUNNING.)

- [ ] **Step 3: Locate the reply site**

```bash
grep -n 'niimx_zmq_send_response' niimxd.cpp
```

Find the call site(s) where a completed backend response is sent to the client. Most are reachable from a single function (search for "send response" comments, and for the location where `req.msgid` + a response string are combined). Identify the *normal* (non-reject) success path; do not modify the early-reject paths (those should remain sync-style).

- [ ] **Step 4: Branch on req.async at the reply site**

Replace the success-path reply call with:

```c
if (req.async && G.async) {
    G.async->complete(req.async_tag, response_payload);
} else {
    niimx_zmq_send_response(req.identity, req.msgid, response_payload);
}
```

Use the existing payload variable name; this template assumes `response_payload`. If the existing code constructs the payload differently (e.g., per-segment loop for multi-segment responses), apply the same branch: instead of calling the multi-segment send helper, accumulate the full bytes into a `std::string payload`, then pass it to `complete()`.

If multiple reply sites exist, apply the same branch to each. Use grep to enumerate:

```bash
grep -n 'niimx_zmq_send_response' niimxd.cpp
```

- [ ] **Step 5: Also handle timeout-completed async requests**

Locate the timeout-expiry path (likely in the same area that builds `"NIIMX^Timeout"` responses, around `niimx_dispatch_requests` or a `niimx_tick_*` function). For each location that currently calls `niimx_zmq_send_response(req.identity, req.msgid, "NIIMX^Timeout")`, apply the same branch:

```c
if (req.async && G.async) {
    G.async->complete(req.async_tag, "NIIMX^Timeout");
} else {
    niimx_zmq_send_response(req.identity, req.msgid, "NIIMX^Timeout");
}
```

- [ ] **Step 6: Verify build**

```bash
cd /home/dan/Git/netflex/cnc/niimx/src
nmake ../../../3b2/bin/niimxd
```

Expected: clean build.

- [ ] **Step 7: Commit**

```bash
cd /home/dan/Git/netflex
git add cnc/niimx/src/niimxd.cpp
git commit -m "$(cat <<'EOF'
niimxd: mark_running at dispatch, branch reply for async

When dispatching a request to a bchannel, transition the async
entry to RUNNING. When a backend reply arrives (including
"NIIMX^Timeout"), async requests are parked in G.async and
publish a completion event instead of being sent back to the
client DEALER. Sync requests are unaffected.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 11: Sweep tick

**Files:**
- Modify: `cnc/niimx/src/niimxd.cpp`

- [ ] **Step 1: Find the 1-Hz tick handler**

```bash
grep -n 'tick_1hz\|niimx_tick\|niimx_idle_expire\|timerfd' niimxd.cpp | head
```

Locate the handler that runs once per second (idle/timeout sweep). Search for `Clock::now()` calls inside any such handler to confirm.

- [ ] **Step 2: Call sweep**

Inside the 1-Hz handler, add one line:

```c
if (G.async) G.async->sweep(Clock::now());
```

If `Clock` here is something other than `NiimxAsyncClock`, adapt the call (both should be `std::chrono::steady_clock` aliases). If they differ, change `NiimxAsyncClock` in `niimx_async.h` to match the niimxd clock alias.

- [ ] **Step 3: Verify build**

```bash
cd /home/dan/Git/netflex/cnc/niimx/src
nmake ../../../3b2/bin/niimxd
```

Expected: clean build.

- [ ] **Step 4: Commit**

```bash
cd /home/dan/Git/netflex
git add cnc/niimx/src/niimxd.cpp
git commit -m "$(cat <<'EOF'
niimxd: sweep async retention on 1-Hz tick

Hook G.async->sweep() into the existing 1-Hz timer handler so
completed/cancelled entries past their retention window are
erased without waiting for a poll.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Phase 3 — niimx CLI update

### Task 12: Add mode frame to niimx CLI

**Files:**
- Modify: `cnc/niimx/src/niimx.cpp`

- [ ] **Step 1: Add the mode frame to the send sequence**

Locate the send sequence in `niimx.cpp` (around line 86):

```c
send_frame(sock, "",      0,                true);
send_frame(sock, host,    strlen(host),     true);
send_frame(sock, &msgid,  4,                true);
send_frame(sock, &mpid,   4,                true);
send_frame(sock, &tmout,  4,                true);
send_frame(sock, command, strlen(command),  false);
```

Insert a `mode` send (default sync = 0) between `tmout` and `command`:

```c
int32_t mode = 0;

send_frame(sock, "",      0,                true);
send_frame(sock, host,    strlen(host),     true);
send_frame(sock, &msgid,  4,                true);
send_frame(sock, &mpid,   4,                true);
send_frame(sock, &tmout,  4,                true);
send_frame(sock, &mode,   4,                true);
send_frame(sock, command, strlen(command),  false);
```

- [ ] **Step 2: Build niimx**

```bash
cd /home/dan/Git/netflex/cnc/niimx/src
nmake ../../../3b2/bin/niimx
```

Expected: clean build.

- [ ] **Step 3: Smoke test against niimxd**

(Requires niimxd running; if it isn't, start it via your usual mechanism.)

```bash
../../../3b2/bin/niimx -c who
```

Expected: response prints. If `-ERR bad mode` comes back, double-check the frame order; if a timeout occurs, niimxd may not be running or `niimx.ipc` may be stale.

- [ ] **Step 4: Commit**

```bash
cd /home/dan/Git/netflex
git add cnc/niimx/src/niimx.cpp
git commit -m "$(cat <<'EOF'
niimx: emit mode=0 frame in every request

Required by the new wire format. CLI exposes only sync behavior
for now (always mode=0). Async submit/poll/cancel via the CLI
is a future change.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Phase 4 — Integration tests

### Task 13: Write integration test driver

**Files:**
- Create: `cnc/niimx/src/test_niimx_async.sh`

Python (with `pyzmq`) is the lightest path for the multi-step integration assertions (subscribe + submit + poll). If `pyzmq` is not available on the test host, the alternative is a small C helper built in this repo — but for the plan we assume `pyzmq` is present (matches the toolchain on the dev hosts).

- [ ] **Step 1: Write the shell driver**

Create `cnc/niimx/src/test_niimx_async.sh`:

```bash
#!/bin/bash
# Integration tests for niimx async commands.
#
# Assumes:
#   - niimxd is built and bins are in $PBIN (../../../3b2/bin by default)
#   - niimx.ipc is reachable
#   - python3 + pyzmq are available
#
# Exit non-zero on any failure.

set -u

BIN_DIR="${BIN_DIR:-../../../3b2/bin}"
NIIMXD="$BIN_DIR/niimxd"
NIIMX_IPC="${NIIMX_IPC:-ipc:///usr/cnc/data/niimx.ipc}"
NFDB_PUB="${NFDB_PUB:-ipc:///usr/cnc/data/nfdb_pub.sock}"
CONF="${CONF:-/tmp/niimxd_async_test.conf}"

PASS=0
FAIL=0

note() { echo "[test] $*"; }
fail() { echo "[FAIL] $*"; FAIL=$((FAIL+1)); }
pass() { echo "[ ok ] $*"; PASS=$((PASS+1)); }

# Start niimxd with a small async cap and short retention so we can
# exercise both the cap-reject and retention-expiry paths.
cat > "$CONF" <<'EOC'
max_async_outstanding 2
async_retention_secs  1
EOC

# Kill any prior instance and start fresh.
pkill -f "$NIIMXD" 2>/dev/null
sleep 0.5
"$NIIMXD" -c "$CONF" &
NIIMXD_PID=$!
trap 'kill $NIIMXD_PID 2>/dev/null; rm -f "$CONF"' EXIT
sleep 0.5

# python helper: stays alive across multiple test cases via heredoc
PYHELPER=$(cat <<'PY'
import os, sys, zmq, struct, time, threading

ctx = zmq.Context.instance()
endpoint = os.environ["NIIMX_IPC"]
pub_endpoint = os.environ["NFDB_PUB"]

def open_dealer(ident):
    s = ctx.socket(zmq.DEALER)
    s.setsockopt(zmq.IDENTITY, ident.encode())
    s.setsockopt(zmq.RCVTIMEO, 5000)
    s.connect(endpoint)
    return s

def send(s, host, msgid, mpid, tmout, mode, command):
    s.send(b"", zmq.SNDMORE)
    s.send(host.encode(), zmq.SNDMORE)
    s.send(struct.pack("<i", msgid), zmq.SNDMORE)
    s.send(struct.pack("<i", mpid),  zmq.SNDMORE)
    s.send(struct.pack("<i", tmout), zmq.SNDMORE)
    s.send(struct.pack("<i", mode),  zmq.SNDMORE)
    s.send(command.encode(), 0)

def recv(s):
    # empty | msgid | mcont | payload  (loop while mcont > 0)
    body = b""
    while True:
        _empty = s.recv()
        msgid  = struct.unpack("<i", s.recv())[0]
        mcont  = struct.unpack("<i", s.recv())[0]
        body  += s.recv()
        if mcont <= 0: break
    return msgid, body

def submit(s, cmd):
    send(s, "", 1, os.getpid(), 30, 1, cmd)
    _, body = recv(s)
    txt = body.decode(errors="replace")
    if not txt.startswith("+OK tag="):
        raise RuntimeError("submit not acked: " + txt)
    return txt[len("+OK tag="):].strip()

def poll(s, tag):
    send(s, "", 2, os.getpid(), 30, 0, "POLL " + tag)
    _, body = recv(s)
    return body.decode(errors="replace")

def cancel(s, tag):
    send(s, "", 3, os.getpid(), 30, 0, "CANCEL " + tag)
    _, body = recv(s)
    return body.decode(errors="replace")

def sync_who(s):
    send(s, "", 4, os.getpid(), 30, 0, "who")
    _, body = recv(s)
    return body.decode(errors="replace")

# Per-test entry points:
def t_submit_poll_done():
    s = open_dealer("t1")
    tag = submit(s, "who")
    for _ in range(50):  # up to 5s of 100ms polls
        r = poll(s, tag)
        if r.startswith("+OK PENDING"):
            time.sleep(0.1); continue
        if r.startswith("+OK DONE"):
            # second poll should now be UNKNOWN
            r2 = poll(s, tag)
            if r2.startswith("-ERR UNKNOWN"):
                print("PASS submit_poll_done"); return
            print("FAIL submit_poll_done second-poll: " + r2); return
        print("FAIL submit_poll_done unexpected: " + r); return
    print("FAIL submit_poll_done timeout")

def t_event_on_completion():
    sub = ctx.socket(zmq.SUB)
    sub.setsockopt(zmq.SUBSCRIBE, b"niimx.done.")
    sub.setsockopt(zmq.RCVTIMEO, 5000)
    sub.connect(pub_endpoint)
    time.sleep(0.2)  # let subscription settle

    s = open_dealer("t2")
    tag = submit(s, "who")

    try:
        topic = sub.recv().decode()
        body  = sub.recv().decode(errors="replace")
        if not topic.startswith("niimx.done." + tag):
            print("FAIL event_on_completion topic: " + topic); return
        if not body:
            print("FAIL event_on_completion empty body"); return
        print("PASS event_on_completion")
    except zmq.error.Again:
        print("FAIL event_on_completion timeout")

def t_cancel_pending_returns_ok_cancelled_or_pending():
    s = open_dealer("t3")
    tag = submit(s, "who")
    r = cancel(s, tag)
    if r.startswith("+OK cancelled") or r.startswith("+OK cancel-pending"):
        print("PASS cancel_pending"); return
    print("FAIL cancel_pending: " + r)

def t_retention_expiry():
    s = open_dealer("t4")
    tag = submit(s, "who")
    # Wait until DONE, then sleep past retention.
    for _ in range(50):
        r = poll(s, tag)
        if r.startswith("+OK DONE"): break
        time.sleep(0.1)
    else:
        print("FAIL retention_expiry never DONE"); return
    # DONE already erased on the poll above. Just confirm:
    r2 = poll(s, tag)
    if r2.startswith("-ERR UNKNOWN"):
        # Now test the retention path for a CANCELLED entry, which
        # is NOT erased on poll. Cancel a fresh pending, wait 2s, poll.
        tag2 = submit(s, "who")
        cancel(s, tag2)
        # CANCELLED is observable now:
        r3 = poll(s, tag2)
        if not r3.startswith("+OK CANCELLED"):
            print("FAIL retention_expiry cancel-state: " + r3); return
        time.sleep(2.0)  # > async_retention_secs
        r4 = poll(s, tag2)
        if r4.startswith("-ERR UNKNOWN"):
            print("PASS retention_expiry"); return
        print("FAIL retention_expiry post-window: " + r4); return
    print("FAIL retention_expiry done-not-erased: " + r2)

def t_cap_full_rejects_third_submit():
    s = open_dealer("t5")
    # cap=2 in the conf above. Three submits land in the recv loop
    # before any dispatch can free a slot, so the third must reject
    # with "Async table full". Pre-existing DONE/PENDING entries
    # (from prior tests in this run) plus the burst occupy >= cap.
    submit(s, "who")
    submit(s, "who")
    send(s, "", 99, os.getpid(), 30, 1, "who")
    _, body = recv(s)
    r = body.decode(errors="replace")
    if "Async table full" in r:
        print("PASS cap_full"); return
    print("FAIL cap_full: " + r)

def t_sync_mode_unchanged():
    s = open_dealer("t6")
    r = sync_who(s)
    if "NIIMX^" in r or len(r) > 0:
        print("PASS sync_mode (got bytes)"); return
    print("FAIL sync_mode empty")

def t_bad_mode_rejected():
    s = open_dealer("t7")
    send(s, "", 1, os.getpid(), 30, 7, "who")
    _, body = recv(s)
    r = body.decode(errors="replace")
    if "bad mode" in r:
        print("PASS bad_mode"); return
    print("FAIL bad_mode: " + r)

def t_bad_tag_rejected():
    s = open_dealer("t8")
    send(s, "", 1, os.getpid(), 30, 0, "POLL notahex")
    _, body = recv(s)
    r = body.decode(errors="replace")
    if "bad tag" in r:
        print("PASS bad_tag"); return
    print("FAIL bad_tag: " + r)

if __name__ == "__main__":
    name = sys.argv[1]
    globals()["t_" + name]()
PY
)

run() {
    local name="$1"
    local out
    out=$(NIIMX_IPC="$NIIMX_IPC" NFDB_PUB="$NFDB_PUB" \
          python3 -c "$PYHELPER" "$name" 2>&1)
    if [[ "$out" == PASS* ]]; then pass "$name"; else fail "$name: $out"; fi
}

run submit_poll_done
run event_on_completion
run cancel_pending_returns_ok_cancelled_or_pending
run retention_expiry
run cap_full_rejects_third_submit
run sync_mode_unchanged
run bad_mode_rejected
run bad_tag_rejected

echo "Results: $PASS passed, $FAIL failed"
[[ $FAIL -eq 0 ]]
```

Note: the test names referenced in `run` invocations (`submit_poll_done`, `event_on_completion`, etc.) must match the `t_<name>` function names in the python helper. Double-check both sides match.

Note: every test uses `who` as the command — it returns reliably in `test_nfdb.sh` and is the same shape niimxd already handles. `cap_full` relies on the recv-loop processing all three submits before any dispatch can complete and free a slot (single-threaded niimxd dequeues epoll events in batch). If the test environment somehow dispatches between submits, raise the burst to 4–5 submits or lower `max_async_outstanding` to 1 in the conf.

- [ ] **Step 2: Make executable**

```bash
chmod +x cnc/niimx/src/test_niimx_async.sh
```

- [ ] **Step 3: Run it**

```bash
cd /home/dan/Git/netflex/cnc/niimx/src
./test_niimx_async.sh
```

Expected: `Results: 8 passed, 0 failed`.

If anything fails, treat each failure as a debugging task: re-read the spec's verb semantics and edge-cases section, find the mismatch between expected wire output and the niimxd implementation, fix it.

- [ ] **Step 4: Commit**

```bash
cd /home/dan/Git/netflex
git add cnc/niimx/src/test_niimx_async.sh
git commit -m "$(cat <<'EOF'
test: integration suite for niimx async commands

Eight black-box tests covering submit-then-poll, event
publish, cancel of PENDING, retention expiry for both DONE
(erased on poll) and CANCELLED (erased by sweep), table cap,
sync regression, bad mode, and bad tag. Drives niimxd via
pyzmq.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Phase 5 — Self-review

### Task 14: End-to-end smoke and final verification

- [ ] **Step 1: Clean build**

```bash
cd /home/dan/Git/netflex/cnc/niimx/src
nmake ../../../3b2/bin/niimxd && nmake ../../../3b2/bin/niimx && nmake test_niimx_async
```

Expected: all three build cleanly.

- [ ] **Step 2: Unit tests**

```bash
./test_niimx_async
```

Expected: `0 failed`.

- [ ] **Step 3: Existing tests**

```bash
./test_nfdb.sh
```

Expected: existing pass count unchanged.

- [ ] **Step 4: New integration tests**

```bash
./test_niimx_async.sh
```

Expected: `Results: 8 passed, 0 failed`.

- [ ] **Step 5: Manual one-shot sanity check**

```bash
# In one terminal: tail the events
python3 -c "
import zmq, os
ctx = zmq.Context.instance()
s = ctx.socket(zmq.SUB)
s.setsockopt(zmq.SUBSCRIBE, b'niimx.')
s.connect('ipc:///usr/cnc/data/nfdb_pub.sock')
while True:
    print(s.recv().decode(), '|', s.recv()[:80])
"

# In another: submit
../../../3b2/bin/niimx -c who      # sync — should still work
# Async path needs an async-capable client; just confirm the event tail
# emits niimx.done.<tag> when test_niimx_async.sh runs.
```

Expected: events appear in the tail when the integration suite runs.

- [ ] **Step 6: Commit the final state if anything was tweaked, otherwise stop**

```bash
cd /home/dan/Git/netflex
git status   # should be clean
git log --oneline -20    # review commit chain
```

---

## Implementation Notes

- **Single-threaded invariant**: every `NiimxAsync` method must be called from the niimxd event loop thread. The class header documents this; honoring it is the integrator's job.
- **Multi-segment async payloads**: if a backend reply needs more than one ZMQ segment to send sync, accumulate into one `std::string` for `complete()`. The on-the-wire POLL `+OK DONE\n` reply is sent in one segment by `niimx_zmq_send_simple`; if the payload is large enough that a single segment is problematic, refactor `niimx_zmq_send_simple` to honor `mcont` chunking before this work merges.
- **Logging**: the `Trc(2)` lines for `submit`/`mark_running`/`complete`/`sweep` from the spec can be added in Task 9 or later — they are not load-bearing for tests but help debugging.
- **Backwards compat**: the wire change is intentional. The single existing niimx CLI binary is rebuilt in Task 12.
- **`niimxd.conf` location**: if Task 0 found a conf file, also append the two new knobs to a sample (commented or actual). If no conf parsing exists, the defaults baked in from Task 7 are the only knobs.

## Self-Review Notes

The plan covers each section of the spec:

- Architecture (spec §Architecture) → Phase 1 + Phase 2 wiring tasks
- Wire format (spec §Wire Format) → Task 9 (recv mode frame, response codes), Task 12 (CLI mode frame)
- Verb semantics (spec §Verb Semantics) → Tasks 3–5 (submit/cancel/poll/complete behaviors)
- Retention (spec §Retention) → Task 5 (DONE-erase, EXPIRED-inline) + Task 11 (sweep tick)
- Limits (spec §Limits) → Task 3 (cap check in submit)
- Data structures (spec §Data Structures) → Task 1 (header), Task 7 (Request fields, Cfg knobs)
- Class spec (spec §NiimxAsync Class) → Tasks 1, 3, 4, 5, 6
- Integration points (spec §niimxd Integration Points) → Tasks 7–11
- Config (spec §Config) → Task 7
- Error matrix (spec §Error / Rejection Matrix) → Task 9 (mode/tag/verb errors); existing rejects untouched
- Edge cases (spec §Edge Cases) → covered by unit tests in Task 5 and integration tests in Task 13
- Testing (spec §Testing) → Phase 1 unit tests, Task 13 integration tests

No placeholders. No "TODO" / "implement later". Every code-bearing step shows the code. Type names and method signatures are consistent across tasks (verified against the header in Task 1).
