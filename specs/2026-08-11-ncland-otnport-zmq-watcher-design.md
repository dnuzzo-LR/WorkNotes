# Design: nclan_seed --watch — otn_portd → ncland es64/frame_link translator

Date: 2026-08-11
Branch: ncland-trace-macros
Author: dnuzzo (via brainstorm)

## Problem

When `OTN_PORTD_ZMQ_PUB=1` is enabled, `otn_portd` republishes every PostgreSQL
NOTIFY on the frame_link RDB tables as a ZMQ message on the niimxd nfdb bus.
These carry only identity keys (`<ne_id>` or `<ne_id>|<prot>|<type>|<slot>`) —
no new field values. We want `ncland` to react to changes in
`es64_snmp_info`-derived state so that:

- If the CLI IP address or port for an interface changes → drop the ncland
  connection and reconnect with the new params.
- If the interface is disabled → terminate the ncland connection and update
  the es64 link_status accordingly.
- If the interface is enabled → try to connect and update the link_status
  based on the connection outcome.

## Why not subscribe ncland directly to `db.otnport.*`

- Payloads are identity-only; ncland would need to link Postgres/rdb and
  duplicate the es64 c-tree reader that `nclan_seed` already contains.
- Keeps ncland's dependency surface small (existing project memory
  `ncland_lanalive_globals_unsafe` warns about pulling lanalive helpers into
  ncland).

## Approach — Diff-and-emit translator

Extend `nclan_seed` with a `--watch` resident mode that:

1. Subscribes to `db.otnport.channel_frmlnk_link_data_change` and
   `db.otnport.channel_frmlnk_ne_change` on the niimxd bus.
2. Maintains an in-memory cache `{neId, slot} → {enabled, ip, port, dtype, type}`
   primed on startup from the es64 c-tree (and any PG-only fields).
3. On each notify, reads the fresh row(s), diffs against the cache, and emits
   at most one specific event on the same bus:
   - `db.ne.enable`  when `enabled 0→1`
   - `db.ne.disable` when `enabled 1→0`
   - `db.ne.update`  when `enabled == 1` and `ip` or `port` changed
   - no emit when nothing meaningful changed (debounce)
4. Updates the cache with the fresh snapshot.

ncland subscribes to the same bus (already does), gains topic mappings for
`db.ne.enable` / `db.ne.disable`, does slot-aware close on disable, and writes
`snmpLinkStat` back to the es64 c-tree via
`upd_es64_snmp_info_change_snmpLinkStat()` after open/close.

## Architecture

```
Postgres (ne_link_data / ne_link_status / ne_snmp_link_data)
   │  AFTER-trigger pg_notify channel_frmlnk_*
   ▼
otn_portd  (OTN_PORTD_ZMQ_PUB=1)
   │  ZMQ PUB → ipc:///usr/cnc/data/nfdb_sub.sock
   ▼
niimxd XSUB/XPUB broker  ↔  ipc:///usr/cnc/data/nfdb_pub.sock
   │
   ▼
nclan_seed --watch (extended, resident)
  - startup: publish app.ne.seed for all NEs (existing)
  - prime:   walk es64_snmp_info → cache
  - SUB:     db.otnport.channel_frmlnk_link_data_change
             db.otnport.channel_frmlnk_ne_change
  - on evt:  fresh read → diff → emit db.ne.enable/disable/update
  - PUB:     connect ipc:///usr/cnc/data/nfdb_sub.sock
   │
   ▼
niimxd broker
   │
   ▼
ncland (existing SUB on app.ne. / db.ne.)
  - parse.cpp: map db.ne.enable → NE_ENABLE, db.ne.disable → NE_DISABLE
  - dispatch: slot-aware close for DISABLE
              call upd_es64_snmp_info_change_snmpLinkStat() after open/close
```

## Components

### nclan_seed (extended)

New CLI flag: `--watch` (default off; existing one-shot behavior preserved).

New source files under `cnc/ncland/src/`:
- `nclan_seed_watch.cpp` / `.h` — resident SUB drain, PUB emit, poll loop.
- `nclan_seed_cache.cpp` / `.h` — `{neId,slot}` snapshot cache: set / get /
  diff.

Extend `nclan_seed_fmt.cpp` with YAML flow formatters for:
- `db.ne.enable`  body: `{neid, dtype, slot, ip, port, ssh_flag, type}`
- `db.ne.disable` body: `{neid, slot, type}`
- `db.ne.update`  body: `{neid, dtype, slot, ip, port, ssh_flag, type}`
  (matches the existing seed body layout so ncland's flow parser is unchanged)

Reuses:
- Existing es64 c-tree reader (`open_es64_snmp_info`,
  `first_es64_snmp_info_neId_slot`, `next_es64_snmp_info_neId_slot`,
  `get_es64_snmp_info_neId_slot`).
- Existing PUB helpers from seed (endpoint / topic prefix / YAML flow).
- Where the `enabled` bit lives (es64.enabled vs a PG column on
  ne_link_data / ne_link_status) is TBD at plan time; watcher reads from
  whichever is authoritative.

### ncland (additive changes only)

- `ncland_notify_parse.cpp` — extend `kind_for()`:
  ```
  if (t == "db.ne.enable")  { *k = NCLAND_EVT_NE_ENABLE;  return true; }
  if (t == "db.ne.disable") { *k = NCLAND_EVT_NE_DISABLE; return true; }
  ```
- `ncland_notify.cpp` — add helper `ncland_find_conn_by_neid_slot()`; use
  it in the DISABLE path (disable is per-interface, not per-NE).
- `ncland_notify_dispatch()`:
  - After successful `try_open` → `upd_es64_snmp_info_change_snmpLinkStat`
    with `snmpLinkStat = <link-up>`.
  - After `try_close` on DISABLE → same helper with
    `snmpLinkStat = <link-down>`.
  - `type` = `DST_CLI` for ncland-driven writes.

### Dependency risk to verify at plan time

`upd_es64_snmp_info_change_snmpLinkStat` (alcatel_es64_db.c:6959) writes:
- es64_snmp_info c-tree
- PG ne_link_status via `rdb_ne_link_status_upd_link_status`
- frame_link c-tree via `set_slot_link_status`

It uses `TRACE()`. Must confirm TRACE + libutility deps don't drag in
lanalive globals (see project memory `ncland_lanalive_globals_unsafe`).
Fallback if unsafe: write only the c-tree portion inline in ncland; the
PG + frame_link cascade will be reconciled by the next SNMP poll from alm.

## Data flow

### Boot
1. Existing seed pass: emit `app.ne.seed` for all NEs (unchanged).
2. Cache prime pass: for each NE × es64 slot row, insert
   `{neId,slot} → {enabled, ip, port, dtype, type}` into cache.
3. Open SUB socket; subscribe to
   `db.otnport.channel_frmlnk_link_data_change` and
   `db.otnport.channel_frmlnk_ne_change`.
4. Open PUB socket; connect to `ipc:///usr/cnc/data/nfdb_sub.sock`.
5. Enter epoll loop on SUB ZMQ_FD.

### On `channel_frmlnk_link_data_change` — extra `<ne_id>|<prot>|<type>|<slot>`
```
parse extra → (neId, prot, type, slot)
row = get_es64_snmp_info_neId_slot(neId, slot)     # authoritative fresh read
new = { row.enabled, row.ipAddr, row.cliPort, frm_dtype(neId), row.type }
old = cache[neId, slot]                             # may be absent

first-match diff:
  if new.enabled == 0 && (old absent || old.enabled != 0):
      emit db.ne.disable { neid, slot, type }
  elif new.enabled == 1 && (old absent || old.enabled == 0):
      emit db.ne.enable  { neid, slot, dtype, ip, port, ssh_flag, type }
  elif new.enabled == 1 && (new.ip != old.ip || new.port != old.port):
      emit db.ne.update  { neid, slot, dtype, ip, port, ssh_flag, type }
  else:
      no emit

cache[neId, slot] = new
```

### On `channel_frmlnk_ne_change` — extra `<ne_id>`
NE-level change. Iterate all cached slots for `neId` and run the same diff.
- Cache empty for neId + es64 has rows → new NE: emit `db.ne.add` (or
  `app.ne.seed`, matching existing NE_CREATE path).
- Cache has entries + es64 has no rows → NE gone: emit `db.ne.delete`.

### ncland dispatch (small adds)
```
db.ne.enable  → NE_ENABLE  → try_open  → on success:
                              upd_es64_snmp_info_change_snmpLinkStat(up)
db.ne.disable → NE_DISABLE → try_close(slot-aware) →
                              upd_es64_snmp_info_change_snmpLinkStat(down)
db.ne.update  → NE_IP_CHANGE → try_close + try_open  (existing;
                              port change hits the same path)
```

### Ordering / duplicate handling
- Broker preserves within-PUB ordering; two rapid updates on one slot are
  processed in receive order, cache reflects final state.
- Startup race (event arrives mid-prime): cache-miss = "old absent";
  diff proceeds correctly on `new` alone (safe idempotent).
- Duplicate event: `try_open` idempotent via `find_conn_by_neid`;
  `try_close` on absent conn is no-op.

## Error handling

### Watcher
- SUB/PUB connect fail: log NF_D1, retry with backoff (1s → 5s → 30s cap).
  Never exit.
- es64 read fail on cache prime: log per-row, skip, continue. Partial cache
  degrades to always-emit — safe.
- es64 read fail on event: log, drop event. Next NOTIFY on same key re-drives.
- Payload malformed (missing `|`, non-numeric neId): log NF_D1, drop.
- PUB emit `zmq_send` EAGAIN (HWM hit): log, drop. PG will re-fire on real
  next change; storm is self-limiting.
- SIGTERM / SIGINT: flush pending PUB, close sockets, exit 0.
- SIGHUP: optional cache rebuild (defer).

### ncland
- Unknown topic before parse.cpp updated: existing code logs "dropped
  unparseable event" (`ncland_notify.cpp:138`) — safe until code lands.
- link_status writeback fail: log NF_D1 with neId/slot/reason. Do NOT roll
  back the CLI open/close — connection action is authoritative; writeback
  is best-effort. Next SNMP poll from alm reconciles.
- Missing es64 row at writeback: `upd_es64_snmp_info_change_snmpLinkStat`
  returns FAILURE via `incGetRecord`; log, skip.
- Slot-aware close on absent conn: existing `try_close` no-ops.
- Race: enable event before prior open finished — `try_open` idempotent via
  `find_conn_by_neid_slot`; second call returns 0.

### Left alone
- Broker restart: ZMQ auto-reconnect; events during outage may be missed —
  accepted; PG state is authoritative, next real change re-drives.
- `OTN_PORTD_ZMQ_PUB=0`: watcher gets no notifies. Optional 5-min silence
  WARN at boot; defer.
- Split-brain (two `nclan_seed --watch` instances): both emit duplicate
  events. Idempotent on ncland but wasteful. Guard with a pidfile /
  advisory lock at `/var/run/nclan_seed_watch.pid`.

## Testing

### Unit — nclan_seed_cache
- `cache_set` / `cache_get` roundtrip.
- Diff table driven directly (no ZMQ):
  - absent → enabled=1 → `db.ne.enable`
  - enabled=1 → enabled=0 → `db.ne.disable`
  - enabled=0 → enabled=1 → `db.ne.enable`
  - enabled=1 same ip/port → no emit
  - enabled=1 ip changed → `db.ne.update`
  - enabled=1 port changed → `db.ne.update`
  - enabled=1 both ip+port → single `db.ne.update`
  - enabled=0 → enabled=0 → no emit
  - cache-miss with new.enabled=0 → no emit

### Unit — nclan_seed_fmt (new formatters)
- `db.ne.enable` YAML flow contains neid, dtype, slot, ip, port, ssh_flag,
  type; parseable by `ncland_notify_parse`.
- `db.ne.disable` YAML flow contains neid, slot, type.
- Escaping: quotes/newlines in IP defensively escaped (mirror otn_portd).

### Unit — ncland parse / dispatch additions
- `ncland_notify_parse` recognises new topics → correct kinds.
- `ncland_find_conn_by_neid_slot()` returns the right id when multiple slots
  share a neId.
- Dispatch on NE_DISABLE with two slots of same neId closes only the matching
  slot.
- Use existing `g_test_open_calls` / `g_test_close_calls` seams.

### Integration — end-to-end on the bus
Fixture: niimxd broker + otn_portd stub emitting arbitrary `db.otnport.*`
messages + `nclan_seed --watch` + a fake ncland-sub that captures topic/body.

Cases:
- PG-shaped payload for an es64 row with `enabled 1→0` → capture
  `db.ne.disable`.
- ip change → capture `db.ne.update` with new ip.
- port change only → capture `db.ne.update`.
- Duplicate event → single emit (idempotence).
- NE-level event covering 3 slots, one changed → single `db.ne.update` for
  the changed slot.

### Manual smoke (documented, not automated)
- Bring up ncland + `nclan_seed --watch` on a lab GNE.
- Change `es64_snmp_info.snmpPort` via `snmpne` tool → verify ncland
  closes+reopens with new port.
- Disable a card via GUI → verify ncland closes CLI, link_status flips down
  in es64 c-tree.
- Enable a card → verify ncland opens CLI, link_status flips up.

### Not tested
- Broker restart recovery (relies on ZMQ auto-reconnect).
- Multi-instance watcher pidfile guard — manual only.

## Open items to resolve at plan time
- Which field authoritatively carries "enabled" for an interface —
  `es64_snmp_info.enabled` (c-tree) vs a PG column on ne_link_data /
  ne_link_status. Determines whether watcher's fresh read is c-tree, PG, or
  both.
- Confirm `upd_es64_snmp_info_change_snmpLinkStat` and its libutility TRACE
  do not pull lanalive-BSS-zero globals when linked into ncland. If they do,
  fall back to c-tree-only writeback from ncland.
- Endpoint / topic-prefix helpers from seed's existing PUB — verify they can
  be shared between seed pass and watch loop (single PUB socket or two).

## Out of scope (deferred)
- SIGHUP cache rebuild.
- 5-minute bus-silence warning.
- Automated broker-restart recovery tests.
- Rework of ncland's per-NE vs per-slot conn model beyond adding
  slot-aware find/close helpers.
