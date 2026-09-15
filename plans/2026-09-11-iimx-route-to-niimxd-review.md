# Code review — route-iimx-traffic-to-niimxd

Date: 2026-09-14
Branch: `route-iimx-traffic-to-niimxd`, 25 commits ahead of `main`
Scope: production files (`niimxlib.c/h`, `iimx_snd.c`, `niimxd.cpp`, `niimx.cpp`, `util.mk`); test scaffolding excluded
Method: 6 finder angles × up to 8 candidates → dedup → spot-verify against code → rank

15 findings below, ranked by severity. Refuted candidates listed at the end.

---

## Correctness / silent-wrong-data

### 1. `niimx_xact` strdup NULL check missing

**File:** `cnc/utility/src/niimxlib.c:795` (and :804, :813, :833, :857, plus niimx_slurp_rspfile)

Six sites do `*rsp = strdup(literal)` without a NULL check; on OOM the caller receives NULL where the header's contract says "*rsp always set on failure", and downstream deref crashes.

**Failure:** Under memory pressure `strdup` returns NULL. `iimx_sendx_call` returns -1 with `*rsp = NULL`. `iimx_snd.c:616`'s `strncmp(rsp,"/usr/cnc/tmp/...",13)`, doGather's `strstr`, rj_gui_cgi's `string rmt_dump_rsp(rrsp)` and `printf("%s", b.buf)` in iisnd all segfault. Legacy path returned a static failure string that could never be NULL.

---

### 2. `iimx_q` slot-clear discards callbacks and leaks Lua registry refs

**File:** `cnc/utility/src/iimx_snd.c:1188`

`iimx_q` divert on `niimx_send` failure `memset`s every populated `iimxQ` slot to zero, discarding registered callbacks. `cnc/rcmd/src/ilua.c:1568` stores `luaL_ref` in `e->data` — never `luaL_unref`'d — so the memset also leaks Lua registry references. New frequent triggers under the toggle (niimxd shed, DEALER teardown) turn a rare shape into a routine leak.

**Failure:** A long-lived Lua automation queues N iimx commands via `inc_iimxq`. The N+1-th send fails (DEALER teardown on EINTR/framing error). Slots 0..N are wiped; N Lua closures stay pinned in `LUA_REGISTRYINDEX`. GC cannot reclaim them; repeated teardown-triggered failures grow rcmd memory without bound. Per `iimx_q`'s contract the callback is the only completion channel — N callers are stuck waiting on completions that will never fire.

---

### 3. `rj_gui_cgi` rspfile callers don't check rc, get failure text as body

**File:** `cnc/restjson/src/rj_gui_cgi.cpp:321` (also :382, :578)

`rj_gui_cgi_almlist` and `rj_gui_cgi_map` call `iimx_sendx_call(...,rspfile=1,&rrsp,105)` and pipe `rrsp` into the `LINK^/DCS^/ALM^` parser without checking rc. Under the divert `niimx_xact` **always** fills `*rsp` — with `"Service Unavailable"`, `"Timeout"`, `"iimx is busy right now. try again later"`, or a `NIIMX^` body — on failure.

**Failure:** `USE_NIIMXD=1` and niimxd is down, briefly congested, or hits an oversized-reply teardown when the GUI hits `/gui/cgi` almlist or map. `rrsp="Service Unavailable"` contains no `LINK^/DCS^/ALM^` prefix; the parser silently produces an empty result. The user sees a blank map/alarm page with no error banner while niimxd is quietly unhealthy — support chases "missing alarms/links" tickets.

---

### 4. niimxd `unlink NIIMX_CONGESTED_FILE` at startup races a running instance

**File:** `cnc/niimx/src/niimxd.cpp:2913`

`main()` unconditionally `unlink(NIIMX_CONGESTED_FILE)` at startup, **before** `niimx_setup_zmq()` binds the ROUTER. Nothing here enforces single-instance, so an aborted second niimxd (systemd retry, monitor race, misconfig) erases the healthy instance's live flag before its own bind fails.

**Failure:** A second niimxd is spawned while a healthy one is running. Its startup unlinks the live congestion flag, then its `zmq_bind` fails and it exits. The healthy daemon is still congested; `niimx_set_congested()` will only rewrite the flag on its next 8-second utilization tick, so for up to 8s clients are handed a stale "not congested" answer and hammer a shedding daemon.

---

### 5. niimxd `fchmod` 0666 unconditionally on spill files

**File:** `cnc/niimx/src/niimxd.cpp:2180`

`niimx_spill_response` `fchmod`s the `mkstemp`'d spill file to 0666 unconditionally. The legacy `fopen` respected umask, so a hardened-umask deployment (022→0644, 077→0600) now gets a world-writable file where legacy had at most group-readable.

**Failure:** Deployment sets `umask 077` as hardening baseline. Under legacy, spill files were mode 0600 (owner only). Under the divert they are mode 0666 world-writable. Between `fwrite` completing and the client reading + unlinking (unsynchronised across processes), any local user can open the file for write and replace its contents; the client then reads attacker-supplied bytes as the command output.

---

### 6. `iimx_sendx_batch_wk` and `iimx_sendx_multi` don't shed on congestion

**File:** `cnc/utility/src/iimx_snd.c:2467` (and 1842)

Both diverts deliberately omit any `niimx_congested()` batch-start shed (the legacy port-80 sockets path never had one), while `iimx_sendx_pool` and `iimx_local` diverts **do** shed. During niimxd congestion these two batch paths flood the daemon with the full batch and collect per-entry `NIIMX^` rejections, keeping the daemon busy rejecting rather than letting it drain.

**Failure:** `niimx_set_congested(true)` is in effect on niimxd. `iimx_local` shed the whole batch at zero cost; `iimx_sendx_batch_wk`/`iimx_sendx_multi` callers (nt6500/adva/infinera TL1 batches, `doGather`, `iisnd`) send every command in the batch, and niimxd processes each just to reply `NIIMX^System congestion`. The "single route by which a shedding daemon still gets fed" rationale used to add the shed to `iimx_q` holds here — and wasn't.

---

### 7. `niimx_next_msgid` signed shift UB and 256-wide collision

**File:** `cnc/utility/src/niimxlib.c:419`

Seeds `((int32_t)getpid() << 8) + 1`, giving each process 256 IDs before wrapping into the neighbouring pid's range **and** doing a signed left shift into the sign bit whenever `getpid() >= 2^23`. `mkstemp` fixes rspfile name uniqueness but msgid still demuxes replies.

**Failure:**
- (a) On a host with `kernel.pid_max` raised (some container/cgroup environments accept up to 2^22 on 32-bit, higher on 64-bit), the shift into the sign bit is C99 UB; gcc is free to fold or produce a negative seed.
- (b) Two pipelining processes past 256 commands: A's 257th msgid collides with B's 1st. Reply demux by (ROUTER identity, msgid) protects wire routing, but any cross-process trace correlation or future msgid-keyed logic (like the pre-`mkstemp` spill-file bug this project already tripped over) matches wrong.

---

### 8. Inconsistent post-conditions across diverted exported symbols

**File:** `cnc/utility/src/iimx_snd.c:3689`

`iimx_local` and `iimx_sendx_pool` diverts leave `ri[].b.buf == NULL` on `IIMX_RQERROR` / `IIMX_RTIMEDOUT` / `IIMX_RERROR` entries. `iimx_sendx_multi` and `iimx_sendx_batch_wk` diverts **do** append `"FAILED^Communication Error"` / `"FAILED^Timed Out"` bodies for the same outcomes. The four exported symbols in `libinc.so` thus have inconsistent post-conditions for the same failure mode.

**Failure:** Out-of-tree caller of exported `iimx_sendx_pool` (or a future in-tree caller modelled after `iimx_sendx_batch_wk`'s callers) reads `ri[i].b.buf` without a NULL guard on entries whose state changed to `IIMX_RTIMEDOUT`. SIGSEGV in what looks like a working transaction. `iimx_sendx_batch_wk`'s divert would have supplied a body for the identical failure mode.

---

### 9. `niimx_xact` NIIMX^ path leaves `*nbytes` nonzero

**File:** `cnc/utility/src/niimxlib.c:872`

NIIMX^ rejection returns -1 with `*nbytes` set to the reply's actual length (line 867), while every other -1 path — congestion, unavailable, send-failed, timeout, desync — leaves `*nbytes` at 0 (set line 772). `niimx_recv`'s contract at `niimxlib.h:264` zeroes `nbytes` on failure; `niimx_xact`'s is silent, creating "rc<0 means body absent" for four paths and "rc<0 means body present, use nbytes" for one.

**Failure:** Migrating caller writes `if (rc<0) log_hex(*rsp, *nbytes)` or `memcpy(dst, *rsp, *nbytes)` expecting the `niimx_recv` convention. Four of five failure paths log/copy 0 bytes and truncate the diagnostic; the NIIMX^ path leaks a body-sized copy. Inconsistency is the class of contract mismatch that hides in code review.

---

### 10. `iimx_sendxn` `memset(rsp, 0, n)` runs before the divert

**File:** `cnc/utility/src/iimx_snd.c:669`

`memset(rsp, 0, n)` at the top of the function runs **before** the `niimx_enabled()` divert. If `rsp == NULL`, or `n` is negative (silent size_t promotion → huge write), this crashes on entry regardless of toggle state. The divert's own comment claims `n<1` is safe.

**Failure:** Any caller passing `rsp=NULL` or negative `n` hits the memset before the divert has a chance to guard. Pre-existing but the divert's docs and n-guard imply the whole path is safe when `n<=0` — misleading anyone auditing the divert.

---

### 11. `strncmp(body, "NIIMX^", 6)` without NULL guard

**File:** `cnc/utility/src/niimxlib.c:872`

Reads `body` without asserting non-NULL; `niimxlib.h` says "*rsp is malloc'd on success" but does not forbid a zero-length body. The current implementation of `niimx_recv` always `realloc`'s even for a 0-byte payload, so `body` is non-NULL today — but this relies on an incidental invariant, not the header's contract.

**Failure:** A future `niimx_recv` optimization that skips realloc when `bufsz==0` (a common "don't allocate empties" pattern) hands `niimx_xact` a NULL body, and `strncmp` segfaults. The mirror `strncmp` against `NIIMX_RSPFILE_PREFIX` a few lines below has the same issue.

---

## Cleanup / altitude / efficiency

### 12. Batch collect loop duplicated ~1200 lines across 4 diverts

**File:** `cnc/utility/src/iimx_snd.c:1799` (and :2399, :3424, :4055)

`iimx_sendx_multi`, `iimx_sendx_batch_wk`, `iimx_local`, `iimx_sendx_pool` all carry the same recv loop: identical `NIIMX_TIMEDOUT`/`NIIMX_DESYNC`/other-rc handling, identical `usleep(50000)` pace, identical msgid lookup, identical `NIIMX^` prefix test, identical `nlost`/`nsends` accounting. A `niimx_collect_batch(ri, ncmd, tmout, lost_body, lost_state)` helper would consolidate the recv-loop invariants that must stay in lock-step.

**Cost:** Any future tweak to collection semantics (new return code, pace-on-EINTR change, desync policy) must be repeated at four sites; a fix applied to three of four is a silent transport-behavior divergence between iimx entry points that only four distinct tests can catch.

---

### 13. Local-host collapse triplet duplicated 4 sites

**File:** `cnc/utility/src/iimx_snd.c:464` (and :674, :1842, :2465)

`gethostname` + `strcmp("localhost") || strcmp("2iimx") || strcasecmp(me,host)` is open-coded at four entry points. Per the design this set is now protocol between the client and niimxd; belongs in a single `niimx_normalize_host(const char*)` helper (or inside `niimx_xact`/`niimx_send`).

**Cost:** Add one alias — an IPv6 loopback, a config-tunable local name, an "ncland" entry — on one site and the toggle routes the same command as local from one entry point and as unknown-remote from another; niimxd answers the second with `NIIMX^Unknown host` and the failure looks like a load-balancing bug rather than a missing alias.

---

### 14. `niimx_xact` makes 4 probe syscalls per command

**File:** `cnc/utility/src/niimxlib.c:792`

Calls `niimx_congested()` (one `access()`) **and** `niimx_avail()` (a `socket()`/`connect()`/`close()` triple) on every single-command transaction. `iimx_sendx_call` and `iimx_sendxn` are the hot paths through `niimx_xact`; the header advises pipelined callers to shed once per batch to avoid per-command syscall cost, but the wrapper itself makes four syscalls per command.

**Cost:** A tight loop of 10 000 `iimx_xact` calls performs 10 000 `access()` plus 30 000 socket-lifecycle syscalls purely for probes, on top of one ZMQ round-trip each. The once-per-batch argument in the header is undercut by `niimx_xact`'s own behaviour. Cache the probes with a short TTL, or fold them into `niimx_send`'s error path via `ZMQ_SNDTIMEO`/`RCVTIMEO` which the code already sets.

---

### 15. `niimx_send` frame-1 skip-drop relies on libzmq internals

**File:** `cnc/utility/src/niimxlib.c:450`

The path relies on libzmq's internal `lb_t::sendpipe()` only committing `_more` on the success path — a libzmq implementation detail, not a public API contract. A future libzmq that commits the empty delimiter to the pipe before returning `EINTR` would leave a half-open message and no teardown, exactly the failure mode `niimx_send_frame`'s own comment warns about.

**Failure:** libzmq is upgraded to a version where an `EINTR` after the delimiter is queued still leaves `_more_out=true`. `niimx_send` returns -1 without dropping the socket. Caller retries; retry extends the still-open multipart message. Daemon parses one over-long message; every following command is one frame out of place. The msgid-constancy check inside `niimx_recv` catches it and returns `NIIMX_DESYNC` — but by then every other in-flight command is lost too.

---

## Refuted on code read (not reported)

| Candidate | Why refuted |
|---|---|
| `iimx_poll` `tmout<=0` hangs forever | Legacy loop has same `if (tmout>0) if ... break;` shape; comment at :954 documents parity |
| `iimx_local` treats `NIIMX^` as `IIMX_RFINISHED` | Code at :2618 explicitly tests `NIIMX^` prefix and sets `IIMX_RERROR` |
| `iimx_sendx_pool` `tmout<1→600` new default | Legacy applies same default at :4326 |
| `doGather` doesn't match `NIIMX^` prefix | Legacy `iimx_connect` failure body `FAILED^Could not connect to Host` didn't match `FAILED^Comm` either — not a regression |
| `iimx_sendx_call` divert routes remote through local `niimx.congested` | Real behavior change but already recorded in the design's 6 documented behavioural deltas |

---

## Deploy-time gaps still open (from the plan, not the review)

Recorded here because a reviewer will ask.

1. **No real `niimxd` has ever run against this.** Daemon-side behaviour is verified by extracting functions verbatim at build time and driving them, or against the `niimx_stub` test double. `niimxd` exits at `ddb_init(GET)` with CNC down.
2. **The `niimx.congested` flag lifecycle is compile-verified only.** If wrong, the failure mode is a permanently shed box. Watch on the first CNC-up target.
3. **Deploy is not partial-safe.** `niimxd` and `libinc.so` share the wire format and must ship together. `libinc.so` must reach `/usr/cnc/lib`; consumers carry no RPATH.
