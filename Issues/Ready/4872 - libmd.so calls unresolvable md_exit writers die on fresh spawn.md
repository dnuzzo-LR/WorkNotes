---
issue: 4872
repo: lightriversoftware/netflex
state: OPEN
status: Ready
sprint: Sprint 15
title: libmd.so calls unresolvable md_exit; fep_bep/bep_fep writers die on fresh spawn (5.4.026)
type: bug
release: 5.4.026
milestone: 5.4.0
author: jgomillion-LR
assignee: dnuzzo-LR
created: 2026-06-15
updated: 2026-06-19
url: https://github.com/lightriversoftware/netflex/issues/4872
---

# Issue 4872 — libmd.so calls unresolvable md_exit; fep_bep/bep_fep writers die on fresh spawn (5.4.026)

[View on GitHub](https://github.com/lightriversoftware/netflex/issues/4872)

## Summary

In netFLEX 5.4.026, `libmd.so` references `md_exit` as an undefined dynamic symbol (`U md_exit`), but **no library or binary in the install exports `md_exit` in its dynamic symbol table** (`.dynsym`). `md_exit` exists only as a local `T` (static text) symbol inside four executables — `lansim`, `md_cnc`, `md_rdr`, `md_wtr` — none of which expose it via `.dynsym`. Since the dynamic linker only resolves PLT entries against `.dynsym`, the lazy PLT resolution of `md_exit` inside `libmd.so` can never succeed at runtime.

The 14 call sites are all in error-exit branches of 5 libmd functions (`set_md_ids`, `send_oob_ack`, `recv_oob_ack`, `send_md_data`, `get_client_socket`), so the symbol is not reached on healthy startup. When it *is* reached, the dynamic linker aborts the calling process with a `symbol lookup error: ... undefined symbol: md_exit`.

This is a **latent / dormant defect**: long-running writer processes that started under a prior build keep their mmap'd libs resident and only re-link `libmd.so` on a fresh spawn. So an install can sit healthy for days after upgrade and then surface this on the first respawn driven by load, OOM, a planned restart, or `systemctl restart netFLEX-md`. The same `U md_exit` + zero-definer condition exists in 5.2.132, but the error-exit path is not observed to trigger there.

## Reproduction Steps

On any 5.4.026 multi-box install (FEP + ≥1 BEP):

```bash
systemctl restart netFLEX-md
journalctl -u netFLEX-md -f | grep 'undefined symbol: md_exit'
```

Within seconds:

```
md[NNNN]: /usr/cnc/bin/fep_bep_msg_wtr: symbol lookup error: /usr/cnc/lib/libmd.so: undefined symbol: md_exit
md[NNNN]: /usr/cnc/bin/fep_bep_frm_wtr: symbol lookup error: /usr/cnc/lib/libmd.so: undefined symbol: md_exit
md[NNNN]: /usr/cnc/bin/bep_fep_msg_wtr: symbol lookup error: /usr/cnc/lib/libmd.so: undefined symbol: md_exit
```

## Observed Behavior

Observed in `journalctl` on 2026-06-15 across one FEP + four BEPs:

| Binary | Where observed |
|---|---|
| `/usr/cnc/bin/fep_bep_msg_wtr` | FEP (holmvm17) |
| `/usr/cnc/bin/fep_bep_frm_wtr` | FEP (holmvm17) |
| `/usr/cnc/bin/bep_fep_msg_wtr` | all 4 BEPs (holmvm13/14/18/19) |

Other binaries linking `libmd.so` may be susceptible if they hit the same error paths. `bep_fep_frm_wtr` was *not* observed hitting `md_exit` — it has a separate deterministic SIGSEGV tracked as #4864.

### Evidence (verified live on holmvm17 FEP)

```
$ nm -D /usr/cnc/lib/libmd.so | grep md_exit
                 U md_exit                      # undefined external

$ readelf -r /usr/cnc/lib/libmd.so | grep md_exit
000000210200  004600000007 R_X86_64_JUMP_SLO 0000000000000000 md_exit + 0
```

- 14 call sites across 5 exported libmd functions: `set_md_ids` (5), `send_oob_ack` (1), `recv_oob_ack` (1), `send_md_data` (3), `get_client_socket` (4). All use `mov $0x1,%edi ; callq <md_exit@plt>` — i.e. `md_exit(1)`, an error-exit-with-status-1 helper.
- A sweep of every ELF in `/usr4/inc5.4.026/{bin,sbin,mbin}` and `lib/*.so*` found **zero exporters** of `md_exit` in `.dynsym`.
- `md_exit` is defined as local `T` in `lansim`, `md_cnc`, `md_rdr`, `md_wtr`, but none expose it via `.dynsym` — so even processes spawned by those parents fail to resolve it.

### File metadata (5.4.026)

- Path: `/usr4/inc5.4.026/lib/libmd.so`
- Size: 134,048 bytes; Mtime: Jun 2 2026 17:09
- MD5: `88d4a474963600a2e1b078b881186eb2`
- Build ID: `040c43808000d2f86270319aa4f50646c847e2d7`

## Resolution / Notes

No resolution posted yet. The automated RCA bot failed on this issue ([workflow run](https://github.com/lightriversoftware/netflex/actions/runs/27579972925)), so no RCA comment exists.

**Suggested fix directions from the reporter (Jason Gomillion), Dev to decide:**

1. Replace `md_exit(1)` calls inside `libmd.so` with internal cleanup + return — a shared library terminating the host process via an unresolvable host symbol is the underlying anti-pattern; let the caller handle the error.
2. Or expose `md_exit` in `.dynsym` of every binary that links `libmd.so` (`-rdynamic` / `-Wl,--export-dynamic`, or `__attribute__((visibility("default")))`).
3. Or move `md_exit`'s definition into a shared library (`libcnc.so` / `libutillib.so`) so it's resolvable by `libmd.so` regardless of which binary loaded it.

**Workaround (operator-side, not a fix):** roll the `/usr/cnc` symlink back to a prior install. Verified in lab; `/usr4/inc5.2.132` is present on every machine.

**Relationship to [[4864 - bep_fep_frm_wtr segfaults in bffw_count_up_ne|#4864]]:** This is a **separate, independent defect** — different source file (`libmd.so` vs `l_bep_fep_frm_wtr.cpp`), different mechanism (dynamic-linker failure vs SIGSEGV). They share the 5.4.026 release and can overlap during a writer-respawn cascade, but have no common root cause. The reporter recommends fixing **both** before 5.4.0 ships to a multi-box customer: either alone is recoverable, but together they make any `netFLEX-md` restart fatal until rollback.

## Attachments

None.

## Metadata

- **Issue:** [#4872](https://github.com/lightriversoftware/netflex/issues/4872)
- **Repo:** lightriversoftware/netflex
- **State:** OPEN
- **Status:** Ready
- **Sprint:** Sprint 15
- **Type:** bug
- **Labels:** bug
- **Software Release:** 5.4.026
- **Milestone:** 5.4.0
- **Author:** Jason Gomillion (jgomillion-LR)
- **Assignee:** Dan Nuzzo (dnuzzo-LR)
- **Created:** 2026-06-15
- **Updated:** 2026-06-19
- **Environment:** Rocky Linux 8.10, kernel 4.18.0-553.123.1.el8_10, glibc 2.28; FEP holmvm17, BEPs holmvm13/14/18/19, GR-standby FEP linc12vm9
- **Related:** #4864 (separate SIGSEGV defect in same release)

## My Notes
*  60214e6ed9c968ead7fd9aff64dc40006010cd60