# netFLEX Multi-Protocol NE Simulator (`nesim`)

## Goal

Build a new standalone network-element simulator, written in **Go**, that simulates every
southbound interface netFLEX supports: **TL1, CLI (telnet/ssh), SNMP, NETCONF, RESTCONF**.
It runs as **one central server** with a **REST API** and a **web GUI**. Any netFLEX host can
attach to its simulators as if they were real NEs.

It replaces today's fragmented simulators. The baseline is **feature parity** with them (see
"Parity baseline"), with **Lua scripting** added wherever it makes responses dynamic,
stateful, or event-driven.

## Problems with today's simulators (why)

- Each protocol has its own simulator, config file, and control mechanism:
  - TL1: `xlansim`/`xlansimd`, configured in `/usr/cnc/features/lansim`.
  - CLI: `.clansim` ilua scripts, `clansim`, and `clansimd`.
  - SNMP: `snmp_sim` plus `snmp_mkports`.
- NETCONF and RESTCONF have no network simulator. The netFLEX client daemons (`netclan`,
  `restclan`) fake them internally when the NE's IP is `localhost`/`xlansim`, so that code
  path is never exercised against a real server.
- TL1 simulators listen on 127.0.0.1 with one port per NE. They are not reachable from other
  hosts.
- Simulators carry no metadata or manifest. There is no UI to browse, create, or copy them,
  and none for copying between hosts under `/simdata`.

## Architecture

- **Single central server process.** It provides:
  - the inventory
  - the simulator runtime, with all protocol listeners
  - the REST API
  - the embedded web GUI
- **IP per simulator.** Each simulator owns one IPv4 address and listens on the **standard
  ports** for the protocols it enables (TL1 port configurable per NE type, SSH 22, telnet 23,
  SNMP 161/udp, NETCONF 830, RESTCONF 443). netFLEX NE records simply point at that IP, and
  netFLEX needs no special simulator handling.
  - A dedicated, configurable **routed subnet** (e.g. `10.200.0.0/16`) is delegated to the
    server: one static route on the network router, or on each attaching host.
  - The server makes the whole range local with **AnyIP**
    (`ip route add local <subnet> dev lo`), so it does not manage an alias per simulator.
  - The server needs `CAP_NET_BIND_SERVICE`. Do not run it as root.
- **IP lifecycle.**
  - Allocate the IP when the simulator is **created**, not when it starts. It stays the same
    across stop/start and server restarts.
  - Store the IP in the simulator manifest and track it in the inventory.
  - On create, the user may pick a specific free IP. Otherwise the next free one is used.
  - Copying a simulator always allocates a **new** IP.
  - Delete releases the IP after a configurable hold period, so a stale netFLEX NE record
    never silently points at a different simulator.
  - Workspaces may have an address block reserved for them (e.g. `10.200.5.0/24` for
    `linc05`).
  - Refuse to start a simulator whose IP is already in use.
- **Workspaces.** The per-host directories under `/simdata/<host>` (NFS) become workspaces.
  The GUI and API can:
  - browse every workspace
  - filter by workspace
  - copy one or more simulators between workspaces
- **Scale:** target hundreds of simulators running at once on one server.
- **Platform:** a single static binary with no CGO if possible, running on RHEL 8/9.

## Simulator model and manifest

A simulator is **one NE**: one IP, one or more protocols enabled. TL1 gateway NEs can still
serve several TIDs, as today.

Each simulator directory has a manifest (`nesim.yaml`) with:

- id, name, description, owner, workspace, tags
- NE vendor/type (netFLEX dtype), software release, TID(s)
- IP address
- enabled protocols, each with its port (default: standard) and credentials
- behaviour options: per-command delays, ACK default, mode flags, autonomous message config
- Lua script entry points, if any
- provenance: imported from (`lansimNNNNNN` path), recorded from (NE/IP/date), copied from

## Inventory and lifecycle

- List, search, and filter simulators across workspaces: by state, protocol, type, owner,
  and tag.
- Create (empty, from a template, or by import), start, stop, restart, delete, clone/copy,
  and rename. Edit the manifest.
- Bulk start/stop. Simulators marked autostart start when the server starts.
- Show runtime status: listening, connected clients, request counts, and the last errors.
- Browse, view, edit, upload, and download response files in the GUI.

## Parity baseline (must reproduce)

### TL1 (today: `xlansim`, `tl1sim.c`)

- **File matching order:** `cmd.AID.suffix` (AID as sent, then lower, then upper), then
  `cmd.suffix`, then `cmd.AID`, then `cmd`, then `cmd.ALL`.
  - A `.ALL` file is filtered by AID.
  - Suffixes are built from PM type/location/direction/time and from NSAP, TTI, OTDR, and
    RTRV-CRS-ALL parameters.
  - `#` in an AID is a literal character, not a wildcard.
  - Fall back to the shared include dir (`IDIR`), then to the per-dtype equipment dir.
- **Response file grammar:**
  - Headers `COMPLD` / `PRTL` / `DENY`.
  - Partial chunks `>` / `<`, terminator `;`, and `<RL>`.
  - Inline autonomous messages, starting `*` or `A … REPT/RPT/EVT`.
  - Directives `@delay N` and `@run`.
  - Substitution variables `$aid $cmd $ctag $tid $date $time[±min] $rangeA-B` and `$seq`.
- **`#!` script responses:** a response file starting `#!` is an external script. Keep this
  for compatibility; Lua is the preferred replacement.
- **Defaults when no file matches:**
  - `ACT-USER` → COMPLD.
  - `rtrv-hdr` / `rtrv-ne-tl1` → COMPLD.
  - Any other `rtrv*` → `DENY SNFL`.
  - All other commands → the configured ACK code.
  - PDS/DACS NEs use their own framing.
- **Multi-TID gateway NEs:** one subdirectory per TID.
- **Per-simulator options:**
  - TID, ACK, per-command DELAY, SEGSLEEP
  - MODE flags (`NO_RESEND`, `NO_LOGIN`, `NO_RTS_LOGIN`, …)
  - TACACS password challenge
  - Z-series prompt emulation
  - a protect/second-login port
- **Autonomous messages:**
  - Send a named alarm file with substitution (`$severity`, `$service`, `$location`,
    `$misc`, `$rate`, …).
  - Replay a bulk alarm script with its directives `T= S= I= A= X= L= M=`.
  - Stress/rate mode.
  - All of these are triggered from the API and GUI. This replaces almgen, the UDP alarm
    port, and SIGUSR1.

### CLI (today: `.clansim` ilua scripts, `clansim`, `clansimd`)

- Login prompt state machine per NE type, with input not validated by default.
- Command-to-file mapping: whitespace, `/`, and `|` become `_`.
- File search order: `<tid>/CLI/`, then `<tid>/`, then `CLI/`, then the root.
- Unknown command → `Error simulator file not found`. `logout` exits.
- Per-NE prompt emulators for the types the C `clansim` hard-codes (TSS5, 1678, LU, PSS1830,
  ncs1000, ces, CISCO_IOS, …).
- Serve both SSH and telnet.
- Parity with `clansimd`'s `INJECT`: inject a one-off reply overlay at runtime through the
  API.
- **Run the existing `.clansim` Lua scripts** (`3b2/data/*.clansim`, `clansim.lua`) unchanged
  or with minimal changes. Fix the `patters`/`patterns` bug in `advanced_sim` as part of the
  port.

### SNMP (today: `snmp_sim`)

- Load `.mdb` MIB dumps.
- Select per-card/context `ctxid.<EngineID>.mdb` files by community string.
- GET, GETNEXT, GETBULK, and SET, with SET changing in-memory state.
- The `simulator` community can create and delete variables. RowStatus destroy deletes the
  row.
- Dynamic OIDs: counters that increment on each read, date/time values, and sysUpTime.
- v1/v2c is required. v3 is optional; see open questions.
- Traps: send from the simulator's own IP, triggered from the API/GUI, Lua, or a schedule.
  This replaces `trapgen` / `trapsend_proxy`.

### NETCONF (today: client-side `netconfRsp.map` in `netclan`)

- A real NETCONF server over SSH on port 830, with hello/capabilities and sessions.
- Match requests with the same rules as today:
  - `netconfRsp[.<slot>].map` holds `req.xml,rsp.xml` pairs.
  - Comparison is structural XML, ignores the `<rpc>` wrapper, and allows `*` wildcards.
  - Fall back to `yang-<tag>.sim`.
- `create-subscription` and notifications, triggered from the API/GUI, Lua, or a notification
  dir/schedule. This replaces the `netclan.<neid>.xml` / `.notsim.dir` drop-in files.

### RESTCONF / REST (today: client-side `restRsp.map` in `restclan`, `tmf814Nms`)

- A real HTTPS server on port 443, with a configurable self-signed or provided certificate.
- Match requests with the same rules as today:
  - `restRsp.map` lines: `[GET,]uri?qs,rsp.json` and `POST,uri,postData,rsp.json`.
  - The `GET!uri!file` form used by tmf814Nms.
  - Fallback file `<uri dir>/<METHOD>.<qs>.json`.
  - POST bodies are wildcarded before matching (OpenRoadm, Groove PM).
- Basic/token auth as the NE types require.

### Recording and builders

- Import the output of today's capture and builder tools (`watch -sim-on`, `makesim`,
  `mknetconfsim`, `mkrestconfsim`, `mksnmpsim`, …) into the new format.
- New: a **capture proxy mode**. The simulator sits between netFLEX and a real NE, records
  request/response pairs per protocol, and produces a simulator. *(In scope or future work?
  See open questions.)*

## Lua scripting (new)

Canned response files stay the default. Lua is an optional layer on top of them, usable per
simulator, per command/URI/OID, or globally:

- **Dynamic responses:** compute a reply from request fields (AID, OID, URI, XML), or
  post-process a canned file.
- **State:** a per-simulator key/value store, so that ENT/DLT/ED, SNMP SET, NETCONF
  `edit-config`, and RESTCONF PUT/POST/DELETE change what later retrieves return. This
  replaces `tlansim`'s symlink swapping.
- **Events:** generate alarms, REPTs, traps, and notifications on a timer, on a command, or on
  an API call.
- **Fault injection:** delays, error responses, dropped or hung sessions, and corrupted
  framing.
- Sandboxed: no unrestricted shell or filesystem access. Hot-reload without restarting the
  simulator.
- An embeddable Go Lua VM (e.g. gopher-lua, Lua 5.1). Check that it is compatible with the
  existing `.clansim` scripts.

## Import / migration

- Import existing `/simdata/<host>/lansimNNNNNN` directories (TL1, CLI, NETCONF, RESTCONF
  data), `.clansim` scripts, and SNMP `.mdb` files into the new format, generating a
  manifest.
- Gather metadata from netFLEX where available: NE id, dtype, TID, and current IP.
- Import single simulators or a whole workspace, with a dry-run report.
- Skip the empty leftover directories (`00`–`59` etc.) that `findRfile` creates.
- Never modify the source `lansim` directories.

## API

- REST/JSON with an OpenAPI spec. Everything the GUI can do is available through the API.
- Covers the inventory, lifecycle, files, events/injection, import, status, and IP
  allocation.
- A small CLI client (`nesimctl`) on top of the API, for scripts and automation.

## Web GUI

- Embedded in the binary. Includes:
  - an inventory table
  - a workspace browser for `/simdata`
  - a simulator detail page (manifest, files, live status, request log)
  - copy/clone between workspaces
  - an event/alarm/trap trigger panel
  - an import wizard
- Show a live request/response log per simulator for debugging.

## Security

- Login on the GUI and API: local users to start, with LDAP as a future option.
- Users own simulators. Mutating actions require ownership or an admin role.
- Lua is sandboxed (see above).

## Non-goals

- Replacing netFLEX's own NE drivers or changing their behaviour. The netFLEX side only
  changes the NE's IP to point at the simulator.
- Simulating X.25 / legacy `lansim` transports.
- Performance/load-testing beyond the existing stress modes.

## Open questions

1. **Import IPs:** when importing an existing simulator, should it keep the IP its netFLEX NE
   record uses today (when that IP fits the range), or always get a new one and have the NE
   repointed?
2. Should **capture proxy mode** be in the initial scope or future work?
3. Is **SNMPv3** needed?
4. Should the client-side fake paths in `netclan`/`restclan`/`xlansimd` (`localhost`/`xlansim`
   checks) be retired once the new simulator exists?
5. Can two netFLEX hosts attach to the same running simulator? If so, do they share Lua
   state?
6. Which subnet will be delegated, and who owns the router change?
