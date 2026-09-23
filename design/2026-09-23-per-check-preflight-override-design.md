# Per-check pre-flight override — design

Date: 2026-09-23
Repo: nfupgrader
Parent design: `~/WorkNotes/design/2026-09-15-web-upgrade-tool-design.md`

## Problem

Today a single "Override blocking checks" checkbox plus one reason overrides
*every* failed pre-flight ERROR at once (`api.py` `/api/launch`,
`web/index.html` `#override-box`, `web/app.js` `launch()`). The user never
explicitly acknowledges each failure.

## Goal

Each failed pre-flight ERROR check must be individually acknowledged/overridden
before Launch. One shared reason covers all overridden checks. Launching with
overridden (unresolved) failures shows a prominent warning.

## Definitions

- **Blocking failure** — a pre-flight check with `fail_text: ERROR` that did not
  pass (existing `checks.blocking_failures`).
- **Unresolved** — a blocking failure that the user overrode rather than fixed.
- WARNING failures stay advisory; they need no per-check action.

## UI (Upgrade tab)

- `renderChecks` adds a per-row **Override** checkbox on each failed ERROR row
  of the pre-flight table (not on the post-flight table). Each checkbox carries
  the check name.
- Remove the global `#override` checkbox. Keep the shared `#reason` text field in
  `#override-box`, shown only when there is at least one blocking failure.
- **Launch** button enablement (recomputed on any checkbox/reason change and
  after each pre-flight run):
  - no blocking failures → enabled;
  - otherwise enabled only when every blocking failure is ticked **and**
    reason is non-blank.
  - A status line explains a disabled state: "N of M blocking checks not
    overridden" or "reason required to override".
- **Launch confirmation:**
  - No unresolved and no failed WARNING checks → existing confirm text unchanged.
  - Otherwise the confirm text begins with a warning block:
    "WARNING: launching with unresolved pre-flight failures." followed by the
    overridden checks (`name — detail`), then (informational) failed WARNING
    checks, then "Upgrading with unresolved pre-flight failures may leave the
    system in an unsupported state.", then the existing
    "cannot be paused" line. Uses `window.confirm` (no new dialog framework).
- On 409 the message names the checks the server says were not overridden, and
  the pre-flight table is re-run so the new failure is visible.

## API — `POST /api/launch`

Payload: `{mode, overrides: [check names], reason}`. The legacy
`override: bool` field is removed (the bundled UI is the only client).

Server logic (checks are re-run server-side as today):

1. `blocking = blocking_failures(results)`
2. `missing = [b for b in blocking if b.name not in overrides]`
3. `missing` non-empty → **409** `{error: "pre-flight checks failed",
   blocking: <full blocking list>, not_overridden: <missing names>}`.
4. `blocking` non-empty and reason blank → **400** `{error: "override requires a reason"}`.
5. Override names whose check now passes (or doesn't exist) are ignored.
6. `overrides` must be a list if present; anything else → **400**.

## Audit

When `blocking` is non-empty: one `override` record
`{mode, client, reason, checks: [names of blocking failures, all overridden]}` —
same shape as today, followed by the existing `launch` record.

## Testing

`tests/test_api.py` (replace the two existing override tests):
- partial override → 409, `not_overridden` lists the missing names;
- full override, blank reason → 400;
- full override + reason → 202; audit `override.checks` equals the blocking names;
- stale override name (check now passing) is ignored and does not appear in audit;
- no blocking failures, no overrides → 202, no `override` audit record;
- non-list `overrides` → 400.

`tests/test_web_assets.py`: drop `override` from the required element IDs.

`checks.py` is unchanged.

## Out of scope

Per-check reasons; acknowledgement of WARNING failures; post-flight changes.
