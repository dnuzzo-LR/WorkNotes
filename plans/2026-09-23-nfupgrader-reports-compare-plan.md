# Reports Before/After Compare Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Port nf-install's report manager into nfupgrader as a Reports tab: snapshots of 21 netFLEX reports taken automatically before/after an Upgrade launch (and manually), with a per-report side-by-side/unified diff.

**Architecture:** `reports.py` (pure-ish: snapshot files + manifest, listing/pruning, compare, diff rows via `difflib`), `snapjobs.py` (one-at-a-time background `SnapshotRunner`, upgrade watcher, before→launch→after sequencing), API routes in `api.py`, launch wiring in `httpd.py`, a new tab in `web/`.

**Tech Stack:** Python 3.6.8 stdlib only (no f-strings, `.format()`; `unittest`, `unittest.mock`), ES5 JavaScript, static HTML/CSS, no external resources.

Spec: `~/WorkNotes/design/2026-09-23-nfupgrader-reports-compare-design.md`
Repo: `~/Git/nfupgrader`. Test command (used throughout):
`cd ~/Git/nfupgrader && python3 -m unittest discover -s tests -p 'test_*.py' 2>&1 | tail -3`
Baseline: 135 tests OK. Ignore BASE/VPATH warnings — this repo does not use nmake.

---

## File map

| File | Change | Responsibility |
|---|---|---|
| `nfupgrader/reports.default.json` | create | the 21 report definitions (from nf-install) |
| `nfupgrader/reports.py` | create | snapshot write/read/list/prune, compare, diff rows |
| `nfupgrader/snapjobs.py` | create | `SnapshotRunner`, `Busy`, `watch_upgrade`, `begin_upgrade` |
| `nfupgrader/api.py` | modify | Context fields, `/api/reports*` routes, launch 409 while busy |
| `nfupgrader/httpd.py` | modify | `perform_launch`, `upgrade_status`, `launch_side_effect`; runner wiring in `main()` |
| `nfupgrader/web/index.html`, `app.js`, `style.css` | modify | Reports tab |
| `tests/fixtures/reports.test.json` | create | 2-report fixture (+1 function entry to be skipped) |
| `tests/test_reports.py`, `tests/test_snapjobs.py` | create | unit tests |
| `tests/test_api.py`, `tests/test_httpd.py`, `tests/test_web_assets.py` | modify | tests |
| `README.md` | modify | document `NFU_REPORTS` + Reports tab |

---

### Task 1: Report definitions + snapshot storage (`reports.py` part 1)

**Files:**
- Create: `nfupgrader/reports.default.json`, `nfupgrader/reports.py`, `tests/test_reports.py`

- [ ] **Step 1: Generate `reports.default.json` from nf-install**

```bash
cd ~/Git/nfupgrader && python3 - <<'EOF'
import json
src = json.load(open("/home/dan/Git/nf-install/reports.json"))
out = [{"name": r["name"], "command": r["command"]} for r in src if r.get("type") == "shell"]
assert len(out) == 21, len(out)
with open("nfupgrader/reports.default.json", "w") as fh:
    json.dump(out, fh, indent=2)
    fh.write("\n")
EOF
python3 -c "import json;print(len(json.load(open('nfupgrader/reports.default.json'))))"
```
Expected: `21`.

- [ ] **Step 2: Write failing tests** — create `tests/test_reports.py`:

```python
import datetime
import json
import os
import tempfile
import unittest

from nfupgrader import reports
from nfupgrader.host import FakeHost, RunResult

REPS = [{"name": "Version", "command": "echo v"}, {"name": "Disk Check", "command": "df"}]
T0 = datetime.datetime(2026, 9, 23, 14, 2, 0)


def fake_host(version="v1\n", disk=None):
    return FakeHost({
        ("sh", "-c", "echo v"): RunResult(0, version, ""),
        ("sh", "-c", "df"): disk or RunResult(0, "/usr4 10%\n", ""),
    })


class RecordingHost(object):
    """Records (argv, timeout); raises for commands listed in `boom`."""

    def __init__(self, boom=()):
        self.calls = []
        self.boom = boom

    def run(self, argv, timeout=None):
        self.calls.append((list(argv), timeout))
        if argv[-1] in self.boom:
            raise OSError("exec failed")
        return RunResult(0, "ok\n", "")


class SnapshotBase(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()

    def snap(self, host, label="manual", when=T0, report_list=REPS, **kw):
        sid = reports.new_snapshot_id(self.root, label, when)
        reports.take_snapshot(host, report_list, self.root, sid, label,
                              now=lambda: when, **kw)
        return sid

    def manifest(self, sid):
        return json.load(open(os.path.join(self.root, sid, "manifest.json")))


class TestDefinitions(unittest.TestCase):
    def test_load_skips_function_entries(self):
        path = os.path.join(tempfile.mkdtemp(), "r.json")
        json.dump([{"name": "M", "command": "manage_reports", "type": "function"},
                   {"name": "A", "command": "true", "type": "shell"},
                   {"name": "B", "command": "false"}], open(path, "w"))
        self.assertEqual(reports.load_reports(path),
                         [{"name": "A", "command": "true"}, {"name": "B", "command": "false"}])

    def test_default_file_has_21_reports(self):
        path = os.path.join(os.path.dirname(reports.__file__), "reports.default.json")
        reps = reports.load_reports(path)
        self.assertEqual(len(reps), 21)
        self.assertIn("INC Info", [r["name"] for r in reps])

    def test_safe_name(self):
        self.assertEqual(reports.safe_name("Disk File Check /usr4!"), "Disk_File_Check_usr4")
        self.assertEqual(reports.safe_name("///"), "report")


class TestSnapshot(SnapshotBase):
    def test_new_id_avoids_collision(self):
        first = reports.new_snapshot_id(self.root, "before", T0)
        self.assertEqual(first, "20260923_140200_before")
        os.makedirs(os.path.join(self.root, first))
        self.assertEqual(reports.new_snapshot_id(self.root, "before", T0),
                         "20260923_140200_before_2")

    def test_files_and_manifest(self):
        sid = self.snap(fake_host(), label="before", mode="upgrade")
        d = os.path.join(self.root, sid)
        self.assertEqual(open(os.path.join(d, "01_Version.txt")).read(), "v1\n")
        self.assertEqual(open(os.path.join(d, "02_Disk_Check.txt")).read(), "/usr4 10%\n")
        m = self.manifest(sid)
        self.assertEqual(m["status"], "complete")
        self.assertEqual(m["label"], "before")
        self.assertEqual(m["mode"], "upgrade")
        self.assertEqual(m["started"], "2026-09-23T14:02:00")
        self.assertEqual(m["finished"], "2026-09-23T14:02:00")
        self.assertEqual([r["rc"] for r in m["reports"]], [0, 0])
        self.assertEqual([r["error"] for r in m["reports"]], [None, None])
        self.assertEqual(m["reports"][1]["command"], "df")

    def test_stderr_appended_after_marker(self):
        sid = self.snap(fake_host(disk=RunResult(2, "partial", "boom\n")))
        text = open(os.path.join(self.root, sid, "02_Disk_Check.txt")).read()
        self.assertEqual(text, "partial\n----- STDERR -----\nboom\n")
        self.assertEqual(self.manifest(sid)["reports"][1]["rc"], 2)

    def test_timeout_recorded(self):
        sid = self.snap(fake_host(disk=RunResult(124, "", "timeout: sh -c df")))
        entry = self.manifest(sid)["reports"][1]
        self.assertEqual(entry["error"], "timeout")
        self.assertIsNone(entry["rc"])

    def test_exception_recorded_and_timeout_passed(self):
        host = RecordingHost(boom=("df",))
        sid = self.snap(host)
        entry = self.manifest(sid)["reports"][1]
        self.assertEqual(entry["error"], "exec failed")
        self.assertIsNone(entry["rc"])
        self.assertEqual(host.calls[0], (["sh", "-c", "echo v"], reports.REPORT_TIMEOUT))

    def test_progress_callback(self):
        seen = []
        self.snap(fake_host(), on_progress=lambda i, n, name: seen.append((i, n, name)))
        self.assertEqual(seen, [(1, 2, "Version"), (2, 2, "Disk Check")])

    def test_pair_and_note_stored(self):
        sid = self.snap(fake_host(), label="after", pair_of="20260923_140000_before", note="x")
        m = self.manifest(sid)
        self.assertEqual(m["pair_of"], "20260923_140000_before")
        self.assertEqual(m["note"], "x")


class TestListing(SnapshotBase):
    def test_newest_first_and_incomplete(self):
        old = self.snap(fake_host(), when=T0)
        new = self.snap(fake_host(), when=T0 + datetime.timedelta(minutes=1))
        path = os.path.join(self.root, old, "manifest.json")
        m = json.load(open(path))
        m["status"] = "running"
        json.dump(m, open(path, "w"))
        listed = reports.list_snapshots(self.root)
        self.assertEqual([s["id"] for s in listed], [new, old])
        self.assertEqual(listed[1]["status"], "incomplete")
        self.assertNotIn("command", listed[0]["reports"][0])
        self.assertEqual(reports.list_snapshots(self.root, live_id=old)[1]["status"], "running")

    def test_ignores_junk_and_missing_root(self):
        self.snap(fake_host())
        os.makedirs(os.path.join(self.root, "junk"))
        self.assertEqual(len(reports.list_snapshots(self.root)), 1)
        self.assertEqual(reports.list_snapshots(os.path.join(self.root, "nope")), [])

    def test_prune_keeps_newest(self):
        ids = [self.snap(fake_host(), when=T0 + datetime.timedelta(seconds=i)) for i in range(5)]
        reports.prune(self.root, keep=3)
        self.assertEqual(sorted(os.listdir(self.root)), ids[2:])

    def test_prune_never_removes_protected(self):
        ids = [self.snap(fake_host(), when=T0 + datetime.timedelta(seconds=i)) for i in range(5)]
        reports.prune(self.root, keep=2, protect=(ids[0],))
        self.assertEqual(sorted(os.listdir(self.root)), [ids[0], ids[4]])

    def test_read_manifest_rejects_bad_ids(self):
        self.assertIsNone(reports.read_manifest(self.root, "../etc"))
        self.assertIsNone(reports.read_manifest(self.root, "x"))
        self.assertIsNone(reports.read_manifest(self.root, "20260923_140200_manual"))

    def test_report_text(self):
        sid = self.snap(fake_host())
        self.assertEqual(reports.report_text(self.root, sid, "Version"), "v1\n")
        self.assertIsNone(reports.report_text(self.root, sid, "Nope"))
        self.assertIsNone(reports.report_text(self.root, "../x", "Version"))


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 3: Run to verify failure**

Run: `cd ~/Git/nfupgrader && python3 -m unittest tests.test_reports 2>&1 | tail -3`
Expected: ImportError / AttributeError (`reports` has no attributes yet or module missing).

- [ ] **Step 4: Implement** — create `nfupgrader/reports.py`:

```python
"""reports.py — before/after report snapshots (ported from nf-install's
ReportManager). A snapshot runs every report in a JSON list through Host.run and
stores one text file per report plus manifest.json. Timestamps and return codes
live only in the manifest so they never show up in a diff."""
import datetime
import json
import os
import re
import shutil
import time

REPORT_TIMEOUT = 300
KEEP = 20
STDERR_MARK = "----- STDERR -----"
_ID_RE = re.compile(r"^\d{8}_\d{6}_(before|after|manual)(_\d+)?$")


def load_reports(path):
    """Load report definitions as [{name, command}], skipping non-shell entries
    (nf-install's reports.json also lists its own menu functions)."""
    with open(path) as fh:
        data = json.load(fh)
    return [{"name": r["name"], "command": r["command"]}
            for r in data if r.get("type", "shell") == "shell"]


def safe_name(name):
    """A filename-safe form of a report name."""
    keep = "".join(c for c in name if c.isalnum() or c in " -_").strip()
    return keep.replace(" ", "_") or "report"


def new_snapshot_id(root, label, when):
    """<YYYYmmdd_HHMMSS>_<label>, suffixed _2, _3... if that directory exists."""
    base = "{}_{}".format(when.strftime("%Y%m%d_%H%M%S"), label)
    sid, n = base, 2
    while os.path.exists(os.path.join(root, sid)):
        sid = "{}_{}".format(base, n)
        n += 1
    return sid


def _iso(when):
    return when.strftime("%Y-%m-%dT%H:%M:%S")


def _write_manifest(snap_dir, man):
    # Write-then-rename so a reader never sees a half-written manifest.
    tmp = os.path.join(snap_dir, "manifest.json.tmp")
    with open(tmp, "w") as fh:
        json.dump(man, fh, indent=1, sort_keys=True)
    os.rename(tmp, os.path.join(snap_dir, "manifest.json"))


def take_snapshot(host, report_list, root, snap_id, label, mode=None, pair_of=None,
                  note="", on_progress=None, now=datetime.datetime.now, clock=time.time):
    """Run every report sequentially and store the results under root/snap_id.
    A failing, timed-out or unrunnable report is recorded, never raised.
    Returns the final manifest."""
    snap_dir = os.path.join(root, snap_id)
    os.makedirs(snap_dir)
    man = {"id": snap_id, "label": label, "mode": mode, "pair_of": pair_of,
           "started": _iso(now()), "finished": None, "status": "running",
           "note": note, "reports": []}
    _write_manifest(snap_dir, man)
    total = len(report_list)
    for idx, rep in enumerate(report_list, 1):
        if on_progress is not None:
            on_progress(idx, total, rep["name"])
        fname = "{:02d}_{}.txt".format(idx, safe_name(rep["name"]))
        entry = {"name": rep["name"], "command": rep["command"], "file": fname,
                 "rc": None, "secs": 0.0, "error": None}
        t0 = clock()
        try:
            r = host.run(["sh", "-c", rep["command"]], timeout=REPORT_TIMEOUT)
            text = r.out
            if r.err:
                if text and not text.endswith("\n"):
                    text += "\n"
                text += STDERR_MARK + "\n" + r.err
            if r.rc == 124 and r.err.startswith("timeout:"):
                entry["error"] = "timeout"
            else:
                entry["rc"] = r.rc
        except Exception as e:                  # a report must never abort the snapshot
            text = ""
            entry["error"] = str(e)
        entry["secs"] = round(clock() - t0, 2)
        with open(os.path.join(snap_dir, fname), "w") as fh:
            fh.write(text)
        man["reports"].append(entry)
        _write_manifest(snap_dir, man)
    man["status"] = "complete"
    man["finished"] = _iso(now())
    _write_manifest(snap_dir, man)
    return man


def read_manifest(root, snap_id):
    """The manifest of an existing snapshot, or None for an invalid/unknown id."""
    if not _ID_RE.match(snap_id or ""):
        return None
    try:
        with open(os.path.join(root, snap_id, "manifest.json")) as fh:
            return json.load(fh)
    except (IOError, OSError, ValueError):
        return None


def list_snapshots(root, live_id=None):
    """Manifest summaries (no per-report command), newest first. A snapshot still
    marked running that is not the live job was interrupted: report incomplete."""
    if not os.path.isdir(root):
        return []
    out = []
    for sid in sorted(os.listdir(root), reverse=True):
        man = read_manifest(root, sid)
        if man is None:
            continue
        if man["status"] == "running" and sid != live_id:
            man["status"] = "incomplete"
        man["reports"] = [dict((k, v) for k, v in r.items() if k != "command")
                          for r in man["reports"]]
        out.append(man)
    return out


def prune(root, keep=KEEP, protect=()):
    """Delete the oldest snapshots so at most `keep` remain, skipping `protect`."""
    ids = sorted(s for s in os.listdir(root) if _ID_RE.match(s))
    excess = len(ids) - keep
    for sid in ids:
        if excess <= 0:
            break
        if sid in protect:
            continue
        shutil.rmtree(os.path.join(root, sid), ignore_errors=True)
        excess -= 1


def report_text(root, snap_id, name):
    """The stored output of report `name` in a snapshot, or None if unknown."""
    man = read_manifest(root, snap_id)
    if man is None:
        return None
    for r in man["reports"]:
        if r["name"] == name:
            try:
                with open(os.path.join(root, snap_id, r["file"]), errors="replace") as fh:
                    return fh.read()
            except (IOError, OSError):
                return None
    return None
```

- [ ] **Step 5: Run tests** — `python3 -m unittest tests.test_reports -v 2>&1 | tail -5` → OK; then the full suite → OK.

- [ ] **Step 6: Commit**

```bash
cd ~/Git/nfupgrader
git add nfupgrader/reports.default.json nfupgrader/reports.py tests/test_reports.py
git commit -m "feat(reports): report definitions and snapshot storage"
```

---

### Task 2: Compare + diff rows (`reports.py` part 2)

**Files:**
- Modify: `nfupgrader/reports.py` (append), `tests/test_reports.py` (append classes before `if __name__`)

- [ ] **Step 1: Write failing tests** — append to `tests/test_reports.py` (above the `if __name__` block):

```python
class TestCompare(SnapshotBase):
    def test_classification(self):
        a = self.snap(fake_host(), when=T0)
        b = self.snap(fake_host(version="v2\n", disk=RunResult(1, "/usr4 10%\n", "")),
                      when=T0 + datetime.timedelta(minutes=1),
                      report_list=REPS + [{"name": "Extra", "command": "true"}])
        res = reports.compare(self.root, a, b)
        self.assertEqual((res["a"], res["b"]), (a, b))
        rows = dict((r["name"], r) for r in res["reports"])
        self.assertEqual([r["name"] for r in res["reports"]], ["Version", "Disk Check", "Extra"])
        self.assertEqual(rows["Version"]["status"], "changed")
        self.assertEqual((rows["Version"]["added"], rows["Version"]["removed"]), (1, 1))
        self.assertEqual(rows["Disk Check"]["status"], "failed")
        self.assertEqual(rows["Extra"]["status"], "missing")
        self.assertEqual(rows["Extra"]["side"], "b")

    def test_unchanged(self):
        a = self.snap(fake_host(), when=T0)
        b = self.snap(fake_host(), when=T0 + datetime.timedelta(minutes=1))
        statuses = [r["status"] for r in reports.compare(self.root, a, b)["reports"]]
        self.assertEqual(statuses, ["unchanged", "unchanged"])

    def test_unknown_snapshot(self):
        a = self.snap(fake_host())
        self.assertIsNone(reports.compare(self.root, a, "20990101_000000_after"))


class TestDiffRows(unittest.TestCase):
    def test_unified(self):
        rows = reports.unified_rows(["x", "y", "z"], ["x", "Y", "z"])
        self.assertEqual([r["op"] for r in rows], ["@", " ", "-", "+", " "])
        self.assertEqual(rows[2]["text"], "y")
        self.assertEqual(rows[3]["text"], "Y")

    def test_unified_keeps_content_that_looks_like_headers(self):
        rows = reports.unified_rows(["--a", "keep"], ["keep"])
        self.assertIn({"op": "-", "text": "--a"}, rows)

    def test_unified_identical_is_empty(self):
        self.assertEqual(reports.unified_rows(["a"], ["a"]), [])

    def test_side_collapses_equal_runs(self):
        a = [str(i) for i in range(20)]
        b = list(a)
        b[10] = "TEN"
        rows = reports.side_rows(a, b)
        self.assertEqual([r["op"] for r in rows],
                         ["skip", "equal", "equal", "equal", "replace",
                          "equal", "equal", "equal", "skip"])
        self.assertEqual(rows[0]["count"], 7)
        self.assertEqual(rows[-1]["count"], 6)
        self.assertEqual((rows[4]["left"], rows[4]["right"]), ("10", "TEN"))
        self.assertEqual((rows[1]["left"], rows[1]["right"]), ("7", "7"))

    def test_side_uneven_replace_pads_with_none(self):
        rows = reports.side_rows(["a", "b"], ["c"])
        self.assertEqual([(r["left"], r["right"]) for r in rows], [("a", "c"), ("b", None)])

    def test_side_identical_is_single_skip(self):
        self.assertEqual(reports.side_rows(["a", "b"], ["a", "b"]),
                         [{"op": "skip", "count": 2}])


class TestDiff(SnapshotBase):
    def test_diff_styles_and_cap(self):
        a = self.snap(fake_host(version="1\n2\n3\n"), when=T0)
        b = self.snap(fake_host(version="1\nX\nY\n"), when=T0 + datetime.timedelta(minutes=1))
        side = reports.diff(self.root, a, b, "Version", "side")
        self.assertFalse(side["truncated"])
        self.assertEqual([r["op"] for r in side["rows"]], ["equal", "replace", "replace"])
        uni = reports.diff(self.root, a, b, "Version", "unified")
        self.assertIn({"op": "+", "text": "X"}, uni["rows"])
        capped = reports.diff(self.root, a, b, "Version", "side", cap=2)
        self.assertTrue(capped["truncated"])
        self.assertEqual(len(capped["rows"]), 2)

    def test_diff_one_sided_report(self):
        a = self.snap(fake_host(), when=T0)
        b = self.snap(fake_host(), when=T0 + datetime.timedelta(minutes=1),
                      report_list=REPS + [{"name": "Extra", "command": "true"}])
        # FakeHost answers the unmocked "true" with rc 1 and stderr "unmocked", so b's
        # Extra text is "----- STDERR -----\nunmocked"; a has no Extra -> two inserts.
        res = reports.diff(self.root, a, b, "Extra", "side")
        self.assertEqual([r["op"] for r in res["rows"]], ["insert", "insert"])

    def test_diff_unknown_report(self):
        a = self.snap(fake_host())
        self.assertIsNone(reports.diff(self.root, a, a, "Nope", "side"))
```


- [ ] **Step 2: Run to verify failure** — `python3 -m unittest tests.test_reports 2>&1 | tail -3` → AttributeError for `compare`/`unified_rows`/`side_rows`/`diff`.

- [ ] **Step 3: Implement** — append to `nfupgrader/reports.py` (and add `import difflib` to the imports):

```python
MAX_ROWS = 2000
CONTEXT = 3


def _failed(entry):
    return entry["error"] is not None or entry["rc"] != 0


def _lines(root, snap_id, name):
    text = report_text(root, snap_id, name)
    return (text or "").splitlines()


def _counts(a_lines, b_lines):
    """(added, removed) line counts between two texts."""
    if a_lines == b_lines:
        return 0, 0
    added = removed = 0
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a_lines, b_lines).get_opcodes():
        if tag in ("replace", "delete"):
            removed += i2 - i1
        if tag in ("replace", "insert"):
            added += j2 - j1
    return added, removed


def compare(root, a, b):
    """Per-report status between snapshots a and b (A's order, then B-only
    reports), or None if either snapshot is unknown."""
    ma, mb = read_manifest(root, a), read_manifest(root, b)
    if ma is None or mb is None:
        return None
    ea = dict((r["name"], r) for r in ma["reports"])
    eb = dict((r["name"], r) for r in mb["reports"])
    names = [r["name"] for r in ma["reports"]] + \
            [r["name"] for r in mb["reports"] if r["name"] not in ea]
    rows = []
    for name in names:
        if name not in ea or name not in eb:
            rows.append({"name": name, "status": "missing", "added": 0, "removed": 0,
                         "side": "a" if name in ea else "b"})
            continue
        added, removed = _counts(_lines(root, a, name), _lines(root, b, name))
        if _failed(ea[name]) or _failed(eb[name]):
            status = "failed"
        elif added or removed:
            status = "changed"
        else:
            status = "unchanged"
        rows.append({"name": name, "status": status, "added": added, "removed": removed})
    return {"a": a, "b": b, "reports": rows}


def unified_rows(a_lines, b_lines, context=CONTEXT):
    """Unified-diff rows: {op: ' '|'+'|'-'|'@', text}."""
    rows = []
    for i, ln in enumerate(difflib.unified_diff(a_lines, b_lines, lineterm="", n=context)):
        if i < 2:                       # the ---/+++ file header lines
            continue
        if ln.startswith("@@"):
            rows.append({"op": "@", "text": ln})
        else:
            rows.append({"op": ln[:1] or " ", "text": ln[1:]})
    return rows


def side_rows(a_lines, b_lines, context=CONTEXT):
    """Side-by-side rows: {op: equal|replace|insert|delete, left, right}, with long
    equal runs collapsed to `context` lines each side around a {op: skip, count}."""
    rows = []
    ops = difflib.SequenceMatcher(None, a_lines, b_lines).get_opcodes()
    last = len(ops) - 1
    for k, (tag, i1, i2, j1, j2) in enumerate(ops):
        if tag == "equal":
            n = i2 - i1
            head = context if k > 0 else 0
            tail = context if k < last else 0
            if n > head + tail:
                shown = list(range(head)) + [None] + list(range(n - tail, n))
            else:
                shown = list(range(n))
            for off in shown:
                if off is None:
                    rows.append({"op": "skip", "count": n - head - tail})
                else:
                    rows.append({"op": "equal", "left": a_lines[i1 + off],
                                 "right": b_lines[j1 + off]})
            continue
        for off in range(max(i2 - i1, j2 - j1)):
            left = a_lines[i1 + off] if i1 + off < i2 else None
            right = b_lines[j1 + off] if j1 + off < j2 else None
            rows.append({"op": tag, "left": left, "right": right})
    return rows


def diff(root, a, b, name, style="side", cap=MAX_ROWS):
    """Diff rows for one report between two snapshots (a side missing the report
    diffs as empty), or None if neither snapshot has it."""
    at, bt = report_text(root, a, name), report_text(root, b, name)
    if at is None and bt is None:
        return None
    al, bl = (at or "").splitlines(), (bt or "").splitlines()
    rows = unified_rows(al, bl) if style == "unified" else side_rows(al, bl)
    return {"rows": rows[:cap], "truncated": len(rows) > cap}
```

- [ ] **Step 4: Run tests** — test_reports OK, full suite OK.

- [ ] **Step 5: Commit**

```bash
git add nfupgrader/reports.py tests/test_reports.py
git commit -m "feat(reports): per-report compare and side/unified diff rows"
```

---

### Task 3: Background runner, upgrade watcher, sequencing (`snapjobs.py`)

**Files:**
- Create: `nfupgrader/snapjobs.py`, `tests/test_snapjobs.py`, `tests/fixtures/reports.test.json`

- [ ] **Step 1: Create the fixture** `tests/fixtures/reports.test.json`:

```json
[
  {"name": "Manage", "command": "manage_reports", "type": "function"},
  {"name": "Version", "command": "echo v", "type": "shell"},
  {"name": "Disk Check", "command": "df", "type": "shell"}
]
```

- [ ] **Step 2: Write failing tests** — create `tests/test_snapjobs.py`:

```python
import datetime
import json
import os
import tempfile
import unittest
from unittest import mock

from nfupgrader import snapjobs, reports
from nfupgrader.audit import AuditLog
from nfupgrader.host import FakeHost, RunResult

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "reports.test.json")
T0 = datetime.datetime(2026, 9, 23, 14, 0, 0)


def sync(fn):
    fn()


class Hold(object):
    """spawn stand-in that keeps the job function instead of running it."""

    def __init__(self):
        self.fns = []

    def __call__(self, fn):
        self.fns.append(fn)


def ticking(start=T0):
    state = {"t": start}

    def now():
        state["t"] += datetime.timedelta(seconds=1)
        return state["t"]
    return now


def host(events=None):
    h = FakeHost({("sh", "-c", "echo v"): RunResult(0, "v1\n", ""),
                  ("sh", "-c", "df"): RunResult(0, "ok\n", "")})
    if events is not None:
        orig = h.run

        def run(argv, timeout=None):
            events.append("report")
            return orig(argv, timeout)
        h.run = run
    return h


def statuses(seq):
    """read_status stand-in: yields seq, then repeats its last value."""
    it = iter(seq)
    last = {"v": None}

    def read():
        try:
            last["v"] = next(it)
        except StopIteration:
            pass
        return last["v"]
    return read


def clock(step=30):
    state = {"t": -step}

    def now():
        state["t"] += step
        return state["t"]
    return now


class RunnerBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.root = os.path.join(self.tmp, "nfupgrader_reports")
        self.audit = AuditLog(os.path.join(self.tmp, "audit.jsonl"))

    def runner(self, spawn=sync, h=None, path=FIXTURE, keep=20):
        return snapjobs.SnapshotRunner(h or host(), path, self.root, audit=self.audit,
                                       spawn=spawn, now=ticking(), keep=keep)

    def events(self):
        return [json.loads(l) for l in open(os.path.join(self.tmp, "audit.jsonl"))]


class TestRunner(RunnerBase):
    def test_sync_run_writes_snapshot_and_audits(self):
        r = self.runner()
        sid = r.start("manual", client="1.2.3.4")
        self.assertIsNone(r.job())
        man = reports.read_manifest(self.root, sid)
        self.assertEqual(man["status"], "complete")
        self.assertEqual([x["name"] for x in man["reports"]], ["Version", "Disk Check"])
        ev = [e for e in self.events() if e["event"] == "report_snapshot"]
        self.assertEqual(ev[0]["id"], sid)
        self.assertEqual(ev[0]["client"], "1.2.3.4")

    def test_busy_while_job_held(self):
        hold = Hold()
        r = self.runner(spawn=hold)
        sid = r.start("manual")
        self.assertTrue(r.busy())
        self.assertEqual(r.job()["id"], sid)
        self.assertEqual(r.live_id(), sid)
        with self.assertRaises(snapjobs.Busy):
            r.start("manual")
        hold.fns[0]()
        self.assertFalse(r.busy())

    def test_progress_visible_during_run(self):
        seen = []
        h = host()
        r = self.runner(h=h)
        orig = h.run

        def run(argv, timeout=None):
            seen.append((r.job()["index"], r.job()["current"]))
            return orig(argv, timeout)
        h.run = run
        r.start("manual")
        self.assertEqual(seen, [(1, "Version"), (2, "Disk Check")])

    def test_then_runs_after_job_cleared(self):
        r = self.runner()
        seen = []
        sid = r.start("before", then=lambda i: seen.append((i, r.busy())))
        self.assertEqual(seen, [(sid, False)])

    def test_snapshot_error_audited_and_then_still_runs(self):
        r = self.runner()
        seen = []
        with mock.patch("nfupgrader.snapjobs.reports.take_snapshot",
                        side_effect=OSError("disk full")):
            r.start("before", then=seen.append)
        self.assertEqual(len(seen), 1)
        self.assertIn("report_snapshot_error", [e["event"] for e in self.events()])
        self.assertFalse(r.busy())

    def test_missing_report_file_gives_empty_snapshot(self):
        r = self.runner(path=os.path.join(self.tmp, "nope.json"))
        sid = r.start("manual")
        self.assertEqual(reports.read_manifest(self.root, sid)["reports"], [])

    def test_prunes_after_run(self):
        r = self.runner(keep=1)
        r.start("manual")
        second = r.start("manual")
        self.assertEqual(os.listdir(self.root), [second])


class TestWatch(unittest.TestCase):
    def run_watch(self, seq, grace=300, start_after=None):
        calls = []
        note = snapjobs.watch_upgrade(statuses(seq), start_after or calls.append,
                                      sleep=lambda s: None, clock=clock(), grace=grace)
        return note, calls

    def test_done_after_running(self):
        note, calls = self.run_watch(["not_started", "running", "running", "done"])
        self.assertEqual(calls, [""])

    def test_died_after_running(self):
        note, calls = self.run_watch(["running", "died"])
        self.assertEqual(calls, ["upgrade died before completion"])

    def test_stale_done_ignored_until_running_seen(self):
        note, calls = self.run_watch(["done", "running", "done"])
        self.assertEqual(calls, [""])

    def test_never_running_hits_grace(self):
        note, calls = self.run_watch(["not_started"], grace=300)
        self.assertEqual(len(calls), 1)
        self.assertTrue(calls[0].startswith("upgrade not observed running"))
        self.assertIn("not_started", calls[0])

    def test_retries_while_busy(self):
        attempts = []

        def start_after(note):
            attempts.append(note)
            if len(attempts) == 1:
                raise snapjobs.Busy()
        self.run_watch(["running", "done"], start_after=start_after)
        self.assertEqual(attempts, ["", ""])


class TestBeginUpgrade(RunnerBase):
    def test_before_then_launch_then_after(self):
        events = []
        r = self.runner(h=host(events))
        before = snapjobs.begin_upgrade(
            r, lambda: events.append("launch"), statuses(["running", "done"]),
            client="ip", audit=self.audit, spawn=sync, sleep=lambda s: None, clock=clock())
        self.assertEqual(events, ["report", "report", "launch", "report", "report"])
        snaps = reports.list_snapshots(self.root)
        self.assertEqual([s["label"] for s in snaps], ["after", "before"])
        self.assertEqual(snaps[0]["pair_of"], before)
        self.assertEqual(snaps[1]["id"], before)

    def test_launch_failure_audited_no_after(self):
        r = self.runner()

        def boom():
            raise OSError("no nf_upgrade")
        snapjobs.begin_upgrade(r, boom, statuses(["running", "done"]), client="ip",
                               audit=self.audit, spawn=sync, sleep=lambda s: None, clock=clock())
        self.assertEqual([s["label"] for s in reports.list_snapshots(self.root)], ["before"])
        err = [e for e in self.events() if e["event"] == "launch_error"]
        self.assertEqual(err[0]["error"], "no nf_upgrade")

    def test_busy_propagates(self):
        r = self.runner(spawn=Hold())
        r.start("manual")
        with self.assertRaises(snapjobs.Busy):
            snapjobs.begin_upgrade(r, lambda: None, statuses(["done"]), client="ip")


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 3: Run to verify failure** — `python3 -m unittest tests.test_snapjobs 2>&1 | tail -3` → ImportError.

- [ ] **Step 4: Implement** — create `nfupgrader/snapjobs.py`:

```python
"""snapjobs.py — run report snapshots in the background, one at a time, and
sequence them around an Upgrade launch: before snapshot -> launch -> watch the
progress record -> after snapshot."""
import datetime
import os
import threading
import time

from nfupgrader import reports

GRACE = 300       # seconds to wait for nf_upgrader to be seen running
INTERVAL = 30     # seconds between progress polls / busy retries


class Busy(Exception):
    """A snapshot job is already running."""


def thread_spawn(fn):
    """Run fn on a daemon thread."""
    t = threading.Thread(target=fn)
    t.daemon = True
    t.start()


class SnapshotRunner(object):
    """Owns the single snapshot job. `spawn` runs the job body (a thread in
    production, inline in tests)."""

    def __init__(self, host, reports_path, root, audit=None, spawn=thread_spawn,
                 now=datetime.datetime.now, keep=reports.KEEP):
        self.host = host
        self.reports_path = reports_path
        self.root = root
        self.audit = audit
        self.spawn = spawn
        self.now = now
        self.keep = keep
        self._lock = threading.Lock()
        self._job = None

    def busy(self):
        with self._lock:
            return self._job is not None

    def job(self):
        """A copy of the running job's state, or None when idle."""
        with self._lock:
            return dict(self._job) if self._job else None

    def live_id(self):
        j = self.job()
        return j["id"] if j else None

    def _progress(self, idx, total, name):
        with self._lock:
            if self._job is not None:
                self._job.update(index=idx, total=total, current=name)

    def _load(self):
        try:
            return reports.load_reports(self.reports_path)
        except (IOError, OSError, ValueError):
            return []

    def start(self, label, mode=None, pair_of=None, note="", then=None, client="auto"):
        """Start a snapshot; returns its id. Raises Busy if one is running.
        `then(snap_id)` is called once the job is finished and no longer busy,
        even if the snapshot itself failed."""
        report_list = self._load()
        with self._lock:
            if self._job is not None:
                raise Busy()
            if not os.path.isdir(self.root):
                os.makedirs(self.root)
            snap_id = reports.new_snapshot_id(self.root, label, self.now())
            self._job = {"id": snap_id, "label": label, "index": 0,
                         "total": len(report_list), "current": "", "status": "running"}
        if self.audit is not None:
            self.audit.record("report_snapshot", id=snap_id, label=label, client=client)

        def work():
            try:
                reports.take_snapshot(self.host, report_list, self.root, snap_id, label,
                                      mode=mode, pair_of=pair_of, note=note,
                                      on_progress=self._progress, now=self.now)
                reports.prune(self.root, self.keep, protect=(snap_id,))
            except Exception as e:
                if self.audit is not None:
                    self.audit.record("report_snapshot_error", id=snap_id, error=str(e))
            finally:
                with self._lock:
                    self._job = None
            # Cleared before `then` so the after-watcher it starts can run a job.
            if then is not None:
                then(snap_id)

        self.spawn(work)
        return snap_id


def watch_upgrade(read_status, start_after, sleep=time.sleep, clock=time.time,
                  grace=GRACE, interval=INTERVAL):
    """Poll read_status() until the upgrade ends, then start_after(note), retrying
    while a snapshot is busy. done/died only count once 'running' was seen, since
    the state file can still hold a terminal state from an earlier run."""
    t0 = clock()
    seen_running = False
    while True:
        st = read_status()
        if st == "running":
            seen_running = True
        elif seen_running and st == "done":
            note = ""
            break
        elif seen_running and st == "died":
            note = "upgrade died before completion"
            break
        elif not seen_running and clock() - t0 >= grace:
            note = "upgrade not observed running (status: {})".format(st)
            break
        sleep(interval)
    while True:
        try:
            start_after(note)
            return note
        except Busy:
            sleep(interval)


def begin_upgrade(runner, launch, read_status, client, audit=None, spawn=thread_spawn,
                  sleep=time.sleep, clock=time.time):
    """Take the before snapshot, then launch(), then watch for completion and take
    the after snapshot. Returns the before id; raises Busy if a job is running.
    A launch() failure is audited and ends the sequence (no after snapshot)."""
    def then(before_id):
        try:
            launch()
        except Exception as e:
            if audit is not None:
                audit.record("launch_error", error=str(e), client=client)
            return

        def start_after(note):
            runner.start("after", mode="upgrade", pair_of=before_id, note=note)
        spawn(lambda: watch_upgrade(read_status, start_after, sleep, clock))

    return runner.start("before", mode="upgrade", then=then, client=client)
```

- [ ] **Step 5: Run tests** — test_snapjobs OK, full suite OK.

- [ ] **Step 6: Commit**

```bash
git add nfupgrader/snapjobs.py tests/test_snapjobs.py tests/fixtures/reports.test.json
git commit -m "feat(reports): background snapshot runner and upgrade before/after sequencing"
```

---

### Task 4: API routes

**Files:**
- Modify: `nfupgrader/api.py`, `tests/test_api.py`

- [ ] **Step 1: Write failing tests** — in `tests/test_api.py` add imports at the top:

```python
import datetime
from nfupgrader import snapjobs
```

and append this class above `if __name__ == "__main__":`:

```python
REPORTS_FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "reports.test.json")


class TestReportsApi(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        base = make_ctx(self.tmp)
        base.host.responses[("sh", "-c", "echo v")] = RunResult(0, "v1\n", "")
        base.host.responses[("sh", "-c", "df")] = RunResult(0, "/usr4 10%\n", "")
        self.root = os.path.join(self.tmp, "nfupgrader_reports")
        ticks = iter(datetime.datetime(2026, 9, 23, 14, 0, s) for s in range(60))
        self.runner = snapjobs.SnapshotRunner(base.host, REPORTS_FIXTURE, self.root,
                                              audit=base.audit, spawn=lambda fn: fn(),
                                              now=lambda: next(ticks))
        self.ctx = base._replace(runner=self.runner, reports_root=self.root,
                                 reports_path=REPORTS_FIXTURE)
        self.sid = self.ctx.auth.redeem("TOK", "127.0.0.1")

    def _get(self, path, **q):
        return api.dispatch("GET", path, dict((k, [v]) for k, v in q.items()), b"",
                            {"nfu_sid": self.sid}, "127.0.0.1", self.ctx)

    def _post(self, path, payload=None):
        return api.dispatch("POST", path, {}, json.dumps(payload or {}).encode(),
                            {"nfu_sid": self.sid}, "127.0.0.1", self.ctx)

    def _json(self, resp):
        return json.loads(resp[2].decode())

    def test_requires_session(self):
        status, _, _ = api.dispatch("GET", "/api/reports", {}, b"", {}, "127.0.0.1", self.ctx)
        self.assertEqual(status, 401)

    def test_run_then_list_and_idle_job(self):
        resp = self._post("/api/reports/run")
        self.assertEqual(resp[0], 202)
        sid = self._json(resp)["id"]
        snaps = self._json(self._get("/api/reports"))["snapshots"]
        self.assertEqual([(s["id"], s["label"], s["status"]) for s in snaps],
                         [(sid, "manual", "complete")])
        self.assertEqual(self._json(self._get("/api/reports/job")), {"job": None})

    def test_busy_run_and_launch_409(self):
        held = []
        self.runner.spawn = held.append
        self.assertEqual(self._post("/api/reports/run")[0], 202)
        self.assertEqual(self._post("/api/reports/run")[0], 409)
        self.assertEqual(self._json(self._get("/api/reports/job"))["job"]["label"], "manual")
        resp = self._post("/api/launch", {"mode": "stage"})
        self.assertEqual(resp[0], 409)
        self.assertEqual(self._json(resp)["error"], "report snapshot in progress")
        held[0]()
        self.assertEqual(self._post("/api/launch", {"mode": "stage"})[0], 202)

    def test_compare_diff_raw(self):
        a = self._json(self._post("/api/reports/run"))["id"]
        self.ctx.host.responses[("sh", "-c", "echo v")] = RunResult(0, "v2\n", "")
        b = self._json(self._post("/api/reports/run"))["id"]
        cmp_ = self._json(self._get("/api/reports/compare", a=a, b=b))
        self.assertEqual([(r["name"], r["status"]) for r in cmp_["reports"]],
                         [("Version", "changed"), ("Disk Check", "unchanged")])
        side = self._json(self._get("/api/reports/diff", a=a, b=b, report="Version", style="side"))
        self.assertEqual(side["rows"], [{"op": "replace", "left": "v1", "right": "v2"}])
        uni = self._json(self._get("/api/reports/diff", a=a, b=b, report="Version",
                                   style="unified"))
        self.assertEqual([r["op"] for r in uni["rows"]], ["@", "-", "+"])
        status, headers, body = self._get("/api/reports/raw", id=a, report="Version")
        self.assertEqual(status, 200)
        self.assertIn(("Content-Type", "text/plain; charset=utf-8"), headers)
        self.assertEqual(body, b"v1\n")

    def test_errors(self):
        a = self._json(self._post("/api/reports/run"))["id"]
        bad = "20990101_000000_after"
        self.assertEqual(self._get("/api/reports/compare", a=a, b=bad)[0], 404)
        self.assertEqual(self._get("/api/reports/diff", a=a, b=bad, report="Version")[0], 404)
        self.assertEqual(self._get("/api/reports/diff", a=a, b=a, report="Nope")[0], 404)
        self.assertEqual(self._get("/api/reports/diff", a=a, b=a, report="Version",
                                   style="weird")[0], 400)
        self.assertEqual(self._get("/api/reports/raw", id="../x", report="Version")[0], 404)
        self.assertEqual(self._get("/api/reports/raw", id=a, report="Nope")[0], 404)

    def test_no_runner(self):
        ctx = make_ctx(tempfile.mkdtemp())
        sid = ctx.auth.redeem("TOK", "127.0.0.1")
        status, _, body = api.dispatch("POST", "/api/reports/run", {}, b"{}",
                                       {"nfu_sid": sid}, "127.0.0.1", ctx)
        self.assertEqual(status, 503)
        status, _, body = api.dispatch("GET", "/api/reports", {}, b"",
                                       {"nfu_sid": sid}, "127.0.0.1", ctx)
        self.assertEqual(json.loads(body.decode()), {"snapshots": []})
```

- [ ] **Step 2: Run to verify failure** — `python3 -m unittest tests.test_api 2>&1 | tail -3` → errors (`_replace` unknown field `runner`, 404s).

- [ ] **Step 3: Implement** — in `nfupgrader/api.py`:

3a. Imports: change the `from nfupgrader import ...` line to also import `reports, snapjobs`:

```python
from nfupgrader import progress, logtail, launcher, localinfo, siteview, site, checks, cfg
from nfupgrader import reports, snapjobs
```

3b. Context: add three fields at the end and extend the defaults to 11 `None`s:

```python
Context = collections.namedtuple("Context", [
    "host", "hostname", "inclogdir", "phase", "tracefile",
    "auth", "audit", "webdir", "nf_upgrade_path",
    "effective_config", "user_config", "launch_cwd", "cnc_cnfg_path",
    "peer_host_factory", "checks_path", "state", "depot_dirs",
    "reports_path", "reports_root", "runner",
])
# The trailing fields are only used by side effects (launch), info gathering,
# checks, the config builder, or reports; default them so the dispatcher/tests
# need not supply each one. (namedtuple field defaults via __new__.__defaults__ — py3.6.)
Context.__new__.__defaults__ = (None,) * 11
```

3c. Helpers after `_json`:

```python
def _q(query, name, default=""):
    return (query.get(name) or [default])[0]


def _reports_root(ctx):
    return ctx.reports_root or os.path.join(ctx.inclogdir, "nfupgrader_reports")


def _snapshot_busy(ctx):
    return ctx.runner is not None and ctx.runner.busy()
```

3d. Routes: insert before `if path == "/api/launch" and method == "POST":`:

```python
    if path == "/api/reports" and method == "GET":
        live = ctx.runner.live_id() if ctx.runner is not None else None
        return _json(200, {"snapshots": reports.list_snapshots(_reports_root(ctx), live)})

    if path == "/api/reports/job" and method == "GET":
        return _json(200, {"job": ctx.runner.job() if ctx.runner is not None else None})

    if path == "/api/reports/run" and method == "POST":
        if ctx.runner is None:
            return _json(503, {"error": "reports unavailable"})
        try:
            snap_id = ctx.runner.start("manual", client=client_ip)
        except snapjobs.Busy:
            return _json(409, {"error": "report snapshot in progress"})
        return _json(202, {"id": snap_id})

    if path == "/api/reports/compare" and method == "GET":
        res = reports.compare(_reports_root(ctx), _q(query, "a"), _q(query, "b"))
        if res is None:
            return _json(404, {"error": "unknown snapshot"})
        return _json(200, res)

    if path == "/api/reports/diff" and method == "GET":
        root = _reports_root(ctx)
        a, b, style = _q(query, "a"), _q(query, "b"), _q(query, "style", "side")
        if style not in ("side", "unified"):
            return _json(400, {"error": "style must be side or unified"})
        if reports.read_manifest(root, a) is None or reports.read_manifest(root, b) is None:
            return _json(404, {"error": "unknown snapshot"})
        res = reports.diff(root, a, b, _q(query, "report"), style)
        if res is None:
            return _json(404, {"error": "unknown report"})
        return _json(200, res)

    if path == "/api/reports/raw" and method == "GET":
        text = reports.report_text(_reports_root(ctx), _q(query, "id"), _q(query, "report"))
        if text is None:
            return _json(404, {"error": "unknown snapshot or report"})
        return 200, [("Content-Type", "text/plain; charset=utf-8")], text.encode("utf-8")
```

3e. In the `/api/launch` handler, right after the `mode not in ("stage", "upgrade")` check, add:

```python
        if _snapshot_busy(ctx):
            return _json(409, {"error": "report snapshot in progress"})
```

- [ ] **Step 4: Run tests** — test_api OK, full suite OK.

- [ ] **Step 5: Commit**

```bash
git add nfupgrader/api.py tests/test_api.py
git commit -m "feat(reports): reports API routes; launch blocked while a snapshot runs"
```

---

### Task 5: httpd wiring — before snapshot around Upgrade launches

**Files:**
- Modify: `nfupgrader/httpd.py`, `tests/test_httpd.py`, `README.md`

- [ ] **Step 1: Write failing tests** — in `tests/test_httpd.py` add imports:

```python
import json
import os
import tempfile
from nfupgrader import api, snapjobs
from nfupgrader.audit import AuditLog
from nfupgrader.host import FakeHost, RunResult
```

and append above `if __name__`:

```python
def launch_ctx(tmp, runner=None, host=None):
    return api.Context(
        host=host or FakeHost(), hostname="h1", inclogdir=tmp, phase="stage",
        tracefile=os.path.join(tmp, "trace"), auth=None,
        audit=AuditLog(os.path.join(tmp, "audit.jsonl")), webdir=tmp,
        nf_upgrade_path="/bin/true", runner=runner)


class FakeRunner(object):
    pass


class TestLaunchSideEffect(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.performed = []

    def perform(self, ctx, mode):
        self.performed.append(mode)

    def events(self):
        return [json.loads(l)["event"] for l in open(os.path.join(self.tmp, "audit.jsonl"))]

    def test_stage_launches_directly(self):
        ctx = launch_ctx(self.tmp, runner=FakeRunner())
        status, body = httpd.launch_side_effect(ctx, "stage", "ip", perform=self.perform,
                                                begin=None)
        self.assertEqual((status, body), (202, {"accepted": True, "mode": "stage"}))
        self.assertEqual(self.performed, ["stage"])

    def test_direct_launch_failure_is_500_and_audited(self):
        def boom(ctx, mode):
            raise OSError('bad "path"')
        status, body = httpd.launch_side_effect(launch_ctx(self.tmp), "stage", "ip",
                                                perform=boom)
        self.assertEqual(status, 500)
        self.assertEqual(body["error"], 'launch failed: bad "path"')
        self.assertIn("launch_error", self.events())

    def test_upgrade_goes_through_before_snapshot(self):
        seen = {}

        def begin(runner, launch, read_status, client, audit=None):
            seen.update(runner=runner, client=client)
            launch()
            return "20260923_140000_before"
        runner = FakeRunner()
        ctx = launch_ctx(self.tmp, runner=runner)
        status, body = httpd.launch_side_effect(ctx, "upgrade", "ip", perform=self.perform,
                                                begin=begin)
        self.assertEqual(status, 202)
        self.assertEqual(body, {"accepted": True, "mode": "upgrade",
                                "snapshot": "20260923_140000_before"})
        self.assertIs(seen["runner"], runner)
        self.assertEqual(self.performed, ["upgrade"])

    def test_upgrade_busy_is_409(self):
        def begin(*a, **kw):
            raise snapjobs.Busy()
        status, body = httpd.launch_side_effect(launch_ctx(self.tmp, runner=FakeRunner()),
                                                "upgrade", "ip", perform=self.perform,
                                                begin=begin)
        self.assertEqual(status, 409)
        self.assertEqual(self.performed, [])

    def test_upgrade_without_runner_launches_directly(self):
        status, body = httpd.launch_side_effect(launch_ctx(self.tmp), "upgrade", "ip",
                                                perform=self.perform)
        self.assertEqual(status, 202)
        self.assertEqual(self.performed, ["upgrade"])

    def test_upgrade_status_reads_upgrade_phase(self):
        host = FakeHost({("pgrep", "-f", "nf_upgrader"): RunResult(1, "", "")})
        open(os.path.join(self.tmp, ".h1_STATE"), "w").write("swidone\n")
        self.assertEqual(httpd.upgrade_status(launch_ctx(self.tmp, host=host)), "done")
```

- [ ] **Step 2: Run to verify failure** — `python3 -m unittest tests.test_httpd 2>&1 | tail -3` → AttributeError `launch_side_effect`.

- [ ] **Step 3: Implement** — in `nfupgrader/httpd.py`:

3a. Imports: `from nfupgrader import api, launcher, progress, snapjobs`.

3b. Add these module-level functions just above `def make_handler(ctx):`:

```python
def perform_launch(ctx, mode):
    """Spawn nf_upgrade. The active config is one chosen in the UI (state), else
    NFU_USER_CONFIG; an effective config is generated from it (sources it + forces
    PROMPTFORVALIDATE=NO) — the customer's own file is never edited."""
    resolved = (ctx.state or {}).get("user_config") or ctx.user_config
    if resolved:
        launcher.write_effective_config(resolved, ctx.effective_config)
    launcher.launch(ctx.nf_upgrade_path, ctx.effective_config, mode, cwd=ctx.launch_cwd)


def upgrade_status(ctx):
    """The upgrade-phase progress status (done/running/died/not_started)."""
    running = launcher.upgrader_running(ctx.host)
    return progress.read_progress(ctx.hostname, "upgrade", ctx.inclogdir, running)["status"]


def launch_side_effect(ctx, mode, ip, perform=None, begin=None):
    """Carry out an accepted launch; returns (status, body dict). Upgrades run a
    before snapshot first (in the background) and then launch; stage launches, or
    any launch without a snapshot runner, spawn nf_upgrade directly — a spawn
    failure (missing/non-executable script, missing config) becomes a clean 500."""
    perform = perform or perform_launch
    begin = begin or snapjobs.begin_upgrade
    if mode == "upgrade" and ctx.runner is not None:
        try:
            before = begin(ctx.runner, lambda: perform(ctx, mode),
                           lambda: upgrade_status(ctx), client=ip, audit=ctx.audit)
        except snapjobs.Busy:
            return 409, {"error": "report snapshot in progress"}
        return 202, {"accepted": True, "mode": mode, "snapshot": before}
    try:
        perform(ctx, mode)
    except Exception as e:
        ctx.audit.record("launch_error", error=str(e), client=ip)
        return 500, {"error": "launch failed: {}".format(e)}
    return 202, {"accepted": True, "mode": mode}
```

3c. In `Handler._handle`, replace the whole `if path == "/api/launch" and status == 202:` block (the `try:` … `except Exception as e:` … that builds `out` by hand) with:

```python
            if path == "/api/launch" and status == 202:
                mode = json.loads(body.decode() or "{}").get("mode")
                status, obj = launch_side_effect(ctx, mode, ip)
                out = json.dumps(obj).encode("utf-8")
                headers = [("Content-Type", "application/json")]
```

Keep the comment above it but update to: `# Perform the launch side effect after a recorded 202 (see launch_side_effect).`

3d. In `main()`, build the host, audit and runner once and pass them into the Context. Replace `host=Host(),` and `audit=AuditLog(...)` in the `api.Context(...)` call accordingly, and add before it:

```python
    host = Host()
    audit = AuditLog(os.path.join(inclogdir, "nfupgrader_audit.jsonl"))
    reports_path = os.environ.get("NFU_REPORTS", os.path.join(here, "reports.default.json"))
    reports_root = os.path.join(inclogdir, "nfupgrader_reports")
    runner = snapjobs.SnapshotRunner(host, reports_path, reports_root, audit=audit)
```

and in the Context call use `host=host`, `audit=audit`, and add `reports_path=reports_path, reports_root=reports_root, runner=runner,`.

3e. `README.md`: in the environment table, add a row after the `NFU_LAUNCH_CWD` row:

```
| `NFU_REPORTS` | `nfupgrader/reports.default.json` | Report list for the Reports tab. Snapshots go to `${INCLOGDIR}/nfupgrader_reports/` (newest 20 kept); an Upgrade launch takes a *before* snapshot first and an *after* snapshot when the upgrade ends. |
```

- [ ] **Step 4: Run tests** — test_httpd OK, full suite OK. Also `python3 -c "import nfupgrader.httpd"` → no error.

- [ ] **Step 5: Commit**

```bash
git add nfupgrader/httpd.py tests/test_httpd.py README.md
git commit -m "feat(reports): take before/after snapshots around Upgrade launches"
```

---

### Task 6: Reports tab UI

**Files:**
- Modify: `nfupgrader/web/index.html`, `nfupgrader/web/app.js`, `nfupgrader/web/style.css`, `tests/test_web_assets.py`

- [ ] **Step 1: Failing asset tests** — in `tests/test_web_assets.py` extend `REQUIRED_IDS` with:

```python
    "btn-reports-run", "reports-job", "reports-list", "cmp-a", "cmp-b",
    "cmp-style", "btn-compare", "cmp-table",
```

and add:

```python
    def test_reports_output_is_escaped(self):
        self.assertIn("function esc(", self.js)
        self.assertIn("esc(r.left)", self.js)
        self.assertIn("esc(r.right)", self.js)
```

Run: `python3 -m unittest tests.test_web_assets 2>&1 | tail -3` → FAIL.

- [ ] **Step 2: `index.html`** — add the tab button after the Upgrade button:

```html
    <button class="tab" data-tab="reports">Reports</button>
```

and add this panel after the `tab-upgrade` section (before `<!-- SITE -->`):

```html
    <!-- REPORTS -->
    <section class="panel" id="tab-reports">
      <div class="card">
        <h2>Report snapshots</h2>
        <div style="margin-bottom:.5rem">
          <button id="btn-reports-run">Run reports now</button>
          <span id="reports-job"></span>
        </div>
        <table id="reports-list" class="checks">
          <thead><tr><th>Snapshot</th><th>Label</th><th>Started</th><th>Status</th><th>Note</th></tr></thead>
          <tbody></tbody>
        </table>
      </div>
      <div class="card">
        <h2>Compare</h2>
        <div style="margin-bottom:.5rem">
          <label>A: <select id="cmp-a"></select></label>
          <label>B: <select id="cmp-b"></select></label>
          <label>View:
            <select id="cmp-style"><option value="side">Side by side</option><option value="unified">Unified</option></select>
          </label>
          <button id="btn-compare">Compare</button>
        </div>
        <table id="cmp-table" class="checks"><tbody></tbody></table>
      </div>
    </section>
```

- [ ] **Step 3: `app.js`** — add after `refreshPostflight` (before `var cfgFields = [];`):

```js
function esc(s) {
  return String(s == null ? "" : s).replace(/&/g, "&amp;").replace(/</g, "&lt;")
    .replace(/>/g, "&gt;").replace(/"/g, "&quot;").replace(/'/g, "&#39;");
}

var snapshots = [];
var reportsJob = null;
var cmpTouched = false;   // user picked A/B by hand; stop auto-selecting the latest pair

function jobText(j) {
  return "Taking " + j.label + " snapshot " + j.index + "/" + j.total +
         (j.current ? " — " + j.current : "") + "…";
}

function refreshReportsJob() {
  getJSON("/api/reports/job", function (d) {
    var was = reportsJob;
    reportsJob = d.job;
    document.getElementById("reports-job").textContent = reportsJob ? jobText(reportsJob) : "";
    document.getElementById("btn-reports-run").disabled = !!reportsJob;
    var msg = document.getElementById("launch-msg");
    if (reportsJob && reportsJob.label === "before") msg.textContent = jobText(reportsJob);
    else if (was && was.label === "before") msg.textContent = "Before snapshot done — upgrade starting.";
    if (was && !reportsJob) refreshReports();
  });
}

function defaultPair() {
  for (var i = 0; i < snapshots.length; i++) {
    if (snapshots[i].label === "after" && snapshots[i].pair_of) {
      return [snapshots[i].pair_of, snapshots[i].id];
    }
  }
  if (snapshots.length >= 2) return [snapshots[1].id, snapshots[0].id];
  var only = snapshots.length ? snapshots[0].id : "";
  return [only, only];
}

function fillSelect(id, chosen) {
  document.getElementById(id).innerHTML = snapshots.map(function (s) {
    return "<option value='" + esc(s.id) + "'" + (s.id === chosen ? " selected" : "") + ">" +
           esc(s.id + " (" + s.label + ", " + s.status + ")") + "</option>";
  }).join("");
}

function refreshReports() {
  getJSON("/api/reports", function (d) {
    var keepA = document.getElementById("cmp-a").value;
    var keepB = document.getElementById("cmp-b").value;
    snapshots = d.snapshots;
    document.querySelector("#reports-list tbody").innerHTML = snapshots.length
      ? snapshots.map(function (s) {
          return "<tr><td>" + esc(s.id) + "</td><td>" + esc(s.label) + "</td><td>" +
                 esc(s.started) + "</td><td>" + esc(s.status) + "</td><td>" +
                 esc(s.note || "") + "</td></tr>";
        }).join("")
      : "<tr><td colspan='5'>No snapshots yet.</td></tr>";
    var pair = defaultPair();
    fillSelect("cmp-a", cmpTouched && keepA ? keepA : pair[0]);
    fillSelect("cmp-b", cmpTouched && keepB ? keepB : pair[1]);
  });
}

var BADGE = { unchanged: "✅", changed: "●", failed: "❌", missing: "⚠️" };

function runCompare() {
  var a = document.getElementById("cmp-a").value;
  var b = document.getElementById("cmp-b").value;
  var body = document.querySelector("#cmp-table tbody");
  if (!a || !b) { body.innerHTML = "<tr><td>Select two snapshots.</td></tr>"; return; }
  body.innerHTML = "<tr><td>" + loadingHTML("Comparing…") + "</td></tr>";
  getJSON("/api/reports/compare?a=" + encodeURIComponent(a) + "&b=" + encodeURIComponent(b),
    function (d) {
      body.innerHTML = "<tr><th></th><th>Report</th><th>Result</th></tr>" +
        d.reports.map(function (r, i) {
          var res = r.status + ((r.added || r.removed) ? " [+" + r.added + " −" + r.removed + "]" : "");
          return "<tr class='cmp-row cmp-" + r.status + "' data-i='" + i + "' data-report='" +
                 esc(r.name) + "'><td>" + BADGE[r.status] + "</td><td>" + esc(r.name) +
                 "</td><td>" + esc(res) + "</td></tr>" +
                 "<tr class='cmp-diff' id='cmp-diff-" + i + "' style='display:none'>" +
                 "<td colspan='3'></td></tr>";
        }).join("");
    });
}

function unifiedHTML(rows) {
  if (!rows.length) return "<p>No differences.</p>";
  return "<pre class='diff'>" + rows.map(function (r) {
    var cls = r.op === "+" ? "add" : (r.op === "-" ? "del" : (r.op === "@" ? "hunk" : "ctx"));
    return "<span class='" + cls + "'>" + esc(r.op === "@" ? r.text : r.op + r.text) + "</span>";
  }).join("") + "</pre>";
}

function sideHTML(rows) {
  if (!rows.length) return "<p>No differences.</p>";
  if (rows.length === 1 && rows[0].op === "skip") {
    return "<p>No differences (" + rows[0].count + " identical lines).</p>";
  }
  return "<table class='diff-side'>" + rows.map(function (r) {
    if (r.op === "skip") {
      return "<tr class='skip'><td colspan='2'>… " + r.count + " unchanged lines …</td></tr>";
    }
    return "<tr class='" + r.op + "'><td>" + esc(r.left) + "</td><td>" + esc(r.right) + "</td></tr>";
  }).join("") + "</table>";
}

function loadDiff(name, cell) {
  var a = document.getElementById("cmp-a").value;
  var b = document.getElementById("cmp-b").value;
  var style = document.getElementById("cmp-style").value;
  var rep = "&report=" + encodeURIComponent(name);
  cell.innerHTML = loadingHTML("Loading diff…");
  getJSON("/api/reports/diff?a=" + encodeURIComponent(a) + "&b=" + encodeURIComponent(b) +
          rep + "&style=" + style, function (d) {
    var links = "<div class='diff-links'>" +
      "<a target='_blank' href='/api/reports/raw?id=" + encodeURIComponent(a) + rep + "'>raw A</a> · " +
      "<a target='_blank' href='/api/reports/raw?id=" + encodeURIComponent(b) + rep + "'>raw B</a>" +
      (d.truncated ? " · <b>diff truncated — see raw</b>" : "") + "</div>";
    cell.innerHTML = links + (style === "unified" ? unifiedHTML(d.rows) : sideHTML(d.rows));
  });
}
```

Note `esc(r.left)` renders `null` (a padded side) as empty — `esc` maps null/undefined to "".

Then add bindings after the existing `document.getElementById("mode").onchange = resetPreflight;` line:

```js
document.getElementById("btn-reports-run").onclick = function () {
  postJSON("/api/reports/run", {}, function (status, d) {
    var el = document.getElementById("reports-job");
    if (status === 202) { el.textContent = "Starting…"; refreshReportsJob(); }
    else { el.textContent = "Error: " + (d.error || status); }
  });
};
document.getElementById("btn-compare").onclick = runCompare;
document.getElementById("cmp-style").onchange = runCompare;
document.getElementById("cmp-a").onchange = function () { cmpTouched = true; };
document.getElementById("cmp-b").onchange = function () { cmpTouched = true; };
document.getElementById("cmp-table").onclick = function (ev) {
  var tr = ev.target.closest ? ev.target.closest("tr.cmp-row") : null;
  if (!tr) return;
  var row = document.getElementById("cmp-diff-" + tr.getAttribute("data-i"));
  if (row.style.display !== "none") { row.style.display = "none"; return; }
  row.style.display = "";
  loadDiff(tr.getAttribute("data-report"), row.firstChild);
};
```

In `launch()`, change the 202 branch to show the before snapshot:

```js
      if (status === 202) {
        if (body.snapshot) { msg.textContent = "Taking before snapshot…"; refreshReportsJob(); }
        else { msg.textContent = "Launched " + mode + "…"; }
      }
```

At the startup section add `refreshReports();` and `refreshReportsJob();` next to the other initial refreshes, and:

```js
setInterval(refreshReportsJob, 3000);
setInterval(refreshReports, 30000);
```

- [ ] **Step 4: `style.css`** — append:

```css
tr.cmp-row { cursor: pointer; }
tr.cmp-changed { background: #fff8e1; }
tr.cmp-failed { background: #fdecea; }
.diff-links { margin: .25rem 0; font-size: .85rem; }
pre.diff { max-height: 32rem; overflow: auto; font-size: .8rem; margin: 0; }
pre.diff span { display: block; min-height: 1em; white-space: pre-wrap; }
pre.diff .add { background: #e6ffed; }
pre.diff .del { background: #ffeef0; }
pre.diff .hunk { color: #6a737d; }
table.diff-side { width: 100%; table-layout: fixed; border-collapse: collapse;
                  font-family: monospace; font-size: .8rem; }
table.diff-side td { white-space: pre-wrap; word-break: break-all; vertical-align: top;
                     padding: 0 .3rem; border-left: 1px solid #ddd; }
table.diff-side tr.replace td { background: #fff5b1; }
table.diff-side tr.delete td:first-child { background: #ffeef0; }
table.diff-side tr.insert td:last-child { background: #e6ffed; }
table.diff-side tr.skip td { color: #6a737d; text-align: center; background: #f6f8fa; }
```

- [ ] **Step 5: Verify** — `node --check nfupgrader/web/app.js` (if node exists); `grep -nE '=>|\blet\b|\bconst\b|`' nfupgrader/web/app.js` → no hits; full suite OK; `sh test/nf_fork_verify.sh | tail -1` → `VERIFY OK`.

- [ ] **Step 6: Commit**

```bash
git add nfupgrader/web/index.html nfupgrader/web/app.js nfupgrader/web/style.css tests/test_web_assets.py
git commit -m "feat(ui): Reports tab with before/after per-report diff"
```

---

### Task 7: Live smoke test (no real upgrade)

- [ ] Start the server on loopback with a harmless launch target and a fixture report list, e.g.
  `INCLOGDIR=<scratch> NFU_UPGRADE_PATH=/bin/true NFU_EFFECTIVE_CONFIG=<scratch>/eff.cfg NFU_LAUNCH_CWD=<scratch> NFU_CNC_CNFG=<scratch>/cnc.cnfg NFU_PORT=18765 NFU_CHECKS=tests/fixtures/checks.test.json NFU_REPORTS=<scratch>/reports.json python3 -m nfupgrader.httpd`
  where `<scratch>/reports.json` holds 2–3 harmless commands (`date +%Y`, `uname -r`, `df -k /`).
- [ ] Via a small urllib client: `POST /api/reports/run` → 202; poll `/api/reports/job` until null; run again; `GET /api/reports/compare` → statuses; `GET /api/reports/diff` both styles; `GET /api/reports/raw` → text.
- [ ] `POST /api/launch {"mode":"upgrade", overrides/reason as needed}` → 202 with `snapshot`; `/api/reports/job` shows the before job; after it finishes the audit shows `report_snapshot` then `launch` activity (`/bin/true` exits immediately, so the watcher will reach the grace path — do not wait 5 minutes; just confirm the before snapshot and no errors).
- [ ] Stop the server.
