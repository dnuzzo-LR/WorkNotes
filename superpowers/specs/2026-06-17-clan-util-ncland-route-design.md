# clan_util — Route Supported dtypes to ncland (#5)

**Status:** design approved 2026-06-17. Implementation plan to follow.
**Predecessor:** `2026-06-16-ncland-mqueue-ctl-design.md` (#4 ncland mqueue cutover).
**Branch:** new branch off `ncland-start` (per implementation choice).
**Memory references:** `project_ncland_is_clan_rewrite` notes "per-client migration" as a deferred follow-up to #4 — this design is that work.

---

## 1. Scope & Constraints

Update `clan_write` / `clan_write_ctag` in `cnc/utility/src/clan_util.c` so that for dtypes listed in `/usr/cnc/lib/data/ncland/ncland.json`, the command is delivered to the **ncland** daemon via the POSIX mqueue `/ncland_ctl` (as `struct dacs_msg`) instead of the legacy clan SysV `Dacsq` (as `struct di_int`). For dtypes NOT in the allowlist, the existing legacy `msgsnd(c->iq, dptr, len, 0)` path runs unchanged.

### In scope

- `clan_write` and `clan_write_ctag` only (TL1/CLI path to legacy clan).
- New module `clan_ncland_route.{h,c}` in `cnc/utility/src/` that owns:
  - Hand-rolled JSON parser for `/usr/cnc/lib/data/ncland/ncland.json` (schema `{"version":1,"dtypes":[<int>,...]}`).
  - Lazy `pthread_once_t` init of the dtype allowlist + the mqd_t.
  - di_int → dacs_msg conversion.
  - `mq_send` to `/ncland_ctl`.
- Public API: `clan_ncland_supports_dtype(int dtype)` + `clan_ncland_send(const struct di_int *, int)`.
- Build: `clan_ncland_route.o` added to libutillib; `-lrt` added to libutillib's LIBS (cascades to consumers).
- Unit tests for the JSON parser + membership + send happy path (test-seam setters).
- **In-code TODO comments** at each deferred-item touch point (hot-reload, mq_send retry, future legacy-clan deletion) so future devs find them while reading the code, not just the spec.

### Out of scope

- `restclan_write_ctag` (REST path). Separate daemon (`restclan`); ncland doesn't accept REST.
- Netclan/netcland branch inside `clan_write_ctag` (NETCONF on multi-NETCLAN NEs). Active separate daemon (`netcland`).
- Hot-reload of `ncland.json` (SIGHUP, inotify). Operators restart consumers if the allowlist changes. **Tracked via in-code TODO at `load_cfg_once`.**
- Per-call retry of mq_send failure. Caller decides. **Tracked via in-code TODO at the `mq_send` call.**

---

## 2. Architecture

```
                    ┌────────────────────────────┐
                    │  clan_write / _write_ctag  │  cnc/utility/src/clan_util.c
                    │  builds struct di_int *dptr│
                    └──────────┬─────────────────┘
                               │
            ┌──────────────────┴──────────────────┐
            │ clan_ncland_supports_dtype(neType)? │  cnc/utility/src/clan_ncland_route.c
            └──────────────┬──────────────────┬───┘
                       yes │              no  │
                           ▼                  ▼
            ┌──────────────────────┐  ┌─────────────────────────┐
            │ clan_ncland_send(    │  │ msgsnd(c->iq, dptr, len)│
            │   dptr, len) →       │  │ legacy clan SysV Dacsq  │
            │   di_int→dacs_msg,   │  └─────────────────────────┘
            │   mq_send /ncland_ctl│
            └──────────┬───────────┘
                       ▼
            POSIX mqueue /ncland_ctl → ncland → NE → screen
```

### 2.1 Lifecycle

- **Config load.** `pthread_once_t` gates a one-shot initializer that calls `parse_ncland_json("/usr/cnc/lib/data/ncland/ncland.json", &arr, &n)`. On success: stash in `static int *g_dtypes` + `static size_t g_dtypes_n`; set `g_cfg_ok = true`. On missing/malformed: TRACE WARN, leave `g_cfg_ok = false`, allowlist stays empty → every dtype routes to legacy. **Re-load is not supported; consumers must restart after editing the allowlist.**
- **mqd_t open.** Separate `pthread_once_t` for `mq_open("/ncland_ctl", O_WRONLY | O_NONBLOCK)`. On failure: TRACE WARN, `g_mqd` stays `(mqd_t)-1` and every `clan_ncland_send` returns -1.
- **Teardown.** None registered. Process exit closes the mqd implicitly. No `atexit` handler (keeps the module simple; ncland-side `mq_unlink` removes the kernel object on its own shutdown).

### 2.2 Thread safety

clan_util is called from multi-threaded consumers (dacsproc, fe). `pthread_once_t` guarantees a single init per process. The dtype lookup is read-only after init (no lock needed). `mq_send` is thread-safe on Linux; the static mqd_t can be shared across threads safely.

### 2.3 Error handling

- `parse_ncland_json` rejects: file > 64 KB (sanity cap), missing `"version"` or value ≠ 1, missing `"dtypes"`, non-integer in dtypes[]. Returns -1; caller treats as empty allowlist.
- `clan_ncland_send` returns -1 on: mqd never opened, mq_send EAGAIN (queue full — ncland too slow), mq_send EMSGSIZE (host sysctl `fs.mqueue.msgsize_max` < sizeof(struct dacs_msg)), other errno. `errno` preserved on EAGAIN/EMSGSIZE. **No fallback** to the legacy SysV path — caller learns the dispatch failed and decides.
- `clan_write_ctag` returns whatever `clan_ncland_send` returns. Existing callers already handle negative return values from `msgsnd`.

---

## 3. Types & APIs

### 3.1 Public API (`clan_ncland_route.h`)

```c
#ifndef CLAN_NCLAND_ROUTE_H
#define CLAN_NCLAND_ROUTE_H

#include <stdbool.h>
#include <di.h>           /* struct di_int */

#ifdef __cplusplus
extern "C" {
#endif

/** @brief Return true if @p dtype is listed in
 *  /usr/cnc/lib/data/ncland/ncland.json and should be routed to ncland.
 *  Loads + caches config on first call. Missing/malformed config →
 *  TRACE WARN once + return false for every dtype (legacy clan fallback).
 *  Thread-safe; idempotent.
 */
bool clan_ncland_supports_dtype(int dtype);

/** @brief Convert @p src into a struct dacs_msg and post it to
 *  /ncland_ctl. Caller already determined this dtype is ncland-supported.
 *
 *  @param src  Source di_int populated by clan_write_ctag.
 *  @param len  msgsnd-style length (= DI_INT_SZ(...)). Unused — the
 *              dacs_msg length is recomputed from dm_text. Param kept
 *              for symmetry with msgsnd's signature.
 *  @return 0 on success; -1 on mqd-not-open or mq_send failure.
 *          errno preserved.
 */
int clan_ncland_send(const struct di_int *src, int len);

#ifdef __cplusplus
}
#endif

#endif
```

### 3.2 di_int → dacs_msg field mapping

| `di_int` (src) | `dacs_msg` (dst) | Notes |
|---|---|---|
| `di_tty` | `dm_tty` | Originator routing (post `setClanPortFunc` mapping). |
| `di_dacsid` | `dm_dacsid` | NE id. Cast int → unsigned (20-bit bitfield). |
| `di_slot` | `dm_slot` | SNMP slot or `SLOT_CLI`. |
| `di_slot_tp` | `dm_slot_tp` | DST_CLI / DST_TL1 / etc. |
| `di_data` | `u.dm_text` | Colon-delim `"ctag:tmout:rtrv:quiet:cmd"` — same format both sides. |
| — | `dm_old_tty = 0` | di_int has no `di_old_tty`. |
| — | `dm_type = DMTYPE_TEXT_MSG` | All inbound commands are text. |
| — | `dm_to_screen = 0` | Inbound; ncland sets it on response. |
| — | `dm_reason = 0` | Inbound; ncland sets it on response. |
| `di_seq` / `di_orig_cnc` / `di_user_priv` / `di_msgdead` / `di_active` | dropped | No equivalent. ncland correlates responses via `dm_tty`. |

### 3.3 Conversion implementation

```c
static size_t di_int_to_dacs_msg(const struct di_int *src, int src_len,
                                 struct dacs_msg *dst)
{
    (void)src_len;
    memset(dst, 0, sizeof(*dst));
    dst->dm_tty       = src->di_tty;
    dst->dm_old_tty   = 0;
    dst->dm_slot      = src->di_slot;
    dst->dm_slot_tp   = src->di_slot_tp;
    dst->dm_dacsid    = (unsigned)src->di_dacsid;
    dst->dm_type      = DMTYPE_TEXT_MSG;
    dst->dm_to_screen = 0;
    dst->dm_reason    = 0;

    strncpy(dst->u.dm_text, (const char *)src->di_data,
            sizeof(dst->u.dm_text) - 1);
    dst->u.dm_text[sizeof(dst->u.dm_text) - 1] = '\0';

    return offsetof(struct dacs_msg, u) + strlen(dst->u.dm_text) + 1;
}
```

---

## 4. JSON Parser

Schema: `{"version":1,"dtypes":[1,2,3,...]}`. Whitespace-tolerant, single-pass scan, no recursion, no external dep.

```c
/* Returns 0 on success (sets *out_arr + *out_n via malloc).
 * Returns -1 on file/parse/schema error. */
static int parse_ncland_json(const char *path, int **out_arr, size_t *out_n)
{
    *out_arr = NULL; *out_n = 0;
    FILE *fp = fopen(path, "r");
    if (!fp) return -1;
    fseek(fp, 0, SEEK_END);
    long sz = ftell(fp);
    fseek(fp, 0, SEEK_SET);
    if (sz <= 0 || sz > 64 * 1024) { fclose(fp); return -1; }
    char *buf = (char *)malloc((size_t)sz + 1);
    if (!buf) { fclose(fp); return -1; }
    size_t nread = fread(buf, 1, (size_t)sz, fp);
    fclose(fp);
    buf[nread] = '\0';

    /* version check */
    const char *p = strstr(buf, "\"version\"");
    if (!p) { free(buf); return -1; }
    p += strlen("\"version\"");
    while (*p && (*p == ' ' || *p == '\t' || *p == ':')) p++;
    if (atoi(p) != 1) { free(buf); return -1; }

    /* dtypes[] */
    p = strstr(buf, "\"dtypes\"");
    if (!p) { free(buf); return -1; }
    p = strchr(p, '[');
    if (!p) { free(buf); return -1; }
    p++;

    size_t cap = 16, n = 0;
    int *arr = (int *)malloc(cap * sizeof(int));
    if (!arr) { free(buf); return -1; }

    while (*p && *p != ']') {
        while (*p && (*p == ' ' || *p == '\t' || *p == '\n' ||
                      *p == '\r' || *p == ',')) p++;
        if (*p == ']' || *p == '\0') break;
        if (*p != '-' && !(*p >= '0' && *p <= '9')) {
            free(arr); free(buf); return -1;
        }
        char *endp = NULL;
        long v = strtol(p, &endp, 10);
        if (endp == p) { free(arr); free(buf); return -1; }
        if (n == cap) {
            cap *= 2;
            int *grow = (int *)realloc(arr, cap * sizeof(int));
            if (!grow) { free(arr); free(buf); return -1; }
            arr = grow;
        }
        arr[n++] = (int)v;
        p = endp;
    }
    free(buf);
    *out_arr = arr;
    *out_n = n;
    return 0;
}
```

**Rejections:** file > 64 KB, missing `"version"`, version ≠ 1, missing `"dtypes"`, missing `[`, non-numeric token. Each → -1, caller treats as empty allowlist.

---

## 5. Init + Send (Module Internals) — with In-Code TODOs

Comments at each deferred-item touch point so future devs find the open question by reading the code, not just the spec.

```c
static pthread_once_t  g_once_cfg = PTHREAD_ONCE_INIT;
static int            *g_dtypes   = NULL;
static size_t          g_dtypes_n = 0;
static bool            g_cfg_ok   = false;

static pthread_once_t  g_once_mqd = PTHREAD_ONCE_INIT;
static mqd_t           g_mqd      = (mqd_t)-1;

static void load_cfg_once(void)
{
    /* TODO #5 hot-reload: pthread_once means /usr/cnc/lib/data/ncland/ncland.json
     * is read exactly once per process lifetime. Editing the allowlist requires
     * restarting every consumer (dacsproc, fe, arm/qrycga, etc.). If ops needs
     * live reload, add SIGHUP handler or inotify watch here and rebuild
     * g_dtypes[] under a rwlock. Not done today: deployment cadence is low,
     * lock-free read path is cheap to preserve. See spec §10 for context. */
    int *arr = NULL; size_t n = 0;
    if (parse_ncland_json("/usr/cnc/lib/data/ncland/ncland.json", &arr, &n) == 0) {
        g_dtypes = arr; g_dtypes_n = n; g_cfg_ok = true;
        TRACE(D3, "clan_ncland_route: loaded %zu dtypes\n", n);
    } else {
        TRACE(D0, "clan_ncland_route: ncland.json missing/malformed; "
                  "routing nothing to ncland\n");
    }
}

static void open_mqd_once(void)
{
    g_mqd = mq_open("/ncland_ctl", O_WRONLY | O_NONBLOCK);
    if (g_mqd == (mqd_t)-1)
        TRACE(D0, "clan_ncland_route: mq_open(/ncland_ctl) failed: %s\n",
              strerror(errno));
}

bool clan_ncland_supports_dtype(int dtype)
{
    pthread_once(&g_once_cfg, load_cfg_once);
    if (!g_cfg_ok) return false;
    for (size_t i = 0; i < g_dtypes_n; i++)
        if (g_dtypes[i] == dtype) return true;
    return false;
}

int clan_ncland_send(const struct di_int *src, int len)
{
    pthread_once(&g_once_mqd, open_mqd_once);
    if (g_mqd == (mqd_t)-1) return -1;

    struct dacs_msg dst;
    size_t dlen = di_int_to_dacs_msg(src, len, &dst);

    /* TODO #5 retry: single mq_send attempt; -1 on EAGAIN (queue full,
     * ncland too slow). ncland's mq_maxmsg defaults to 10; if production
     * shows persistent EAGAIN under load, options are:
     *   (a) bump ncland's mq_maxmsg (ncland_mq_init attr) — bounded by
     *       /proc/sys/fs/mqueue/msg_max (default 10 on RHEL 8).
     *   (b) add bounded retry loop here with a short sleep between attempts.
     * Both are deferred until the failure mode is observed. See spec §10. */
    if (mq_send(g_mqd, (const char *)&dst, dlen, 0) != 0) {
        TRACE(D0, "clan_ncland_send: mq_send neid=%u tty=%ld: %s\n",
              dst.dm_dacsid, dst.dm_tty, strerror(errno));
        return -1;
    }
    return 0;
}
```

---

## 6. clan_write_ctag Splice — with Legacy-Deletion TODO

```c
    /* #5 route: if this dtype is owned by ncland, send via POSIX mqueue
     * instead of the legacy clan SysV queue. No fallback on failure —
     * caller learns the dispatch failed and decides retry/reporting. */
    if (clan_ncland_supports_dtype(neType)) {
        TRACE(D3, "%s: routing neid=%d dtype=%d to ncland\n",
              __func__, c->dacsid, neType);
        return clan_ncland_send(dptr, len);
    }

    /* TODO #5 legacy-clan retirement: once every dtype has migrated into
     * /usr/cnc/lib/data/ncland/ncland.json, this msgsnd path + c->iq +
     * the CLAN.iq setup in clan_open()/clan_open_ctag() can be deleted.
     * Migration cadence is driven by NE-type validation against ncland.
     * See ~/docs/superpowers/specs/2026-06-17-clan-util-ncland-route-design.md §10. */
    int rc = msgsnd( c->iq, dptr, len, 0 );
```

---

## 7. Build Wiring (`util.mk`)

Two changes:

1. Add `clan_ncland_route.o` to libutillib's object list (inspect util.mk for the current pattern; mirror exactly).
2. Add `-lrt` to libutillib's published LIBS variable so every consumer's link line pulls it in. If libutillib is a static archive with no published LIBS variable, `-lrt` goes into the link lines of each consumer .mk that links libutillib AND calls clan_write (per explore: `cnc/dacsproc`, `cnc/arm`, `cnc/fe`).

Resolved in the plan based on util.mk inspection.

---

## 8. Testing

### 8.1 Unit tests (`clan_ncland_route_tests.c`)

Standalone test binary in `cnc/utility/src/` using `assert()`-based checks. New util.mk target.

| Test | Asserts |
|------|---------|
| Parse happy path | Tmp `ncland.json` with `{"version":1,"dtypes":[1,2,207]}` → n==3, arr[]={1,2,207}. |
| Parse rejects bad version | `{"version":2,"dtypes":[]}` → -1. |
| Parse rejects missing dtypes | `{"version":1}` → -1. |
| Parse rejects non-int element | `{"version":1,"dtypes":[1,"foo",3]}` → -1. |
| Membership hit/miss | Via `clan_ncland_route_set_dtypes_for_test`, install {1,2,207}; assert hit on 207, miss on 99. |
| `clan_ncland_send` round-trip | Via `clan_ncland_route_set_mqd_for_test`, install tmp mqd; build a di_int, call send, mq_receive on the other side, assert dacs_msg fields. |

Test-only setters in the header, guarded by `#ifdef CLAN_NCLAND_ROUTE_TEST`. Only the test binary `#define`s the macro.

### 8.2 Integration (manual)

Deploy updated `libutillib` + a consumer (e.g. `dacsproc`) on lin07n. Edit `/usr/cnc/lib/data/ncland/ncland.json` to include `207` (Nokia 1830 PSS). With ncland running and PSS518 connected, issue a TL1 command via the existing front-end. Confirm via daemon log: ncland shows `warehouse_handle_dacs_msg: neid=518 ...` instead of legacy clan handling it.

Rollback: remove `207` from `ncland.json`, restart consumer. No code rollback needed.

---

## 9. Files Touched / Created

| File | Responsibility |
|------|----------------|
| `cnc/utility/src/clan_ncland_route.h` (new) | Public API: 2 functions. Test-seam setters under `CLAN_NCLAND_ROUTE_TEST` guard. |
| `cnc/utility/src/clan_ncland_route.c` (new) | `parse_ncland_json`, `load_cfg_once`, `open_mqd_once`, `di_int_to_dacs_msg`, `clan_ncland_supports_dtype`, `clan_ncland_send`. Includes the §5/§6 TODO comments. |
| `cnc/utility/src/clan_ncland_route_tests.c` (new) | 6 assert-based tests per §8.1. |
| `cnc/utility/src/clan_util.c` | Add `#include "clan_ncland_route.h"`. Add the §6 route branch + legacy-deletion TODO comment in `clan_write_ctag` before existing `msgsnd`. |
| `cnc/utility/src/util.mk` | Add `clan_ncland_route.o` to libutillib; add `-lrt`; add test target. |

Total: 3 new files, 2 modified files.

---

## 10. Open Questions / Deferred

All items below have **in-code TODO comments** at their respective touch points so the next dev finds them by reading the source. The spec is the long-form rationale.

- **Hot-reload of `ncland.json`.** TODO at `load_cfg_once` in `clan_ncland_route.c` §5. Operator workflow: edit config + restart consumer. SIGHUP/inotify path deferred until ops needs it.
- **mq_send retry on EAGAIN.** TODO at `clan_ncland_send`'s `mq_send` call in §5. Today: single attempt; -1 on EAGAIN. If queue-full becomes common (high cmd rate vs ncland's `mq_maxmsg=10`), bump `mq_maxmsg` ncland-side OR add bounded retry here.
- **`-lrt` linking model.** Plan-time decision based on util.mk inspection: add to libutillib's LIBS export OR add per-consumer.
- **Per-consumer integration test.** Not in scope; covered by §8.2 manual deployment.
- **Legacy-clan deletion.** TODO at the surviving `msgsnd` line in `clan_write_ctag` §6. Once every dtype migrates to `ncland.json`, the `msgsnd` branch + `c->iq` + CLAN.iq setup in `clan_open()` can be deleted as a cleanup PR.
