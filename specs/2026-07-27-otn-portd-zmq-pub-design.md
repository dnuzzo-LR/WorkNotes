# otn_portd: forward postgres NOTIFY to niimxd ZMQ bus

**Date:** 2026-07-27
**Author:** Dan Nuzzo
**Branch:** `otn-portd-zmq`
**Status:** Approved design, ready for implementation plan

## Problem

`otn_portd` (cnc/otn_port/src/otn_portd.c) currently subscribes to postgres
`LISTEN` channels and fans notifications out to `RdbSock` client processes
via the existing `librdb_notify` protocol. We want the same NOTIFYs to also
land on the niimxd XPUB/XSUB event bus so any subscriber connected to
`nfdb_pub.sock` (e.g. `ncland`, `nfdb_client`) can consume them without
adding an `RdbSock` dependency.

The behavior must be opt-in via a `system_defines` toggle so we can enable
it per site without redeploying.

## Non-goals

- Not touching the existing `RdbSock` notify path. It stays exactly as-is.
- Not modifying `librdb_notify`. The ZMQ pub lives entirely inside
  `otn_portd`.
- Not adding runtime enable/disable. Toggle is read once at startup;
  changing it requires a daemon restart.
- Not changing niimxd. It already binds the XPUB (`nfdb_pub.sock`) and
  XSUB (`nfdb_sub.sock`) endpoints — see
  `cnc/niimx/src/niimxd.cpp:866-877`.

## Architecture

New self-contained publisher module:

```
cnc/otn_port/include/otn_port_zmq_pub.h
cnc/otn_port/src/otn_port_zmq_pub.c
```

Public API (three functions, C linkage):

```c
/**
 * @brief Initialize the ZMQ publisher.
 *
 * Reads system_define `OTN_PORTD_ZMQ_PUB`. If 0 or absent, the module
 * stays disabled and _notify() becomes a cheap no-op. If enabled,
 * creates a ZMQ context, opens a ZMQ_PUB socket, connects to niimxd's
 * XSUB endpoint (ipc:///usr/cnc/data/nfdb_sub.sock), and sleeps 1s to
 * cover the PUB slow-joiner problem.
 *
 * @return 0 on success or when disabled by sysdef; -1 on hard ZMQ
 *         failure (module left disabled, caller may continue).
 */
int  otn_port_zmq_pub_init(void);

/**
 * @brief Tear down publisher (idempotent). Called on shutdown.
 */
void otn_port_zmq_pub_close(void);

/**
 * @brief Publish one postgres NOTIFY to the niimxd bus.
 *
 * No-op when the module is disabled. Best-effort: failures are logged
 * at TRACE(D3) and do NOT propagate to the caller — the existing
 * RdbSock fanout must not be impacted by ZMQ hiccups.
 *
 * @param channel  postgres relname (topic tail).
 * @param extra    postgres NOTIFY payload string (may be empty).
 * @return 0 on send (or disabled); -1 on transient send failure.
 */
int  otn_port_zmq_pub_notify(const char *channel, const char *extra);
```

Internal state (file-scope statics):

```c
static void *g_ctx  = NULL;
static void *g_sock = NULL;   /* NULL == module disabled */
```

The socket handle doubles as the enabled flag. All three API functions
check `g_sock` before touching ZMQ.

### Integration points in otn_portd.c

Two changes to `cnc/otn_port/src/otn_portd.c`:

1. `main()` — call `otn_port_zmq_pub_init()` once, after `mm_cmm()` and
   before `pthread_create(..., pg_listen, ...)`. Return value is not
   checked (init failure is logged internally; the RdbSock path must
   run either way). Success/failure logs go through the existing
   `TRACE(...)` facility.

2. `pg_listen()` — inside the existing
   `while ((notify = PQnotifies(con)) != NULL)` loop, add one call:

   ```c
   otn_port_zmq_pub_notify(notify->relname, notify->extra);
   ```

   Placed after the RdbSock fanout, before `PQfreemem(notify)`. Order
   matters: RdbSock delivery is the incumbent contract and must not be
   delayed by ZMQ work.

No other file in `cnc/otn_port/` is touched.

## Wire format

- **Transport:** ZMQ PUB → niimxd XSUB → niimxd XPUB → subscribers.
- **Endpoint (publisher):** `ipc:///usr/cnc/data/nfdb_sub.sock`. Hard-coded
  constant in `otn_port_zmq_pub.c` (matches
  `ENDPOINT_SUB_DEFAULT` in `cnc/niimx/src/nfdb_proto.h:40`).
- **Multipart:** two frames.

**Frame 0 (topic):** `db.otnport.<channel>`
- `<channel>` = postgres `relname` verbatim (already a C identifier —
  channel names are constrained by `librdb_notify`, no escaping needed).
- `db.` prefix reserved for the niimxd DB event bus; `otnport.`
  namespace prevents collision with existing `db.ne.*` (nfdb NE
  lifecycle).

**Frame 1 (body, flow-YAML):** `{channel: <relname>, extra: '<escaped>'}`
- Body is a single-line flow-mapping matching the convention consumed
  by `cnc/ncland/src/ncland_notify.cpp` (subscribers already know how
  to parse this shape).
- `<escaped>` = the postgres NOTIFY `extra` string transformed as
  follows, then wrapped in single quotes:
  - Each single quote (`'`) doubled (`'` → `''`) — flow-YAML rule.
  - Each CR/LF (`\r`, `\n`) replaced with a single space — flow-YAML
    single-quoted scalars must stay on one line, and postgres allows
    LF inside NOTIFY payloads up to 8000 bytes.
  No other transforms.

**Example wire (topic frame + body frame):**

```
db.otnport.otn_port_alarm_change
{channel: otn_port_alarm_change, extra: '17,foo'}
```

## Toggle

- **Name:** `OTN_PORTD_ZMQ_PUB`
- **Location:** `system_defines` file (path resolved by `FEAT_SYSTEM` in
  `cnc/utility/src/system_defines64.c`).
- **API:** `get_sysdef_int("OTN_PORTD_ZMQ_PUB=", &v, 0)`. Non-zero
  enables. Default = disabled.
- **When read:** once, at the top of `otn_port_zmq_pub_init()`.
- **Change to take effect:** restart otn_portd.

## Failure handling

| Failure mode                           | Behavior                                      |
|----------------------------------------|-----------------------------------------------|
| sysdef absent or `=0`                  | Module stays disabled; init returns 0.        |
| `zmq_ctx_new()` fails                  | Log D0, leave disabled, init returns -1.      |
| `zmq_socket(ZMQ_PUB)` fails            | Log D0, destroy ctx, disabled, init returns -1. |
| `zmq_connect()` fails                  | Log D0, close socket + destroy ctx, disabled, init returns -1. |
| `zmq_msg_send()` fails in `_notify()`  | Log D3, return -1. Do NOT disable module (transient errors during startup are expected). |

Init failure never aborts otn_portd — the existing RdbSock path is the
contract and must survive ZMQ misconfiguration.

## Build

Add to `cnc/otn_port/src/otn_port.mk`:

- Compile `otn_port_zmq_pub.c` into the `otn_portd` binary.
- Link `-lzmq`. Use the `mklib` clone idiom (project memory
  `project_mklib_clone_idiom`) if `otn_port.mk`'s existing pattern
  requires it; otherwise append `-lzmq` in the LINUX branch
  (project is Linux-only per `feedback_linux_only`).

Include path: `<zmq.h>` — already available system-wide (niimxd,
ncland, md all use it directly).

## Testing

Manual verification only. No new automated tests.

1. **End-to-end with sysdef enabled:**
   - Set `OTN_PORTD_ZMQ_PUB=1` in system_defines.
   - Ensure `niimxd` is running (XPUB/XSUB bound).
   - Start `otn_portd`. Confirm trace log line reporting ZMQ pub connected.
   - In a second shell, subscribe to the bus:
     `nfdb_client -e ipc:///usr/cnc/data/nfdb_pub.sock` and issue the
     subscribe command for `db.otnport.` prefix.
   - Trigger a postgres NOTIFY on a channel otn_portd listens to
     (e.g. `psql -c "NOTIFY otn_port_alarm_change, '17,test';"`).
   - Verify the two-frame message arrives with expected topic + body.
   - Verify existing RdbSock subscribers still receive the notify.

2. **Sysdef disabled (default):**
   - Remove or set `OTN_PORTD_ZMQ_PUB=0`.
   - Restart otn_portd; confirm trace line "otn_port_zmq_pub: disabled".
   - Confirm no ZMQ socket is opened (`lsof -p $(pidof otn_portd) | grep sock`).
   - Confirm existing RdbSock notify path unchanged.

3. **Init failure:**
   - Point `nfdb_sub.sock` at an invalid path (temp move) or stop
     niimxd. Enable the sysdef, restart otn_portd.
   - Confirm otn_portd continues running, error is logged, RdbSock
     path still works.

## Open questions

None outstanding — design approved 2026-07-27.

## References

- `cnc/otn_port/src/otn_portd.c` — target daemon.
- `cnc/niimx/src/niimxd.cpp:866-877` — XPUB/XSUB bind.
- `cnc/niimx/src/nfdb_proto.h:33-40` — endpoint constants.
- `cnc/ncland/src/ncland_notify.cpp` — reference subscriber (same wire
  format on the other end).
- `cnc/md/src/nfsend.cpp:146-164` — reference PUB publisher (slow-joiner sleep pattern).
- `cnc/utility/src/system_defines64.c:92-125` — `get_sysdef_int` API.
- Project memories: `project_nfdb_event_bus`, `project_mklib_clone_idiom`,
  `feedback_linux_only`.
