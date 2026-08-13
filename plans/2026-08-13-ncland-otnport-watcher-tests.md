# nclan_seed --watch — Unit Test Inventory

Date: 2026-08-13
Branch: `ncland-otnport-watcher` (tip `3b10dbbd7`)
Related:
- Design:    `~/WorkNotes/specs/2026-08-11-ncland-otnport-zmq-watcher-design.md`
- Plan:      `~/WorkNotes/plans/2026-08-13-ncland-otnport-watcher-plan.md`
- Followups: `~/WorkNotes/plans/2026-08-13-ncland-otnport-watcher-followups.md`

**36 new tests + 1 refreshed. Suite grew 193 → 229. 0 failed / 6 skipped / 0 xfail.**

Test framework: `TEST("group", "name") { REQUIRE(...); }` (see
`include/nfunit-test.hpp`). Run via `nmake ncland_unit_tests && ./ncland_unit_tests`
(with BASE / VPATH / L64_SETUP=1 exported).

---

## `cache` — NclanSeedCache (15)

File: `cnc/ncland/src/nclan_seed_cache_tests.cpp`

### Scaffolding (Task 1, 5 tests)
- `C1 empty on construction` — get returns false on empty
- `C2 set then get returns the row`
- `C3 set overwrites`
- `C4 erase`
- `C5 different slots distinct` — same neid, different slots stay separate

### Diff decision table (Task 2, 10 tests)
- `D1 absent + fresh disabled = NONE`
- `D2 absent + fresh enabled = ENABLE`
- `D3 enabled -> disabled = DISABLE`
- `D4 disabled -> enabled = ENABLE`
- `D5 enabled same = NONE`
- `D6 enabled ip change = UPDATE`
- `D7 enabled port change = UPDATE`
- `D8 enabled ip+port both change = single UPDATE`
- `D9 disabled unchanged = NONE`
- `D10 absent + fresh disabled (cache-miss ignored) = NONE`

## `seedread` — fresh-read composer (4)

File: `cnc/ncland/src/nclan_seed_read_tests.cpp`

Task 4 — all use injected fake pg/ctree functions (module-static +
captureless lambdas), no live DB required.
- `R1 combines pg enabled + ctree fields`
- `R2 enabled=0 propagates`
- `R3 pg failure returns -1`
- `R4 ctree failure returns -1`

## `watch` — handle_one dispatch (5)

File: `cnc/ncland/src/nclan_seed_watch_tests.cpp`

Task 9 — pure function with injected `read` and `emit` callbacks; a
`record_emit` in an anonymous namespace captures topic+body to a vector
for assertions.
- `W1 first-sight enabled emits db.ne.enable`
- `W2 no change = no emit`
- `W3 ip change emits db.ne.update`
- `W4 enabled -> disabled emits db.ne.disable`
- `W5 read failure returns -1, no emit, cache untouched`

## `seedfmt` — body formatters (3)

File: `cnc/ncland/src/nclan_seed_tests.cpp` (appended)

Task 3 — shape-only smoke; parser round-trip lives in `nparse`.
- `E1 format_ne_enable identical to seed body`
- `E2 format_ne_update identical to seed body`
- `E3 format_ne_disable is neid+slot only`

## `nparse` — parser topic mappings (3)

File: `cnc/ncland/src/ncland_notify_parse_tests.cpp` (appended)

Task 6 — new topics + tolerance for the minimal disable body.
- `P-enable maps to NE_ENABLE`
- `P-disable maps to NE_DISABLE, tolerates minimal body`
- `P-unknown-topic still returns -1`

## `notify` — dispatch + finder (6 new, 1 refreshed)

File: `cnc/ncland/src/ncland_unit_tests.cpp` (appended to T16 block)

### Refreshed (Task 0, existing assertion updated)
- `T16.1 init/close don't crash; close resets fd to -1` — was
  `fd is -1 in stub mode`; now asserts `notify_fd >= 0` after real ZMQ
  SUB init, `== -1` after close.

### Slot-aware finder (Task 7, 2 tests)
- `T16.slot-finder returns -1 when no matching slot`
- `T16.slot-finder returns id matching (neid, slot)` — 2 conns same
  neid, different slots

### Slot-aware DISABLE dispatch + writeback (Task 8, 4 tests)
- `T16.9 NE_DISABLE closes only the matching-slot conn`
- `T16.10 NE_DISABLE with no matching slot is a close no-op`
- `T16.11 NE_ENABLE on success bumps writeback counter`
- `T16.12 NE_DISABLE always bumps writeback counter`

Task 8 also added the test seam `g_notify_writeback_disabled` and
`g_test_writeback_calls`; a static initializer in `ncland_unit_tests.cpp`
disables real ES64 writeback for all tests (mirrors `g_warehouse_no_connect`).

---

## Test counts by task

| Task | New tests | Notes |
|------|-----------|-------|
| 0    | 0         | Refreshed T16.1 assertions (see above) |
| 1    | 5         | cache scaffolding C1-C5 |
| 2    | 10        | cache diff D1-D10 |
| 3    | 3         | seedfmt E1-E3 |
| 4    | 4         | seedread R1-R4 |
| 5    | 0         | Production PG/ctree readers — exercised in Task 12 smoke |
| 6    | 3         | nparse P-enable/disable/unknown-topic |
| 7    | 2         | notify T16.slot-finder x2 |
| 8    | 4         | notify T16.9-T16.12 + test seam |
| 9    | 5         | watch W1-W5 |
| 10   | 0         | Watch loop I/O — exercised in Task 12 smoke |
| 11   | 0         | CLI wiring — smoke check via `-h` |
| 12   | 0         | Manual smoke doc (`cnc/ncland/src/WATCH-SMOKE.md`) |
| **Total** | **36** | |

## Known coverage gaps

Not blockers; captured in
`~/WorkNotes/plans/2026-08-13-ncland-otnport-watcher-followups.md`.

- `parse_link_data_payload` / `parse_ne_payload` — static in
  `nclan_seed_watch.cpp`, currently only covered via the live watch
  loop. Followup #7.
- `NCLAND_EVT_NE_DTYPE_CHANGE` — pre-existing dispatch branch, no test.
  Followup #8.
- Production `nclan_seed_pg_enabled` / `nclan_seed_ctree_row` readers
  need a live DB + c-tree; exercised only via the manual smoke.
- `nclan_seed_watch_run` I/O loop needs a live ZMQ bus; exercised only
  via the manual smoke.

## Alternative artifact

If a copy shipped inside the branch alongside `WATCH-SMOKE.md` would be
easier to find at review time, copy this file to
`cnc/ncland/src/WATCH-TESTS.md` in the worktree and commit there.
