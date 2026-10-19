# nfupgrader — New Installation Mode — Design

**Date:** 2026-10-19
**Status:** Design agreed in conversation. Not yet planned or implemented.
**Repository:** `nfupgrader`
**Author:** Dan Nuzzo, with Claude
**Builds on:** `~/WorkNotes/design/2026-09-15-web-upgrade-tool-design.md`

---

## Why

The web upgrade tool went over well at the demo, and management asked that it handle
**new installations** too, so a customer can bring up netFLEX on a fresh box from the
same page they later use to upgrade it. Today new installs are done with `nf-install`
(`~/Git/nf-install`, the curses "MARK" tool) or by hand from `wiki/install.adoc`.

## What a new install means here

As Dan defined it: run the checks the upgrade runs now, make sure the app is **not**
installed, find the RPM — then install it, configure it for its role in the site, and
install the web GUI.

## Goals

- When netFLEX is not installed on the local host, the tool offers **Install** instead
  of Stage/Upgrade, using the same tabs, check machinery, progress record and log viewer.
- A hard, non-overridable refusal to install over an existing netFLEX.
- Operator picks the CORE RPM with the existing depot picker.
- Operator picks the box's role — **Singlebox, FEP or BEP** — and, for FEP/BEP, the
  mate host and which host is the master. The tool generates `cnc.cnfg` from that.
- Install the web GUI (default on for Singlebox/FEP, off for BEP).
- A visible **license-file stub**, so license setup can be added later without
  rearranging the UI or the install sequence.
- After a successful install the tool naturally flips back to upgrade mode.

## Non-goals

- **Disk setup.** No `pvcreate`/`vgcreate`/`lvcreate`/`mkfs`/fstab. `/usr4` is assumed
  to exist and be mounted ("not mostly used anyway"). If it's missing, a blocking check
  says so; the tool doesn't fix it.
- The rest of nf-install's ON list: EMS/NMS mode, INC queues, scmds, password modes,
  `del_chk_ties`, dbchg flag, Apache rewrite (`506apache.sh`). Out for now.
- Porting nf-install's other SYSTEM scripts (SELinux, EPEL, polkit, sshd, crond).
  The install runs the checks the upgrader already has.
- Installing on any host other than the local one (same rule as upgrade).
- Multibox link setup (`/etc/services` RDR ports, ssh-copy-id, link tests) — that's
  `multibox_config.py` territory in nf-install and stays out.
- Changing `nf_upgrade` / `nf_upgrader`. They must stay behavior-identical to `ref/`.
- **Starting netFLEX.** First start is interactive; the operator does it by hand after
  the install. The Overview showing the app DOWN after install is expected.

## Anti-goals

- Installing on top of an existing netFLEX, under any circumstances.
- Leaving behind anything that confuses the **first upgrade** after install (wrong
  load-directory name, a stray `.${HOST}_STATE`, a real directory where a symlink
  belongs).

---

## Constraints (unchanged from the repo)

- Shipped shell is **ksh88-safe** (`/usr/bin/ksh`): no `PIPESTATUS`, no `pipefail`, no
  arrays; wrap sub-commands with `run_logged`; use `nf_log.ksh` severities.
- Python **3.6.8 stdlib only**, `.format()`, `subprocess.run(..., stdout=PIPE,
  stderr=PIPE, universal_newlines=True)`.
- The tool runs as root; no `sudo` prefixes (nf-install's are dropped when porting).
  Commands nf-install runs as `cmm` use `su cmm -c` (or equivalent) instead.
- `test/nf_fork_verify.sh` must still print `VERIFY OK` — the forks are untouched.

---

## Decided design

### 1. Detecting "installed"

netFLEX counts as installed if **any** of:

- `rpm -q netFLEX-CORE` succeeds, or
- `/usr/cnc` exists (as a directory or a symlink).

(nf-install only checks `/usr/cnc/bin/dacs`; that's too weak for a hard stop.)

The API exposes this as a boolean (e.g. on `/api/info`). It drives the mode: installed →
today's upgrade UI; not installed → install UI.

**Exception — an unfinished install of ours.** If `${INCLOGDIR}/.${HOST}_INSTALL_STATE`
exists and is not `done`, the host is in **install mode** even though `/usr/cnc` and/or
the RPM are present: the install is either still running or died part-way (§6a). A
state file reading `done` while nothing is installed (e.g. the box was wiped) is stale;
it is archived (renamed with a timestamp) when a new install starts.

### 2. Overview tab

When not installed, the local host shows a clear "netFLEX is not installed on this host"
state with a pointer to Config, instead of a row of `-`/DOWN values. The site table is
empty (no `cnc.cnfg` yet); that's expected and shouldn't look like an error.

### 3. Config tab (install form)

Replaces the upgrade form when not installed. Mocked up and agreed:

| Field | Behaviour |
|---|---|
| **CORE RPM** | Existing depot picker (`netFLEX-CORE*.rpm` in the depot dirs). Supplies `DEPOTFILE`, `NEW_GENERIC`, `NEW_LOAD` exactly as today. |
| **Box type** | Dropdown: Singlebox / FEP / BEP. |
| **This host** | Read-only, from `hostname`. |
| **Mate host** | FEP/BEP only. **Type-or-pick box**: one text input; focusing it lists hosts from `/etc/hosts` (name + IP), typing filters the list, and any typed name is accepted. A name not in `/etc/hosts` gets a warning, not a block. Label reads "Mate (BEP) host" or "Mate (FEP) host". |
| **Master** | FEP/BEP only. Radio: this host / the mate. |
| **Install web GUI** | Checkbox. Default ticked for Singlebox and FEP, unticked for BEP; operator may override. |
| **License file** | Greyed-out item labelled "coming later". No input yet. |
| **cnc.cnfg preview** | Live preview of the file that will be written. |

"Use this config" saves it, as the upgrade form does today.

`/etc/hosts` parsing: skip comments/blank lines, skip loopback entries (`127.*`, `::1`),
offer every name/alias on each remaining line with its IP.

### 4. cnc.cnfg generation

Generated by the tool from the form — not copied from a template with baked-in names.
Format follows nf-install's `nf/singlebox.cnc.cnfg` / `nf/multibox.cnc.cnfg`:

Singlebox:
```
# SingleBox (MUST be MultiBox or Single)
# SystemPrompt=netFLEX
# SystemString=INTEGRATED NETWORK CONTROLLER
# Master Machine Name
<thishost>
# Mach Mach-# Mate Mate-# Mach-Type (CNC=1 Mux=0) Override-Link (CNC)
```

Multibox (FEP line first, BEP line second, regardless of which box we're on):
```
# MultiBox (MUST be MultiBox or Single)
# SystemPrompt=netFLEX
# SystemString=INTEGRATED NETWORK CONTROLLER
# Master Machine Name
<master>
# Mach Mach-# Mate Mate-# Mach-Type (CNC=1 Mux=0) Override-Link (CNC)
<fep> 1 <bep> 1 1 9
<bep> 1 <fep> 1 0 0
```

where `<fep>`/`<bep>` are this host and the mate according to the box type. The
generated file is the one `site.py` / `_machtype` will read after install, so the
existing parsers must round-trip it (a test should assert that).

### 5. Checks (Upgrade tab, labelled "Install" in this mode)

- The phase selector shows only **Install** when not installed.
- **Hard stop:** "netFLEX not already installed" (§1). Not overridable. If it fails,
  Launch is disabled with no override control.
- **Pre-flight:** the existing **SYSTEM**-category checks (lvm2, cronie, firewalld,
  `kernel.pid_max`, `kernel.pty.max`, cmm nproc soft/hard), with the existing fix
  buttons and per-check override-with-reason.
- **`/usr4` present and mounted** — blocking ERROR, no fix. (The existing `disk-usr4`
  5 GB-free check also applies.)
- **`/usr4` mode 775** — blocking ERROR with a `chmod 775 /usr4` fix, as nf-install's
  `013cncstage.sh` requires. *(Claude's call.)*
- **nproc checks must not block on a fresh box.** Today their reference value comes from
  `/usr/cnc*/features/linux-nproc.conf`, which doesn't exist yet. Give them a fallback
  expected value (nf-install uses 32768) when neither file is present.
- **Runtime package checks (NETFLEX and SUPPORT categories) also run pre-flight for
  install**, with their existing dnf/yum fix buttons. Dan's definition was "run the
  checks the upgrade runs now", and the CORE RPM's `Requires:` are commented out
  (`netflex depot/mkspecfile64:132`), so nothing else guarantees `ksh` (needed by
  `nf_install`, `nf_log.ksh` and the `cmm` login shell) or `httpd` (RPM `%post` writes
  `/etc/httpd/conf.d`; web GUI) exist before step 3. *(Claude's call.)*
- Checks that need the installed app (`gr-idle`, `no-grestore`, `dbcheck-*`,
  `staged-present`, touchfiles, `system_defines`, `post-app-up`) do **not** run for
  install. The touchfile/`system_define` checks are for modes that are non-goals here,
  so they are left out of install post-flight too rather than producing a page of
  expected warnings. *(Claude's call.)*
- Checks gain `phase: "install"` alongside `any`/`stage`/`upgrade`; filtering in
  `checks._applies` extends accordingly. Existing checks' behaviour for stage/upgrade is
  unchanged.

### 6. The install engine: new `nf_install` ksh script

A new script at the repo root beside the forks, sourcing `nf_log.ksh`, launched detached
by the launcher like `nf_upgrade`. It reads the same style of shell `.cfg` the tool
already renders (plus the new install fields).

**Its own state file:** `${INCLOGDIR}/.${HOSTNAME}_INSTALL_STATE`. It must **not** write
`.${HOSTNAME}_STATE`: `nf_upgrader` stage mode treats an existing `.${HOSTNAME}_STATE`
as a resume point (`nf_upgrader:1727`), so a leftover install state would derail the
first upgrade.

**Trace file:** `${INCLOGDIR}/install_inc_${HOSTNAME}.<pid>`, in the `nf_log.ksh` line
format, so `logtail.py` can follow it.

**Steps** (state names are the builder's; order is fixed):

1. **start** — re-verify "not installed" (§1) and `/usr4` mounted; refuse otherwise.
2. **prepare /usr/cnc** — create `/usr4/inc${NEW_GENERIC}${NEW_LOAD}` and point both
   `/usr/cnc` and `/usr/cnc_stage` symlinks at it. **Use the upgrader's naming**
   (`inc` + generic + load, e.g. `inc5.3.002`), not nf-install's `inc${BUILD}`
   (`inc53.002`), because `nf_upgrader` derives `LOADROOT` from the `/usr/cnc` link and
   names the next load `${LOADROOT}/inc${NEW_GENERIC}${NEW_LOAD}`.
3. **install RPM** — `dnf install -y ${DEPOTFILE}` (falls back to `yum` on RHEL 7 if
   `dnf` is absent).
4. **remove /usr/cnc_stage** link.
5. **cnc.cnfg** — write the generated file to `/usr/cnc/features/cnc.cnfg`.
6. **linux_packages** — `/usr/cnc/bin/linux_packages`.
7. **cmmperm** — `/usr/cnc/bin/cmmperm` as `cmm`.
8. **license-migrate** — `/usr/cnc/mbin/license-migrate` as `cmm`. Failure is a WARN,
   not fatal (as in nf-install).
9. **post-install** — `/usr/cnc/bin/post-install.sh`.
10. **database** — `/usr/cnc/bin/nf_upgrade_pg.sh`.
11. **web GUI** (if enabled) — the nf-install `530gui.sh` way, *not* the upgrade's
    `inc_linux_web_install` (that one migrates an existing web DB, which a fresh box
    doesn't have):
    - in `/usr/cnc/GUI_REL`, untar `LINUXWEB64.*.tar` and run `./lwebsetup`, answering
      `n`, `n` on stdin;
    - in `/usr/cnc/web/data`: copy `/usr/cnc/web/maps.default/*` to `/usr/cnc/web/maps`,
      run `cpjsmap.osm jsmap`, `cp -p def.sys_config.flx sys_config`,
      `cp -p def.system_defines.flx system_defines`;
    - write a basic `Interfaces` (`[<host>_i]`, `host=<host>`, `protocol=ssh`);
    - `chown cmm:cmm sys_config system_defines Interfaces`.
    Web GUI failure is a WARN; the core install still counts as done.
12. **license** — stub. Logs "license setup not yet implemented — skipped" at INFO.
13. **done**.

Each sub-command goes through `run_logged`. Any fatal failure logs FATAL, leaves the
state file at the failed step and exits non-zero.

### 6a. When an install dies part-way

Dan wants all three of these:

1. **Report it.** The Install tab shows the failed step, the FATAL line(s) from the
   trace file, and a plain message that support can clean up manually, naming the trace
   file path to send them.
2. **Resume.** A "Resume install" action relaunches `nf_install` with a resume flag; it
   reads `.${HOST}_INSTALL_STATE` and continues from the failed step, skipping the
   step-1 "not installed" guard (the guard is for fresh starts only). Each step must
   therefore be safe to re-run: e.g. step 2 accepts links already pointing at the
   intended load dir, step 3 accepts the same CORE version already installed, the web
   GUI step re-runs cleanly. Pre-flight checks re-run before resume (hard stop excluded).
3. **Reset.** A "Reset and start over" action, offered only for an unfinished install
   recorded in `.${HOST}_INSTALL_STATE`. It is destructive, so it gets its own confirm
   modal requiring a typed reason, and is audited like overrides. It:
   - `rpm -e netFLEX-CORE` if installed;
   - removes `/usr/cnc` and `/usr/cnc_stage` **only if they are symlinks** pointing at the
     load dir this install created;
   - removes that load dir (`/usr4/inc${NEW_GENERIC}${NEW_LOAD}`) — only that one, and
     only if the install state file names it as ours;
   - archives `.${HOST}_INSTALL_STATE` (timestamp rename) and keeps the trace files.
   Reset runs as a logged job (its own trace file, followed by the Log tab) and never
   touches anything it can't prove this install created; if something doesn't match, it
   stops and falls back to "report it". It is refused while `nf_install` is running.

`nf_install` records the load dir and CORE version it is installing (e.g. a second line
in the state file, or a sidecar file) so Resume and Reset know what "ours" is.

### 7. Launcher, progress, liveness

- Launch accepts mode `install` and runs `nf_install -c <cfg>` detached, through the
  same confirm modal.
- `steps.py` gains the install step list; `progress.py` reads
  `.${HOST}_INSTALL_STATE` in install mode and treats `done` as terminal.
- Liveness (`upgrader_running` and the guards that use it) must also detect a running
  `nf_install`, so launch/fix/report locks hold during an install.
- **No "before" report snapshot** for install (every report command is under
  `/usr/cnc`).
- Log tab follows the newest `install_inc_${HOST}.<pid>`.

### 8. After install

Post-flight for install re-runs the package checks. The UI **stays on the install
result** — progress record, any WARNs (web GUI, license-migrate) and post-flight
results — with an "Install complete" banner and a button to continue to the upgrade
view. Only after that (or a page reload) does it switch to upgrade mode. *(Claude's
call: the operator should see the outcome before it disappears.)* The `.${HOST}_INSTALL_STATE` file stays in `INCLOGDIR` as a record.

### 9. Config storage

New fields added to `cfg.FIELDS` for install: box type, mate host, master host,
install-web-GUI flag. Upgrade-only fields (`UNLINKDIR`, `INIT_CARD_DB`, `RESTARTINC`, GR
wait, patch/ext depots) are hidden in install mode. The saved `.cfg` stays a shell
`KEY="v"` file. (nf-install's JSON is not used.)

---

## Left to the builder

- Exact state names, API shapes and field/key names.
- Where the "installed" boolean lives in the API, and how the UI switches modes.
- Implementation of the type-or-pick box (an `<input list>` + `<datalist>` is fine if it
  behaves as described; a small custom dropdown is fine too).
- How `nf_install` gets the generated `cnc.cnfg` (written by Python into `INCLOGDIR` and
  copied by the script, or rendered by the script from cfg fields) — either works as long
  as the preview and the installed file are identical.
- Test layout, following the existing `tests/` and `test/` patterns.
