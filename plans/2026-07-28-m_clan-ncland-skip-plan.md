# m_clan ncland-skip Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Teach `m_clan` (cnc/sdi/src/m_clan.c) to skip legacy clan startup for NEs that `ncland.json` claims, and upgrade the underlying `clan_ncland_route` allowlist to the groups schema so routing matches ncland's own loader.

**Architecture:** Rewrite `libutillib`'s hand-rolled `ncland.json` parser to consume the groups schema (`{version, groups:[{group, neid_range, dtypes}]}`), replace `clan_ncland_supports_dtype(dtype)` with `clan_ncland_supports(neid, dtype)`, migrate the sole in-tree caller in `clan_util.c`, and gate `m_clan.c::get_next_clan()` on the new API. All parsing stays in C (no nlohmann/json), matching `libutillib` conventions.

**Tech Stack:** C (gcc 4.8.5 baseline), Lucent/AT&T nmake, POSIX mqueue, existing libtrace TRACE macros.

**Spec:** `~/WorkNotes/specs/2026-07-28-m_clan-ncland-skip-design.md`

---

## File Layout

- `cnc/utility/src/clan_ncland_route.h` — API: replace `clan_ncland_supports_dtype` with `clan_ncland_supports(neid, dtype)`. Rename test shim.
- `cnc/utility/src/clan_ncland_route.c` — Parser rewrite (groups schema), state now `struct clan_ncland_group[]`, new supports check.
- `cnc/utility/src/clan_ncland_route_tests.c` — Rewrite parse tests, add neid+dtype lookup tests.
- `cnc/utility/src/clan_util.c` — One-line migration from dtype-only to (neid, dtype).
- `cnc/sdi/src/m_clan.c` — `#include "clan_ncland_route.h"` + skip block in `get_next_clan`.

No `.mk` edits: `libutillib`'s object list already includes `clan_ncland_route.o` (util.mk:242); `m_clan` already links `$(UTILLIB)` (sdisrc.mk:187); test target already exists (util.mk:1232-1238).

---

## Task 1: Rewrite header (API + struct + test shim)

**Files:**
- Modify: `cnc/utility/src/clan_ncland_route.h`

- [ ] **Step 1: Replace header contents**

```c
/** @file clan_ncland_route.h
 *  Route supported (neid,dtype) pairs from libutillib's clan_write_ctag to
 *  the ncland daemon (POSIX mqueue /ncland_ctl, struct dacs_msg payload)
 *  instead of the legacy clan SysV Dacsq. See:
 *  ~/WorkNotes/specs/2026-07-28-m_clan-ncland-skip-design.md */
#ifndef CLAN_NCLAND_ROUTE_H
#define CLAN_NCLAND_ROUTE_H

#include <stdbool.h>
#include <stddef.h>
#include <mqueue.h>
#include <di.h>

#ifdef __cplusplus
extern "C" {
#endif

/** @brief One decoded ncland.json group. */
struct clan_ncland_group {
    int     lo;        /**< Inclusive neid lower bound. */
    int     hi;        /**< Inclusive neid upper bound. */
    int    *dtypes;    /**< Allowed dtypes for this group (heap). */
    size_t  n_dtypes;  /**< Length of dtypes[]. */
};

/** @brief Return true iff /usr/cnc/lib/data/ncland/ncland.json contains a
 *  group whose neid_range covers @p neid AND whose dtypes[] contains
 *  @p dtype. Loads + caches config on first call. Missing/malformed config
 *  → TRACE WARN once + return false for every (neid, dtype). Thread-safe;
 *  idempotent. */
bool clan_ncland_supports(int neid, int dtype);

/** @brief Convert @p src into a struct dacs_msg and post it to
 *  /ncland_ctl. Caller already determined this (neid,dtype) is
 *  ncland-supported.
 *
 *  @param src  Source di_int populated by clan_write_ctag.
 *  @param len  msgsnd-style length (DI_INT_SZ). Unused — dacs_msg length
 *              is recomputed from dm_text. Kept for symmetry with msgsnd.
 *  @return 0 on success; -1 on mqd-not-open or mq_send failure. */
int clan_ncland_send(const struct di_int *src, int len);

#ifdef CLAN_NCLAND_ROUTE_TEST
/* Test-only entry points. Define the macro before #include to enable. */
int  test_parse_ncland_json(const char *path,
                            struct clan_ncland_group **out_arr,
                            size_t *out_n);
void clan_ncland_route_set_groups_for_test(const struct clan_ncland_group *arr,
                                           size_t n);
void clan_ncland_route_set_mqd_for_test(mqd_t mqd);
void clan_ncland_route_reset_for_test(void);
#endif

#ifdef __cplusplus
}
#endif

#endif
```

- [ ] **Step 2: Commit**

```bash
git add cnc/utility/src/clan_ncland_route.h
git commit -m "clan_ncland_route: header — groups-schema API + struct"
```

---

## Task 2: Rewrite parser + lookup (implementation)

**Files:**
- Modify: `cnc/utility/src/clan_ncland_route.c`

- [ ] **Step 1: Replace static state block (near top of file, currently lines 24-35)**

Replace:

```c
/* Static state — populated by Tasks 2 and 3. */
static pthread_once_t  g_once_cfg = PTHREAD_ONCE_INIT;
static int            *g_dtypes   = NULL;
static size_t          g_dtypes_n = 0;
static bool            g_cfg_ok   = false;
```

With:

```c
/* Static state — populated by Tasks 2 and 3. */
static pthread_once_t             g_once_cfg   = PTHREAD_ONCE_INIT;
static struct clan_ncland_group  *g_groups     = NULL;
static size_t                     g_n_groups   = 0;
static bool                       g_cfg_ok     = false;
```

- [ ] **Step 2: Replace `parse_ncland_json` with groups-aware parser**

Replace the entire `parse_ncland_json` function (currently lines 37-117) with:

```c
/* Skip ASCII whitespace. Groups parser helper. */
static void jskip_ws(const char **pp)
{
    const char *p = *pp;
    while (*p == ' ' || *p == '\t' || *p == '\n' || *p == '\r') p++;
    *pp = p;
}

/* Consume a specific literal char, skipping leading whitespace. Return 1 on
 * hit, 0 on miss. Advances *pp past the char on hit. */
static int jexpect(const char **pp, char c)
{
    jskip_ws(pp);
    if (**pp != c) return 0;
    (*pp)++;
    return 1;
}

/* Match a bare key like "version" at *pp, optionally consuming trailing ':'.
 * Return 1 on hit (advances past key + optional colon), 0 on miss. */
static int jmatch_key(const char **pp, const char *key)
{
    const char *p = *pp;
    jskip_ws(&p);
    if (*p != '"') return 0;
    p++;
    size_t klen = strlen(key);
    if (strncmp(p, key, klen) != 0 || p[klen] != '"') return 0;
    p += klen + 1;
    jskip_ws(&p);
    if (*p == ':') { p++; }
    *pp = p;
    return 1;
}

/* Parse a base-10 integer at *pp into *out. Returns 1 on success (advances
 * *pp past the digits), 0 on failure. Range-checked against [-1, INT_MAX]. */
static int jparse_int(const char **pp, int *out)
{
    jskip_ws(pp);
    char *endp = NULL;
    errno = 0;
    long v = strtol(*pp, &endp, 10);
    if (endp == *pp || errno == ERANGE || v < 0 || v > 65535) return 0;
    *out = (int)v;
    *pp = endp;
    return 1;
}

/* Skip a JSON string value: assumes *pp points at opening '"'. Consumes
 * everything up to and including closing '"'. Returns 1 on success, 0 on
 * unterminated string. Simple: no escape unescaping needed for the fields
 * we care about (group description is log-only). */
static int jskip_string(const char **pp)
{
    if (**pp != '"') return 0;
    const char *p = *pp + 1;
    while (*p && *p != '"') {
        if (*p == '\\' && p[1]) p += 2;
        else                    p++;
    }
    if (*p != '"') return 0;
    *pp = p + 1;
    return 1;
}

/* Check for overlap between two closed intervals. */
static int intervals_overlap(int lo1, int hi1, int lo2, int hi2)
{
    return !(hi1 < lo2 || hi2 < lo1);
}

/* Free a groups array + its dtype sub-arrays. Safe on NULL. */
static void free_groups(struct clan_ncland_group *arr, size_t n)
{
    if (!arr) return;
    for (size_t i = 0; i < n; i++) free(arr[i].dtypes);
    free(arr);
}

/**
 * @brief Parse /usr/cnc/lib/data/ncland/ncland.json (groups schema) into
 *        a malloc'd array of clan_ncland_group.
 *
 * Schema:
 * {
 *   "version": 1,
 *   "groups": [
 *     { "group": "<name>", "neid_range": [lo, hi], "dtypes": [<int>, ...] },
 *     ...
 *   ]
 * }
 *
 * Rejects: file > 64 KB; missing/wrong version; missing/empty groups[];
 * missing group/neid_range/dtypes fields; neid_range not [int,int] with
 * 0 <= lo <= hi; dtypes[] empty or out-of-range element; overlapping
 * ranges between any two groups; legacy top-level "dtypes" key.
 *
 * @param path     Filesystem path.
 * @param out_arr  Output array (caller owns; free via free_groups pattern).
 * @param out_n    Output count.
 * @return 0 on success; -1 on file/parse/schema error.
 */
static int parse_ncland_json(const char *path,
                             struct clan_ncland_group **out_arr,
                             size_t *out_n)
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

    /* Legacy top-level "dtypes" is a hard reject — align with ncland_seed. */
    if (strstr(buf, "\"dtypes\"")) {
        /* Reject only if it appears BEFORE any "groups". A "dtypes" nested
         * inside groups[] is legitimate. Cheap check: if "dtypes" occurs and
         * "groups" doesn't, or "dtypes" occurs before "groups", reject. */
        const char *dt = strstr(buf, "\"dtypes\"");
        const char *gr = strstr(buf, "\"groups\"");
        if (!gr || dt < gr) { free(buf); return -1; }
    }

    const char *p = buf;

    if (!jexpect(&p, '{'))         { free(buf); return -1; }
    if (!jmatch_key(&p, "version")) { free(buf); return -1; }
    int version = 0;
    if (!jparse_int(&p, &version) || version != 1) { free(buf); return -1; }
    if (!jexpect(&p, ','))          { free(buf); return -1; }

    if (!jmatch_key(&p, "groups"))  { free(buf); return -1; }
    if (!jexpect(&p, '['))          { free(buf); return -1; }

    size_t cap = 4, n = 0;
    struct clan_ncland_group *arr =
        (struct clan_ncland_group *)calloc(cap, sizeof(*arr));
    if (!arr) { free(buf); return -1; }

    /* At least one group required. */
    jskip_ws(&p);
    if (*p == ']') { free(arr); free(buf); return -1; }

    for (;;) {
        if (!jexpect(&p, '{')) { free_groups(arr, n); free(buf); return -1; }

        /* Expect "group" : "<string>" — skip string value, no capture. */
        if (!jmatch_key(&p, "group")) { free_groups(arr, n); free(buf); return -1; }
        jskip_ws(&p);
        if (!jskip_string(&p))        { free_groups(arr, n); free(buf); return -1; }
        if (!jexpect(&p, ','))        { free_groups(arr, n); free(buf); return -1; }

        /* "neid_range" : [lo, hi] */
        if (!jmatch_key(&p, "neid_range")) { free_groups(arr, n); free(buf); return -1; }
        if (!jexpect(&p, '['))             { free_groups(arr, n); free(buf); return -1; }
        int lo = 0, hi = 0;
        if (!jparse_int(&p, &lo))          { free_groups(arr, n); free(buf); return -1; }
        if (!jexpect(&p, ','))             { free_groups(arr, n); free(buf); return -1; }
        if (!jparse_int(&p, &hi))          { free_groups(arr, n); free(buf); return -1; }
        if (!jexpect(&p, ']'))             { free_groups(arr, n); free(buf); return -1; }
        if (lo < 0 || hi < lo)             { free_groups(arr, n); free(buf); return -1; }
        if (!jexpect(&p, ','))             { free_groups(arr, n); free(buf); return -1; }

        /* "dtypes" : [ int, int, ... ] — non-empty */
        if (!jmatch_key(&p, "dtypes"))     { free_groups(arr, n); free(buf); return -1; }
        if (!jexpect(&p, '['))             { free_groups(arr, n); free(buf); return -1; }

        size_t dcap = 8, dn = 0;
        int *dtypes = (int *)malloc(dcap * sizeof(int));
        if (!dtypes) { free_groups(arr, n); free(buf); return -1; }

        jskip_ws(&p);
        if (*p == ']') {
            /* empty dtypes[] → reject */
            free(dtypes); free_groups(arr, n); free(buf); return -1;
        }
        for (;;) {
            int v = 0;
            if (!jparse_int(&p, &v)) {
                free(dtypes); free_groups(arr, n); free(buf); return -1;
            }
            if (dn == dcap) {
                dcap *= 2;
                int *grow = (int *)realloc(dtypes, dcap * sizeof(int));
                if (!grow) {
                    free(dtypes); free_groups(arr, n); free(buf); return -1;
                }
                dtypes = grow;
            }
            dtypes[dn++] = v;
            jskip_ws(&p);
            if (*p == ',') { p++; continue; }
            if (*p == ']') { p++; break; }
            free(dtypes); free_groups(arr, n); free(buf); return -1;
        }

        /* Grow group array if needed. */
        if (n == cap) {
            cap *= 2;
            struct clan_ncland_group *grow = (struct clan_ncland_group *)
                realloc(arr, cap * sizeof(*arr));
            if (!grow) {
                free(dtypes); free_groups(arr, n); free(buf); return -1;
            }
            arr = grow;
            memset(&arr[n], 0, (cap - n) * sizeof(*arr));
        }

        /* Overlap check against every prior group. O(n^2) — n is single digits. */
        for (size_t i = 0; i < n; i++) {
            if (intervals_overlap(arr[i].lo, arr[i].hi, lo, hi)) {
                free(dtypes); free_groups(arr, n); free(buf); return -1;
            }
        }

        arr[n].lo       = lo;
        arr[n].hi       = hi;
        arr[n].dtypes   = dtypes;
        arr[n].n_dtypes = dn;
        n++;

        if (!jexpect(&p, '}')) { free_groups(arr, n); free(buf); return -1; }
        jskip_ws(&p);
        if (*p == ',') { p++; continue; }
        if (*p == ']') { p++; break; }
        free_groups(arr, n); free(buf); return -1;
    }

    /* Trailing content is OK (loose parser); we've consumed the groups array. */
    free(buf);
    *out_arr = arr;
    *out_n   = n;
    return 0;
}
```

Add `#include <errno.h>` and `#include <string.h>` at top if not already present — they are.

- [ ] **Step 3: Replace `load_cfg_once` and `clan_ncland_supports_dtype` (currently lines 119-162)**

Replace with:

```c
/**
 * @brief Lazy one-time load of /usr/cnc/lib/data/ncland/ncland.json into
 *        g_groups/g_n_groups. Called via pthread_once from
 *        clan_ncland_supports.
 *
 * On parse error, leaves g_cfg_ok = false (routes nothing — caller drops
 * back to legacy clan path).
 */
static void load_cfg_once(void)
{
#ifdef CLAN_NCLAND_ROUTE_TEST
    if (g_test_override) return;
#endif
    /* TODO hot-reload: pthread_once means /usr/cnc/lib/data/ncland/ncland.json
     * is read exactly once per process lifetime. Editing the allowlist requires
     * restarting every consumer (dacsproc, fe, arm/qrycga, m_clan, etc.). If
     * ops needs live reload, add SIGHUP or inotify here and rebuild g_groups
     * under a rwlock. Deferred: deployment cadence is low. */
    struct clan_ncland_group *arr = NULL;
    size_t n = 0;
    if (parse_ncland_json("/usr/cnc/lib/data/ncland/ncland.json", &arr, &n) == 0) {
        g_groups   = arr;
        g_n_groups = n;
        g_cfg_ok   = true;
        TRACE(D3, "clan_ncland_route: loaded %zu groups\n", n);
    } else {
        TRACE(D0, "clan_ncland_route: ncland.json missing/malformed; "
                  "routing nothing to ncland\n");
    }
}

/**
 * @brief Return true iff some group covers @p neid AND its dtypes[]
 *        contains @p dtype.
 *
 * Triggers lazy load via pthread_once on first call. Lock-free read path.
 *
 * @param neid   Network element id.
 * @param dtype  Data type.
 * @return true if (neid,dtype) is routed to ncland; false otherwise
 *         (including any cfg load failure).
 */
bool clan_ncland_supports(int neid, int dtype)
{
    pthread_once(&g_once_cfg, load_cfg_once);
    if (!g_cfg_ok) return false;
    for (size_t i = 0; i < g_n_groups; i++) {
        const struct clan_ncland_group *g = &g_groups[i];
        if (neid < g->lo || neid > g->hi) continue;
        for (size_t j = 0; j < g->n_dtypes; j++)
            if (g->dtypes[j] == dtype) return true;
        /* neid was in this group's range but dtype wasn't. Ranges are
         * non-overlapping, so no other group can cover this neid — done. */
        return false;
    }
    return false;
}
```

- [ ] **Step 4: Update test shims at bottom of file (currently lines 254-311)**

Replace the `#ifdef CLAN_NCLAND_ROUTE_TEST` block at bottom with:

```c
#ifdef CLAN_NCLAND_ROUTE_TEST
/**
 * @brief Test shim: expose parse_ncland_json for unit tests.
 */
int test_parse_ncland_json(const char *path,
                           struct clan_ncland_group **out_arr,
                           size_t *out_n)
{ return parse_ncland_json(path, out_arr, out_n); }

/**
 * @brief Test shim: populate g_groups[] directly + mark test override.
 *        Copies the input; caller retains ownership of arr.
 *
 * @param arr  Group array (deep-copied).
 * @param n    Array length.
 */
void clan_ncland_route_set_groups_for_test(const struct clan_ncland_group *arr,
                                           size_t n)
{
    free_groups(g_groups, g_n_groups);
    g_groups = NULL; g_n_groups = 0;
    if (n > 0) {
        g_groups = (struct clan_ncland_group *)calloc(n, sizeof(*g_groups));
        for (size_t i = 0; i < n; i++) {
            g_groups[i].lo       = arr[i].lo;
            g_groups[i].hi       = arr[i].hi;
            g_groups[i].n_dtypes = arr[i].n_dtypes;
            g_groups[i].dtypes   = (int *)malloc(arr[i].n_dtypes * sizeof(int));
            memcpy(g_groups[i].dtypes, arr[i].dtypes,
                   arr[i].n_dtypes * sizeof(int));
        }
        g_n_groups = n;
    }
    g_cfg_ok = true;
    g_test_override = true;
}

/**
 * @brief Test shim: install a pre-opened mqd.
 */
void clan_ncland_route_set_mqd_for_test(mqd_t mqd)
{
    g_mqd = mqd;
}

/**
 * @brief Test shim: zero out all module state.
 */
void clan_ncland_route_reset_for_test(void)
{
    free_groups(g_groups, g_n_groups);
    g_groups = NULL; g_n_groups = 0; g_cfg_ok = false;
    g_test_override = false;
    if (g_mqd != (mqd_t)-1) { mq_close(g_mqd); g_mqd = (mqd_t)-1; }
}
#endif
```

- [ ] **Step 5: Verify includes near top of file**

Confirm `#include <limits.h>` is not needed (only INT_MAX-ish checks use literal 65535). Confirm `<errno.h>` and `<string.h>` present — they are.

- [ ] **Step 6: Commit**

```bash
git add cnc/utility/src/clan_ncland_route.c
git commit -m "clan_ncland_route: groups-schema parser + (neid,dtype) supports"
```

---

## Task 3: Rewrite standalone tests

**Files:**
- Modify: `cnc/utility/src/clan_ncland_route_tests.c`

- [ ] **Step 1: Replace file contents**

```c
/** @file clan_ncland_route_tests.c
 *  Standalone tests for clan_ncland_route (groups schema). Built with
 *  -DCLAN_NCLAND_ROUTE_TEST via util.mk clan_ncland_route_tests target. */
#define CLAN_NCLAND_ROUTE_TEST
#include "clan_ncland_route.h"

#include <assert.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <fcntl.h>
#include <mqueue.h>
#include <msgscreen.h>

/* ----------------------------------------------------------- */
/* Helpers                                                     */
/* ----------------------------------------------------------- */

/* Write @p body to a fresh /tmp file. Caller must unlink. */
static void write_tmp(char *path_out, const char *body)
{
    strcpy(path_out, "/tmp/clan_ncland_route_test_XXXXXX");
    int fd = mkstemp(path_out);
    assert(fd >= 0);
    ssize_t len = (ssize_t)strlen(body);
    assert(write(fd, body, len) == len);
    close(fd);
}

/* Free the array returned by test_parse_ncland_json. */
static void free_groups_arr(struct clan_ncland_group *arr, size_t n)
{
    for (size_t i = 0; i < n; i++) free(arr[i].dtypes);
    free(arr);
}

/* ----------------------------------------------------------- */
/* Parse tests                                                 */
/* ----------------------------------------------------------- */

static void test_parse_happy(void) {
    char path[64];
    write_tmp(path,
        "{\"version\":1,\"groups\":["
        " {\"group\":\"A\",\"neid_range\":[1,500],\"dtypes\":[206,207]},"
        " {\"group\":\"B\",\"neid_range\":[501,1000],\"dtypes\":[223]}"
        "]}");
    struct clan_ncland_group *arr = NULL; size_t n = 0;
    int rc = test_parse_ncland_json(path, &arr, &n);
    unlink(path);
    assert(rc == 0);
    assert(n == 2);
    assert(arr[0].lo == 1   && arr[0].hi == 500);
    assert(arr[0].n_dtypes == 2 && arr[0].dtypes[0] == 206 && arr[0].dtypes[1] == 207);
    assert(arr[1].lo == 501 && arr[1].hi == 1000);
    assert(arr[1].n_dtypes == 1 && arr[1].dtypes[0] == 223);
    free_groups_arr(arr, n);
    printf("PASS: test_parse_happy\n");
}

static void test_parse_bad_version(void) {
    char path[64];
    write_tmp(path, "{\"version\":2,\"groups\":[]}");
    struct clan_ncland_group *arr = NULL; size_t n = 0;
    int rc = test_parse_ncland_json(path, &arr, &n);
    unlink(path);
    assert(rc == -1);
    printf("PASS: test_parse_bad_version\n");
}

static void test_parse_missing_groups(void) {
    char path[64];
    write_tmp(path, "{\"version\":1}");
    struct clan_ncland_group *arr = NULL; size_t n = 0;
    int rc = test_parse_ncland_json(path, &arr, &n);
    unlink(path);
    assert(rc == -1);
    printf("PASS: test_parse_missing_groups\n");
}

static void test_parse_empty_groups(void) {
    char path[64];
    write_tmp(path, "{\"version\":1,\"groups\":[]}");
    struct clan_ncland_group *arr = NULL; size_t n = 0;
    int rc = test_parse_ncland_json(path, &arr, &n);
    unlink(path);
    assert(rc == -1);
    printf("PASS: test_parse_empty_groups\n");
}

static void test_parse_bad_range(void) {
    char path[64];
    /* hi < lo */
    write_tmp(path,
        "{\"version\":1,\"groups\":["
        " {\"group\":\"A\",\"neid_range\":[500,1],\"dtypes\":[1]}"
        "]}");
    struct clan_ncland_group *arr = NULL; size_t n = 0;
    int rc = test_parse_ncland_json(path, &arr, &n);
    unlink(path);
    assert(rc == -1);
    printf("PASS: test_parse_bad_range\n");
}

static void test_parse_missing_dtypes(void) {
    char path[64];
    write_tmp(path,
        "{\"version\":1,\"groups\":["
        " {\"group\":\"A\",\"neid_range\":[1,10]}"
        "]}");
    struct clan_ncland_group *arr = NULL; size_t n = 0;
    int rc = test_parse_ncland_json(path, &arr, &n);
    unlink(path);
    assert(rc == -1);
    printf("PASS: test_parse_missing_dtypes\n");
}

static void test_parse_empty_dtypes(void) {
    char path[64];
    write_tmp(path,
        "{\"version\":1,\"groups\":["
        " {\"group\":\"A\",\"neid_range\":[1,10],\"dtypes\":[]}"
        "]}");
    struct clan_ncland_group *arr = NULL; size_t n = 0;
    int rc = test_parse_ncland_json(path, &arr, &n);
    unlink(path);
    assert(rc == -1);
    printf("PASS: test_parse_empty_dtypes\n");
}

static void test_parse_non_int_dtype(void) {
    char path[64];
    write_tmp(path,
        "{\"version\":1,\"groups\":["
        " {\"group\":\"A\",\"neid_range\":[1,10],\"dtypes\":[1,\"foo\",3]}"
        "]}");
    struct clan_ncland_group *arr = NULL; size_t n = 0;
    int rc = test_parse_ncland_json(path, &arr, &n);
    unlink(path);
    assert(rc == -1);
    printf("PASS: test_parse_non_int_dtype\n");
}

static void test_parse_overlap(void) {
    char path[64];
    write_tmp(path,
        "{\"version\":1,\"groups\":["
        " {\"group\":\"A\",\"neid_range\":[1,500],\"dtypes\":[1]},"
        " {\"group\":\"B\",\"neid_range\":[400,600],\"dtypes\":[2]}"
        "]}");
    struct clan_ncland_group *arr = NULL; size_t n = 0;
    int rc = test_parse_ncland_json(path, &arr, &n);
    unlink(path);
    assert(rc == -1);
    printf("PASS: test_parse_overlap\n");
}

static void test_parse_legacy_flat_rejected(void) {
    char path[64];
    write_tmp(path, "{\"version\":1,\"dtypes\":[1,2,3]}");
    struct clan_ncland_group *arr = NULL; size_t n = 0;
    int rc = test_parse_ncland_json(path, &arr, &n);
    unlink(path);
    assert(rc == -1);
    printf("PASS: test_parse_legacy_flat_rejected\n");
}

/* ----------------------------------------------------------- */
/* Supports lookup                                             */
/* ----------------------------------------------------------- */

static void test_supports_hit_miss(void) {
    clan_ncland_route_reset_for_test();
    int dtA[] = {206, 207};
    int dtB[] = {223};
    struct clan_ncland_group groups[] = {
        { .lo = 1,   .hi = 500,  .dtypes = dtA, .n_dtypes = 2 },
        { .lo = 501, .hi = 1000, .dtypes = dtB, .n_dtypes = 1 },
    };
    clan_ncland_route_set_groups_for_test(groups, 2);

    assert(clan_ncland_supports(1,    206) == true);
    assert(clan_ncland_supports(500,  207) == true);
    assert(clan_ncland_supports(600,  223) == true);
    assert(clan_ncland_supports(1000, 223) == true);

    /* neid in range, dtype not in that group's dtypes */
    assert(clan_ncland_supports(1,    223) == false);
    assert(clan_ncland_supports(600,  206) == false);

    /* neid outside all ranges */
    assert(clan_ncland_supports(0,    206) == false);
    assert(clan_ncland_supports(1001, 223) == false);

    clan_ncland_route_reset_for_test();
    printf("PASS: test_supports_hit_miss\n");
}

static void test_supports_empty_cfg(void) {
    clan_ncland_route_reset_for_test();
    /* No set_groups_for_test call — g_cfg_ok is false, but pthread_once
     * has fired on prior tests. Force-clear by reset (leaves g_test_override
     * false so a real load would occur; but no /usr/cnc file, so still false).
     * That path is exercised implicitly by load_cfg_once's error branch. */
    assert(clan_ncland_supports(1, 1) == false);
    printf("PASS: test_supports_empty_cfg\n");
}

/* ----------------------------------------------------------- */
/* Send round-trip (unchanged)                                 */
/* ----------------------------------------------------------- */

static void test_send_round_trip(void) {
    clan_ncland_route_reset_for_test();

    struct mq_attr attr;
    memset(&attr, 0, sizeof(attr));
    attr.mq_maxmsg  = 4;
    attr.mq_msgsize = sizeof(struct dacs_msg);
    mq_unlink("/clan_ncland_route_test");
    mqd_t rd = mq_open("/clan_ncland_route_test",
                       O_CREAT | O_RDONLY | O_NONBLOCK, 0600, &attr);
    assert(rd != (mqd_t)-1);
    mqd_t wr = mq_open("/clan_ncland_route_test",
                       O_WRONLY | O_NONBLOCK);
    assert(wr != (mqd_t)-1);

    clan_ncland_route_set_mqd_for_test(wr);

    struct di_int di;
    memset(&di, 0, sizeof(di));
    di.di_tty      = 55;
    di.di_dacsid   = 207;
    di.di_slot     = 3;
    di.di_slot_tp  = 9;
    snprintf((char *)di.di_data, sizeof(di.di_data),
             "CTAG1:30:0:0:show shelf");

    int rc = clan_ncland_send(&di, 0);

    struct dacs_msg m;
    ssize_t n = mq_receive(rd, (char *)&m, sizeof(m), NULL);

    mq_close(rd);
    clan_ncland_route_reset_for_test();
    mq_unlink("/clan_ncland_route_test");

    assert(rc == 0);
    assert(n > 0);
    assert(m.dm_tty    == 55);
    assert(m.dm_dacsid == 207u);
    assert(m.dm_slot   == 3);
    assert(m.dm_slot_tp == 9);
    assert(m.dm_type   == DMTYPE_TEXT_MSG);
    assert(strcmp(m.u.dm_text, "CTAG1:30:0:0:show shelf") == 0);
    printf("PASS: test_send_round_trip\n");
}

int main(void) {
    test_parse_happy();
    test_parse_bad_version();
    test_parse_missing_groups();
    test_parse_empty_groups();
    test_parse_bad_range();
    test_parse_missing_dtypes();
    test_parse_empty_dtypes();
    test_parse_non_int_dtype();
    test_parse_overlap();
    test_parse_legacy_flat_rejected();
    test_supports_hit_miss();
    test_supports_empty_cfg();
    test_send_round_trip();
    printf("All tests passed.\n");
    return 0;
}
```

- [ ] **Step 2: Run tests — expect failures until Task 2 is in**

```bash
cd cnc/utility/src
nmake -f util.mk clan_ncland_route_tests
./clan_ncland_route_tests
```
Expected: all PASS if Tasks 1 & 2 are committed. Any FAIL means a bug in Task 2's parser — fix in `clan_ncland_route.c`, rebuild, retest.

- [ ] **Step 3: Commit**

```bash
git add cnc/utility/src/clan_ncland_route_tests.c
git commit -m "clan_ncland_route: tests — groups schema + (neid,dtype) lookup"
```

---

## Task 4: Migrate clan_util caller

**Files:**
- Modify: `cnc/utility/src/clan_util.c:348`

- [ ] **Step 1: Update the call site**

Change:

```c
	if ( clan_ncland_supports_dtype( neType ) )
```

To:

```c
	if ( clan_ncland_supports( c->dacsid, neType ) )
```

- [ ] **Step 2: Verify libutillib builds**

```bash
cd cnc/utility/src
nmake -f util.mk clan_util.o
nmake -f util.mk
```
Expected: clean build; no undefined-reference warnings for `clan_ncland_supports_dtype`.

- [ ] **Step 3: Commit**

```bash
git add cnc/utility/src/clan_util.c
git commit -m "clan_util: use clan_ncland_supports(neid,dtype)"
```

---

## Task 5: Wire the skip into m_clan

**Files:**
- Modify: `cnc/sdi/src/m_clan.c`

- [ ] **Step 1: Add include**

Near the existing include block (around line 29, after `<nf_signal.h>`), add:

```c
#include "clan_ncland_route.h"
```

- [ ] **Step 2: Insert skip in get_next_clan**

In `get_next_clan()`, immediately after the existing HAS_CLAN_CARD / isMeProvDemo continue-block (currently lines 298-302) and before the Remote Interfaces block (currently lines 305-311), insert:

```c
			/*******************************/
			/* Skip if ncland handles this */
			/* (neid, dtype)               */
			/*******************************/
			if (clan_ncland_supports(loop,
			    frmlnk->dcs_type[PRIMARY_INTERFACE])) {
				TRACE(D2, "NeId:%d dtype:%d routed to ncland; "
				          "skip clan\n",
				      loop,
				      frmlnk->dcs_type[PRIMARY_INTERFACE]);
				continue;
			}
```

The final structure of the top of the NE loop should be:

```c
		if (!NE_UNASSIGNED(frmlnk, FepId)) {
			/***********************/
			/* No process to start */
			/***********************/
			if (!HAS_CLAN_CARD(frmlnk->dcs_type[PRIMARY_INTERFACE])
			    &&
			    (!isMeProvDemo
			     (frmlnk->dcs_type[PRIMARY_INTERFACE])))
				continue;

			/*******************************/
			/* Skip if ncland handles this */
			/* (neid, dtype)               */
			/*******************************/
			if (clan_ncland_supports(loop,
			    frmlnk->dcs_type[PRIMARY_INTERFACE])) {
				TRACE(D2, "NeId:%d dtype:%d routed to ncland; "
				          "skip clan\n",
				      loop,
				      frmlnk->dcs_type[PRIMARY_INTERFACE]);
				continue;
			}

			/*********************/
			/* Remote Interfaces */
			/*********************/
			if (frmlnk->remote[PRIMARY_INTERFACE]) {
			    ...
```

- [ ] **Step 3: Build m_clan**

```bash
cd cnc/sdi/src
nmake -f sdisrc.mk ../../../3b2/bin/m_clan
```
Expected: clean build.

- [ ] **Step 4: Sanity-check trace output (manual)**

On a system with a populated `/usr/cnc/lib/data/ncland/ncland.json`, start `m_clan` with `debug_lvl m_clan 2` and confirm `NeId:X dtype:Y routed to ncland; skip clan` lines appear for expected NEs, and that no `clan` process is forked for those (neid, slot) pairs. If manual verification is not available in the current environment, note that skipped explicitly rather than claim it verified.

- [ ] **Step 5: Commit**

```bash
git add cnc/sdi/src/m_clan.c
git commit -m "m_clan: skip clan startup for ncland-routed (neid,dtype)"
```

---

## Task 6: Full-library rebuild sanity

**Files:** none.

- [ ] **Step 1: Rebuild libutillib and dependents**

```bash
cd cnc/utility/src
nmake -f util.mk
cd cnc/sdi/src
nmake -f sdisrc.mk
```
Expected: clean build, no unresolved symbols. Any `clan_ncland_supports_dtype` reference outside the code we've updated is a bug — grep for it:

```bash
grep -rn "clan_ncland_supports_dtype" /home/dan/Git/netflex/cnc
```
Expected output: no matches (the old name is gone from source, header, tests).

- [ ] **Step 2: Re-run standalone tests**

```bash
cd cnc/utility/src
./clan_ncland_route_tests
```
Expected: `All tests passed.`

- [ ] **Step 3: No commit unless build fixed something**

If the rebuild required a patch, commit it here. Otherwise this task closes with no new commit.

---

## Done

All spec requirements covered:
- §4.1 New API — Task 1.
- §4.2 Parser rewrite — Task 2.
- §4.3 clan_util migration — Task 4.
- §4.4 m_clan skip — Task 5.
- §4.5 Build (no .mk changes needed) — verified in Task 6.
- §5 Tests — Task 3, run in Task 3 & 6.
