# Design: Generic loopback → RDB immediate sync for restjson EM

**Date:** 2026-09-29
**Author:** Dan Nuzzo (dnuzzo@lightriver.com)
**Related:** #4962 (DMAT6 loopback state read; crash fix already merged — PRs #7657/#7658), and the follow-on "status doesn't update right away" lag.

## Problem

When an operator operates or releases a loopback through the restjson EM, the
Loopback tab does not reflect the new state on the first retrieve — a second
retrieve ~6s later is needed.

Root cause (proven by trace instrumentation, see memory
`project_dmat6_loopback_rdb_lag`): the restjson EM send path
(`empsim_dmat6_upd` → `em_upd`) operates the loopback via an **SNMP MIB set**
and returns. It never writes `facility.loopback` in RDB. The RDB row is only
updated later by the asynchronous NE DB-change / alarm-match handler
(`cnc/dno/src/alc_1830_dbchg.c:1692` → `rdb_facility_find_and_update_loopback`),
which fires when the NE reports the change back — the ~6s lag.

Every other card family already writes RDB immediately in its send path
(`rdb_facility_update_loopback` at `uCat1830Pss.c:1129/1288`, `uCatCienaWs.c`,
`uCatInfinera*.cpp`, `uCatCisco4200.cpp`, …), all on the RMT path
(`rmt_atasade.c`). The restjson EM is the outlier.

Note: `facilities` is a **view** (`SELECT * FROM facility WHERE invalid = FALSE`,
created in `cnc/rdb/upgrade/48.1.15.sql`). Writes to the base `facility` table
are visible through the view immediately; the `facility`/`facilities`
singular/plural split is not a bug.

## Decision

A read-side retry was previously rejected — the fix belongs where the state is
written, not in every reader. This design adds an **optimistic immediate RDB
write** in the restjson EM, placed **generically** so all restjson EM cards
benefit.

## Approach (chosen: A — generic choke point)

All restjson EM requests funnel through one dispatcher in `empsim.cpp` (~line
189): it parses `action`/`tab`/`neid`/`aid`/`parms`, then `switch(cardtp)` calls
the per-card handler (`empsim_sfm6` / `empsim_dd2m4` / `empsim_dmat6`) and
returns `rc`. The post-switch point has full context and covers every card.

Loopback operate/release is **always** driven through the Loopback tab
(`TAB_EQPT_LOOPBACK`) — confirmed with the product owner — so keying on that tab
catches every case with no false positives.

Alternatives rejected:
- **B. Shared helper called from each per-card `_upd`.** Not automatic; every
  future card must remember to call it. Less generic than required.
- **C. Detect the loopback OID inside `em_upd()`.** `em_upd` is a generic
  MIB-set loop; sniffing which OID means "loopback" is fragile and card-specific.

## Components

One static helper in `empsim.cpp`:

```c
/**
 * @brief Optimistically mirror a just-sent loopback operate/release into RDB.
 *
 * Best-effort: on any problem it logs and returns without disturbing the
 * caller's rc. The async NE DB-change handler remains the backstop.
 *
 * @param r  The EM request that was just handled (SEND on the Loopback tab).
 */
static void em_sync_loopback_rdb(EM_REQ *r);
```

Invoked from the dispatcher immediately after the `switch(cardtp)`:

```c
if (rc == SUCCESS && r->action == PORT_PROV_SEND_ACTION && r->tab == TAB_EQPT_LOOPBACK)
    em_sync_loopback_rdb(r);
```

## Data flow

1. Operator submits Operate/Release on the Loopback tab → dispatcher →
   per-card handler → `em_upd` sends the SNMP set → returns `rc == SUCCESS`.
2. Dispatcher calls `em_sync_loopback_rdb(r)`.
3. Helper derives the target loopback state from the submitted form fields:
   - `action` → `"Operate Loopback"` | `"Release Loopback"`
   - `LPBKTYPE` → `"FACILITY"` | `"TERMINAL"`
   - Mapping: Release → `RDB_FACILITY_LOOPBACK_NONE`;
     Operate + FACILITY → `RDB_FACILITY_LOOPBACK_FACILITY`;
     Operate + TERMINAL → `RDB_FACILITY_LOOPBACK_TERMINAL`.
4. Helper resolves the PTP the same way the read does
   (`rdb_fac_to_ptp(getDbConn(), neid, aid)` → `ptpFac->facAid`; fall back to
   `r->aid` if no PTP), so the row written is the row `port_is_looped_rdb()`
   reads.
5. Helper calls
   `rdb_facility_find_and_update_loopback(getDbConn(), neid, ptpFacAid, enum)`,
   which updates the base `facility` table (visible via the `facilities` view
   at once) and calls `rdb_card_cache_hash_update()` — refreshing both the
   direct-SQL and cached read paths.
6. Next retrieve reads the fresh state immediately. When the async NE
   DB-change handler later runs, it reconciles the row to confirmed NE truth.

## Includes

`empsim.cpp` must add the `rdb_otn_port_facilities.h` include wrapped in
`extern "C"` (the header has no guards; unwrapped inclusion from C++ mangles
`getDbConn` / `rdb_fac_to_ptp` / `rdb_facility_find_and_update_loopback` and
segfaults at call time — same trap as #4962).

## Error handling

Best-effort, never changes `rc`:
- `getDbConn()` NULL → trace + return.
- PTP resolve fails → write against `r->aid`.
- `action` / `LPBKTYPE` missing or unrecognized → trace + return.
- RDB write failure → trace; async handler is the backstop.

## Open items to confirm during implementation (not blocking design)

1. **DB-Only field.** The Loopback form has a `DBONLY` toggle. We write RDB on
   success regardless (correct for both modes). Verify DB-Only doesn't already
   write `facility.loopback` elsewhere so we are not double-driving it — the
   write is idempotent, so this is a cleanliness check.
2. **PTP vs raw AID target.** Confirm via trace that `ptpFac->facAid` is the row
   the read observes, so read and write stay aligned.
3. **Form field access.** Confirm the field names / accessor (`GPV("action")`
   vs `GPV("$action")`, `GPV("LPBKTYPE")`) as delivered on a Loopback SEND;
   read from `r->parms` if the GPV globals are not valid at the choke point.

## Testing

No EM unit-test harness exists. Verify by the trace method used for #4962:
- Operate FACILITY → first retrieve shows `looped` non-zero immediately (no ~6s).
- Operate TERMINAL → first retrieve shows TERMINAL.
- Release → first retrieve shows NONE immediately.
- Confirm a DMAT6 client port and (if available) an sfm6/dd2m4 card to prove the
  generic hook fires for all restjson cards.

## Build / patchbuild

Changed file `cnc/restjson/src/empsim.cpp` is in `OBJ_EMLIB` (`rjlib.mk`) →
rebuilds **libem++.so**. Restart processes / INC: to be confirmed with the user
per the patchbuild-notes convention.
