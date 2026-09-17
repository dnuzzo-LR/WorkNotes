# Web Upgrade Tool — Design

**Date:** 2026-09-15
**Status:** Design agreed through architecture and the `inc_upgrader` contract. No open inputs. Not yet planned or implemented.
**Repository:** `nfupgrader`
**Author:** Dan Nuzzo, with Claude

**Revision 2026-09-16 (a):** After review with customer support, scope narrowed to **upgrade the local host only**. The tool no longer coordinates or drives upgrades on other hosts. It still *displays* read-only status for every host in the site. This removed the coordinator, the remote gated process, the go-token-over-ssh transport, and the host-ordering question. ssh survives solely as a read-only channel for peer status. Sections below reflect this; the coordinator design is preserved only in the git history of this file.

**Revision 2026-09-16 (b):** Two additions. (1) The scripts are **forked, not modified** — new `nf_upgrade` / `nf_upgrader` are created drop-in behavior-compatible with the originals, which are left untouched, so they can eventually replace them once proven. The `-S` gate, the `swsstart` split and the logging cleanup all land in the forks only. (2) A **logging cleanup** gives every emitted line a severity prefix (`INFO` / `WARN` / `ERROR` / `FATAL`) and tags sub-command output as `EXEC`, and the web UI gains a severity-filterable log viewer over the trace file.

**Revision 2026-09-16 (c):** Per-state **stepping is dropped**. The upgrade runs straight through, as the original scripts do; the customer launches a phase and watches it. The UI keeps an **on-screen progress record**: how many states the phase has, how many have completed, and the current state — read from the existing state file, no pausing. This removes the `-S` gate, the `GO`/`GATED`/`ABORT` control files, and the `swsstart` split (its only purpose was to gate the CORE install). The **forks' sole functional change is now the logging cleanup.** Because nothing stops mid-run, checks reshape from per-state gates into **pre-flight** checks (block the Launch button, override allowed) and **post-flight** checks (advisory). Sections below reflect all of this.

---

## Problem

Upgrades are performed today with `3b2/shell/inc_upgrade` and `3b2/shell/inc_upgrader`. They work, but they are not something we want to hand to customers: a hand-written config file, a `ksh` invocation, a backgrounded process, and a log file to tail. The customer has asked to perform upgrades themselves.

We want a web layer over those scripts that lets the customer step through the upgrade of **the host they are logged into** one state at a time, runs system checks between states, and shows the current state of the installation — version, GR, multibox, and the read-only status of every host in the site.

## Goals

- Launch an upgrade **on the local host** and keep an on-screen progress record: how many states the phase has, how many have completed, and the current state.
- Run pre-flight checks before launch and post-flight checks after completion.
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

`${INCLOGDIR}/.${HOSTNAME}_STATE` is written after each state (`write_state`, `:136`) as a crash-resume point. **This is exactly the signal the progress record consumes** — the file advances as the run proceeds, and the UI maps the current state to its position in the ordered list. No script change is needed to observe progress. (An earlier revision added a pause-gate here for per-state stepping; that stepping was later dropped, so the gate is gone and the run proceeds untouched.)

Both loops end identically with `esac; write_state $STATE; done` at `:1106` and `:1468` — the write-after-each-state that the progress record relies on.

### State arms are not one-to-one with state names

The `swsstart` arm (`:1028-1042`) runs `doswsstart`, `calc_stage_space` *and* `doswscoreupgrade` together — so the largest single action in staging, installing the CORE RPM, sits inside one state. The progress record therefore dwells on `swsstart` while the CORE RPM installs; finer granularity there is surfaced through the new log lines, not by splitting the state (the split was considered and dropped — see decision 14).

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
| 1 | **Launch-and-monitor, not stepping** — run the phase straight through, show a progress record (N states, completed, current) | Customer wanted visibility, not per-state control; keeps the fork minimal and the run identical to the original |
| 2 | Run the fork detached (as the original `nohup`s it); monitor the state file + trace, never pause | Preserves today's execution semantics exactly; no `RETROROOT` re-entry hazard; preamble runs once |
| 3 | New repository `nfupgrader`, Python 3.6.8, stdlib only | No runtime dependencies on a customer box |
| 4 | Vendor the check and multibox logic from `nf-install` | `InternalChecks` and `multibox_config.py` are the expensive parts to rebuild |
| 5 | **Local-host upgrade only; the tool runs on each host in turn** | Support asked to narrow scope; removes the coordinator, remote launch, and cross-host failure handling |
| 6 | **Peer hosts are read-only** — status displayed, never driven | Keeps the "see all the hosts" requirement without the risk and complexity of remote control |
| 7 | ssh used only to *read* peer status; nothing installed on peers | Read-only ssh is best-effort and low-risk; a peer that is unreachable simply shows unknown |
| 8 | Checks defined as JSON for common kinds, code for the rest | Mirrors what `nf-install` already does |
| 9 | Pre-flight ERROR blocks the Launch button, with a logged override; post-flight checks are advisory | Nothing stops mid-run, so checks gate the launch, not each state; a customer stuck at 2am on a slightly wrong check is worse than a recorded override |
| 10 | Token auth, single-use, exchanged for a session cookie | No account management; access scoped to whoever had a root shell |
| 11 | Plain HTTP bound to loopback; customer tunnels over ssh | Risk accepted by Dan; nothing is exposed and the token never crosses a network |
| 12 | Dashboard shows always-true facts, plus app-dependent detail only when the app is up | Most information sources live under the symlink that moves |
| 13 | Config builder *and* import, both ending at a review screen | Hand-writing the `.cfg` is a real part of the unfriendliness |
| 14 | ~~Split the `swsstart` arm~~ — **dropped** | Its only purpose was to gate the CORE install; with no stepping there is no gate, and dropping it keeps the fork strictly equivalent to the original |
| 15 | **Fork the scripts** — new `nf_upgrade` / `nf_upgrader`, originals untouched | The log cleanup touches ~305 emit sites in the battle-tested engine; too much to edit in place. Forks are drop-in compatible and can replace the originals once proven |
| 16 | **Logging cleanup** — severity prefix on every line, `EXEC` for sub-command output | Consistent, machine-parseable log the viewer can color and filter |
| 17 | Sub-command output wrapped via a status-preserving helper, not `cmd \| sed` | A pipe destroys `$?`, and `PIPESTATUS`/`pipefail` are ksh93-only; the helper is ksh88-safe |

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
  └── ssh -L tunnel ──────►    progress record + audit log        sshd
                               web/ static assets                      ▲
                                    │                                  │
                               nf_upgrader                    read-only ssh, one-shot:
                                 (detached, LOCAL only,        rpm -qi / readlink /
                                  runs straight through)       cat .CNC_UP .GR_STATUS
                               .${HOST}_STATE file  ◄──read     (best effort; unknown if
                               trace file           ◄──tail     unreachable)
```

One Python service per host, installed under `/opt/nfupgrader` so the symlink flip cannot touch it. It upgrades **only the host it runs on**; the customer starts it again on the next host they want to upgrade.

The service launches `nf_upgrader` detached — exactly as the original `nohup`s `inc_upgrader` — and then **watches**, never steers: it reads the existing `.${HOST}_STATE` file for the progress record and tails the trace file for live output. There are no `GO`/`GATED`/`ABORT` files; the upgrade runs to completion on its own. ssh is used only to *read* peer status for the dashboard: a handful of one-shot commands (`rpm -qi`, `readlink`, `cat` of marker files), best-effort, with an unreachable peer shown as unknown. Peer status collection never blocks the local upgrade and never writes anything on a peer.

### Modules

Each is one file with one purpose.

| Module | Responsibility |
|---|---|
| `host.py` | **The one seam.** `Host.run(argv)` executes locally, or over ssh for read-only peer collection, and returns exit status, stdout, stderr. Every collector, check and action goes through it. Local upgrade actions only ever use the local `Host`; ssh is reserved for peer *reads*. |
| `site.py` | Parses `/usr/cnc/features/cnc.cnfg` into a FEP/BEP inventory with mates, and identifies which entry is the local host. Vendored from `nf-install/modules/multibox_config.py`. |
| `steps.py` | The ordered state list per phase as data: state name, phase, human description. Drives the "N states / completed / current" progress record. |
| `progress.py` | Local upgrade progress, re-derived on every read from the state file, the trace tail and pid liveness: maps the current state to its index in `steps.py`'s ordered list to yield completed-count / total / current. No site-wide plan — there is nothing to coordinate. |
| `checks.py` | JSON-defined checks plus coded ones. Vendored from `nf-install/modules/system_checks.py`, curses stripped. |
| `collect.py` | Tier-1 and tier-2 fact gathering. |
| `upgrade_cfg.py` | Build, import, validate and render the `.cfg`. |
| `audit.py` | Append-only action log. |
| `httpd.py` / `api.py` | stdlib HTTP server, token exchange, JSON endpoints. |
| `web/` | Static HTML, CSS and vanilla JS. No framework, no CDN, no build step — the box may have no internet. |

`host.py` is still the load-bearing decision even though control is local-only: it isolates the read-only ssh used for peer status, and a fake `Host` makes the entire system — local upgrade plus peer dashboard — testable without a real box, which matters for something that cannot be casually run.

---

## The forked scripts (`nf_upgrade` / `nf_upgrader`)

`inc_upgrade` and `inc_upgrader` are **not modified**. New `nf_upgrade` and `nf_upgrader` are forked from them, carry the logging cleanup below, and are **drop-in behavior-compatible**: same flags, same config variables, same state machine, same state/control files, same exit codes. The **only** intended difference is the log line format — output formatting, not behavior. There is no gate, no new flag, no new control file, and no change to the state machine. The goal is that ops can replace the originals with the forks once they are proven equivalent.

Behavior-compatibility is a test target, not just an intention — see [Testing](#testing).

### The progress signal — no script change needed

The original already writes `.${HOST}_STATE` after every state via `write_state` (`:136`). That is the entire signal the UI needs. The service reads that file, looks the current state up in `steps.py`'s ordered list for the phase, and renders **completed-count / total / current state**. Nothing pauses; the file simply advances as the upgrade runs, and the progress record follows it.

Liveness is read the same way it always was — `kill -0` on the pid from the launch, plus the trace tail — to distinguish three conditions:

- **running** — pid alive, state file advancing
- **done** — pid exited, state file at `swscomplete` / `swidone`
- **died** — pid gone, state file short of the terminal state

A died process leaves the state file exactly as the existing resume path expects, so recovery is the original's own rerun path: the service offers to relaunch, and `nf_upgrader` resumes from the state file unchanged. This is the same reconciliation discipline the coordinator design used, reduced to a single host and to read-only observation.

---

## Logging cleanup and the log viewer

Today the scripts emit ~305 lines through a mix of `printf`, `print` and `echo`, with inconsistent severity: 66 sites carry the word `ERROR` or `WARNING`, the rest carry nothing. The forks give every emitted line a severity prefix and a timestamp, and tag sub-command output distinctly, so the trace file is machine-parseable and the web UI can color and filter it.

### Line format

```
2026-09-16 14:03:11 INFO  Exporting GSHM data to /usr/cnc_stage/export
2026-09-16 14:03:12 EXEC  [rpm] netFLEX-CORE-5.4.0-29 ...
2026-09-16 14:05:44 ERROR dbcheck -AV reported 3 errors
2026-09-16 14:05:44 FATAL Aborting upgrade
```

Five line-classes in a single fixed column: `INFO`, `WARN`, `ERROR`, `FATAL`, and `EXEC`. The first four are severities of the script's own output; `EXEC` means "raw output from a sub-process," which the viewer can dim, indent or collapse under its step.

### How the script's own output is converted

Four helpers defined near the top of each fork:

```ksh
log()      { printf '%s %-5s %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$1" "$2"; }
loginfo()  { log INFO  "$*"; }
logwarn()  { log WARN  "$*"; }
logerror() { log ERROR "$*"; }
logfatal() { log FATAL "$*"; }
```

Emit sites are converted: the 66 `ERROR`/`WARNING` sites become `logerror` / `logwarn`, everything else becomes `loginfo`. `fail_upgrade` and `exit_upgrade` emit a `FATAL` line — and because many current `ERROR:`-then-`fail_upgrade` sequences are in fact fatal, the pass fixes their severity along the way.

**Two exclusions — these sites are left raw:**

1. **Control-file writes** — `${STATEFILE}` (`:138`), `${WEBSTATEFILE}` (`:143`), `.OLD_GENERIC` (`:862`), `.OLD_LOAD` (`:863`), `.LPRESULT` (`:1139`). These are data files the script itself reads back with `cat`. A prefix here would corrupt them and break resume. This is the trap that rules out a blind global substitution.
2. **Banner and usage text** — cosmetic; left as printed.

### How sub-command output is tagged

Sub-commands whose output currently flows to the trace file are wrapped so each of their lines comes out as `EXEC [tag]`. The wrap must **not** use `cmd | sed` — a pipe makes `$?` reflect `sed`, not the command, and many call sites check `$?` immediately after. `PIPESTATUS` and `set -o pipefail` would fix that but are ksh93-only; the forks are `#!/usr/bin/ksh` and must stay ksh88-safe. So a status-preserving helper:

```ksh
run_logged()   # run_logged <tag> cmd args...
{
	typeset tag="$1"; shift
	typeset tmp="${INCLOGDIR}/.run.$$"
	"$@" > "${tmp}" 2>&1
	typeset rc=$?
	sed "s/^/$(date '+%Y-%m-%d %H:%M:%S') EXEC  [${tag}] /" "${tmp}"
	rm -f "${tmp}"
	[ ${rc} -ne 0 ] && logwarn "[${tag}] exited ${rc}"
	return ${rc}
}
```

Call sites become `run_logged rpm rpm -qi netFLEX-CORE; if [ $? -ne 0 ] ...` — the child's real exit status is preserved, and a non-zero exit is noted without the caller having to. A non-zero `rc` is surfaced as a `WARN`; whether it is actually fatal remains the caller's existing decision.

**Not wrapped:** sub-commands whose output is captured into a variable (`X=$(cmd)`) or redirected to a control file — wrapping would corrupt the captured value or the data file. Those keep their current form.

With script output prefixed and sub-command output tagged, every line in the trace file is classified. Any unmatched line — for example output from an old-format log or a stray write — is shown by the viewer as `INFO`.

### The viewer

Folds into the phase-2 live-output feature. Same trace file, plus: severity filter (e.g. show `WARN` and above), per-class color, `EXEC` lines dimmable or collapsible, and tail-follow for the running step. The file is served read-only.

---

## Checks engine

Because the upgrade no longer stops between states, checks are not bound to individual states. They run at the two moments the tool *does* control — before a phase launches and after it finishes:

- **Pre-flight** — run before the Launch button acts, gating the launch. This is where disk space, staged-versus-installed generic, dbcheck cleanliness, GR-idle and the rest belong.
- **Post-flight** — run after the phase reaches its terminal state, purely to report. Advisory; they annotate the completed run, they cannot un-launch it.

A check entry carries the `nf-install` shape — `name`, `description`, `fail_text` — plus a binding of `preflight` or `postflight`, a phase (`stage` / `upgrade`), and an optional `MACHTYPE` narrowing so BEP-only and FEP-only checks are expressible.

**JSON-expressible kinds:** rpm package, touchfile, system define, service status, disk space, kernel parameter, `limits.conf` entry (all already implemented in `nf-install`), plus file exists, symlink target, command exit status, command output match.

**Coded checks** handle what JSON cannot: staged-versus-installed generic comparison, retrofit log scanning for FAIL lines, `dbcheck -AV` error counts.

Every check runs through `Host.run` against the **local** host — checks gate the local launch, so they run where the upgrade runs. (Peer status is collected separately and read-only; it is display, not a gate.)

**Failure policy:** a pre-flight ERROR disables the Launch button. Overriding requires a typed confirmation and a reason. Both the override and the reason go to the audit log and to the host's trace file. A WARNING is displayed and ignorable. Post-flight results are always advisory — shown, never blocking, since the phase has already run.

---

## Information display

Collected for the **local host** in full, and for **peers** read-only over ssh (tier 1 only, best-effort).

**Tier 1 — always true.** `rpm -qi` on `netFLEX-CORE`, `netFLEX-CORE-PATCH`, `netFLEX-CORE-IPATCH`; the targets of `/usr/cnc`, `/usr/cnc_stage`, `/usr/cnc_saved`; the `cnc.cnfg` parse; `.CNC_UP`; `.GR_STATUS`; `grestore.pid`; `df`; the state file. None of it depends on the application running or on a binary that moves. These are the facts the tool reads from peers as well as locally.

**Tier 2 — app-dependent.** `incinfo`, `rdb version`, `rdb test`, `dbcheck -AV` error count, NE status from `/usr/cnc/ambin/up`, and multibox link status from `/usr/cnc/mbin/nestat -B`. Fetched only when `.CNC_UP` exists; rendered as "unavailable" rather than stale when it does not. Local host only — not gathered from peers.

### Per-host information displayed

The site view shows one row or card per host. The **local host** shows every field below; a **peer** shows the tier-1 fields (identity, reachability, install, GR) and omits the tier-2 and upgrade-progress fields, which require the app or a running upgrade the tool is not observing on that host. A field that cannot be read — peer unreachable, or app down for a tier-2 field — renders as `unknown` / `unavailable`, never as a stale or guessed value.

| Field | Source | Local | Peer | Needs app up |
|---|---|:---:|:---:|:---:|
| Hostname | `cnc.cnfg` / `hostname` | ✅ | ✅ | — |
| Role + number (FEP *n* / BEP *n*) | `cnc.cnfg` machine record (`type` 1=FEP, 0=BEP) | ✅ | ✅ | — |
| Mate host | `cnc.cnfg` mate fields | ✅ | ✅ | — |
| This-host marker (the one this instance can upgrade) | local identity vs `cnc.cnfg` | ✅ | — | — |
| ssh reachability | ssh probe result | — | ✅ | — |
| Installed CORE version | `rpm -qi netFLEX-CORE` | ✅ | ✅ | — |
| PATCH / IPATCH version | `rpm -qi netFLEX-CORE-PATCH` / `-IPATCH` | ✅ | ✅ | — |
| Installed load path | `readlink /usr/cnc` | ✅ | ✅ | — |
| Staged load path + version | `readlink /usr/cnc_stage` + `rpm -qi` there | ✅ | ✅ | — |
| Saved (rollback) load path | `readlink /usr/cnc_saved` | ✅ | ✅ | — |
| App up / down | `.CNC_UP` present | ✅ | ✅ | — |
| Machine type (NMS / …) | `mach_type` | ✅ | ✅ | — |
| GR transfer status | `.GR_STATUS` (`IN_PROGRESS` / `COMPLETED` / absent) | ✅ | ✅ | — |
| GR restore status | `grestore.pid` present | ✅ | ✅ | — |
| Filesystem headroom (`/usr4`, `/usr2`, `/tmp`, `/var`) | `df -k` | ✅ | ✅ | — |
| **Upgrade progress** (phase, current state, *completed / total*) | `.${HOST}_STATE` + `steps.py` + pid liveness | ✅ | — | — |
| **Run status** (running / done / died) | pid liveness + terminal state | ✅ | — | — |
| `incinfo` detail | `incinfo` | ✅ | — | ✅ |
| RDB version / test | `rdb version`, `rdb test` | ✅ | — | ✅ |
| `dbcheck -AV` error count | `dbcheck -AV` | ✅ | — | ✅ |
| NE status summary | `/usr/cnc/ambin/up` | ✅ | — | ✅ |
| Multibox link status (rdr/wtr per connected host) | `/usr/cnc/mbin/nestat -B` | ✅ | — | ✅ |

Peer rows are tier-1 by deliberate choice (decision 7): tier-2 fields would need either the app up on the peer or commands run under its `/usr/cnc`, which is more coupling and risk than a status display warrants. The upgrade-progress fields are local-only because this instance observes only its own upgrade.

Three panels sit alongside the per-host rows:

- **GR panel** — transfer status, restore status, and elapsed time when the local upgrade is blocked in `check_gr_inprogress`.
- **Local install panel** — the three symlink targets and the CORE / PATCH / IPATCH versions for the host being upgraded.
- **Multibox link panel** — from `nestat -B` on the local host: one line per connected host giving the WTR and RDR socket state (`UP` / `DOWN`), the port, the last state-change time, and the message count. This is the local host's own view of its links to the other boxes; it is a tier-2 read (app must be up) and is not gathered from peers. On a single-box system the panel is simply empty.

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

Recorded: every login and token redemption, every phase launch with its full argv, every pre-flight and post-flight check result, every override with its reason, every state transition observed, every config written, and every read-only peer ssh command with target host and exit status.

Entries are also appended to the local host's existing trace file, so the record appears in whatever log bundle support already collects.

This is the requirement Dan stated as the condition for accepting the auth and transport risks: *"as long as there is a log file for the whole thing."*

---

## Error handling

| Condition | Behaviour |
|---|---|
| Peer unreachable over ssh | Shown as unknown in the site view. Never inferred. Never affects the local upgrade. |
| Upgrade process died mid-run | Detected by pid gone + state file short of terminal. Offered as a resume — relaunched detached; `nf_upgrader` resumes from the state file, its own rerun path. |
| GR transfer or restore in progress | Its own status: "blocked on GR transfer, 47 minutes elapsed", never a hang. `check_gr_inprogress` blocks inside the script for up to 4 hours. |
| Pre-flight check fails with ERROR | Launch disabled; override available with a typed reason. |
| Service crash or restart | Progress record re-derived from the local state file, trace tail and pid liveness. The upgrade itself keeps running regardless — the service only observes it. |

---

## Testing

The fake `Host` is the whole strategy. Canned command output drives the progress record, the checks engine and the dashboard with no boxes involved — including canned peer output for the read-only site view. A stub `nf_upgrader` that just advances the state file drives the progress-record logic without a real upgrade.

- Fixture `cnc.cnfg` files for singlebox, multibox and GR layouts.
- A real-box walk-through on a lab host closes each phase.

**Fork equivalence is its own test target.** The whole point of forking is that `nf_upgrader` can replace `inc_upgrader`. Because the fork's *only* functional change is the log format, equivalence is clean to assert: run both against the same fixture inputs and diff the resulting state/control-file transitions and exit codes — they must be identical. The trace file is expected to differ (that is the log-format change) and is excluded from that diff; the log format is checked separately by asserting every non-excluded line matches the `TIMESTAMP SEVERITY …` grammar.

---

## Phasing

Each phase gets its own spec and plan. The narrowing to local-host-only collapsed the former multi-host coordination phase into a small read-only peer-collection step folded into the dashboard.

| Phase | Content |
|---|---|
| 1 | Fork `nf_upgrade` / `nf_upgrader` = originals + logging cleanup (`log*` helpers, `run_logged`, emit-site conversion). Prove action/exit-code equivalence to the originals; assert the log grammar. This is the only phase that touches the shell. |
| 2 | `host.py` / `site.py` / `steps.py` / `progress.py` + web UI: launch a phase, live output + severity-filtering log viewer, and the **progress record** (N states / completed / current). Audit log. |
| 3 | Checks engine: pre-flight (blocking + override) and post-flight (advisory). |
| 4 | Dashboard: local tiers 1 and 2, plus read-only peer status over ssh, GR and multibox views. |
| 5 | Config builder. |

Dropping the gate collapsed the former phases 1 and 2 into one: the fork's sole change is now the logging cleanup, so there is nothing behavioral to test separately from it.

**Phase 1 plan:** `~/WorkNotes/plans/2026-09-17-nfupgrader-phase1-fork-logging-plan.md`.

---

## Open Questions

None. The host-ordering question is moot under local-host-only scope — the customer chooses which host to run the tool on and in what order, host by host.

---

## References

- `3b2/shell/inc_upgrade`, `3b2/shell/inc_upgrader`, `3b2/shell/linux_packages`, `3b2/shell/post-install.sh` — netflex repo, commit `776f42242`. The originals; `nf_upgrade` / `nf_upgrader` will be forked from these two and live alongside them in `3b2/shell/`.
- `~/Git/nf-install` — `modules/system_checks.py`, `modules/multibox_config.py`, `modules/upgrade_manager.py`, `system-checks.json`, `reports.json`, `UPGRADE_INTEGRATION_README.md`
- `gui/web-terminal` — existing standalone web service in the netflex repo (Rust/axum). Its `PreAuthenticated` session model (`src/session.rs:33`) depends on the netFLEX web GUI and does not carry over.
