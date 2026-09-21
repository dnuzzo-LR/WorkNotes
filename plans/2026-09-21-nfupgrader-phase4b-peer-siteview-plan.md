# nfupgrader Phase 4b — Read-Only Peer Status / Site View Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Extend the dashboard from "this host" to the whole site: every host in `cnc.cnfg` shown in one table — the local host with full detail, and each peer with read-only tier-1 status gathered over ssh (or marked unreachable).

**Architecture:** Add an `SshHost` alongside `Host` behind the same `run(argv)` seam, so the Phase-4a collectors work unchanged against a peer by swapping the host object. `localinfo.gather` grows a `tier1_only` flag (peers are tier-1 only, per design decision 7). A new `siteview.gather_site` composes local-full + peer-tier1 rows, marking unreachable peers. A session-gated `/api/site` endpoint returns it; a UI table renders it. Read-only throughout — peers are never written to and never driven.

**Tech Stack:** Python 3.6.8, stdlib only (`shlex`, `subprocess` via the host seam), `unittest`. Repo: `~/Git/nfupgrader`.

---

## Context

- Design: `~/WorkNotes/design/2026-09-15-web-upgrade-tool-design.md` — "Per-host information displayed" (peers = tier-1 only; upgrade-progress + tier-2 local-only) and decision 7 (ssh reads peer status; nothing installed on peers).
- Phase 4a built `localinfo.gather(host, hostname, cnc_cnfg_path)` through `Host.run`, plus `site.parse_cnfg`/`local_host`. This phase reuses both unchanged except for the `tier1_only` flag.
- ssh assumption: the service runs as root on the local host and reaches peers by their `cnc.cnfg` name over key-based ssh (`BatchMode=yes`, no password prompt). Setting up that trust is an operator step (nf-install has `run_ssh_copy_id`); this phase just uses it.

---

## File Structure

| Path | Responsibility |
|---|---|
| `nfupgrader/host.py` | Add `build_ssh_argv()` (pure) + `SshHost` (wraps ssh, same `run` interface); refactor local exec into a shared `_exec`. |
| `nfupgrader/localinfo.py` | Add `tier1_only` param to `gather` (skip tier-2 for peers). |
| `nfupgrader/siteview.py` | `gather_site(hosts, local_name, cnc_cnfg_path, local_host, make_peer_host)` → local-full + peer-tier1 rows, unreachable marking. |
| `nfupgrader/api.py` | Add `GET /api/site`; add `peer_host_factory` Context field. |
| `nfupgrader/httpd.py` | Provide the real `SshHost` factory. |
| `nfupgrader/web/index.html` / `app.js` / `style.css` | "Site" table of all hosts. |
| `tests/test_host.py`, `tests/test_localinfo.py`, `tests/test_siteview.py`, `tests/test_api.py` | Tests. |

Run tests: `python3 -m unittest discover -s tests -p 'test_*.py'`

---

## Task 1: `SshHost` + `build_ssh_argv` (TDD)

**Files:**
- Modify: `nfupgrader/host.py`
- Modify: `tests/test_host.py`

- [ ] **Step 1: Add failing tests**

Append to `tests/test_host.py`:
```python
from nfupgrader.host import build_ssh_argv, SshHost


class TestSsh(unittest.TestCase):
    def test_build_ssh_argv_quotes_remote_command(self):
        argv = build_ssh_argv("bep1", ["rpm", "-qi", "netFLEX-CORE"])
        self.assertEqual(argv[0], "ssh")
        self.assertIn("BatchMode=yes", argv)
        self.assertIn("bep1", argv)
        # the remote command is a single trailing string, shell-quoted
        self.assertEqual(argv[-1], "rpm -qi netFLEX-CORE")

    def test_build_ssh_argv_quotes_spaces(self):
        argv = build_ssh_argv("bep1", ["sh", "-c", "echo a b"])
        self.assertEqual(argv[-1], "sh -c 'echo a b'")

    def test_sshhost_runs_through_local_exec(self):
        # SshHost.run builds an ssh argv and executes it locally; against a
        # loopback 'ssh' we cannot rely on the network, so just prove it
        # constructs and returns a RunResult (rc from the ssh attempt).
        r = SshHost("nonexistent.invalid").run(["true"])
        self.assertIsNotNone(r.rc)
```

- [ ] **Step 2: Run to verify failure**

Run: `python3 -m unittest tests.test_host -v`
Expected: FAIL — `build_ssh_argv`/`SshHost` missing.

- [ ] **Step 3: Refactor local exec + add ssh**

In `host.py`, extract the subprocess call into a module function and reuse it:
```python
import shlex


def _exec(argv, timeout=None):
    """Run argv locally, capture output, return a RunResult. rc 127 if the
    binary is missing, 124 on timeout."""
    try:
        p = subprocess.run(
            argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            universal_newlines=True, timeout=timeout,
        )
        return RunResult(p.returncode, p.stdout, p.stderr)
    except FileNotFoundError:
        return RunResult(127, "", "not found: {}".format(argv[0]))
    except subprocess.TimeoutExpired:
        return RunResult(124, "", "timeout: {}".format(" ".join(argv)))


def build_ssh_argv(name, argv, connect_timeout=5):
    """Build an ssh argv that runs `argv` on host `name` non-interactively.
    The remote command is a single shell-quoted string."""
    remote = " ".join(shlex.quote(a) for a in argv)
    return ["ssh", "-o", "BatchMode=yes",
            "-o", "ConnectTimeout={}".format(connect_timeout),
            name, remote]


class SshHost(object):
    """Runs commands on a peer over ssh, same interface as Host. Read-only use."""

    def __init__(self, name, connect_timeout=5):
        self.name = name
        self.local = False
        self.connect_timeout = connect_timeout

    def run(self, argv, timeout=None):
        return _exec(build_ssh_argv(self.name, argv, self.connect_timeout), timeout=timeout)
```
Change `Host.run` to `return _exec(argv, timeout=timeout)` (drop its duplicated try/except).

- [ ] **Step 4: Run to verify pass**

Run: `python3 -m unittest tests.test_host -v`
Expected: PASS (existing 4 + 3 new). The `nonexistent.invalid` ssh returns non-zero (255) — the test only asserts a RunResult comes back.

- [ ] **Step 5: Commit**

```bash
git add nfupgrader/host.py tests/test_host.py
git commit -m "feat: SshHost + build_ssh_argv behind the Host.run seam"
```

---

## Task 2: `tier1_only` on `localinfo.gather` (TDD)

**Files:**
- Modify: `nfupgrader/localinfo.py`
- Modify: `tests/test_localinfo.py`

- [ ] **Step 1: Add failing test**

Append to `TestGather` in `tests/test_localinfo.py`:
```python
    def test_gather_tier1_only_skips_tier2_even_when_app_up(self):
        tmp = tempfile.mkdtemp()
        info = localinfo.gather(self._host(app_up=True), "fep1", _cnfg(tmp), tier1_only=True)
        self.assertTrue(info["app_up"])          # tier-1 still reported
        self.assertIsNone(info["app_detail"])     # tier-2 skipped
        self.assertIsNone(info["links"])
```

- [ ] **Step 2: Run to verify failure**

Run: `python3 -m unittest tests.test_localinfo -v`
Expected: FAIL — `gather` has no `tier1_only`.

- [ ] **Step 3: Add the flag**

Change the signature and the tier-2 guard in `localinfo.py`:
```python
def gather(host, hostname, cnc_cnfg_path="/usr/cnc/features/cnc.cnfg", tier1_only=False):
```
and:
```python
    if app_up and not tier1_only:
        info["app_detail"] = {
            ...
        }
        info["links"] = host.run(["/usr/cnc/mbin/nestat", "-B"]).out.strip()
```

- [ ] **Step 4: Run to verify pass**

Run: `python3 -m unittest tests.test_localinfo -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add nfupgrader/localinfo.py tests/test_localinfo.py
git commit -m "feat: localinfo.gather tier1_only flag for peers"
```

---

## Task 3: `siteview.gather_site` (TDD)

**Files:**
- Create: `nfupgrader/siteview.py`
- Create: `tests/test_siteview.py`

- [ ] **Step 1: Write the failing test**

`tests/test_siteview.py`:
```python
import os
import tempfile
import unittest
from nfupgrader import siteview, site
from nfupgrader.host import FakeHost, RunResult


def _tier1_responses(app_up=False):
    return {
        ("rpm", "-qi", "netFLEX-CORE"): RunResult(0, "Version     : 5.4.0\nRelease     : 29\n", ""),
        ("rpm", "-qi", "netFLEX-CORE-PATCH"): RunResult(1, "", ""),
        ("rpm", "-qi", "netFLEX-CORE-IPATCH"): RunResult(1, "", ""),
        ("readlink", "/usr/cnc"): RunResult(0, "/usr/inc/inc54.029\n", ""),
        ("readlink", "/usr/cnc_stage"): RunResult(1, "", ""),
        ("readlink", "/usr/cnc_saved"): RunResult(1, "", ""),
        ("test", "-f", "/usr/cnc/.CNC_UP"): RunResult(0 if app_up else 1, "", ""),
        ("cat", "/usr/cnc/.GR_STATUS"): RunResult(1, "", ""),
        ("test", "-f", "/usr/cnc/grestore.pid"): RunResult(1, "", ""),
        ("df", "-kP"): RunResult(0, "H\n/d 1 1 1 5% /usr4\n", ""),
        ("true",): RunResult(0, "", ""),   # reachability probe
    }


class TestSiteView(unittest.TestCase):
    def _cnfg(self, tmp):
        p = os.path.join(tmp, "cnc.cnfg")
        open(p, "w").write("fep1 1 fep2 2 1 0\nfep2 2 fep1 1 1 0\n")
        return p

    def test_local_full_peer_tier1(self):
        tmp = tempfile.mkdtemp()
        cnfg = self._cnfg(tmp)
        hosts = site.parse_cnfg(open(cnfg).read())
        local = FakeHost(_tier1_responses(app_up=True))
        peers = {"fep2": FakeHost(_tier1_responses(app_up=True))}
        view = siteview.gather_site(hosts, "fep1", cnfg, local,
                                    lambda n: peers[n])
        rows = {r["name"]: r for r in view["hosts"]}
        self.assertTrue(rows["fep1"]["is_local"])
        self.assertTrue(rows["fep1"]["reachable"])
        self.assertIsNotNone(rows["fep1"]["info"]["app_detail"])   # local = full
        self.assertFalse(rows["fep2"]["is_local"])
        self.assertTrue(rows["fep2"]["reachable"])
        self.assertIsNone(rows["fep2"]["info"]["app_detail"])      # peer = tier1 only

    def test_unreachable_peer_marked(self):
        tmp = tempfile.mkdtemp()
        cnfg = self._cnfg(tmp)
        hosts = site.parse_cnfg(open(cnfg).read())
        local = FakeHost(_tier1_responses())
        # peer's reachability probe fails
        dead = FakeHost({("true",): RunResult(255, "", "unreachable")})
        view = siteview.gather_site(hosts, "fep1", cnfg, local, lambda n: dead)
        rows = {r["name"]: r for r in view["hosts"]}
        self.assertFalse(rows["fep2"]["reachable"])
        self.assertIsNone(rows["fep2"]["info"])
        self.assertEqual(view["local"], "fep1")


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run to verify failure**

Run: `python3 -m unittest tests.test_siteview -v`
Expected: FAIL — module missing.

- [ ] **Step 3: Implement `siteview.py`**

```python
"""siteview.py — compose a whole-site view: the local host in full detail and
each peer read-only (tier-1) over ssh, marking unreachable peers. Never writes
to or drives a peer."""
from nfupgrader import localinfo


def gather_site(hosts, local_name, cnc_cnfg_path, local_host, make_peer_host):
    """hosts: list[HostInfo]. local_host: a Host for this box. make_peer_host:
    callable(name) -> a Host (SshHost in production, a fake in tests)."""
    rows = []
    for h in hosts:
        if h.name == local_name:
            info = localinfo.gather(local_host, h.name, cnc_cnfg_path)
            rows.append({"name": h.name, "role": h.role, "is_local": True,
                         "reachable": True, "info": info})
            continue
        peer = make_peer_host(h.name)
        if peer.run(["true"]).rc != 0:
            rows.append({"name": h.name, "role": h.role, "is_local": False,
                         "reachable": False, "info": None})
        else:
            info = localinfo.gather(peer, h.name, cnc_cnfg_path, tier1_only=True)
            rows.append({"name": h.name, "role": h.role, "is_local": False,
                         "reachable": True, "info": info})
    return {"local": local_name, "hosts": rows}
```

- [ ] **Step 4: Run to verify pass**

Run: `python3 -m unittest tests.test_siteview -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add nfupgrader/siteview.py tests/test_siteview.py
git commit -m "feat: siteview.gather_site (local full + peers tier1 over ssh)"
```

---

## Task 4: `/api/site` endpoint (TDD)

**Files:**
- Modify: `nfupgrader/api.py`
- Modify: `tests/test_api.py`

- [ ] **Step 1: Add the Context field + a test**

In `api.py` add `"peer_host_factory"` to the `Context` field list and extend the defaults tuple by one `None` (→ five `None`s). Add `from nfupgrader import siteview, site` and the route (after `/api/info`):
```python
    if path == "/api/site" and method == "GET":
        hosts = site.read_site(ctx.cnc_cnfg_path or "/usr/cnc/features/cnc.cnfg")
        factory = ctx.peer_host_factory or (lambda name: __import__(
            "nfupgrader.host", fromlist=["SshHost"]).SshHost(name))
        view = siteview.gather_site(hosts, ctx.hostname,
                                    ctx.cnc_cnfg_path or "/usr/cnc/features/cnc.cnfg",
                                    ctx.host, factory)
        return _json(200, view)
```

In `tests/test_api.py`, add a multibox cnc.cnfg and a fake peer factory to a new test:
```python
    def test_site_requires_session(self):
        status, _, _ = api.dispatch("GET", "/api/site", {}, b"", {}, "127.0.0.1", self.ctx)
        self.assertEqual(status, 401)

    def test_site_with_session_lists_hosts(self):
        # rewrite cnc.cnfg for two hosts and give a fake peer factory
        open(self.ctx.cnc_cnfg_path, "w").write("h1 1 h2 2 1 0\nh2 2 h1 1 1 0\n")
        from nfupgrader.host import FakeHost, RunResult
        peer = FakeHost({("true",): RunResult(255, "", "")})   # h2 unreachable
        ctx = self.ctx._replace(peer_host_factory=lambda n: peer)
        sid = ctx.auth.redeem("TOK", "127.0.0.1")
        status, _, body = api.dispatch(
            "GET", "/api/site", {}, b"", {"nfu_sid": sid}, "127.0.0.1", ctx)
        self.assertEqual(status, 200)
        data = json.loads(body.decode())
        names = [h["name"] for h in data["hosts"]]
        self.assertEqual(sorted(names), ["h1", "h2"])
```
(Note: `self.ctx` is built fresh per test in `setUp`; `_replace` gives a copy with the fake factory. `make_ctx` already answers the local tier-1 commands.)

- [ ] **Step 2: Run to verify failure**

Run: `python3 -m unittest tests.test_api -v`
Expected: FAIL — `/api/site` 404 and/or Context has no `peer_host_factory`.

- [ ] **Step 3: Implement** (per Step 1 edits).

- [ ] **Step 4: Run to verify pass**

Run: `python3 -m unittest tests.test_api -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add nfupgrader/api.py tests/test_api.py
git commit -m "feat: /api/site endpoint (whole-site read-only view)"
```

---

## Task 5: httpd wiring — real SshHost factory

**Files:**
- Modify: `nfupgrader/httpd.py`

- [ ] **Step 1: Provide the factory in `main`**

Add `from nfupgrader.host import Host, SshHost` (extend the existing import) and set the Context field:
```python
        peer_host_factory=lambda name: SshHost(name),
```
(Add it to the `api.Context(...)` construction alongside `cnc_cnfg_path`.)

- [ ] **Step 2: Import check**

Run: `python3 -c "import nfupgrader.httpd" && echo ok`
Expected: `ok`.

- [ ] **Step 3: Commit**

```bash
git add nfupgrader/httpd.py
git commit -m "feat: wire SshHost peer factory into the service"
```

---

## Task 6: Site table UI

**Files:**
- Modify: `nfupgrader/web/index.html`, `nfupgrader/web/app.js`, `nfupgrader/web/style.css`

- [ ] **Step 1: Add the section to `index.html`** (before "About this host")

```html
  <section id="site">
    <h2>Site</h2>
    <table id="site-table">
      <thead><tr>
        <th>Host</th><th>Role</th><th>Reach</th><th>App</th>
        <th>CORE</th><th>Installed</th><th>Staged</th><th>GR</th>
      </tr></thead>
      <tbody></tbody>
    </table>
  </section>
```

- [ ] **Step 2: Render in `app.js`**

```javascript
function cell(v) { return "<td>" + (v === null || v === undefined || v === "" ? "-" : v) + "</td>"; }

function refreshSite() {
  getJSON("/api/site", function (s) {
    var tb = document.querySelector("#site-table tbody");
    tb.innerHTML = "";
    s.hosts.forEach(function (h) {
      var i = h.info || {};
      var v = i.versions || {}, l = i.loads || {}, g = i.gr || {};
      var tr = document.createElement("tr");
      if (h.is_local) tr.className = "local";
      var reach = h.reachable ? "ok" : "unreachable";
      var app = !h.reachable ? "?" : (i.app_up ? "UP" : "DOWN");
      tr.innerHTML =
        cell(h.name + (h.is_local ? " (this)" : "")) + cell(h.role) + cell(reach) +
        cell(app) + cell(v.core) + cell(l.installed) + cell(l.staged) +
        cell(g.transfer);
      tb.appendChild(tr);
    });
  });
}
```
Add near the other intervals:
```javascript
refreshSite();
setInterval(refreshSite, 15000);
```

- [ ] **Step 3: Style in `style.css`**

```css
#site-table { border-collapse: collapse; width: 100%; font-size: 13px; }
#site-table th, #site-table td { border: 1px solid #ddd; padding: .3rem .5rem; text-align: left; }
#site-table tr.local { background: #eef7f0; font-weight: bold; }
```

- [ ] **Step 4: Sanity check**

Run: `grep -q "/api/site" nfupgrader/web/app.js && grep -q "site-table" nfupgrader/web/index.html && echo WIRED`
Expected: `WIRED`.

- [ ] **Step 5: Commit**

```bash
git add nfupgrader/web/
git commit -m "feat: Site table UI (all hosts, reachability, tier-1 status)"
```

---

## Task 7: Full suite + smoke + manual

- [ ] **Step 1: Full suite**

Run: `python3 -m unittest discover -s tests -p 'test_*.py'`
Expected: all PASS.

- [ ] **Step 2: Smoke `/api/site`**

On a box, point `NFU_CNC_CNFG` at a multi-host `cnc.cnfg` (fixture is fine) and curl `/api/site` with the session cookie. Expected: one row per host; the local row has full `info`; peers show `reachable` true/false and tier-1 `info` (no `app_detail`). Unreachable peers show `reachable:false, info:null`.

- [ ] **Step 3: Manual — real peers (if a multibox lab is available)**

On a real multibox host with ssh keys to its peers, load the page; confirm the Site table lists every host, the local row is highlighted with full detail, and peers show tier-1 status. A peer with no ssh trust shows `unreachable` (expected). Read-only — safe.

---

## Self-Review

**Spec coverage:** peers over ssh, read-only, tier-1 only → `SshHost` (T1) + `tier1_only` (T2) + `gather_site` (T3). Whole-site table → `/api/site` (T4) + UI (T6). Unreachable → `gather_site` marks `reachable:false`, UI shows it. Local stays full detail (tier-1+2) → `gather_site` local branch.

**Placeholder scan:** complete code each step; no TBD.

**Type/name consistency:** `build_ssh_argv(name, argv)` and `SshHost(name).run(argv)` consistent across host tests and siteview. `gather(..., tier1_only=False)` matches localinfo, siteview, and its test. `gather_site(hosts, local_name, cnc_cnfg_path, local_host, make_peer_host)` signature matches the api call site and tests. `Context` gains exactly `peer_host_factory` (defaults extended to five `None`s), set in httpd. Row shape `{name, role, is_local, reachable, info}` consistent between siteview, api test, and app.js.

**Deferred (not gaps):** peers gathered sequentially (a handful of hosts; ssh `ConnectTimeout=5` bounds the unreachable cost) — threading can come later if a large site is slow. ssh user/key setup is an operator step (nf-install provides `run_ssh_copy_id`), out of scope here. Structured `nestat -B` table still deferred (local panel shows raw text from Phase 4a).
