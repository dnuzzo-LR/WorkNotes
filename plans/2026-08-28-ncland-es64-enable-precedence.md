# ncland es64 enable-precedence Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.
>
> **Makefile note:** This project uses Lucent/AT&T **nmake**. Any Makefile edit MUST use the `writing-nmake-makefiles` skill. Do not assume GNU make.
>
> **Build env note:** nclan-seed / ncland_unit_tests link `-lssh -lyaml-cpp` etc.; a 64-bit corelinux env is required (`L64_SETUP=1` and a matching `VPATH` inc dir per project setup). If the link fails on `-lssh`/`-lyaml-cpp`, stop and confirm the build env rather than hacking paths.

**Goal:** Make `es64_snmp_info.enabled` the sole authority for an NE slot's up/down decision in nclan-seed whenever an es64 record is present, overriding `frame_link.disable` in both directions.

**Architecture:** A single pure decision helper encodes the precedence rule. Both publish paths — the startup seed walk (`nclan_seed.cpp`) and the live watch read (`nclan_seed_read.cpp`) — feed it the data they already have and act on its verdict. One rule, one place, so the two tables can never re-diverge in behavior.

**Tech Stack:** C++ (gcc 4.8.5 baseline), Lucent nmake, `nfunit-test.hpp` unit framework, es64 ISAM (`alcatel_es64_db.h`), `frame_link` shared memory, nfdb/ZMQ publish.

---

## The precedence rule

For one `(neid, slot)`:

```
es64 record found (GOT_ONE) AND del_flag == 0  ->  enabled = es64_snmp_info.enabled
otherwise                                       ->  enabled = fallback (!frame_link.disable, or PG mirror)
```

Non-es64 dtypes never have an es64 record, so they fall through to frame_link automatically.

## File structure

- **Create** `cnc/ncland/src/nclan_seed_enable.h` — helper declaration.
- **Create** `cnc/ncland/src/nclan_seed_enable.cpp` — helper definition (pure, no I/O).
- **Create** `cnc/ncland/src/nclan_seed_enable_tests.cpp` — helper unit tests.
- **Modify** `cnc/ncland/src/nclan_seed_cache.h` — add `es64_del_flag`, `es64_enabled` to `NclanSeedRow`.
- **Modify** `cnc/ncland/src/nclan_seed_read.cpp` — `nclan_seed_ctree_row` captures es64 enabled/del_flag; `nclan_seed_read_row` applies the helper.
- **Modify** `cnc/ncland/src/nclan_seed_read_tests.cpp` — update stub; add precedence tests.
- **Modify** `cnc/ncland/src/nclan_seed.cpp` — remove per-NE disable gate; per-slot es64 fetch + helper.
- **Modify** `cnc/ncland/src/Makefile` — add new objects to `ncland_unit_tests` and `$(PBIN)/nclan-seed`.

---

## Task 1: Pure precedence helper + tests

**Files:**
- Create: `cnc/ncland/src/nclan_seed_enable.h`
- Create: `cnc/ncland/src/nclan_seed_enable.cpp`
- Test: `cnc/ncland/src/nclan_seed_enable_tests.cpp`
- Modify: `cnc/ncland/src/Makefile`

- [ ] **Step 1: Write the header**

Create `cnc/ncland/src/nclan_seed_enable.h`:

```cpp
#ifndef NCLAN_SEED_ENABLE_H
#define NCLAN_SEED_ENABLE_H

/**
 * @brief Decide whether an NE card slot is enabled, applying es64 precedence.
 *
 * When a live (non-deleted) es64_snmp_info record exists for the slot, its
 * enabled field is authoritative in both directions. Otherwise the caller's
 * fallback (derived from frame_link.disable, or its PG mirror) decides.
 *
 * @param es64_got_one      1 if get_es64_snmp_info_neId_slot returned GOT_ONE.
 * @param es64_del_flag     es64 record del_flag (a deleted record does not count).
 * @param es64_enabled      es64_snmp_info.enabled (consulted only when present).
 * @param fallback_enabled  Enabled bit to use when no live es64 record exists
 *                          (seed walk passes frame_link.disable == 0; the watch
 *                          path passes the ne_link_data PG enabled bit).
 * @return 1 if the slot is enabled, 0 otherwise.
 */
int nclan_seed_slot_enabled(int es64_got_one, int es64_del_flag,
                            int es64_enabled, int fallback_enabled);

#endif
```

- [ ] **Step 2: Write the failing tests**

Create `cnc/ncland/src/nclan_seed_enable_tests.cpp`:

```cpp
#include "../../../include/nfunit-test.hpp"
#include "nclan_seed_enable.h"

TEST("seedenable", "E1 es64 present+enabled overrides frame_link disabled") {
    /* The 1801 case: frame_link disabled (fallback=0) but es64 enabled. */
    REQUIRE(nclan_seed_slot_enabled(1, 0, 1, 0) == 1);
}
TEST("seedenable", "E2 es64 present+disabled overrides frame_link enabled") {
    REQUIRE(nclan_seed_slot_enabled(1, 0, 0, 1) == 0);
}
TEST("seedenable", "E3 es64 absent falls back to frame_link enabled") {
    REQUIRE(nclan_seed_slot_enabled(0, 0, 0, 1) == 1);
}
TEST("seedenable", "E4 es64 absent falls back to frame_link disabled") {
    REQUIRE(nclan_seed_slot_enabled(0, 0, 0, 0) == 0);
}
TEST("seedenable", "E5 deleted es64 record is treated as absent") {
    /* del_flag set: ignore es64_enabled, use fallback. */
    REQUIRE(nclan_seed_slot_enabled(1, 1, 1, 0) == 0);
    REQUIRE(nclan_seed_slot_enabled(1, 1, 0, 1) == 1);
}
TEST("seedenable", "E6 non-zero es64_enabled normalizes to 1") {
    REQUIRE(nclan_seed_slot_enabled(1, 0, 2, 0) == 1);
}
```

- [ ] **Step 3: Add to the build (nmake — use writing-nmake-makefiles skill)**

In `cnc/ncland/src/Makefile`, append `nclan_seed_enable.o nclan_seed_enable_tests.o` to the `ncland_unit_tests ::` dependency list (line ~36), immediately after `nclan_seed_read_tests.o`:

```
... nclan_seed_read.o nclan_seed_read_tests.o nclan_seed_enable.o nclan_seed_enable_tests.o nclan_seed_watch.o ...
```

- [ ] **Step 4: Run the tests to verify they FAIL (link error)**

Run: `nmake ncland_unit_tests`
Expected: FAIL — undefined reference to `nclan_seed_slot_enabled` (nclan_seed_enable.cpp not written yet).

- [ ] **Step 5: Write the minimal implementation**

Create `cnc/ncland/src/nclan_seed_enable.cpp`:

```cpp
#include "nclan_seed_enable.h"

int nclan_seed_slot_enabled(int es64_got_one, int es64_del_flag,
                            int es64_enabled, int fallback_enabled)
{
    if (es64_got_one && !es64_del_flag)
        return es64_enabled ? 1 : 0;
    return fallback_enabled ? 1 : 0;
}
```

- [ ] **Step 6: Build and run tests**

Run: `nmake .TEST`
Expected: PASS — the six `seedenable` tests pass, all existing suites still pass.

- [ ] **Step 7: Commit**

```bash
git add cnc/ncland/src/nclan_seed_enable.h cnc/ncland/src/nclan_seed_enable.cpp \
        cnc/ncland/src/nclan_seed_enable_tests.cpp cnc/ncland/src/Makefile
git commit -m "feat(ncland): pure es64/frame_link enable-precedence helper"
```

---

## Task 2: Watch path uses es64 precedence

**Files:**
- Modify: `cnc/ncland/src/nclan_seed_cache.h`
- Modify: `cnc/ncland/src/nclan_seed_read.cpp`
- Test: `cnc/ncland/src/nclan_seed_read_tests.cpp`

- [ ] **Step 1: Add es64 fields to NclanSeedRow**

In `cnc/ncland/src/nclan_seed_cache.h`, extend the struct (after `type`):

```cpp
struct NclanSeedRow {
    int         enabled;      /**< 1 = enabled, 0 = disabled (post-precedence). */
    std::string ip;           /**< CLI IP address (may be empty). */
    int         port;         /**< CLI TCP port (0 = dtype default). */
    int         dtype;        /**< Numeric dcs_type[0] for the neId. */
    int         type;         /**< frame_link type index (per-slot). */
    int         es64_del_flag; /**< es64 record del_flag (1 = deleted). */
    int         es64_enabled;  /**< es64_snmp_info.enabled as read from c-tree. */
};
```

Note: `NclanSeedCache::diff` compares only `enabled`/`ip`/`port`, so the new fields do not affect diff behavior.

- [ ] **Step 2: Write the failing tests**

Edit `cnc/ncland/src/nclan_seed_read_tests.cpp`. First update the existing ctree stub so it represents "es64 present but deleted" — preserving the existing R1–R4 semantics that PG drives `enabled`:

```cpp
static int stub_enabled = -1;
static int stub_read_ctree(int neid, int slot, NclanSeedRow *out) {
    (void)neid; (void)slot;
    out->ip = "10.0.0.1";
    out->port = 23;
    out->dtype = 245;
    out->type = 1;
    out->es64_del_flag = 1;   /* deleted -> not present -> PG fallback drives enabled */
    out->es64_enabled  = 0;
    return 0;
}
```

Then add precedence tests at the end of the file:

```cpp
/* Live (non-deleted) es64 record: enabled comes from es64, PG is ignored. */
static int stub_read_ctree_live_en1(int neid, int slot, NclanSeedRow *out) {
    (void)neid; (void)slot;
    out->ip = "10.0.0.2"; out->port = 22; out->dtype = 207; out->type = 1;
    out->es64_del_flag = 0; out->es64_enabled = 1;
    return 0;
}
static int stub_read_ctree_live_en0(int neid, int slot, NclanSeedRow *out) {
    (void)neid; (void)slot;
    out->ip = "10.0.0.3"; out->port = 22; out->dtype = 207; out->type = 1;
    out->es64_del_flag = 0; out->es64_enabled = 0;
    return 0;
}
static int stub_pg_fail(int, int) { return -1; }

TEST("seedread", "R5 live es64 enabled beats conflicting PG disabled") {
    NclanSeedRow row;
    /* PG would say disabled (and would even fail); es64 must win and PG is not consulted. */
    int rc = nclan_seed_read_row(1801, 3, &row, stub_pg_fail, stub_read_ctree_live_en1);
    REQUIRE(rc == 0);
    REQUIRE(row.enabled == 1);
}
TEST("seedread", "R6 live es64 disabled beats conflicting PG enabled") {
    stub_enabled = 1;   /* PG says enabled */
    NclanSeedRow row;
    int rc = nclan_seed_read_row(1801, 3, &row, stub_read_pg_enabled, stub_read_ctree_live_en0);
    REQUIRE(rc == 0);
    REQUIRE(row.enabled == 0);
}
```

- [ ] **Step 3: Run tests to verify R5/R6 FAIL**

Run: `nmake .TEST`
Expected: FAIL — R5/R6 fail because `nclan_seed_read_row` still always uses PG; R5 also returns -1 (PG stub fails) instead of 0.

- [ ] **Step 4: Rewrite nclan_seed_read_row to apply precedence**

In `cnc/ncland/src/nclan_seed_read.cpp`, replace `nclan_seed_read_row` (lines 17-26) with:

```cpp
#include "nclan_seed_enable.h"   /* add near the top with the other includes */

int nclan_seed_read_row(int neid, int slot, NclanSeedRow *out,
                        NclanSeedPgReadFn pg,
                        NclanSeedCtreeReadFn ct)
{
    if (ct(neid, slot, out) != 0) return -1;   /* ct sets es64_del_flag/es64_enabled */

    /* ct() returning 0 means the es64 record was found (GOT_ONE). A live
     * (non-deleted) record is authoritative; only then is PG skipped. */
    int present = (out->es64_del_flag == 0);
    int fallback = 0;
    if (!present) {
        fallback = pg(neid, slot);
        if (fallback < 0) return -1;
    }
    out->enabled = nclan_seed_slot_enabled(1, out->es64_del_flag,
                                           out->es64_enabled, fallback);
    return 0;
}
```

- [ ] **Step 5: Capture es64 enabled/del_flag in the production c-tree read**

In `cnc/ncland/src/nclan_seed_read.cpp`, in `nclan_seed_ctree_row` (lines 57-75), after the `GOT_ONE` check, replace the `out->enabled = 0;` line and populate the new fields:

```cpp
int nclan_seed_ctree_row(int neid, int slot, NclanSeedRow *out)
{
    ES64_SNMP_INFO_REC rec;
    memset(&rec, 0, sizeof(rec));
    rec.neId = neid;
    rec.slot = slot;
    rec.type = DST_CLI;
    if (get_es64_snmp_info_neId_slot(&rec) != GOT_ONE)
        return -1;
    out->ip            = rec.ipAddr;
    out->port          = rec.cliPort;
    out->dtype         = frm_dtype(neid);
    out->type          = rec.type;
    out->es64_del_flag = rec.del_flag;
    out->es64_enabled  = rec.enabled;
    out->enabled       = 0;   /* Set by nclan_seed_read_row via the precedence helper. */
    return 0;
}
```

- [ ] **Step 6: Build and run tests**

Run: `nmake .TEST`
Expected: PASS — R1–R6 pass (R1–R4 via the deleted-record fallback, R5/R6 via es64 precedence), all other suites pass.

- [ ] **Step 7: Commit**

```bash
git add cnc/ncland/src/nclan_seed_cache.h cnc/ncland/src/nclan_seed_read.cpp \
        cnc/ncland/src/nclan_seed_read_tests.cpp
git commit -m "feat(ncland): watch path honors es64 enable over frame_link"
```

---

## Task 3: Seed walk uses es64 precedence

**Files:**
- Modify: `cnc/ncland/src/nclan_seed.cpp`

- [ ] **Step 1: Add the helper include**

In `cnc/ncland/src/nclan_seed.cpp`, add near the existing project includes (after line 20-21, the `nclan_seed_fmt.h` / `nclan_seed_watch.h` includes):

```cpp
#include "nclan_seed_enable.h"
```

- [ ] **Step 2: Remove the per-NE frame_link disable gate**

Delete the per-NE early skip and its comment (`nclan_seed.cpp:245-251`):

```cpp
        /* Skip administratively disabled NEs. ... */
        if (flp->disable)              { ++skipped; continue; }
```

Keep the two structural skips above it (`NE_UNASSIGNED` at 242 and `!HAS_CLAN_CARD` at 244) — those are provisioning structure, not enable state.

- [ ] **Step 3: Fetch the es64 record once per slot and apply the helper**

In the per-slot loop (`nclan_seed.cpp:259-281`), replace the current slot body from the `clan_entry` call through the ip-resolution block with:

```cpp
        for (int k = 1; k <= MAX_CLAN_PER_NE; ++k) {
            int slot = 0, sc = 0;
            clan_entry(flp, k, &slot, &sc);
            if (slot == 0) continue;       /* no card in this position */

            /* Fetch the es64 record once: it decides enable AND (when the
             * card starts a clan) supplies ip/port/ssh. */
            ES64_SNMP_INFO_REC r;
            memset(&r, 0, sizeof(r));
            int got_one = 0;
            if (es64_dtype && have_es64) {
                r.neId = neid; r.slot = slot; r.type = DST_CLI;
                got_one = (get_es64_snmp_info_neId_slot(&r) == GOT_ONE);
            }

            /* es64 wins when a live record exists; else frame_link.disable. */
            if (!nclan_seed_slot_enabled(got_one, got_one ? r.del_flag : 0,
                                         got_one ? r.enabled : 0,
                                         flp->disable == 0)) {
                ++skipped;
                continue;
            }

            std::string ip;
            int port = 0;
            int ssh  = gne_ssh;

            if (got_one && !r.del_flag && sc) {
                ip   = r.ipAddr;
                port = r.cliPort;
                ssh  = r.ssh_flag;
            }
            if (ip.empty()) {
                const char *fip = getNeIp(neid);
                ip = fip ? fip : "";
            }
```

Leave the rest of the loop body (the `getTid` / `NeIdentity` / `ncland_seed_format` / publish block, `nclan_seed.cpp:282` onward) unchanged.

- [ ] **Step 4: Build the production binary and unit tests**

Run: `nmake $(PBIN)/nclan-seed && nmake .TEST`
Expected: PASS — clean compile of nclan-seed and all unit suites pass.

- [ ] **Step 5: Commit**

```bash
git add cnc/ncland/src/nclan_seed.cpp
git commit -m "feat(ncland): seed walk honors es64 enable over frame_link"
```

---

## Task 4: Ensure the production nclan-seed links the helper

**Files:**
- Modify: `cnc/ncland/src/Makefile`

- [ ] **Step 1: Add the helper object to the nclan-seed target (nmake — use writing-nmake-makefiles skill)**

In `cnc/ncland/src/Makefile`, the `$(PBIN)/nclan-seed ::` rule (line ~42) links `nclan_seed_read.o`, which now references `nclan_seed_slot_enabled`. Add `nclan_seed_enable.o` to that dependency list:

```
$(PBIN)/nclan-seed :: nclan_seed.o nclan_seed_fmt.o nclan_seed_cache.o nclan_seed_read.o nclan_seed_enable.o nclan_seed_watch.o $(INCLIBS) -latm -linc -lxmlparse -lssh -ljson -lnetclan
```

- [ ] **Step 2: Build the production binary**

Run: `nmake $(PBIN)/nclan-seed`
Expected: PASS — links without undefined `nclan_seed_slot_enabled`.

- [ ] **Step 3: Full test sweep**

Run: `nmake .TEST`
Expected: PASS — every suite green.

- [ ] **Step 4: Commit**

```bash
git add cnc/ncland/src/Makefile
git commit -m "build(ncland): link enable helper into nclan-seed"
```

---

## Manual verification (post-implementation)

On a target where node 1801 shows `frame_link.disable != 0` but
`es64_snmp_info.enabled == 1`:

1. Restart / re-run nclan-seed (the seed walk) or trigger a frame_link/es64
   change the watcher sees.
2. Confirm an `app.ne.seed` (and, on the watcher path, `app.ne.enable`) is
   published for neid 1801 — check `/usr/cnc/trace/nclan-seed`.
3. Confirm ncland then submits a connect for 1801 (its group trace shows
   `notify: event neid=1801 ... topic=app.ne.seed` followed by
   `warehouse_open_conn_by_ne neid:1801`).

## Self-review notes

- **Spec coverage:** rule (Task 1), seed walk (Task 3), watch path (Task 2),
  del_flag exclusion (E5, plus del_flag wired at both call sites), scope
  boundary (watch stays es64-centric — no new non-es64 handling added), tests
  (Tasks 1–2), build wiring (Tasks 1 & 4). All spec sections mapped.
- **Consistency:** helper name `nclan_seed_slot_enabled` and signature
  `(es64_got_one, es64_del_flag, es64_enabled, fallback_enabled)` identical
  across header, tests, and both call sites. New row fields `es64_del_flag` /
  `es64_enabled` named identically in the struct, ctree read, and read tests.
