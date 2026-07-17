# ncland Multi-Process Group Support — Design

**Date:** 2026-07-17
**Author:** Dan Nuzzo (with Claude)
**Status:** Approved for planning
**Component:** `cnc/ncland/src`

## 1. Motivation

`ncland` today is a single process that manages every NE it can reach. A single
warehouse has `MAX_CONNS = 256` conn slots and one epoll loop. Sites with more
than ~256 concurrently-managed NEs, or sites that want to partition NEs by
device family for blast-radius reasons, cannot scale it horizontally.

This change lets an operator declare N *groups* of NEs in
`/usr/cnc/lib/data/ncland/ncland.json`; `ncland` then starts one child process
per group and routes inbound commands to the correct child.

## 2. Config schema

`/usr/cnc/lib/data/ncland/ncland.json` becomes:

```json
{
  "version": 1,
  "groups": [
    { "group": "Group One Description", "neid_range": [1, 500],    "dtypes": [206, 207] },
    { "group": "Group Two Description", "neid_range": [501, 1000], "dtypes": [223, 254] }
  ]
}
```

### 2.1 Rules (fatal on violation; parent exits non-zero, no children spawned)

- `version` == 1.
- `groups` is a non-empty array.
- Each entry has:
  - `group` — string, free-form description (only used in logs).
  - `neid_range` — 2-element int array `[start, end]`, `0 ≤ start ≤ end`.
    **Inclusive on both ends.**
  - `dtypes` — non-empty array of ints.
- No two groups' `neid_range` intervals overlap. (Dtype overlap between groups
  is fine — a dtype can appear in two disjoint neid ranges.)
- The legacy top-level `"dtypes"` key is **rejected** with an error telling the
  operator to migrate. No legacy fallback in v1.

### 2.2 New parser

Lives in `ncland_seed.cpp` next to (and replacing) `ncland_seed_load_filter`.
New types in `ncland.h`:

```c
typedef struct {
    std::string group_desc;   /* verbatim "group" field, for logs */
    int         neid_lo;      /* inclusive */
    int         neid_hi;      /* inclusive */
    std::unordered_set<int> dtypes;
} ncland_group_t;

int ncland_config_load_groups(const char *path,
                              std::vector<ncland_group_t> *out);
```

Return 0 on success, -1 on file/parse/schema/overlap error (with `LOG_ERROR`
describing which rule failed).

## 3. Process topology

```
                             ┌───────────────────────────┐
   clanfe ──dacs_msg───▶ /ncland_ctl (POSIX MQ)          │
                             │                            │
                             │   ncland (parent/router)   │
                             │   - reads ncland.json      │
                             │   - forks children         │
                             │   - forks nclan-seed once  │
                             │   - supervises SIGCHLD     │
                             │   - routes msg by neid     │
                             └────┬──────────────┬────────┘
                                  │              │
                mq_send /ncland_ctl_g0    mq_send /ncland_ctl_g1
                                  │              │
                     ┌────────────▼──┐   ┌───────▼────────┐
                     │ ncland child  │   │ ncland child   │
                     │ g0 (warehouse)│   │ g1 (warehouse) │
                     │ neid 1..500   │   │ neid 501..1000 │
                     │ dtypes 206,207│   │ dtypes 223,254 │
                     └───────────────┘   └────────────────┘
                          │                     │
                          └──── nfdb ZMQ SUB ───┘   (both subscribe; each filters by group)
                          └──── SysV screen msq ─┘  (qwrite to clanfe unchanged)
```

- **Parent** has no `conn` table, no NE fds, no ZMQ SUB.  It owns
  `/ncland_ctl`, a routing table `neid_range → child_mq_fd`, a signalfd, and
  a restart-backoff timerfd.
- **Child** is the existing warehouse process, with three changes:
  - Opens `/ncland_ctl_g<idx>` (not the fixed `/ncland_ctl`).
  - Applies a `neid_lo/neid_hi` filter in addition to the dtype allowlist.
  - Loads its config entry from `ncland.json` using `--group-index N`.

### 3.1 Invocation

- Human/systemd: `ncland [-l …] [-X]` — parent mode; parent reads config,
  forks children.
- Child (parent-launched): `ncland --group-index N [-l …] [-X]` — warehouse
  mode; child re-reads the same `ncland.json`, picks entry N, opens its own MQ.

Config source of truth remains the JSON file; the argv flag is a selector.

## 4. Command routing (parent)

Parent's main loop = `epoll_wait` on:

- `/ncland_ctl` (POSIX mq, `EPOLLIN`),
- signalfd (SIGTERM/SIGINT/SIGHUP/SIGCHLD),
- restart-backoff timerfd.

New file `ncland_router.cpp` with:

```c
typedef struct {
    int      idx;                    /* 0..N-1 */
    pid_t    pid;                    /* current child pid, -1 if not running */
    mqd_t    child_mq;               /* write-only handle to /ncland_ctl_g<idx> */
    char     child_mq_name[64];
    int      neid_lo, neid_hi;
    int      restart_attempts;
    time_t   started_at;             /* used to reset attempts after 60s uptime */
    time_t   next_restart_after;     /* 0 = restart immediately */
    ncland_group_t cfg;              /* keep the parsed group for restart */
} router_child_t;

typedef struct {
    mqd_t                       ctl_mqd;          /* /ncland_ctl inbound */
    std::vector<router_child_t> children;
    int                         epfd, sigfd, timerfd;
    time_t                      shutting_down;
} ncland_router_t;

int  router_init(ncland_router_t *r, const std::vector<ncland_group_t> &groups);
int  router_spawn_child(ncland_router_t *r, router_child_t *c);
void router_route_msg(ncland_router_t *r, const struct dacs_msg *m);
int  router_run(ncland_router_t *r);
void router_shutdown(ncland_router_t *r);
```

### 4.1 Routing rule (`router_route_msg`)

1. `neid = m->dm_dacsid` (same field the child's `warehouse_handle_dacs_msg`
   reads today).
2. Binary-search `children` sorted by `neid_lo`. Find the entry with
   `neid_lo ≤ neid ≤ neid_hi`. If none, drop with a rate-limited
   `LOG_WARN("route: neid=%d in no group; dropped")`.
3. `mq_send(c->child_mq, m, sizeof(*m), 0)` — non-blocking. On `EAGAIN`
   (child queue full) log a rate-limited warning and drop. `MSG_PRIO = 0`
   (matches current behavior).

Message bytes on the child MQ are byte-identical to `/ncland_ctl`; child code
path unchanged.

## 5. Child changes

### 5.1 Argv

`ncland.cpp::ncland_parse_args`: add `--group-index N` (via `getopt_long`).
Default `-1`.

- `-1` (default) → parent/router mode.
- `≥ 0` → warehouse mode for that group.

Add to `ncland_config_t`:

```c
int   group_index;         /* -1 = parent; ≥ 0 = child for that group */
int   neid_lo, neid_hi;    /* populated in child from ncland.json[group_index] */
char  mq_name[64];         /* "/ncland_ctl_g<idx>" in child */
```

### 5.2 `warehouse_init`

- Drop the call to `ncland_seed_load_filter` (retired).
- Call `ncland_config_load_groups(cfg->ncland_json_path, &groups)`.
- Pick entry `cfg->group_index`, populate:
  - `wh->dtype_allow` ← group's `dtypes` set,
  - `wh->neid_lo`, `wh->neid_hi` ← group's range.
- Pass `mq_name` (built as `"/ncland_ctl_g<idx>"`) to `ncland_mq_init`.

Add to `ncland_wh_t`:

```c
int neid_lo, neid_hi;      /* inclusive */
```

### 5.3 neid filter enforcement

Two places consult the range:

- **`seed_cb`** (`ncland_seed.cpp`): before the dtype check, drop if
  `neid ∉ [neid_lo, neid_hi]`.
- **`ncland_notify_dispatch`** (`ncland_notify.cpp`): same guard on the
  event's neid, applied to every event kind. Out-of-range CREATE/ENABLE/
  DTYPE_CHANGE → drop; DELETE/DISABLE/IP_CHANGE for an out-of-range neid →
  drop (nothing to close/change, we never opened it).

`warehouse_handle_dacs_msg` does *not* re-check the range: the router only
sends in-range neids. If a bug leaked one, the existing
`ncland_find_conn_by_neid` returns -1 and the message is dropped via the
existing `no session for neid=…` warning — safe fallback.

### 5.4 Seeder

Parent forks `nclan-seed` **once**, after all children have been spawned.
Because seed messages fly on the nfdb bus and every child subscribes,
duplicating the seeder per child would waste frame_link walks.

To avoid a race where seed messages arrive before a child's SUB has
connected, the parent waits 500 ms after the last successful
`router_spawn_child` before `execlp("nclan-seed", …)`. This is a known-
imperfect heuristic. If it proves flaky in the field, replace with an
explicit child-ready fd handshake (children write "READY\n" to a pipe the
parent reads before forking the seeder). Not built in v1.

## 6. Supervision, shutdown, errors

### 6.1 Supervision — parent SIGCHLD handler

1. `waitpid(-1, &status, WNOHANG)` in a loop.
2. For each reaped pid, look up which `router_child_t` owned it. If none
   (e.g. `nclan-seed`), reap and continue.
3. If parent is `shutting_down`, mark child `pid = -1` and continue (no
   restart).
4. Otherwise compute backoff:
   `min(30, 1 << min(restart_attempts, 5))` seconds → 1, 2, 4, 8, 16, 30, 30…
   Set `next_restart_after = now + backoff`; arm the `timerfd` if not
   already armed.
5. `LOG_ERROR("child g%d pid=%d exited status=0x%x; restart in %ds (attempt %d)")`.

Parent's timerfd handler: iterate children with
`next_restart_after ≤ now && pid == -1`, call `router_spawn_child`. On
successful fork, `restart_attempts++` and `next_restart_after = 0`. A child
that stays alive for ≥ 60 s (checked whenever the parent processes a message
or timer tick) resets `restart_attempts` to 0.

Repeated crashes never give up — matches the chosen "respawn with backoff"
behavior.

### 6.2 Shutdown

- Parent gets SIGTERM/SIGINT/SIGHUP → sets `shutting_down = time(NULL)`,
  forwards SIGTERM to every child, closes `/ncland_ctl` (new messages are
  dropped), sets `shutdown_deadline = now + SHUTDOWN_GRACE_S`.
- Main loop reaps children as they exit. If deadline passes with children
  still alive, parent SIGKILLs stragglers, waits for their SIGCHLD, exits.
- Existing child shutdown code (`warehouse_shutdown`, drain of `CS_READY`
  conns, per-conn `disconnect_steps`) runs unchanged.
- Each child unlinks its own `/ncland_ctl_g<idx>` on exit; parent unlinks
  `/ncland_ctl`.

### 6.3 Failure modes

| Failure | Handling |
|---|---|
| `ncland.json` missing / unparseable / schema invalid | Parent LOG_ERROR + exit non-zero. No children spawned. |
| Group overlap detected | Parent LOG_ERROR (both group descriptions + neid ranges) + exit non-zero. |
| `mq_open` on `/ncland_ctl_g<idx>` fails in child | Child exits non-zero. Parent's backoff kicks in — persistent failures visible in log. |
| `mq_send` to child MQ returns `EAGAIN` (queue full) | Parent drops the msg with rate-limited WARN. Same behavior clanfe sees today when the single-process daemon is overloaded. |
| Router receives a msg for neid outside every range | Parent drops with rate-limited WARN. |
| `nclan-seed` fails to exec | Parent WARNs (matches current behavior); NE inventory only populates via runtime notify events. |

## 7. Testing

- **`ncland_config_load_groups`** unit tests (mirror the existing
  `ncland_seed` tests in `ncland_unit_tests.cpp`): well-formed input, missing
  keys, wrong types, `neid_range` inversion, overlap between groups, empty
  dtypes, legacy top-level `dtypes` key rejection, wrong version.
- **`router_route_msg`** unit tests: neid in first/middle/last group; neid
  below/above/between ranges; EAGAIN path (`mq_send` returns `EAGAIN`, WARN
  emitted, msg dropped, router still healthy).
- **Backoff** unit test: inject a mockable `time()` (or a seam); simulate
  crashes; assert `next_restart_after` sequence is 1, 2, 4, 8, 16, 30, 30.
- **Uptime reset**: simulate a child alive for 60 s + crash; assert
  `restart_attempts` resets to 0 before backoff calc.
- **Integration test** (`cnc/ncland/src/test/integration/`): drop a two-group
  `ncland.json`, launch parent, assert two child processes exist with the
  expected argv, send a `dacs_msg` with a neid in group 1 via `mq_send`, and
  assert child 1's MQ received it and child 2's did not.

## 8. Non-goals (v1)

- Runtime reload of `groups[]` (SIGHUP still means graceful shutdown).
- Cross-group NE moves without a full parent restart.
- Per-group log files. All children still LOG to the same sink; log lines
  already carry pid.
- Multiple parent instances / single-instance lock. Same story as the
  current single-process daemon (systemd/monit owns the lock).

## 9. Files touched

New:

- `cnc/ncland/src/ncland_router.cpp`  — router process implementation.
- `cnc/ncland/src/ncland_router.h`    — router types + prototypes.

Modified:

- `cnc/ncland/src/ncland.h`
  - Add `ncland_group_t`, `ncland_config_load_groups` prototype.
  - Add `group_index`, `neid_lo`, `neid_hi`, `mq_name` to `ncland_config_t`.
  - Add `neid_lo`, `neid_hi` to `ncland_wh_t`.
- `cnc/ncland/src/ncland.cpp`
  - Long-opt parsing for `--group-index`.
  - `main()` branches: `group_index < 0` → `router_run`; else existing
    `warehouse_init` + `ncland_warehouse_run`.
  - Move the existing `nclan-seed` fork from `main()` into the router
    (`router_run`) after children start.
- `cnc/ncland/src/ncland_seed.cpp`
  - Replace `ncland_seed_load_filter` with `ncland_config_load_groups`.
  - `seed_cb` gains neid_lo/hi guard.
- `cnc/ncland/src/ncland_warehouse.cpp`
  - `warehouse_init`: load groups, pick entry by `group_index`, build
    `mq_name`, populate `neid_lo/hi`.
- `cnc/ncland/src/ncland_notify.cpp` (`ncland_notify_dispatch`)
  - neid_lo/hi guard.
- `cnc/ncland/src/ncland_unit_tests.cpp`
  - Config parser tests + router routing tests + backoff test.
- `cnc/ncland/src/Makefile`
  - Add `ncland_router.o` to the build.
