# niimx Async Commands — Design

**Date:** 2026-05-20
**Component:** `cnc/niimx` (`niimxd`, niimx ZMQ protocol)
**Status:** Design — pending review

## Goal

Add an asynchronous command mode to the niimx ZMQ protocol. Today the protocol is strictly synchronous: a client sends a command and blocks on the response (or its `tmout` expires). The new mode lets a client submit a command, receive an opaque tag immediately, and either poll for the result by tag or subscribe to a completion event published on `nfdb_pub.sock`.

The existing synchronous flow remains the default. Async is opt-in via a new request frame.

## Non-Goals

- No authentication or ACL on POLL/CANCEL (single-process trust model).
- No persistent tag store. Tags die with the daemon.
- No multi-process tag namespace (single `niimxd`).
- No streaming of partial results. State transitions are binary: PENDING → DONE/CANCELLED.
- No back-compat layer for old 6-frame clients. The only existing niimx client is not in production use; it will be updated to send the new mode frame.

## Architecture

```
┌─────────┐  ROUTER (niimx.ipc)              ┌──────────────────┐
│ niimx   │ ───────────────────────────────► │ niimxd           │
│ CLI /   │   mode=1 + cmd  → SUBMIT         │                  │
│ library │   mode=0 + "POLL <tag>"          │  niimx_handle_   │
│         │   mode=0 + "CANCEL <tag>"        │  zmq()           │
│         │ ◄─────────────────────────────── │      │           │
└─────────┘   sync reply / tag / poll-result │      ▼           │
     │                                       │  NiimxAsync      │
     │   SUB on nfdb_pub.sock                │  (new module)    │
     │ ◄───────────────────────────────────  │   • table        │
     │   topic="niimx.done.<tag>"            │   • cap          │
     │   body =full payload                  │   • sweep        │
     │                                       │      │           │
     │                                       │      ▼           │
     │                                       │  bchannel pool   │
     │                                       │  (unchanged)     │
     │                                       └──────────────────┘
```

A new module `cnc/niimx/src/niimx_async.{h,cpp}` owns the async tag table and provides `submit`, `mark_running`, `complete`, `poll`, `cancel`, and `sweep` operations. `niimxd` integrates the module through a small number of hook points (parsing the mode frame, marking running, branching the reply path, ticking sweep).

Completion events are published on the existing `nfdb_pub.sock` XPUB socket — already bound by `niimxd` post-merge with `nfd` — under the topic `niimx.done.<tag>` (or `niimx.cancelled.<tag>`).

## Wire Format

### Request frames (ROUTER side, after ZMQ identity and empty delimiter)

| # | Frame   | Size | Notes                                                |
|---|---------|------|------------------------------------------------------|
| 1 | host    | var  | Unchanged.                                           |
| 2 | msgid   | 4    | Unchanged (client correlation, not the async tag).   |
| 3 | mpid    | 4    | Unchanged.                                           |
| 4 | tmout   | 4    | Unchanged.                                           |
| 5 | **mode**| **4**| **NEW.** `0` = sync (legacy semantics), `1` = async-submit. |
| 6 | command | var  | Unchanged.                                           |

The mode frame is **required**. There is no fallback to a 6-frame request; malformed input is rejected with `-ERR bad mode`.

### Response frames

Response framing is unchanged: `empty | msgid(4) | mcont(4) | payload`. The payload content differs by verb:

- **SUBMIT (mode=1) ack** — sent immediately, single segment (`mcont=0`):
  - Success: `+OK tag=<16-hex>\n`
  - Reject: `NIIMX^...` for legacy congestion/host rejects (preserves wording), `-ERR <reason>` for new rejects (e.g., `NIIMX^Async table full`).
- **POLL response**:
  - `+OK PENDING\n`
  - `+OK DONE\n` followed by the stored payload bytes (multi-segment if large; existing `mcont` semantics apply)
  - `+OK CANCELLED\n`
  - `+OK EXPIRED\n`
  - `-ERR UNKNOWN tag\n`
- **CANCEL response**:
  - `+OK cancelled\n`
  - `+OK cancel-pending\n`
  - `-ERR already-done\n`
  - `-ERR UNKNOWN tag\n`

### Event frames on `nfdb_pub.sock`

Two-frame, matching `nfdb_event_publish` shape:

1. Topic: `niimx.done.<16-hex-tag>` or `niimx.cancelled.<16-hex-tag>`
2. Body: full payload bytes (exactly what POLL would return after `+OK DONE\n`, without the status prefix)

Subscribers can prefix-match `niimx.done.` or exact-match a tag they are tracking.

### Tag format

Tags are 64-bit unsigned integers, rendered as 16-character lowercase hex. The generator is seeded at startup as `(time(NULL) << 32) | random32()` and increments monotonically per `submit`. Range exhaustion is not a concern.

## Verb Semantics

### SUBMIT (mode=1, command = the real backend command)

1. `niimx_handle_zmq` parses the 7-frame request.
2. Existing rejects (meltdown, queue full, unknown host, host unavailable) still run first.
3. `G.async->submit(...)` allocates a tag and stores an entry in state `PENDING`. If the table is at `max_async_outstanding`, it returns `0` and SUBMIT replies `NIIMX^Async table full`.
4. niimxd sends an immediate `+OK tag=<hex>` response on the original DEALER↔ROUTER pair.
5. The original request, now flagged async with its tag, is appended to `G.requestQ` as usual.
6. When `niimx_dispatch_requests` pulls it for a bchannel, it calls `G.async->mark_running(tag)`.
7. When the backend reply arrives, the existing reply site branches: async requests go to `G.async->complete(tag, payload)` instead of `niimx_zmq_send_response`. `complete` stores the payload and publishes the two-frame event.

### POLL (mode=0, command = `POLL <hex-tag>`)

Handled inline in `niimx_handle_zmq`. POLL is never enqueued in `requestQ`. `G.async->poll(tag, &payload_out)` returns one of the enum states; the response is sent immediately.

**DONE is delivered once.** When `poll()` finds an entry in state `DONE`, it returns the payload and erases the entry inline. A subsequent POLL of the same tag returns `-ERR UNKNOWN tag`. This bounds memory tightly under the normal "submit, poll until done" client pattern: the retention window only governs entries that the client never polls (or polls only while PENDING).

`PENDING`/`RUNNING` POLLs do not erase the entry. `CANCELLED` POLLs do not erase the entry — it remains until the retention window elapses, so a client that issued CANCEL and is subscribing to the cancelled event has a chance to confirm by POLL as well.

### CANCEL (mode=0, command = `CANCEL <hex-tag>`)

Handled inline.

- If the entry is `PENDING` and the matching Request is still in `requestQ`: `cancel()` returns `CANCELLED_NOW`; the caller scans `requestQ` to erase the request; the entry transitions to `CANCELLED` (kept around until retention).
- If the entry is `RUNNING` on a bchannel: `cancel()` sets `discard_on_reply` and returns `CANCEL_PENDING`. When the backend reply arrives, `complete()` discards the payload and publishes `niimx.cancelled.<tag>` so subscribers see closure.
- If the entry is `DONE` or `CANCELLED`: returns `ALREADY_DONE`.
- If the tag is unknown or already swept: returns `NOT_FOUND`.

### Retention

The retention window is an *upper bound* on how long a completed entry persists. There are three paths to entry removal:

1. **First DONE-returning POLL** erases the entry inline (see POLL above). This is the normal path.
2. **First EXPIRED-returning POLL** erases the entry inline (the window elapsed and the client polled before sweep ran).
3. **Sweep** erases any `DONE` or `CANCELLED` entry whose `completed_at + async_retention_secs < now`.

`CANCELLED` entries are *not* erased on POLL; they live out the full retention window so the cancelling client can observe terminal state via either POLL or the published event. `PENDING` and `RUNNING` entries are never erased by sweep.

Subscribers that miss the completion event have no replay path. After the entry is gone, POLL returns `-ERR UNKNOWN tag`. Clients should treat EXPIRED and UNKNOWN equivalently as "result no longer available."

### Verb syntax

POLL and CANCEL verbs are matched case-sensitively against uppercase `POLL ` and `CANCEL ` prefixes (with the trailing space). The tag argument is exactly 16 lowercase hex characters. Whitespace beyond the single separator is not tolerated. A malformed tag (wrong length, non-hex) returns `-ERR bad tag`.

### Limits

`max_async_outstanding` caps the table size globally. There is no per-client cap; the single trusted client model makes this unnecessary.

## Data Structures

```c
typedef enum {
    NIIMX_ASYNC_PENDING,    /**< Submitted, in RequestQ or dispatched. */
    NIIMX_ASYNC_RUNNING,    /**< Dispatched to backend, awaiting reply. */
    NIIMX_ASYNC_DONE,       /**< Backend reply received and stored. */
    NIIMX_ASYNC_CANCELLED,  /**< Cancelled before or during execution. */
    NIIMX_ASYNC_EXPIRED     /**< Transient: marked by poll() when it
                                 finds completed_at + retention_secs has
                                 passed but sweep has not yet run. The
                                 entry is erased before poll() returns.
                                 Never observed by a second poll() of
                                 the same tag. */
} niimx_async_state_t;

/**
 * @brief One outstanding async command tracked by NiimxAsync.
 */
struct niimx_async_entry_t {
    uint64_t            tag;              /**< 64-bit monotonic id. */
    niimx_async_state_t state;            /**< Current state. */
    std::string         identity;         /**< ZMQ identity of the submitter. */
    int32_t             msgid;            /**< Original client msgid. */
    std::string         host;             /**< Target host. */
    std::string         command;          /**< Original command string. */
    std::string         payload;          /**< Filled on completion. */
    Clock::time_point   submitted_at;
    Clock::time_point   completed_at;     /**< Set on DONE/CANCELLED. */
    bool                discard_on_reply; /**< Set by cancel() when RUNNING. */
};
```

`Request` (in `niimxd.cpp`) gains:

```c
uint64_t async_tag;  /**< 0 = sync request, non-zero = parked in g_async. */
bool     async;      /**< Convenience flag = (async_tag != 0). */
```

`NiimxGlobalState` gains:

```c
NiimxAsync *async;  /**< Owns the async table. */
```

## `NiimxAsync` Class

```c
class NiimxAsync {
public:
    /**
     * @brief Construct.
     * @param xpub_sock        nfdb XPUB socket to publish on (nfdb_g_xpub).
     * @param max_outstanding  Global cap; 0 = unlimited.
     * @param retention_secs   Retention window for DONE/CANCELLED entries.
     */
    NiimxAsync(void *xpub_sock, size_t max_outstanding, int retention_secs);

    /** @return new tag on success, 0 if at cap. */
    uint64_t submit(const std::string &identity, int32_t msgid,
                    const std::string &host, const std::string &command);

    /** @brief Move tag to RUNNING when its Request is dispatched. */
    void mark_running(uint64_t tag);

    /**
     * @brief Store backend reply and publish event.
     *
     * If the entry was cancel-pending, payload is discarded and a
     * cancelled event fires (subscribers always see a terminal state).
     */
    void complete(uint64_t tag, const std::string &payload);

    /**
     * @brief Read status + payload.
     *
     * Side effects:
     *   - DONE     : payload copied to *payload_out, entry erased before return.
     *   - EXPIRED  : entry erased before return.
     *   - PENDING/RUNNING/CANCELLED: entry left in place.
     *
     * A second poll() of a tag that just returned DONE or EXPIRED will
     * find no entry and return a sentinel state meaning UNKNOWN. The
     * caller maps that to "-ERR UNKNOWN tag".
     */
    niimx_async_state_t poll(uint64_t tag, std::string *payload_out);

    enum cancel_result_t { CANCELLED_NOW, CANCEL_PENDING, NOT_FOUND, ALREADY_DONE };

    /** @brief Cancel by tag. Caller is responsible for evicting the
     *         matching Request from RequestQ when CANCELLED_NOW. */
    cancel_result_t cancel(uint64_t tag);

    /** @brief Erase entries past retention. Called from the 1-Hz tick. */
    void sweep(Clock::time_point now);

    size_t size() const { return table_.size(); }

private:
    void publish_event(uint64_t tag, const std::string &payload,
                       const char *suffix /* "done" or "cancelled" */);

    void                                       *xpub_;
    size_t                                      cap_;
    int                                         retention_secs_;
    uint64_t                                    next_tag_;
    std::map<uint64_t, niimx_async_entry_t>     table_;
};
```

`std::map` is used (not `unordered_map`) for deterministic iteration during sweep/debug dumps. The table is bounded by `max_async_outstanding`; log-N lookup is fine.

The class is single-threaded by contract — same invariant the rest of `niimxd` already relies on. No internal locking.

## niimxd Integration Points

About 35 lines of glue across `niimxd.cpp`.

- **Startup**: after `nfdb_g_xpub` is bound, `G.async = new NiimxAsync(nfdb_g_xpub, Cfg.max_async_outstanding, Cfg.async_retention_secs);`
- **Recv** (`niimx_handle_zmq`, ~line 956): receive the new 5th frame `mode`, branch on `(mode, command_prefix)`:
  - `mode==0, "POLL ..."` → POLL handler, immediate reply
  - `mode==0, "CANCEL ..."` → CANCEL handler, immediate reply
  - `mode==0, other` → existing sync enqueue
  - `mode==1, "POLL ..."` or `"CANCEL ..."` → `-ERR mode mismatch`
  - `mode==1, other` → SUBMIT path
  - else → `-ERR bad mode`
- **SUBMIT**: existing pre-enqueue rejects run first; on accept, allocate tag, immediate ack, enqueue Request with `async=true, async_tag=tag`.
- **Dispatch** (`niimx_dispatch_requests`, ~line 1928): `if (req.async) G.async->mark_running(req.async_tag);`
- **Reply site**: `if (req.async) G.async->complete(req.async_tag, payload); else niimx_zmq_send_response(...);`
- **1-Hz tick**: `G.async->sweep(Clock::now());`
- **Shutdown**: `delete G.async; G.async = nullptr;` after XPUB close.

## Config

Two new knobs in `niimxd.conf`, loaded into `Cfg` alongside `max_iq` / `reject`:

```
max_async_outstanding  1024     # global cap; 0 = unlimited
async_retention_secs   300      # retention window for DONE/CANCELLED entries
```

## Error / Rejection Matrix

| Condition                                    | Response                                                |
|----------------------------------------------|---------------------------------------------------------|
| `SystemUtilization >= reject`                | `NIIMX^System congestion: Total Meltdown — command not processed` |
| `requestQ` full                              | `NIIMX^System congestion: request queue full`           |
| Host unknown                                 | `NIIMX^Unknown host: <h>`                               |
| Host unavailable                             | `NIIMX^Host unavailable: <h>`                           |
| Async table at cap                           | `NIIMX^Async table full`                                |
| Malformed mode frame (non-integer / OOR)     | `-ERR bad mode`                                         |
| mode=1 with command starting `POLL`/`CANCEL` | `-ERR mode mismatch`                                    |
| Malformed POLL/CANCEL tag (length / non-hex) | `-ERR bad tag`                                          |
| POLL of unknown tag                          | `-ERR UNKNOWN tag`                                      |
| CANCEL of unknown tag                        | `-ERR UNKNOWN tag`                                      |
| CANCEL of already-terminal tag               | `-ERR already-done`                                     |

Legacy rejects keep their existing `NIIMX^` prefix because callers grep for it. New rejects use `-ERR` since no caller exists yet.

## Edge Cases

- **Backend reply for a CANCELLED tag**: `complete()` sees `state == CANCELLED` or `discard_on_reply == true`; payload is discarded, state transitions to `CANCELLED`, and `niimx.cancelled.<tag>` is published.
- **Backend reply for a swept tag**: `complete()` finds no entry; log at `Trc(1)` and drop. Only happens if `async_retention_secs < tmout`, which is an operator config error.
- **POLL races with completion**: single-threaded event loop guarantees no interleaving; no locks needed. This invariant is documented in the class header.
- **POLL before SUBMIT ack reaches the client**: the client must wait for the SUBMIT ack before polling. Polling an unknown tag returns `-ERR UNKNOWN tag`.
- **Second POLL after DONE**: the entry is erased on the DONE-returning POLL. A second POLL returns `-ERR UNKNOWN tag`. Clients that need the payload more than once must capture it from the first DONE response (or subscribe to the completion event).
- **POLL after CANCEL**: returns `+OK CANCELLED` (entry not erased). The entry persists until retention window elapses, then sweep removes it. Repeated POLLs during the window all return `+OK CANCELLED`.
- **CANCEL after CANCEL**: returns `-ERR already-done`.
- **Daemon restart**: all in-flight tags are lost. Subscribers receive nothing. Clients must re-submit. Matches existing `RequestQ` semantics.
- **Large payloads**: stored once in `entry.payload`; ZMQ XPUB fan-out handles per-subscriber copies. POLL returns the same buffer.
- **Backend timeout for an async request**: the existing tmout fires a `NIIMX^Timeout` payload. For async, that string flows through `complete()` like any other payload. Subscribers see `niimx.done.<tag>` with body `NIIMX^Timeout`. The *async tag* is DONE; the *backend response* is an error string. Clients interpret the body.

## Tracing

Add `Trc(2)` lines at:

- `submit` — tag and a 40-char snippet of command
- `mark_running` — tag
- `complete` — tag, payload size, and whether it was discarded
- `cancel` — tag and result
- `sweep` — count of entries erased

## Testing

### Unit tests — `cnc/niimx/src/test_niimx_async.cpp`

Construct `NiimxAsync` with a stub publish function (capture topic+payload into a vector). The class header exposes a `publish_fn` typedef so tests can inject without ZMQ. Drive:

1. `submit` returns a non-zero tag; state is `PENDING`; `size() == 1`.
2. With `cap=2`: the third `submit` returns `0`.
3. `mark_running` transitions to `RUNNING`.
4. `complete` transitions to `DONE`, stores payload, and the publish stub is called exactly once with topic `niimx.done.<hex>` and the full payload.
5. First `poll` returns `DONE` + payload; second `poll` of the same tag returns `UNKNOWN` (entry erased on the DONE-returning poll). Verify `size() == 0` after.
5a. Submit, complete, advance clock past retention without polling, run `sweep`; `poll` returns `UNKNOWN`. Submit again, complete, advance clock past retention, then `poll` *before* the next sweep; verify `poll` returns `EXPIRED` and erases the entry; the next `poll` returns `UNKNOWN`.
6. `cancel` of `PENDING` returns `CANCELLED_NOW`; entry remains until retention; repeated `poll` calls within the window all return `CANCELLED` and do not erase the entry; `sweep` past retention erases it; subsequent `poll` returns `UNKNOWN`.
7. `cancel` of `RUNNING` returns `CANCEL_PENDING`; subsequent `complete` discards the payload and publishes `niimx.cancelled.<hex>`.
8. `cancel` after `DONE` returns `ALREADY_DONE`.
9. `cancel` of an unknown tag returns `NOT_FOUND`.
10. `sweep` erases `DONE`/`CANCELLED` past retention; never erases `PENDING`/`RUNNING`.

### Integration test — extend `test_nfdb.sh` or new `test_niimx_async.sh`

End-to-end against a running `niimxd`:

1. Start `niimxd`, capture pid.
2. With a small ZMQ test client, send a `mode=1` request for `who` against local host. Read ack, parse tag.
3. POLL the tag in a 100 ms loop; expect zero or more `PENDING` then exactly one `DONE` + payload. A subsequent POLL of the same tag must return `-ERR UNKNOWN tag`.
4. Subscribe to `nfdb_pub.sock` on a second connection before SUBMIT; verify `niimx.done.<tag>` arrives with the same payload.
5. Submit a long-running cmd, immediately CANCEL; verify `+OK cancelled` (or `+OK cancel-pending`) and a `niimx.cancelled.<tag>` event.
6. Submit, wait past retention, POLL; expect `EXPIRED`.
7. Run with `max_async_outstanding=2`; the third submit must get `NIIMX^Async table full`.
8. Send a `mode=0` request; verify unchanged behavior (regression sanity).

The harness adds entries under a `# --- async tests ---` block in the existing `test_nfdb.sh` pass/fail counter.

## File Map

**New:**
- `cnc/niimx/src/niimx_async.h`
- `cnc/niimx/src/niimx_async.cpp`
- `cnc/niimx/src/test_niimx_async.cpp`

**Modified:**
- `cnc/niimx/src/niimxd.cpp` — ~35 lines across startup, recv, dispatch, reply, tick, shutdown; two new fields on `Request`.
- `cnc/niimx/src/Makefile` — link `niimx_async.o` into `niimxd`; add `test_niimx_async` target.
- `cnc/niimx/src/test_nfdb.sh` (or new `test_niimx_async.sh`) — integration test block.
- `niimxd.conf` (sample/template) — two new knobs.

**Also modified (forced by wire change):**
- `cnc/niimx/src/niimx.cpp` (the CLI) — gains a `mode` frame in every request it sends. The CLI continues to expose sync-only behavior for now (always sends `mode=0`); async submit/poll/cancel is not exposed as CLI options in this work. The single existing caller is acceptable to break per project owner; only one binary exists and it is not yet in production use.

**Unchanged:**
- All `nfdb_*` files.

## Out of Scope

- Authentication / ACL on POLL/CANCEL.
- Persistent tag store.
- Multi-process tag namespace.
- Streaming partial results.

## Open Questions

None at this time. All wire format, semantics, retention, limits, and integration decisions are settled.
