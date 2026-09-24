# Pre-flight Check Fixes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A failed pre-flight check whose kind has a known remedy (rpm, service, kernel param, ulimit, touchfile) gets a Fix button that runs it as root and re-runs the check.

**Architecture:** Fix definitions live beside the check kinds in `checks.py` (`FIXES`, `describe_fix`, `apply_fix`, `evaluate`, `find_check`); `POST /api/checks/fix` in `api.py` validates, locks, audits, applies and re-evaluates; the pre-flight table in `app.js` renders a Fix column.

**Tech Stack:** Python 3.6.8 stdlib (`.format()`, no f-strings), ES5 JS, `unittest`.

Spec: `~/WorkNotes/design/2026-09-24-nfupgrader-check-fixes-design.md`
Repo: `~/Git/nfupgrader`. Tests: `cd ~/Git/nfupgrader && timeout 180 python3 -m unittest discover -s tests -p 'test_*.py' 2>&1 | tail -3` (baseline 267 OK; also run with `LC_ALL=C`). Ignore BASE/VPATH warnings.

---

### Task 1: Fix definitions in `checks.py`

**Files:** Modify `nfupgrader/checks.py`; Test `tests/test_checks.py`

- [ ] **Step 1: Failing tests** — append to `tests/test_checks.py` above `if __name__`:

```python
class RecordingHost(object):
    """Maps tuple(argv) -> RunResult (default rc 0) and records (argv, timeout)."""

    def __init__(self, responses=None):
        self.responses = responses or {}
        self.calls = []

    def run(self, argv, timeout=None):
        self.calls.append((list(argv), timeout))
        return self.responses.get(tuple(argv), RunResult(0, "", ""))


class TestFixes(unittest.TestCase):
    def setUp(self):
        d = tempfile.mkdtemp()
        self._saved = (checks.SYSCTL_CONF, checks.LIMITS_CONF)
        checks.SYSCTL_CONF = os.path.join(d, "sysctl.d", "99-netflex.conf")
        checks.LIMITS_CONF = os.path.join(d, "limits.d", "99-netflex.conf")

    def tearDown(self):
        checks.SYSCTL_CONF, checks.LIMITS_CONF = self._saved

    def check(self, kind, **params):
        return {"name": "c", "kind": kind, "when": "preflight", "phase": "any",
                "fail_text": "ERROR", "params": params}

    def test_describe_only_for_fixable_kinds(self):
        self.assertEqual(checks.describe_fix(self.check("rpm_installed", pkg="ksh")),
                         "dnf install -y ksh")
        self.assertIn("systemctl start firewalld",
                      checks.describe_fix(self.check("service_active", name="firewalld")))
        self.assertIn("sysctl -w kernel.shmmax=68719476736", checks.describe_fix(
            self.check("kernel_param", param="kernel.shmmax", expected_value="68719476736")))
        self.assertIn("/usr/cnc/features/ems",
                      checks.describe_fix(self.check("touchfile", file="ems")))
        self.assertIsNone(checks.describe_fix(self.check("system_define", variable="X")))
        self.assertIsNone(checks.describe_fix(self.check("dbcheck_clean")))

    def test_rpm_fix(self):
        h = RecordingHost()
        ok, detail = checks.apply_fix(h, self.check("rpm_installed", pkg="ksh"))
        self.assertTrue(ok)
        self.assertEqual(h.calls, [(["dnf", "install", "-y", "ksh"], 600)])
        h = RecordingHost({("dnf", "install", "-y", "nope"):
                           RunResult(1, "", "Last metadata...\nError: Unable to find a match: nope\n")})
        ok, detail = checks.apply_fix(h, self.check("rpm_installed", pkg="nope"))
        self.assertFalse(ok)
        self.assertTrue(detail.startswith("rc=1: "))
        self.assertIn("Unable to find a match: nope", detail)

    def test_service_fix_start_and_enable(self):
        h = RecordingHost()
        ok, detail = checks.apply_fix(h, self.check("service_active", name="firewalld"))
        self.assertTrue(ok)
        self.assertEqual([c[0] for c in h.calls], [["systemctl", "start", "firewalld"],
                                                   ["systemctl", "enable", "firewalld"]])
        h = RecordingHost({("systemctl", "enable", "firewalld"): RunResult(1, "", "no")})
        ok, detail = checks.apply_fix(h, self.check("service_active", name="firewalld"))
        self.assertTrue(ok)
        self.assertTrue(detail.startswith("WARNING:"))
        h = RecordingHost({("systemctl", "start", "firewalld"): RunResult(5, "", "boom")})
        ok, _ = checks.apply_fix(h, self.check("service_active", name="firewalld"))
        self.assertFalse(ok)
        self.assertEqual(len(h.calls), 1)

    def test_kernel_param_sets_and_persists(self):
        os.makedirs(os.path.dirname(checks.SYSCTL_CONF))
        open(checks.SYSCTL_CONF, "w").write("# ours\nkernel.shmmax = 1\nvm.swappiness=10\n")
        h = RecordingHost()
        ok, detail = checks.apply_fix(h, self.check("kernel_param", param="kernel.shmmax",
                                                    expected_value="68719476736"))
        self.assertTrue(ok)
        self.assertEqual(h.calls, [(["sysctl", "-w", "kernel.shmmax=68719476736"], 10)])
        self.assertEqual(open(checks.SYSCTL_CONF).read(),
                         "# ours\nvm.swappiness=10\nkernel.shmmax=68719476736\n")

    def test_kernel_param_creates_file_and_fails_on_sysctl_error(self):
        ok, _ = checks.apply_fix(RecordingHost(), self.check("kernel_param", param="a.b",
                                                             expected_value="1"))
        self.assertTrue(ok)
        self.assertEqual(open(checks.SYSCTL_CONF).read(), "a.b=1\n")
        h = RecordingHost({("sysctl", "-w", "a.b=2"): RunResult(255, "", "permission denied")})
        ok, detail = checks.apply_fix(h, self.check("kernel_param", param="a.b", expected_value="2"))
        self.assertFalse(ok)
        self.assertEqual(open(checks.SYSCTL_CONF).read(), "a.b=1\n")   # unchanged

    def test_ulimit_replaces_matching_entry(self):
        os.makedirs(os.path.dirname(checks.LIMITS_CONF))
        open(checks.LIMITS_CONF, "w").write("cnc soft nofile 1024\ncnc hard nofile 4096\n")
        ok, detail = checks.apply_fix(RecordingHost(), self.check(
            "ulimit", domain="cnc", limit_type="soft", limit_item="nofile", expected_value="65536"))
        self.assertTrue(ok)
        self.assertEqual(open(checks.LIMITS_CONF).read(),
                         "cnc hard nofile 4096\ncnc soft nofile 65536\n")

    def test_touchfile_uses_scmd(self):
        h = RecordingHost()
        ok, _ = checks.apply_fix(h, self.check("touchfile", file="ems"))
        self.assertTrue(ok)
        self.assertEqual(h.calls, [(["/usr/cnc/bin/scmd", "touch", "/usr/cnc/features/ems"], 30)])

    def test_apply_unfixable_and_exception(self):
        ok, detail = checks.apply_fix(RecordingHost(), self.check("dbcheck_clean"))
        self.assertFalse(ok)

        class Boom(object):
            def run(self, argv, timeout=None):
                raise OSError("exec failed")
        ok, detail = checks.apply_fix(Boom(), self.check("rpm_installed", pkg="ksh"))
        self.assertFalse(ok)
        self.assertIn("exec failed", detail)

    def test_results_carry_fix_only_when_failed_and_fixable(self):
        h = FakeHost({("rpm", "-q", "ksh"): RunResult(0, "ksh-1\n", ""),
                      ("rpm", "-q", "lvm2"): RunResult(1, "not installed\n", "")})
        cl = [self.check("rpm_installed", pkg="ksh"), self.check("rpm_installed", pkg="lvm2"),
              self.check("dbcheck_clean")]
        cl[0]["name"], cl[1]["name"], cl[2]["name"] = "ksh", "lvm2", "db"
        res = dict((r["name"], r) for r in checks.run_checks(h, cl, "preflight", "stage"))
        self.assertIsNone(res["ksh"]["fix"])                       # passed
        self.assertEqual(res["lvm2"]["fix"], "dnf install -y lvm2")
        self.assertIsNone(res["db"]["fix"])                        # not fixable

    def test_find_check_and_evaluate(self):
        cl = [dict(self.check("rpm_installed", pkg="ksh"), name="ksh"),
              dict(self.check("rpm_installed", pkg="x"), name="x", phase="upgrade")]
        self.assertEqual(checks.find_check(cl, "ksh", "preflight", "stage")["name"], "ksh")
        self.assertIsNone(checks.find_check(cl, "x", "preflight", "stage"))   # wrong phase
        self.assertIsNone(checks.find_check(cl, "nope", "preflight", "stage"))
        r = checks.evaluate(FakeHost({("rpm", "-q", "ksh"): RunResult(0, "ksh\n", "")}),
                            cl[0], "preflight")
        self.assertTrue(r["passed"])
        self.assertIsNone(r["fix"])
```

Also make sure `tests/test_checks.py` imports `os` and `tempfile` at the top (add `import os` / `import tempfile` if missing), and add `"fix"` to the key tuple in `test_result_shape`.

Run: `python3 -m unittest tests.test_checks 2>&1 | tail -3` → errors (no SYSCTL_CONF etc.).

- [ ] **Step 2: Implement** — in `nfupgrader/checks.py`:

2a. Add `import os` to the imports and update the module docstring's last sentence to: `Pre-flight ERRORs block Launch (override logged); everything post-flight is advisory. Some kinds also have a fix (FIXES) that can be applied from the UI.`

2b. Add after the `KINDS` dict:

```python
# --- fixes (ported from nf-install system_checks fix_*; the service runs as root) ---

SYSCTL_CONF = "/etc/sysctl.d/99-netflex.conf"
LIMITS_CONF = "/etc/security/limits.d/99-netflex.conf"
SCMD = "/usr/cnc/bin/scmd"


def _cmd_detail(r):
    """'rc=N: <last lines of stderr, else stdout>' for a failed fix command."""
    lines = (r.err.strip() or r.out.strip()).splitlines()[-3:]
    text = " | ".join(ln.strip() for ln in lines)
    if len(text) > 300:
        text = text[-300:]
    return "rc={}: {}".format(r.rc, text) if text else "rc={}".format(r.rc)


def _replace_lines(path, keep, new_line):
    """Rewrite path keeping the lines for which keep(line) is true, then append
    new_line. Written to a temp file and renamed so a reader never sees half a file."""
    try:
        with open(path) as fh:
            lines = fh.read().splitlines(True)
    except (IOError, OSError):
        lines = []
    lines = [ln for ln in lines if keep(ln)]
    if lines and not lines[-1].endswith("\n"):
        lines[-1] += "\n"
    lines.append(new_line + "\n")
    d = os.path.dirname(path)
    if d and not os.path.isdir(d):
        os.makedirs(d)
    tmp = path + ".nfupgrader.tmp"
    with open(tmp, "w") as fh:
        fh.writelines(lines)
    os.rename(tmp, path)


def _fix_rpm(host, p):
    r = host.run(["dnf", "install", "-y", p["pkg"]], timeout=600)
    if r.rc != 0:
        return False, _cmd_detail(r)
    return True, "{} installed".format(p["pkg"])


def _fix_service(host, p):
    name = p["name"]
    r = host.run(["systemctl", "start", name], timeout=30)
    if r.rc != 0:
        return False, "start failed, " + _cmd_detail(r)
    r = host.run(["systemctl", "enable", name], timeout=30)
    if r.rc != 0:
        return True, "WARNING: {} started but not enabled, {}".format(name, _cmd_detail(r))
    return True, "{} started and enabled".format(name)


def _fix_kernel_param(host, p):
    setting = "{}={}".format(p["param"], p["expected_value"])
    r = host.run(["sysctl", "-w", setting], timeout=10)
    if r.rc != 0:
        return False, _cmd_detail(r)
    key = p["param"] + "="
    try:
        _replace_lines(SYSCTL_CONF, lambda ln: not ln.replace(" ", "").startswith(key), setting)
    except (IOError, OSError) as e:
        return True, "WARNING: {} set live but not persisted: {}".format(setting, e)
    return True, "{} set and persisted in {}".format(setting, SYSCTL_CONF)


def _fix_ulimit(host, p):
    head = [p["domain"], p["limit_type"], p["limit_item"]]
    entry = " ".join(head + [p["expected_value"]])

    def keep(ln):
        parts = ln.split()
        return not (len(parts) >= 4 and parts[:3] == head)
    try:
        _replace_lines(LIMITS_CONF, keep, entry)
    except (IOError, OSError) as e:
        return False, "could not write {}: {}".format(LIMITS_CONF, e)
    return True, "'{}' written to {}".format(entry, LIMITS_CONF)


def _fix_touchfile(host, p):
    path = "/usr/cnc/features/" + p["file"]
    r = host.run([SCMD, "touch", path], timeout=30)
    if r.rc != 0:
        return False, _cmd_detail(r)
    return True, "{} created".format(path)


# kind -> (describe(params) -> str, apply(host, params) -> (ok, detail))
FIXES = {
    "rpm_installed": (lambda p: "dnf install -y {}".format(p["pkg"]), _fix_rpm),
    "service_active": (lambda p: "systemctl start {0} && systemctl enable {0}".format(p["name"]),
                       _fix_service),
    "kernel_param": (lambda p: "sysctl -w {}={} (persisted in {})".format(
        p["param"], p["expected_value"], SYSCTL_CONF), _fix_kernel_param),
    "ulimit": (lambda p: "add '{} {} {} {}' to {}".format(
        p["domain"], p["limit_type"], p["limit_item"], p["expected_value"], LIMITS_CONF),
               _fix_ulimit),
    "touchfile": (lambda p: "{} touch /usr/cnc/features/{}".format(SCMD, p["file"]),
                  _fix_touchfile),
}


def describe_fix(check):
    """One line saying what the fix for this check would do, or None."""
    f = FIXES.get(check.get("kind"))
    if f is None:
        return None
    try:
        return f[0](check.get("params", {}))
    except (KeyError, TypeError):
        return None


def apply_fix(host, check):
    """Run the fix for a check; returns (ok, detail). Never raises."""
    f = FIXES.get(check.get("kind"))
    if f is None:
        return False, "no fix for kind {}".format(check.get("kind"))
    try:
        return f[1](host, check.get("params", {}))
    except Exception as e:
        return False, "fix error: {}".format(e)
```

2c. Replace `run_checks` with `evaluate` + a thinner `run_checks`, and add `find_check`:

```python
def evaluate(host, c, when):
    """Run one check definition; returns its result dict:
    {name, description, severity, passed, detail, blocking, fix}."""
    kind = KINDS.get(c["kind"])
    if kind is None:
        passed, detail = False, "unknown kind: {}".format(c["kind"])
    else:
        try:
            passed, detail = kind(host, c.get("params", {}))
        except Exception as e:                       # a check must never crash the run
            passed, detail = False, "check error: {}".format(e)
    severity = c.get("fail_text", "ERROR")
    return {
        "name": c.get("name", c["kind"]),
        "description": c.get("description", ""),
        "severity": severity,
        "passed": passed,
        "detail": detail,
        "blocking": (when == "preflight" and severity == "ERROR" and not passed),
        "fix": None if passed else describe_fix(c),
    }


def run_checks(host, check_list, when, phase, machtype=None):
    """Evaluate every check bound to (when, phase, machtype)."""
    return [evaluate(host, c, when) for c in check_list if _applies(c, when, phase, machtype)]


def find_check(check_list, name, when, phase, machtype=None):
    """The definition named `name` that applies to (when, phase, machtype), or None."""
    for c in check_list:
        if c.get("name", c["kind"]) == name and _applies(c, when, phase, machtype):
            return c
    return None
```

- [ ] **Step 3: Run** — test_checks OK; full suite OK (both locales).

- [ ] **Step 4: Commit** — `git add nfupgrader/checks.py tests/test_checks.py && git commit -m "feat(checks): fixes for rpm, service, kernel param, ulimit and touchfile checks"` (+ Co-Authored-By trailer).

---

### Task 2: `POST /api/checks/fix`

**Files:** Modify `nfupgrader/api.py`; Create `tests/fixtures/checks.fix.json`; Test `tests/test_api.py`

- [ ] **Step 1: Fixture** `tests/fixtures/checks.fix.json`:

```json
[
  {"name": "pkg-ksh", "description": "ksh installed", "fail_text": "ERROR",
   "when": "preflight", "phase": "any", "kind": "rpm_installed", "params": {"pkg": "ksh"}},
  {"name": "sdi-mode", "description": "SDI test mode", "fail_text": "WARNING",
   "when": "preflight", "phase": "any", "kind": "system_define",
   "params": {"variable": "M_SDI_TEST_MODE", "expected_values": ["1"]}},
  {"name": "upgrade-only", "description": "only in upgrade", "fail_text": "ERROR",
   "when": "preflight", "phase": "upgrade", "kind": "rpm_installed", "params": {"pkg": "x"}}
]
```

- [ ] **Step 2: Failing tests** — append to `tests/test_api.py` above `if __name__`:

```python
FIX_FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "checks.fix.json")


class InstallingHost(FakeHost):
    """rpm -q ksh fails until `dnf install -y ksh` has run."""

    def run(self, argv, timeout=None):
        if list(argv) == ["dnf", "install", "-y", "ksh"]:
            self.responses[("rpm", "-q", "ksh")] = RunResult(0, "ksh-1\n", "")
        return FakeHost.run(self, argv, timeout)


class TestCheckFix(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        base = make_ctx(self.tmp, running=False)
        host = InstallingHost(dict(base.host.responses))
        host.responses[("rpm", "-q", "ksh")] = RunResult(1, "package ksh is not installed\n", "")
        host.responses[("dnf", "install", "-y", "ksh")] = RunResult(0, "Complete!\n", "")
        self.ctx = base._replace(host=host, checks_path=FIX_FIXTURE, phase="stage")
        self.sid = self.ctx.auth.redeem("TOK", "127.0.0.1")

    def fix(self, payload, cookies=None):
        status, _, body = api.dispatch(
            "POST", "/api/checks/fix", {}, json.dumps(payload).encode(),
            {"nfu_sid": self.sid} if cookies is None else cookies, "127.0.0.1", self.ctx)
        return status, json.loads(body.decode())

    def events(self):
        path = os.path.join(self.tmp, "audit.jsonl")
        return [json.loads(l) for l in open(path)] if os.path.exists(path) else []

    def test_requires_session(self):
        self.assertEqual(self.fix({"name": "pkg-ksh"}, cookies={})[0], 401)

    def test_listing_offers_fix_for_failed_fixable_check(self):
        status, _, body = api.dispatch("GET", "/api/checks",
                                       {"when": ["preflight"], "phase": ["stage"]}, b"",
                                       {"nfu_sid": self.sid}, "127.0.0.1", self.ctx)
        res = dict((r["name"], r) for r in json.loads(body.decode())["results"])
        self.assertEqual(res["pkg-ksh"]["fix"], "dnf install -y ksh")
        self.assertIsNone(res["sdi-mode"]["fix"])

    def test_fix_runs_audits_and_rechecks(self):
        status, d = self.fix({"name": "pkg-ksh", "phase": "stage"})
        self.assertEqual(status, 200)
        self.assertEqual(d["fix"], {"ok": True, "detail": "ksh installed"})
        self.assertTrue(d["result"]["passed"])
        ev = [(e["event"], e.get("name")) for e in self.events()]
        self.assertIn(("check_fix_start", "pkg-ksh"), ev)
        self.assertIn(("check_fix", "pkg-ksh"), ev)
        done = [e for e in self.events() if e["event"] == "check_fix"][0]
        self.assertTrue(done["ok"])

    def test_already_passing_runs_nothing(self):
        self.ctx.host.responses[("rpm", "-q", "ksh")] = RunResult(0, "ksh-1\n", "")
        status, d = self.fix({"name": "pkg-ksh"})
        self.assertEqual(status, 200)
        self.assertIsNone(d["fix"])
        self.assertNotIn(["dnf", "install", "-y", "ksh"], self.ctx.host.calls)
        self.assertEqual(self.events(), [e for e in self.events() if e["event"] != "check_fix_start"])

    def test_refusals(self):
        self.assertEqual(self.fix({"name": "nope"})[0], 404)
        self.assertEqual(self.fix({"name": "upgrade-only", "phase": "stage"})[0], 404)
        self.assertEqual(self.fix({"name": "sdi-mode"})[0], 400)
        self.assertEqual(self.fix({"name": "pkg-ksh", "phase": "weird"})[0], 400)

    def test_blocked_while_upgrade_running(self):
        self.ctx.host.responses.update(running_responses(True))
        status, d = self.fix({"name": "pkg-ksh"})
        self.assertEqual((status, d["error"]), (409, "upgrade in progress"))

    def test_blocked_while_snapshot_busy(self):
        class BusyRunner(object):
            def busy(self):
                return True
        self.ctx = self.ctx._replace(runner=BusyRunner())
        status, d = self.fix({"name": "pkg-ksh"})
        self.assertEqual((status, d["error"]), (409, "report snapshot in progress"))

    def test_one_fix_at_a_time(self):
        self.assertTrue(api._FIX_LOCK.acquire(False))
        try:
            status, d = self.fix({"name": "pkg-ksh"})
        finally:
            api._FIX_LOCK.release()
        self.assertEqual((status, d["error"]), (409, "another fix is running"))
```

Run: `python3 -m unittest tests.test_api 2>&1 | tail -3` → failures (404 from the dispatcher).

- [ ] **Step 3: Implement** — in `nfupgrader/api.py`:

3a. `import threading` with the other imports, and below `Context.__new__.__defaults__` add:

```python
# One check fix at a time: fixes change system state as root (dnf, sysctl, limits).
_FIX_LOCK = threading.Lock()
```

3b. Add the route just before `if path == "/api/checks" and method == "GET":`:

```python
    if path == "/api/checks/fix" and method == "POST":
        try:
            payload = json.loads(body.decode("utf-8") or "{}")
        except ValueError:
            return _json(400, {"error": "bad json"})
        if launcher.upgrader_running(ctx.host):
            return _json(409, {"error": "upgrade in progress"})
        if _snapshot_busy(ctx):
            return _json(409, {"error": "report snapshot in progress"})
        phase = payload.get("phase") or _active_phase(ctx)
        if phase not in ("stage", "upgrade"):
            return _json(400, {"error": "phase must be stage or upgrade"})
        name = payload.get("name") or ""
        check = checks.find_check(_load_checks(ctx), name, "preflight", phase, _machtype(ctx))
        if check is None:
            return _json(404, {"error": "unknown check"})
        what = checks.describe_fix(check)
        if what is None:
            return _json(400, {"error": "no fix for this check"})
        before = checks.evaluate(ctx.host, check, "preflight")
        if before["passed"]:
            return _json(200, {"fix": None, "result": before})
        if not _FIX_LOCK.acquire(False):
            return _json(409, {"error": "another fix is running"})
        try:
            ctx.audit.record("check_fix_start", name=name, kind=check["kind"], fix=what,
                             client=client_ip)
            ok, detail = checks.apply_fix(ctx.host, check)
            ctx.audit.record("check_fix", name=name, kind=check["kind"], ok=ok, detail=detail,
                             client=client_ip)
            after = checks.evaluate(ctx.host, check, "preflight")
        finally:
            _FIX_LOCK.release()
        return _json(200, {"fix": {"ok": ok, "detail": detail}, "result": after})
```

- [ ] **Step 4: Run** — test_api OK; full suite OK (both locales).

- [ ] **Step 5: Commit** — `git add nfupgrader/api.py tests/test_api.py tests/fixtures/checks.fix.json && git commit -m "feat(api): POST /api/checks/fix applies a check's fix and re-runs it"` (+ trailer).

---

### Task 3: Fix buttons in the pre-flight table

**Files:** Modify `nfupgrader/web/index.html`, `nfupgrader/web/app.js`; Test `tests/test_web_assets.py`

- [ ] **Step 1: Failing tests** — add `"fix-msg",` to `REQUIRED_IDS`, and:

```python
    def test_preflight_fix_buttons(self):
        self.assertIn("esc(r.fix)", self.js)
        self.assertIn("function runFix(", self.js)
        self.assertIn('postJSON("/api/checks/fix"', self.js)
        self.assertIn("Run as root on ", self.js)
        fix_fn = self.js.split("function runFix(", 1)[1].split("\nfunction ", 1)[0]
        self.assertIn("runPreflight();", fix_fn)
```

- [ ] **Step 2: `index.html`** — directly after `<div id="preflight"></div>` add `<div id="fix-msg"></div>`.

- [ ] **Step 3: `app.js`**

3a. Replace `renderChecks` (escapes every value; adds a Fix column in the pre-flight table):

```js
function renderChecks(el, data, overridable) {
  var html = "<table class='checks'><tr><th></th><th>Check</th><th>Detail</th>" +
             (overridable ? "<th>Override</th><th>Fix</th>" : "") + "</tr>";
  data.results.forEach(function (r) {
    var mark = r.passed ? "✅" : (r.severity === "ERROR" ? "❌" : "⚠️");
    var extra = "";
    if (overridable) {
      extra = "<td>" + (r.blocking
        ? "<input type='checkbox' class='ovr' data-name='" + esc(r.name) + "'>" : "") + "</td>" +
        "<td>" + (r.fix
        ? "<button type='button' class='fix' data-name='" + esc(r.name) + "' data-fix='" +
          esc(r.fix) + "' title='" + esc(r.fix) + "'>Fix</button>" : "") + "</td>";
    }
    html += "<tr class='" + (r.passed ? "pass" : "fail-" + esc(r.severity)) + "'>" +
            "<td>" + mark + "</td><td>" + esc(r.name) + " — " + esc(r.description) + "</td>" +
            "<td>" + esc(r.detail || "") + "</td>" + extra + "</tr>";
  });
  html += "</table>";
  el.innerHTML = html;
}
```

3b. Add after `runPreflight()`:

```js
function runFix(btn) {
  var name = btn.getAttribute("data-name");
  var host = lastInfo && lastInfo.host ? lastInfo.host : "this host";
  if (!window.confirm("Run as root on " + host + ":\n\n" + btn.getAttribute("data-fix") +
                      "\n\nContinue?")) return;
  Array.prototype.forEach.call(document.querySelectorAll("#preflight button.fix"),
                               function (b) { b.disabled = true; });
  btn.textContent = "Fixing…";
  var msg = document.getElementById("fix-msg");
  msg.textContent = "Fixing " + name + "…";
  postJSON("/api/checks/fix", { name: name, phase: document.getElementById("mode").value },
    function (status, d) {
      if (status === 200 && d.fix) msg.textContent = (d.fix.ok ? "✅ " : "❌ ") + name + ": " + d.fix.detail;
      else if (status === 200) msg.textContent = "✅ " + name + ": already passing";
      else msg.textContent = "❌ " + name + ": " + (d.error || ("HTTP " + status));
      runPreflight();
    });
}
```

3c. Binding, next to `document.getElementById("preflight").onchange = updateLaunchState;`:

```js
document.getElementById("preflight").onclick = function (ev) {
  var b = ev.target.closest ? ev.target.closest("button.fix") : null;
  if (b && !b.disabled) runFix(b);
};
```

3d. In `resetPreflight()` also clear `document.getElementById("fix-msg").textContent = "";`.

- [ ] **Step 4: Verify** — `node --check nfupgrader/web/app.js`; ES5 grep clean; full suite OK (both locales); `sh test/nf_fork_verify.sh | tail -1` → VERIFY OK.

- [ ] **Step 5: Commit** — `git add nfupgrader/web/index.html nfupgrader/web/app.js tests/test_web_assets.py && git commit -m "feat(ui): Fix buttons on failed pre-flight checks"` (+ trailer).

---

### Task 4: Live check (loopback, no real system change)

- [ ] Run the server with `NFU_UPGRADE_PATH=/bin/true` and a scratch `NFU_CHECKS` holding one `touchfile` check for a flag that doesn't exist, and a scratch `PATH` whose `scmd`… — simpler: verify via the API only that a failing `rpm_installed` check for a nonexistent package reports `fix: "dnf install -y <pkg>"`, and **do not** POST the fix on the dev box (it would really run dnf as the service user). The fix path itself is covered by unit tests.
