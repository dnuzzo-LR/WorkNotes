# ncland Multi-Process Group Support — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Split `ncland` into a router-parent + per-group warehouse-children, driven by a new `groups[]` schema in `/usr/cnc/lib/data/ncland/ncland.json`. Each child owns its own POSIX MQ (`/ncland_ctl_g<idx>`) and filters NEs by (neid_range ∩ dtype allowlist).

**Architecture:** Parent process reads the config, spawns one child per group with `--group-index N`, and routes inbound `dacs_msg` on `/ncland_ctl` to the child that owns the target neid. Children run the existing warehouse code path unchanged except for (a) reading their group config, (b) using a per-group MQ name, and (c) applying an neid-range filter in seed and notify dispatch. Parent supervises children with exponential backoff restart; forks `nclan-seed` once after all children are up.

**Tech Stack:** C++17, Lucent nmake, POSIX mqueue, nlohmann::json, nfunit-test, ZMQ SUB (unchanged), libssh (unchanged).

**Spec:** `~/WorkNotes/plans/2026-07-17-ncland-multiprocess-groups-design.md`

---

## File Structure

**New files:**

| File | Purpose |
|---|---|
| `cnc/ncland/src/ncland_router.h` | Router types + prototypes: `router_child_t`, `ncland_router_t`, `router_init/spawn_child/route_msg/run/shutdown`. |
| `cnc/ncland/src/ncland_router.cpp` | Router process implementation: config→children spawn, epoll on `/ncland_ctl`+signalfd+timerfd, SIGCHLD-driven backoff restart, forward `dacs_msg` to per-group child MQ, seed fork after all children up. |
| `cnc/ncland/src/ncland_args.cpp` | (Added during Task 6.) `config_defaults` + `ncland_parse_args`. Extracted from `ncland.cpp` so the argv tests can link `ncland_parse_args` without pulling in `main()` (which conflicts with the nfunit test framework's own `main`). |
| `cnc/ncland/src/test/fixtures/ncland_groups_good.json` | Two-group happy-path fixture used by parser + integration tests. |
| `cnc/ncland/src/test/fixtures/ncland_groups_overlap.json` | Overlap fixture (parser must reject). |
| `cnc/ncland/src/test/fixtures/ncland_groups_legacy.json` | Legacy top-level `"dtypes"` fixture (parser must reject with migration message). |

**Modified files:**

| File | Change |
|---|---|
| `cnc/ncland/src/ncland.h` | Add `ncland_group_t`, `ncland_config_load_groups()` proto. Add `group_index`, `neid_lo`, `neid_hi`, `mq_name[64]` to `ncland_config_t`. Add `neid_lo`, `neid_hi` to `ncland_wh_t`. |
| `cnc/ncland/src/ncland.cpp` | `getopt_long` for `--group-index`. `main()` branches: `group_index < 0` → `router_run`; else warehouse. Delete the inline `nclan-seed` fork (moved into router). |
| `cnc/ncland/src/ncland_seed.cpp` | Add `ncland_config_load_groups()`. Retire `ncland_seed_load_filter()` (marked deprecated + kept only if any external caller remains; if none, delete). `seed_cb` gains `neid_lo/hi` guard. |
| `cnc/ncland/src/ncland_warehouse.cpp` | `warehouse_init`: call `ncland_config_load_groups`, select `cfg->group_index`, populate `wh->dtype_allow` + `wh->neid_lo/hi`. Pass `cfg->mq_name` to `ncland_mq_init`. |
| `cnc/ncland/src/ncland_notify.cpp` | `try_open` / dispatch: guard by `wh->neid_lo/hi`. |
| `cnc/ncland/src/ncland_unit_tests.cpp` | New suite `groups` (parser). New suite `router` (routing + backoff). Extend suite `notify` (neid-range guard). |
| `cnc/ncland/src/Makefile` | Add `ncland_router.o` to `NCLAND_OBJS`. |

Split rationale: router process is a distinct responsibility from the warehouse; putting it in its own translation unit keeps `ncland.cpp` a thin argv+dispatch shim and lets the router be unit-tested in isolation from any warehouse `conn` table state.

---

## Task 1: Config parser — types + prototype

**Files:**
- Modify: `cnc/ncland/src/ncland.h`

**Purpose:** Land the shared types before code that uses them. No behavior change.

- [ ] **Step 1: Add `ncland_group_t` and `ncland_config_load_groups()` prototype**

Add after the existing `struct ncland_seed_ne_t` definition in `ncland.h` (near line 514):

```c
/* ------------------------------------------------------------------ */
/* Group config (ncland.json v1 "groups" schema)                       */
/* ------------------------------------------------------------------ */

/** @brief One entry from ncland.json "groups" array. */
struct ncland_group_t {
    std::string             group_desc;  /**< Verbatim "group" field; log-only. */
    int                     neid_lo;     /**< Inclusive lower bound. */
    int                     neid_hi;     /**< Inclusive upper bound. */
    std::unordered_set<int> dtypes;      /**< Allowed dtypes for this group. */
};

/** @brief Load ncland.json groups[] into *out.
 *
 *  Validates: version == 1; groups[] non-empty; each entry has group (string),
 *  neid_range [start,end] with 0 <= start <= end, dtypes (non-empty int array);
 *  no two groups' neid_range intervals overlap; the legacy top-level "dtypes"
 *  key is rejected.
 *
 *  @param path Filesystem path.
 *  @param out  Destination (cleared on success).
 *  @return 0 on success, -1 on any file/parse/schema/overlap error (logged). */
int ncland_config_load_groups(const char *path,
                              std::vector<ncland_group_t> *out);
```

Add `#include <vector>` to the top of `ncland.h` if not already present (it isn't).

- [ ] **Step 2: Verify compile**

Run: `cd /home/dan/Git/netflex/cnc/ncland/src && nmake ncland_unit_tests.o`
Expected: clean compile (no other source references the new symbols yet).

- [ ] **Step 3: Commit**

```bash
git add cnc/ncland/src/ncland.h
git commit -m "ncland: add ncland_group_t and load_groups prototype"
```

---

## Task 2: Config parser — happy-path test + fixture

**Files:**
- Create: `cnc/ncland/src/test/fixtures/ncland_groups_good.json`
- Modify: `cnc/ncland/src/ncland_unit_tests.cpp`

- [ ] **Step 1: Write the fixture**

File `cnc/ncland/src/test/fixtures/ncland_groups_good.json`:

```json
{
  "version": 1,
  "groups": [
    { "group": "Group One", "neid_range": [1, 500],    "dtypes": [206, 207] },
    { "group": "Group Two", "neid_range": [501, 1000], "dtypes": [223, 254] }
  ]
}
```

- [ ] **Step 2: Write the failing test**

Add to `ncland_unit_tests.cpp` under a new `groups` suite (after the existing `seed` suite tests, before the notify suite):

```cpp
/* ------------------------------------------------------------------ */
/* T18: ncland.json groups[] loader                                    */
/* ------------------------------------------------------------------ */

TEST("groups", "T18.1 load_groups parses two-group fixture") {
    std::vector<ncland_group_t> gs;
    int rc = ncland_config_load_groups(
        "test/fixtures/ncland_groups_good.json", &gs);
    REQUIRE_EQ(rc, 0);
    REQUIRE_EQ((int)gs.size(), 2);
    REQUIRE_EQ(gs[0].neid_lo, 1);
    REQUIRE_EQ(gs[0].neid_hi, 500);
    REQUIRE_EQ((int)gs[0].dtypes.size(), 2);
    REQUIRE(gs[0].dtypes.count(206) == 1);
    REQUIRE(gs[0].dtypes.count(207) == 1);
    REQUIRE_EQ(gs[1].neid_lo, 501);
    REQUIRE_EQ(gs[1].neid_hi, 1000);
    REQUIRE(gs[1].dtypes.count(223) == 1);
    REQUIRE(gs[1].dtypes.count(254) == 1);
    REQUIRE_EQ(gs[0].group_desc, std::string("Group One"));
}
```

- [ ] **Step 3: Run test to verify it fails**

Run: `cd /home/dan/Git/netflex/cnc/ncland/src && nmake ncland_unit_tests`
Expected: link error `undefined reference to 'ncland_config_load_groups'`.

- [ ] **Step 4: Implement minimal parser body**

Add to `ncland_seed.cpp` (after `ncland_seed_load_filter`):

```cpp
/**
 * @brief Load ncland.json groups[] into *out. See header for schema rules.
 */
int ncland_config_load_groups(const char *path,
                              std::vector<ncland_group_t> *out)
{
    LOG_DEBUG("ncland_config_load_groups: path=%s out=%p",
              path ? path : "(null)", (void *)out);
    if (!path || !out) return -1;

    std::ifstream in(path);
    if (!in.is_open()) {
        LOG_ERROR("groups: cannot read %s", path);
        return -1;
    }

    nlohmann::json root;
    try { in >> root; }
    catch (const std::exception &e) {
        LOG_ERROR("groups: malformed JSON in %s: %s", path, e.what());
        return -1;
    }

    if (!root.is_object()
        || !root.contains("version")
        || !root["version"].is_number_integer()
        || root["version"].get<int>() != NCLAND_JSON_VERSION) {
        LOG_ERROR("groups: unsupported/missing version in %s", path);
        return -1;
    }

    if (root.contains("dtypes")) {
        LOG_ERROR("groups: legacy top-level \"dtypes\" in %s — migrate to \"groups\"", path);
        return -1;
    }

    if (!root.contains("groups") || !root["groups"].is_array()
        || root["groups"].empty()) {
        LOG_ERROR("groups: \"groups\" missing/empty in %s", path);
        return -1;
    }

    std::vector<ncland_group_t> parsed;
    for (const auto &g : root["groups"]) {
        if (!g.is_object()
            || !g.contains("group")      || !g["group"].is_string()
            || !g.contains("neid_range") || !g["neid_range"].is_array()
            || g["neid_range"].size() != 2
            || !g["neid_range"][0].is_number_integer()
            || !g["neid_range"][1].is_number_integer()
            || !g.contains("dtypes")     || !g["dtypes"].is_array()
            || g["dtypes"].empty()) {
            LOG_ERROR("groups: malformed group entry in %s", path);
            return -1;
        }
        ncland_group_t e;
        e.group_desc = g["group"].get<std::string>();
        e.neid_lo    = g["neid_range"][0].get<int>();
        e.neid_hi    = g["neid_range"][1].get<int>();
        if (e.neid_lo < 0 || e.neid_hi < e.neid_lo) {
            LOG_ERROR("groups: bad neid_range [%d,%d] in %s",
                      e.neid_lo, e.neid_hi, path);
            return -1;
        }
        for (const auto &dt : g["dtypes"]) {
            if (!dt.is_number_integer()) {
                LOG_ERROR("groups: non-integer dtype in %s", path);
                return -1;
            }
            e.dtypes.insert(dt.get<int>());
        }
        parsed.push_back(std::move(e));
    }

    /* Overlap check: sort by lo, then any adjacent pair with prev.hi >= next.lo overlaps. */
    std::vector<ncland_group_t> sorted = parsed;
    std::sort(sorted.begin(), sorted.end(),
              [](const ncland_group_t &a, const ncland_group_t &b) {
                  return a.neid_lo < b.neid_lo;
              });
    for (size_t i = 1; i < sorted.size(); i++) {
        if (sorted[i].neid_lo <= sorted[i-1].neid_hi) {
            LOG_ERROR("groups: overlap between \"%s\" [%d,%d] and \"%s\" [%d,%d] in %s",
                      sorted[i-1].group_desc.c_str(), sorted[i-1].neid_lo, sorted[i-1].neid_hi,
                      sorted[i].group_desc.c_str(),   sorted[i].neid_lo,   sorted[i].neid_hi,
                      path);
            return -1;
        }
    }

    *out = std::move(parsed);
    LOG_INFO("groups: loaded %zu group(s) from %s", out->size(), path);
    return 0;
}
```

Add `#include <algorithm>` at the top of `ncland_seed.cpp` if not already present.

- [ ] **Step 5: Run test to verify it passes**

Run: `nmake ncland_unit_tests && ./ncland_unit_tests -s groups -v`
Expected: `T18.1 load_groups parses two-group fixture` PASS.

- [ ] **Step 6: Commit**

```bash
git add cnc/ncland/src/ncland_seed.cpp cnc/ncland/src/ncland_unit_tests.cpp \
        cnc/ncland/src/test/fixtures/ncland_groups_good.json
git commit -m "ncland: implement groups[] JSON parser with happy-path test"
```

---

## Task 3: Config parser — error-path tests

**Files:**
- Create: `cnc/ncland/src/test/fixtures/ncland_groups_overlap.json`
- Create: `cnc/ncland/src/test/fixtures/ncland_groups_legacy.json`
- Modify: `cnc/ncland/src/ncland_unit_tests.cpp`

- [ ] **Step 1: Write the overlap fixture**

File `cnc/ncland/src/test/fixtures/ncland_groups_overlap.json`:

```json
{
  "version": 1,
  "groups": [
    { "group": "A", "neid_range": [1, 500], "dtypes": [1] },
    { "group": "B", "neid_range": [400, 900], "dtypes": [2] }
  ]
}
```

- [ ] **Step 2: Write the legacy-schema fixture**

File `cnc/ncland/src/test/fixtures/ncland_groups_legacy.json`:

```json
{ "version": 1, "dtypes": [1, 2, 3] }
```

- [ ] **Step 3: Add error-path tests**

Append under the `groups` suite in `ncland_unit_tests.cpp`:

```cpp
TEST("groups", "T18.2 missing file returns -1") {
    std::vector<ncland_group_t> gs;
    REQUIRE_EQ(ncland_config_load_groups("/no/such/path.json", &gs), -1);
}

TEST("groups", "T18.3 overlap between groups rejected") {
    std::vector<ncland_group_t> gs;
    REQUIRE_EQ(ncland_config_load_groups(
        "test/fixtures/ncland_groups_overlap.json", &gs), -1);
}

TEST("groups", "T18.4 legacy top-level dtypes rejected") {
    std::vector<ncland_group_t> gs;
    REQUIRE_EQ(ncland_config_load_groups(
        "test/fixtures/ncland_groups_legacy.json", &gs), -1);
}

TEST("groups", "T18.5 empty groups[] rejected") {
    char tmp[] = "/tmp/ncland_groups_empty_XXXXXX";
    int fd = mkstemp(tmp);
    REQUIRE_GT(fd, 0);
    const char *body = "{\"version\":1,\"groups\":[]}";
    REQUIRE_EQ((int)write(fd, body, strlen(body)), (int)strlen(body));
    close(fd);
    std::vector<ncland_group_t> gs;
    REQUIRE_EQ(ncland_config_load_groups(tmp, &gs), -1);
    unlink(tmp);
}

TEST("groups", "T18.6 inverted neid_range rejected") {
    char tmp[] = "/tmp/ncland_groups_inv_XXXXXX";
    int fd = mkstemp(tmp);
    REQUIRE_GT(fd, 0);
    const char *body =
        "{\"version\":1,\"groups\":[{\"group\":\"X\","
        "\"neid_range\":[500,1],\"dtypes\":[1]}]}";
    REQUIRE_EQ((int)write(fd, body, strlen(body)), (int)strlen(body));
    close(fd);
    std::vector<ncland_group_t> gs;
    REQUIRE_EQ(ncland_config_load_groups(tmp, &gs), -1);
    unlink(tmp);
}

TEST("groups", "T18.7 unknown version rejected") {
    char tmp[] = "/tmp/ncland_groups_v999_XXXXXX";
    int fd = mkstemp(tmp);
    REQUIRE_GT(fd, 0);
    const char *body =
        "{\"version\":999,\"groups\":[{\"group\":\"X\","
        "\"neid_range\":[1,10],\"dtypes\":[1]}]}";
    REQUIRE_EQ((int)write(fd, body, strlen(body)), (int)strlen(body));
    close(fd);
    std::vector<ncland_group_t> gs;
    REQUIRE_EQ(ncland_config_load_groups(tmp, &gs), -1);
    unlink(tmp);
}
```

- [ ] **Step 4: Run the suite**

Run: `nmake ncland_unit_tests && ./ncland_unit_tests -s groups -v`
Expected: T18.1 through T18.7 all PASS.

- [ ] **Step 5: Commit**

```bash
git add cnc/ncland/src/test/fixtures/ncland_groups_overlap.json \
        cnc/ncland/src/test/fixtures/ncland_groups_legacy.json \
        cnc/ncland/src/ncland_unit_tests.cpp
git commit -m "ncland: groups[] parser error-path tests"
```

---

## Task 4: Warehouse — neid_lo/hi field + seed guard

**Files:**
- Modify: `cnc/ncland/src/ncland.h`
- Modify: `cnc/ncland/src/ncland_seed.cpp`
- Modify: `cnc/ncland/src/ncland_unit_tests.cpp`

**Purpose:** Add the range filter in the seed path *before* the warehouse init switches over — keeps the change tightly reviewable and lets existing tests keep passing.

- [ ] **Step 1: Add fields to `ncland_wh_t`**

In `ncland.h` inside `struct ncland_wh` (near the `dtype_allow` line, around line 204):

```c
    /* Group filter — inclusive.  Populated in child-mode warehouse_init from
     * ncland.json[group_index].  In single-group tests, leave as 0..INT_MAX
     * (any neid accepted). */
    int              neid_lo;
    int              neid_hi;
```

- [ ] **Step 2: Write the failing test**

Add under the `seed` suite in `ncland_unit_tests.cpp` (after T15.6):

```cpp
TEST("seed", "T15.7 seed_run filters by neid_lo/hi") {
    ncland_wh_t wh{};
    for (int i = 0; i < MAX_CONNS; i++) conn_init(&wh.conn[i], i);
    wh.dtype_allow.insert(7);
    wh.neid_lo = 100;
    wh.neid_hi = 200;

    extern std::vector<ncland_seed_ne_t> g_test_seed_nes;
    g_test_seed_nes.clear();
    g_test_seed_nes.push_back(make_ne( 99, 1, 7, "10.0.0.1"));  /* below range */
    g_test_seed_nes.push_back(make_ne(100, 1, 7, "10.0.0.2"));  /* lo boundary */
    g_test_seed_nes.push_back(make_ne(150, 1, 7, "10.0.0.3"));  /* inside */
    g_test_seed_nes.push_back(make_ne(200, 1, 7, "10.0.0.4"));  /* hi boundary */
    g_test_seed_nes.push_back(make_ne(201, 1, 7, "10.0.0.5"));  /* above range */

    extern int g_test_seed_open_count;
    g_test_seed_open_count = 0;
    REQUIRE_EQ(ncland_seed_run(&wh), 0);
    REQUIRE_EQ(g_test_seed_open_count, 3);   /* 100, 150, 200 */

    g_test_seed_nes.clear();
}
```

- [ ] **Step 3: Run test to verify it fails**

Run: `nmake ncland_unit_tests && ./ncland_unit_tests -s seed -t "T15.7*" -v`
Expected: `T15.7 seed_run filters by neid_lo/hi` FAIL — open_count is 5, not 3.

- [ ] **Step 4: Implement neid guard in `seed_cb`**

In `ncland_seed.cpp` `seed_cb` (around line 107), add the guard before the dtype check:

```cpp
static void seed_cb(const ncland_seed_ne_t *ne, void *u)
{
    ncland_wh_t *wh = (ncland_wh_t *)u;
    /* Group range filter: 0 sentinel means "any" (single-group tests). */
    if (wh->neid_hi > 0 && (ne->neid < wh->neid_lo || ne->neid > wh->neid_hi))
        return;
    if (wh->dtype_allow.count(ne->dtype) == 0) return;
    if (seed_find_existing(wh, ne->neid) >= 0) return;
    /* ... rest unchanged ... */
```

Add rationale as an inline comment where the sentinel is checked: `wh->neid_hi == 0` means "field never populated" — existing tests that predate this change do not touch neid_lo/hi and must keep passing. Real child processes will always set `neid_hi > 0`.

- [ ] **Step 5: Run tests to verify T15.7 passes and existing seed tests still pass**

Run: `./ncland_unit_tests -s seed -v`
Expected: T15.1 – T15.7 all PASS.

- [ ] **Step 6: Commit**

```bash
git add cnc/ncland/src/ncland.h cnc/ncland/src/ncland_seed.cpp cnc/ncland/src/ncland_unit_tests.cpp
git commit -m "ncland: add wh->neid_lo/hi and enforce in seed_cb"
```

---

## Task 5: Notify — neid_lo/hi guard

**Files:**
- Modify: `cnc/ncland/src/ncland_notify.cpp`
- Modify: `cnc/ncland/src/ncland_unit_tests.cpp`

- [ ] **Step 1: Write the failing test**

Append to the `notify` suite in `ncland_unit_tests.cpp` (after T16.6):

```cpp
TEST("notify", "T16.7 CREATE outside neid range is skipped") {
    ncland_wh_t wh{};
    for (int i = 0; i < MAX_CONNS; i++) conn_init(&wh.conn[i], i);
    wh.dtype_allow.insert(7);
    wh.neid_lo = 100;
    wh.neid_hi = 200;

    extern int g_test_open_calls;
    g_test_open_calls = 0;
    ncland_notify_event_t e = make_evt(NCLAND_EVT_NE_CREATE, 50, 1, 7, "10.0.0.1");
    ncland_notify_dispatch(&wh, &e);
    REQUIRE_EQ(g_test_open_calls, 0);

    e = make_evt(NCLAND_EVT_NE_CREATE, 150, 1, 7, "10.0.0.2");
    ncland_notify_dispatch(&wh, &e);
    REQUIRE_EQ(g_test_open_calls, 1);
}

TEST("notify", "T16.8 DELETE outside neid range is a no-op") {
    ncland_wh_t wh{};
    for (int i = 0; i < MAX_CONNS; i++) conn_init(&wh.conn[i], i);
    wh.neid_lo = 100; wh.neid_hi = 200;

    /* Simulate a rogue conn for an out-of-range neid — dispatch must not touch it. */
    wh.conn[0].state = CS_READY;
    wh.conn[0].neid  = 50;

    extern int g_test_close_calls;
    g_test_close_calls = 0;
    ncland_notify_event_t e = make_evt(NCLAND_EVT_NE_DELETE, 50, 0, 0, "");
    ncland_notify_dispatch(&wh, &e);
    REQUIRE_EQ(g_test_close_calls, 0);
    REQUIRE_EQ((int)wh.conn[0].state, (int)CS_READY);   /* untouched */
}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `./ncland_unit_tests -s notify -t "T16.7*" -v` then `-t "T16.8*"`
Expected: both FAIL (guard not yet implemented).

- [ ] **Step 3: Add guard at the top of `ncland_notify_dispatch`**

In `ncland_notify.cpp`:

```cpp
void ncland_notify_dispatch(ncland_wh_t *wh, const ncland_notify_event_t *e)
{
    if (!wh || !e) return;

    /* Group range filter — every event kind honors it.  0 sentinel = any
     * (single-group tests / legacy warehouse_init that predates groups). */
    if (wh->neid_hi > 0 && (e->neid < wh->neid_lo || e->neid > wh->neid_hi))
        return;

    switch (e->kind) {
    /* ... rest unchanged ... */
```

- [ ] **Step 4: Run notify suite**

Run: `./ncland_unit_tests -s notify -v`
Expected: T16.1 – T16.8 all PASS.

- [ ] **Step 5: Commit**

```bash
git add cnc/ncland/src/ncland_notify.cpp cnc/ncland/src/ncland_unit_tests.cpp
git commit -m "ncland: apply neid_lo/hi guard in notify_dispatch"
```

---

## Task 6: `ncland_config_t` gets group fields + `--group-index` argv

**Files:**
- Modify: `cnc/ncland/src/ncland.h`
- Modify: `cnc/ncland/src/ncland.cpp`
- Modify: `cnc/ncland/src/ncland_unit_tests.cpp`

- [ ] **Step 1: Extend `ncland_config_t`**

In `ncland.h` inside `struct ncland_config` (around line 244):

```c
    int  group_index;                 /**< -1 = parent/router mode; >=0 = child for that group. */
    int  neid_lo;                     /**< Populated in child from ncland.json[group_index]. */
    int  neid_hi;                     /**< Populated in child from ncland.json[group_index]. */
    char mq_name[64];                 /**< "/ncland_ctl" in parent; "/ncland_ctl_g<idx>" in child. */
```

- [ ] **Step 2: Write the failing test**

Add to the `build` suite (or a new `argv` suite) in `ncland_unit_tests.cpp`:

```cpp
TEST("argv", "T19.1 --group-index absent means group_index=-1") {
    char *argv[] = { (char*)"ncland", nullptr };
    ncland_config_t cfg;
    REQUIRE_EQ(ncland_parse_args(1, argv, &cfg), 0);
    REQUIRE_EQ(cfg.group_index, -1);
    REQUIRE_EQ(strcmp(cfg.mq_name, "/ncland_ctl"), 0);
}

TEST("argv", "T19.2 --group-index=2 sets group_index and mq_name") {
    char *argv[] = { (char*)"ncland", (char*)"--group-index", (char*)"2", nullptr };
    ncland_config_t cfg;
    REQUIRE_EQ(ncland_parse_args(3, argv, &cfg), 0);
    REQUIRE_EQ(cfg.group_index, 2);
    REQUIRE_EQ(strcmp(cfg.mq_name, "/ncland_ctl_g2"), 0);
}

TEST("argv", "T19.3 --group-index negative rejected") {
    char *argv[] = { (char*)"ncland", (char*)"--group-index", (char*)"-1", nullptr };
    ncland_config_t cfg;
    REQUIRE_EQ(ncland_parse_args(3, argv, &cfg), -1);
}
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `nmake ncland_unit_tests && ./ncland_unit_tests -s argv -v`
Expected: T19.1–T19.3 FAIL (defaults + long-opt not wired).

- [ ] **Step 4: Wire `--group-index` in `ncland.cpp`**

Replace the top of `ncland.cpp` with:

```cpp
#include "ncland.h"
#include "nflog.hpp"

#include <getopt.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <errno.h>
#include <sys/types.h>
```

Update `config_defaults`:

```cpp
static void config_defaults(ncland_config_t *cfg)
{
    LOG_DEBUG("config_defaults: cfg=%p", (void *)cfg);
    memset(cfg, 0, sizeof(*cfg));
    cfg->nworkers  = 1;
    cfg->max_conns = MAX_CONNS;
    strncpy(cfg->ncland_json_path,
            "/usr/cnc/lib/data/ncland/ncland.json",
            sizeof(cfg->ncland_json_path) - 1);
    cfg->log_level    = 0;
    cfg->skip_sim     = 0;
    cfg->group_index  = -1;
    cfg->neid_lo      = 0;
    cfg->neid_hi      = 0;
    strncpy(cfg->mq_name, "/ncland_ctl", sizeof(cfg->mq_name) - 1);
}
```

Replace `ncland_parse_args` to use `getopt_long`:

```cpp
int ncland_parse_args(int argc, char *argv[], ncland_config_t *cfg)
{
    LOG_DEBUG("ncland_parse_args: argc=%d argv=%p cfg=%p",
              argc, (void *)argv, (void *)cfg);
    config_defaults(cfg);

    optind = 1;

    static struct option long_opts[] = {
        {"group-index", required_argument, nullptr, 'G'},
        {nullptr, 0, nullptr, 0}
    };

    int opt;
    while ((opt = getopt_long(argc, argv, "w:n:f:l:X", long_opts, nullptr)) != -1) {
        switch (opt) {
        case 'w': {
            int v = atoi(optarg);
            if (v < 1 || v > MAX_WORKERS) return -1;
            cfg->nworkers = v;
            break;
        }
        case 'n': {
            int v = atoi(optarg);
            if (v < 1 || v > MAX_CONNS) return -1;
            cfg->max_conns = v;
            break;
        }
        case 'f':
            strncpy(cfg->ncland_json_path, optarg,
                    sizeof(cfg->ncland_json_path) - 1);
            break;
        case 'l':
            cfg->log_level = atoi(optarg);
            break;
        case 'X':
            cfg->skip_sim = 1;
            break;
        case 'G': {
            int v = atoi(optarg);
            if (v < 0) return -1;
            cfg->group_index = v;
            snprintf(cfg->mq_name, sizeof(cfg->mq_name),
                     "/ncland_ctl_g%d", v);
            break;
        }
        default:
            return -1;
        }
    }
    return 0;
}
```

- [ ] **Step 5: Run tests**

Run: `./ncland_unit_tests -s argv -v`
Expected: T19.1–T19.3 PASS.

- [ ] **Step 6: Commit**

```bash
git add cnc/ncland/src/ncland.h cnc/ncland/src/ncland.cpp cnc/ncland/src/ncland_unit_tests.cpp
git commit -m "ncland: add --group-index long-opt and mq_name derivation"
```

---

## Task 7: warehouse_init reads groups[] via group_index

**Files:**
- Modify: `cnc/ncland/src/ncland_warehouse.cpp`
- Modify: `cnc/ncland/src/ncland_unit_tests.cpp`
- Modify: `cnc/ncland/src/test/fixtures/ncland_good.json` (retire) — see step 1.

**Purpose:** Replace the old `ncland_seed_load_filter()` call with `ncland_config_load_groups()` + group-selection by `cfg->group_index`. Child mode is fully wired after this task.

- [ ] **Step 1: Migrate the T9.1 fixture to the new schema**

Overwrite `cnc/ncland/src/test/fixtures/ncland_good.json` with:

```json
{
  "version": 1,
  "groups": [
    { "group": "TestGroup", "neid_range": [1, 100000], "dtypes": [1, 7, 12, 23] }
  ]
}
```

This preserves the "4 dtypes" invariant that T9.1 asserts and puts every neid the seed tests use inside range.

- [ ] **Step 2: Add a failing test for group selection**

In the `warehouse` suite of `ncland_unit_tests.cpp`, add:

```cpp
TEST("warehouse", "T9.8 warehouse_init selects group_index and populates neid_lo/hi") {
    ncland_config_t cfg; memset(&cfg, 0, sizeof(cfg));
    cfg.nworkers    = 1;
    cfg.group_index = 1;                      /* second group */
    strncpy(cfg.mq_name, "/ncland_ctl_g1", sizeof(cfg.mq_name) - 1);
    strncpy(cfg.ncland_json_path,
            "test/fixtures/ncland_groups_good.json",
            sizeof(cfg.ncland_json_path) - 1);

    ncland_wh_t wh{};
    REQUIRE_EQ(warehouse_init(&wh, &cfg), 0);
    REQUIRE_EQ(wh.neid_lo, 501);
    REQUIRE_EQ(wh.neid_hi, 1000);
    REQUIRE_EQ((int)wh.dtype_allow.size(), 2);
    REQUIRE(wh.dtype_allow.count(223) == 1);
    REQUIRE(wh.dtype_allow.count(254) == 1);

    warehouse_shutdown(&wh);
}

TEST("warehouse", "T9.9 warehouse_init rejects out-of-range group_index") {
    ncland_config_t cfg; memset(&cfg, 0, sizeof(cfg));
    cfg.nworkers    = 1;
    cfg.group_index = 42;                     /* no such group */
    strncpy(cfg.ncland_json_path,
            "test/fixtures/ncland_groups_good.json",
            sizeof(cfg.ncland_json_path) - 1);

    ncland_wh_t wh{};
    REQUIRE_EQ(warehouse_init(&wh, &cfg), -1);
}
```

- [ ] **Step 3: Run to verify failure**

Run: `nmake ncland_unit_tests && ./ncland_unit_tests -s warehouse -t "T9.8*" -v`
Expected: FAIL (neid_lo/hi unset, dtype_allow contents wrong).

- [ ] **Step 4: Rewire `warehouse_init`**

In `ncland_warehouse.cpp` replace the `ncland_seed_load_filter` block (around lines 266–277) with:

```cpp
    wh->neid_lo    = 0;
    wh->neid_hi    = 0;

    std::vector<ncland_group_t> groups;
    if (ncland_config_load_groups(cfg->ncland_json_path, &groups) != 0) {
        LOG_ERROR("warehouse_init: load_groups failed (%s)", cfg->ncland_json_path);
        return -1;
    }
    if (cfg->group_index < 0 || (size_t)cfg->group_index >= groups.size()) {
        LOG_ERROR("warehouse_init: group_index=%d out of range (0..%zu)",
                  cfg->group_index, groups.size());
        return -1;
    }
    const ncland_group_t &g = groups[cfg->group_index];
    wh->dtype_allow = g.dtypes;
    wh->neid_lo     = g.neid_lo;
    wh->neid_hi     = g.neid_hi;
    LOG_INFO("warehouse_init: group_index=%d desc=\"%s\" neid=[%d,%d] dtypes=%zu",
             cfg->group_index, g.group_desc.c_str(),
             g.neid_lo, g.neid_hi, g.dtypes.size());
```

Change the `ncland_mq_init` call to use `cfg->mq_name`:

```cpp
    if (ncland_mq_init(wh, cfg->mq_name) != 0) {
        LOG_ERROR("warehouse_init: ncland_mq_init failed on %s — daemon will accept no commands",
                  cfg->mq_name);
        /* Continue: daemon stays up so operators can see the log. */
    }
```

- [ ] **Step 5: Update T9.1 to pass a `group_index` and `mq_name`**

Edit the existing `T9.1` test to set `cfg.group_index = 0` and `strncpy(cfg.mq_name, "/ncland_ctl_t91", …)` (unique per test to avoid MQ name collisions across tests). Keep the `REQUIRE_EQ(wh.dtype_allow.size(), 4u)` assertion (fixture now returns 4 dtypes via one group).

- [ ] **Step 6: Run full test suite**

Run: `./ncland_unit_tests -v`
Expected: all suites (build, structs, mq, proto, ssh, telnet, conn, worker, warehouse, signals, timeout, seed, notify, argv, groups, stepper) PASS. Any failure is a regression — fix before commit.

- [ ] **Step 7: Commit**

```bash
git add cnc/ncland/src/ncland_warehouse.cpp cnc/ncland/src/ncland_unit_tests.cpp \
        cnc/ncland/src/test/fixtures/ncland_good.json
git commit -m "ncland: warehouse_init reads groups[] and selects by group_index"
```

---

## Task 8: Router types + `router_init` (skeleton)

**Files:**
- Create: `cnc/ncland/src/ncland_router.h`
- Create: `cnc/ncland/src/ncland_router.cpp`
- Modify: `cnc/ncland/src/Makefile`
- Modify: `cnc/ncland/src/ncland_unit_tests.cpp`

- [ ] **Step 1: Write `ncland_router.h`**

```cpp
/**
 * @file ncland_router.h
 * @brief Router process: reads ncland.json groups[], forks one warehouse
 * child per group, routes inbound dacs_msg on /ncland_ctl to the child that
 * owns the target neid, and supervises children with exponential-backoff
 * restart.
 */

#ifndef NCLAND_ROUTER_H
#define NCLAND_ROUTER_H

#include "ncland.h"

#include <mqueue.h>
#include <sys/types.h>
#include <vector>

/** @brief Per-child supervision + routing state. */
typedef struct {
    int              idx;                    /**< 0..N-1. */
    pid_t            pid;                    /**< Current child pid; -1 if not running. */
    mqd_t            child_mq;               /**< Write-only handle to /ncland_ctl_g<idx>. */
    char             child_mq_name[64];      /**< For mq_unlink at shutdown. */
    int              neid_lo, neid_hi;       /**< Group's neid range, inclusive. */
    int              restart_attempts;       /**< Exponential backoff counter. */
    time_t           started_at;             /**< When current pid was spawned. */
    time_t           next_restart_after;     /**< 0 = restart immediately. */
    ncland_group_t   cfg;                    /**< Preserved for restart across group config lifetimes. */
} router_child_t;

/** @brief Router process state. */
typedef struct {
    mqd_t                       ctl_mqd;         /**< /ncland_ctl inbound handle. */
    std::vector<router_child_t> children;        /**< One per group, sorted by neid_lo. */
    int                         epfd;            /**< epoll instance fd. */
    int                         sigfd;           /**< signalfd for SIGTERM/SIGINT/SIGHUP/SIGCHLD. */
    int                         timerfd;         /**< restart-backoff timerfd. */
    time_t                      shutting_down;   /**< 0 = running; nonzero = SIGTERM timestamp. */
    time_t                      shutdown_deadline; /**< shutting_down + SHUTDOWN_GRACE_S. */
    char                        argv0[256];      /**< Path to this executable, for child fork+exec. */
} ncland_router_t;

/** @brief Initialise router state from groups[]: open /ncland_ctl, allocate
 *  children[], sort by neid_lo. Does NOT spawn children (spawn separately so
 *  unit tests can drive routing without live processes).
 *  @return 0 on success, -1 on error (logged). */
int  router_init(ncland_router_t *r,
                 const std::vector<ncland_group_t> &groups,
                 const char *argv0);

/** @brief Fork+exec one child with `--group-index N` argv.
 *  @return 0 on success, -1 on failure (logged). Sets c->pid, c->started_at,
 *  bumps restart_attempts. */
int  router_spawn_child(ncland_router_t *r, router_child_t *c);

/** @brief Look up the child that owns @p neid.
 *  @return Pointer into r->children (do not free), or NULL if no group covers @p neid. */
router_child_t *router_find_child(ncland_router_t *r, int neid);

/** @brief Forward one dacs_msg to the child owning m->dm_dacsid.
 *  Drops (with rate-limited warn) if no group covers the neid or mq_send
 *  returns EAGAIN. */
void router_route_msg(ncland_router_t *r, const struct dacs_msg *m);

/** @brief Run the router epoll loop until SIGTERM/SIGHUP drain completes.
 *  Spawns all children before entering the loop, then forks nclan-seed once
 *  after a startup grace. */
int  router_run(ncland_router_t *r);

/** @brief mq_close+unlink /ncland_ctl, SIGKILL any stragglers past deadline,
 *  close epfd/sigfd/timerfd. Idempotent. */
void router_shutdown(ncland_router_t *r);

/** @brief Compute backoff seconds for an attempt count.
 *  @return min(30, 1 << min(attempts, 5)); pure function, unit-testable. */
int  router_backoff_seconds(int attempts);

#endif /* NCLAND_ROUTER_H */
```

- [ ] **Step 2: Write the failing tests**

Add a new suite in `ncland_unit_tests.cpp`:

```cpp
#include "ncland_router.h"

/* ------------------------------------------------------------------ */
/* T20: router                                                         */
/* ------------------------------------------------------------------ */

TEST("router", "T20.1 backoff sequence 1,2,4,8,16,30,30") {
    REQUIRE_EQ(router_backoff_seconds(0), 1);
    REQUIRE_EQ(router_backoff_seconds(1), 2);
    REQUIRE_EQ(router_backoff_seconds(2), 4);
    REQUIRE_EQ(router_backoff_seconds(3), 8);
    REQUIRE_EQ(router_backoff_seconds(4), 16);
    REQUIRE_EQ(router_backoff_seconds(5), 30);
    REQUIRE_EQ(router_backoff_seconds(6), 30);
    REQUIRE_EQ(router_backoff_seconds(100), 30);
}
```

- [ ] **Step 3: Create `ncland_router.cpp` skeleton with `router_backoff_seconds` only**

```cpp
// ncland_router.cpp — router process: reads groups[], spawns children,
// forwards inbound dacs_msg by neid, supervises with exponential backoff.

#include "ncland_router.h"
#include "nflog.hpp"

#include <algorithm>
#include <errno.h>
#include <fcntl.h>
#include <string.h>
#include <unistd.h>

int router_backoff_seconds(int attempts)
{
    if (attempts < 0) attempts = 0;
    int cap_attempts = attempts > 5 ? 5 : attempts;
    int v = 1 << cap_attempts;
    return v > 30 ? 30 : v;
}

/* router_init, router_spawn_child, router_find_child, router_route_msg,
 * router_run, router_shutdown — implemented in subsequent tasks. */
```

- [ ] **Step 4: Add `ncland_router.o` to the Makefile**

In `cnc/ncland/src/Makefile`, edit `NCLAND_OBJS`:

```make
NCLAND_OBJS = ncland_conn.o ncland_proto.o ncland_ssh.o \
	ncland_warehouse.o ncland_worker.o ncland_stepper.o ncland_telnet.o \
	ncland_mq.o ncland_seed.o ncland_notify.o ncland_notify_parse.o \
	ncland_registry.o ncland_connpool.o ncland_lua.o \
	ncland_router.o
```

- [ ] **Step 5: Run tests**

Run: `nmake ncland_unit_tests && ./ncland_unit_tests -s router -v`
Expected: T20.1 PASS.

- [ ] **Step 6: Commit**

```bash
git add cnc/ncland/src/ncland_router.h cnc/ncland/src/ncland_router.cpp \
        cnc/ncland/src/Makefile cnc/ncland/src/ncland_unit_tests.cpp
git commit -m "ncland: add router skeleton and backoff helper"
```

---

## Task 9: `router_find_child` — routing lookup

**Files:**
- Modify: `cnc/ncland/src/ncland_router.cpp`
- Modify: `cnc/ncland/src/ncland_unit_tests.cpp`

- [ ] **Step 1: Write the failing test**

Append to the `router` suite in `ncland_unit_tests.cpp`:

```cpp
static router_child_t make_child(int idx, int lo, int hi) {
    router_child_t c; memset(&c, 0, sizeof(c));
    c.idx = idx; c.neid_lo = lo; c.neid_hi = hi;
    c.pid = -1; c.child_mq = (mqd_t)-1;
    return c;
}

TEST("router", "T20.2 find_child hits correct group by neid") {
    ncland_router_t r; memset(&r, 0, sizeof(r));
    /* placement-new the children vector since memset trampled it */
    new (&r.children) std::vector<router_child_t>();
    r.children.push_back(make_child(0,   1,  500));
    r.children.push_back(make_child(1, 501, 1000));

    REQUIRE_EQ(router_find_child(&r,   1)->idx, 0);
    REQUIRE_EQ(router_find_child(&r, 250)->idx, 0);
    REQUIRE_EQ(router_find_child(&r, 500)->idx, 0);
    REQUIRE_EQ(router_find_child(&r, 501)->idx, 1);
    REQUIRE_EQ(router_find_child(&r, 999)->idx, 1);
    REQUIRE_EQ(router_find_child(&r,1000)->idx, 1);
    REQUIRE(router_find_child(&r, 1001) == NULL);
    REQUIRE(router_find_child(&r,    0) == NULL);

    r.children.~vector();
}
```

- [ ] **Step 2: Run to verify failure**

Run: `nmake ncland_unit_tests && ./ncland_unit_tests -s router -t "T20.2*" -v`
Expected: link/undef error `undefined reference to router_find_child`.

- [ ] **Step 3: Implement `router_find_child`**

Append to `ncland_router.cpp`:

```cpp
router_child_t *router_find_child(ncland_router_t *r, int neid)
{
    if (!r) return nullptr;
    /* children[] is sorted ascending by neid_lo (router_init sorts once). */
    auto it = std::upper_bound(
        r->children.begin(), r->children.end(), neid,
        [](int v, const router_child_t &c) { return v < c.neid_lo; });
    if (it == r->children.begin()) return nullptr;
    --it;
    if (neid <= it->neid_hi) return &(*it);
    return nullptr;
}
```

- [ ] **Step 4: Run tests**

Run: `./ncland_unit_tests -s router -v`
Expected: T20.1, T20.2 PASS.

- [ ] **Step 5: Commit**

```bash
git add cnc/ncland/src/ncland_router.cpp cnc/ncland/src/ncland_unit_tests.cpp
git commit -m "ncland: router_find_child with binary-search lookup"
```

---

## Task 10: `router_init` — open /ncland_ctl + build children[]

**Files:**
- Modify: `cnc/ncland/src/ncland_router.cpp`
- Modify: `cnc/ncland/src/ncland_unit_tests.cpp`

- [ ] **Step 1: Write the failing test**

Append to `router` suite:

```cpp
TEST("router", "T20.3 router_init populates children sorted by neid_lo") {
    std::vector<ncland_group_t> gs;
    ncland_group_t g0; g0.group_desc="Two"; g0.neid_lo=501; g0.neid_hi=1000;
    g0.dtypes.insert(223); g0.dtypes.insert(254);
    ncland_group_t g1; g1.group_desc="One"; g1.neid_lo=1;   g1.neid_hi=500;
    g1.dtypes.insert(206); g1.dtypes.insert(207);
    gs.push_back(g0); gs.push_back(g1);

    ncland_router_t r;
    memset(&r, 0, sizeof(r));
    new (&r.children) std::vector<router_child_t>();

    REQUIRE_EQ(router_init(&r, gs, "/tmp/ncland-fake"), 0);
    REQUIRE_EQ((int)r.children.size(), 2);
    /* Sorted ascending by neid_lo. */
    REQUIRE_EQ(r.children[0].neid_lo, 1);
    REQUIRE_EQ(r.children[1].neid_lo, 501);
    REQUIRE_EQ(strcmp(r.children[0].child_mq_name, "/ncland_ctl_g0"), 0);
    REQUIRE_EQ(strcmp(r.children[1].child_mq_name, "/ncland_ctl_g1"), 0);

    router_shutdown(&r);
}
```

- [ ] **Step 2: Run to verify failure**

Run: `nmake ncland_unit_tests && ./ncland_unit_tests -s router -t "T20.3*" -v`
Expected: undefined-symbol link error.

- [ ] **Step 3: Implement `router_init` and a `router_shutdown` skeleton**

Append to `ncland_router.cpp`:

```cpp
int router_init(ncland_router_t *r,
                const std::vector<ncland_group_t> &groups,
                const char *argv0)
{
    if (!r || groups.empty() || !argv0) return -1;

    r->ctl_mqd           = (mqd_t)-1;
    r->epfd              = -1;
    r->sigfd             = -1;
    r->timerfd           = -1;
    r->shutting_down     = 0;
    r->shutdown_deadline = 0;
    strncpy(r->argv0, argv0, sizeof(r->argv0) - 1);
    r->argv0[sizeof(r->argv0) - 1] = '\0';

    /* Build children sorted by neid_lo. The idx we assign here is the
     * position in the sorted vector, which is also the value the child
     * receives as --group-index and the number in /ncland_ctl_g<idx>. */
    std::vector<ncland_group_t> sorted = groups;
    std::sort(sorted.begin(), sorted.end(),
              [](const ncland_group_t &a, const ncland_group_t &b) {
                  return a.neid_lo < b.neid_lo;
              });
    r->children.clear();
    for (size_t i = 0; i < sorted.size(); i++) {
        router_child_t c; memset(&c, 0, sizeof(c));
        new (&c.cfg) ncland_group_t(sorted[i]);   /* copy-construct */
        c.idx      = (int)i;
        c.pid      = -1;
        c.child_mq = (mqd_t)-1;
        c.neid_lo  = sorted[i].neid_lo;
        c.neid_hi  = sorted[i].neid_hi;
        c.restart_attempts   = 0;
        c.started_at         = 0;
        c.next_restart_after = 0;
        snprintf(c.child_mq_name, sizeof(c.child_mq_name),
                 "/ncland_ctl_g%d", (int)i);
        r->children.push_back(std::move(c));
    }
    LOG_INFO("router: initialised %zu group(s)", r->children.size());
    return 0;
}

void router_shutdown(ncland_router_t *r)
{
    if (!r) return;
    if (r->ctl_mqd != (mqd_t)-1) { mq_close(r->ctl_mqd); r->ctl_mqd = (mqd_t)-1; }
    for (auto &c : r->children) {
        if (c.child_mq != (mqd_t)-1) { mq_close(c.child_mq); c.child_mq = (mqd_t)-1; }
        if (c.child_mq_name[0]) mq_unlink(c.child_mq_name);
    }
    if (r->timerfd >= 0) { close(r->timerfd); r->timerfd = -1; }
    if (r->sigfd   >= 0) { close(r->sigfd);   r->sigfd   = -1; }
    if (r->epfd    >= 0) { close(r->epfd);    r->epfd    = -1; }
    r->children.clear();
}
```

Add `#include <mqueue.h>` if not already at the top.

- [ ] **Step 4: Run tests**

Run: `./ncland_unit_tests -s router -v`
Expected: T20.1–T20.3 PASS.

- [ ] **Step 5: Commit**

```bash
git add cnc/ncland/src/ncland_router.cpp cnc/ncland/src/ncland_unit_tests.cpp
git commit -m "ncland: router_init sorts groups and builds children[]"
```

---

## Task 11: `router_route_msg` — forward to child MQ

**Files:**
- Modify: `cnc/ncland/src/ncland_router.cpp`
- Modify: `cnc/ncland/src/ncland_unit_tests.cpp`

- [ ] **Step 1: Write the failing test**

Append to `router` suite. This test opens two real per-child MQs (as consumer), routes a message, and reads it back from the correct MQ.

```cpp
TEST("router", "T20.4 route_msg delivers to the child that owns the neid") {
    /* Unique mq names to avoid collisions with any leaked queue. */
    const char *mq0 = "/ncland_test_g0";
    const char *mq1 = "/ncland_test_g1";
    mq_unlink(mq0); mq_unlink(mq1);

    struct mq_attr a; memset(&a, 0, sizeof(a));
    a.mq_maxmsg  = 4;
    a.mq_msgsize = sizeof(struct dacs_msg);
    mqd_t r0 = mq_open(mq0, O_CREAT | O_RDONLY | O_NONBLOCK, 0600, &a);
    mqd_t r1 = mq_open(mq1, O_CREAT | O_RDONLY | O_NONBLOCK, 0600, &a);
    REQUIRE(r0 != (mqd_t)-1);
    REQUIRE(r1 != (mqd_t)-1);

    ncland_router_t r; memset(&r, 0, sizeof(r));
    new (&r.children) std::vector<router_child_t>();

    router_child_t c0 = make_child(0,   1,  500);
    strncpy(c0.child_mq_name, mq0, sizeof(c0.child_mq_name) - 1);
    c0.child_mq = mq_open(mq0, O_WRONLY | O_NONBLOCK);
    REQUIRE(c0.child_mq != (mqd_t)-1);

    router_child_t c1 = make_child(1, 501, 1000);
    strncpy(c1.child_mq_name, mq1, sizeof(c1.child_mq_name) - 1);
    c1.child_mq = mq_open(mq1, O_WRONLY | O_NONBLOCK);
    REQUIRE(c1.child_mq != (mqd_t)-1);

    r.children.push_back(c0);
    r.children.push_back(c1);

    /* neid 250 -> g0 */
    struct dacs_msg m; memset(&m, 0, sizeof(m));
    m.dm_dacsid = 250;
    snprintf(m.u.dm_text, sizeof(m.u.dm_text), "CTAG:30:0:0:show ver");
    router_route_msg(&r, &m);

    struct dacs_msg got; unsigned int prio = 0;
    ssize_t n = mq_receive(r0, (char*)&got, sizeof(got), &prio);
    REQUIRE_EQ((int)n, (int)sizeof(struct dacs_msg));
    REQUIRE_EQ((int)got.dm_dacsid, 250);

    n = mq_receive(r1, (char*)&got, sizeof(got), &prio);
    REQUIRE_EQ((int)n, -1);
    REQUIRE_EQ(errno, EAGAIN);

    /* neid 5000 -> no group: dropped, both queues stay empty. */
    m.dm_dacsid = 5000;
    router_route_msg(&r, &m);
    n = mq_receive(r0, (char*)&got, sizeof(got), &prio);
    REQUIRE_EQ((int)n, -1);
    n = mq_receive(r1, (char*)&got, sizeof(got), &prio);
    REQUIRE_EQ((int)n, -1);

    /* cleanup */
    mq_close(r.children[0].child_mq); mq_close(r.children[1].child_mq);
    mq_close(r0); mq_close(r1);
    mq_unlink(mq0); mq_unlink(mq1);
    r.children.~vector();
}
```

- [ ] **Step 2: Run to verify failure**

Run: `nmake ncland_unit_tests && ./ncland_unit_tests -s router -t "T20.4*" -v`
Expected: `undefined reference to router_route_msg`.

- [ ] **Step 3: Implement `router_route_msg`**

Append to `ncland_router.cpp`:

```cpp
void router_route_msg(ncland_router_t *r, const struct dacs_msg *m)
{
    if (!r || !m) return;

    router_child_t *c = router_find_child(r, (int)m->dm_dacsid);
    if (!c) {
        /* Rate-limit by simple 1s cadence. Static local keeps state cheaply. */
        static time_t last_warn = 0;
        time_t now = time(NULL);
        if (now != last_warn) {
            LOG_WARN("route: neid=%u in no group; dropped", m->dm_dacsid);
            last_warn = now;
        }
        return;
    }
    if (c->child_mq == (mqd_t)-1) {
        LOG_WARN("route: child g%d has no mq open; dropped neid=%u",
                 c->idx, m->dm_dacsid);
        return;
    }
    if (mq_send(c->child_mq, (const char *)m, sizeof(*m), 0) < 0) {
        if (errno == EAGAIN) {
            static time_t last_full_warn = 0;
            time_t now = time(NULL);
            if (now != last_full_warn) {
                LOG_WARN("route: child g%d mq full; dropped neid=%u",
                         c->idx, m->dm_dacsid);
                last_full_warn = now;
            }
        } else {
            LOG_ERROR("route: mq_send to g%d failed: %s",
                      c->idx, strerror(errno));
        }
    }
}
```

- [ ] **Step 4: Run tests**

Run: `./ncland_unit_tests -s router -v`
Expected: T20.1–T20.4 PASS.

- [ ] **Step 5: Commit**

```bash
git add cnc/ncland/src/ncland_router.cpp cnc/ncland/src/ncland_unit_tests.cpp
git commit -m "ncland: router_route_msg forwards to per-group child MQ"
```

---

## Task 12: `router_spawn_child` — fork+exec

**Files:**
- Modify: `cnc/ncland/src/ncland_router.cpp`

**Rationale:** Not TDD-first because a real fork+exec is hard to isolate in a unit test — the integration test at the end of the plan exercises this. Small, careful function.

- [ ] **Step 1: Implement `router_spawn_child`**

Append to `ncland_router.cpp`:

```cpp
int router_spawn_child(ncland_router_t *r, router_child_t *c)
{
    if (!r || !c) return -1;

    /* Create (or ensure exists) the child's MQ before fork so the writer
     * handle is always openable by the parent below. Same attrs as the
     * warehouse's ncland_mq_init default. */
    struct mq_attr a; memset(&a, 0, sizeof(a));
    a.mq_maxmsg  = 32;
    a.mq_msgsize = sizeof(struct dacs_msg);
    mqd_t tmp = mq_open(c->child_mq_name,
                        O_CREAT | O_RDWR | O_NONBLOCK, 0600, &a);
    if (tmp == (mqd_t)-1) {
        LOG_ERROR("router: mq_open(create) %s failed: %s",
                  c->child_mq_name, strerror(errno));
        return -1;
    }
    mq_close(tmp);

    pid_t pid = fork();
    if (pid < 0) {
        LOG_ERROR("router: fork g%d failed: %s", c->idx, strerror(errno));
        return -1;
    }
    if (pid == 0) {
        /* Child: exec ncland with --group-index N. Preserve argv0 path. */
        char idx_str[16];
        snprintf(idx_str, sizeof(idx_str), "%d", c->idx);
        execl(r->argv0, r->argv0, "--group-index", idx_str, (char *)NULL);
        /* If we reach here, exec failed. */
        _exit(127);
    }

    /* Parent: open write-only handle to the child's MQ. */
    if (c->child_mq != (mqd_t)-1) mq_close(c->child_mq);
    c->child_mq = mq_open(c->child_mq_name, O_WRONLY | O_NONBLOCK);
    if (c->child_mq == (mqd_t)-1)
        LOG_WARN("router: mq_open(WRONLY) %s failed: %s (routing to g%d will drop)",
                 c->child_mq_name, strerror(errno), c->idx);

    c->pid                = pid;
    c->started_at         = time(NULL);
    c->next_restart_after = 0;
    c->restart_attempts  += 1;
    LOG_INFO("router: spawned g%d pid=%d (attempt %d) mq=%s",
             c->idx, (int)pid, c->restart_attempts, c->child_mq_name);
    return 0;
}
```

- [ ] **Step 2: Verify it compiles**

Run: `nmake ncland_router.o`
Expected: clean compile.

- [ ] **Step 3: Commit**

```bash
git add cnc/ncland/src/ncland_router.cpp
git commit -m "ncland: router_spawn_child fork+exec with per-group MQ"
```

---

## Task 13: `router_run` — main epoll loop, supervision, seed fork

**Files:**
- Modify: `cnc/ncland/src/ncland_router.cpp`

- [ ] **Step 1: Implement `router_run`**

Append to `ncland_router.cpp`:

```cpp
#include <sys/epoll.h>
#include <sys/signalfd.h>
#include <sys/timerfd.h>
#include <sys/wait.h>
#include <signal.h>

/* Ensure /ncland_ctl exists and open O_RDONLY | O_NONBLOCK. */
static mqd_t router_open_ctl_mq(void)
{
    struct mq_attr a; memset(&a, 0, sizeof(a));
    a.mq_maxmsg  = 32;
    a.mq_msgsize = sizeof(struct dacs_msg);
    mqd_t q = mq_open("/ncland_ctl",
                      O_CREAT | O_RDONLY | O_NONBLOCK, 0600, &a);
    if (q == (mqd_t)-1)
        LOG_ERROR("router: mq_open /ncland_ctl failed: %s", strerror(errno));
    return q;
}

/* Arm the timerfd for the soonest pending restart. */
static void router_arm_timer(ncland_router_t *r)
{
    time_t soonest = 0;
    for (const auto &c : r->children) {
        if (c.pid < 0 && c.next_restart_after > 0 &&
            (soonest == 0 || c.next_restart_after < soonest))
            soonest = c.next_restart_after;
    }
    struct itimerspec it; memset(&it, 0, sizeof(it));
    if (soonest > 0) {
        time_t now = time(NULL);
        it.it_value.tv_sec = (soonest > now) ? (soonest - now) : 1;
    }
    timerfd_settime(r->timerfd, 0, &it, nullptr);
}

int router_run(ncland_router_t *r)
{
    if (!r || r->children.empty()) return -1;

    signal(SIGPIPE, SIG_IGN);

    r->ctl_mqd = router_open_ctl_mq();
    if (r->ctl_mqd == (mqd_t)-1) return -1;

    r->epfd    = epoll_create1(0);
    r->timerfd = timerfd_create(CLOCK_MONOTONIC, 0);

    sigset_t mask; sigemptyset(&mask);
    sigaddset(&mask, SIGCHLD);
    sigaddset(&mask, SIGTERM);
    sigaddset(&mask, SIGINT);
    sigaddset(&mask, SIGHUP);
    sigaddset(&mask, SIGPIPE);
    sigprocmask(SIG_BLOCK, &mask, NULL);
    r->sigfd = signalfd(-1, &mask, SFD_NONBLOCK | SFD_CLOEXEC);

    auto add = [&](int fd) {
        struct epoll_event ev{};
        ev.events = EPOLLIN;
        ev.data.fd = fd;
        epoll_ctl(r->epfd, EPOLL_CTL_ADD, fd, &ev);
    };
    add((int)r->ctl_mqd);
    add(r->sigfd);
    add(r->timerfd);

    /* Spawn all children. */
    for (auto &c : r->children) router_spawn_child(r, &c);

    /* Startup grace, then fork nclan-seed once. Use a nanosleep — the loop
     * hasn't started epolling yet, so blocking briefly here is fine. */
    struct timespec ts; ts.tv_sec = 0; ts.tv_nsec = 500 * 1000 * 1000;
    nanosleep(&ts, nullptr);
    pid_t seeder = fork();
    if (seeder == 0) {
        execlp("nclan-seed", "nclan-seed", (char *)NULL);
        _exit(127);
    } else if (seeder < 0) {
        LOG_WARN("router: fork nclan-seed failed: %s", strerror(errno));
    } else {
        LOG_INFO("router: spawned nclan-seed pid=%d", (int)seeder);
    }

    /* Main loop. */
    struct epoll_event events[8];
    while (true) {
        /* Exit condition: shutting_down AND all children reaped (or deadline). */
        if (r->shutting_down) {
            bool any_alive = false;
            for (const auto &c : r->children) if (c.pid > 0) { any_alive = true; break; }
            if (!any_alive) break;
            if (time(NULL) >= r->shutdown_deadline) {
                for (auto &c : r->children)
                    if (c.pid > 0) kill(c.pid, SIGKILL);
                /* one more waitpid pass below */
            }
        }

        int n = epoll_wait(r->epfd, events, 8, 1000);
        if (n < 0) { if (errno == EINTR) continue; break; }

        for (int i = 0; i < n; i++) {
            int fd = events[i].data.fd;

            if (fd == r->sigfd) {
                struct signalfd_siginfo si;
                while (read(r->sigfd, &si, sizeof(si)) == (ssize_t)sizeof(si)) {
                    if ((int)si.ssi_signo == SIGTERM ||
                        (int)si.ssi_signo == SIGINT  ||
                        (int)si.ssi_signo == SIGHUP) {
                        if (r->shutting_down) continue;
                        r->shutting_down     = time(NULL);
                        r->shutdown_deadline = r->shutting_down + SHUTDOWN_GRACE_S;
                        LOG_INFO("router: signal %u — draining", si.ssi_signo);
                        for (auto &c : r->children)
                            if (c.pid > 0) kill(c.pid, SIGTERM);
                        /* Stop accepting new messages. */
                        mq_close(r->ctl_mqd);
                        mq_unlink("/ncland_ctl");
                        r->ctl_mqd = (mqd_t)-1;
                    } else if ((int)si.ssi_signo == SIGCHLD) {
                        int status; pid_t pid;
                        while ((pid = waitpid(-1, &status, WNOHANG)) > 0) {
                            router_child_t *hit = nullptr;
                            for (auto &c : r->children)
                                if (c.pid == pid) { hit = &c; break; }
                            if (!hit) continue;   /* nclan-seed or stranger */
                            LOG_ERROR("router: child g%d pid=%d exited status=0x%x",
                                      hit->idx, pid, status);
                            hit->pid = -1;
                            if (r->shutting_down) continue;
                            /* Reset attempts if the child had been alive >= 60s. */
                            time_t now = time(NULL);
                            if (hit->started_at > 0 && now - hit->started_at >= 60)
                                hit->restart_attempts = 0;
                            int back = router_backoff_seconds(hit->restart_attempts);
                            hit->next_restart_after = now + back;
                            LOG_INFO("router: g%d restart in %ds (attempt %d)",
                                     hit->idx, back, hit->restart_attempts + 1);
                        }
                        router_arm_timer(r);
                    }
                }
            } else if (fd == r->timerfd) {
                uint64_t exp; ssize_t rd = read(r->timerfd, &exp, sizeof(exp));
                (void)rd;
                if (r->shutting_down) continue;
                time_t now = time(NULL);
                for (auto &c : r->children) {
                    if (c.pid < 0 && c.next_restart_after > 0 && c.next_restart_after <= now)
                        router_spawn_child(r, &c);
                }
                router_arm_timer(r);
            } else if ((mqd_t)fd == r->ctl_mqd) {
                struct dacs_msg m; unsigned int prio = 0;
                while (true) {
                    ssize_t rr = mq_receive(r->ctl_mqd, (char*)&m, sizeof(m), &prio);
                    if (rr <= 0) break;
                    router_route_msg(r, &m);
                }
            }
        }
    }

    /* Final reap sweep. */
    int status;
    while (waitpid(-1, &status, WNOHANG) > 0) {}
    return 0;
}
```

- [ ] **Step 2: Verify compile**

Run: `nmake ncland_router.o`
Expected: clean compile.

- [ ] **Step 3: Commit**

```bash
git add cnc/ncland/src/ncland_router.cpp
git commit -m "ncland: router_run epoll loop with supervision and seed fork"
```

---

## Task 14: `main()` dispatch — parent vs child

**Files:**
- Modify: `cnc/ncland/src/ncland.cpp`

- [ ] **Step 1: Rewrite `main()`**

Replace the existing `main` body with:

```cpp
#include "ncland_router.h"

int main(int argc, char *argv[])
{
    LOG_DEBUG("main: argc=%d argv=%p", argc, (void *)argv);
    ncland_config_t cfg;
    if (ncland_parse_args(argc, argv, &cfg) < 0) {
        fprintf(stderr,
            "Usage: %s [-w nworkers] [-n max_conns] "
            "[-f ncland_json] [-l log_level] [-X] [--group-index N]\n",
            argv[0]);
        return 1;
    }

    if (cfg.group_index < 0) {
        /* Parent/router mode. */
        std::vector<ncland_group_t> groups;
        if (ncland_config_load_groups(cfg.ncland_json_path, &groups) != 0) {
            fprintf(stderr, "ncland: failed to load %s\n", cfg.ncland_json_path);
            return 1;
        }
        ncland_router_t r;
        memset(&r, 0, sizeof(r));
        new (&r.children) std::vector<router_child_t>();
        if (router_init(&r, groups, argv[0]) != 0) {
            fprintf(stderr, "ncland: router_init failed\n");
            return 1;
        }
        int rc = router_run(&r);
        router_shutdown(&r);
        r.children.~vector();
        return rc;
    }

    /* Child/warehouse mode. */
    ncland_wh_t wh{};
    if (warehouse_init(&wh, &cfg) != 0) {
        fprintf(stderr, "warehouse_init failed\n");
        return 1;
    }

    char *start;
    Frmlnk = (struct frame_link *)atch_frm(&start);

    return ncland_warehouse_run(&wh);
}
```

Delete the old `fork()` for `nclan-seed` — it now lives in `router_run`.

Add `#include <new>` if needed for placement-new (some toolchains require it — it is header-only).

- [ ] **Step 2: Verify compile of the ncland binary**

Run: `nmake ncland`
Expected: clean build of `$(PBIN)/ncland`.

- [ ] **Step 3: Commit**

```bash
git add cnc/ncland/src/ncland.cpp
git commit -m "ncland: main() dispatches router vs warehouse by --group-index"
```

---

## Task 15: Integration test — two-group launch + route

**Files:**
- Create: `cnc/ncland/src/test/integration/it_groups.sh`

- [ ] **Step 1: Write the integration script**

```bash
#!/bin/bash
# it_groups.sh -- verify parent forks one warehouse per group and routes by neid.
#
# Preconditions:
#   - ncland built (nmake in cnc/ncland/src).
#   - Run from cnc/ncland/src.

set -euo pipefail

TMP=$(mktemp -d)
trap 'rm -rf "$TMP"; for m in /ncland_ctl /ncland_ctl_g0 /ncland_ctl_g1; do
      [ -e /dev/mqueue$m ] && rm -f /dev/mqueue$m || true; done' EXIT

CFG="$TMP/ncland.json"
cat > "$CFG" <<EOF
{
  "version": 1,
  "groups": [
    { "group": "GroupOne", "neid_range": [1, 500],    "dtypes": [206, 207] },
    { "group": "GroupTwo", "neid_range": [501, 1000], "dtypes": [223, 254] }
  ]
}
EOF

# Clean any leftover MQs from a prior run.
for m in /ncland_ctl /ncland_ctl_g0 /ncland_ctl_g1; do
    [ -e /dev/mqueue$m ] && rm -f /dev/mqueue$m || true
done

$PBIN/ncland -f "$CFG" -X &
NCLAND_PID=$!
sleep 1

# Two children must exist.
CHILDREN=$(pgrep -P "$NCLAND_PID" ncland | wc -l)
if [ "$CHILDREN" -lt 2 ]; then
    echo "FAIL: expected >=2 children under pid $NCLAND_PID, got $CHILDREN"
    kill $NCLAND_PID 2>/dev/null || true
    exit 1
fi

# Per-group MQs must exist.
for m in /ncland_ctl /ncland_ctl_g0 /ncland_ctl_g1; do
    if [ ! -e /dev/mqueue$m ]; then
        echo "FAIL: missing $m"
        kill $NCLAND_PID 2>/dev/null || true
        exit 1
    fi
done

# Clean shutdown.
kill $NCLAND_PID
wait $NCLAND_PID 2>/dev/null || true
echo "PASS: two-group launch"
```

Make it executable.

- [ ] **Step 2: Verify the script runs**

Run: `cd cnc/ncland/src && chmod +x test/integration/it_groups.sh && ./test/integration/it_groups.sh`
Expected: `PASS: two-group launch`. (Requires `$PBIN` set and `ncland` binary installed — same requirements as the existing `it_smoke.sh`.)

- [ ] **Step 3: Commit**

```bash
git add cnc/ncland/src/test/integration/it_groups.sh
git commit -m "ncland: integration test for two-group launch"
```

---

## Task 16: Full-suite regression run

**Files:** none (verification only)

- [ ] **Step 1: Run the whole unit-test binary**

Run: `cd cnc/ncland/src && nmake ncland_unit_tests && ./ncland_unit_tests -v`
Expected: every suite passes — build, structs, mq, proto, ssh, telnet, conn, worker, warehouse, signals, timeout, seed, notify, argv, groups, router, stepper.

- [ ] **Step 2: Run integration smoke test**

Run: `./test/integration/it_smoke.sh && ./test/integration/it_groups.sh`
Expected: both PASS.

- [ ] **Step 3: If anything fails, fix, then re-run this task**

Never commit a suite that regresses an existing test. Any regression at this point is a bug introduced by tasks 1-15 — trace it to the offending task and fix in a follow-up commit (do not rewrite history).

- [ ] **Step 4: Final commit if any fix was needed**

```bash
git add <files>
git commit -m "ncland: fix regression uncovered by full-suite run"
```

---

## Self-Review

**Spec coverage:**

- §2 config schema → Tasks 1 (types), 2 (happy path), 3 (errors).
- §3 topology → Tasks 8 (skeleton), 10 (init), 12 (spawn), 14 (main dispatch).
- §4 routing → Tasks 9 (find_child), 11 (route_msg).
- §5.1 argv → Task 6.
- §5.2 warehouse_init → Task 7.
- §5.3 neid filter → Tasks 4 (seed), 5 (notify).
- §5.4 seeder → Task 13 (`router_run` forks `nclan-seed` after the 500 ms grace).
- §6.1 supervision (SIGCHLD, backoff, uptime reset) → Task 13 + Task 8 (backoff helper).
- §6.2 shutdown → Task 13 (SIGTERM propagation, deadline, SIGKILL).
- §6.3 failure modes → covered by parser errors (T18.x), routing drops (T20.4 no-group + EAGAIN path in T20.4 via non-blocking send), spawn failure logged in Task 12.
- §7 testing → Tasks 2–5, 8–11 unit tests; Task 15 integration test.
- §8 non-goals → no tasks (intentional).
- §9 files touched → all covered.

**Placeholder scan:** none — every step contains complete code or a specific command.

**Type consistency:** `ncland_group_t`, `router_child_t`, `ncland_router_t`, `router_backoff_seconds`, `router_find_child`, `router_route_msg`, `router_spawn_child`, `router_init`, `router_run`, `router_shutdown` — same names in header, in .cpp, and in tests. `mq_name` field consistent between `ncland_config_t` (Task 6) and its use in `warehouse_init` (Task 7). `neid_lo`/`neid_hi` names consistent in `ncland_group_t`, `router_child_t`, `ncland_wh_t`, and `ncland_config_t`.
