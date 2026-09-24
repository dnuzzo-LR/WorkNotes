# upgsnap (netFLEX upgrade snapshot) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A new read-only C command `/usr/cnc/bin/upgsnap` that prints normalized, diff-friendly `key: value` sections (system, rdb, nes, ne_links, equipment, alarms, dbcheck, db_*) for before/after upgrade comparison.

**Architecture:** `upgsnap.c` (options, attach, section table, output) + gatherers in `upgsnap_sys.c`, `upgsnap_rdb.c`, `upgsnap_ne.c`, `upgsnap_db.c` + pure helpers in `upgsnap_fmt.c` (buffer, sanitize, sort, dbcheck-summary parse, subprocess lines). All gathering happens with stdout redirected to /dev/null (libraries such as `atch_frm` print to stdout); results are printed at the end. Shared memory is touched only when `is_cnc_up(1,0,NULL)` says netFLEX is up.

**Tech Stack:** C (gnu99, gcc 4.8.5 baseline; dev box has gcc 8.5), Lucent nmake (`cnc/tools/src/tools.mk`, `global.nmk`), netFLEX libs (`$(CORELIBS) $(UTILLIB) $(LIBCNCDB) -lnelib -linc -lgr -latm -lrdb -lpq`).

Spec: `~/WorkNotes/design/2026-09-24-netflex-upgsnap-design.md`
Research facts used below (file:line on origin/main, verified on origin/inc54.0): see the "Facts" section at the end.

**Repo / branch:** `~/Git/netflex`. Create `feat/upgsnap` from `origin/main` (the checkout is currently on an unrelated branch — do `git fetch origin && git switch -c feat/upgsnap origin/main`; stash or commit nothing of the user's current branch; if the working tree is dirty, STOP and ask). BASE and VPATH[0] must equal `/home/dan/Git/netflex` (they do today).

**Rules for every task:**
- Any edit to `tools.mk` / `install_cnc`-adjacent make logic: load the `writing-nmake-makefiles` skill first (Lucent nmake, not GNU make).
- Doxygen `/** */` on every function and struct; `upgsnap_`/`UPGSNAP_` prefix on all non-static symbols; C that compiles with gcc 4.8.5 (`-std=gnu99`): no `_Static_assert`, no `for` declarations issue (gnu99 allows them), no `__auto_type`.
- clangd LSP is not available in this session: navigate with ast-grep / grep and say so.
- Local build: the netFLEX libraries are not built on the dev box (`3b2/lib` is nearly empty), so **compile objects** and **link the test driver** locally; the full `upgsnap` link happens on a build host / CI.

---

## File map

| File | Responsibility |
|---|---|
| `include/upgsnap.h` | public types, constants, prototypes (Doxygen) |
| `cnc/tools/src/upgsnap_fmt.c` | libc-only helpers: line buffer, kv, sanitize, trim, sort, write, dbcheck summary parse, subprocess-to-lines |
| `cnc/tools/src/upgsnap_test.c` | non-installed unit test driver for `upgsnap_fmt.c` |
| `cnc/tools/src/upgsnap.c` | main: options, trace, stdout quieting, attach, section table, output, exit code |
| `cnc/tools/src/upgsnap_sys.c` | `system` section |
| `cnc/tools/src/upgsnap_rdb.c` | `rdb` section |
| `cnc/tools/src/upgsnap_ne.c` | `nes`, `equipment`, `ne_links` sections (sole includer of `<dtype.ext>`) |
| `cnc/tools/src/upgsnap_db.c` | `alarms`, `dbcheck`, `db_tie/alias/opr/opr_notify` sections |
| `cnc/tools/src/tools.mk` | `.ALL_LINUX` entries + rules for `upgsnap` and `upgsnap_test` |
| `3b2/shell/install_cnc` | `bin="$bin upgsnap"` |
| `3b2/data/upgsnap.md` | user doc (sections, keys, exit codes) |

---

### Task 1: Header, formatting helpers, unit test driver, build rules

**Files:** Create `include/upgsnap.h`, `cnc/tools/src/upgsnap_fmt.c`, `cnc/tools/src/upgsnap_test.c`; Modify `cnc/tools/src/tools.mk`.

- [ ] **Step 1: Branch** — `cd ~/Git/netflex && git status --short` (must be clean; if not, STOP and ask) `&& git fetch origin && git switch -c feat/upgsnap origin/main`. Verify `echo $BASE; echo ${VPATH%%:*}` both print `/home/dan/Git/netflex`.

- [ ] **Step 2: Header** — create `include/upgsnap.h`:

```c
/**
 * @file upgsnap.h
 * @brief upgsnap: read-only, diff-friendly snapshot of a netFLEX host for
 *        before/after upgrade comparison.
 *
 * Each section prints "key: value" lines. Gatherers append to an
 * upgsnap_buf_t; main() prints the buffers once all gathering is done, so
 * library chatter on stdout never mixes with the snapshot.
 */
#ifndef UPGSNAP_H
#define UPGSNAP_H

#include <stdio.h>
#include <stddef.h>

#define UPGSNAP_OK        0     /**< Section gathered completely. */
#define UPGSNAP_PARTIAL   1     /**< Section printed, but something could not be read. */
#define UPGSNAP_USAGE     2     /**< Bad command line. */
#define UPGSNAP_VALUE_MAX 1024  /**< Longest value kept on one line (longer is truncated). */

/**
 * @brief Growable list of output lines for one section.
 */
typedef struct {
    char   **lines;  /**< Heap strings, one per output line (no trailing newline). */
    size_t   count;  /**< Number of lines in use. */
    size_t   cap;    /**< Allocated slots. */
} upgsnap_buf_t;

/**
 * @brief What main() managed to attach before gathering.
 */
typedef struct {
    int app_up;   /**< is_cnc_up() said netFLEX is running. */
    int frm_ok;   /**< frame_link attached and c-tree initialised. */
} upgsnap_env_t;

/**
 * @brief A section gatherer: appends lines to @p out.
 *
 * @param out  Buffer to append to.
 * @param env  Attach state from main().
 * @return UPGSNAP_OK or UPGSNAP_PARTIAL.
 */
typedef int (*upgsnap_gather_fn)(upgsnap_buf_t *out, const upgsnap_env_t *env);

/**
 * @brief One entry of the section table.
 */
typedef struct {
    const char        *name;    /**< Section name used by --section. */
    upgsnap_gather_fn  gather;  /**< Gatherer. */
    int                sort;    /**< Non-zero: sort lines before printing. */
} upgsnap_section_t;

/* ---- upgsnap_fmt.c (libc only) ---- */

/** @brief Initialise an empty buffer. @param b Buffer. */
void upgsnap_buf_init(upgsnap_buf_t *b);

/** @brief Free all lines and reset the buffer. @param b Buffer. */
void upgsnap_buf_free(upgsnap_buf_t *b);

/**
 * @brief Append "key: value", value printf-formatted, then sanitized.
 * @param b    Buffer.
 * @param key  Key text (sanitized too).
 * @param fmt  printf format for the value.
 * @return 0 on success, -1 on allocation failure.
 */
int upgsnap_kv(upgsnap_buf_t *b, const char *key, const char *fmt, ...)
    __attribute__((format(printf, 3, 4)));

/**
 * @brief Append one pre-formatted line (sanitized).
 * @param b     Buffer.
 * @param text  Line text.
 * @return 0 on success, -1 on allocation failure.
 */
int upgsnap_line(upgsnap_buf_t *b, const char *text);

/**
 * @brief Make @p s safe for one output line: tabs/CR/LF become spaces, other
 *        non-printable bytes become '?', trailing spaces are removed.
 * @param s NUL-terminated string, modified in place.
 */
void upgsnap_sanitize(char *s);

/** @brief Remove leading and trailing whitespace in place. @param s String. */
void upgsnap_trim(char *s);

/** @brief Sort lines with strcmp(). @param b Buffer. */
void upgsnap_sort(upgsnap_buf_t *b);

/**
 * @brief Write every line followed by '\n'.
 * @param b   Buffer.
 * @param fp  Destination stream.
 */
void upgsnap_write(const upgsnap_buf_t *b, FILE *fp);

/**
 * @brief Append "status: unavailable (<why>)"; a section that cannot run in
 *        the current state is not a failure.
 * @param b    Buffer.
 * @param why  Reason text.
 * @return UPGSNAP_OK.
 */
int upgsnap_unavailable(upgsnap_buf_t *b, const char *why);

/**
 * @brief Parse one line of `dbcheck -AS` output and append keys for it.
 *
 * Recognises "  Error N \t\tFOUND\t\tFIXED", "  Total\t\t\tFOUND\t\tFIXED" and
 * "db audit(s) complete, 0 errors found".
 *
 * @param b     Buffer.
 * @param line  One output line (no newline needed).
 * @return 1 if the line was a summary line, else 0.
 */
int upgsnap_dbcheck_parse(upgsnap_buf_t *b, const char *line);

/**
 * @brief Run @p cmd with popen() and append each output line (sanitized,
 *        truncated to UPGSNAP_VALUE_MAX).
 * @param b    Buffer.
 * @param cmd  Shell command.
 * @return The command's exit status, or -1 if it could not be started.
 */
int upgsnap_cmd_lines(upgsnap_buf_t *b, const char *cmd);

/* ---- gatherers ---- */

/** @brief "system" section. @param out Buffer. @param env Attach state. @return UPGSNAP_OK/PARTIAL. */
int upgsnap_sys_gather(upgsnap_buf_t *out, const upgsnap_env_t *env);
/** @brief "rdb" section. @param out Buffer. @param env Attach state. @return UPGSNAP_OK/PARTIAL. */
int upgsnap_rdb_gather(upgsnap_buf_t *out, const upgsnap_env_t *env);
/** @brief "nes" section. @param out Buffer. @param env Attach state. @return UPGSNAP_OK/PARTIAL. */
int upgsnap_nes_gather(upgsnap_buf_t *out, const upgsnap_env_t *env);
/** @brief "equipment" section. @param out Buffer. @param env Attach state. @return UPGSNAP_OK/PARTIAL. */
int upgsnap_equipment_gather(upgsnap_buf_t *out, const upgsnap_env_t *env);
/** @brief "ne_links" section. @param out Buffer. @param env Attach state. @return UPGSNAP_OK/PARTIAL. */
int upgsnap_ne_links_gather(upgsnap_buf_t *out, const upgsnap_env_t *env);
/** @brief "alarms" section. @param out Buffer. @param env Attach state. @return UPGSNAP_OK/PARTIAL. */
int upgsnap_alarms_gather(upgsnap_buf_t *out, const upgsnap_env_t *env);
/** @brief "dbcheck" section. @param out Buffer. @param env Attach state. @return UPGSNAP_OK/PARTIAL. */
int upgsnap_dbcheck_gather(upgsnap_buf_t *out, const upgsnap_env_t *env);
/** @brief "db_tie" section. @param out Buffer. @param env Attach state. @return UPGSNAP_OK/PARTIAL. */
int upgsnap_db_tie_gather(upgsnap_buf_t *out, const upgsnap_env_t *env);
/** @brief "db_alias" section. @param out Buffer. @param env Attach state. @return UPGSNAP_OK/PARTIAL. */
int upgsnap_db_alias_gather(upgsnap_buf_t *out, const upgsnap_env_t *env);
/** @brief "db_opr" section. @param out Buffer. @param env Attach state. @return UPGSNAP_OK/PARTIAL. */
int upgsnap_db_opr_gather(upgsnap_buf_t *out, const upgsnap_env_t *env);
/** @brief "db_opr_notify" section. @param out Buffer. @param env Attach state. @return UPGSNAP_OK/PARTIAL. */
int upgsnap_db_opr_notify_gather(upgsnap_buf_t *out, const upgsnap_env_t *env);

#endif /* UPGSNAP_H */
```

- [ ] **Step 3: Failing unit test driver** — create `cnc/tools/src/upgsnap_test.c`:

```c
/**
 * @file upgsnap_test.c
 * @brief Unit tests for upgsnap_fmt.c. Built by tools.mk, never installed.
 *        Run: ../../../3b2/bin/upgsnap_test  (exit 0 = all passed).
 */
#include <stdio.h>
#include <string.h>
#include <upgsnap.h>

static int Failures = 0;

/** @brief Record a failed check. @param ok Condition. @param what Description. */
static void check(int ok, const char *what)
{
    if (!ok) {
        printf("FAIL: %s\n", what);
        Failures++;
    }
}

/** @brief sanitize/trim behaviour. */
static void test_sanitize(void)
{
    char a[] = "a\tb\r\nc\001d  ";
    char t[] = "  x y \n";

    upgsnap_sanitize(a);
    check(strcmp(a, "a b  c?d") == 0, "sanitize replaces controls and trims trailing spaces");
    upgsnap_trim(t);
    check(strcmp(t, "x y") == 0, "trim both ends");
}

/** @brief kv, line, sort and write. */
static void test_buffer(void)
{
    upgsnap_buf_t b;
    char out[256];
    FILE *fp;
    size_t n;

    upgsnap_buf_init(&b);
    upgsnap_kv(&b, "ne.00010.tid", "%s", "TID\tTEN");
    upgsnap_kv(&b, "ne.00002.tid", "%d", 2);
    upgsnap_line(&b, "zz raw\nline");
    check(b.count == 3, "three lines appended");
    upgsnap_sort(&b);
    check(strcmp(b.lines[0], "ne.00002.tid: 2") == 0, "sorted first");
    check(strcmp(b.lines[1], "ne.00010.tid: TID TEN") == 0, "value sanitized");
    check(strcmp(b.lines[2], "zz raw line") == 0, "raw line sanitized");

    fp = tmpfile();
    upgsnap_write(&b, fp);
    rewind(fp);
    n = fread(out, 1, sizeof(out) - 1, fp);
    out[n] = 0;
    fclose(fp);
    check(strcmp(out, "ne.00002.tid: 2\nne.00010.tid: TID TEN\nzz raw line\n") == 0, "write");

    upgsnap_buf_free(&b);
    check(b.count == 0 && b.lines == NULL, "free resets");

    upgsnap_buf_init(&b);
    check(upgsnap_unavailable(&b, "netFLEX not running") == UPGSNAP_OK, "unavailable is OK");
    check(strcmp(b.lines[0], "status: unavailable (netFLEX not running)") == 0, "unavailable text");
    upgsnap_buf_free(&b);
}

/** @brief Long values are truncated, never split across lines. */
static void test_truncate(void)
{
    upgsnap_buf_t b;
    char big[3000];

    memset(big, 'x', sizeof(big) - 1);
    big[sizeof(big) - 1] = 0;
    upgsnap_buf_init(&b);
    upgsnap_kv(&b, "k", "%s", big);
    check(b.count == 1, "one line");
    check(strlen(b.lines[0]) == strlen("k: ") + UPGSNAP_VALUE_MAX, "truncated to UPGSNAP_VALUE_MAX");
    upgsnap_buf_free(&b);
}

/** @brief dbcheck summary parsing. */
static void test_dbcheck(void)
{
    upgsnap_buf_t b;

    upgsnap_buf_init(&b);
    check(upgsnap_dbcheck_parse(&b, "  Error 7 \t\t12\t\t0") == 1, "error row");
    check(upgsnap_dbcheck_parse(&b, "  Total\t\t\t12\t\t0") == 1, "total row");
    check(upgsnap_dbcheck_parse(&b, "    Performing xcc audit") == 0, "noise ignored");
    check(b.count == 2, "two keys");
    check(strcmp(b.lines[0], "error.007.found: 12") == 0, "error key");
    check(strcmp(b.lines[1], "total_errors: 12") == 0, "total key");
    upgsnap_buf_free(&b);

    upgsnap_buf_init(&b);
    check(upgsnap_dbcheck_parse(&b, "    db audit(s) complete, 0 errors found") == 1, "clean");
    check(strcmp(b.lines[0], "total_errors: 0") == 0, "clean total");
    upgsnap_buf_free(&b);
}

/** @brief Subprocess lines and exit status. */
static void test_cmd(void)
{
    upgsnap_buf_t b;

    upgsnap_buf_init(&b);
    check(upgsnap_cmd_lines(&b, "printf 'a\\tb\\nc\\n'") == 0, "exit 0");
    check(b.count == 2 && strcmp(b.lines[0], "a b") == 0 && strcmp(b.lines[1], "c") == 0,
          "lines captured");
    upgsnap_buf_free(&b);

    upgsnap_buf_init(&b);
    check(upgsnap_cmd_lines(&b, "exit 3") == 3, "exit status returned");
    check(b.count == 0, "no output");
    upgsnap_buf_free(&b);
}

/**
 * @brief Run all tests.
 * @return 0 if all passed, 1 otherwise.
 */
int main(void)
{
    test_sanitize();
    test_buffer();
    test_truncate();
    test_dbcheck();
    test_cmd();
    printf("%s\n", Failures ? "FAILED" : "ALL PASSED");
    return Failures ? 1 : 0;
}
```

- [ ] **Step 4: Build rules** — load the `writing-nmake-makefiles` skill, then in `cnc/tools/src/tools.mk`:
  - add `$(PBIN)/upgsnap \` and `$(PBIN)/upgsnap_test \` to the `.ALL_LINUX :` list (it starts at ~tools.mk:117; place them next to `frm_vs_rdb`, ~:366, matching the existing indentation/continuations);
  - add rules next to the `frm_vs_rdb` rule (~:1809), following that exact pattern (tab-indented recipe):

```make
$(PBIN)/upgsnap :: upgsnap.o upgsnap_fmt.o upgsnap_sys.o upgsnap_rdb.o upgsnap_ne.o upgsnap_db.o $(CORELIBS) $(UTILLIB) $(LIBCNCDB) -lnelib -linc -lgr -latm -lrdb -lpq
	$(CC) $(CFLAGS) $(LDFLAGS) -o $(PBIN)/upgsnap $(*) -lpthread

$(PBIN)/upgsnap_test :: upgsnap_test.o upgsnap_fmt.o
	$(CC) $(CFLAGS) $(LDFLAGS) -o $(PBIN)/upgsnap_test $(*)
```
  (`upgsnap_test` is deliberately not added to install_cnc; `frmlnk_rdb_test` is the precedent.)

- [ ] **Step 5: Run the test to see it fail** — `cd ~/Git/netflex/cnc/tools/src && nmake -f tools.mk ../../../3b2/bin/upgsnap_test` → expected: link/compile failure (upgsnap_fmt.c missing). Use the target spelling the nmake skill confirms if this one is rejected.

- [ ] **Step 6: Implement** `cnc/tools/src/upgsnap_fmt.c`:

```c
/**
 * @file upgsnap_fmt.c
 * @brief libc-only helpers for upgsnap: line buffer, key/value formatting,
 *        sanitizing, sorting, dbcheck summary parsing and subprocess capture.
 *        Kept free of netFLEX headers so upgsnap_test links without them.
 */
#include <ctype.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/wait.h>
#include <upgsnap.h>

void upgsnap_buf_init(upgsnap_buf_t *b)
{
    b->lines = NULL;
    b->count = 0;
    b->cap = 0;
}

void upgsnap_buf_free(upgsnap_buf_t *b)
{
    size_t i;

    for (i = 0; i < b->count; i++)
        free(b->lines[i]);
    free(b->lines);
    upgsnap_buf_init(b);
}

/**
 * @brief Take ownership of @p s and append it.
 * @param b Buffer.
 * @param s Heap string.
 * @return 0, or -1 (and @p s freed) on allocation failure.
 */
static int push(upgsnap_buf_t *b, char *s)
{
    if (b->count == b->cap) {
        size_t ncap = b->cap ? b->cap * 2 : 32;
        char **n = realloc(b->lines, ncap * sizeof(char *));

        if (n == NULL) {
            free(s);
            return -1;
        }
        b->lines = n;
        b->cap = ncap;
    }
    b->lines[b->count++] = s;
    return 0;
}

void upgsnap_sanitize(char *s)
{
    char *p;
    size_t n;

    for (p = s; *p; p++) {
        unsigned char c = (unsigned char)*p;

        if (c == '\t' || c == '\r' || c == '\n')
            *p = ' ';
        else if (c < 0x20 || c == 0x7f)
            *p = '?';
    }
    n = strlen(s);
    while (n > 0 && s[n - 1] == ' ')
        s[--n] = 0;
}

void upgsnap_trim(char *s)
{
    size_t n = strlen(s);
    size_t i = 0;

    while (n > 0 && isspace((unsigned char)s[n - 1]))
        s[--n] = 0;
    while (s[i] && isspace((unsigned char)s[i]))
        i++;
    if (i)
        memmove(s, s + i, n - i + 1);
}

int upgsnap_kv(upgsnap_buf_t *b, const char *key, const char *fmt, ...)
{
    char val[UPGSNAP_VALUE_MAX + 1];
    char *line;
    size_t klen;
    va_list ap;

    va_start(ap, fmt);
    vsnprintf(val, sizeof(val), fmt, ap);   /* truncates at UPGSNAP_VALUE_MAX */
    va_end(ap);
    upgsnap_sanitize(val);

    klen = strlen(key);
    line = malloc(klen + 2 + strlen(val) + 1);
    if (line == NULL)
        return -1;
    sprintf(line, "%s: %s", key, val);
    upgsnap_sanitize(line);
    return push(b, line);
}

int upgsnap_line(upgsnap_buf_t *b, const char *text)
{
    char *line = strdup(text);

    if (line == NULL)
        return -1;
    if (strlen(line) > UPGSNAP_VALUE_MAX)
        line[UPGSNAP_VALUE_MAX] = 0;
    upgsnap_sanitize(line);
    return push(b, line);
}

/** @brief qsort comparator on line strings. */
static int cmp_lines(const void *a, const void *b)
{
    return strcmp(*(char * const *)a, *(char * const *)b);
}

void upgsnap_sort(upgsnap_buf_t *b)
{
    if (b->count > 1)
        qsort(b->lines, b->count, sizeof(char *), cmp_lines);
}

void upgsnap_write(const upgsnap_buf_t *b, FILE *fp)
{
    size_t i;

    for (i = 0; i < b->count; i++)
        fprintf(fp, "%s\n", b->lines[i]);
}

int upgsnap_unavailable(upgsnap_buf_t *b, const char *why)
{
    upgsnap_kv(b, "status", "unavailable (%s)", why);
    return UPGSNAP_OK;
}

int upgsnap_dbcheck_parse(upgsnap_buf_t *b, const char *line)
{
    int err, found, fixed;
    char key[32];

    /* sscanf whitespace matches the tabs dbcheck prints */
    if (sscanf(line, " Error %d %d %d", &err, &found, &fixed) == 3) {
        snprintf(key, sizeof(key), "error.%03d.found", err);
        upgsnap_kv(b, key, "%d", found);
        return 1;
    }
    if (sscanf(line, " Total %d %d", &found, &fixed) == 2) {
        upgsnap_kv(b, "total_errors", "%d", found);
        return 1;
    }
    if (strstr(line, "db audit(s) complete, 0 errors found") != NULL) {
        upgsnap_kv(b, "total_errors", "%d", 0);
        return 1;
    }
    return 0;
}

int upgsnap_cmd_lines(upgsnap_buf_t *b, const char *cmd)
{
    char buf[UPGSNAP_VALUE_MAX + 2];
    FILE *p = popen(cmd, "r");
    int st, truncated = 0;

    if (p == NULL)
        return -1;
    while (fgets(buf, sizeof(buf), p) != NULL) {
        size_t n = strlen(buf);
        int complete = (n > 0 && buf[n - 1] == '\n');

        if (!truncated)
            upgsnap_line(b, buf);
        /* the rest of an over-long line is discarded, not emitted as a new line */
        truncated = !complete;
    }
    st = pclose(p);
    if (st == -1)
        return -1;
    return WIFEXITED(st) ? WEXITSTATUS(st) : -1;
}
```
  (`upgsnap_line` strips the trailing newline via sanitize → trailing space trimmed.)

- [ ] **Step 7: Run the test** — rebuild `upgsnap_test` and run `../../../3b2/bin/upgsnap_test` → `ALL PASSED`, exit 0. Also compile with the baseline dialect check: `gcc -std=gnu99 -Wall -Wextra -fsyntax-only -I../../../include upgsnap_fmt.c upgsnap_test.c` → no warnings.

- [ ] **Step 8: Commit** — `git add include/upgsnap.h cnc/tools/src/upgsnap_fmt.c cnc/tools/src/upgsnap_test.c cnc/tools/src/tools.mk && git commit -m "upgsnap: formatting helpers, unit test driver, build rules"` (+ Co-Authored-By trailer). The `upgsnap` target cannot link yet; that's expected until Task 5.

---

### Task 2: main + `system` section

**Files:** Create `cnc/tools/src/upgsnap.c`, `cnc/tools/src/upgsnap_sys.c`.

- [ ] **Step 1: `upgsnap.c`**

```c
/**
 * @file upgsnap.c
 * @brief upgsnap entry point: options, attach, section table, output.
 *
 * Usage: upgsnap [--list] [--section NAME]   (-h/--help handled by getopts)
 * Read-only. Gathering runs with stdout redirected to /dev/null because some
 * netFLEX library calls (e.g. atch_frm) print to stdout; the snapshot is
 * printed afterwards.
 */
#include <errno.h>
#include <fcntl.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <guidefs.h>
#include <dcompool.h>
#include <d1compool.h>
#include <dshm_data.h>
#include <dcompool.ext>
#include <version.h>
#include <getopts.h>
#include <trace.h>
#include <utilmisc.h>
#include <utillibinc.h>
#include <com_db.h>
#include <upgsnap.h>

static volatile char Csccs[] = OVINC_VERSION;
FILE *Tptr;
extern int Trace, DebugLvl;
extern unsigned int ShmAlloc;

/** @brief All sections, in display order. */
static const upgsnap_section_t Sections[] = {
    { "system",        upgsnap_sys_gather,           1 },
    { "rdb",           upgsnap_rdb_gather,           1 },
    { "nes",           upgsnap_nes_gather,           1 },
    { "ne_links",      upgsnap_ne_links_gather,      1 },
    { "equipment",     upgsnap_equipment_gather,     1 },
    { "alarms",        upgsnap_alarms_gather,        1 },
    { "dbcheck",       upgsnap_dbcheck_gather,       1 },
    { "db_tie",        upgsnap_db_tie_gather,        0 },   /* inc_db export order is stable */
    { "db_alias",      upgsnap_db_alias_gather,      0 },
    { "db_opr",        upgsnap_db_opr_gather,        0 },
    { "db_opr_notify", upgsnap_db_opr_notify_gather, 0 },
    { NULL, NULL, 0 }
};

/**
 * @brief Point stdout at /dev/null; library code may print there.
 * @return Saved stdout descriptor (restore with restore_stdout), or -1.
 */
static int quiet_stdout(void)
{
    int saved, dn;

    fflush(stdout);
    saved = dup(STDOUT_FILENO);
    dn = open("/dev/null", O_WRONLY);
    if (dn >= 0) {
        dup2(dn, STDOUT_FILENO);
        close(dn);
    }
    return saved;
}

/** @brief Undo quiet_stdout(). @param saved Descriptor it returned. */
static void restore_stdout(int saved)
{
    fflush(stdout);
    if (saved >= 0) {
        dup2(saved, STDOUT_FILENO);
        close(saved);
    }
}

/**
 * @brief Attach netFLEX shared memory and c-tree read-only, only if netFLEX is up.
 * @param env Filled in with what succeeded.
 */
static void attach(upgsnap_env_t *env)
{
    char *start = NULL;

    env->app_up = is_cnc_up(1, 0, NULL);
    env->frm_ok = 0;
    if (!env->app_up)
        return;
    if (set_tunables() == FAILURE)
        return;
    ShmAlloc = (DP_ALLOC | FRM_ALLOC | UNIT_ALLOC | COMMON_ALLOC | PM_ALLOC | AAL_ALLOC);
    if (ddb_init(0) == FAILURE)
        return;
    access_db();
    Frmlnk = atch_frm(&start);
    if (Frmlnk == (struct frame_link *)FAILURE) {
        Frmlnk = NULL;
        return;
    }
    ct_init(0, 0, 0);
    env->frm_ok = 1;
}

/** @brief Find a section by name. @param name Name. @return Entry or NULL. */
static const upgsnap_section_t *find_section(const char *name)
{
    const upgsnap_section_t *s;

    for (s = Sections; s->name; s++)
        if (strcmp(s->name, name) == 0)
            return s;
    return NULL;
}

/**
 * @brief Entry point.
 * @param argc Argument count.
 * @param argv Arguments.
 * @return UPGSNAP_OK, UPGSNAP_PARTIAL or UPGSNAP_USAGE.
 */
int main(int argc, char **argv)
{
    enum { OP_LIST = 1, OP_SECTION };
    struct options opts[] = {
        { OP_LIST,    0, "l", "list",    "List section names" },
        { OP_SECTION, 1, "s", "section", "Print only section NAME" },
        { 0, 0, NULL, NULL, NULL }
    };
    const upgsnap_section_t *only = NULL, *s;
    upgsnap_buf_t bufs[sizeof(Sections) / sizeof(Sections[0])];
    int results[sizeof(Sections) / sizeof(Sections[0])];
    upgsnap_env_t env;
    char *args = NULL;
    int opt, saved, i, rc = UPGSNAP_OK;

    (void)Csccs;
    while ((opt = getopts(argc, argv, opts, &args)) != 0) {
        switch (opt) {
        case OP_LIST:
            for (s = Sections; s->name; s++)
                printf("%s\n", s->name);
            return UPGSNAP_OK;
        case OP_SECTION:
            only = find_section(args ? args : "");
            if (only == NULL) {
                fprintf(stderr, "upgsnap: unknown section '%s' (see --list)\n", args ? args : "");
                free(args);
                return UPGSNAP_USAGE;
            }
            break;
        case -2:
            fprintf(stderr, "upgsnap: unknown option %s\n", args ? args : "");
            free(args);
            getopts_usage(argv[0], opts);
            return UPGSNAP_USAGE;
        default:
            fprintf(stderr, "upgsnap: option parsing failed\n");
            free(args);
            return UPGSNAP_USAGE;
        }
        free(args);
        args = NULL;
    }

    openTraceFile("/usr/cnc/trace/upgsnap", "a");
    setbuf(Tptr, NULL);
    debug_lvl("upgsnap", Tptr);
    Trace = DebugLvl;

    saved = quiet_stdout();
    attach(&env);
    for (i = 0, s = Sections; s->name; s++, i++) {
        upgsnap_buf_init(&bufs[i]);
        results[i] = UPGSNAP_OK;
        if (only && only != s)
            continue;
        results[i] = s->gather(&bufs[i], &env);
        if (s->sort)
            upgsnap_sort(&bufs[i]);
    }
    restore_stdout(saved);

    for (i = 0, s = Sections; s->name; s++, i++) {
        if (only && only != s)
            continue;
        if (!only)
            printf("== %s ==\n", s->name);
        upgsnap_write(&bufs[i], stdout);
        if (!only)
            printf("\n");
        if (results[i] != UPGSNAP_OK)
            rc = UPGSNAP_PARTIAL;
        upgsnap_buf_free(&bufs[i]);
    }
    return rc;
}
```

- [ ] **Step 2: `upgsnap_sys.c`**

```c
/**
 * @file upgsnap_sys.c
 * @brief "system" section: host, OS, netFLEX version, RPMs, machine role,
 *        web server, app state and disk mount sizes.
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <strings.h>
#include <unistd.h>
#include <sys/stat.h>
#include <sys/statvfs.h>
#include <feat_path.h>
#include <utillibinc.h>
#include <upgsnap.h>

/** @brief Mounts reported by size (usage changes constantly and is not reported). */
static const char *Mounts[] = { "/", "/tmp", "/usr2", "/usr4", "/var", NULL };

/** @brief Web servers probed, in incinfo's order plus the RHEL system httpd. */
static const char *WebServers[] = {
    "/opt/hpws/apache/bin/httpd", "/opt/apache/bin/httpd", "/usr/apache2/bin/httpd",
    "/usr/sbin/httpd", NULL
};

/**
 * @brief Run @p cmd and keep the first output line, trimmed.
 * @param cmd  Shell command.
 * @param out  Result buffer.
 * @param size Size of @p out.
 */
static void first_line(const char *cmd, char *out, int size)
{
    char *nl;

    if (getcmdoutput((char *)cmd, out, size) < 0)
        out[0] = 0;
    if ((nl = strchr(out, '\n')) != NULL)
        *nl = 0;
    upgsnap_trim(out);
}

/**
 * @brief Installed version of an RPM, or "not installed".
 * @param b   Buffer.
 * @param key Output key.
 * @param pkg Package name.
 */
static void rpm_version(upgsnap_buf_t *b, const char *key, const char *pkg)
{
    char cmd[256], out[256];

    snprintf(cmd, sizeof(cmd),
             "rpm -q --qf '%%{VERSION}-%%{RELEASE}' %s 2>/dev/null", pkg);
    first_line(cmd, out, sizeof(out));
    upgsnap_kv(b, key, "%s",
               (out[0] == 0 || strncmp(out, "package ", 8) == 0) ? "not installed" : out);
}

/**
 * @brief netFLEX version from /usr/cnc/data/version.h ("SOFTWARE VERSION: X"),
 *        skipping the TEMPLATE line that precedes the real define.
 * @param out  Result ("54.1.16"), empty if unknown.
 * @param size Size of @p out.
 */
static void netflex_version(char *out, size_t size)
{
    FILE *fp = fopen("/usr/cnc/data/version.h", "r");
    char line[512], *p;

    out[0] = 0;
    if (fp == NULL)
        return;
    while (fgets(line, sizeof(line), fp) != NULL) {
        if (strstr(line, "OVINC_VERSION") == NULL || strstr(line, "#define") == NULL)
            continue;
        if ((p = strstr(line, "SOFTWARE VERSION: ")) != NULL) {
            p += strlen("SOFTWARE VERSION: ");
            snprintf(out, size, "%s", p);
            p = strpbrk(out, " \"\n");
            if (p)
                *p = 0;
        }
    }
    fclose(fp);
}

/**
 * @brief PRETTY_NAME from /etc/os-release.
 * @param out  Result, "unknown" if absent.
 * @param size Size of @p out.
 */
static void os_release(char *out, size_t size)
{
    FILE *fp = fopen("/etc/os-release", "r");
    char line[256];

    snprintf(out, size, "unknown");
    if (fp == NULL)
        return;
    while (fgets(line, sizeof(line), fp) != NULL) {
        if (strncmp(line, "PRETTY_NAME=", 12) == 0) {
            char *v = line + 12;

            upgsnap_trim(v);
            if (*v == '"') {
                v++;
                if (strlen(v) && v[strlen(v) - 1] == '"')
                    v[strlen(v) - 1] = 0;
            }
            snprintf(out, size, "%s", v);
            break;
        }
    }
    fclose(fp);
}

/**
 * @brief Machine role from CONFIG_FILE (cnc.cnfg), as incinfo reads it:
 *        "single" for a single box, else FEP/BEP for this host's row.
 * @param host This host name.
 * @return Static string.
 */
static const char *machine_role(const char *host)
{
    FILE *fp = fopen(CONFIG_FILE, "r");
    char line[512], mmach[128], smach[128];
    int mach_num, mate_num, machtype, over_link, multi = -1;
    const char *role = "unknown";

    if (fp == NULL)
        return role;
    while (fgets(line, sizeof(line), fp) != NULL) {
        if (line[0] == '#') {
            if (multi < 0 && strncasecmp(line, "# Single", 8) == 0)
                multi = 0;
            else if (multi < 0 && strncasecmp(line, "# Multi", 7) == 0)
                multi = 1;
            continue;
        }
        if (multi != 1)
            continue;
        if (sscanf(line, "%127s%d%127s%d%d%d", mmach, &mach_num, smach, &mate_num,
                   &machtype, &over_link) == 6 && strcasecmp(mmach, host) == 0) {
            role = machtype ? "FEP" : "BEP";
            break;
        }
    }
    fclose(fp);
    return multi == 0 ? "single" : role;
}

/**
 * @brief Web server version ("Apache/2.4.37"), or "not found".
 * @param out  Result.
 * @param size Size of @p out.
 */
static void web_server(char *out, int size)
{
    char cmd[300], *p;
    int i;

    snprintf(out, size, "not found");
    for (i = 0; WebServers[i]; i++) {
        if (access(WebServers[i], X_OK) != 0)
            continue;
        snprintf(cmd, sizeof(cmd), "%s -v 2>/dev/null | grep 'version:'", WebServers[i]);
        first_line(cmd, out, size);
        if ((p = strstr(out, "version: ")) != NULL) {
            memmove(out, p + 9, strlen(p + 9) + 1);
            if ((p = strchr(out, ' ')) != NULL)
                *p = 0;
        } else {
            snprintf(out, size, "unknown (%s)", WebServers[i]);
        }
        return;
    }
}

/**
 * @brief True if @p path is a mount point (its device differs from its parent's).
 * @param path Directory.
 * @return 1 or 0.
 */
static int is_mount(const char *path)
{
    struct stat st, pst;
    char parent[512];

    if (strcmp(path, "/") == 0)
        return 1;
    if (stat(path, &st) != 0 || !S_ISDIR(st.st_mode))
        return 0;
    snprintf(parent, sizeof(parent), "%s/..", path);
    if (stat(parent, &pst) != 0)
        return 0;
    return st.st_dev != pst.st_dev;
}

int upgsnap_sys_gather(upgsnap_buf_t *out, const upgsnap_env_t *env)
{
    char host[256], buf[512], key[64];
    struct statvfs vfs;
    int i;

    if (gethostname(host, sizeof(host)) != 0)
        snprintf(host, sizeof(host), "unknown");
    host[sizeof(host) - 1] = 0;
    upgsnap_kv(out, "host", "%s", host);

    os_release(buf, sizeof(buf));
    upgsnap_kv(out, "os", "%s", buf);

    netflex_version(buf, sizeof(buf));
    upgsnap_kv(out, "netflex.version", "%s", buf[0] ? buf : "unknown");

    rpm_version(out, "rpm.core", "netFLEX-CORE");
    rpm_version(out, "rpm.patch", "netFLEX-CORE-PATCH");
    rpm_version(out, "rpm.ipatch", "netFLEX-CORE-IPATCH");

    upgsnap_kv(out, "machine.role", "%s", machine_role(host));

    web_server(buf, sizeof(buf));
    upgsnap_kv(out, "httpd", "%s", buf);

    upgsnap_kv(out, "app_up", "%s", env->app_up ? "yes" : "no");

    for (i = 0; Mounts[i]; i++) {
        if (!is_mount(Mounts[i]) || statvfs(Mounts[i], &vfs) != 0)
            continue;
        snprintf(key, sizeof(key), "disk.%s.size_kb", Mounts[i]);
        upgsnap_kv(out, key, "%llu",
                   (unsigned long long)vfs.f_blocks * vfs.f_frsize / 1024ULL);
    }
    return UPGSNAP_OK;
}
```

- [ ] **Step 3: Compile objects locally** — `cd cnc/tools/src && nmake -f tools.mk upgsnap.o upgsnap_sys.o` (or the object-target spelling the nmake skill confirms). Fix any header/type issues the real headers reveal (e.g. an include that pulls `bool`: add `#include <stdbool.h>` before netFLEX headers; `FAILURE`/`SUCCESS` definitions — find their header with grep if not pulled in). Expected: both `.o` build with no warnings under `-Wall`.

- [ ] **Step 4: Commit** — `git add cnc/tools/src/upgsnap.c cnc/tools/src/upgsnap_sys.c && git commit -m "upgsnap: main, section table, system section"`.

---

### Task 3: `rdb` section

**Files:** Create `cnc/tools/src/upgsnap_rdb.c`.

- [ ] **Step 1: Implement**

```c
/**
 * @file upgsnap_rdb.c
 * @brief "rdb" section: server location, direct/PgBouncer connectivity, data
 *        present, schema version current vs system. Mirrors `rdb test` and
 *        `rdb version` (cnc/rdb/src/rdb.c) without their side effects.
 */
#include <stdio.h>
#include <string.h>
#include <unistd.h>
#include <libpq-fe.h>
#include <rdb.h>
#include <rdb_util.h>
#include <rdb_inc_versions.h>
#include <rdb_otn_port.h>
#include <upgsnap.h>

int upgsnap_rdb_gather(upgsnap_buf_t *out, const upgsnap_env_t *env)
{
    RdbUpgradeEntry sys;
    DBCONN *con;
    BOOLEAN bouncer;
    char *cur;
    int direct_ok = 0, bouncer_ok = 0, data_ok;

    (void)env;
    upgsnap_kv(out, "location", "%s", get_rdb_server_location());
    if (access("/usr/bin/psql", F_OK) != 0) {
        upgsnap_kv(out, "status", "%s", "psql not installed");
        return UPGSNAP_PARTIAL;
    }

    con = rdb_connect_credentials(RDB_DIRECT_HOST, RDB_USERNAME, RDB_PASSWORD, "postgres",
                                  RDB_DIRECT_PORT);
    if (con) {
        direct_ok = 1;
        rdb_close(__func__, con, TRUE, FALSE, NULL);
    }
    upgsnap_kv(out, "direct_connect", "%s", direct_ok ? "ok" : "fail");

    bouncer = rdb_pgbouncer_enabled();
    upgsnap_kv(out, "pgbouncer.enabled", "%s", bouncer ? "yes" : "no");
    if (bouncer) {
        con = rdb_connect_db(__func__, NULL, RDB_OTNPORT_DB, NULL);
        if (con) {
            bouncer_ok = 1;
            rdb_close(__func__, con, TRUE, FALSE, NULL);
        }
        upgsnap_kv(out, "pgbouncer.connect", "%s", bouncer_ok ? "ok" : "fail");
    }

    data_ok = bouncer ? bouncer_ok : direct_ok;
    upgsnap_kv(out, "has_data", "%s",
               data_ok ? (rdb_has_data() ? "yes" : "no") : "unknown (no connection)");

    memset(&sys, 0, sizeof(sys));
    if (rdb_version_get_sys(&sys) == SUCCESS)
        upgsnap_kv(out, "version.system", "%s", sys.version);
    else
        upgsnap_kv(out, "version.system", "%s", "unknown");

    cur = data_ok ? rdb_current_version(NULL) : NULL;
    upgsnap_kv(out, "version.current", "%s", (cur && *cur) ? cur : "unknown");
    upgsnap_kv(out, "version.match", "%s",
               (cur && *cur && sys.version[0] && rdb_version_compare(sys.version, cur) == 0)
                   ? "yes" : "no");
    if (cur)
        rdb_free(cur);

    return data_ok ? UPGSNAP_OK : UPGSNAP_PARTIAL;
}
```

- [ ] **Step 2: Compile** `upgsnap_rdb.o` locally; resolve header issues (e.g. `SUCCESS` location, `TRUE/FALSE` from rdb.h). No warnings.

- [ ] **Step 3: Commit** — `git commit -m "upgsnap: rdb section"`.

---

### Task 4: `nes`, `equipment`, `ne_links` sections

**Files:** Create `cnc/tools/src/upgsnap_ne.c`.

- [ ] **Step 1: Implement**

```c
/**
 * @file upgsnap_ne.c
 * @brief NE sections from frame_link and the es64_snmp_info table:
 *        "nes" (link state and totals), "equipment" (stable config fields),
 *        "ne_links" (SNMP/CLI/NETCONF per NE.slot). Never prints passwords,
 *        community strings, counters, times or pids.
 *
 * Only this file includes <dtype.ext> (it defines the dtypes[] table).
 */
#include <stdio.h>
#include <string.h>
#include <guidefs.h>
#include <dcompool.h>
#include <d1compool.h>
#include <dshm_data.h>
#include <dtype.ext>
#include <dcompool.ext>
#include <msgscreen.h>
#include <link_status.h>
#include <fv_com.h>
#include <alcatel_es64_db.h>
#include <misc_util.h>
#include <utillibinc.h>
#include <upgsnap.h>

extern int NeAvail;
extern int ES64_SnmpInfo_DbState;

/** @brief Name for a dtype index, "?" when out of range. @param t Index. @return Name. */
static const char *dtype_name(int t)
{
    size_t n = sizeof(dtypes) / sizeof(dtypes[0]);

    return (t >= 0 && (size_t)t < n && dtypes[t]) ? dtypes[t] : "?";
}

/** @brief An NE slot in use (site-wide, all FEPs). @param f Entry. @return 1/0. */
static int ne_real(const struct frame_link *f)
{
    return !NE_UNASSIGNED(f, ALL_FEP) && f->dcs_type[0] > 0;
}

/**
 * @brief "disabled", "up" (either side for protected NEs) or "down".
 * @param f    Entry.
 * @param neid NE id.
 * @return Static string.
 */
static const char *link_state(struct frame_link *f, int neid)
{
    int up;

    if (IS_DISABLED(f->disable) || IS_DISABLED(f->auto_disable))
        return "disabled";
    up = get_link_status(f, neid, 0, NO_LINK_OPTION) == NE_LINK_UP;
    if (!up && f->protectbit)
        up = get_link_status(f, neid, 1, NO_LINK_OPTION) == NE_LINK_UP;
    return up ? "up" : "down";
}

/**
 * @brief NE release text (release[] need not be NUL-terminated).
 * @param f    Entry.
 * @param out  Result.
 * @param size Size of @p out.
 */
static void ne_release(const struct frame_link *f, char *out, size_t size)
{
    snprintf(out, size, "%.*s", (int)sizeof(f->release), f->release);
}

/** @brief Append "ne.<id>.<field>: value". */
static void ne_kv(upgsnap_buf_t *b, int neid, const char *field, const char *fmt, const char *v)
{
    char key[64];

    snprintf(key, sizeof(key), "ne.%05d.%s", neid, field);
    upgsnap_kv(b, key, fmt, v);
}

/** @brief Append an integer "ne.<id>.<field>: n". */
static void ne_int(upgsnap_buf_t *b, int neid, const char *field, long v)
{
    char key[64];

    snprintf(key, sizeof(key), "ne.%05d.%s", neid, field);
    upgsnap_kv(b, key, "%ld", v);
}

int upgsnap_nes_gather(upgsnap_buf_t *out, const upgsnap_env_t *env)
{
    int neid, up = 0, down = 0, dis = 0, total = 0, gne_up = 0, gne_total = 0;
    char rel[32];

    if (!env->frm_ok)
        return upgsnap_unavailable(out, env->app_up ? "frame_link not attached"
                                                    : "netFLEX not running");
    for (neid = 1; neid < NeAvail; neid++) {
        struct frame_link *f = &Frmlnk[neid];
        const char *st;
        int type;

        if (!ne_real(f))
            continue;
        type = f->dcs_type[0];
        st = link_state(f, neid);
        total++;
        if (strcmp(st, "up") == 0)
            up++;
        else if (strcmp(st, "down") == 0)
            down++;
        else
            dis++;
        if (HAS_COMM_GRP(type) && f->sagelnk[0] >= 0) {
            gne_total++;
            if (strcmp(st, "up") == 0)
                gne_up++;
        }
        ne_kv(out, neid, "tid", "%s", f->sys_name);
        ne_kv(out, neid, "dtype", "%s", dtype_name(type));
        ne_kv(out, neid, "link", "%s", st);
        ne_release(f, rel, sizeof(rel));
        ne_kv(out, neid, "release", "%s", rel);
    }
    upgsnap_kv(out, "total.nes", "%d", total);
    upgsnap_kv(out, "total.up", "%d", up);
    upgsnap_kv(out, "total.down", "%d", down);
    upgsnap_kv(out, "total.disabled", "%d", dis);
    upgsnap_kv(out, "total.gne", "%d", gne_total);
    upgsnap_kv(out, "total.gne_up", "%d", gne_up);
    return UPGSNAP_OK;
}

int upgsnap_equipment_gather(upgsnap_buf_t *out, const upgsnap_env_t *env)
{
    int neid, s;

    if (!env->frm_ok)
        return upgsnap_unavailable(out, env->app_up ? "frame_link not attached"
                                                    : "netFLEX not running");
    for (neid = 1; neid < NeAvail; neid++) {
        struct frame_link *f = &Frmlnk[neid];
        char rel[32], v[64], field[32];

        if (!ne_real(f))
            continue;
        ne_kv(out, neid, "tid", "%s", f->sys_name);
        ne_kv(out, neid, "dtype", "%s", dtype_name(f->dcs_type[0]));
        ne_int(out, neid, "sub_dtype", f->sub_dtype);
        ne_release(f, rel, sizeof(rel));
        ne_kv(out, neid, "release", "%s", rel);
        ne_int(out, neid, "nfrmnum", f->nfrmnum);
        ne_int(out, neid, "fepid", f->fepid);
        ne_int(out, neid, "sagelnk0", f->sagelnk[0]);
        ne_int(out, neid, "sagelnk1", f->sagelnk[1]);
        ne_int(out, neid, "protectbit", f->protectbit);
        ne_int(out, neid, "dual_gne", f->dual_gne);
        ne_int(out, neid, "multi_gne", f->multi_gne);
        ne_kv(out, neid, "login", "%s", f->login);
        ne_kv(out, neid, "def_owner", "%s", f->def_owner_str);
        ne_kv(out, neid, "dl_aid", "%s", f->dl_aid);
        snprintf(v, sizeof(v), "%.*s/%.*s", (int)sizeof(f->baud[0]), f->baud[0],
                 (int)sizeof(f->baud[1]), f->baud[1]);
        ne_kv(out, neid, "baud", "%s", v);
        ne_kv(out, neid, "access", "%s", f->ssh ? "ssh" : (f->telnet ? "telnet" : "raw"));
        ne_int(out, neid, "timezone", f->timezone);
        ne_int(out, neid, "pm_enable", f->pm_enable);
        ne_int(out, neid, "surveil_only", f->surveil_only);
        ne_int(out, neid, "cli_disable", f->cli_disable);
        ne_int(out, neid, "ext_acc", f->ext_acc);
        ne_int(out, neid, "auto_mon", f->auto_mon);
        ne_int(out, neid, "quote_tid", f->quote_tid);
        ne_int(out, neid, "tod_enable", f->tod_enable);
        ne_int(out, neid, "xcon_match_enabled", f->xcon_match_enabled);
        for (s = 1; s <= 6; s++) {
            int slot = 0, cli = 0, snmp = 0, en = 0;

            switch (s) {
            case 1: slot = f->link_slot_1; cli = f->link_cli_1; snmp = f->link_snmp_1; en = f->link_enable_1; break;
            case 2: slot = f->link_slot_2; cli = f->link_cli_2; snmp = f->link_snmp_2; en = f->link_enable_2; break;
            case 3: slot = f->link_slot_3; cli = f->link_cli_3; snmp = f->link_snmp_3; en = f->link_enable_3; break;
            case 4: slot = f->link_slot_4; cli = f->link_cli_4; snmp = f->link_snmp_4; en = f->link_enable_4; break;
            case 5: slot = f->link_slot_5; cli = f->link_cli_5; snmp = f->link_snmp_5; en = f->link_enable_5; break;
            case 6: slot = f->link_slot_6; cli = f->link_cli_6; snmp = f->link_snmp_6; en = f->link_enable_6; break;
            }
            if (slot == 0 && !cli && !snmp && !en)
                continue;
            snprintf(field, sizeof(field), "link%d", s);
            snprintf(v, sizeof(v), "slot=%d cli=%d snmp=%d enable=%d", slot, cli, snmp, en);
            ne_kv(out, neid, field, "%s", v);
        }
    }
    return UPGSNAP_OK;
}

/** @brief "snmp", "cli", "netconf" or "type<n>". @param t Record type. @param buf Scratch. @return Name. */
static const char *link_type(int t, char *buf, size_t size)
{
    if (t == DST_SNMP)
        return "snmp";
    if (t == DST_CLI)
        return "cli";
    if (t == DST_NETCONF)
        return "netconf";
    snprintf(buf, size, "type%d", t);
    return buf;
}

int upgsnap_ne_links_gather(upgsnap_buf_t *out, const upgsnap_env_t *env)
{
    ES64_SNMP_INFO_REC r;
    int opened = 0, rc, n = 0;

    if (!env->frm_ok)
        return upgsnap_unavailable(out, env->app_up ? "frame_link not attached"
                                                    : "netFLEX not running");
    if (ES64_SnmpInfo_DbState != OPEN) {
        if (open_es64_snmp_info() != SUCCESS) {
            upgsnap_kv(out, "status", "%s", "cannot open es64_snmp_info");
            return UPGSNAP_PARTIAL;
        }
        opened = 1;
    }
    memset(&r, 0, sizeof(r));             /* empty prefix key: whole table */
    for (rc = first_es64_snmp_info_neId_slot(&r); rc == GOT_ONE;
         rc = next_es64_snmp_info_neId_slot(&r)) {
        char key[64], tbuf[16], ip[ES64_IPADDR_SZ + 1];
        const char *tn = link_type(r.type, tbuf, sizeof(tbuf));
        const char *proto;
        int port;

        if (r.type == DST_CLI) {
            proto = r.ssh_flag ? "ssh" : "telnet";
            port = r.cliPort;
        } else if (r.type == DST_NETCONF) {
            proto = "ssh";
            port = r.cliPort;
        } else {
            proto = "snmp";
            port = r.snmpPort;
        }
        snprintf(ip, sizeof(ip), "%s", r.ipAddr);
        snprintf(key, sizeof(key), "%05d.%02d.%s", r.neId, (int)r.slot, tn);
        upgsnap_kv(out, key, "%s %s %s:%d enabled=%d", r.snmpLinkStat ? "up" : "down",
                   proto, ip, port, r.enabled);
        n++;
    }
    if (opened)
        close_es64_snmp_info();
    upgsnap_kv(out, "total.links", "%d", n);
    if (rc != NOT_FOUND) {
        upgsnap_kv(out, "status", "%s", "es64_snmp_info read error");
        return UPGSNAP_PARTIAL;
    }
    return UPGSNAP_OK;
}
```

- [ ] **Step 2: Compile** `upgsnap_ne.o` locally. Expect to resolve: the header that defines `HAS_COMM_GRP` (grep `define HAS_COMM_GRP` in include/ and add it), `DST_SNMP/DST_CLI/DST_NETCONF` (msgscreen.h), `GOT_ONE` (flags.h — add `#include <flags.h>` if not pulled in), `OPEN`/`NOT_FOUND` (fv_com.h), `SUCCESS`. Field names must match `struct frame_link` exactly (d1compool.h:1171); if a name differs, fix the code, never the struct. No warnings.

- [ ] **Step 3: Commit** — `git commit -m "upgsnap: nes, equipment and ne_links sections"`.

---

### Task 5: `alarms`, `dbcheck`, `db_*` sections; link

**Files:** Create `cnc/tools/src/upgsnap_db.c`.

- [ ] **Step 1: Implement**

```c
/**
 * @file upgsnap_db.c
 * @brief "alarms" (active alarm counts by severity), "dbcheck" (audit error
 *        counts from `dbcheck -AS`) and "db_*" (config DB exports via
 *        `inc_db <db> --export`). dbcheck and inc_db are run read-only:
 *        no repair/force options are ever passed.
 */
#include <stdbool.h>
#include <stdio.h>
#include <string.h>
#include <guidefs.h>
#include <dcompool.h>
#include <d1compool.h>
#include <com_db.h>
#include <alm_db.h>
#include <upgsnap.h>

/** @brief Active alarm counts. */
typedef struct {
    int cr;     /**< Critical. */
    int mj;     /**< Major. */
    int mn;     /**< Minor. */
    int warn;   /**< Warning. */
    int info;   /**< Informational. */
    int other;  /**< Any other severity string. */
    int total;  /**< All counted records. */
} upgsnap_alm_counts_t;

/** @brief dump_alm() callback: count one record by its sev string. */
static int count_alarm(ALM_REC *a, void *data)
{
    upgsnap_alm_counts_t *c = data;

    c->total++;
    if (strcmp(a->sev, SEV_STR_CR) == 0)
        c->cr++;
    else if (strcmp(a->sev, SEV_STR_MJ) == 0)
        c->mj++;
    else if (strcmp(a->sev, SEV_STR_MN) == 0)
        c->mn++;
    else if (strcmp(a->sev, SEV_STR_WARN) == 0)
        c->warn++;
    else if (strcmp(a->sev, SEV_STR_INFO) == 0)
        c->info++;
    else
        c->other++;
    return 0;
}

int upgsnap_alarms_gather(upgsnap_buf_t *out, const upgsnap_env_t *env)
{
    upgsnap_alm_counts_t c;
    int status = ALM_IN_ALARM, rc;

    if (!env->frm_ok)
        return upgsnap_unavailable(out, env->app_up ? "frame_link not attached"
                                                    : "netFLEX not running");
    memset(&c, 0, sizeof(c));
    if (ct_open_alm() != SUCCESS) {
        upgsnap_kv(out, "status", "%s", "cannot open alarm database");
        return UPGSNAP_PARTIAL;
    }
    rc = dump_alm(count_alarm, &c, &status, NULL, NULL);
    ct_close_alm();
    upgsnap_kv(out, "active.critical", "%d", c.cr);
    upgsnap_kv(out, "active.major", "%d", c.mj);
    upgsnap_kv(out, "active.minor", "%d", c.mn);
    upgsnap_kv(out, "active.warning", "%d", c.warn);
    upgsnap_kv(out, "active.info", "%d", c.info);
    upgsnap_kv(out, "active.other", "%d", c.other);
    upgsnap_kv(out, "active.total", "%d", c.total);
    if (rc == FAILURE) {
        upgsnap_kv(out, "status", "%s", "alarm database read error");
        return UPGSNAP_PARTIAL;
    }
    return UPGSNAP_OK;
}

int upgsnap_dbcheck_gather(upgsnap_buf_t *out, const upgsnap_env_t *env)
{
    char line[1024];
    FILE *p;
    int seen = 0;

    if (!env->app_up)
        return upgsnap_unavailable(out, "netFLEX not running");
    /* -A all audits, -S summary only; never -F/-X/-R (repair). Can take minutes. */
    p = popen("/usr/cnc/bin/dbcheck -AS 2>&1", "r");
    if (p == NULL) {
        upgsnap_kv(out, "status", "%s", "cannot run dbcheck");
        return UPGSNAP_PARTIAL;
    }
    while (fgets(line, sizeof(line), p) != NULL)
        seen |= upgsnap_dbcheck_parse(out, line);
    pclose(p);
    if (!seen) {
        upgsnap_kv(out, "status", "%s", "no summary from dbcheck");
        return UPGSNAP_PARTIAL;
    }
    return UPGSNAP_OK;
}

/**
 * @brief Export one config DB via inc_db (records in primary-index order).
 * @param out Buffer.
 * @param env Attach state.
 * @param db  inc_db database name.
 * @return UPGSNAP_OK or UPGSNAP_PARTIAL.
 */
static int db_export(upgsnap_buf_t *out, const upgsnap_env_t *env, const char *db)
{
    char cmd[128];
    int rc;

    if (!env->app_up)
        return upgsnap_unavailable(out, "netFLEX not running");
    snprintf(cmd, sizeof(cmd), "/usr/cnc/mbin/inc_db %s --export 2>/dev/null", db);
    rc = upgsnap_cmd_lines(out, cmd);
    if (rc != 0) {
        upgsnap_kv(out, "status", "inc_db exit %d", rc);
        return UPGSNAP_PARTIAL;
    }
    return UPGSNAP_OK;
}

int upgsnap_db_tie_gather(upgsnap_buf_t *out, const upgsnap_env_t *env)
{
    return db_export(out, env, "tie");
}

int upgsnap_db_alias_gather(upgsnap_buf_t *out, const upgsnap_env_t *env)
{
    return db_export(out, env, "alias");
}

int upgsnap_db_opr_gather(upgsnap_buf_t *out, const upgsnap_env_t *env)
{
    return db_export(out, env, "opr");
}

int upgsnap_db_opr_notify_gather(upgsnap_buf_t *out, const upgsnap_env_t *env)
{
    return db_export(out, env, "opr_notify");
}
```

- [ ] **Step 2: Compile** `upgsnap_db.o` locally; resolve prototypes for `ct_open_alm`/`ct_close_alm`/`dump_alm` (alm_db.h; grep if elsewhere), `ALM_IN_ALARM`, `SEV_STR_*`, `FAILURE`/`SUCCESS`. No warnings.

- [ ] **Step 3: Link on a build host** — on a host/CI where the netFLEX libraries are built (`3b2/lib` populated): `cd cnc/tools/src && nmake -f tools.mk ../../../3b2/bin/upgsnap` → binary links with no undefined symbols. If `dump_alm` or others are unresolved, add the library that defines them (grep `util.mk` for the object → library) to the rule. Also build on RHEL 7 (gcc 4.8.5) to confirm the baseline.

- [ ] **Step 4: Commit** — `git commit -m "upgsnap: alarms, dbcheck and config DB export sections"`.

---

### Task 6: Install + documentation

**Files:** Modify `3b2/shell/install_cnc`; Create `3b2/data/upgsnap.md`.

- [ ] **Step 1: install_cnc** — after the `bin="$bin vinstall setaliasperm … frmlnkd"` line (~:980 on main; ~:977 on inc54.0), add:

```sh
bin="$bin upgsnap"
```
  Verify `ksh -n 3b2/shell/install_cnc` passes.

- [ ] **Step 2: Doc** — create `3b2/data/upgsnap.md` describing: purpose (read-only upgrade snapshot for before/after diffs), usage (`--list`, `--section NAME`, no args = all with `== name ==` headers), exit codes (0/1/2), each section and its keys (as in the tables above), what is deliberately excluded (counters, timestamps, pids, disk usage, alarm records, passwords/community strings), that `dbcheck` can take minutes, and that `open_es64_snmp_info`/`ct_open_alm` may rebuild a corrupt c-tree file exactly as `snmpne -D` / `syshlth -a` do.

- [ ] **Step 3: Commit** — `git commit -m "upgsnap: install to /usr/cnc/bin, add upgsnap.md"`.

---

### Task 7: Verification on a netFLEX host (manual)

- [ ] On holmvm32 (or another box running the new build): `upgsnap --list` lists the 11 sections.
- [ ] `upgsnap --section system`, `rdb`, `nes`, `ne_links`, `equipment`, `alarms`: each exits 0 with sensible values; run each twice → identical output (`cmp`).
- [ ] `upgsnap | grep -iE 'passw|community|nepasswd'` → nothing.
- [ ] `upgsnap --section dbcheck` completes and its totals match `dbcheck -AS`.
- [ ] Stop netFLEX (or on a box where it's down): shared-memory sections print `status: unavailable (netFLEX not running)`, exit 0; `system` still complete.
- [ ] `upgsnap --section bogus` → exit 2 with the message; `upgsnap -h` prints usage.
- [ ] Trace file `/usr/cnc/trace/upgsnap` exists; nothing else changed under /usr/cnc (compare `find /usr/cnc -newer <stamp>` excluding trace).

Then: open a PR into `main` (no Patchbuild notes block: base is main) ending with the body line required by the global instructions (`Just remember it takes a village`). Backports to `inc54.1` / `inc54.0` are separate PRs *with* Patchbuild notes — ask the user for the rebuild products / restart values then (per global instructions: never guess them).

---

## Facts (from research; file:line on origin/main, same on inc54.0 unless noted)

- `is_cnc_up(int tp, int waitflag, char **msg)` misc1.c:1470 — pass tp=1; never exits.
- `atch_frm(char **start)` atch_frm.c:44 — returns `(struct frame_link *)FAILURE`, prints failures to **stdout**, never exits.
- `set_tunables(void)` ddb_init.c:1132 — FAILURE if no license shm; sets FepId.
- `ddb_init(int)` read-only mapping (ddb_init_protect PROT_READ); `ShmAlloc` flags DP/FRM/UNIT/COMMON/PM/AAL in dshm_data.h:208-213.
- Skip `atch_bep_socket_stat()` (creates shm); not needed with NO_LINK_OPTION.
- `getopts` include/getopts.h:42-56; -h/--help built in (exit 0); arg-taking options' `*args` must be freed.
- `NE_UNASSIGNED(f,x)` d1compool.h:1516, `ALL_FEP 0`; `IS_DISABLED(x) ((x))`; `NO_LINK_OPTION 0`; `NE_LINK_UP 1` (link_status.h); `get_link_status(frm, neid, protect, opt)` ddb_init.c:3446.
- `dtypes[]` defined in dtype.ext (include in one TU only); `sys_name[TID_SIZE]`, `release[RELEASE_SIZE]` (not NUL-terminated), `baud[2][3]`, `login[]`, `def_owner_str[]`, `dl_aid[]`, bitfields per d1compool.h:1171-1505. Secrets: `nepasswd_cur/pnd`, `dkpath[0]` (SNMP community).
- es64: `ES64_SNMP_INFO_REC` alcatel_es64_db.h:495; `open/close_es64_snmp_info`, `first/next_es64_snmp_info_neId_slot` return `GOT_ONE`/`NOT_FOUND`/`FAILURE`; `ES64_SnmpInfo_DbState` extern; `DST_SNMP 1, DST_CLI 2, DST_NETCONF 3` (msgscreen.h:874).
- alarms: `ALM_REC.sev` string, `SEV_STR_CR/MJ/MN/WARN/INFO` alm_db.h:51-61; `ALM_IN_ALARM 1`; `dump_alm(cb, data, statusFilt, neidFilt, userFilt)` alm_db.c:4248 after `ct_open_alm()`; needs Frmlnk attached.
- version: `/usr/cnc/data/version.h` `#define OVINC_VERSION "@(#)SOFTWARE VERSION: 54.1.16 …"` preceded by a TEMPLATE comment line.
- RPM: avoid `getCmdR` (24 h cache + writes /usr/cnc/data/*rev); use `getcmdoutput(char*, char*, int)` misc.c:385 (returns length or -1; exit status not reported).
- Role: CONFIG_FILE (/usr/cnc/features/cnc.cnfg): `# Single`/`# Multi`; rows `%s%d%s%d%d%d` → machtype 1 FEP / 0 BEP.
- RDB: rdb.h:110-130, rdb_util.h:151-156, rdb_inc_versions.h:18, `RDB_OTNPORT_DB` in rdb_otn_port.h:6; close with `rdb_close(__func__, con, TRUE, FALSE, NULL)`; `rdb_current_version` returns calloc'd (empty on failure) → `rdb_free`.
- dbcheck summary printf format dbcheck.c:587-620 (`  Error %d \t\t%d\t\t%d`, `  Total\t\t\t%d\t\t%d`, `db audit(s) complete, 0 errors found`); `-S` = summary; never `-F/-X/-R`.
- Build: tools.mk `.SOURCE.h : ../../../include`; `.ALL_LINUX :` at ~:117; rule pattern at frm_vs_rdb ~:1809; libs not built on the dev box (`3b2/lib`).
- install_cnc `bin=` lines ~:969-984 (main), ~:967-981 (inc54.0); loop silently skips missing files.
