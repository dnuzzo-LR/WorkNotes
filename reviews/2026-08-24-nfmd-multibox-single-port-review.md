# NFMD Multibox Single-Port ZMQ Forwarding — Code Review

**Date:** 2026-08-24
**Branch:** `multibox-zmq-signle-port` (at same SHA as `main` — code lives in HEAD, not diff)
**Reviewer:** Dan Nuzzo (with Claude assist)
**Scope:** `cnc/md/src/md.c`, `cnc/md/src/l_md_sock.c`, all eight `cnc/md/src/l_*.cpp` workers, `include/md.h`
**Related:** `~/WorkNotes/reviews/2026-06-15-mdzmq-review.md` (earlier ZMQ md review)

---

## Feature Intent

When system_define `NFMD=<port>` is configured to a value `> 1` (specifically `> 1024`
as a port number), the `md` process becomes the single owner of one external ZMQ
port. The eight `l_*.cpp` helper processes (rdr/wtr × BEP/FEP × frm/msg) run in
a **forwarded mode**: they don't hold TCP sockets themselves. They exchange
messages with `md` via ZMQ, and `md` multiplexes / demultiplexes traffic on the
one external port using a 34-byte routing tag prepended to each message.

## Architecture (as coded)

```
Remote peer                       Local md                  Local workers
  wtr ──PUSH──► [tcp://*:port]─PULL──►[dispatch]──PUSH──► [ipc://.../md-<name>]─PULL rdr
  rdr ◄──PULL─  (no return path from local rdrs back to md — one-way)
                                                          [ipc://.../md-<name>]        wtr ──PUSH──►[tcp://remote:port]
```

**One-way inbound forwarding only.** Local `wtr` workers open their own PUSH
connections directly to *remote* md's PULL. Local md is not involved in the
outbound path. The branch name "single-port" describes the receive side only.

---

## Critical Bugs (feature is currently non-functional)

### C1. NFMD threshold mismatch — parent and children disagree

- `md.c:119` — parent: `if (nfmd > 1024) MdZmqPort = nfmd;`
- All 8 workers, e.g. `l_bep_fep_frm_rdr.cpp:210`, `l_bep_fep_msg_rdr.cpp:220`,
  `l_fep_bep_frm_rdr.cpp:213`, `l_bep_fep_frm_wtr.cpp:200`,
  `l_bep_fep_msg_wtr.cpp:200`, `l_fep_bep_msg_rdr.cpp:210`,
  `l_fep_bep_frm_wtr.cpp:227`, `l_fep_bep_msg_wtr.cpp:211`:
  `if (1 == nfmd) MdUseZmq = 1;`

**No single NFMD value activates both sides.** Setting NFMD to a port number
enables md's ZMQ mode but leaves every worker in legacy TCP mode; setting NFMD=1
enables the workers but leaves md legacy. Feature is unreachable as written.

**Fix:** Workers should mirror the parent:
```c
if (nfmd > 1024) { MdUseZmq = 1; MdZmqPort = nfmd; }
```

---

### C2. `MdZmqTag` is never populated — routing tag is all zeros

- `l_md_sock.c:43` — `char MdZmqTag[40];` (BSS-zero global)
- No writer anywhere in the tree (verified by grep)
- `l_md_sock.c:753, 765` — `md_zmqsend` only prepends the tag when
  `MdUseZmq > 1`, but workers only set `MdUseZmq = 1` (bug C1). Two
  independent gates each guarantee **the routing header is never prepended**.
- Even if the gate were fixed, the payload would be 34 zero bytes.
- md dispatcher (`md.c:854-861`) reads those zeros, does
  `strcmp("", Procs[loop].tag)`, never matches → every inbound message dropped
  and leaked.

**Fix:** Populate `MdZmqTag` from argv/env in each worker's main() with the
same string md builds in `md_load_cnf` (`sprintf(tag,"%s:%02d:%02d",...)` for
rdrs; `orig_smach` for wtrs). Use `MdUseZmq >= 1` (or the fixed threshold) as
the send-side gate.

---

### C3. `md_wait_onevent` dispatch loop broken (md.c:761-897)

Multiple defects in the same function:

1. `while (1)` at line 829 has **no exit condition** for `pollitem != 0`
   branch — pure spin. Only the `if (0 == pollitem)` block does anything.
2. After a successful match + forward, inner `for` `break`s but the outer
   `while(1)` iterates and calls `zmq_msg_recv` again on the same socket —
   blocks the whole md dispatcher.
3. **Unmatched-tag path leaks `msg`** — no `zmq_msg_close(&msg)` after the
   for-loop terminates without a match, then re-loops.
4. Dead code at line 823-824:
   ```c
   if (0 == pollitem) forward=(-1);
   else               forward=(-1);
   ```
   Identical branches.
5. Dead code at line 886: `zmq_msg_close(&dmsg);` **after** `break` at 885.

**Fix:** Remove `while(1)`. One recv per poll iteration. On unmatched tag,
log + `zmq_msg_close` + drop. Always close `msg` on every path. Return to
`zmq_poll` at the top of `md_wait_onevent`.

---

### C4. Poll-item index off-by-N (md.c:862-865)

```c
forward = loop;                            /* Procs[] index */
ssock   = poll_items[forward+1].socket;    /* wrong: not 1:1 */
```

`poll_items[]` is populated in `md_wait_onevent` (lines 784-802) **only for
Procs entries with `cmd[0]` non-empty**, and md.h:180 `struct procs` already
stores `poll_idx` (assigned at md.c:799). If any Procs slot below `forward` is
empty, the `+1` mapping is wrong and md forwards to the wrong worker.

**Fix:**
```c
ssock = poll_items[Procs[forward].poll_idx].socket;
```

---

## High Severity

### H1. Magic offsets in `md_mkzmqaddr` (md.c:899-926)

```c
if (strstr(cmdline, "rdr")) {
    snprintf(addr, n-1, "ipc:///usr/cnc/data/md-%s", tag+13);
    sptr = addr+22;
} else {
    snprintf(addr, n-1, "tcp://%s:%d", tag, MdZmqPort);
    return;
}
```

- `tag + 13` — hardcoded prefix strip. For a rdr tag like
  `bep_fep_frm_rdr:01:02` (21 chars), `tag[13] = 'd'` → the IPC path contains
  `dr:01:02`. If the naming scheme ever changes by one char, silent corruption.
- For a wtr tag = `orig_smach` (raw hostname, often < 13 chars) — reads past
  the string end.
- `addr + 22` — hardcoded offset into `ipc:///usr/cnc/data/md-` (23 chars) →
  sanitize loop begins **on the `-` character**, not after the prefix.

**Fix:** Use `strchr(tag, ':')` or `strrchr(tag, '/')` for slicing, compute
prefix length via `strlen()`. Or better, build the tag once and pass it
explicitly rather than reconstructing from `cmdline`.

---

### H2. Tag comparison uses `strcmp` on binary bytes (md.c:851-861)

```c
char hdr[ZMQRTRPKTHDR+1];
memcpy(hdr, zmq_msg_data(&msg), ZMQRTRPKTHDR);
hdr[ZMQRTRPKTHDR] = '\0';
...
if (0 == strcmp(hdr, Procs[loop].tag)) { ... }
```

Payload's first 34 bytes are opaque. If any tag byte is `\0` mid-tag, strcmp
stops early → false matches. `Procs[loop].tag` was built with `sprintf` so it
is C-string, but `hdr` is not. Use `memcmp(hdr, Procs[loop].tag, ZMQRTRPKTHDR)`
with a fixed comparison length, and pad `Procs[loop].tag` to `ZMQRTRPKTHDR`
with a defined fill byte (space or zero).

---

### H3. `Sd = 1` magic sentinel (all rdr workers)

`l_bep_fep_frm_rdr.cpp:336`, `l_bep_fep_msg_rdr.cpp` (similar), `l_fep_bep_frm_rdr.cpp` (similar):

```c
if (NULL == (ZmqSocket = md_zmqbind(ZmqContext, bind_addr)))
    TRACE(0, "#ERROR: ...");
else
    Sd = 1;
```

`Sd` is the file-descriptor global used by `md_reconnect` (`close(Sd)` at
`l_md_sock.c:290`) and `md_exit` (`close(Sd)` at `l_md_sock.c:807`). Value
1 == stdout.

- `md_exit` is guarded by `if (1 == MdUseZmq)` — OK.
- `md_reconnect` is **not** guarded. Any code path that calls
  `md_reconnect` while in ZMQ mode (mixed init? retry?) will `close(1)` and
  take out stdout, silently breaking TRACE.

**Fix:** Use `Sd = -2` as a "ZMQ mode active" sentinel, or decouple `Sd`
from ZMQ mode entirely (introduce `ZmqActive` bool).

---

### H4. IPC path collision / stale socket (md.c:909)

No `unlink()` before `zmq_bind("ipc://...")`. If a prior md/worker crashed
without cleanup, or a second md instance runs on the same host (dev/test), the
bind fails with EADDRINUSE.

**Fix:** `unlink(path)` before `zmq_bind` (Linux allows unlink of open sockets;
existing binders keep their fd). Or use `ipc://@abstract` (Linux only).

---

### H5. Config reload silently disabled in ZMQ mode (md.c:170-176)

```c
if (MdZmqPort > 1024) {
    md_wait_onevent();
    continue;
} else {
    alarm(CNFG_CHG_SECS);   /* legacy path */
}
```

Legacy path arms SIGALRM to call `md_recheck_cnfg`, which detects `md.cnf`
mtime changes and re-spawns processes. ZMQ path never arms the alarm →
`md.cnf` changes are ignored until md is bounced.

**Fix:** Either re-arm the alarm in the ZMQ loop, or integrate an mtime check
into the `zmq_poll` timeout body.

---

### H6. `md_setup_socket` sleeps 20 s in event loop (md.c:764-768)

```c
if (Ok > md_setup_socket()) {
    TRACE(1, "Could not create ZMQ socket. Wait and try again\n");
    sleep(20);
    return -1;
}
```

Blocks the whole md dispatcher for 20 s on any bind failure. Should return
immediately and rely on `zmq_poll` timeout (or a bounded backoff outside the
event loop).

---

## Medium Severity

### M1. `zmq_setsockopt(sock, ZMQ_LINGER, 0, 0)` is a no-op (l_md_sock.c:610)

```c
zmq_setsockopt(rep_socket, ZMQ_LINGER, 0, 0);
```

Passes NULL pointer and zero length. zmq expects `int*` and `sizeof(int)`.
Silently returns EINVAL and does nothing. PUSH path (`md_zmqconnect`) doesn't
set linger at all.

**Fix:** `int val=0; zmq_setsockopt(sock, ZMQ_LINGER, &val, sizeof(val));`

---

### M2. Writer fallback uses wrong port (l_bep_fep_frm_wtr.cpp:349-354)

```c
if ((env = getenv("MD_ZADDR")))
    strncpy(connect_addr, env, ...);
else
    sprintf(connect_addr, "tcp://%s:%d", HostName, Port);   /* legacy service port */
```

If `MD_ZADDR` is unset (worker started standalone), `Port` comes from
`getservbyname` — the legacy TCP service port, **not** `MdZmqPort`. Misleading.
Either require MD_ZADDR in ZMQ mode or explicitly use `MdZmqPort`.

---

### M3. Error strings misidentify socket types (l_md_sock.c:588, 636, 656)

- `md_zmqbind` error prints `"ZMQ_REP"` — socket is actually `ZMQ_PULL`.
- `md_zmqconnect` error prints `"ZMQ_RSP"` — socket is actually `ZMQ_PUSH`.
- `md_zmqconnect` error path prints `"zmq_bind"` when the failed call was
  `zmq_connect`.

Comment/code drift from a prior REQ/REP prototype. Confusing during triage.

---

### M4. `MdOneSocket` is dead (l_md_sock.c:40, md.h:544)

Declared, externed, never written, never read anywhere in the tree. Remove or
wire it in.

---

### M5. `MdZmqTag[40]` — size mismatch (l_md_sock.c:43)

Buffer is 40 bytes, only first `ZMQRTRPKTHDR` (34) used. Waste, and easy to
misinterpret as an off-by-6 bug during future maintenance. Either resize to
`ZMQRTRPKTHDR` or document why 40.

---

### M6. Missing Doxygen on new/changed functions (CLAUDE.md rule)

Per project convention, all new/modified C/C++ functions require Doxygen
headers. Missing on: `md_setup_socket`, `md_wait_onevent`, `md_mkzmqaddr`
(md.c); modified `md_start_proc`, `md_load_cnf`. `l_md_sock.c` has partial
coverage (`md_zmqbind`, `md_zmqconnect`, `md_zmqrecv` documented;
`md_zmqsend`, `md_zmqclose`, `md_zmq_ctx_new`, `md_exit` not).

---

## Design Observations

### D1. One-way forwarding only

- md's PULL only *receives* from remote wtrs.
- md's per-proc PUSH sockets only *send* to local rdrs.
- Local wtrs bypass local md entirely and PUSH direct to remote md's PULL.
- Local rdrs have no back-channel to md.

Legitimate design (matches existing rdr/wtr split), but the "single external
port" claim is inbound-only. Outbound side still has N remote connections from
N local wtrs.

### D2. No status/error back-channel

ZMQ_PULL/PUSH is unidirectional. Any worker-to-md status (heartbeat ack, error
report, rebind request) needs a separate socket pair. Currently absent.
Heartbeats do fire (ZMQ_HEARTBEAT_IVL/TIMEOUT are set on both sides), so pipe
liveness is detected, but application-level status is not.

### D3. `SNDTIMEO=10s` on PUSH

`MD_ZMQ_SNDTIMEO_MS = 10000` (md.h:557). Any legit slow peer produces EAGAIN
and the writer treats it as a failed link. Consider larger SNDTIMEO plus a HWM
tuned to expected burst size, or accept the trade-off explicitly.

### D4. Branch name typo

`multibox-zmq-signle-port` → `single-port`. Cosmetic, but permanent in git if
merged.

---

## Recommendation

**Do NOT merge.** The three critical bugs (C1 threshold mismatch, C2 tag never
populated, C3 dispatcher loop) each independently prevent the feature from
working. C4 (poll-index off-by-N) makes any working state route to wrong
workers.

## Suggested next steps

1. Fix threshold gate — single check (e.g. `nfmd > 1024`) with `MdZmqPort` and
   `MdUseZmq` set together on both sides.
2. Populate `MdZmqTag` in every worker `main()` from argv/env; align send-side
   gate.
3. Rewrite `md_wait_onevent` dispatch: one recv per poll iteration, `memcmp`
   tag, unmatched → log + close + drop, always close msg, no `while(1)`.
4. Use `Procs[i].poll_idx` for socket lookup.
5. Replace magic offsets in `md_mkzmqaddr` with `strchr`/`strlen`.
6. Kill `Sd=1` sentinel — use `Sd=-2` or a separate `ZmqActive` flag.
7. `unlink()` IPC path before bind.
8. Re-arm config-reload alarm (or integrate mtime check) in ZMQ event loop.
9. Fix `zmq_setsockopt(ZMQ_LINGER)` call — pass real `int*` and `sizeof(int)`.
10. Clean up misleading error strings ("ZMQ_REP"/"ZMQ_RSP"/"zmq_bind" in
    `zmq_connect` path).
11. Remove `MdOneSocket` if truly unused; otherwise wire in.
12. Add Doxygen to new/modified functions per CLAUDE.md.
13. Rename branch to `multibox-zmq-single-port` before merge.

## Files reviewed

- `cnc/md/src/md.c` (esp. lines 116-129, 258-515, 562-618, 746-926)
- `cnc/md/src/l_md_sock.c` (all)
- `cnc/md/src/l_bep_fep_frm_rdr.cpp` (esp. 195-355, 380-430, 500-610)
- `cnc/md/src/l_bep_fep_frm_wtr.cpp` (esp. 195-425)
- `cnc/md/src/l_bep_fep_msg_rdr.cpp` (skimmed — same pattern)
- `cnc/md/src/l_bep_fep_msg_wtr.cpp` (skimmed — same pattern)
- `cnc/md/src/l_fep_bep_frm_rdr.cpp`, `l_fep_bep_frm_wtr.cpp` (skimmed)
- `cnc/md/src/l_fep_bep_msg_rdr.cpp`, `l_fep_bep_msg_wtr.cpp` (skimmed)
- `include/md.h` (lines 100-115 macros, 173-181 struct procs, 358-381 sock
  msg structs, 542-563 ZMQ externs/constants)
