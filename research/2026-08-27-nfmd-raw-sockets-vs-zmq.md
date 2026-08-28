# NFMD system_define: raw TCP sockets vs. the ZMQ transport

**Component:** `cnc/md` (the `md` mediation daemon and its per-link reader/writer processes)
**Date:** 2026-08-27
**Scope:** what `NFMD=` in `system_defines` controls, and specifically what `NFMD=1` changes about how the `md` link processes move bytes.

---

## TL;DR

`NFMD` is a system-define read at process start with:

```c
int nfmd = (-1);
get_sysdef_int("NFMD=", &nfmd, -1);
```

It selects the **transport** used by the `md` inter-machine link processes (the
`fep`/`bep` frame and message readers and writers). There are three regimes:

| `NFMD` value            | Transport for the link processes                         | Global set        |
|-------------------------|----------------------------------------------------------|-------------------|
| unset / `-1` / anything `<= 1024` and `!= 1` | **Legacy raw BSD/TCP sockets** (the historical default) | `MdUseZmq = -1`   |
| **`1`**                 | **ZeroMQ** `PUSH`/`PULL` sockets over TCP (per link)     | `MdUseZmq = 1`    |
| `> 1024`                | ZMQ **router / single-port multiplex** mode in the `md` parent, bound to that TCP port | `MdZmqPort = nfmd` |

The question at hand — "what does `NFMD=1` do regarding raw sockets vs. the ZMQ
library" — is the middle row: **`NFMD=1` flips every `md` link process from raw
sockets to ZeroMQ.**

---

## The switch: `NFMD=1` sets `MdUseZmq`

Each `md` child process (the readers/writers) runs the same idiom near startup.
Example from `cnc/md/src/fep_fep_wtr.c:166`:

```c
int nfmd = (-1);
get_sysdef_int("NFMD=", &nfmd, -1);
if (1 == nfmd) MdUseZmq = 1;

if (1 == MdUseZmq) {
    if (NULL == (ZmqContext = md_zmq_ctx_new())) {
        TRACE(0,"#ERROR: zmq_ctx_new() e:%d \"%s\"\n",errno,strerror(errno));
        ffw_exit(1);
    }
}
```

The same three lines appear in every link process:

- `cnc/md/src/fep_fep_rdr.c:161`
- `cnc/md/src/fep_fep_wtr.c:168`
- `cnc/md/src/l_fep_bep_frm_rdr.cpp:213`
- `cnc/md/src/l_fep_bep_frm_wtr.cpp:227`
- `cnc/md/src/l_fep_bep_msg_rdr.cpp:208`
- `cnc/md/src/l_fep_bep_msg_wtr.cpp:211`
- `cnc/md/src/l_bep_fep_frm_rdr.cpp:210`
- `cnc/md/src/l_bep_fep_frm_wtr.cpp:200`
- `cnc/md/src/l_bep_fep_msg_rdr.cpp:220`
- `cnc/md/src/l_bep_fep_msg_wtr.cpp:200`

`MdUseZmq` is the file-global flag declared in `cnc/md/src/l_md_sock.c:39`
(`int32_t MdUseZmq = (-1);`). It is the single condition that every transport
decision in the link processes keys off of. **It is only ever assigned the value
`1`** (from `NFMD=1`) or left at its `-1` default — see "Router mode" below for
why the `MdUseZmq > 1` code paths exist but are currently dormant.

---

## What "raw sockets" means (the `NFMD != 1` default)

When `MdUseZmq != 1`, the link processes use the historical hand-rolled TCP
socket layer in `cnc/md/src/l_md_sock.c`:

- **Connect / accept:** writers call `md_reconnect()` → `socket(CommDomain,
  SOCK_STREAM, 0)` + `connect()`; readers `accept()` via
  `get_client_socket()`. Buffer sizes, `SO_LINGER`, `SO_REUSEADDR`,
  `SO_SNDBUF`/`SO_RCVBUF` all set by hand.
- **Send:** `send_md_data()` — a blocking `send()` with manual partial-write
  handling and an `alarm(10)` watchdog around the loop.
- **Receive:** blocking `read()`/`recv()` on the fd, with `alarm(timer)` used to
  bound the wait so a stalled peer surfaces as an interrupted read.
- **Liveness / ping:** out-of-band `MSG_OOB` bytes (`send_oob_ack()`,
  `recv_oob_ack()`, `SIOCATMARK`) plus `SIGURG` handling implement the
  keep-alive PING/ACK between mates.
- **Addressing:** `/etc/services` (`getservbyname` on names like
  `fep%d-fep%d`) + `/etc/hosts` (`gethostbyname`) resolve the host/port.
- **Teardown:** `md_exit()` does `shutdown(Sd,2)` + `close(Sd)`.

This is the classic SysV/BSD sockets path — one dedicated TCP connection per
link, per direction.

---

## What `NFMD=1` switches to: the ZMQ library path

With `MdUseZmq == 1`, the same link processes instead use the ZeroMQ wrappers,
also in `cnc/md/src/l_md_sock.c`. The BSD-socket calls above are bypassed and
replaced by:

- **Context:** `md_zmq_ctx_new()` → `zmq_ctx_new()` (created once at startup,
  guarded by the `if (1 == MdUseZmq)` block shown above).
- **Writer endpoint:** `md_zmqconnect()` creates a **`ZMQ_PUSH`** socket and
  `zmq_connect()`s it. The endpoint comes from `getenv("MD_ZADDR")` if set,
  otherwise a synthesized `tcp://<host>:<port>`
  (`cnc/md/src/fep_fep_wtr.c:249`).
- **Reader endpoint:** `md_zmqbind()` creates a **`ZMQ_PULL`** socket and
  `zmq_bind()`s it (e.g. `tcp://*:<port>`).
- **Send:** `md_zmqsend()` → `zmq_msg_init_size()` + `zmq_msg_send()`.
- **Receive:** `md_zmqrecv()` → `zmq_msg_recv()` with `ZMQ_RCVTIMEO`.

### Why PUSH/PULL and how liveness is preserved

The ZMQ path is engineered to reproduce the failure semantics the raw-socket
PING/OOB scheme gave, because the rest of `md` (link-down detection, restart,
PING_MSG/PING_ALIVE checks) depends on "a dead peer shows up as a failed
read/send." The wrappers document this explicitly:

- **`ZMQ_RCVTIMEO`** in `md_zmqrecv()` bounds the receive the way `alarm(timer)`
  bounded the old `recv()`. Timeout → `EAGAIN` → return `-1`, which is treated
  exactly like the old interrupted read and re-arms the existing liveness logic.
  A mid-wait `EINTR` is retried, not reported as a dead link.
- **`ZMQ_SNDTIMEO`** in `md_zmqconnect()` bounds `md_zmqsend()`. A `PUSH` socket
  with no live peer goes *mute* and would otherwise block forever; the timeout
  lets a dead peer be reported (`EAGAIN` → `-1`) rather than hanging.
- **ZMTP heartbeats** (`ZMQ_HEARTBEAT_IVL` / `ZMQ_HEARTBEAT_TIMEOUT`, set on
  both bind and connect) tear the pipe down when a peer dies silently with no
  FIN/RST — which is precisely the condition that puts a `PUSH` socket into the
  mute state the `SNDTIMEO` guards against.
- `ZMQ_LINGER = 0` on close so a dying process does not block flushing.

Tunables live in `include/md.h` (`MD_ZMQ_HEARTBEAT_IVL_MS`,
`MD_ZMQ_HEARTBEAT_TIMEOUT_MS`, `MD_ZMQ_SNDTIMEO_MS`).

### Behavior differences to be aware of

- **No `MSG_OOB` / `SIGURG` ping** on the ZMQ path — liveness is heartbeats +
  send/recv timeouts instead. `md_exit()` also skips the `shutdown/close(Sd)`
  dance when `MdUseZmq == 1` (`cnc/md/src/l_md_sock.c:800`).
- **`zmq_connect()` is asynchronous** — it succeeds even with no peer present,
  so a non-NULL socket does **not** mean the far end is up (documented at
  `md_zmqconnect()`). Connection-up is inferred later from successful traffic /
  heartbeats.
- Message framing is ZMQ messages (length-delimited) rather than a byte stream,
  so the manual partial-write loop in `send_md_data()` has no analogue on the
  ZMQ side.

---

## Router mode (`NFMD > 1024`) — separate feature, noted for completeness

When `NFMD` is greater than 1024 it is interpreted as a **TCP port number**, and
the `md` **parent** daemon takes a different path (`cnc/md/src/md.c:119`):

```c
if (nfmd > 1024) {
    MdZmqPort = nfmd;
    if (NULL == ZmqContext) {
        if (NULL == (ZmqContext = md_zmq_ctx_new())) { ... exit(1); }
    }
}
```

In this mode the parent runs an event loop (`md_wait_onevent()`,
`cnc/md/src/md.c:170`), binds a single ZMQ socket on `MdZmqPort`
(`cnc/md/src/md.c:750`), and multiplexes all links over that one port using a
routing header prefix — `ZMQRTRPKTHDR` (34 bytes, `include/md.h:552`) carrying
an `MdZmqTag`, passed to children via the `MD_ZADDR` env var. The
`MdUseZmq > 1` branches in `md_zmqsend()`/`md_zmqrecv()`
(`cnc/md/src/l_md_sock.c:753,765`) are the child-side of this router protocol
(prepend/strip the routing tag).

**Important nuance:** in the current tree `MdUseZmq` is *only ever set to `1`*
(never `> 1`) — the child processes only enable ZMQ on `NFMD == 1`. So the
`MdUseZmq > 1` router-tag code and the parent's single-port multiplex are best
read as **in-progress / not-yet-wired scaffolding** for the consolidated router
design, not an active production path selected purely by `NFMD > 1024`. Treat
`NFMD=1` as the supported "use ZMQ" setting today.

---

## Quick reference

- **Turn on ZMQ transport for `md` links:** put `NFMD=1` in `system_defines`.
- **Leave it off / legacy raw TCP sockets:** omit `NFMD`, or set it to anything
  that is neither `1` nor `> 1024`.
- **Flag that everything keys off:** `MdUseZmq` in `cnc/md/src/l_md_sock.c`.
- **All transport wrappers:** `cnc/md/src/l_md_sock.c`
  (`md_zmqbind`/`md_zmqconnect`/`md_zmqsend`/`md_zmqrecv` vs.
  `md_reconnect`/`get_client_socket`/`send_md_data`/`recv_oob_ack`).
- **Tunables:** `include/md.h`.

### Related

The `md` msg/frame reader/writer pairing and its ZMQ-mode restart behavior are
covered in the separate note on the 60s ZMQ restart-storm (memory:
`md-msg-rdr-wtr-restart-storm`).
