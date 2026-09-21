# nfupgrader — UI Polish Pass Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Turn the functional one-long-scroll page into a cohesive tabbed dashboard with a consistent visual system — header + always-visible progress, tabs (Overview / Upgrade / Site / Log), cards, status badges, consistent typography/spacing/colors, responsive width — **without changing any backend behavior or endpoint**.

**Architecture:** Pure front-end restyle of `web/index.html`, `web/app.js`, `web/style.css`. A CSS design system (custom properties for palette/spacing, card/badge/table components) replaces the ad-hoc styles. The page is reorganized into a header, a persistent progress strip, a tab bar, and tab panels — but **every element ID the existing JS depends on is preserved**, and all polling/handlers keep working. Tab switching is a few lines of vanilla JS. No framework, no CDN, no external assets (the box is offline).

**Tech Stack:** HTML5 + CSS (flexbox/grid, custom properties) + vanilla ES5-compatible JS. No build step. Repo: `~/Git/nfupgrader`.

---

## Context

- Design: `~/WorkNotes/design/2026-09-15-web-upgrade-tool-design.md`. All feature phases (1, 2, 3, 4a, 4b, 5) are implemented; this pass is the deferred visual polish, explicitly held to the end so the restyle happens once over the full panel set.
- Hard constraints: self-contained (CSP-free but offline — inline or local `style.css`/`app.js` only, no CDN, no web fonts, no external images); must render in an unknown/shared Windows browser (stick to broadly-supported CSS — flexbox, grid, custom properties are fine; avoid bleeding-edge selectors); **no backend change** — `api.py`/`httpd.py` untouched.

## The element-ID contract (must be preserved)

`app.js` binds to these IDs; the restructure moves them between containers but must keep every one, unchanged:

```
progress:  phase  status  bar-fill  counter  current
log:       min-sev  log
info:      about-grid  links
site:      site-table (with a <tbody>)
checks:    mode  btn-check  btn-launch  preflight  override-box  override  reason  launch-msg  postflight
config:    cfg-load-depots  cfg-depot  cfg-form  cfg-preview  cfg-save  cfg-msg  cfg-rendered  cfg-issues
```

A test in Task 4 asserts all of these still exist in `index.html`, so the polish can't silently drop one.

## Information architecture (tabs)

| Tab | Contains | Rationale |
|---|---|---|
| **Overview** | About-this-host grid, GR summary, disk | At-a-glance state of this box |
| **Upgrade** | Config builder → pre-flight checks → launch controls → post-flight | The workflow, top-to-bottom in the order you do it |
| **Site** | All-hosts table | The multi-host picture |
| **Log** | Severity filter + trace console | Live output, given room |

A **header** (product name, hostname, app UP/DOWN badge) and a **persistent progress strip** (phase · status · bar · counter) sit above the tabs and show on every tab, since progress is the thing you always want visible.

---

## Task 1: CSS design system + component styles

**Files:**
- Rewrite: `nfupgrader/web/style.css`

- [ ] **Step 1: Replace `style.css` with the design system**

```css
:root {
  --bg: #f4f6f8; --surface: #ffffff; --ink: #1c2733; --muted: #5b6b7b;
  --line: #d7dee5; --accent: #2d6a4f; --accent-ink: #ffffff;
  --ok: #2d6a4f; --warn: #8a6d00; --err: #b00020; --unknown: #6b7684;
  --radius: 8px; --gap: 1rem; --mono: ui-monospace, "SFMono-Regular", Menlo, Consolas, monospace;
}
* { box-sizing: border-box; }
body { margin: 0; background: var(--bg); color: var(--ink);
  font-family: system-ui, -apple-system, "Segoe UI", Roboto, sans-serif; font-size: 14px; }

/* header + progress strip */
.app-header { background: var(--ink); color: #fff; padding: .7rem 1.2rem;
  display: flex; align-items: center; gap: 1rem; }
.app-header h1 { font-size: 1.05rem; margin: 0; font-weight: 600; }
.app-header .host { color: #b9c4cf; font-family: var(--mono); }
.badge { padding: .1rem .5rem; border-radius: 999px; font-size: 12px; font-weight: 600; }
.badge.up { background: var(--ok); color: #fff; }
.badge.down { background: var(--err); color: #fff; }
.badge.unknown { background: var(--unknown); color: #fff; }

.progress-strip { background: #223044; color: #fff; padding: .5rem 1.2rem;
  display: flex; align-items: center; gap: 1rem; font-size: 13px; }
.progress-strip .bar { flex: 1; background: #14202f; height: 12px; border-radius: 999px; overflow: hidden; max-width: 420px; }
.progress-strip #bar-fill { background: var(--accent); height: 100%; width: 0; transition: width .3s; }

/* tabs */
.tabs { display: flex; gap: .25rem; padding: 0 1.2rem; background: var(--surface);
  border-bottom: 1px solid var(--line); }
.tab { padding: .6rem 1rem; border: none; background: none; cursor: pointer;
  color: var(--muted); font-size: 14px; border-bottom: 3px solid transparent; }
.tab:hover { color: var(--ink); }
.tab.active { color: var(--accent); border-bottom-color: var(--accent); font-weight: 600; }

/* layout + cards */
main { padding: var(--gap) 1.2rem; max-width: 1100px; margin: 0 auto; }
.panel { display: none; }
.panel.active { display: block; }
.card { background: var(--surface); border: 1px solid var(--line); border-radius: var(--radius);
  padding: 1rem 1.2rem; margin-bottom: var(--gap); }
.card h2 { font-size: 1rem; margin: 0 0 .75rem; }
.card h3 { font-size: .9rem; margin: 1rem 0 .5rem; color: var(--muted); }

/* key/value grid (about) */
.grid { display: grid; grid-template-columns: 200px 1fr; gap: .3rem .9rem; }
.grid .k { color: var(--muted); } .grid .v { font-family: var(--mono); }

/* buttons + inputs */
button { padding: .5rem .9rem; border: 1px solid var(--line); border-radius: 6px;
  background: var(--surface); cursor: pointer; font-size: 14px; }
button:hover { border-color: var(--accent); }
button.primary { background: var(--accent); color: var(--accent-ink); border-color: var(--accent); }
button:disabled { opacity: .5; cursor: not-allowed; }
select, input[type=text], input:not([type]) { padding: .4rem .5rem; border: 1px solid var(--line);
  border-radius: 6px; font-size: 14px; }

/* tables (site + checks) */
table { border-collapse: collapse; width: 100%; font-size: 13px; }
th, td { border: 1px solid var(--line); padding: .35rem .6rem; text-align: left; }
th { background: #eef2f6; }
tr.local { background: #eaf5ee; font-weight: 600; }
.pill { padding: .05rem .45rem; border-radius: 999px; font-size: 12px; font-weight: 600; color: #fff; }
.pill.ok { background: var(--ok); } .pill.down { background: var(--err); }
.pill.unknown { background: var(--unknown); }

/* checks */
table.checks tr.fail-ERROR { background: #fdecea; }
table.checks tr.fail-WARNING { background: #fff8e1; }

/* config form */
.cfg-row { display: grid; grid-template-columns: 210px 280px 1fr; gap: .3rem .7rem;
  align-items: center; margin: .2rem 0; }
.cfg-row label { color: var(--muted); }
.cfg-row .hint { color: var(--muted); font-size: 12px; }
.issue.ERROR { color: var(--err); } .issue.WARNING { color: var(--warn); }

/* log console */
#log, #cfg-rendered { background: #0f1720; color: #d7e0ea; padding: .75rem;
  border-radius: var(--radius); font-family: var(--mono); font-size: 12px;
  overflow: auto; white-space: pre-wrap; }
#log { height: 60vh; }
.sev-WARN { color: #e9c46a; } .sev-ERROR { color: #e76f51; }
.sev-FATAL { color: #ff6b6b; font-weight: bold; } .sev-EXEC { color: #8895a3; }

/* responsive */
@media (max-width: 720px) {
  .grid { grid-template-columns: 1fr; }
  .cfg-row { grid-template-columns: 1fr; }
  .progress-strip { flex-wrap: wrap; }
}
```

- [ ] **Step 2: Verify it is valid, self-contained CSS**

Run: `grep -nE 'http://|https://|@import|url\(' nfupgrader/web/style.css || echo "NO EXTERNAL REFS"`
Expected: `NO EXTERNAL REFS` (no CDN/fonts/external images).

- [ ] **Step 3: Commit**

```bash
git add nfupgrader/web/style.css
git commit -m "feat(ui): design-system stylesheet (header, tabs, cards, badges)"
```

---

## Task 2: Restructure `index.html` into header + progress strip + tabs

**Files:**
- Rewrite: `nfupgrader/web/index.html`

Preserve **every** ID from the contract above; only the surrounding structure/classes change.

- [ ] **Step 1: Replace `index.html`**

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
  <header class="app-header">
    <h1>netFLEX Upgrade</h1>
    <span class="host" id="host">this host</span>
    <span id="app-badge" class="badge unknown">APP ?</span>
  </header>

  <div class="progress-strip">
    <span>Phase: <b id="phase">-</b></span>
    <span>Status: <b id="status">-</b></span>
    <div class="bar"><div id="bar-fill"></div></div>
    <span id="counter">0 / 0</span>
    <span id="current" style="color:#b9c4cf"></span>
  </div>

  <nav class="tabs">
    <button class="tab active" data-tab="overview">Overview</button>
    <button class="tab" data-tab="upgrade">Upgrade</button>
    <button class="tab" data-tab="site">Site</button>
    <button class="tab" data-tab="log">Log</button>
  </nav>

  <main>
    <!-- OVERVIEW -->
    <section class="panel active" id="tab-overview">
      <div class="card">
        <h2>About this host</h2>
        <div id="about-grid" class="grid"></div>
        <h3>Multibox links</h3>
        <pre id="links">-</pre>
      </div>
    </section>

    <!-- UPGRADE workflow -->
    <section class="panel" id="tab-upgrade">
      <div class="card">
        <h2>1 · Upgrade configuration</h2>
        <div style="margin-bottom:.5rem">
          <button id="cfg-load-depots">List depots</button>
          <select id="cfg-depot"><option value="">— choose a CORE RPM —</option></select>
        </div>
        <form id="cfg-form"></form>
        <div style="margin:.5rem 0">
          <button type="button" id="cfg-preview">Preview</button>
          <button type="button" id="cfg-save" class="primary">Use this config</button>
          <span id="cfg-msg"></span>
        </div>
        <pre id="cfg-rendered"></pre>
        <div id="cfg-issues"></div>
      </div>

      <div class="card">
        <h2>2 · Pre-flight checks &amp; launch</h2>
        <div style="margin-bottom:.5rem">
          <label>Phase:
            <select id="mode"><option value="stage">Stage</option><option value="upgrade">Upgrade</option></select>
          </label>
          <button id="btn-check">Run pre-flight checks</button>
          <button id="btn-launch" class="primary" disabled>Launch</button>
          <span id="launch-msg"></span>
        </div>
        <div id="preflight"></div>
        <div id="override-box" style="display:none">
          <label><input type="checkbox" id="override"> Override blocking checks</label>
          <input type="text" id="reason" placeholder="reason (required to override)" size="40">
        </div>
        <h3>Post-flight</h3>
        <div id="postflight">-</div>
      </div>
    </section>

    <!-- SITE -->
    <section class="panel" id="tab-site">
      <div class="card">
        <h2>Site</h2>
        <table id="site-table">
          <thead><tr>
            <th>Host</th><th>Role</th><th>Reach</th><th>App</th>
            <th>CORE</th><th>Installed</th><th>Staged</th><th>GR</th>
          </tr></thead>
          <tbody></tbody>
        </table>
      </div>
    </section>

    <!-- LOG -->
    <section class="panel" id="tab-log">
      <div class="card">
        <h2>Log</h2>
        <label>Show:
          <select id="min-sev">
            <option value="">All</option>
            <option value="INFO">INFO+</option>
            <option value="WARN">WARN+</option>
            <option value="ERROR">ERROR+</option>
            <option value="FATAL">FATAL only</option>
          </select>
        </label>
        <pre id="log"></pre>
      </div>
    </section>
  </main>

  <script src="/app.js"></script>
</body>
</html>
```

- [ ] **Step 2: Confirm every contract ID is present**

Run:
```bash
for id in host app-badge phase status bar-fill counter current \
  about-grid links cfg-load-depots cfg-depot cfg-form cfg-preview cfg-save cfg-msg \
  cfg-rendered cfg-issues mode btn-check btn-launch launch-msg preflight override-box \
  override reason postflight site-table min-sev log; do
  grep -q "id=\"$id\"" nfupgrader/web/index.html || echo "MISSING: $id"
done; echo "done"
```
Expected: `done` with no `MISSING` lines.

- [ ] **Step 3: Commit**

```bash
git add nfupgrader/web/index.html
git commit -m "feat(ui): tabbed layout with header + persistent progress strip"
```

---

## Task 3: Tab navigation JS + app UP/DOWN badge (no behavior change)

**Files:**
- Modify: `nfupgrader/web/app.js`

- [ ] **Step 1: Add tab switching near the top of `app.js`** (after the `getJSON`/`postJSON` helpers)

```javascript
function initTabs() {
  var tabs = document.querySelectorAll(".tab");
  var panels = document.querySelectorAll(".panel");
  function show(name) {
    tabs.forEach(function (t) { t.classList.toggle("active", t.getAttribute("data-tab") === name); });
    panels.forEach(function (p) { p.classList.toggle("active", p.id === "tab-" + name); });
    try { localStorage.setItem("nfu_tab", name); } catch (e) {}
  }
  tabs.forEach(function (t) { t.onclick = function () { show(t.getAttribute("data-tab")); }; });
  var saved;
  try { saved = localStorage.getItem("nfu_tab"); } catch (e) {}
  show(saved || "overview");
}
```

- [ ] **Step 2: Set the header app badge inside `refreshProgress` or `refreshInfo`**

In `refreshInfo`, after computing `i.app_up`, add:
```javascript
    var badge = document.getElementById("app-badge");
    badge.textContent = i.app_up ? "APP UP" : "APP DOWN";
    badge.className = "badge " + (i.app_up ? "up" : "down");
```

- [ ] **Step 3: Call `initTabs()` in the startup block**

Add `initTabs();` alongside the initial `refreshProgress()` etc. Keep all existing `setInterval` polling exactly as-is (tabs only show/hide; data keeps refreshing).

- [ ] **Step 4: Update the site table reachability/app cells to use pills** (optional polish, keeps data identical)

In `refreshSite`, replace the plain `reach`/`app` cells with pill markup:
```javascript
      var reach = h.reachable
        ? "<span class='pill ok'>ok</span>"
        : "<span class='pill down'>unreachable</span>";
      var app = !h.reachable ? "<span class='pill unknown'>?</span>"
        : (i.app_up ? "<span class='pill ok'>UP</span>" : "<span class='pill down'>DOWN</span>");
      tr.innerHTML =
        cell(h.name + (h.is_local ? " (this)" : "")) + cell(h.role) +
        "<td>" + reach + "</td><td>" + app + "</td>" +
        cell(v.core) + cell(l.installed) + cell(l.staged) + cell(g.transfer);
```

- [ ] **Step 5: Verify wiring**

Run: `grep -q "initTabs" nfupgrader/web/app.js && grep -q "app-badge" nfupgrader/web/app.js && echo WIRED`
Expected: `WIRED`.

- [ ] **Step 6: Commit**

```bash
git add nfupgrader/web/app.js
git commit -m "feat(ui): tab navigation, app badge, status pills (no behavior change)"
```

---

## Task 4: Guard tests + full suite

**Files:**
- Create: `tests/test_web_assets.py`

- [ ] **Step 1: Write a guard test**

`tests/test_web_assets.py`:
```python
import os
import re
import unittest

WEB = os.path.join(os.path.dirname(os.path.dirname(__file__)), "nfupgrader", "web")

REQUIRED_IDS = [
    "host", "app-badge", "phase", "status", "bar-fill", "counter", "current",
    "about-grid", "links", "cfg-load-depots", "cfg-depot", "cfg-form",
    "cfg-preview", "cfg-save", "cfg-msg", "cfg-rendered", "cfg-issues",
    "mode", "btn-check", "btn-launch", "launch-msg", "preflight", "override-box",
    "override", "reason", "postflight", "site-table", "min-sev", "log",
]


class TestWebAssets(unittest.TestCase):
    def setUp(self):
        self.html = open(os.path.join(WEB, "index.html")).read()
        self.css = open(os.path.join(WEB, "style.css")).read()
        self.js = open(os.path.join(WEB, "app.js")).read()

    def test_all_required_ids_present(self):
        missing = [i for i in REQUIRED_IDS if ('id="%s"' % i) not in self.html]
        self.assertEqual(missing, [], "index.html missing IDs: %s" % missing)

    def test_no_external_resources(self):
        # self-contained: no CDN/fonts/remote images in any asset
        for name, text in (("html", self.html), ("css", self.css)):
            self.assertNotIn("http://", text, name)
            self.assertNotIn("https://", text, name)
        self.assertNotIn("@import", self.css)

    def test_tabs_and_panels_match(self):
        tabs = set(re.findall(r'data-tab="([a-z]+)"', self.html))
        panels = set(re.findall(r'id="tab-([a-z]+)"', self.html))
        self.assertEqual(tabs, panels, "tab buttons and panels differ")


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run it**

Run: `python3 -m unittest tests.test_web_assets -v`
Expected: PASS (3 tests).

- [ ] **Step 3: Full suite — proves no backend regression**

Run: `python3 -m unittest discover -s tests -p 'test_*.py'`
Expected: all PASS (previous count + 3).

- [ ] **Step 4: Commit**

```bash
git add tests/test_web_assets.py
git commit -m "test(ui): guard required element IDs, self-containment, tab/panel parity"
```

---

## Task 5: Live smoke + manual acceptance

- [ ] **Step 1: Serve and confirm assets load**

Start the server against fixtures/box, redeem the token, then:
```bash
curl -s -b cj "http://127.0.0.1:$PORT/" | grep -q 'class="tabs"' && echo "tabs served"
curl -s -b cj "http://127.0.0.1:$PORT/style.css" | grep -q ':root' && echo "css served"
curl -s -b cj "http://127.0.0.1:$PORT/app.js"   | grep -q 'initTabs' && echo "js served"
```
Expected: all three confirmations.

- [ ] **Step 2: Manual browser acceptance on r9dev19**

Load the page. Confirm: header shows host + APP badge; progress strip is visible on every tab; the four tabs switch content and the active tab persists on reload; Overview shows host/links; Upgrade shows the config→checks→launch flow; Site shows the host table with pills; Log fills and the severity filter works. Nothing external is fetched (offline-safe). All existing actions (list depots, preview, save, run checks, launch gating/override) still work — this pass changed only presentation.

---

## Self-Review

**Scope:** front-end only — `index.html`, `app.js`, `style.css`, plus a guard test. No `api.py`/`httpd.py`/module change, so the 117 existing tests still pass unchanged (Task 4 Step 3 proves it).

**ID contract:** every ID `app.js` uses is enumerated and asserted present (Task 4). The restructure moves elements into tab panels but renames nothing, so all handlers/pollers keep working.

**Placeholder scan:** complete `style.css`, `index.html`, and JS additions given; no TBD.

**Consistency:** tab `data-tab` names (`overview/upgrade/site/log`) match panel IDs (`tab-*`) — asserted by `test_tabs_and_panels_match`. `app-badge` is the one new ID (set in `refreshInfo`, present in HTML, in the required list). Progress-strip IDs (`phase/status/bar-fill/counter/current`) reused from the old markup, so `refreshProgress` is unchanged.

**Constraints honored:** no external resources (asserted), ES5-compatible JS (var/function, no arrow-only APIs beyond forEach which is fine), responsive breakpoint, broadly-supported CSS. Deferred: no icon set/logo (text + emoji-free badges); a dark theme could be a later toggle via the existing custom properties.
