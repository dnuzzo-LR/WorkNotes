---
issue: 4864
repo: lightriversoftware/netflex
state: OPEN
status: Ready
sprint: Sprint 15
title: bep_fep_frm_wtr segfaults in bffw_count_up_ne (5.4.026 multi-box)
type: bug
release: 5.4.026
milestone: 5.4.0
author: jgomillion-LR
assignee: dnuzzo-LR
created: 2026-06-15
updated: 2026-06-19
url: https://github.com/lightriversoftware/netflex/issues/4864
---

# Issue 4864 — bep_fep_frm_wtr segfaults in bffw_count_up_ne (5.4.026 multi-box)

[View on GitHub](https://github.com/lightriversoftware/netflex/issues/4864)

## Summary

On netFLEX 5.4.026 multi-box installs (FEP + ≥1 BEP, ≥1k NEs), the BEP-side process `bep_fep_frm_wtr` deterministically segfaults a few seconds after every fresh spawn, on every BEP. The fault is a user-mode read (`error 4`) of unmapped memory at a constant instruction pointer `0x407a42`, inside `bffw_count_up_ne(int*)` in `cnc/md/src/l_bep_fep_frm_wtr.cpp`.

The crash dereferences `Frmlnk[ entry->field@0x14 ] .field@0x26` — i.e. `Frmlnk[ GNE_NODE ].rem_num[0]` — using an out-of-range index. The function takes a "linked-entry" indirection branch whenever `field@0x10` (`sagelnk[0]`) is negative, but never checks the NE type or bounds the index first.

Because the BEP-side writer dies right after connecting to the FEP frame socket, the FEP-side `fep_bep_frm_rdr` accepts the connection, immediately gets RST'd (`errno:104`, connection reset by peer), exits, sleeps 11 s, and retries forever. The result is a respawn storm on both sides: every FRM RDR socket shows DOWN in `nestat -B`, and NE state collapses across the install.

This is a **latent defect** — the `bffw_count_up_ne` function body and the `iFrmlnk` accessor are byte-identical between 5.2.132 (works) and 5.4.026 (crashes). What changed is the *data* reaching the loop: in 5.4.026 an SNMP-type NE now carries a negative `sagelnk[0]` and a non-index `sagelnk[1]` while being FEP-assigned, so the loop wrongly treats the overloaded SNMP field as a `Frmlnk` index.

## Reproduction Steps

On any 5.4.026 multi-box (FEP + ≥1 BEP, ≥1k NEs):

```bash
# On the BEP:
systemctl restart netFLEX-md         # any path that respawns bep_fep_frm_wtr
ps -eo pid,etime,cmd | grep bep_fep_frm_wtr | grep -v grep
# sleep 12
ps -eo pid,etime,cmd | grep bep_fep_frm_wtr | grep -v grep
# Different PIDs both times, uptime always <12s.

dmesg -T | grep bep_fep_frm_wtr | tail -5
# Repeated: bep_fep_frm_wtr[NNNN]: segfault at 7fXXXXXXXXbe6 ip 0000000000407a42 sp ... error 4
```

Crash signature is byte-identical across spawns and across BEPs: `ip 0x407a42` is constant, faulting address ends in `…be6`, matching `base + index*0x1b8 + 0x26` with a garbage `index`.

## Observed Behavior

Validated on holmvm17 + holmvm13/14/18/19 + linc12vm9 (2026-06-15):

- All 4 BEPs respawn `bep_fep_frm_wtr` every ~10–15 s and segfault every time; process uptime never exceeds ~12 s.
- Restart counts in `/usr/cnc/trace/md` since the 12:35 trigger:
  - BEP holmvm14: `bep_fep_frm_wtr` = **836 restarts** (the segfault); other BEP-side processes 2–4 (stable).
  - FEP holmvm17: `fep_bep_frm_rdr` = **4,862 restarts** (errno:104 loop driven by the BEP crash).
- FEP `nestat -B`: every FRM RDR socket DOWN (ports 7002, 7004, 7102, 7104).
- FEP `fep_bep_frm_rdr` trace loop: `STARTING → Set Socket Port 7004 UP → recv failed errno:104 → EXITING 1 → SLEEPING 11`.
- NE state on FEP: 3,495 DSBL+, 2,410 DSBL, 503 DOWN, **7 UP**.
- BEP traces show successful TCP connect, `PROCESS STATUS Socket Up`, `UPLOAD TUNABLES`, then the process vanishes — the segfault is not caught by the application logger.

### Pinpointed code (disassembly)

The faulting access is `((entry_t*)(Frmlnk_base + entry->field@0x14 * 440))->field@0x26`. The function takes the indirection path whenever `field@0x10` (`sagelnk[0]`) is negative; `field@0x14` (`sagelnk[1]` / `GNE_NODE`) holds the index. In a live gdb dump of the analogous FEP-side process, entry[0] had `field@0x10 = -1` and `field@0x14 = -1`, so it would read `Frmlnk_base - 0x1b8` — just before the array.

### Regression boundary

`bep_fep_frm_wtr` grew from 88,104 → 117,912 bytes (+33%) between 5.2.132 and 5.4.026, but `bffw_count_up_ne` (302 bytes), the `iFrmlnk` accessor (0x1b7 bytes), the `0x1b8` stride, and field offsets `0x10/0x14/0x20/0x26` are all identical. The regression is in what populates `Frmlnk[i]`, not in the crashing function itself.

## Resolution / Notes

**No fix merged yet** (issue is OPEN). An automated RCA (DRAFT/UNVERIFIED) was posted and the reporter confirmed it matches their independent investigation 1:1.

Automated RCA — netflex-workflow-bot, 2026-06-15:

> `bffw_count_up_ne()` dereferences `Frmlnk[ f->GNE_NODE ]` with **no type guard and no bounds check** … For SNMP NEs, `sagelnk[0]` holds an IP / server-ID / PM-collection-time and `sagelnk[1]` holds an unrelated server-ID / flag / timestamp — values that are routinely negative and are **not** a `Frmlnk` index. When such an NE is assigned to this FEP and `sagelnk[0] < 0`, `bffw_count_up_ne` wrongly takes the indirection branch and indexes `Frmlnk[ sagelnk[1] ]`.
>
> **This is a missing-guard bug, and the correct guard already exists 400 lines up in the same file.** `bffw_send_dacs_stat` (lines 721–736) guards the identical pattern with BOTH a type check and a bounds check. The same `!IS_SNMP(NE_TYPE(f,0)) && sagelnk[0] < 0` + `GNE_NODE < 0 || GNE_NODE > neavail` guard recurs in `l_bep_fep_msg_rdr.cpp:775` & `:1012` and `l_fep_bep_frm_rdr.cpp:635`. `bffw_count_up_ne` is the lone copy that omits both.

Recommended fix (from RCA) — in `cnc/md/src/l_bep_fep_frm_wtr.cpp`, `bffw_count_up_ne()`:

```c
-		if( f->sagelnk[0] < 0 )
-		{
-			rem = Frmlnk[ f->GNE_NODE ].rem_num[0];
-		}
-		else
-			rem = f->rem_num[0];
+		if( !IS_SNMP( NE_TYPE( f, 0 ) ) && f->sagelnk[0] < 0 )
+		{
+			if( f->GNE_NODE < 0 || f->GNE_NODE >= NeAvail )
+			{
+				TRACE( D0, "NE=%d, bad GNE_NODE=%d fepid:%d\n",
+				       loop, f->GNE_NODE, f->fepid );
+				continue;          /* skip this NE from the up-link count */
+			}
+			rem = Frmlnk[ f->GNE_NODE ].rem_num[0];
+		}
+		else
+			rem = f->rem_num[0];
```

> The `!IS_SNMP(...)` clause is the primary fix; the bounds check is defense-in-depth matching the siblings. Do **not** clear SNMP entries — for them a negative `sagelnk[0]` is normal data. Fix links into the standalone `bep_fep_frm_wtr` executable (no `.so` rebuild needed); ship the new binary to every BEP. Confidence: HIGH.

Reporter (jgomillion-LR), 2026-06-16:

> RCA matches our independent investigation 1:1 — the offsets (`0x10`/`0x14`/`0x26`), the `0x1b8` stride, and the `jns` safe-path branch all line up with the disassembly we captured on holmvm17 … **Heads-up on timing: we have a netFLEX load scheduled for Charter Lab on Friday 2026-06-19.** If a patched `bep_fep_frm_wtr` can be built and validated by then, that load would go in clean on a multi-box config. If not, we'll plan around the workaround (avoid restarting `netFLEX-md`, keep the 5.2.132 rollback warm). Happy to validate a candidate binary on holmvm17 lab.

**Workaround (operator-side only, not a fix):** roll the `/usr/cnc` symlink back to 5.2.132 on the FEP and every BEP, then `new_boot_nms` per the standard downgrade playbook. Verified in lab; `/usr4/inc5.2.132` is staged on every machine.

### Proposed fix — verified against live source (dnuzzo-LR, 2026-06-22)

RCA re-verified directly against `/git/netflex` `main` in `cnc/md/src/l_bep_fep_frm_wtr.cpp`. Confirmed 1:1:
- **Bug:** `bffw_count_up_ne` lines 1142–1147 index `Frmlnk[ f->GNE_NODE ]` behind only `if( f->sagelnk[0] < 0 )` — no type guard, no bounds check.
- **Correct pattern already in same file:** `bffw_send_dacs_stat` lines 721–736 guards the identical access with `!IS_SNMP( dcs_type ) && f->sagelnk[0] < 0` plus `f->GNE_NODE < 0 || f->GNE_NODE > neavail`.
- All macros (`IS_SNMP`, `NE_TYPE`, `GNE_NODE`, `NE_UNASSIGNED`) and `TRACEF` are already in scope in this TU (the sibling uses them) — fix compiles with no new includes.

Patch to apply in `bffw_count_up_ne()`:

```c
-		if( f->sagelnk[0] < 0 )
-		{
-			rem = Frmlnk[ f->GNE_NODE ].rem_num[0];
-		}
-		else
-			rem = f->rem_num[0];
+		if( !IS_SNMP( NE_TYPE( f, 0 ) ) && f->sagelnk[0] < 0 )
+		{
+			if( f->GNE_NODE < 0 || f->GNE_NODE > NeAvail )
+			{
+				TRACEF( D0, "NE=%d, bad GNE_NODE=%d fepid:%d\n",
+				        loop, f->GNE_NODE, f->fepid );
+				continue;        /* drop from up-link count, don't index Frmlnk */
+			}
+			rem = Frmlnk[ f->GNE_NODE ].rem_num[0];
+		}
+		else
+			rem = f->rem_num[0];
```

Three deliberate refinements vs. the RCA draft above:
1. `> NeAvail` + `TRACEF` (not `>= NeAvail` / `TRACE`) — match the proven sibling guard byte-for-byte; either bound is safe, consistency is lower-risk.
2. `continue` rather than `SET_FRMNUM( f, UNASSIGNED )` — `bffw_count_up_ne` is a read-only counter; it must not mutate shared NE state. (The sibling clears the NE because it is the authoritative status path.)
3. `!IS_SNMP(...)` is the actual fix (stops SNMP NEs taking the branch); the bounds check is defense-in-depth. Do **not** clear SNMP entries — a negative `sagelnk[0]` is normal data for them.

**Build / ship:** links into the standalone `bep_fep_frm_wtr` executable — no `.so` rebuild. Ship the new binary to every BEP (segfault is BEP-side); the FEP `fep_bep_frm_rdr` errno:104 respawn storm clears on its own once the BEP writer stops dying.

**Validation:** deploy to one BEP, `systemctl restart netFLEX-md`, confirm `bep_fep_frm_wtr` uptime > ~12 s and no new `dmesg` segfault, then watch NE state recover on the FEP.

**Timing flag:** Charter Lab load was scheduled Fri 2026-06-19 — that date has passed. Confirm with jgomillion-LR whether the patch made that window or they fell back to the 5.2.132 rollback, so the fix targets the right next load.

**Related / separate defect:** #4872 — `libmd.so` calls an unresolvable `md_exit` symbol on fresh spawn of the same writer processes. Different file and mechanism (dynamic-linker failure vs SIGSEGV); surfaces in the same respawn cascade but should be tracked separately — do not conflate.

## Attachments

None.

## Metadata

- **Issue:** [#4864](https://github.com/lightriversoftware/netflex/issues/4864)
- **Repo:** lightriversoftware/netflex
- **State:** OPEN
- **Status:** Ready
- **Sprint:** Sprint 15
- **Type:** bug
- **Labels:** bug, rca-complete
- **Software Release:** 5.4.026
- **Milestone:** 5.4.0
- **Author:** Jason Gomillion (jgomillion-LR)
- **Assignee:** Dan Nuzzo (dnuzzo-LR)
- **Created:** 2026-06-15
- **Updated:** 2026-06-19
- **Environment:** Rocky Linux 8.10, kernel 4.18.0-553.123.1.el8_10, glibc 2.28; FEP holmvm17, BEPs holmvm13/14/18/19, GR-standby FEP linc12vm9

## My Notes

* add gate check when checking the rem_num in frame_link
* be83ae17b39973a94d91f5301d445e26dbcc1caa
  