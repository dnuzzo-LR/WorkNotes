# Data Schema Documentation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the extractor pipeline, three artifacts (`inventory.yaml`, `joins.yaml`, `semantics.md`), and skill-consumption README described in `docs/superpowers/specs/2026-05-22-data-schema-doc-design.md`.

**Architecture:** Tiered. Python extractors auto-generate `inventory.yaml` from PostgreSQL custom-format dumps (rdb), libclang AST of ctree DB headers, and libclang AST of `include/d1compool.h` (shm). A seed-joins step produces `joins.yaml` from cross-source name match; hand edits are preserved by field-level merge. Hand-written `semantics.md` covers the four critical entity groups.

**Tech Stack:** Python 3.6+ (RHEL 8 baseline), `pyyaml`, `sqlparse`, `clang.cindex` (libclang Python bindings), `pytest`, `pg_restore` (called via `subprocess`). Build glue via nmake (project standard).

---

## File Structure

**Created:**

```
cnc/tools/schema-extract/
├── README.md                       # internal: how to run extractors
├── extract.mk                      # nmake-compatible entry points
├── requirements.txt                # pinned pyyaml, sqlparse, clang versions
├── common.py                       # shared yaml emit + type maps + error helper
├── extract_rdb.py                  # pg_restore + sqlparse -> rdb partial
├── extract_ctree.py                # libclang over alldbs.c include list
├── extract_shm.py                  # libclang over d1compool.h
├── merge.py                        # combines 3 partials into inventory.yaml
├── seed_joins.py                   # field-merge joins.yaml; emits orphans
├── snapshot_test.py                # CI: regen and diff against committed copies
└── tests/
    ├── conftest.py
    ├── test_common.py
    ├── test_extract_rdb.py
    ├── test_extract_ctree.py
    ├── test_extract_shm.py
    ├── test_merge.py
    ├── test_seed_joins.py
    └── fixtures/
        ├── sample.sql              # small synthetic schema
        ├── sample_db.h             # synthetic ctree record header
        └── sample_d1compool.h      # synthetic shm struct header

docs/data-schema/
├── README.md                       # skill consumption contract
├── inventory.yaml                  # generated; committed
├── joins.yaml                      # seeded + hand-edited; committed
└── semantics.md                    # hand-written; committed
```

**Modified:** None (greenfield).

---

## Notes for the implementing engineer

This codebase uses **Lucent/AT&T nmake** for all `*.mk` files (see `CLAUDE.md` in repo root). Do **not** assume GNU make syntax. Look at any existing `cnc/tools/**/*.mk` for syntax reference before writing `extract.mk`.

PostgreSQL custom-format dumps (`cnc/rdb/schema/inc_schema`, `.../otn_port_schema`) are **binary**, not text. They must be converted to SQL via `pg_restore --schema-only --no-owner --no-privileges <file>`.

Ctree DB headers follow the pattern shown in `include/ne_db1.h`: a `struct <name>_rec { ... };` declaration plus a typedef. Most fields are POD; some use sized macros (`USER32_SZ`, etc.) which libclang resolves via the system include path.

The shm header `include/d1compool.h` is large (~2300 lines). `struct frame_link` starts at line 1158, `struct dacs_port` at line 335. Both contain C bitfields, which libclang reports via cursor kind `FIELD_DECL` with `is_bitfield()` true.

The repo has `BASE` and `VPATH` env-var conventions (see `CLAUDE.md`). When invoking C tooling, respect them; the extractors invoke libclang directly and need `-I` flags derived from `VPATH`.

---

## Task 1: Bootstrap directory and shared helpers

**Files:**
- Create: `cnc/tools/schema-extract/requirements.txt`
- Create: `cnc/tools/schema-extract/common.py`
- Create: `cnc/tools/schema-extract/tests/conftest.py`
- Create: `cnc/tools/schema-extract/tests/test_common.py`

- [ ] **Step 1: Write `requirements.txt`**

```
pyyaml==6.0.1
sqlparse==0.4.4
clang==14.0.0
pytest==7.4.4
```

- [ ] **Step 2: Write failing test for `common.emit_yaml`**

`tests/test_common.py`:

```python
import io
import pytest
from common import emit_yaml, ExtractError, sql_to_logical_type, c_to_logical_type


def test_emit_yaml_is_deterministic():
    data = {"b": 2, "a": 1, "c": {"y": 2, "x": 1}}
    out1 = emit_yaml(data)
    out2 = emit_yaml(data)
    assert out1 == out2
    # Keys are sorted, top level first
    assert out1.index("a:") < out1.index("b:")
    assert out1.index("c:") < out1.index("y:")


def test_extract_error_is_loud():
    with pytest.raises(ExtractError) as exc:
        raise ExtractError("missing source file: foo.sql")
    assert "missing source file" in str(exc.value)


def test_sql_type_mapping():
    assert sql_to_logical_type("integer") == "int"
    assert sql_to_logical_type("character varying(64)") == "varchar(64)"
    assert sql_to_logical_type("timestamp without time zone") == "timestamp"
    assert sql_to_logical_type("bigint") == "int64"


def test_c_type_mapping():
    assert c_to_logical_type("int") == "int"
    assert c_to_logical_type("unsigned int") == "uint"
    assert c_to_logical_type("char [8]") == "char[8]"
    assert c_to_logical_type("short") == "short"


def test_sql_unknown_type_raises():
    with pytest.raises(ExtractError):
        sql_to_logical_type("hstore")


def test_c_unknown_type_raises():
    with pytest.raises(ExtractError):
        c_to_logical_type("__m128i")
```

- [ ] **Step 3: Run to verify failure**

```
cd cnc/tools/schema-extract && python -m pytest tests/test_common.py -v
```

Expected: ImportError or ModuleNotFoundError for `common`.

- [ ] **Step 4: Implement `common.py`**

```python
"""Shared helpers for schema extractors.

Loud-fail philosophy: every unmapped type raises ExtractError. No silent
partial output. See docs/superpowers/specs/2026-05-22-data-schema-doc-design.md.
"""
import io
import sys
import yaml


class ExtractError(RuntimeError):
    """Raised when an extractor cannot map a type or finds invalid input.

    Callers must let this propagate so the process exits non-zero.
    """


def emit_yaml(data) -> str:
    """Emit deterministic YAML: sorted keys, no aliases, block style."""
    return yaml.safe_dump(
        data,
        sort_keys=True,
        default_flow_style=False,
        allow_unicode=True,
        width=120,
    )


def write_yaml(path: str, data) -> None:
    """Write deterministic YAML to path."""
    with open(path, "w") as f:
        f.write(emit_yaml(data))


_SQL_TYPE_MAP = {
    "smallint": "short",
    "integer": "int",
    "int": "int",
    "bigint": "int64",
    "real": "float",
    "double precision": "double",
    "boolean": "bool",
    "text": "text",
    "bytea": "bytes",
    "date": "date",
    "timestamp without time zone": "timestamp",
    "timestamp with time zone": "timestamptz",
    "time without time zone": "time",
}


def sql_to_logical_type(sql_type: str) -> str:
    """Map a PostgreSQL type spelling to a logical type string."""
    s = sql_type.strip().lower()
    # character varying(N) -> varchar(N)
    if s.startswith("character varying"):
        return s.replace("character varying", "varchar").replace(" ", "")
    if s.startswith("character("):
        return s.replace("character", "char").replace(" ", "")
    if s in _SQL_TYPE_MAP:
        return _SQL_TYPE_MAP[s]
    # numeric(p,s)
    if s.startswith("numeric"):
        return s.replace(" ", "")
    raise ExtractError(f"unknown SQL type: {sql_type!r}")


_C_TYPE_MAP = {
    "int": "int",
    "unsigned int": "uint",
    "short": "short",
    "unsigned short": "ushort",
    "long": "long",
    "unsigned long": "ulong",
    "long long": "int64",
    "char": "char",
    "unsigned char": "uchar",
    "signed char": "char",
    "float": "float",
    "double": "double",
    "time_t": "time_t",
    "size_t": "size_t",
}


def c_to_logical_type(c_type: str) -> str:
    """Map a C type spelling to a logical type string.

    Handles arrays: 'char [8]' -> 'char[8]'.
    """
    s = " ".join(c_type.split())
    # Strip array spec, remember it
    array_suffix = ""
    if "[" in s:
        base, _, rest = s.partition("[")
        array_suffix = "[" + rest.replace(" ", "")
        s = base.strip()
    if s in _C_TYPE_MAP:
        return _C_TYPE_MAP[s] + array_suffix
    raise ExtractError(f"unknown C type: {c_type!r}")
```

`tests/conftest.py`:

```python
import sys
import pathlib

# Make sibling modules importable from tests/
ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
```

- [ ] **Step 5: Run tests, verify pass**

```
cd cnc/tools/schema-extract && python -m pytest tests/test_common.py -v
```

Expected: 6 passed.

- [ ] **Step 6: Commit**

```bash
git add cnc/tools/schema-extract/requirements.txt \
        cnc/tools/schema-extract/common.py \
        cnc/tools/schema-extract/tests/conftest.py \
        cnc/tools/schema-extract/tests/test_common.py
git commit -m "feat(schema-extract): add common helpers (yaml emit, type maps)"
```

---

## Task 2: rdb extractor

**Files:**
- Create: `cnc/tools/schema-extract/extract_rdb.py`
- Create: `cnc/tools/schema-extract/tests/fixtures/sample.sql`
- Create: `cnc/tools/schema-extract/tests/test_extract_rdb.py`

- [ ] **Step 1: Write the fixture SQL**

`tests/fixtures/sample.sql`:

```sql
CREATE TABLE public.ne (
    neid integer NOT NULL,
    ne_name character varying(64) NOT NULL,
    ne_type integer
);
ALTER TABLE ONLY public.ne ADD CONSTRAINT ne_pkey PRIMARY KEY (neid);
CREATE UNIQUE INDEX ne_name_idx ON public.ne (ne_name);

CREATE TABLE public.alm (
    alm_id bigint NOT NULL,
    neid integer NOT NULL,
    severity integer
);
ALTER TABLE ONLY public.alm ADD CONSTRAINT alm_pkey PRIMARY KEY (alm_id);
ALTER TABLE ONLY public.alm
    ADD CONSTRAINT alm_neid_fkey FOREIGN KEY (neid) REFERENCES public.ne(neid);
```

- [ ] **Step 2: Write failing tests**

`tests/test_extract_rdb.py`:

```python
import pathlib
import pytest
from extract_rdb import parse_sql_to_partial

FIXTURE_DIR = pathlib.Path(__file__).parent / "fixtures"


def test_parses_columns_and_types():
    sql = (FIXTURE_DIR / "sample.sql").read_text()
    out = parse_sql_to_partial(sql, source_label="fixtures/sample.sql")
    ne = out["sources"]["rdb"]["tables"]["ne"]
    assert ne["columns"]["neid"]["type"] == "int"
    assert ne["columns"]["neid"]["nullable"] is False
    assert ne["columns"]["ne_name"]["type"] == "varchar(64)"
    assert ne["columns"]["ne_name"]["nullable"] is False
    assert ne["columns"]["ne_type"]["nullable"] is True


def test_captures_primary_key():
    sql = (FIXTURE_DIR / "sample.sql").read_text()
    out = parse_sql_to_partial(sql, source_label="fixtures/sample.sql")
    assert out["sources"]["rdb"]["tables"]["ne"]["primary_key"] == ["neid"]
    assert out["sources"]["rdb"]["tables"]["alm"]["primary_key"] == ["alm_id"]


def test_captures_unique_index():
    sql = (FIXTURE_DIR / "sample.sql").read_text()
    out = parse_sql_to_partial(sql, source_label="fixtures/sample.sql")
    idxs = out["sources"]["rdb"]["tables"]["ne"]["indexes"]
    assert idxs == [{"name": "ne_name_idx", "cols": ["ne_name"], "unique": True}]


def test_captures_foreign_key():
    sql = (FIXTURE_DIR / "sample.sql").read_text()
    out = parse_sql_to_partial(sql, source_label="fixtures/sample.sql")
    alm = out["sources"]["rdb"]["tables"]["alm"]
    # FK metadata is attached to the column dict
    assert alm["columns"]["neid"]["fk"] == "ne.neid"


def test_records_source_file():
    sql = (FIXTURE_DIR / "sample.sql").read_text()
    out = parse_sql_to_partial(sql, source_label="fixtures/sample.sql")
    assert out["sources"]["rdb"]["tables"]["ne"]["source_file"] == "fixtures/sample.sql"
```

- [ ] **Step 3: Run, verify failure**

```
cd cnc/tools/schema-extract && python -m pytest tests/test_extract_rdb.py -v
```

Expected: ImportError.

- [ ] **Step 4: Implement `extract_rdb.py`**

```python
"""Extract rdb (PostgreSQL) schema into a partial inventory.yaml.

Reads PostgreSQL custom-format dumps from cnc/rdb/schema/ via pg_restore,
parses the resulting SQL with sqlparse, and emits a partial yaml.
"""
import argparse
import pathlib
import re
import subprocess
import sys

import sqlparse
from sqlparse.sql import Identifier, IdentifierList, Parenthesis
from sqlparse.tokens import Keyword, DML, Name, Punctuation

from common import ExtractError, sql_to_logical_type, write_yaml


_CREATE_TABLE_RE = re.compile(
    r"CREATE TABLE\s+(?:public\.)?(\w+)\s*\((.+?)\)\s*;",
    re.IGNORECASE | re.DOTALL,
)
_ADD_PK_RE = re.compile(
    r"ALTER TABLE\s+ONLY\s+(?:public\.)?(\w+)\s+"
    r"ADD CONSTRAINT\s+\w+\s+PRIMARY KEY\s*\(([^)]+)\)",
    re.IGNORECASE,
)
_ADD_FK_RE = re.compile(
    r"ALTER TABLE\s+ONLY\s+(?:public\.)?(\w+)\s+"
    r"ADD CONSTRAINT\s+\w+\s+FOREIGN KEY\s*\(([^)]+)\)\s+"
    r"REFERENCES\s+(?:public\.)?(\w+)\s*\(([^)]+)\)",
    re.IGNORECASE,
)
_CREATE_INDEX_RE = re.compile(
    r"CREATE\s+(UNIQUE\s+)?INDEX\s+(\w+)\s+ON\s+(?:public\.)?(\w+)\s*\(([^)]+)\)",
    re.IGNORECASE,
)


def _parse_column_decl(decl: str):
    """Parse 'colname <type> [NOT NULL] [DEFAULT ...]' -> (name, type, nullable)."""
    decl = decl.strip().rstrip(",").strip()
    if not decl:
        return None
    # Skip table-level constraints inside parens
    if decl.upper().startswith(("CONSTRAINT", "PRIMARY KEY", "FOREIGN KEY", "UNIQUE", "CHECK")):
        return None
    parts = decl.split(None, 1)
    if len(parts) < 2:
        return None
    name = parts[0]
    rest = parts[1]
    nullable = "NOT NULL" not in rest.upper()
    # Strip DEFAULT clause and NOT NULL marker to isolate the type
    type_str = re.sub(r"\bNOT NULL\b", "", rest, flags=re.IGNORECASE)
    type_str = re.sub(r"\bDEFAULT\b.*$", "", type_str, flags=re.IGNORECASE).strip()
    return name, type_str, nullable


def parse_sql_to_partial(sql: str, source_label: str) -> dict:
    """Parse plain SQL text into a partial inventory dict."""
    tables = {}

    for m in _CREATE_TABLE_RE.finditer(sql):
        tname = m.group(1)
        cols_blob = m.group(2)
        cols = {}
        # Split top-level commas (sqlparse handles paren nesting better, but our
        # synthetic + pg_restore output is well-formed enough for a depth count)
        depth = 0
        buf = []
        decls = []
        for ch in cols_blob:
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
            if ch == "," and depth == 0:
                decls.append("".join(buf))
                buf = []
            else:
                buf.append(ch)
        if buf:
            decls.append("".join(buf))

        for d in decls:
            parsed = _parse_column_decl(d)
            if parsed is None:
                continue
            cname, ctype, nullable = parsed
            cols[cname] = {
                "type": sql_to_logical_type(ctype),
                "nullable": nullable,
            }

        tables[tname] = {
            "primary_key": [],
            "columns": cols,
            "indexes": [],
            "source_file": source_label,
        }

    for m in _ADD_PK_RE.finditer(sql):
        tname, cols = m.group(1), m.group(2)
        if tname in tables:
            tables[tname]["primary_key"] = [c.strip() for c in cols.split(",")]

    for m in _ADD_FK_RE.finditer(sql):
        src_t, src_c, tgt_t, tgt_c = m.group(1), m.group(2).strip(), m.group(3), m.group(4).strip()
        if src_t in tables and src_c in tables[src_t]["columns"]:
            tables[src_t]["columns"][src_c]["fk"] = f"{tgt_t}.{tgt_c}"

    for m in _CREATE_INDEX_RE.finditer(sql):
        unique = bool(m.group(1))
        iname, tname, cols = m.group(2), m.group(3), m.group(4)
        if tname in tables:
            tables[tname]["indexes"].append({
                "name": iname,
                "cols": [c.strip() for c in cols.split(",")],
                "unique": unique,
            })

    return {"sources": {"rdb": {"type": "postgresql", "tables": tables}}}


def dump_to_sql(dump_path: pathlib.Path) -> str:
    """Run pg_restore -s on a custom-format dump and return SQL text."""
    if not dump_path.exists():
        raise ExtractError(f"missing rdb dump file: {dump_path}")
    try:
        result = subprocess.run(
            ["pg_restore", "--schema-only", "--no-owner", "--no-privileges", str(dump_path)],
            capture_output=True, check=True, text=True,
        )
    except FileNotFoundError:
        raise ExtractError("pg_restore not found on PATH")
    except subprocess.CalledProcessError as e:
        raise ExtractError(f"pg_restore failed on {dump_path}: {e.stderr}")
    return result.stdout


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True, help="output partial YAML path")
    ap.add_argument("dumps", nargs="+", help="paths to pg_dump custom-format files")
    args = ap.parse_args()

    merged_tables = {}
    for d in args.dumps:
        path = pathlib.Path(d)
        sql = dump_to_sql(path)
        partial = parse_sql_to_partial(sql, source_label=str(path))
        for tname, tbl in partial["sources"]["rdb"]["tables"].items():
            if tname in merged_tables:
                raise ExtractError(f"duplicate rdb table within source: {tname}")
            merged_tables[tname] = tbl

    write_yaml(args.out, {"sources": {"rdb": {"type": "postgresql", "tables": merged_tables}}})


if __name__ == "__main__":
    try:
        main()
    except ExtractError as e:
        print(f"extract_rdb: ERROR: {e}", file=sys.stderr)
        sys.exit(1)
```

- [ ] **Step 5: Run tests, verify pass**

```
cd cnc/tools/schema-extract && python -m pytest tests/test_extract_rdb.py -v
```

Expected: 5 passed.

- [ ] **Step 6: Commit**

```bash
git add cnc/tools/schema-extract/extract_rdb.py \
        cnc/tools/schema-extract/tests/fixtures/sample.sql \
        cnc/tools/schema-extract/tests/test_extract_rdb.py
git commit -m "feat(schema-extract): add rdb extractor (pg_restore + sqlparse)"
```

---

## Task 3: ctree extractor

**Files:**
- Create: `cnc/tools/schema-extract/extract_ctree.py`
- Create: `cnc/tools/schema-extract/tests/fixtures/sample_db.h`
- Create: `cnc/tools/schema-extract/tests/test_extract_ctree.py`

- [ ] **Step 1: Write the fixture header**

`tests/fixtures/sample_db.h`:

```c
#ifndef SAMPLE_DB_H
#define SAMPLE_DB_H

#define TID_SIZE 20

struct sample_rec {
    short del_flag;
    int   neid;
    char  tid[TID_SIZE];
    long  x;
    long  y;
};
typedef struct sample_rec SAMPLE_REC;

#endif
```

- [ ] **Step 2: Write failing tests**

`tests/test_extract_ctree.py`:

```python
import pathlib
import pytest
from extract_ctree import parse_header_to_partial

FIXTURE_DIR = pathlib.Path(__file__).parent / "fixtures"


def test_extracts_record_struct():
    out = parse_header_to_partial(
        FIXTURE_DIR / "sample_db.h",
        record_name_hint="sample_rec",
        source_label="fixtures/sample_db.h",
    )
    tables = out["sources"]["ctree"]["tables"]
    assert "sample_rec" in tables
    cols = tables["sample_rec"]["columns"]
    assert cols["del_flag"]["type"] == "short"
    assert cols["neid"]["type"] == "int"
    assert cols["tid"]["type"].startswith("char[")
    assert cols["x"]["type"] == "long"


def test_field_offsets_are_present():
    out = parse_header_to_partial(
        FIXTURE_DIR / "sample_db.h",
        record_name_hint="sample_rec",
        source_label="fixtures/sample_db.h",
    )
    cols = out["sources"]["ctree"]["tables"]["sample_rec"]["columns"]
    assert cols["del_flag"]["offset"] == 0
    assert cols["neid"]["offset"] >= cols["del_flag"]["offset"]


def test_source_file_recorded():
    out = parse_header_to_partial(
        FIXTURE_DIR / "sample_db.h",
        record_name_hint="sample_rec",
        source_label="fixtures/sample_db.h",
    )
    t = out["sources"]["ctree"]["tables"]["sample_rec"]
    assert t["source_file"] == "fixtures/sample_db.h"


def test_missing_header_raises():
    from common import ExtractError
    with pytest.raises(ExtractError):
        parse_header_to_partial(
            FIXTURE_DIR / "no_such.h",
            record_name_hint="x",
            source_label="x",
        )
```

- [ ] **Step 3: Run, verify failure**

```
cd cnc/tools/schema-extract && python -m pytest tests/test_extract_ctree.py -v
```

Expected: ImportError.

- [ ] **Step 4: Implement `extract_ctree.py`**

```python
"""Extract ctree (c-tree ISAM) record schemas into a partial inventory.yaml.

Strategy:
  * Read cnc/utility/src/alldbs.c, collect the ordered list of #include "<x>_db.h".
  * For each header, run libclang over it (with project include paths) and
    capture every struct ending in '_rec' as a ctree record.
  * Field type/offset extracted from libclang AST.
"""
import argparse
import pathlib
import re
import sys

from typing import List, Optional

import clang.cindex
from clang.cindex import CursorKind

from common import ExtractError, c_to_logical_type, write_yaml


_INCLUDE_RE = re.compile(r'#include\s*[<"](\w+_db\w*\.h)[>"]')


def parse_alldbs_includes(alldbs_path: pathlib.Path) -> List[str]:
    """Return ordered list of <something>_db<...>.h header names from alldbs.c."""
    if not alldbs_path.exists():
        raise ExtractError(f"missing alldbs.c: {alldbs_path}")
    seen = []
    for line in alldbs_path.read_text().splitlines():
        m = _INCLUDE_RE.search(line)
        if m:
            h = m.group(1)
            if h not in seen:
                seen.append(h)
    return seen


def _walk_struct(cursor, parent_offset_bits=0):
    """Yield (field_name, c_type_spelling, offset_bytes) for record fields."""
    for child in cursor.get_children():
        if child.kind != CursorKind.FIELD_DECL:
            continue
        try:
            off_bits = child.get_field_offsetof()
        except Exception:
            off_bits = -1
        offset = off_bits // 8 if off_bits >= 0 else None
        yield child.spelling, child.type.spelling, offset


def parse_header_to_partial(
    header_path: pathlib.Path,
    record_name_hint: Optional[str] = None,
    source_label: Optional[str] = None,
    include_paths: Optional[List[str]] = None,
) -> dict:
    """Parse a ctree DB header. Capture all struct decls ending in '_rec'.

    If record_name_hint is set, only that struct is captured (used by tests
    so the fixture does not need the project's full include tree).
    """
    if not header_path.exists():
        raise ExtractError(f"missing ctree header: {header_path}")

    args = ["-x", "c", "-std=c99"]
    for ip in include_paths or []:
        args.extend(["-I", ip])
    # Allow tests to use minimal headers by ignoring missing system includes:
    args.append("-Wno-everything")

    index = clang.cindex.Index.create()
    tu = index.parse(str(header_path), args=args)
    if not tu:
        raise ExtractError(f"libclang failed to parse {header_path}")

    tables = {}
    for cur in tu.cursor.walk_preorder():
        if cur.kind != CursorKind.STRUCT_DECL:
            continue
        if not cur.spelling:
            continue
        if record_name_hint is not None and cur.spelling != record_name_hint:
            continue
        if record_name_hint is None and not cur.spelling.endswith("_rec"):
            continue
        cols = {}
        for fname, ctype, off in _walk_struct(cur):
            entry = {"type": c_to_logical_type(ctype)}
            if off is not None:
                entry["offset"] = off
            cols[fname] = entry
        if not cols:
            continue
        size = cur.type.get_size() if cur.type else None
        record = {
            "primary_key": [],
            "columns": cols,
            "source_file": source_label or str(header_path),
        }
        if size and size > 0:
            record["record_size"] = size
        if cur.spelling in tables:
            raise ExtractError(f"duplicate ctree record within source: {cur.spelling}")
        tables[cur.spelling] = record

    return {"sources": {"ctree": {"type": "c-isam", "tables": tables}}}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--alldbs", required=True, help="path to alldbs.c")
    ap.add_argument("--include-root", action="append", default=[],
                    help="include search path (repeatable)")
    ap.add_argument("--out", required=True, help="output partial YAML path")
    args = ap.parse_args()

    alldbs = pathlib.Path(args.alldbs)
    includes = [pathlib.Path(p) for p in args.include_root]

    headers = parse_alldbs_includes(alldbs)
    merged = {}
    for hname in headers:
        # Resolve via include search paths
        resolved = None
        for root in includes:
            candidate = root / hname
            if candidate.exists():
                resolved = candidate
                break
        if not resolved:
            raise ExtractError(f"header not found in include roots: {hname}")
        partial = parse_header_to_partial(
            resolved,
            source_label=str(resolved),
            include_paths=[str(p) for p in includes],
        )
        for rec_name, rec in partial["sources"]["ctree"]["tables"].items():
            if rec_name in merged:
                raise ExtractError(f"duplicate ctree record across headers: {rec_name}")
            merged[rec_name] = rec

    write_yaml(args.out, {"sources": {"ctree": {"type": "c-isam", "tables": merged}}})


if __name__ == "__main__":
    try:
        main()
    except ExtractError as e:
        print(f"extract_ctree: ERROR: {e}", file=sys.stderr)
        sys.exit(1)
```

- [ ] **Step 5: Run tests, verify pass**

```
cd cnc/tools/schema-extract && python -m pytest tests/test_extract_ctree.py -v
```

Expected: 4 passed. If libclang Python bindings can't find `libclang.so`, set `LIBCLANG_PATH` env var or call `clang.cindex.Config.set_library_file('/usr/lib64/libclang.so')` in `common.py`.

- [ ] **Step 6: Commit**

```bash
git add cnc/tools/schema-extract/extract_ctree.py \
        cnc/tools/schema-extract/tests/fixtures/sample_db.h \
        cnc/tools/schema-extract/tests/test_extract_ctree.py
git commit -m "feat(schema-extract): add ctree extractor (libclang AST)"
```

---

## Task 4: shm extractor

**Files:**
- Create: `cnc/tools/schema-extract/extract_shm.py`
- Create: `cnc/tools/schema-extract/tests/fixtures/sample_d1compool.h`
- Create: `cnc/tools/schema-extract/tests/test_extract_shm.py`

- [ ] **Step 1: Write fixture header**

`tests/fixtures/sample_d1compool.h`:

```c
#ifndef SAMPLE_D1COMPOOL_H
#define SAMPLE_D1COMPOOL_H

#define NeAvail 256

struct frame_link {
    int neid;
    unsigned short ne_feature_1;
    unsigned int  passwd_doit : 1;
    unsigned int  passwd_ip   : 1;
};

struct dacs_port {
    int neid;
    int next_dg;
};

extern struct frame_link *Frmlnk;
extern struct dacs_port  *Dcsprt;

#endif
```

- [ ] **Step 2: Write failing tests**

`tests/test_extract_shm.py`:

```python
import pathlib
import pytest
from extract_shm import parse_d1compool

FIXTURE = pathlib.Path(__file__).parent / "fixtures" / "sample_d1compool.h"


def test_finds_frame_link_array():
    out = parse_d1compool(FIXTURE, source_label="fixtures/sample_d1compool.h")
    arrays = out["sources"]["shm"]["arrays"]
    assert "frame_link" in arrays
    fl = arrays["frame_link"]
    assert fl["element_type"] == "struct frame_link"
    assert fl["capacity"] == "NeAvail"
    assert fl["index_by"] == "neid"
    assert fl["fields"]["neid"]["type"] == "int"
    assert fl["fields"]["ne_feature_1"]["type"] == "ushort"


def test_finds_dacs_port_array():
    out = parse_d1compool(FIXTURE, source_label="fixtures/sample_d1compool.h")
    assert "dacs_port" in out["sources"]["shm"]["arrays"]


def test_records_source_file():
    out = parse_d1compool(FIXTURE, source_label="fixtures/sample_d1compool.h")
    fl = out["sources"]["shm"]["arrays"]["frame_link"]
    assert fl["source_file"] == "fixtures/sample_d1compool.h"


def test_bitfields_captured():
    out = parse_d1compool(FIXTURE, source_label="fixtures/sample_d1compool.h")
    fl = out["sources"]["shm"]["arrays"]["frame_link"]
    assert "passwd_doit" in fl["fields"]
    # Bitfield width annotated
    assert fl["fields"]["passwd_doit"].get("bit_width") == 1


def test_missing_file_raises():
    from common import ExtractError
    with pytest.raises(ExtractError):
        parse_d1compool(pathlib.Path("/no/such.h"), source_label="x")
```

- [ ] **Step 3: Run, verify failure**

```
cd cnc/tools/schema-extract && python -m pytest tests/test_extract_shm.py -v
```

Expected: ImportError.

- [ ] **Step 4: Implement `extract_shm.py`**

```python
"""Extract shm (shared-memory C arrays) into a partial inventory.yaml.

Targets struct decls in include/d1compool.h (frame_link, dacs_port, plus any
sibling extern array pointers declared in the same file). Capacity symbol
(e.g. NeAvail) is captured from the matching extern declaration when known;
otherwise from a project-supplied --capacity-symbol arg.
"""
import argparse
import pathlib
import re
import sys

from typing import List, Optional

import clang.cindex
from clang.cindex import CursorKind

from common import ExtractError, c_to_logical_type, write_yaml


_EXTERN_PTR_RE = re.compile(
    r'extern\s+struct\s+(\w+)\s*\*\s*(\w+)\s*;'
)


def _struct_fields(cur):
    out = {}
    for child in cur.get_children():
        if child.kind != CursorKind.FIELD_DECL:
            continue
        entry = {"type": c_to_logical_type(child.type.spelling)}
        if child.is_bitfield():
            entry["bit_width"] = child.get_bitfield_width()
        out[child.spelling] = entry
    return out


def parse_d1compool(
    header_path: pathlib.Path,
    source_label: Optional[str] = None,
    include_paths: Optional[List[str]] = None,
    capacity_symbol: str = "NeAvail",
    index_by: str = "neid",
) -> dict:
    """Parse a d1compool-style header. Emit shm.arrays for each struct that
    has a matching `extern struct <X> *<Y>;` declaration in the same file."""
    if not header_path.exists():
        raise ExtractError(f"missing shm header: {header_path}")

    text = header_path.read_text()
    extern_arrays = {m.group(1): m.group(2) for m in _EXTERN_PTR_RE.finditer(text)}

    args = ["-x", "c", "-std=c99", "-Wno-everything"]
    for ip in include_paths or []:
        args.extend(["-I", ip])

    index = clang.cindex.Index.create()
    tu = index.parse(str(header_path), args=args)
    if not tu:
        raise ExtractError(f"libclang failed to parse {header_path}")

    arrays = {}
    for cur in tu.cursor.walk_preorder():
        if cur.kind != CursorKind.STRUCT_DECL or not cur.spelling:
            continue
        if cur.spelling not in extern_arrays:
            continue
        fields = _struct_fields(cur)
        if not fields:
            continue
        arrays[cur.spelling] = {
            "element_type": f"struct {cur.spelling}",
            "capacity": capacity_symbol,
            "index_by": index_by,
            "fields": fields,
            "source_file": source_label or str(header_path),
        }

    return {"sources": {"shm": {"type": "shared-memory-array", "arrays": arrays}}}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--header", required=True, help="path to d1compool.h")
    ap.add_argument("--include-root", action="append", default=[])
    ap.add_argument("--out", required=True)
    ap.add_argument("--capacity-symbol", default="NeAvail")
    ap.add_argument("--index-by", default="neid")
    args = ap.parse_args()

    partial = parse_d1compool(
        pathlib.Path(args.header),
        source_label=args.header,
        include_paths=args.include_root,
        capacity_symbol=args.capacity_symbol,
        index_by=args.index_by,
    )
    write_yaml(args.out, partial)


if __name__ == "__main__":
    try:
        main()
    except ExtractError as e:
        print(f"extract_shm: ERROR: {e}", file=sys.stderr)
        sys.exit(1)
```

- [ ] **Step 5: Run tests, verify pass**

```
cd cnc/tools/schema-extract && python -m pytest tests/test_extract_shm.py -v
```

Expected: 5 passed.

- [ ] **Step 6: Commit**

```bash
git add cnc/tools/schema-extract/extract_shm.py \
        cnc/tools/schema-extract/tests/fixtures/sample_d1compool.h \
        cnc/tools/schema-extract/tests/test_extract_shm.py
git commit -m "feat(schema-extract): add shm extractor (frame_link, dacs_port)"
```

---

## Task 5: merge partials into `inventory.yaml`

**Files:**
- Create: `cnc/tools/schema-extract/merge.py`
- Create: `cnc/tools/schema-extract/tests/test_merge.py`

- [ ] **Step 1: Write failing tests**

`tests/test_merge.py`:

```python
import pytest
from merge import merge_partials


def test_merges_three_sources():
    a = {"sources": {"rdb":   {"type": "postgresql",          "tables": {"ne":  {"columns": {}}}}}}
    b = {"sources": {"ctree": {"type": "c-isam",              "tables": {"ne1": {"columns": {}}}}}}
    c = {"sources": {"shm":   {"type": "shared-memory-array", "arrays": {"frame_link": {"fields": {}}}}}}
    out = merge_partials([a, b, c], schema_version="abc123-20260522")
    assert set(out["sources"].keys()) == {"rdb", "ctree", "shm"}
    assert out["schema_version"] == "abc123-20260522"
    assert "generated_at" in out


def test_enums_section_deduped():
    a = {"sources": {"rdb": {"type": "postgresql", "tables": {}}},
         "enums":   {"severity": {"source_file": "a.sql",
                                  "values": [{"name": "MAJOR", "value": 1}]}}}
    b = {"sources": {"ctree": {"type": "c-isam", "tables": {}}},
         "enums":   {"severity": {"source_file": "b.h",
                                  "values": [{"name": "MAJOR", "value": 1}]}}}
    out = merge_partials([a, b], schema_version="x")
    assert "severity" in out["enums"]


def test_collision_within_source_raises():
    from common import ExtractError
    a = {"sources": {"rdb": {"type": "postgresql", "tables": {"ne": {"columns": {}}}}}}
    b = {"sources": {"rdb": {"type": "postgresql", "tables": {"ne": {"columns": {}}}}}}
    with pytest.raises(ExtractError):
        merge_partials([a, b], schema_version="x")
```

- [ ] **Step 2: Run, verify failure**

```
cd cnc/tools/schema-extract && python -m pytest tests/test_merge.py -v
```

Expected: ImportError.

- [ ] **Step 3: Implement `merge.py`**

```python
"""Merge per-source partial YAMLs into the final inventory.yaml."""
import argparse
import datetime
import pathlib
import subprocess
import sys

from typing import List, Optional

import yaml

from common import ExtractError, write_yaml


def _git_short_sha() -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"], text=True
        ).strip()
    except Exception:
        return "nogit"


def _today_stamp() -> str:
    return datetime.datetime.utcnow().strftime("%Y%m%d")


def merge_partials(partials: List[dict], schema_version: Optional[str] = None) -> dict:
    out = {
        "schema_version": schema_version or f"{_git_short_sha()}-{_today_stamp()}",
        "generated_at": datetime.datetime.utcnow().isoformat() + "Z",
        "sources": {},
        "enums": {},
    }
    for p in partials:
        for src_name, src in (p.get("sources") or {}).items():
            if src_name not in out["sources"]:
                out["sources"][src_name] = src
                continue
            # Same source mentioned twice -> merge tables/arrays, fail on dup
            dst = out["sources"][src_name]
            for key in ("tables", "arrays"):
                src_items = src.get(key, {})
                dst_items = dst.setdefault(key, {})
                for name, item in src_items.items():
                    if name in dst_items:
                        raise ExtractError(
                            f"duplicate {key[:-1]} within source {src_name}: {name}"
                        )
                    dst_items[name] = item
        for ename, edef in (p.get("enums") or {}).items():
            existing = out["enums"].get(ename)
            if existing is None:
                out["enums"][ename] = edef
            else:
                # Dedupe by name. If value sets disagree, that's a real conflict.
                existing_vals = {(v["name"], v["value"]) for v in existing["values"]}
                new_vals = {(v["name"], v["value"]) for v in edef["values"]}
                if existing_vals != new_vals:
                    raise ExtractError(f"conflicting enum definitions: {ename}")
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("partials", nargs="+")
    args = ap.parse_args()

    parsed = []
    for p in args.partials:
        with open(p) as f:
            parsed.append(yaml.safe_load(f))
    merged = merge_partials(parsed)
    write_yaml(args.out, merged)


if __name__ == "__main__":
    try:
        main()
    except ExtractError as e:
        print(f"merge: ERROR: {e}", file=sys.stderr)
        sys.exit(1)
```

- [ ] **Step 4: Run tests, verify pass**

```
cd cnc/tools/schema-extract && python -m pytest tests/test_merge.py -v
```

Expected: 3 passed.

- [ ] **Step 5: Commit**

```bash
git add cnc/tools/schema-extract/merge.py \
        cnc/tools/schema-extract/tests/test_merge.py
git commit -m "feat(schema-extract): add merge step (3 partials -> inventory.yaml)"
```

---

## Task 6: seed joins with field-level merge

**Files:**
- Create: `cnc/tools/schema-extract/seed_joins.py`
- Create: `cnc/tools/schema-extract/tests/test_seed_joins.py`

- [ ] **Step 1: Write failing tests**

`tests/test_seed_joins.py`:

```python
import pytest
from seed_joins import seed_joins_yaml


INVENTORY = {
    "sources": {
        "rdb": {"type": "postgresql", "tables": {
            "ne":  {"primary_key": ["neid"], "columns": {"neid": {"type": "int"}, "ne_name": {"type": "varchar(64)"}}},
            "alm": {"primary_key": ["alm_id"], "columns": {"alm_id": {"type": "int64"}, "neid": {"type": "int", "fk": "ne.neid"}}},
        }},
        "ctree": {"type": "c-isam", "tables": {
            "ne1": {"primary_key": ["neid"], "columns": {"neid": {"type": "int"}}},
        }},
        "shm": {"type": "shared-memory-array", "arrays": {
            "frame_link": {"index_by": "neid", "fields": {"neid": {"type": "int"}}},
        }},
    },
}


def test_seeds_appearances_from_name_match():
    out = seed_joins_yaml(INVENTORY, existing=None)
    appearances = out["keys"]["neid"]["appearances"]
    sources = {(a["source"], a.get("table") or a.get("array")) for a in appearances}
    assert ("rdb", "ne") in sources
    assert ("rdb", "alm") in sources
    assert ("ctree", "ne1") in sources
    assert ("shm", "frame_link") in sources


def test_pk_and_array_index_roles():
    out = seed_joins_yaml(INVENTORY, existing=None)
    by_table = {(a["source"], a.get("table") or a.get("array")): a["role"]
                for a in out["keys"]["neid"]["appearances"]}
    assert by_table[("rdb", "ne")]   == "pk"
    assert by_table[("ctree", "ne1")] == "pk"
    assert by_table[("shm", "frame_link")] == "array_index"
    assert by_table[("rdb", "alm")].startswith("fk -> ")


def test_preserves_hand_edited_semantic():
    existing = {
        "keys": {
            "neid": {
                "semantic": "Network element identifier",
                "type": "int",
                "appearances": [
                    {"source": "rdb", "table": "ne", "column": "neid", "role": "pk"},
                ],
            }
        },
        "cross_source_paths": [
            {"name": "NE -> alarms", "hops": ["rdb.ne.neid", "rdb.alm.neid"]},
        ],
    }
    out = seed_joins_yaml(INVENTORY, existing=existing)
    assert out["keys"]["neid"]["semantic"] == "Network element identifier"
    assert out["cross_source_paths"] == existing["cross_source_paths"]


def test_orphans_emitted_for_removed_keys():
    existing = {
        "keys": {
            "gone_key": {"semantic": "removed", "type": "int", "appearances": []},
        }
    }
    out = seed_joins_yaml(INVENTORY, existing=existing)
    assert "gone_key" in out["orphans"]
    assert "gone_key" not in out["keys"]


def test_role_override_preserved():
    existing = {
        "keys": {
            "neid": {
                "semantic": "NE id",
                "type": "int",
                "appearances": [
                    {"source": "rdb", "table": "alm", "column": "neid", "role": "fk -> ne.neid (explicit)"},
                ],
            }
        }
    }
    out = seed_joins_yaml(INVENTORY, existing=existing)
    by_table = {(a["source"], a.get("table") or a.get("array")): a["role"]
                for a in out["keys"]["neid"]["appearances"]}
    assert by_table[("rdb", "alm")] == "fk -> ne.neid (explicit)"
```

- [ ] **Step 2: Run, verify failure**

```
cd cnc/tools/schema-extract && python -m pytest tests/test_seed_joins.py -v
```

Expected: ImportError.

- [ ] **Step 3: Implement `seed_joins.py`**

```python
"""Seed joins.yaml from inventory.yaml. Field-merged: preserves hand edits.

Rules:
  * `appearances` is regenerated from inventory each run (with role inferred).
  * Existing `appearances[*].role` overrides survive if the user added text
    beyond the canonical role (e.g. 'fk -> ne.neid (explicit)').
  * Existing `keys[K].semantic` and `keys[K].type` overrides are preserved.
  * Existing `cross_source_paths` are preserved verbatim.
  * Keys no longer in inventory move from `keys` to `orphans`.
"""
import argparse
import pathlib
import sys

from typing import Optional

import yaml

from common import ExtractError, write_yaml


def _collect_appearances(inventory: dict) -> dict:
    """Return {key_name: [appearance, ...]} from name match across sources."""
    appearances: dict = {}

    rdb_tables = inventory.get("sources", {}).get("rdb", {}).get("tables", {})
    for tname, tbl in rdb_tables.items():
        pk = set(tbl.get("primary_key") or [])
        for col, meta in (tbl.get("columns") or {}).items():
            if col in pk:
                role = "pk"
            elif "fk" in meta:
                role = f"fk -> {meta['fk']}"
            else:
                role = "field"
            appearances.setdefault(col, []).append(
                {"source": "rdb", "table": tname, "column": col, "role": role}
            )

    ctree_tables = inventory.get("sources", {}).get("ctree", {}).get("tables", {})
    for tname, tbl in ctree_tables.items():
        pk = set(tbl.get("primary_key") or [])
        for col in (tbl.get("columns") or {}):
            role = "pk" if col in pk else "field"
            appearances.setdefault(col, []).append(
                {"source": "ctree", "table": tname, "column": col, "role": role}
            )

    shm_arrays = inventory.get("sources", {}).get("shm", {}).get("arrays", {})
    for aname, arr in shm_arrays.items():
        idx = arr.get("index_by")
        for fname in (arr.get("fields") or {}):
            role = "array_index" if fname == idx else "field"
            appearances.setdefault(fname, []).append(
                {"source": "shm", "array": aname, "column": fname, "role": role}
            )

    return appearances


def _infer_type(inventory: dict, key: str) -> Optional[str]:
    """Best-effort type pull: first match wins."""
    for src in inventory.get("sources", {}).values():
        for tbl in (src.get("tables") or {}).values():
            col = (tbl.get("columns") or {}).get(key)
            if col and "type" in col:
                return col["type"]
        for arr in (src.get("arrays") or {}).values():
            fld = (arr.get("fields") or {}).get(key)
            if fld and "type" in fld:
                return fld["type"]
    return None


def _appearance_id(a: dict) -> tuple:
    return (a["source"], a.get("table") or a.get("array"), a["column"])


def _merge_appearance(existing: dict, fresh: dict) -> dict:
    """Preserve role override if existing role string extends the canonical role."""
    out = dict(fresh)
    if existing.get("role") and existing["role"] != fresh["role"]:
        out["role"] = existing["role"]
    return out


def seed_joins_yaml(inventory: dict, existing: Optional[dict]) -> dict:
    """Produce a new joins.yaml dict honouring hand edits in `existing`."""
    fresh_appearances = _collect_appearances(inventory)
    existing = existing or {}
    existing_keys = existing.get("keys", {})

    new_keys = {}
    for key, fresh_aps in fresh_appearances.items():
        prev = existing_keys.get(key, {})
        prev_aps = {_appearance_id(a): a for a in prev.get("appearances", [])}
        merged_aps = []
        for fa in fresh_aps:
            pid = _appearance_id(fa)
            merged_aps.append(_merge_appearance(prev_aps.get(pid, {}), fa))
        new_keys[key] = {
            "semantic": prev.get("semantic", ""),
            "type": prev.get("type") or _infer_type(inventory, key) or "unknown",
            "appearances": merged_aps,
        }

    orphans = {
        k: v for k, v in existing_keys.items()
        if k not in fresh_appearances
    }

    return {
        "keys": new_keys,
        "orphans": orphans,
        "cross_source_paths": existing.get("cross_source_paths", []),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--inventory", required=True)
    ap.add_argument("--existing", default=None,
                    help="existing joins.yaml (preserved hand edits)")
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    with open(args.inventory) as f:
        inv = yaml.safe_load(f)
    existing = None
    if args.existing and pathlib.Path(args.existing).exists():
        with open(args.existing) as f:
            existing = yaml.safe_load(f)

    write_yaml(args.out, seed_joins_yaml(inv, existing))


if __name__ == "__main__":
    try:
        main()
    except ExtractError as e:
        print(f"seed_joins: ERROR: {e}", file=sys.stderr)
        sys.exit(1)
```

- [ ] **Step 4: Run tests, verify pass**

```
cd cnc/tools/schema-extract && python -m pytest tests/test_seed_joins.py -v
```

Expected: 5 passed.

- [ ] **Step 5: Commit**

```bash
git add cnc/tools/schema-extract/seed_joins.py \
        cnc/tools/schema-extract/tests/test_seed_joins.py
git commit -m "feat(schema-extract): add joins seeder with field-merge"
```

---

## Task 7: nmake driver

**Files:**
- Create: `cnc/tools/schema-extract/extract.mk`
- Create: `cnc/tools/schema-extract/README.md`

- [ ] **Step 1: Inspect existing nmake patterns**

Look at any existing `cnc/tools/**/*.mk` and `cnc/utility/util.mk` for include conventions:

```
ls cnc/tools/*/*.mk 2>/dev/null
ls cnc/utility/*.mk
```

- [ ] **Step 2: Write `extract.mk`**

```make
:PACKAGE: PYTHON

OUTDIR        = $(BASE)/docs/data-schema
RDB_DUMPS     = $(BASE)/cnc/rdb/schema/inc_schema $(BASE)/cnc/rdb/schema/otn_port_schema
ALLDBS_C      = $(BASE)/cnc/utility/src/alldbs.c
D1COMPOOL_H   = $(BASE)/include/d1compool.h
INC_ROOTS     = $(BASE)/include $(BASE)/common/include

PARTIALS      = rdb.partial.yaml ctree.partial.yaml shm.partial.yaml

all : inventory joins

inventory : $(OUTDIR)/inventory.yaml

joins : $(OUTDIR)/joins.yaml

$(OUTDIR)/inventory.yaml : $(PARTIALS)
        python merge.py --out $(@) $(PARTIALS)

rdb.partial.yaml : $(RDB_DUMPS)
        python extract_rdb.py --out $(@) $(RDB_DUMPS)

ctree.partial.yaml : $(ALLDBS_C)
        python extract_ctree.py --alldbs $(ALLDBS_C) \
                $(INC_ROOTS:%=--include-root %) --out $(@)

shm.partial.yaml : $(D1COMPOOL_H)
        python extract_shm.py --header $(D1COMPOOL_H) \
                $(INC_ROOTS:%=--include-root %) --out $(@)

$(OUTDIR)/joins.yaml : $(OUTDIR)/inventory.yaml
        python seed_joins.py --inventory $(<) \
                --existing $(@) --out $(@)

clean :
        rm -f $(PARTIALS)
```

If the syntax above does not match what the project's other `.mk` files do, copy the structure from a sibling and adapt — the `writing-nmake-makefiles` skill is the authority.

- [ ] **Step 3: Write `cnc/tools/schema-extract/README.md`**

```markdown
# schema-extract

Generates the data-schema artifacts consumed by report-writing skill.

## Prereqs

* Python 3.6+
* `pg_restore` on PATH
* libclang installed (`dnf install clang-libs` on RHEL 8)
* `pip install -r requirements.txt`

## Regen

```
cd cnc/tools/schema-extract
nmake -f extract.mk
```

Outputs to `docs/data-schema/inventory.yaml` and `docs/data-schema/joins.yaml`.
`joins.yaml` hand-edits (`semantic`, role overrides, `cross_source_paths`)
are preserved by field-level merge; keys missing from the new inventory
move to the `orphans:` block for review.

## Tests

```
python -m pytest tests/ -v
```
```

- [ ] **Step 4: Commit**

```bash
git add cnc/tools/schema-extract/extract.mk \
        cnc/tools/schema-extract/README.md
git commit -m "feat(schema-extract): add nmake driver and README"
```

---

## Task 8: snapshot test

**Files:**
- Create: `cnc/tools/schema-extract/snapshot_test.py`

- [ ] **Step 1: Implement snapshot test**

```python
"""Snapshot test: regen artifacts into a temp dir and diff against committed.

Exit non-zero if drift is detected. Intended to run in CI after a real
extraction has been committed.
"""
import difflib
import pathlib
import shutil
import subprocess
import sys
import tempfile

REPO = pathlib.Path(__file__).resolve().parents[3]
EXTRACT_DIR = REPO / "cnc/tools/schema-extract"
COMMITTED = REPO / "docs/data-schema"


def regen_into(tmpdir: pathlib.Path):
    # Reproduce what extract.mk does, but write outputs into tmpdir
    subprocess.run([
        "python", str(EXTRACT_DIR / "extract_rdb.py"),
        "--out", str(tmpdir / "rdb.partial.yaml"),
        str(REPO / "cnc/rdb/schema/inc_schema"),
        str(REPO / "cnc/rdb/schema/otn_port_schema"),
    ], check=True)
    subprocess.run([
        "python", str(EXTRACT_DIR / "extract_ctree.py"),
        "--alldbs", str(REPO / "cnc/utility/src/alldbs.c"),
        "--include-root", str(REPO / "include"),
        "--include-root", str(REPO / "common/include"),
        "--out", str(tmpdir / "ctree.partial.yaml"),
    ], check=True)
    subprocess.run([
        "python", str(EXTRACT_DIR / "extract_shm.py"),
        "--header", str(REPO / "include/d1compool.h"),
        "--include-root", str(REPO / "include"),
        "--out", str(tmpdir / "shm.partial.yaml"),
    ], check=True)
    subprocess.run([
        "python", str(EXTRACT_DIR / "merge.py"),
        "--out", str(tmpdir / "inventory.yaml"),
        str(tmpdir / "rdb.partial.yaml"),
        str(tmpdir / "ctree.partial.yaml"),
        str(tmpdir / "shm.partial.yaml"),
    ], check=True)
    subprocess.run([
        "python", str(EXTRACT_DIR / "seed_joins.py"),
        "--inventory", str(tmpdir / "inventory.yaml"),
        "--existing", str(COMMITTED / "joins.yaml"),
        "--out", str(tmpdir / "joins.yaml"),
    ], check=True)


def diff_file(a: pathlib.Path, b: pathlib.Path) -> str:
    if not b.exists():
        return f"missing committed file: {b}"
    diff = difflib.unified_diff(
        a.read_text().splitlines(keepends=True),
        b.read_text().splitlines(keepends=True),
        fromfile=str(a), tofile=str(b),
    )
    return "".join(diff)


def main():
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="schema-snapshot-"))
    try:
        # Strip generated_at line before diff so timestamp doesn't churn
        regen_into(tmp)
        bad = []
        for fname in ("inventory.yaml", "joins.yaml"):
            fresh = tmp / fname
            committed = COMMITTED / fname
            d = diff_file(fresh, committed)
            if d:
                # Filter out generated_at line differences
                lines = [l for l in d.splitlines() if "generated_at" not in l and "schema_version" not in l]
                if any(l.startswith(("+", "-")) and not l.startswith(("+++", "---")) for l in lines):
                    bad.append((fname, "\n".join(lines)))
        if bad:
            for name, d in bad:
                print(f"=== drift in {name} ===")
                print(d)
            sys.exit(1)
        print("snapshot OK")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Commit**

```bash
git add cnc/tools/schema-extract/snapshot_test.py
git commit -m "feat(schema-extract): add snapshot test for CI"
```

---

## Task 9: First real extraction run

**Files:**
- Create: `docs/data-schema/inventory.yaml`
- Create: `docs/data-schema/joins.yaml`

- [ ] **Step 1: Set up `BASE` and `VPATH` per repo convention**

```bash
export BASE=$(git rev-parse --show-toplevel)
export VPATH=$BASE:$VPATH
```

- [ ] **Step 2: Install Python deps**

```bash
cd cnc/tools/schema-extract
pip install -r requirements.txt
```

- [ ] **Step 3: Run the pipeline**

```bash
cd cnc/tools/schema-extract
nmake -f extract.mk
```

Expected output: `docs/data-schema/inventory.yaml` and `docs/data-schema/joins.yaml` created/updated.

- [ ] **Step 4: Spot-check the output**

```bash
yq '.sources.rdb.tables | keys | length' docs/data-schema/inventory.yaml
yq '.sources.ctree.tables | keys | length' docs/data-schema/inventory.yaml
yq '.sources.shm.arrays | keys' docs/data-schema/inventory.yaml
yq '.keys.neid.appearances' docs/data-schema/joins.yaml
```

Expected:
- rdb table count > 50 (the schema is large)
- ctree table count > 100 (alldbs.c includes ~120+ headers)
- shm arrays should list `frame_link`, `dacs_port`
- `neid` appearances should span all three sources

- [ ] **Step 5: Hand-edit `joins.yaml` for critical keys**

Add `semantic` text and any FK direction overrides for at least:
`neid`, `ckt_id`, `aid`, `port_id`, `tid`, `slot`, `card_id`, `alm_id`.

Add `cross_source_paths` recipes for the report types listed in §1 of the spec, e.g.:

```yaml
cross_source_paths:
  - name: "NE -> alarms"
    hops: [rdb.ne.neid, rdb.alm.neid]
  - name: "NE -> shm liveness"
    hops: [rdb.ne.neid, shm.frame_link.neid]
  - name: "Circuit -> SNC -> route hops"
    hops: [rdb.ckt_info.ckt_id, rdb.snc.ckt_id, rdb.wdm_snc_route.snc_id, rdb.wdm_snc_route_hop.route_id]
```

- [ ] **Step 6: Re-run seed_joins to verify hand edits survive**

```bash
cd cnc/tools/schema-extract
nmake -f extract.mk joins
```

Expected: `git diff docs/data-schema/joins.yaml` shows your `semantic` and `cross_source_paths` still in place; only auto-fields may change.

- [ ] **Step 7: Commit**

```bash
git add docs/data-schema/inventory.yaml docs/data-schema/joins.yaml
git commit -m "docs(data-schema): initial inventory.yaml and joins.yaml"
```

---

## Task 10: semantics.md — Network elements & topology

**Files:**
- Create: `docs/data-schema/semantics.md`

- [ ] **Step 1: Create the file with the four group headings**

```markdown
# Data Semantics

This document gives prose semantics for entities the report-writing skill is
expected to reason about. Structural information lives in `inventory.yaml`;
this file adds *meaning*. Skill keys off `### Entity:` headings and
`**Sources:**` lines to cross-reference inventory.

## Network elements & topology

## Circuits & services

## Alarms & events

## PM & metrics
```

- [ ] **Step 2: Populate the Network elements & topology section**

For each entity below, write a section using the exact sub-structure shown in
spec §5.3:

```
### Entity: <Name>
**Sources:** `<source>.<table_or_array>`, ...

**Purpose.** ...

**Lifecycle.** ...

**Key fields (semantic).**
- `<field>` — ...

**Gotchas.** ...
```

Entities to cover in this section:
- Network Element (NE) — `rdb.ne`, `ctree.ne1`, `shm.frame_link[neid]`
- Frame Link — `shm.frame_link`
- DACS Port — `shm.dacs_port`
- NE Network — `rdb.ne_ntwk`
- Link — `rdb.linkdb`
- Protect Group — `rdb.protect_group`

Pull SME knowledge from existing field comments in `include/d1compool.h`, code
in `cnc/dno/`, and any operator-facing docs the team has. Where a field has
non-obvious meaning (e.g. `frame_link.next_dg` is a packed bitfield index,
not a chronological "next"), write the gotcha out explicitly.

- [ ] **Step 3: Cross-check against `inventory.yaml`**

For each `### Entity:` section, verify every name in its `**Sources:**` line
exists in `inventory.yaml`. Mismatches are a sign the inventory drifted or
the section spelled a name wrong.

- [ ] **Step 4: Commit**

```bash
git add docs/data-schema/semantics.md
git commit -m "docs(data-schema): add semantics for NE & topology entities"
```

---

## Task 11: semantics.md — Circuits & services

**Files:**
- Modify: `docs/data-schema/semantics.md`

- [ ] **Step 1: Populate the Circuits & services section**

Same sub-structure as Task 10. Entities:
- Circuit — `rdb.ckt_info`, `rdb.ckt_lifecyc`
- SNC (Sub-Network Connection) — `rdb.snc`, `rdb.wdm_snc`, `rdb.wdm_snc_route`
- Service — `rdb.svc`, `rdb.eth_svc`
- CTP (Connection Termination Point) — `rdb.ctp`
- TIE / Indirect TIE — `rdb.tie`, `rdb.indirect_tie`
- Eth Xcon — `rdb.eth_xcon`
- LSP Tunnel / MPLS PW — `rdb.lsp_tunnel`, `rdb.mpls_pw`

For each, explain at minimum:
- What state field marks in-service vs OOS (and the enum values that mean each).
- How the entity references NEs (which column holds `neid` or `aid`).
- Whether the entity is hierarchical (e.g. SNC → route → route hop) and how
  the levels link.

- [ ] **Step 2: Cross-check against `inventory.yaml`** (same procedure as Task 10 Step 3)

- [ ] **Step 3: Commit**

```bash
git add docs/data-schema/semantics.md
git commit -m "docs(data-schema): add semantics for circuit & service entities"
```

---

## Task 12: semantics.md — Alarms & events

**Files:**
- Modify: `docs/data-schema/semantics.md`

- [ ] **Step 1: Populate the Alarms & events section**

Entities:
- Alarm — `rdb.alm`, `rdb.almhist`
- Alarm Map — `rdb.alm_map`
- Fallout — `rdb.fallout`
- Event (`eventd` daemon's persisted records, if any in rdb)

For each, explain:
- Severity field + enum values.
- Active vs cleared (typical `alm` is active; `almhist` is history).
- Linkage to source NE (`neid`), source object (`aid`), and originating alarm
  template (`alm_map_id` or similar).
- Retention policy if known.

- [ ] **Step 2: Cross-check against `inventory.yaml`**

- [ ] **Step 3: Commit**

```bash
git add docs/data-schema/semantics.md
git commit -m "docs(data-schema): add semantics for alarm & event entities"
```

---

## Task 13: semantics.md — PM & metrics

**Files:**
- Modify: `docs/data-schema/semantics.md`

- [ ] **Step 1: Populate the PM & metrics section**

Entities:
- PM 24-hour bin — `rdb.probpm24`
- PM Schedule — `rdb.schedpm`
- Monitor Type — `rdb.montype`
- Statistics — `rdb.stats`
- Light Level Threshold — `rdb.lightlvlthr`

For each, explain:
- Bin granularity (15-min, 1-hr, 24-hr).
- How counters reset and when.
- Threshold-crossing semantics (`tca` or similar) and how thresholds relate
  to alarms (cross-reference Alarms section).
- Linkage to the monitored object (`neid`, `aid`, port_id).

- [ ] **Step 2: Cross-check against `inventory.yaml`**

- [ ] **Step 3: Commit**

```bash
git add docs/data-schema/semantics.md
git commit -m "docs(data-schema): add semantics for PM & metrics entities"
```

---

## Task 14: skill-consumption README

**Files:**
- Create: `docs/data-schema/README.md`

- [ ] **Step 1: Write the README**

```markdown
# Data Schema Documentation

Source of truth for *where* dynamic data lives in netFLEX and *what it means*.

## Files

| File             | Role                                                       | Generated? |
|------------------|------------------------------------------------------------|------------|
| `inventory.yaml` | Every table/struct/field across rdb, ctree, shm            | yes        |
| `joins.yaml`     | Cross-source key map; seeded auto, hand-extended           | partial    |
| `semantics.md`   | Prose meaning for the four critical entity groups          | no         |

## How a report-writing skill should consume these

1. **Load order.** `inventory.yaml` → `joins.yaml` → `semantics.md`. First
   two parsed as YAML; third chunked by `###` headings.
2. **Entity resolution.** Given an entity name, look it up in `inventory.yaml`
   across all three sources, then pull the matching `### Entity:` section
   from `semantics.md` (if any), then pull all keys from `joins.yaml` whose
   appearances include the entity's tables/arrays.
3. **Cross-source assembly.** For multi-source reports, either pick a named
   recipe from `joins.yaml.cross_source_paths` or trace hops through
   `joins.yaml.keys` between requested entities.
4. **Field-level answers.** `inventory.yaml` provides type/nullable/key info.
   `semantics.md` adds prose only for the four curated groups. For
   auto-only entities, answer structurally and label the response
   "no curated semantics; see `<source_file>`."
5. **Cite the version.** Every generated report MUST include the
   `schema_version` from `inventory.yaml` (e.g. "schema: abc123-20260522")
   so stale extracts are visible.
6. **Don't invent fields.** Never reference a field that does not appear in
   `inventory.yaml`. Mark inferred joins as "inferred" when they are not
   present in `joins.yaml.keys[*].appearances` or
   `joins.yaml.cross_source_paths`.

## Regen

See `cnc/tools/schema-extract/README.md`.
```

- [ ] **Step 2: Commit**

```bash
git add docs/data-schema/README.md
git commit -m "docs(data-schema): add skill consumption contract README"
```

---

## Task 15: CI snapshot hook (optional but recommended)

**Files:**
- Modify: the repo's CI config (`.github/workflows/*.yml` or equivalent)

- [ ] **Step 1: Add a CI job that runs the snapshot test**

```yaml
- name: schema-extract snapshot
  run: |
    pip install -r cnc/tools/schema-extract/requirements.txt
    python cnc/tools/schema-extract/snapshot_test.py
```

This requires `pg_restore` and libclang to be present in the CI image; if
they are not, install them before the run step (`apt-get install postgresql-client libclang-14-dev` or distro equivalent).

- [ ] **Step 2: Commit**

```bash
git add .github/workflows/<filename>
git commit -m "ci: enforce schema-extract snapshot test"
```

---

## Self-review notes (filled in during writing)

**Spec coverage map:**

| Spec section                    | Implemented by             |
|---------------------------------|----------------------------|
| §2 sources in scope             | Tasks 2, 3, 4               |
| §3 tiered generation strategy   | Tasks 2-6 (auto), 10-13 (curated) |
| §4 output artifacts             | Tasks 5, 6, 9, 10-13, 14    |
| §5.1 inventory.yaml schema      | Tasks 2, 3, 4, 5            |
| §5.2 joins.yaml schema          | Task 6                      |
| §5.3 semantics.md structure     | Tasks 10-13                 |
| §6 extractor pipeline           | Task 7                      |
| §6.1 error handling             | Task 1 (`ExtractError`) + raises in each extractor |
| §6.2 testing                    | Tasks 1-6 (unit), 8 (snapshot) |
| §7 skill integration contract   | Task 14                     |
| §9 acceptance criteria          | Tasks 9, 10-13, 14          |

Acceptance criteria reach: regen pipeline + snapshot test (T7, T8), `inventory.yaml` content (T9), `joins.yaml` semantic + role + cross-source-paths (T9 step 5), `semantics.md` complete for the four groups (T10-13), README load contract (T14).
