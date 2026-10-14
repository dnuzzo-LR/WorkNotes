# #8205 MGNE Any-GNE RT Login — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** In multi-GNE comm groups, let any GNE bring a down RT up as fast as possible. Do it by ending denied logins at once, having only the sending lan time out its own logins, and backing off per RT and per GNE (SARB). Everything is gated by `MGNE_ANYGNE` in the ping file, off by default.

**Architecture:** The new bookkeeping goes in a self-contained module, `mgne_anygne.c` in `liblan`. It holds the pending-login table, the per-RT and GNE backoffs, and TL1 deny parsing and classification. It has no shared memory, no `TRACE`, and takes time as a parameter, so it's unit-tested standalone. `lanalive.c` calls it at five hook points, each wrapped in `if (lan_mgne_on())`. The tunables live in `comm_tunable.c` and its `_s` mirror, and the banner is printed in `lan.c` and `lan_s.c`.

**Tech Stack:** C (gnu89; gcc 4.8.5 baseline, RHEL 7/8/9), Lucent nmake, and assert-based unit tests following `cnc/flexlib/test/`.

**Spec:** `~/WorkNotes/specs/2026-10-14-8205-mgne-any-gne-login-design.md`. Read it before Task 1; §-references below point into it.

## Global Constraints

- Branch `issue-8205-53.0` (base `inc53.0`). Commit subjects are prefixed `[inc53.0] #8205 `.
- Before any nmake: `BASE` = repo root, and the first `VPATH` entry = `BASE`. If headers or `global*.nmk` aren't found, **stop and tell Dan**; don't hack paths.
- All Makefiles are Lucent nmake. Use the `writing-nmake-makefiles` skill for every `.mk` edit.
- Must compile with gcc 4.8.5. No C99-only constructs in `lanalive.c` beyond what it already uses. Put declarations at block start in new code. No `stdbool` in the module.
- **Feature off ⇒ identical behaviour.** Every new branch in `lanalive.c` sits inside `if (lan_mgne_on())`.
- **No change to `struct frame_link` / `Frm[]` / md frm sync.** New state is process-local only.
- Doxygen `/** */` comments on every new function, struct, enum and file-static. Use the prefix `mgne_` (module) or `lan_mgne_` (lanalive glue), and `MGNE_` for macros.
- Tunable keys, defaults and clamps (spec §6.1):
  - `MGNE_ANYGNE=0|1`, default 0.
  - `MGNE_RT_BACKOFF=<min>/<max>`: default min = `PollCycle` (stored as 0 meaning "use PollCycle"), max 300.
  - `MGNE_SARB_BACKOFF=<min>/<max>`: default 60/300.
  - Clamp: min ≥ 1, max ≥ min, max ≤ 3600 (`MGNE_BACKOFF_CAP 3600`).
- The D0 trace on every ended attempt has this exact shape:
  `MGNE: NE:%d login via GNE:%d DENY %s from %s -> rt backoff %ds%s\n`.
  The trailing `%s` is `" gne backoff %ds"` or `""`.
- The stale guard threshold is `3 * WaitForLogin`.
- The rebuild/restart list for the PR is **asked of Dan**, never guessed. The builder check found at least `liblan.so`, `libsdi.so`, `libsdi_s.so`, `lan` and `lan.small`.

## Review Focus

1. **A lan restarts or dies mid-login.** RTs are left `LAN_LOGGING_IN` and no lan has them pending. Expect them to be freed within `3*WaitForLogin`, not stuck forever. Pinned by `test_stale_predicate` (Task 1); wired in Task 4.
2. **The reply TID differs in case or quoting from the GNE's TID** (`"toldohvdo…"` vs `TOLDOHVDO…`). Expect it still classified as from the GNE. Pinned by `test_parse_tid_quote_case` (Task 1).
3. **A truncated or non-response buffer** (no `M  ` line, no TID line, autonomous `REPT`). Expect a parse failure, no crash, and fall-through to the existing code. Pinned by `test_parse_rejects_non_response` (Task 1).
4. **Misconfigured backoff** (`max<min`, negative, huge). Expect clamping per Global Constraints, and doubling that never exceeds max. Pinned by `test_clamp_backoff` and `test_rt_backoff_doubles_to_max` (Task 1).
5. **A garbage ctag decodes to a neid ≤ 0 or ≥ `NeAvail`.** Expect every table operation to ignore it, with no out-of-bounds write. Pinned by `test_neid_out_of_range_ignored` (Task 1).

---

### Task 1: `mgne_anygne` module (pure logic) + unit tests

**Files:**
- Create: `include/mgne_anygne.h`
- Create: `cnc/sdi/src/mgne_anygne.c`
- Create: `cnc/sdi/test/test_mgne_anygne.c`
- Create: `cnc/sdi/test/test.mk` (modelled on `cnc/flexlib/test/test.mk`, but `CFLAGS = -std=gnu89 -g -Wall -Wextra -Werror -O2`, without `-m32`)

**Interfaces:**
- Produces (in `include/mgne_anygne.h`, with include guard `MGNE_ANYGNE_H`):
```c
#include <time.h>
#define MGNE_BACKOFF_CAP 3600

/** @brief Classification of a TL1 DENY relevant to MGNE login handling. */
typedef enum {
	MGNE_DENY_NONE = 0,	/**< Not a deny, or not a login/logout ctag. */
	MGNE_DENY_GNE_RT,	/**< GNE-originated deny, RT-specific (IITA etc.). */
	MGNE_DENY_GNE_SARB,	/**< GNE-originated SARB: GNE out of resources. */
	MGNE_DENY_RT		/**< Deny originated by the RT itself. */
} mgne_deny_class_t;

/** @brief Parsed header of a TL1 response. */
typedef struct {
	char tid[64];		/**< Source TID, quotes stripped, upper-cased. */
	char ctag_kind;		/**< 'G' (ZG login), 'T' (ZT logout), 0 other. */
	const char *ctag_body;	/**< Points into msg just past "ZG"/"ZT" (for a36l_ne). */
	int is_deny;		/**< 1 if completion code is DENY. */
	char code[8];		/**< First error-code token after DENY, e.g. "IITA". */
} mgne_resp_t;

int    mgne_init(int ne_avail);                 /* 0 ok; -1 calloc failure; idempotent */
void   mgne_configure(int rt_min, int rt_max, int sarb_min, int sarb_max);
void   mgne_clamp_backoff(int *min, int *max);  /* applies Global Constraints clamp */
void   mgne_pending_set(int neid, time_t now);
time_t mgne_pending_get(int neid);              /* 0 if none / out of range */
void   mgne_pending_clear(int neid);
int    mgne_pending_next(int after);            /* next neid > after with pending, or -1 */
int    mgne_rt_backoff_apply(int neid, time_t now);   /* returns interval applied (s) */
void   mgne_rt_backoff_reset(int neid);
int    mgne_rt_backoff_remaining(int neid, time_t now); /* 0 if may try */
int    mgne_gne_backoff_apply(time_t now);
void   mgne_gne_backoff_reset(void);
int    mgne_gne_backoff_remaining(time_t now);
int    mgne_is_stale(time_t acc_time, time_t now, int wait_for_login); /* 1 if now-acc_time > 3*wait */
int    mgne_parse_resp(const char *msg, mgne_resp_t *out);   /* 0 parsed; -1 not a TL1 response */
mgne_deny_class_t mgne_classify(const mgne_resp_t *r, const char *gne_tid);
```
- Backoff semantics: the first `apply` sets the interval to min. Each later `apply` before a `reset` doubles it, capped at max. The `next_try` time is now + interval. `remaining` is `max(0, next_try-now)`. Table operations with `neid <= 0 || neid >= ne_avail` are no-ops (get/remaining return 0; `pending_next` skips).
- `mgne_classify`:
  - Not a deny, or `ctag_kind` == 0 → `MGNE_DENY_NONE`.
  - `r->tid` equals `gne_tid` (compared after the same strip-quotes/upper-case normalisation) and code is `SARB` → `GNE_SARB`.
  - Equal TIDs, any other code → `GNE_RT`.
  - Different TIDs → `RT`.
- `mgne_parse_resp`: skip leading whitespace and CR/LF. The first token is the TID (strip `"` and upper-case it), followed by date and time. The next line is `M  <ctag> <COMPLD|DENY>`. If DENY, `code` is the first non-blank token on the following line.

- [ ] **Step 1: Write the failing tests** in `cnc/sdi/test/test_mgne_anygne.c`, using the `struct test {name, fn}` table and `main` runner style from `cnc/flexlib/test/test_farray.c`, with `assert`. Use these real production replies as fixtures (`\r\n` line ends):
  - `IITA_GNE` = `"\r\n\n   TOLDOHVDO12030204A 26-10-13 16:26:05\r\nM  ZG0QJY DENY\r\n   IITA\r\n   \"INPUT, INVALID TARGET IDENTIFIER\"\r\n;"`
  - `SARB_GNE` = same shape, TID `LSVMKYPLO20050107A`, ctag `ZG0QO7`, code `SARB`.
  - `PLNA_RT` = TID `DELTOHLCO12011114A`, ctag `ZT0QIV`, code `PLNA`.
  - `COMPLD_RT` = TID `ANARMICCO11010107A`, `M  ZG0QDC COMPLD`.

  The tests and their assertions:
  - `test_parse_iita_gne`: parse returns 0; `tid`=="TOLDOHVDO12030204A"; `ctag_kind`=='G'; `strncmp(ctag_body,"0QJY",4)==0`; `is_deny`==1; `code`=="IITA"; classify with gne_tid "TOLDOHVDO12030204A" → `MGNE_DENY_GNE_RT`.
  - `test_classify_sarb_gne`: `SARB_GNE` with gne_tid "LSVMKYPLO20050107A" → `MGNE_DENY_GNE_SARB`.
  - `test_classify_rt_deny`: `PLNA_RT` with gne_tid "TOLDOHVDO12030204A" → `MGNE_DENY_RT`; `ctag_kind`=='T'.
  - `test_classify_compld_none`: `COMPLD_RT` → `is_deny`==0, classify → `MGNE_DENY_NONE`.
  - `test_parse_tid_quote_case`: `IITA_GNE` with the TID written as `"toldohvdo12030204a"` → classify with gne_tid "TOLDOHVDO12030204A" → `GNE_RT`.
  - `test_parse_rejects_non_response`: returns -1 for each of `""`, `"garbage"`, the TID line alone, and an autonomous `"\r\n\n   TID 26-10-13 16:26:05\r\n*C 1234 REPT ALM\r\n;"`.
  - `test_pending_roundtrip`: init(1000); set(34375,100) → get==100; `pending_next(0)`==34375; `pending_next(34375)`==-1; clear → get==0.
  - `test_rt_backoff_doubles_to_max`: configure(20,300,60,300); applies at now=0 return 20, 40, 80, 160, 300, 300; remaining(neid, 0) after the last apply ==300; reset → remaining==0.
  - `test_gne_backoff`: configure(20,300,60,300); `gne_backoff_apply(1000)`==60; remaining(1030)==30; remaining(1060)==0; apply again ==120; reset → remaining==0.
  - `test_clamp_backoff`: (0,0)→(1,1); (50,10)→(50,50); (-5,99999)→(1,3600); (20,300)→unchanged.
  - `test_stale_predicate`: `is_stale(100, 100+75, 25)`==0; `is_stale(100, 100+76, 25)`==1; `is_stale(0, 5, 25)`==1 (ACC_TIME 0 counts as stale).
  - `test_neid_out_of_range_ignored`: init(1000); set(0,..), set(-3,..) and set(1000,..) don't crash and get returns 0 for each; `rt_backoff_apply(1000,0)`==0; `rt_backoff_remaining(-1,0)`==0.

- [ ] **Step 2: Run them and confirm they fail.**
  Run, from `cnc/sdi/test`: `gcc -std=gnu89 -Wall -Wextra -Werror -I$BASE/include -o /tmp/claude-1039/t_mgne test_mgne_anygne.c ../src/mgne_anygne.c`
  Expected: a compile failure, because `mgne_anygne.h` and `mgne_anygne.c` don't exist yet.

- [ ] **Step 3: Implement `include/mgne_anygne.h` and `cnc/sdi/src/mgne_anygne.c`** to the interface above. Use file-static `calloc`'d arrays sized `ne_avail`: `time_t *pending`, `time_t *rt_next`, `int *rt_ival`. Keep the GNE backoff in statics. There must be no dependency on `Frm`, `TRACE` or any netFLEX global, because the module must link in the bare test.

- [ ] **Step 4: Run the tests and confirm they pass.**
  Run the same gcc command, then `/tmp/claude-1039/t_mgne`.
  Expected: the runner prints each test name followed by `ok`, and exits 0.
  Also run `gcc -std=gnu89 -m32 …` if 32-bit multilib is installed, since production `lan` is 32-bit unless `L64_SETUP`.

- [ ] **Step 5: Add the test binary to `cnc/sdi/test/test.mk`** (`$(PBIN)/test_mgne_anygne :: test_mgne_anygne.o ../src/mgne_anygne.o`) using the `writing-nmake-makefiles` skill.

- [ ] **Step 6: Commit**
```bash
git add include/mgne_anygne.h cnc/sdi/src/mgne_anygne.c cnc/sdi/test/
git commit -m "[inc53.0] #8205 add mgne_anygne login bookkeeping module with unit tests"
```

### Task 2: Tunables, banner, build wiring and lanalive init

**Files:**
- Modify: `include/comm_tunable.h` (extern decls next to `GneRtLoginGap`)
- Modify: `cnc/sdi/src/comm_tunable.c` (globals near line 55; parse in the `*buf == 'M'` branch at ~453)
- Modify: `cnc/sdi/src/comm_tunable_s.c` (an identical mirror; `GneRtLoginGap` is at :53/:325)
- Modify: `cnc/sdi/src/lan.c` (banner ~647; init call right after `MultiGNE = GNE_Frm->multi_gne;` at :637)
- Modify: `cnc/sdi/src/lan_s.c` (banner ~604; init after :594)
- Modify: `cnc/sdi/src/sdilib.mk` (add `mgne_anygne.o` to `OBJ_LIBLAN`)
- Modify: `cnc/sdi/src/lanalive.c` (new glue functions near the other file-statics, ~line 87)

**Interfaces:**
- Consumes: `mgne_init`, `mgne_configure`, `mgne_clamp_backoff` (Task 1).
- Produces:
  - Globals `int MgneAnyGne = 0; int MgneRtBackoffMin = 0; int MgneRtBackoffMax = 300; int MgneSarbBackoffMin = 60; int MgneSarbBackoffMax = 300;`, defined in **both** `comm_tunable.c` and `comm_tunable_s.c`.
  - In `lanalive.c`:
    - `int lan_mgne_on(void)`: returns `MultiGNE && MgneAnyGne && <init succeeded>`.
    - `void lan_mgne_init(void)`: called from `lan.c`/`lan_s.c` after `MultiGNE` is set. It resolves `MgneRtBackoffMin==0 → PollCycle`, applies `mgne_clamp_backoff` to both pairs, then calls `mgne_init(NeAvail)` and `mgne_configure(...)`. On `mgne_init` failure it traces D0 and the feature stays off.

- [ ] **Step 1: Parse the keys.**
  - `MGNE_ANYGNE=%d` (any non-zero becomes 1).
  - `MGNE_RT_BACKOFF=%d/%d`.
  - `MGNE_SARB_BACKOFF=%d/%d`.

  Each gets a `TRACE(D2, ...)` like its neighbours. Don't clamp here; clamping happens in `lan_mgne_init`, which also traces any changed value at D0. Mirror the change in `comm_tunable_s.c`.

- [ ] **Step 2: Add the banner line** in `lan.c` and `lan_s.c`, just after the `MULITGNE:` line:
  `"       MGNE_ANYGNE:%d RtBackoff:%d/%d SarbBackoff:%d/%d Active:%d\n"`.
  Print it after `lan_mgne_init()`, so it shows resolved values and `Active` = `lan_mgne_on()`. If the banner is printed before `MultiGNE` is set, print this line separately, right after the init call.

- [ ] **Step 3: Wire the build.** Add `mgne_anygne.o` to `OBJ_LIBLAN` in `sdilib.mk` (use the `writing-nmake-makefiles` skill).

- [ ] **Step 4: Build and verify.**
  - Check BASE/VPATH.
  - From `cnc/sdi/src`: `nmake -f sdilib.mk` (`liblan`, `libsdi`, `libsdi_s`), then `nmake -f sdisrc.mk $BASE/3b2/bin/lan $BASE/3b2/bin/lan.small`.
  - Expected: no errors or new warnings. `nm -D $BASE/3b2/lib/liblan.so | grep -c mgne_` is > 0.
  - Then run `lan`'s banner path on holmvm64 if Dan approves, or just trust the build. There's no local runtime.

- [ ] **Step 5: Commit**
```bash
git add include/comm_tunable.h cnc/sdi/src/comm_tunable.c cnc/sdi/src/comm_tunable_s.c cnc/sdi/src/lan.c cnc/sdi/src/lan_s.c cnc/sdi/src/sdilib.mk cnc/sdi/src/lanalive.c
git commit -m "[inc53.0] #8205 add MGNE_ANYGNE / backoff tunables (off by default)"
```

### Task 3: End denied logins immediately (`send_to_screen` hook)

**Files:**
- Modify: `cnc/sdi/src/lanalive.c`: add a `static int lan_mgne_handle_resp(char *msg)` and call it at the top of `send_to_screen()` (~2116), **before** the existing `if (strstr(msg, "DENY") && (lptr = strstr(msg, "M  ")))` block at ~2134:
  `if (lan_mgne_on() && lan_mgne_handle_resp(msg)) return (SUCCESS);`

**Interfaces:**
- Consumes: `mgne_parse_resp`, `mgne_classify`, `mgne_pending_*`, `mgne_rt_backoff_apply`, `mgne_gne_backoff_apply` (Task 1); `lan_mgne_on` (Task 2); existing `a36l_ne()`, `Frm[]`, `GET_TID()`, `AND_STATUS_PTR`, `Dacsid`, `NeAvail`.
- Produces: `lan_mgne_handle_resp` returns 1 when it consumed the reply (the caller returns SUCCESS) and 0 to fall through to today's code.

Behaviour (spec §6.3). Use `gne_tid` = `GET_TID(&Frm[Dacsid])`, and `neid` = `a36l_ne(r.ctag_body)`:

| Reply | Action | Return |
|---|---|---|
| Parse fails, or `MGNE_DENY_NONE` | Nothing | 0 |
| `GNE_SARB`, ctag `T` | `mgne_gne_backoff_apply(now)` | 0 |
| `GNE_RT` or `GNE_SARB`, ctag `G` | See below | 1 |
| `RT` | Nothing. The RT deny path sets ACC_TIME 0 and the Task 4 sender-timeout ends it within one pass. | 0 |

For `GNE_RT` or `GNE_SARB` with ctag `G`:
- If `mgne_pending_get(neid)`:
  - Clear `LAN_LOGGING_IN` on `Frm[neid]`.
  - Call `mgne_pending_clear`.
  - `if (Frm[neid].login_trys > 0) Frm[neid].login_trys--;`
  - Call `mgne_rt_backoff_apply`; for SARB also `mgne_gne_backoff_apply`.
  - Emit the D0 `MGNE:` line from Global Constraints.
  - Also `log_msg(msg, FROM_DCS_MSG, ...)` exactly as the existing BAD TID branch does, so the event log keeps the deny.
- Either way, this skips the existing branch's quote retry, `SET_MODIFIED` and auto-disable.

- [ ] **Step 1: Implement `lan_mgne_handle_resp` and the call** as above.
- [ ] **Step 2: Build** `liblan` (as in Task 2, Step 4). Expected: clean.
- [ ] **Step 3: Desk-check against the production traces.** Feed each reply text from `cnc/sdi/src/lan.33465` (IITA ZG from TOLDOHVDO12030204A) and `lan.33244` (SARB ZG0QO7) through the Task 1 test binary: add them as two more fixtures and assertions in `test_mgne_anygne.c` (`test_prod_fixture_33465_iita`, `test_prod_fixture_33244_sarb`), copying the text exactly from the trace. Expected: `GNE_RT` and `GNE_SARB`.
- [ ] **Step 4: Commit**
```bash
git commit -am "[inc53.0] #8205 MGNE: end GNE-denied RT logins immediately, SARB GNE backoff"
```

### Task 4: RT loop: sender-only timeout, backoff gate, stale guard, no pre-claim, UP-only cap, modified resets backoff

**Files:**
- Modify: `cnc/sdi/src/lanalive.c`, at these points in the RT loop of `lanalive()` and nearby:
  - **a. Pending on send.** In `login_time` (~1734), when `lan_login(0, rt_index) != FAILURE` and `lan_mgne_on()`, call `mgne_pending_set(rt_index, curtime)`.
  - **b. Backoff gate.** In `login_time`'s `else` block, before the `GneRtLoginGap` pacing check (~1715) and therefore before `login_trys++`: if `lan_mgne_on()` and either `mgne_gne_backoff_remaining(curtime)` or `mgne_rt_backoff_remaining(rt_index, curtime)` is non-zero, trace D3 `MGNE BACKOFF: rt:%d rt_rem:%d gne_rem:%d` and `goto test_cnt`.
  - **c. LOGGING_IN branch** (~1190), when `lan_mgne_on()`:
    - **Pending here:** use `curtime - mgne_pending_get(rt)` against `WaitForLogin` instead of ACC_TIME. Treat ACC_TIME==0 (RT denied) as timed out. On timeout, keep today's log, `update_link_error`, `send_ring_logout`, `ddmstatmsg` and `set_link_status` down, plus `SET_STATUS_PTR(fptr, LAN_INIT)`. Add `mgne_pending_clear` and `mgne_rt_backoff_apply`. **Skip `NE_MODIFIED(fptr, SET_LINK_PRIV)`.**
    - **Not pending here:** if `mgne_is_stale(GET_ACC_TIME_PTR(fptr), curtime, WaitForLogin)`, clear `LAN_LOGGING_IN`, trace D0 `MGNE: NE:%d stale login by another GNE cleared`, and fall to `test_cnt`. Otherwise unlock (`if (multi_gne) unlock_neid(rt_index,1)`) and `continue`, as the existing not-yet-timed-out path does.
  - **d. No pre-claim.** At ~998–999, wrap `if (!fptr->GNE_NODE) fptr->GNE_NODE = gne;` in `if (!lan_mgne_on())`.
  - **e. UP-only cap.** In `set_multi_num_rt()` (~10441), when `lan_mgne_on()`, count an RT only if `get_link_status(&Frm[rt_index], rt_index, ProtectIndex, NO_LINK_OPTION) == NE_LINK_UP`.
  - **f. Real modified resets backoff.** In the `GET_MODIFIED(fptr)` block (~1012), on the non-RUBY path when `!fptr->disable` and `lan_mgne_on()`, call `mgne_rt_backoff_reset(rt_index)` before `goto login_time`.
  - **g. Success resets.** In `process_good_login()` (~10563) and the login-COMPLD path at ~8058–8081: if `lan_mgne_on()`, call `mgne_pending_clear(dacsid)`, `mgne_rt_backoff_reset(dacsid)`, and `mgne_gne_backoff_reset()`. Reset the GNE backoff only when `dacsid != Dacsid`, because a COMPLD from an RT proves the GNE is serving.

**Interfaces:**
- Consumes: everything from Tasks 1–3.
- Produces: no new symbols.

- [ ] **Step 1: Implement a–g.** Each is a small `if (lan_mgne_on())` insertion. The non-MGNE path must be byte-for-byte the same logic as before.
- [ ] **Step 2: Gate audit.** Run `git diff -U0 inc53.0 -- cnc/sdi/src/lanalive.c | grep -n 'mgne_'`. Expected: every hit is inside an `if (lan_mgne_on())` (or `&& lan_mgne_on()`) block, or inside a `lan_mgne_*` function. Report the listing in the task summary.
- [ ] **Step 3: Build** `liblan`, `libsdi`, `libsdi_s`, `lan`, `lan.small` and `xlansim` (everything that links `-llan`: see `sdisrc.mk` lines 7–12, 81 and 100). Expected: clean.
- [ ] **Step 4: Commit**
```bash
git commit -am "[inc53.0] #8205 MGNE: sender-only login timeout, per-RT backoff, stale guard, UP-only RT cap"
```

### Task 5: Release pending logins on exit

**Files:**
- Modify: `cnc/sdi/src/lanalive.c`: add `void lan_mgne_release_pending(void)`, which walks `mgne_pending_next` and clears `LAN_LOGGING_IN` on each neid (link stays down).
- Modify: `cnc/sdi/src/lan.c` and `lan_s.c`: call it in `exit_lan()` just before the `SENT LOGOUT` loop (`lan_logout`), when `lan_mgne_on()`.

**Interfaces:**
- Consumes: `mgne_pending_next` (Task 1), `lan_mgne_on` (Task 2).
- Produces: `void lan_mgne_release_pending(void)`.

- [ ] **Step 1: Implement it**, and trace D0 `MGNE: released %d pending logins on exit` with the count.
- [ ] **Step 2: Build** `lan` and `lan.small`. Expected: clean.
- [ ] **Step 3: Commit**
```bash
git commit -am "[inc53.0] #8205 MGNE: release this lan's pending RT logins on exit"
```

### Task 6: Cross-compiler build + lab smoke (holmvm64, Dan approves first)

- [ ] **Step 1: Build on RHEL7 gcc 4.8.5 and RHEL9 gcc 11** via the `netflex-build:nmake-build` skill (`remote_build`) for `liblan`, `libsdi`, `libsdi_s`, `lan` and `lan.small`, and re-run the Task 1 unit test there. Expected: clean builds, and the tests pass.
- [ ] **Step 2: Ask Dan** before touching holmvm64. It runs `inc54.029_patch`, not 53.0. So this is only a smoke test of the code paths, run from a 53.0-built `liblan` only if Dan says that's valid. Otherwise skip to Step 4.
- [ ] **Step 3: Lab smoke, if approved.**
  1. With the flag off, restart cgrp 30485's lans. The banner shows `Active:0`, and RTs come up as before.
  2. Add `/usr/cnc/tables/<gne>.ping` (a full `lan.ping` copy plus `MGNE_ANYGNE=1`) for all 30485 GNEs and restart them. The banner shows `Active:1` and all RTs come up.
  3. Bounce one GNE. The other GNEs pick its RTs up within one or two poll cycles.
  4. Remove the files. Rollback is clean.
- [ ] **Step 4: Hand off to Dan.** Give him the production test procedure (spec §8) and ask for the patchbuild rebuild/restart list for the PR.

## Not in this plan (per spec)

- No mixed-group warning (spec §6.7: it can't be done without shared state, so it's dropped).
- No xlansim IITA support.
- No forward ports.
- No #4039 pacing changes.
