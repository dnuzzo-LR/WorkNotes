# nfupgrader Phase 2 — Local Service, Progress Record & Log Viewer Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A loopback-only Python web service, run on the host being upgraded, that launches a stage or upgrade phase via the Phase-1 forks, shows a live progress record (N states / completed / current) and a severity-filterable tail of the trace log, gated by a single-use token, with every action written to an append-only audit log.

**Architecture:** Small stdlib-only Python modules with one responsibility each, all reachable behind a testable request dispatcher. `Host.run` is the single execution seam (local now; ssh added in Phase 4). The service never steers the upgrade — it launches `nf_upgrade` detached and then only *observes*: it re-derives progress on every request from the `.${HOST}_STATE` file plus whether an `nf_upgrader` process is alive. A vanilla-JS page polls JSON endpoints.

**Tech Stack:** Python 3.6.8, **stdlib only** (`http.server`, `subprocess`, `json`, `secrets`, `os`, `re`, `unittest`). No third-party runtime or test dependencies. No f-strings (use `.format()`); `subprocess.run(..., stdout=PIPE, stderr=PIPE, universal_newlines=True)`. Repo: `~/Git/nfupgrader`.

---

## Context

- Design: `~/WorkNotes/design/2026-09-15-web-upgrade-tool-design.md`. This plan implements the **Phase 2** row plus the token/loopback auth (decisions 10, 11), which is foundational to standing up the web layer.
- Phase 1 (done) produced `nf_upgrade` / `nf_upgrader` (behavior-identical forks with a `TIMESTAMP SEVERITY message` log) and `nf_log.ksh`. Phase 2 consumes their artifacts: the trace file and the `.${HOST}_STATE` file. **Do not modify the forks.**
- Later phases: checks engine (3), dashboard + read-only peers (4), config builder (5).

## Key decisions taken in this plan

1. **Liveness without a pidfile.** `nf_upgrade` backgrounds `nf_upgrader` and exits, so the launcher never holds the child pid, and the forks are frozen (can't add a pidfile write). Run status is therefore re-derived as: terminal `STATE` → `done`; else an `nf_upgrader` process is alive (`pgrep -f`) → `running`; else `died`. This matches the design's "re-derive from the state file + liveness," with liveness = process presence. Assumes one upgrade per host at a time, which is inherent.
2. **Effective config.** The service never edits the customer's `.cfg`. It writes a derived copy that sources theirs and forces `PROMPTFORVALIDATE=NO` (so `confirm_setup` doesn't block on `read`). The original is untouched.
3. **Testable dispatcher.** All routing/auth logic lives in a pure `api.dispatch(method, path, query, body, cookies, ctx)` returning `(status, headers, body)`. `httpd.py` is a thin `BaseHTTPRequestHandler` that calls it, so the API is unit-tested without sockets.
4. **stdlib `unittest`**, not pytest — keeps "no third-party" true even for tests.

---

## File Structure

All under `~/Git/nfupgrader/`.

| Path | Responsibility |
|---|---|
| `nfupgrader/__init__.py` | Package marker. |
| `nfupgrader/host.py` | `RunResult` + `Host` (local subprocess) + `FakeHost` (canned) — the one execution seam. |
| `nfupgrader/steps.py` | Ordered state lists per phase + human labels + lookup helpers. |
| `nfupgrader/progress.py` | Pure `compute_progress(...)` + `read_progress(...)` reader over the state file and liveness. |
| `nfupgrader/site.py` | Parse `cnc.cnfg` → host inventory; identify the local host. |
| `nfupgrader/audit.py` | `AuditLog.record(event, **fields)` append-only JSONL. |
| `nfupgrader/auth.py` | `TokenAuth`: single-use token → session cookie, IP-bound. |
| `nfupgrader/launcher.py` | Build effective config, build launch argv, launch detached, discover liveness. |
| `nfupgrader/logtail.py` | Parse a trace line into `(ts, severity, msg)`; tail with an optional min-severity filter. |
| `nfupgrader/api.py` | Pure request dispatcher wiring the above into JSON endpoints. |
| `nfupgrader/httpd.py` | Thin `http.server` handler + `main()` (loopback bind, token print). |
| `nfupgrader/web/index.html` | Single page: progress panel, log panel, launch controls. |
| `nfupgrader/web/app.js` | Vanilla JS: poll progress/log, severity filter, launch. |
| `nfupgrader/web/style.css` | Minimal styling. |
| `tests/test_*.py` | stdlib `unittest` per module. |
| `run.sh` | Convenience launcher: `python3 -m nfupgrader.httpd`. |

Run all tests: `python3 -m unittest discover -s tests -p 'test_*.py' -v`

---

## Conventions (every task obeys)

- Python 3.6.8: no f-strings (`"{}".format(x)`), no walrus, no positional-only params. `subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)`.
- Timestamps: reuse the fork's format `%Y-%m-%d %H:%M:%S` where log-adjacent; audit uses ISO-8601 UTC.
- No third-party imports anywhere (runtime or test).
- Every module gets a docstring and functions get concise docstrings.

---

## Task 1: Package + test scaffolding

**Files:**
- Create: `nfupgrader/__init__.py`
- Create: `tests/__init__.py`
- Create: `run.sh`

- [ ] **Step 1: Create the package marker**

`nfupgrader/__init__.py`:
```python
"""nfupgrader — customer-facing local upgrade service (Phase 2+)."""
__all__ = []
```

- [ ] **Step 2: Create the tests package marker**

`tests/__init__.py`:
```python
```
(empty file)

- [ ] **Step 3: Create run.sh**

`run.sh`:
```sh
#!/bin/sh
# Launch the nfupgrader service (loopback only). Prints the access URL+token.
cd "$(dirname "$0")" || exit 2
exec python3 -m nfupgrader.httpd "$@"
```

- [ ] **Step 4: Verify discovery finds no tests yet but runs clean**

Run: `chmod +x run.sh; python3 -m unittest discover -s tests -p 'test_*.py'`
Expected: `Ran 0 tests`, exit 0.

- [ ] **Step 5: Commit**

```bash
git add nfupgrader/__init__.py tests/__init__.py run.sh
git commit -m "chore: python package + test scaffolding for phase 2"
```

---

## Task 2: `host.py` — the execution seam (TDD)

**Files:**
- Create: `nfupgrader/host.py`
- Create: `tests/test_host.py`

- [ ] **Step 1: Write the failing test**

`tests/test_host.py`:
```python
import unittest
from nfupgrader.host import Host, FakeHost, RunResult


class TestHost(unittest.TestCase):
    def test_local_run_captures_rc_and_stdout(self):
        r = Host().run(["sh", "-c", "echo hello; exit 0"])
        self.assertEqual(r.rc, 0)
        self.assertEqual(r.out.strip(), "hello")

    def test_local_run_captures_nonzero_and_stderr(self):
        r = Host().run(["sh", "-c", "echo oops 1>&2; exit 3"])
        self.assertEqual(r.rc, 3)
        self.assertIn("oops", r.err)

    def test_fakehost_returns_canned_result_by_argv(self):
        fake = FakeHost({("cat", "/x"): RunResult(0, "data", "")})
        r = fake.run(["cat", "/x"])
        self.assertEqual(r.out, "data")
        self.assertEqual(r.rc, 0)

    def test_fakehost_unknown_argv_defaults_to_rc1(self):
        r = FakeHost({}).run(["nope"])
        self.assertEqual(r.rc, 1)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run to verify failure**

Run: `python3 -m unittest tests.test_host -v`
Expected: FAIL — `nfupgrader.host` not importable.

- [ ] **Step 3: Implement `host.py`**

```python
"""host.py — the single execution seam. Every command the service runs goes
through a Host, so local vs remote lives in one place and tests use FakeHost."""
import collections
import subprocess

RunResult = collections.namedtuple("RunResult", ["rc", "out", "err"])


class Host(object):
    """Runs commands on the local machine (Phase 2). ssh support arrives in Phase 4."""

    def __init__(self, name="localhost"):
        self.name = name
        self.local = True

    def run(self, argv, timeout=None):
        """Run argv (a list), capture output, return a RunResult. Never raises on
        non-zero exit; a missing binary yields rc 127."""
        try:
            p = subprocess.run(
                argv,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                universal_newlines=True,
                timeout=timeout,
            )
            return RunResult(p.returncode, p.stdout, p.stderr)
        except FileNotFoundError:
            return RunResult(127, "", "not found: {}".format(argv[0]))
        except subprocess.TimeoutExpired:
            return RunResult(124, "", "timeout: {}".format(" ".join(argv)))


class FakeHost(object):
    """Test double. Maps tuple(argv) -> RunResult; unknown argv -> rc 1."""

    def __init__(self, responses=None):
        self.name = "fake"
        self.local = True
        self.responses = responses or {}
        self.calls = []

    def run(self, argv, timeout=None):
        self.calls.append(list(argv))
        return self.responses.get(tuple(argv), RunResult(1, "", "unmocked"))
```

- [ ] **Step 4: Run to verify pass**

Run: `python3 -m unittest tests.test_host -v`
Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
git add nfupgrader/host.py tests/test_host.py
git commit -m "feat: Host execution seam with FakeHost test double"
```

---

## Task 3: `steps.py` — state machine as data (TDD)

**Files:**
- Create: `nfupgrader/steps.py`
- Create: `tests/test_steps.py`

- [ ] **Step 1: Write the failing test**

`tests/test_steps.py`:
```python
import unittest
from nfupgrader import steps


class TestSteps(unittest.TestCase):
    def test_stage_has_six_states_in_order(self):
        self.assertEqual(
            steps.states_for("stage"),
            ["swsstart", "swspatchinstall", "swswebinstall",
             "swscopycnc", "swsretropass1", "swscomplete"],
        )

    def test_upgrade_has_nine_states_in_order(self):
        self.assertEqual(
            steps.states_for("upgrade"),
            ["swscomplete", "swistart", "swicopycnc", "swipackages",
             "swipreboot", "swiretropass2", "swibootinc", "postinstall", "swidone"],
        )

    def test_total(self):
        self.assertEqual(steps.total("stage"), 6)
        self.assertEqual(steps.total("upgrade"), 9)

    def test_index_of_known_state(self):
        self.assertEqual(steps.index_of("stage", "swscopycnc"), 3)

    def test_index_of_unknown_state_is_minus_one(self):
        self.assertEqual(steps.index_of("stage", "bogus"), -1)

    def test_label_is_human_readable(self):
        self.assertIn("CORE", steps.label("swscoreupgrade") + steps.label("swsstart") + steps.label("swscopycnc"))

    def test_unknown_phase_raises(self):
        with self.assertRaises(KeyError):
            steps.states_for("nope")


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run to verify failure**

Run: `python3 -m unittest tests.test_steps -v`
Expected: FAIL — module missing.

- [ ] **Step 3: Implement `steps.py`**

```python
"""steps.py — the upgrade state machine as data, mirroring nf_upgrader's
stage_new_sw / upgrade_new_sw loops. Drives the progress record."""

_STAGE = ["swsstart", "swspatchinstall", "swswebinstall",
          "swscopycnc", "swsretropass1", "swscomplete"]

_UPGRADE = ["swscomplete", "swistart", "swicopycnc", "swipackages",
            "swipreboot", "swiretropass2", "swibootinc", "postinstall", "swidone"]

_STATES = {"stage": _STAGE, "upgrade": _UPGRADE}

_LABELS = {
    "swsstart":        "Preliminary checks and CORE install",
    "swspatchinstall": "Install patch filesets",
    "swswebinstall":   "Install web GUI",
    "swscopycnc":      "Copy CORE files (pass 1)",
    "swsretropass1":   "Database retrofit (pass 1 of 3)",
    "swscomplete":     "Staging complete",
    "swistart":        "Stop application, begin upgrade",
    "swicopycnc":      "Copy CORE files (pass 2), switch links",
    "swipackages":     "Linux package updates",
    "swipreboot":      "Pre-boot preparation",
    "swiretropass2":   "Database retrofit (pass 2 of 3)",
    "swibootinc":      "Boot the application",
    "postinstall":     "Post-install steps",
    "swidone":         "Upgrade complete",
    "swscoreupgrade":  "Install CORE RPM",  # retained for label lookups
}


def states_for(phase):
    """Ordered list of states for 'stage' or 'upgrade'. KeyError if unknown."""
    return list(_STATES[phase])


def total(phase):
    """Number of states in the phase."""
    return len(_STATES[phase])


def index_of(phase, state):
    """Zero-based index of state within the phase, or -1 if not present."""
    try:
        return _STATES[phase].index(state)
    except ValueError:
        return -1


def label(state):
    """Human-readable description for a state name, or the name itself."""
    return _LABELS.get(state, state)
```

- [ ] **Step 4: Run to verify pass**

Run: `python3 -m unittest tests.test_steps -v`
Expected: PASS (7 tests).

- [ ] **Step 5: Commit**

```bash
git add nfupgrader/steps.py tests/test_steps.py
git commit -m "feat: steps.py state machine as data with labels"
```

---

## Task 4: `progress.py` — the progress record (TDD)

**Files:**
- Create: `nfupgrader/progress.py`
- Create: `tests/test_progress.py`

- [ ] **Step 1: Write the failing test**

`tests/test_progress.py`:
```python
import os
import tempfile
import unittest
from nfupgrader import progress


class TestComputeProgress(unittest.TestCase):
    def test_midway_running(self):
        p = progress.compute_progress("stage", "swscopycnc", running=True)
        self.assertEqual(p["phase"], "stage")
        self.assertEqual(p["current"], "swscopycnc")
        self.assertEqual(p["completed"], 3)   # 3 states done before swscopycnc
        self.assertEqual(p["total"], 6)
        self.assertEqual(p["status"], "running")
        self.assertIn("Copy", p["label"])

    def test_terminal_is_done_regardless_of_process(self):
        p = progress.compute_progress("stage", "swscomplete", running=False)
        self.assertEqual(p["status"], "done")
        self.assertEqual(p["completed"], 6)
        self.assertEqual(p["total"], 6)

    def test_nonterminal_no_process_is_died(self):
        p = progress.compute_progress("upgrade", "swipreboot", running=False)
        self.assertEqual(p["status"], "died")

    def test_unknown_state_reports_completed_zero(self):
        p = progress.compute_progress("stage", "weird", running=True)
        self.assertEqual(p["completed"], 0)
        self.assertEqual(p["current"], "weird")


class TestReadProgress(unittest.TestCase):
    def test_reads_state_file_and_uses_liveness(self):
        d = tempfile.mkdtemp()
        open(os.path.join(d, ".h1_STATE"), "w").write("swscopycnc\n")
        p = progress.read_progress("h1", "stage", d, running=True)
        self.assertEqual(p["current"], "swscopycnc")
        self.assertEqual(p["completed"], 3)
        self.assertEqual(p["status"], "running")

    def test_missing_state_file_is_not_started(self):
        d = tempfile.mkdtemp()
        p = progress.read_progress("h1", "stage", d, running=False)
        self.assertEqual(p["status"], "not_started")
        self.assertIsNone(p["current"])
        self.assertEqual(p["completed"], 0)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run to verify failure**

Run: `python3 -m unittest tests.test_progress -v`
Expected: FAIL — module missing.

- [ ] **Step 3: Implement `progress.py`**

```python
"""progress.py — re-derive the local upgrade progress record on every read.

The service only observes: it reads .${HOST}_STATE and a liveness boolean
(is an nf_upgrader process alive) and never remembers state itself."""
import os
from nfupgrader import steps

_TERMINAL = {"stage": "swscomplete", "upgrade": "swidone"}


def compute_progress(phase, current, running):
    """Pure: map a phase + current state + liveness to a progress dict.

    status is one of: done, running, died. (not_started is decided by the
    reader when there is no state file.)"""
    total = steps.total(phase)
    idx = steps.index_of(phase, current)
    completed = idx if idx >= 0 else 0
    if current == _TERMINAL[phase]:
        completed = total
        status = "done"
    elif running:
        status = "running"
    else:
        status = "died"
    return {
        "phase": phase,
        "current": current,
        "label": steps.label(current),
        "completed": completed,
        "total": total,
        "status": status,
    }


def read_progress(hostname, phase, inclogdir, running):
    """Read .${hostname}_STATE from inclogdir and compute progress. If the state
    file is absent, the phase has not started."""
    path = os.path.join(inclogdir, ".{}_STATE".format(hostname))
    if not os.path.exists(path):
        return {
            "phase": phase, "current": None, "label": "",
            "completed": 0, "total": steps.total(phase), "status": "not_started",
        }
    with open(path) as fh:
        current = fh.read().strip()
    return compute_progress(phase, current, running)
```

- [ ] **Step 4: Run to verify pass**

Run: `python3 -m unittest tests.test_progress -v`
Expected: PASS (6 tests).

- [ ] **Step 5: Commit**

```bash
git add nfupgrader/progress.py tests/test_progress.py
git commit -m "feat: progress.py re-derives the progress record from state file + liveness"
```

---

## Task 5: `site.py` — cnc.cnfg inventory + local host (TDD)

**Files:**
- Create: `nfupgrader/site.py`
- Create: `tests/test_site.py`
- Create: `tests/fixtures/cnc.singlebox.cnfg`
- Create: `tests/fixtures/cnc.multibox.cnfg`

- [ ] **Step 1: Create fixtures**

`tests/fixtures/cnc.singlebox.cnfg`:
```
# name num mate matenum type flags
fep1 1 fep1 1 1 0
```

`tests/fixtures/cnc.multibox.cnfg`:
```
# name num mate matenum type flags
fep1 1 fep2 2 1 0
fep2 2 fep1 1 1 0
bep1 1 bep2 2 0 0
bep2 2 bep1 1 0 0
```

- [ ] **Step 2: Write the failing test**

`tests/test_site.py`:
```python
import os
import unittest
from nfupgrader import site

FIX = os.path.join(os.path.dirname(__file__), "fixtures")


class TestSite(unittest.TestCase):
    def test_parse_multibox_counts_and_roles(self):
        text = open(os.path.join(FIX, "cnc.multibox.cnfg")).read()
        hosts = site.parse_cnfg(text)
        self.assertEqual(len(hosts), 4)
        self.assertEqual(hosts[0].name, "fep1")
        self.assertEqual(hosts[0].role, "FEP")
        self.assertEqual(hosts[0].mate, "fep2")
        self.assertEqual(hosts[2].role, "BEP")

    def test_parse_ignores_comments_and_blanks(self):
        hosts = site.parse_cnfg("# c\n\nfep1 1 fep1 1 1 0\n")
        self.assertEqual(len(hosts), 1)

    def test_local_host_match(self):
        hosts = site.parse_cnfg(open(os.path.join(FIX, "cnc.multibox.cnfg")).read())
        self.assertEqual(site.local_host(hosts, "bep2").role, "BEP")

    def test_local_host_absent_returns_none(self):
        hosts = site.parse_cnfg(open(os.path.join(FIX, "cnc.singlebox.cnfg")).read())
        self.assertIsNone(site.local_host(hosts, "elsewhere"))


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 3: Run to verify failure**

Run: `python3 -m unittest tests.test_site -v`
Expected: FAIL — module missing.

- [ ] **Step 4: Implement `site.py`**

```python
"""site.py — parse /usr/cnc/features/cnc.cnfg into a host inventory and identify
the local host. Vendored logic from nf-install's multibox_config.py.

A machine record is 6 space-separated fields:
    name  number  mate  mate_number  type  flags
where type 1 = CNC/FEP, 0 = MUX/BEP."""
import collections

HostInfo = collections.namedtuple(
    "HostInfo", ["name", "number", "mate", "mate_number", "role"]
)


def parse_cnfg(text):
    """Return a list of HostInfo from cnc.cnfg text, skipping comments/blanks."""
    hosts = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if len(parts) != 6:
            continue
        role = "FEP" if parts[4] == "1" else "BEP"
        hosts.append(HostInfo(parts[0], parts[1], parts[2], parts[3], role))
    return hosts


def local_host(hosts, hostname):
    """Return the HostInfo whose name matches hostname, or None."""
    for h in hosts:
        if h.name == hostname:
            return h
    return None


def read_site(path="/usr/cnc/features/cnc.cnfg"):
    """Read and parse cnc.cnfg from disk; [] if the file is absent."""
    try:
        with open(path) as fh:
            return parse_cnfg(fh.read())
    except (IOError, OSError):
        return []
```

- [ ] **Step 5: Run to verify pass**

Run: `python3 -m unittest tests.test_site -v`
Expected: PASS (4 tests).

- [ ] **Step 6: Commit**

```bash
git add nfupgrader/site.py tests/test_site.py tests/fixtures/cnc.singlebox.cnfg tests/fixtures/cnc.multibox.cnfg
git commit -m "feat: site.py cnc.cnfg inventory + local host identification"
```

---

## Task 6: `logtail.py` — parse and tail the trace log (TDD)

**Files:**
- Create: `nfupgrader/logtail.py`
- Create: `tests/test_logtail.py`

- [ ] **Step 1: Write the failing test**

`tests/test_logtail.py`:
```python
import os
import tempfile
import unittest
from nfupgrader import logtail


class TestParse(unittest.TestCase):
    def test_parse_well_formed(self):
        e = logtail.parse_line("2026-09-18 10:13:33 INFO  Exporting GSHM data")
        self.assertEqual(e["severity"], "INFO")
        self.assertEqual(e["msg"], "Exporting GSHM data")
        self.assertEqual(e["ts"], "2026-09-18 10:13:33")

    def test_parse_exec(self):
        e = logtail.parse_line("2026-09-18 10:13:33 EXEC  [rpm] netFLEX-CORE-5.4.0-29")
        self.assertEqual(e["severity"], "EXEC")
        self.assertEqual(e["msg"], "[rpm] netFLEX-CORE-5.4.0-29")

    def test_unmatched_line_is_info_raw(self):
        e = logtail.parse_line("legacy child output with no prefix")
        self.assertEqual(e["severity"], "INFO")
        self.assertEqual(e["msg"], "legacy child output with no prefix")
        self.assertIsNone(e["ts"])


class TestTail(unittest.TestCase):
    def _write(self):
        fd, path = tempfile.mkstemp()
        os.write(fd, b"2026-09-18 10:00:00 INFO  a\n"
                     b"2026-09-18 10:00:01 WARN  b\n"
                     b"2026-09-18 10:00:02 ERROR c\n"
                     b"2026-09-18 10:00:03 EXEC  [x] d\n")
        os.close(fd)
        return path

    def test_tail_all(self):
        entries = logtail.tail(self._write())
        self.assertEqual(len(entries), 4)

    def test_min_severity_filter(self):
        entries = logtail.tail(self._write(), min_severity="WARN")
        sevs = [e["severity"] for e in entries]
        self.assertEqual(sevs, ["WARN", "ERROR"])  # EXEC and INFO excluded

    def test_offset_returns_only_new(self):
        path = self._write()
        first = logtail.tail(path, offset=0)
        self.assertEqual(first["next_offset"] > 0, True) if isinstance(first, dict) else None
        # tail returns a list; use tail_since for offsets:
        chunk = logtail.tail_since(path, 0)
        self.assertEqual(len(chunk["entries"]), 4)
        again = logtail.tail_since(path, chunk["next_offset"])
        self.assertEqual(len(again["entries"]), 0)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run to verify failure**

Run: `python3 -m unittest tests.test_logtail -v`
Expected: FAIL — module missing.

- [ ] **Step 3: Implement `logtail.py`**

```python
"""logtail.py — parse trace lines into structured entries and tail the file
incrementally with an optional minimum-severity filter."""
import os
import re

# Severities the fork emits, plus EXEC for sub-command output. Ordered for
# min-severity filtering; EXEC is treated as below WARN (it is child output,
# not a severity) so a WARN+ filter hides it.
_ORDER = {"EXEC": 0, "INFO": 1, "WARN": 2, "ERROR": 3, "FATAL": 4}
_LINE = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) "
    r"(?P<sev>INFO|WARN|ERROR|FATAL|EXEC)\s{1,2}(?P<msg>.*)$"
)


def parse_line(line):
    """Return {ts, severity, msg} for a trace line. Unmatched lines (e.g. raw
    child output) are classified INFO with ts None."""
    m = _LINE.match(line.rstrip("\n"))
    if m:
        return {"ts": m.group("ts"), "severity": m.group("sev"), "msg": m.group("msg")}
    return {"ts": None, "severity": "INFO", "msg": line.rstrip("\n")}


def _passes(entry, min_severity):
    if not min_severity:
        return True
    return _ORDER.get(entry["severity"], 1) >= _ORDER.get(min_severity, 1)


def tail(path, min_severity=None, limit=None):
    """Return a list of parsed entries from the whole file, optionally filtered
    by minimum severity and truncated to the last `limit` entries."""
    try:
        with open(path) as fh:
            lines = fh.readlines()
    except (IOError, OSError):
        return []
    entries = [parse_line(ln) for ln in lines]
    entries = [e for e in entries if _passes(e, min_severity)]
    if limit is not None:
        entries = entries[-limit:]
    return entries


def tail_since(path, offset, min_severity=None):
    """Incremental tail: read from byte `offset`, return new entries and the new
    offset. Lets the UI poll for only what was appended."""
    try:
        size = os.path.getsize(path)
    except (IOError, OSError):
        return {"entries": [], "next_offset": 0}
    if offset > size:      # file rotated/truncated -> restart
        offset = 0
    with open(path) as fh:
        fh.seek(offset)
        data = fh.read()
        new_offset = fh.tell()
    entries = [parse_line(ln) for ln in data.splitlines()]
    entries = [e for e in entries if _passes(e, min_severity)]
    return {"entries": entries, "next_offset": new_offset}
```

- [ ] **Step 4: Run to verify pass**

Run: `python3 -m unittest tests.test_logtail -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add nfupgrader/logtail.py tests/test_logtail.py
git commit -m "feat: logtail.py trace parsing + incremental severity-filtered tail"
```

---

## Task 7: `audit.py` — append-only audit log (TDD)

**Files:**
- Create: `nfupgrader/audit.py`
- Create: `tests/test_audit.py`

- [ ] **Step 1: Write the failing test**

`tests/test_audit.py`:
```python
import json
import os
import tempfile
import unittest
from nfupgrader.audit import AuditLog


class TestAudit(unittest.TestCase):
    def test_record_appends_json_lines(self):
        path = os.path.join(tempfile.mkdtemp(), "audit.jsonl")
        a = AuditLog(path)
        a.record("launch", mode="stage", client="127.0.0.1")
        a.record("override", check="disk", reason="known-good")
        lines = open(path).read().strip().splitlines()
        self.assertEqual(len(lines), 2)
        first = json.loads(lines[0])
        self.assertEqual(first["event"], "launch")
        self.assertEqual(first["mode"], "stage")
        self.assertIn("ts", first)

    def test_record_is_appendonly_across_instances(self):
        path = os.path.join(tempfile.mkdtemp(), "audit.jsonl")
        AuditLog(path).record("a", n=1)
        AuditLog(path).record("b", n=2)
        self.assertEqual(len(open(path).read().strip().splitlines()), 2)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run to verify failure**

Run: `python3 -m unittest tests.test_audit -v`
Expected: FAIL — module missing.

- [ ] **Step 3: Implement `audit.py`**

```python
"""audit.py — append-only JSONL audit log. Every service action is recorded
here; the requirement Dan set as the condition for the auth/transport risks."""
import datetime
import json


class AuditLog(object):
    def __init__(self, path):
        self.path = path

    def record(self, event, **fields):
        """Append one JSON object: {ts, event, **fields}. ts is ISO-8601 UTC."""
        entry = {"ts": datetime.datetime.utcnow().isoformat() + "Z", "event": event}
        entry.update(fields)
        with open(self.path, "a") as fh:
            fh.write(json.dumps(entry, sort_keys=True) + "\n")
        return entry
```

- [ ] **Step 4: Run to verify pass**

Run: `python3 -m unittest tests.test_audit -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add nfupgrader/audit.py tests/test_audit.py
git commit -m "feat: audit.py append-only JSONL action log"
```

---

## Task 8: `auth.py` — single-use token + IP-bound session (TDD)

**Files:**
- Create: `nfupgrader/auth.py`
- Create: `tests/test_auth.py`

- [ ] **Step 1: Write the failing test**

`tests/test_auth.py`:
```python
import unittest
from nfupgrader.auth import TokenAuth


class TestAuth(unittest.TestCase):
    def test_token_redeems_once_into_a_session(self):
        a = TokenAuth(token="TOK")
        sid = a.redeem("TOK", "127.0.0.1")
        self.assertIsNotNone(sid)
        self.assertTrue(a.valid_session(sid, "127.0.0.1"))

    def test_token_is_single_use(self):
        a = TokenAuth(token="TOK")
        a.redeem("TOK", "127.0.0.1")
        self.assertIsNone(a.redeem("TOK", "127.0.0.1"))

    def test_wrong_token_rejected(self):
        a = TokenAuth(token="TOK")
        self.assertIsNone(a.redeem("NOPE", "127.0.0.1"))

    def test_session_is_ip_bound(self):
        a = TokenAuth(token="TOK")
        sid = a.redeem("TOK", "127.0.0.1")
        self.assertFalse(a.valid_session(sid, "10.0.0.9"))

    def test_unknown_session_invalid(self):
        a = TokenAuth(token="TOK")
        self.assertFalse(a.valid_session("bogus", "127.0.0.1"))

    def test_autogenerated_token_is_long(self):
        a = TokenAuth()
        self.assertGreaterEqual(len(a.token), 16)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run to verify failure**

Run: `python3 -m unittest tests.test_auth -v`
Expected: FAIL — module missing.

- [ ] **Step 3: Implement `auth.py`**

```python
"""auth.py — single-use token exchanged for an IP-bound session cookie.

The token is the whole credential (decision 10); loopback binding (decision 11)
keeps it off the network. Redemption is one-shot; the session id is then required
and is tied to the client IP."""
import secrets


class TokenAuth(object):
    def __init__(self, token=None):
        self.token = token or secrets.token_urlsafe(24)
        self._redeemed = False
        self._sessions = {}   # sid -> client_ip

    def redeem(self, token, client_ip):
        """Exchange the (single-use) token for a new session id, or None."""
        if self._redeemed or token != self.token:
            return None
        self._redeemed = True
        sid = secrets.token_urlsafe(24)
        self._sessions[sid] = client_ip
        return sid

    def valid_session(self, sid, client_ip):
        """True iff sid is known and bound to this client_ip."""
        return self._sessions.get(sid) == client_ip
```

- [ ] **Step 4: Run to verify pass**

Run: `python3 -m unittest tests.test_auth -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add nfupgrader/auth.py tests/test_auth.py
git commit -m "feat: auth.py single-use token + IP-bound session"
```

---

## Task 9: `launcher.py` — effective config, argv, detach, liveness (TDD)

**Files:**
- Create: `nfupgrader/launcher.py`
- Create: `tests/test_launcher.py`

- [ ] **Step 1: Write the failing test**

`tests/test_launcher.py`:
```python
import os
import tempfile
import unittest
from nfupgrader import launcher
from nfupgrader.host import FakeHost, RunResult


class TestLauncher(unittest.TestCase):
    def test_build_argv_stage(self):
        argv = launcher.build_launch_argv("/opt/nfupgrader/nf_upgrade", "/tmp/eff.cfg", "stage")
        self.assertEqual(argv, ["/opt/nfupgrader/nf_upgrade", "-c", "/tmp/eff.cfg", "-s"])

    def test_build_argv_upgrade(self):
        argv = launcher.build_launch_argv("/x/nf_upgrade", "/x/eff.cfg", "upgrade")
        self.assertEqual(argv[-1], "-u")

    def test_build_argv_rejects_bad_mode(self):
        with self.assertRaises(ValueError):
            launcher.build_launch_argv("/x/nf_upgrade", "/x/eff.cfg", "sideways")

    def test_effective_config_sources_original_and_forces_no_prompt(self):
        src = os.path.join(tempfile.mkdtemp(), "user.cfg")
        open(src, "w").write('INCLOGDIR="/usr/cnc/logs"\nNEW_GENERIC="54.1"\n')
        eff = launcher.write_effective_config(src, os.path.join(tempfile.mkdtemp(), "eff.cfg"))
        body = open(eff).read()
        self.assertIn('. "{}"'.format(src), body)
        self.assertIn("PROMPTFORVALIDATE=NO", body)

    def test_liveness_true_when_pgrep_finds_process(self):
        fake = FakeHost({("pgrep", "-f", "nf_upgrader"): RunResult(0, "4321\n", "")})
        self.assertTrue(launcher.upgrader_running(fake))

    def test_liveness_false_when_pgrep_empty(self):
        fake = FakeHost({("pgrep", "-f", "nf_upgrader"): RunResult(1, "", "")})
        self.assertFalse(launcher.upgrader_running(fake))


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run to verify failure**

Run: `python3 -m unittest tests.test_launcher -v`
Expected: FAIL — module missing.

- [ ] **Step 3: Implement `launcher.py`**

```python
"""launcher.py — launch the local upgrade via the Phase-1 forks and observe it.

We never edit the customer's config; we write a derived config that sources it
and forces PROMPTFORVALIDATE=NO (so confirm_setup does not block on read). The
launch is detached (nf_upgrade backgrounds nf_upgrader and returns); liveness is
discovered by process presence, since we never hold the child pid."""
import os
import subprocess

_MODE_FLAG = {"stage": "-s", "upgrade": "-u"}


def build_launch_argv(nf_upgrade_path, config_path, mode):
    """Return the argv to launch nf_upgrade for the given mode."""
    if mode not in _MODE_FLAG:
        raise ValueError("bad mode: {}".format(mode))
    return [nf_upgrade_path, "-c", config_path, _MODE_FLAG[mode]]


def write_effective_config(user_config, effective_path):
    """Write a derived config that sources the user's config and forces
    PROMPTFORVALIDATE=NO. Returns effective_path. The user's file is untouched."""
    with open(effective_path, "w") as fh:
        fh.write("# generated by nfupgrader; do not edit\n")
        fh.write('. "{}"\n'.format(user_config))
        fh.write("PROMPTFORVALIDATE=NO\n")
    return effective_path


def upgrader_running(host):
    """True iff an nf_upgrader process is alive on the host."""
    return host.run(["pgrep", "-f", "nf_upgrader"]).rc == 0


def launch(nf_upgrade_path, config_path, mode, cwd=None):
    """Launch nf_upgrade detached. Returns the Popen (which exits quickly, as
    nf_upgrade backgrounds nf_upgrader). Progress is tracked via the state file
    and upgrader_running(), not this handle."""
    argv = build_launch_argv(nf_upgrade_path, config_path, mode)
    return subprocess.Popen(
        argv, cwd=cwd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
```

- [ ] **Step 4: Run to verify pass**

Run: `python3 -m unittest tests.test_launcher -v`
Expected: PASS (6 tests; `launch()` itself is exercised in Task 12 integration, not unit-tested).

- [ ] **Step 5: Commit**

```bash
git add nfupgrader/launcher.py tests/test_launcher.py
git commit -m "feat: launcher.py effective config, launch argv, detach, liveness"
```

---

## Task 10: `api.py` — pure request dispatcher (TDD)

**Files:**
- Create: `nfupgrader/api.py`
- Create: `tests/test_api.py`

The dispatcher is pure: it receives request parts + a context object holding the
collaborators and returns `(status, headers, body_bytes)`. This is where auth
gating, routing, and JSON shaping live — all unit-testable without sockets.

- [ ] **Step 1: Write the failing test**

`tests/test_api.py`:
```python
import json
import os
import tempfile
import unittest
from nfupgrader import api
from nfupgrader.auth import TokenAuth
from nfupgrader.audit import AuditLog
from nfupgrader.host import FakeHost, RunResult


def make_ctx(tmp, running=True):
    host = FakeHost({("pgrep", "-f", "nf_upgrader"): RunResult(0 if running else 1, "", "")})
    open(os.path.join(tmp, ".h1_STATE"), "w").write("swscopycnc\n")
    open(os.path.join(tmp, "trace"), "w").write("2026-09-18 10:00:00 INFO  hi\n")
    return api.Context(
        host=host, hostname="h1", inclogdir=tmp, phase="stage",
        tracefile=os.path.join(tmp, "trace"),
        auth=TokenAuth(token="TOK"), audit=AuditLog(os.path.join(tmp, "audit.jsonl")),
        webdir=tmp, nf_upgrade_path="/x/nf_upgrade",
    )


class TestApi(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.ctx = make_ctx(self.tmp)

    def _session(self):
        return self.ctx.auth.redeem("TOK", "127.0.0.1")

    def test_progress_requires_session(self):
        status, _, _ = api.dispatch("GET", "/api/progress", {}, b"", {}, "127.0.0.1", self.ctx)
        self.assertEqual(status, 401)

    def test_progress_with_session_returns_record(self):
        sid = self._session()
        status, _, body = api.dispatch(
            "GET", "/api/progress", {}, b"", {"nfu_sid": sid}, "127.0.0.1", self.ctx)
        self.assertEqual(status, 200)
        data = json.loads(body.decode())
        self.assertEqual(data["current"], "swscopycnc")
        self.assertEqual(data["completed"], 3)
        self.assertEqual(data["status"], "running")

    def test_log_endpoint_filters_and_paginates(self):
        sid = self._session()
        status, _, body = api.dispatch(
            "GET", "/api/log", {"offset": ["0"]}, b"", {"nfu_sid": sid}, "127.0.0.1", self.ctx)
        self.assertEqual(status, 200)
        data = json.loads(body.decode())
        self.assertIn("entries", data)
        self.assertIn("next_offset", data)

    def test_redeem_sets_cookie_and_redirects(self):
        status, headers, _ = api.dispatch(
            "GET", "/auth", {"token": ["TOK"]}, b"", {}, "127.0.0.1", self.ctx)
        self.assertIn(status, (302, 303))
        self.assertTrue(any(h[0].lower() == "set-cookie" for h in headers))

    def test_redeem_bad_token_401(self):
        status, _, _ = api.dispatch(
            "GET", "/auth", {"token": ["NOPE"]}, b"", {}, "127.0.0.1", self.ctx)
        self.assertEqual(status, 401)

    def test_launch_records_audit_and_needs_session(self):
        status, _, _ = api.dispatch(
            "POST", "/api/launch", {}, json.dumps({"mode": "stage"}).encode(),
            {}, "127.0.0.1", self.ctx)
        self.assertEqual(status, 401)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run to verify failure**

Run: `python3 -m unittest tests.test_api -v`
Expected: FAIL — module missing.

- [ ] **Step 3: Implement `api.py`**

```python
"""api.py — pure request dispatcher. Holds routing, auth gating, and JSON
shaping so the HTTP layer (httpd.py) stays a thin adapter and the API is
unit-testable without sockets."""
import collections
import json

from nfupgrader import progress, logtail, launcher

Context = collections.namedtuple("Context", [
    "host", "hostname", "inclogdir", "phase", "tracefile",
    "auth", "audit", "webdir", "nf_upgrade_path",
])


def _json(status, obj):
    body = json.dumps(obj).encode("utf-8")
    return status, [("Content-Type", "application/json")], body


def _needs_session(cookies, client_ip, ctx):
    sid = cookies.get("nfu_sid")
    return bool(sid) and ctx.auth.valid_session(sid, client_ip)


def dispatch(method, path, query, body, cookies, client_ip, ctx):
    """Return (status, headers, body_bytes) for a request."""
    # --- auth redemption (no session required) ---
    if path == "/auth" and method == "GET":
        token = (query.get("token") or [""])[0]
        sid = ctx.auth.redeem(token, client_ip)
        if not sid:
            ctx.audit.record("auth_fail", client=client_ip)
            return _json(401, {"error": "invalid or spent token"})
        ctx.audit.record("auth_ok", client=client_ip)
        headers = [
            ("Set-Cookie", "nfu_sid={}; HttpOnly; Path=/; SameSite=Strict".format(sid)),
            ("Location", "/"),
        ]
        return 303, headers, b""

    # --- everything else requires a valid session ---
    if not _needs_session(cookies, client_ip, ctx):
        return _json(401, {"error": "authentication required"})

    if path == "/api/progress" and method == "GET":
        running = launcher.upgrader_running(ctx.host)
        rec = progress.read_progress(ctx.hostname, ctx.phase, ctx.inclogdir, running)
        return _json(200, rec)

    if path == "/api/log" and method == "GET":
        offset = int((query.get("offset") or ["0"])[0])
        min_sev = (query.get("min") or [None])[0]
        chunk = logtail.tail_since(ctx.tracefile, offset, min_severity=min_sev)
        return _json(200, chunk)

    if path == "/api/launch" and method == "POST":
        try:
            payload = json.loads(body.decode("utf-8") or "{}")
        except ValueError:
            return _json(400, {"error": "bad json"})
        mode = payload.get("mode")
        if mode not in ("stage", "upgrade"):
            return _json(400, {"error": "mode must be stage or upgrade"})
        ctx.audit.record("launch", mode=mode, client=client_ip)
        # The httpd layer performs the actual launch (side effect); the
        # dispatcher records intent and returns accepted. See httpd.on_launch.
        return _json(202, {"accepted": True, "mode": mode})

    return _json(404, {"error": "not found"})
```

Note: `/api/launch` records intent and returns 202; the actual `launcher.launch()` side effect is wired in `httpd.py` (Task 11) so the pure dispatcher stays free of process spawning. The audit entry is the testable contract.

- [ ] **Step 4: Run to verify pass**

Run: `python3 -m unittest tests.test_api -v`
Expected: PASS (6 tests).

- [ ] **Step 5: Commit**

```bash
git add nfupgrader/api.py tests/test_api.py
git commit -m "feat: api.py pure request dispatcher with auth gating"
```

---

## Task 11: `httpd.py` — thin HTTP adapter + main (server bootstrap)

**Files:**
- Create: `nfupgrader/httpd.py`
- Create: `tests/test_httpd.py`

- [ ] **Step 1: Write the failing test (cookie + query parsing helpers)**

`tests/test_httpd.py`:
```python
import unittest
from nfupgrader import httpd


class TestHelpers(unittest.TestCase):
    def test_parse_cookies(self):
        self.assertEqual(httpd.parse_cookies("nfu_sid=abc; other=1")["nfu_sid"], "abc")

    def test_parse_cookies_empty(self):
        self.assertEqual(httpd.parse_cookies(""), {})

    def test_client_ip_prefers_socket(self):
        self.assertEqual(httpd.client_ip_from(("10.1.1.9", 5555)), "10.1.1.9")


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run to verify failure**

Run: `python3 -m unittest tests.test_httpd -v`
Expected: FAIL — module missing.

- [ ] **Step 3: Implement `httpd.py`**

```python
"""httpd.py — thin http.server adapter over api.dispatch, plus main() that binds
loopback and prints the one-time access URL. Serves static files from web/."""
import os
import posixpath
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse, parse_qs

from nfupgrader import api, launcher
from nfupgrader.auth import TokenAuth
from nfupgrader.audit import AuditLog
from nfupgrader.host import Host

_CTYPES = {".html": "text/html", ".js": "application/javascript",
           ".css": "text/css"}


def parse_cookies(header):
    """Parse a Cookie header into a dict."""
    out = {}
    for part in (header or "").split(";"):
        part = part.strip()
        if "=" in part:
            k, v = part.split("=", 1)
            out[k.strip()] = v.strip()
    return out


def client_ip_from(client_address):
    """Extract the client IP from a (host, port) tuple."""
    return client_address[0]


def make_handler(ctx):
    class Handler(BaseHTTPRequestHandler):
        def _serve_static(self, path):
            name = "index.html" if path in ("/", "") else path.lstrip("/")
            safe = posixpath.normpath(name)
            if safe.startswith("..") or safe.startswith("/"):
                self.send_error(403); return
            full = os.path.join(ctx.webdir, safe)
            if not os.path.isfile(full):
                self.send_error(404); return
            ext = os.path.splitext(full)[1]
            with open(full, "rb") as fh:
                data = fh.read()
            self.send_response(200)
            self.send_header("Content-Type", _CTYPES.get(ext, "application/octet-stream"))
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _handle(self, method):
            parsed = urlparse(self.path)
            path = parsed.path
            # Static assets and the app shell are served directly.
            if method == "GET" and (path == "/" or path.startswith("/web/")
                                    or path.endswith((".js", ".css", ".html"))):
                self._serve_static(path if path != "/" else "/index.html")
                return
            query = parse_qs(parsed.query)
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length) if length else b""
            cookies = parse_cookies(self.headers.get("Cookie", ""))
            ip = client_ip_from(self.client_address)
            status, headers, out = api.dispatch(method, path, query, body, cookies, ip, ctx)
            # Perform the launch side effect after a recorded 202.
            if path == "/api/launch" and status == 202:
                import json as _json
                mode = _json.loads(body.decode() or "{}").get("mode")
                launcher.launch(ctx.nf_upgrade_path, ctx.effective_config, mode, cwd=ctx.webdir)
            self.send_response(status)
            for k, v in headers:
                self.send_header(k, v)
            self.send_header("Content-Length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)

        def do_GET(self):
            self._handle("GET")

        def do_POST(self):
            self._handle("POST")

        def log_message(self, *a):
            pass  # silence default stderr logging; we have the audit log

    return Handler


def build_context(webdir, inclogdir, hostname, phase, tracefile,
                  nf_upgrade_path, effective_config):
    ctx = api.Context(
        host=Host(), hostname=hostname, inclogdir=inclogdir, phase=phase,
        tracefile=tracefile, auth=TokenAuth(),
        audit=AuditLog(os.path.join(inclogdir, "nfupgrader_audit.jsonl")),
        webdir=webdir, nf_upgrade_path=nf_upgrade_path,
    )
    # httpd needs the effective-config path for the launch side effect; attach it.
    return ctx._replace() , ctx  # placeholder; replaced below


def main(argv=None):
    """Bind 127.0.0.1 on an ephemeral (or $NFU_PORT) port and print the URL+token."""
    here = os.path.dirname(os.path.abspath(__file__))
    webdir = os.path.join(here, "web")
    inclogdir = os.environ.get("INCLOGDIR", "/usr/cnc/logs")
    hostname = os.uname()[1]
    phase = os.environ.get("NFU_PHASE", "stage")
    tracefile = os.environ.get("NFU_TRACEFILE",
                               os.path.join(inclogdir, "{}_inc_{}".format(phase, hostname)))
    nf_upgrade_path = os.path.join(here, "..", "nf_upgrade")
    effective_config = os.environ.get("NFU_EFFECTIVE_CONFIG",
                                      os.path.join(inclogdir, "nfupgrader_effective.cfg"))

    ctx = api.Context(
        host=Host(), hostname=hostname, inclogdir=inclogdir, phase=phase,
        tracefile=tracefile, auth=TokenAuth(),
        audit=AuditLog(os.path.join(inclogdir, "nfupgrader_audit.jsonl")),
        webdir=webdir, nf_upgrade_path=nf_upgrade_path,
    )
    # Attach the effective-config path used by the launch side effect.
    ctx = _ContextWithConfig(ctx, effective_config)

    port = int(os.environ.get("NFU_PORT", "0"))
    server = HTTPServer(("127.0.0.1", port), make_handler(ctx))
    real_port = server.server_address[1]
    url = "http://127.0.0.1:{}/auth?token={}".format(real_port, ctx.auth.token)
    print("nfupgrader listening on 127.0.0.1:{}".format(real_port))
    print("Open (single-use):\n    {}".format(url))
    print("From your workstation, tunnel first, e.g.:")
    print("    ssh -L {p}:127.0.0.1:{p} <this-host>   then browse the URL above".format(p=real_port))
    sys.stdout.flush()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.shutdown()


class _ContextWithConfig(object):
    """Wrap Context to add effective_config without breaking the namedtuple API."""
    def __init__(self, ctx, effective_config):
        self._ctx = ctx
        self.effective_config = effective_config

    def __getattr__(self, name):
        return getattr(self._ctx, name)


if __name__ == "__main__":
    main()
```

Note: `Context` is a namedtuple (immutable, clean for tests); `httpd` needs one extra field (`effective_config`) only for the launch side effect, so `_ContextWithConfig` decorates it without polluting the pure API. `build_context` above is unused scaffolding — delete it before committing (kept here only to show the reasoning); `main` constructs the context directly.

- [ ] **Step 4: Remove the unused `build_context` stub**

Delete the `build_context` function from `httpd.py` (it was illustrative). Confirm nothing references it: `grep -n build_context nfupgrader/httpd.py` → no matches after deletion.

- [ ] **Step 5: Run helper tests + import check**

Run: `python3 -m unittest tests.test_httpd -v && python3 -c "import nfupgrader.httpd"`
Expected: PASS (3 tests) and a clean import.

- [ ] **Step 6: Commit**

```bash
git add nfupgrader/httpd.py tests/test_httpd.py
git commit -m "feat: httpd.py thin http.server adapter + loopback main()"
```

---

## Task 12: Web UI (progress + severity-filtered log + launch)

**Files:**
- Create: `nfupgrader/web/index.html`
- Create: `nfupgrader/web/style.css`
- Create: `nfupgrader/web/app.js`

The UI is validated manually (Task 13); no unit tests. Keep it vanilla — no framework, no CDN.

- [ ] **Step 1: `index.html`**

```html
<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>netFLEX Upgrade</title>
  <link rel="stylesheet" href="/style.css">
</head>
<body>
  <h1>netFLEX Upgrade &mdash; <span id="host">this host</span></h1>

  <section id="progress">
    <h2>Progress</h2>
    <div id="phase-line">Phase: <b id="phase">-</b> &middot; Status: <b id="status">-</b></div>
    <div class="bar"><div id="bar-fill"></div></div>
    <div id="counter">0 / 0</div>
    <div id="current">-</div>
  </section>

  <section id="controls">
    <h2>Launch</h2>
    <button id="btn-stage">Start Staging</button>
    <button id="btn-upgrade">Start Upgrade</button>
    <span id="launch-msg"></span>
  </section>

  <section id="logs">
    <h2>Log</h2>
    <label>Show:
      <select id="min-sev">
        <option value="">All</option>
        <option value="INFO">INFO+</option>
        <option value="WARN">WARN+</option>
        <option value="ERROR">ERROR+</option>
        <option value="FATAL">FATAL only-ish</option>
      </select>
    </label>
    <pre id="log"></pre>
  </section>

  <script src="/app.js"></script>
</body>
</html>
```

- [ ] **Step 2: `style.css`**

```css
body { font-family: system-ui, sans-serif; margin: 2rem; max-width: 900px; }
h1 { font-size: 1.3rem; }
section { border: 1px solid #ccc; border-radius: 6px; padding: 1rem; margin: 1rem 0; }
.bar { background: #eee; height: 18px; border-radius: 9px; overflow: hidden; }
#bar-fill { background: #2d6a4f; height: 100%; width: 0; transition: width .3s; }
#counter { font-weight: bold; margin-top: .3rem; }
button { padding: .5rem 1rem; margin-right: .5rem; cursor: pointer; }
#log { background: #111; color: #ddd; padding: .75rem; height: 320px;
       overflow-y: auto; white-space: pre-wrap; font-size: 12px; }
.sev-WARN { color: #e9c46a; } .sev-ERROR { color: #e76f51; }
.sev-FATAL { color: #ff4d4d; font-weight: bold; } .sev-EXEC { color: #888; }
</style>
```
(Save as CSS — drop the trailing `</style>` if the editor added it; this file is pure CSS.)

- [ ] **Step 3: `app.js`**

```javascript
"use strict";
var logOffset = 0;

function getJSON(url, cb) {
  var x = new XMLHttpRequest();
  x.open("GET", url, true);
  x.onreadystatechange = function () {
    if (x.readyState === 4 && x.status === 200) cb(JSON.parse(x.responseText));
  };
  x.send();
}

function postJSON(url, obj, cb) {
  var x = new XMLHttpRequest();
  x.open("POST", url, true);
  x.setRequestHeader("Content-Type", "application/json");
  x.onreadystatechange = function () {
    if (x.readyState === 4) cb(x.status, x.responseText ? JSON.parse(x.responseText) : {});
  };
  x.send(JSON.stringify(obj));
}

function refreshProgress() {
  getJSON("/api/progress", function (p) {
    document.getElementById("phase").textContent = p.phase;
    document.getElementById("status").textContent = p.status;
    document.getElementById("current").textContent =
      (p.current || "-") + (p.label ? " — " + p.label : "");
    document.getElementById("counter").textContent = p.completed + " / " + p.total;
    var pct = p.total ? Math.round((p.completed / p.total) * 100) : 0;
    document.getElementById("bar-fill").style.width = pct + "%";
  });
}

function refreshLog() {
  var min = document.getElementById("min-sev").value;
  getJSON("/api/log?offset=" + logOffset + (min ? "&min=" + min : ""), function (d) {
    logOffset = d.next_offset;
    var pre = document.getElementById("log");
    d.entries.forEach(function (e) {
      var line = document.createElement("div");
      line.className = "sev-" + e.severity;
      line.textContent = (e.ts ? e.ts + " " : "") + e.severity + "  " + e.msg;
      pre.appendChild(line);
    });
    pre.scrollTop = pre.scrollHeight;
  });
}

function launch(mode) {
  postJSON("/api/launch", { mode: mode }, function (status, body) {
    document.getElementById("launch-msg").textContent =
      status === 202 ? ("Launched " + mode + "…") : ("Error: " + (body.error || status));
  });
}

document.getElementById("btn-stage").onclick = function () { launch("stage"); };
document.getElementById("btn-upgrade").onclick = function () { launch("upgrade"); };
document.getElementById("min-sev").onchange = function () {
  logOffset = 0;
  document.getElementById("log").innerHTML = "";
  refreshLog();
};

refreshProgress();
refreshLog();
setInterval(refreshProgress, 2000);
setInterval(refreshLog, 2000);
```

- [ ] **Step 4: Sanity — files exist and reference the endpoints**

Run: `grep -q "/api/progress" nfupgrader/web/app.js && grep -q "/api/log" nfupgrader/web/app.js && grep -q "/api/launch" nfupgrader/web/app.js && echo WIRED`
Expected: `WIRED`.

- [ ] **Step 5: Commit**

```bash
git add nfupgrader/web/index.html nfupgrader/web/style.css nfupgrader/web/app.js
git commit -m "feat: vanilla web UI — progress record, severity log, launch"
```

---

## Task 13: Full test run + manual acceptance

**Files:** none (verification + manual gate).

- [ ] **Step 1: Run the whole suite**

Run: `python3 -m unittest discover -s tests -p 'test_*.py' -v`
Expected: all tests PASS, exit 0. Record the count.

- [ ] **Step 2: Smoke-launch the server against a fake trace/state (no real upgrade)**

```bash
mkdir -p /tmp/nfu && printf 'swscopycnc\n' > /tmp/nfu/.$(hostname)_STATE
printf '2026-09-18 10:00:00 INFO  smoke\n2026-09-18 10:00:01 WARN  careful\n' > /tmp/nfu/stage_inc_$(hostname)
INCLOGDIR=/tmp/nfu NFU_PORT=8099 NFU_TRACEFILE=/tmp/nfu/stage_inc_$(hostname) \
  python3 -m nfupgrader.httpd &
sleep 1
# grab the token from the printed URL, then:
curl -s "http://127.0.0.1:8099/auth?token=<TOKEN>" -i | grep -i set-cookie
```
Expected: a `Set-Cookie: nfu_sid=…` header and a 303 redirect. Then, reusing the cookie, `GET /api/progress` returns `"current":"swscopycnc","completed":3,"status":...` and `GET /api/log` returns the two entries. Kill the server after.

- [ ] **Step 3: Manual browser acceptance (tunnel)**

From a workstation: `ssh -L 8099:127.0.0.1:8099 <lab-host>`, open the printed `/auth?token=…` URL. Confirm: the token redeems once (a second use of the same URL fails), the progress bar/counter reflect the state file, and the log panel tails with the severity filter working. Do **not** click Launch unless on a lab host prepared for a real stage.

- [ ] **Step 4: Optional real launch on a lab host**

On a lab host with a valid config: set `NFU_EFFECTIVE_CONFIG` via the effective-config path, click **Start Staging**, and confirm the progress record advances through the stage states and the log tails live. This is the same manual gate as Phase 1 Task 13, now driven from the UI.

- [ ] **Step 5: Commit any fixups and tag the phase**

```bash
git add -A && git commit -m "test: phase 2 full-suite + smoke verification notes" || true
```

---

## Self-Review

**Spec coverage** (design Phase-2 row + auth decisions 10/11):
- `host.py`/`site.py`/`steps.py`/`progress.py` → Tasks 2–5. `progress.py` re-derives from state file + liveness (design's observe-only model).
- Launch a phase → `launcher.py` (Task 9) + httpd launch side effect (Task 11).
- Live output + severity-filtering log viewer → `logtail.py` (Task 6) + `/api/log` (Task 10) + UI select (Task 12).
- Progress record (N/completed/current) → `progress.py` + `/api/progress` + UI bar/counter.
- Audit log → `audit.py` (Task 7), recorded on auth + launch in `api.py`.
- Token/loopback auth → `auth.py` (Task 8), gated in `api.py`, loopback bind + tunnel hint in `httpd.main` (Task 11).

**Placeholder scan:** every code step has complete code. The one illustrative stub (`build_context` in Task 11) is explicitly deleted in Task 11 Step 4, with a grep check. No TBD/TODO.

**Type/name consistency:** `RunResult(rc,out,err)` and `Host.run(argv)` used identically across host/launcher/api tests. `Context` fields (`host, hostname, inclogdir, phase, tracefile, auth, audit, webdir, nf_upgrade_path`) match between `api.Context`, the api tests' `make_ctx`, and `httpd.main`. `progress.read_progress(hostname, phase, inclogdir, running)` signature matches its test and the `api` call site. `logtail.tail_since(path, offset, min_severity=None)` returns `{entries, next_offset}` consistently in module, test, api, and app.js. `auth.redeem(token, ip)` / `valid_session(sid, ip)` consistent across auth test and api. Cookie name `nfu_sid` consistent in api, httpd, tests.

**Python 3.6 compliance:** no f-strings (all `.format()`); `subprocess.run(..., universal_newlines=True)`; `secrets` and `http.server`/`urllib.parse` are 3.6 stdlib; `subprocess.DEVNULL` and `start_new_session` are 3.6-valid.

**Known deferrals (not gaps):** peer/site display and ssh in `Host` → Phase 4; checks → Phase 3; config builder UI → Phase 5 (Phase 2 uses a provided config via the effective-config wrapper). Liveness by process-presence (not pid) is a deliberate decision documented above.
