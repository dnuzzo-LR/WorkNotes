# ncland: IP-based Lua simulator startup (clan model)

**Date:** 2026-08-26
**Component:** `cnc/ncland/src`
**Status:** Design approved, ready for implementation plan

## Problem

The legacy `clan` process (`cnc/sdi/src/clan.c`) could, when an NE's address was a
loopback/simulator address, spawn a Lua-based `.clansim` simulator as a child
process and talk to it over a PTY instead of connecting to a real NE over the
network. The rewrite, `ncland`, detects simulator addresses but never starts a
simulator — the feature was dropped in the rewrite. This restores it, following
clan's mechanism.

Out of scope: the newer `clansimd` daemon (persistent, ZMQ-controlled) and its
`clansimctl` client. Those stay in the tree and keep building but are **on hold**;
ncland will not wire to them. This feature uses clan's fork-a-script model only.

## How legacy clan did it (reference)

- `is_ipaddr_sim(addr)` (clan.c:336) → true for `localhost`, `clansim`, `xlansim`,
  `127.0.0.1`.
- On a sim address, resolve a `.clansim` executable script in this order
  (clan.c:1589-1605), `access(path, R_OK|X_OK)`:
  1. `/usr/cnc/data/ne<neid>.clansim`
  2. `/usr/cnc/lib/clan/<dtype>.clansim`
  3. `/usr/cnc/lib/clan/def.<dtype>.clansim`
- Spawn: `inc_subproc(C.siminfo, simpath, "clansim", s_neid, s_tid, 0)`
  (clan.c:1613). `inc_subproc` (`cnc/utility/src/misc2.c:1040`) forks a child on a
  **PTY**, execs the script with argv `{"clansim", <neid>, <tid>}`, and returns
  `info[0]` = PTY master fd, `info[1]` = child pid.
- `s_tid = GET_TID(&Frmlnk[neid])` — `GET_TID` is `((f)->sys_name)`
  (`include/d1compool.h:1523`).
- clan then set `c->rfd = c->wfd = siminfo[0]` (clan.c:2972 and ~30 other call
  sites) and ran its **normal** login/command logic against that fd.
- **PTY mode:** clan does **no** `termios`/`cfmakeraw`/`tcsetattr` anywhere, and
  neither does `inc_subproc`. The PTY runs in its default line discipline —
  **cooked, echo on**. The `.clansim` scripts were written and tested against
  that. We match this exactly.
- Teardown: clan tracked the pid (`C.clansim`) and on shutdown did
  `kill(pid, SIGTERM)` + `waitpid` (clan.c:845-861).
- If no script resolved, clan fell back to forking a TCP `clansim` daemon
  (`start_clansim`, clan.c:637). **That fallback is dropped here** (it belongs to
  the on-hold clansimd path).

## Why this is a small change in ncland

ncland's downstream I/O is already transport-agnostic:

- `ncland_ne_read` / `ncland_ne_write` (`ncland_worker.cpp:38-71`) branch only on
  `c->chan != NULL` (SSH channel) vs. raw `read`/`write(c->net_fd)`. A PTY master
  is a plain fd, so with `chan == NULL` the raw path is used with **no worker
  changes**.
- The connect thread-pool completion handler
  (`ncland_warehouse.cpp:783-805`) is generic: install carrier into a conn-table
  slot → `epoll_ctl(ADD, net_fd)` → `ncland_login_start`. It works for any carrier
  whose connect callback produced `net_fd` set, `ssh`/`chan == NULL`,
  `state == CS_READY`.

So the sim rides the existing pool → completion → login → worker-I/O → teardown
machinery. Only the connect step and teardown-reap are new.

## Design

### 1. Trigger (`warehouse_open_conn_by_ne`, `ncland_warehouse.cpp:423`)

`ncland_addr_is_simulator(ip)` already exists (`ncland_warehouse.cpp:55`):
matches `*lansim*`, `127.x`, `::1`, `0.0.0.0`, `localhost`. Current behavior:

- `wh->skip_sim` (`-X`) && sim address → skip (drop, return -1). **Unchanged.**

New behavior when a sim address is seen and `-X` is **not** set:

- Resolve a `.clansim` script (see §3).
  - **No script found → drop the connect with an NF_D1 log and return -1.**
    (No telnet/ssh fall-through, no daemon fallback.)
  - Script found → build the carrier, set `c->sim = 1`, record the resolved
    `script_path` on the carrier, and **skip the es64-credential lookup**
    (`ncland_warehouse.cpp:480-527`) — the `.clansim` script presents its own
    login dialogue; no NE password applies. Submit to the connect pool exactly
    as a real NE (`connpool_submit`).

### 2. `conn_t` additions (`ncland.h:122`)

```c
int  sim;                  /**< 1 = this carrier/slot is a spawned .clansim child (PTY transport). */
int  sim_pid;              /**< PID of the .clansim child to reap; 0/-1 when none. */
char sim_script[256];      /**< Resolved .clansim path (for logging / re-exec). */
```

- `conn_init` (`ncland_conn.cpp:14`): zero all three (`sim_pid = 0`).
- `conn_reset` (`ncland_conn.cpp:27`): clear all three so a reused idle slot carries
  no stale sim state.

### 3. New file: `ncland_sim.cpp` (+ `ncland_sim.h`)

Component prefix `ncland_sim_`. Two functions plus the resolver.

```c
/** Resolve a .clansim script for (neid, dtype) using clan's search order.
 *  Returns 1 and fills out[] on hit; 0 on miss. */
int ncland_sim_resolve_script(int neid, int dtype, char *out, size_t outsz);

/** Fork+exec the resolved .clansim script on a PTY (via inc_subproc).
 *  On success: c->net_fd = PTY master (set O_NONBLOCK), c->sim_pid = child pid,
 *  c->ssh = NULL, c->chan = NULL, c->state = CS_READY. Returns 0 / -1.
 *  Matches clan exactly: NO termios changes (PTY stays cooked, echo on). */
int ncland_sim_connect(conn_t *c);

/** Reap the child: if sim_pid > 0, kill(SIGTERM) + waitpid; close net_fd;
 *  clear sim/sim_pid/net_fd. Safe on a non-sim or already-torn-down conn. */
void ncland_sim_disconnect(conn_t *c);
```

- `ncland_sim_resolve_script` replicates clan's three `access(R_OK|X_OK)` checks
  in order. Replicated locally (a few lines) rather than pulling
  `csim_resolve_script` out of the on-hold clansimd TU, keeping ncland independent
  of that code.
- `ncland_sim_connect` builds argv like clan — `inc_subproc(info, c->sim_script,
  "clansim", s_neid, s_tid, NULL)` — with `s_neid` = `"%d"` of `c->neid` and
  `s_tid` = `GET_TID(&Frmlnk[c->neid])` (include `d1compool.h`; `Frmlnk` is the
  existing warehouse extern). After `inc_subproc` returns 0, set `net_fd`
  non-blocking (`fcntl O_NONBLOCK`) so it lives in the warehouse epoll like every
  other fd. **No `tcsetattr`.** On `inc_subproc` failure, `state = CS_DISCONNECTED`,
  return -1.
- `ncland_sim_disconnect` mirrors clan's teardown (`kill` + `waitpid`); use a
  bounded reap (`waitpid` with `WNOHANG` retry or a short blocking wait) so a wedged
  child can't hang the warehouse.

### 4. Connect dispatch (`warehouse_connect_cb`, `ncland_warehouse.cpp:124`)

```c
static int warehouse_connect_cb(void *user, void *item) {
    (void)user;
    conn_t *c = (conn_t *)item;
    if (c->sim) return ncland_sim_connect(c);
    return (c->ssh_flag == 1) ? ncland_ssh_connect(c) : ncland_telnet_connect(c);
}
```

Everything after this — install, epoll-add, `login_start`, worker read/write,
prompt match, response delivery — is unchanged.

### 5. Teardown wiring (reap on every path)

Add `ncland_sim_disconnect(c)` alongside the existing ssh/telnet disconnects in
both teardown paths, so the child is always reaped:

- `conn_free` (`ncland_conn.cpp:82`) — normal slot teardown (drop, shutdown drain).
- `warehouse_carrier_free` (`ncland_warehouse.cpp:162`) — carrier discarded on
  connect failure or table-full.

`ncland_sim_disconnect` must be a no-op when `sim_pid <= 0`, so calling it on
non-sim conns is harmless.

### 6. Build (`cnc/ncland/src/Makefile`)

- Add `ncland_sim.o` to `NCLAND_OBJS` (picks it up for `ncland` and the unit-test
  target automatically).
- `inc_subproc` lives in `cnc/utility/src/misc2.c`; ncland already links `-linc`
  and `$(UTILLIB)` (the same libs clan resolves it from), so **no new link flag is
  expected**. Verify at first link; if unresolved, add the carrying lib.
- Compiled with the existing C++17 `%.o : %.cpp` rule. This is an nmake
  (Lucent/AT&T) Makefile — use the `writing-nmake-makefiles` skill for the edit.

## Threaded-fork risk (accepted, documented)

`warehouse_connect_cb` runs on a `connpool` worker thread. `inc_subproc` calls
`fdopen`/`trace` (which allocate / touch stdio locks) **between `fork` and
`execv`**. In a multithreaded process this is technically unsafe: if another
thread holds the malloc arena or an stdio lock at the instant of `fork`, the child
can deadlock before `execv`.

Decision: **accept it** — this is clan's exact mechanism (the stated requirement),
the window is tiny (`execv` fires almost immediately), and pool threads are
normally blocked in `recv`, not in `malloc`. Legacy clan was single-threaded so
never hit this.

Escape hatch (only if it ever manifests): replace the `inc_subproc` call with a
purpose-built PTY spawner that does **only** async-signal-safe calls between
`fork` and `execv` (`open` ptsname, `dup2`, `execv` — no `fdopen`/`trace`). Not
implemented now.

## Testing

- **Unit (ncland_unit_tests):**
  - `ncland_sim_resolve_script` — hits each of the three paths in order and misses
    cleanly when none exist (use a temp dir / override root if the hardcoded
    `/usr/cnc/...` paths block testing; otherwise cover the ordering logic with a
    seam).
  - `ncland_sim_disconnect` — no-op when `sim_pid <= 0`; reaps a real short-lived
    child (fork a `sleep`/`/bin/true`, confirm `waitpid` succeeds, fd closed).
  - `conn_reset`/`conn_init` clear `sim`, `sim_pid`, `sim_script`.
- **Integration (manual, on a box with a real `.clansim`):**
  - Seed an NE whose address is a sim address (e.g. `127.0.0.1` or `xlansim`) with
    a matching `.clansim` script present → confirm child spawns, transport comes
    up, a TL1 command round-trips, and teardown reaps the child (no zombie).
  - Same NE with **no** script present → confirm the connect is dropped with the
    NF_D1 log and no child is spawned.
  - Confirm `-X` still skips sim addresses entirely (regression).
  - Watch for echo bleed in the login dialogue (cooked PTY). If the login Lua
    matches its own echoed command, that is the known cooked-mode risk; the
    minimal fix is clearing `ECHO` only — but per decision we ship cooked to match
    clan and only revisit if observed.

## Files touched

| File | Change |
|------|--------|
| `ncland.h` | add `sim`, `sim_pid`, `sim_script[]` to `conn_t` |
| `ncland_sim.cpp` (new) | resolver + `ncland_sim_connect` + `ncland_sim_disconnect` |
| `ncland_sim.h` (new) | prototypes |
| `ncland_conn.cpp` | init/reset the new fields |
| `ncland_warehouse.cpp` | sim branch in `open_conn_by_ne`; sim dispatch in `connect_cb`; reap in `carrier_free` |
| `ncland_conn.cpp` (`conn_free`) | reap via `ncland_sim_disconnect` |
| `Makefile` | add `ncland_sim.o` to `NCLAND_OBJS` |
