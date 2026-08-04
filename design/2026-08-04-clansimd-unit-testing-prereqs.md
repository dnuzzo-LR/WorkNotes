# clansimd unit testing — prerequisites analysis

**Date:** 2026-08-04
**Branch:** `ncland-pss-msg-cleanup` (netflex repo)
**Goal:** Be able to run unit tests against `clansimd` (standalone Lua NE-simulator
daemon). Two prerequisites identified.

---

## Prereq #1 — clansim.lua dynamic sim-files path  ✅ DONE

### Problem
`clansim.read_file` (the simulator's canned-response lookup) resolves the root of
the simulator file tree like this:

```lua
local simdata = inc.sysdef('SIMDATA=','/usr/cnc/data')
local root = simdata .. '/lansim'
if isdir('/usr/cnc/lansim') then
    root = '/usr/cnc/lansim'      -- clobbers injected root
end
```

`clansimd` already injects its per-instance `sim_data_root` via the
`inc.sysdef('SIMDATA=')` shim (`csim_luabridge.cpp:47`), **but** the
`/usr/cnc/lansim` autodetect overrides it whenever that dir exists — so on any box
with a production install the injected/test path is thrown away. That blocks
pointing the simulator at a test fixture tree.

### Fix (applied)
`CLANSIM_SIM_ROOT` env var, when set, overrides the **SIMDATA system-define value**
(not the whole resolution — the `/lansim` stem is still appended and the
`/usr/cnc/lansim` autodetect still applies):

```lua
-- CLANSIM_SIM_ROOT overrides the SIMDATA system-define value (embedder/test);
-- '/lansim' still appended, /usr/cnc/lansim autodetect still applies.
local simdata = (os.getenv and os.getenv('CLANSIM_SIM_ROOT'))
    or inc.sysdef('SIMDATA=','/usr/cnc/data')
local root = simdata .. '/lansim'
...
if isdir('/usr/cnc/lansim') then root = '/usr/cnc/lansim' end
```

- **Non-breaking:** env unset → identical to before.
- **Path scheme unchanged:** `CLANSIM_SIM_ROOT=<sandbox>` →
  `<sandbox>/lansim<zneid>/<tid>/CLI/<cmd>`, matching the existing test copy
  target `sim_data_root + "/lansim000007"` (`csim_daemon_tests.cpp:178`).
- **Applied to BOTH copies** (kept byte-identical):
  - `3b2/data/clansim.lua`
  - `cnc/ncland/src/test/fixtures/clansim.lua`  (the copy tests load via
    `d.cfg.lua_lib_dir = "test/fixtures"`)
- `os.getenv` verified available: the bridge calls `luaL_openlibs`
  (`csim_luabridge.cpp:158`) then only overrides `os.exit`.

### Open caveat
The `/usr/cnc/lansim` autodetect still runs **after** the env override, so on a box
where that dir exists it still clobbers `root`. Fine if the test host has no
`/usr/cnc/lansim`. If a test needs the env path to win even there, gate that
autodetect on `CLANSIM_SIM_ROOT` being unset. (Deferred — chosen semantics were
"override SIMDATA, not replace the resolution.")

---

## Prereq #2 — start an interface with NO frame_link entry  ⏳ DESIGN (unresolved)

### Why it's needed
To drive a live `ncland → clansimd` session in a test, ncland must open a
connection to a **simulated, unprovisioned NE**. Today an NE open depends on the
`Frmlnk` (frame_link) provisioning shared segment in several places, so an NE that
was never provisioned cannot be started.

`clansimd` itself starts sims fine without frame_link (its `do_start` resolves by
neid/dtype/script only — `csim_daemon.cpp:40`). The coupling is entirely on the
**ncland** side (`ncland.cpp:203/280` do `Frmlnk = atch_frm(&start)`).

### frame_link coupling surface — 3 sites (confirmed complete with Dan)
All three are gated today only by `c->neid > 0`:

| Dir | Site | Touches |
|-----|------|---------|
| READ  | `ncland_warehouse.cpp:504` (credential block, ~480–527) | `Frmlnk[neid].login / .nepasswd_cur` (fallback when es64 user empty) |
| WRITE (up)   | `ncland_lua.cpp:690` on login success | `setFrameLinkStatus(neid,1)` + `snmpSetLinkStatus(...,1)` |
| WRITE (down) | `ncland_warehouse.cpp:219` in `warehouse_drop_conn` | `setFrameLinkStatus(neid,0)` + `snmpSetLinkStatus(...,0)` |

Notes:
- `setFrameLinkStatus(neid, up)` indexes `Frmlnk[neid]` — garbage / out-of-range
  for an unprovisioned neid.
- `snmpSetLinkStatus(...)` writes the ES64 snmp_info c-tree DB — may not be open in
  a test.
- Credential block already tolerates *missing es64 record* (warns, "connecting
  without"), but the `Frmlnk` fallback path and the two writes are the hazard.

### Existing scaffolding (half-built, not wired)
- `ncland_parse_open_msg()` — parses an OPEN body `ip:port:user:pass:dtype`.
  **Built + unit-tested** (`ncland_proto.cpp:87`, tests `ncland_unit_tests.cpp:129`).
- Per `ncland.h:398–403`, it is explicitly **not yet wired**: *"once
  `warehouse_handle_dacs_msg` learns to route CMDs vs OPENs by `dm_type`, it will
  call this to parse the OPEN body before invoking `warehouse_open_conn_by_ne`."*
- `warehouse_open_conn_by_ne(wh, neid, slot, dtype, ip, port, ssh_flag)`
  (`ncland_warehouse.cpp:422`) — takes params inline, but has **no creds args**;
  it fetches creds internally from es64/Frmlnk.
- How opens are normally triggered: ZMQ events `app.ne.seed` / `db.ne.*`
  (`ncland_notify.cpp:214/227` → `try_open` → `warehouse_open_conn_by_ne`). The
  seed body is built by `nclan_seed.cpp`, which **enumerates NEs from `Frmlnk`** —
  the root reason a start needs provisioning.
- No `DMTYPE_OPEN` exists yet; all mq msgs are `DMTYPE_TEXT_MSG`
  (`msgscreen.h:849`).

### Proposed mechanism (agreed: single flag, 3 guards)
Add a per-conn flag, e.g. `c->no_framelink`, set when the interface is started via
inline OPEN. It gates:
1. **credential block** → use inline `user`/`pass`, skip es64/Frmlnk read.
2. **link-UP publish** (`ncland_lua.cpp:690`) → skip
   `setFrameLinkStatus`/`snmpSetLinkStatus`.
3. **link-DOWN publish** (`ncland_warehouse.cpp:219`) → skip both.

Seed/event opens leave the flag false → behavior unchanged. Also implies
`warehouse_open_conn_by_ne` gains optional `user`/`pass` (+ the flag), NULL for
existing callers.

### Scope options (UNDECIDED — the open decision)
- **A. Inline-creds open + guards** (smallest): add optional `user`/`pass` +
  `no_framelink` to `warehouse_open_conn_by_ne`; guard all 3 sites. Directly
  callable by clansimd unit tests (cf. `ncland_unit_tests.cpp:1171` already calls
  the open fn directly). No new DMTYPE, no sender changes. Unblocks testing.
- **B. A + wire mq OPEN**: also define `DMTYPE_OPEN`, route CMD vs OPEN in
  `warehouse_handle_dacs_msg`, and emit OPEN from `clan_util`/`clan.c`. Full
  production path; touches shared `msgscreen.h` + the clan sender.
- **C. A + ncland OPEN route only**: A, plus ncland-side OPEN parse/route on a
  chosen discriminator, but leave the clan sender for later. Testable via a
  crafted `dacs_msg`, no shared-header changes.

### Open questions to resolve offline
1. **Test harness shape:** C++ unit test calling `warehouse_open_conn_by_ne`
   directly, vs end-to-end message into the daemon? (Decides whether the mq OPEN
   path is needed at all now.)
2. **OPEN discriminator (if B/C):** new `DMTYPE_OPEN`, a sentinel neid, or a field
   in the body? This is what decides whether shared `msgscreen.h` + the clan
   sender get touched.
3. **Credentials for the unprovisioned NE:** inline in the OPEN body (current
   `ip:port:user:pass:dtype` shape), hardcoded test defaults, or connect-without?

---

## Related context (this session)
Prereq work sits alongside other ncland changes already committed on
`ncland-pss-msg-cleanup` (commit `50ff5ddde`):
- strip trailing NE prompt from replies (`ncland_worker.cpp`)
- `REG_NEWLINE` for `errors[]` regex + `(^|\n)Error:` in `nokia_1830_pss.yaml`
- shared config-path define `CLAN_NCLAND_JSON_PATH` (→ `/usr/cnc/features/ncland.json`)
- PSI NE support: `nokia_psi_{l,m,2t}.yaml` (dtypes 156/158/215) `extends:
  nokia_1830_pss`

The clansim.lua prereq-#1 change is currently **uncommitted** in the working tree
(both `3b2/data/clansim.lua` and `test/fixtures/clansim.lua`).
