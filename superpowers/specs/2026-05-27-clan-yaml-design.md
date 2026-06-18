# clan-rewrite — YAML-driven NE Configuration

**Status:** Design approved 2026-05-27 (revised 2026-05-27 to incorporate transport, tech stack, and architecture details from `2026-05-04-ncland-zeromq-seeding-design.md` / `~/ncland-zeromq.md`; revised 2026-06-04 to resolve `GAPS.md` — credential tokens §7.3, YAML inheritance via `extends:` + abstract bases §7.5, `transport.protocol` list §7.6, `prompt.adapt_from_actual` §7.7, reserved session vars §9.2; revised 2026-06-04 to make seeding event-driven over the **nfdb ZMQ bus** — new `nclan-seed` CLI reads `frame_link` and publishes `app.ne.seed` events; clan-rewrite SUBs `app.ne.*` (seed) + `db.ne.*` (runtime); warehouse never reads `frame_link`; single SUB ingestion path for seed + runtime, §4.4 / §10.1 / §11)
**Author:** Dan Nuzzo (dnuzzo@lightriver.com)
**Scope:** `cnc/sdi/` — bootstraps a rewrite of `clan.c` that runs in parallel with the legacy implementation.

## 1. Purpose & Goals

`cnc/sdi/src/clan.c` (~10 K lines) is the CLI gateway between netFLEX clients and network elements (NEs). It hardcodes per-NE-type behavior — prompt regexes, login sequences, keepalive commands, paging handling, post-command processing — in C `#define`s, switch ladders, and per-NE `setup<Vendor>Connection()` functions. Adding a new NE type requires recompilation.

This document specifies a replacement (`clan-rewrite`, working name) where per-NE behavior is described in YAML files and optionally backed by companion Lua scripts. The rewrite runs alongside `clan.c` during validation: legacy handles NE types until each one is independently verified, then ownership transfers.

**Goals**

1. Adding a new NE type requires only dropping a `.yaml` (and optional `.lua`) into a config directory — no C recompilation.
2. All static, declarative per-NE data lives in YAML.
3. Complex, conditional per-NE logic lives in companion Lua scripts.
4. The rewrite coexists with `clan.c` on the same daemon, dispatching per NE type. Cutover is per-NE, reversible.
5. Behavioral parity with `clan.c` is verifiable before any NE type is migrated.
6. Caller-boundary transport, NE-lifecycle eventing, and process model align with the patterns established by `ncland` so the two daemons present a consistent integration surface.

**Non-goals**

- Rewriting non-NE-specific parts of `clan.c` (logging plumbing, supervisor wiring) is out of scope for this document and handled by the implementation plan as needed.
- This document does not specify how clients discover which dtype lives on which endpoint — that question is captured in §14 Open Questions.

## 2. Tech Stack

Mirrors `~/ncland-zeromq.md` so clan-rewrite and ncland share build conventions and developer ergonomics:

| Component | Purpose | Library / Flag |
|-----------|---------|----------------|
| C++17 | Implementation language | `g++` |
| libzmq | Caller-boundary ROUTER socket | `-lzmq` |
| nfdb ZMQ event bus (on `niimxd`) | NE-lifecycle event ingestion (SUB) and `nclan-seed` publish | `-lzmq` (SUB socket); `libnfdb` for `nclan-seed`'s `PUBLISH` (§11) |
| cJSON | JSON Schema validation of `ne/*.yaml` | `-ljson` |
| libyaml (or yaml-cpp) | YAML parsing | `-lyaml` — exact lib picked during plan |
| Lua 5.4 (or LuaJIT) | Companion-script runtime | `-llua` |
| libssh | SSH transport to NEs that require it | `-lssh` |
| epoll | Main event loop (fd readiness) | linux syscalls |
| signalfd | Signal handling (`SIGHUP` for reload, `SIGTERM` for shutdown) | linux syscall |
| nfunit-test.hpp | Unit-test harness | header-only, matches ncland |

**Compiler targets:** `g++ 8.5.0` (RHEL 8) and `g++ 11.x` (RHEL 9), matching ncland's choice. **Note:** the project-wide CLAUDE.md baseline is `gcc 4.8.5` (RHEL 7), which does not support C++17. clan-rewrite intentionally diverges from that baseline, the same way ncland does. RHEL 7 systems continue to use the legacy `clan.c` (unchanged); clan-rewrite is RHEL 8+ only. Documented in §14 Open Questions for confirmation.

## 3. Background

Catalog of per-NE variation that motivated the design (cited file:line, from `cnc/sdi/src/clan.c`):

- **~25 NE type families** identified, each with its own `dtype` enum value (lines 107–179 define prompt regex pairs; lines 1300–1430 dispatch on `dtype`).
- **Prompt detection:** each NE has a `prompt1`/`prompt2` regex pair (e.g., `CIENA_RLS` uses `[-a-zA-Z0-9/:^ #]+#` and `[-a-zA-Z0-9/:()^ #]+#`).
- **Login sequences:** ~15 NE families set a `neednewline=1` flag after connect (lines 631–696). Others have multi-step dialogues handled in per-NE `setup<Vendor>Connection()` functions (e.g., `setupCienaRLSConnection` L1306, `setupNokiaFxConnection` L1332, `setupTSS5Connection` L2932).
- **Keepalives:** per-NE command and interval (lines 1731–1767, 393–415). Examples: `whoami\n` every 600s, `\n` every 43s with an 8s wakeup, `exec ping %h\n` every 600s.
- **Paging:** three families of more-prompt patterns — Cisco/Juniper `--More--`, Ciena/Fuji `] (q,g,space,enter)`, default `--More-- or (q)uit` (lines 2282–2320).
- **Disconnect:** some NEs need a clean shutdown (e.g., `ALC_1850_TSS5` write-memory + save-prompt at L1978–1995; `CIENA_WaveServer` config save at L1954).
- **Transport:** mix of telnet and ssh; banner timeouts vary; default command timeout 300 s, login 60 s (L367–388).

There is no central per-NE config table today. The information is spread across `#define`s, if-ladders, function pointers, and per-NE feature files. This rewrite extracts it.

The sibling project `ncland` (see `docs/superpowers/specs/2026-05-04-ncland-zeromq-seeding-design.md` and `~/ncland-zeromq.md`) is already partway through replacing POSIX MQ with ZeroMQ ROUTER on its caller boundary. clan-rewrite reuses that ROUTER transport pattern; for NE-lifecycle eventing both daemons now target the nfdb ZMQ bus (§11) rather than the PG-NOTIFY path the earlier ncland draft assumed.

## 4. Architecture

### 4.1 Process Model

clan-rewrite adopts ncland's warehouse/worker split:

- **Warehouse process** (one per daemon). Owns the ZeroMQ ROUTER socket, the YAML registry, the nfdb-event SUB socket (§11), and the worker fleet. Single-threaded epoll loop.
- **Worker processes** (configurable count, e.g. `-w 4`). Each worker handles a partition of the NE-side I/O — talks libssh/telnet to NEs, drives the YAML stepper + Lua VM for sessions it owns.
- **Internal IPC**: each worker is connected to the warehouse over a Unix-domain `ctl_sock` pair (`socketpair(AF_UNIX, SOCK_STREAM, 0, sv)`). All warehouse↔worker traffic flows over these sockets. `SCM_RIGHTS` is used to hand pre-opened NE fds from warehouse to worker on assignment.

### 4.2 Boundary Discipline

**ZeroMQ is only on the external caller boundary.** Workers do not speak ZMQ; warehouse does not speak ZMQ to workers. Mirrors ncland: "ZeroMQ touches only the external caller boundary; warehouse↔worker IPC stays on the existing Unix-domain ctl_sock (extended to carry framed cmd/rsp in addition to SCM_RIGHTS)."

The internal `ctl_sock` carries multiplexed frames using single-byte tags (matching ncland's `NCLAND_CTL_TAG_*` enum):

| Tag | Direction | Payload |
|-----|-----------|---------|
| `'F'` | warehouse → worker | SCM_RIGHTS fd transfer (NE connection handoff) |
| `'C'` | warehouse → worker | `clan2_cmd_msg_t` (proxied client command) |
| `'R'` | worker → warehouse | `clan2_rsp_msg_t` (response, forwarded to caller via ROUTER) |
| `'X'` | warehouse → worker | graceful shutdown |

Frame layout per direction: `[1-byte tag][fixed-size payload]`. No length prefix needed — payload size is determined by tag.

### 4.3 Diagram

```
clients (DEALER) ─────► ZMQ ROUTER ◄───── ncland warehouse
                            │
                            │ ipc:///var/run/clan2/ctl.sock
                            ▼
                  ┌─────────────────────┐
                  │  clan-rewrite       │
                  │  warehouse          │
                  │  ┌───────────────┐  │
                  │  │ YAML registry │  │
                  │  │ stepper       │  │
                  │  │ Lua VM        │  │
                  │  │ epoll loop    │  │
                  │  │ nfdb SUB fd   │  │
                  │  │ ZMQ_FD        │  │
                  │  │ signalfd      │  │
                  │  └───────────────┘  │
                  └──────┬───────┬──────┘
                ctl_sock │       │ ctl_sock
              (framed +  │       │ (framed +
               SCM_RIGHTS)│       │  SCM_RIGHTS)
                         ▼       ▼
                     ┌──────┐ ┌──────┐
                     │workr │ │workr │ ... (N workers)
                     │libssh│ │telnet│
                     │YAML  │ │YAML  │
                     │stepr │ │stepr │
                     │Lua VM│ │Lua VM│
                     └──┬───┘ └──┬───┘
                        ▼        ▼
                       NE        NE
```

### 4.4 Startup Seeding

**The warehouse never reads `frame_link`.** Seeding is delegated to a standalone command-line tool, `nclan-seed` (§11.5), that reads `frame_link` and publishes one `app.ne.seed` event per NE onto the **nfdb ZMQ event bus** (the pub/sub broker that `niimxd` owns — see §11). clan-rewrite is seeded by *consuming those events* on the same SUB socket it uses for runtime NE-lifecycle events — a single ingestion path for seed-time and runtime changes.

Startup ordering: the warehouse connects and subscribes its SUB socket **first**, then `fork`/`exec`s `nclan-seed` (§10.1 step 9). ZMQ pub/sub is non-durable (slow-joiner), so the subscription must be established before the tool publishes, or seed events are lost.

`nclan-seed` is **idempotent and re-runnable** — opening an already-open session is a no-op (§11), so re-running the tool (or a second consumer such as ncland running its own copy) replays harmlessly. A re-seed after `SIGHUP` is just another `nclan-seed` invocation.

Because `nclan-seed` is now the *only* reader of `frame_link`, the `atch_frm`/`m_clan` shm-concurrency concern (§14.10) is scoped to a short-lived one-shot tool rather than the long-running daemon.

### 4.5 Dispatcher Rule

For each session-open request, look up `dtype` in the YAML registry. Hit → handled by clan-rewrite. Miss → legacy `clan.c` handles it as today.

Until clients have a clean way to learn endpoint ownership (§14.4), legacy `clan.c` is the request entry point for unknown dtypes, and clan-rewrite is reached by either:
- clients that already know clan-rewrite's endpoint, or
- a small forwarding hook inside `clan.c` that consults the YAML registry's known dtypes and proxies matching traffic over the ROUTER.

### 4.6 Four Planes

1. **Registry** — startup-loaded map: `ne_type → (parsed YAML, compiled regexes, Lua module)`.
2. **Stepper** — executes the YAML step language for `login_steps`, `post_command`, `disconnect_steps`. Maintains per-session state. Runs inside workers.
3. **Lua bridge** — exposes `session` and `global` tables to Lua scripts. `global` is process-wide per worker, mediated by a proxy that locks on read/write.
4. **Transport + eventing** — warehouse ZeroMQ ROUTER for client traffic (§5); **nfdb-event SUB socket** for NE-lifecycle events, fed at seed time by `nclan-seed` (`app.ne.*`) and at runtime by the nfdb `ne`-DB layer (`db.ne.*`) (§11); internal `ctl_sock` framed protocol for warehouse↔worker (§4.2).

## 5. Transport (ZeroMQ ROUTER)

clan-rewrite exposes a single ZeroMQ ROUTER socket bound to a configurable endpoint (default `ipc:///var/run/clan2/ctl.sock`). All client traffic flows over this socket; the legacy clan.c IPC mechanism continues to serve dtypes the YAML registry does not claim.

This section borrows directly from the ncland design — see `~/ncland-zeromq.md` Tasks 2–4 for the canonical implementation pattern.

### 5.1 Wire Format

- Client → clan-rewrite (DEALER → ROUTER): `[identity][empty][cmd_bytes]`
- clan-rewrite → client (ROUTER → DEALER): `[identity][empty][rsp_bytes]`

`cmd_bytes` / `rsp_bytes` are the existing C structs already used by the clan↔client protocol (renamed `clan2_cmd_msg_t` / `clan2_rsp_msg_t` in clan-rewrite for clarity). Clients see identical payloads — only the framing transport changes.

### 5.2 Identity Stash

ROUTER prepends the caller identity on receive; clan-rewrite must remember it until the worker produces a response. A fixed-size table (`CLAN2_MAX_PENDING`, default 256) keyed by message sequence number holds outstanding identities. Overflow → WARN log, request rejected immediately (does not block the ROUTER socket). Same pattern as `warehouse_pending_stash` / `warehouse_pending_take` in ncland.

### 5.3 Edge-Triggered Readiness via `ZMQ_FD`

`zmq_getsockopt(socket, ZMQ_FD, ...)` returns an OS-level fd suitable for `epoll`. The fd is **edge-triggered** — readiness fires once per level transition, not per frame. The drain loop must:

1. On `EPOLLIN`, loop `zmq_recv(... ZMQ_DONTWAIT)` until it returns `EAGAIN`.
2. Re-check `ZMQ_EVENTS` for `ZMQ_POLLIN`. If still set, continue draining.
3. Return only when `EAGAIN` and `ZMQ_EVENTS` is clear.

Single-frame reads per epoll wake will deadlock under load.

### 5.4 High-Water Marks

`ZMQ_SNDHWM` and `ZMQ_RCVHWM` default to 4096. ROUTER drops outbound frames to vanished clients (no blocking by design). Inbound HWM acts as backpressure — clients see EAGAIN on DEALER send when the warehouse is saturated.

### 5.5 Configuration

CLI flag `-c <endpoint>` overrides the default. The YAML `transport:` block inside each NE file is unrelated — it describes the NE-side transport (telnet/ssh to the equipment), not the caller boundary.

### 5.6 Coexistence with ncland

ncland uses the same wire pattern on its own ROUTER endpoint. Clients learn which endpoint owns which dtype via an ownership mechanism that is **not yet decided for clan-rewrite** — see §14 Open Questions. Until decided, clan-rewrite's endpoint serves only dtypes the YAML registry declares; clients without that knowledge continue to use the legacy clan.c IPC, which routes correctly because legacy dtypes are unaffected.

## 6. File Layout

```
cnc/sdi/clan2/
├── ne/                           # one YAML per NE type (+ optional abstract bases)
│   ├── _defaults.yaml            # implicit base merged under every file
│   ├── ciena.base.yaml           # abstract: true — shared Ciena behavior (§7.5)
│   ├── ciena_rls.yaml            # extends: ciena.base
│   ├── ciena_ces.yaml            # extends: ciena.base
│   ├── fuji_1finity_x100.base.yaml  # abstract: true — Fuji X100 family base
│   ├── fuji_1finity_s100.yaml   # extends: fuji_1finity_x100.base
│   ├── fuji_1finity_t100.yaml   # extends: fuji_1finity_x100.base
│   ├── fuji_1finity_l1xx.yaml   # extends: fuji_1finity_x100.base
│   ├── alc_1850_tss5.yaml
│   ├── nokia_fx.yaml
│   ├── ciena_rls.lua             # companion Lua sits FLAT beside the .yaml
│   ├── alc_1850_tss5.lua         #   (updated 2026-06-08: no lua/ subdir)
│   └── ...
├── schema/
│   └── ne_schema.json            # JSON Schema for ne/*.yaml validation
├── src/                          # the clan-rewrite C/C++ code (warehouse + workers)
└── nclan-seed/                   # standalone seeder: frame_link → nfdb app.ne.seed events (§11.5)
```

**Conventions:**

- `ne_type:` is the NE's **display name** from the `dtypes[]` table (`include/dtype.ext`), e.g. `"CIENA 6500 RLS"` — **not** the C enum symbol (`CIENA_RLS`). The loader maps `ne_type → numeric dtype` by matching `dtypes[]` (`ncland_registry_dtype_for_name`); the registry is keyed by that numeric dtype (which the seed/`db.ne.*` events also carry). **Decision 2026-06-05:** `dtypes[]` holds display names, not enum symbols, so the display name is the only runtime-available string to match on.
- Filename is organizational (kebab/lowercase of the type, e.g. `ciena_rls.yaml`) and is **not** required to equal `ne_type` — display names contain spaces. The loader does not enforce a filename↔`ne_type` match.
- Lua filename matches `lua_module:` field exactly, and lives **flat in the same directory** as the `.yaml` (resolved as `NCLAND_NE_DIR/<lua_module>`; updated 2026-06-08, no `lua/` subdir).
- `_defaults.yaml` is merged under every NE file; per-NE keys win.
- A file may inherit from one parent via `extends:` and overlay its own keys on top (§7.5). Chains are multi-level, single-parent.
- Abstract base files (`abstract: true`) are templates only — never registered as NEs, exempt from the filename and `ne_type` rules. Used to group shared behavior (vendor family, transport family). A naming convention such as `*.base.yaml` is recommended for humans but the loader keys off the `abstract:` field, not the name.

## 7. YAML Schema

### 7.1 Top-Level Structure

```yaml
ne_type: "CIENA 6500 RLS"       # required (concrete files); must match a dtypes[] DISPLAY name (include/dtype.ext)
extends: ciena.base             # optional; basename of parent file to inherit from (§7.5)
display_name: "Ciena RLS"       # required; human-readable
vendor: ciena                   # required; lowercase short name
schema_version: 1               # required; loader rejects unknown versions

transport:
  protocol: [ssh, telnet]       # scalar or list; list = preferred order, try-then-fallback (§7.6)
  default_port: 23
  banner_timeout_s: 30
  max_buffer_bytes: 1048576     # default 1 MB; expect-buffer cap

prompt:
  primary_regex:  '[-a-zA-Z0-9/:^ #]+#'
  secondary_regex: '[-a-zA-Z0-9/:()^ #]+#'
  need_newline_after_connect: true
  adapt_from_actual: false      # passed to lua_module::login as a session var; stepper itself does nothing (§7.7)

timeouts:
  login_s:   60
  command_s: 300
  inter_command_ms: 0

keepalive:
  enabled: true
  interval_s: 600
  command: "exec ping %h\n"     # see §7.3 for substitutions
  expect_response: true

paging:                          # implicit & automatic — stepper drains it on every command read (§8.3)
  more_pattern: '--More-- or (q)uit'
  more_response: " "
  large_output_scan: true

errors:                          # patterns indicating "command rejected"
  - 'Error:'
  - 'Invalid command'
  - 'Permission denied'

post_command:   [...]            # step language — §8
disconnect_steps: [...]          # step language — §8

lua_module: ciena_rls.lua        # REQUIRED — must expose login(session, global, args). See §9.5
```

### 7.2 Required vs Optional

Required (on the **fully-merged concrete file**, §7.5): `ne_type`, `display_name`, `vendor`, `schema_version`, `prompt.primary_regex`, `transport.protocol`, `lua_module`.

These may be supplied by the file itself, by `_defaults.yaml`, or by any ancestor via `extends:` — the loader checks them only after the merge completes. An individual fragment (a base, or a thin variant) may therefore omit keys its ancestors or descendants supply. Abstract files (`abstract: true`) are exempt — they are never validated as standalone NEs.

All other keys default from `_defaults.yaml`. A minimal NE file is ~6 lines (the extra one is the `lua_module:` pointer; the file it names supplies `login()`), or as few as 2–3 lines when it `extends:` a base that carries the bulk of the config.

**Login intentionally lives in Lua, not YAML.** Earlier drafts of this spec defined a `login_steps:` block in step-language form. Authoring real NEs (ALC_1850_TSS5, NOKIA_FX, CIENA_RLS) showed that login dialogues are inherently imperative — banner detection, credential branching, prompt capture — and the step-language encoding was awkward. Login moved to a required `login(session, global, args)` entry point in each NE's companion Lua module. `post_command` and `disconnect_steps` remain declarative because their bodies are mostly data (a more-pattern + response, or a fixed cleanup sequence).

### 7.3 String Substitutions

Applied to every string field at runtime:

| Token | Replaced with |
|-------|---------------|
| `%h`  | NE hostname |
| `%u`  | login user |
| `%p`  | login password (primary) |
| `%p_enable` | enable-mode password |
| `%p_read`   | read-community / read credential |
| `%p_write`  | write-community / write credential |
| `%p_admin`  | admin credential |
| `%t`  | NE type (`ne_type` value) |

Literal `%` is `%%`. The `%p_*` tokens mirror the reserved session-var names (§9.2) so a keepalive command such as `exec ping %h credential %p_read\n` resolves without dropping to Lua. A token whose backing session var is nil expands to the empty string (WARN logged once per session).

### 7.4 Validation Rules

Loader rejects (skip the NE type, daemon continues):

- Unknown top-level key.
- Required key missing.
- Wrong scalar type (e.g., `interval_s: "600s"` instead of integer).
- Regex that fails to compile.
- `schema_version` not in supported set.
- `ne_type` duplicated across concrete files (both skipped).
- `extends:` names a parent file that does not exist (skip leaf, WARN).
- `extends:` chain contains a cycle (skip every file on the cycle, WARN).
- Filename does not match `ne_type:` lowercased (concrete files only; abstract files exempt).
- A merged-concrete file is missing any required key (§7.2) after the full `_defaults` → ancestors → self merge.

Validation runs on the **fully-merged concrete file**. Regex compilation, required-key checks, and `lua_module` verification (§10.1) all happen post-merge — a base or thin variant is never validated standalone.

### 7.5 Inheritance via `extends:`

A file may name one parent with `extends: <basename>` (the parent file's name without `.yaml`). The parent may itself `extends:` another — chains are multi-level but each file has exactly **one** parent (no multiple inheritance / mixins).

**Merge order**, lowest precedence to highest:

```
_defaults.yaml  →  [ extends chain, root ancestor first ]  →  this file
```

- **Maps deep-merge**; the more-derived value wins per key. Same rule as `_defaults.yaml` overlay today, extended to the whole chain.
- **Lists replace wholesale.** A child that sets `errors:`, `post_command:`, `disconnect_steps:`, or `transport.protocol:` fully overrides the inherited list — entries are not appended. To tweak one entry, restate the list. (No way to *remove* a single inherited list entry except by restating the list without it.)
- `_defaults.yaml` remains the implicit base for **every** file, including those with `extends:`. It sits beneath the named ancestors.

**Abstract base files** (`abstract: true`) are templates only:

- Never entered into the registry (§4.6) — never dispatched, no `dtype` round-trip check (§13.1).
- Exempt from the `ne_type` requirement and the filename-match rule.
- Carry shared behavior for a group: a vendor family, a transport family, or a multi-dtype equipment family.
- A naming convention (e.g. `*.base.yaml`) keeps them visually obvious; the loader distinguishes them by the `abstract:` field, not the filename, and they live in the same `ne/` directory as concrete files.

**Replaces the earlier `ne_aliases:` proposal.** The clan.c `FUJI_1FINITY_X100` family — `FUJI_1FINITY_S100` (119), `FUJI_1FINITY_T100` (120), `FUJI_1FINITY_L1XX` (121), identical behavior — is now three thin concrete files over one abstract base:

```
ne/fuji_1finity_x100.base.yaml    abstract: true            # shared behavior
ne/fuji_1finity_s100.yaml         extends: fuji_1finity_x100.base   ne_type: FUJI_1FINITY_S100
ne/fuji_1finity_t100.yaml         extends: fuji_1finity_x100.base   ne_type: FUJI_1FINITY_T100
ne/fuji_1finity_l1xx.yaml         extends: fuji_1finity_x100.base   ne_type: FUJI_1FINITY_L1XX
```

Each variant registers its own real dtype (so each passes the CI round-trip check) while the behavior lives once in the base. Variants share the base's `lua_module` unless they override it. Grouping generalizes beyond multi-dtype families — e.g. `ciena.base.yaml` holding prompt/paging/error defaults common to all Ciena NEs, or `telnet.base.yaml` holding transport defaults.

### 7.6 transport.protocol — List Form

`protocol:` accepts either a scalar (`ssh`) or a list (`[ssh, telnet]`). A list is preferred-order: the worker tries the first protocol at session-open; on connect/banner failure it falls back to the next, in order. A scalar is equivalent to a one-element list (no fallback). `default_port` applies to whichever protocol connects, unless a protocol carries its own well-known port the worker already knows (ssh 22, telnet 23) — explicit `default_port` always wins. Mirrors clan.c's runtime telnet-vs-ssh selection.

### 7.7 prompt.adapt_from_actual

A boolean flag (default `false`). The stepper itself does **nothing** with it — it is copied verbatim into the session as a read-only var (`session:get_var('adapt_from_actual')`) before `lua_module::login()` runs. NEs whose real prompt differs from `primary_regex` (e.g. CIENA_RLS) use it inside `login()` to decide whether to rewrite the working prompt regex from the actually-observed prompt, then publish the result via `session:set_var('adapted_primary_regex', ...)`. Declared here so the flag is part of the schema rather than an implicit `_defaults.yaml` convention.

## 8. Step Language

Used in `post_command` and `disconnect_steps`. **Not used for login** — login lives in `lua_module::login()` (see §9.5).

Each step is a map with exactly one `op` key.

### 8.1 Ops

| Op | Required fields | Optional fields | Effect |
|----|-----------------|-----------------|--------|
| `expect` | `regex` | `timeout_s`, `on_timeout` | Wait for regex match on output. `on_timeout` ∈ {`fail` (default), `continue`, `goto:<label>`}. |
| `send` | `text` | — | Write text to NE. Substitutions applied. |
| `expect_send` | `regex`, `send` | `timeout_s`, `on_timeout` | Combo: wait then send. Sugar for the common login pattern. |
| `branch` | `if_match`, `then` | `else` | If regex matches the last output buffer, run `then` steps; else run `else`. |
| `label` | `name` | — | Marker for `goto`. |
| `sleep` | `ms` | — | Wall-clock sleep. Use sparingly. |
| `set` | `var`, `value` | — | Set a session variable, readable by later steps and Lua. |
| `lua` | `func` | `args` | Call function in `lua_module`. `args` is a YAML list passed as a Lua table. |

### 8.2 Goto / Label Semantics

- `goto:<label>` jumps to the matching `label` step in the same block. Forward and backward jumps allowed.
- Loops must make progress; the stepper enforces a per-block step budget (default 1000) to prevent runaway scripts. Configurable per-block via `max_steps:` sibling to the block.
- Loader rejects `goto:` to an undefined label.

### 8.3 post_command Block

Runs after every user-issued command (commands proxied from a client), before the response goes to the client. Does **not** run for `send` ops issued inside `lua_module::login()`, `disconnect_steps`, or keepalive traffic. Has access to the original command via `session:get_var('last_command')`.

**Paging is automatic and implicit.** The stepper drains the `paging:` block (§7.1) on every command read on its own — matching `more_pattern`, sending `more_response`, looping until the prompt returns. Authors do **not** wire paging into `post_command`. The `post_command` block is for *additional* per-command processing that runs *after* paging is already drained: scanning for error patterns, re-detecting an adapted prompt, vendor-specific cleanup.

### 8.4 disconnect_steps Block

Runs on graceful session close. Examples: `write memory\n` + `y\n` for `ALC_1850_TSS5`, `config save\n` for `CIENA_WaveServer`. Skipped on abnormal close.

## 9. Lua Bridge

> **Execution model (2026-06-08):** `login()` runs in the main epoll loop as a
> Lua **coroutine**. `session:expect()` yields when the awaited pattern is not
> yet in the read buffer; the loop reads bytes on `EPOLLIN`, appends them, and
> resumes the coroutine — `expect` never blocks the loop. Login success →
> `CS_READY`; error / `fail()` / timeout / NE disconnect → teardown. See
> `2026-06-05-ncland-lua-login-design.md`.

### 9.1 Call Contract

```lua
function func_name(session, global, args)
  -- session: per-connection state
  -- global:  process-wide state
  -- args:    table from YAML `args:` list, or nil
end
```

### 9.2 session API

| Method | Purpose |
|--------|---------|
| `send(str)` | Write to NE. Substitutions NOT applied — Lua does its own formatting. |
| `expect(regex, timeout_s)` | Wait for match; returns matched string or nil on timeout. |
| `last_output()` | Bytes since last `expect`. |
| `get_var(name)` / `set_var(name, value)` | Session-scoped vars; shared with step language. |
| `log(msg)` | Append to daemon log at DEBUG. |
| `fail(reason)` | Abort sequence; same as `error(reason)`. |
| `ne_type`, `host`, `user` | Read-only fields. |

#### Reserved session vars

The bridge pre-populates these session vars from the NE record (frame_link / SNMP info / config) at session-open, before `login()` runs. NE authors rely on these names rather than guessing; the same names back the `%`-substitution tokens in §7.3. A var with no source value is `nil`.

| Var | Source / meaning |
|-----|------------------|
| `user`      | login user |
| `password`  | primary login password |
| `p_enable`  | enable-mode password |
| `p_read`    | read credential / community |
| `p_write`   | write credential / community |
| `card_type` | NE card / shelf type, where applicable |
| `host`      | NE hostname / IP |
| `ne_type`   | dtype enum name (the matched concrete file's `ne_type`) |
| `slot`      | NE slot |
| `neid`      | NE identifier |
| `primary_regex`   | `prompt.primary_regex` from the NE's YAML — so `login()` matches the prompt against the single source of truth instead of re-hardcoding it (added 2026-06-09). |
| `secondary_regex` | `prompt.secondary_regex` from the NE's YAML. |

Authors may `set_var` any other name freely; only the reserved set is bridge-populated. `adapt_from_actual` (§7.7) and `last_command` (§8.3) are additional bridge-set vars scoped to their features.

### 9.3 global Table

> **Updated 2026-06-08 (single-process model):** the daemon is now a single
> process with one shared Lua state and per-conn coroutines, all driven by the
> one main loop. There are no worker processes. `global` is therefore a single
> **process-wide** shared Lua table with **no lock** — the earlier per-worker /
> mutexed-proxy description below is obsolete. See
> `2026-06-05-ncland-lua-login-design.md` §2/§9.3.

- Process-wide scope. Persists for the daemon's lifetime.
- Loader initializes empty.
- Shared across every Lua call from every NE type and every connection.
- Plain shared table — no proxy, no mutex (the single main-loop thread is the
  only code that ever touches Lua, so concurrent access cannot occur).
- Intended uses: cross-NE registries, learned per-NE topology, rate limiters,
  vendor-wide caches.
- Not for: anything that must survive a daemon restart (no persistence).

### 9.4 Step-Language Scope

Step-language `set` and `branch` see only `session` vars. `global` is Lua-only by design — keeps cross-NE side effects visible and grep-able in `.lua` files instead of buried in YAML.

### 9.5 Required Entry Points

Every NE's `lua_module` must define:

```lua
-- Required. Drive the NE from raw transport through to a steady-state
-- prompt match. Called after the transport (telnet / ssh) is connected
-- but before the stepper takes over. Must leave the session sitting at
-- the CLI prompt described by ne/<name>.yaml's prompt.primary_regex.
function M.login(session, global, args)
  ...
end
```

Optional helper hooks (rarely needed since post_command / disconnect_steps cover the common cases):

```lua
function M.post_command_hook(session, global, args) end   -- runs after the YAML post_command block, if defined
function M.disconnect_hook(session, global, args)   end   -- runs after the YAML disconnect_steps block, if defined
```

Loader rejects an NE whose `lua_module` does not export `login`. `post_command_hook` / `disconnect_hook` are optional; loader does not require them.

The bridge contract (§9.1) and APIs (§9.2 / §9.3) apply identically to `login()` and any other Lua function.

## 10. Loader & Runtime

### 10.1 Warehouse Startup Sequence

1. Parse CLI flags (`-w <workers>`, `-c <ctl_endpoint>`, `-f <ncland.json allowlist>`, `-l <log_level>`). The NE-config directory is **not** a flag — it is hardcoded to `NCLAND_NE_DIR` = `/usr/cnc/lib/data/ncland` (the install dir that also holds `ncland.json` and `ctl.sock`).
2. Install `signalfd` for `SIGTERM` + `SIGHUP`; register in epoll.
3. Scan `ne/*.yaml`. First pass: parse every file's raw YAML and index by basename (so `extends:` targets resolve). Detect `extends:` cycles and missing parents now — drop offending leaves with a WARN.
4. Resolve each file. For every file:
   a. Skip if `abstract: true` — it is a template, not a registered NE (but it remains available as an `extends:` target).
   b. Build the merge chain: `_defaults.yaml` → ancestors (root first, via `extends:`) → this file. Maps deep-merge with more-derived keys winning; lists replace wholesale (§7.5).
   c. Validate the merged result against `schema/ne_schema.json` and the required-key set (§7.2).
   d. Compile every regex in `prompt`, `paging`, `errors`, and step `expect`/`branch`.
   e. `lua_module:` is required on the merged file. Verify `lua/<file>.lua` exists, `luac`-compiles cleanly, and exports `login`. Reject the NE type if any check fails.
5. Build the in-memory registry `(ne_type → entry)`. Abstract files never appear here.
6. Bind ZMQ ROUTER on the configured endpoint (§5); obtain `ZMQ_FD` and register in epoll.
7. Connect a ZMQ **SUB** socket to the nfdb XPUB endpoint (`ipc:///usr/cnc/data/nfdb_pub.sock`), subscribe to the `app.ne.` and `db.ne.` topic prefixes, obtain its `ZMQ_FD` and register in epoll (edge-triggered). Non-fatal if connect fails (degraded: no seed, no runtime lifecycle — §11.3).
8. Fork `N` worker processes; each is connected via a `socketpair` for `ctl_sock`. Workers inherit the YAML registry (read-only after fork) and create their own per-worker Lua state.
9. **Seed:** with the SUB subscription established, `fork`/`exec` `nclan-seed` (§11.5). It reads `frame_link` and publishes one `app.ne.seed` event per NE to the nfdb bus. The warehouse receives those events on its SUB fd in the main loop and opens sessions for registry-owned dtypes (handing each NE fd to a worker via `SCM_RIGHTS`) — identical code path to runtime `db.ne.add` (§11). The warehouse itself never touches `frame_link`.
10. Enter the main epoll loop.
11. Log a one-line summary: `loaded=N types, workers=M, nfdb=<ok|degraded>, errors=[...]` (seeded-NE count is logged later, as seed events arrive asynchronously).

### 10.2 Worker Startup Sequence

1. Inherit YAML registry and the `ctl_sock` fd from parent.
2. Initialize the Lua state per `lua_module:` (one VM per worker, not per session).
3. Register `ctl_sock` in own epoll loop (edge-triggered).
4. Loop: drain `ctl_sock` frames (`F`, `C`, `X` tags), service NE I/O, write `R`-tagged responses back.

### 10.3 Session Open

1. Warehouse receives client `cmd_msg` over ROUTER. Stashes caller identity by `seq`.
2. Looks up `dtype` in registry. Hit → assign to a worker (least-loaded). Miss → reject with error (legacy path handles its own dtypes; clan-rewrite doesn't bounce-route).
3. Sends `'C'`-tagged frame over the worker's `ctl_sock`.
4. Worker reads frame, opens transport per `transport:`, calls `lua_module::login()` to drive the NE to its steady-state prompt, then runs the actual command (with `post_command` step block draining paging / errors after), returns `'R'`-tagged frame.
5. Warehouse reads `'R'`, looks up stashed identity by `seq`, sends `[identity][empty][rsp]` back over ROUTER.

### 10.4 Hot Reload (Phase 2)

`SIGHUP` rescans `ne/` and `lua/`, builds a new registry, swaps atomically in the warehouse. Workers either re-fork or receive a new registry over `ctl_sock` (TBD during plan). Existing sessions keep their old definitions until close. Off by default; behind a config flag. Out of scope for phase 1.

## 11. NE-Lifecycle Eventing (nfdb ZMQ bus)

clan-rewrite has a **single** NE-lifecycle ingestion path: a ZMQ **SUB** socket connected to the **nfdb event bus**. nfdb is the ctree-backed DB + pub/sub broker that `niimxd` owns (`cnc/niimx/src/`); it is the concrete realization of the ZMQ pub/sub migration noted in memory `project_nfdb_event_bus` / `project_rdb_notify_zmq`. **Prerequisite:** the nfdb merge into niimxd is currently stashed (`~/Git/niimx-nfdb-merge-stashed`), not yet pushed — none of the eventing below functions until it lands and `niimxd` binds the nfdb sockets.

Bus endpoints (defaults, `nfdb_proto.h`):

- **XPUB** `ipc:///usr/cnc/data/nfdb_pub.sock` — consumers connect a ZMQ **SUB** here and filter by topic prefix.
- **XSUB** `ipc:///usr/cnc/data/nfdb_sub.sock` — publishers connect a ZMQ **PUB** here.
- **ROUTER** `ipc:///usr/cnc/data/nfdb.sock` — DB command interface (incl. `PUBLISH`), reachable via `libnfdb`.

Every event is two ZMQ frames: `[topic][yaml_body]`. Two producers feed clan-rewrite's SUB socket:

- **`nclan-seed`** (§11.5) — at startup, reads `frame_link` and publishes one **`app.ne.seed`** event per NE. (Clients may only publish `app.*`; the `db.*` prefix is reserved to the nfdb DB layer.)
- **nfdb `ne`-DB layer** — runtime adds/removes/changes to the `ne` database auto-publish **`db.ne.add` / `db.ne.update` / `db.ne.delete`** (`nfdb_cmd.cpp`). Whatever process mutates the `ne` DB (e.g. `otn_portd`, once it writes there) is the runtime source; clan-rewrite is decoupled from it.

clan-rewrite subscribes to both `app.ne.` and `db.ne.` prefixes and maps each topic to a lifecycle action:

| Topic | Behavior in clan-rewrite |
|-------|--------------------------|
| `app.ne.seed`  | Seed event. If `dtype` is in the YAML registry: open / prepare a session for the neid. **Idempotent** — already-open neid is a no-op (covers re-runs / duplicate seeders). |
| `db.ne.add`    | Same as seed (a new NE appeared at runtime). |
| `db.ne.update` | If the identity changed materially (dtype / ip), close + reopen against the new values; if the new dtype is unowned, close; if a previously-unowned NE became owned, open. Otherwise no-op. |
| `db.ne.delete` | Close any open session for that neid. |

(`enable`/`disable` semantics fold into add/update/delete on this bus; there is no separate kind.)

### 11.1 Event Payload

Bodies are **YAML, identity only** — no credentials. The seed and DB events carry at least `neid`, `dtype` (the numeric `dcs_type[0]` value), `ip`, `tid`, and `slot`. The **consumer** maps the numeric `dtype` to a registry `ne_type`/alias (clan-rewrite's registry already holds that value↔name correspondence — §13.1 round-trip), so the producer need not know enum names. Workers fetch NE credentials at session-open from their own source (RDB / per-neid `frame_link` accessor / secure store — §10.3, exact source resolved in the plan) and populate the §9.2 reserved session vars. Secrets never cross the bus.

`nclan-seed` emits flow-style single-line YAML so it survives the `PUBLISH` command path (which rejoins body tokens with single spaces, `nfdb_cmd.cpp` `cmd_publish`): e.g. `app.ne.seed` → `{neid: 5, dtype: 245, ip: 10.0.0.1, tid: NODE5, slot: 12}`.

### 11.2 Wiring

- The SUB socket's `ZMQ_FD` is registered in the warehouse epoll loop alongside the client `ZMQ_FD` and the `signalfd`. `ZMQ_FD` is edge-triggered — the drain loop must `zmq_recv(…ZMQ_DONTWAIT)` until `EAGAIN` and re-check `ZMQ_EVENTS` (same discipline as §5.3).
- Each event is two frames `[topic][yaml]`; parse the YAML into the identity tuple, dispatch through the topic table. Open/close actions go through the warehouse's worker-assignment machinery — the same path seed events use (seed and runtime share one dispatch implementation; there is no separate seed code path).

### 11.3 Failure Handling

- SUB connect failure is **non-fatal**. Logs WARN; clan-rewrite runs degraded — never seeded, sees no lifecycle changes, serves no NEs until the bus is reachable and `nclan-seed` is re-run. (Without the subscription the warehouse also skips the §10.1 step-9 `fork`/`exec` of `nclan-seed` — nothing would receive its events.)
- YAML parse failure on a single event → WARN, drop that event, keep draining.
- Repeated parse failures (≥ 100 in a 60-second window) → WARN, suspend the subscription, log a high-severity event for ops.

### 11.4 Build Dependencies

clan-rewrite's subscriber needs only `-lzmq` (a plain SUB socket + YAML parse). `nclan-seed` links `-lzmq` and **`libnfdb`** (`nfdb_connect` / `nfdb_command`, `nfdb_clientlib.cpp`) to issue `PUBLISH`, plus libsdi for `atch_frm`/`atch_aal`. No CORBA, no `-lrdb_notify`.

### 11.5 nclan-seed Tool

A standalone command-line binary — the **only** reader of `frame_link`.

1. `atch_aal(0)` then `atch_frm(&start)` to attach the SDI shm and obtain the `frame_link` array (`include/d1compool.h:2239`; extern `Frmlnk`).
2. Iterate `neid` from 1 to `NeAvail-1` (the bound used by the `IS_*` macros, `d1compool.h:2236`). **`neid` is the array index**, not a stored field. Per record extract identity: `dtype` = `Frmlnk[neid].dcs_type[0]` (numeric), `ip` = `getNeIp(neid)`, `tid` = `getTid(neid)` (`misc_util.h:181-182` — clean accessors that hide the type-dependent `dkpath`/`sys_name` layout), `slot`/frame from `nfrmnum`. Skip empty/inactive slots (e.g. `dcs_type[0] == 0` / no TID) using the same test existing frame_link iterators use.
3. For each kept NE, `PUBLISH app.ne.seed "<flow-yaml>"` via `libnfdb` against `nfdb.sock` (the server relays it onto XPUB; the `PUBLISH` ack confirms delivery to the broker, sidestepping publisher-side slow-joiner).
4. Exit. One-shot, idempotent, re-runnable; consumers dedup by neid (already-open = no-op).

CLI surface: `nclan-seed [-e <nfdb.sock>] [--dtype <name>...] [--dry-run]`. `--dtype` restricts emission to specific dtypes (limits noise); `--dry-run` prints the events it would publish without sending. The warehouse spawns it with no dtype filter — every consumer (clan-rewrite, ncland, …) decides for itself which dtypes it owns. `nclan-seed` lives in the clan-rewrite source tree but is system-wide infrastructure: any subscriber is seeded by one run.

## 12. Error Handling

### 12.1 Load-Time Errors

All produce one WARN log line with file path, JSON-pointer (or step index), and reason. The NE type is skipped; daemon stays up. Categories:

- YAML parse error.
- Schema violation (unknown key, wrong type, missing required including `lua_module`).
- Regex compile error.
- Step `goto:` to undefined label.
- `lua_module:` missing or its file missing.
- Lua syntax error (`luaL_loadfile` fails).
- Lua module does not export required `login` function.
- Duplicate `ne_type:` across concrete files.
- `extends:` names a nonexistent parent.
- `extends:` chain forms a cycle (every file on the cycle skipped).

Schema/regex/`lua_module` checks apply to the **merged** concrete file (§7.5); a base or thin variant is never validated standalone.

Transport-level load errors (ZMQ bind failure, ZMQ_FD not obtainable, worker `socketpair` failure) are fatal — clan-rewrite cannot serve clients without its ROUTER socket and workers. Notify subscribe failure is non-fatal (see §11.3).

### 12.2 Runtime Errors

| Condition | Behavior |
|-----------|----------|
| `expect` timeout, `on_timeout: fail` | Close session; return error to client in legacy format. |
| `send` write fails | Same. |
| Lua `error()` / `session:fail()` | Close session; reason in daemon log. |
| Lua uncaught exception | Caught at C boundary; treat as fail; stack trace in daemon log only (not leaked). |
| Output buffer exceeds `max_buffer_bytes` | Fail with `buffer_full`. |
| Step block exceeds `max_steps` budget | Fail with `step_budget_exceeded`. |
| ZMQ pending-call table full | WARN; reject incoming cmd with `busy`; do not block ROUTER. |
| Worker died unexpectedly | Warehouse marks its NEs as orphaned, respawns the worker, re-seeds the worker's NE partition. Sessions in flight on the dead worker fail with `worker_lost`. |
| Notify parse error | See §11.3. |

### 12.3 Logging

- DEBUG: every step — `[ne=X host=Y step=N op=Z ...]` with elapsed ms.
- DEBUG: every Lua call — function name, duration.
- DEBUG: every ZMQ recv/send — frame size, identity hash, seq, elapsed.
- DEBUG: every `ctl_sock` frame — tag, payload size, worker idx.
- DEBUG: every notify event — kind, neid, dtype, action taken.
- WARN: load-time errors, notify suspensions, pending-table overflows, worker deaths.
- INFO: runtime fails (matches legacy noise level).

**Secret redaction:** the stepper must redact `%p` (password) and any substring matching `%p`'s runtime value from `send` payloads and Lua `log()` output before emission to any sink. Same rule as legacy `clan.c` — passwords never appear in DEBUG traces.

### 12.4 Schema Versioning

`schema_version: 1` required. Loader rejects unknown versions. Incompatible schema change → bump to 2 and ship a converter. v1 files keep loading until manually updated.

## 13. Test Plan

### 13.1 Static (CI)

- Every concrete `ne/*.yaml`, **after merge** (`_defaults` → `extends:` ancestors → self), schema-validates + every regex compiles + `lua_module` exists + Lua `luac`-compiles. Abstract files (`abstract: true`) are checked for parse + cycle-free `extends:` only, not for required NE keys.
- Round-trip: every concrete file's `ne_type:` matches an entry in the `dtypes[]` display-name table (`include/dtype.ext`) — i.e. `ncland_registry_dtype_for_name(ne_type) >= 0`. Abstract files exempt.
- Inheritance integrity: every `extends:` target resolves to an existing file; no `extends:` cycles; no concrete file left missing a required key after merge.
- Reference: snapshot today's per-NE constants from `clan.c` into a baseline JSON; diff vs generated YAML on every PR.
- `grep` enforcement: no remaining `mq_open`/`mq_send`/`<mqueue.h>`/`mqd_t` references inside `cnc/sdi/clan2/src/` (mirrors ncland's static check).

### 13.2 Replay

- Instrument `clan.c` to log every read/write byte per session to `traces/<ne_type>/<session-id>.cap`. Build corpus over ~2 weeks from live ops.
- Replay harness feeds `*.cap` to `clan-rewrite` via a pty mock. Asserts same bytes, same order, same final prompt. Regex-tolerant where timing differs.
- Coverage gate: an NE type cannot graduate to shadow mode until ≥ 20 traces replay clean.

### 13.3 Transport Tests

Borrowed from `~/ncland-zeromq.md` Tasks 2–4:

- ROUTER bind + ZMQ_FD obtainable.
- `recv_cmd` / `send_rsp` round-trip via DEALER client.
- Drain loop handles N queued frames before returning.
- Pending-call stash + take round-trip; table-full rejection.

### 13.4 Worker IPC Tests

Borrowed from `~/ncland-zeromq.md` Tasks 6–8:

- Warehouse forwards `'C'`-tagged cmd over `ctl_sock` to worker.
- Worker reads `'C'` frame, dispatches.
- Worker writes `'R'`-tagged rsp; warehouse forwards to caller via ROUTER.
- `'X'` shutdown frame causes worker to exit cleanly.
- SCM_RIGHTS fd handoff round-trip.

### 13.5 Eventing Tests

- `app.ne.seed` / `db.ne.add` for a registry-owned dtype → open action; for an unowned dtype → dropped, no side effect.
- `db.ne.delete` → close action for the neid.
- `db.ne.update` with changed ip/dtype → close+reopen; unchanged → no-op.
- Malformed topic / unparseable YAML body → dropped, no crash, keeps draining.
- Duplicate `app.ne.seed` for an already-open neid is a no-op (idempotency).
- Two-frame `[topic][yaml]` parse: topic prefix match drives dispatch; YAML identity tuple extracted correctly.
- Subscribe-before-spawn ordering: warehouse does not `fork`/`exec` `nclan-seed` until the SUB subscription is established.

### 13.5a nclan-seed Tests

- Given a mock/test `frame_link` of N kept NE records, `nclan-seed` issues exactly N `PUBLISH app.ne.seed` commands with correct `{neid, dtype, ip, tid, slot}` bodies (assert via a stub `libnfdb`/loopback subscriber).
- Emitted bodies carry **no** credential fields (assert no `password`/`login` substring).
- Empty/inactive frame_link slots are skipped (count matches kept records, not array size).
- `--dtype` filter restricts emission to the named dtypes.
- `--dry-run` publishes nothing (prints only).
- dtype enum value is rendered as its **name** (matches a registry `ne_type`).

### 13.6 Live Shadow Mode

- Config `shadow_dtypes:` list. For NE types in the list, every client command request is handled by `clan.c` **and** mirrored to `clan-rewrite` (keepalives are not mirrored; each backend runs its own). Legacy response goes to the client; both client-visible responses are logged and diffed offline.
- `clan-rewrite` errors in shadow mode do not affect users.
- Daily divergence report. NE type stays in shadow until zero divergence for 7 consecutive days.

### 13.7 Promotion

- Move NE type from `shadow_dtypes:` to `primary_dtypes:` (single line config change).
- Rollback: revert the line.

### 13.8 New-NE Acceptance

NE types added directly to YAML (no legacy implementation) require:
- Full trace set from a lab unit.
- Replay-clean against those traces.
- 48 h shadow against the lab unit.

### 13.9 Cutover State Machine

```
legacy-only → shadow → primary → legacy-removed
```

Last step is a per-NE cleanup PR that deletes the C code from `clan.c`. Not rushed — rollback option stays open until each NE type is confidently stable in `primary`.

## 14. Open Questions

To resolve during implementation planning:

1. **Directory location** — RESOLVED 2026-06-04: the rewrite **is** `ncland` (New CLAN Daemon), living in `cnc/ncland/src`, which already holds the warehouse/worker/conn/ssh/telnet scaffolding, the `ne/*.yaml` + `lua/*.lua` artifacts, the `nfunit-test.hpp` harness, and `ncland.mk`. New work (loader, Lua bridge, ZMQ swap, nfdb eventing, `nclan-seed`) lands there and reuses that scaffolding. The `cnc/sdi/clan2/` paths in §6 describe the *source* layout (aspirational naming; real source home is `cnc/ncland/src`). At **runtime**, the YAML loader reads NE config from a hardcoded install dir — `NCLAND_NE_DIR` = `/usr/cnc/lib/data/ncland` (`*.yaml` + companion `*.lua` directly in that dir, alongside `ncland.json`). No ne-dir flag. (YAML loader/Lua bridge still to build.)
2. **Lua runtime choice** — LuaJIT vs PUC-Rio Lua 5.4. Pick during plan; both fit the bridge API.
3. **YAML parser choice** — libyaml (C) vs yaml-cpp (C++). libyaml is leaner and avoids an extra dep; yaml-cpp is more ergonomic. Pick during plan.
4. **Client → endpoint discovery** — clients need to know which ROUTER endpoint owns which dtype (clan.c legacy IPC vs clan-rewrite ROUTER vs ncland ROUTER). User opted **not** to introduce an ownership-manifest file (analogue of ncland.json) for clan-rewrite in this revision. Therefore the legacy clan.c dispatcher must consult the YAML registry's `ne_type:` set at startup — either by reading the YAML files itself, by making a side-channel call into clan-rewrite, or by linking against a shared "registered dtypes" library. Pick during plan. Reopening the manifest decision is a valid outcome.
5. **nfdb merge is a prerequisite** — the nfdb bus (§11) is stashed in `~/Git/niimx-nfdb-merge-stashed`, not pushed. clan-rewrite eventing cannot be integration-tested until that merge lands and `niimxd` binds `nfdb_pub.sock`/`nfdb_sub.sock`. Confirm landing timeline; until then, only `nclan-seed` and the subscribe/parse/dispatch module are unit-testable (against a stub/loopback).
6. **Runtime `db.ne.*` producer + event YAML schema** — confirm which process writes the nfdb `ne` DB at runtime (e.g. `otn_portd`) and the exact field set / key names the auto-published `db.ne.{add,update,delete}` YAML carries, so `nclan-seed`'s `app.ne.seed` body uses the **same** field names and clan-rewrite parses one schema for both. Also confirm the `ne` DB schema via `nfdb DESC ne`.
7. **dtype name mapping + ip/tid extraction** — `nclan-seed` must render `dcs_type[0]` as its enum **name** (matching a registry `ne_type`) and pull `ip`/`tid` out of the type-overloaded `dkpath`/`sys_name` fields. Find the existing helper(s) that map dtype→name and that return a clean IP/TID per neid; reuse rather than re-derive the per-type layout.
8. **Worker credential source** — where workers fetch NE credentials at session-open (RDB query by neid, a single-record `frame_link` accessor, or a secure store), since events are identity-only (§11.1). Confirm the per-neid accessor exists and is safe to call from a worker.
9. **RHEL 7 baseline divergence** — CLAUDE.md establishes gcc 4.8.5 / RHEL 7 as the project-wide minimum. clan-rewrite (matching ncland) requires C++17 and gcc 8.5+. Confirm with the team that RHEL 7 systems continue on legacy `clan.c`. If RHEL 7 support is required for clan-rewrite, the language choice must drop to C++11 or C, and several tech-stack choices revisit.
10. **Vendor-scoped defaults** — should `_defaults.yaml` be vendor-scoped (e.g., `_defaults_ciena.yaml`) as well as global? Defer until ≥ 3 NE types per vendor exist.
11. **Concurrent `atch_aal` / `atch_frm` access** — `nclan-seed` (§11.5) attaches to the same shm `m_clan` attaches to. Now scoped to a short-lived one-shot tool, not the daemon. Confirm with the SDI maintainer; if not safe, `nclan-seed` proxies through `m_clan` or reads RDB directly. Same open item as `ncland-zeromq.md` Open Item 2.
12. **Declarative branch-on-session-var (`op: branch_var`)** — deferred (GAPS Gap 3). The step language `branch` op matches only the output buffer; card-type-gated disconnect logic (TSS5 write-memory, optional Cisco writemem) stays in Lua via `op: lua`. Revisit only if enough NEs want fully-declarative card-type-gated disconnect to justify extending `branch` with `if_var:` / `matches:`. No payoff beyond ergonomics today.

## 15. References

- `cnc/sdi/src/clan.c` — current implementation.
- `~/Git/niimx-nfdb-merge-stashed/` — stashed nfdb-into-niimxd merge (design doc `2026-05-15-niimxd-nfdb-merge.md`, `nfdb_cmd.cpp/.h`, `nfdb_proto.cpp/.h`, `nfdb_clientlib.cpp`). Defines the bus: XPUB `nfdb_pub.sock`, XSUB `nfdb_sub.sock`, ROUTER `nfdb.sock`; `[topic][yaml]` framing; `db.<db>.{add,update,delete}` auto-events; reserved `db.` prefix; `PUBLISH`/`SCAN`/etc. **Prerequisite for §11.**
- `cnc/niimx/src/niimx.cpp` — reference ZMQ client (DEALER↔ROUTER framing pattern).
- `include/d1compool.h:1158` — `frame_link` struct (neid = array index; `dcs_type[0]`, `dkpath`, `sys_name`, `login`, `nepasswd_cur`, …); `atch_frm`/`Frmlnk` at `:2239`, `atch_aal` at `include/utillibinc.h:418` — what `nclan-seed` reads (§11.5).
- Memory: `project_nfdb_event_bus` — endpoints, topics, reserved-prefix details for the nfdb bus.
- `docs/superpowers/specs/2026-05-04-ncland-zeromq-seeding-design.md` — sibling spec for ncland's ZMQ + seeding rewrite. Source of the ROUTER transport pattern and rdb_notify wiring.
- `~/ncland-zeromq.md` — full 17-task TDD implementation plan for ncland. Tasks 2–4 (ZMQ wrapper), 5 (pending stash), 6–8 (worker ctl_sock framing + SCM_RIGHTS), 13–14 (notify), and 16 (m_clan ownership hook) are the direct references for §4, §5, §10, and §11.
- Memory: `project_sdi_one_gne_per_process` — per-GNE static state is safe (relevant for `global` table scope).
- Memory: `project_rdb_notify_zmq` — the ZMQ pub/sub migration; realized as the nfdb bus (§11). clan-rewrite consumes the nfdb SUB socket, not librdb_notify directly.
- Memory: `feedback_use_nmake` — build system uses Lucent/AT&T nmake.
