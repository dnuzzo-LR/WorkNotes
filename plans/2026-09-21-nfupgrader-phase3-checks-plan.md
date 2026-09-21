# nfupgrader Phase 3 — Checks Engine Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Run **pre-flight** checks before a launch (an ERROR blocks the Launch button; the customer may override with a typed reason that is logged) and **post-flight** checks after a phase completes (advisory only). Checks are JSON-defined for common kinds plus a few coded ones, each evaluated through the `Host.run` seam.

**Architecture:** A `checks.py` module holds a registry of check *kinds* (each a small evaluator `fn(host, params) -> (passed, detail)`), a loader for a JSON check list, and `run_checks(host, checks, when, phase, machtype)` that filters by binding and evaluates. The API gains `GET /api/checks` (run and report) and gates `POST /api/launch` on pre-flight ERRORs unless an override+reason is supplied (recorded in the audit log). The UI shows a checks panel, disables Launch on blocking failures, offers an override box, and shows post-flight results when the phase finishes.

**Tech Stack:** Python 3.6.8, stdlib only (`json`, `re`), `unittest`. Repo: `~/Git/nfupgrader`.

---

## Context

- Design: `~/WorkNotes/design/2026-09-15-web-upgrade-tool-design.md` — "Checks engine": binding (`preflight`/`postflight`, phase, optional `MACHTYPE`), JSON kinds (rpm package, touchfile, system define, service status, disk space, kernel param, limits.conf, file exists, symlink target, command exit status, command output match), coded checks (staged-vs-installed, retrofit-log scan, dbcheck error count), and the failure policy (pre-flight ERROR blocks Launch, override typed + logged; WARNING advisory; post-flight advisory).
- Builds on Phase 2/4: reuses `Host.run`, `audit.AuditLog`, `site` (for MACHTYPE), and `localinfo` parsers (`parse_df`, `parse_dbcheck_errors`) where useful.
- Scope note: this phase implements a solid **core** kind set (below). `limits.conf` and `system_define` are expressible via `command_output_match` for now and can get dedicated kinds later; that's a documented deferral, not a gap.

## Kind set (this phase)

| kind | params | passes when |
|---|---|---|
| `rpm_installed` | `{pkg}` | `rpm -q <pkg>` rc 0 |
| `file_exists` | `{path}` | `test -f <path>` rc 0 |
| `file_absent` | `{path}` | `test -f <path>` rc != 0 |
| `symlink_target` | `{path, contains?}` | `readlink <path>` rc 0 (and output contains `contains` if given) |
| `command_status` | `{argv, expect_rc?}` | `argv` rc == `expect_rc` (default 0) |
| `command_output_match` | `{argv, pattern, negate?}` | regex `pattern` found in output (absent if `negate`) |
| `disk_space_min` | `{mount, min_gb}` | `df -kP` shows ≥ `min_gb` free on `mount` |
| `service_active` | `{name}` | `systemctl is-active <name>` rc 0 |
| `dbcheck_clean` | `{}` | `dbcheck -AV` reports 0 ERROR lines |
| `staged_present` | `{}` | `readlink /usr/cnc_stage` rc 0 and non-empty |

Each evaluator runs only through `host.run` (or a pure parser on its output), so a check behaves identically wherever the host points.

---

## File Structure

| Path | Responsibility |
|---|---|
| `nfupgrader/checks.py` | Kind registry, `load_checks(path)`, `run_checks(...)`, `blocking_failures(results)`. |
| `nfupgrader/checks.default.json` | Starter check list (a few pre/post-flight entries). |
| `nfupgrader/api.py` | `GET /api/checks`; gate `POST /api/launch` on pre-flight ERRORs + override. |
| `nfupgrader/httpd.py` | `checks_path` Context field (env `NFU_CHECKS`, default bundled JSON). |
| `nfupgrader/web/index.html` / `app.js` / `style.css` | Checks panel, Launch gating, override box, post-flight results. |
| `tests/test_checks.py`, `tests/test_api.py` | Tests. |

Run tests: `python3 -m unittest discover -s tests -p 'test_*.py'`

---

## Task 1: `checks.py` kind evaluators (TDD)

**Files:**
- Create: `nfupgrader/checks.py`
- Create: `tests/test_checks.py`

- [ ] **Step 1: Write the failing tests**

`tests/test_checks.py`:
```python
import unittest
from nfupgrader import checks
from nfupgrader.host import FakeHost, RunResult


class TestKinds(unittest.TestCase):
    def test_rpm_installed(self):
        h = FakeHost({("rpm", "-q", "ksh"): RunResult(0, "ksh-20120801\n", "")})
        ok, _ = checks.KINDS["rpm_installed"](h, {"pkg": "ksh"})
        self.assertTrue(ok)

    def test_rpm_missing(self):
        h = FakeHost({("rpm", "-q", "nope"): RunResult(1, "not installed\n", "")})
        ok, _ = checks.KINDS["rpm_installed"](h, {"pkg": "nope"})
        self.assertFalse(ok)

    def test_file_exists_and_absent(self):
        h = FakeHost({("test", "-f", "/a"): RunResult(0, "", ""),
                      ("test", "-f", "/b"): RunResult(1, "", "")})
        self.assertTrue(checks.KINDS["file_exists"](h, {"path": "/a"})[0])
        self.assertFalse(checks.KINDS["file_exists"](h, {"path": "/b"})[0])
        self.assertTrue(checks.KINDS["file_absent"](h, {"path": "/b"})[0])
        self.assertFalse(checks.KINDS["file_absent"](h, {"path": "/a"})[0])

    def test_symlink_target_contains(self):
        h = FakeHost({("readlink", "/usr/cnc"): RunResult(0, "/usr4/inc5.5.003\n", "")})
        self.assertTrue(checks.KINDS["symlink_target"](h, {"path": "/usr/cnc", "contains": "inc5.5"})[0])
        self.assertFalse(checks.KINDS["symlink_target"](h, {"path": "/usr/cnc", "contains": "inc9"})[0])

    def test_command_status(self):
        h = FakeHost({("true",): RunResult(0, "", ""), ("false",): RunResult(1, "", "")})
        self.assertTrue(checks.KINDS["command_status"](h, {"argv": ["true"]})[0])
        self.assertFalse(checks.KINDS["command_status"](h, {"argv": ["false"]})[0])
        self.assertTrue(checks.KINDS["command_status"](h, {"argv": ["false"], "expect_rc": 1})[0])

    def test_command_output_match_and_negate(self):
        h = FakeHost({("cat", "/usr/cnc/.GR_STATUS"): RunResult(0, "COMPLETED\n", "")})
        self.assertTrue(checks.KINDS["command_output_match"](
            h, {"argv": ["cat", "/usr/cnc/.GR_STATUS"], "pattern": "COMPLETED"})[0])
        self.assertTrue(checks.KINDS["command_output_match"](
            h, {"argv": ["cat", "/usr/cnc/.GR_STATUS"], "pattern": "IN_PROGRESS", "negate": True})[0])

    def test_disk_space_min(self):
        df = "H\n/d 100 1 6291456 5% /usr4\n"   # ~6 GiB free (kb)
        h = FakeHost({("df", "-kP"): RunResult(0, df, "")})
        self.assertTrue(checks.KINDS["disk_space_min"](h, {"mount": "/usr4", "min_gb": 5})[0])
        self.assertFalse(checks.KINDS["disk_space_min"](h, {"mount": "/usr4", "min_gb": 10})[0])

    def test_service_active(self):
        h = FakeHost({("systemctl", "is-active", "httpd"): RunResult(0, "active\n", "")})
        self.assertTrue(checks.KINDS["service_active"](h, {"name": "httpd"})[0])

    def test_dbcheck_clean(self):
        h = FakeHost({("dbcheck", "-AV"): RunResult(0, "all good\n", "")})
        self.assertTrue(checks.KINDS["dbcheck_clean"](h, {})[0])
        h2 = FakeHost({("dbcheck", "-AV"): RunResult(0, "ERROR: x\n", "")})
        self.assertFalse(checks.KINDS["dbcheck_clean"](h2, {})[0])

    def test_staged_present(self):
        h = FakeHost({("readlink", "/usr/cnc_stage"): RunResult(0, "/usr4/inc5.5.103\n", "")})
        self.assertTrue(checks.KINDS["staged_present"](h, {})[0])
        h2 = FakeHost({("readlink", "/usr/cnc_stage"): RunResult(1, "", "")})
        self.assertFalse(checks.KINDS["staged_present"](h2, {})[0])


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run to verify failure**

Run: `python3 -m unittest tests.test_checks -v`
Expected: FAIL — module missing.

- [ ] **Step 3: Implement the kinds in `checks.py`**

```python
"""checks.py — pre-flight/post-flight check engine. Each kind is a small
evaluator fn(host, params) -> (passed: bool, detail: str) run through Host.run.
A JSON list binds kinds to a moment (preflight/postflight), a phase, and an
optional MACHTYPE. Pre-flight ERRORs block Launch (override logged); everything
post-flight is advisory."""
import json
import re

from nfupgrader import localinfo


def _rpm_installed(host, p):
    r = host.run(["rpm", "-q", p["pkg"]])
    return r.rc == 0, r.out.strip() or r.err.strip()


def _file_exists(host, p):
    return host.run(["test", "-f", p["path"]]).rc == 0, p["path"]


def _file_absent(host, p):
    return host.run(["test", "-f", p["path"]]).rc != 0, p["path"]


def _symlink_target(host, p):
    r = host.run(["readlink", p["path"]])
    tgt = r.out.strip()
    if r.rc != 0 or not tgt:
        return False, "no symlink at {}".format(p["path"])
    if "contains" in p and p["contains"] not in tgt:
        return False, "{} -> {}".format(p["path"], tgt)
    return True, "{} -> {}".format(p["path"], tgt)


def _command_status(host, p):
    r = host.run(list(p["argv"]))
    return r.rc == p.get("expect_rc", 0), "rc={}".format(r.rc)


def _command_output_match(host, p):
    r = host.run(list(p["argv"]))
    found = re.search(p["pattern"], r.out + r.err) is not None
    if p.get("negate"):
        return (not found), ("matched" if found else "absent")
    return found, ("matched" if found else "not found")


def _disk_space_min(host, p):
    rows = localinfo.parse_df(host.run(["df", "-kP"]).out, keep=(p["mount"],))
    if not rows or rows[0]["avail_kb"] is None:
        return False, "no df row for {}".format(p["mount"])
    free_gb = rows[0]["avail_kb"] / (1024.0 * 1024.0)
    return free_gb >= p["min_gb"], "{:.1f} GB free".format(free_gb)


def _service_active(host, p):
    r = host.run(["systemctl", "is-active", p["name"]])
    return r.rc == 0, r.out.strip()


def _dbcheck_clean(host, p):
    n = localinfo.parse_dbcheck_errors(host.run(["dbcheck", "-AV"]).out)
    return n == 0, "{} error(s)".format(n)


def _staged_present(host, p):
    r = host.run(["readlink", "/usr/cnc_stage"])
    return (r.rc == 0 and bool(r.out.strip())), r.out.strip() or "no staged load"


KINDS = {
    "rpm_installed": _rpm_installed,
    "file_exists": _file_exists,
    "file_absent": _file_absent,
    "symlink_target": _symlink_target,
    "command_status": _command_status,
    "command_output_match": _command_output_match,
    "disk_space_min": _disk_space_min,
    "service_active": _service_active,
    "dbcheck_clean": _dbcheck_clean,
    "staged_present": _staged_present,
}
```

- [ ] **Step 4: Run to verify pass**

Run: `python3 -m unittest tests.test_checks -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add nfupgrader/checks.py tests/test_checks.py
git commit -m "feat: checks.py kind evaluators (rpm/file/disk/dbcheck/...)"
```

---

## Task 2: `load_checks` + `run_checks` + `blocking_failures` (TDD)

**Files:**
- Modify: `nfupgrader/checks.py`
- Modify: `tests/test_checks.py`

- [ ] **Step 1: Add failing tests**

Append to `tests/test_checks.py`:
```python
import os
import tempfile


class TestRun(unittest.TestCase):
    def _checks(self):
        return [
            {"name": "staged", "description": "staged load present", "fail_text": "ERROR",
             "when": "preflight", "phase": "upgrade", "kind": "staged_present", "params": {}},
            {"name": "disk", "description": "5GB on /usr4", "fail_text": "ERROR",
             "when": "preflight", "phase": "any", "kind": "disk_space_min",
             "params": {"mount": "/usr4", "min_gb": 5}},
            {"name": "warnonly", "description": "advisory", "fail_text": "WARNING",
             "when": "preflight", "phase": "stage", "kind": "file_exists",
             "params": {"path": "/nope"}},
            {"name": "post", "description": "dbcheck", "fail_text": "WARNING",
             "when": "postflight", "phase": "any", "kind": "dbcheck_clean", "params": {}},
            {"name": "bep-only", "description": "bep thing", "fail_text": "ERROR",
             "when": "preflight", "phase": "any", "machtype": "BEP",
             "kind": "file_exists", "params": {"path": "/x"}},
        ]

    def _host(self):
        return FakeHost({
            ("readlink", "/usr/cnc_stage"): RunResult(0, "/usr4/inc5.5.103\n", ""),
            ("df", "-kP"): RunResult(0, "H\n/d 1 1 1048576 99% /usr4\n", ""),  # 1 GiB free
            ("test", "-f", "/nope"): RunResult(1, "", ""),
            ("dbcheck", "-AV"): RunResult(0, "clean\n", ""),
        })

    def test_load_checks(self):
        p = os.path.join(tempfile.mkdtemp(), "c.json")
        json_text = '[{"name":"x","kind":"file_exists","when":"preflight","phase":"any","params":{"path":"/a"}}]'
        open(p, "w").write(json_text)
        self.assertEqual(len(checks.load_checks(p)), 1)

    def test_run_filters_by_when_phase_machtype(self):
        results = checks.run_checks(self._host(), self._checks(), "preflight", "upgrade", machtype="FEP")
        names = [r["name"] for r in results]
        self.assertIn("staged", names)      # preflight + upgrade
        self.assertIn("disk", names)        # preflight + any
        self.assertNotIn("warnonly", names) # phase stage != upgrade
        self.assertNotIn("post", names)     # postflight
        self.assertNotIn("bep-only", names) # machtype BEP != FEP

    def test_blocking_only_preflight_error_failures(self):
        results = checks.run_checks(self._host(), self._checks(), "preflight", "upgrade", machtype="FEP")
        blk = checks.blocking_failures(results)
        blk_names = [r["name"] for r in blk]
        self.assertIn("disk", blk_names)        # ERROR + failed (1GB < 5GB)
        self.assertNotIn("staged", blk_names)   # passed
        # a WARNING failure is never blocking
        stage_res = checks.run_checks(self._host(), self._checks(), "preflight", "stage")
        self.assertEqual(checks.blocking_failures(stage_res), [])

    def test_result_shape(self):
        r = checks.run_checks(self._host(), self._checks(), "preflight", "upgrade", machtype="FEP")[0]
        for k in ("name", "description", "severity", "passed", "detail", "blocking"):
            self.assertIn(k, r)
```

- [ ] **Step 2: Run to verify failure**

Run: `python3 -m unittest tests.test_checks -v`
Expected: FAIL — `load_checks`/`run_checks`/`blocking_failures` missing.

- [ ] **Step 3: Implement**

Append to `checks.py`:
```python
def load_checks(path):
    """Load a JSON list of check definitions."""
    with open(path) as fh:
        return json.load(fh)


def _applies(check, when, phase, machtype):
    if check.get("when") != when:
        return False
    cphase = check.get("phase", "any")
    if cphase not in ("any", phase):
        return False
    cmt = check.get("machtype")
    if cmt is not None and cmt != machtype:
        return False
    return True


def run_checks(host, check_list, when, phase, machtype=None):
    """Evaluate every check bound to (when, phase, machtype). Returns a list of
    result dicts: {name, description, severity, passed, detail, blocking}."""
    results = []
    for c in check_list:
        if not _applies(c, when, phase, machtype):
            continue
        kind = KINDS.get(c["kind"])
        if kind is None:
            passed, detail = False, "unknown kind: {}".format(c["kind"])
        else:
            try:
                passed, detail = kind(host, c.get("params", {}))
            except Exception as e:                       # a check must never crash the run
                passed, detail = False, "check error: {}".format(e)
        severity = c.get("fail_text", "ERROR")
        blocking = (when == "preflight" and severity == "ERROR" and not passed)
        results.append({
            "name": c.get("name", c["kind"]),
            "description": c.get("description", ""),
            "severity": severity,
            "passed": passed,
            "detail": detail,
            "blocking": blocking,
        })
    return results


def blocking_failures(results):
    """The subset of results that block a launch."""
    return [r for r in results if r["blocking"]]
```

- [ ] **Step 4: Run to verify pass**

Run: `python3 -m unittest tests.test_checks -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add nfupgrader/checks.py tests/test_checks.py
git commit -m "feat: checks load/run/blocking with when+phase+machtype binding"
```

---

## Task 3: default check list

**Files:**
- Create: `nfupgrader/checks.default.json`

- [ ] **Step 1: Write a sensible starter set**

```json
[
  {
    "name": "disk-usr4",
    "description": "At least 5 GB free on /usr4 before staging",
    "fail_text": "ERROR", "when": "preflight", "phase": "any",
    "kind": "disk_space_min", "params": {"mount": "/usr4", "min_gb": 5}
  },
  {
    "name": "gr-idle",
    "description": "No GR transfer in progress",
    "fail_text": "ERROR", "when": "preflight", "phase": "any",
    "kind": "command_output_match",
    "params": {"argv": ["cat", "/usr/cnc/.GR_STATUS"], "pattern": "IN_PROGRESS", "negate": true}
  },
  {
    "name": "no-grestore",
    "description": "No GR restore in progress",
    "fail_text": "ERROR", "when": "preflight", "phase": "any",
    "kind": "file_absent", "params": {"path": "/usr/cnc/grestore.pid"}
  },
  {
    "name": "dbcheck-clean",
    "description": "Database check reports no errors",
    "fail_text": "ERROR", "when": "preflight", "phase": "upgrade",
    "kind": "dbcheck_clean", "params": {}
  },
  {
    "name": "staged-present",
    "description": "A staged load exists before upgrading",
    "fail_text": "ERROR", "when": "preflight", "phase": "upgrade",
    "kind": "staged_present", "params": {}
  },
  {
    "name": "post-dbcheck",
    "description": "Database check clean after upgrade",
    "fail_text": "WARNING", "when": "postflight", "phase": "upgrade",
    "kind": "dbcheck_clean", "params": {}
  },
  {
    "name": "post-app-up",
    "description": "Application booted after upgrade",
    "fail_text": "WARNING", "when": "postflight", "phase": "upgrade",
    "kind": "file_exists", "params": {"path": "/usr/cnc/.CNC_UP"}
  }
]
```

- [ ] **Step 2: Validate it loads**

Run: `python3 -c "from nfupgrader import checks; print(len(checks.load_checks('nfupgrader/checks.default.json')))"`
Expected: `7`.

- [ ] **Step 3: Commit**

```bash
git add nfupgrader/checks.default.json
git commit -m "feat: default pre/post-flight check list"
```

---

## Task 4: `/api/checks` + launch gating (TDD)

**Files:**
- Modify: `nfupgrader/api.py`
- Modify: `tests/test_api.py`

- [ ] **Step 1: Add tests**

In `tests/test_api.py`, extend `make_ctx` to answer the default-check commands and set a `checks_path`. Add:
```python
    def test_checks_endpoint(self):
        sid = self._session()
        status, _, body = api.dispatch(
            "GET", "/api/checks", {"when": ["preflight"], "phase": ["stage"]},
            b"", {"nfu_sid": sid}, "127.0.0.1", self.ctx)
        self.assertEqual(status, 200)
        data = json.loads(body.decode())
        self.assertIn("results", data)
        self.assertIn("blocking", data)

    def test_launch_blocked_by_preflight_error(self):
        sid = self._session()
        status, _, body = api.dispatch(
            "POST", "/api/launch", {}, json.dumps({"mode": "upgrade"}).encode(),
            {"nfu_sid": sid}, "127.0.0.1", self.ctx)
        self.assertEqual(status, 409)
        data = json.loads(body.decode())
        self.assertTrue(len(data["blocking"]) >= 1)

    def test_launch_override_requires_reason(self):
        sid = self._session()
        status, _, _ = api.dispatch(
            "POST", "/api/launch", {}, json.dumps({"mode": "upgrade", "override": True}).encode(),
            {"nfu_sid": sid}, "127.0.0.1", self.ctx)
        self.assertEqual(status, 400)   # override without reason

    def test_launch_override_with_reason_proceeds_and_audits(self):
        sid = self._session()
        status, _, _ = api.dispatch(
            "POST", "/api/launch", {},
            json.dumps({"mode": "upgrade", "override": True, "reason": "known good"}).encode(),
            {"nfu_sid": sid}, "127.0.0.1", self.ctx)
        self.assertEqual(status, 202)
        events = [json.loads(l)["event"] for l in
                  open(os.path.join(self.tmp, "audit.jsonl")).read().splitlines()]
        self.assertIn("override", events)
        self.assertIn("launch", events)
```
Extend `make_ctx`'s FakeHost so a pre-flight ERROR fails (e.g. `df -kP` shows little space, `readlink /usr/cnc_stage` rc 1 so `staged-present` fails), write a `checks.default.json` copy or point `checks_path` at the repo's default, and pass `checks_path=` to `api.Context`. Ensure a clean `stage` pre-flight (for `test_checks_endpoint` no assertion on blocking count) and a failing `upgrade` pre-flight (for the launch-block tests).

- [ ] **Step 2: Run to verify failure**

Run: `python3 -m unittest tests.test_api -v`
Expected: FAIL — no `/api/checks`, launch not gated, Context has no `checks_path`.

- [ ] **Step 3: Implement**

In `api.py`: add `"checks_path"` to `Context` fields and extend defaults by one `None` (→ six `None`s). Add `from nfupgrader import checks`. Add a helper to resolve MACHTYPE and load checks:
```python
def _machtype(ctx):
    hosts = site.read_site(ctx.cnc_cnfg_path or "/usr/cnc/features/cnc.cnfg")
    me = site.local_host(hosts, ctx.hostname)
    return me.role if me else None


def _load(ctx):
    import os
    path = ctx.checks_path or os.path.join(os.path.dirname(__file__), "checks.default.json")
    try:
        return checks.load_checks(path)
    except (IOError, OSError, ValueError):
        return []
```
Add the `/api/checks` route (after `/api/site`):
```python
    if path == "/api/checks" and method == "GET":
        when = (query.get("when") or ["preflight"])[0]
        phase = (query.get("phase") or [ctx.phase])[0]
        results = checks.run_checks(ctx.host, _load(ctx), when, phase, _machtype(ctx))
        return _json(200, {"results": results, "blocking": checks.blocking_failures(results)})
```
Replace the `/api/launch` body with pre-flight gating:
```python
    if path == "/api/launch" and method == "POST":
        try:
            payload = json.loads(body.decode("utf-8") or "{}")
        except ValueError:
            return _json(400, {"error": "bad json"})
        mode = payload.get("mode")
        if mode not in ("stage", "upgrade"):
            return _json(400, {"error": "mode must be stage or upgrade"})
        results = checks.run_checks(ctx.host, _load(ctx), "preflight", mode, _machtype(ctx))
        blocking = checks.blocking_failures(results)
        override = bool(payload.get("override"))
        if blocking and not override:
            return _json(409, {"error": "pre-flight checks failed", "blocking": blocking})
        if blocking and override:
            reason = (payload.get("reason") or "").strip()
            if not reason:
                return _json(400, {"error": "override requires a reason"})
            ctx.audit.record("override", mode=mode, client=client_ip, reason=reason,
                             checks=[b["name"] for b in blocking])
        ctx.audit.record("launch", mode=mode, client=client_ip)
        return _json(202, {"accepted": True, "mode": mode})
```

- [ ] **Step 4: Run to verify pass**

Run: `python3 -m unittest tests.test_api -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add nfupgrader/api.py tests/test_api.py
git commit -m "feat: /api/checks + pre-flight launch gating with logged override"
```

---

## Task 5: httpd wiring — `checks_path`

**Files:**
- Modify: `nfupgrader/httpd.py`

- [ ] **Step 1: Add env + Context field**

Add:
```python
    checks_path = os.environ.get(
        "NFU_CHECKS", os.path.join(here, "checks.default.json"))
```
and pass `checks_path=checks_path` in the `api.Context(...)` construction.

- [ ] **Step 2: Import check**

Run: `python3 -c "import nfupgrader.httpd" && echo ok`
Expected: `ok`.

- [ ] **Step 3: Commit**

```bash
git add nfupgrader/httpd.py
git commit -m "feat: NFU_CHECKS path into the service context"
```

---

## Task 6: UI — checks panel, Launch gating, override, post-flight

**Files:**
- Modify: `nfupgrader/web/index.html`, `app.js`, `style.css`

- [ ] **Step 1: Add markup to `index.html`** (inside/near the controls section)

Replace the existing controls section with:
```html
  <section id="controls">
    <h2>Launch</h2>
    <div>
      <label>Phase:
        <select id="mode"><option value="stage">Stage</option><option value="upgrade">Upgrade</option></select>
      </label>
      <button id="btn-check">Run pre-flight checks</button>
      <button id="btn-launch" disabled>Launch</button>
    </div>
    <div id="preflight"></div>
    <div id="override-box" style="display:none">
      <label><input type="checkbox" id="override"> Override blocking checks</label>
      <input type="text" id="reason" placeholder="reason (required to override)" size="40">
    </div>
    <span id="launch-msg"></span>
    <h3>Post-flight</h3>
    <div id="postflight">-</div>
  </section>
```

- [ ] **Step 2: Replace the launch logic in `app.js`**

Remove the old `btn-stage`/`btn-upgrade` handlers and `launch(mode)`; add:
```javascript
function renderChecks(el, data) {
  var html = "<table class='checks'><tr><th></th><th>Check</th><th>Detail</th></tr>";
  data.results.forEach(function (r) {
    var mark = r.passed ? "✅" : (r.severity === "ERROR" ? "❌" : "⚠️");
    html += "<tr class='" + (r.passed ? "pass" : "fail-" + r.severity) + "'>" +
            "<td>" + mark + "</td><td>" + r.name + " — " + r.description + "</td>" +
            "<td>" + (r.detail || "") + "</td></tr>";
  });
  html += "</table>";
  el.innerHTML = html;
}

function runPreflight() {
  var mode = document.getElementById("mode").value;
  getJSON("/api/checks?when=preflight&phase=" + mode, function (data) {
    renderChecks(document.getElementById("preflight"), data);
    var blocked = data.blocking.length > 0;
    document.getElementById("override-box").style.display = blocked ? "block" : "none";
    document.getElementById("btn-launch").disabled = false;  // enabled; server re-checks
  });
}

function launch() {
  var mode = document.getElementById("mode").value;
  var override = document.getElementById("override").checked;
  var reason = document.getElementById("reason").value;
  if (!window.confirm("Start the " + mode.toUpperCase() +
      " now? This runs the real upgrade and cannot be paused.")) return;
  postJSON("/api/launch", { mode: mode, override: override, reason: reason },
    function (status, body) {
      var msg = document.getElementById("launch-msg");
      if (status === 202) { msg.textContent = "Launched " + mode + "…"; }
      else if (status === 409) { msg.textContent = "Blocked by pre-flight checks — override to proceed."; }
      else { msg.textContent = "Error: " + (body.error || status); }
    });
}

function refreshPostflight() {
  var mode = document.getElementById("mode").value;
  getJSON("/api/checks?when=postflight&phase=" + mode, function (data) {
    if (data.results.length) renderChecks(document.getElementById("postflight"), data);
  });
}

document.getElementById("btn-check").onclick = runPreflight;
document.getElementById("btn-launch").onclick = launch;
```
And add `refreshPostflight();` to the initial calls plus `setInterval(refreshPostflight, 15000);`.

- [ ] **Step 3: Style in `style.css`**

```css
table.checks { border-collapse: collapse; margin: .5rem 0; width: 100%; font-size: 13px; }
table.checks td, table.checks th { border: 1px solid #ddd; padding: .25rem .5rem; text-align: left; }
tr.fail-ERROR { background: #fdecea; }
tr.fail-WARNING { background: #fff8e1; }
```

- [ ] **Step 4: Sanity check**

Run: `grep -q "/api/checks" nfupgrader/web/app.js && grep -q "btn-check" nfupgrader/web/index.html && echo WIRED`
Expected: `WIRED`.

- [ ] **Step 5: Commit**

```bash
git add nfupgrader/web/
git commit -m "feat: checks UI — pre-flight panel, launch gating, override, post-flight"
```

---

## Task 7: Full suite + smoke + manual

- [ ] **Step 1: Full suite**

Run: `python3 -m unittest discover -s tests -p 'test_*.py'`
Expected: all PASS.

- [ ] **Step 2: Smoke the gate**

Start the server against a box/fixtures where a pre-flight ERROR fails (e.g. no staged load). Redeem the token, then:
```bash
curl -s -b cj "http://127.0.0.1:$PORT/api/checks?when=preflight&phase=upgrade" | python3 -m json.tool | head
curl -s -b cj -X POST -H 'Content-Type: application/json' -d '{"mode":"upgrade"}' \
  "http://127.0.0.1:$PORT/api/launch" -o /dev/null -w '%{http_code}\n'   # expect 409
curl -s -b cj -X POST -H 'Content-Type: application/json' \
  -d '{"mode":"upgrade","override":true,"reason":"lab test"}' \
  "http://127.0.0.1:$PORT/api/launch" -o /dev/null -w '%{http_code}\n'   # expect 202
grep override "$INCLOGDIR/nfupgrader_audit.jsonl"
```
Expected: 409 then 202, and an `override` audit line with the reason.

- [ ] **Step 3: Manual on r9dev19**

Load the page, pick a phase, click **Run pre-flight checks** — confirm the panel shows pass/❌/⚠️ per check (GR idle, disk, staged, dbcheck). Confirm Launch is blocked with a message until override + reason. Read-only until you actually confirm a launch.

---

## Self-Review

**Spec coverage:** pre-flight blocking + typed logged override → `run_checks`/`blocking_failures` (T1–2) + launch gating (T4) + override audit; post-flight advisory → `when=postflight`, never blocking; JSON kinds → T1 kind set; coded checks → `dbcheck_clean`/`staged_present` kinds (retrofit-log scan expressible via `command_output_match`, noted); binding by when/phase/MACHTYPE → `_applies` + `_machtype`; failure policy (ERROR blocks, WARNING advisory) → `blocking` computed only for preflight ERROR.

**Placeholder scan:** complete code each step; the one prose instruction (extend `make_ctx`) is bounded by explicit requirements and asserted by the tests. No TBD.

**Type/name consistency:** evaluator contract `(passed, detail)` uniform across `KINDS`; result dict `{name, description, severity, passed, detail, blocking}` consistent across `run_checks`, api, tests, app.js. `run_checks(host, list, when, phase, machtype)` signature matches api call sites and tests. `Context` gains exactly `checks_path` (defaults extended to six `None`s), set in httpd. Endpoint paths `/api/checks`, override payload `{mode, override, reason}` consistent between api, tests, app.js.

**Deferred (not gaps):** `limits.conf` / `system_define` dedicated kinds (use `command_output_match` meanwhile); retrofit-log scan as a named coded kind; per-check re-run in the UI. All additive to the registry.
