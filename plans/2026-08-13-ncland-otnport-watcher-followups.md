# nclan_seed --watch — Deferred + Follow-up Items

Date: 2026-08-13
Branch shipped: `ncland-otnport-watcher` (tip `3b10dbbd7`)
Related:
- Design: `~/WorkNotes/specs/2026-08-11-ncland-otnport-zmq-watcher-design.md`
- Plan:   `~/WorkNotes/plans/2026-08-13-ncland-otnport-watcher-plan.md`
- Smoke:  `cnc/ncland/src/WATCH-SMOKE.md` (in the branch)

The 12-task plan intentionally deferred some scope, and the final
holistic review flagged a small set of follow-ups. This doc consolidates
everything worth capturing so nothing gets lost when the branch merges.

## Deferred (called out in design + plan + code)

### 1. NE-level `channel_frmlnk_ne_change` per-slot re-diff
- **Where it stubs out today:** `cnc/ncland/src/nclan_seed_watch.cpp:199`
  — the `else if (t.find("ne_change") != std::string::npos)` branch
  parses the payload but does not iterate cached slots for the NE.
- **Why deferred:** link-level notifies on `channel_frmlnk_link_data_change`
  already fire per-slot whenever a real state change happens. NE-level
  events add cases the initial version doesn't need.
- **When it matters:** NE add / delete / bulk provisioning where a
  single NE-level notify may be the only signal that a whole set of
  slots changed. Also: initial provisioning of a new NE where the
  link-level trigger may not fire (depends on trigger scope).
- **Sketch:** on `ne_change`, iterate `cache` entries whose `neid`
  matches; call `handle_one(neid, slot)` per cached slot; add a special
  case for "NE gone" (cache had entries, PG has none) → emit
  `db.ne.delete`.

### 2. Boot-time cache prime from ES64 c-tree
- **Where it stubs out today:** `cnc/ncland/src/nclan_seed_watch.cpp:157`
  — `NclanSeedCache cache;` starts empty.
- **Why deferred:** first-notify per `(neid, slot)` sees the cache
  as absent; per the diff table an enabled row emits `db.ne.enable`
  (idempotent on ncland) and a disabled row emits nothing (silently
  primes the cache). Both are safe.
- **When it matters:** if boot-time false-enable events become noisy in
  the tail (many enabled interfaces re-emitting `db.ne.enable` on daemon
  restart).
- **Sketch:** call `first_es64_snmp_info_neId_slot` /
  `next_es64_snmp_info_neId_slot`, populate cache with PG-fresh enabled
  bit + c-tree ip/port/dtype before entering the poll loop.

### 3. Multi-instance pidfile guard
- **Where it lives today:** nowhere. If a second `nclan-seed -w` runs
  concurrently, both process every notify and emit duplicates.
- **Impact:** benign on ncland (parser + dispatch are idempotent) but
  wasteful, and pollutes the bus.
- **Sketch:** `flock` on `/var/run/nclan_seed_watch.pid` at startup;
  exit 0 if another instance already holds the lock (log so operators
  aren't confused).

### 4. SIGHUP cache rebuild
- Not needed for correctness (next real notify re-drives), but a
  useful operator escape hatch if the cache is suspected of drift.

### 5. 5-minute bus-silence warning at boot
- If `OTN_PORTD_ZMQ_PUB=0` in `otn_portd`'s environment, the watcher
  subscribes but never receives anything. Log a WARN after 5 minutes of
  silence at startup so operators notice the misconfiguration.

### 6. Broker restart recovery tests
- ZMQ auto-reconnects when `niimxd` restarts, and events during the
  outage may be missed. Accepted — PG state is authoritative and the
  next real change re-drives. No automated test.

## Final-review follow-ups (from `superpowers:code-reviewer` on
`ncland-otnport-watcher` tip)

### 7. Isolated unit tests for `parse_link_data_payload` and `parse_ne_payload`
- Both live in `cnc/ncland/src/nclan_seed_watch.cpp` as static helpers
  and are covered only via the full watch loop (requires live ZMQ).
- Both are trivially testable — string in, `NotifyKey` out.
- Add tests to `cnc/ncland/src/nclan_seed_watch_tests.cpp`: valid
  payload, missing separator, non-numeric, empty, extra tokens, etc.
- Blocker to add: `parse_link_data_payload` / `parse_ne_payload` are
  `static` in the .cpp; either expose via the header or move the tests
  into the same TU.

### 8. `NCLAND_EVT_NE_DTYPE_CHANGE` dispatch has no test coverage
- Pre-dates this branch (untouched here); noted as a coverage gap in the
  final review. If the dispatch behavior is ever changed, add T16.x
  tests covering the was-open/now-allowed matrix in `ncland_notify.cpp`.

### 9. `KeyHash` shift-by-32 assumes 64-bit `size_t`
- `cnc/ncland/src/nclan_seed_cache.h:53` computes
  `(static_cast<size_t>(k.neid) << 32) ^ ...`. UB on a 32-bit `size_t`.
- Project builds 64-bit only (`L64_SETUP=1`) so this is fine in practice.
- Defensive fix if we ever consider 32-bit builds: cast through
  `uint64_t` and back to `size_t`.

## Not follow-ups (already fixed on this branch)

- ~~T16.1 stale stub assertions~~ (Task 0)
- ~~`get_es64_snmp_info_neId_slot` signature + return code + missing
  `type` in ISAM key~~ (Task 5 fix commit `1cb162d42`)
- ~~zmq_recv truncation past stack buffer~~ (Task 10 fix `ab92c969e`)
- ~~NclanSeedDiff enum value collision with c-tree macros~~ (Task 11
  cleanup `7eb4db70c`)
- ~~`--watch` silently dropping every event when es64 open fails~~
  (final followup `3b10dbbd7`)
- ~~stale "Implemented in Task 10" comment on
  `nclan_seed_watch_run`~~ (final followup `3b10dbbd7`)
- ~~`body_disable` ssh_flag inconsistency~~ (final followup `3b10dbbd7`)

## When to tackle these

None are blockers for merging the base feature. Reasonable ordering if
picked up as a follow-on branch:

1. **NE-level notify** (#1) — completes the "watcher covers every
   trigger" property; likely first user-visible gap.
2. **Cache prime** (#2) — reduce restart noise once #1 is in.
3. **Parser unit tests** (#7) — small, low-risk, closes a coverage gap.
4. **Bus-silence WARN** (#5) — operational polish.
5. **Pidfile guard** (#3) — cheap safety net.
6. **SIGHUP rebuild** (#4) — once #2 exists, this becomes trivial.
7. Everything else — leave until a real trigger.
