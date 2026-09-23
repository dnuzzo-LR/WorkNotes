# Reports before/after compare — design

Date: 2026-09-23
Repo: nfupgrader
Source feature: `~/Git/nf-install` — `reports.json` + `modules/unified_manager.py`
(`ReportManager`: Run All → `reports/JUMBO_<ts>.txt`; List/View/Compare → `diff` / `diff -y` of two
user-picked files).
Parent design: `~/WorkNotes/design/2026-09-15-web-upgrade-tool-design.md`

## Goal

Let the customer compare the system before and after an upgrade by running a fixed set of netFLEX
reports and viewing a per-report diff in the web UI. Snapshots are taken automatically around an
**Upgrade** launch and can also be taken manually at any time.

## Decisions (from brainstorming)

- Capture: automatic around Upgrade launches **and** a manual "Run reports now" button.
- Launch sequencing: Launch returns immediately; the server takes the *before* snapshot in the
  background, then starts nf_upgrade. Report failures/timeouts are recorded, never block.
- Modes: automatic snapshots for `upgrade` only; `stage` launches are unchanged.
- Compare UI: per-report list with changed/unchanged/failed badges; expand a report for its diff,
  side-by-side or unified.
- Diffs computed server-side with `difflib`; browser only renders.
- Retention: newest 20 snapshots kept; older pruned automatically.
- An *after* snapshot is taken even if the upgrade dies (flagged).

## 1. Report definitions

- `nfupgrader/reports.default.json`: the 21 shell entries from nf-install `reports.json`
  (the two `type: function` menu entries dropped). Same `{name, command}` shape; `type` ignored.
- Override with env `NFU_REPORTS` (mirrors `NFU_CHECKS`).
- Each report runs as `host.run(["sh", "-c", command], timeout=300)`, sequentially.

## 2. Snapshot storage — `nfupgrader/reports.py`

```
$INCLOGDIR/nfupgrader_reports/<YYYYmmdd_HHMMSS>_<label>/
    manifest.json
    01_RDB_Test.txt  02_RDB_Version.txt ...
```

- Snapshot id = directory name. `label` ∈ `before | after | manual`.
- Report file = stdout; if stderr non-empty, appended after a line `----- STDERR -----`.
  No timestamps/rc in the file so they never show as diffs.
- Filename = `NN_<safe name>.txt` (NN = 1-based position; safe name = alnum/`-`/`_`, spaces → `_`).
- `manifest.json`:
  ```json
  {"id": "...", "label": "before", "mode": "upgrade", "pair_of": null,
   "started": "ISO-8601", "finished": "ISO-8601|null",
   "status": "running|complete|incomplete", "note": "",
   "reports": [{"name": "...", "command": "...", "file": "01_....txt",
                "rc": 0, "secs": 1.2, "error": null}]}
  ```
  `error` is `"timeout"` or the exception text when the report couldn't run; `rc` is null then.
  The manifest is rewritten after each report so progress survives a crash.
- Reading: a manifest with `status: running` and no live job for that id is reported as
  `incomplete`.
- `pair_of`: on an *after* snapshot, the id of its *before*.
- Pruning: after a snapshot completes, delete the oldest snapshot dirs beyond 20
  (never the running one).
  - A *before* and the *after*(s) whose `pair_of` names it form one unit: pruned together
    (each dir counts toward the excess) and kept whole if any member is protected.
  - Protected: the live job's id and its `pair_of`, plus the newest *before* while it has no
    *after* yet (an upgrade from it may be in flight).

## 3. Job runner

- Single in-process runner (a `threading.Thread`); at most one snapshot job at a time.
- In-memory job state: `{id, label, index, total, current, status}` served by
  `GET /api/reports/job` (`{"job": null}` when idle).
- Starting a job while one runs → caller receives a "busy" error (API maps to 409).
- The runner takes an optional `then` callback invoked after the snapshot completes (used for
  launch sequencing). Tests inject a synchronous runner.

## 4. Upgrade integration

- `/api/launch` gating (pre-flight + per-check override) is unchanged. It additionally returns
  **409** `{"error": "report snapshot in progress"}` if a snapshot job is running.
- In `httpd`, after a 202 for `mode == "upgrade"`: instead of calling `launcher.launch` directly,
  start a *before* snapshot job whose `then` callback performs the existing launch (effective
  config + `launcher.launch`) and starts the after-watcher. The launch response body gains
  `"snapshot": "<before id>"`. Launch spawn errors inside the callback are audited as
  `launch_error` (response has already been sent).
- `stage` launches keep today's direct path.
- **After-watcher** thread: every 30 s reads the upgrade status (`progress.read_progress(host,
  "upgrade", ...)` + `launcher.upgrader_running`). It only trusts `done`/`died` **after it has seen
  `running` at least once** (the state file may still hold a terminal state from an earlier run).
  - seen running, then `done` → *after* snapshot with `pair_of = before id`;
  - seen running, then `died` → same, `note: "upgrade died before completion"`;
  - never seen running within a 5-minute grace → same, `note: "upgrade not observed running (status: X)"`.
  If a manual job is running at that moment, retry every 30 s until it can start.
- The runner stays busy *through* `then` (which launches nf_upgrade), so `/api/launch` and
  manual runs can't race the launch; busy clears only once `then` returns. `then` must not call
  `start()` itself: the after snapshot is started later by a separate watcher thread that `then`
  spawns, retrying on busy.
- While nf_upgrader is running, `POST /api/reports/run` → 409 `{"error": "upgrade in progress"}`
  and `POST /api/launch` → 409 `{"error": "upgrade already running"}` (checked right after the
  snapshot-busy check). The watcher's own after snapshot calls the runner directly, unaffected.
  The UI treats only a 409 carrying `not_overridden` as a pre-flight block; other 409s just show
  "Not launched: <error>".
- Interrupted before: if the service dies mid-*before*, `then` never runs and nothing is launched.
  `GET /api/reports/job` returns `"notice": "Before snapshot <id> was interrupted — the upgrade
  was not launched."` when no job runs, the newest snapshot is an `incomplete` *before* with no
  *after* naming it, and nf_upgrader is not running (else `null`); the UI shows it in
  `#launch-msg` and `#reports-job`.
- Audit: `report_snapshot` records `{id, label, client|"auto"}` at start.

## 5. API (all require a session)

| Method/Path | Result |
|---|---|
| `GET /api/reports` | `{"snapshots": [manifest summaries, newest first]}` (summary = manifest minus per-report command) |
| `POST /api/reports/run` | 202 `{"id"}`; 409 if a job runs or nf_upgrader is running; label `manual` |
| `GET /api/reports/job` | `{"job": {...}|null, "launch_error": str|null, "notice": str|null}` |
| `GET /api/reports/compare?a=&b=` | `{"a", "b", "reports": [{name, status, added, removed}]}` |
| `GET /api/reports/diff?a=&b=&report=&style=side\|unified` | `{"rows": [...], "truncated": bool}` |
| `GET /api/reports/raw?id=&report=` | `text/plain` report file |

- Compare `status` per report name (union of both manifests, order of A then new in B):
  `unchanged` | `changed` (with `added`/`removed` line counts) | `failed` (rc≠0 or error on either
  side; still diffable) | `missing` (absent on one side).
- Diff rows:
  - unified: `{"op": " |+|-|@", "text"}` from `difflib.unified_diff(n=3)`;
  - side: `{"op": "equal|replace|insert|delete", "left", "right"}` from `SequenceMatcher` opcodes
    (equal runs collapsed to 3 lines of context each side, a `{"op": "skip", "count"}` row between).
  - Capped at 2000 rows → `truncated: true`.
- Ids/report names are validated against existing snapshot dirs / manifest entries (no path
  traversal); unknown → 404.

## 6. UI — new **Reports** tab

- Tab button + panel `tab-reports`.
- Top: `#btn-reports-run` ("Run reports now"), `#reports-job` progress line, `#reports-list`
  table (label, started, status, note).
- Compare: `#cmp-a`, `#cmp-b` selects (default: the newest `before` and the `after` whose `pair_of`
  names it; else that `before` and the newest snapshot since it; else the two newest — only ids
  present in the list), `#cmp-style` (side/unified), `#cmp-table` rows with badge + counts; clicking a row
  loads its diff into an expandable row, with links to raw A/B.
- Upgrade tab: `#launch-msg` shows "Taking before snapshot N/21 — <report>…" while the before job
  runs (poll `/api/reports/job` every 3 s while a job is active).
- ES5 only, no external resources; HTML-escape all report text in rendered diffs (report output
  is untrusted text).

## 7. Testing

- `tests/test_reports.py`: snapshot writing (files, manifest, stderr marker, timeout/error
  entries) via `FakeHost`; `incomplete` detection; pruning to 20; compare classification; unified
  and side diff rows incl. collapse and truncation; id/report validation.
- `tests/test_api.py`: reports endpoints (list, run → 202 then 409 while busy, job, compare, diff,
  raw, 404s); launch 409 while a snapshot runs.
- `tests/test_httpd.py` (or a new unit around the sequencing helper): upgrade launch runs the
  before snapshot then launch, in order, with a synchronous runner; stage launches directly;
  watcher starts the after snapshot on `done` and on `died` after grace (driven with injected
  clock/progress functions).
- `tests/test_web_assets.py`: new IDs; `reports` tab/panel pair.

## Out of scope

Deleting snapshots from the UI; editing the report list in the UI; per-host (site-wide) reports;
resuming an after-watcher across service restarts (manual "Run reports now" covers it).
