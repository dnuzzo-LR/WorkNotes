# nfupgrader Phase 1 — Fork + Logging Cleanup Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fork `3b2/shell/inc_upgrade` / `inc_upgrader` into `nf_upgrade` / `nf_upgrader` that are behavior-identical to the originals but emit a consistent, severity-prefixed, machine-parseable log — with automated proof that no behavioral line changed.

**Architecture:** The originals are left untouched. The forks `source` a new logging include (`nf_log.ksh`) and route every one of the scripts' own stdout emissions (`printf` / `print` / `echo` with no redirection) through severity helpers (`loginfo` / `logwarn` / `logerror` / `logfatal`). A small curated set of sub-commands whose output currently lands unredirected in the trace file is wrapped by a status-preserving `run_logged` helper that tags their output `EXEC`. Control-file writes (`printf … > ${STATEFILE}` etc.), banners and usage text are left raw. Equivalence is proven by a "behavioral skeleton" diff: with emit lines removed and the rename/wrap normalized away, the fork must be byte-identical to the original.

**Tech Stack:** ksh (AT&T ksh93 on the dev box; the helpers are written ksh88-safe for customer boxes running `/usr/bin/ksh`). `ksh -n` for syntax checking. A small self-contained ksh assertion library for unit tests. `sed`/`awk`/`diff` for the equivalence verifiers. No external test framework, no build step.

---

## Why this is Phase 1 only

The full spec (`~/WorkNotes/design/2026-09-15-web-upgrade-tool-design.md`) has five phases. This plan implements **Phase 1**: the forked shell scripts and their logging cleanup. It is the only phase that touches the shell, and everything else (the Python service, progress record, checks, dashboard, config builder) reads the artifacts this phase produces. Phases 2–5 get their own plans.

## Scope boundaries

**In scope:** creating `nf_log.ksh`, `nf_upgrade`, `nf_upgrader`; converting emit sites; the curated `run_logged` wrap; the unit tests and equivalence/coverage/grammar verifiers.

**Out of scope (Phase 1):** any Python, the web UI, running a real end-to-end upgrade. A real staged-box run is the *manual* acceptance gate (Task 13); it cannot be automated here because `nf_upgrader` needs a fully staged `/usr/cnc` environment to execute for real.

---

## File Structure

| File | Responsibility |
|---|---|
| `3b2/shell/nf_log.ksh` | **New.** The logging include: `nf_log`, `loginfo`, `logwarn`, `logerror`, `logfatal`, `run_logged`. Sourced by both forks. Sole home of the log format — DRY. |
| `3b2/shell/nf_upgrade` | **New.** Fork of `inc_upgrade`: sources `nf_log.ksh`, converts its ~87 emit sites, retargets its `exec` to `nf_upgrader`. |
| `3b2/shell/nf_upgrader` | **New.** Fork of `inc_upgrader`: sources `nf_log.ksh`, converts its ~218 emit sites, wraps the curated sub-command set with `run_logged`. |
| `3b2/shell/test/nf_test_lib.ksh` | **New.** Tiny assertion library (`assert_eq`, `assert_match`, `assert_rc`) + a runner that prints pass/fail and exits non-zero on any failure. |
| `3b2/shell/test/nf_log_test.ksh` | **New.** Unit tests for `nf_log.ksh` (format, each severity, `run_logged` status preservation + `EXEC` tagging + non-zero WARN). |
| `3b2/shell/test/nf_fork_verify.sh` | **New.** Static verifiers over the forks: `ksh -n` syntax, emit-coverage (no unconverted bare emits outside the allowlist), behavioral-skeleton equivalence vs the originals, and log-grammar of converted lines. |

The originals `3b2/shell/inc_upgrade` and `3b2/shell/inc_upgrader` are **read-only references** for this whole plan. Never edit them.

---

## Conversion rules (the contract every conversion task obeys)

These rules are mechanical and exact. The verifiers in Task 5 enforce them.

**A line is a "bare emit" (→ convert) iff:** it matches, from its first non-blank character, one of `printf` / `print` / `echo`, **and** it contains no output redirection. "Redirection" = a `>` immediately preceded by whitespace (regex `[[:space:]]>`). This deliberately does **not** match `->` inside a message (e.g. retrofit `25.1 -> 25.1`), because there the `>` is preceded by `-`, not whitespace.

**Conversion mapping for a bare emit:**
- Strip a trailing `\n` from the format string (the helpers add the newline).
- If the message text contains `ERROR` → `logerror`; if it contains `WARNING`/`WARN` → `logwarn`; otherwise → `loginfo`.
- Collapse the emit to a single message argument. Example:
  - `printf "\nERROR: Invalid DEPOT file [$DEPOTFILE] in config file.\n"` → `logerror "Invalid DEPOT file [$DEPOTFILE] in config file."`
  - `printf "Exporting GSHM data to ${EXPORTDIR}\n"` → `loginfo "Exporting GSHM data to ${EXPORTDIR}"`
  - `echo "PostgreSQL version is $PG_VERSION"` → `loginfo "PostgreSQL version is $PG_VERSION"`
  - `print "BASEREV=$BASEREV\n"` → `loginfo "BASEREV=$BASEREV"`
- Preserve the surrounding indentation exactly.
- Do **not** introduce a literal `" >"` (space-then-`>`) into any converted message — it would look like a redirection to the verifier. (None of the current messages need one.)

**Exclusions — leave the line byte-for-byte unchanged:**
1. **Redirected emits** — anything with `[[:space:]]>` (`printf "$1\n" > ${STATEFILE}`, `… >> ${TRACEFILE}`, `… >/dev/null`). This covers all control-file writes: `${STATEFILE}` (`:138`), `${WEBSTATEFILE}` (`:143`), `.OLD_GENERIC` (`:862`), `.OLD_LOAD` (`:863`), `.LPRESULT` (`:1139`).
2. **Banner text** — the body of `tool_banner` (`inc_upgrader:159`–174) and the ASCII banner in `inc_upgrade`.
3. **Usage text** — the body of `print_usage` (`inc_upgrader:175`–186; `inc_upgrade:26`–35).

**Fatal severity:** in `fail_upgrade` and `exit_upgrade`, the `printf "Aborting upgrade, try again!\n\n"` becomes `logfatal "Aborting upgrade"`. (Handled explicitly in Task 7.)

**`run_logged` set:** only sub-commands whose stdout currently flows **unredirected** into the trace file. Discovery command (run it; wrap exactly what it returns, nothing more):
```
grep -nE '^[[:space:]]*(sh|rpm|/usr/cnc/(bin|mbin|ambin)/[a-z_]+)[[:space:]]' inc_upgrader | grep -vE '[[:space:]]>|\|'
```
As of commit `776f42242` this is the two `sh /usr/cnc/bin/post-install.sh` calls (`:1438`, `:1447`). Wrap form: `sh /usr/cnc/bin/post-install.sh` → `run_logged post-install sh /usr/cnc/bin/post-install.sh`.

---

## Task 1: Test harness library

**Files:**
- Create: `3b2/shell/test/nf_test_lib.ksh`

- [ ] **Step 1: Write the assertion library**

```ksh
# nf_test_lib.ksh — minimal ksh assertion helpers. Source this, call asserts,
# then call nf_test_summary at the end. Exit code reflects failures.
integer NF_TEST_PASS=0
integer NF_TEST_FAIL=0

assert_eq() {   # assert_eq <label> <expected> <actual>
	if [ "$2" = "$3" ]; then
		NF_TEST_PASS=$((NF_TEST_PASS+1)); printf 'ok   %s\n' "$1"
	else
		NF_TEST_FAIL=$((NF_TEST_FAIL+1))
		printf 'FAIL %s\n     expected: [%s]\n     actual:   [%s]\n' "$1" "$2" "$3"
	fi
}

assert_match() {   # assert_match <label> <ere> <actual>
	if print -r -- "$3" | grep -qE "$2"; then
		NF_TEST_PASS=$((NF_TEST_PASS+1)); printf 'ok   %s\n' "$1"
	else
		NF_TEST_FAIL=$((NF_TEST_FAIL+1))
		printf 'FAIL %s\n     pattern: [%s]\n     actual:  [%s]\n' "$1" "$2" "$3"
	fi
}

assert_rc() {   # assert_rc <label> <expected_rc> <actual_rc>
	assert_eq "$1" "$2" "$3"
}

nf_test_summary() {
	printf -- '---- %d passed, %d failed ----\n' "$NF_TEST_PASS" "$NF_TEST_FAIL"
	[ "$NF_TEST_FAIL" -eq 0 ]
}
```

- [ ] **Step 2: Sanity-check it loads under ksh**

Run: `ksh -c '. 3b2/shell/test/nf_test_lib.ksh; assert_eq self a a; nf_test_summary'`
Expected: prints `ok   self` then `---- 1 passed, 0 failed ----`, exit 0.

- [ ] **Step 3: Commit**

```bash
git add 3b2/shell/test/nf_test_lib.ksh
git commit -m "test(nfupgrader): add minimal ksh assertion library"
```

---

## Task 2: `nf_log.ksh` — severity helpers (TDD)

**Files:**
- Create: `3b2/shell/nf_log.ksh`
- Test: `3b2/shell/test/nf_log_test.ksh`

- [ ] **Step 1: Write the failing test**

```ksh
# nf_log_test.ksh
DIR="$(cd "$(dirname "$0")/.." && pwd)"
. "${DIR}/test/nf_test_lib.ksh"
. "${DIR}/nf_log.ksh"

TS='[0-9]{4}-[0-9]{2}-[0-9]{2} [0-9]{2}:[0-9]{2}:[0-9]{2}'

assert_match "info format"  "^${TS} INFO  hello world$"  "$(loginfo 'hello world')"
assert_match "warn format"  "^${TS} WARN  careful$"      "$(logwarn 'careful')"
assert_match "error format" "^${TS} ERROR broke$"        "$(logerror 'broke')"
assert_match "fatal format" "^${TS} FATAL dead$"         "$(logfatal 'dead')"

nf_test_summary
```

- [ ] **Step 2: Run test to verify it fails**

Run: `ksh 3b2/shell/test/nf_log_test.ksh`
Expected: FAIL — `nf_log.ksh` does not exist / functions undefined.

- [ ] **Step 3: Write the minimal include**

```ksh
# nf_log.ksh — logging helpers for nf_upgrade / nf_upgrader.
# Sourced, never executed. ksh88-safe (no PIPESTATUS/pipefail, no arrays).
# run_logged needs a writable dir; it uses ${INCLOGDIR:-/tmp}.

nf_log() {   # nf_log <SEVERITY> <message>
	printf '%s %-5s %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$1" "$2"
}
loginfo()  { nf_log INFO  "$*"; }
logwarn()  { nf_log WARN  "$*"; }
logerror() { nf_log ERROR "$*"; }
logfatal() { nf_log FATAL "$*"; }
```

- [ ] **Step 4: Run test to verify it passes**

Run: `ksh 3b2/shell/test/nf_log_test.ksh`
Expected: PASS — `---- 4 passed, 0 failed ----`, exit 0.

Note the two spaces after `INFO`/`WARN`: the `%-5s` field pads the 4-letter severities to width 5, giving one column. `ERROR`/`FATAL` are already 5 wide.

- [ ] **Step 5: Commit**

```bash
git add 3b2/shell/nf_log.ksh 3b2/shell/test/nf_log_test.ksh
git commit -m "feat(nfupgrader): add nf_log.ksh severity helpers with tests"
```

---

## Task 3: `run_logged` — status-preserving sub-command wrapper (TDD)

**Files:**
- Modify: `3b2/shell/nf_log.ksh`
- Modify: `3b2/shell/test/nf_log_test.ksh`

- [ ] **Step 1: Add failing tests**

Insert before `nf_test_summary` in `nf_log_test.ksh`:

```ksh
export INCLOGDIR="${TMPDIR:-/tmp}"

# EXEC tagging: each child line is prefixed with timestamp, EXEC, and [tag].
out="$(run_logged demo printf 'one\ntwo\n')"
assert_match "exec line 1" "^${TS} EXEC  \[demo\] one$" "$(print -r -- "$out" | sed -n 1p)"
assert_match "exec line 2" "^${TS} EXEC  \[demo\] two$" "$(print -r -- "$out" | sed -n 2p)"

# Exit status of the child is preserved through the wrapper.
run_logged fail sh -c 'exit 7' >/dev/null
assert_rc "rc preserved" 7 "$?"

# A non-zero child also emits a WARN line naming the tag and code.
warn="$(run_logged fail sh -c 'exit 7')"
assert_match "nonzero warns" "WARN  \[fail\] exited 7" "$warn"

# A zero-exit child emits no WARN line.
ok="$(run_logged good sh -c 'exit 0')"
assert_match "zero no warn" '^$' "$(print -r -- "$ok" | grep WARN)"
```

- [ ] **Step 2: Run to verify failure**

Run: `ksh 3b2/shell/test/nf_log_test.ksh`
Expected: FAIL — `run_logged` undefined.

- [ ] **Step 3: Implement `run_logged`**

Append to `nf_log.ksh`:

```ksh
run_logged() {   # run_logged <tag> cmd [args...]
	typeset tag="$1"; shift
	typeset tmp="${INCLOGDIR:-/tmp}/.nfrun.$$"
	"$@" > "${tmp}" 2>&1
	typeset rc=$?
	sed "s/^/$(date '+%Y-%m-%d %H:%M:%S') EXEC  [${tag}] /" "${tmp}"
	rm -f "${tmp}"
	[ ${rc} -ne 0 ] && logwarn "[${tag}] exited ${rc}"
	return ${rc}
}
```

No pipe is used (`cmd > tmp; rc=$?`), so `$?` reflects the child, not `sed` — the property the test asserts. `PIPESTATUS`/`set -o pipefail` are avoided; this runs on ksh88.

- [ ] **Step 4: Run to verify passes**

Run: `ksh 3b2/shell/test/nf_log_test.ksh`
Expected: PASS — `---- 8 passed, 0 failed ----`, exit 0.

- [ ] **Step 5: Commit**

```bash
git add 3b2/shell/nf_log.ksh 3b2/shell/test/nf_log_test.ksh
git commit -m "feat(nfupgrader): add run_logged status-preserving EXEC wrapper"
```

---

## Task 4: Fork the scripts verbatim + source the include + retarget exec

This establishes a safe checkpoint: the forks are the originals plus only (a) the sourced include and (b) `nf_upgrade` calling `nf_upgrader`. No emit conversion yet.

**Files:**
- Create: `3b2/shell/nf_upgrade` (from `inc_upgrade`)
- Create: `3b2/shell/nf_upgrader` (from `inc_upgrader`)

- [ ] **Step 1: Copy the originals**

```bash
cp 3b2/shell/inc_upgrade   3b2/shell/nf_upgrade
cp 3b2/shell/inc_upgrader  3b2/shell/nf_upgrader
chmod --reference=3b2/shell/inc_upgrade  3b2/shell/nf_upgrade
chmod --reference=3b2/shell/inc_upgrader 3b2/shell/nf_upgrader
```

- [ ] **Step 2: Source the include in `nf_upgrader`**

In `3b2/shell/nf_upgrader`, immediately after the `PATH=${PATH}:${PWD}:/usr/sbin:/sbin` line near the top (currently `inc_upgrader:13`), add:

```ksh
NF_LOG_LIB="$(dirname "$0")/nf_log.ksh"
[ -r "${NF_LOG_LIB}" ] && . "${NF_LOG_LIB}"
```

- [ ] **Step 3: Source the include in `nf_upgrade`**

In `3b2/shell/nf_upgrade`, immediately after the `export PATH=${PATH}:${PWD}:/usr/sbin` line near the top (currently `inc_upgrade:17`), add the same two lines as Step 2.

- [ ] **Step 4: Retarget `nf_upgrade`'s exec to `nf_upgrader`**

In `3b2/shell/nf_upgrade`, change both invocations (currently `inc_upgrade:541` and `:543`) from `${EXECDIR}/inc_upgrader` to `${EXECDIR}/nf_upgrader`. Also update the comment at `:525` (`Make sure correct version of inc_upgrader is run.` → `… nf_upgrader …`).

- [ ] **Step 5: Syntax-check both forks**

Run: `ksh -n 3b2/shell/nf_upgrade && ksh -n 3b2/shell/nf_upgrader && echo SYNTAX_OK`
Expected: `SYNTAX_OK`, exit 0.

- [ ] **Step 6: Verify the diff is *only* the expected lines**

Run:
```bash
diff 3b2/shell/inc_upgrader 3b2/shell/nf_upgrader
diff 3b2/shell/inc_upgrade  3b2/shell/nf_upgrade
```
Expected: the `nf_upgrader` diff shows only the two added `NF_LOG_LIB` lines. The `nf_upgrade` diff shows only the two added `NF_LOG_LIB` lines, the two `inc_upgrader`→`nf_upgrader` exec retargets, and the one comment change. Nothing else.

- [ ] **Step 7: Commit**

```bash
git add 3b2/shell/nf_upgrade 3b2/shell/nf_upgrader
git commit -m "feat(nfupgrader): fork nf_upgrade/nf_upgrader, source nf_log.ksh"
```

---

## Task 5: Fork verifiers (written before conversion, so they fail first)

**Files:**
- Create: `3b2/shell/test/nf_fork_verify.sh`

- [ ] **Step 1: Write the verifier script**

```sh
#!/bin/sh
# nf_fork_verify.sh — static equivalence/coverage/grammar checks for the forks.
# Run from repo root: sh 3b2/shell/test/nf_fork_verify.sh
cd "$(dirname "$0")/.." || exit 2   # -> 3b2/shell
fail=0

# --- 1. Syntax --------------------------------------------------------------
for f in nf_upgrade nf_upgrader; do
	if ksh -n "$f"; then echo "ok   syntax $f"
	else echo "FAIL syntax $f"; fail=1; fi
done

# --- 2. Behavioral skeleton: fork must equal original once emits/rename/wrap
#        are normalized away. Proves no control-flow line changed. -----------
# B() reduces a script to its behavioral skeleton:
#   * drop the sourced-include line
#   * rename nf_upgrade(r) -> inc_upgrade(r)  (neutralize the fork rename)
#   * unwrap "run_logged <tag> " prefixes back to the bare command
#   * delete every emit/log line that has NO "[space]>" redirection
#     (bare printf/print/echo AND log*/logfatal calls); keep redirected
#     emits (control-file writes) so they ARE compared.
B() {
	sed -e '/nf_log\.ksh/d' \
	    -e 's/nf_upgrader/inc_upgrader/g' -e 's/nf_upgrade/inc_upgrade/g' \
	    -e 's/^\([[:space:]]*\)run_logged [^ ]* /\1/' "$1" \
	| awk '!(/^[[:space:]]*(printf|print|echo|log|loginfo|logwarn|logerror|logfatal)([[:space:]]|\(|$)/ && $0 !~ /[[:space:]]>/)'
}
for pair in "inc_upgrade nf_upgrade" "inc_upgrader nf_upgrader"; do
	set -- $pair
	if diff "$(B "$1" >/tmp/nfb.orig.$$; echo /tmp/nfb.orig.$$)" \
	        "$(B "$2" >/tmp/nfb.fork.$$; echo /tmp/nfb.fork.$$)" >/tmp/nfb.diff.$$ 2>&1; then
		echo "ok   skeleton $2 == $1"
	else
		echo "FAIL skeleton $2 != $1"; sed 's/^/       /' /tmp/nfb.diff.$$; fail=1
	fi
	rm -f /tmp/nfb.orig.$$ /tmp/nfb.fork.$$ /tmp/nfb.diff.$$
done

# --- 3. Emit coverage: no unconverted bare emit remains in a fork, except the
#        banner/usage allowlist. A bare emit = printf/print/echo at line start
#        with no "[space]>" redirection. ----------------------------------
#   nf_upgrade allowlist:  print_usage body + ASCII banner
#   nf_upgrader allowlist:  tool_banner body + print_usage body
check_coverage() {   # check_coverage <fork> <sed-range-deletions...>
	fork="$1"; shift
	remaining=$(sed "$@" "$fork" \
		| grep -nE '^[[:space:]]*(printf|print|echo)([[:space:]]|$)' \
		| grep -vE '[[:space:]]>')
	if [ -z "$remaining" ]; then echo "ok   coverage $fork"
	else echo "FAIL coverage $fork (unconverted bare emits):"; echo "$remaining" | sed 's/^/       /'; fail=1; fi
}
# Ranges are matched by content, not line number, so they survive edits:
check_coverage nf_upgrader \
	-e '/^function tool_banner$/,/^}$/d' \
	-e '/^function print_usage$/,/^}$/d'
check_coverage nf_upgrade \
	-e '/^function print_usage$/,/^}$/d'

# --- 4. Grammar: every converted log line, when executed, matches the format.
#        Static proxy: log*/run_logged calls are well-formed (quoted arg). ---
for f in nf_upgrade nf_upgrader; do
	bad=$(grep -nE '^[[:space:]]*(loginfo|logwarn|logerror|logfatal)([[:space:]]|$)' "$f" \
		| grep -vE '(loginfo|logwarn|logerror|logfatal)[[:space:]]+"' || true)
	if [ -z "$bad" ]; then echo "ok   grammar $f"
	else echo "FAIL grammar $f (log call without quoted message):"; echo "$bad" | sed 's/^/       /'; fail=1; fi
done

echo "----"
[ "$fail" -eq 0 ] && echo "VERIFY OK" || echo "VERIFY FAILED"
exit $fail
```

- [ ] **Step 2: Run it — expect coverage to fail (emits not yet converted)**

Run: `sh 3b2/shell/test/nf_fork_verify.sh`
Expected: syntax `ok`, skeleton `ok` (Task 4 changed no behavioral lines), **coverage `FAIL`** for both forks (hundreds of bare emits remain), grammar `ok` (no log calls yet). Overall `VERIFY FAILED`, exit 1. This is the red state the conversion tasks turn green.

- [ ] **Step 3: Commit**

```bash
git add 3b2/shell/test/nf_fork_verify.sh
git commit -m "test(nfupgrader): add fork skeleton/coverage/grammar verifiers"
```

---

## Task 6: Convert emit sites in `nf_upgrade`

`nf_upgrade` is the smaller script (~87 emits). Convert it whole, per the [Conversion rules](#conversion-rules-the-contract-every-conversion-task-obeys).

**Files:**
- Modify: `3b2/shell/nf_upgrade`

- [ ] **Step 1: List the bare emits to convert**

Run:
```bash
sed '/^function print_usage$/,/^}$/d' 3b2/shell/nf_upgrade \
 | grep -nE '^[[:space:]]*(printf|print|echo)([[:space:]]|$)' | grep -vE '[[:space:]]>'
```
This is the exact work-list (banner/usage excluded, redirected lines excluded).

- [ ] **Step 2: Convert each listed line**

Apply the mapping for every line in the work-list. Representative conversions in this file:
- `printf "LARCH=$LARCH\n"` → `loginfo "LARCH=$LARCH"`
- `printf "ERROR: Must provide one and only one of -s or -u options\n"` → `logerror "Must provide one and only one of -s or -u options"`
- `printf "\nWARNING: Reinstalling same ${PRODUCT} software [${OLD_GENERIC}.${OLD_LOAD}] that is currently installed.\n"` → `logwarn "Reinstalling same ${PRODUCT} software [${OLD_GENERIC}.${OLD_LOAD}] that is currently installed."`
- `print "BASEREV=$BASEREV\n"` → `loginfo "BASEREV=$BASEREV"`

Leave `print_usage` body, the ASCII banner, and every `… > file` / `… >> ${TRACEFILE}` line untouched.

- [ ] **Step 3: Syntax + verifier**

Run: `ksh -n 3b2/shell/nf_upgrade && sh 3b2/shell/test/nf_fork_verify.sh`
Expected: `coverage nf_upgrade` now `ok`; `skeleton nf_upgrade == inc_upgrade` still `ok`; `grammar nf_upgrade` `ok`. (`nf_upgrader` coverage still FAILs — converted next.)

If skeleton fails: a non-emit line was accidentally changed. The printed diff names it — revert that line to the original.

- [ ] **Step 4: Commit**

```bash
git add 3b2/shell/nf_upgrade
git commit -m "refactor(nfupgrader): route nf_upgrade output through log helpers"
```

---

## Task 7: Convert `nf_upgrader` — batch A (top of file through the fatal helpers and export functions)

Convert emits from the top of `nf_upgrader` through line ~610 (the `get_data_dir`/`write_state`/`fail_upgrade`/`exit_upgrade`/`calc_*`/`export_*`/`check_db_export_needed`/`stop_inc`/`backup_rdb` region), **excluding** the `tool_banner` and `print_usage` bodies.

**Files:**
- Modify: `3b2/shell/nf_upgrader`

- [ ] **Step 1: List batch-A bare emits**

Run:
```bash
sed -n '1,615p' 3b2/shell/nf_upgrader \
 | sed -e '/^function tool_banner$/,/^}$/d' -e '/^function print_usage$/,/^}$/d' \
 | grep -nE '^[[:space:]]*(printf|print|echo)([[:space:]]|$)' | grep -vE '[[:space:]]>'
```

- [ ] **Step 2: Convert each listed line** per the mapping. Handle the fatal helpers explicitly:

In `fail_upgrade`:
```ksh
logfatal "Aborting upgrade"
write_state "${STATE} failed"
exit 1
```
In `exit_upgrade`:
```ksh
logfatal "Aborting upgrade"
exit 1
```
(The original `printf "Aborting upgrade, try again!\n\n"` in each becomes the `logfatal` line. `write_state`, `exit`, and all control-file writes stay exactly as they are.)

- [ ] **Step 3: Syntax + verifier**

Run: `ksh -n 3b2/shell/nf_upgrader && sh 3b2/shell/test/nf_fork_verify.sh`
Expected: `skeleton nf_upgrader == inc_upgrader` still `ok`; grammar `ok`; coverage `nf_upgrader` still `FAIL` (later batches remain). No skeleton regression.

- [ ] **Step 4: Commit**

```bash
git add 3b2/shell/nf_upgrader
git commit -m "refactor(nfupgrader): log helpers in nf_upgrader preamble + fatal paths"
```

---

## Task 8: Convert `nf_upgrader` — batch B (stage functions + stage loop)

Convert emits from line ~616 through ~1108 (`doswsstart` … `doswsretropass1`, `doswswebinstall`, `stage_new_sw`).

**Files:**
- Modify: `3b2/shell/nf_upgrader`

- [ ] **Step 1: List batch-B bare emits**

Run:
```bash
sed -n '616,1108p' 3b2/shell/nf_upgrader \
 | grep -nE '^[[:space:]]*(printf|print|echo)([[:space:]]|$)' | grep -vE '[[:space:]]>'
```

- [ ] **Step 2: Convert each listed line** per the mapping. Note the retrofit messages containing `->` (e.g. `PASS 1 retrofit steps for ${OLD_GENERIC}${OLD_LOAD} -> ${NEW_GENERIC}${NEW_LOAD}`) convert normally to `loginfo` — the `->` is preserved in the message and does not count as a redirection.

- [ ] **Step 3: Syntax + verifier**

Run: `ksh -n 3b2/shell/nf_upgrader && sh 3b2/shell/test/nf_fork_verify.sh`
Expected: `skeleton` still `ok`, `grammar` `ok`, coverage still `FAIL` (batches C/D remain).

- [ ] **Step 4: Commit**

```bash
git add 3b2/shell/nf_upgrader
git commit -m "refactor(nfupgrader): log helpers in nf_upgrader stage functions"
```

---

## Task 9: Convert `nf_upgrader` — batch C (upgrade functions + upgrade loop)

Convert emits from line ~1110 through ~1475 (`doswistart` … `doswiretropass3`, `upgrade_new_sw`, `check_gr_inprogress`).

**Files:**
- Modify: `3b2/shell/nf_upgrader`

- [ ] **Step 1: List batch-C bare emits**

Run:
```bash
sed -n '1110,1475p' 3b2/shell/nf_upgrader \
 | grep -nE '^[[:space:]]*(printf|print|echo)([[:space:]]|$)' | grep -vE '[[:space:]]>'
```

- [ ] **Step 2: Convert each listed line** per the mapping. Do **not** touch the two `sh /usr/cnc/bin/post-install.sh` lines here — they are handled in Task 11.

- [ ] **Step 3: Syntax + verifier**

Run: `ksh -n 3b2/shell/nf_upgrader && sh 3b2/shell/test/nf_fork_verify.sh`
Expected: `skeleton` `ok`, `grammar` `ok`, coverage still `FAIL` (batch D remains).

- [ ] **Step 4: Commit**

```bash
git add 3b2/shell/nf_upgrader
git commit -m "refactor(nfupgrader): log helpers in nf_upgrader upgrade functions"
```

---

## Task 10: Convert `nf_upgrader` — batch D (main body)

Convert the remaining emits from line ~1476 to end (the main execution body after the functions: arg parsing, setup, the stage/upgrade dispatch).

**Files:**
- Modify: `3b2/shell/nf_upgrader`

- [ ] **Step 1: List batch-D bare emits**

Run:
```bash
sed -n '1476,$p' 3b2/shell/nf_upgrader \
 | grep -nE '^[[:space:]]*(printf|print|echo)([[:space:]]|$)' | grep -vE '[[:space:]]>'
```

- [ ] **Step 2: Convert each listed line** per the mapping. Leave the `tool_banner` call (`tool_banner`) and the `print_usage` calls as-is — those are invocations, not emits.

- [ ] **Step 3: Syntax + verifier**

Run: `ksh -n 3b2/shell/nf_upgrader && sh 3b2/shell/test/nf_fork_verify.sh`
Expected: **`coverage nf_upgrader` now `ok`.** `skeleton` `ok`, `grammar` `ok` for both forks. Overall now only the `run_logged` wrap (Task 11) remains before full green — but note coverage/skeleton/grammar are already all `ok`, so the script prints `VERIFY OK`, exit 0.

- [ ] **Step 4: Commit**

```bash
git add 3b2/shell/nf_upgrader
git commit -m "refactor(nfupgrader): log helpers in nf_upgrader main body"
```

---

## Task 11: Wrap the curated sub-command set with `run_logged`

**Files:**
- Modify: `3b2/shell/nf_upgrader`

- [ ] **Step 1: Rediscover the wrap set (guards against drift)**

Run:
```bash
grep -nE '^[[:space:]]*(sh|rpm|/usr/cnc/(bin|mbin|ambin)/[a-z_]+)[[:space:]]' 3b2/shell/nf_upgrader \
 | grep -vE '[[:space:]]>|\|'
```
Expected: the two `sh /usr/cnc/bin/post-install.sh` lines. Wrap exactly these.

- [ ] **Step 2: Wrap them**

Change each occurrence of:
```ksh
sh /usr/cnc/bin/post-install.sh
```
to:
```ksh
run_logged post-install sh /usr/cnc/bin/post-install.sh
```

- [ ] **Step 3: Syntax + verifier**

Run: `ksh -n 3b2/shell/nf_upgrader && sh 3b2/shell/test/nf_fork_verify.sh`
Expected: `VERIFY OK`, exit 0. The skeleton check unwraps `run_logged post-install ` back to `sh /usr/cnc/bin/post-install.sh`, so equivalence still holds.

- [ ] **Step 4: Commit**

```bash
git add 3b2/shell/nf_upgrader
git commit -m "feat(nfupgrader): tag post-install output via run_logged"
```

---

## Task 12: Full verification gate + runtime grammar assertion

Everything is converted. This task adds a runtime check that a real emitted line matches the grammar (not just the static form) and locks the whole suite green.

**Files:**
- Modify: `3b2/shell/test/nf_fork_verify.sh`

- [ ] **Step 1: Add a runtime grammar assertion to the verifier**

Append before the final `echo "----"` in `nf_fork_verify.sh`:

```sh
# --- 5. Runtime grammar: sourcing the include and emitting really produces
#        the TIMESTAMP SEVERITY grammar (catches a broken date/format). ------
line=$(ksh -c '. ./nf_log.ksh; loginfo "smoke test"')
if echo "$line" | grep -qE '^[0-9]{4}-[0-9]{2}-[0-9]{2} [0-9]{2}:[0-9]{2}:[0-9]{2} INFO  smoke test$'; then
	echo "ok   runtime-grammar"
else
	echo "FAIL runtime-grammar: [$line]"; fail=1
fi
```

- [ ] **Step 2: Run the full suite**

Run: `sh 3b2/shell/test/nf_fork_verify.sh; echo "exit=$?"`
Expected: every line `ok`, `VERIFY OK`, `exit=0`.

- [ ] **Step 3: Run the unit tests too, confirm both green**

Run: `ksh 3b2/shell/test/nf_log_test.ksh && sh 3b2/shell/test/nf_fork_verify.sh && echo ALL_GREEN`
Expected: `---- 8 passed, 0 failed ----` then `VERIFY OK` then `ALL_GREEN`.

- [ ] **Step 4: Commit**

```bash
git add 3b2/shell/test/nf_fork_verify.sh
git commit -m "test(nfupgrader): assert runtime log grammar in fork verifier"
```

---

## Task 13: Manual acceptance on a lab host (documented gate, not automated)

The automated suite proves *no behavioral line changed* and *the log format is well-formed*. It cannot prove a real upgrade still works, because that needs a staged `/usr/cnc`. Close Phase 1 with a real run on a lab/test host — never a customer box.

**Files:** none (documentation of the manual gate; record results in the PR).

- [ ] **Step 1: Stage on a lab host with the fork**

On a lab host that has a valid upgrade config, run the fork exactly as the original would be run:
```
./nf_upgrade -c <config> -s
```
Watch `${INCLOGDIR}/stage_inc_<host>.<pid>`.

- [ ] **Step 2: Confirm behavior parity**

Verify: the state file `${INCLOGDIR}/.<host>_STATE` advances through `swsstart → swspatchinstall → swswebinstall → swscopycnc → swsretropass1 → swscomplete` exactly as `inc_upgrade -s` does, and the staging completes with the same result.

- [ ] **Step 3: Confirm log quality**

Verify in the trace file: every line the script itself emits carries a `TIMESTAMP SEVERITY` prefix; `post-install` output (upgrade phase) appears as `EXEC [post-install] …`; control/state files contain no prefixes (e.g. `cat ${INCLOGDIR}/.<host>_STATE` prints a bare state name). Spot-check:
```
grep -vE '^[0-9]{4}-[0-9]{2}-[0-9]{2} [0-9]{2}:[0-9]{2}:[0-9]{2} (INFO |WARN |ERROR|FATAL|EXEC )' stage_inc_<host>.<pid>
```
Expected: only child-process output lines that the scripts don't wrap (documented ceiling), never a bare line the script itself printed.

- [ ] **Step 4: Optionally run the upgrade phase**

If the lab host can take it: `./nf_upgrade -c <config> -u`, and confirm the upgrade state path and a working boot, matching `inc_upgrade -u`.

- [ ] **Step 5: Record results and open the PR**

```bash
git push -u origin <branch>
gh pr create --assignee @me --title "nfupgrader Phase 1: fork nf_upgrade/nf_upgrader + logging cleanup" \
  --body "Forks are behavior-identical to inc_upgrade/inc_upgrader (skeleton diff clean) with a consistent severity-prefixed log. Automated: nf_log unit tests + nf_fork_verify. Manual lab stage/upgrade run recorded below.

<paste lab-run observations>"
```

---

## Self-Review

**Spec coverage** (against the design doc, "The forked scripts", "Logging cleanup", "Testing", Phasing row 1):
- Fork, originals untouched → Tasks 4, 6–11; originals declared read-only.
- Drop-in behavior compatibility, equivalence as a test target → Task 5 skeleton verifier, run every conversion task; Task 13 lab parity.
- `log*` helpers → Task 2. `run_logged` status-preserving, ksh88-safe, not `cmd|sed` → Task 3. EXEC class → Tasks 3, 11.
- Emit-site conversion with severity mapping → Tasks 6–10. Fatal paths → Task 7.
- Control-file-write and banner/usage exclusions → Conversion rules + verifier allowlist + skeleton keeps redirected lines.
- Grammar assertion → Task 5 (static) + Task 12 (runtime).
- Sub-command wrap limited to unredirected-to-trace set → Task 11 with rediscovery grep.

**Placeholder scan:** no TBD/TODO; every code step shows complete code; conversion tasks give the exact work-list command plus representative mappings and the mechanical rule, and are gated by a verifier that mechanically proves completeness (coverage) and safety (skeleton) — so "convert each listed line" is unambiguous and checkable, not a hand-wave.

**Type/name consistency:** helper names `nf_log` / `loginfo` / `logwarn` / `logerror` / `logfatal` / `run_logged` used identically in the include, the tests, the verifier, and the conversion tasks. `run_logged <tag> cmd…` signature consistent in Task 3 (def), Task 11 (use), and the skeleton unwrap regex. Verifier function `B()` and `check_coverage()` defined once and reused. `nf_test_lib.ksh` asserts (`assert_eq`/`assert_match`/`assert_rc`/`nf_test_summary`) defined in Task 1, used in Tasks 2–3.

**Known ceiling (documented, not a gap):** sub-commands already redirected to side files (e.g. `rpm … >> ${TRACEFILE}.rpm`) or discarded keep their behavior and are not EXEC-tagged; their output in the trace/side files stays unprefixed. This is intentional (behavior-preserving) and surfaced in Task 13 Step 3.
