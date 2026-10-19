# nfupgrader New-Install Mode Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** When netFLEX is not installed on the local host, nfupgrader offers an Install flow — pre-flight checks with a non-overridable "not installed" hard stop, an install form (RPM, Singlebox/FEP/BEP, mate, master, web GUI, license stub) that generates `cnc.cnfg`, a detached ksh installer with progress and log, and resume / reset / report for an install that dies part-way.

**Architecture:** A new ksh script `nf_install` (beside the untouched forks) does the install, resume (`-r`) and reset (`-x`), writing its own `.${HOST}_INSTALL_STATE` / `.${HOST}_INSTALL_INFO` and `install_inc_` / `reset_inc_` traces. Python adds `cnccnfg.py` (cnc.cnfg + /etc/hosts), `installstate.py` (installed? / host mode / install state), an `install` phase in `steps`/`progress`/`checks`/`cfg`, `nf_install` liveness and launch in `launcher`, and install-aware endpoints in `api`/`httpd`. The web UI switches Config and the Upgrade tab to install mode from `/api/install/status`.

**Tech Stack:** Python 3.6.8 stdlib (`.format()`, no f-strings, `subprocess.run(..., stdout=PIPE, stderr=PIPE, universal_newlines=True)`), ksh88-safe shell, ES5 JS, `unittest`.

**Spec:** `~/WorkNotes/design/2026-10-19-nfupgrader-new-install-design.md`

Repo: `~/Git/nfupgrader`. Ignore BASE/VPATH warnings (not an nmake repo).
Python tests: `cd ~/Git/nfupgrader && timeout 180 python3 -m unittest discover -s tests -p 'test_*.py' 2>&1 | tail -3` — baseline **381 OK**; also run with `LC_ALL=C`.
Shell tests: `ksh test/nf_log_test.ksh`, `ksh test/nf_install_test.ksh` (new), `sh test/nf_fork_verify.sh` → `VERIFY OK`.

Landing: work on branch `new-install`; commit per task on the branch. Do **not** merge to `main` until Dan says "commit and merge" (then ff-merge, re-run tests on main, delete branch; no remote).

## Global Constraints

- Shipped shell runs under `/usr/bin/ksh`, **ksh88-safe**: no `PIPESTATUS`, no `set -o pipefail`, no arrays; wrap sub-commands with `run_logged`; log only via `loginfo`/`logwarn`/`logerror`/`logfatal` with a quoted message.
- Python 3.6.8 stdlib only; `.format()`; no f-strings, no `capture_output`/`text=`.
- `nf_upgrade` / `nf_upgrader` are **not modified**; `sh test/nf_fork_verify.sh` must print `VERIFY OK`.
- The service runs as root: no `sudo` anywhere. Commands nf-install runs as cmm use `su cmm -c '<cmd>'`.
- No disk setup (`pvcreate`/`vgcreate`/`lvcreate`/`mkfs`/fstab). No netFLEX start. No EMS/NMS/queues/scmds/password modes/Apache rewrite.
- Load dir naming: `${LOADROOT}/inc${NEW_GENERIC}${NEW_LOAD}` with LOADROOT `/usr4` (e.g. `/usr4/inc5.3.002`).
- The install never writes `${INCLOGDIR}/.${HOSTNAME}_STATE` (nf_upgrader treats it as a resume point, `nf_upgrader:1727`).
- ES5 JS only (RHEL 7 Firefox): no arrow functions, `let`/`const`, template literals, `inset:`; no external resources; every element `app.js` looks up at startup must appear in `index.html` before the `<script>` tag.

## Review Focus

1. **Mate host typed with spaces, empty, or equal to this host** — cnc.cnfg is space-separated, so a name with whitespace corrupts it; expect an ERROR in validation, never a written file. (Task 3 `test_install_rejects_bad_mate`.)
2. **Service restarted mid-install** (`ctx.state` empty) — the page must still show install mode, the install progress and a running install, from the state file and `pgrep nf_install`. (Task 8 `test_active_phase_install_from_state_file_after_restart`.)
3. **Stale page launches Install after netFLEX got installed** — expect 409 with the hard check, no override possible even when named in `overrides`. (Task 8 `test_launch_install_hard_stop_not_overridable`.)
4. **Reset when `/usr/cnc` points at a different load than the install recorded** (someone fixed it by hand) — expect reset to refuse and remove nothing. (Task 7 `reset refuses foreign link`.)
5. **An old `isdone` install state on a box that was since wiped** — a fresh install must archive it and proceed, not resume or refuse. (Task 6 `stale done state archived`.)

---

### Task 1: Check engine — install phase, hard checks, new kinds, default list

**Files:**
- Modify: `nfupgrader/checks.py` (`_applies`, `evaluate`, `KINDS`, `FIXES`, `_FIX_ARGS_OK`; new helpers)
- Modify: `nfupgrader/checks.default.json`
- Test: `tests/test_checks.py`

**Interfaces:**
- Produces:
  - A check's `"phase"` may be a string (`"any"`, `"stage"`, `"upgrade"`, `"install"`) or a list of those; `"any"` matches every phase including `install`.
  - `evaluate(...)` result gains `"hard": bool` (true iff the definition has `"hard": true`).
  - `hard_failures(results) -> list` — results with `hard` and not `passed`.
  - `install_postflight(host, check_list) -> list` — every `rpm_installed` check that applies to `("preflight", "install")`, evaluated with `when="postflight"`.
  - Kinds: `netflex_absent` (params `pkg` default `"netFLEX-CORE"`, `path` default `"/usr/cnc"`; runs `["rpm","-q",pkg]` and `["sh","-c","test -e {path} || test -L {path}"]`; passes iff both rc ≠ 0; detail `"not installed"` / `"netFLEX-CORE installed"` / `"/usr/cnc exists"`), `mountpoint` (params `path`; runs `["mountpoint","-q",path]`; passes iff rc 0; detail `"<path> mounted"` / `"<path> is not a mount point"`), `path_mode` (params `path`, `mode`; runs `["stat","-c","%a",path]`; passes iff stripped output == mode; detail `"<path> mode <found>"`).
  - Fix for `path_mode`: runs `["chmod", mode, path]`; describe `"chmod <mode> <path>"`; args OK only if `path` starts with `/` and `mode` matches `^[0-7]{3,4}$`.

- [ ] **Step 1: Write failing tests** in `tests/test_checks.py`:

```python
class TestInstallPhase(unittest.TestCase):
    def test_phase_list_and_any_match_install(self):
        c_any = {"when": "preflight", "phase": "any", "kind": "file_exists", "params": {"path": "/x"}}
        c_su = {"when": "preflight", "phase": ["stage", "upgrade"], "kind": "file_exists", "params": {"path": "/x"}}
        c_in = {"when": "preflight", "phase": "install", "kind": "file_exists", "params": {"path": "/x"}}
        self.assertTrue(checks._applies(c_any, "preflight", "install", None))
        self.assertFalse(checks._applies(c_su, "preflight", "install", None))
        self.assertTrue(checks._applies(c_su, "preflight", "upgrade", None))
        self.assertTrue(checks._applies(c_in, "preflight", "install", None))
        self.assertFalse(checks._applies(c_in, "preflight", "stage", None))

    def test_netflex_absent(self):
        absent = FakeHost({})          # every command rc 1
        r = checks.evaluate(absent, {"name": "x", "kind": "netflex_absent", "hard": True}, "preflight")
        self.assertTrue(r["passed"]); self.assertTrue(r["hard"])
        rpm = FakeHost({("rpm", "-q", "netFLEX-CORE"): RunResult(0, "netFLEX-CORE-5.4.0-29\n", "")})
        r = checks.evaluate(rpm, {"name": "x", "kind": "netflex_absent", "hard": True}, "preflight")
        self.assertFalse(r["passed"]); self.assertTrue(r["blocking"])
        link = FakeHost({("sh", "-c", "test -e /usr/cnc || test -L /usr/cnc"): RunResult(0, "", "")})
        self.assertFalse(checks.evaluate(link, {"name": "x", "kind": "netflex_absent"}, "preflight")["passed"])

    def test_hard_failures(self):
        rs = [{"hard": True, "passed": False, "name": "a"}, {"hard": False, "passed": False, "name": "b"},
              {"hard": True, "passed": True, "name": "c"}]
        self.assertEqual([r["name"] for r in checks.hard_failures(rs)], ["a"])

    def test_mountpoint_and_path_mode(self):
        h = FakeHost({("mountpoint", "-q", "/usr4"): RunResult(0, "", ""),
                      ("stat", "-c", "%a", "/usr4"): RunResult(0, "755\n", "")})
        self.assertTrue(checks.evaluate(h, {"name": "m", "kind": "mountpoint", "params": {"path": "/usr4"}}, "preflight")["passed"])
        r = checks.evaluate(h, {"name": "p", "kind": "path_mode", "params": {"path": "/usr4", "mode": "775"}}, "preflight")
        self.assertFalse(r["passed"]); self.assertEqual(r["fix"], "chmod 775 /usr4")

    def test_path_mode_fix_runs_chmod_and_rejects_bad_args(self):
        h = FakeHost({("chmod", "775", "/usr4"): RunResult(0, "", "")})
        ok, _ = checks.apply_fix(h, {"kind": "path_mode", "params": {"path": "/usr4", "mode": "775"}})
        self.assertTrue(ok); self.assertIn(["chmod", "775", "/usr4"], h.calls)
        self.assertIsNone(checks.describe_fix({"kind": "path_mode", "params": {"path": "usr4", "mode": "775"}}))
        self.assertIsNone(checks.describe_fix({"kind": "path_mode", "params": {"path": "/usr4", "mode": "-R"}}))

    def test_install_postflight_reevaluates_packages_only(self):
        lst = [{"name": "ksh", "when": "preflight", "phase": "any", "kind": "rpm_installed", "params": {"pkg": "ksh"}},
               {"name": "gr", "when": "preflight", "phase": ["stage", "upgrade"], "kind": "rpm_installed", "params": {"pkg": "x"}},
               {"name": "fw", "when": "preflight", "phase": "any", "kind": "service_active", "params": {"name": "firewalld"}}]
        rs = checks.install_postflight(FakeHost({}), lst)
        self.assertEqual([r["name"] for r in rs], ["ksh"])
        self.assertFalse(rs[0]["blocking"])

class TestDefaultChecksInstall(unittest.TestCase):
    def setUp(self):
        self.lst = checks.load_checks(os.path.join(os.path.dirname(checks.__file__), "checks.default.json"))

    def names(self, phase):
        return {c["name"] for c in self.lst if checks._applies(c, "preflight", phase, None)}

    def test_install_preflight_set(self):
        n = self.names("install")
        for want in ("netflex-absent", "usr4-mounted", "usr4-mode", "disk-usr4", "ksh", "apache",
                     "tmux", "lvm2", "firewall", "nproc_soft", "nproc_hard"):
            self.assertIn(want, n)
        for gone in ("gr-idle", "no-grestore", "ems_mode", "nms_mode", "dbchg_mode", "hide_passwd_mode",
                     "no_passwd_mode", "del_chk_ties_mode", "curlhttps", "msditestmode",
                     "dbcheck-clean", "staged-present"):
            self.assertNotIn(gone, n)

    def test_upgrade_preflight_unchanged_by_install_entries(self):
        self.assertNotIn("netflex-absent", self.names("upgrade"))
        self.assertIn("gr-idle", self.names("stage"))
        self.assertIn("ems_mode", self.names("upgrade"))

    def test_netflex_absent_is_hard(self):
        c = [c for c in self.lst if c["name"] == "netflex-absent"][0]
        self.assertTrue(c["hard"]); self.assertEqual(c["phase"], "install")

    def test_nproc_has_fallback_value(self):
        for name in ("nproc_soft", "nproc_hard"):
            c = [c for c in self.lst if c["name"] == name][0]
            self.assertEqual(c["params"]["expected_value"], "32768")
            self.assertEqual(checks._ulimit_expected(dict(c["params"], reference=["/nonexistent"]))[0], "32768")
```

- [ ] **Step 2: Run, verify failures**

Run: `python3 -m unittest tests.test_checks -v 2>&1 | tail -20` — Expected: the new tests FAIL/ERROR (`hard_failures`, `install_postflight`, kinds missing).

- [ ] **Step 3: Implement** in `checks.py`: phase-list handling in `_applies`; `"hard"` in `evaluate`'s result; `hard_failures`; `install_postflight`; the three kinds in `KINDS`; `path_mode` in `FIXES` and `_FIX_ARGS_OK`.
  In `checks.default.json`: set `"phase": ["stage", "upgrade"]` on `gr-idle`, `no-grestore`, `ems_mode`, `nms_mode`, `dbchg_mode`, `hide_passwd_mode`, `no_passwd_mode`, `del_chk_ties_mode`, `curlhttps`, `msditestmode`; add `"expected_value": "32768"` to both nproc checks' params; add after `disk-usr4`:

```json
  {"name": "netflex-absent", "category": "NETFLEX", "description": "netFLEX is not already installed",
   "fail_text": "ERROR", "when": "preflight", "phase": "install", "hard": true,
   "kind": "netflex_absent", "params": {}},
  {"name": "usr4-mounted", "category": "SYSTEM", "description": "/usr4 is a mounted filesystem",
   "fail_text": "ERROR", "when": "preflight", "phase": "install",
   "kind": "mountpoint", "params": {"path": "/usr4"}},
  {"name": "usr4-mode", "category": "SYSTEM", "description": "/usr4 is mode 775",
   "fail_text": "ERROR", "when": "preflight", "phase": "install",
   "kind": "path_mode", "params": {"path": "/usr4", "mode": "775"}},
```

- [ ] **Step 4: Run full Python suite** — Expected: all pass (381 + new).
- [ ] **Step 5: Commit** — `git add nfupgrader/checks.py nfupgrader/checks.default.json tests/test_checks.py && git commit -m "feat(checks): install phase, hard checks, netflex_absent/mountpoint/path_mode kinds"`

---

### Task 2: `cnccnfg.py` — generate cnc.cnfg and read /etc/hosts

**Files:**
- Create: `nfupgrader/cnccnfg.py`
- Test: `tests/test_cnccnfg.py`

**Interfaces:**
- Produces:
  - `BOX_TYPES = ("SINGLE", "FEP", "BEP")`
  - `valid_hostname(name) -> bool` — `^[A-Za-z0-9][A-Za-z0-9._-]*$`.
  - `render_cnc_cnfg(boxtype, thishost, mate=None, master=None) -> str` — exact text per spec §4 (header lines verbatim from `~/Git/nf-install/nf/*.cnc.cnfg`; SINGLE has no machine lines; multibox FEP line `"<fep> 1 <bep> 1 1 9"` then BEP line `"<bep> 1 <fep> 1 0 0"`; trailing newline). Raises `ValueError` for an unknown box type, an invalid host name, mate missing/equal to thishost for FEP/BEP, or master not in {thishost, mate}.
  - `render_from_values(values, thishost) -> str` — from cfg values `BOXTYPE`, `MATEHOST`, `MASTERHOST` (stripped); same `ValueError`s.
  - `parse_etc_hosts(text) -> list of {"name", "ip"}` — skips blank/comment lines and trailing `#` comments, skips IPs starting `127.` and `::1`, yields every name/alias in order, first occurrence of a name wins.
  - `read_etc_hosts(path="/etc/hosts") -> list` — `[]` if unreadable.

- [ ] **Step 1: Write failing tests** `tests/test_cnccnfg.py`:

```python
import os, tempfile, unittest
from nfupgrader import cnccnfg, site

class TestRender(unittest.TestCase):
    def test_single(self):
        self.assertEqual(cnccnfg.render_cnc_cnfg("SINGLE", "h1"),
            "# SingleBox (MUST be MultiBox or Single)\n# SystemPrompt=netFLEX\n"
            "# SystemString=INTEGRATED NETWORK CONTROLLER\n# Master Machine Name\nh1\n"
            "# Mach Mach-# Mate Mate-# Mach-Type (CNC=1 Mux=0) Override-Link (CNC)\n")

    def test_bep_puts_fep_line_first(self):
        t = cnccnfg.render_cnc_cnfg("BEP", "b1", mate="f1", master="f1")
        self.assertTrue(t.startswith("# MultiBox"))
        self.assertTrue(t.endswith("\nf1\n# Mach Mach-# Mate Mate-# Mach-Type (CNC=1 Mux=0) Override-Link (CNC)\n"
                                   "f1 1 b1 1 1 9\nb1 1 f1 1 0 0\n"))

    def test_round_trips_through_site_parser(self):
        hosts = site.parse_cnfg(cnccnfg.render_cnc_cnfg("FEP", "f1", mate="b1", master="b1"))
        self.assertEqual([(h.name, h.role, h.mate) for h in hosts], [("f1", "FEP", "b1"), ("b1", "BEP", "f1")])
        self.assertEqual(site.local_host(hosts, "b1").role, "BEP")

    def test_rejects(self):
        for args in (("XEP", "h1"), ("FEP", "h1"), ("FEP", "h1", "h1", "h1"), ("FEP", "h1", "b 1", "h1"),
                     ("FEP", "h1", "b1", "zz")):
            with self.assertRaises(ValueError):
                cnccnfg.render_cnc_cnfg(*args)

    def test_from_values_strips(self):
        t = cnccnfg.render_from_values({"BOXTYPE": "FEP", "MATEHOST": " b1 ", "MASTERHOST": "f1"}, "f1")
        self.assertIn("f1 1 b1 1 1 9\n", t)

class TestEtcHosts(unittest.TestCase):
    def test_parse(self):
        text = ("127.0.0.1 localhost localhost.localdomain\n::1 localhost6\n# c\n\n"
                "10.0.0.2 nyc-bep1 bep1.example  # mate\n10.0.0.3 nyc-fep2\n10.0.0.9 nyc-bep1\n")
        self.assertEqual(cnccnfg.parse_etc_hosts(text),
                         [{"name": "nyc-bep1", "ip": "10.0.0.2"}, {"name": "bep1.example", "ip": "10.0.0.2"},
                          {"name": "nyc-fep2", "ip": "10.0.0.3"}])

    def test_read_missing(self):
        self.assertEqual(cnccnfg.read_etc_hosts("/nonexistent/hosts"), [])
```

- [ ] **Step 2: Run** `python3 -m unittest tests.test_cnccnfg -v` — Expected: ImportError.
- [ ] **Step 3: Implement `nfupgrader/cnccnfg.py`** with the interfaces above.
- [ ] **Step 4: Run** — Expected: PASS; full suite green.
- [ ] **Step 5: Commit** — `git add nfupgrader/cnccnfg.py tests/test_cnccnfg.py && git commit -m "feat(install): cnc.cnfg generator and /etc/hosts reader"`

---

### Task 3: `cfg.py` — install fields and mode-aware validation

**Files:**
- Modify: `nfupgrader/cfg.py`
- Test: `tests/test_cfg.py`

**Interfaces:**
- Consumes: `cnccnfg.valid_hostname`, `cnccnfg.BOX_TYPES`.
- Produces:
  - Every `FIELDS` entry gains `"modes"`. Both modes: `INCLOGDIR`, `NEW_GENERIC`, `NEW_LOAD`, `DEPOTFILE`, `INSTALLWEBGUI`, `PROMPTFORVALIDATE`. Upgrade-only: all other existing fields. New install-only fields, appended before `PROMPTFORVALIDATE`:
    `{"name": "BOXTYPE", "required": True, "kind": "enum", "values": ["SINGLE","FEP","BEP"], "modes": ["install"]}`,
    `{"name": "MATEHOST", "required": False, "kind": "string", "modes": ["install"]}`,
    `{"name": "MASTERHOST", "required": False, "kind": "string", "modes": ["install"]}`,
    `{"name": "CNCCNFG", "required": False, "kind": "path", "modes": ["install"], "hidden": True}`.
  - `fields_for(mode) -> list` — FIELDS entries whose `modes` contain mode (`ValueError` for other modes).
  - `validate(values, mode="upgrade", hostname=None) -> list` — required/enum/int rules apply only to `fields_for(mode)`. Install adds: for FEP/BEP, `MATEHOST` required, must be `valid_hostname`, must differ from `hostname`; `MASTERHOST` required and must be `hostname` or `MATEHOST` (all ERRORs, field-named). Existing upgrade behaviour unchanged.

- [ ] **Step 1: Write failing tests** in `tests/test_cfg.py`:

```python
class TestInstallMode(unittest.TestCase):
    BASE = {"INCLOGDIR": "/l", "NEW_GENERIC": "5.3.0", "NEW_LOAD": "02", "DEPOTFILE": "/d/x.rpm"}

    def test_fields_for(self):
        inst = [f["name"] for f in cfg.fields_for("install")]
        self.assertIn("BOXTYPE", inst); self.assertIn("INSTALLWEBGUI", inst)
        self.assertNotIn("UNLINKDIR", inst); self.assertNotIn("RESTARTINC", inst)
        self.assertNotIn("BOXTYPE", [f["name"] for f in cfg.fields_for("upgrade")])

    def test_single_ok(self):
        self.assertEqual(cfg.validate(dict(self.BASE, BOXTYPE="SINGLE"), "install", "h1"), [])

    def test_upgrade_validation_ignores_install_fields(self):
        self.assertEqual([i for i in cfg.validate(self.BASE) if i["field"] == "BOXTYPE"], [])

    def test_install_rejects_bad_mate(self):
        for mate in ("", "h1", "b 1", " "):
            issues = cfg.validate(dict(self.BASE, BOXTYPE="FEP", MATEHOST=mate, MASTERHOST="h1"), "install", "h1")
            self.assertIn("MATEHOST", [i["field"] for i in issues if i["level"] == "ERROR"], mate)

    def test_master_must_be_one_of_pair(self):
        issues = cfg.validate(dict(self.BASE, BOXTYPE="BEP", MATEHOST="f1", MASTERHOST="x"), "install", "h1")
        self.assertIn("MASTERHOST", [i["field"] for i in issues])
        self.assertEqual(cfg.validate(dict(self.BASE, BOXTYPE="BEP", MATEHOST="f1", MASTERHOST="f1"), "install", "h1"), [])

    def test_render_includes_install_keys(self):
        out = cfg.render_cfg(dict(self.BASE, BOXTYPE="FEP", MATEHOST="b1", MASTERHOST="h1", CNCCNFG="/l/c"))
        for k in ('BOXTYPE="FEP"', 'MATEHOST="b1"', 'MASTERHOST="h1"', 'CNCCNFG="/l/c"'):
            self.assertIn(k, out)
```

- [ ] **Step 2: Run** `python3 -m unittest tests.test_cfg -v` — Expected: FAIL.
- [ ] **Step 3: Implement** in `cfg.py` per Interfaces.
- [ ] **Step 4: Run full suite** — Expected: green (existing `test_cfg`/`test_api` config tests unchanged).
- [ ] **Step 5: Commit** — `git commit -am "feat(cfg): install-mode fields and validation"` (add the test file).

---

### Task 4: Install state machine — `steps`, `progress`, `installstate`

**Files:**
- Modify: `nfupgrader/steps.py`, `nfupgrader/progress.py`
- Create: `nfupgrader/installstate.py`
- Test: `tests/test_steps.py`, `tests/test_progress.py`, `tests/test_installstate.py`

**Interfaces:**
- Produces:
  - `steps._INSTALL = ["isstart", "isprepare", "isrpm", "isunstage", "iscnfg", "ispackages", "iscmmperm", "islicmigrate", "ispostinstall", "isdatabase", "iswebgui", "islicense", "isdone"]`; `states_for("install")` works. Labels: `isstart` "Preliminary checks", `isprepare` "Create load directory and /usr/cnc links", `isrpm` "Install CORE RPM", `isunstage` "Remove /usr/cnc_stage link", `iscnfg` "Install cnc.cnfg", `ispackages` "Linux packages", `iscmmperm` "Set cmm permissions", `islicmigrate` "License migrate", `ispostinstall` "Post-install script", `isdatabase` "Database setup", `iswebgui` "Install web GUI", `islicense` "License file (not yet implemented)", `isdone` "Install complete".
  - `progress._TERMINAL["install"] = "isdone"`; `progress.state_file(hostname, phase) -> str` returns `".{h}_INSTALL_STATE"` for install, else `".{h}_STATE"`; `read_state(hostname, inclogdir, phase="stage")` reads that file; `read_progress` passes its `phase` through. Existing callers unchanged (default phase keeps `.{h}_STATE`).
  - `installstate.read_install_info(hostname, logdir) -> dict` — `KEY=VALUE` lines of `.{h}_INSTALL_INFO` (keys `LOADDIR`, `VERSION`, `DEPOTFILE`); `{}` if absent.
  - `installstate.unfinished(hostname, logdir) -> bool` — install state file exists, non-empty, and ≠ `isdone`.
  - `installstate.netflex_installed(host) -> bool` — `["rpm","-q","netFLEX-CORE"]` rc 0 or `["sh","-c","test -e /usr/cnc || test -L /usr/cnc"]` rc 0 (same commands as Task 1's kind).
  - `installstate.host_mode(host, hostname, logdir) -> "install" | "upgrade"` — `install` if `unfinished(...)` or not `netflex_installed(host)`; else `upgrade`.

- [ ] **Step 1: Write failing tests:**

```python
# tests/test_steps.py
    def test_install_states(self):
        s = steps.states_for("install")
        self.assertEqual((s[0], s[-1], len(s)), ("isstart", "isdone", 13))
        self.assertEqual(steps.label("iswebgui"), "Install web GUI")

# tests/test_progress.py
    def test_install_reads_its_own_state_file(self):
        d = tempfile.mkdtemp()
        open(os.path.join(d, ".h1_STATE"), "w").write("swscopycnc\n")
        open(os.path.join(d, ".h1_INSTALL_STATE"), "w").write("ispostinstall\n")
        p = progress.read_progress("h1", "install", d, running=False)
        self.assertEqual((p["current"], p["status"], p["step"], p["total"]), ("ispostinstall", "died", 9, 13))
        self.assertEqual(progress.read_progress("h1", "stage", d, False)["current"], "swscopycnc")

    def test_install_done(self):
        p = progress.compute_progress("install", "isdone", False)
        self.assertEqual((p["status"], p["completed"]), ("done", 13))

# tests/test_installstate.py
class TestInstallState(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
    def put(self, name, text):
        open(os.path.join(self.d, name), "w").write(text)
    def test_info(self):
        self.put(".h1_INSTALL_INFO", "LOADDIR=/usr4/inc5.3.002\nVERSION=5.3.0.02\n")
        self.assertEqual(installstate.read_install_info("h1", self.d)["LOADDIR"], "/usr4/inc5.3.002")
        self.assertEqual(installstate.read_install_info("h2", self.d), {})
    def test_unfinished(self):
        self.assertFalse(installstate.unfinished("h1", self.d))
        self.put(".h1_INSTALL_STATE", "isrpm\n");  self.assertTrue(installstate.unfinished("h1", self.d))
        self.put(".h1_INSTALL_STATE", "isdone\n"); self.assertFalse(installstate.unfinished("h1", self.d))
    def test_host_mode(self):
        rpm = {("rpm", "-q", "netFLEX-CORE"): RunResult(0, "x", "")}
        self.assertEqual(installstate.host_mode(FakeHost({}), "h1", self.d), "install")
        self.assertEqual(installstate.host_mode(FakeHost(rpm), "h1", self.d), "upgrade")
        self.put(".h1_INSTALL_STATE", "isdatabase\n")
        self.assertEqual(installstate.host_mode(FakeHost(rpm), "h1", self.d), "install")
```

- [ ] **Step 2: Run** the three modules — Expected: FAIL.
- [ ] **Step 3: Implement** per Interfaces.
- [ ] **Step 4: Run full suite** — Expected: green.
- [ ] **Step 5: Commit** — `git commit -m "feat(install): install state machine, progress and host mode"` (add all touched files).

---

### Task 5: `launcher.py` — nf_install liveness and launch

**Files:**
- Modify: `nfupgrader/launcher.py`
- Test: `tests/test_launcher.py`

**Interfaces:**
- Produces:
  - `is_script_argv(argv, script) -> bool` — today's `is_upgrader_argv` logic generalized; `is_upgrader_argv(argv)` stays as `is_script_argv(argv, "nf_upgrader")`.
  - `script_running(host, script) -> bool` — today's `upgrader_running` logic (`pgrep -f <script>` + `/proc/<pid>/cmdline`); `upgrader_running(host)` kept as a wrapper; `installer_running(host)` = `script_running(host, "nf_install")`; `anything_running(host)` = either.
  - `INSTALL_ACTIONS = {"install": "-i", "resume": "-r", "reset": "-x"}`; `build_install_argv(nf_install_path, config_path, action) -> list` = `[path, "-c", config_path, flag]`, `ValueError` on unknown action; `launch_install(nf_install_path, config_path, action, cwd=None) -> Popen` (detached exactly like `launch`).

- [ ] **Step 1: Write failing tests:**

```python
    def test_install_argv(self):
        self.assertEqual(launcher.build_install_argv("/o/nf_install", "/l/e.cfg", "resume"),
                         ["/o/nf_install", "-c", "/l/e.cfg", "-r"])
        with self.assertRaises(ValueError):
            launcher.build_install_argv("/o/nf_install", "/l/e.cfg", "stage")

    def test_installer_running(self):
        h = FakeHost({("pgrep", "-f", "nf_install"): RunResult(0, "77\n", ""),
                      ("cat", "/proc/77/cmdline"): RunResult(0, "/usr/bin/ksh\0/opt/nfupgrader/nf_install\0-c\0x\0-i\0", "")})
        self.assertTrue(launcher.installer_running(h))
        self.assertTrue(launcher.anything_running(h))
        self.assertFalse(launcher.upgrader_running(h))

    def test_editor_on_nf_install_is_not_running(self):
        self.assertFalse(launcher.is_script_argv(["vi", "nf_install"], "nf_install"))
        self.assertFalse(launcher.is_script_argv(["/usr/bin/ksh", "/o/nf_installer"], "nf_install"))
```

- [ ] **Step 2: Run** `python3 -m unittest tests.test_launcher -v` — Expected: FAIL.
- [ ] **Step 3: Implement** per Interfaces.
- [ ] **Step 4: Run full suite** — Expected: green.
- [ ] **Step 5: Commit** — `git commit -m "feat(launcher): nf_install liveness and launch"`.

---

### Task 6: `nf_install` — fresh install and resume

**Files:**
- Create: `nf_install` (executable, `#! /usr/bin/ksh`)
- Create: `test/nf_install_test.ksh`
- Modify: `test/nf_fork_verify.sh` (add `nf_install` to the syntax loop §1 and the grammar loop §4 only; §2/§3 untouched)

**Interfaces:**
- Consumes: `nf_log.ksh` (`loginfo`/`logwarn`/`logerror`/`logfatal`/`run_logged`); the cfg keys `INCLOGDIR`, `NEW_GENERIC`, `NEW_LOAD`, `DEPOTFILE`, `CNCCNFG`, `INSTALLWEBGUI`.
- Produces (contract the Python side relies on):
  - Usage: `nf_install -c <cfg> -i|-r|-x [-h]`. Sources the cfg. Unless `NFI_FOREGROUND=YES` or `NFI_CHILD` is set, re-runs itself in the background (`NFI_CHILD=1 nohup "$0" "$@" </dev/null >/dev/null 2>&1 &`) and exits 0, like `nf_upgrade` backgrounding `nf_upgrader`.
  - Roots overridable for tests: `INCROOT=${NFI_INCROOT:-/usr/cnc}`, `STAGEROOT=${NFI_STAGEROOT:-/usr/cnc_stage}`, `LOADROOT=${NFI_LOADROOT:-/usr4}`. `HOSTNAME=$(hostname)`.
  - Trace `${INCLOGDIR}/install_inc_${HOSTNAME}.$$` (`-i`/`-r`) or `reset_inc_${HOSTNAME}.$$` (`-x`); all output appended there (`exec >>"$TRACEFILE" 2>&1`).
  - State `${INCLOGDIR}/.${HOSTNAME}_INSTALL_STATE` holds the **step to run next** (crash-resume point, same convention as `nf_upgrader`): written `isstart` at fresh start, then after each step the next step's name; a fatal step exits 1 leaving the state at that step.
  - Info `${INCLOGDIR}/.${HOSTNAME}_INSTALL_INFO`: `LOADDIR=${LOADROOT}/inc${NEW_GENERIC}${NEW_LOAD}`, `VERSION=${NEW_GENERIC}.${NEW_LOAD}`, `DEPOTFILE=…`, written at fresh start.
  - `-i` with an unfinished state → FATAL "unfinished install … use resume or reset", exit 1, nothing changed. `-i` with state `isdone` → rename state and info to `<file>.<YYYYmmddHHMMSS>`, then fresh start.
  - `-r` requires an unfinished state (else FATAL, exit 1); reads `LOADDIR` from info; continues from the recorded step; **skips the isstart "not installed" guard** (isstart on resume only re-checks the `/usr4` mount).
  - Steps (each sub-command via `run_logged <tag> …`):
    - `isstart`: FATAL if `rpm -q netFLEX-CORE` succeeds or `$INCROOT` exists/is a link (fresh only); FATAL unless `mountpoint -q $LOADROOT`.
    - `isprepare`: `mkdir -p $LOADDIR`; for `$INCROOT` and `$STAGEROOT`: absent → `ln -s $LOADDIR`; a link already resolving (`readlink`) to `$LOADDIR` → keep; anything else → FATAL.
    - `isrpm`: if `rpm -q netFLEX-CORE` succeeds: compare `rpm -q --qf '%{VERSION}-%{RELEASE}' netFLEX-CORE` with `rpm -qp --qf '%{VERSION}-%{RELEASE}' $DEPOTFILE` — equal → INFO "already installed", else FATAL. Otherwise `dnf install -y $DEPOTFILE` (`yum` when `/usr/bin/dnf` is absent — overridable via `NFI_PKGTOOL` for tests); non-zero → FATAL.
    - `isunstage`: remove `$STAGEROOT` if it is a link; absent is fine; a real directory → FATAL.
    - `iscnfg`: `cp $CNCCNFG $INCROOT/features/cnc.cnfg`; failure → FATAL.
    - `ispackages`: `$INCROOT/bin/linux_packages` → FATAL on failure.
    - `iscmmperm`: `su cmm -c "$INCROOT/bin/cmmperm"` → FATAL on failure.
    - `islicmigrate`: `su cmm -c "$INCROOT/mbin/license-migrate"` → failure is `logwarn`, continue.
    - `ispostinstall`: `$INCROOT/bin/post-install.sh` → FATAL on failure.
    - `isdatabase`: `$INCROOT/bin/nf_upgrade_pg.sh` → FATAL on failure.
    - `iswebgui`: when `INSTALLWEBGUI` ≠ `NO`, the sequence in spec §6 step 11 (first `LINUXWEB64.*.tar` in `$INCROOT/GUI_REL`; `lwebsetup` fed `n`,`n` on stdin; maps/jsmap/sys_config/system_defines copies; write `Interfaces` with `[${HOSTNAME}_i]` / `   host=${HOSTNAME}` / `   protocol=ssh`; `chown cmm:cmm sys_config system_defines Interfaces`). No tar, or any failure → `logwarn`, continue. `NO` → `loginfo` "skipped".
    - `islicense`: `loginfo "license setup not yet implemented - skipped"`.
    - `isdone`: `loginfo` complete; exit 0.

- [ ] **Step 1: Write the failing ksh test** `test/nf_install_test.ksh` (source `nf_test_lib.ksh`). A `setup` function builds a sandbox `$SB` with `logs/`, `usr4/`, a cfg file (`INCLOGDIR=$SB/logs`, `NEW_GENERIC=5.3.0`, `NEW_LOAD=02`, `DEPOTFILE=$SB/netFLEX-CORE-5.3.0.02-1.x86_64.rpm`, `CNCCNFG=$SB/cnc.cnfg`, `INSTALLWEBGUI=YES`), and a `$SB/bin` of stubs prepended to `PATH`. Every stub appends its name and args to `$SB/calls`:
  - `hostname` prints `h1`; `mountpoint` exits 0.
  - `rpm`: `-q …` exits 0 iff `$SB/rpmdb` exists. The `--qf` forms print `5.3.0.02-1`. `-e` removes `$SB/rpmdb`.
  - `dnf` (via `NFI_PKGTOOL=$SB/bin/dnf`): touches `$SB/rpmdb` and creates in `$STAGEROOT` (which resolves to the load dir) `bin/` stubs `linux_packages`, `cmmperm`, `post-install.sh` and `nf_upgrade_pg.sh`, plus `mbin/license-migrate`, `features/`, and a `GUI_REL/LINUXWEB64.rhel8.tar` holding an `lwebsetup` stub. Each stub logs to `$SB/calls`; `post-install.sh` exits `${FAIL_POSTINSTALL:-0}` and `license-migrate` exits `${FAIL_LICMIG:-0}`.
  - `su` runs its `-c` argument with `sh -c`. `chown` exits 0.

  Run with `NFI_FOREGROUND=YES NFI_INCROOT=$SB/usr/cnc NFI_STAGEROOT=$SB/usr/cnc_stage NFI_LOADROOT=$SB/usr4`. Cases (`assert_*` labels):
  - `fresh install`: rc 0. State `isdone`. `$SB/usr/cnc` → `$SB/usr4/inc5.3.002`. `$SB/usr/cnc_stage` absent. `features/cnc.cnfg` equals `$SB/cnc.cnfg`. `$SB/calls` order: dnf, linux_packages, cmmperm, license-migrate, post-install.sh, nf_upgrade_pg.sh, lwebsetup. `web/data/Interfaces` contains `[h1_i]`. No `logs/.h1_STATE`. The trace `logs/install_inc_h1.*` has `INFO` lines and no `FATAL`.
  - `refuse installed`: `$SB/usr/cnc` pre-created as a directory. rc 1, trace has `FATAL`, no `dnf` in calls, state `isstart`.
  - `fail and resume`: `FAIL_POSTINSTALL=1` gives rc 1 and state `ispostinstall`. Then `-r` without the failure gives rc 0 and state `isdone`. `dnf` appears exactly once in calls.
  - `licmig warn`: `FAIL_LICMIG=1` gives rc 0, a `WARN` line in the trace, state `isdone`.
  - `webgui off`: `INSTALLWEBGUI=NO`, so no `lwebsetup` in calls.
  - `unfinished refuses -i`: state `isrpm`. `-i` gives rc 1 and the state stays `isrpm`.
  - `stale done state archived`: state `isdone`, nothing installed. `-i` gives rc 0. A `logs/.h1_INSTALL_STATE.<ts>` file exists, and the new state is `isdone`.

- [ ] **Step 2: Run** `ksh test/nf_install_test.ksh` — Expected: FAIL (no `nf_install`).
- [ ] **Step 3: Implement `nf_install`** per Interfaces, structured like `nf_upgrader`'s loop (`STATE=$(cat $STATEFILE)`; `while true; do case $STATE in …) do_x; STATE=next;; isdone) …; break;; esac; write_state $STATE; done`). Add `nf_install` to `nf_fork_verify.sh` §1 and §4 loops.
- [ ] **Step 4: Run** `ksh test/nf_install_test.ksh` (all ok), `ksh -n nf_install`, `sh test/nf_fork_verify.sh` → `VERIFY OK`, `ksh test/nf_log_test.ksh`.
- [ ] **Step 5: Commit** — `git add nf_install test/nf_install_test.ksh test/nf_fork_verify.sh && git commit -m "feat(install): nf_install script with resume"`

---

### Task 7: `nf_install -x` — reset

**Files:**
- Modify: `nf_install`
- Test: `test/nf_install_test.ksh`

**Interfaces:**
- Produces: `-x` requires an unfinished state (else FATAL, rc 1) and an info `LOADDIR` that starts with `${LOADROOT}/inc` (else FATAL). It then does the following, every sub-command via `run_logged`:
  1. Pre-check: for `$INCROOT` and `$STAGEROOT`, each must be absent, or a link whose `readlink` equals `LOADDIR`. Anything else gives FATAL and **removes nothing**.
  2. If `rpm -q netFLEX-CORE` succeeds, run `rpm -e netFLEX-CORE`. A failure is FATAL.
  3. Remove the two links.
  4. `rm -rf "$LOADDIR"`.
  5. Rename the state and info files to `<file>.<YYYYmmddHHMMSS>`.
  6. `loginfo` "reset complete". rc 0.

  Traces are kept.

- [ ] **Step 1: Add failing cases** to `test/nf_install_test.ksh`:
  - `reset after failure`: `FAIL_POSTINSTALL=1 -i`, then `-x`. Expect rc 0, `rpmdb` gone, both links gone, `usr4/inc5.3.002` gone, no `.h1_INSTALL_STATE`, an archived `.h1_INSTALL_STATE.*` present, and a `logs/reset_inc_h1.*` trace.
  - `reset refuses foreign link`: after a failed install, repoint `$SB/usr/cnc` at `$SB/usr4/other`. Expect `-x` rc 1, `rpmdb` still present, the load dir still present, and the state unchanged.
  - `reset refuses when done`: state `isdone`. Expect rc 1.
- [ ] **Step 2: Run** — Expected: the new cases FAIL.
- [ ] **Step 3: Implement** the `-x` path.
- [ ] **Step 4: Run** `ksh test/nf_install_test.ksh`, `sh test/nf_fork_verify.sh` — Expected: all ok, `VERIFY OK`.
- [ ] **Step 5: Commit** — `git commit -am "feat(install): nf_install reset"`.

---

### Task 8: API — install status, mode-aware progress/log/checks/fix/launch, reset request

**Files:**
- Modify: `nfupgrader/api.py`, `nfupgrader/logtail.py`
- Test: `tests/test_api.py`, `tests/test_logtail.py`

**Interfaces:**
- Consumes: Tasks 1, 4, 5.
- Produces:
  - `Context` gains trailing fields `nf_install_path`, `hosts_path`, so `Context.__new__.__defaults__ = (None,) * 13`.
  - `logtail.latest_of(inclogdir, phases, hostname) -> str`: the newest existing `<phase>_inc_<host>.<pid>` across `phases`, else `latest_trace(inclogdir, phases[0], hostname)`.
  - `api.host_mode(ctx)` = `installstate.host_mode(ctx.host, ctx.hostname, upgrade_logdir(ctx))`.
  - `_active_phase(ctx)`, in order:
    1. `ctx.state["phase"]` if it is in `("stage", "upgrade", "install")`.
    2. Else `"install"` if `host_mode(ctx) == "install"`.
    3. Else the existing rules.
  - `_active_tracefile`: for phase `install`, use `latest_of(logdir, ["install", "reset"], host)`.
  - `GET /api/install/status` returns `{"mode", "installed", "running", "unfinished", "state", "label", "info", "trace", "fatal"}`:
    - `installed` is `netflex_installed`.
    - `running` is `installer_running`.
    - `state` is the install state or None; `label` is `steps.label(state)` or `""`.
    - `info` is `read_install_info`.
    - `trace` is the newest install/reset trace path.
    - `fatal` is the last ≤10 `msg`s with severity ≥ ERROR from that trace (`logtail.tail(path, "ERROR", 10)`).
  - `/api/checks`: phase `install` is accepted. `when=postflight&phase=install` returns `checks.install_postflight(...)`.
  - `/api/checks/fix`: phase may be `install`, and the guard uses `launcher.anything_running`.
  - `/api/launch`:
    - Mode is one of `stage`/`upgrade`/`install`.
    - `install` needs `host_mode == "install"`, else 409 `"netFLEX is already installed"`. `stage`/`upgrade` need `host_mode == "upgrade"`, else 409 `"netFLEX is not installed"`.
    - The running guard uses `anything_running`.
    - For install, `action = "resume"` if `installstate.unfinished(...)` else `"install"`. On resume, hard results are dropped before blocking is computed.
    - Any remaining `checks.hard_failures` gives 409 `{"error": "netFLEX is already installed", "hard": [...]}` regardless of `overrides`.
    - 202 body is `{"accepted": True, "mode": mode, "action": action}`; action is None for stage/upgrade. Audit `launch` records the action.
  - `POST /api/install/reset` with `{reason}`:
    - 409 if not `unfinished`, or if `anything_running`, a fix is in progress or a snapshot is busy.
    - 400 if the reason is empty.
    - Otherwise audit `install_reset` with `reason` and `client`, and return 202 `{"accepted": True, "action": "reset"}`.
  - `/api/reports/run` and `_interrupted_notice` use `anything_running`.

- [ ] **Step 1: Write failing tests** in `tests/test_api.py`. First, in `make_ctx`, add `("rpm", "-q", "netFLEX-CORE"): RunResult(0, "netFLEX-CORE-5.4.0-29\n", "")`. Existing tests model an installed box, and without this `host_mode` would read them as `install`. Then add a new class `TestInstallApi`. It uses a helper `install_ctx(tmp, installed=False, state=None, running=False)` built from `make_ctx`:
  - `installed=False` adds `("rpm","-q","netFLEX-CORE")` → rc 1 and leaves the `sh -c test` unmocked (rc 1).
  - `state` writes `.h1_INSTALL_STATE`.
  - `running` mocks `pgrep -f nf_install` with a cmdline.
  - It uses a fixture checks file `tests/fixtures/checks.install.json` with `netflex-absent` (hard, install), one `rpm_installed` `ksh` (any), and one `["stage","upgrade"]` check.

  Tests:
  - `test_status_fresh_box` — mode `install`, installed False, unfinished False.
  - `test_active_phase_install_from_state_file_after_restart` — `ctx.state == {}`, state `isrpm`, `running=True`. `/api/progress` gives phase `install`, current `isrpm`, status `running`.
  - `test_checks_install_excludes_stage_upgrade` — the `["stage","upgrade"]` check is absent; `netflex-absent` is present with `hard` true.
  - `test_launch_install_ok` — 202, action `install`.
  - `test_launch_install_hard_stop_not_overridable` — `installed=True` (rpm rc 0) with `ctx.state["phase"]="install"`. 409 whether `overrides` names `netflex-absent` or not.
  - `test_launch_resume_skips_hard` — state `ispostinstall`, rpm installed. Gives 202 with action `resume`.
  - `test_launch_stage_refused_when_not_installed` — 409.
  - `test_reset_requires_reason_and_unfinished` — no state gives 409; state `isrpm` with an empty reason gives 400; with a reason gives 202, and the audit has `install_reset`.
  - `test_reset_refused_while_installing` — 409.
  - `test_status_reports_fatal_lines` — write `logs/install_inc_h1.123` containing an `ERROR` line and a `FATAL` line; `fatal` lists both msgs.

  In `tests/test_logtail.py`: `test_latest_of_picks_newest_across_phases` (set mtimes with `os.utime`).
- [ ] **Step 2: Run** `python3 -m unittest tests.test_api tests.test_logtail -v 2>&1 | tail -30` — Expected: FAIL.
- [ ] **Step 3: Implement** per Interfaces.
- [ ] **Step 4: Run full suite** — Expected: green, including all pre-existing `TestApi`/`TestCheckFix` cases (unmocked `pgrep -f nf_install` is rc 1 = not running).
- [ ] **Step 5: Commit** — `git commit -m "feat(api): install status, install launch/resume, reset request"` (add fixture).

---

### Task 9: API — install config endpoints

**Files:**
- Modify: `nfupgrader/api.py`
- Test: `tests/test_api.py`

**Interfaces:**
- Consumes: Tasks 2 and 3.
- Produces:
  - `GET /api/config/fields?mode=install|upgrade`: `mode` defaults to `host_mode(ctx)`; an invalid mode gives 400. Returns `{"fields": cfg.fields_for(mode), "mode": mode, "hostname": ctx.hostname}`, keeping today's INCLOGDIR default substitution.
  - `GET /api/install/hosts` returns `{"hosts": cnccnfg.read_etc_hosts(ctx.hosts_path or "/etc/hosts")}`.
  - `POST /api/config/preview` with `{values, mode}`:
    - `issues` is `cfg.validate(values, mode, ctx.hostname)`.
    - In install mode with BOXTYPE FEP/BEP, it also adds a WARNING on `MATEHOST` ("<name> is not in /etc/hosts") when the stripped mate isn't among the hosts names.
    - Adds `"cnc_cnfg"`: `cnccnfg.render_from_values(values, ctx.hostname)`, or None on `ValueError`/upgrade mode.
  - `POST /api/config/save` with `{values, mode}`: in install mode it writes the rendered cnc.cnfg to `os.path.join(ctx.inclogdir, "nfupgrader_cnc.cnfg")` and sets `values["CNCCNFG"]` to that path before `render_cfg`. ERROR issues give 400 as today. Audit `config_saved` gains `mode`.

- [ ] **Step 1: Write failing tests:**
  - `test_fields_install` — includes BOXTYPE, excludes UNLINKDIR, `hostname == "h1"`.
  - `test_hosts_endpoint` — reads a temp hosts file through `hosts_path`.
  - `test_preview_install_renders_cnc_and_warns_unknown_mate` — mate `zz` gives a WARNING and `cnc_cnfg` containing `"h1 1 zz 1 1 9"`.
  - `test_preview_install_invalid_has_no_cnc` — FEP with no mate gives `cnc_cnfg` None plus an ERROR.
  - `test_save_install_writes_cnc_cnfg` — the saved `.cfg` contains `CNCCNFG="<inclogdir>/nfupgrader_cnc.cnfg"`, and that file equals the preview text.
  - `test_save_upgrade_unchanged` — no mode means today's behaviour.
- [ ] **Step 2: Run** — Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run full suite** — Expected: green.
- [ ] **Step 5: Commit** — `git commit -m "feat(api): install config fields, /etc/hosts, cnc.cnfg preview and save"`.

---

### Task 10: httpd — wire install/resume/reset side effects

**Files:**
- Modify: `nfupgrader/httpd.py`
- Test: `tests/test_httpd.py`

**Interfaces:**
- Consumes: Task 5 `launch_install`, Task 8 response bodies.
- Produces:
  - `main()` sets `nf_install_path = os.environ.get("NFU_INSTALL_PATH", os.path.join(here, "..", "nf_install"))` and `hosts_path = "/etc/hosts"` on the Context.
  - `perform_install(ctx, action)` writes the effective config exactly as `perform_launch` does, then calls `launcher.launch_install(ctx.nf_install_path, ctx.effective_config, action, cwd=ctx.launch_cwd)`.
  - `launch_side_effect(ctx, mode, ip, perform=None, begin=None, action=None, perform_inst=None)`: for mode `install` it calls `perform_inst(ctx, action)` under `_FIX_LOCK` with **no snapshot**. A spawn error gives 500 and audits `launch_error`. Success sets `state["phase"]="install"` and `launched_at`, and returns 202 `{"accepted": True, "mode": "install", "action": action}`.
  - `safe_dispatch` reads `action` from the dispatcher's 202 JSON (not the request) for `/api/launch`. On a 202 from `/api/install/reset` it runs `reset_side_effect(ctx, ip, perform_inst=None)`, which does the same as the install path with action `reset`.

- [ ] **Step 1: Write failing tests:**
  - `test_install_launch_skips_snapshot` — with a runner present, `begin` is never called and `perform_inst` gets `"install"`.
  - `test_resume_action_passed_through` — dispatch stub returns 202 `{"action":"resume"}`, so `perform_inst` gets `"resume"`.
  - `test_reset_side_effect` — a reset 202 runs `perform_inst(ctx, "reset")`, and `state["phase"] == "install"`.
  - `test_install_spawn_failure_is_500`.
- [ ] **Step 2: Run** `python3 -m unittest tests.test_httpd -v` — Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run full suite** — Expected: green.
- [ ] **Step 5: Commit** — `git commit -m "feat(httpd): launch nf_install for install, resume and reset"`.

---

### Task 11: Web UI — Config tab install form

**Files:**
- Modify: `nfupgrader/web/index.html`, `nfupgrader/web/app.js`, `nfupgrader/web/style.css`
- Test: `tests/test_web_assets.py`

**Interfaces:**
- Consumes: `/api/install/status`, `/api/config/fields?mode=`, `/api/install/hosts`, `/api/config/preview`, `/api/config/save` (Tasks 8–9).
- Produces:
  - In `index.html`, inside the Config card after `#cfg-depot`'s row, a `<div id="install-cfg" style="display:none">` containing:
    - `select#cfg-boxtype` with options SINGLE "Singlebox", FEP, BEP
    - `#cfg-thishost`, read-only
    - `#cfg-mate-row` holding `input#cfg-mate list="cfg-hosts"`, `datalist#cfg-hosts`, `label#cfg-mate-label` and `span#cfg-mate-warn`
    - `#cfg-master-row` with radios `#cfg-master-me` and `#cfg-master-mate` (both `name="cfg-master"`), their labels showing the host names
    - `input#cfg-webgui` checkbox
    - a disabled `input#cfg-license` with the text "License file — coming later"
    - `pre#cfg-cnc-preview`

    The `h2` gets `id="cfg-title"`.
  - In `app.js`:
    - `var hostMode = null;`
    - `refreshInstall()` polls `/api/install/status` every 2 s and calls `applyHostMode(s)` when the mode changes.
    - `applyHostMode(s)` sets `hostMode`, sets `cfg-title` to "Install configuration" or "Upgrade configuration", toggles `#install-cfg`, and calls `buildForm()`.
    - `buildForm()` fetches `/api/config/fields?mode=` + hostMode and skips `hidden` fields as well as BOXTYPE/MATEHOST/MASTERHOST, which the install block renders.
    - `formValues()` adds BOXTYPE, MATEHOST (the trimmed mate), MASTERHOST (this host or the mate, from the radios) and INSTALLWEBGUI (YES/NO from the checkbox) in install mode.
    - Preview and save post `{values, mode: hostMode}`.
    - `previewInstall()` runs on every install-field `input`/`change` and fills `#cfg-cnc-preview` and `#cfg-mate-warn`.
    - Choosing SINGLE hides the mate and master rows.
    - The web GUI checkbox defaults to ticked for SINGLE/FEP and unticked for BEP until the operator touches it (`webguiTouched`).
    - The mate label reads "Mate (BEP) host" on a FEP and "Mate (FEP) host" on a BEP.
    - `loadHosts()` fills `#cfg-hosts` with `<option value="name">ip</option>`.

- [ ] **Step 1: Write failing tests** in `tests/test_web_assets.py`:
  - Add `"install-cfg", "cfg-boxtype", "cfg-mate", "cfg-hosts", "cfg-master-me", "cfg-master-mate", "cfg-webgui", "cfg-license", "cfg-cnc-preview", "cfg-title"` to `REQUIRED_IDS`.
  - `test_mate_is_type_or_pick` — `list="cfg-hosts"` in the html and `<datalist id="cfg-hosts">`.
  - `test_license_stub_disabled` — the `cfg-license` element has `disabled` and the text "coming later".
  - `test_install_form_wiring` — `"function applyHostMode("`, `"/api/install/status"`, `"/api/install/hosts"`, `"mode: hostMode"` and `"webguiTouched"` are all in the js, and `setInterval(refreshInstall` is in the js.
  - Keep `test_elements_used_by_app_js_come_before_the_script_tag` passing.
- [ ] **Step 2: Run** `python3 -m unittest tests.test_web_assets -v` — Expected: FAIL.
- [ ] **Step 3: Implement** (ES5 only).
- [ ] **Step 4: Run full suite** — Expected: green. Then a manual smoke run: `NFU_PORT=8099 ./run.sh` on a box without netFLEX (or with `NFU_CNC_CNFG` pointing nowhere and `INCLOGDIR` in a temp dir). Open the URL. The Config tab shows the install form; picking FEP, typing a mate and choosing the master updates the cnc.cnfg preview live.
- [ ] **Step 5: Commit** — `git commit -m "feat(ui): install configuration form"`.

---

### Task 12: Web UI — Install tab, overview empty state, failure panel, done banner; docs

**Files:**
- Modify: `nfupgrader/web/index.html`, `nfupgrader/web/app.js`, `nfupgrader/web/style.css`, `README.md`, `CLAUDE.md`
- Test: `tests/test_web_assets.py`

**Interfaces:**
- Consumes: `/api/install/status`, `/api/launch` (mode `install`, 202 `action`), `/api/install/reset`, `/api/checks?phase=install`.
- Produces:
  - **Overview:** `div#not-installed` at the top of the overview card. It is shown when `hostMode === "install"`, reads "netFLEX is not installed on this host. Set up the install on the Config tab." and links to the Config tab. The site table is hidden while it shows.
  - **Upgrade tab in install mode:**
    - The tab button `[data-tab="upgrade"]` gets `id="tab-btn-upgrade"` and its text becomes "Install".
    - `#mode` is rebuilt to a single `<option value="install">Install</option>` (restored to Stage/Upgrade when leaving install mode).
    - `#btn-launch` reads "Resume install" when `s.unfinished`, otherwise "Launch".
    - `launchConfirmText("install")` describes installing the chosen RPM on this host; for a resume it names the failed step `s.label`.
  - **Hard checks:** `renderChecks` shows no Override checkbox for results with `r.hard`. `updateLaunchState` keeps Launch disabled while any hard check fails.
  - **Failure panel `div#install-failed`:** shown when `s.unfinished && !s.running`. It contains:
    - the failed step label
    - `pre#install-fatal` with `s.fatal` lines (escaped)
    - `#install-trace`: "Send this file to support: " + `s.trace`
    - `button#btn-install-resume`, which runs the normal pre-flight → launch flow
    - `button#btn-install-reset`, which opens `showConfirm` whose body includes `<input id='reset-reason'>`, describes exactly what is removed (CORE RPM, the `/usr/cnc` and `/usr/cnc_stage` links, `s.info.LOADDIR`), and on OK posts `/api/install/reset` with the reason, rejecting an empty one client-side.
  - **Done banner `div#install-done`:** shown when this page has seen `hostMode === "install"` (`installSeen`) and the status now has `state === "isdone"`. It reads "Install complete" and keeps the install progress, post-flight and any WARN lines from the trace visible. `button#btn-install-continue` clears `installSeen` and calls `applyHostMode` with the upgrade mode. Until then `applyHostMode` does not switch away from install.
  - **Docs:** README gains an "Installing on a new host" section (flow, `nf_install` flags, state/info/trace files, reset safety). CLAUDE.md "Testing" gains `ksh test/nf_install_test.ksh` and notes that `nf_install` is new, not a fork.

- [ ] **Step 1: Write failing tests** in `tests/test_web_assets.py`:
  - Add to `REQUIRED_IDS`: `"not-installed", "install-failed", "install-fatal", "install-trace", "btn-install-resume", "btn-install-reset", "install-done", "btn-install-continue", "tab-btn-upgrade"`.
  - `test_hard_checks_not_overridable` — the `renderChecks` body references `r.hard`.
  - `test_reset_requires_reason_in_modal` — the js contains `id='reset-reason'` and `"/api/install/reset"`.
  - `test_done_banner_waits_for_continue` — the js contains `installSeen` and `"btn-install-continue"`.
  - `test_resume_label` — the js contains `"Resume install"`.
- [ ] **Step 2: Run** — Expected: FAIL.
- [ ] **Step 3: Implement** the UI and docs.
- [ ] **Step 4: Verify everything:**
  - `timeout 180 python3 -m unittest discover -s tests -p 'test_*.py' 2>&1 | tail -3` → OK, and the same with `LC_ALL=C`.
  - `ksh test/nf_log_test.ksh` and `ksh test/nf_install_test.ksh` → 0 failed.
  - `sh test/nf_fork_verify.sh` → `VERIFY OK`.
  - Manual: with the server against a sandbox (`INCLOGDIR` temp, a `.h1_INSTALL_STATE` of `ispostinstall` and a trace with a FATAL line), the failure panel, the Resume label and the reset modal render.
- [ ] **Step 5: Commit** — `git commit -m "feat(ui): install tab, failure resume/reset, done banner; docs"`.
- [ ] **Step 6: Stop and hand to Dan** for lab acceptance on holmvm32 or a fresh box: a fresh Singlebox install, a FEP/BEP pair config, a forced failure followed by Resume, and a Reset. Merge only on "commit and merge".
