# clan_util → ncland Routing (#5) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a `clan_ncland_route` module to `libutillib` that reads `/usr/cnc/lib/data/ncland/ncland.json`, and splice `clan_write_ctag` to forward commands for listed dtypes to the new ncland daemon (POSIX mqueue `/ncland_ctl`, `struct dacs_msg` payload) instead of legacy clan (SysV `Dacsq`, `struct di_int`).

**Architecture:** New C module `cnc/utility/src/clan_ncland_route.{h,c}` owns JSON load, `pthread_once_t` lazy init, mqd_t lifecycle, di_int→dacs_msg conversion, `mq_send`. `clan_write_ctag` gains one branch right before its existing `msgsnd(c->iq, ...)`: if `clan_ncland_supports_dtype(neType)` → call `clan_ncland_send(dptr, len)` instead. No fallback on send failure; caller decides retry.

**Tech Stack:** C99 via Lucent/AT&T nmake (`util.mk`), POSIX mqueue (`mq_open`/`mq_send`, `-lrt`), `pthread_once`, hand-rolled flat JSON parser. Unit tests via standalone `assert()`-based test binary.

**Design doc:** `~/docs/superpowers/specs/2026-06-17-clan-util-ncland-route-design.md` (read first).

---

## Critical context for the implementer (read first)

- **Build:** Lucent/AT&T **nmake** (NOT GNU make). From `cnc/utility/src`: `nmake -f util.mk <target>`. Ensure `BASE = git rev-parse --show-toplevel` (`/home/dan/Git/netflex`) and `VPATH[0] == $BASE`. If `nmake` fails on missing `globaldefs.nmk` / `global*.nmk` / project headers, STOP and report — do not hack include paths.
- **Branch:** create new branch off `ncland-start` (the #4 branch): `git checkout -b clan-util-ncland-route ncland-start`. All commits land here.
- **Commit trailer (every commit):** `Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>`.
- **Doxygen** required on every new function and struct. Match the project's `/** @brief ... @param ... @return ... */` style.
- **Sysctl requirement** (carried from #4): host needs `fs.mqueue.msgsize_max >= 10248` for `mq_send` of `struct dacs_msg`. Default RHEL 8 is 8192 — fails with EMSGSIZE otherwise. Apply via `sudo sysctl -w fs.mqueue.msgsize_max=16384` before running send tests. Already set on dev host (verified during #4).
- **libutillib is a static archive** (`$(LDIR)/libutillib.$(LIBTYPE)` built from `OBJ_UTILLIB`). New `.o` goes in `OBJ_UTILLIB` (line ~244 in `util.mk`). Consumers link the archive directly; `-lrt` must be added to each consumer's link line.
- **Consumers needing `-lrt`** (from explore step): `cnc/dacsproc`, `cnc/arm`, `cnc/fe`. The plan adds `-lrt` per-consumer (libutillib is a static archive with no LIBS export variable).
- **Existing `clan_write_ctag`** lives at `cnc/utility/src/clan_util.c:233`. The splice point is right before `msgsnd(c->iq, dptr, len, 0)` at line ~330. `neType` is already computed earlier (line ~258).
- **`struct di_int`** is from `<di.h>` — fields: `di_mtype`, `di_tty`, `di_seq`, `di_orig_cnc`, `di_dacsid`, `di_active`, `di_slot_tp`, `di_slot`, `di_user_priv`, `di_msgdead`, `di_data[DACS_MSG_SZ]`.
- **`struct dacs_msg`** is from `<msgscreen.h>` — already pulled into clan_util's include chain. Fields: `dm_tty`, `dm_type`, `dm_old_tty`, `dm_slot`, `dm_slot_tp`, bitfields `dm_up/active/to_screen/reason/dacsid`, union `u.dm_text[MAXDACSMSG+1]`. `DMTYPE_TEXT_MSG = 800`.
- **TRACE macro**: existing logging in clan_util uses `TRACE(level, fmt, ...)`. `D0` = highest priority (errors), `D3` = info, `D5` = debug, `D7` = trace.
- **Test harness**: cnc/utility has no `nfunit-test.hpp` equivalent. New test binary uses raw `assert()` + a small print-on-pass helper. Build via a new util.mk target; not part of CI yet.
- **Field-mapping table** (memorize before writing the conversion):

| `di_int` (src) | `dacs_msg` (dst) |
|---|---|
| `di_tty` | `dm_tty` |
| `di_dacsid` | `dm_dacsid` (cast to unsigned; 20-bit bitfield) |
| `di_slot` | `dm_slot` |
| `di_slot_tp` | `dm_slot_tp` |
| `di_data` | `u.dm_text` (same colon-delim format) |
| — | `dm_old_tty = 0` (no di_int equivalent) |
| — | `dm_type = DMTYPE_TEXT_MSG` |
| — | `dm_to_screen = 0` |
| — | `dm_reason = 0` |
| `di_seq` / `di_orig_cnc` / `di_user_priv` / `di_msgdead` / `di_active` | dropped |

---

## File Structure

| File | Responsibility |
|------|----------------|
| `cnc/utility/src/clan_ncland_route.h` (new) | Public API: `clan_ncland_supports_dtype`, `clan_ncland_send`. Test-seam setters guarded by `CLAN_NCLAND_ROUTE_TEST`. |
| `cnc/utility/src/clan_ncland_route.c` (new) | `parse_ncland_json`, `load_cfg_once`, `open_mqd_once`, `di_int_to_dacs_msg`, public API bodies. Hosts the in-code TODO comments for deferred items (hot-reload, EAGAIN retry). |
| `cnc/utility/src/clan_ncland_route_tests.c` (new) | Assert-based test binary: 6 tests covering JSON parse, membership, send round-trip. |
| `cnc/utility/src/clan_util.c` | Add `#include "clan_ncland_route.h"`. Add 5-line route branch + legacy-deletion TODO comment in `clan_write_ctag` before existing `msgsnd`. |
| `cnc/utility/src/util.mk` | Add `clan_ncland_route.o` to `OBJ_UTILLIB`. Add new test target `clan_ncland_route_tests`. |
| `cnc/dacsproc/src/<name>.mk` | Add `-lrt` to clan-write-using consumer's link line. |
| `cnc/arm/src/<name>.mk` | Add `-lrt`. |
| `cnc/fe/src/<name>.mk` | Add `-lrt`. |

Tasks are sequential. Each task builds and passes its own tests.

---

## Task 1: Module skeleton + `parse_ncland_json` + 4 parse tests

**Files:** `clan_ncland_route.h` (new), `clan_ncland_route.c` (new), `clan_ncland_route_tests.c` (new), `util.mk` (modified)

- [ ] **Step 1: Create `clan_ncland_route.h`** with the full public API + test seams (test-binding helpers live behind a macro so production builds don't see them):

```c
/** @file clan_ncland_route.h
 *  Route supported dtypes from libutillib's clan_write_ctag to the ncland
 *  daemon (POSIX mqueue /ncland_ctl, struct dacs_msg payload) instead of
 *  the legacy clan SysV Dacsq. See:
 *  ~/docs/superpowers/specs/2026-06-17-clan-util-ncland-route-design.md */
#ifndef CLAN_NCLAND_ROUTE_H
#define CLAN_NCLAND_ROUTE_H

#include <stdbool.h>
#include <stddef.h>
#include <mqueue.h>
#include <di.h>

#ifdef __cplusplus
extern "C" {
#endif

/** @brief Return true if @p dtype is listed in
 *  /usr/cnc/lib/data/ncland/ncland.json and should be routed to ncland.
 *  Loads + caches config on first call. Missing/malformed config →
 *  TRACE WARN once + return false for every dtype (legacy clan fallback).
 *  Thread-safe; idempotent. */
bool clan_ncland_supports_dtype(int dtype);

/** @brief Convert @p src into a struct dacs_msg and post it to
 *  /ncland_ctl. Caller already determined this dtype is ncland-supported.
 *
 *  @param src  Source di_int populated by clan_write_ctag.
 *  @param len  msgsnd-style length (DI_INT_SZ). Unused — dacs_msg length
 *              is recomputed from dm_text. Kept for symmetry with msgsnd.
 *  @return 0 on success; -1 on mqd-not-open or mq_send failure.
 *          errno preserved on EAGAIN/EMSGSIZE. */
int clan_ncland_send(const struct di_int *src, int len);

#ifdef CLAN_NCLAND_ROUTE_TEST
/* Test-only setters. Define the macro before #include to enable. */
void clan_ncland_route_set_dtypes_for_test(const int *arr, size_t n);
void clan_ncland_route_set_mqd_for_test(mqd_t mqd);
void clan_ncland_route_reset_for_test(void);
#endif

#ifdef __cplusplus
}
#endif

#endif
```

- [ ] **Step 2: Create `clan_ncland_route.c` skeleton** with `parse_ncland_json` only (other functions land in later tasks). This file will grow across Tasks 2 and 3.

```c
/** @file clan_ncland_route.c
 *  Route supported dtypes to ncland. See:
 *  ~/docs/superpowers/specs/2026-06-17-clan-util-ncland-route-design.md */
#include "clan_ncland_route.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stddef.h>
#include <errno.h>
#include <pthread.h>
#include <fcntl.h>
#include <mqueue.h>
#include <msgscreen.h>
#include <trace.h>

/* Static state — populated by Tasks 2 and 3. */
static pthread_once_t  g_once_cfg = PTHREAD_ONCE_INIT;
static int            *g_dtypes   = NULL;
static size_t          g_dtypes_n = 0;
static bool            g_cfg_ok   = false;

static pthread_once_t  g_once_mqd = PTHREAD_ONCE_INIT;
static mqd_t           g_mqd      = (mqd_t)-1;

/**
 * @brief Parse /usr/cnc/lib/data/ncland/ncland.json into a malloc'd int array.
 *
 * Schema: {"version":1,"dtypes":[<int>,...]}. Hand-rolled flat parser:
 * whitespace-tolerant, single-pass scan, no recursion. Rejects: file >
 * 64 KB, missing "version", version != 1, missing "dtypes", non-numeric
 * token inside dtypes[].
 *
 * @param path     Filesystem path.
 * @param out_arr  Output array (caller frees).
 * @param out_n    Output count.
 * @return 0 on success; -1 on file/parse/schema error.
 */
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

    const char *p = strstr(buf, "\"version\"");
    if (!p) { free(buf); return -1; }
    p += strlen("\"version\"");
    while (*p && (*p == ' ' || *p == '\t' || *p == ':')) p++;
    if (atoi(p) != 1) { free(buf); return -1; }

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

/* Stub bodies — Tasks 2 and 3 fill these in. */
bool clan_ncland_supports_dtype(int dtype) { (void)dtype; return false; }
int  clan_ncland_send(const struct di_int *src, int len)
{ (void)src; (void)len; return -1; }

#ifdef CLAN_NCLAND_ROUTE_TEST
void clan_ncland_route_set_dtypes_for_test(const int *arr, size_t n)
{ (void)arr; (void)n; }
void clan_ncland_route_set_mqd_for_test(mqd_t mqd) { (void)mqd; }
void clan_ncland_route_reset_for_test(void) { }
#endif
```

`<trace.h>` is the existing TRACE macro header in cnc/utility/include or system include. Confirm path during Step 5 build.

- [ ] **Step 3: Create `clan_ncland_route_tests.c`** with the 4 parse tests. Standalone binary with its own `main`:

```c
/** @file clan_ncland_route_tests.c
 *  Standalone tests for clan_ncland_route. Build with -DCLAN_NCLAND_ROUTE_TEST
 *  via the util.mk clan_ncland_route_tests target. */
#define CLAN_NCLAND_ROUTE_TEST
#include "clan_ncland_route.h"

#include <assert.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

/* Local extern of the static parse_ncland_json — to make it testable,
 * Step 4 will promote it to non-static OR add a test-binding wrapper.
 * For Task 1 we test it indirectly via the public API (supports_dtype
 * after a load). But the public API is a stub in Task 1. So actually
 * we declare the test-binding wrapper here and add it to the .c. */
extern int test_parse_ncland_json(const char *path, int **out_arr, size_t *out_n);

static void test_parse_happy(void) {
    char path[] = "/tmp/clan_ncland_route_test_XXXXXX";
    int fd = mkstemp(path);
    assert(fd >= 0);
    const char *body = "{\"version\":1,\"dtypes\":[1,2,207]}";
    assert(write(fd, body, strlen(body)) == (ssize_t)strlen(body));
    close(fd);

    int *arr = NULL; size_t n = 0;
    assert(test_parse_ncland_json(path, &arr, &n) == 0);
    assert(n == 3);
    assert(arr[0] == 1 && arr[1] == 2 && arr[2] == 207);
    free(arr);
    unlink(path);
    printf("PASS: test_parse_happy\n");
}

static void test_parse_bad_version(void) {
    char path[] = "/tmp/clan_ncland_route_test_XXXXXX";
    int fd = mkstemp(path);
    assert(fd >= 0);
    const char *body = "{\"version\":2,\"dtypes\":[]}";
    assert(write(fd, body, strlen(body)) == (ssize_t)strlen(body));
    close(fd);

    int *arr = NULL; size_t n = 0;
    assert(test_parse_ncland_json(path, &arr, &n) == -1);
    unlink(path);
    printf("PASS: test_parse_bad_version\n");
}

static void test_parse_missing_dtypes(void) {
    char path[] = "/tmp/clan_ncland_route_test_XXXXXX";
    int fd = mkstemp(path);
    assert(fd >= 0);
    const char *body = "{\"version\":1}";
    assert(write(fd, body, strlen(body)) == (ssize_t)strlen(body));
    close(fd);

    int *arr = NULL; size_t n = 0;
    assert(test_parse_ncland_json(path, &arr, &n) == -1);
    unlink(path);
    printf("PASS: test_parse_missing_dtypes\n");
}

static void test_parse_non_int_element(void) {
    char path[] = "/tmp/clan_ncland_route_test_XXXXXX";
    int fd = mkstemp(path);
    assert(fd >= 0);
    const char *body = "{\"version\":1,\"dtypes\":[1,\"foo\",3]}";
    assert(write(fd, body, strlen(body)) == (ssize_t)strlen(body));
    close(fd);

    int *arr = NULL; size_t n = 0;
    assert(test_parse_ncland_json(path, &arr, &n) == -1);
    unlink(path);
    printf("PASS: test_parse_non_int_element\n");
}

int main(void) {
    test_parse_happy();
    test_parse_bad_version();
    test_parse_missing_dtypes();
    test_parse_non_int_element();
    printf("All tests passed.\n");
    return 0;
}
```

- [ ] **Step 4: Expose `parse_ncland_json` to the test binary.** In `clan_ncland_route.c`, drop the `static` from `parse_ncland_json` and add a forward declaration in `clan_ncland_route.h` under the `#ifdef CLAN_NCLAND_ROUTE_TEST` block:

```c
#ifdef CLAN_NCLAND_ROUTE_TEST
int test_parse_ncland_json(const char *path, int **out_arr, size_t *out_n);
/* … other test setters from Step 1 … */
#endif
```

And in `clan_ncland_route.c`, add a thin wrapper (kept under the same guard) that calls the now-non-static parser:

```c
#ifdef CLAN_NCLAND_ROUTE_TEST
extern int parse_ncland_json(const char *path, int **out_arr, size_t *out_n);  /* hoisted */
int test_parse_ncland_json(const char *path, int **out_arr, size_t *out_n)
{ return parse_ncland_json(path, out_arr, out_n); }
#endif
```

Two options here — pick whichever is cleaner:
- (a) Remove `static` from `parse_ncland_json` and let the test bind directly via the `extern` declaration. Slightly leakier ABI but zero wrapper.
- (b) Keep `parse_ncland_json` static and provide `test_parse_ncland_json` as the only test entry point. Cleaner encapsulation.

Recommended: (b). Apply: KEEP `static` on `parse_ncland_json`. The `#ifdef CLAN_NCLAND_ROUTE_TEST` block in `.c` calls the static directly via the wrapper (which IS in the same TU, so static visibility works).

- [ ] **Step 5: Wire `util.mk`.**

(a) Add `clan_ncland_route.o` to `OBJ_UTILLIB` (currently around line 244 of `util.mk`). Insert it adjacent to `clan_util.o` for findability:

```
clan_util.o clan_ncland_route.o cmds.o cnc_up.o cnv_dnymsg.o common.o compool.o \
```

(b) Add a new test target near the end of `util.mk` (alongside any other test targets, or after the lint target):

```
clan_ncland_route_tests : clan_ncland_route_tests.o clan_ncland_route_test_build.o
	$(CC) $(CCFLAGS) $(LDFLAGS) -o clan_ncland_route_tests \
		clan_ncland_route_tests.o clan_ncland_route_test_build.o \
		-lrt -lpthread

/* Test build of the route module with -DCLAN_NCLAND_ROUTE_TEST */
clan_ncland_route_test_build.o : clan_ncland_route.c
	$(CC) $(CCFLAGS) -DCLAN_NCLAND_ROUTE_TEST -c \
		-o clan_ncland_route_test_build.o clan_ncland_route.c
```

(c) Build + test:

```bash
cd /home/dan/Git/netflex/cnc/utility/src
nmake -f util.mk $(LDIR)/libutillib.$(LIBTYPE)   # ensure libutillib still builds
nmake -f util.mk clan_ncland_route_tests
./clan_ncland_route_tests
```

Expected output:
```
PASS: test_parse_happy
PASS: test_parse_bad_version
PASS: test_parse_missing_dtypes
PASS: test_parse_non_int_element
All tests passed.
```

- [ ] **Step 6: Commit**

```bash
cd /home/dan/Git/netflex
git checkout -b clan-util-ncland-route ncland-start
git add cnc/utility/src/clan_ncland_route.h cnc/utility/src/clan_ncland_route.c \
        cnc/utility/src/clan_ncland_route_tests.c cnc/utility/src/util.mk
git commit -m "$(cat <<'EOF'
[utility] #5 clan_ncland_route: skeleton + parse_ncland_json + 4 parse tests

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 2: `clan_ncland_supports_dtype` + `load_cfg_once` + membership test

**Files:** `clan_ncland_route.c` (modified), `clan_ncland_route_tests.c` (modified)

- [ ] **Step 1: Replace the stub `clan_ncland_supports_dtype` and add `load_cfg_once`.** Edit `clan_ncland_route.c`:

```c
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

bool clan_ncland_supports_dtype(int dtype)
{
    pthread_once(&g_once_cfg, load_cfg_once);
    if (!g_cfg_ok) return false;
    for (size_t i = 0; i < g_dtypes_n; i++)
        if (g_dtypes[i] == dtype) return true;
    return false;
}
```

Delete the stub `clan_ncland_supports_dtype` from Task 1.

- [ ] **Step 2: Implement test-only setters in `clan_ncland_route.c`** (replace the Task 1 stubs):

```c
#ifdef CLAN_NCLAND_ROUTE_TEST
void clan_ncland_route_set_dtypes_for_test(const int *arr, size_t n)
{
    /* Bypass pthread_once: write the state directly and mark cfg_ok. */
    free(g_dtypes);
    g_dtypes = NULL; g_dtypes_n = 0;
    if (n > 0) {
        g_dtypes = (int *)malloc(n * sizeof(int));
        memcpy(g_dtypes, arr, n * sizeof(int));
        g_dtypes_n = n;
    }
    g_cfg_ok = true;
    /* Mark the once-token as done so a subsequent supports_dtype call
     * doesn't overwrite our injected state. */
    static pthread_once_t done = { /* PTHREAD_ONCE_INIT-equivalent already-fired */ };
    /* Portable trick: re-init by assignment from a local that's been
     * walked through pthread_once. Linux glibc's pthread_once_t is an
     * int; assigning a value that signals "done" works. Simpler: call
     * pthread_once with a no-op so the runtime marks it done. */
    pthread_once(&g_once_cfg, (void(*)(void))NULL);
    (void)done;
}

void clan_ncland_route_set_mqd_for_test(mqd_t mqd)
{
    g_mqd = mqd;
    pthread_once(&g_once_mqd, (void(*)(void))NULL);
}

void clan_ncland_route_reset_for_test(void)
{
    free(g_dtypes);
    g_dtypes = NULL; g_dtypes_n = 0; g_cfg_ok = false;
    if (g_mqd != (mqd_t)-1) { mq_close(g_mqd); g_mqd = (mqd_t)-1; }
    /* Re-arm once tokens. */
    static const pthread_once_t fresh = PTHREAD_ONCE_INIT;
    g_once_cfg = fresh;
    g_once_mqd = fresh;
}
#endif
```

**Note on `pthread_once` re-arm:** glibc's `pthread_once_t` is an `int`; `PTHREAD_ONCE_INIT` is `0`. Resetting to `0` un-marks it. The cleanest approach: keep the `static const pthread_once_t fresh = PTHREAD_ONCE_INIT;` + struct-copy assignment as shown. If that fails to compile on the target gcc version, fall back to direct `g_once_cfg = (pthread_once_t)PTHREAD_ONCE_INIT;`.

The "call pthread_once with NULL fn" trick won't work — pthread_once derefs the fn pointer. Replace that line with: don't call pthread_once at all in the setter; instead, before any real call, the test calls `clan_ncland_route_reset_for_test()` THEN `clan_ncland_route_set_dtypes_for_test(...)`. The setter's job is just "preload the state for the next call." `clan_ncland_supports_dtype`'s pthread_once will fire once and call `load_cfg_once`, which will overwrite the state — but if the test's setter already populated, we want to skip that load. **Cleanest path: have the setter set a `g_test_override = true` flag that `load_cfg_once` checks and short-circuits.**

Revised — add a test-override sentinel:

```c
#ifdef CLAN_NCLAND_ROUTE_TEST
static bool g_test_override = false;
#endif

static void load_cfg_once(void)
{
#ifdef CLAN_NCLAND_ROUTE_TEST
    if (g_test_override) return;   /* test setter populated state */
#endif
    /* … existing body … */
}

#ifdef CLAN_NCLAND_ROUTE_TEST
void clan_ncland_route_set_dtypes_for_test(const int *arr, size_t n)
{
    free(g_dtypes);
    g_dtypes = NULL; g_dtypes_n = 0;
    if (n > 0) {
        g_dtypes = (int *)malloc(n * sizeof(int));
        memcpy(g_dtypes, arr, n * sizeof(int));
        g_dtypes_n = n;
    }
    g_cfg_ok = true;
    g_test_override = true;
}

void clan_ncland_route_set_mqd_for_test(mqd_t mqd)
{
    g_mqd = mqd;
    /* open_mqd_once also gets a g_test_override-style check in Task 3. */
}

void clan_ncland_route_reset_for_test(void)
{
    free(g_dtypes);
    g_dtypes = NULL; g_dtypes_n = 0; g_cfg_ok = false;
    g_test_override = false;
    if (g_mqd != (mqd_t)-1) { mq_close(g_mqd); g_mqd = (mqd_t)-1; }
    /* pthread_once tokens stay armed; the override flag does the work. */
}
#endif
```

This is clean: production code never touches `g_test_override`, test code controls state via the flag, no `pthread_once_t` re-arming acrobatics.

- [ ] **Step 3: Add the membership test in `clan_ncland_route_tests.c`:**

```c
static void test_supports_dtype_hit_miss(void) {
    clan_ncland_route_reset_for_test();
    int allow[] = {1, 2, 207};
    clan_ncland_route_set_dtypes_for_test(allow, 3);
    assert(clan_ncland_supports_dtype(207) == true);
    assert(clan_ncland_supports_dtype(1) == true);
    assert(clan_ncland_supports_dtype(99) == false);
    assert(clan_ncland_supports_dtype(0) == false);
    clan_ncland_route_reset_for_test();
    printf("PASS: test_supports_dtype_hit_miss\n");
}

/* Add to main(): */
int main(void) {
    test_parse_happy();
    test_parse_bad_version();
    test_parse_missing_dtypes();
    test_parse_non_int_element();
    test_supports_dtype_hit_miss;     /* will fail to call — change next */
    printf("All tests passed.\n");
    return 0;
}
```

Fix the typo — call site needs `()`:
```c
    test_supports_dtype_hit_miss();
```

- [ ] **Step 4: Build + test:**

```bash
cd /home/dan/Git/netflex/cnc/utility/src
nmake -f util.mk $(LDIR)/libutillib.$(LIBTYPE)
nmake -f util.mk clan_ncland_route_tests
./clan_ncland_route_tests
```

Expected: 5 PASS lines + `All tests passed.`

- [ ] **Step 5: Commit**

```bash
cd /home/dan/Git/netflex
git add cnc/utility/src/clan_ncland_route.c cnc/utility/src/clan_ncland_route_tests.c
git commit -m "$(cat <<'EOF'
[utility] #5 clan_ncland_route: supports_dtype + load_cfg_once + membership test

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 3: `di_int_to_dacs_msg` + `clan_ncland_send` + `open_mqd_once` + round-trip test

**Files:** `clan_ncland_route.c` (modified), `clan_ncland_route_tests.c` (modified)

- [ ] **Step 1: Implement the conversion + `open_mqd_once` + real `clan_ncland_send`.** Edit `clan_ncland_route.c` (replace the Task 1 stub `clan_ncland_send`):

```c
/**
 * @brief Convert struct di_int → struct dacs_msg. See spec §3.2 mapping table.
 *
 * @param src      Source di_int populated by clan_write_ctag.
 * @param src_len  Unused (kept for signature symmetry).
 * @param dst      Output dacs_msg, zeroed before fill.
 * @return Byte count to pass to mq_send (header + text + NUL).
 */
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

static void open_mqd_once(void)
{
#ifdef CLAN_NCLAND_ROUTE_TEST
    if (g_mqd != (mqd_t)-1) return;   /* test setter already installed an mqd */
#endif
    g_mqd = mq_open("/ncland_ctl", O_WRONLY | O_NONBLOCK);
    if (g_mqd == (mqd_t)-1)
        TRACE(D0, "clan_ncland_route: mq_open(/ncland_ctl) failed: %s\n",
              strerror(errno));
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

Delete the Task 1 stub `clan_ncland_send`.

- [ ] **Step 2: Add the round-trip test.** Edit `clan_ncland_route_tests.c`:

```c
#include <fcntl.h>
#include <mqueue.h>
#include <msgscreen.h>

static void test_send_round_trip(void) {
    clan_ncland_route_reset_for_test();

    /* Set up a tmp POSIX mqueue for the test (writer/reader pair). */
    struct mq_attr attr = {0};
    attr.mq_maxmsg  = 4;
    attr.mq_msgsize = sizeof(struct dacs_msg);
    /* Pre-cleanup in case a prior crashed run left it. */
    mq_unlink("/clan_ncland_route_test");
    mqd_t rd = mq_open("/clan_ncland_route_test",
                       O_CREAT | O_RDONLY | O_NONBLOCK, 0600, &attr);
    assert(rd != (mqd_t)-1);
    mqd_t wr = mq_open("/clan_ncland_route_test",
                       O_WRONLY | O_NONBLOCK);
    assert(wr != (mqd_t)-1);

    /* Install the writer mqd into the module. */
    clan_ncland_route_set_mqd_for_test(wr);

    /* Build a di_int that exercises the field mapping. */
    struct di_int di;
    memset(&di, 0, sizeof(di));
    di.di_tty      = 55;
    di.di_dacsid   = 207;
    di.di_slot     = 3;
    di.di_slot_tp  = 9;
    snprintf((char *)di.di_data, sizeof(di.di_data),
             "CTAG1:30:0:0:show shelf");

    int rc = clan_ncland_send(&di, 0);
    assert(rc == 0);

    /* Drain the reader side. */
    struct dacs_msg m;
    ssize_t n = mq_receive(rd, (char *)&m, sizeof(m), NULL);
    assert(n > 0);
    assert(m.dm_tty       == 55);
    assert((int)m.dm_dacsid == 207);
    assert(m.dm_slot      == 3);
    assert(m.dm_slot_tp   == 9);
    assert(m.dm_type      == DMTYPE_TEXT_MSG);
    assert(m.dm_old_tty   == 0);
    assert(strcmp(m.u.dm_text, "CTAG1:30:0:0:show shelf") == 0);

    /* Cleanup. */
    mq_close(rd);
    /* Don't mq_close(wr) — the module owns it now and reset_for_test() does it. */
    clan_ncland_route_reset_for_test();
    mq_unlink("/clan_ncland_route_test");
    printf("PASS: test_send_round_trip\n");
}

/* Add to main(): */
int main(void) {
    test_parse_happy();
    test_parse_bad_version();
    test_parse_missing_dtypes();
    test_parse_non_int_element();
    test_supports_dtype_hit_miss();
    test_send_round_trip();
    printf("All tests passed.\n");
    return 0;
}
```

- [ ] **Step 3: Build + test:**

Verify sysctl first (the test creates a real mqueue with `mq_msgsize = sizeof(struct dacs_msg) ≈ 10272`):

```bash
cat /proc/sys/fs/mqueue/msgsize_max   # must be >= 10272
# If < 10272:
sudo sysctl -w fs.mqueue.msgsize_max=16384
```

Then:
```bash
cd /home/dan/Git/netflex/cnc/utility/src
nmake -f util.mk $(LDIR)/libutillib.$(LIBTYPE)
nmake -f util.mk clan_ncland_route_tests
./clan_ncland_route_tests
```

Expected: 6 PASS lines + `All tests passed.`

- [ ] **Step 4: Commit**

```bash
cd /home/dan/Git/netflex
git add cnc/utility/src/clan_ncland_route.c cnc/utility/src/clan_ncland_route_tests.c
git commit -m "$(cat <<'EOF'
[utility] #5 clan_ncland_route: di_int->dacs_msg + clan_ncland_send + round-trip test

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 4: Splice `clan_write_ctag` + `-lrt` per consumer + integration verify

**Files:** `clan_util.c` (modified), `cnc/dacsproc/src/<name>.mk` (modified), `cnc/arm/src/<name>.mk` (modified), `cnc/fe/src/<name>.mk` (modified)

- [ ] **Step 1: Add `#include` to `clan_util.c`.** Near the existing includes (before the function definitions):

```c
#include "clan_ncland_route.h"
```

- [ ] **Step 2: Add the route branch in `clan_write_ctag`.** Find the line `int rc = msgsnd( c->iq, dptr, len, 0 );` (around line 330 in `clan_util.c`). Insert this block **immediately before** that line:

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

- [ ] **Step 3: Add `-lrt` to consumer link lines.**

For each of `cnc/dacsproc`, `cnc/arm`, `cnc/fe`: find the `.mk` file(s) that link executables using `libutillib`, and add `-lrt` to the link command.

Locate the link lines:
```bash
cd /home/dan/Git/netflex
grep -rn -- "-lutillib\|libutillib\." cnc/dacsproc/src/*.mk cnc/arm/src/*.mk cnc/fe/src/*.mk 2>&1 | head -20
```

For each link command that pulls libutillib, append ` -lrt` at the end. Example pattern (each consumer's .mk will look similar):

Before:
```
$(PBIN)/dacsproc :: dacsproc.o $(OBJS) $(INCLIBS) -lutillib -lflex ...
	$(CC) $(CCFLAGS) $(LDFLAGS) -o $(<) $(*) -lpthread
```

After:
```
$(PBIN)/dacsproc :: dacsproc.o $(OBJS) $(INCLIBS) -lutillib -lflex ...
	$(CC) $(CCFLAGS) $(LDFLAGS) -o $(<) $(*) -lpthread -lrt
```

**Caveat:** if a `.mk` already has `-lrt` (some do for other POSIX functions), don't double-add.

- [ ] **Step 4: Build verification.**

(a) libutillib rebuilds cleanly with the new clan_util.c:
```bash
cd /home/dan/Git/netflex/cnc/utility/src
nmake -f util.mk $(LDIR)/libutillib.$(LIBTYPE)
```
Exit 0 expected.

(b) Each touched consumer builds:
```bash
cd /home/dan/Git/netflex/cnc/dacsproc/src
nmake -f <name>.mk    # replace with actual mk filename per consumer
```

If any consumer fails with `undefined reference to mq_send` or `mq_open`, the `-lrt` add is missing in that consumer's link line. Find the missing one and add it.

(c) Route-module tests still pass:
```bash
cd /home/dan/Git/netflex/cnc/utility/src
nmake -f util.mk clan_ncland_route_tests && ./clan_ncland_route_tests
```

Expected: 6 PASS + `All tests passed.`

- [ ] **Step 5: Manual integration verification (deferred / optional).**

Spec §8.2: deploy updated `libutillib` + a consumer (e.g. dacsproc) to lin07n. Edit `/usr/cnc/lib/data/ncland/ncland.json` to include `207`. With ncland running and PSS518 connected, issue a TL1 command. Confirm via ncland daemon log: `warehouse_handle_dacs_msg: neid=518 ...` instead of legacy clan.

Not required for commit — manual ops work.

- [ ] **Step 6: Commit**

```bash
cd /home/dan/Git/netflex
git add cnc/utility/src/clan_util.c \
        cnc/dacsproc/src/*.mk cnc/arm/src/*.mk cnc/fe/src/*.mk
git commit -m "$(cat <<'EOF'
[utility] #5 clan_write_ctag: route ncland-supported dtypes to /ncland_ctl

Adds the route branch in clan_write_ctag and -lrt to each consumer
(dacsproc, arm, fe) for POSIX mqueue support. Branch fires only when
the dtype is listed in /usr/cnc/lib/data/ncland/ncland.json. No fallback
on mq_send failure — caller learns FAILURE and decides.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Self-Review (against the spec)

**Spec coverage:**
- §1 Scope: clan_write/_ctag, JSON parser, mq_send, no fallback → Tasks 1-4 cover all. ✓
- §2 Architecture: routing flow, lazy init, no teardown → Task 2 (`load_cfg_once`) + Task 3 (`open_mqd_once`). ✓
- §3.1 Public API → Task 1 (header). ✓
- §3.2/§3.3 field mapping + conversion → Task 3. ✓
- §4 JSON parser → Task 1. ✓
- §5 Init + Send + in-code TODOs → Task 2 (hot-reload TODO) + Task 3 (EAGAIN-retry TODO). ✓
- §6 splice + legacy-deletion TODO → Task 4 Step 2. ✓
- §7 build wiring → Task 1 Step 5 (libutillib) + Task 4 Step 3 (consumer `-lrt`). ✓
- §8.1 unit tests (6) → Task 1 (4 parse) + Task 2 (1 membership) + Task 3 (1 round-trip). ✓
- §8.2 manual integration → Task 4 Step 5. ✓
- §9 Files Touched → matches plan File Structure. ✓
- §10 deferred items → in-code TODO comments at `load_cfg_once`, `clan_ncland_send` mq_send, `clan_write_ctag` legacy msgsnd line. ✓

**Placeholder scan:** No "TBD" / "implement later" / "fill in details" — every code step shows complete code. Task 1 Step 4 documents a real implementation choice (option a vs b, recommend b) with rationale; not a placeholder. Task 2 Step 2 documents the pthread_once-reset pitfall and lands on a `g_test_override` flag — complete code shown.

**Type consistency:** `clan_ncland_supports_dtype`, `clan_ncland_send`, `parse_ncland_json`, `di_int_to_dacs_msg`, `load_cfg_once`, `open_mqd_once`, `g_dtypes`/`g_dtypes_n`/`g_cfg_ok`/`g_once_cfg`/`g_mqd`/`g_once_mqd`/`g_test_override`, `clan_ncland_route_set_dtypes_for_test`/`_set_mqd_for_test`/`_reset_for_test`, `test_parse_ncland_json` all used identically across tasks. di_int fields (`di_tty`, `di_dacsid`, `di_slot`, `di_slot_tp`, `di_data`) and dacs_msg fields (`dm_tty`, `dm_dacsid`, `dm_slot`, `dm_slot_tp`, `dm_old_tty`, `dm_type`, `dm_to_screen`, `dm_reason`, `u.dm_text`) used identically. `DMTYPE_TEXT_MSG`, `MAXDACSMSG`, `CLAN_NCLAND_ROUTE_TEST`, `TRACE` level constants (`D0`/`D3`) used identically.

---

## Execution note

Tasks 1→4 are sequential. Each task ends green (tests pass + libutillib rebuilds). Task 4 is the cross-cutting one — adds `-lrt` to multiple consumer Makefiles; if any consumer doesn't actually link clan_write today, it won't need the flag (skip). Manual integration test in Task 4 Step 5 is optional and ops-time, not commit-time. Recommended: subagent-driven, two-stage review per task; extra care on Task 4 where the surface is broader than a single file.
