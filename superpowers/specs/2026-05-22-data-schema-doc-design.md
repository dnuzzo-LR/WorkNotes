# Data Schema & Semantic Content Document — Design

**Date:** 2026-05-22
**Status:** Approved (brainstorm phase)
**Author:** dnuzzo (with Claude)

## 1. Purpose

Produce a durable, machine-readable description of every dynamic-data location
in the netFLEX application so that a downstream "report writing" skill can
answer questions and generate reports of four kinds:

1. Network inventory (NE list, card/port counts, circuit counts, capacity)
2. Operational status (alarms, link/port up/down, in-service vs OOS)
3. Topology / connectivity (NE linkage, circuit paths, A-Z, protect groups)
4. Ad-hoc query answering ("where does X live, what joins to Y, what shm holds Z")

The document is the skill's source of truth for *where data lives* and
*what it means*.

## 2. Data sources in scope

netFLEX stores dynamic data in three places:

| Source | Technology                | Authoritative location                    |
|--------|---------------------------|-------------------------------------------|
| rdb    | PostgreSQL                | `cnc/rdb/schema/**/*.sql`, upgrade scripts |
| ctree  | c-tree ISAM tables        | DB headers included by `cnc/utility/src/alldbs.c` |
| shm    | Shared-memory C arrays    | `struct frame_link[]`, `struct dacs_port[]` in `include/d1compool.h` |

All three are in scope. The document covers both structural information
(tables, columns, struct fields, types, keys) and semantic information
(what each entity means, lifecycle, gotchas, cross-source relationships).

## 3. Generation strategy

Tiered:

- **Auto-extract structural inventory** for *all* tables/structs across all
  three sources. Breadth coverage; regen-able.
- **Hand-curate deep semantics** for four critical entity groups. Depth
  coverage; SME-written prose.

Entity groups receiving deep semantic curation:

- Network elements & topology (`ne`, `ne_ntwk`, `frame_link`, `dacs_port`, `links`, `protect_group`)
- Circuits & services (`ckt_info`, `snc`, `svc`, `ctp`, `tie`, `indirect_tie`, `eth_xcon`, `lsp_tunnel`, `mpls_pw`)
- Alarms & events (`alm`, `almhist`, `alm_map`, `fallout`, `eventd`)
- PM & metrics (`probpm24`, `schedpm`, `montype`, `stats`, `lightlvlthr`)

Auto-inventory covers everything else without semantic prose.

## 4. Output artifacts

```
docs/data-schema/
├── inventory.yaml        # auto-gen; every table/struct/field with type+keys
├── joins.yaml            # auto-gen seed + hand-edited cross-source key map
├── semantics.md          # hand-written; deep prose for the 4 critical groups
└── README.md             # how to regen, how skill consumes
```

`inventory.yaml` is regenerated wholesale on every extractor run and is not
hand-edited. `joins.yaml` is regenerated *by field*: `seed_joins.py` only
writes the `appearances` arrays and `type` for keys it discovers via name
match across sources. It never overwrites `semantic`, an explicit `role`
direction override, or any `cross_source_paths` entry. Keys present in
`joins.yaml` but no longer in inventory are emitted into a top-level
`orphans:` block for manual review rather than deleted. `semantics.md` is
never touched by scripts.

## 5. Artifact schemas

### 5.1 `inventory.yaml`

Top-level structure:

```yaml
schema_version: <git short sha>-<YYYYMMDD>
generated_at: <ISO-8601>

sources:
  rdb:
    type: postgresql
    tables:
      <table_name>:
        primary_key: [<col>, ...]
        columns:
          <col_name>:
            type: <sql type>
            nullable: <bool>
            doc: <string, optional>
            enum_ref: <enum_name, optional>
        indexes:
          - { name: <idx_name>, cols: [<col>, ...], unique: <bool> }
        source_file: <path>

  ctree:
    type: c-isam
    tables:
      <record_name>:
        primary_key: [<col>, ...]
        record_size: <int, if known>
        columns:
          <field_name>:
            type: <c type>
            offset: <int, if known>
        source_file: <path>

  shm:
    type: shared-memory-array
    arrays:
      <array_name>:                # e.g. frame_link, dacs_port
        element_type: <c struct name>
        capacity: <symbol or int>  # e.g. NeAvail
        index_by: <field name>     # e.g. neid
        fields:
          <field_name>:
            type: <c type>
            doc: <string, optional>
            enum_ref: <enum_name, optional>
        source_file: <path>

enums:
  <enum_name>:
    source_file: <path>
    values:
      - { name: <symbol>, value: <int>, doc: <string, optional> }
```

### 5.2 `joins.yaml`

```yaml
keys:
  <key_name>:                       # e.g. neid, ckt_id, aid, port_id
    semantic: <prose>
    type: <c/sql type>
    appearances:
      - { source: <rdb|ctree|shm>, table|array: <name>, column: <field>, role: <pk|fk -> X.y|array_index|field> }

cross_source_paths:
  - name: <human label>             # e.g. "NE -> alarms"
    hops:
      - <source>.<table>.<col>
      - <source>.<table>.<col>
```

`appearances` is seeded automatically by name match across sources;
`semantic`, FK direction in `role`, and `cross_source_paths` are hand-edited.

### 5.3 `semantics.md`

Top-level `##` sections are the four critical groups. Each entity within a
group is an `### Entity: <Name>` heading followed by this fixed sub-structure:

```markdown
### Entity: <Name>
**Sources:** `<source>.<table_or_array>`, ...

**Purpose.** <one paragraph>

**Lifecycle.** <how rows are created, mutated, retired>

**Key fields (semantic).**
- `<field>` — <prose>
- ...

**Gotchas.** <indexing quirks, deprecated paths, legacy vs authoritative source, etc>
```

The skill keys off the `### Entity:` heading and `Sources:` line to
cross-reference `inventory.yaml`.

## 6. Extractor pipeline

```
                  cnc/rdb/schema/**/*.sql
                              │
                  extract_rdb.py  ──> rdb.partial.yaml

                  cnc/utility/src/alldbs.c (include list)
                  + each include/<x>_db.h
                              │
                  extract_ctree.py  ──> ctree.partial.yaml

                  include/d1compool.h
                              │
                  extract_shm.py  ──> shm.partial.yaml

                  rdb.partial.yaml + ctree.partial.yaml + shm.partial.yaml
                              │
                           merge.py
                              │
                              ▼
                       inventory.yaml

                       inventory.yaml
                              │
                         seed_joins.py
                              │
                              ▼
                        joins.yaml (seed; hand-extended below the seed marker)
```

Tooling:

- **rdb extractor:** `sqlparse` over `CREATE TABLE` / `ALTER TABLE` statements.
  Captures columns, types, NOT NULL, PRIMARY KEY, FOREIGN KEY, CREATE INDEX.
- **ctree extractor:** libclang AST walk of each header listed in
  `alldbs.c`. Captures record struct name + field names + types + offsets.
- **shm extractor:** libclang AST of `include/d1compool.h`. Emits `frame_link`
  and `dacs_port` plus any sibling arrays declared as `extern struct ... *X`
  in that header.
- **enums:** all three extractors emit enum declarations into the shared
  `enums:` section; `merge.py` dedupes by name.

All extractors live in `cnc/tools/schema-extract/` and are invoked by a
single nmake-compatible Makefile. Regen is on-demand, committed to git.

### 6.1 Error handling

Extractors fail loud (non-zero exit) on:

- Unknown SQL or C type they cannot map
- Missing source file
- Duplicate table or struct name *within the same source* (cross-source name reuse is fine; each source has its own namespace in inventory.yaml)
- libclang parse failure

No silent partial output. CI runs the extractors against the committed
inventory.yaml as a snapshot test; drift fails CI.

### 6.2 Testing

- Unit tests per extractor covering: one representative table per source,
  one enum, one cross-referencing field, one error case.
- Snapshot test: regen full inventory + joins seed in a clean dir; `diff`
  against committed copies; non-zero on drift.

## 7. Skill integration contract

The downstream report-writing skill consumes the three artifacts as follows:

1. **Load order.** `inventory.yaml` → `joins.yaml` → `semantics.md`. The
   first two parsed as YAML; the third chunked by `###` headings.
2. **Entity resolution.** Given an entity name, the skill:
   - Looks it up in inventory across all three sources.
   - Pulls the matching `### Entity:` section from `semantics.md`, if any.
   - Pulls all `keys:` entries from `joins.yaml` whose `appearances` include
     the entity's tables/arrays.
3. **Cross-source report assembly.** For multi-source reports the skill
   either picks a named `cross_source_paths` recipe or traces hops through
   `joins.yaml` between requested entities.
4. **Field-level questions.** Inventory provides type / nullable / key info.
   Semantics adds prose only for the four curated groups. For auto-only
   entities, the skill answers structurally and labels the response
   "no curated semantics; see `<source_file>`."
5. **Versioning.** Inventory's `schema_version` (git sha + extract date) is
   cited in every generated report so stale extracts are visible.
6. **Loading hint.** `docs/data-schema/README.md` instructs the skill to
   always load all three artifacts, never invent fields absent from
   inventory, and mark inferred joins as "inferred" when they are not
   present in `joins.yaml`.

## 8. Out of scope

- Static configuration files (only dynamic stores covered).
- Java / web / GUI persistent stores beyond rdb.
- Building the report-writing skill itself; this spec only defines the
  document it will consume.
- Migration tooling for renaming tables across upgrades.

## 9. Acceptance criteria

- Extractor pipeline regenerates `inventory.yaml` and `joins.yaml` seed
  reproducibly; snapshot test passes.
- `inventory.yaml` lists every rdb table reachable from
  `cnc/rdb/schema/`, every ctree record reachable from `alldbs.c`, and
  the `frame_link` / `dacs_port` arrays plus their fields.
- `joins.yaml` includes seeded `appearances` for at least the keys
  `neid`, `ckt_id`, `aid`, `port_id`, `tid`, `slot`, `card_id`, `alm_id`,
  with hand-written `semantic` and `role` fields.
- `semantics.md` has a complete `### Entity:` section for every entity
  listed in §3 under the four critical groups.
- README documents regen command and skill load contract.
