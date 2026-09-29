# restjson EM Loopback → RDB Immediate Sync Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the restjson EM write `facility.loopback` to RDB immediately after a successful loopback operate/release, so the Loopback tab shows the new state on the first retrieve instead of ~6s later.

**Architecture:** A generic hook in the `empsim.cpp` request dispatcher, fired after the per-card `switch(cardtp)` on a successful `SEND` to the Loopback tab. It calls a new static helper `em_sync_loopback_rdb()` that derives the intended state from the submitted form and writes it via the existing `rdb_facility_find_and_update_loopback()`. Covers every restjson EM card (sfm6/dd2m4/dmat6/future). The async NE DB-change handler remains the backstop that reconciles to confirmed NE state.

**Tech Stack:** C++ (`cnc/restjson/src`, builds `libem++.so` via Lucent nmake `rjlib.mk`), RDB (`rdb_otn_port_facilities`), SNMP EM.

**Spec:** `~/WorkNotes/specs/2026-09-29-restjson-em-loopback-rdb-sync-design.md`

**Testing note:** There is no unit-test harness for the restjson EM layer (handlers are driven by a live NE over SNMP). Verification is therefore (a) a clean `nmake` build and (b) a runtime trace check against a live/simulated NE, mirroring how #4962 was verified. This is a deliberate, honest adaptation of TDD to a layer with no test seam — do not fabricate a unit-test framework.

---

## File Structure

- Modify: `cnc/restjson/src/empsim.cpp`
  - Add an `extern "C"`-wrapped `#include <rdb_otn_port_facilities.h>` (top, after existing includes).
  - Add the static helper `em_sync_loopback_rdb(EM_REQ *r)` (above the dispatcher function that contains the `switch(r.cardtp)`).
  - Add the trigger call after that `switch`.

No other files change. `empsim.cpp` is in `OBJ_EMLIB` (`rjlib.mk`) → rebuilds `libem++.so`.

---

## Task 1: Add the RDB header include (extern "C" wrapped)

**Files:**
- Modify: `cnc/restjson/src/empsim.cpp` (after `#include <trace.h>`)

- [ ] **Step 1: Add the wrapped include**

Locate the include block at the top of `empsim.cpp` ending with:

```cpp
#include <trace.h>
```

Immediately after that line, add:

```cpp
/* rdb_otn_port_facilities.h has no extern "C" guards but its functions are
 * defined in C files; including it from this C++ file unwrapped mangles
 * getDbConn / rdb_fac_to_ptp / rdb_facility_find_and_update_loopback and
 * segfaults at call time (same trap as #4962). */
extern "C" {
#include <rdb_otn_port_facilities.h>
}
```

- [ ] **Step 2: Verify it compiles (no use yet)**

Run (with `BASE`/`VPATH` aligned to the repo root):

```bash
cd $BASE/cnc/restjson/src && nmake -f rjlib.mk ../../../3b2/lib/libem++.so
```

Expected: builds without new errors. (If the header pulls conflicting symbols, stop and report — do not hack include order.)

- [ ] **Step 3: Commit**

```bash
git add cnc/restjson/src/empsim.cpp
git commit -m "restjson EM: include rdb_otn_port_facilities.h (extern C) for loopback sync"
```

---

## Task 2: Implement the `em_sync_loopback_rdb()` helper

**Files:**
- Modify: `cnc/restjson/src/empsim.cpp` (add static function immediately above the dispatcher function that holds `switch(r.cardtp)` — the one calling `empsim_sfm6/dd2m4/dmat6`, around line 150)

- [ ] **Step 1: Add the helper**

Insert this complete function just before that dispatcher function's definition:

```cpp
/**
 * @brief Optimistically mirror a just-sent loopback operate/release into RDB.
 *
 * The restjson EM operates loopbacks over SNMP and returns without writing the
 * facility.loopback column; RDB is otherwise only updated ~6s later by the
 * asynchronous NE DB-change handler, so the Loopback tab shows a stale state on
 * the first retrieve. This writes the requested state immediately so the next
 * retrieve is correct. Best-effort: on any problem it logs and returns, leaving
 * the async handler as the backstop that reconciles to confirmed NE state.
 *
 * @param r  EM request just handled (a successful SEND on the Loopback tab).
 */
static void em_sync_loopback_rdb(EM_REQ *r)
{
	const char *action   = GPV("action");
	const char *lpbktype = GPV("LPBKTYPE");

	if (!action || !action[0]) {
		TRACE(1,"%s: no action field; skip loopback RDB sync for %d.%s\n",
			__func__, r->neid, r->aid);
		return;
	}

	RdbFacilityLoopback loopback;
	if (strstr(action, "Release")) {
		loopback = RDB_FACILITY_LOOPBACK_NONE;
	} else if (lpbktype && strstr(lpbktype, "TERMINAL")) {
		loopback = RDB_FACILITY_LOOPBACK_TERMINAL;
	} else if (lpbktype && strstr(lpbktype, "FACILITY")) {
		loopback = RDB_FACILITY_LOOPBACK_FACILITY;
	} else {
		TRACE(1,"%s: unrecognized action=[%s] lpbktype=[%s]; skip %d.%s\n",
			__func__, action, lpbktype ? lpbktype : "", r->neid, r->aid);
		return;
	}

	DBCONN *dbc = getDbConn();
	if (!dbc) {
		TRACE(1,"%s: no DB handle; skip loopback RDB sync for %d.%s\n",
			__func__, r->neid, r->aid);
		return;
	}

	/* Write the same facility row the read observes: the PTP that the read
	 * resolves via rdb_fac_to_ptp()/port_is_looped_rdb(). Fall back to the
	 * request AID when no PTP is found. */
	char *facAid = r->aid;
	OtnPortFacilityEntry *ptpFac = rdb_fac_to_ptp(dbc, r->neid, r->aid);
	if (ptpFac)
		facAid = ptpFac->facAid;

	int wrc = rdb_facility_find_and_update_loopback(dbc, r->neid, facAid, loopback);
	TRACE(1,"%s: %d.%s loopback<-%d wrc=%d\n",
		__func__, r->neid, facAid, (int)loopback, wrc);

	if (ptpFac)
		destroy_facility(ptpFac, TRUE);
}
```

- [ ] **Step 2: Verify it compiles**

Run:

```bash
cd $BASE/cnc/restjson/src && nmake -f rjlib.mk ../../../3b2/lib/libem++.so
```

Expected: builds clean. A `-Wunused-function` warning for `em_sync_loopback_rdb` is acceptable at this step (wired up in Task 3). If it errors on an unknown symbol (`GPV`, `RdbFacilityLoopback`, `rdb_fac_to_ptp`, `getDbConn`), stop — Task 1's include or the `emlib.h` `GPV` macro is not in scope.

- [ ] **Step 3: Commit**

```bash
git add cnc/restjson/src/empsim.cpp
git commit -m "restjson EM: add em_sync_loopback_rdb() optimistic loopback writer"
```

---

## Task 3: Wire the hook into the dispatcher

**Files:**
- Modify: `cnc/restjson/src/empsim.cpp` (immediately after the `switch(r.cardtp) { ... }` that calls `empsim_dmat6(&r)` etc., ~line 204)

- [ ] **Step 1: Add the trigger**

Find the end of the dispatch switch:

```cpp
	case DMAT6_CARD:
		rc = empsim_dmat6(&r);
		break;
	case -3:
		rc == empsim_newcard(&r);
	default:
		rc = FAILURE;
		break;
	}
```

Immediately after the closing `}` of that `switch`, add:

```cpp

	/* After a successful loopback operate/release (Loopback tab is the only
	 * path that changes loop state), mirror the requested state into RDB so the
	 * next retrieve reflects it immediately instead of waiting for the async NE
	 * DB-change handler. Generic across all restjson EM cards. */
	if (rc == SUCCESS && r.action == PORT_PROV_SEND_ACTION && r.tab == TAB_EQPT_LOOPBACK)
		em_sync_loopback_rdb(&r);
```

- [ ] **Step 2: Verify it compiles**

Run:

```bash
cd $BASE/cnc/restjson/src && nmake -f rjlib.mk ../../../3b2/lib/libem++.so
```

Expected: clean build, no unused-function warning now.

- [ ] **Step 3: Commit**

```bash
git add cnc/restjson/src/empsim.cpp
git commit -m "restjson EM: sync loopback state to RDB on successful Loopback SEND (#4962 follow-on)"
```

---

## Task 4: Runtime trace verification (live/simulated NE)

**Files:** none (manual verification with the operator/NE)

- [ ] **Step 1: Deploy the rebuilt `libem++.so`**

Rebuild product is `libem++.so`; restart per patchbuild convention (confirm restart process with the user — likely `USER`).

- [ ] **Step 2: Operate a FACILITY loopback, retrieve once**

On a DMAT6 client port (e.g. `DMAT6-1-1-C1`): Operate Loopback, type FACILITY, then do a **single** retrieve.

Expected in the trace: `em_sync_loopback_rdb: <neid>.<aid> loopback<-<FACILITY enum> wrc=0`, and the Loopback tab shows **Release Loopback / FACILITY** on the first retrieve (no ~6s wait). Confirm `facAid` in the trace is the PTP AID the read uses (resolves spec open item #2).

- [ ] **Step 3: Release the loopback, retrieve once**

Release Loopback, single retrieve.

Expected: `loopback<-<NONE enum>` in trace; the tab shows **Operate Loopback** immediately.

- [ ] **Step 4: (If available) repeat on an sfm6 or dd2m4 card**

Confirms the generic hook fires for non-DMAT6 restjson cards.

- [ ] **Step 5: Confirm async handler still reconciles**

Wait for the async NE DB-change cycle; confirm the state remains correct (not flipped back), i.e. the optimistic write agrees with confirmed NE truth.

---

## Self-Review

- **Spec coverage:** trigger (Task 3), state mapping + PTP target + write + includes + error handling (Tasks 1–2), testing (Task 4) — all spec sections mapped. DB-Only (spec open item #1) resolved during planning: no restjson code reads `DBONLY`, so we write on SUCCESS unconditionally; helper does not branch on it. PTP-vs-AID (item #2) verified in Task 4 Step 2. Form-field accessor (item #3) resolved: `GPV("action")`/`GPV("LPBKTYPE")` via `emlib.h`'s `GPV(N) = get_tokval(r->tok,r->ntok,N)`.
- **Placeholder scan:** none — all code shown in full.
- **Type consistency:** `em_sync_loopback_rdb(EM_REQ*)`, `RdbFacilityLoopback`, `rdb_facility_find_and_update_loopback(DBCONN*,int,char*,RdbFacilityLoopback)`, `rdb_fac_to_ptp(DBCONN*,int,char*)`, `destroy_facility(OtnPortFacilityEntry*,BOOLEAN)` all match header declarations. Constants `PORT_PROV_SEND_ACTION` ('S') and `TAB_EQPT_LOOPBACK` (15) match `flex_form.h` / `port_prov.h`.
