# Merge Overview + Site Tabs Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** One Overview tab showing the site host table, where this host's row expands to the detailed host overview.

**Architecture:** Web-only change. `#tab-overview` hosts `#site-table`; a persistent `<tr id="local-detail">` (holding `#about-grid` and `#links`) lives in the tbody and is *moved* after the local row on each `/api/site` refresh instead of being rebuilt. Toggle state persists in localStorage.

**Tech Stack:** static HTML, ES5 JavaScript, CSS; Python `unittest` asset tests.

Spec: `~/WorkNotes/design/2026-09-23-nfupgrader-overview-site-merge-design.md`
Repo: `~/Git/nfupgrader`. Tests: `cd ~/Git/nfupgrader && timeout 180 python3 -m unittest discover -s tests -p 'test_*.py' 2>&1 | tail -3` (baseline 239 OK). Ignore BASE/VPATH warnings.

---

### Task 1: Merge the tabs

**Files:**
- Modify: `nfupgrader/web/index.html` (nav tabs; OVERVIEW and SITE sections)
- Modify: `nfupgrader/web/app.js` (`initTabs`, `cell`, `refreshSite`, `refreshInfo`, startup block)
- Modify: `nfupgrader/web/style.css`
- Test: `tests/test_web_assets.py`

- [ ] **Step 1: Failing asset tests** — in `tests/test_web_assets.py` add `"local-detail",` to `REQUIRED_IDS`, and add to `class TestWebAssets`:

```python
    def test_site_tab_merged_into_overview(self):
        self.assertNotIn('data-tab="site"', self.html)
        self.assertNotIn('id="tab-site"', self.html)
        overview = self.html.split('id="tab-overview"', 1)[1].split("</section>", 1)[0]
        for i in ("site-table", "local-detail", "about-grid", "links"):
            self.assertIn('id="%s"' % i, overview)

    def test_local_detail_toggle_persists_and_is_moved(self):
        self.assertIn("nfu_local_open", self.js)
        self.assertIn("function toggleLocal(", self.js)
        self.assertIn("tb.appendChild(detail)", self.js)

    def test_saved_tab_falls_back_to_overview(self):
        self.assertIn('names.indexOf(saved) >= 0 ? saved : "overview"', self.js)
```

Run: `python3 -m unittest tests.test_web_assets 2>&1 | tail -3` → FAIL.

- [ ] **Step 2: `index.html`**

2a. Delete the nav button `<button class="tab" data-tab="site">Site</button>`.

2b. Delete the whole `<!-- SITE -->` section (`<section class="panel" id="tab-site"> … </section>`).

2c. Replace the `<!-- OVERVIEW -->` section with:

```html
    <!-- OVERVIEW -->
    <section class="panel active" id="tab-overview">
      <div class="card">
        <h2>Site <span id="site-spin" class="spinner" style="display:none"></span></h2>
        <table id="site-table">
          <thead><tr>
            <th class="tog"></th><th>Host</th><th>Role</th><th>Reach</th><th>App</th>
            <th>CORE</th><th>Installed</th><th>Staged</th><th>GR</th>
          </tr></thead>
          <tbody>
            <tr id="local-detail" class="local-detail"><td colspan="9">
              <div id="about-grid" class="grid"></div>
              <h3>Multibox links</h3>
              <pre id="links">-</pre>
            </td></tr>
          </tbody>
        </table>
      </div>
    </section>
```

- [ ] **Step 3: `app.js`**

3a. `initTabs()`: replace the final `show(saved || "overview");` with:

```js
  var names = [];
  tabs.forEach(function (t) { names.push(t.getAttribute("data-tab")); });
  // A saved tab that no longer exists (e.g. the old "site" tab) falls back to Overview.
  show(saved && names.indexOf(saved) >= 0 ? saved : "overview");
```

3b. `cell()`: escape its value (host names/versions are server text):

```js
function cell(v) { return "<td>" + (v === null || v === undefined || v === "" ? "-" : esc(v)) + "</td>"; }
```

(`esc` is a function declaration later in the file, so it is hoisted.)

3c. Replace `refreshSite()` with the following, and add the toggle helpers right above it:

```js
var lastInfo = null;   // last /api/info response; used when /api/site lacks this host
var localOpen = true;
try { localOpen = localStorage.getItem("nfu_local_open") !== "0"; } catch (e) {}

function applyLocalOpen() {
  document.getElementById("local-detail").style.display = localOpen ? "" : "none";
  var t = document.getElementById("local-toggle");
  if (t) {
    t.textContent = localOpen ? "▾" : "▸";
    t.setAttribute("aria-expanded", localOpen ? "true" : "false");
  }
}

function toggleLocal() {
  localOpen = !localOpen;
  try { localStorage.setItem("nfu_local_open", localOpen ? "1" : "0"); } catch (e) {}
  applyLocalOpen();
}

function localFromInfo(i) {
  return { name: i.host, role: (i.topology || {}).role, reachable: true, is_local: true, info: i };
}

function refreshSite() {
  if (!loaded.site) spin("site-spin", true);
  getJSON("/api/site", function (s) {
    spin("site-spin", false);
    loaded.site = true;
    var tb = document.querySelector("#site-table tbody");
    var detail = document.getElementById("local-detail");
    // Drop every row except the persistent detail row, which is moved, not rebuilt,
    // so the host overview survives the 15 s site poll.
    Array.prototype.slice.call(tb.rows).forEach(function (r) {
      if (r !== detail) tb.removeChild(r);
    });
    var hosts = s.hosts.slice();
    var hasLocal = hosts.some(function (h) { return h.is_local; });
    if (!hasLocal && lastInfo) hosts.unshift(localFromInfo(lastInfo));
    hosts.forEach(function (h) {
      var i = h.info || {};
      var v = i.versions || {}, l = i.loads || {}, g = i.gr || {};
      var tr = document.createElement("tr");
      if (h.is_local) tr.className = "local";
      var reach = h.reachable
        ? "<span class='pill ok'>ok</span>"
        : "<span class='pill down'>unreachable</span>";
      var app = !h.reachable ? "<span class='pill unknown'>?</span>"
        : (i.app_up ? "<span class='pill ok'>UP</span>" : "<span class='pill down'>DOWN</span>");
      var tog = h.is_local
        ? "<td class='tog'><button id='local-toggle' class='toggle' type='button' " +
          "aria-label='Show host details'></button></td>"
        : "<td class='tog'></td>";
      tr.innerHTML = tog +
        "<td class='hostcell'>" + esc(h.name + (h.is_local ? " (this)" : "")) + "</td>" +
        cell(h.role) + "<td>" + reach + "</td><td>" + app + "</td>" +
        cell(v.core) + cell(l.installed) + cell(l.staged) + cell(g.transfer);
      tb.appendChild(tr);
      if (h.is_local) tb.appendChild(detail);
    });
    applyLocalOpen();
  });
}
```

(If there is no local row at all, the detail row stays where it was — first in the tbody — so the host overview remains visible.)

3d. `refreshInfo()`: delete `if (!loaded.info) spin("overview-spin", true);` and `spin("overview-spin", false);`, and add `lastInfo = i;` as the first line inside the `getJSON` callback.

3e. Click binding — add after the other `document.getElementById(...).onclick` bindings:

```js
document.getElementById("site-table").onclick = function (ev) {
  var t = ev.target;
  if (!t.closest) return;
  if (t.closest("#local-toggle") || t.closest("tr.local td.hostcell")) toggleLocal();
};
```

3f. Startup block: the line that sets `document.querySelector("#site-table tbody").innerHTML = "<tr>…Loading site…</tr>";` would wipe the detail row. Replace it with:

```js
(function () {
  var tb = document.querySelector("#site-table tbody");
  var tr = tb.insertRow(0);
  tr.innerHTML = "<td colspan='9' class='loading'><span class='spinner'></span> Loading site…</td>";
})();
applyLocalOpen();
```

Keep the existing `about-grid` "Loading host info…" and `links` "Loading…" lines.

- [ ] **Step 4: `style.css`** — append:

```css
#site-table th.tog, #site-table td.tog { width: 1.6rem; padding-right: 0; }
button.toggle { border: 0; background: none; cursor: pointer; font-size: .9rem; padding: 0 .2rem; }
tr.local td.hostcell { cursor: pointer; }
tr.local-detail > td { background: var(--surface); padding: .6rem .8rem 1rem 2rem; font-weight: normal; }
```

- [ ] **Step 5: Verify** — `node --check nfupgrader/web/app.js`; `grep -nE '=>|\blet\b|\bconst\b|`' nfupgrader/web/app.js` → no hits; `grep -n "overview-spin\|tab-site" nfupgrader/web/*` → no hits; full suite OK (and with `LC_ALL=C`).

- [ ] **Step 6: Commit**

```bash
git add nfupgrader/web/index.html nfupgrader/web/app.js nfupgrader/web/style.css tests/test_web_assets.py
git commit -m "feat(ui): merge Site into Overview; this host's row expands to host details"
```

---

### Task 2: Browser check (manual)

- [ ] Run the service on loopback (`NFU_UPGRADE_PATH=/bin/true`, scratch `INCLOGDIR`), open the URL.
- [ ] Overview shows the site table; this host's row is expanded with the host grid + multibox links.
- [ ] Toggle and host-name click collapse/expand; state survives a reload.
- [ ] Wait > 15 s: detail stays open and filled (not reset to "Loading…").
- [ ] With `localStorage.nfu_tab = "site"` set, a reload lands on Overview.
