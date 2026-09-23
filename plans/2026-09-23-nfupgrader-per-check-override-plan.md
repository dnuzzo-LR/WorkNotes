# Per-check Pre-flight Override Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the single "override all blocking checks" switch with a per-check override (one shared reason), and warn loudly at launch when overridden failures remain.

**Architecture:** `/api/launch` accepts `overrides: [check names]` and returns 409 naming any blocking check not in that list. The UI renders an Override checkbox on each failed ERROR row, gates the Launch button on full coverage + reason, and builds a warning `window.confirm` listing overridden (and advisory WARNING) failures.

**Tech Stack:** Python 3.6.8 stdlib (`unittest`, no f-strings), plain ES5 JavaScript (no arrow functions / `let` / template literals), static HTML.

Spec: `~/WorkNotes/design/2026-09-23-per-check-preflight-override-design.md`
Repo: `~/Git/nfupgrader` (baseline: `python3 -m unittest discover -s tests -p 'test_*.py'` → 129 tests OK)

---

## File map

- Modify `nfupgrader/api.py` (`/api/launch` handler, ~lines 143-165) — override list validation.
- Modify `tests/test_api.py` (~lines 146-172) — replace old override tests.
- Modify `nfupgrader/web/index.html` (`#override-box`, ~lines 74-78) — drop global checkbox, add status line.
- Modify `nfupgrader/web/app.js` (`renderChecks`, `runPreflight`, `launch`, bindings ~lines 156-190, 277-278).
- Modify `tests/test_web_assets.py` — required IDs.

Test fixture facts (`tests/fixtures/checks.test.json` + `make_ctx` in `tests/test_api.py`): in `upgrade` mode only `staged-present` fails (ERROR). `disk-usr4`, `gr-idle`, `no-grestore`, `dbcheck-clean` pass. Making dbcheck output contain `ERROR` (`self.ctx.host.responses[("dbcheck", "-AV")] = RunResult(0, "ERROR: x\n", "")`) adds `dbcheck-clean` as a second blocking failure. `stage` mode has no blocking failures.

---

### Task 1: API — per-check overrides on `/api/launch`

**Files:**
- Modify: `nfupgrader/api.py` (the `if path == "/api/launch" and method == "POST":` block)
- Test: `tests/test_api.py`

- [ ] **Step 1: Replace the old override tests with new failing tests**

In `tests/test_api.py` (`RunResult` is already imported), delete `test_launch_override_requires_reason` and `test_launch_override_with_reason_proceeds_and_audits`, and put these in their place (inside `class TestApi`):

```python
    def _launch(self, payload):
        sid = self._session()
        status, _, body = api.dispatch(
            "POST", "/api/launch", {}, json.dumps(payload).encode(),
            {"nfu_sid": sid}, "127.0.0.1", self.ctx)
        return status, json.loads(body.decode())

    def _audit(self):
        path = os.path.join(self.tmp, "audit.jsonl")
        if not os.path.exists(path):
            return []
        return [json.loads(l) for l in open(path).read().splitlines()]

    def test_launch_partial_override_names_missing(self):
        self.ctx.host.responses[("dbcheck", "-AV")] = RunResult(0, "ERROR: x\n", "")
        status, data = self._launch({"mode": "upgrade", "overrides": ["staged-present"],
                                     "reason": "known good"})
        self.assertEqual(status, 409)
        self.assertEqual(data["not_overridden"], ["dbcheck-clean"])
        self.assertEqual(sorted(b["name"] for b in data["blocking"]),
                         ["dbcheck-clean", "staged-present"])

    def test_launch_full_override_requires_reason(self):
        status, _ = self._launch({"mode": "upgrade", "overrides": ["staged-present"]})
        self.assertEqual(status, 400)
        status, _ = self._launch({"mode": "upgrade", "overrides": ["staged-present"],
                                  "reason": "   "})
        self.assertEqual(status, 400)

    def test_launch_full_override_with_reason_audits_names(self):
        self.ctx.host.responses[("dbcheck", "-AV")] = RunResult(0, "ERROR: x\n", "")
        status, _ = self._launch({"mode": "upgrade",
                                  "overrides": ["staged-present", "dbcheck-clean"],
                                  "reason": "known good"})
        self.assertEqual(status, 202)
        recs = self._audit()
        ovr = [r for r in recs if r["event"] == "override"]
        self.assertEqual(len(ovr), 1)
        self.assertEqual(sorted(ovr[0]["checks"]), ["dbcheck-clean", "staged-present"])
        self.assertEqual(ovr[0]["reason"], "known good")
        self.assertIn("launch", [r["event"] for r in recs])

    def test_launch_stale_override_name_ignored(self):
        status, _ = self._launch({"mode": "upgrade",
                                  "overrides": ["staged-present", "disk-usr4", "no-such"],
                                  "reason": "known good"})
        self.assertEqual(status, 202)
        ovr = [r for r in self._audit() if r["event"] == "override"]
        self.assertEqual(ovr[0]["checks"], ["staged-present"])

    def test_launch_no_blocking_no_override_record(self):
        status, _ = self._launch({"mode": "stage"})
        self.assertEqual(status, 202)
        events = [r["event"] for r in self._audit()]
        self.assertNotIn("override", events)
        self.assertIn("launch", events)

    def test_launch_overrides_must_be_list(self):
        status, _ = self._launch({"mode": "upgrade", "overrides": "staged-present",
                                  "reason": "x"})
        self.assertEqual(status, 400)

    def test_launch_legacy_override_flag_no_longer_bypasses(self):
        status, data = self._launch({"mode": "upgrade", "override": True, "reason": "x"})
        self.assertEqual(status, 409)
        self.assertEqual(data["not_overridden"], ["staged-present"])
```

(`AuditLog.record` writes `{ts, event, **fields}`, so `reason`/`checks` are top-level keys.)

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd ~/Git/nfupgrader && python3 -m unittest tests.test_api -v 2>&1 | tail -30`
Expected: FAIL/ERROR on `test_launch_partial_override_names_missing` (KeyError `not_overridden`), `test_launch_stale_override_name_ignored` / `test_launch_full_override_with_reason_audits_names` (409 instead of 202), `test_launch_overrides_must_be_list`, `test_launch_legacy_override_flag_no_longer_bypasses`.

- [ ] **Step 3: Implement**

In `nfupgrader/api.py`, replace the body of the `/api/launch` handler from `results = checks.run_checks(...)` through the `ctx.audit.record("override", ...)` call with:

```python
        overrides = payload.get("overrides", [])
        if not isinstance(overrides, list):
            return _json(400, {"error": "overrides must be a list of check names"})
        results = checks.run_checks(ctx.host, _load_checks(ctx), "preflight", mode, _machtype(ctx))
        blocking = checks.blocking_failures(results)
        # Every blocking failure must be overridden by name; names for checks
        # that now pass (or never existed) are simply ignored.
        missing = [b["name"] for b in blocking if b["name"] not in overrides]
        if missing:
            return _json(409, {"error": "pre-flight checks failed", "blocking": blocking,
                               "not_overridden": missing})
        if blocking:
            reason = (payload.get("reason") or "").strip()
            if not reason:
                return _json(400, {"error": "override requires a reason"})
            ctx.audit.record("override", mode=mode, client=client_ip, reason=reason,
                             checks=[b["name"] for b in blocking])
```

Leave the following `ctx.audit.record("launch", ...)` and 202 return unchanged.

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd ~/Git/nfupgrader && python3 -m unittest discover -s tests -p 'test_*.py' 2>&1 | tail -3`
Expected: `OK` (134 tests: 129 − 2 removed + 7 added).

- [ ] **Step 5: Commit**

```bash
cd ~/Git/nfupgrader
git add nfupgrader/api.py tests/test_api.py
git commit -m "feat(launch): require each blocking pre-flight check to be overridden by name"
```

---

### Task 2: UI — per-row override, gated Launch, launch warning

**Files:**
- Modify: `nfupgrader/web/index.html` (`#override-box`)
- Modify: `nfupgrader/web/app.js`
- Test: `tests/test_web_assets.py`

- [ ] **Step 1: Update the asset test (failing)**

In `tests/test_web_assets.py`, change `REQUIRED_IDS` — remove `"override",` and add `"override-msg",`:

```python
    "mode", "btn-check", "btn-launch", "launch-msg", "preflight", "override-box",
    "override-msg", "reason", "postflight", "site-table", "min-sev", "log",
```

Add to `class TestWebAssets`:

```python
    def test_launch_posts_per_check_overrides(self):
        self.assertIn("overrides:", self.js)
        self.assertNotIn('id="override"', self.html)
```

- [ ] **Step 2: Run to verify failure**

Run: `cd ~/Git/nfupgrader && python3 -m unittest tests.test_web_assets -v 2>&1 | tail -8`
Expected: FAIL — `override-msg` missing; `overrides:` not in app.js.

- [ ] **Step 3: Update `index.html`**

Replace the `#override-box` div with:

```html
        <div id="override-box" style="display:none">
          <p>Tick <b>Override</b> on each failed check you accept, and give a reason.</p>
          <input type="text" id="reason" placeholder="reason (required to override)" size="40">
          <span id="override-msg"></span>
        </div>
```

- [ ] **Step 4: Update `app.js`**

4a. Replace `renderChecks` with a version that optionally renders an Override column (used only for pre-flight):

```js
function renderChecks(el, data, overridable) {
  var html = "<table class='checks'><tr><th></th><th>Check</th><th>Detail</th>" +
             (overridable ? "<th>Override</th>" : "") + "</tr>";
  data.results.forEach(function (r) {
    var mark = r.passed ? "✅" : (r.severity === "ERROR" ? "❌" : "⚠️");
    var ovr = "";
    if (overridable) {
      ovr = "<td>" + (r.blocking
        ? "<input type='checkbox' class='ovr' data-name='" + r.name + "'>" : "") + "</td>";
    }
    html += "<tr class='" + (r.passed ? "pass" : "fail-" + r.severity) + "'>" +
            "<td>" + mark + "</td><td>" + r.name + " — " + r.description + "</td>" +
            "<td>" + (r.detail || "") + "</td>" + ovr + "</tr>";
  });
  html += "</table>";
  el.innerHTML = html;
}
```

4b. Replace `runPreflight` and `launch` with:

```js
var preflight = null;  // last pre-flight /api/checks response for the selected mode

function overriddenNames() {
  var boxes = document.querySelectorAll("#preflight input.ovr:checked");
  return Array.prototype.map.call(boxes, function (b) { return b.getAttribute("data-name"); });
}

function updateLaunchState() {
  var btn = document.getElementById("btn-launch");
  var why = document.getElementById("override-msg");
  if (!preflight) { btn.disabled = true; why.textContent = ""; return; }
  var total = preflight.blocking.length;
  var missing = total - overriddenNames().length;
  var reason = document.getElementById("reason").value.trim();
  var msg = "";
  if (missing > 0) msg = missing + " of " + total + " blocking checks not overridden";
  else if (total > 0 && !reason) msg = "reason required to override";
  why.textContent = msg;
  btn.disabled = msg !== "";
}

function runPreflight() {
  var mode = document.getElementById("mode").value;
  getJSON("/api/checks?when=preflight&phase=" + mode, function (data) {
    preflight = data;
    renderChecks(document.getElementById("preflight"), data, true);
    document.getElementById("override-box").style.display =
      data.blocking.length > 0 ? "block" : "none";
    updateLaunchState();
  });
}

function resetPreflight() {
  preflight = null;
  document.getElementById("preflight").innerHTML = "";
  document.getElementById("override-box").style.display = "none";
  updateLaunchState();
}

function launchConfirmText(mode) {
  var base = "Start the " + mode.toUpperCase() +
             " now? This runs the real upgrade and cannot be paused.";
  var over = preflight.results.filter(function (r) { return r.blocking; });
  var warn = preflight.results.filter(function (r) {
    return !r.passed && r.severity !== "ERROR";
  });
  if (!over.length && !warn.length) return base;
  var lines = ["WARNING: launching with unresolved pre-flight failures.", ""];
  if (over.length) {
    lines.push("Overridden:");
    over.forEach(function (r) { lines.push("  - " + r.name + " — " + (r.detail || "")); });
    lines.push("");
  }
  if (warn.length) {
    lines.push("Warnings:");
    warn.forEach(function (r) { lines.push("  - " + r.name + " — " + (r.detail || "")); });
    lines.push("");
  }
  lines.push("Upgrading with unresolved pre-flight failures may leave the system " +
             "in an unsupported state.", "", base);
  return lines.join("\n");
}

function launch() {
  var mode = document.getElementById("mode").value;
  var reason = document.getElementById("reason").value;
  if (!preflight) return;
  if (!window.confirm(launchConfirmText(mode))) return;
  postJSON("/api/launch", { mode: mode, overrides: overriddenNames(), reason: reason },
    function (status, body) {
      var msg = document.getElementById("launch-msg");
      if (status === 202) { msg.textContent = "Launched " + mode + "…"; }
      else if (status === 409) {
        msg.textContent = "Blocked — not overridden: " +
          (body.not_overridden || []).join(", ") + ". Checks re-run.";
        runPreflight();
      }
      else { msg.textContent = "Error: " + (body.error || status); }
    });
}
```

4c. At the bindings (near `document.getElementById("btn-launch").onclick = launch;`) add:

```js
document.getElementById("preflight").onchange = updateLaunchState;
document.getElementById("reason").oninput = updateLaunchState;
document.getElementById("mode").onchange = resetPreflight;
```

(`#mode` has no existing `onchange` handler.)

4d. Verify no ES6 slipped in: `grep -nE '=>|\blet\b|\bconst\b|`' nfupgrader/web/app.js` → no new hits in the edited region.

- [ ] **Step 5: Run the full suite**

Run: `cd ~/Git/nfupgrader && python3 -m unittest discover -s tests -p 'test_*.py' 2>&1 | tail -3`
Expected: `OK` (135 tests).

- [ ] **Step 6: Commit**

```bash
cd ~/Git/nfupgrader
git add nfupgrader/web/index.html nfupgrader/web/app.js tests/test_web_assets.py
git commit -m "feat(ui): per-check pre-flight override with launch warning"
```

---

### Task 3: Manual smoke check in the browser

- [ ] **Step 1:** `cd ~/Git/nfupgrader && ./run.sh` (note printed URL + token). Open it, go to the Upgrade tab.
- [ ] **Step 2:** Select Upgrade, Run pre-flight checks. Expect: failed ERROR rows have an Override checkbox; Launch disabled with "N of M blocking checks not overridden".
- [ ] **Step 3:** Tick all → message "reason required to override"; type a reason → Launch enabled.
- [ ] **Step 4:** Click Launch → confirm dialog begins with "WARNING: launching with unresolved pre-flight failures." and lists overridden checks. **Click Cancel** (do not launch a real upgrade on a dev box).
- [ ] **Step 5:** Switch mode to Stage → table clears, Launch disabled until re-run.
