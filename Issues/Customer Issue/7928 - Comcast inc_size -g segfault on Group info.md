# 7928 — [SUPPORT] Comcast: inc_size command failing on the Group info portion

**State:** Open · **Milestone:** 5.4.1 · **Priority:** Time Permitting · **Host:** linc11vm15 (Rocky 8.10, 54.1 build 29)
**GitHub:** lightriversoftware/netflex#7928 · **Branch:** issue-7928-inc-size-failing

## Symptom
`inc_size -zndbg` works; adding `-g` prints `Group info:` then `Segmentation fault`, before any `Group N ...` line.

## Root Cause
`get_group_dtypes()` iterates `i <= MAX_NE_TYPES` (`cnc/tools/src/inc_size.c:262`), returning dtype 265, but `dtypes[]` has only 265 entries (0–264). `print_groups()` then does `strlen(dtypes[265])` (`inc_size.c:228`) — out-of-bounds read.

- `MAX_NE_TYPES` = 265 (`common/include/ne_defs.h:550`); `dtypes[]` in `include/dtype.ext` has 265 entries; `nm` on the binary confirms size `0x848` = 265 × 8.
- 64-bit RHEL 8 build (`/inc/inc54.1/inc54.128_corelinux64/3b2/bin/inc_size`): the 8 bytes after `dtypes[]` are alignment padding before `pss_dtypes` = `0x0` → `strlen(NULL)` → SIGSEGV. The crash happens in the column-width loop, before anything prints, which matches the issue output.
- `NeGroup[]` is `char[MAX_LIC_NE_TYPES]` (512), so slot 265 exists. Unused slots are `0` (= group index 0, "Group 1") on licenses from:
  - the V1→V2 migrator: memset 0, then memcpy only 256 slots (`cnc/license-migrate/src/migrators.c:376-380`)
  - `lic_create`: zero-filled global, never fills in a "no group" value (`cnc/tools/src/lic_create.c:125`)
  - Only `licbox` wipes to `-1` (`licbox.c:2472`).
- Library `set_group_cnts()` correctly uses `loop < MAX_NE_TYPES` (`cnc/utility/src/license.c:161`).
- Latent since the inc52.0 import, not a regression.

Related problems:
- The same loop can write `grp_dtypes[265]` (the closing `-1`) past a 265-slot stack array if a group matches all of dtypes 1–265.
- `cnc/tools/src/licbox.c:2459` has an identical `get_group_dtypes()` with the same `<=` bug.

Caveat: in the 32-bit rh8 build (`inc54.128_core_rh8`), `dtypes[265]` aliases `pss_dtypes[0]` (a valid pointer), so it would print a wrong row instead of crashing. The customer's x86_64 RPM matches the 64-bit build. Not reproduced live (no CNC up locally).

## Proposed Fix
**Files:** `cnc/tools/src/inc_size.c:262`, `cnc/tools/src/licbox.c:2459`

```c
-	for (int i = 1; i <= MAX_NE_TYPES; i++)
+	for (int i = 1; i < MAX_NE_TYPES; i++)
```

With `<`, a group matches at most 264 dtypes, so the closing `-1` lands at index 264. That fixes both the read and the write. Dtype 265 has no name in `dtypes[]`, so nothing valid is dropped, and this matches `set_group_cnts()`.

Optional, separate decision: have the migrator and `lic_create` fill `NeGroup` with `-1` before populating, so unused slots stop counting as group 1. This changes license contents.

**Risk:** very low.

**Verify:**
1. On a 64-bit RH8 sim with a migrated or `lic_create` license (`lice` shows `NeGroup[265] == 0`), run `inc_size -g` before and after the change.
2. Run it under valgrind: no invalid reads or writes in `print_groups`.
3. Run `licbox` and open its group screen.

## Not this bug (possible separate tickets)
- `JSON Counts` parse error: `invalid literal; last read: '/'`
- `WEIGHTED NE` Avail `-13872.25` against a license of `0.00`
