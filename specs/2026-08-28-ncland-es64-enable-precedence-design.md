# ncland: es64_snmp_info takes precedence over frame_link for NE enable state

Date: 2026-08-28
Component: `cnc/ncland` (nclan-seed seed walk + watch loop)
Status: Design approved, pending spec review

## Problem

Node 1801 (group PSS-G2, dtype 207) would not come up. The `ncland.PSS-G2`
trace shows every other NE in the `[1801,1830]` range seeded, connected, and
logged in — but **1801 never appears** past the group-range definition: no
`app.ne.seed`, no `app.ne.enable`, no connect attempt.

Root cause: 1801 is provisioned in both `frame_link` and `es64_snmp_info`, but
the two tables disagree:

- `frame_link.disable != 0` (administratively disabled)
- `es64_snmp_info.enabled == 1` (enabled)

Both the seed walk and the watch loop key the enable decision **solely on
frame_link**, so 1801 is skipped and never published to the nfdb bus. ncland
only connects NEs it is told about, so it never attempts 1801. This is not an
ncland bug — it is the publisher (nclan-seed) trusting frame_link as the single
source of truth.

### Where the two paths gate today

- **Seed walk** — `nclan_seed.cpp:251`, per-NE, before the per-slot loop:
  ```c
  if (flp->disable) { ++skipped; continue; }
  ```
  es64 is consulted later (`nclan_seed.cpp:268-277`) only for ip/port/ssh.

- **Watch loop** — `nclan_seed_read.cpp`. `nclan_seed_ctree_row` already fetches
  the es64 record (for ip/port/dtype) but **discards `rec.enabled`**
  (`nclan_seed_read.cpp:73`), then `nclan_seed_read_row` overwrites `enabled`
  from `nclan_seed_pg_enabled` — a `SELECT enabled FROM ne_link_data` that
  mirrors `frame_link.disable`.

Both paths therefore ignore the `es64_snmp_info.enabled` field that is already
in hand.

## Goal

When an `es64_snmp_info` record is present for a card slot, that record's
`enabled` field is the **sole authority** for whether nclan-seed publishes the
slot as up — overriding `frame_link.disable` in **both** directions. This
resolves the 1801 mismatch and eliminates the split-authority that let the two
tables drift apart.

## Data model facts

- `es64_snmp_info` — unique key `(neId, slot, type)`. Fields:
  `int enabled` (`include/alcatel_es64_db.h:511`), `char del_flag`,
  `int start_clan`, plus ipAddr/cliPort/ssh_flag. **Per card slot.**
- `frame_link.disable` — **per NE** (whole node).
- Precedence granularity is therefore per-(neid, slot): individual clan cards
  can be independently enabled, which the seed/watch topics already support
  (they publish per neid+slot).

## The rule (agreed)

Total per-slot override:

```
slot_enabled(neid, slot, dtype):
    if es64 record present for (neid, slot, DST_CLI) and rec.del_flag == 0:
        return rec.enabled != 0          # es64 wins, both directions
    else:
        return frame_link[neid].disable == 0
```

- Non-es64 dtypes never have an es64 record → frame_link stays authoritative
  automatically (no special-casing needed).
- A `del_flag`'d es64 record does **not** count as present.

## Approach A — shared precedence helper

Encode the rule once and call it from both paths, so they cannot re-diverge
(re-divergence is the exact bug class being fixed).

### Helper

New function (name TBD in plan, e.g. `nclan_seed_slot_enabled`) that:
1. Attempts `get_es64_snmp_info_neId_slot(neid, slot, DST_CLI)`.
2. On `GOT_ONE` with `del_flag == 0`, returns `rec.enabled ? 1 : 0` and, so the
   record isn't fetched twice, makes the fetched record available to the caller
   for ip/port/ssh reuse (out-param or a thin "fetch once, decide + reuse"
   shape — settle in the plan).
3. Otherwise returns `frame_link[neid].disable == 0 ? 1 : 0`.

Returns an enabled bit; a distinct error/`-1` return is only needed if the DB
handle is unavailable (mirror existing `nclan_seed_pg_enabled` error contract).

### Seed walk — `nclan_seed.cpp`

- Remove the per-NE early skip `if (flp->disable) continue;` (line 251).
- Keep the structural per-NE skips `NE_UNASSIGNED` (242) and `!HAS_CLAN_CARD`
  (244) — those are provisioning structure, not enable state.
- Inside the per-slot loop: compute `enabled` via the helper. If not enabled,
  `++skipped; continue;` that slot. Reuse the es64 record the helper fetched for
  the existing ip/port/ssh assignment (`nclan_seed.cpp:268-277`) — no second
  lookup.
- Preserve current behavior that ip/port/ssh sourcing is for `es64_dtype`
  slots; the enable helper runs for all slots (non-es64 → frame_link path).

### Watch path — `nclan_seed_read.cpp`

- `nclan_seed_ctree_row`: capture `rec.enabled` and `rec.del_flag` into
  `NclanSeedRow` (add fields as needed) instead of hard-coding
  `out->enabled = 0`.
- `nclan_seed_read_row`: if the es64 record was present (del_flag==0), set
  `out->enabled = es64.enabled`; only call `pg()` as the fallback when the
  record is absent. Keep `pg()` wired as the documented fallback even though the
  current `ctree_row` returns -1 on a missing record (so relaxing that later
  needs no rewire).
- The `ne_link_data` CLI/WORKING filtering comment/logic in
  `nclan_seed_pg_enabled` stays as-is (fallback only).

### Scope boundary (confirmed)

The watch path stays **es64-centric**. This change redirects the enable
*authority* for nodes es64 already covers; it does **not** add new
frame_link-only handling for non-es64 nodes in the watcher. Out of scope.

## Testing

- **Helper unit tests** (stub the es64 fetch + frame_link.disable) for four
  cases:
  1. es64 present + enabled=1 → up (even if frame_link.disable=1)  ← the 1801 case
  2. es64 present + enabled=0 → down (even if frame_link.disable=0)
  3. es64 absent + frame_link.disable=0 → up
  4. es64 absent + frame_link.disable=1 → down
  5. es64 present but del_flag=1 → treated as absent (falls to frame_link)
- **Watch-path test** — extend existing `nclan_seed_read` tests (they already
  inject `pg`/`ctree` stubs): assert `es64.enabled` beats a *conflicting* PG
  value, and that `pg()` is consulted only when the es64 record is absent.
- **Regression** — existing seed/watch/notify tests still pass.

## Non-goals

- Fixing whatever provisioning path let `es64_snmp_info.enabled` and
  `frame_link.disable` diverge (worth a separate look, but not this change).
- Any change to ncland itself (`ncland_warehouse` / `ncland_notify`); the fix is
  entirely in the nclan-seed publisher.
- Non-es64 node handling in the watch loop.

## Platform

C++ compiled under the project's nmake toolchain; must build on gcc 4.8.5
baseline. Helper is plain C++ over existing es64/frame_link APIs — no new deps.
