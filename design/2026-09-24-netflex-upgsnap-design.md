# upgsnap — netFLEX upgrade snapshot command — design

Date: 2026-09-24
Repo: netflex (`lightriversoftware/netflex`), branch off `main`; to be backported to `inc54.1` and
`inc54.0`.
Consumer: nfupgrader Reports tab (before/after per-report diff).

## Goal

Replace the 21 ad-hoc report commands nfupgrader runs before and after an upgrade with one
purpose-built, **read-only** netFLEX command whose output is **normalized for diffing**: stable
ordering, no volatile fields (message counters, timestamps, PIDs, df usage, alarm ids/times) and
no secrets (NE passwords). It captures state; it makes no pass/fail judgement.

## Decisions (brainstorming)

- Job: diffable snapshot only (no verdicts).
- Form: a new C command (nmake, gcc 4.8.5 baseline, Doxygen, `upgsnap_` prefix).
- Sections: System & versions, RDB, NEs & equipment, Databases (+ disk mounts/sizes in system).
- Output: `--section <name>` per section; nfupgrader lists one report per section.
- Base: `main`; backports to inc54.1 / inc54.0 — only APIs present on all three branches
  (verified: rdb_version_get_sys, rdb_current_version, rdb_version_compare,
  rdb_pgbouncer_enabled, rdb_has_data, rdb_connect_db, rdb_connect_credentials,
  first/next_es64_snmp_info_neId_slot, get_link_status, read_gr_data, atch_bep_socket_stat,
  generic_rprint, getopts, getcmdoutput, is_cnc_up).
- dbcheck audits (file-local in dbcheck.c) and inc_db exports are obtained by running the
  existing tools (`dbcheck -A`, `inc_db <db> --export`) rather than linking their code.

## Command

`/usr/cnc/bin/upgsnap [--list] [--section NAME] [-h]`

- no args: every section, each preceded by `== NAME ==` and followed by a blank line.
- `--list`: section names, one per line, in display order.
- `--section NAME`: that section only (unknown name → usage error).
- Output lines: `key: value`, sorted where the source order is not already stable and
  meaningful; values single-line (embedded newlines/tabs replaced by spaces).
- Exit: 0 success; 1 at least one section could not be fully gathered (it still prints
  `status: <reason>` lines); 2 usage error.
- Read-only: never passes repair/force/zero/write options to anything; no NE password fields.
  Only side effect: appends to its trace file `/usr/cnc/trace/upgsnap` (house convention).

## Sections

| name | contents | source |
|---|---|---|
| `system` | hostname; os (PRETTY_NAME); netflex release + build; rpm CORE / PATCH / IPATCH (`rpm -q`); machtype; gr role (FEP/BEP, not act/stby); httpd version; app up yes/no; `disk.<mount>.size_kb` for / /usr2 /usr4 /tmp /var (present mounts only; no used/avail) | sources incinfo uses (version.h, os-release, CONFIG_FILE, read_gr_data, getcmdoutput), statvfs |
| `rdb` | location; direct connect ok/fail; pgbouncer enabled + ok/fail; has_data; version current / system / match | librdb |
| `nes` | totals up/down/disabled/total/gne_up; per NE `ne.<id>.tid`, `.dtype`, `.link` (up/down/disabled), `.release` | frame_link (`Frmlnk`, `get_link_status`, disable bits) |
| `ne_links` | per `<neid>.<slot>`: `<snmp|cli|netconf>_<up|down>`, proto, ip:port | es64_snmp_info c-tree (what `snmpne -A [-C] -D` dumps) |
| `equipment` | per NE a whitelist of stable frame_link fields (exact list fixed in the plan, drawn from nfdata's printfrm fields); excludes counters, times, pids, passwords, usr_chg | frame_link |
| `alarms` | active alarm counts by severity + total | alm database walk |
| `dbcheck` | per-audit error counts + total (parsed from `dbcheck -A` summary table) | subprocess |
| `db_tie`, `db_alias`, `db_opr`, `db_opr_notify` | config records in primary-index order | subprocess `inc_db <db> --export` |

When netFLEX is down (shared memory not attached), sections that need it print
`status: unavailable (netFLEX not running)` and are not counted as failures.

## Structure

- `cnc/tools/src/upgsnap.c` — main, options (`getopts`), section table, dispatch, header printing.
- `cnc/tools/src/upgsnap_sys.c`, `upgsnap_rdb.c`, `upgsnap_ne.c`, `upgsnap_db.c` — gatherers.
- `cnc/tools/src/upgsnap_fmt.c` — pure formatting helpers (key/value emit, value sanitizing,
  sorted line buffer) — the unit-tested part.
- `include/upgsnap.h` — prefixed public types/functions, Doxygen.
- Build: `$(PBIN)/upgsnap` rule in `cnc/tools/src/tools.mk` (added to `.ALL_LINUX`), linking
  `$(CORELIBS) $(UTILLIB) $(LIBCNCDB) -lnelib -linc -lgr -latm -lrdb -lpq`; per-house pattern
  (`frm_vs_rdb`).
- Install: add `upgsnap` to the `bin=` list in `3b2/shell/install_cnc`.

## Testing

- `cnc/tools/src/upgsnap_test.c`: non-installed driver for `upgsnap_fmt.c` (sorting, sanitizing,
  section header/exit-code aggregation) — built via a separate target, run by hand/CI.
- Build on the RHEL 7/8/9 build hosts (gcc 4.8.5/8.5/11).
- Manual on holmvm32: run each section twice (no change between runs → identical output), run
  across an app restart (only expected lines differ), confirm no password text in output.

## nfupgrader follow-up (separate change, separate repo)

- If `/usr/cnc/bin/upgsnap` exists, generate the report list from `upgsnap --list`
  (`upgsnap --section NAME` each); else fall back to the current 21-command list.
- Per-report timeout override (dbcheck may take minutes).
- The before snapshot runs the *old* load's binaries, hence the need for the 54.0/54.1 backports.

## Out of scope

Pass/fail verdicts; JSON output; peer hosts; changing dbcheck/inc_db; alarm record contents.
