# ncland YAML Loader / Registry Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the ncland YAML registry — load `ne/*.yaml` from `NCLAND_NE_DIR`, apply `_defaults` + `extends:`/`abstract:` inheritance, validate, compile prompt regexes, and expose a lookup keyed by numeric `dtype` — so the warehouse can resolve per-NE config at session-open.

**Architecture:** A new `ncland_registry.{h,cpp}` using **yaml-cpp** (installed, `-lyaml-cpp`). Pure, layered functions (merge → resolve extends → validate → build entry) each unit-tested via `nfunit-test.hpp`, then a directory loader and a one-call warehouse hook. The registry maps **numeric dtype → `ne_entry_t`** (events carry the numeric `dcs_type`; the name↔number map comes from `extern char *dtypes[]`, `include/dgcounts.h`). Step-language blocks (`post_command`/`disconnect_steps`) are stored as raw YAML for the future stepper (#3); this plan does not parse them.

**Tech Stack:** C++17, yaml-cpp (`-lyaml-cpp`), POSIX `regcomp` (prompt regexes), `dtypes[]` (dgcounts.h), nfunit-test.hpp, Lucent/AT&T nmake. Spec: `~/docs/superpowers/specs/2026-05-27-clan-yaml-design.md` §6, §7, §10.1. Build: `nmake -f ncland.mk <target>`.

---

## Prerequisites / facts (verified)

- yaml-cpp lib present: `/usr/lib64/libyaml-cpp.so` (0.6.2); header `<yaml-cpp/yaml.h>`.
- `NCLAND_NE_DIR "/usr/cnc/lib/data/ncland"` already defined in `ncland.h`.
- `extern char *dtypes[];` in `include/dgcontnts.h`→ actually `include/dgcounts.h:39`; `dtypes[n]` is the enum name for numeric dtype `n`. `MAX_NE_TYPES` (from `ne_defs.h`) bounds it. e.g. `CIENA_RLS==89`, `dtypes[89]=="CIENA_RLS"`.
- Real `ne/*.yaml` shape: top-level scalars (`ne_type`,`display_name`,`vendor`,`schema_version`,`lua_module`), maps (`transport`,`prompt`,`timeouts`,`keepalive`,`paging`), lists (`errors`,`post_command`,`disconnect_steps`), plus optional `extends:` / `abstract:`.
- Tests run in `cnc/ncland/src`; add suites to the `ncland_unit_tests` target.

---

## File Structure

**Create:**
- `cnc/ncland/src/ncland_registry.h` — `ne_entry_t`, `ncland_registry_t`, and the public API (`ncland_registry_load`, `ncland_registry_find`, helpers).
- `cnc/ncland/src/ncland_registry.cpp` — implementation (merge, extends resolution, validation, regex compile, dir load).
- `cnc/ncland/src/ncland_registry_tests.cpp` — nfunit suites (`reg`).

**Modify:**
- `cnc/ncland/src/ncland.mk` — add `-lyaml-cpp` where ncland links; add `ncland_registry.o` to the daemon + `ncland_registry.o ncland_registry_tests.o` to the test target.
- `cnc/ncland/src/ncland.h` — declare nothing new here unless a shared type is needed; the registry types live in `ncland_registry.h`.
- `cnc/ncland/src/ncland_warehouse.cpp` — load the registry once in `warehouse_init` (Task 7).

---

## Task 1: Registry header + yaml-cpp links (smoke)

**Files:** create `ncland_registry.h`, `ncland_registry.cpp`, `ncland_registry_tests.cpp`; modify `ncland.mk`.

- [ ] **Step 1: Write `ncland_registry.h`** (the data model + API)
```cpp
#ifndef NCLAND_REGISTRY_H
#define NCLAND_REGISTRY_H

#include <string>
#include <vector>
#include <unordered_map>
#include <regex.h>
#include <yaml-cpp/yaml.h>

/** @brief Fully-resolved per-NE config (one concrete ne/*.yaml after merge). */
struct ne_entry_t {
    std::string ne_type;        /**< Enum name, e.g. "CIENA_RLS". */
    int         dtype = -1;     /**< Numeric dcs_type (events key on this). */
    std::string vendor;
    std::string display_name;
    int         schema_version = 0;

    /* transport */
    std::vector<std::string> protocol;       /**< Preferred order, e.g. {"ssh","telnet"}. */
    int    default_port      = 23;
    int    banner_timeout_s  = 30;
    size_t max_buffer_bytes  = 1048576;

    /* prompt */
    std::string primary_regex;
    std::string secondary_regex;
    bool    need_newline_after_connect = false;
    bool    adapt_from_actual          = false;
    regex_t prmpt1re;  bool prmpt1_valid = false;
    regex_t prmpt2re;  bool prmpt2_valid = false;

    /* timeouts */
    int login_s = 60, command_s = 300, inter_command_ms = 0;

    /* keepalive */
    bool        ka_enabled = false;
    int         ka_interval_s = 600;
    std::string ka_command = "\n";
    bool        ka_expect_response = true;

    /* paging */
    std::string more_pattern, more_response;
    bool        large_output_scan = false;

    /* errors (regex strings; compiled lazily by the stepper later) */
    std::vector<std::string> errors;

    /* lua */
    std::string lua_module;

    /* raw step blocks — parsed by the stepper subsystem (#3), not here */
    YAML::Node post_command;
    YAML::Node disconnect_steps;
};

/** @brief The loaded registry: numeric dtype -> entry. */
struct ncland_registry_t {
    std::unordered_map<int, ne_entry_t> by_dtype;
};

/**
 * @brief Load all ne/<name>.yaml under dir into reg (clears reg first).
 * @param dir  Directory to scan (e.g. NCLAND_NE_DIR).
 * @param reg  Output registry.
 * @param errc Optional: number of files skipped due to errors (may be NULL).
 * @return 0 on success (even if some NE types were skipped), -1 if dir unreadable.
 */
int ncland_registry_load(const char *dir, ncland_registry_t *reg, int *errc);

/** @brief Find a loaded entry by numeric dtype; NULL if absent. */
const ne_entry_t *ncland_registry_find(const ncland_registry_t *reg, int dtype);

#endif /* NCLAND_REGISTRY_H */
```

- [ ] **Step 2: Write a stub `ncland_registry.cpp`**
```cpp
#include "ncland_registry.h"

int ncland_registry_load(const char *, ncland_registry_t *reg, int *errc)
{
    if (reg) reg->by_dtype.clear();
    if (errc) *errc = 0;
    return 0;
}

const ne_entry_t *ncland_registry_find(const ncland_registry_t *reg, int dtype)
{
    if (!reg) return nullptr;
    auto it = reg->by_dtype.find(dtype);
    return (it == reg->by_dtype.end()) ? nullptr : &it->second;
}
```

- [ ] **Step 3: Write the smoke test** `ncland_registry_tests.cpp`
```cpp
#include "../../../include/nfunit-test.hpp"
#include "ncland_registry.h"

TEST("reg", "R1 yaml-cpp parses a node") {
    YAML::Node n = YAML::Load("{a: 1, b: [x, y]}");
    REQUIRE(n["a"].as<int>() == 1);
    REQUIRE(n["b"].size() == 2);
    REQUIRE(n["b"][0].as<std::string>() == "x");
}
TEST("reg", "R2 empty registry find is null") {
    ncland_registry_t reg;
    REQUIRE(ncland_registry_find(&reg, 89) == nullptr);
}
```

- [ ] **Step 4: Wire the Makefile** (use `writing-nmake-makefiles`)
In `ncland.mk`: append `-lyaml-cpp` to the `$(PBIN)/ncland` recipe and the `ncland_unit_tests` recipe link lines; add `ncland_registry.o` to the `ncland` prereqs and `ncland_registry.o ncland_registry_tests.o` to the `ncland_unit_tests` prereqs. (yaml-cpp headers are in the default system include path; if not, add `-I/usr/include` is already implied — verify the compile finds `<yaml-cpp/yaml.h>`.)

- [ ] **Step 5: Build + run**
```
cd /home/dan/Git/netflex/cnc/ncland/src
nmake -f ncland.mk ncland_unit_tests
./ncland_unit_tests reg
```
Expected: R1, R2 pass. If `<yaml-cpp/yaml.h>` isn't found, locate it (`find /usr/include -name yaml.h -path '*yaml-cpp*'`) and add its `-I` to `CCFLAGS` in ncland.mk.

- [ ] **Step 6: Commit**
```
git add cnc/ncland/src/ncland_registry.h cnc/ncland/src/ncland_registry.cpp \
        cnc/ncland/src/ncland_registry_tests.cpp cnc/ncland/src/ncland.mk
git commit -m "ncland: registry skeleton + yaml-cpp wired into build"
```

---

## Task 2: ne_type name ↔ numeric dtype

**Files:** modify `ncland_registry.cpp`, `.h`, `_tests.cpp`.

- [ ] **Step 1: Failing test**
```cpp
TEST("reg", "R3 dtype name<->number via dtypes[]") {
    REQUIRE(ncland_registry_dtype_for_name("CIENA_RLS") == 89);   // ne_defs.h CIENA_RLS=89
    REQUIRE(ncland_registry_dtype_for_name("NOT_A_REAL_TYPE") == -1);
}
```

- [ ] **Step 2: Declare in `ncland_registry.h`** (before the `#endif`)
```cpp
/** @brief Numeric dtype for an enum name via dtypes[], or -1 if unknown. */
int ncland_registry_dtype_for_name(const std::string &name);
```

- [ ] **Step 3: Implement in `ncland_registry.cpp`** (add near top)
```cpp
extern "C" {
#include <ne_defs.h>     /* MAX_NE_TYPES */
}
extern char *dtypes[];   /* include/dgcounts.h: dtypes[n] == enum name for n */

int ncland_registry_dtype_for_name(const std::string &name)
{
    for (int i = 0; i < MAX_NE_TYPES; ++i)
        if (dtypes[i] && name == dtypes[i]) return i;
    return -1;
}
```
> If `ne_defs.h` is C++-hostile inside `extern "C"`, include it without the wrapper (it is under `common/include`, already on VPATH). Confirm `MAX_NE_TYPES` resolves.

- [ ] **Step 4: Build + run** `./ncland_unit_tests reg` → R3 passes (links `-lcncdb`/whatever defines `dtypes[]`; it's already on ncland's link line).
- [ ] **Step 5: Commit** `git commit -m "ncland: registry ne_type<->dtype mapping via dtypes[]"`

---

## Task 3: Deep-merge of YAML nodes (maps merge, lists replace)

**Files:** modify `ncland_registry.cpp`, `.h`, `_tests.cpp`.

- [ ] **Step 1: Failing test**
```cpp
TEST("reg", "R4 deep-merge: maps merge, child wins, lists replace") {
    YAML::Node base = YAML::Load("{transport: {protocol: telnet, default_port: 23}, errors: [a, b]}");
    YAML::Node over = YAML::Load("{transport: {protocol: ssh}, errors: [c]}");
    YAML::Node m = ncland_registry_merge(base, over);
    REQUIRE(m["transport"]["protocol"].as<std::string>() == "ssh");   // child wins
    REQUIRE(m["transport"]["default_port"].as<int>() == 23);          // base kept
    REQUIRE(m["errors"].size() == 1);                                 // list replaced
    REQUIRE(m["errors"][0].as<std::string>() == "c");
}
```

- [ ] **Step 2: Declare** in `.h`:
```cpp
/** @brief Deep-merge over onto base: maps merge recursively (over wins),
 *  scalars and sequences from over replace base wholesale. Returns a new node. */
YAML::Node ncland_registry_merge(const YAML::Node &base, const YAML::Node &over);
```

- [ ] **Step 3: Implement**
```cpp
YAML::Node ncland_registry_merge(const YAML::Node &base, const YAML::Node &over)
{
    if (!over) return YAML::Clone(base);
    if (!base) return YAML::Clone(over);
    if (!over.IsMap() || !base.IsMap())
        return YAML::Clone(over);   /* scalar/sequence: over replaces */

    YAML::Node out = YAML::Clone(base);
    for (auto it = over.begin(); it != over.end(); ++it) {
        std::string key = it->first.as<std::string>();
        out[key] = out[key] ? ncland_registry_merge(out[key], it->second)
                            : YAML::Clone(it->second);
    }
    return out;
}
```

- [ ] **Step 4: Build + run** → R4 passes.
- [ ] **Step 5: Commit** `git commit -m "ncland: registry deep-merge (maps merge, lists replace)"`

---

## Task 4: extends-chain resolution (index, walk, cycle/missing)

**Files:** modify `ncland_registry.cpp`, `.h`, `_tests.cpp`.

- [ ] **Step 1: Failing test**
```cpp
TEST("reg", "R5 extends chain merges defaults->ancestors->self") {
    std::unordered_map<std::string, YAML::Node> idx;
    idx["_defaults"]  = YAML::Load("{transport: {protocol: telnet, default_port: 23}}");
    idx["ciena.base"] = YAML::Load("{vendor: ciena, transport: {protocol: ssh}}");
    idx["ciena_rls"]  = YAML::Load("{ne_type: CIENA_RLS, extends: ciena.base, transport: {default_port: 22}}");
    std::string err;
    YAML::Node m = ncland_registry_resolve(idx, "ciena_rls", &err);
    REQUIRE(err.empty());
    REQUIRE(m["vendor"].as<std::string>() == "ciena");                 // from base
    REQUIRE(m["transport"]["protocol"].as<std::string>() == "ssh");    // base over defaults
    REQUIRE(m["transport"]["default_port"].as<int>() == 22);           // self over base
}
TEST("reg", "R6 extends cycle + missing parent are errors") {
    std::unordered_map<std::string, YAML::Node> idx;
    idx["a"] = YAML::Load("{extends: b}");
    idx["b"] = YAML::Load("{extends: a}");
    idx["c"] = YAML::Load("{extends: nope}");
    std::string err;
    REQUIRE(!ncland_registry_resolve(idx, "a", &err)); REQUIRE(!err.empty());
    err.clear();
    REQUIRE(!ncland_registry_resolve(idx, "c", &err)); REQUIRE(!err.empty());
}
```

- [ ] **Step 2: Declare**
```cpp
/** @brief Resolve a file's full config: _defaults -> extends ancestors (root first)
 *  -> self. idx maps basename -> raw parsed node (must include "_defaults" if present).
 *  On cycle / missing parent, returns a null Node and sets *err. */
YAML::Node ncland_registry_resolve(
    const std::unordered_map<std::string, YAML::Node> &idx,
    const std::string &name, std::string *err);
```

- [ ] **Step 3: Implement**
```cpp
#include <vector>
#include <set>

YAML::Node ncland_registry_resolve(
    const std::unordered_map<std::string, YAML::Node> &idx,
    const std::string &name, std::string *err)
{
    /* Walk extends from self up to the root, detecting cycles/missing. */
    std::vector<std::string> chain;     /* self .. root */
    std::set<std::string> seen;
    std::string cur = name;
    while (true) {
        if (seen.count(cur)) { if (err) *err = "extends cycle at " + cur; return YAML::Node(); }
        seen.insert(cur);
        auto it = idx.find(cur);
        if (it == idx.end()) { if (err) *err = "missing parent " + cur; return YAML::Node(); }
        chain.push_back(cur);
        const YAML::Node &n = it->second;
        if (!n["extends"]) break;
        cur = n["extends"].as<std::string>();
    }
    /* Build: _defaults (if present) then root..self. */
    YAML::Node acc;
    auto def = idx.find("_defaults");
    if (def != idx.end()) acc = YAML::Clone(def->second);
    for (auto rit = chain.rbegin(); rit != chain.rend(); ++rit)
        acc = ncland_registry_merge(acc, idx.at(*rit));
    if (err) err->clear();
    return acc;
}
```

- [ ] **Step 4: Build + run** → R5, R6 pass.
- [ ] **Step 5: Commit** `git commit -m "ncland: registry extends resolution (cycle/missing detection)"`

---

## Task 5: Build + validate one entry from a merged node

**Files:** modify `ncland_registry.cpp`, `.h`, `_tests.cpp`.

- [ ] **Step 1: Failing test**
```cpp
TEST("reg", "R7 build entry from merged node") {
    YAML::Node n = YAML::Load(
        "ne_type: CIENA_RLS\n"
        "vendor: ciena\n"
        "schema_version: 1\n"
        "transport: {protocol: [ssh, telnet], default_port: 22}\n"
        "prompt: {primary_regex: '[a-z]+#', need_newline_after_connect: true}\n"
        "keepalive: {enabled: true, interval_s: 600, command: \"x\\n\"}\n"
        "errors: ['Error:', 'Denied']\n"
        "lua_module: ciena_rls.lua\n");
    ne_entry_t e; std::string err;
    REQUIRE(ncland_registry_build(n, &e, &err) == 0);
    REQUIRE(e.ne_type == "CIENA_RLS"); REQUIRE(e.dtype == 89);
    REQUIRE(e.protocol.size() == 2); REQUIRE(e.protocol[0] == "ssh");
    REQUIRE(e.default_port == 22);
    REQUIRE(e.need_newline_after_connect == true);
    REQUIRE(e.prmpt1_valid == true);
    REQUIRE(e.ka_enabled == true);
    REQUIRE(e.errors.size() == 2);
    REQUIRE(e.lua_module == "ciena_rls.lua");
}
TEST("reg", "R8 validation rejects missing required + bad regex") {
    ne_entry_t e; std::string err;
    REQUIRE(ncland_registry_build(YAML::Load("vendor: x"), &e, &err) != 0);          // no ne_type
    REQUIRE(ncland_registry_build(
        YAML::Load("ne_type: CIENA_RLS\nvendor: c\nschema_version: 1\n"
                   "transport: {protocol: ssh}\nprompt: {primary_regex: '('}\n"     // bad regex
                   "lua_module: x.lua\n"), &e, &err) != 0);
}
```

- [ ] **Step 2: Declare**
```cpp
/** @brief Populate *out from a fully-merged node; compile prompt regexes.
 *  Validates required keys (§7.2), ne_type<->dtype round-trip, regex compile.
 *  @return 0 on success, -1 on validation error (*err set, no regex leaked). */
int ncland_registry_build(const YAML::Node &n, ne_entry_t *out, std::string *err);
```

- [ ] **Step 3: Implement** (handle scalar-or-list `protocol`; compile `primary_regex`/`secondary_regex` with `regcomp(REG_EXTENDED)`; required: `ne_type`, `vendor`, `schema_version`, `transport.protocol`, `prompt.primary_regex`, `lua_module`)
```cpp
static void to_list(const YAML::Node &n, std::vector<std::string> *out) {
    out->clear();
    if (!n) return;
    if (n.IsSequence()) { for (auto &x : n) out->push_back(x.as<std::string>()); }
    else                  out->push_back(n.as<std::string>());
}

int ncland_registry_build(const YAML::Node &n, ne_entry_t *out, std::string *err)
{
    auto fail = [&](const std::string &m){ if (err) *err = m; return -1; };
    if (!n || !n.IsMap())                 return fail("not a map");
    if (!n["ne_type"])                    return fail("missing ne_type");
    out->ne_type = n["ne_type"].as<std::string>();
    out->dtype   = ncland_registry_dtype_for_name(out->ne_type);
    if (out->dtype < 0)                   return fail("ne_type not a known dtype: " + out->ne_type);
    if (!n["vendor"])                     return fail("missing vendor");
    out->vendor = n["vendor"].as<std::string>();
    if (!n["schema_version"])             return fail("missing schema_version");
    out->schema_version = n["schema_version"].as<int>();
    if (out->schema_version != 1)         return fail("unsupported schema_version");
    if (n["display_name"]) out->display_name = n["display_name"].as<std::string>();
    if (!n["lua_module"])                 return fail("missing lua_module");
    out->lua_module = n["lua_module"].as<std::string>();

    const YAML::Node t = n["transport"];
    if (!t || !t["protocol"])             return fail("missing transport.protocol");
    to_list(t["protocol"], &out->protocol);
    if (out->protocol.empty())            return fail("empty transport.protocol");
    if (t["default_port"])     out->default_port     = t["default_port"].as<int>();
    if (t["banner_timeout_s"]) out->banner_timeout_s = t["banner_timeout_s"].as<int>();
    if (t["max_buffer_bytes"]) out->max_buffer_bytes = t["max_buffer_bytes"].as<size_t>();

    const YAML::Node p = n["prompt"];
    if (!p || !p["primary_regex"])        return fail("missing prompt.primary_regex");
    out->primary_regex = p["primary_regex"].as<std::string>();
    if (regcomp(&out->prmpt1re, out->primary_regex.c_str(), REG_EXTENDED) != 0)
        return fail("primary_regex failed to compile");
    out->prmpt1_valid = true;
    if (p["secondary_regex"]) {
        out->secondary_regex = p["secondary_regex"].as<std::string>();
        if (!out->secondary_regex.empty() &&
            regcomp(&out->prmpt2re, out->secondary_regex.c_str(), REG_EXTENDED) != 0) {
            regfree(&out->prmpt1re); out->prmpt1_valid = false;
            return fail("secondary_regex failed to compile");
        }
        out->prmpt2_valid = !out->secondary_regex.empty();
    }
    if (p["need_newline_after_connect"]) out->need_newline_after_connect = p["need_newline_after_connect"].as<bool>();
    if (p["adapt_from_actual"])          out->adapt_from_actual          = p["adapt_from_actual"].as<bool>();

    if (const YAML::Node tm = n["timeouts"]) {
        if (tm["login_s"])          out->login_s          = tm["login_s"].as<int>();
        if (tm["command_s"])        out->command_s        = tm["command_s"].as<int>();
        if (tm["inter_command_ms"]) out->inter_command_ms = tm["inter_command_ms"].as<int>();
    }
    if (const YAML::Node k = n["keepalive"]) {
        if (k["enabled"])         out->ka_enabled        = k["enabled"].as<bool>();
        if (k["interval_s"])      out->ka_interval_s     = k["interval_s"].as<int>();
        if (k["command"])         out->ka_command        = k["command"].as<std::string>();
        if (k["expect_response"]) out->ka_expect_response = k["expect_response"].as<bool>();
    }
    if (const YAML::Node pg = n["paging"]) {
        if (pg["more_pattern"])      out->more_pattern      = pg["more_pattern"].as<std::string>();
        if (pg["more_response"])     out->more_response     = pg["more_response"].as<std::string>();
        if (pg["large_output_scan"]) out->large_output_scan = pg["large_output_scan"].as<bool>();
    }
    if (n["errors"]) to_list(n["errors"], &out->errors);
    out->post_command    = n["post_command"]    ? n["post_command"]    : YAML::Node();
    out->disconnect_steps = n["disconnect_steps"] ? n["disconnect_steps"] : YAML::Node();
    if (err) err->clear();
    return 0;
}
```

- [ ] **Step 4: Build + run** → R7, R8 pass.
- [ ] **Step 5: Commit** `git commit -m "ncland: registry build+validate entry, compile prompt regexes"`

---

## Task 6: Directory loader (scan, abstract skip, dup detect, build map)

**Files:** modify `ncland_registry.cpp`, `_tests.cpp`.

- [ ] **Step 1: Failing test** (writes temp YAML files, loads, asserts)
```cpp
#include <cstdio>
#include <cstdlib>
#include <fstream>

static std::string mk_tmpdir() {
    char tmpl[] = "/tmp/ncreg_XXXXXX";
    char *d = mkdtemp(tmpl);
    return d ? std::string(d) : std::string();
}
static void put(const std::string &dir, const char *fn, const char *body) {
    std::ofstream f(dir + "/" + fn); f << body; f.close();
}

TEST("reg", "R9 directory load: defaults + extends + abstract skip") {
    std::string d = mk_tmpdir(); REQUIRE(!d.empty());
    put(d, "_defaults.yaml", "transport: {protocol: telnet, default_port: 23}\n"
                             "schema_version: 1\n");
    put(d, "ciena.base.yaml", "abstract: true\nvendor: ciena\n"
                              "prompt: {primary_regex: '[a-z]+#'}\nlua_module: x.lua\n");
    put(d, "ciena_rls.yaml", "ne_type: CIENA_RLS\nextends: ciena.base\n"
                             "transport: {protocol: ssh, default_port: 22}\n");
    ncland_registry_t reg; int errc = -1;
    REQUIRE(ncland_registry_load(d.c_str(), &reg, &errc) == 0);
    const ne_entry_t *e = ncland_registry_find(&reg, 89);   // CIENA_RLS
    REQUIRE(e != nullptr);
    REQUIRE(e->vendor == "ciena");                 // from abstract base
    REQUIRE(e->protocol[0] == "ssh");              // self over base
    REQUIRE(e->default_port == 22);
    REQUIRE(reg.by_dtype.size() == 1);             // abstract base NOT registered
}
```

- [ ] **Step 2: Implement `ncland_registry_load`** (replace the stub). Use POSIX `opendir`/`readdir`; index every `*.yaml` (basename without extension) into a map; skip `_defaults`; for each non-abstract file resolve+build and insert by dtype (skip+log dups).
```cpp
#include <dirent.h>
#include <cstring>
#include "nflog.hpp"

int ncland_registry_load(const char *dir, ncland_registry_t *reg, int *errc)
{
    if (!reg) return -1;
    reg->by_dtype.clear();
    int errors = 0;

    DIR *dp = opendir(dir);
    if (!dp) { LOG_WARN("registry: cannot open %s", dir); if (errc) *errc = 0; return -1; }

    /* Pass 1: parse every *.yaml, index by basename. */
    std::unordered_map<std::string, YAML::Node> idx;
    struct dirent *de;
    while ((de = readdir(dp)) != nullptr) {
        const char *dot = strrchr(de->d_name, '.');
        if (!dot || strcmp(dot, ".yaml") != 0) continue;
        std::string base(de->d_name, dot - de->d_name);
        std::string path = std::string(dir) + "/" + de->d_name;
        try { idx[base] = YAML::LoadFile(path); }
        catch (const std::exception &ex) { LOG_WARN("registry: parse %s: %s", path.c_str(), ex.what()); ++errors; }
    }
    closedir(dp);

    /* Pass 2: resolve + build every concrete (non-abstract, non-_defaults) file. */
    for (auto &kv : idx) {
        const std::string &base = kv.first;
        if (base == "_defaults") continue;
        if (kv.second["abstract"] && kv.second["abstract"].as<bool>()) continue;

        std::string err;
        YAML::Node merged = ncland_registry_resolve(idx, base, &err);
        if (!merged) { LOG_WARN("registry: %s: %s", base.c_str(), err.c_str()); ++errors; continue; }

        ne_entry_t e;
        if (ncland_registry_build(merged, &e, &err) != 0) {
            LOG_WARN("registry: %s: %s", base.c_str(), err.c_str()); ++errors; continue;
        }
        if (reg->by_dtype.count(e.dtype)) {
            LOG_WARN("registry: duplicate dtype %d (%s) — skipping", e.dtype, e.ne_type.c_str());
            ++errors; continue;
        }
        reg->by_dtype.emplace(e.dtype, std::move(e));
    }
    if (errc) *errc = errors;
    LOG_INFO("registry: loaded %zu NE types from %s (%d skipped)",
             reg->by_dtype.size(), dir, errors);
    return 0;
}
```

- [ ] **Step 3: Build + run** `./ncland_unit_tests reg` → R9 passes (and R1–R8 still green).
- [ ] **Step 4: Commit** `git commit -m "ncland: registry directory loader (extends/abstract/dup handling)"`

---

## Task 7: Load the registry in warehouse_init

**Files:** modify `ncland_warehouse.cpp`, `ncland.h` (add a registry member to `ncland_wh_t`).

- [ ] **Step 1: Add a registry to `ncland_wh_t`** in `ncland.h` (near `dtype_allow`):
```cpp
    ncland_registry_t registry;   /**< YAML NE-config registry (NCLAND_NE_DIR). */
```
and at the top of `ncland.h` add `#include "ncland_registry.h"` (after the existing includes).

- [ ] **Step 2: Load it in `warehouse_init`** (`ncland_warehouse.cpp`), right after the `ncland_seed_load_filter` block:
```cpp
    int reg_errc = 0;
    if (ncland_registry_load(NCLAND_NE_DIR, &wh->registry, &reg_errc) != 0)
        LOG_WARN("warehouse_init: registry dir %s unreadable (continuing)", NCLAND_NE_DIR);
    else
        LOG_INFO("warehouse_init: registry %zu types (%d skipped)",
                 wh->registry.by_dtype.size(), reg_errc);
```
(Non-fatal — a missing/empty registry must not stop the daemon; dispatch simply finds nothing for unowned dtypes.)

- [ ] **Step 3: Build the daemon + unit tests**
```
nmake -f ncland.mk ../../../3b2/bin/ncland     # links registry + -lyaml-cpp
nmake -f ncland.mk ncland_unit_tests && ./ncland_unit_tests
```
Expected: clean link; full suite green (reg suite + all prior). Note new total.

- [ ] **Step 4: Commit** `git commit -m "ncland: load YAML registry at warehouse startup"`

---

# Done

The registry is built and loaded at startup: `wh->registry.by_dtype[dtype]` yields the fully-merged, validated, regex-compiled `ne_entry_t` for any owned NE type.

**Explicitly deferred (later subsystems / plans):**
- Using the entry at session-open (prompt regexes, transport.protocol selection, keepalive) — needs the **session path wiring** (A/B item #4).
- Parsing `post_command`/`disconnect_steps` (stored raw here) — the **stepper** (#3).
- `login()` + `session`/`global` — the **Lua bridge** (#2).
- The `ne_type↔dtype` CI round-trip check + `lua_module` file-existence/`luac` checks (§13.1) — add when the Lua bridge lands (it needs the lua dir).

**Spec coverage:** §6 layout/`extends`/`abstract` → Tasks 4,6. §7.1–7.6 fields → Task 5. §7.4 validation → Tasks 5,6. §7.5 inheritance/merge → Tasks 3,4. §10.1 steps 3–5 (scan/merge/validate/compile/registry) → Tasks 5,6,7. yaml-cpp choice (§14.3) → resolved.
```
