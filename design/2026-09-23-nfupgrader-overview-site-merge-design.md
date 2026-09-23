# Merge Overview + Site tabs — design

Date: 2026-09-23
Repo: nfupgrader (web UI only — no server/API change)

## Goal

One **Overview** tab: a site table of every host, where this host's row can be expanded to show the
detailed host overview that used to be its own tab.

## Current state

- Overview tab (`#tab-overview`): `#about-grid` key/value grid + `#links` (multibox links), filled
  by `refreshInfo()` from `/api/info` (local host, full detail incl. incinfo/rdb/dbcheck, disks).
- Site tab (`#tab-site`): `#site-table` (Host, Role, Reach, App, CORE, Installed, Staged, GR),
  filled by `refreshSite()` from `/api/site`; the tbody is rebuilt on every 15 s poll. Peers carry
  tier-1 info only.

## Design

- Remove the Site tab button and `#tab-site` panel. `#tab-overview` holds one card "Site" with
  `#site-table` plus a leading narrow column.
- **Local row only** gets a toggle (▸ collapsed / ▾ expanded) in the first column; peers get an empty
  cell. Clicking the toggle (or the host name) shows/hides a detail row directly beneath it.
- **Detail row** `<tr id="local-detail"><td colspan="9">…</td></tr>` contains `#about-grid` and the
  "Multibox links" `<pre id="links">`. It is created once (in index.html, inside a hidden holder,
  or in JS at startup) and **moved** into place after the local row on every `refreshSite()` rebuild,
  so its content survives site polls. `refreshInfo()` keeps writing `#about-grid`/`#links` as today.
- **Default expanded**; the open/closed state persists in `localStorage` key `nfu_local_open`
  (wrapped in try/catch like `nfu_tab`).
- **No local host in `/api/site`** (e.g. missing cnc.cnfg): synthesize a local row from the last
  `/api/info` response (host, role from topology, app_up, versions.core, loads, gr.transfer) so the
  detail stays reachable. Before either response arrives, show the existing loading row.
- **Saved tab fallback**: `initTabs()` falls back to `overview` when the saved tab (e.g. `site`) has
  no matching button.
- One spinner in the card header, shown until the first `/api/site` response (as `#site-spin` does
  now); the old `#overview-spin` is removed (the detail grid shows its own "Loading host info…"
  placeholder until `/api/info` answers).
- The header APP badge is still driven by `refreshInfo()`.
- ES5 only; escape host names/values as the rest of the UI does (use `esc()` for new code).

## Testing (`tests/test_web_assets.py`)

- REQUIRED_IDS: keep `about-grid`, `links`, `site-table`; add `local-detail`; drop nothing else.
- Tabs/panels pairing test still passes (site button + panel both removed).
- New asserts: no `data-tab="site"`; app.js references `nfu_local_open`; initTabs has a fallback
  when the saved tab is unknown; the detail row is moved with `insertBefore`/`appendChild` rather
  than rebuilt via innerHTML (assert the move call on `local-detail`).
- Manual: load the page, confirm the table renders, local row expanded, toggle collapses/expands and
  persists across reload, detail survives a 15 s site poll.

## Out of scope

Expanding peer rows; any API change.
