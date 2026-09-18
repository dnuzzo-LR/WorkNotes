# nfupgrader Phase 4a — Local Host Information Panel Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans. Steps use checkbox (`- [ ]`) syntax.

**Goal:** An "About this host" panel showing the local box's read-only facts — product versions, installed/staged/saved loads, single/multibox/GR topology, app up/down, GR status, disk headroom, and (when the app is up) `incinfo`/`rdb`/`dbcheck`/multibox-link (`nestat -B`) detail — served at `/api/info` and rendered on the page.

**Architecture:** A new `localinfo.py` gathers facts entirely through the existing `Host.run` seam (so the same collectors serve peers over ssh in full Phase 4). Small pure parsers turn command output into structured data and are unit-tested with `FakeHost`; a `gather()` composes them. A session-gated `/api/info` endpoint returns the dict; a UI section renders it and refreshes on a slow interval. Everything is read-only — no writes, safe on a box that cannot be upgraded.

**Tech Stack:** Python 3.6.8, stdlib only, `unittest`. No f-strings; `subprocess` already wrapped by `Host`. Repo: `~/Git/nfupgrader`.

---

## Context

- Design: `~/WorkNotes/design/2026-09-15-web-upgrade-tool-design.md` — the "Information display" and "Per-host information displayed" sections, tier 1 (always true) and tier 2 (app up only), plus the multibox link panel (`nestat -B`).
- This is **Phase 4a**: the *local host* facts only. Read-only peer status over ssh (the full site view) is **Phase 4b**, deferred. The collectors are built on `Host.run` so 4b reuses them by swapping a local `Host` for an ssh one.
- Reuses `site.py` (cnc.cnfg parse) from Phase 2.

## Data sources (grounded in the design + the fork scripts)

| Fact | Source (run via `Host.run`) | Tier |
|---|---|---|
| CORE / PATCH / IPATCH version | `rpm -qi netFLEX-CORE` / `-PATCH` / `-IPATCH` | 1 |
| Installed / staged / saved load | `readlink /usr/cnc` / `/usr/cnc_stage` / `/usr/cnc_saved` | 1 |
| Topology (single/multi, role, mate, count) | `cnc.cnfg` via `site.py` | 1 |
| App up | `test -f /usr/cnc/.CNC_UP` | 1 |
| GR transfer / restore | `cat /usr/cnc/.GR_STATUS`; `test -f /usr/cnc/grestore.pid` | 1 |
| Disk headroom | `df -kP` | 1 |
| incinfo / rdb / dbcheck | `/usr/cnc/bin/incinfo`, `rdb version`, `rdb test`, `dbcheck -AV` | 2 |
| Multibox links | `/usr/cnc/mbin/nestat -B` | 2 |

Tier-2 collectors run only when `.CNC_UP` exists; otherwise they render "app down — unavailable". `incinfo`/`rdb`/`nestat -B`/`up` output is captured as raw text for display (light parsing only); `rpm`, `df`, and `dbcheck` error-count are parsed into structure.

---

## File Structure

| Path | Responsibility |
|---|---|
| `nfupgrader/localinfo.py` | Parsers (`parse_rpm_version`, `parse_df`, `parse_dbcheck_errors`) + `gather(host, hostname, cnc_cnfg_path, roots)` composing all facts. |
| `tests/test_localinfo.py` | Unit tests for parsers + `gather` with `FakeHost`. |
| `nfupgrader/api.py` | Add `GET /api/info` route (session-gated). |
| `tests/test_api.py` | Add a test for `/api/info`. |
| `nfupgrader/httpd.py` | Pass the info-gathering collaborators via `Context` (already has `host`, `hostname`; add `cnc_cnfg_path`). |
| `nfupgrader/web/index.html` | Add an "About this host" section. |
| `nfupgrader/web/app.js` | Fetch `/api/info`, render, refresh every 15s. |
| `nfupgrader/web/style.css` | Minor styles for the info grid. |

Run tests: `python3 -m unittest discover -s tests -p 'test_*.py'`

---

## Task 1: `localinfo.py` parsers (TDD)

**Files:**
- Create: `nfupgrader/localinfo.py`
- Create: `tests/test_localinfo.py`

- [ ] **Step 1: Write the failing parser tests**

`tests/test_localinfo.py`:
```python
import unittest
from nfupgrader import localinfo

RPM_OUT = """Name        : netFLEX-CORE
Version     : 5.4.0
Release     : 29
Architecture: x86_64
"""

DF_OUT = """Filesystem     1024-blocks     Used Available Capacity Mounted on
/dev/sda4         73400320 46000000  27400320      63% /usr4
/dev/sda2         11534336  9000000   2534336      79% /usr2
tmpfs              8000000    10000   7990000       1% /tmp
"""


class TestParsers(unittest.TestCase):
    def test_rpm_version_combines_version_release(self):
        self.assertEqual(localinfo.parse_rpm_version(RPM_OUT), "5.4.0-29")

    def test_rpm_version_missing_returns_none(self):
        self.assertIsNone(localinfo.parse_rpm_version("package not installed"))

    def test_df_parses_rows(self):
        rows = localinfo.parse_df(DF_OUT, keep=("/usr4", "/tmp"))
        mounts = {r["mount"]: r for r in rows}
        self.assertIn("/usr4", mounts)
        self.assertIn("/tmp", mounts)
        self.assertNotIn("/usr2", mounts)   # filtered out
        self.assertEqual(mounts["/usr4"]["use"], "63%")
        self.assertEqual(mounts["/usr4"]["avail_kb"], 27400320)

    def test_df_keep_none_returns_all(self):
        self.assertEqual(len(localinfo.parse_df(DF_OUT)), 3)

    def test_dbcheck_error_count(self):
        out = "checking...\nERROR: bad tie\nERROR: bad alarm\nok\n"
        self.assertEqual(localinfo.parse_dbcheck_errors(out), 2)

    def test_dbcheck_zero(self):
        self.assertEqual(localinfo.parse_dbcheck_errors("all clean\n"), 0)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run to verify failure**

Run: `python3 -m unittest tests.test_localinfo -v`
Expected: FAIL — module missing.

- [ ] **Step 3: Implement the parsers in `localinfo.py`**

```python
"""localinfo.py — gather this host's read-only facts through the Host.run seam.
Pure parsers turn command output into structure; gather() composes them. The
same collectors serve peers over ssh in Phase 4b."""
import re


def parse_rpm_version(out):
    """From `rpm -qi <pkg>` output return 'Version-Release', or None if absent."""
    ver = rel = None
    for line in out.splitlines():
        if line.startswith("Version"):
            ver = line.split(":", 1)[1].strip()
        elif line.startswith("Release"):
            rel = line.split(":", 1)[1].strip()
    if not ver:
        return None
    return "{}-{}".format(ver, rel) if rel else ver


def parse_df(out, keep=None):
    """Parse `df -kP` output into [{mount, use, avail_kb}]. If keep is given
    (an iterable of mount points), return only those."""
    rows = []
    lines = out.splitlines()
    for line in lines[1:]:               # skip header
        parts = line.split()
        if len(parts) < 6:
            continue
        mount = parts[5]
        if keep is not None and mount not in keep:
            continue
        try:
            avail_kb = int(parts[3])
        except ValueError:
            avail_kb = None
        rows.append({"mount": mount, "use": parts[4], "avail_kb": avail_kb})
    return rows


def parse_dbcheck_errors(out):
    """Count ERROR lines in dbcheck -AV output."""
    return sum(1 for ln in out.splitlines() if "ERROR" in ln.upper())
```

- [ ] **Step 4: Run to verify pass**

Run: `python3 -m unittest tests.test_localinfo -v`
Expected: PASS (6 tests).

- [ ] **Step 5: Commit**

```bash
git add nfupgrader/localinfo.py tests/test_localinfo.py
git commit -m "feat: localinfo.py parsers (rpm version, df, dbcheck errors)"
```

---

## Task 2: `localinfo.gather` (TDD)

**Files:**
- Modify: `nfupgrader/localinfo.py`
- Modify: `tests/test_localinfo.py`

- [ ] **Step 1: Add the gather test**

Append to `tests/test_localinfo.py`:
```python
import os
import tempfile
from nfupgrader.host import FakeHost, RunResult


def _cnfg(tmp):
    p = os.path.join(tmp, "cnc.cnfg")
    open(p, "w").write("fep1 1 fep2 2 1 0\nfep2 2 fep1 1 1 0\n"
                       "bep1 1 bep2 2 0 0\nbep2 2 bep1 1 0 0\n")
    return p


class TestGather(unittest.TestCase):
    def _host(self, app_up=True):
        r = {
            ("rpm", "-qi", "netFLEX-CORE"): RunResult(0, "Version     : 5.4.0\nRelease     : 29\n", ""),
            ("rpm", "-qi", "netFLEX-CORE-PATCH"): RunResult(1, "not installed", ""),
            ("rpm", "-qi", "netFLEX-CORE-IPATCH"): RunResult(1, "not installed", ""),
            ("readlink", "/usr/cnc"): RunResult(0, "/usr/inc/inc54.029\n", ""),
            ("readlink", "/usr/cnc_stage"): RunResult(0, "/usr/inc/inc54.129\n", ""),
            ("readlink", "/usr/cnc_saved"): RunResult(1, "", ""),
            ("test", "-f", "/usr/cnc/.CNC_UP"): RunResult(0 if app_up else 1, "", ""),
            ("cat", "/usr/cnc/.GR_STATUS"): RunResult(0, "COMPLETED\n", ""),
            ("test", "-f", "/usr/cnc/grestore.pid"): RunResult(1, "", ""),
            ("df", "-kP"): RunResult(0, "Filesystem 1024-blocks Used Available Capacity Mounted on\n"
                                        "/dev/sda4 73400320 46000000 27400320 63% /usr4\n", ""),
            ("/usr/cnc/bin/incinfo",): RunResult(0, "INC 5.4.0-29\n", ""),
            ("rdb", "version"): RunResult(0, "rdb 1.2.3\n", ""),
            ("dbcheck", "-AV"): RunResult(0, "clean\n", ""),
            ("/usr/cnc/mbin/nestat", "-B"): RunResult(0, "01 fep2  -> UP 7001 MSG WTR 5\n", ""),
        }
        return FakeHost(r)

    def test_gather_tier1_topology_and_versions(self):
        tmp = tempfile.mkdtemp()
        info = localinfo.gather(self._host(app_up=False), "fep1", _cnfg(tmp))
        self.assertEqual(info["topology"]["kind"], "multibox")
        self.assertEqual(info["topology"]["role"], "FEP")
        self.assertEqual(info["topology"]["mate"], "fep2")
        self.assertEqual(info["topology"]["host_count"], 4)
        self.assertEqual(info["versions"]["core"], "5.4.0-29")
        self.assertIsNone(info["versions"]["patch"])
        self.assertEqual(info["loads"]["installed"], "/usr/inc/inc54.029")
        self.assertIsNone(info["loads"]["saved"])
        self.assertFalse(info["app_up"])
        self.assertEqual(info["gr"]["transfer"], "COMPLETED")
        self.assertFalse(info["gr"]["restore"])
        self.assertTrue(any(d["mount"] == "/usr4" for d in info["disk"]))

    def test_gather_tier2_absent_when_app_down(self):
        tmp = tempfile.mkdtemp()
        info = localinfo.gather(self._host(app_up=False), "fep1", _cnfg(tmp))
        self.assertIsNone(info["app_detail"])
        self.assertIsNone(info["links"])

    def test_gather_tier2_present_when_app_up(self):
        tmp = tempfile.mkdtemp()
        info = localinfo.gather(self._host(app_up=True), "fep1", _cnfg(tmp))
        self.assertIsNotNone(info["app_detail"])
        self.assertIn("INC", info["app_detail"]["incinfo"])
        self.assertEqual(info["app_detail"]["dbcheck_errors"], 0)
        self.assertIn("fep2", info["links"])

    def test_gather_singlebox(self):
        tmp = tempfile.mkdtemp()
        p = os.path.join(tmp, "cnc.cnfg")
        open(p, "w").write("fep1 1 fep1 1 1 0\n")
        info = localinfo.gather(self._host(), "fep1", p)
        self.assertEqual(info["topology"]["kind"], "singlebox")
```

- [ ] **Step 2: Run to verify failure**

Run: `python3 -m unittest tests.test_localinfo -v`
Expected: FAIL — `gather` missing.

- [ ] **Step 3: Implement `gather` in `localinfo.py`**

Append:
```python
from nfupgrader import site

_DEFAULT_ROOTS = ["/usr/cnc", "/usr/cnc_stage", "/usr/cnc_saved"]
_DISK_KEEP = ("/usr4", "/usr2", "/tmp", "/var")


def _readlink(host, path):
    r = host.run(["readlink", path])
    return r.out.strip() if r.rc == 0 and r.out.strip() else None


def _rpm(host, pkg):
    return parse_rpm_version(host.run(["rpm", "-qi", pkg]).out)


def _test_f(host, path):
    return host.run(["test", "-f", path]).rc == 0


def gather(host, hostname, cnc_cnfg_path="/usr/cnc/features/cnc.cnfg"):
    """Collect this host's read-only facts. Tier-2 (app-dependent) fields are
    gathered only when /usr/cnc/.CNC_UP exists."""
    try:
        with open(cnc_cnfg_path) as fh:
            hosts = site.parse_cnfg(fh.read())
    except (IOError, OSError):
        hosts = []
    me = site.local_host(hosts, hostname)
    topology = {
        "kind": "multibox" if len(hosts) > 1 else "singlebox",
        "role": me.role if me else None,
        "mate": (me.mate if me and me.mate != me.name else None),
        "host_count": len(hosts),
    }

    app_up = _test_f(host, "/usr/cnc/.CNC_UP")

    gr_status = host.run(["cat", "/usr/cnc/.GR_STATUS"])
    gr = {
        "transfer": gr_status.out.strip() if gr_status.rc == 0 and gr_status.out.strip() else "none",
        "restore": _test_f(host, "/usr/cnc/grestore.pid"),
    }

    info = {
        "host": hostname,
        "topology": topology,
        "versions": {
            "core": _rpm(host, "netFLEX-CORE"),
            "patch": _rpm(host, "netFLEX-CORE-PATCH"),
            "ipatch": _rpm(host, "netFLEX-CORE-IPATCH"),
        },
        "loads": {
            "installed": _readlink(host, "/usr/cnc"),
            "staged": _readlink(host, "/usr/cnc_stage"),
            "saved": _readlink(host, "/usr/cnc_saved"),
        },
        "app_up": app_up,
        "gr": gr,
        "disk": parse_df(host.run(["df", "-kP"]).out, keep=_DISK_KEEP),
        "app_detail": None,
        "links": None,
    }

    if app_up:
        info["app_detail"] = {
            "incinfo": host.run(["/usr/cnc/bin/incinfo"]).out.strip(),
            "rdb_version": host.run(["rdb", "version"]).out.strip(),
            "dbcheck_errors": parse_dbcheck_errors(host.run(["dbcheck", "-AV"]).out),
        }
        info["links"] = host.run(["/usr/cnc/mbin/nestat", "-B"]).out.strip()

    return info
```

- [ ] **Step 4: Run to verify pass**

Run: `python3 -m unittest tests.test_localinfo -v`
Expected: PASS (all).

- [ ] **Step 5: Commit**

```bash
git add nfupgrader/localinfo.py tests/test_localinfo.py
git commit -m "feat: localinfo.gather composes host facts via Host.run"
```

---

## Task 3: `/api/info` endpoint (TDD)

**Files:**
- Modify: `nfupgrader/api.py`
- Modify: `tests/test_api.py`

- [ ] **Step 1: Add the Context field and a test**

In `tests/test_api.py`, extend `make_ctx` to pass `cnc_cnfg_path` and add:
```python
    def test_info_requires_session(self):
        status, _, _ = api.dispatch("GET", "/api/info", {}, b"", {}, "127.0.0.1", self.ctx)
        self.assertEqual(status, 401)

    def test_info_with_session_returns_facts(self):
        sid = self._session()
        status, _, body = api.dispatch(
            "GET", "/api/info", {}, b"", {"nfu_sid": sid}, "127.0.0.1", self.ctx)
        self.assertEqual(status, 200)
        data = json.loads(body.decode())
        self.assertIn("topology", data)
        self.assertIn("versions", data)
```
Update `make_ctx` so the FakeHost also answers the localinfo commands (reuse the response dict shape from `test_localinfo`'s `_host`), write a `cnc.cnfg` into `tmp`, and pass `cnc_cnfg_path=<that path>` to `api.Context`.

- [ ] **Step 2: Run to verify failure**

Run: `python3 -m unittest tests.test_api -v`
Expected: FAIL — `/api/info` returns 404, and/or Context has no `cnc_cnfg_path`.

- [ ] **Step 3: Add the field + route**

In `api.py`, add `"cnc_cnfg_path"` to the `Context` namedtuple field list, and extend the defaults tuple by one `None` (so it becomes `(None, None, None, None)`). Add the import `from nfupgrader import localinfo` and the route (after `/api/log`):
```python
    if path == "/api/info" and method == "GET":
        info = localinfo.gather(ctx.host, ctx.hostname, ctx.cnc_cnfg_path
                                or "/usr/cnc/features/cnc.cnfg")
        return _json(200, info)
```

- [ ] **Step 4: Run to verify pass**

Run: `python3 -m unittest tests.test_api -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add nfupgrader/api.py tests/test_api.py
git commit -m "feat: /api/info endpoint serving local host facts"
```

---

## Task 4: Wire `cnc_cnfg_path` through httpd

**Files:**
- Modify: `nfupgrader/httpd.py`

- [ ] **Step 1: Populate the new Context field in `main`**

Add near the other env reads:
```python
    cnc_cnfg_path = os.environ.get("NFU_CNC_CNFG", "/usr/cnc/features/cnc.cnfg")
```
and pass `cnc_cnfg_path=cnc_cnfg_path` to `api.Context(...)`.

- [ ] **Step 2: Import check**

Run: `python3 -c "import nfupgrader.httpd" && echo ok`
Expected: `ok`.

- [ ] **Step 3: Commit**

```bash
git add nfupgrader/httpd.py
git commit -m "feat: pass cnc.cnfg path into the service context"
```

---

## Task 5: "About this host" UI section

**Files:**
- Modify: `nfupgrader/web/index.html`
- Modify: `nfupgrader/web/app.js`
- Modify: `nfupgrader/web/style.css`

- [ ] **Step 1: Add the section to `index.html`** (after the progress section)

```html
  <section id="about">
    <h2>About this host</h2>
    <div id="about-grid" class="grid"></div>
    <h3>Multibox links</h3>
    <pre id="links">-</pre>
  </section>
```

- [ ] **Step 2: Render it in `app.js`** (add and call on a 15s interval)

```javascript
function row(label, value) {
  return "<div class='k'>" + label + "</div><div class='v'>" +
         (value === null || value === undefined || value === "" ? "-" : value) + "</div>";
}

function refreshInfo() {
  getJSON("/api/info", function (i) {
    var t = i.topology, v = i.versions, l = i.loads, g = i.gr;
    var html = "";
    html += row("Host", i.host);
    html += row("Topology", t.kind + (t.role ? " (" + t.role + ")" : "") +
                (t.host_count ? " — " + t.host_count + " hosts" : ""));
    html += row("Mate", t.mate);
    html += row("Application", i.app_up ? "UP" : "DOWN");
    html += row("CORE", v.core);
    html += row("PATCH", v.patch);
    html += row("IPATCH", v.ipatch);
    html += row("Installed load", l.installed);
    html += row("Staged load", l.staged);
    html += row("Saved load", l.saved);
    html += row("GR transfer", g.transfer);
    html += row("GR restore", g.restore ? "in progress" : "none");
    (i.disk || []).forEach(function (d) {
      html += row("Disk " + d.mount, d.use + " used");
    });
    if (i.app_detail) {
      html += row("incinfo", i.app_detail.incinfo);
      html += row("rdb", i.app_detail.rdb_version);
      html += row("dbcheck errors", i.app_detail.dbcheck_errors);
    }
    document.getElementById("about-grid").innerHTML = html;
    document.getElementById("links").textContent =
      i.links ? i.links : (i.app_up ? "(no links reported)" : "application down — unavailable");
  });
}
```
Add at the bottom, next to the other intervals:
```javascript
refreshInfo();
setInterval(refreshInfo, 15000);
```

- [ ] **Step 3: Style the grid in `style.css`**

```css
.grid { display: grid; grid-template-columns: 200px 1fr; gap: .25rem .75rem; }
.grid .k { color: #555; }
.grid .v { font-family: monospace; }
#links { background: #111; color: #ddd; padding: .5rem; overflow-x: auto; }
```

- [ ] **Step 4: Sanity check wiring**

Run: `grep -q "/api/info" nfupgrader/web/app.js && grep -q "about-grid" nfupgrader/web/index.html && echo WIRED`
Expected: `WIRED`.

- [ ] **Step 5: Commit**

```bash
git add nfupgrader/web/
git commit -m "feat: About this host UI panel (versions, topology, GR, disk, links)"
```

---

## Task 6: Full suite + smoke + manual check

- [ ] **Step 1: Full test suite**

Run: `python3 -m unittest discover -s tests -p 'test_*.py'`
Expected: all PASS.

- [ ] **Step 2: Smoke `/api/info` against fixtures**

Start the server with `INCLOGDIR`/`NFU_CNC_CNFG` pointing at fixture files (or the real box), redeem the token, then:
```bash
curl -s -b cookiejar "http://127.0.0.1:$PORT/api/info" | python3 -m json.tool | head -40
```
Expected: a JSON object with `topology`, `versions`, `loads`, `gr`, `disk`, and (if `.CNC_UP` exists) `app_detail` + `links`.

- [ ] **Step 3: Manual browser check on r9dev19**

Load the page; confirm the "About this host" panel shows this box's real versions/loads/topology/GR/disk, and the multibox link panel shows `nestat -B` output (or "application down — unavailable"). This is read-only and safe on a box that cannot be upgraded — it is the primary acceptance for this phase.

---

## Self-Review

**Spec coverage:** tier-1 facts (versions, loads, topology single/multi, app up, GR, disk) → `gather` Tasks 1–2; tier-2 (incinfo/rdb/dbcheck/nestat -B), app-gated → `gather` Task 2; endpoint + UI → Tasks 3–5. Multibox link panel (`nestat -B`) → gather `links` + UI `#links`. Read-only throughout (only `rpm`/`readlink`/`cat`/`test`/`df`/status commands; no writes).

**Placeholder scan:** every step has complete code. No TBD.

**Type/name consistency:** `gather(host, hostname, cnc_cnfg_path)` signature matches across localinfo, api route, and tests. `Context` gains exactly one field `cnc_cnfg_path` (defaults extended to 4 `None`s), populated in httpd. `parse_df` returns `{mount, use, avail_kb}` used consistently in tests and UI (`d.mount`, `d.use`). Endpoint path `/api/info` consistent in api, test, app.js.

**Deferred (not gaps):** read-only peer/site view over ssh = Phase 4b; `nestat -B` shown as raw text now (structured table can come later); GR "is this a GR site" is inferred from `.GR_STATUS` presence — refine if a firmer signal is needed.
