# netFLEX Multi-Protocol NE Simulator (`nesim`)

## Goal

Build a new standalone network-element simulator, written in **Go**, that simulates every
southbound interface netFLEX supports: **TL1, CLI (telnet/ssh), SNMP, NETCONF, RESTCONF**.
It runs as **one central server** with a **REST API** and a **web GUI**. Any netFLEX host can
attach to its simulators as if they were real NEs.

It replaces today's fragmented simulators. The baseline is **feature parity** with them (see
"Parity baseline"), with **Lua scripting** added wherever it makes responses dynamic,
stateful, or event-driven.

A primary user is an **LLM coding agent**, such as Claude Code. The agent uses the simulator
to write tests for netFLEX NE drivers, and to verify that a code change behaves correctly
and causes no regressions. Every feature must work without a human using the GUI (see
"LLM and automated testing").

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
  - A simulator created in the app gets its IP when it is **created**.
  - An **imported** simulator has no IP after import. It gets one the **first time it is
    started**.
  - Once assigned, the IP stays the same across stop/start and server restarts.
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
- **One host per running simulator.** A running simulator is attached to exactly **one**
  netFLEX host. It is never shared.
  - The attached host is fixed at start. It defaults to the workspace's host (e.g.
    `linc05`) and can be overridden on start.
  - The host is identified by its source IP(s). A host with several addresses can list all
    of them.
  - The simulator refuses connections (TL1/CLI/NETCONF/RESTCONF) and drops packets (SNMP)
    from any other source. Each rejection is logged and shown in status and the journal,
    naming the host that holds the simulator.
  - Moving a simulator to a different host means stopping it and restarting it attached to
    the other host. Its state, and therefore Lua state, belongs to that single attachment.
- **Scale:** target hundreds of simulators running at once on one server.
- **Platform:** a single static binary with no CGO if possible, running on RHEL 8/9.

## Simulator model and manifest

A simulator is **one NE**: one IP, one or more protocols enabled. TL1 gateway NEs can still
serve several TIDs, as today.

Each simulator directory has a manifest (`nesim.yaml`) with:

- id, name, description, owner, workspace, tags
- NE vendor/type (netFLEX dtype), software release, TID(s)
- IP address
- enabled protocols, each with its port (default: standard) and credentials (incl. SNMP
  communities, SNMPv3 engine ID and USM users)
- behaviour options: per-command delays, ACK default, mode flags, autonomous message config
- Lua script entry points, if any
- provenance: imported from (`lansimNNNNNN` path), copied from

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
- **v1, v2c and v3 are all required.** For v3:
  - Each simulator has its own snmpEngineID, configurable in the manifest.
  - USM users are set per simulator in the manifest. Auth: none/MD5/SHA/SHA-2. Privacy:
    none/DES/AES.
  - The v3 context (contextName / contextEngineID) selects the per-card `ctxid` data, in
    place of the community string used with v1/v2c.
- Traps and informs (v1/v2c/v3): send from the simulator's own IP, triggered from the
  API/GUI, Lua, or a schedule. This replaces `trapgen` / `trapsend_proxy`.

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

### Existing capture and builder output

- Import the output of today's capture and builder tools (`watch -sim-on`, `makesim`,
  `mknetconfsim`, `mkrestconfsim`, `mksnmpsim`, …) into the new format. Those tools stay in
  netFLEX unchanged.

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
- Import does **not** allocate an IP. The simulator stays unaddressed until its first start
  (see "IP lifecycle").
- Gather metadata from netFLEX where available: NE id, dtype, and TID.
- Import single simulators or a whole workspace, with a dry-run report.
- Skip the empty leftover directories (`00`–`59` etc.) that `findRfile` creates.
- Never modify the source `lansim` directories.

## LLM and automated testing (new)

The agent must be able to set up NE behaviour, run netFLEX against it, and check what
netFLEX did, entirely through tools.

- **MCP server.** The server exposes its API as MCP tools, so an LLM agent can drive it
  directly. Tools include:
  - list, create, start, stop, and delete simulators
  - read and write response files and Lua
  - inject events (alarms, traps, notifications)
  - query the request journal
  - snapshot and reset state
  - explain how a request was matched
- **Ephemeral test simulators.**
  - Create a simulator in one call from a template, from an existing simulator (cloned
    copy-on-write), or from inline response definitions.
  - Scope test simulators to a test session. Each has a TTL and is cleaned up automatically
    when the session ends or the TTL expires. Test sims never pollute workspaces.
  - Use a separate IP pool for test simulators, so tests can run in parallel.
- **Request journal.**
  - Record every request and response per simulator, with timestamps: TL1 command, CLI
    line, SNMP PDU, NETCONF RPC, RESTCONF method/URI/body.
  - Queryable and filterable through the API and MCP, so a test can assert e.g. "netFLEX
    sent `ED-OCH` to AID X with parameter Y" or "no SET was issued".
- **Unmatched requests.** Report every request that matched no response file, with
  the paths that were searched. This tells the agent which simulator data is missing.
- **Match explain / dry run.** Given a request, return which file or Lua handler would
  answer it, the search order that was tried, and the rendered response. Do this without a
  client connection.
- **Expectations.** Optionally declare expected requests (with order, count, and parameter
  matchers) and later verify them. The result is structured pass/fail with a diff.
- **Deterministic mode.** For repeatable tests:
  - a fixed or controllable clock (`$date`, `$time`, sysUpTime)
  - seeded `$range` values
  - deterministic sequence numbers and delays
- **State snapshot/restore.** Save and restore a simulator's full state (Lua store, SNMP
  DB, overlays) by name, so tests start from a known baseline.
- **Golden-journal regression.** Record a journal from a known-good netFLEX build and save
  it as a golden file. Later runs are diffed against it, normalising ctags, timestamps,
  and sequence numbers, so a code change shows up as a precise list of changed requests.
- **Declarative scenarios.** A YAML scenario file contains:
  - the simulators to stand up (inline or by reference)
  - initial state
  - events to inject, with timing
  - expectations and golden-journal comparisons

  `nesimctl test run <scenario>` runs it and produces JUnit XML plus JSON results for CI.
  Scenarios are plain text so an LLM can write and review them.
- **Machine-readable documentation.**
  - The OpenAPI spec.
  - A JSON Schema for the manifest and scenario formats.
  - A reference for each response-file grammar and the Lua API.
  - Ship an `llms.txt` / agent skill describing how to use the simulator for testing.
- **Actionable errors.** Every error says what was expected and how to fix it, such as a
  malformed response file with its line number, a Lua error with a stack trace, or an IP
  conflict naming the owner.
- **Validation.** Provide lint/validate endpoints for response files, manifests, Lua
  scripts, and scenarios, so an agent can check its work before starting a simulator.

## API

- REST/JSON with an OpenAPI spec. Everything the GUI can do is available through the API.
- Covers the inventory, lifecycle, files, events/injection, import, status, and IP
  allocation.
- A small CLI client (`nesimctl`) on top of the API, for scripts, CI, and test runs.
  Machine-readable (`--json`) output on every command.
- An MCP server exposing the same operations (see "LLM and automated testing").

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
- Recording or capturing a real NE to build a simulator. The simulator does not proxy or
  capture live NE traffic.

## Open questions

1. Should the client-side fake paths in `netclan`/`restclan`/`xlansimd` (`localhost`/`xlansim`
   checks) be retired once the new simulator exists?
2. Which subnet will be delegated, and who owns the router change?
3. Should scenarios also **drive netFLEX**? Scenarios could run netFLEX commands on an
   attached host (e.g. trigger a provision or audit), or the agent could drive netFLEX
   itself and use the simulator only for setup and assertions.
4. Which netFLEX test framework should consume the results, if any? Or is JUnit XML plus
   JSON enough?

## Assumptions to confirm

- An imported simulator **keeps** the IP it gets on first start for all later starts. The
  alternative is a fresh IP on every start.
- The single-host rule is enforced by **source IP**, and the attached host defaults to the
  workspace's host.

## Status (2026-10-14)

Prompt refinement paused. Before turning this into a spec, resolve the open questions above
and confirm the assumptions.

Decided so far:
- One central server; IP per simulator via an AnyIP routed subnet.
- Existing simulators are imported/migrated, not read in place.
- Feature parity with today's simulators, plus Lua.
- SNMPv3 is required.
- Recording real NEs is a non-goal.
- An LLM agent is a primary user.
- No sharing of a running simulator between hosts.
