# mqorderd — MQ circuit-order feed (issue #5726) — Design

Date: 2026-10-14
Repo: netflex, branch `issue-5726-mq-circuit-feed`

## Intent

One customer gets circuit orders through **lterm → orderprint**. lterm acts
as a *printer interface*. `start.c` starts it as a long-running process
(`cnc/adm/src/start.c:976`, when the `lterm=` system_define is set, active
machine only). It listens on a TCP port (`lterm.<printer>.port`), and the
customer's print system connects there and streams order pages. lterm
splits the stream into pages, classifies each one, writes it to a file and
runs `orderprint` on that file. That source is going away. The same orders
will now come in on an **IBM MQ queue**.

`mqorderd` is a long-running daemon. It reads orders from MQ and hands each
one to `orderprint` **exactly the way lterm does**, so orderprint and
everything after it stay unchanged.

This is a **cutover**, not a dual feed. Only one customer uses lterm/orderprint,
and lterm is retired there once mqorderd is live.

### What success looks like
- Every order put on the queue reaches orderprint. **No order is lost**, even
  if the daemon crashes or is stopped. If orderprint fails on a page, that
  page is kept on disk and traced, the same recovery model as lterm today.
- Connection problems are obvious and self-healing. The daemon never exits
  because MQ is unreachable, and the log names the actual MQ reason code.
- orderprint's per-printer state (`/usr/cnc/orders/printer/<printer>`,
  `.lastrun`, order files) carries on from where lterm left off.

### The user's two biggest worries
1. **Losing orders.**
2. **Not being able to connect.**

## Goals
- Read orders from an IBM MQ queue configured in a YAML file.
- Feed orderprint with the same file naming and command line lterm uses.
- Transactional consumption: commit the MQ get only after every page of the
  message has been handed to orderprint (or kept aside on failure).
- Survive and recover from MQ outages without manual action.
- Run as a systemd service that boot_nms starts and stops.
- A way to validate config and connectivity at the customer before cutover.
- A way to inspect real messages before go-live (capture-only mode).

## Non-goals
- No changes to `orderprint`. If the MQ payload format turns out to be
  different, adapting it is a follow-up, not part of this design.
- No dual-feed or lterm coexistence mode.
- No sending anything back over MQ (no replies, no acknowledgement messages).
- No GUI or web configuration. The YAML is edited by hand during setup.

## Anti-goals (a failure even if it "works")
- An order disappears silently. Any message that can't be processed must
  end up somewhere a person can find it, with a loud trace entry.
- The daemon spins, crash-loops, or floods the trace while MQ is down.
- The daemon starts at OS boot before the INC application is up and burns
  through orders that orderprint can't process yet.
- orderprint is re-run on a page it already partly processed. It isn't
  idempotent (see "orderprint is not retry-safe"), so this would duplicate
  pages in the order accumulation file.
- A poison message blocks every order queued behind it forever.
- Other customers see any change in behavior, because mqorderd has no YAML
  at their sites.

## Constraints
- **Location (user's decision):** `cnc/mqorder/` in netflex, built and shipped
  with the INC build.
- **Language (user's decision):** Go. This is the first Go code in the repo.
  The reason for choosing it is a single binary with no runtime package
  installs. Go modules are compiled in.
- **MQ client (user's decision):** assume the IBM MQ C client libraries are
  present on targets at `/opt/mqm`. Use the official
  `github.com/ibm-messaging/mq-golang` (`ibmmq` package, cgo).
- **Build environment facts:**
  - Go 1.24.1 is at `/opt/devtools/bin/go` on the dev box. Build hosts need
    it too.
  - Build-time MQ is `MQ_ROOT = /opt/devtools/mqm/v9.1.0-7` (`global.nmk:54`).
    It is the 9.1.0.7 Redist client, with headers in `inc/` and **64-bit libs
    in `lib64/`**.
  - `global.nmk` defaults to `MQ32=1`, meaning the C MQ code links 32-bit. Go
    must build 64-bit and link `$(MQ_ROOT)/lib64`. At runtime that is
    `/opt/mqm/lib64`.
  - The mq-golang version must compile against the **MQ 9.1 headers**. Newer
    releases may reference constants that only exist in later MQ versions.
    Pin a version that builds against 9.1.0.7.
- **Build system:** all makefiles are Lucent/AT&T nmake. The Go build must be
  driven from an nmake makefile in `cnc/mqorder/` that runs `go build`
  (CGO_ENABLED=1, CGO_CFLAGS/CGO_LDFLAGS pointing at `$(MQ_ROOT)`), with
  modules vendored (`go mod vendor`) so the build needs no network. The
  output goes to `$(PBIN)/mqorderd`, like other products.
- **RHEL 7/8/9:** cgo links dynamically against glibc. Build on each RHEL
  release as the C code is now, or the binary will not run on older RHEL.
  Go 1.24 supports the RHEL 7 kernel (3.10).
- **Runs as `cmm`**, like other netFLEX services. orderprint is installed
  setuid root (`3b2/shell/cmmperm.sh:102`), so the unit **must not** set
  `NoNewPrivileges=` or any other hardening option that blocks setuid.
- **No redundancy at the customer (user's decision).** There's no
  active/standby, so no role check is needed and a boot_nms trigger is
  enough. Document this assumption in the unit file and the example YAML.
- **64-bit only (user's decision, 2026-10-20).** Tested: mq-golang v5.7.3
  builds and links against `/opt/devtools/mqm/v9.1.0-7` (`inc` + `lib64`),
  but it doesn't support linux/386: it ships no 386 constants file, and
  `mqi.go:1412` uses a `1<<31` array length. Build only when
  `$(L64_SETUP)` == "1". In the 32-bit pass the makefile does nothing. Link
  with `-Wl,-rpath,/opt/mqm/lib64`.

## Existing behavior that must be matched (lterm)

From `cnc/rcmd/src/lterm.c`:

0. **Split into pages** (`processInput`, ~lines 300–445). orderprint handles
   **one page per call** and assembles an order itself, so this step
   matters:
   - Form-feed (`\f`, 0x0C) ends a page. Data before the first form-feed
     belongs to the first page, and the first form-feed doesn't emit
     anything empty.
   - `\r` is dropped from page content. At each `\r`, if the page so far
     contains `CKT` or `SYS` and `sscanf(page, "PG C%d-%d")` gives
     `page == maxpage`, an end of order is noted. The next non-whitespace
     byte after that starts a new page, even without a form-feed.
   - Note: lterm's `sscanf` matches only at the start of the page buffer.
     Replicate that behavior exactly; don't "fix" it.

   For each page, `writeOut` (~lines 448–562) does:

1. **Classify** the page:
   - contains (`CKT` or `SYS`) **and** `" PG "` → prefix `ckt`
     (lterm traces a warning if len < 900, but still processes it)
   - contains `ORDERS COMPLETED YESTERDAY` or
     `ORDERS/CIRCUITS/SEGMENTS COMPLETED YESTERDAY` → prefix `ocy`
   - otherwise → junk
2. **Write** to `<dir>/<prefix>.<printer>.<session>.<now>.<pid>.<seq>`, where:
   - `<dir>` defaults to `/usr/cnc/orders/printer/<printer>`
   - `<now>` is the epoch second
   - `<seq>` is 2 digits, reset when the second changes
   - `<pid>` is 6 digits
3. **Run** `/usr/cnc/bin/orderprint <printer> <path>`. If it exits 0,
   `unlink(path)`.

lterm has special junk/ocy handling: once it's in an `ocy` report, later
junk pages are appended to the last ocy file, because an ocy report spans
pages and only its first page has the header. mqorderd **keeps this
behavior**: page splitting happens per message, and the ocy state carries
across pages and messages exactly as lterm's static state does.

`orderprint` (`cnc/atfeed/src/orderprint.c:88`) usage is
`orderprint [-v] [-o orddir] <printer> <file>`. It uses the CNC database and
system_defines, so it needs the INC application to be up.

### orderprint is not retry-safe
Before it exits non-zero, orderprint may already have done any of these:
- appended the page to the `<printer_path>/<cac>` accumulation file
- moved the input file itself (`movefile`, e.g. `orderprint.c:329`, exit 2)
- written DB records
- copied ocy files locally or by `rcp`

lterm never retried. On failure it left the file where it was. **mqorderd
never re-runs orderprint on a page that orderprint has already run on.**

## Design

### Configuration — YAML
One file per instance: `/usr/cnc/data/mqorderd/<printer>.yaml`. The filename
(without `.yaml`) is the **instance name** and must match `printer:` inside
it (the daemon errors at startup if they differ).

The fields cover everything `mqOpenConn` in `cnc/utility/src/mqclient.c:1142`
uses today, plus mqorderd's own settings:

```yaml
printer: <name>            # SAME printer name the customer's lterm uses today
mq:
  host: 10.0.0.1
  port: 1415
  channel: SSL_ORDERS
  queue_manager: QM_ORDERS
  queue: Q_ORDERS
  cipher_spec: TLS_RSA_WITH_AES_256_CBC_SHA256   # optional; omit for non-TLS
  key_repository: /var/mqm/ssl/inc               # optional; stem, no .kdb
  backout_queue: Q_ORDERS_BOQ                    # optional
  max_backout: 5                                 # optional, default 5; poison guard only
  user: ""                                       # optional MQCSP user/password
  password_file: ""                              # optional; file, never inline
orders:
  dir: /usr/cnc/orders/printer/<name>            # optional, lterm default
  orderprint: /usr/cnc/bin/orderprint            # optional
  failed_dir: /usr/cnc/orders/mqorderd/<name>/failed   # optional
  junk_dir:   /usr/cnc/orders/mqorderd/<name>/junk     # optional
  capture_only: false
```

**Shipped:** `3b2/data/mqorderd.yaml.example` installs as
`/usr/cnc/data/mqorderd/mqorderd.yaml.example`. The `.example` suffix
keeps it from matching the boot_nms `*.yaml` glob. The real YAML is created
during setup at the customer.

The cipher spec must be a TLS CipherSpec the queue manager's channel accepts.
The `MQMTOSI_*` sample value `NULL_MD5` is obsolete in MQ 9.x.

### Message processing — the no-loss loop
**Rule (user's decision): if orderprint ran, the page is handed over.** On
failure the page is kept aside and the message is still committed, like
lterm. MQ backs out only when nothing was handed over yet.

For each message:

1. `MQGET` under syncpoint (`MQGMO_SYNCPOINT`), with a wait interval
   (e.g. 30s) so shutdown and reconnect stay responsive. Use
   `MQGMO_CONVERT`. Do **not** set `MQGMO_ACCEPT_TRUNCATED_MSG`: on
   `MQRC_TRUNCATED_MSG_FAILED`, grow the buffer and get again.
2. **Split into pages** with lterm's rules (user's decision: always split,
   so it works for a one-page message and for a whole order). A message
   with no form-feed is one page. Trailing data after the last form-feed
   is flushed as a final page if it holds any non-whitespace. lterm drops
   that remainder when a connection closes; mqorderd must not, because a
   message boundary is a real end of data.
3. **Check the journal** (see below). If this MsgId was partly processed
   before a crash, skip the pages already handed over.
4. For each page, in order:
   1. **Classify** with lterm's rules, including the ocy/junk append state.
   2. **Junk:** write it to `junk_dir` (MsgId + page number in the
      filename), trace a warning with the first ~200 bytes, and continue.
   3. **Write** the page file with lterm's naming (session = 0) and `fsync`.
   4. Record "MsgId page N started" in the journal and `fsync`.
   5. Unless `capture_only` is set, **run orderprint** (fork/exec directly,
      not via a shell):
      - exit 0 → unlink the page file if it still exists (orderprint may
        have moved it).
      - non-zero or signal → **keep the page**:
        - copy it to `failed_dir` as `<lterm-name>.<MsgId>.p<N>` (take it
          from wherever orderprint left it, either the original path or
          `<printer_path>/<basename>`)
        - trace an error with the exit status and the order's CAC if it
          can be parsed
        - **continue with the next page** (user's decision: a failed page
          doesn't stop the rest of the order)
5. `MQCMIT`, then clear this MsgId from the journal.
6. **Back out only when nothing was handed over:**
   - Can't write a page file (disk full, permissions), or orderprint can't
     be exec'd at all (missing binary, ENOEXEC) → `MQBACK` and clear the
     journal.
   - Wait before the next get (5s, growing to 60s) so a persistent failure
     doesn't spin.
   - If this happens after some pages were already handed over, commit
     instead and keep the unprocessed pages in `failed_dir`. Never re-run
     orderprint on a handed-over page.
7. **Poison guard:** if `MQMD.BackoutCount >= max_backout` (only reachable
   through the step 6 backouts or a crash loop):
   - write the whole raw message to `failed_dir`
   - if `backout_queue` is configured, also put it there in the same unit
     of work
   - commit, and trace at error level
8. **capture_only:** split and write every page (junk too) to `orders.dir`
   with lterm naming, don't run orderprint, commit, and keep the files.
   This is for checking real messages before go-live.

**Journal (my call).** This closes the crash window. Without it, a crash
after orderprint ran but before `MQCMIT` would redeliver the message and
re-run orderprint on pages it already processed, which is the duplicate
this design must prevent.

- The journal is a small file per instance
  (`/usr/cnc/orders/mqorderd/<printer>/journal`), recording the in-flight
  MsgId and which pages were started.
- On redelivery of that MsgId:
  - pages marked started are **not** re-run
  - the last started page, whose outcome is unknown, is copied to
    `failed_dir` with an error trace for a person to check
  - processing resumes at the next page
- A different MsgId means the journal is stale. Trace it and clear it.

`orderprint` is fast and per-page, so processing one message at a time is
fine. No concurrency.

### Connection handling
- Connect with `MQCONNX`: client mode, a `MQCD` built from the YAML (as
  `mqOpenConn` does), and `MQSCO.KeyRepository` when TLS is configured.
- If connect/open fails, or a get/commit returns a connection-level reason
  (`2009 CONNECTION_BROKEN`, `2059 Q_MGR_NOT_AVAILABLE`, `2161/2162` quiescing,
  etc.):
  - disconnect cleanly
  - retry with exponential backoff: 5s, doubling, capped at 60s, retrying
    forever
  - reset the backoff after a successful open
- Every failure is traced with the **numeric reason code and its name**,
  plus host/port/channel/qmgr/queue. To keep MQ outages from flooding the
  trace, repeated identical failures are summarized: log the first, then
  one line every 10 minutes with a count.
- In-flight work during a disconnect: an uncommitted get rolls back on the
  server side and the message is redelivered. The journal prevents
  orderprint from being re-run on pages it already handled.

### Command line
- `mqorderd <printer>` runs the daemon. It reads
  `/usr/cnc/data/mqorderd/<printer>.yaml`.
- `mqorderd -c <file>` uses an explicit YAML path, for testing.
- `mqorderd --check <printer>` loads config, connects, opens the queue for
  input, prints the queue depth (`MQINQ CURDEPTH`) and OK/FAIL with the
  reason code, then exits 0/1. This is the pre-cutover validation.
- `mqorderd -v` prints the version.

### Logging
- Trace file: `/usr/cnc/trace/mqorderd.<printer>`, appended, with a timestamp
  on each line. Like lterm's `/usr/cnc/trace/lterm.<printer>`, it's where
  support already looks.
- Startup failures (bad YAML, missing dirs) also go to **stderr** so they
  appear in `journalctl -u netFLEX-mqorderd@<printer>`.
- Verbosity: honor the netFLEX debug convention if it's cheap
  (`/usr/cnc/features/debug`, as `debug_lvl` reads it). Otherwise use a
  `log_level` YAML key. This is the builder's call.

### Lifecycle — systemd + boot_nms
**Unit:** `3b2/data/netFLEX-mqorderd@.service`, a template where the
instance = printer name:

```ini
[Unit]
Description=netFLEX MQ order feed (%i)
After=network-online.target

[Service]
User=cmm
Group=cmm
ExecStart=/usr/cnc/bin/mqorderd %i
Restart=always
RestartSec=10
KillSignal=SIGTERM
TimeoutStopSec=120
Environment=LD_LIBRARY_PATH=/opt/mqm/lib64:/usr/cnc/lib
# Do NOT add NoNewPrivileges= or other hardening: orderprint is setuid root.
# No active/standby support: assumes a single (non-redundant) INC host.
```
- The binary carries an rpath to `/opt/mqm/lib64`. The `LD_LIBRARY_PATH`
  line is only a backup.
- **No `[Install]` section, and it is never `systemctl enable`d.** Enabling
  it would start it at OS boot, before the INC application is up.
- `TimeoutStopSec` must be longer than a worst-case orderprint run.

**boot_nms (user's decision): the YAML's existence triggers start and stop.**
- New `start_mqorderd()`:
  - If `/usr/cnc/data/mqorderd/*.yaml` matches nothing, do nothing.
  - Otherwise copy `/usr/cnc/data/netFLEX-mqorderd@.service` to
    `/usr/lib/systemd/system`, run `systemctl daemon-reload`, then
    `systemctl start netFLEX-mqorderd@<printer>` for each YAML.
  - Called at the point in startup where the INC application is known to be
    up.
- New `stop_mqorderd()`:
  - Run `systemctl stop` for every `netFLEX-mqorderd@*` instance that's
    running. Use `systemctl list-units`, not the YAML list, so an instance
    whose YAML was removed still gets stopped.
  - Called early in shutdown, before the application goes down.
- Model both on `start_linux_mux()`/`stop_linux_mux()` (`boot_nms:894`/`1010`),
  including the `$AUTOBOOT`/`$AUTOSHUTDOWN` messaging style. Note the
  existing `start_linux_mux` call sites are MultiBox/md-specific, so
  mqorderd needs its own call sites in both the interactive and autoboot
  paths.
- The service's output goes to `/usr/cnc/trace/mqorderd.service`, following
  the md pattern.
- **Not added to `include/sys_procs.ext`** (user's decision: moving services
  toward systemctl).

**Shutdown in the daemon:**
- On SIGTERM, stop taking new messages.
- If an orderprint is running, let it finish, finish the current message's
  remaining pages, then commit. Never stop between pages of a message
  without committing.
- Then `MQDISC` and exit 0.
- Never kill orderprint mid-run.

### Packaging / install
- Product: `$(PBIN)/mqorderd`, plus the unit and the YAML example under
  `3b2/data`. Add them to the same packaging lists the other
  `3b2/data/netFLEX-*.service` files and `/usr/cnc/bin` daemons are in.
- Directories `/usr/cnc/data/mqorderd` and `failed`/`junk` are created owned
  by `cmm`, either at install or on first start by the daemon. Builder's call.

### Testing
- **Unit tests (Go):**
  - classification against lterm's rules, including the edge strings
  - filename generation (seq rollover in the same second)
  - YAML validation (printer/filename mismatch, missing required fields)
  - page splitting against lterm's `processInput` rules, using table tests
    built from the code. lterm's input-file mode can't serve as an oracle:
    `lterm.c:123` calls `processInput` with 4 of its 6 arguments, so
    `justcap`/`keepjunk` are undefined and it may run orderprint.
  - the processing state machine, using a fake MQ interface and a fake
    orderprint:
    - all pages succeed
    - page 3 of 5 fails (kept, rest processed, committed)
    - exec failure (backout)
    - crash mid-message, then redelivery (journal skips handed-over pages)
    - poison backout count
- **Integration:** use `cnc/tools/src/mqreq.c` or `amqsput` from the MQ
  samples to put test orders on a queue. Real lterm capture files from
  `/usr/cnc/orders/printer/<printer>` make good fixtures.
- **Pre-go-live at the customer:** run `--check`, then a period with
  `capture_only: true`, then compare captured files with lterm output for
  the same orders.

## Left to the builder
- Go package layout inside `cnc/mqorder/`, and the internal interfaces
  (keep MQ behind an interface so the loop is testable).
- Exact wait interval, retry/backoff constants, and the log throttle
  interval, within the bounds above.
- Exact boot_nms call sites (start after app up, stop before app down, in
  both interactive and autoboot paths).
- Whether verbosity follows `/usr/cnc/features/debug` or a YAML key.
- Directory creation at install time or at daemon start.
- How the nmake rule invokes Go (`GOFLAGS=-mod=vendor`, `GOCACHE` location,
  build ID/version stamping).

## Open risks
1. **Message format is unverified.** The expected case is lterm's text format
   (`CKT … PG …` pages separated by form-feeds), whether a message holds one
   page or a whole order; splitting handles both. If it's a different format
   (XML/JSON, EBCDIC, no form-feeds between pages), classification and
   splitting change. capture_only exists to find out. **This must be
   verified before go-live.**
2. **Failed pages need manual recovery, as with lterm today.** An order with
   a failed page won't complete in orderprint until someone re-feeds the
   kept page. Make the `failed_dir` trace line clear enough to act on.
3. **Stale journal under mixed priorities.** If a different message is delivered before the crashed one is redelivered, the journal is cleared and the crashed message's pages could run again. Orders are expected at a single priority, where MQ redelivers the backed-out message first.
4. **Build hosts need Go 1.24** plus the MQ 9.1 headers and `lib64`. Confirm on every RHEL build host before wiring into the
   release build.

## Unrelated findings
- `cnc/rcmd/src/lterm.c:123`: file mode calls `processInput(fd,0,dir,printer)`
  without its `justcap`/`keepjunk` arguments, which is undefined behavior.
  It's moot once lterm is retired.
- `cnc/utility/src/mqclient.c:1207–1209` passes a stack buffer
(`char sslkeyr[128]`) to `putenv()`, which leaves a dangling `MQSSLKEYR` once
`mqOpenConn` returns. It's separate from this work and worth its own issue.
