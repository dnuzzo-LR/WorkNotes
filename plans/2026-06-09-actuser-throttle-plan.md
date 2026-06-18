# ACT-USER Login Throttle — Spec & Implementation Plan

**Date:** 2026-06-09
**Owner:** dnuzzo@lightriver.com
**Status:** Draft

## 1. Problem

The TL1 `ACT-USER` command is issued by every `lan` process when establishing a session with a network element. When many sessions are restarted in close succession — link flap storms, mass topology re-discovery, service restart, etc. — the aggregate `ACT-USER` rate across all running `lan` processes on a host can overwhelm NEs (or their AAA back-ends), causing login DoS, account lockouts, or cascading failures.

A throttle is needed that enforces a maximum number of `ACT-USER` commands per minute **globally across all processes on the host**, with no daemon or central broker.

## 2. Goals / Non-Goals

### Goals
- Host-wide cap on the rate of `ACT-USER` TL1 commands sent.
- Self-contained: no broker, no refiller process, no external dependency.
- Survive a `lan` process being killed mid-throttle without permanently stalling the rest.
- Configurable rate via existing `system_defines` machinery.
- Drop-in for `lanalive.c` (telnet) call path. `clan.c` (SSH) is out of scope.
- **GNE logins only.** RT (Remote Terminal) logins ride on already-established GNE sessions and do not generate independent TACACS traffic. They bypass the throttle.
- Fail-open if the throttle subsystem itself errors — login must not be permanently blocked by a broken throttle.

### Non-Goals
- Per-NE or per-dtype scoping (single global bucket only — may be added later).
- Throttling commands other than `ACT-USER`.
- Cross-host coordination (one bucket per host is sufficient).
- Smooth/sliding-window rate limiting — fixed-window token bucket is acceptable.
- Persistent state across reboots — bucket starts fresh.

## 3. Design

### 3.1 Approach

A **shared mmap'd file** holds a fixed-window token bucket protected by a **robust process-shared pthread mutex**. Each `lan` process attaches the segment on first use. When a process needs to send `ACT-USER`, it acquires the mutex, checks the window, lazily refills the token count if the window has rolled, and either decrements the token count (proceed) or releases the mutex, sleeps until window-end, and retries.

The segment is a regular file at `/usr/cnc/data/ne_act_user_throttle` (not POSIX `shm_open`), so the on-disk path is fully under operator control rather than implicitly under `/dev/shm`. Backing-store performance is equivalent — both are page-cache-backed; the regular file is occasionally written back to its filesystem, which is irrelevant to the throttle's hot path.

No daemon, no separate refiller. The first arriving requester after a window expires performs the refill on the path of execution — analogous to lazy initialization.

### 3.2 Shared Memory Layout

```c
struct ne_throttle_shm {
    unsigned int     magic;          /* set last during init, sentinel for attach-race */
    int              version;        /* layout version (2) */
    pthread_mutex_t  lock;           /* PROCESS_SHARED + ROBUST */
    time_t           window_start;   /* wallclock seconds at start of current window */
    int              tokens;         /* tokens remaining in current window */
    int              max_per_window; /* configured max tokens per window */
    int              window_secs;    /* window length, seconds; ACTUSER_WINDOW_SECS at create time */
    unsigned long    acquire_count;  /* total successful acquires; drives periodic state dump */
};
```

- Backing file: `/usr/cnc/data/ne_act_user_throttle`
- File-lock sentinel for one-shot init: `/usr/cnc/tmp/ne_act_user_throttle.init`
- File mode `0660`, owned by `cmm:cmm` (created via `open(O_CREAT|O_EXCL)` then `fchown`).

### 3.3 Public API (`include/ne_throttle.h`)

```c
/**
 * @brief Block until permission is granted to send an ACT-USER TL1 command.
 *
 * Acquires one token from the host-wide ACT-USER rate bucket. If no tokens
 * are available in the current window, sleeps until the window rolls and
 * retries. Always returns 0 — failures in the throttle subsystem fail open
 * (the caller proceeds) so a broken throttle never blocks logins.
 *
 * Lazy initialization: the first call in a process attaches (and if needed,
 * creates) the shared segment.
 *
 * @return 0 always (fail-open).
 */
int  ne_actuser_throttle_acquire(void);

/**
 * @brief Detach from the shared segment.
 *
 * Optional. Safe to omit — the OS reclaims the mapping at exit. The shared
 * segment itself is never unlinked.
 */
void ne_actuser_throttle_detach(void);
```

The API is intentionally minimal: one call site change per chokepoint, no error handling required in the caller.

### 3.4 Algorithm — `acquire`

```
attach if not attached      (lazy init; fail-open on error)

loop:
    lock mutex
    if EOWNERDEAD:
        reset window_start = now, tokens = max
        pthread_mutex_consistent()

    now = time(NULL)
    elapsed = now - window_start
    if elapsed >= window_secs:
        window_start = now
        tokens = max_per_window
        TRACE D0: "window rolled; refilling tokens=N"

    if tokens > 0:
        tokens--
        acquire_count++
        if (acquire_count % NETT_STATE_DUMP_EVERY) == 0:
            TRACE D0: "state: acquires=N tokens=A/B elapsed=C/60 s pid=P"
        unlock
        return 0

    remaining_ms = (window_secs - elapsed) * 1000
    unlock
    TRACE D0: "bucket empty; sleeping <ms> for window roll"
    sleep(remaining_ms + per-pid jitter)
    repeat
```

Per-PID jitter (0–250 ms) on the sleep prevents a thundering-herd wake at the exact window boundary when many processes are simultaneously blocked.

### 3.5 Init Race

Multiple processes may try to create the segment simultaneously on a fresh host. Resolution:

1. `open(O_RDWR)` without `O_CREAT`.
2. On `ENOENT`: take `flock(LOCK_EX)` on the sentinel file, retry `open(O_RDWR)`. If still missing, create with `O_CREAT | O_EXCL`, `fchown` to `cmm:cmm`, `ftruncate`, `mmap`, initialize the structure, set `magic` last.
3. Release the flock.
4. Map the segment, then spin-wait (1 ms sleeps, 5 s cap) until `magic == MAGIC` for the case where another process is mid-init when we attached.

### 3.6 Robust Mutex

The shared mutex is initialized with both `PTHREAD_PROCESS_SHARED` and `PTHREAD_MUTEX_ROBUST`. If a process is killed while holding the lock:

- The next acquirer receives `EOWNERDEAD`.
- The acquirer resets `window_start = now` and `tokens = max_per_window` (safe over-approximation of state — the only invariant that matters is that the counter is non-negative and the window is current).
- The acquirer calls `pthread_mutex_consistent()` to clear the inconsistent state and continues.

No condition variables are used. Robust mutexes interact poorly with `pthread_cond_wait` (the cond can be left in an undefined state if the owner dies during wait). Polling with `nanosleep` is simpler and adequate.

### 3.7 Configuration

Two keys are read from `system_defines` on first attach:

```
ACTUSER_RATE_PER_WINDOW=30
ACTUSER_WINDOW_SECS=60
```

| Key | Meaning | Default | Bounds |
|-----|---------|---------|--------|
| `ACTUSER_RATE_PER_WINDOW` | Tokens per window | (unset → throttle disabled) | `> 0` enables |
| `ACTUSER_WINDOW_SECS` | Window length, seconds | `60` | clamped to `[1, 3600]` |

`ACTUSER_RATE_PER_WINDOW` is exactly what its name says: tokens per
`ACTUSER_WINDOW_SECS`. The effective per-minute rate (useful for
sizing against TACACS or AAA throughput caps) is
`ACTUSER_RATE_PER_WINDOW * (60 / ACTUSER_WINDOW_SECS)`.

Both values are read **once at segment creation**. Changing either at
runtime requires removing the segment file (`rm /usr/cnc/data/ne_act_user_throttle`)
and letting it be recreated by any subsequent acquire. This is
acceptable — config changes are rare and operator-initiated.

### 3.8 Trace Output

The throttle emits the following TRACE events. All operational events are at **D0** so the throttle's behavior is visible in production trace files without raising the global trace level. Verbose per-acquire detail stays at D1+.

| Level | Event | Where |
|-------|-------|-------|
| D0 | `attaching, rate=N/window, window=W s` | `attach()` entry; `W` reflects configured `ACTUSER_WINDOW_SECS` |
| D0 | `created shm <path>, tokens=N window=W s` | first creator after init |
| D0 | `joined existing shm <path>, max=N, tokens=T window=W s` | non-creator attach |
| D1 | `ACTUSER_RATE_PER_WINDOW unset or <=0; throttle disabled` | rate=0 path |
| D0 | `attach failed (<errno>); fail-open` | open/mmap error |
| D0 | `segment magic/version mismatch on <path>; fail-open. Remove <path> and retry.` | layout drift |
| D0 | `prior owner died; resetting window, refilling tokens=N` | EOWNERDEAD recovery |
| D0 | `lock unrecoverable; fail-open this call` | unrecoverable mutex error |
| D0 | `window rolled (elapsed=E); refilling tokens=N` | every ~W s |
| D0 | `bucket empty; sleeping <ms> for window roll` | every block |
| D0 | `state: acquires=N tokens=A/B elapsed=C/W s pid=P` | every `NETT_STATE_DUMP_EVERY` (50) acquires |
| D1 | `token taken; T remaining in window (elapsed=E s)` | every acquire |
| D3 | `acquire requested` / `disabled; acquire passes through` | per-call entry |

The periodic state dump (`state:`) gives ops a heartbeat on bucket occupancy without logging every acquire. At rate=5/window with the default 60 s window and `NETT_STATE_DUMP_EVERY=50` that's roughly one dump line every 10 minutes per host (independent of how many `lan` processes are present, since the counter is shared). The `window=` field in the attach/join/create lines reflects what's stored in the segment, so operators can confirm `ACTUSER_WINDOW_SECS` took effect.

## 4. Integration Points

### 4.1 lanalive.c

Single chokepoint at line **8356** (`comm_write(Message, node)`), which is the post-switch send for the login dispatch in `lan_login()`. Eight `sprintf` cases write `ACT-USER:...` into the shared `Message` buffer; other cases write `LGN-USER`, `RTRV-HDR`, `RTRV-NEHEALTH`, `ACT-SESSION`, `UTL:FRM...`, or `LOGIN:FRM...`.

Gate by **string prefix AND GNE/RT discriminator** at the chokepoint:

```c
if (strncmp(Message, "ACT-USER", 8) == 0) {
    if (gne) {
        TRACE(D2, "[ACTUSER_THROTTLE] gating ACT-USER for GNE node=%d\n", node);
        ne_actuser_throttle_acquire();
    } else {
        TRACE(D2, "[ACTUSER_THROTTLE] skip ACT-USER for RT node=%d\n", node);
    }
}

if (comm_write(Message, node) == FAILURE)
    return (FAILURE);
```

Two reasons for the dual condition:

1. **String prefix** — `lan_login()` dispatches by `dtype` to ~30 cases, only 8 of which produce `ACT-USER:...`. Other branches produce `LGN-USER`, `RTRV-HDR`, `RTRV-NEHEALTH`, `ACT-SESSION`, `UTL:FRM...`, `LOGIN:FRM...`. The prefix check throttles only the actual ACT-USER traffic without per-case duplication.
2. **GNE flag** — `lan_login(int gne, int node)` takes `gne` as a boolean flag (1 = logging in to a Gateway NE, 0 = logging in to a Remote Terminal). RT logins ride on an already-authenticated GNE session and don't independently hit the upstream TACACS / AAA back-end, so they are not the source of the #4381 DoS. Throttling them would only slow ring recovery. Callers confirm the semantic:
   - `lan_login(1, gne_id)` from L350 / L758 — GNE login
   - `lan_login(0, rt_index)` from L1716 — RT login
   The body of `lan_login()` also uses `if (gne)` to select between `ACT-USER:` and `RTRV-HDR:` formatters at the `CASE_DOUBLE_LOGIN` branch (L8251), confirming this is the canonical GNE/RT discriminator in this function.

This is the minimal patch: one insert at the post-switch send, no per-case duplication, no risk of missing a `case`.

### 4.3 Headers

`lanalive.c` adds:

```c
#include <ne_throttle.h>
```

## 5. Build System

### 5.1 New Source File

`cnc/utility/src/ne_actuser_throttle.c` — ~200 lines, implements the API.

### 5.2 util.mk

Add `ne_actuser_throttle.o` to the `OBJ_NELIB` list in `cnc/utility/src/util.mk` (around line 360, before `nelib_misc.o`). This places it in `libnelib.so`, which is already linked by every binary that calls into the lan login paths (`sdi`, `lan`, `lan_s`, `lan_min`, `nt_lan`, `lansim`, `xlansim`, `sam_lan`, `spin`, `g2in`, `g2out`, `lanstat`, per `cnc/sdi/src/sdisrc.mk`).

### 5.3 Header

`include/ne_throttle.h` — public API per §3.3.

### 5.4 Linker Flags

`libnelib.so` consumers need `-lpthread` (robust mutexes require pthreads). `-lrt` is **not** required — the implementation uses `open()`/`mmap()` on a regular file, not `shm_open()`. Verify each linker line in `sdisrc.mk` already includes `-lpthread`; on modern toolchains it almost always is.

## 6. Failure Modes

| Scenario | Behavior |
|----------|----------|
| `open` fails (permissions, `/usr/cnc/data` missing, disk full) | Fail-open: `acquire` returns 0, login proceeds without throttle. Log once per process. |
| Process killed while holding mutex | `EOWNERDEAD` returned to next acquirer; window reset and `pthread_mutex_consistent` called. |
| Window stuck due to clock jump (NTP) backward | Negative `elapsed` triggers immediate refill (same code path as expired window). |
| Window stuck due to clock jump forward | Window appears expired; immediate refill. No harm. |
| Pre-existing segment has wrong `version` / `magic` | Detached, log warning. Operator must `rm /usr/cnc/data/ne_act_user_throttle`. Treat as fail-open. |
| All processes blocked simultaneously | Sleep with per-PID jitter prevents synchronized wake; first to wake refills and proceeds. |

## 7. Testing Plan

### 7.1 Unit-level (standalone)
- Single process: `acquire` N+1 times within 1 minute. Verify first N return immediately, (N+1)th blocks ~remaining-window seconds, then returns.
- Two processes: combined rate respects the global cap.
- Kill process mid-acquire: next acquirer recovers via `EOWNERDEAD` without hanging.
- `rm /usr/cnc/data/ne_act_user_throttle` between runs: re-creates cleanly.
- Periodic state dump: after >= `NETT_STATE_DUMP_EVERY` total acquires, verify one `state: acquires=…` D0 line appears in trace.

### 7.2 Integration
- Restart cluster of `lan` processes against a real lab NE; confirm aggregated login rate matches `ACTUSER_RATE_PER_WINDOW`.

### 7.3 Regression
- Verify non-ACT-USER login dispatches (e.g., `LGN-USER`, `LOGIN:FRM`, `RTRV-HDR` cases) are NOT throttled — `strncmp` prefix check must be exact.

## 8. Implementation Plan (ordered)

1. **Create header** `include/ne_throttle.h` with Doxygen-documented prototypes and constants.
2. **Create source** `cnc/utility/src/ne_actuser_throttle.c` implementing the API per §3. Functions:
   - static `read_rate_config()`
   - static `init_shm_contents(struct ne_throttle_shm *)`
   - static `attach(void)`
   - static `lock_robust(void)`
   - public `ne_actuser_throttle_acquire(void)`
   - public `ne_actuser_throttle_detach(void)`
3. **Update `cnc/utility/src/util.mk`**: add `ne_actuser_throttle.o` to `OBJ_NELIB`.
4. **Patch `cnc/sdi/src/lanalive.c`**: add `#include <ne_throttle.h>`; insert `strncmp`-gated `ne_actuser_throttle_acquire()` before `comm_write` at line 8356.
5. **Verify linker flags** in `cnc/sdi/src/sdisrc.mk` — ensure `-lpthread` is present for all binaries that link `libnelib`.
6. **Build** `libnelib.so` and dependent binaries with `nmake`. Resolve any RHEL 7 / gcc 4.8.5 compatibility issues.
7. **Standalone test driver** (`cnc/utility/src/ne_actuser_throttle_test.c`) — not linked into a shipped binary; manual build target for verification per §7.1.
8. **Integration test** with `lansim` / lab NE per §7.2.
9. **Documentation** — add operator note to wherever `system_defines` values are documented, describing `ACTUSER_RATE_PER_WINDOW=`.

## 9. Open Questions

- **Default rate value — acceptable?** Implementation default is "throttle disabled" until `ACTUSER_RATE_PER_WINDOW` is set in `system_defines`. Suggested initial deploy: `30` tokens per default 60 s window. Operators may want lower for sensitive NEs. Field testing has used rate=5 to validate backpressure behavior.
- **File ownership / permissions** — all `lan` processes run as `cmm:cmm`; file and sentinel created mode 0660 owned by `cmm:cmm` via `fchown` after `open(O_CREAT|O_EXCL)`. Operators running a sibling tool under a different uid will get fail-open behavior.
- **State-dump interval** — currently `NETT_STATE_DUMP_EVERY = 50`. At rate=5/window with the default 60 s window that's one dump every ~10 min; at rate=30/window it's ~every 1.7 min. Tune if operators want a different cadence.
- **Future: per-NE-type buckets** — design leaves room (keyed file name, e.g. `/usr/cnc/data/ne_act_user_throttle_<dtype>`). Not in scope for v1.

## 10. References

- `cnc/sdi/src/lanalive.c:8356` — telnet login chokepoint
- `cnc/utility/src/util.mk:331-364` — `OBJ_NELIB` and `libnelib.so` build rule
- `cnc/sdi/src/sdisrc.mk` — `libnelib` link sites
- `mmap(2)`, `pthread_mutexattr_setrobust(3)` — shared mappings and robust-mutex semantics
