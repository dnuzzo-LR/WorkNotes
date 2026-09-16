# Web Upgrade Tool — Design

**Date:** 2026-09-15
**Status:** Design agreed through architecture and the `inc_upgrader` contract. No open inputs. Not yet planned or implemented.
**Repository:** `nfupgrader`
**Author:** Dan Nuzzo, with Claude

**Revision 2026-09-16:** After review with customer support, scope narrowed to **upgrade the local host only**. The tool no longer coordinates or drives upgrades on other hosts. It still *displays* read-only status for every host in the site. This removed the coordinator, the remote gated process, the go-token-over-ssh transport, and the host-ordering question. ssh survives solely as a read-only channel for peer status. Sections below reflect this; the coordinator design is preserved only in the git history of this file.

---

## Problem

Upgrades are performed today with `3b2/shell/inc_upgrade` and `3b2/shell/inc_upgrader`. They work, but they are not something we want to hand to customers: a hand-written config file, a `ksh` invocation, a backgrounded process, and a log file to tail. The customer has asked to perform upgrades themselves.

We want a web layer over those scripts that lets the customer step through the upgrade of **the host they are logged into** one state at a time, runs system checks between states, and shows the current state of the installation — version, GR, multibox, and the read-only status of every host in the site.

## Goals

- Step through every upgrade state individually **on the local host**, so a check can be attached to each one.
- Reuse the existing shell scripts as the engine. They are battle-tested; the web layer wraps them, it does not replace them.
- Borrow the system-checking machinery from `nf-install`.
- Show current installation facts: versions, GR status, multibox topology, and per-host status for the whole site — read-only for peers.

## Non-Goals

- **Driving upgrades on any host other than the local one.** The customer runs the tool on each host they want to upgrade. Support asked for this narrowing explicitly.
- Rewriting the upgrade logic in Python.
- Replacing `nf-install`. This is a separate tool with a separate purpose.
- Rollback. The existing scripts do not offer it and this project does not add it.

---

## What the existing scripts actually do

Findings that shaped the design. Line numbers are against `3b2/shell/inc_upgrader` at commit `776f42242`.

### The state machine runs to completion

Both phases are a `while true` loop that falls through every state without stopping:

```
STAGE    (inc_upgrade -s):
  swsstart → swspatchinstall → swswebinstall → swscopycnc → swsretropass1 → swscomplete

UPGRADE  (inc_upgrade -u):
  swscomplete → swistart → swicopycnc → swipackages → swipreboot
              → swiretropass2 → swibootinc → postinstall → swidone
```

`${INCLOGDIR}/.${HOSTNAME}_STATE` is written after each state (`write_state`, `:136`), but only as a crash-resume point — nothing pauses. Per-state stepping therefore requires a change to `inc_upgrader`.

Both loops end identically with `esac; write_state $STATE; done` at `:1106` and `:1468`. A single gate inserted there covers all fifteen states.

### State arms are not one-to-one with state names

The `swsstart` arm (`:1028-1042`) runs `doswsstart`, `calc_stage_space` *and* `doswscoreupgrade` together — so the largest single action in staging, installing the CORE RPM, has no pause point in front of it.

### The `/usr/cnc` symlink is swapped mid-upgrade

`doswipreboot` (`:1271-1297`) removes and recreates `/usr/cnc`, `/usr/cnc_stage` and `/usr/cnc_saved`. Consequences:

- The netFLEX web GUI lives at `/usr/cnc/web` (`gui/web/web_install:987`). It is swapped out from under itself mid-upgrade and is unavailable while the app is down. **The upgrade UI cannot live in the netFLEX web GUI.**
- `RETROROOT` is computed once at `:40` from the live `/usr/cnc` symlink. Any design that re-enters the script after `swipreboot` recomputes it against the *new* load. It happens to be unused past that point today, but that is an accident of the current code, not a contract.

### Nothing reboots the box

No `reboot`, `shutdown` or `init 6` anywhere in the script. `swibootinc` boots the *application*. `stop_inc` (`:484`) calls `new_boot_nms remote_shutdown`. `linux_packages` does not restart services — its `httpd` lines are yum installs and its `systemctl` lines are commented out (`linux_packages:110,124-125,150-151,352`). `/opt` is untouched throughout.

**Therefore a service installed under `/opt` survives the upgrade of its own host,** including the symlink flip and the application outage. This is what lets the tool run *on the very host it is upgrading* and keep serving its UI across the whole upgrade.

### Other relevant behaviour

- `confirm_setup` blocks on an interactive `read` unless `PROMPTFORVALIDATE=NO`. The tool must always set it.
- `inc_upgrade` already `nohup`s `inc_upgrader` and returns immediately — the detached-launch pattern the tool uses locally is the script's own.
- `check_gr_inprogress` (`:1476`) blocks *inside* the script for up to `MAX_ELAPSED_GR_WAIT_TIME` (default 4 hours) while `/usr/cnc/.GR_STATUS` reads `IN_PROGRESS` or `grestore.pid` exists.
- `MACHTYPE=BEP` short-circuits DB export (`:410`) and skips retrofits. BEPs take the short path through the machine.
- `inc_upgrader` parses `getopts "c:suhnL"` (`:1558`); `inc_upgrade` parses `getopts "c:sunmhL"`. `-S` is free in both.

---

## Decisions

| # | Decision | Rationale |
|---|---|---|
| 1 | True per-state stepping, gated inside `inc_upgrader` | Checks can attach to every state |
| 2 | One resident gated process, not re-invocation per state | Preserves today's execution semantics exactly; no `RETROROOT` re-entry hazard; preamble runs once |
| 3 | New repository `nfupgrader`, Python 3.6.8, stdlib only | No runtime dependencies on a customer box |
| 4 | Vendor the check and multibox logic from `nf-install` | `InternalChecks` and `multibox_config.py` are the expensive parts to rebuild |
| 5 | **Local-host upgrade only; the tool runs on each host in turn** | Support asked to narrow scope; removes the coordinator, remote launch, and cross-host failure handling |
| 6 | **Peer hosts are read-only** — status displayed, never driven | Keeps the "see all the hosts" requirement without the risk and complexity of remote control |
| 7 | ssh used only to *read* peer status; nothing installed on peers | Read-only ssh is best-effort and low-risk; a peer that is unreachable simply shows unknown |
| 8 | Checks defined as JSON for common kinds, code for the rest | Mirrors what `nf-install` already does |
| 9 | ERROR blocks the Next button, with a logged override | A customer stuck at 2am on a slightly wrong check is worse than a recorded override |
| 10 | Token auth, single-use, exchanged for a session cookie | No account management; access scoped to whoever had a root shell |
| 11 | Plain HTTP bound to loopback; customer tunnels over ssh | Risk accepted by Dan; nothing is exposed and the token never crosses a network |
| 12 | Dashboard shows always-true facts, plus app-dependent detail only when the app is up | Most information sources live under the symlink that moves |
| 13 | Config builder *and* import, both ending at a review screen | Hand-writing the `.cfg` is a real part of the unfriendliness |
| 14 | Split the `swsstart` arm into `swsstart` + `swscoreupgrade` | Puts a gate in front of the CORE RPM install; backward-compatible for free |

### Note on decision 11

Loopback binding means the customer needs an ssh tunnel to reach a tool whose purpose is to be friendly. That tension is accepted for now and flagged for revisit. The bind address is a config value, not a constant, so exposing it later is a configuration change rather than a code change.

### Note on decision 10

Two consequences, accepted knowingly:

- The token is the entire credential. Mitigated by single use, redemption into a session cookie with an immediate redirect to a clean URL, expiry if unredeemed, and session binding to the client IP — and substantially defused by loopback binding.
- There is no username for the audit trail. Overrides are attributable only to the OS user who started the service and the client IP. Accepted; the requirement Dan stated is a complete log, not per-user attribution.

---

## Architecture

```
Customer desk                THIS host (being upgraded)          peer hosts (display only)
─────────────                ──────────────────────────          ─────────────────────────
browser                      /opt/nfupgrader                      fep2 / bep1 / bep2
  │                            http.server on 127.0.0.1
  └── ssh -L tunnel ──────►    local progress + audit log         sshd
                               web/ static assets                      ▲
                                    │                                  │
                               gated inc_upgrader             read-only ssh, one-shot:
                                 (detached, LOCAL only)        rpm -qi / readlink /
                               state · gated · go · abort       cat .CNC_UP .GR_STATUS
                               files, all local                 (best effort; unknown if
                                                                 unreachable)
```

One Python service per host, installed under `/opt/nfupgrader` so the symlink flip cannot touch it. It upgrades **only the host it runs on**; the customer starts it again on the next host they want to upgrade.

The gate control files are all local — there is no remote launch and no go-token over ssh. ssh is used only to *read* peer status for the dashboard: a handful of one-shot commands (`rpm -qi`, `readlink`, `cat` of marker files), best-effort, with an unreachable peer shown as unknown. Peer status collection never blocks the local upgrade and never writes anything on a peer.

### Modules

Each is one file with one purpose.

| Module | Responsibility |
|---|---|
| `host.py` | **The one seam.** `Host.run(argv)` executes locally, or over ssh for read-only peer collection, and returns exit status, stdout, stderr. Every collector, check and action goes through it. Local upgrade actions only ever use the local `Host`; ssh is reserved for peer *reads*. |
| `site.py` | Parses `/usr/cnc/features/cnc.cnfg` into a FEP/BEP inventory with mates, and identifies which entry is the local host. Vendored from `nf-install/modules/multibox_config.py`. |
| `steps.py` | The state machine as data: state name, phase, human description, bound checks. |
| `progress.py` | Local upgrade progress, re-derived from the state/gated/pid files on every read. No site-wide plan — there is nothing to coordinate. |
| `checks.py` | JSON-defined checks plus coded ones. Vendored from `nf-install/modules/system_checks.py`, curses stripped. |
| `collect.py` | Tier-1 and tier-2 fact gathering. |
| `upgrade_cfg.py` | Build, import, validate and render the `.cfg`. |
| `audit.py` | Append-only action log. |
| `httpd.py` / `api.py` | stdlib HTTP server, token exchange, JSON endpoints. |
| `web/` | Static HTML, CSS and vanilla JS. No framework, no CDN, no build step — the box may have no internet. |

`host.py` is still the load-bearing decision even though control is local-only: it isolates the read-only ssh used for peer status, and a fake `Host` makes the entire system — local upgrade plus peer dashboard — testable without a real box, which matters for something that cannot be casually run.

---

## The `inc_upgrader` contract

### The gate

A new flag `-S` enables step mode. It is off by default, so every existing caller is unaffected. One new function:

```ksh
function step_gate
{
	[ "${STEPMODE}" != "YES" ] && return
	printf "STEP_GATE: parked before ${STATE} (`date`)\n"
	printf "${STATE} $$ `date +%s`\n" > ${GATEDFILE}
	while [ ! -f "${GOFILE}" -a ! -f "${ABORTFILE}" ]
	do
		sleep 2
	done
	rm -f ${GATEDFILE}
	if [ -f "${ABORTFILE}" ]
	then
		rm -f ${ABORTFILE}
		printf "STEP_GATE: abort requested at ${STATE}\n"
		exit_upgrade
	fi
	rm -f ${GOFILE}
}
```

Called immediately after `write_state $STATE` at `:1106` and `:1468`. One function, two call lines.

The semantics fall out of the existing code: the case arm advances `STATE` to the *next* state before `write_state` runs, so the gate parks with the state file already naming what comes next. "Finished the last one, waiting before the next one" — exactly the resume point the script already understands.

`-S` must also be accepted by `inc_upgrade`, which passes `$*` through.

### Control files in `${INCLOGDIR}`

All four files are local to the host being upgraded.

| File | Written by | Meaning |
|---|---|---|
| `.${HOST}_STATE` | script (exists today) | Next state to run — authoritative |
| `.${HOST}_GATED` | script (new) | Parked. Contains state, pid, epoch timestamp |
| `.${HOST}_GO` | the service (new) | Advance one state |
| `.${HOST}_ABORT` | the service (new) | Stop cleanly at the gate |

The service distinguishes three conditions with `kill -0` on the local pid:

- **parked** — `GATED` present, pid alive
- **running** — no `GATED`, pid alive
- **dead** — pid gone

No new crash-recovery mechanism is needed. A dead process leaves the state file exactly as the existing resume path expects. The three new files exist only in step mode.

### The `swsstart` split

The `swsstart` arm becomes two arms:

- `swsstart` — `doswsstart` + `calc_stage_space`, then `STATE=swscoreupgrade`
- `swscoreupgrade` — `doswscoreupgrade`, then `STATE=swspatchinstall`

Backward-compatible without special handling: an old state file can only ever contain `swsstart`, which still resumes correctly into the same sequence of actions.

### Local progress, not a site plan

There is no site-wide plan file — the tool drives one host, so there is nothing to sequence. Local upgrade progress is **re-derived on every read** from the local state file, gated marker and pid liveness, and on every service restart. The service never displays its own memory of what the upgrade was doing; the on-disk files are authoritative. This is the same reconciliation discipline the coordinator design used, reduced to a single host.

---

## Checks engine

A check entry carries the `nf-install` shape — `name`, `description`, `fail_text` — plus a binding: which state, `before` or `after` it, optionally narrowed by phase or by `MACHTYPE` so BEP-only and FEP-only checks are expressible.

**JSON-expressible kinds:** rpm package, touchfile, system define, service status, disk space, kernel parameter, `limits.conf` entry (all already implemented in `nf-install`), plus file exists, symlink target, command exit status, command output match.

**Coded checks** handle what JSON cannot: staged-versus-installed generic comparison, retrofit log scanning for FAIL lines, `dbcheck -AV` error counts.

Every check runs through `Host.run` against the **local** host — checks gate the local upgrade, so they run where the upgrade runs. (Peer status is collected separately and read-only; it is display, not a gate.)

**Failure policy:** an ERROR disables the Next button. Overriding requires a typed confirmation and a reason. Both the override and the reason go to the audit log and to the host's trace file. A WARNING is displayed and ignorable.

---

## Information display

Collected for the **local host** in full, and for **peers** read-only over ssh (tier 1 only, best-effort).

**Tier 1 — always true.** `rpm -qi` on `netFLEX-CORE`, `netFLEX-CORE-PATCH`, `netFLEX-CORE-IPATCH`; the targets of `/usr/cnc`, `/usr/cnc_stage`, `/usr/cnc_saved`; the `cnc.cnfg` parse; `.CNC_UP`; `.GR_STATUS`; `grestore.pid`; `df`; the state file. None of it depends on the application running or on a binary that moves. These are the facts the tool reads from peers as well as locally.

**Tier 2 — app-dependent.** `incinfo`, `rdb version`, `rdb test`, `dbcheck -AV` error count, NE status from `/usr/cnc/ambin/up`. Fetched only when `.CNC_UP` exists; rendered as "unavailable" rather than stale when it does not. Local host only — not gathered from peers.

**Site view** shows every host: type and number, ssh reachability, app up/down, installed version, staged version, and (for the local host) current upgrade state. The local host is marked as the one this instance can act on; all others are read-only. A peer that is unreachable over ssh shows unknown. Plus a GR panel (transfer status, restore status, and elapsed time when blocked) and a local install panel (symlink targets, CORE/PATCH/IPATCH versions).

`nf-install`'s `reports.json` — NE dumps, database exports, per-filesystem checks — is deliberately **not** ported. It would let a customer run `inc_db --export` mid-upgrade. Possible later as a separate support-facing tab.

---

## Config builder

Two entry paths, one exit:

- **Build** — a form with per-field validation. The depot picker lists RPMs actually present on disk; generic and load are derived from the chosen RPM; the rest get sane defaults.
- **Import** — point at an existing `.cfg`. It is parsed, validated and displayed.

Both end at a review screen showing the final values before anything runs.

`PROMPTFORVALIDATE=NO` is always forced — `confirm_setup` otherwise blocks on an interactive `read`. On an imported file, the tool displays exactly what it is overriding rather than editing silently.

Fields handled: `INCLOGDIR`, `NEW_GENERIC`, `NEW_LOAD`, `DEPOTFILE`, `PATCHDEPOTFILE`, `EXTDEPOTFILE`, `ATTEXTPATCHDEPOTFILE`, `INSTALLWEBGUI`, `RESTARTINC`, `INIT_CARD_DB`, `UNLINKDIR`, `STAGENICEVAL`, `UPGRADENICEVAL`, `LINUXPACKAGES`, `REMOVEINCLOGS`, `CLEANUPPROCDIR`, `MAX_ELAPSED_GR_WAIT_TIME`, `CUSTOMER_UPGRADE`, `PROMPTFORVALIDATE`.

---

## Audit log

Append-only JSONL under `/opt/nfupgrader/var`, with a human-readable rendering available in the UI.

Recorded: every login and token redemption, every step launch with its full argv, every go-token drop, every check result, every override with its reason, every abort, every config written, and every read-only peer ssh command with target host and exit status.

Entries are also appended to the local host's existing trace file, so the record appears in whatever log bundle support already collects.

This is the requirement Dan stated as the condition for accepting the auth and transport risks: *"as long as there is a log file for the whole thing."*

---

## Error handling

| Condition | Behaviour |
|---|---|
| Peer unreachable over ssh | Shown as unknown in the site view. Never inferred. Never affects the local upgrade. |
| Local gated process dead | Offered as a resume — relaunched detached from the local state file. |
| GR transfer or restore in progress | Its own status: "blocked on GR transfer, 47 minutes elapsed", never a hang. `check_gr_inprogress` blocks inside the script for up to 4 hours. |
| Check fails with ERROR | Next disabled; override available with a typed reason. |
| Service crash or restart | Full reconciliation of local progress from the local state file, gated marker and pid liveness. |

---

## Testing

The fake `Host` is the whole strategy. Canned command output drives the state machine, the checks engine and the dashboard with no boxes involved — including canned peer output for the read-only site view.

- Fixture `cnc.cnfg` files for singlebox, multibox and GR layouts.
- The shell gate is tested standalone against a stub `inc_upgrader` before it goes near the real one.
- A real-box walk-through on a lab host closes each phase.

---

## Phasing

Each phase gets its own spec and plan. The narrowing to local-host-only collapsed the former multi-host coordination phase into a small read-only peer-collection step folded into the dashboard.

| Phase | Content |
|---|---|
| 1 | `-S` gate + `swsstart` split, driven by a CLI, local host. Nothing else is testable until stepping works. |
| 2 | `host.py` / `site.py` / `progress.py` + web UI, local host, live output, audit log. |
| 3 | Checks engine. |
| 4 | Dashboard: local tiers 1 and 2, plus read-only peer status over ssh, GR and multibox views. |
| 5 | Config builder. |

---

## Open Questions

None. The host-ordering question is moot under local-host-only scope — the customer chooses which host to run the tool on and in what order, host by host.

---

## References

- `3b2/shell/inc_upgrade`, `3b2/shell/inc_upgrader`, `3b2/shell/linux_packages`, `3b2/shell/post-install.sh` — netflex repo, commit `776f42242`
- `~/Git/nf-install` — `modules/system_checks.py`, `modules/multibox_config.py`, `modules/upgrade_manager.py`, `system-checks.json`, `reports.json`, `UPGRADE_INTEGRATION_README.md`
- `gui/web-terminal` — existing standalone web service in the netflex repo (Rust/axum). Its `PreAuthenticated` session model (`src/session.rs:33`) depends on the netFLEX web GUI and does not carry over.
