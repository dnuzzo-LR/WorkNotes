# clansimctl — CLI Client for clansimd

Companion command-line tool for the `clansimd` NE-simulator daemon (spec:
`2026-07-23-clansimd-simulator-design.md`). Speaks the daemon's zmq REQ/REP
JSON protocol so operators and scripts can start, stop, list, inspect, reset,
reload, and inject canned replies without hand-rolling JSON payloads.

## Goals

* One binary, `clansimctl`, installed alongside `clansimd` in `$(PBIN)`.
* Every daemon verb has a subcommand.
* Human-readable output by default; `--json` emits the raw daemon reply for
  scripting.
* Small: single `.cpp`, no new headers, no new library dependencies (reuses
  the `-lzmq` and header-only `nlohmann/json` already linked into
  `cnc/ncland/src`).

## Non-goals

* No config file, no daemonization, no persistent state.
* No interactive REPL — subcommand-per-invocation only.
* No end-to-end daemon integration test in v1; unit tests cover pure
  request-building and formatting.

## CLI Surface

```
clansimctl [--ctl ENDPOINT] [--timeout SECS] [--json] <subcommand> [args]
```

### Global flags

| Flag | Default | Purpose |
| --- | --- | --- |
| `--ctl ENDPOINT` | `$CLANSIMD_CTL` or `ipc:///tmp/clansimd.ctl` | zmq REQ endpoint (any zmq string). |
| `--timeout SECS` | `5` | REQ send/recv timeout. `0` = wait forever. |
| `--json` | off | Print the raw JSON reply on stdout instead of formatted output. |
| `-h`, `--help` | — | Usage. |

Flag wins over env var.

### Subcommands

| Command | Args | Daemon verb | Human output on success |
| --- | --- | --- | --- |
| `start` | `--dtype N` (or `--script PATH`), `--neid N`, `--tid STR`, `--port N` (default 0) | `START` | `id=<n> port=<n>` |
| `stop` | `<id>` | `STOP` | `ok` |
| `list` | — | `LIST` | Aligned table (see below) |
| `status` | `<id>` | `STATUS` | Key/value block |
| `reset` | `<id>` | `RESET` | `ok` |
| `reload` | `<id>` | `RELOAD` | `ok` |
| `inject` | `<id> --cmd STR` (`--reply STR` \| `--reply-file PATH`) | `INJECT` | `ok` |

`--json` on any command prints the entire daemon reply verbatim
(`{"ok":..,"req_id":..,"result":..}`) — no filtering.

### `list` table

```
ID  DTYPE  NEID  TID   PORT   STATUS
1   999    7     NE7   20000  connected
2   103    88    NE88  20001  listening
```

Columns are left-aligned, padded to the widest cell in that column. Statuses
map from the daemon's `csim_status_t` enum:

| Value | Label |
| --- | --- |
| 0 | `idle` |
| 1 | `listening` |
| 2 | `connected` |
| 3 | `err` |

Unknown values print numerically (`?<n>`).

### `status` output

Human mode prints one `key=value` per line, in field order returned by the
daemon (`id, status, port, bytes_in, bytes_out, last_cmd`). Status is decoded
using the same label table as `list`.

### Exit codes

| Code | Meaning |
| --- | --- |
| 0 | Success (`reply.ok == true`). |
| 1 | Daemon returned `ok:false`, or transport error (bind/connect/parse). Error text printed to stderr. |
| 2 | Usage error (bad flag, missing required arg). |
| 3 | `--timeout` expired waiting for reply. |

## Internal Structure

Single file `cnc/ncland/src/clansimctl.cpp`. No new headers.

```
main(argc, argv)
 ├── parse_globals(argc, argv)              // strips --ctl/--timeout/--json/-h
 ├── if remaining[0] missing → usage, exit 2
 └── dispatch on remaining[0]:
       "start"  → cmd_start(remaining)
       "stop"   → cmd_stop(remaining)
       "list"   → cmd_list()
       "status" → cmd_status(remaining)
       "reset"  → cmd_reset(remaining)
       "reload" → cmd_reload(remaining)
       "inject" → cmd_inject(remaining)
       default  → usage, exit 2
```

Each `cmd_*`:

1. Parses its own args (returns exit 2 on misuse).
2. Builds `nlohmann::json` request envelope:
   `{"v":1, "cmd":"<VERB>", "req_id":"<pid.counter>", "args":{...}}`.
3. Calls `rpc_call(endpoint, timeout, envelope)`.
4. If `!reply["ok"]` → print `reply["error"]` to stderr, exit 1.
5. If `--json` → print reply verbatim; else format `reply["result"]`.

### Helpers

* `rpc_call(endpoint, timeout_ms, request_json) → reply_json`
  Creates a fresh `zmq::context_t` and `REQ` socket per call. Sets
  `ZMQ_SNDTIMEO`, `ZMQ_RCVTIMEO`, and `ZMQ_LINGER=0`. Connects, sends,
  receives. On `EAGAIN` → exit 3. On any other zmq/parse failure → exit 1.
* `req_id_gen()` — returns `"<pid>.<counter++>"`. No RNG needed.
* `read_reply_file(path)` — slurps a file into a string for `inject --reply-file`.
* `format_list_table(result_json)` — computes column widths, prints header + rows.
* `format_status_block(result_json)` — prints `key=value` lines.
* `status_label(int)` — enum → string per the table above.

### Argument parsing

Handwritten. No new library. Long flags only (matches `clansimd` style).
Positional `<id>` accepted for `stop/status/reset/reload/inject`. `start`
uses only long flags (no positional).

## Build Wiring

Add to `cnc/ncland/src/Makefile`:

```make
.MAIN : $(PBIN)/ncland $(PBIN)/nclan-seed $(PBIN)/clansimd $(PBIN)/clansimctl

$(PBIN)/clansimctl :: clansimctl.o $(INCLIBS) -latm -linc -lxmlparse -lssh -ljson -lnetclan
    $(CPLUS_CC) $(CCFLAGS:M!=-AC99) $(LDFLAGS) -o $(<) $(*) -lpthread -lzmq -lrt
```

Rationale:

* No `$(CSIM_OBJS)` link — the CLI constructs its request JSON directly; it
  does not need `csim_control.o`.
* `%.o : %.cpp` implicit rule (present at top of Makefile) already compiles
  with `-std=c++17`.
* Install falls under whatever depot rule sweeps `$(PBIN)` (same route as
  `clansimd`).

Build order matches convention: bare `nmake` builds all four `.MAIN` targets.

## Testing

New file `cnc/ncland/src/clansimctl_tests.cpp` added to the
`ncland_unit_tests` link line. Tests are pure-function focused (no zmq
round-trip in unit tests):

* `parse_globals` — strips flags, honors `$CLANSIMD_CTL`, `--ctl` wins.
* `build_start_req` — asserts JSON shape for `dtype`/`script`/`port` combinations.
* `build_inject_req` — asserts reply loaded from `--reply-file`.
* `format_list_table` — asserts column widths + status labels.
* `format_status_block` — asserts `key=value` ordering.
* `status_label` — asserts enum → string mapping incl. unknown fallback.

End-to-end coverage against a live daemon is deferred; the existing
`CsimDaemon` suite already exercises the daemon dispatch path via
`csim_daemon_dispatch`. A future test that forks `clansimd` and shells out to
`clansimctl` can be added when there's a second consumer of the client path.

## Docs

Append a **`Client`** section to `cnc/ncland/src/CLANSIMD.md` mirroring the
existing `Example session` JSON block, using `clansimctl` invocations:

```sh
clansimctl start  --dtype 999 --neid 7 --tid NE7
clansimctl list
clansimctl inject 1 --cmd 'PING' --reply 'PONG'
clansimctl stop   1
```

## Open Questions / Deferred

* Bash completion — nice-to-have, not v1.
* Batch mode (`clansimctl < script.txt`) — not v1; scripts can loop.
* `wait-listening` helper (poll `status` until `port` open) — punt until a
  consumer asks.
