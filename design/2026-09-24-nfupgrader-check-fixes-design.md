# Pre-flight check fixes — design

Date: 2026-09-24
Repo: nfupgrader
Source feature: `~/Git/nf-install/modules/system_checks.py` — per-kind `fix_*` functions run from
the checks screen (background), then the check is re-tested.

## Goal

A failed pre-flight check whose kind has a known remedy gets a **Fix** button that runs that remedy
as root on the local host, then re-runs the check.

## Decisions (brainstorming)

- Scope: every kind nf-install can fix — rpm_installed, service_active, kernel_param, ulimit,
  touchfile. (system_define and the other kinds: no fix.) nf-install's touchfile "remove" is not
  ported (checks only require presence).
- Per-row Fix only; no batch "fix all". Each fix is individually confirmed.
- Pre-flight only; post-flight rows get no Fix.

## 1. Fix definitions (`nfupgrader/checks.py`)

`FIXES = {kind: (describe(params) -> str, apply(host, params) -> (ok, detail))}`. The service runs
as root, so nf-install's `sudo` is dropped.

| kind | describe / apply | timeout |
|---|---|---|
| rpm_installed | `dnf install -y <pkg>` | 600 s |
| service_active | `systemctl start <svc>` then `systemctl enable <svc>`; started-but-not-enabled → ok with "WARNING: …" detail | 30 s each |
| kernel_param | `sysctl -w <p>=<v>`, then replace/add `<p>=<v>` in `SYSCTL_CONF` (`/etc/sysctl.d/99-netflex.conf`); live-but-not-persisted → ok with warning detail | 10 s |
| ulimit | replace/add `<domain> <type> <item> <value>` in `LIMITS_CONF` (`/etc/security/limits.d/99-netflex.conf`) | n/a |
| touchfile | `/usr/cnc/bin/scmd touch /usr/cnc/features/<file>` | 30 s |

- Commands go through `host.run` (process-group kill on timeout). File edits are local Python
  writes (write temp + rename); `SYSCTL_CONF`/`LIMITS_CONF` are module constants that tests
  override.
- `detail` for a failed command = "rc=N: <last stderr/stdout line(s), ≤ 300 chars>".
- `run_checks` results gain `"fix"`: the describe() text when the kind is fixable **and the check
  failed**, else `null`.
- `find_check(check_list, name, when, phase, machtype)` returns the applicable definition or None.

## 2. API — `POST /api/checks/fix`

Body `{name, phase}` (phase defaults to the active phase). In order:
1. 409 `{"error": "upgrade in progress"}` if `launcher.upgrader_running`; 409
   `{"error": "report snapshot in progress"}` if the snapshot runner is busy.
2. 404 if `name` isn't an applicable pre-flight check for phase/machtype; 400 if its kind has no
   fix.
3. Re-run that check; if it now passes → 200 `{fix: null, result}` (nothing to do).
4. Non-blocking lock (module-level `threading.Lock`); if held → 409
   `{"error": "another fix is running"}`.
5. Audit `check_fix_start {name, kind, fix: describe, client}`; apply; audit
   `check_fix {name, kind, ok, detail, client}`; re-run the check.
6. 200 `{fix: {ok, detail}, result: <check result>}`. Unexpected exception inside apply → ok
   false with the exception text (still audited).

Synchronous: the request is held for the fix's duration (≤ ~10 min for dnf); the threaded server
keeps other requests responsive.

## 3. UI (Upgrade tab, pre-flight table)

- Failed rows with `r.fix` get a `Fix` button (`class='fix'`, `data-name`), in a Fix column shown
  only in the pre-flight table.
- Click → `window.confirm("Run as root on <host>:\n\n<fix>\n\nContinue?")` → all Fix buttons
  disabled, clicked one reads "Fixing…" → POST → `#fix-msg` shows "✅ <name>: <detail>" or
  "❌ <name>: <detail>" (or the error for 4xx) → `runPreflight()` so override/Launch state is
  recomputed.
- All text escaped via `esc()`.

## 4. Testing

- `tests/test_checks.py`: each kind's describe/apply with FakeHost (argv + timeout asserted via a
  recording host); service start ok/enable fail; kernel_param persist to temp SYSCTL_CONF
  (replace existing line, append new); ulimit replace/append in temp LIMITS_CONF; failed command
  detail; `fix` field present only on failed fixable checks; `find_check`.
- `tests/test_api.py`: fix success path (audit start + result, re-run shows pass), not failing →
  fix null, unknown name 404, unfixable kind 400, upgrade running 409, snapshot busy 409, lock
  held 409, requires session.
- `tests/test_web_assets.py`: `#fix-msg`, Fix button rendered from `r.fix` via esc, confirm text,
  runPreflight after fix.

## Out of scope

Batch fix; post-flight fixes; fixes on peer hosts; touchfile removal; editable fix commands in the
checks JSON.
