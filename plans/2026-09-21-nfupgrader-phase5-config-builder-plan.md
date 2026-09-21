# nfupgrader Phase 5 — Config Builder Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Let the customer build an upgrade `.cfg` from a validated form (with a depot picker that lists RPMs on disk and derives generic/load) **or** import an existing `.cfg`, review the final values on one screen, and save it as the active config the Launch flow uses — so they never hand-edit a file or set `NFU_USER_CONFIG` by hand. `PROMPTFORVALIDATE=NO` is always forced; the customer's own file is never edited.

**Architecture:** A `cfg.py` module owns the field metadata, parse/render/validate, and generic/load derivation — all pure and unit-tested. Depot discovery runs through `Host.run`. New `/api/config/*` endpoints expose fields, depots, a validate+preview, import, and save. Save writes the config and records it in a small mutable service `state` dict (held on the Context) that the launch flow reads, so a config chosen in the UI drives the next launch. A config-builder UI section composes it.

**Tech Stack:** Python 3.6.8, stdlib only (`re`, `json`), `unittest`. Repo: `~/Git/nfupgrader`.

---

## Context

- Design: `~/WorkNotes/design/2026-09-15-web-upgrade-tool-design.md` — "Config builder": build **and** import, both ending at a review screen; `PROMPTFORVALIDATE=NO` always forced; on import show exactly what is overridden; field list enumerated there.
- Phase 2 already has the launch flow with `launcher.write_effective_config` (sources a config + forces `PROMPTFORVALIDATE=NO`) and reads `ctx.user_config` / `NFU_USER_CONFIG`. Phase 5 lets the UI set the active config at runtime via a mutable `state` dict, falling back to the env var.
- Fields (from the fork scripts): `INCLOGDIR`, `NEW_GENERIC`, `NEW_LOAD`, `DEPOTFILE`, `PATCHDEPOTFILE`, `EXTDEPOTFILE`, `ATTEXTPATCHDEPOTFILE`, `INSTALLWEBGUI`, `RESTARTINC`, `INIT_CARD_DB`, `UNLINKDIR`, `STAGENICEVAL`, `UPGRADENICEVAL`, `LINUXPACKAGES`, `REMOVEINCLOGS`, `CLEANUPPROCDIR`, `MAX_ELAPSED_GR_WAIT_TIME`, `CUSTOMER_UPGRADE`, `PROMPTFORVALIDATE` (fixed `NO`).

---

## File Structure

| Path | Responsibility |
|---|---|
| `nfupgrader/cfg.py` | `FIELDS` metadata; `parse_cfg`, `render_cfg`, `validate`, `derive_generic_load`, `list_depots`. |
| `nfupgrader/api.py` | `/api/config/fields`, `/api/config/depots`, `/api/config/preview`, `/api/config/import`, `/api/config/save`; `state` Context field; launch resolves active config from `state`. |
| `nfupgrader/httpd.py` | Create the shared `state` dict; `NFU_DEPOT_DIRS` env; pass both into the Context. |
| `nfupgrader/web/index.html` / `app.js` / `style.css` | Config-builder section: form, depot picker, import, review/preview, "Use this config". |
| `tests/test_cfg.py`, `tests/test_api.py` | Tests. |

Run tests: `python3 -m unittest discover -s tests -p 'test_*.py'`

---

## Task 1: `cfg.py` — fields, parse, render, derive (TDD)

**Files:**
- Create: `nfupgrader/cfg.py`
- Create: `tests/test_cfg.py`

- [ ] **Step 1: Write the failing tests**

`tests/test_cfg.py`:
```python
import unittest
from nfupgrader import cfg


class TestParseRender(unittest.TestCase):
    def test_parse_basic_assignments(self):
        text = '# comment\nINCLOGDIR="/usr/cnc/logs"\nNEW_GENERIC=5.5.0\n\nNEW_LOAD="03"\n'
        d = cfg.parse_cfg(text)
        self.assertEqual(d["INCLOGDIR"], "/usr/cnc/logs")
        self.assertEqual(d["NEW_GENERIC"], "5.5.0")
        self.assertEqual(d["NEW_LOAD"], "03")

    def test_parse_ignores_non_assignment_lines(self):
        d = cfg.parse_cfg("echo hi\nFOO=bar\n. /some/file\n")
        self.assertEqual(d, {"FOO": "bar"})

    def test_render_quotes_and_forces_promptforvalidate(self):
        text = cfg.render_cfg({"INCLOGDIR": "/usr/cnc/logs", "NEW_GENERIC": "5.5.0"})
        self.assertIn('INCLOGDIR="/usr/cnc/logs"', text)
        self.assertIn('PROMPTFORVALIDATE=NO', text)

    def test_render_skips_empty_values(self):
        text = cfg.render_cfg({"INCLOGDIR": "/x", "PATCHDEPOTFILE": ""})
        self.assertNotIn("PATCHDEPOTFILE", text)

    def test_derive_generic_load(self):
        self.assertEqual(cfg.derive_generic_load("5.5.0.03"), ("5.5.0", "03"))
        self.assertEqual(cfg.derive_generic_load("5.5.0"), ("5.5.0", ""))

    def test_fields_include_required_and_fixed(self):
        by = {f["name"]: f for f in cfg.FIELDS}
        self.assertTrue(by["NEW_GENERIC"]["required"])
        self.assertTrue(by["DEPOTFILE"]["required"])
        self.assertTrue(by["PROMPTFORVALIDATE"].get("fixed"))


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run to verify failure**

Run: `python3 -m unittest tests.test_cfg -v`
Expected: FAIL — module missing.

- [ ] **Step 3: Implement `cfg.py` (parse/render/derive/FIELDS)**

```python
"""cfg.py — upgrade .cfg field metadata, parse/render/validate, and helpers for
the config builder. render_cfg always forces PROMPTFORVALIDATE=NO; the customer's
own file is never edited (import parses a copy of the values)."""
import re

FIELDS = [
    {"name": "INCLOGDIR", "required": True, "kind": "path", "default": "/usr/cnc/logs",
     "help": "Log directory"},
    {"name": "NEW_GENERIC", "required": True, "kind": "string", "help": "Target generic, e.g. 5.5.0"},
    {"name": "NEW_LOAD", "required": True, "kind": "string", "help": "Target load, e.g. 03"},
    {"name": "DEPOTFILE", "required": True, "kind": "path", "help": "CORE RPM path"},
    {"name": "PATCHDEPOTFILE", "required": False, "kind": "path"},
    {"name": "EXTDEPOTFILE", "required": False, "kind": "path"},
    {"name": "ATTEXTPATCHDEPOTFILE", "required": False, "kind": "path"},
    {"name": "INSTALLWEBGUI", "required": False, "kind": "enum", "values": ["YES", "NO"], "default": "YES"},
    {"name": "RESTARTINC", "required": False, "kind": "enum", "values": ["YES", "NO"], "default": "YES"},
    {"name": "INIT_CARD_DB", "required": False, "kind": "enum", "values": ["YES", "NO"], "default": "YES"},
    {"name": "UNLINKDIR", "required": False, "kind": "path"},
    {"name": "STAGENICEVAL", "required": False, "kind": "int", "default": "39"},
    {"name": "UPGRADENICEVAL", "required": False, "kind": "int", "default": "-39"},
    {"name": "LINUXPACKAGES", "required": False, "kind": "enum", "values": ["YES", "NO"], "default": "YES"},
    {"name": "REMOVEINCLOGS", "required": False, "kind": "enum", "values": ["YES", "NO"]},
    {"name": "CLEANUPPROCDIR", "required": False, "kind": "enum", "values": ["YES", "NO"]},
    {"name": "MAX_ELAPSED_GR_WAIT_TIME", "required": False, "kind": "int", "default": "14400"},
    {"name": "CUSTOMER_UPGRADE", "required": False, "kind": "enum", "values": ["YES", "NO"], "default": "YES"},
    {"name": "PROMPTFORVALIDATE", "required": False, "kind": "enum", "values": ["NO"],
     "default": "NO", "fixed": True},
]

_FIELD_NAMES = [f["name"] for f in FIELDS]
_ASSIGN = re.compile(r'^\s*([A-Z_][A-Z0-9_]*)=(.*)$')


def parse_cfg(text):
    """Parse shell KEY=VALUE assignments into a dict; ignore comments, blank and
    non-assignment lines; strip surrounding single/double quotes."""
    out = {}
    for line in text.splitlines():
        if line.lstrip().startswith("#"):
            continue
        m = _ASSIGN.match(line)
        if not m:
            continue
        val = m.group(2).strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in ("'", '"'):
            val = val[1:-1]
        out[m.group(1)] = val
    return out


def render_cfg(values):
    """Render a .cfg from a values dict. Emits KEY="value" for each non-empty
    field in canonical order and always forces PROMPTFORVALIDATE=NO."""
    lines = ["# generated by nfupgrader"]
    for name in _FIELD_NAMES:
        if name == "PROMPTFORVALIDATE":
            continue
        v = values.get(name, "")
        if v is None or str(v) == "":
            continue
        lines.append('{}="{}"'.format(name, v))
    lines.append("PROMPTFORVALIDATE=NO")
    return "\n".join(lines) + "\n"


def derive_generic_load(version):
    """Split a netFLEX version like '5.5.0.03' into (generic, load) = ('5.5.0','03').
    A version with no dot returns (version, '')."""
    if "." in version:
        head, tail = version.rsplit(".", 1)
        return head, tail
    return version, ""
```

- [ ] **Step 4: Run to verify pass**

Run: `python3 -m unittest tests.test_cfg -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add nfupgrader/cfg.py tests/test_cfg.py
git commit -m "feat: cfg.py fields, parse/render, generic-load derivation"
```

---

## Task 2: `cfg.validate` (TDD)

**Files:**
- Modify: `nfupgrader/cfg.py`
- Modify: `tests/test_cfg.py`

- [ ] **Step 1: Add failing tests**

Append to `tests/test_cfg.py`:
```python
class TestValidate(unittest.TestCase):
    def test_missing_required_is_error(self):
        issues = cfg.validate({"NEW_GENERIC": "5.5.0"})  # missing INCLOGDIR, NEW_LOAD, DEPOTFILE
        fields = {i["field"]: i for i in issues if i["level"] == "ERROR"}
        self.assertIn("INCLOGDIR", fields)
        self.assertIn("NEW_LOAD", fields)
        self.assertIn("DEPOTFILE", fields)

    def test_enum_out_of_range_is_error(self):
        issues = cfg.validate(_ok({"INSTALLWEBGUI": "MAYBE"}))
        self.assertTrue(any(i["field"] == "INSTALLWEBGUI" and i["level"] == "ERROR" for i in issues))

    def test_int_non_numeric_is_error(self):
        issues = cfg.validate(_ok({"STAGENICEVAL": "high"}))
        self.assertTrue(any(i["field"] == "STAGENICEVAL" and i["level"] == "ERROR" for i in issues))

    def test_negative_int_ok(self):
        issues = cfg.validate(_ok({"UPGRADENICEVAL": "-39"}))
        self.assertFalse(any(i["field"] == "UPGRADENICEVAL" for i in issues))

    def test_clean_config_no_errors(self):
        self.assertEqual([i for i in cfg.validate(_ok({})) if i["level"] == "ERROR"], [])


def _ok(extra):
    base = {"INCLOGDIR": "/usr/cnc/logs", "NEW_GENERIC": "5.5.0",
            "NEW_LOAD": "03", "DEPOTFILE": "/depot/netFLEX-CORE-5.5.0.03-1.x86_64.rpm"}
    base.update(extra)
    return base
```

- [ ] **Step 2: Run to verify failure**

Run: `python3 -m unittest tests.test_cfg -v`
Expected: FAIL — `validate` missing.

- [ ] **Step 3: Implement `validate`**

Append to `cfg.py`:
```python
_BY_NAME = {f["name"]: f for f in FIELDS}


def validate(values):
    """Structural validation of a values dict. Returns a list of
    {field, level, message}. level is ERROR or WARNING. Does not touch the
    filesystem (path existence is checked separately, with a Host)."""
    issues = []
    for f in FIELDS:
        name = f["name"]
        v = values.get(name, "")
        v = "" if v is None else str(v).strip()
        if f.get("required") and not v:
            issues.append({"field": name, "level": "ERROR",
                           "message": "{} is required".format(name)})
            continue
        if not v:
            continue
        if f["kind"] == "enum" and v not in f["values"]:
            issues.append({"field": name, "level": "ERROR",
                           "message": "{} must be one of {}".format(name, "/".join(f["values"]))})
        elif f["kind"] == "int":
            try:
                int(v)
            except ValueError:
                issues.append({"field": name, "level": "ERROR",
                               "message": "{} must be an integer".format(name)})
    return issues
```

- [ ] **Step 4: Run to verify pass**

Run: `python3 -m unittest tests.test_cfg -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add nfupgrader/cfg.py tests/test_cfg.py
git commit -m "feat: cfg.validate structural field validation"
```

---

## Task 3: `cfg.list_depots` (TDD)

**Files:**
- Modify: `nfupgrader/cfg.py`
- Modify: `tests/test_cfg.py`

- [ ] **Step 1: Add failing test**

Append to `tests/test_cfg.py`:
```python
from nfupgrader.host import FakeHost, RunResult


class TestDepots(unittest.TestCase):
    def test_list_depots_parses_names(self):
        out = ("/depot/netFLEX-CORE-5.5.0.03-1.rhel8.x86_64.rpm\n"
               "/depot/netFLEX-CORE-5.5.0.04-1.rhel8.x86_64.rpm\n")
        h = FakeHost({("find", "/depot", "-maxdepth", "1", "-name", "netFLEX-CORE*.rpm"):
                      RunResult(0, out, "")})
        depots = cfg.list_depots(h, ["/depot"])
        self.assertEqual(len(depots), 2)
        self.assertEqual(depots[0]["generic"], "5.5.0")
        self.assertEqual(depots[0]["load"], "03")
        self.assertEqual(depots[0]["path"], "/depot/netFLEX-CORE-5.5.0.03-1.rhel8.x86_64.rpm")

    def test_list_depots_empty_dir(self):
        h = FakeHost({("find", "/none", "-maxdepth", "1", "-name", "netFLEX-CORE*.rpm"):
                      RunResult(1, "", "")})
        self.assertEqual(cfg.list_depots(h, ["/none"]), [])
```

- [ ] **Step 2: Run to verify failure**

Run: `python3 -m unittest tests.test_cfg -v`
Expected: FAIL.

- [ ] **Step 3: Implement `list_depots`**

Append to `cfg.py`:
```python
_DEPOT_RE = re.compile(r"netFLEX-CORE-([0-9][0-9.]*)-")


def list_depots(host, dirs):
    """Scan dirs for netFLEX-CORE*.rpm via Host.run(find). Return
    [{path, name, generic, load}] with generic/load derived from the version."""
    out = []
    for d in dirs:
        r = host.run(["find", d, "-maxdepth", "1", "-name", "netFLEX-CORE*.rpm"])
        if r.rc != 0:
            continue
        for path in r.out.split():
            name = path.rsplit("/", 1)[-1]
            m = _DEPOT_RE.search(name)
            generic, load = derive_generic_load(m.group(1)) if m else ("", "")
            out.append({"path": path, "name": name, "generic": generic, "load": load})
    return out
```

- [ ] **Step 4: Run to verify pass**

Run: `python3 -m unittest tests.test_cfg -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add nfupgrader/cfg.py tests/test_cfg.py
git commit -m "feat: cfg.list_depots discovery via Host.run(find)"
```

---

## Task 4: config API endpoints — fields, depots, preview, import (TDD)

**Files:**
- Modify: `nfupgrader/api.py`
- Modify: `tests/test_api.py`

- [ ] **Step 1: Add tests**

Extend `make_ctx` to answer the depot `find` (for one dir, e.g. `/depot`) and set `depot_dirs=["/depot"]` and `state={}` on the Context. Add:
```python
    def test_config_fields(self):
        sid = self._session()
        status, _, body = api.dispatch("GET", "/api/config/fields", {}, b"",
                                       {"nfu_sid": sid}, "127.0.0.1", self.ctx)
        self.assertEqual(status, 200)
        self.assertTrue(any(f["name"] == "NEW_GENERIC" for f in json.loads(body.decode())["fields"]))

    def test_config_depots(self):
        sid = self._session()
        status, _, body = api.dispatch("GET", "/api/config/depots", {}, b"",
                                       {"nfu_sid": sid}, "127.0.0.1", self.ctx)
        self.assertEqual(status, 200)
        self.assertIn("depots", json.loads(body.decode()))

    def test_config_preview_reports_issues_and_rendered(self):
        sid = self._session()
        payload = json.dumps({"values": {"NEW_GENERIC": "5.5.0"}}).encode()  # missing required
        status, _, body = api.dispatch("POST", "/api/config/preview", {}, payload,
                                       {"nfu_sid": sid}, "127.0.0.1", self.ctx)
        self.assertEqual(status, 200)
        data = json.loads(body.decode())
        self.assertTrue(any(i["level"] == "ERROR" for i in data["issues"]))
        self.assertIn("PROMPTFORVALIDATE=NO", data["rendered"])

    def test_config_import_shows_values_and_override(self):
        sid = self._session()
        import os as _os
        p = _os.path.join(self.tmp, "user.cfg")
        open(p, "w").write('INCLOGDIR="/usr/cnc/logs"\nPROMPTFORVALIDATE=YES\n')
        status, _, body = api.dispatch("GET", "/api/config/import", {"path": [p]}, b"",
                                       {"nfu_sid": sid}, "127.0.0.1", self.ctx)
        self.assertEqual(status, 200)
        data = json.loads(body.decode())
        self.assertEqual(data["values"]["INCLOGDIR"], "/usr/cnc/logs")
        self.assertTrue(data["forces_promptforvalidate"])   # original had YES; we force NO
```

- [ ] **Step 2: Run to verify failure**

Run: `python3 -m unittest tests.test_api -v`
Expected: FAIL — Context has no `depot_dirs`/`state`; endpoints 404.

- [ ] **Step 3: Implement**

In `api.py`: add `"state"` and `"depot_dirs"` to the `Context` field list; extend the defaults tuple by two (so all trailing optionals default). Add `from nfupgrader import cfg`. Add routes (after `/api/checks`):
```python
    if path == "/api/config/fields" and method == "GET":
        return _json(200, {"fields": cfg.FIELDS})

    if path == "/api/config/depots" and method == "GET":
        depots = cfg.list_depots(ctx.host, ctx.depot_dirs or ["/opt/apt/upgrade", "."])
        return _json(200, {"depots": depots})

    if path == "/api/config/preview" and method == "POST":
        try:
            values = json.loads(body.decode("utf-8") or "{}").get("values", {})
        except ValueError:
            return _json(400, {"error": "bad json"})
        return _json(200, {"issues": cfg.validate(values), "rendered": cfg.render_cfg(values)})

    if path == "/api/config/import" and method == "GET":
        p = (query.get("path") or [""])[0]
        try:
            with open(p) as fh:
                text = fh.read()
        except (IOError, OSError):
            return _json(404, {"error": "cannot read {}".format(p)})
        values = cfg.parse_cfg(text)
        forces = values.get("PROMPTFORVALIDATE", "NO") != "NO"
        return _json(200, {"values": values, "forces_promptforvalidate": forces,
                           "issues": cfg.validate(values)})
```

- [ ] **Step 4: Run to verify pass**

Run: `python3 -m unittest tests.test_api -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add nfupgrader/api.py tests/test_api.py
git commit -m "feat: /api/config fields, depots, preview, import"
```

---

## Task 5: save endpoint + launch uses the active config (TDD)

**Files:**
- Modify: `nfupgrader/api.py`
- Modify: `nfupgrader/httpd.py`
- Modify: `tests/test_api.py`

- [ ] **Step 1: Add tests**

```python
    def test_config_save_sets_active_and_writes_file(self):
        sid = self._session()
        payload = json.dumps({"values": {
            "INCLOGDIR": "/usr/cnc/logs", "NEW_GENERIC": "5.5.0", "NEW_LOAD": "03",
            "DEPOTFILE": "/depot/netFLEX-CORE-5.5.0.03-1.x86_64.rpm"}}).encode()
        status, _, body = api.dispatch("POST", "/api/config/save", {}, payload,
                                       {"nfu_sid": sid}, "127.0.0.1", self.ctx)
        self.assertEqual(status, 200)
        saved = json.loads(body.decode())["saved"]
        self.assertIn("PROMPTFORVALIDATE=NO", open(saved).read())
        self.assertEqual(self.ctx.state["user_config"], saved)

    def test_config_save_rejects_invalid(self):
        sid = self._session()
        payload = json.dumps({"values": {"NEW_GENERIC": "5.5.0"}}).encode()  # missing required
        status, _, body = api.dispatch("POST", "/api/config/save", {}, payload,
                                       {"nfu_sid": sid}, "127.0.0.1", self.ctx)
        self.assertEqual(status, 400)
        self.assertTrue(len(json.loads(body.decode())["issues"]) >= 1)
```

- [ ] **Step 2: Run to verify failure**

Run: `python3 -m unittest tests.test_api -v`
Expected: FAIL — no `/api/config/save`.

- [ ] **Step 3: Implement the save route** (after import) in `api.py`:

```python
    if path == "/api/config/save" and method == "POST":
        try:
            values = json.loads(body.decode("utf-8") or "{}").get("values", {})
        except ValueError:
            return _json(400, {"error": "bad json"})
        issues = cfg.validate(values)
        if any(i["level"] == "ERROR" for i in issues):
            return _json(400, {"error": "config invalid", "issues": issues})
        saved = os.path.join(ctx.inclogdir, "nfupgrader_user.cfg")
        with open(saved, "w") as fh:
            fh.write(cfg.render_cfg(values))
        if ctx.state is not None:
            ctx.state["user_config"] = saved
        ctx.audit.record("config_saved", path=saved, client=client_ip)
        return _json(200, {"saved": saved})
```

- [ ] **Step 4: Launch resolves the active config**

In `api.py`, add a helper and use it wherever the launch path needs the config. Since the actual spawn is in `httpd`, expose the resolved config for httpd by having the `/api/launch` 202 response include it, OR resolve in httpd from `ctx.state`. Do the latter — in `httpd.py`, change the launch side effect to:
```python
                resolved = (ctx.state or {}).get("user_config") or ctx.user_config
                if resolved:
                    launcher.write_effective_config(resolved, ctx.effective_config)
                launcher.launch(ctx.nf_upgrade_path, ctx.effective_config, mode,
                                cwd=ctx.launch_cwd)
```
(Replacing the previous `if ctx.user_config:` block.)

- [ ] **Step 5: Wire `state` + `depot_dirs` in `httpd.main`**

Add:
```python
    depot_dirs = os.environ.get("NFU_DEPOT_DIRS", "/opt/apt/upgrade").split(":")
    state = {}
```
and pass `state=state, depot_dirs=depot_dirs` in the `api.Context(...)` construction. (Extend the `api.Context` field list + defaults accordingly — see Step 3 of Task 4.)

- [ ] **Step 6: Run to verify pass + import check**

Run: `python3 -m unittest tests.test_api -v && python3 -c "import nfupgrader.httpd" && echo ok`
Expected: PASS then `ok`.

- [ ] **Step 7: Commit**

```bash
git add nfupgrader/api.py nfupgrader/httpd.py tests/test_api.py
git commit -m "feat: /api/config/save + launch uses UI-chosen active config"
```

---

## Task 6: Config builder UI

**Files:**
- Modify: `nfupgrader/web/index.html`, `app.js`, `style.css`

- [ ] **Step 1: Add the section to `index.html`** (before the Launch/controls section)

```html
  <section id="config">
    <h2>Upgrade configuration</h2>
    <div>
      <button id="cfg-load-depots">List depots</button>
      <select id="cfg-depot"><option value="">— choose a CORE RPM —</option></select>
    </div>
    <form id="cfg-form"></form>
    <div>
      <button type="button" id="cfg-preview">Preview</button>
      <button type="button" id="cfg-save">Use this config</button>
      <span id="cfg-msg"></span>
    </div>
    <pre id="cfg-rendered"></pre>
    <div id="cfg-issues"></div>
  </section>
```

- [ ] **Step 2: Implement in `app.js`**

```javascript
var cfgFields = [];

function buildForm() {
  getJSON("/api/config/fields", function (d) {
    cfgFields = d.fields;
    var f = document.getElementById("cfg-form");
    f.innerHTML = "";
    cfgFields.forEach(function (fld) {
      var wrap = document.createElement("div");
      wrap.className = "cfg-row";
      var label = fld.name + (fld.required ? " *" : "");
      var input;
      if (fld.kind === "enum") {
        input = "<select name='" + fld.name + "'" + (fld.fixed ? " disabled" : "") + ">" +
          fld.values.map(function (v) { return "<option>" + v + "</option>"; }).join("") + "</select>";
      } else {
        input = "<input name='" + fld.name + "' value='" + (fld.default || "") + "'>";
      }
      wrap.innerHTML = "<label>" + label + "</label>" + input +
        "<span class='hint'>" + (fld.help || "") + "</span>";
      f.appendChild(wrap);
    });
  });
}

function formValues() {
  var v = {};
  cfgFields.forEach(function (fld) {
    var el = document.querySelector("[name='" + fld.name + "']");
    if (el) v[fld.name] = el.value;
  });
  return v;
}

function renderIssues(issues) {
  document.getElementById("cfg-issues").innerHTML = issues.map(function (i) {
    return "<div class='issue " + i.level + "'>" + i.level + ": " + i.field + " — " + i.message + "</div>";
  }).join("");
}

document.getElementById("cfg-load-depots").onclick = function () {
  getJSON("/api/config/depots", function (d) {
    var sel = document.getElementById("cfg-depot");
    sel.innerHTML = "<option value=''>— choose a CORE RPM —</option>" +
      d.depots.map(function (x) {
        return "<option value='" + x.path + "' data-g='" + x.generic + "' data-l='" + x.load + "'>" +
               x.name + "</option>";
      }).join("");
  });
};

document.getElementById("cfg-depot").onchange = function () {
  var opt = this.options[this.selectedIndex];
  if (!opt.value) return;
  var set = function (n, val) { var e = document.querySelector("[name='" + n + "']"); if (e) e.value = val; };
  set("DEPOTFILE", opt.value);
  set("NEW_GENERIC", opt.getAttribute("data-g"));
  set("NEW_LOAD", opt.getAttribute("data-l"));
};

document.getElementById("cfg-preview").onclick = function () {
  postJSON("/api/config/preview", { values: formValues() }, function (status, d) {
    document.getElementById("cfg-rendered").textContent = d.rendered || "";
    renderIssues(d.issues || []);
  });
};

document.getElementById("cfg-save").onclick = function () {
  postJSON("/api/config/save", { values: formValues() }, function (status, d) {
    var msg = document.getElementById("cfg-msg");
    if (status === 200) { msg.textContent = "Saved — this config will be used on launch."; renderIssues([]); }
    else { msg.textContent = "Not saved — fix the errors."; renderIssues(d.issues || []); }
  });
};

buildForm();
```

- [ ] **Step 3: Style in `style.css`**

```css
.cfg-row { display: grid; grid-template-columns: 200px 260px 1fr; gap: .3rem .6rem; align-items: center; margin: .15rem 0; }
.cfg-row .hint { color: #777; font-size: 12px; }
#cfg-rendered { background: #111; color: #ddd; padding: .5rem; overflow-x: auto; }
.issue.ERROR { color: #b00020; } .issue.WARNING { color: #8a6d00; }
```

- [ ] **Step 4: Sanity check**

Run: `grep -q "/api/config/save" nfupgrader/web/app.js && grep -q "cfg-form" nfupgrader/web/index.html && echo WIRED`
Expected: `WIRED`.

- [ ] **Step 5: Commit**

```bash
git add nfupgrader/web/
git commit -m "feat: config builder UI (form, depot picker, preview, save)"
```

---

## Task 7: Full suite + smoke + manual

- [ ] **Step 1: Full suite**

Run: `python3 -m unittest discover -s tests -p 'test_*.py'`
Expected: all PASS.

- [ ] **Step 2: Smoke the config flow**

Start the server (point `NFU_DEPOT_DIRS` at a dir with a fake `netFLEX-CORE-*.rpm`). Redeem the token, then:
```bash
curl -s -b cj "http://127.0.0.1:$PORT/api/config/depots" | python3 -m json.tool
# preview a missing-required config -> issues + rendered
printf '{"values":{"NEW_GENERIC":"5.5.0"}}' > /tmp/prev.json
curl -s -b cj -X POST -H 'Content-Type: application/json' --data @/tmp/prev.json \
  "http://127.0.0.1:$PORT/api/config/preview" | python3 -m json.tool
# save a valid config -> saved path, PROMPTFORVALIDATE=NO in the file
printf '{"values":{"INCLOGDIR":"/usr/cnc/logs","NEW_GENERIC":"5.5.0","NEW_LOAD":"03","DEPOTFILE":"/tmp/core.rpm"}}' > /tmp/save.json
curl -s -b cj -X POST -H 'Content-Type: application/json' --data @/tmp/save.json \
  "http://127.0.0.1:$PORT/api/config/save"
grep PROMPTFORVALIDATE "$INCLOGDIR/nfupgrader_user.cfg"
```
Expected: depots listed; preview shows required-field ERRORs + a rendered cfg with `PROMPTFORVALIDATE=NO`; save returns a path and the file forces `PROMPTFORVALIDATE=NO`.

- [ ] **Step 3: Manual on r9dev19**

Load the page → **Upgrade configuration** section. Click **List depots** (if a depot dir is set), pick a CORE RPM → DEPOTFILE/generic/load auto-fill. **Preview** shows the rendered `.cfg` and any validation errors. **Use this config** saves it; the message confirms it will be used on launch. Then a pre-flight + Launch uses that config (no `NFU_USER_CONFIG` needed). Read-only until an actual launch is confirmed.

---

## Self-Review

**Spec coverage:** build (form + depot picker + derived generic/load) → T1/T3/T6; import (parse + show values + show forced-override) → T4 `/api/config/import` with `forces_promptforvalidate`; review screen → `/api/config/preview` rendered + issues, UI `#cfg-rendered`; `PROMPTFORVALIDATE=NO` always forced → `render_cfg`; customer file never edited → import only reads; save wires the active config into launch → T5 `state["user_config"]` + httpd resolve; full field list → `FIELDS`.

**Placeholder scan:** complete code each step; the two prose instructions (extend `make_ctx`, extend the `Context` field list/defaults) are bounded and asserted by tests. No TBD.

**Type/name consistency:** `render_cfg`/`parse_cfg`/`validate`/`derive_generic_load`/`list_depots` signatures consistent across cfg, api, tests, app.js. Issue shape `{field, level, message}` consistent (cfg.validate, api, UI). Depot shape `{path, name, generic, load}` consistent (cfg, api test, app.js data-attrs). `Context` gains exactly `state` and `depot_dirs` (defaults extended); `state["user_config"]` written by save and read by the httpd launch resolve. Endpoint paths `/api/config/{fields,depots,preview,import,save}` consistent between api, tests, app.js.

**Deferred (not gaps):** filesystem existence checks for path fields (DEPOTFILE exists) can be added as WARNING-level checks via Host later; the depot version→generic/load derivation targets the netFLEX `X.Y.Z.LL` pattern (INC `X.Y.LL` can get its own rule); a single "active config" (no multi-profile) is intentional for one host.
