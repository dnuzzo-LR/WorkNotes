# nclan-seed + ncland nfdb Eventing Implementation Plan (re-scoped to recovered code)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Wire `ncland` to the nfdb NE-event bus: fill the `ncland_notify.cpp` SUB stub so the warehouse consumes `app.ne.*` (seed) + `db.ne.*` (runtime) events, and build the external `nclan-seed` tool that reads `frame_link` and publishes `app.ne.seed`.

**Architecture — REUSE, don't rebuild.** `cnc/ncland/src` (commit `c417505ee`, recovered 2026-06-04) already has: the `ncland_notify_event_t` struct + kind enum, the **fully-implemented `ncland_notify_dispatch()`** (the entire §11 open/close/dtype/ip table with idempotency + test seams), the `ncland_seed_load_filter()` dtype-allowlist (`wh->dtype_allow`), `warehouse_open_conn_by_ne()`, and all warehouse wiring (`warehouse_init` calls `ncland_notify_init`+`ncland_seed_run`; `warehouse_main_loop` registers `wh->notify_fd` in epoll and calls `ncland_notify_drain` on readiness). The **only gaps**: (1) `ncland_notify_init`/`drain`/`close` are no-op stubs — implement them as a ZMQ SUB on the nfdb bus feeding the existing dispatch; (2) the `frame_link` walk is an explicit TODO — relocate it into the external `nclan-seed` publisher per the spec's "warehouse never reads frame_link" decision.

**Tech Stack:** C++17 (`ncland.mk`), ZeroMQ (`-lzmq`, already linked), `libnfdb` (for `nclan-seed` PUBLISH), `-lsdi`/`getNeIp`/`getTid` for frame_link, `nfunit-test.hpp`. Spec: `~/docs/superpowers/specs/2026-05-27-clan-yaml-design.md` §11. Build: `nmake -f ncland.mk <target>` (no default Makefile).

---

## ⚠️ Prerequisite (gates integration only)

The nfdb bus is stashed/unpushed (`~/Git/niimx-nfdb-merge-stashed`; memory `project_nfdb_event_bus`). Until it lands and `niimxd` binds `nfdb_pub.sock`/`nfdb.sock` and `libnfdb` installs:
- **Unit tasks (1, 4) unblocked** — pure parser/formatter + nfunit.
- **Tasks 2, 5, 6 build the code** but their live behavior + the `-lnfdb` link (Task 5) and integration tests (3, 7) are **GATED**.

Verify `BASE`/`VPATH` before builds (already confirmed aligned this session). Branch: `ncland-start`. Recovered base is committed (`f075fbab8`).

---

## What already exists (verified — do NOT reimplement)

| Symbol / file | State |
|---|---|
| `ncland_notify_event_t {kind, neid, slot, dtype, ip[64]}` (`ncland.h:669`) | ✅ |
| `ncland_notify_evt_kind_t` enum `NCLAND_EVT_NE_{CREATE,DELETE,ENABLE,DISABLE,DTYPE_CHANGE,IP_CHANGE}` (`ncland.h:659`) | ✅ |
| `ncland_notify_dispatch(wh, e)` — full §11 table + idempotency + `g_test_open_calls`/`g_test_close_calls` seams | ✅ done |
| `ncland_seed_load_filter(path, &set)` → `wh->dtype_allow` (consumer ownership filter) | ✅ done |
| `warehouse_open_conn_by_ne(wh, neid, slot, dtype, ip)` | ✅ done |
| Warehouse wiring: `notify_init`+`seed_run` at init; `notify_fd` in epoll + `notify_drain` on readiness (`ncland_warehouse.cpp:97,100,616,717`) | ✅ done |
| `nfunit-test.hpp` harness + `ncland_unit_tests` target | ✅ done |

**Gaps this plan fills:** `ncland_notify_init/drain/close` bodies (Tasks 1–2); external `nclan-seed` publisher (Tasks 4–5); fork/exec wiring (Task 6).

---

# Phase A — Notify drain = nfdb SUB (feeds the existing dispatch)

## Task 1: Pure topic+YAML → ncland_notify_event_t parser

Factor the wire-parsing out of the ZMQ I/O so it unit-tests with no bus.

**Files:** create `cnc/ncland/src/ncland_notify_parse.cpp`, `ncland_notify_parse.h`, `ncland_notify_parse_tests.cpp`.

- [ ] **Step 1: failing test** `ncland_notify_parse_tests.cpp`
```cpp
#include "../../../include/nfunit-test.hpp"
#include "ncland.h"
#include "ncland_notify_parse.h"

static const std::string BODY =
    "{neid: 5, dtype: 245, ip: 10.0.0.1, tid: NODE5, slot: 12}";

TEST("nparse", "P1 app.ne.seed -> CREATE") {
    ncland_notify_event_t e;
    REQUIRE(ncland_notify_parse("app.ne.seed", BODY, &e) == 0);
    REQUIRE(e.kind == NCLAND_EVT_NE_CREATE);
    REQUIRE(e.neid == 5); REQUIRE(e.dtype == 245); REQUIRE(e.slot == 12);
    REQUIRE(std::string(e.ip) == "10.0.0.1");
}
TEST("nparse", "P2 topic->kind") {
    ncland_notify_event_t e;
    REQUIRE(ncland_notify_parse("db.ne.add",    BODY, &e) == 0 && e.kind == NCLAND_EVT_NE_CREATE);
    REQUIRE(ncland_notify_parse("db.ne.delete", BODY, &e) == 0 && e.kind == NCLAND_EVT_NE_DELETE);
    REQUIRE(ncland_notify_parse("db.ne.update", BODY, &e) == 0 && e.kind == NCLAND_EVT_NE_IP_CHANGE);
}
TEST("nparse", "P3 unknown topic / bad body -> -1") {
    ncland_notify_event_t e;
    REQUIRE(ncland_notify_parse("app.other",   BODY,        &e) == -1);
    REQUIRE(ncland_notify_parse("app.ne.seed", "not yaml",  &e) == -1);
}
TEST("nparse", "P4 delete needs only neid") {
    ncland_notify_event_t e;
    REQUIRE(ncland_notify_parse("db.ne.delete", "{neid: 9}", &e) == 0);
    REQUIRE(e.kind == NCLAND_EVT_NE_DELETE && e.neid == 9);
}
```

> `db.ne.update` → `NCLAND_EVT_NE_IP_CHANGE` is deliberate: that dispatch arm does close+reopen, which re-applies whatever changed (ip/dtype). Rationale: a generic DB update can't say what field changed, and close+reopen is the safe superset. Note this in the spec follow-up.

- [ ] **Step 2: header** `ncland_notify_parse.h`
```cpp
#ifndef NCLAND_NOTIFY_PARSE_H
#define NCLAND_NOTIFY_PARSE_H
#include <string>
#include "ncland.h"   /* ncland_notify_event_t */

/**
 * @brief Map an nfdb event (topic + flow-YAML body) to a notify event.
 *        app.ne.seed / db.ne.add -> CREATE; db.ne.delete -> DELETE;
 *        db.ne.update -> IP_CHANGE (close+reopen, re-applies any change).
 * @param topic event topic (frame 0).
 * @param yaml  flow-YAML body "{neid: N, dtype: N, ip: S, tid: S, slot: N}".
 * @param out   filled on success (kind/neid/slot/dtype/ip).
 * @return 0 on success, -1 if topic unrecognised or neid unparseable.
 */
int ncland_notify_parse(const std::string &topic, const std::string &yaml,
                        ncland_notify_event_t *out);
#endif
```

- [ ] **Step 3: implementation** `ncland_notify_parse.cpp`
```cpp
#include "ncland_notify_parse.h"
#include <cstdlib>
#include <cstring>

namespace {
bool kind_for(const std::string &t, ncland_notify_evt_kind_t *k) {
    if (t == "app.ne.seed" || t == "db.ne.add") { *k = NCLAND_EVT_NE_CREATE;    return true; }
    if (t == "db.ne.delete")                    { *k = NCLAND_EVT_NE_DELETE;    return true; }
    if (t == "db.ne.update")                    { *k = NCLAND_EVT_NE_IP_CHANGE; return true; }
    return false;
}
bool flow_get(const std::string &y, const std::string &key, std::string &out) {
    std::string needle = key + ":";
    size_t pos = 0;
    while ((pos = y.find(needle, pos)) != std::string::npos) {
        char b = pos ? y[pos-1] : '{';
        if (b == '{' || b == ' ' || b == ',') break;
        pos += needle.size();
    }
    if (pos == std::string::npos) return false;
    size_t v = pos + needle.size();
    while (v < y.size() && y[v] == ' ') ++v;
    size_t e = v;
    while (e < y.size() && y[e] != ',' && y[e] != '}') ++e;
    out = y.substr(v, e - v);
    while (!out.empty() && out.back() == ' ') out.pop_back();
    return true;
}
} // namespace

int ncland_notify_parse(const std::string &topic, const std::string &yaml,
                        ncland_notify_event_t *out)
{
    if (!out) return -1;
    ncland_notify_evt_kind_t k;
    if (!kind_for(topic, &k)) return -1;
    if (yaml.empty() || yaml[0] != '{') return -1;

    std::string s;
    if (!flow_get(yaml, "neid", s) || s.empty()) return -1;

    memset(out, 0, sizeof(*out));
    out->kind = k;
    out->neid = atoi(s.c_str());
    if (flow_get(yaml, "dtype", s) && !s.empty()) out->dtype = atoi(s.c_str());
    if (flow_get(yaml, "slot",  s) && !s.empty()) out->slot  = atoi(s.c_str());
    if (flow_get(yaml, "ip", s)) { strncpy(out->ip, s.c_str(), sizeof(out->ip)-1); }
    return 0;
}
```

- [ ] **Step 4: link tests** — in `ncland.mk`, add `ncland_notify_parse.o ncland_notify_parse_tests.o` to the `ncland_unit_tests` prerequisite list (use `writing-nmake-makefiles`; keep recipe TABs).

- [ ] **Step 5: build + run**
```bash
cd /home/dan/Git/netflex/cnc/ncland/src
nmake -f ncland.mk ncland_unit_tests
./ncland_unit_tests nparse
```
Expected: P1–P4 pass.

- [ ] **Step 6: commit**
```bash
cd /home/dan/Git/netflex
git add cnc/ncland/src/ncland_notify_parse.h cnc/ncland/src/ncland_notify_parse.cpp \
        cnc/ncland/src/ncland_notify_parse_tests.cpp cnc/ncland/src/ncland.mk
git commit -m "ncland: pure nfdb topic+YAML -> ncland_notify_event_t parser + tests

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 2: Implement ncland_notify_init/drain/close as the nfdb SUB

Replace the stub bodies in `ncland_notify.cpp`. Keep `ncland_notify_dispatch` untouched.

**Files:** modify `cnc/ncland/src/ncland_notify.cpp`, `cnc/ncland/src/ncland.h` (one field).

- [ ] **Step 1: add a SUB-socket field to `ncland_wh_t`** in `ncland.h` (next to `notify_fd`):
```cpp
    void   *notify_sub;      /**< ZMQ SUB socket on the nfdb XPUB endpoint; NULL if not connected. */
```
Initialise it to `nullptr` wherever the warehouse zeroes its fields (alongside `wh->notify_fd = -1;` in `warehouse_init`).

- [ ] **Step 2: find the existing ZMQ context** the warehouse uses (the ROUTER/ctl in `ncland_zmq.cpp` creates one). Reuse it for the SUB. Run:
```bash
grep -nE 'zmq_ctx_new|zmq_ctx|->ctx|G\.ctx|wh->.*ctx' cnc/ncland/src/ncland_zmq.cpp cnc/ncland/src/ncland.h
```
Use the discovered context accessor in `ncland_notify_init`. If there is genuinely no shared context reachable from `wh`, create one in `ncland_notify_init` and store it (report this as DONE_WITH_CONCERNS so the controller can confirm).

- [ ] **Step 3: rewrite the three functions** in `ncland_notify.cpp` (add includes `#include <zmq.h>`, `#include "ncland_notify_parse.h"`; keep `#include "ncland.h"`/`"nflog.hpp"`):
```cpp
#define NCLAND_NFDB_PUB_ENDPOINT "ipc:///usr/cnc/data/nfdb_pub.sock"

int ncland_notify_init(ncland_wh_t *wh)
{
    if (!wh) return -1;
    wh->notify_fd  = -1;
    wh->notify_sub = nullptr;

    void *ctx = /* the shared ZMQ context found in Step 2 */;
    void *sub = zmq_socket(ctx, ZMQ_SUB);
    if (!sub) { LOG_ERROR("notify: zmq_socket(SUB) failed"); return -1; }
    if (zmq_connect(sub, NCLAND_NFDB_PUB_ENDPOINT) != 0) {
        LOG_WARN("notify: connect %s failed (degraded, no events)",
                 NCLAND_NFDB_PUB_ENDPOINT);
        zmq_close(sub);
        return 0;   /* non-fatal: warehouse runs without lifecycle events */
    }
    zmq_setsockopt(sub, ZMQ_SUBSCRIBE, "app.ne.", 7);
    zmq_setsockopt(sub, ZMQ_SUBSCRIBE, "db.ne.",  6);

    int fd = -1; size_t fsz = sizeof(fd);
    zmq_getsockopt(sub, ZMQ_FD, &fd, &fsz);
    wh->notify_sub = sub;
    wh->notify_fd  = fd;
    LOG_INFO("notify: subscribed app.ne./db.ne. on %s (fd=%d)",
             NCLAND_NFDB_PUB_ENDPOINT, fd);
    return 0;
}

void ncland_notify_close(ncland_wh_t *wh)
{
    if (!wh) return;
    if (wh->notify_sub) { zmq_close(wh->notify_sub); wh->notify_sub = nullptr; }
    wh->notify_fd = -1;
}

int ncland_notify_drain(ncland_wh_t *wh)
{
    if (!wh || !wh->notify_sub) return 0;
    int events; size_t esz = sizeof(events);
    while (true) {
        if (zmq_getsockopt(wh->notify_sub, ZMQ_EVENTS, &events, &esz) != 0) break;
        if (!(events & ZMQ_POLLIN)) break;

        zmq_msg_t m; std::string topic, body; bool more = false;
        zmq_msg_init(&m);
        if (zmq_msg_recv(&m, wh->notify_sub, ZMQ_DONTWAIT) < 0) { zmq_msg_close(&m); break; }
        topic.assign((char*)zmq_msg_data(&m), zmq_msg_size(&m));
        more = zmq_msg_more(&m); zmq_msg_close(&m);
        if (more) {
            zmq_msg_init(&m);
            if (zmq_msg_recv(&m, wh->notify_sub, ZMQ_DONTWAIT) >= 0)
                body.assign((char*)zmq_msg_data(&m), zmq_msg_size(&m));
            more = zmq_msg_more(&m); zmq_msg_close(&m);
        }
        while (more) { zmq_msg_init(&m); zmq_msg_recv(&m, wh->notify_sub, ZMQ_DONTWAIT);
                       more = zmq_msg_more(&m); zmq_msg_close(&m); }   /* defensive */

        ncland_notify_event_t e;
        if (ncland_notify_parse(topic, body, &e) == 0)
            ncland_notify_dispatch(wh, &e);
        else
            LOG_WARN("notify: dropped unparseable event topic=%s", topic.c_str());
    }
    return 0;
}
```

- [ ] **Step 4: build** (`-lzmq` already on the test/main link lines)
```bash
cd /home/dan/Git/netflex/cnc/ncland/src
nmake -f ncland.mk ncland_unit_tests
./ncland_unit_tests
```
Expected: clean build; all suites still pass (the existing `ncland_notify_dispatch` tests + new `nparse`). The init/drain bodies aren't exercised by unit tests (no bus) — that's Task 3.

- [ ] **Step 5: commit**
```bash
cd /home/dan/Git/netflex
git add cnc/ncland/src/ncland_notify.cpp cnc/ncland/src/ncland.h
git commit -m "ncland: implement notify init/drain/close as nfdb ZMQ SUB feeding dispatch

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 3: Notify integration test (GATED — niimxd+nfdb)

**Files:** create `cnc/ncland/src/test_ncland_notify.sh` (model on `test/integration/it_smoke.sh`).

- [ ] **Step 1:** start `niimxd`, run `ncland` (or a minimal harness that calls `ncland_notify_init`+`ncland_notify_drain`), then:
```bash
BINDIR=../../../3b2/bin
"$BINDIR/nfdb" PUBLISH app.ne.seed "{neid: 5, dtype: 245, ip: 10.0.0.1, tid: NODE5, slot: 12}"
```
Assert (via ncland trace log) that dispatch fired `try_open` for neid 5 (subject to dtype 245 being in `ncland.json`). `# GATED: nfdb not pushed`.

- [ ] **Step 2: commit** the script.

---

# Phase B — nclan-seed external publisher

The `ncland_seed.cpp` in-process `frame_link` walk (its `ncland_seed_for_each_ne` TODO) is **superseded** by this tool per spec §4.4 ("warehouse never reads frame_link"). `ncland_seed_load_filter` (the `ncland.json` dtype allowlist) **stays** — it's the consumer-side `wh->dtype_allow` filter. `ncland_seed_run` becomes vestigial (prod no-op); Task 6 replaces its call site with a fork/exec of `nclan-seed`.

## Task 4: Pure NE identity formatter + skip predicate

**Files:** create `nclan_seed_fmt.h`, `nclan_seed_fmt.cpp`, `nclan_seed_tests.cpp` (suite `seedfmt`). Wire into `ncland_unit_tests` via `ncland.mk`.

(Identical to the formatter in the prior plan revision — `NeIdentity{neid,dtype,ip,tid,slot}`, `ncland_seed_keep(nfrmnum,dtype)` using `UNASSIGNED`/`MAX_NE_TYPES` from `<dcompool.h>`, `ncland_seed_format()` → `"{neid: 5, dtype: 245, ip: 10.0.0.1, tid: NODE5, slot: 12}"`. Tests S1–S3. See git history of this plan file for the full code, or reproduce the three S-tests + the two functions verbatim.)

- [ ] Steps: write failing `seedfmt` tests → header → impl → add `nclan_seed_fmt.o nclan_seed_tests.o` to `ncland_unit_tests` in `ncland.mk` → `./ncland_unit_tests seedfmt` green → commit.

> Reuse note: the format string MUST match `ncland_notify_parse`'s expected body exactly (same keys/order/spacing), so seed events round-trip through Task 1's parser. Add one test asserting `ncland_notify_parse("app.ne.seed", ncland_seed_format(ne), &e)` recovers the identity — this locks producer/consumer compatibility.

## Task 5: nclan-seed main (frame_link walk → PUBLISH app.ne.seed)

**Files:** create `cnc/ncland/src/nclan_seed.cpp`; add a `$(PBIN)/nclan-seed` target to `ncland.mk`.

- [ ] Implement per `cnc/rcmd/src/neinfo.c`: `set_tunables(); Frmlnk=(struct frame_link*)atch_frm(&start);` (check `==FAILURE`); iterate `ne=1..NeAvail`; `if (!ncland_seed_keep(Frmlnk[ne].nfrmnum, Frmlnk[ne].dcs_type[0])) continue;`; `getNeIp(ne)`/`getTid(ne)`; `ncland_seed_format(...)`; publish via `libnfdb`:
```cpp
extern "C" { struct NFDB; NFDB *nfdb_connect(const char*); int nfdb_command(NFDB*,const char*,...); void nfdb_close(NFDB*); }
// ... nfdb_command(conn, "PUBLISH app.ne.seed \"%s\"", body.c_str());
```
CLI `-e <nfdb.sock>` `-t <dtype>` `-n`(dry-run). Includes `<dcompool.h>`, `<utillibinc.h>`, `<misc_util.h>`, `"nclan_seed_fmt.h"`. (Full main is in this plan file's git history — reproduce it, swapping the prior `cnc/sdi/clan2/` paths for `cnc/ncland/src`.)

- [ ] `ncland.mk` target:
```
$(PBIN)/nclan-seed :: nclan_seed.o nclan_seed_fmt.o $(INCLIBS) -latm -linc -lxmlparse -lssh -ljson -lnetclan
	$(CPLUS_CC) $(CCFLAGS:M!=-AC99) $(LDFLAGS) -o $(<) $(*) -lpthread -lssh -lrt -llua -lnfdb -lzmq
```
- [ ] Build (`nmake -f ncland.mk ../../../3b2/bin/nclan-seed`) — GATED on `-lnfdb`. `-n` dry-run smoke test needs only shm. Commit.

## Task 6: Seed via fork/exec after SUB is live

**Files:** modify `cnc/ncland/src/ncland_warehouse.cpp`.

- [ ] In `warehouse_init`, the existing sequence is `ncland_notify_init(wh)` then `ncland_seed_run(wh)`. Per spec model-B: once the SUB is established (notify_init succeeded and `wh->notify_fd >= 0`), `fork`/`exec` `$(PBIN)/nclan-seed` so its `app.ne.seed` events arrive on the now-live SUB. Replace the `ncland_seed_run(wh)` call's role:
  - Keep `ncland_seed_load_filter()` (loads `wh->dtype_allow`) wherever it currently runs.
  - Replace in-process seeding with: `if (wh->notify_fd >= 0) ncland_spawn_seeder();` (a small `fork`+`execl` of the nclan-seed binary; log on failure, non-fatal).
  - Leave `ncland_seed_run` defined (vestigial no-op in prod) to avoid touching its unit tests; just stop relying on it for prod seeding.
- [ ] Build `ncland`, confirm `ncland_unit_tests` still green. Commit.

## Task 7: End-to-end seeding integration (GATED)

- [ ] With niimxd+nfdb live and a seeded `frame_link`: start `ncland`; confirm the forked `nclan-seed` publishes `app.ne.seed` per NE and the warehouse opens conns for allowlisted dtypes (trace log / `g_test_open_calls` analogue). `# GATED`.

---

# Done

- `ncland_notify.cpp`: real nfdb SUB (init/drain/close) feeding the **existing** `ncland_notify_dispatch` — `app.ne.*` (seed) + `db.ne.*` (runtime) unified path.
- `nclan-seed`: external `frame_link → app.ne.seed` publisher (took over the in-process walk TODO).
- Reused as-is: dispatch table, event struct, dtype allowlist, warehouse epoll wiring.

**Spec coverage:** §11.1 payload + parser → Task 1. §11.2 wiring (SUB drain, edge-triggered, dispatch) → Task 2 (epoll registration already in warehouse). §11.3 failure (non-fatal connect, drop unparseable) → Task 2. §11.5 nclan-seed → Tasks 4–5. §4.4 fork/exec after SUB live → Task 6. §13.5/13.5a → Tasks 1,3,4,7.

**Spec follow-ups to note:** (a) `db.ne.update → IP_CHANGE` (close+reopen as the safe superset) — confirm vs a finer field-diff later; (b) `ncland_seed_run` retired in favour of the external tool — update spec §4.4 prose if desired.
```
