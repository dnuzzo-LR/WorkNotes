# nclan_seed --watch + ncland enable/disable Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a resident `nclan_seed --watch` mode that translates `db.otnport.channel_frmlnk_*` PG-notify events into specific `db.ne.{enable,disable,update}` events on the niimxd nfdb bus, and extend ncland to consume them with slot-aware close and writeback of `snmpLinkStat` to the ES64 record.

**Architecture:** Diff-and-emit translator. Watcher opens a ZMQ SUB on `ipc:///usr/cnc/data/nfdb_pub.sock`, subscribes to the frmlnk channels, primes an in-memory `{neId, slot} → {enabled, ip, port, dtype, type}` cache from the ES64 c-tree at boot, and on each notify does a fresh PG read of `ne_link_data` (authoritative for `enabled`) + c-tree lookup for `ip/port/dtype`, diffs against the cache, and emits at most one specific event via the existing `nfdb_command("PUBLISH ...")` REQ path. ncland gains topic → kind mappings, a slot-aware conn finder, and calls `upd_es64_snmp_info_change_snmpLinkStat()` after open/close.

**Tech Stack:** C++17, Lucent nmake build, existing ncland test harness (`nfunit-test.hpp` with `TEST`/`REQUIRE`), ZMQ (`-lzmq`), PostgreSQL via `librdb` (already linked), `nfdb_clientlib` (already linked).

**Design source:** `~/WorkNotes/specs/2026-08-11-ncland-otnport-zmq-watcher-design.md`

---

## Prerequisites

Verify build environment before starting:

- `git rev-parse --show-toplevel` = `/home/dan/Git/netflex`
- `BASE=/home/dan/Git/netflex`
- `VPATH` first segment = `$BASE`
- `L64_SETUP=1` and 64-bit inc paths present in `VPATH` (per project memory `ncland_build_env`)

Baseline that the existing unit tests build and pass green before touching anything:

```
cd cnc/ncland/src && nmake ncland_unit_tests && ./ncland_unit_tests
```

Do NOT proceed if that command fails on `main` — the plan assumes a green baseline.

---

## File Structure

**New files under `cnc/ncland/src/`:**

- `nclan_seed_cache.h` / `.cpp` — `{neId,slot} → SnapRow` in-memory map. Set/get/erase/diff.
- `nclan_seed_watch.h` / `.cpp` — resident SUB drain, cache prime, event dispatch, emit.
- `nclan_seed_read.h` / `.cpp` — fresh-read helper: returns the authoritative `{enabled, ip, port, dtype, type}` for `(neId, slot)`. `enabled` from PG `ne_link_data`; `ip/port/dtype/type` from ES64 c-tree + `frm_dtype`.
- `nclan_seed_cache_tests.cpp` — unit tests for cache diff.
- `nclan_seed_watch_tests.cpp` — unit tests for the dispatch function (bus-side is faked).
- `nclan_seed_read_tests.cpp` — unit tests for the fresh-read helper's field packing (real DB access is stubbed via a small seam).

**Modified files:**

- `cnc/ncland/src/nclan_seed.cpp` — add `-w` to getopt, branch to watch loop.
- `cnc/ncland/src/nclan_seed_fmt.h` / `.cpp` — add formatters `format_ne_enable / format_ne_disable / format_ne_update`.
- `cnc/ncland/src/nclan_seed_tests.cpp` — add tests for the three new formatters.
- `cnc/ncland/src/ncland_notify_parse.cpp` — extend `kind_for()` with `db.ne.enable` / `db.ne.disable`.
- `cnc/ncland/src/ncland_notify_parse_tests.cpp` — extend for new topics.
- `cnc/ncland/src/ncland_notify.cpp` — add `ncland_find_conn_by_neid_slot()`; make DISABLE dispatch slot-aware; call `upd_es64_snmp_info_change_snmpLinkStat` after successful open/close.
- `cnc/ncland/src/ncland_notify.h` — declare new finder for tests.
- `cnc/ncland/src/Makefile` — add new `.o` entries to the `nclan-seed` and `ncland_unit_tests` targets.

---

## Task 1: Cache module — types + set/get/erase (no diff yet)

**Files:**
- Create: `cnc/ncland/src/nclan_seed_cache.h`
- Create: `cnc/ncland/src/nclan_seed_cache.cpp`
- Create: `cnc/ncland/src/nclan_seed_cache_tests.cpp`
- Modify: `cnc/ncland/src/Makefile` (add `nclan_seed_cache.o` and `nclan_seed_cache_tests.o`)

- [ ] **Step 1: Write the failing tests**

`cnc/ncland/src/nclan_seed_cache_tests.cpp`:

```cpp
#include "../../../include/nfunit-test.hpp"
#include "nclan_seed_cache.h"

TEST("cache", "C1 empty on construction") {
    NclanSeedCache c;
    NclanSeedRow r;
    REQUIRE(c.get(1, 2, &r) == false);
}
TEST("cache", "C2 set then get returns the row") {
    NclanSeedCache c;
    NclanSeedRow in{ /*enabled*/1, /*ip*/"10.0.0.1", /*port*/23,
                     /*dtype*/245, /*type*/1 };
    c.set(5, 12, in);
    NclanSeedRow out;
    REQUIRE(c.get(5, 12, &out) == true);
    REQUIRE(out.enabled == 1);
    REQUIRE(out.ip == "10.0.0.1");
    REQUIRE(out.port == 23);
    REQUIRE(out.dtype == 245);
    REQUIRE(out.type == 1);
}
TEST("cache", "C3 set overwrites") {
    NclanSeedCache c;
    c.set(5, 12, NclanSeedRow{1, "10.0.0.1", 23, 245, 1});
    c.set(5, 12, NclanSeedRow{1, "10.0.0.2", 22, 245, 1});
    NclanSeedRow out;
    c.get(5, 12, &out);
    REQUIRE(out.ip == "10.0.0.2");
    REQUIRE(out.port == 22);
}
TEST("cache", "C4 erase") {
    NclanSeedCache c;
    c.set(5, 12, NclanSeedRow{1, "x", 1, 1, 1});
    c.erase(5, 12);
    NclanSeedRow out;
    REQUIRE(c.get(5, 12, &out) == false);
}
TEST("cache", "C5 different slots distinct") {
    NclanSeedCache c;
    c.set(5, 12, NclanSeedRow{1, "a", 1, 1, 1});
    c.set(5, 13, NclanSeedRow{0, "b", 2, 2, 2});
    NclanSeedRow out;
    c.get(5, 12, &out); REQUIRE(out.ip == "a");
    c.get(5, 13, &out); REQUIRE(out.ip == "b");
}
```

- [ ] **Step 2: Create the header**

`cnc/ncland/src/nclan_seed_cache.h`:

```cpp
#ifndef NCLAN_SEED_CACHE_H
#define NCLAN_SEED_CACHE_H

#include <string>
#include <unordered_map>
#include <cstdint>

/**
 * @brief Snapshot of an ES64 interface row as the watcher sees it.
 */
struct NclanSeedRow {
    int         enabled;  /**< 1 = enabled, 0 = disabled. */
    std::string ip;       /**< CLI IP address (may be empty). */
    int         port;     /**< CLI TCP port (0 = dtype default). */
    int         dtype;    /**< Numeric dcs_type[0] for the neId. */
    int         type;     /**< frame_link type index (per-slot). */
};

/**
 * @brief Result of a diff between prior and current snapshots.
 */
enum class NclanSeedDiff {
    NONE,     /**< No emit needed. */
    ENABLE,   /**< Interface became enabled. */
    DISABLE,  /**< Interface became disabled. */
    UPDATE    /**< Enabled and ip/port changed. */
};

/**
 * @brief In-memory {neId, slot} → NclanSeedRow map for the --watch loop.
 */
class NclanSeedCache {
public:
    void set(int neid, int slot, const NclanSeedRow &row);
    bool get(int neid, int slot, NclanSeedRow *out) const;
    void erase(int neid, int slot);

    /**
     * @brief Compute the emit decision for a fresh row vs cached prior.
     * @param prior_present false if there is no cached prior.
     */
    static NclanSeedDiff diff(bool prior_present,
                              const NclanSeedRow &prior,
                              const NclanSeedRow &fresh);

private:
    struct Key { int neid; int slot;
                 bool operator==(const Key &o) const {
                     return neid == o.neid && slot == o.slot;
                 } };
    struct KeyHash { size_t operator()(const Key &k) const {
                     return (static_cast<size_t>(k.neid) << 32)
                            ^ static_cast<size_t>(k.slot); } };
    std::unordered_map<Key, NclanSeedRow, KeyHash> m_;
};

#endif
```

- [ ] **Step 3: Write minimal cpp (set/get/erase only, diff returns NONE for now)**

`cnc/ncland/src/nclan_seed_cache.cpp`:

```cpp
#include "nclan_seed_cache.h"

void NclanSeedCache::set(int neid, int slot, const NclanSeedRow &row) {
    m_[Key{neid, slot}] = row;
}
bool NclanSeedCache::get(int neid, int slot, NclanSeedRow *out) const {
    auto it = m_.find(Key{neid, slot});
    if (it == m_.end()) return false;
    *out = it->second;
    return true;
}
void NclanSeedCache::erase(int neid, int slot) {
    m_.erase(Key{neid, slot});
}
NclanSeedDiff NclanSeedCache::diff(bool, const NclanSeedRow &,
                                   const NclanSeedRow &) {
    return NclanSeedDiff::NONE;  /* Filled in by Task 2. */
}
```

- [ ] **Step 4: Wire into Makefile**

Modify `cnc/ncland/src/Makefile`, add `nclan_seed_cache.o` to the `nclan-seed` object list at line 42 and `nclan_seed_cache_tests.o` + `nclan_seed_cache.o` to the `ncland_unit_tests ::` line at line 36.

Line 42 becomes:
```
$(PBIN)/nclan-seed :: nclan_seed.o nclan_seed_fmt.o nclan_seed_cache.o $(INCLIBS) -latm -linc -lxmlparse -lssh -ljson -lnetclan
```

Line 36 gains `nclan_seed_cache.o nclan_seed_cache_tests.o` immediately after `nclan_seed_tests.o`.

- [ ] **Step 5: Build tests and run**

```
cd cnc/ncland/src && nmake ncland_unit_tests && ./ncland_unit_tests
```

Expected: all five `cache` tests pass; existing tests still pass.

- [ ] **Step 6: Commit**

```
git add cnc/ncland/src/nclan_seed_cache.h cnc/ncland/src/nclan_seed_cache.cpp cnc/ncland/src/nclan_seed_cache_tests.cpp cnc/ncland/src/Makefile
git commit -m "nclan_seed: add NclanSeedCache scaffolding"
```

---

## Task 2: Cache diff logic

**Files:**
- Modify: `cnc/ncland/src/nclan_seed_cache.cpp` (fill in `diff`)
- Modify: `cnc/ncland/src/nclan_seed_cache_tests.cpp` (append diff tests)

- [ ] **Step 1: Append the failing diff tests**

Append to `cnc/ncland/src/nclan_seed_cache_tests.cpp`:

```cpp
static NclanSeedRow R(int enabled, const char *ip, int port) {
    return NclanSeedRow{enabled, ip, port, 245, 1};
}
TEST("cache", "D1 absent + fresh disabled = NONE") {
    REQUIRE(NclanSeedCache::diff(false, R(0,"",0), R(0,"",0))
            == NclanSeedDiff::NONE);
}
TEST("cache", "D2 absent + fresh enabled = ENABLE") {
    REQUIRE(NclanSeedCache::diff(false, R(0,"",0), R(1,"1.2.3.4",23))
            == NclanSeedDiff::ENABLE);
}
TEST("cache", "D3 enabled -> disabled = DISABLE") {
    REQUIRE(NclanSeedCache::diff(true, R(1,"1.2.3.4",23), R(0,"1.2.3.4",23))
            == NclanSeedDiff::DISABLE);
}
TEST("cache", "D4 disabled -> enabled = ENABLE") {
    REQUIRE(NclanSeedCache::diff(true, R(0,"1.2.3.4",23), R(1,"1.2.3.4",23))
            == NclanSeedDiff::ENABLE);
}
TEST("cache", "D5 enabled same = NONE") {
    REQUIRE(NclanSeedCache::diff(true, R(1,"1.2.3.4",23), R(1,"1.2.3.4",23))
            == NclanSeedDiff::NONE);
}
TEST("cache", "D6 enabled ip change = UPDATE") {
    REQUIRE(NclanSeedCache::diff(true, R(1,"1.2.3.4",23), R(1,"9.9.9.9",23))
            == NclanSeedDiff::UPDATE);
}
TEST("cache", "D7 enabled port change = UPDATE") {
    REQUIRE(NclanSeedCache::diff(true, R(1,"1.2.3.4",23), R(1,"1.2.3.4",22))
            == NclanSeedDiff::UPDATE);
}
TEST("cache", "D8 enabled ip+port both change = single UPDATE") {
    REQUIRE(NclanSeedCache::diff(true, R(1,"1.2.3.4",23), R(1,"9.9.9.9",22))
            == NclanSeedDiff::UPDATE);
}
TEST("cache", "D9 disabled unchanged = NONE") {
    REQUIRE(NclanSeedCache::diff(true, R(0,"x",1), R(0,"x",1))
            == NclanSeedDiff::NONE);
}
TEST("cache", "D10 absent + fresh disabled (cache-miss ignored) = NONE") {
    REQUIRE(NclanSeedCache::diff(false, R(0,"",0), R(0,"1.2.3.4",23))
            == NclanSeedDiff::NONE);
}
```

- [ ] **Step 2: Run to verify failures**

```
cd cnc/ncland/src && nmake ncland_unit_tests && ./ncland_unit_tests
```

Expected: 10 new `cache` tests fail; existing tests pass.

- [ ] **Step 3: Implement `diff`**

Replace the `diff` body in `nclan_seed_cache.cpp`:

```cpp
NclanSeedDiff NclanSeedCache::diff(bool prior_present,
                                   const NclanSeedRow &prior,
                                   const NclanSeedRow &fresh) {
    if (!prior_present) {
        return fresh.enabled ? NclanSeedDiff::ENABLE
                             : NclanSeedDiff::NONE;
    }
    if (prior.enabled && !fresh.enabled) return NclanSeedDiff::DISABLE;
    if (!prior.enabled && fresh.enabled) return NclanSeedDiff::ENABLE;
    if (!fresh.enabled) return NclanSeedDiff::NONE;
    /* both enabled */
    if (prior.ip != fresh.ip || prior.port != fresh.port)
        return NclanSeedDiff::UPDATE;
    return NclanSeedDiff::NONE;
}
```

- [ ] **Step 4: Run to verify all pass**

```
./ncland_unit_tests
```

Expected: all `cache` tests green.

- [ ] **Step 5: Commit**

```
git add cnc/ncland/src/nclan_seed_cache.cpp cnc/ncland/src/nclan_seed_cache_tests.cpp
git commit -m "nclan_seed: NclanSeedCache::diff decision table"
```

---

## Task 3: YAML formatters for new event bodies

**Files:**
- Modify: `cnc/ncland/src/nclan_seed_fmt.h` (add three declarations)
- Modify: `cnc/ncland/src/nclan_seed_fmt.cpp` (add three definitions)
- Modify: `cnc/ncland/src/nclan_seed_tests.cpp` (round-trip tests)

- [ ] **Step 1: Add failing round-trip tests**

Append to `cnc/ncland/src/nclan_seed_tests.cpp`:

```cpp
TEST("seedfmt", "E1 format_ne_enable round-trips through parser") {
    NeIdentity ne{5, 245, "10.0.0.1", "NODE5", 12, 23, 1};
    ncland_notify_event_t e;
    REQUIRE(ncland_notify_parse("db.ne.enable",
            ncland_seed_format_enable(ne), &e) == 0);
    REQUIRE(e.kind == NCLAND_EVT_NE_ENABLE);
    REQUIRE(e.neid == 5); REQUIRE(e.slot == 12);
    REQUIRE(std::string(e.ip) == "10.0.0.1"); REQUIRE(e.port == 23);
    REQUIRE(e.ssh_flag == 1); REQUIRE(e.dtype == 245);
}
TEST("seedfmt", "E2 format_ne_update round-trips") {
    NeIdentity ne{5, 245, "10.0.0.2", "NODE5", 12, 22, 1};
    ncland_notify_event_t e;
    REQUIRE(ncland_notify_parse("db.ne.update",
            ncland_seed_format_update(ne), &e) == 0);
    REQUIRE(e.kind == NCLAND_EVT_NE_IP_CHANGE);
    REQUIRE(std::string(e.ip) == "10.0.0.2"); REQUIRE(e.port == 22);
}
TEST("seedfmt", "E3 format_ne_disable is neid+slot") {
    /* Disable body carries only neid, slot, type; parser tolerates
       missing ip/dtype for DISABLE kind. */
    NeIdentity ne{5, 245, "", "", 12, 0, 0};
    ncland_notify_event_t e;
    REQUIRE(ncland_notify_parse("db.ne.disable",
            ncland_seed_format_disable(ne), &e) == 0);
    REQUIRE(e.kind == NCLAND_EVT_NE_DISABLE);
    REQUIRE(e.neid == 5); REQUIRE(e.slot == 12);
}
```

- [ ] **Step 2: Add declarations**

Append to `cnc/ncland/src/nclan_seed_fmt.h` before the `#endif`:

```cpp
/**
 * @brief Render an "interface enabled" event body.
 *        Layout matches ncland_seed_format so ncland's parser is unchanged.
 */
std::string ncland_seed_format_enable(const NeIdentity &ne);

/**
 * @brief Render an "interface disabled" event body.
 *        Neid + slot only; ip/dtype omitted.
 */
std::string ncland_seed_format_disable(const NeIdentity &ne);

/**
 * @brief Render an "interface ip/port changed" event body.
 *        Same layout as ncland_seed_format.
 */
std::string ncland_seed_format_update(const NeIdentity &ne);
```

- [ ] **Step 3: Add definitions**

Append to `cnc/ncland/src/nclan_seed_fmt.cpp`. Reuse the existing `ncland_seed_format` implementation for enable/update since the body layout is identical (identity + slot + port + ssh_flag):

```cpp
std::string ncland_seed_format_enable(const NeIdentity &ne) {
    return ncland_seed_format(ne);
}
std::string ncland_seed_format_update(const NeIdentity &ne) {
    return ncland_seed_format(ne);
}
std::string ncland_seed_format_disable(const NeIdentity &ne) {
    /* Minimal body: neid, slot. Parser tolerates the missing fields. */
    char buf[128];
    snprintf(buf, sizeof(buf), "{neid: %d, slot: %d}", ne.neid, ne.slot);
    return buf;
}
```

- [ ] **Step 4: Ensure parser accepts `db.ne.disable` as topic even though bodies are minimal**

This is not implemented yet — parser mapping happens in Task 6. The tests above will fail on `parse` returning -1 for the new topics. That is expected. Skip the run for now and continue with Task 4; return here after Task 6.

Actually — the parser tests belong with Task 6. To keep this task green now, defer the round-trip tests to Task 6. Instead, add smoke tests here that check formatter output shape only:

Replace the three tests above with these:

```cpp
TEST("seedfmt", "E1 format_ne_enable identical to seed body") {
    NeIdentity ne{5, 245, "10.0.0.1", "NODE5", 12, 23, 1};
    REQUIRE(ncland_seed_format_enable(ne) == ncland_seed_format(ne));
}
TEST("seedfmt", "E2 format_ne_update identical to seed body") {
    NeIdentity ne{5, 245, "10.0.0.2", "NODE5", 12, 22, 1};
    REQUIRE(ncland_seed_format_update(ne) == ncland_seed_format(ne));
}
TEST("seedfmt", "E3 format_ne_disable is neid+slot only") {
    NeIdentity ne{5, 245, "", "", 12, 0, 0};
    REQUIRE(ncland_seed_format_disable(ne) == "{neid: 5, slot: 12}");
}
```

- [ ] **Step 5: Build tests and run**

```
cd cnc/ncland/src && nmake ncland_unit_tests && ./ncland_unit_tests
```

Expected: three new `seedfmt` tests pass; all existing tests still pass.

- [ ] **Step 6: Commit**

```
git add cnc/ncland/src/nclan_seed_fmt.h cnc/ncland/src/nclan_seed_fmt.cpp cnc/ncland/src/nclan_seed_tests.cpp
git commit -m "nclan_seed: add enable/disable/update body formatters"
```

---

## Task 4: Fresh-read helper (PG enabled + c-tree ip/port/dtype)

**Files:**
- Create: `cnc/ncland/src/nclan_seed_read.h`
- Create: `cnc/ncland/src/nclan_seed_read.cpp`
- Create: `cnc/ncland/src/nclan_seed_read_tests.cpp`
- Modify: `cnc/ncland/src/Makefile` (add `.o` entries)

**Design note:** `enabled` is authoritative in PG `ne_link_data` (per design doc resolution). `ip`, `port` are read from the ES64 c-tree via existing `get_es64_snmp_info_neId_slot` because that is what the CLI-connect path itself reads (`cnc/sdi/src/clan.c:1489`) — comparing against it makes the diff detect changes that will actually take effect on next connect. `dtype` comes from the frame_link `dcs_type[0]` via `frm_dtype(neId)` (same helper the existing seed pass uses).

- [ ] **Step 1: Write the failing test**

`cnc/ncland/src/nclan_seed_read_tests.cpp`:

```cpp
#include "../../../include/nfunit-test.hpp"
#include "nclan_seed_read.h"

/* The read helper composes three sub-reads. To test the composition
   without PG/ctree, we inject stubs. */

static int stub_enabled = -1;
static int stub_read_ctree(int neid, int slot, NclanSeedRow *out) {
    (void)neid; (void)slot;
    out->ip = "10.0.0.1";
    out->port = 23;
    out->dtype = 245;
    out->type = 1;
    return 0;
}
static int stub_read_pg_enabled(int neid, int slot) {
    (void)neid; (void)slot;
    return stub_enabled;
}

TEST("seedread", "R1 combines pg enabled + ctree fields") {
    stub_enabled = 1;
    NclanSeedRow row;
    int rc = nclan_seed_read_row(5, 12, &row,
                                 stub_read_pg_enabled,
                                 stub_read_ctree);
    REQUIRE(rc == 0);
    REQUIRE(row.enabled == 1);
    REQUIRE(row.ip == "10.0.0.1");
    REQUIRE(row.port == 23);
    REQUIRE(row.dtype == 245);
    REQUIRE(row.type == 1);
}
TEST("seedread", "R2 enabled=0 propagates") {
    stub_enabled = 0;
    NclanSeedRow row;
    nclan_seed_read_row(5, 12, &row, stub_read_pg_enabled, stub_read_ctree);
    REQUIRE(row.enabled == 0);
}
TEST("seedread", "R3 pg failure returns -1") {
    NclanSeedRow row;
    int rc = nclan_seed_read_row(5, 12, &row,
        [](int, int) { return -1; },   /* pg fail */
        stub_read_ctree);
    REQUIRE(rc == -1);
}
TEST("seedread", "R4 ctree failure returns -1") {
    stub_enabled = 1;
    NclanSeedRow row;
    int rc = nclan_seed_read_row(5, 12, &row,
        stub_read_pg_enabled,
        [](int, int, NclanSeedRow *) { return -1; });
    REQUIRE(rc == -1);
}
```

- [ ] **Step 2: Create header**

`cnc/ncland/src/nclan_seed_read.h`:

```cpp
#ifndef NCLAN_SEED_READ_H
#define NCLAN_SEED_READ_H

#include "nclan_seed_cache.h"

/**
 * @brief PG-read stub type: returns enabled bit (0/1) or -1 on error.
 */
using NclanSeedPgReadFn = int (*)(int neid, int slot);

/**
 * @brief C-tree read stub type: fills ip/port/dtype/type. Returns 0/-1.
 */
using NclanSeedCtreeReadFn = int (*)(int neid, int slot, NclanSeedRow *out);

/**
 * @brief Compose a fresh {enabled, ip, port, dtype, type} snapshot.
 *        `enabled` comes from PG (authoritative), the rest from c-tree.
 * @return 0 on success; -1 if either sub-read failed.
 */
int nclan_seed_read_row(int neid, int slot, NclanSeedRow *out,
                        NclanSeedPgReadFn   pg,
                        NclanSeedCtreeReadFn ct);

/**
 * @brief Production PG read: SELECT enabled FROM ne_link_data
 *        WHERE ne_id = neid AND slot = slot.
 * @return 0/1 on success, -1 on error or no row.
 */
int nclan_seed_pg_enabled(int neid, int slot);

/**
 * @brief Production c-tree read: wraps get_es64_snmp_info_neId_slot.
 * @return 0 on success (row->enabled left at its c-tree value but is
 *         overwritten by the caller); -1 on missing record.
 */
int nclan_seed_ctree_row(int neid, int slot, NclanSeedRow *out);

#endif
```

- [ ] **Step 3: Minimal cpp — compose function only**

`cnc/ncland/src/nclan_seed_read.cpp`:

```cpp
#include "nclan_seed_read.h"

int nclan_seed_read_row(int neid, int slot, NclanSeedRow *out,
                        NclanSeedPgReadFn pg,
                        NclanSeedCtreeReadFn ct)
{
    if (ct(neid, slot, out) != 0) return -1;
    int en = pg(neid, slot);
    if (en < 0) return -1;
    out->enabled = en;
    return 0;
}

/* Production readers deferred to Task 5; return -1 stubs for now so
   the test binary still links. */
int nclan_seed_pg_enabled(int, int)               { return -1; }
int nclan_seed_ctree_row (int, int, NclanSeedRow*) { return -1; }
```

- [ ] **Step 4: Wire into Makefile**

Add `nclan_seed_read.o` to the `nclan-seed` target (line 42) and `nclan_seed_read.o nclan_seed_read_tests.o` to `ncland_unit_tests ::` (line 36).

- [ ] **Step 5: Run tests**

```
cd cnc/ncland/src && nmake ncland_unit_tests && ./ncland_unit_tests
```

Expected: four `seedread` tests pass.

- [ ] **Step 6: Commit**

```
git add cnc/ncland/src/nclan_seed_read.h cnc/ncland/src/nclan_seed_read.cpp cnc/ncland/src/nclan_seed_read_tests.cpp cnc/ncland/src/Makefile
git commit -m "nclan_seed: add fresh-read composition helper"
```

---

## Task 5: Production PG + c-tree readers

**Files:**
- Modify: `cnc/ncland/src/nclan_seed_read.cpp` (fill in `nclan_seed_pg_enabled`, `nclan_seed_ctree_row`)

No new unit tests here — these functions require a live PG connection and c-tree files. They are exercised in the integration smoke of Task 11.

- [ ] **Step 1: Implement c-tree read**

Replace the stub in `nclan_seed_read.cpp`:

```cpp
#include <alcatel_es64_db.h>   /* ES64_SNMP_INFO_REC, get_es64_snmp_info_neId_slot */
#include <ne_defs.h>           /* frm_dtype */
#include <flags.h>             /* SUCCESS/FAILURE, GOT_ONE */

int nclan_seed_ctree_row(int neid, int slot, NclanSeedRow *out)
{
    ES64_SNMP_INFO_REC rec;
    if (get_es64_snmp_info_neId_slot(neid, slot, &rec) != SUCCESS)
        return -1;
    out->ip    = rec.ipAddr;
    out->port  = rec.cliPort;
    out->dtype = frm_dtype(neid);
    out->type  = rec.type;
    /* out->enabled overwritten by nclan_seed_read_row from PG. */
    out->enabled = 0;
    return 0;
}
```

- [ ] **Step 2: Implement PG read**

Add near the top of `nclan_seed_read.cpp`:

```cpp
#include <rdb_conn.h>          /* DBCONN, rdbNeCommGetDbConn */
#include <rdb_common.h>        /* rdb_exec_query, RDBROW helpers */
```

Then replace the `nclan_seed_pg_enabled` stub:

```cpp
int nclan_seed_pg_enabled(int neid, int slot)
{
    DBCONN *db = rdbNeCommGetDbConn();
    if (!db) return -1;
    char sql[256];
    snprintf(sql, sizeof(sql),
        "SELECT enabled FROM ne_link_data "
        "WHERE ne_id = %d AND slot = %d LIMIT 1",
        neid, slot);
    RDBROW row = rdb_exec_query(db, sql);
    if (!row) return -1;
    int en = -1;
    if (rdb_row_count(row) > 0) {
        const char *v = rdb_row_get(row, 0, 0);
        if (v) en = (v[0] == 't' || v[0] == '1') ? 1 : 0;
    }
    rdb_row_free(row);
    return en;
}
```

> **Note to implementer:** RDB accessor function names (`rdb_exec_query`, `rdb_row_count`, `rdb_row_get`, `rdb_row_free`) are the canonical names in this codebase per grep of `cnc/rdb/src/`. If they differ in your branch's headers, substitute the equivalents used by `rdb_ne_link_data_store_record` / `rdb_ne_link_data_build_sql` in `cnc/rdb/src/rdb_otn_port_ne_link_data.c` — that file is the reference for correct RDB idioms in this table.

- [ ] **Step 3: Build**

```
cd cnc/ncland/src && nmake nclan-seed && nmake ncland_unit_tests && ./ncland_unit_tests
```

Expected: nclan-seed builds; unit tests still green.

- [ ] **Step 4: Commit**

```
git add cnc/ncland/src/nclan_seed_read.cpp
git commit -m "nclan_seed: production PG enabled + c-tree row readers"
```

---

## Task 6: Parser topic mappings + tolerant field parsing

**Files:**
- Modify: `cnc/ncland/src/ncland_notify_parse.cpp` (extend `kind_for`)
- Modify: `cnc/ncland/src/ncland_notify_parse_tests.cpp` (add tests)

The existing enum values `NCLAND_EVT_NE_ENABLE` and `NCLAND_EVT_NE_DISABLE` are already present in `ncland.h`; only the topic string mapping is missing.

- [ ] **Step 1: Write failing tests**

Append to `cnc/ncland/src/ncland_notify_parse_tests.cpp`:

```cpp
TEST("parse", "P-enable maps to NE_ENABLE") {
    ncland_notify_event_t e;
    REQUIRE(ncland_notify_parse("db.ne.enable",
        "{neid: 5, dtype: 245, ip: 10.0.0.1, port: 23, tid: NODE5, slot: 12, ssh_flag: 1}",
        &e) == 0);
    REQUIRE(e.kind == NCLAND_EVT_NE_ENABLE);
    REQUIRE(e.neid == 5);
    REQUIRE(e.slot == 12);
}
TEST("parse", "P-disable maps to NE_DISABLE, tolerates minimal body") {
    ncland_notify_event_t e;
    REQUIRE(ncland_notify_parse("db.ne.disable", "{neid: 5, slot: 12}",
                                &e) == 0);
    REQUIRE(e.kind == NCLAND_EVT_NE_DISABLE);
    REQUIRE(e.neid == 5);
    REQUIRE(e.slot == 12);
}
TEST("parse", "P-unknown-topic still returns -1") {
    ncland_notify_event_t e;
    REQUIRE(ncland_notify_parse("db.other.thing", "{neid: 5}", &e) == -1);
}
```

- [ ] **Step 2: Run to see failures**

```
cd cnc/ncland/src && nmake ncland_unit_tests && ./ncland_unit_tests
```

Expected: three new `parse` tests fail (kind_for returns false for the new topics).

- [ ] **Step 3: Extend `kind_for`**

In `cnc/ncland/src/ncland_notify_parse.cpp`, add to `kind_for` before the final `return false`:

```cpp
    if (t == "db.ne.enable")  { *k = NCLAND_EVT_NE_ENABLE;  return true; }
    if (t == "db.ne.disable") { *k = NCLAND_EVT_NE_DISABLE; return true; }
```

- [ ] **Step 4: Run tests**

```
./ncland_unit_tests
```

Expected: all `parse` tests green. If the "tolerates minimal body" test fails because required-field parsing rejects it, add a per-kind gate: for `NCLAND_EVT_NE_DISABLE`, require only `neid` and `slot`; skip the ip/port/dtype required-field checks. Confirm by reading the failed field guard in `ncland_notify_parse` and gating it on `kind`.

- [ ] **Step 5: Commit**

```
git add cnc/ncland/src/ncland_notify_parse.cpp cnc/ncland/src/ncland_notify_parse_tests.cpp
git commit -m "ncland: parse db.ne.enable / db.ne.disable topics"
```

---

## Task 7: Slot-aware conn finder in ncland

**Files:**
- Modify: `cnc/ncland/src/ncland_notify.cpp` (add `ncland_find_conn_by_neid_slot`)
- Modify: `cnc/ncland/src/ncland_notify.h` (declare it — check if already declared; if there's no public header for it, expose via a `.h` sibling or add to `ncland.h` next to `ncland_find_conn_by_neid`)

- [ ] **Step 1: Locate the existing declaration site**

Grep for `ncland_find_conn_by_neid` in headers under `cnc/ncland/src/`. Add the new declaration adjacent to that one:

```c
/**
 * @brief Find the connection id serving (neid, slot) or -1 if none.
 */
int ncland_find_conn_by_neid_slot(ncland_wh_t *wh, int neid, int slot);
```

- [ ] **Step 2: Write the failing test**

Append to whichever tests file drives ncland_notify.cpp coverage today (likely a fresh `ncland_notify_tests.cpp`; if it doesn't exist, create one and register it in the Makefile per the existing pattern):

```cpp
TEST("finder", "F1 returns -1 when no matching slot") {
    ncland_wh_t wh;
    ncland_wh_init_for_test(&wh);          /* if such helper exists;
                                              otherwise construct wh with
                                              two conns manually per the
                                              existing test harness */
    /* insert one conn (neid=5, slot=12) */
    /* ... */
    REQUIRE(ncland_find_conn_by_neid_slot(&wh, 5, 99) == -1);
}
TEST("finder", "F2 returns id matching neid+slot") {
    /* two conns with same neid, different slots */
    /* insert (neid=5, slot=12) as id 0 and (neid=5, slot=13) as id 1 */
    REQUIRE(ncland_find_conn_by_neid_slot(&wh, 5, 13) == 1);
}
```

If ncland_notify.cpp has no existing test harness for the warehouse, add the finder as a thin wrapper first and gate the tests behind whatever seam the file already supports (grep for `g_test_open_calls` / `g_test_close_calls` usage and follow that pattern).

- [ ] **Step 3: Implement**

In `ncland_notify.cpp`, next to `ncland_find_conn_by_neid` (~line 155):

```cpp
int ncland_find_conn_by_neid_slot(ncland_wh_t *wh, int neid, int slot)
{
    /* Follow the exact iteration idiom of ncland_find_conn_by_neid.
       Match on both neid and slot. */
    for (int i = 0; i < wh->count; i++) {
        if (wh->conns[i].neid == neid && wh->conns[i].slot == slot)
            return i;
    }
    return -1;
}
```

(Field names `wh->count` / `wh->conns[i].neid` / `.slot` may differ — mirror the existing `ncland_find_conn_by_neid` exactly, adding a `.slot ==` clause.)

- [ ] **Step 4: Run tests**

```
cd cnc/ncland/src && nmake ncland_unit_tests && ./ncland_unit_tests
```

Expected: two new `finder` tests pass.

- [ ] **Step 5: Commit**

```
git add cnc/ncland/src/ncland_notify.cpp cnc/ncland/src/ncland_notify.h
git commit -m "ncland: add slot-aware conn finder"
```

---

## Task 8: Slot-aware DISABLE dispatch + snmpLinkStat writeback

**Files:**
- Modify: `cnc/ncland/src/ncland_notify.cpp` (dispatch + writeback)

- [ ] **Step 1: Write the failing test**

Add a test that fires an `NCLAND_EVT_NE_DISABLE` event with a `slot` field into a warehouse populated with two conns sharing the same neid but different slots, and asserts that only the matching-slot conn's close counter increments:

```cpp
TEST("dispatch", "X1 DISABLE closes only the matching slot") {
    /* Reset g_test_close_calls, populate two conns, dispatch,
       assert g_test_close_calls incremented by exactly one and the
       remaining conn is the non-matching-slot one. */
}
```

- [ ] **Step 2: Modify the DISABLE branch of `ncland_notify_dispatch` (~line 216)**

Current:

```cpp
case NCLAND_EVT_NE_DELETE:
case NCLAND_EVT_NE_DISABLE:
    try_close(wh, e->neid);
    break;
```

New: split them so DISABLE is slot-aware:

```cpp
case NCLAND_EVT_NE_DELETE:
    try_close(wh, e->neid);                /* per-NE, all slots */
    break;
case NCLAND_EVT_NE_DISABLE: {
    int id = ncland_find_conn_by_neid_slot(wh, e->neid, e->slot);
    if (id >= 0) try_close_by_id(wh, id);  /* see step 3 */
    break;
}
```

- [ ] **Step 3: Add `try_close_by_id`**

The existing `try_close(wh, neid)` calls `ncland_find_conn_by_neid` then closes. Refactor: extract the "close by conn index" body into `static int try_close_by_id(ncland_wh_t *wh, int id)`; have `try_close(wh, neid)` call it. This keeps the existing `g_test_close_calls` bump path and gives DISABLE a slot-precise entry.

- [ ] **Step 4: Add snmpLinkStat writeback (design doc §Components → ncland)**

After a successful `try_open` and after a successful `try_close_by_id`, call `upd_es64_snmp_info_change_snmpLinkStat` to reflect the CLI-side link state back into ES64 (which cascades to PG `ne_link_status` and frame_link — see design doc's helper-safety analysis; already verified safe from ncland).

Add near the top of `ncland_notify.cpp`:

```cpp
extern "C" {
#include <alcatel_es64_db.h>   /* ES64_SNMP_INFO_REC, upd_es64_snmp_info_change_snmpLinkStat */
#include <msgscreen.h>         /* DST_CLI */
}

/**
 * @brief Write CLI link status back to the ES64 record.
 * @param up 1 = link up, 0 = link down.
 */
static void writeback_link_status(int neid, int slot, int type, int up)
{
    ES64_SNMP_INFO_REC rec;
    memset(&rec, 0, sizeof(rec));
    rec.neId          = neid;
    rec.slot          = slot;
    rec.type          = DST_CLI;
    rec.snmpLinkStat  = up ? 1 : 0;
    (void)type;                            /* passed for future use */
    (void)upd_es64_snmp_info_change_snmpLinkStat(&rec);
    /* Failure is best-effort per design doc; next SNMP poll reconciles. */
}
```

Then wire it into try_open / try_close_by_id:

- After `try_open` reports success: `writeback_link_status(neid, slot, type, 1)`
- After `try_close_by_id` reports success: `writeback_link_status(neid, slot, type, 0)`

Guard both calls behind a `#ifndef NCLAND_UNIT_TEST` compile flag if the unit-test build shouldn't hit the real ES64 c-tree; check whether the `ncland_unit_tests` target sets such a flag today — grep the Makefile. If it doesn't, add `-DNCLAND_UNIT_TEST` to the test-only .o compile step and wrap `writeback_link_status`'s body in that guard.

- [ ] **Step 5: Run tests**

```
cd cnc/ncland/src && nmake ncland_unit_tests && ./ncland_unit_tests && nmake ncland
```

Expected: dispatch test passes; ncland binary still builds.

- [ ] **Step 6: Commit**

```
git add cnc/ncland/src/ncland_notify.cpp
git commit -m "ncland: slot-aware DISABLE dispatch + snmpLinkStat writeback"
```

---

## Task 9: Watch loop (SUB + prime + dispatch + emit)

**Files:**
- Create: `cnc/ncland/src/nclan_seed_watch.h`
- Create: `cnc/ncland/src/nclan_seed_watch.cpp`
- Create: `cnc/ncland/src/nclan_seed_watch_tests.cpp`
- Modify: `cnc/ncland/src/Makefile`

- [ ] **Step 1: Header defines the loop entry + one testable pure function**

`cnc/ncland/src/nclan_seed_watch.h`:

```cpp
#ifndef NCLAN_SEED_WATCH_H
#define NCLAN_SEED_WATCH_H

#include <string>
#include "nclan_seed_cache.h"

struct NFDB;   /* opaque */

/**
 * @brief Emit callback: publishes a single (topic, body) pair. Returns 0/-1.
 */
using NclanSeedEmitFn = int (*)(void *ctx, const char *topic,
                                const std::string &body);

/**
 * @brief React to one notify by fresh-reading, diffing, and emitting.
 *        Pure with respect to injected reader/emitter — unit-testable.
 * @param cache Shared cache; updated in place with the fresh row.
 * @param neid  Parsed from the notify payload.
 * @param slot  Parsed from the notify payload (-1 for NE-level notify).
 * @param read  Fresh-read function; returns 0/-1.
 * @param emit  Emit callback.
 * @param ctx   Opaque context passed to emit.
 * @return 0 on success (including the "no emit" case); -1 on read failure.
 */
int nclan_seed_watch_handle_one(
    NclanSeedCache &cache,
    int neid, int slot,
    int (*read)(int neid, int slot, NclanSeedRow *out),
    NclanSeedEmitFn emit,
    void *ctx);

/**
 * @brief Run the resident watch loop. Blocks until SIGTERM/SIGINT.
 * @param nfdb REQ-connected NFDB* used for PUBLISH commands.
 * @param sub_endpoint  ZMQ SUB endpoint, e.g. "ipc:///usr/cnc/data/nfdb_pub.sock".
 * @return 0 on clean shutdown; non-zero on unrecoverable init failure.
 */
int nclan_seed_watch_run(NFDB *nfdb, const char *sub_endpoint);

#endif
```

- [ ] **Step 2: Write failing tests for `handle_one`**

`cnc/ncland/src/nclan_seed_watch_tests.cpp`:

```cpp
#include "../../../include/nfunit-test.hpp"
#include "nclan_seed_watch.h"
#include <vector>

struct Emitted { std::string topic; std::string body; };
static std::vector<Emitted> emissions;
static int record_emit(void *, const char *topic, const std::string &body) {
    emissions.push_back({topic, body});
    return 0;
}

static NclanSeedRow g_fresh;
static int stub_read(int, int, NclanSeedRow *out) { *out = g_fresh; return 0; }
static int fail_read(int, int, NclanSeedRow *)    { return -1; }

TEST("watch", "W1 first-sight enabled emits db.ne.enable") {
    NclanSeedCache c; emissions.clear();
    g_fresh = NclanSeedRow{1, "10.0.0.1", 23, 245, 1};
    int rc = nclan_seed_watch_handle_one(c, 5, 12, stub_read, record_emit, nullptr);
    REQUIRE(rc == 0);
    REQUIRE(emissions.size() == 1);
    REQUIRE(emissions[0].topic == "db.ne.enable");
    NclanSeedRow chk; REQUIRE(c.get(5, 12, &chk) == true);
    REQUIRE(chk.enabled == 1);
}
TEST("watch", "W2 no change = no emit") {
    NclanSeedCache c; emissions.clear();
    NclanSeedRow r{1, "10.0.0.1", 23, 245, 1};
    c.set(5, 12, r);
    g_fresh = r;
    nclan_seed_watch_handle_one(c, 5, 12, stub_read, record_emit, nullptr);
    REQUIRE(emissions.empty());
}
TEST("watch", "W3 ip change emits db.ne.update") {
    NclanSeedCache c; emissions.clear();
    c.set(5, 12, NclanSeedRow{1, "10.0.0.1", 23, 245, 1});
    g_fresh = NclanSeedRow{1, "10.0.0.2", 23, 245, 1};
    nclan_seed_watch_handle_one(c, 5, 12, stub_read, record_emit, nullptr);
    REQUIRE(emissions.size() == 1);
    REQUIRE(emissions[0].topic == "db.ne.update");
}
TEST("watch", "W4 enabled -> disabled emits db.ne.disable") {
    NclanSeedCache c; emissions.clear();
    c.set(5, 12, NclanSeedRow{1, "10.0.0.1", 23, 245, 1});
    g_fresh = NclanSeedRow{0, "10.0.0.1", 23, 245, 1};
    nclan_seed_watch_handle_one(c, 5, 12, stub_read, record_emit, nullptr);
    REQUIRE(emissions.size() == 1);
    REQUIRE(emissions[0].topic == "db.ne.disable");
}
TEST("watch", "W5 read failure returns -1, no emit, cache untouched") {
    NclanSeedCache c; emissions.clear();
    c.set(5, 12, NclanSeedRow{1, "prior", 1, 1, 1});
    int rc = nclan_seed_watch_handle_one(c, 5, 12, fail_read, record_emit, nullptr);
    REQUIRE(rc == -1);
    REQUIRE(emissions.empty());
    NclanSeedRow chk; c.get(5, 12, &chk);
    REQUIRE(chk.ip == "prior");
}
```

- [ ] **Step 3: Implement `handle_one` (skeleton for `run` — returns 0 for now)**

`cnc/ncland/src/nclan_seed_watch.cpp`:

```cpp
#include "nclan_seed_watch.h"
#include "nclan_seed_fmt.h"

/* NeIdentity → body reuse via nclan_seed_fmt helpers. */
static std::string body_enable(const NclanSeedRow &r, int neid, int slot) {
    NeIdentity ne{neid, r.dtype, r.ip, "", slot, r.port,
                  /*ssh_flag*/-1};   /* watcher does not know ssh_flag;
                                        parser tolerates -1 */
    return ncland_seed_format_enable(ne);
}
static std::string body_update(const NclanSeedRow &r, int neid, int slot) {
    NeIdentity ne{neid, r.dtype, r.ip, "", slot, r.port, -1};
    return ncland_seed_format_update(ne);
}
static std::string body_disable(int neid, int slot) {
    NeIdentity ne{neid, 0, "", "", slot, 0, 0};
    return ncland_seed_format_disable(ne);
}

int nclan_seed_watch_handle_one(
    NclanSeedCache &cache, int neid, int slot,
    int (*read)(int, int, NclanSeedRow *),
    NclanSeedEmitFn emit, void *ctx)
{
    NclanSeedRow fresh;
    if (read(neid, slot, &fresh) != 0) return -1;

    NclanSeedRow prior;
    bool have_prior = cache.get(neid, slot, &prior);
    NclanSeedDiff d = NclanSeedCache::diff(have_prior, prior, fresh);
    cache.set(neid, slot, fresh);

    switch (d) {
    case NclanSeedDiff::ENABLE:
        return emit(ctx, "db.ne.enable",  body_enable (fresh, neid, slot));
    case NclanSeedDiff::DISABLE:
        return emit(ctx, "db.ne.disable", body_disable(neid, slot));
    case NclanSeedDiff::UPDATE:
        return emit(ctx, "db.ne.update",  body_update (fresh, neid, slot));
    case NclanSeedDiff::NONE:
    default:
        return 0;
    }
}

int nclan_seed_watch_run(NFDB *, const char *) {
    /* Implemented in Task 10. */
    return 0;
}
```

- [ ] **Step 4: Wire Makefile**

Add `nclan_seed_watch.o` to `nclan-seed` target (line 42) and `nclan_seed_watch.o nclan_seed_watch_tests.o` to `ncland_unit_tests ::` (line 36).

- [ ] **Step 5: Run tests**

```
cd cnc/ncland/src && nmake ncland_unit_tests && ./ncland_unit_tests
```

Expected: five `watch` tests pass.

- [ ] **Step 6: Commit**

```
git add cnc/ncland/src/nclan_seed_watch.h cnc/ncland/src/nclan_seed_watch.cpp cnc/ncland/src/nclan_seed_watch_tests.cpp cnc/ncland/src/Makefile
git commit -m "nclan_seed: watch dispatch (handle_one) with unit tests"
```

---

## Task 10: Watch loop I/O — SUB init, prime, poll, emit

**Files:**
- Modify: `cnc/ncland/src/nclan_seed_watch.cpp` (fill in `nclan_seed_watch_run`)

No unit tests — this is I/O glue exercised in Task 11's integration smoke.

- [ ] **Step 1: Add extern C declarations for nfdb + SUB endpoint constant**

Near the top of `nclan_seed_watch.cpp`:

```cpp
#include <cstdio>
#include <cstring>
#include <signal.h>
#include <zmq.h>
#include "nclan_seed_read.h"

extern "C" {
    struct NFDB;
    int nfdb_command(NFDB *conn, const char *fmt, ...);
}

static volatile sig_atomic_t g_stop = 0;
static void on_signal(int) { g_stop = 1; }
```

- [ ] **Step 2: Emit callback that wraps nfdb_command**

```cpp
struct EmitCtx { NFDB *nfdb; };
static int emit_via_nfdb(void *ctx, const char *topic,
                         const std::string &body)
{
    EmitCtx *c = static_cast<EmitCtx *>(ctx);
    int rc = nfdb_command(c->nfdb, "PUBLISH %s \"%s\"",
                          topic, body.c_str());
    if (rc != 0) {
        fprintf(stderr, "watch: nfdb PUBLISH %s failed rc=%d\n",
                topic, rc);
    }
    return rc;
}
```

- [ ] **Step 3: Parse the notify payload → (neid, slot)**

The two channels have different payload shapes (design doc §Data flow):

- `channel_frmlnk_link_data_change` → `<ne_id>|<prot>|<type>|<slot>`
- `channel_frmlnk_ne_change` → `<ne_id>`

Add:

```cpp
struct NotifyKey { int neid; int slot;  /* slot = -1 for NE-level */ };

static bool parse_link_data_payload(const std::string &s, NotifyKey *out) {
    /* Split on '|'; take first and fourth. */
    size_t p1 = s.find('|');           if (p1 == std::string::npos) return false;
    size_t p2 = s.find('|', p1 + 1);   if (p2 == std::string::npos) return false;
    size_t p3 = s.find('|', p2 + 1);   if (p3 == std::string::npos) return false;
    out->neid = atoi(s.substr(0, p1).c_str());
    out->slot = atoi(s.substr(p3 + 1).c_str());
    return true;
}
static bool parse_ne_payload(const std::string &s, NotifyKey *out) {
    out->neid = atoi(s.c_str());
    out->slot = -1;
    return out->neid > 0;
}
```

- [ ] **Step 4: Implement the loop**

Replace the `return 0;` stub in `nclan_seed_watch_run`:

```cpp
int nclan_seed_watch_run(NFDB *nfdb, const char *sub_endpoint)
{
    /* Signal shutdown. */
    signal(SIGTERM, on_signal);
    signal(SIGINT,  on_signal);

    /* ZMQ SUB. */
    void *ctx = zmq_ctx_new();
    void *sub = zmq_socket(ctx, ZMQ_SUB);
    if (!sub) { fprintf(stderr, "watch: zmq_socket SUB failed\n"); return 2; }
    if (zmq_setsockopt(sub, ZMQ_SUBSCRIBE,
                       "db.otnport.channel_frmlnk_link_data_change",
                       42) != 0 ||
        zmq_setsockopt(sub, ZMQ_SUBSCRIBE,
                       "db.otnport.channel_frmlnk_ne_change",
                       35) != 0) {
        fprintf(stderr, "watch: subscribe failed: %s\n", zmq_strerror(errno));
        return 2;
    }
    if (zmq_connect(sub, sub_endpoint) != 0) {
        fprintf(stderr, "watch: connect(%s): %s\n",
                sub_endpoint, zmq_strerror(errno));
        return 2;
    }

    NclanSeedCache cache;
    EmitCtx emit_ctx{nfdb};

    /* TODO(prime): walk ES64 c-tree and prime the cache. Skipping
       here means the first notify per (neid, slot) will treat that
       slot as "absent" — a fresh disabled row emits nothing, a fresh
       enabled row emits db.ne.enable. Both are safe on ncland
       (idempotent). Add priming as a follow-up if the boot-time
       false-enable events become noisy. */

    /* Poll loop. */
    while (!g_stop) {
        zmq_pollitem_t items[1] = { { sub, 0, ZMQ_POLLIN, 0 } };
        int rc = zmq_poll(items, 1, 1000 /*ms*/);
        if (rc < 0) {
            if (errno == EINTR) continue;
            fprintf(stderr, "watch: zmq_poll: %s\n", zmq_strerror(errno));
            break;
        }
        if (rc == 0) continue;   /* timeout tick */

        /* Frame 0 = topic, frame 1 = payload. */
        char topic[128] = {0};
        char body [256] = {0};
        int  tlen = zmq_recv(sub, topic, sizeof(topic) - 1, 0);
        if (tlen < 0) continue;
        int  more = 0; size_t ms = sizeof(more);
        zmq_getsockopt(sub, ZMQ_RCVMORE, &more, &ms);
        int  blen = 0;
        if (more) blen = zmq_recv(sub, body, sizeof(body) - 1, 0);
        (void)blen;

        std::string t(topic, tlen);
        std::string b(body,  blen > 0 ? blen : 0);

        NotifyKey k;
        if (t.find("link_data_change") != std::string::npos) {
            if (!parse_link_data_payload(b, &k)) continue;
            nclan_seed_watch_handle_one(cache, k.neid, k.slot,
                                        nclan_seed_read_wrapper,
                                        emit_via_nfdb, &emit_ctx);
        } else if (t.find("ne_change") != std::string::npos) {
            if (!parse_ne_payload(b, &k)) continue;
            /* NE-level: iterate cached slots for this neid and re-diff each. */
            /* Minimal implementation: rely on subsequent link_data_change
               events to catch per-slot changes; skip here. */
        }
    }

    zmq_close(sub);
    zmq_ctx_term(ctx);
    return 0;
}
```

- [ ] **Step 5: Add the read wrapper**

Above `nclan_seed_watch_run`, add:

```cpp
static int nclan_seed_read_wrapper(int neid, int slot, NclanSeedRow *out) {
    return nclan_seed_read_row(neid, slot, out,
                               nclan_seed_pg_enabled,
                               nclan_seed_ctree_row);
}
```

- [ ] **Step 6: Build**

```
cd cnc/ncland/src && nmake nclan-seed && nmake ncland_unit_tests && ./ncland_unit_tests
```

Expected: nclan-seed builds; unit tests still green (I/O code is not unit-tested here).

- [ ] **Step 7: Commit**

```
git add cnc/ncland/src/nclan_seed_watch.cpp
git commit -m "nclan_seed: watch loop SUB + poll + emit"
```

---

## Task 11: CLI wiring for --watch

**Files:**
- Modify: `cnc/ncland/src/nclan_seed.cpp` (getopt string + branch)

- [ ] **Step 1: Extend getopt at line 110**

Change:

```cpp
while ((opt = getopt(argc, argv, "e:nh")) != -1) {
```

to:

```cpp
while ((opt = getopt(argc, argv, "e:nhw")) != -1) {
```

Add the flag holder near the other flag variables at the top of `main`:

```cpp
bool watch = false;
```

Add the case in the switch:

```cpp
case 'w': watch = true; break;
```

- [ ] **Step 2: Branch to the watch loop after seed pass completes**

Find the point at which the seed pass finishes (near line 195 after the last `nfdb_command` in the loop and before `return 0;` at line 211). Add before the return:

```cpp
if (watch) {
    #include "nclan_seed_watch.h"   /* move to top-of-file includes */
    /* Seed pass done; hand off to the resident loop. */
    int rc = nclan_seed_watch_run(conn, "ipc:///usr/cnc/data/nfdb_pub.sock");
    nfdb_close(conn);
    return rc;
}
```

(Move the `#include "nclan_seed_watch.h"` to the top-of-file include block; the comment above is illustrative only.)

- [ ] **Step 3: Update usage text**

Grep for the existing usage/help text in `nclan_seed.cpp` and add:

```
  -w    Watch mode: after seed pass, remain resident and translate
        db.otnport.* notifies into db.ne.enable/disable/update events.
```

- [ ] **Step 4: Build and run one-shot smoke**

```
cd cnc/ncland/src && nmake nclan-seed
$PBIN/nclan-seed -h                   # help text now mentions -w
$PBIN/nclan-seed -n                   # dry-run one-shot still works
```

- [ ] **Step 5: Commit**

```
git add cnc/ncland/src/nclan_seed.cpp
git commit -m "nclan_seed: --watch CLI flag hands off to watch loop"
```

---

## Task 12: Manual integration smoke (documented, not automated)

**Files:**
- No source changes. Create `cnc/ncland/src/WATCH-SMOKE.md` with the smoke steps below.

- [ ] **Step 1: Author the smoke doc**

`cnc/ncland/src/WATCH-SMOKE.md`:

```markdown
# nclan_seed --watch smoke

Pre-req: `OTN_PORTD_ZMQ_PUB=1` in the otn_portd environment;
niimxd running with nfdb XPUB/XSUB bound.

1. Start ncland (existing systemd unit or manual).
2. Start `nclan-seed -w &` (or via its unit).
3. In another shell, tail niimxd's PUB stream with the debug subscriber
   (whatever tool the project uses for `db.ne.*`).
4. Toggle `es64_snmp_info.snmpPort` for a lab interface via the `snmpne`
   tool. Expect: `db.ne.update` in the tail; ncland closes+reopens the
   CLI conn for that slot; `ne_link_status` in PG reflects the new state.
5. Disable a card via the GUI. Expect: `db.ne.disable`; ncland closes
   the matching-slot conn; `es64_snmp_info.snmpLinkStat` = 0.
6. Re-enable. Expect: `db.ne.enable`; ncland opens the conn;
   `snmpLinkStat` = 1 on open success.
7. Send SIGTERM to nclan-seed. Verify clean exit (no leftover ZMQ
   contexts, no stuck ncland conns).
```

- [ ] **Step 2: Commit**

```
git add cnc/ncland/src/WATCH-SMOKE.md
git commit -m "nclan_seed: manual watch smoke doc"
```

---

## Deferred (out of scope for this plan)

Per design doc §Out of scope:

- SIGHUP cache rebuild.
- 5-minute bus-silence warning at boot.
- Automated broker-restart recovery tests.
- Multi-instance pidfile guard at `/var/run/nclan_seed_watch.pid`.
- Priming the cache at boot by walking the ES64 c-tree (see TODO(prime)
  comment in `nclan_seed_watch_run`). First notify per slot behaves
  correctly without it; add if boot-time false-enables become noisy.
- NE-level `channel_frmlnk_ne_change` per-slot re-diff. First
  implementation relies on the coincident per-link notifies to cover
  slot changes; revisit if a real-world NE-add/delete pattern shows
  gaps.

## Self-review notes

- Cache module Tasks 1-2 build together — running Task 2 before Task 1 will fail to link `NclanSeedCache::diff`.
- Task 6 introduces `db.ne.disable` parser tolerance; Task 3's smoke
  formatter test does not depend on it. Round-trip formatter tests
  belong in Task 6 (parser side) not Task 3.
- Task 8 refactors `try_close` into `try_close_by_id` — verify the
  existing `g_test_close_calls` bump moves with the extracted body,
  otherwise F1/F2 finder tests and any prior close tests may drift.
- Task 10 leaves NE-level `channel_frmlnk_ne_change` handling minimal
  and unpriming — both documented as deferred; do not treat as bugs
  during review.
- Task 5's RDB accessor names (`rdb_exec_query`, `rdb_row_get`, etc.)
  are the codebase idiom but the exact spelling may differ in this
  branch. Cross-check against `rdb_otn_port_ne_link_data.c` before
  merging.
