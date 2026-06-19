# clan-rewrite schema gaps

Surfaced while authoring the first 5 representative NE YAMLs + Lua scripts.
Each item lists the gap, the NE(s) that hit it, the workaround used in
the current files, and proposed resolutions for spec refinement.

**Revision 2026-05-27:** spec decision to move `login_steps:` into the
required `lua_module::login()` entry point resolved or downgraded
several gaps. Status field added to each.

Spec under refinement: `docs/superpowers/specs/2026-05-27-clan-yaml-design.md`.

---

## Gap 1 — Credential substitution tokens

**Status:** **RESOLVED 2026-06-04** — resolution A applied. §7.3 now lists
`%p_enable`, `%p_read`, `%p_write`, `%p_admin`; nil-backed token expands to
empty string with a one-shot WARN.

Login no longer uses `%p`-style substitutions
(Lua reads creds via `session:get_var`), but YAML scalar fields such as
`keepalive.command` still go through the substitution table.

**Spec today.** §6.3 / §7.3 defines four runtime substitutions:
`%h` host, `%u` user, `%p` password, `%t` ne_type.

**Reality.** Real NEs use multiple distinct credentials. Lua side names
already settled on: `password`, `p_enable`, `p_read`, `p_write`,
`card_type`, `host`, `user`. YAML side should mirror those names as
substitution tokens so a keepalive command like
`exec ping %h credential %p_read\n` works without dropping to Lua.

**Proposed resolutions.**

A. **Enumerate.** Add `%p_enable`, `%p_read`, `%p_write`, `%p_admin` to
   the §6.3 / §7.3 table. **Recommended.**

B. **Generic namespace.** `%cred.<name>` opaque to the spec.

C. **Lua-only.** Lua substitutes — defeats the YAML-data principle.

---

## Gap 2 — Capturing matched text from `expect`

**Status:** **OBSOLETE.** Login moved to Lua, which captures matches
directly via `session:expect(...)` return value + `session:last_output()`.
No remaining use case in `post_command` / `disconnect_steps` for the
5 reviewed NEs.

Keep on the radar in case a future NE needs to thread a captured match
from `post_command` step language into a later step.

---

## Gap 3 — Branching on session variables (not just output buffer)

**Status:** **DEFERRED 2026-06-04** — recorded as spec §14 Open Question 11.
Resolution C (status quo, Lua `op: lua`). Revisit only if enough NEs want
fully-declarative card-type-gated disconnect.

(downgraded) Login (the noisy use case) is now Lua. Remaining
case: card-type-gated disconnect (TSS5 write-memory, Cisco optional
writemem) — both still pushed to Lua via `op: lua` calls inside
`disconnect_steps`.

If `op: branch_var` were added, those disconnect blocks could become
fully declarative. Modest ergonomic win; not urgent.

**Proposed resolutions** (unchanged from rev 1):

A. **Extend `branch`** with `if_var: <name>, matches: <regex>` (mutually
   exclusive with `if_match`). Recommended if Gap 3 is revisited.

B. **`op: branch_var`** as a separate op.

C. **Status quo.** Keep card-type-gated logic in Lua via `op: lua`.

---

## Gap 4 — One YAML representing multiple dtypes

**Status:** **RESOLVED 2026-06-04 (revised)** — `ne_aliases:` rejected by user
in favor of YAML-file inheritance. New §7.5: `extends: <basename>` gives
multi-level single-parent inheritance; `_defaults` → ancestors → self merge
(maps deep-merge, lists replace); `abstract: true` base files are templates
excluded from the registry, used to group equipment (vendor / transport /
multi-dtype families). Fuji X100 = 3 thin concrete files over one abstract
base, each registering its own real dtype. §7.4 validation, §10.1 loader
two-pass resolution (cycle + missing-parent detection), §12.1 errors, §13.1
CI checks all updated.

**Spec today.** §6.1 says one `ne_type:` per file.

**Reality.** `FUJI_1FINITY_X100` is a clan.c macro covering three dtype
enum values (S100=119, T100=120, L1XX=121) with identical behavior.

**Current workaround.** `fuji_1finity_x100.yaml` declares
`ne_aliases: [FUJI_1FINITY_S100, FUJI_1FINITY_T100, FUJI_1FINITY_L1XX]`
as an ad-hoc field. Loader would reject it today.

**Proposed resolutions.**

A. **`ne_types:` list.** Replace singular field. Verbose for single-dtype
   files.

B. **Keep `ne_type:` + bless `ne_aliases:`.** Backwards-compatible.
   **Recommended.**

C. **Symlinks on disk.** No schema change. Operations folks will hate it.

---

## Gap 5 — Adaptive prompt regex (CIENA_RLS)

**Status:** **RESOLVED 2026-06-04** — new §7.7 documents
`prompt.adapt_from_actual` as a bool copied into a read-only session var;
stepper does nothing, Lua `login()` acts on it. Also added to §7.1 example.

(was: OBSOLETE as schema feature) Login is Lua, so
`ciena_rls.lua::login` does the adapt-from-actual rewrite directly and
exposes the result via `session:set_var('adapted_primary_regex', ...)`.

The YAML keeps `prompt.adapt_from_actual: true` as a flag the loader
passes into the session via a session var; Lua decides what to do with
it. Spec change needed: document this flag formally (currently it's
implicit in `_defaults.yaml`).

**Minimal spec edit:** §7.1 adds one line:
`prompt.adapt_from_actual: bool — passed to lua_module::login as a
session var. Stepper does nothing with it on its own.`

---

## Gap 6 — Transport selection at runtime

**Status:** **RESOLVED 2026-06-04** — resolution A applied. New §7.6:
`transport.protocol` accepts scalar or list; list = preferred-order
try-then-fallback. §7.1 example shows `[ssh, telnet]`.

**Spec today.** §7.1 `transport.protocol: telnet | ssh | serial` is a
single value.

**Reality.** clan.c picks telnet vs SSH at session-open based on global
config and per-NE flags.

**Current workaround.** YAML files state "preferred" protocol;
stepper has no fallback rule.

**Proposed resolutions.**

A. **`protocol` becomes a list.** `protocol: [ssh, telnet]` means try
   SSH first, fall back to telnet. **Recommended.**

B. **`transport.fallback:`** explicit field.

C. **Move selection out of YAML** into per-deployment config.

---

## Gap 7 — Paging drain: automatic vs explicit

**Status:** **RESOLVED 2026-06-04** — clarified, no schema change. §7.1
`paging:` comment + §8.3 paragraph state paging is implicit/automatic
(stepper drains every command read); `post_command` is for additional
processing after paging is drained.

**Spec today.** §7.1 has a `paging:` block. §8.3 says `post_command`
runs after every command.

**Open question.** Is the `paging:` block stepper-automatic (every
command read auto-drains paging until prompt), or does the YAML need
to wire it explicitly into `post_command`?

**Recommendation.** Spec clarifies: `paging:` is implicit and automatic.
`post_command` is for ADDITIONAL processing after paging is drained.

Add one sentence to §7.1 + §8.3. No schema change.

---

## Gap 8 — `expect_prompt` shortcut

**Status:** **OBSOLETE.** Login is Lua; the repetition of the prompt
regex no longer shows up in YAML. `post_command` steps rarely
re-expect the prompt explicitly. Drop from list.

---

## Gap 9 (new) — Session var naming conventions

**Status:** **RESOLVED 2026-06-04** — §9.2 now has a "Reserved session vars"
table (`user`, `password`, `p_enable`, `p_read`, `p_write`, `card_type`,
`host`, `ne_type`, `slot`, `neid`) bridge-populated at session-open.

(discovered during rev 2 authoring)

**Observation.** Lua scripts read `session:get_var('password')`,
`get_var('p_enable')`, `get_var('card_type')`, `get_var('user')`. The
stepper has to populate these from frame_link / SNMP info / config at
session-open time. Names are currently ad-hoc.

**Recommendation.** §9.2 spec section adds a "Reserved session vars"
table listing the standard names every NE author can rely on:
`user`, `password`, `p_enable`, `p_read`, `p_write`, `card_type`,
`host`, `ne_type`, `slot`, `neid`. Bridge populates them from the NE
record at session start. Unknown vars stay nil.

Stops every NE author from guessing key names.

---

## Summary of recommended spec edits (revised order)

**All applied 2026-06-04** to `2026-05-27-clan-yaml-design.md`.

1. ✅ **Gap 1** — added `%p_enable`, `%p_read`, `%p_write`, `%p_admin` to §7.3 substitution table.
2. ✅ **Gap 4** — YAML-file inheritance via `extends:` + `abstract:` bases (new §7.5; §7.4/§10.1/§12.1/§13.1 updated). Supersedes the `ne_aliases:` idea.
3. ✅ **Gap 5** — `prompt.adapt_from_actual` documented as session-var flag passed to Lua (new §7.7 + §7.1 example).
4. ✅ **Gap 6** — `transport.protocol` accepts a list, preferred-order fallback (new §7.6 + §7.1 example).
5. ✅ **Gap 7** — paging clarified implicit/automatic (§7.1 comment + §8.3 paragraph). No schema change.
6. ✅ **Gap 9 (new)** — Reserved session vars table added to §9.2.
7. ⏸️ **Gap 3** — deferred; recorded as spec §14 Open Question 11.
8. 🗑️ **Gap 2, Gap 8** — obsolete; dropped from tracking.
