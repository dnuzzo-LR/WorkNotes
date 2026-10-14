# #8205 — MGNE "any GNE, fastest wins" RT login (lanalive) — Design

- **Issue:** lightriversoftware/netflex#8205 (Lumen, 5.3.0 build 34 = `inc53.034`)
- **Target branch:** `inc53.0` (working branch `issue-8205-53.0`)
- **Files expected to change:** `cnc/sdi/src/lanalive.c`, `cnc/sdi/src/comm_tunable.c`
  **and its mirror `comm_tunable_s.c`**, `include/comm_tunable.h`, and the startup banner
  in `cnc/sdi/src/lan.c` **and `lan_s.c`**. `lan.small` links `-llan -lsdi_s`, so every
  new extern that `lanalive.o` references must also be defined in `comm_tunable_s.c`,
  the same way `GneRtLoginGap` is mirrored there.
- **Date:** 2026-10-14

## 1. Intent and why

In a multi-GNE comm group (`MULITGNE:1`), RTs never come back up after GNEs bounce.
This was confirmed on Lumen production, comm group 30485, using D7 traces from
2026-10-14 18:00–18:26: all 33 RTs were DOWN after an nereset.

The root cause is a cross-process starvation loop in `lanalive.c`. Every GNE's lan
process shares the RT frame (`Frm[]`) in shared memory:

1. A RT goes down. A lan whose GNE is **not** the RT's `GNE_NODE` (a "foreign" lan)
   sends ACT-USER through its own GNE. It sets `LAN_LOGGING_IN` and the access time.
2. The foreign GNE can't reach that TID and answers `DENY IITA`. Or the GNE is out
   of resources and answers `DENY SARB "ALL RESOURCES BUSY"`. Neither reply ends the
   attempt:
   - The IITA/PLNA branch at the top of `send_to_screen()` (~2142–2170) logs it. For
     quote-TID dtypes it also sets `SET_QUOTE_TID` + `SET_MODIFIED`; otherwise it calls
     `set_auto_disable_ne`. It never clears `LAN_LOGGING_IN`.
   - SARB carries the GNE's TID, so it isn't recognized as a reply to the RT login.
   - An RT-originated deny (`RECEIVED LOGIN DENIAL RESPONSE`, ~3876–3890) sets ACC_TIME
     to 0, so the next pass goes through the timeout path below. That path also calls
     `NE_MODIFIED`.

   `LAN_LOGGING_IN` stays set.
3. The **owner** lan (where `GNE_NODE == gne`) sees `LAN_LOGGING_IN` and waits out
   `WaitForLogin`. It then logs `LOGIN TIMEOUT`, sends CANC-USER through **its own**
   GNE (the RT answers PLNA), sets `LAN_INIT` and calls
   `NE_MODIFIED(fptr, SET_LINK_PRIV)` (~line 1190–1235).
4. A foreign lan sees `ne_modified` on a DOWN RT and jumps straight to
   `login_time` (~line 1097). Back to step 1. The period is about 2 poll cycles (~44s).

The evidence: 541 RT logins across 6 GNEs, and only **1** went to the sending GNE's
own RT. Owners only time out and log out. 34375, 34560 and 35052 answer PLNA through
their owner 33465, so they are reachable there but never logged in to. GNEs 33091,
33102, 33214 and 33244 answer most logins and logouts with SARB.

**Dan's chosen direction:** any GNE in the group may grab a down RT, and the first
good login wins. The fix is to make that work correctly. Owner priority is not part
of it.

## 2. Goals

1. A down RT in a multi-GNE group comes up through **whichever GNE can reach it, as
   fast as possible**.
2. A failed login attempt ends **immediately** when its DENY arrives, instead of
   lingering until another lan's timeout.
3. Only the lan that **sent** a login times it out and logs it out, through its own GNE.
4. A GNE that failed a given RT backs off **that RT**. A GNE answering **SARB** backs off
   **all** RT logins.
5. A deny or timeout no longer sets `ne_modified`, so other lans don't all pile in at
   once. They pick the RT up on their normal pass.
6. The behaviour is gated by a `lan.ping` setting that is **off by default**. With it
   off, lanalive behaves exactly as `inc53.0` does today.

## 3. Non-goals

- Owner-first or "sticky" ownership. Explicitly rejected in favour of fastest wins.
- Rebalancing RTs across GNEs once they're up.
- Changes to xlansim or a lab reproduction. Dan's call: test in production.
- The #4039 GNERTLOGINGAP pacing fix (`432d83511`, separate branch). It stays separate
  and must keep working unchanged.
- The SARB root cause (GNE gateway session exhaustion). That's for Lumen and support.
- Single-GNE and dual-GNE behaviour.
- Forward-porting to `inc53.1`, `inc54.x` or `main`. That's later. **Note for the
  port:** `inc54.0+` has #7845 (`iita_request_neighbor_adp`, an IITA on a `ZG` login
  starts ADP on the neighbors). In this mode a foreign GNE's IITA is expected and
  must not trigger ADP. Reconcile that when porting.
- Picking up the post-`inc53.034` fixes already on `inc53.0` (#7330, #7691, #8107).
  They ship as they are.

## 4. Anti-goals (failure even if it "works")

- **Any behaviour change with the setting off.** Every new branch must be gated on the
  new flag (plus `MultiGNE`).
- **A change to the `struct frame_link` / `Frm[]` shared-memory layout**, or to anything
  md/frm readers sync. All new state is **process-local** in the lan. Per memory note
  `project_sdi_one_gne_per_process`, file-static per-GNE state in `cnc/sdi` is safe
  because each GNE has its own lan process.
- **RTs stuck forever in `LAN_LOGGING_IN`** if the sending lan dies mid-login (see §6.6).
- **Hammering SARB GNEs harder than today.**
- **Breaking the user enable/disable flow.** Real `ne_modified` from the database or GUI
  (disable/enable/modify) must still be honoured exactly as today.
- **Code that won't compile with gcc 4.8.5 (C89/gnu89).** No declarations after
  statements in old-style blocks unless the file already does it, and no newer libc.

## 5. Constraints

- **Toolchain.** RHEL 7/8/9; gcc 4.8.5 is the baseline. Build with Lucent nmake.
  `BASE`/`VPATH` must point at the repo.
- **Comment style.** New functions, structs and statics get Doxygen comments, per
  Dan's CLAUDE.md. Match the surrounding code's style (tabs, K&R braces). Use the
  `TRACE(Dn, ...)` levels already in this area: D0 for state changes worth seeing in
  production, D2/D3 for per-pass decisions.
- **Naming.** New tunables and statics share one prefix. My call: `MgneAnyGne*` for
  variables and `MGNE_ANYGNE*` for `lan.ping` keys.
- **`lan.ping` parsing.** `load_comm_tunes()` in `comm_tunable.c` dispatches on the
  key's **first character** (`else if (*buf == 'G')` …). The new keys must go into the
  right branch, which is `'M'` for `MGNE_*`. Check how the existing `'M'` keys
  (`MAX_RT_PER_GNE`, `MAC_SPEC`, …) are matched so they don't shadow the new ones.
- **Per-GNE `.ping` override.** `lan.c` (~484) uses `/usr/cnc/tables/<neid>.ping` instead
  of `lan.ping` when that file exists. It replaces `lan.ping` entirely; it doesn't add to
  it. This is how the change gets switched on for a limited set of NEs.

## 6. Design (the decided parts)

### 6.1 Gate

The new `lan.ping` keys, parsed in `load_comm_tunes()` and declared like
`GneRtLoginGap` (globals in `comm_tunable.c`, limits in `include/comm_tunable.h`).
The names and defaults are my calls; values are clamped and out-of-range values
traced at D0, like `GNERTLOGINGAP`:

| Key | Meaning | Default | Clamp |
|---|---|---|---|
| `MGNE_ANYGNE=0/1` | Master switch | 0 (off) | 0/1 |
| `MGNE_RT_BACKOFF=<min>/<max>` | Per-RT backoff, in seconds, after this GNE fails that RT | `PollCycle` / 300 | min ≥ 1, max ≥ min, max ≤ 3600 |
| `MGNE_SARB_BACKOFF=<min>/<max>` | GNE-wide backoff after SARB | 60 / 300 | same |

The feature is active only when `MultiGNE && MgneAnyGne`. `MultiGNE` is set at
`lan.c:637`, after `load_comm_tunes()` (~511), and `PollCycle` is also only final
after parsing. So the "active" decision and the `PollCycle` default for
`MGNE_RT_BACKOFF` must be resolved after both, not inside the parser. Add `MGNE_ANYGNE` and
the backoff values to the startup banner in `lan.c` (next to `MULITGNE:` ~647) so
traces show whether it's on.

### 6.2 Process-local state (new, file-static in `lanalive.c`)

- **Pending-login table**, indexed by neid and sized `NeAvail` (calloc on first use, the
  same pattern as `IitaLastSent` in `inc54.0`): the time this lan sent ACT-USER for
  that RT, or 0. Set wherever this lan's `lan_login(0, rt)` succeeds in sending.
  Cleared on COMPLD, on a deny, on this lan's own timeout, and when the RT is seen UP
  or disabled.
- **Per-RT backoff table**, indexed by neid: `next_try` time and current interval.
  - On a failure (deny or timeout), the interval starts at `min` and then doubles up
    to `max`, and `next_try = now + interval`.
  - Reset to 0 when this lan gets a COMPLD for that RT, or when the RT is seen UP
    through any GNE.
- **GNE SARB backoff:** one `next_try` / interval pair for this lan's GNE.
  - Set on any `DENY SARB` that comes from **this GNE's TID** for an RT login (`ZG`) or
    logout (`ZT`).
  - Reset on any COMPLD to an RT login through this GNE.
  - While it's active, this lan sends **no RT ACT-USERs**. GNE pings, GNE keep-alive
    and traffic to RTs already UP carry on.

### 6.3 Ending an attempt on a deny

This happens in the **sending lan**, which is always the one that gets the reply,
because each lan has its own GNE connection.

When a reply carries a login ctag (`ZG<neid36>`) and `DENY`, and the feature is on:

1. Decode the RT neid from the ctag (`a36l_ne(lptr + 2)`, as the existing BAD TID
   branch does).
2. Act only if this lan has a pending login for that neid; otherwise ignore it as today.
3. Clear `LAN_LOGGING_IN` on the RT, clear this lan's pending entry, and leave the link
   down. Do **not** call `NE_MODIFIED` / `SET_MODIFIED(…,1)`. Do **not** send CANC-USER;
   there's no session to cancel.
4. Classify the deny:
   - **From the GNE's TID with SARB:** GNE-wide backoff (§6.2). Also apply per-RT
     backoff at `min`, so this lan doesn't retry the same RT first when the GNE backoff
     ends.
   - **From the GNE's TID with any other code (IITA, …):** per-RT backoff.
   - **From the RT's TID (credentials or other, e.g. IIFM/PLNA):** per-RT backoff. Any
     other existing handling of RT-originated denials (`RECEIVED LOGIN DENIAL RESPONSE`
     path ~3876: `login_trys` parity logout, `Login_trys` / auto-disable) keeps its
     current meaning. The builder must make sure the RT ends up not `LAN_LOGGING_IN` in
     this mode.
5. Trace each ended attempt at D0 in one greppable line, e.g.
   `MGNE: NE:<rt> login via GNE:<gne> DENY <code> from <tid> -> rt backoff <n>s [gne backoff <m>s]`.

This has to cover **SARB**, which today never reaches the IITA/PLNA branch at the top
of `send_to_screen()` because the reply's TID is the GNE's, so it falls through to GNE
processing ("Sending to Srcn/Alrmhndlr Msg"). The `ZG`-ctag check has to run before
that routing. (The `#ifdef SARB_NOT_LOGGED_IN` block at ~3820 is dead code; nothing
defines that macro.)

**Side effects of the existing IITA branch (my calls).** In this mode, IITA from a
foreign GNE is routine, so the new check runs **before** the existing branch, and for
a deny on a login (`ZG`) from **this GNE's TID** it skips:
- `set_auto_disable_ne`. A GNE that can't reach the RT says nothing about the RT.
- The quote-TID retry (`SET_QUOTE_TID` + `SET_MODIFIED`). That would quote the TID for
  every GNE and set `ne_modified`, breaking goal 5. Quote-needing dtypes (Nortel
  3x00/6500, Ciena 61xx/Z) still get the retry the first time **the RT itself** denies,
  or when the feature is off. Lumen's group 30485 is dtype 51 (`GENERIC_TL1`), which
  isn't a quote dtype, so this doesn't affect the test.

The `ZT` (logout) denies and the feature-off path go through the existing branch
unchanged.

**Login try counting (my call).** A deny that comes from a GNE (IITA, SARB) means the
RT never saw the login. In this mode it undoes this lan's `fptr->login_trys++` for that
attempt (never below 0), so GNE-level failures don't count toward `Login_trys` /
auto-disable or the `login_trys & 1` parity logout. RT-originated denies and timeouts
still count as they do today. Lumen runs `LOGIN TRYS:0` and `AutoDisable:0`, so this is
belt-and-braces for other sites.

### 6.4 Only the sender times out

This is in the RT loop (~1190, `else if (GET_STATUS_PTR(fptr) & LAN_LOGGING_IN)`).
With the feature on:

- **If this lan has a pending login for the RT:** today's timeout logic, against its own
  send time. Log `LOGIN TIMEOUT`, send CANC-USER through this lan's GNE (that's where
  the session would be), set the link down and `LAN_INIT`, clear the pending entry, and
  apply per-RT backoff. **Skip `NE_MODIFIED(fptr, SET_LINK_PRIV)`.** The builder must
  confirm nothing else relies on that call in this path (`nupload` / `link_priv_set`
  side effects).
- **If this lan has no pending login for the RT** (another GNE's attempt): do nothing.
  Unlock and continue, unless it's stale (§6.6).
- The `GNE_NODE != gne` branch (~964–1000) already skips RTs that are `LAN_LOGGING_IN`.
  Keep that. The owner branch (`GNE_NODE == gne`) must now skip foreign attempts too.

### 6.5 Who may try a down RT, and when

- Any lan may attempt a down, enabled, not-`LAN_LOGGING_IN` RT on its normal pass. That
  is the existing path, both owner and foreign.
- **Before calling `lan_login(0, rt)`** at `login_time` (~1734), check in this order and
  skip (`goto test_cnt`) when either applies:
  1. the GNE SARB backoff is active, or
  2. this RT's backoff hasn't expired.

  Put the check at the call site so the normal path and the `ne_modified → goto
  login_time` path both pass through it. Do it **before** `login_trys++` (the same
  reason as the GNERTLOGINGAP comment there), so skipped passes don't count toward
  `Login_trys` / auto-disable.
- **Don't claim ownership before a successful login.** Lines ~998–999
  (`if (!fptr->GNE_NODE) fptr->GNE_NODE = gne;`) set `GNE_NODE` before any login. In this
  mode, ownership is set only by the existing success paths (`process_good_login()`
  ~10571, ~8079 → `GNE_NODE = Dacsid` plus `tell_master2("multi_gne=…")`). The builder
  must check what reads `GNE_NODE == 0` on a down RT:
  - `set_multi_num_rt` / `MaxRtPerGne` counting (see the UP-only rule below)
  - `lan_logout` on exit (~8525, which skips RTs not owned by the exiting GNE; correct)
  - (~1815 is under `#ifdef WTF`; dead code, ignore it)

  If leaving it 0 is unsafe somewhere, keep the pre-claim and document why.
- **`MaxRtPerGne` counts only UP RTs in this mode (my call).** `GNE_NODE` isn't cleared
  when an RT goes down, and #5334 preserves it on the BEP. So `set_multi_num_rt()`
  (~10441), which counts every RT with `GNE_NODE == gne`, would count a GNE's stale,
  down RTs. With Lumen's `MAX_RT_PER_GNE=30` and 33 RTs, a former big owner could hit
  `add_rt <= 0` and be blocked from foreign RTs that only it can reach. With the feature
  on, count only RTs that are UP. Don't clear `GNE_NODE` on down, so there's no md/BEP
  sync change. A stale owner simply enters its old RTs through the owner branch as one
  more GNE trying.
- **No rebalancing.** Once an RT is UP through some GNE, other lans skip it (the existing
  "Skipping … Link UP" branch).
- **Real `ne_modified` vs backoff (my call).** In this mode lan itself no longer sets
  `ne_modified` on a deny or timeout, so a `GET_MODIFIED` on an enabled, down RT means
  a database or GUI change (enable, credentials, TID). It **clears this lan's per-RT
  backoff** for that RT, so a user fix is retried right away. The GNE SARB backoff
  still applies, because the GNE can't serve anyone. Disable handling is unchanged.

### 6.6 Stale foreign attempt guard

A lan can die (SIGTERM, GNE disable, crash) with its RT logins still pending.
`LAN_LOGGING_IN` would then stay set on those RTs forever, because no lan's pending
table owns them.

My call: with the feature on, if an RT is `LAN_LOGGING_IN`, this lan has no pending
entry for it, and its access time is older than `3 × WaitForLogin`, this lan clears
`LAN_LOGGING_IN` (leaving the link down) and traces at D0
`MGNE: NE:<rt> stale login by another GNE cleared`. The RT then follows the normal path
on a later pass. The multiplier is a builder detail, but it must comfortably exceed one
`WaitForLogin` plus a poll cycle.

The builder should also check whether `exit_lan` → `lan_logout` already clears
`LAN_LOGGING_IN` for this lan's pending RTs. If it doesn't, and it's cheap and safe to
do it only for RTs in this lan's pending table, do that too. The guard stays either
way, because of crashes.

### 6.7 Mixed group (some GNEs on, some off)

This is an operational requirement: **every GNE in the comm group must have the setting
on together.** A GNE on the old code still times out foreign logins and pounces on
`ne_modified`, which keeps the loop alive.

My call: lan warns at startup (D0) when it can tell, cheaply and **without** a
shared-memory layout change, that another GNE in its group is running without the
feature. If that isn't possible, drop the warning and rely on the test procedure (§8).
Don't add shared state to support it.

## 7. Left to the builder

- Exact insertion points and helper function boundaries. For example, one
  `mgne_anygne_*` helper set for: pending-table operations, backoff check/apply, and
  deny classification.
- How to detect "reply TID == this GNE's TID" robustly. Compare against
  `GET_TID(GNE_Frm)` / `Frm[Dacsid]` with the same quote and case handling as the
  existing code (`NeTidToCaps`, `TID_USES_QUOTES`).
- Whether `ZT` (logout) SARB from this GNE should also feed the GNE backoff. My call:
  yes, since it shows the same resource exhaustion.
- Trace wording and levels beyond the D0 lines named above.
- Unit-level tests: lanalive has no harness. Any small test seam is welcome but not
  required. Validation is the production test in §8, plus a build on all three RHEL
  gcc versions.

## 8. Validation (production, limited NE set)

Dan's call: test in production on comm group 30485 only.

1. Install the patched build. **The rebuild/restart list is to be confirmed by Dan at PR
   time.** The reviewer's read of the makefiles is at least `liblan.so`, `libsdi.so`
   (comm_tunable), `lan` and `lan.small`, because of the `_s` mirrors. Restart: `lan`.
2. For **every** GNE in 30485 (33091, 33098, 33102, 33203, 33214, 33244, 33381,
   33465), create `/usr/cnc/tables/<neid>.ping` as a full copy of `lan.ping` plus
   `MGNE_ANYGNE=1`, then restart those lans together.
3. Check that the startup banner shows `MGNE_ANYGNE:1` on all of them and `0` on every
   other GNE.
4. Watch `nestat -G 30485` and the lan traces at D3 or higher. Success means:
   - Down RTs that are reachable (34375, 34560, 35052 through 33465; 33203's RTs) come
     up within a few poll cycles through whichever GNE reaches them.
   - No `LOGIN TIMEOUT` for an RT unless the same lan's trace shows its own
     `SENT LOGIN` for it.
   - `MGNE: … DENY IITA` lines followed by per-RT backoff, with no repeat attempts by
     the same GNE inside the backoff window.
   - SARB GNEs (33091, 33102, 33214, 33244 at the time of the traces) show GNE backoff
     and send far fewer ACT-USERs than today (about 10–17 SARB per minute each).
   - 34414: its owner 33465 gives IITA. It comes up through another GNE if one can reach
     it, otherwise it stays down and backed off without churn.
5. **Rollback:** delete the per-GNE `.ping` files and restart those lans.

## 9. Open items for Dan

- The rebuild products and processes to restart for the PR's patchbuild notes.
- The SARB root cause on the Lumen GNEs (gateway session usage on BTCKMIIKO16030205A,
  CHCGILMQO31070043A, KNWDMIBTO13010905A, LSVMKYPLO20050107A). That's for support and
  Lumen, outside this change.
