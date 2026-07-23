# clansimd — Standalone Lua NE-Simulator Daemon: Design Spec

**Date:** 2026-07-23
**Branch:** `ncland-simulator`
**Location:** `cnc/ncland/src`
**Status:** Design approved; ready for implementation planning.

---

## 1. Motivation

Today, when `clan` connects to a simulated network element (target IP is
`clansim`/`xlansim`/`localhost`), it **forks one simulator subprocess per
connection** (`cnc/sdi/src/clan.c` ~L1582–1624). The simulator can be either:

- the hardcoded-C `clansim` (`cnc/sdi/src/clansim.c`), **or**
- a per-NE / per-dtype **lua script** run by `/usr/cnc/bin/ilua`
  (`/usr/cnc/data/ne<neid>.clansim`, `/usr/cnc/lib/clan/<dtype>.clansim`,
  `def.<dtype>.clansim`).

The lua path is the model to follow. Each `.clansim` file is a tiny lua wrapper:

```lua
#!/usr/cnc/bin/ilua
require('clansim')                       -- /usr/cnc/lua/clansim.lua (shared)
clansim.default_sim(arg,{'password:','prompt00>','prompt00#'})
```

`clansim.lua` (`3b2/data/clansim.lua`) walks a `prompts[]` login state machine
over **stdio** (`io.read`/`io.write`), then for each command line looks up a
response via `clansim.read_file()` — **canned files** in a directory tree
(`$SIMDATA/lansim/<neid>/<tid>/CLI/<command>`, spaces→underscores), with an
optional inline `patterns` table.

**Problems with the current model:** one process per connection, no dynamic
control, awkward to unit-test, and the hardcoded-C `clansim` (`lansim`-style)
is a dead end we explicitly do **not** want to extend.

**Goal:** a single **standalone daemon** that hosts **all** simulators, driven
by the *same* lua scripts and directory structure, controllable at runtime over
a **zmq req/rsp** socket, and built for deterministic unit + integration
testing.

Explicitly **not** following the `lansim`/hardcoded-C canned-response model.

---

## 2. Confirmed Decisions

| Decision | Choice |
|---|---|
| Client transport to a sim | **TCP port per sim** — daemon binds a listening port per running sim; client connects to `host:port` like today's `clansim`. |
| Lua reuse | **Verbatim** — run existing `.clansim` + `clansim.lua` + canned-file tree unchanged; daemon provides a compatible `inc.*` shim + stdio↔socket bridge. |
| Control API | **Rich** — START / STOP / LIST / STATUS / RESET / RELOAD / INJECT. |
| Test goal | **Both** — daemon as an integration fixture for clan/ncland, *and* unit-testing the lua device models in isolation. |
| Execution model | **Single-threaded epoll + lua coroutines.** One event loop; each sim connection is a coroutine whose `io.read` yields on an empty socket. |
| JSON | Reuse the `-ljson` C library ncland already links. |
| Control endpoint | Default `ipc:///tmp/clansimd.ctl`, overridable via `--ctl <endpoint>`. No auth in v1 (local ipc). |
| clan integration | **Deferred to phase 2.** v1 = daemon is standalone and test-driven; making `clan` connect to the daemon instead of forking `.clansim` comes later. |
| Connections per sim | **One active connection per sim** (reconnect reuses the sim's `lua_State` so device state persists); extra concurrent clients rejected — mirrors `clansim`'s `listen(1)`. |
| INJECT mechanism | **Per-sim overlay directory** used as that sim's `SIMDATA`; INJECT writes the reply as the canned file `read_file()` already searches for. **Zero change to `clansim.lua`.** |

---

## 3. Component

- **Binary:** `clansimd` (clan simulator daemon).
- **Language/toolchain:** C++17, g++ 8.5 (RHEL 8 baseline), also builds on g++ 11.x.
- **Symbol prefix:** `csim_`; macros `CSIM_`.
- **Reused infra:** `nflog.hpp` (logging), `nfunit-test.hpp` (unit tests), the
  ncland epoll idiom, and the ncland lua coroutine-drive pattern
  (`ncland_lua_coroutine_drive`).
- **Link line:** `-llua -lzmq -ljson -lrt -lpthread` (already used by ncland).

---

## 4. Architecture — single-threaded epoll

One event loop owns every fd; no threads, no locks:

```
epoll_wait:
  zmq REP fd      -> control: START/STOP/LIST/STATUS/RESET/RELOAD/INJECT
  listener fd (N) -> accept -> new connection coroutine in that sim's lua_State
  conn fd (M)     -> read bytes -> lua_resume(coroutine)
```

- Each running **sim** owns its own `lua_State` — it holds device state and
  survives client disconnect/reconnect.
- Each client **connection** is a coroutine within that sim's `lua_State`.
- `io.read("*l")` **yields** the coroutine when no full line is buffered;
  `EPOLLIN` on the connection fd appends bytes and **resumes** it.
- Because the loop is single-threaded, rich control commands (RESET/INJECT/…)
  mutate sim state **between** coroutine resumes — **no locking, race-free,
  deterministic** (which is what makes unit tests reliable).

---

## 5. Data Model

```cpp
struct csim_conn {
    int         fd;          // accepted client socket (nonblocking)
    int         coro_ref;    // registry ref to the connection's lua coroutine
    std::string rbuf;        // bytes from client, consumed line-wise by io.read
    std::string txbuf;       // bytes queued by io.write/print, flushed after resume
};

struct csim_instance {
    int          id;             // control-assigned sim id
    int          dtype;          // device type (may be -1 if started by explicit script)
    int          neid;
    std::string  tid;
    std::string  script_path;    // resolved .clansim path
    std::string  sim_data_root;  // per-sim overlay dir (SIMDATA) for INJECT
    int          listen_fd;      // TCP listener
    int          port;           // assigned/auto port
    lua_State   *L;              // per-sim interpreter (device state lives here)
    csim_conn    conn;           // single active connection (fd < 0 when idle)
    int          status;         // IDLE / LISTENING / CONNECTED / ERROR
    struct { long bytes_in, bytes_out; std::string last_cmd; } stats;
};

struct csim_daemon {
    int          epoll_fd;
    void        *zmq_ctx;
    void        *rep_sock;       // zmq REP control socket
    std::unordered_map<int, csim_instance> sims;   // by id
    std::unordered_map<int, csim_instance*> by_fd; // listener & conn fd -> sim
    int          next_id;
    int          next_port;
    struct {
        std::vector<std::string> sim_dirs;   // script search roots
        std::string sim_data_root;           // base for canned-file tree + overlays
        int         port_base;               // auto-port allocation base
        std::string ctl_endpoint;            // zmq REP bind endpoint
    } cfg;
};
```

---

## 6. Lua Bridge (the crux) — `csim_luabridge`

Sets up a per-sim `lua_State` so **existing `.clansim` scripts run verbatim**:

1. **Standard libs + package path.** Open Lua stdlib; set `package.path` to
   include the script directory so `require('clansim')` resolves the unchanged
   `clansim.lua`.
2. **`inc` shim** — only the surface the scripts actually use:
   - `inc.app_setup(name)` → log/no-op.
   - `inc.trace(level, msg)` → route to `nflog`.
   - `inc.sysdef(key, default)` → config/env lookup; returns this sim's
     `sim_data_root` for `SIMDATA=` (so canned files + INJECT overlay resolve
     per-sim).
3. **`io.read` / `io.write` / `print` overridden** in this state:
   - `io.read("*l")` is a C closure: returns a buffered line from `conn.rbuf`,
     or `lua_yield`s when no full line is present.
   - `io.write` / `print` append to `conn.txbuf`; the daemon flushes `txbuf` to
     the socket after every resume.
4. **`os.exit` overridden** → raises a "logout" that terminates the coroutine
   and closes the connection. It **must never terminate the daemon process.**
5. The script chunk itself (`clansim.default_sim(arg,{…})` →
   `advanced_sim`'s `while true` loop) **is** the coroutine body. `arg` is set
   to `{neid, tid}`, matching the existing `argv[1]`/`argv[2]` convention.

**Yield safety:** the yield originates in the `io.read` C closure and unwinds
through the direct call chain (`advanced_sim` → script chunk → coroutine),
which the ncland lua build already supports via `lua_resume`. No `lua_pcall`
sits between the yield point and the coroutine boundary.

---

## 7. Control Protocol (zmq REP, JSON)

**Request envelope:** `{"v":1,"cmd":"START","args":{…},"req_id":"…"}`
**Reply:** `{"ok":true,"result":{…}}` or `{"ok":false,"error":"…"}`

| cmd | args | result / effect |
|---|---|---|
| `START` | `dtype` **or** `script`, `neid`, `tid`, `port` (0 = auto) | `{id, port}`. Resolves script via clan's order: `ne<neid>.clansim` → `<dtype>.clansim` → `def.<dtype>.clansim` under `cfg.sim_dirs`. Errors if none found. |
| `STOP` | `id` | Close listener + connection, free `lua_State`, remove overlay dir. |
| `LIST` | — | Array of `{id,dtype,neid,tid,port,status}`. |
| `STATUS` | `id` | `{status, bytes_in, bytes_out, last_cmd}`. |
| `RESET` | `id` | Rebuild a fresh `lua_State` (device "reboots"), keep the port/listener. |
| `RELOAD` | `id` | Re-read the script file from disk (pick up edits), keep port. |
| `INJECT` | `id`, `cmd`, `reply` | Write `reply` to the sim's overlay dir as the canned file `read_file()` searches for (command→underscored filename). Next matching command returns `reply`. |

---

## 8. Connection Lifecycle

1. **START** → `socket`/`bind`/`listen(1)` on the assigned port; `epoll_ctl(ADD)`
   the listener. Create the per-sim `lua_State` and overlay dir.
2. **Accept** (listener `EPOLLIN`) → accept one client, set nonblocking, create
   the connection coroutine, prime the script; first `lua_resume` runs to the
   initial `io.read` yield, emitting the first prompt via `io.write`.
   (Second concurrent client is rejected while one is active.)
3. **Client line** (conn `EPOLLIN`) → append to `conn.rbuf`, `lua_resume`;
   coroutine consumes the line, writes the response to `conn.txbuf`; daemon
   flushes to the socket; coroutine yields at the next `io.read`.
4. **`logout` / `os.exit`** → tear down the coroutine and close the client fd,
   but **keep the sim + listener** (reconnectable) until an explicit `STOP`.

---

## 9. Testing

**Unit (`nfunit-test.hpp`):**
- Control envelope parse/format (JSON round-trip, error replies).
- START path-resolution order (`ne<neid>` → `<dtype>` → `def.<dtype>`).
- **io-bridge drive** — feed bytes to a sim `lua_State` with no socket at all
  and assert the emitted bytes for a login sequence and a command lookup. This
  is the "unit-test the sims themselves" goal.
- INJECT overlay resolution (INJECT then command → injected reply wins over the
  base canned tree).

**Integration (`test/integration/*.sh`):**
- Launch the daemon, zmq `START` a fixture sim, TCP-connect, run login + a
  command, assert the canned response, then `STOP`.

**Fixtures (`test/fixtures/`):**
- A sample `.clansim` script, a copy of `clansim.lua`, and a small canned-file
  tree under `test/fixtures/lansim/`.

---

## 10. Files (new, in `cnc/ncland/src`)

| File | Purpose |
|---|---|
| `clansimd.cpp` | Entry point: arg/config parse, build daemon, run epoll loop. |
| `csim_daemon.{h,cpp}` | Daemon struct, epoll loop, fd routing, port allocation. |
| `csim_instance.{h,cpp}` | Sim lifecycle: start/stop/reset/reload, listener, `lua_State` mgmt, overlay dir. |
| `csim_luabridge.{h,cpp}` | Per-sim `lua_State` setup: `package.path`, `inc` shim, `io`/`print`/`os.exit` overrides, coroutine drive. |
| `csim_control.{h,cpp}` | zmq REP handling; JSON envelope parse/dispatch. |
| `csim_control_tests.cpp`, `csim_luabridge_tests.cpp`, `csim_instance_tests.cpp` | Unit tests (into the unit-test binary). |
| `Makefile` (edit) | New `clansimd` target linking `-llua -lzmq -ljson -lrt -lpthread`; test objs added. |
| `test/fixtures/…`, `test/integration/it_clansimd.sh` | Fixtures + integration test. |

`csim_conn` may be folded into `csim_instance.{h,cpp}` rather than its own file,
since v1 allows one connection per sim.

---

## 11. Phasing

- **Phase 1 (this spec):** standalone `clansimd` — epoll loop, lua bridge running
  existing scripts verbatim, TCP-per-sim, full zmq control API, unit +
  integration tests. Driven entirely via zmq/TCP by tests.
- **Phase 2 (later):** wire `clan`/`ncland` to drive the daemon over zmq
  (START a sim, connect to the returned port) instead of forking one `.clansim`
  subprocess per connection. Out of scope here.

---

## 12. Open Risks / To Watch During Implementation

- **Coroutine yield across the `io.read` C boundary** — verify against the exact
  linked Lua version early with a minimal spike before building out the bridge.
- **`os.exit` sandboxing** — a missed override would let a script kill the whole
  daemon; cover with a unit test that runs a `logout` and asserts the process
  survives.
- **Port exhaustion / reuse** — auto-port allocation must skip in-use ports and
  reclaim on STOP.
- **Overlay-dir cleanup** — remove per-sim overlay dirs on STOP to avoid stale
  injected responses leaking across sims that reuse an id.
