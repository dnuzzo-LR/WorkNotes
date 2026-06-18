# MD ZMQ Review — NFMD-gated libzmq path

Scope: `cnc/md/src/md.c`, `cnc/md/src/l_md_sock.c`, `cnc/md/src/fep_fep_{rdr,wtr}.c`, `cnc/md/src/l_{bep_fep,fep_bep}_{frm,msg}_{rdr,wtr}.cpp`. Code gated by `get_sysdef_int("NFMD=", &nfmd, -1)`.

Introduced in commit `cc71b807b` (inc52.132 source drop, 2025-12-02).

## Critical

**1. `md.c:789` — wrong socket assigned to every poll item**
```c
poll_items[nitems].socket=ZmqMdSocket;  // should be pptr->zmqsocket
```
Loop populates per-proc poll slots but uses inbound MD socket for all. Polling broken — md never sees per-worker traffic.

**2. `md.c:822-824` — dead `forward` assignment, infinite loop**
```c
if (0 == pollitem) forward=(-1);
else forward=(-1);  // both branches identical
```
Plus the `while(1)` body has only an `if (0==pollitem)` branch. When a non-MD socket fires (`pollitem!=0`), inner loop body is empty → infinite loop, no break, no progress.

**3. `md.c:885-886` — `break` before `zmq_msg_close(dmsg)`**
```c
break;
zmq_msg_close(&dmsg);  // unreachable
```
Leaks `dmsg` on every successful forward. Long-run process → ZMQ buffer exhaustion.

**4. `md.c:829` `while(1)` never terminates if tag-match fails** — `msg` consumed but no `Procs[loop].tag` matches `hdr`, for-loop exits without break, outer `while(1)` re-enters `zmq_msg_recv` on already-drained socket → blocks forever. Also leaks `msg` (no `zmq_msg_close` on no-match path).

**5. Inconsistent NFMD semantics between md.c and workers**
- `md.c:119`: `if (nfmd > 1024) MdZmqPort=nfmd;` (port number)
- workers (all 9 rdr/wtr): `if (1 == nfmd) MdUseZmq=1;` (must be literally 1)

Setting `NFMD=5555` enables md.c zmq path, leaves workers on legacy sockets. Setting `NFMD=1` enables workers, leaves md.c on `wait()`. No value enables both. **Feature can't be turned on coherently.**

**6. `l_md_sock.c:589` — `zmq_setsockopt` arg bug**
```c
zmq_setsockopt(rep_socket,ZMQ_LINGER,0,0);
```
3rd arg is `optval` pointer, 4th is `optlen`. Passing `0,0` = NULL ptr + zero size. Setsockopt no-ops or errors silently. Should be:
```c
int linger=0; zmq_setsockopt(s, ZMQ_LINGER, &linger, sizeof(linger));
```

## High

**7. `md.c:221` `tag` stack array uninitialized; only written in some branches**
Lines 300/323/437 (fep_fep paths) build `cmd_buf` without `sprintf(tag,...)`. Then `strcpy(Procs[found].tag,tag)` at 363/466 copies indeterminate stack bytes. Routing key garbage. Also `tag` reused across loop iterations — stale value from prior iteration can leak into next.

**8. `md.c:899 md_mkzmqaddr` magic offsets `tag+13` and `addr+22`**
```c
snprintf(addr,n-1,"ipc:///usr/cnc/data/md-%s",tag+13);
sptr=addr+22;
```
Hardcoded prefix skip assumes `FEP_BEP_FRM_RDR` macro length. Any change → silent corruption. Tag prefix is `"<macro>:NN:NN"` — `tag+13` is fragile.

**9. Mixed `MdZmqPort` thresholds** — `md.c:170` uses `>1024`, `md.c:580` uses `>0`. If port set 1..1024 by accident, env exported but main loop stays on `wait()`. Pick one threshold.

**10. `MdZmqTag` global never initialized but used in `l_md_sock.c:673`**
```c
if (MdUseZmq>1) memcpy(zmq_msg_data(&msg), MdZmqTag, ZMQRTRPKTHDR);
```
Defined `char MdZmqTag[40]` at file scope (zero-init BSS), never written. The `>1` mode unreachable (no caller sets `MdUseZmq` to anything but 1) → dead/half-finished feature. Either finish it or strip it.

**11. ZMQ_PULL/PUSH socket type — no error reporting back to sender**
PUSH blocks at HWM by default; receiver crash invisible to sender. Old TCP path detected lost connection via `recv()==-1`; PUSH path won't. `bffr_recv_data` zmq-branch returns `md_zmqrecv` value — original returned `FAILURE`-checked but len-bound; new path skips the multi-read loop and ignores `bytes < len` mismatch (line 504).

## Medium

**12. `l_md_sock.c:575,597` — error msgs say `ZMQ_REP`/`ZMQ_RSP`, code uses `ZMQ_PULL`/`ZMQ_PUSH`**
Misleading traces during debug.

**13. `md.c:835` `zmq_msg_init_size(&msg, ZMQMAXPKTSZ+ZMQRTRPKTHDR)` then `zmq_msg_recv`**
`zmq_msg_recv` does not honor pre-allocated size — uses internal buffer. Use `zmq_msg_init(&msg)`. Allocation wasted.

**14. `md.c:42` `int Port;` global** — no apparent purpose in this file, untouched. Either dead or shadow of extern from `md.h:?` causing multiple-def link risk depending on linker.

**15. md.c:580 `setenv("MD_ZADDR",...)` in parent before fork** — leaks env into ALL future children (not just current `pptr`). Next forked child gets stale `MD_ZADDR` until next setenv. Should set in child between fork and execv, or use `posix_spawn`-style explicit env.

**16. `md.c:765 sleep(20)` inside `md_wait_onevent`** — blocks SIGALRM-driven cnfg recheck for 20s on socket-setup failure. Plus the alarm is never re-armed in the zmq path (line 174: `if (MdZmqPort>1024) … continue;` skips `alarm(CNFG_CHG_SECS)`). **Config recheck disabled entirely when zmq enabled.** SIGUSR2/SIGTERM still fire but `md_recheck_cnfg` never triggered.

**17. `md_load_cnf` builds Procs[].zmqsocket via `md_zmqconnect`** — but `md_zmqconnect` creates PUSH socket. PUSH connects, can't recv. Yet `md_wait_onevent` puts these in `ZMQ_POLLIN`. PUSH never has POLLIN. Polling broken from both ends (see #1).

## Minor

**18. `Procs[found].tag` size 256 but `ZMQRTRPKTHDR=34`** — header truncates tag, no length check. Tag uniqueness within 34 bytes not verified.

**19. Heavy `TRACE(0,...)` and `TRACE(4,...)` in hot paths** (`md_wait_onevent`, `md_zmqsend`) — verbose at default debug. Demote to D5+.

**20. `md_zmq_ctx_new()` wrapper** — pointless indirection over `zmq_ctx_new()`. Either add ZMQ_IO_THREADS tuning or drop.

**21. No `zmq_ctx_destroy` on exit** — daemon life, no leak in practice, but `md_bailout` doesn't close `ZmqMdSocket` or per-proc sockets either.

**22. inc52.132 dropped `.c` versions and replaced with `.cpp` for many files** (mdsrc.mk change). Build artifact mixing — confirm `mdsrc.mk` no longer compiles the stale `.c` siblings.

## Recommendations

- Pick single NFMD semantic: e.g. `NFMD=<port>` everywhere, derive `MdUseZmq=(MdZmqPort>0)` in workers. One sentinel, one truth.
- Fix bugs 1–4 before any further test. Code path can't function as written.
- Strip `MdUseZmq>1` router-with-tag mode entirely OR finish it (init `MdZmqTag`, wire forwarding loop). Half-state invites confusion.
- Replace `tag+13`/`addr+22` with proper format-aware parsing or pass `service` field directly.
- Add `zmq_msg_close` on all paths in `md_wait_onevent`.
- Re-arm SIGALRM in zmq main loop, else dynamic config reload dies.

Net: feature looks like work-in-progress prototype, not ready. Recommend hold NFMD path off-by-default in production builds until #1–6 fixed.
