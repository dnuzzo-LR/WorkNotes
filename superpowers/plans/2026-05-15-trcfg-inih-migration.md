# Trace Config (trcfg) via inih Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the ad-hoc `sscanf`-based parsing of `/usr/cnc/features/debug` with a versatile INI-backed trace config library (`trcfg`) using the vendored `inih` parser, preserving full backward compatibility with the existing flat-line file format.

**Architecture:** Vendor `ini.c` / `ini.h` from the `inih` project (BSD-3, single-file C89 parser) into `cnc/utility/src/`. Build a thin wrapper `trcfg.c` that loads `/usr/cnc/features/debug` once, stores parsed entries in two buckets: (1) **legacy bucket** for lines parsed before any `[section]` header — accessed via `trcfg_legacy(proc)` returning the raw RHS string for existing `sscanf`-based callers; (2) **structured bucket** for `[proc]` sections with typed key/value lookups via `trcfg_int(proc, key, dflt)` and `trcfg_str(proc, key, dflt)`. Existing `debug_lvlAll` / `debug_lvlId` / `debug_lvlPair` continue to work unchanged via the legacy bucket; new code uses structured sections. No field-file migration needed.

**Tech Stack:** C89 (gcc 4.8.5 baseline for RHEL7), Lucent/AT&T nmake, vendored `inih` r58+ (BSD-3-Clause), `libutillib.a` build target in `cnc/utility/src/util.mk`.

---

## File Structure

| File | Responsibility | Action |
|------|----------------|--------|
| `cnc/utility/src/ini.c` | Vendored inih parser implementation | Create |
| `cnc/utility/src/ini.h` | Vendored inih public header | Create |
| `cnc/utility/src/trcfg.c` | Wrapper: load file, two buckets, lookup API | Create |
| `include/trcfg.h` | Public API declarations for trcfg_* | Create |
| `include/utilproto.h` | Add prototypes (mirror of trcfg.h) | Modify |
| `include/utillibinc.h` | Add prototypes (mirror of trcfg.h) | Modify |
| `cnc/utility/src/util.mk` | Add `ini.o` and `trcfg.o` to `OBJ_UTILLIB` | Modify |
| `cnc/utility/src/util64.mk` | Same as util.mk for 64-bit build | Modify |
| `cnc/utility/src/debug.c` | Migrate `debug_lvlPair` to call `trcfg_legacy` | Modify |
| `cnc/utility/src/trcfg_test.c` | Standalone test harness (not linked into lib) | Create |

---

## Task 1: Vendor the inih library

**Files:**
- Create: `cnc/utility/src/ini.c`
- Create: `cnc/utility/src/ini.h`

- [ ] **Step 1: Download inih r58 source**

Fetch the tagged release source from `https://github.com/benhoyt/inih` (tag `r58`). Use the `ini.c` and `ini.h` files only — do not vendor the `examples/`, `tests/`, `extra/` directories.

```bash
cd /tmp
curl -L https://github.com/benhoyt/inih/archive/refs/tags/r58.tar.gz -o inih.tgz
tar xzf inih.tgz
cp inih-r58/ini.c /home/dan/Git/netflex/cnc/utility/src/ini.c
cp inih-r58/ini.h /home/dan/Git/netflex/cnc/utility/src/ini.h
```

- [ ] **Step 2: Verify license header preserved**

Open `cnc/utility/src/ini.c` and `cnc/utility/src/ini.h`. Confirm both files start with the BSD-3-Clause license block referencing Ben Hoyt. Do not strip or modify the license text.

- [ ] **Step 3: Configure compile-time options via wrapper header**

Edit `cnc/utility/src/ini.h` and add the following block immediately after the include guard `#define INI_H`:

```c
/* netFLEX trcfg configuration of inih */
#define INI_ALLOW_MULTILINE      0  /* one key per line, no continuation */
#define INI_ALLOW_BOM            0  /* ASCII-only file */
#define INI_ALLOW_INLINE_COMMENTS 0 /* legacy lines may contain ',' / ';' in RHS */
#define INI_STOP_ON_FIRST_ERROR  0  /* keep parsing past bad lines */
#define INI_CALL_HANDLER_ON_NEW_SECTION 0
#define INI_ALLOW_NO_VALUE       0
#define INI_INITIAL_ALLOC        512
#define INI_MAX_LINE             512
```

Rationale: legacy lines like `alrmhndlr=7,EX,smain.c,alm_db.c:145:1233` would be truncated at `;` if inline comments were on; turning them off preserves the legacy RHS byte-for-byte.

- [ ] **Step 4: Commit the vendored library**

```bash
git add cnc/utility/src/ini.c cnc/utility/src/ini.h
git commit -m "vendor inih r58 (BSD-3) into cnc/utility/src for trcfg"
```

---

## Task 2: Wire inih into util.mk / util64.mk

**Files:**
- Modify: `cnc/utility/src/util.mk` (add `ini.o` to `OBJ_UTILLIB`)
- Modify: `cnc/utility/src/util64.mk` (mirror change)

- [ ] **Step 1: Locate `OBJ_UTILLIB` line in util.mk**

Open `cnc/utility/src/util.mk`. The variable `OBJ_UTILLIB` starts near line 241 with `OBJ_UTILLIB = Dial.o adj_sz.o ...`. The list continues across multiple `\`-continued lines and is sorted roughly alphabetically.

- [ ] **Step 2: Add `ini.o` and `trcfg.o` to `OBJ_UTILLIB`**

Insert `ini.o` and `trcfg.o` near other `i*`/`t*` entries respectively, preserving the existing column formatting and `\` line continuations. Use the writing-nmake-makefiles skill conventions (no tabs/spaces ambiguity; mirror surrounding style).

Example insertion (place each in the correct alphabetical position within the existing list — do **not** create a separate line block):

```
... grouped.o gui_chg.o help_tbl.o ini.o int_bound.o ip_util.o ...
... tirks_util.o tracedb.o trcfg.o ttbl_*.o ...
```

- [ ] **Step 3: Mirror change in util64.mk**

Open `cnc/utility/src/util64.mk` and make the identical insertion into its `OBJ_UTILLIB` variable.

- [ ] **Step 4: Run nmake from the src directory and verify ini.o builds**

```bash
cd /home/dan/Git/netflex/cnc/utility/src
nmake -f util.mk ini.o
```

Expected: `ini.o` compiles with no warnings. If gcc 4.8.5 baseline produces warnings about `static inline`, `restrict`, or C99 syntax, halt — the vendored file should be C89-clean; report which line failed.

- [ ] **Step 5: Commit**

```bash
git add cnc/utility/src/util.mk cnc/utility/src/util64.mk
git commit -m "build: add ini.o and trcfg.o to libutillib"
```

---

## Task 3: Create trcfg public header

**Files:**
- Create: `include/trcfg.h`

- [ ] **Step 1: Write the header**

```c
/**
 * @file trcfg.h
 * @brief Trace/debug configuration loader backed by /usr/cnc/features/debug.
 *
 * Parses the debug config file as INI.  Lines before any [section] header
 * are stored in a "legacy bucket" addressable as proc=<rhs>, preserving
 * the historical flat format used by debug_lvlAll/debug_lvlId/debug_lvlPair.
 * Lines inside [section] headers are stored as structured key=value pairs
 * addressable via trcfg_int/trcfg_str.
 */
#ifndef TRCFG_H
#define TRCFG_H

#ifdef __cplusplus
extern "C" {
#endif

/**
 * @brief Load /usr/cnc/features/debug into the in-memory config store.
 *
 * Safe to call repeatedly; each call discards the previous store and
 * re-reads the file.  Equivalent to trcfg_reload().
 *
 * @return SUCCESS on parse; FAILURE if the file cannot be opened.
 */
int trcfg_init(void);

/**
 * @brief Discard the current store and re-parse the config file.
 *
 * @return SUCCESS on parse; FAILURE if the file cannot be opened.
 */
int trcfg_reload(void);

/**
 * @brief Return the raw RHS string of a legacy (no-section) line.
 *
 * For a file line `proc=value-string`, returns the value-string. Used
 * by callers that still parse legacy comma/colon-laden RHS themselves
 * (debug_lvlAll, debug_lvlId, debug_lvlPair).
 *
 * @param proc  Process tag to match (exact string compare).
 * @return Pointer to internal storage (do not free); NULL if not found.
 */
const char *trcfg_legacy(const char *proc);

/**
 * @brief Look up an integer key in a [section].
 *
 * @param proc  Section name.
 * @param key   Key within the section.
 * @param dflt  Value returned if section or key not found.
 * @return Parsed integer or @p dflt.
 */
int trcfg_int(const char *proc, const char *key, int dflt);

/**
 * @brief Look up a string key in a [section].
 *
 * @param proc  Section name.
 * @param key   Key within the section.
 * @param dflt  String returned if section or key not found.
 * @return Pointer to internal storage (do not free); @p dflt if not found.
 */
const char *trcfg_str(const char *proc, const char *key, const char *dflt);

#ifdef __cplusplus
}
#endif

#endif /* TRCFG_H */
```

- [ ] **Step 2: Commit**

```bash
git add include/trcfg.h
git commit -m "trcfg: add public header for INI-backed trace config"
```

---

## Task 4: Write the failing test for trcfg_legacy

**Files:**
- Create: `cnc/utility/src/trcfg_test.c`

- [ ] **Step 1: Write the test harness skeleton**

```c
/* Standalone test for trcfg.  Not linked into libutillib.
 * Build:
 *   gcc -I../../../include -DTRCFG_TEST_CONFIG=\"./trcfg_test.ini\" \
 *       trcfg_test.c trcfg.c ini.c -o trcfg_test
 * Run:
 *   ./trcfg_test
 * Expected exit status: 0 on all-pass.
 */
#include <stdio.h>
#include <string.h>
#include <stdlib.h>
#include <trcfg.h>

static int failures = 0;

#define EXPECT_STREQ(a, b) do {                                    \
    const char *_a = (a), *_b = (b);                               \
    if (!_a || !_b || strcmp(_a, _b) != 0) {                       \
        fprintf(stderr, "FAIL %s:%d: \"%s\" != \"%s\"\n",          \
                __FILE__, __LINE__, _a ? _a : "(null)",            \
                _b ? _b : "(null)");                               \
        failures++;                                                \
    }                                                              \
} while (0)

#define EXPECT_EQ(a, b) do {                                       \
    long _a = (long)(a), _b = (long)(b);                           \
    if (_a != _b) {                                                \
        fprintf(stderr, "FAIL %s:%d: %ld != %ld\n",                \
                __FILE__, __LINE__, _a, _b);                       \
        failures++;                                                \
    }                                                              \
} while (0)

static void write_fixture(const char *path)
{
    FILE *f = fopen(path, "w");
    if (!f) { perror(path); exit(2); }
    fputs("# legacy lines (no section header)\n", f);
    fputs("cpymemd=5\n", f);
    fputs("alrmhndlr=7,EX,smain.c,alm_db.c:145:1233\n", f);
    fputs("\n", f);
    fputs("[wvlsvc]\n", f);
    fputs("level = 4\n", f);
    fputs("modules = main.c,db.c\n", f);
    fputs("\n", f);
    fputs("[arm]\n", f);
    fputs("level = 3\n", f);
    fputs("neid  = 442\n", f);
    fclose(f);
}

int main(void)
{
    const char *path = "./trcfg_test.ini";
    write_fixture(path);

    /* The production code reads /usr/cnc/features/debug; the test build
     * overrides via -DTRCFG_TEST_CONFIG=\"./trcfg_test.ini\". */
    if (trcfg_init() != 0 /* SUCCESS */) {
        fprintf(stderr, "trcfg_init failed\n");
        return 2;
    }

    EXPECT_STREQ(trcfg_legacy("cpymemd"), "5");
    EXPECT_STREQ(trcfg_legacy("alrmhndlr"),
                 "7,EX,smain.c,alm_db.c:145:1233");
    EXPECT_EQ(trcfg_legacy("nonexistent") == NULL, 1);

    EXPECT_EQ(trcfg_int("wvlsvc", "level", -1), 4);
    EXPECT_STREQ(trcfg_str("wvlsvc", "modules", ""), "main.c,db.c");

    EXPECT_EQ(trcfg_int("arm", "level", -1), 3);
    EXPECT_EQ(trcfg_int("arm", "neid",  -1), 442);

    EXPECT_EQ(trcfg_int("arm", "missing_key", 99), 99);
    EXPECT_STREQ(trcfg_str("arm", "missing_key", "fallback"), "fallback");
    EXPECT_EQ(trcfg_int("missing_proc", "level", -7), -7);

    remove(path);
    if (failures) {
        fprintf(stderr, "%d test(s) failed\n", failures);
        return 1;
    }
    puts("trcfg_test: all pass");
    return 0;
}
```

- [ ] **Step 2: Attempt to build the test (it must fail — trcfg.c does not exist yet)**

```bash
cd /home/dan/Git/netflex/cnc/utility/src
gcc -I../../../include -DTRCFG_TEST_CONFIG=\"./trcfg_test.ini\" \
    trcfg_test.c trcfg.c ini.c -o trcfg_test 2>&1 | head -5
```

Expected: build fails with `trcfg.c: No such file or directory`. This confirms the test is wired before the implementation.

- [ ] **Step 3: Commit the failing test**

```bash
git add cnc/utility/src/trcfg_test.c
git commit -m "trcfg: add failing test harness (red phase)"
```

---

## Task 5: Implement trcfg.c — store and inih callback

**Files:**
- Create: `cnc/utility/src/trcfg.c`

- [ ] **Step 1: Write the implementation**

```c
/**
 * @file trcfg.c
 * @brief INI-backed trace config loader.
 *
 * Two storage buckets:
 *   - "legacy"     : lines parsed before any [section] header.
 *                    Keyed by proc name; value = raw RHS string.
 *   - "structured" : lines parsed inside [section] headers.
 *                    Keyed by (section, key); value = raw RHS string.
 *
 * Both buckets use simple linear-search arrays.  The debug config file is
 * small (tens of lines in practice) so a hash table is unnecessary.
 *
 * Storage is owned by this translation unit.  Strings are strdup'd from
 * inih's transient buffers and freed on reload.
 */
#include <pragma_utillib.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include <retcodes.h>
#include <trcfg.h>
#include "ini.h"

#ifndef TRCFG_TEST_CONFIG
#define TRCFG_CONFIG_PATH "/usr/cnc/features/debug"
#else
#define TRCFG_CONFIG_PATH TRCFG_TEST_CONFIG
#endif

typedef struct {
    char *proc;     /**< Proc tag (legacy bucket only) or section (structured). */
    char *key;      /**< Key name (structured only; NULL for legacy). */
    char *value;    /**< RHS string, strdup'd. */
} trcfg_entry_t;

static trcfg_entry_t *g_legacy     = NULL;
static int            g_legacy_n   = 0;
static int            g_legacy_cap = 0;

static trcfg_entry_t *g_struct     = NULL;
static int            g_struct_n   = 0;
static int            g_struct_cap = 0;

static void free_bucket(trcfg_entry_t **buf, int *n, int *cap)
{
    int i;
    if (*buf == NULL)
        return;
    for (i = 0; i < *n; i++) {
        free((*buf)[i].proc);
        free((*buf)[i].key);
        free((*buf)[i].value);
    }
    free(*buf);
    *buf = NULL;
    *n = 0;
    *cap = 0;
}

static int grow_bucket(trcfg_entry_t **buf, int *cap)
{
    int new_cap = (*cap == 0) ? 16 : (*cap * 2);
    trcfg_entry_t *p = (trcfg_entry_t *)realloc(*buf,
                              (size_t)new_cap * sizeof(trcfg_entry_t));
    if (p == NULL)
        return FAILURE;
    *buf = p;
    *cap = new_cap;
    return SUCCESS;
}

static int append_entry(trcfg_entry_t **buf, int *n, int *cap,
                        const char *proc, const char *key, const char *value)
{
    char *dup_proc, *dup_key = NULL, *dup_value;
    if (*n == *cap && grow_bucket(buf, cap) != SUCCESS)
        return FAILURE;
    dup_proc  = strdup(proc);
    dup_value = strdup(value ? value : "");
    if (key != NULL)
        dup_key = strdup(key);
    if (dup_proc == NULL || dup_value == NULL ||
        (key != NULL && dup_key == NULL)) {
        free(dup_proc); free(dup_key); free(dup_value);
        return FAILURE;
    }
    (*buf)[*n].proc  = dup_proc;
    (*buf)[*n].key   = dup_key;
    (*buf)[*n].value = dup_value;
    (*n)++;
    return SUCCESS;
}

/**
 * @brief inih callback.  section == "" means line was before any [header].
 */
static int handler(void *user, const char *section,
                   const char *name, const char *value)
{
    (void)user;
    if (section == NULL || section[0] == '\0') {
        /* Legacy bucket: name is the proc tag, value is the RHS string. */
        return append_entry(&g_legacy, &g_legacy_n, &g_legacy_cap,
                            name, NULL, value) == SUCCESS ? 1 : 0;
    }
    /* Structured bucket: section is proc, name is key. */
    return append_entry(&g_struct, &g_struct_n, &g_struct_cap,
                        section, name, value) == SUCCESS ? 1 : 0;
}

int trcfg_init(void)
{
    return trcfg_reload();
}

int trcfg_reload(void)
{
    int rc;
    free_bucket(&g_legacy, &g_legacy_n, &g_legacy_cap);
    free_bucket(&g_struct, &g_struct_n, &g_struct_cap);
    rc = ini_parse(TRCFG_CONFIG_PATH, handler, NULL);
    /* ini_parse returns 0 on success, -1 if file cannot be opened,
     * -2 on memory error, or a line number on parse error. */
    if (rc == -1)
        return FAILURE;
    return SUCCESS;
}

const char *trcfg_legacy(const char *proc)
{
    int i;
    if (proc == NULL)
        return NULL;
    /* Last-write-wins: scan in reverse. */
    for (i = g_legacy_n - 1; i >= 0; i--) {
        if (strcmp(g_legacy[i].proc, proc) == 0)
            return g_legacy[i].value;
    }
    return NULL;
}

const char *trcfg_str(const char *proc, const char *key, const char *dflt)
{
    int i;
    if (proc == NULL || key == NULL)
        return dflt;
    for (i = g_struct_n - 1; i >= 0; i--) {
        if (strcmp(g_struct[i].proc, proc) == 0 &&
            strcmp(g_struct[i].key,  key)  == 0)
            return g_struct[i].value;
    }
    return dflt;
}

int trcfg_int(const char *proc, const char *key, int dflt)
{
    const char *s = trcfg_str(proc, key, NULL);
    char *end;
    long v;
    if (s == NULL)
        return dflt;
    v = strtol(s, &end, 0);
    if (end == s)
        return dflt;
    return (int)v;
}
```

- [ ] **Step 2: Build and run the test**

```bash
cd /home/dan/Git/netflex/cnc/utility/src
gcc -I../../../include -DTRCFG_TEST_CONFIG=\"./trcfg_test.ini\" \
    trcfg_test.c trcfg.c ini.c -o trcfg_test && ./trcfg_test
```

Expected output:
```
trcfg_test: all pass
```

Exit status 0.

- [ ] **Step 3: If failures, diagnose**

If any `FAIL` lines appear, inspect the line number, fix the corresponding logic in `trcfg.c` (do not edit the test to make it pass), rebuild, rerun. Common pitfalls:
- `ini_parse` returning the line number of a parse error → check the fixture file for stray characters.
- `trcfg_legacy("alrmhndlr")` returning `"7"` instead of the full comma string → `INI_ALLOW_INLINE_COMMENTS` is on; verify Step 3 of Task 1 was applied.

- [ ] **Step 4: Build the library and verify no regressions**

```bash
cd /home/dan/Git/netflex/cnc/utility/src
nmake -f util.mk
```

Expected: clean build, `trcfg.o` and `ini.o` appear in the archive.

- [ ] **Step 5: Commit**

```bash
git add cnc/utility/src/trcfg.c
git commit -m "trcfg: implement INI-backed trace config loader"
```

---

## Task 6: Add prototypes to utillibinc.h and utilproto.h

**Files:**
- Modify: `include/utillibinc.h`
- Modify: `include/utilproto.h`

- [ ] **Step 1: Add trcfg_* prototypes to utillibinc.h**

Open `include/utillibinc.h`. Locate the `extern "C"` block (around line 410, near the existing `debug_lvl` and `debug_lvlPair` declarations). Insert the following immediately after the `debug_lvlPair` line:

```c
	int  trcfg_init(void);
	int  trcfg_reload(void);
	const char *trcfg_legacy(const char *proc);
	int  trcfg_int(const char *proc, const char *key, int dflt);
	const char *trcfg_str(const char *proc, const char *key, const char *dflt);
```

- [ ] **Step 2: Add same prototypes to utilproto.h**

Open `include/utilproto.h`. Locate the `extern "C"` block (around line 13). Insert the same five prototypes immediately after the `debug_lvlPair` line.

- [ ] **Step 3: Rebuild library to verify headers parse**

```bash
cd /home/dan/Git/netflex/cnc/utility/src
nmake -f util.mk
```

Expected: clean build, no header-related warnings.

- [ ] **Step 4: Commit**

```bash
git add include/utillibinc.h include/utilproto.h
git commit -m "trcfg: declare public API in utillibinc.h and utilproto.h"
```

---

## Task 7: Migrate debug_lvlPair to use trcfg_legacy

**Files:**
- Modify: `cnc/utility/src/debug.c` (lines 294–335 — the body of `debug_lvlPair`)

- [ ] **Step 1: Replace the file-open / fgets loop with a trcfg_legacy lookup**

Locate the current implementation of `debug_lvlPair` in `cnc/utility/src/debug.c` (starts around line 294). Replace the function body with:

```c
int
debug_lvlPair( const char* proc, FILE* tfptr, int* p_key, int* p_level )
{
    const char *rhs;
    int  key;
    int  level;

    if ( proc == NULL || p_key == NULL || p_level == NULL )
        return( FAILURE );

    if ( trcfg_reload() != SUCCESS )
        return( FAILURE );

    rhs = trcfg_legacy( proc );
    if ( rhs == NULL )
        return( FAILURE );

    if ( sscanf( rhs, "%d,%d", &key, &level ) != 2 )
        return( FAILURE );

    *p_key   = key;
    *p_level = level;

    if ( tfptr )
    {
        time_t secs;
        time( &secs );
        fprintf( tfptr,
                 "debug_lvlPair: proc: %s key=%d level=%d. %s",
                 proc, key, level, ctime( &secs ) );
    }
    return( SUCCESS );
}
```

Also remove the now-unused local prototype at `cnc/utility/src/debug.c:25`:

```c
int debug_lvlPair( const char* proc, FILE* tfptr, int* p_key, int* p_level );
```

(Keep it only if other functions in the same file call it before its definition; check by searching for `debug_lvlPair` in `debug.c` — if the only references are the prototype, the function definition, and the `fprintf` log message, the prototype can be removed.)

- [ ] **Step 2: Add the trcfg.h include at the top of debug.c**

Open `cnc/utility/src/debug.c`. After the existing `#include <tracemod.h>` line (near line 13), add:

```c
#include <trcfg.h>
```

- [ ] **Step 3: Rebuild and confirm no regressions**

```bash
cd /home/dan/Git/netflex/cnc/utility/src
nmake -f util.mk
```

Expected: clean build.

- [ ] **Step 4: Smoke-test debug_lvlPair with a fixture file**

Create a temporary fixture and a tiny driver to confirm `debug_lvlPair` still parses `proc=key,level` lines correctly:

```bash
cd /tmp
sudo mkdir -p /usr/cnc/features
echo 'cpymemd.neid=442,5' | sudo tee /usr/cnc/features/debug
cat > /tmp/lvlpair_smoke.c <<'EOF'
#include <stdio.h>
#include <retcodes.h>
#include <utilproto.h>
int main(void) {
    int k = -1, l = -1;
    int rc = debug_lvlPair("cpymemd.neid", stderr, &k, &l);
    printf("rc=%d key=%d level=%d\n", rc, k, l);
    return rc == SUCCESS && k == 442 && l == 5 ? 0 : 1;
}
EOF
gcc -I/home/dan/Git/netflex/include /tmp/lvlpair_smoke.c \
    -L/home/dan/Git/netflex/cnc/utility/src -lutillib -o /tmp/lvlpair_smoke
/tmp/lvlpair_smoke
```

Expected: `rc=0 key=442 level=5` and exit status 0.

(If `sudo` access to `/usr/cnc/features/debug` is not available in the build environment, skip this step and document it as a manual test on a target box.)

- [ ] **Step 5: Commit**

```bash
git add cnc/utility/src/debug.c
git commit -m "debug_lvlPair: migrate to trcfg_legacy for file parsing"
```

---

## Task 8: Optional — migrate debug_lvlAll to use trcfg_legacy

**Files:**
- Modify: `cnc/utility/src/debug.c` (lines 72–137 — the body of `debug_lvlAll`)

This task is optional and may be deferred. It is included for completeness because the legacy parser inside `debug_lvlAll` is the most fragile remaining `sscanf` site.

- [ ] **Step 1: Replace the file-open / fgets loop with trcfg_legacy**

Inside `debug_lvlAll`, replace the `fopen` / `while (fgets …)` block with:

```c
{
    const char *rhs;
    int  level;
    char modules_copy[ 512 ];

    if ( trcfg_reload() != SUCCESS )
        return( FAILURE );

    rhs = trcfg_legacy( proc );
    if ( rhs == NULL )
        return( FAILURE );

    if ( sscanf( rhs, "%d", &level ) != 1 )
        return( FAILURE );

    if ( level != DebugLvl )
    {
        if ( ! firsttime )
        {
            time_t secs;
            time( &secs );
            if ( tfptr )
                fprintf( tfptr,
                         "DEBUG LEVEL CHANGED FROM %d TO %d. %s",
                         DebugLvl, level, ctime( &secs ));
        }
        else
            firsttime = 0;

        DebugLvl = level;
    }

    /* initTraceModules expects the legacy "proc=rhs" buffer shape. */
    snprintf( modules_copy, sizeof(modules_copy), "%s=%s", proc, rhs );
    initTraceModules( modules_copy );
    return( SUCCESS );
}
```

Note: `initTraceModules` historically received the entire fgets line including `proc=`. We reconstruct that shape via `snprintf` to avoid changing the consumer.

- [ ] **Step 2: Rebuild**

```bash
cd /home/dan/Git/netflex/cnc/utility/src
nmake -f util.mk
```

- [ ] **Step 3: Smoke-test**

On a development target, set `/usr/cnc/features/debug` to a known content, run a process that calls `debug_chk`, confirm trace output behavior matches the previous build.

- [ ] **Step 4: Commit**

```bash
git add cnc/utility/src/debug.c
git commit -m "debug_lvlAll: migrate to trcfg_legacy for file parsing"
```

---

## Task 9: Document the migration

**Files:**
- Modify: `cnc/utility/src/debug.c` (file-top comment block)

- [ ] **Step 1: Add a top-of-file note describing the dual format**

Open `cnc/utility/src/debug.c`. Insert the following comment block immediately after the `#include` lines:

```c
/**
 * @section trcfg_migration Trace config migration (2026-05-15)
 *
 * /usr/cnc/features/debug is now parsed by the trcfg library (which uses
 * the vendored inih INI parser).  Two coexisting formats are supported in
 * the same file:
 *
 * 1. Legacy flat lines (no section header) — keep working unchanged:
 *
 *        cpymemd=5
 *        alrmhndlr=7,EX,smain.c,alm_db.c:145:1233
 *        cpymemd.neid=442,5
 *
 *    Accessed via trcfg_legacy(proc) which returns the raw RHS string.
 *
 * 2. Structured INI sections — for new procs and richer config:
 *
 *        [wvlsvc]
 *        level   = 4
 *        modules = main.c, db.c
 *
 *        [arm]
 *        level = 3
 *        neid  = 442
 *
 *    Accessed via trcfg_int(proc, key, dflt) and trcfg_str(proc, key, dflt).
 *
 * A proc should appear in exactly one format.  Mixing legacy and
 * structured entries for the same proc is not defined.
 */
```

- [ ] **Step 2: Commit**

```bash
git add cnc/utility/src/debug.c
git commit -m "docs: describe trcfg dual-format migration in debug.c"
```

---

## Self-Review Notes

- **Spec coverage:** legacy compatibility (Tasks 5, 7), new INI sections (Tasks 5, 6), inih vendoring (Task 1), build wiring (Task 2), public API (Tasks 3, 6), tests (Task 4), production migration of one caller (Task 7), optional broader migration (Task 8), documentation (Task 9). All five points from the brainstorm discussion are covered.
- **Gotchas captured:** inline-comment handling (Task 1 Step 3), last-write-wins duplicate-key policy (Task 5 — reverse scan), gcc 4.8.5 C89 compatibility (Task 2 Step 4 check).
- **Placeholder scan:** no `TBD`, `TODO`, `implement later` strings in steps; all code blocks are complete.
- **Type consistency:** `trcfg_init` / `trcfg_reload` / `trcfg_legacy` / `trcfg_int` / `trcfg_str` signatures match across header (Task 3), implementation (Task 5), header re-declarations (Task 6), and callers (Tasks 7, 8).
- **Risk:** `trcfg_reload()` is called on every `debug_lvlPair` invocation in Task 7 to preserve the historical "re-read file each call" semantic. If a calling daemon invokes this in a hot loop, consider caching in a follow-up plan.
