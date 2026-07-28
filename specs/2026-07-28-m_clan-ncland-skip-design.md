# m_clan: skip clan startup for ncland-routed NEs

Date: 2026-07-28
Author: dnuzzo@lightriver.com

## 1. Problem

`cnc/sdi/src/m_clan.c` starts a legacy `clan` (or `clan_libssh`) process for
every NE with `HAS_CLAN_CARD(dcs_type)` that has an assigned slot. As
supported dtypes migrate to `ncland`, both processes now attempt to manage
the same (neid, slot). m_clan must respect `/usr/cnc/lib/data/ncland/ncland.json`
and refuse to fork a legacy clan for any (neid, dtype) that ncland claims.

Additionally, `libutillib`'s `clan_ncland_route.c` still parses the obsolete
flat `{"version":1,"dtypes":[...]}` schema. Production `ncland.json` uses the
groups schema:

```json
{
  "version": 1,
  "groups": [
    { "group": "GroupOne", "neid_range": [1, 500],    "dtypes": [206, 207] },
    { "group": "GroupTwo", "neid_range": [501, 1000], "dtypes": [223, 254] }
  ]
}
```

`ncland_seed.cpp` (C++/nlohmann) is the current reference for
schema/validation rules. clan_ncland_route must catch up.

## 2. Goals

- m_clan honors `ncland.json` and never forks legacy clan for a routed
  (neid, dtype) pair.
- `libutillib` gains a groups-aware `clan_ncland_supports(neid, dtype)` API,
  replacing dtype-only `clan_ncland_supports_dtype`.
- `clan_util.c` uses the new API so TL1 routing and m_clan gating share one
  source of truth.
- Existing behavior for non-routed NEs unchanged.

## 3. Non-goals

- Hot reload of `ncland.json` (existing TODO in `load_cfg_once` still open).
- Retiring the legacy clan code path.
- Linking `nlohmann::json` into libutillib (C library; keep hand-rolled).
- Any change to ncland's own loader (`ncland_seed.cpp`).

## 4. Design

### 4.1 New API (libutillib)

`cnc/utility/src/clan_ncland_route.h`:

```c
/** @brief Return true iff ncland.json contains a group whose neid_range
 *  covers @p neid AND whose dtypes array contains @p dtype.
 *  Loads + caches config on first call. Missing/malformed config →
 *  TRACE WARN once + return false for every (neid, dtype) (legacy clan
 *  fallback). Thread-safe; idempotent. */
bool clan_ncland_supports(int neid, int dtype);
```

`clan_ncland_supports_dtype` is deleted from the header. Only in-tree caller
is `clan_util.c`, migrated in this change.

### 4.2 Parser rewrite (libutillib)

`cnc/utility/src/clan_ncland_route.c`:

- Static state changes:
  ```c
  struct clan_ncland_group {
      int   lo;         /* inclusive */
      int   hi;         /* inclusive */
      int  *dtypes;
      size_t n_dtypes;
  };
  static struct clan_ncland_group *g_groups   = NULL;
  static size_t                    g_n_groups = 0;
  static bool                      g_cfg_ok   = false;
  ```
- Replace `parse_ncland_json` with a groups-aware hand-rolled parser.
  Retain single-pass, no-recursion style. Reject:
  - file > 64 KB
  - missing/non-integer `version`, version != 1
  - missing `groups`, empty `groups[]`
  - group entry missing `group` (string), `neid_range` (2-int array),
    or `dtypes` (non-empty int array)
  - `lo < 0`, `hi < lo`, `dtype < 0 || dtype > 65535`
  - overlapping neid_range intervals between any two groups
  - legacy top-level `dtypes` key (align with ncland_seed rejection)
- Lookup: `clan_ncland_supports(neid, dtype)` walks `g_groups[]` linearly.
  Group counts are small (single digits), dtype arrays are small too;
  no need for hashing or interval trees.

### 4.3 clan_util migration

`cnc/utility/src/clan_util.c` line 348:

```c
- if ( clan_ncland_supports_dtype( neType ) )
+ if ( clan_ncland_supports( c->dacsid, neType ) )
```

`c->dacsid` is the neid; `neType = frm_dtype(c->dacsid)` was already
computed above. No new context needed.

### 4.4 m_clan skip

`cnc/sdi/src/m_clan.c`:

- Add `#include "clan_ncland_route.h"`.
- In `get_next_clan()`, immediately after the existing
  `HAS_CLAN_CARD || isMeProvDemo` gate and before the remote-interface
  check, insert:

  ```c
  if (clan_ncland_supports(loop, frmlnk->dcs_type[PRIMARY_INTERFACE])) {
      TRACE(D2, "NeId:%d dtype:%d routed to ncland; skip clan\n",
            loop, frmlnk->dcs_type[PRIMARY_INTERFACE]);
      continue;
  }
  ```

Skip at NE-loop granularity, not per-slot, so slot scanning short-circuits
entirely — no batch-break burn, no useless DB reads.

`start_clan_proc` is untouched; no defense-in-depth check there.
`get_next_clan` is the only path that reaches it.

### 4.5 Build

- `libutillib` already picks up `clan_ncland_route.c` via `util.mk` line 242.
- m_clan already links `$(UTILLIB)`. No `.mk` edits needed.

## 5. Tests

`cnc/utility/src/clan_ncland_route_tests.c` rewrite. New test fixtures are
`/tmp/...` temp files written inline (same pattern as existing tests).

Parser tests (all use `test_parse_ncland_json`):

- Happy: single group, two groups non-overlapping — succeeds; group count
  and dtype content match.
- Bad version, missing groups, empty groups[] — all `-1`.
- Group missing `group` / `neid_range` / `dtypes` — `-1` each.
- `neid_range` not a 2-int array, `lo > hi`, `lo < 0` — `-1` each.
- Non-int inside `dtypes[]`, negative dtype, out-of-range dtype — `-1` each.
- Overlapping `neid_range` between two groups — `-1`.
- Legacy top-level `dtypes` key present — `-1`.

Lookup tests (using `clan_ncland_route_set_groups_for_test`):

- (neid inside range, dtype in group) — `true`.
- (neid inside range, dtype not in group) — `false`.
- (neid outside all ranges, dtype anywhere) — `false`.
- Empty config after failed load — always `false`.

`test_send_round_trip` unchanged (parser-independent).

Test shim rename: `clan_ncland_route_set_dtypes_for_test` →
`clan_ncland_route_set_groups_for_test(const struct clan_ncland_group *,
size_t)`.

## 6. Risk / rollout

- If `ncland.json` is malformed, m_clan reverts to legacy behavior (starts
  clan for every NE). This is the safe direction: worst case is old
  behavior, not a silent NE outage.
- New allowlist entries take effect only after m_clan restart (pthread_once
  gate). Existing operational cadence — deploy JSON, bounce m_clan +
  ncland — is unchanged.
- No shared-memory or IPC format changes.

## 7. Files touched

- `cnc/utility/src/clan_ncland_route.c` — parser rewrite, new API.
- `cnc/utility/src/clan_ncland_route.h` — API rename, test-shim rename.
- `cnc/utility/src/clan_util.c` — one-line call-site update.
- `cnc/utility/src/clan_ncland_route_tests.c` — full rewrite.
- `cnc/sdi/src/m_clan.c` — include + skip block in `get_next_clan`.
