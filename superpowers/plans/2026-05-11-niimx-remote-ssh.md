# niimxd Remote SSH Bchannel Support — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Extend niimxd to run USER processes on remote netFLEX hosts over libssh, while preserving the existing single-threaded epoll loop and state machines.

**Architecture:** Approach A from the design doc — augment the existing `bchannel` struct with an `m_remote` flag plus libssh handles; epoll polls `ssh_get_fd()` for remote bchannels. State machines, dispatch logic, queue, and timers stay unchanged. Wire protocol gains a `host` frame inserted between the empty delimiter and `msgid`. No backward-compat shim — `niimx.cpp` and `niimxlib.c` are updated atomically with the daemon.

**Tech Stack:** C++ (gcc 4.8.5 baseline), libssh, libzmq, AT&T nmake, libssh-devel headers in `/usr/include/libssh/`.

**Source of truth design doc:** `/home/dan/Git/niimx-netflex/2026-04-09-niimx-remote-ssh-design.md`

---

## Assumptions & Notes

- **Single-file daemon:** Keep all new code in `cnc/niimx/src/niimxd.cpp`. SSH helpers are static functions in the same translation unit. Splitting into `niimxd_ssh.cpp` is a refactor and out of scope here.
- **Test strategy:** No unit-test framework exists in this project. Each task verifies via build + targeted `niimx` test client invocations. Where a remote SSH host is unavailable in the dev environment, tests fall back to `localhost` (the dev host's own sshd, with the dev user's `~/.ssh/authorized_keys` containing their own key). Note this explicitly in any test command.
- **TDD adaptation:** Because there is no test framework, "write the failing test" steps are replaced with "define the expected observable behavior and a one-line invocation that demonstrates it." Each task ends with a commit only after that invocation produces the expected output.
- **gcc 4.8.5 baseline:** No `std::optional`, no `if constexpr`, no structured bindings. `auto` / range-for / lambdas are fine. libssh API is C, so no template concerns.
- **Doxygen:** Every new function and struct member gets a `/** @brief … @param … @return … */` block per `CLAUDE.md`.
- **Naming prefix:** All new symbols use the existing `niimx_` prefix.

---

## File Structure

| File | Action | Responsibility |
|------|--------|----------------|
| `cnc/niimx/src/niimxd.cpp` | Modify | Daemon: SSH transport, host-aware dispatch, config additions, health probing |
| `cnc/niimx/src/niimx.cpp`  | Modify | Test client: `-H host` flag, host frame in wire format |
| `cnc/niimx/src/Makefile`   | Modify | Link daemon against `-lssh` |
| `cnc/utility/src/niimxlib.c` | Modify | Library API: add `host` parameter, host frame in wire format |
| `include/utilmisc.h`        | Modify | Public declaration of new `niimx()` signature |

No new source files. No restructuring.

---

## Task 1: Wire libssh into the build

**Files:**
- Modify: `cnc/niimx/src/Makefile:22`

- [ ] **Step 1: Confirm libssh is visible to the compiler**

Run: `ls /usr/include/libssh/libssh.h && ldconfig -p | grep 'libssh.so\b'`
Expected: both lines print without error.

- [ ] **Step 2: Add `-lssh` to the niimxd link line**

Existing:
```make
$(PBIN)/niimxd : niimxd.o $(CORELIBS) $(UTILLIB)  -lnelib
	$(CPLUS_CC)  $(LDFLAGS) -o $(<) $(*) $(ACCLIBS) -lzmq
```
Change to:
```make
$(PBIN)/niimxd : niimxd.o $(CORELIBS) $(UTILLIB)  -lnelib
	$(CPLUS_CC)  $(LDFLAGS) -o $(<) $(*) $(ACCLIBS) -lzmq -lssh
```

- [ ] **Step 3: Add a trivial libssh smoke include to niimxd.cpp**

In `cnc/niimx/src/niimxd.cpp`, after `#include <zmq.h>`:
```cpp
#include <libssh/libssh.h>
```

- [ ] **Step 4: Build**

Run: `cd cnc/niimx/src && nmake`
Expected: builds with no new warnings. `niimxd` ldd shows `libssh.so.4 => /lib64/libssh.so.4`.

Verify: `ldd $BASE/3b2/bin/niimxd | grep libssh`

- [ ] **Step 5: Commit**

```bash
git add cnc/niimx/src/Makefile cnc/niimx/src/niimxd.cpp
git commit -m "niimxd: link against libssh for remote SSH transport"
```

---

## Task 2: Extend data model — bchannel, Request, NiimxConfig, NiimxGlobalState

**Files:**
- Modify: `cnc/niimx/src/niimxd.cpp` (struct definitions around lines 133, 171, 219, 324)

- [ ] **Step 1: Add SSH fields to `bchannel`**

In `struct bchannel` (~line 219), after `string m_login;` add:
```cpp
    // ── remote SSH transport ─────────────────────────────────────────────
    bool          m_remote       = false;   /**< true: SSH transport, false: local PTY. */
    ssh_session   m_ssh_session  = nullptr; /**< libssh session handle (remote only). */
    ssh_channel   m_ssh_channel  = nullptr; /**< libssh channel handle (remote only). */
    string        m_remote_host;            /**< Target hostname, empty for local. */
```

Update the struct Doxygen header to document the four new members.

- [ ] **Step 2: Add `host` to `Request`**

In `struct Request` (~line 171), after `string command;`:
```cpp
    string  host;       /**< Target remote host; empty string means local. */
```

Update the Doxygen header.

- [ ] **Step 3: Add config fields**

In `struct NiimxConfig` (~line 133), after the existing scalars:
```cpp
    int  remote_nconn       = 0;            /**< NIIMX.REMOTE_NCONN: slots per remote host. */
    char remote_hosts[1024] = "";           /**< NIIMX.REMOTE_HOSTS: comma-separated host list. */
```

Add corresponding defaults at the top of the file:
```cpp
#define NIIMX_DEF_REMOTE_NCONN 0
```

- [ ] **Step 4: Add global state fields**

In `struct NiimxGlobalState` (~line 324):
```cpp
    vector<string>                remote_host_list;    /**< Parsed Cfg.remote_hosts. */
    unordered_map<string,bool>    host_available;      /**< SSH reachability per host. */
```

Update the Doxygen header.

- [ ] **Step 5: Build and confirm**

Run: `cd cnc/niimx/src && nmake`
Expected: clean build. No new warnings.

- [ ] **Step 6: Commit**

```bash
git add cnc/niimx/src/niimxd.cpp
git commit -m "niimxd: extend bchannel/Request/Config/global state with remote SSH fields"
```

---

## Task 3: Config loader — parse REMOTE_HOSTS / REMOTE_NCONN

**Files:**
- Modify: `cnc/niimx/src/niimxd.cpp` (`niimx_load_config` ~line 1748)
- Add static helper: `niimx_parse_remote_hosts`

- [ ] **Step 1: Add parser helper**

Place above `niimx_load_config`:
```cpp
/**
 * @brief Split a comma-separated host list into a vector of trimmed strings.
 *
 * Empty entries (consecutive commas, leading/trailing commas) are skipped.
 * Whitespace surrounding each entry is trimmed.
 *
 * @param csv   Comma-separated host string (e.g. "host1,host2,host3").
 * @param out   Vector to populate; cleared before parsing.
 */
static void niimx_parse_remote_hosts(const char* csv, vector<string>& out) {
    out.clear();
    if (!csv || !*csv) return;
    const char* p = csv;
    while (*p) {
        while (*p == ' ' || *p == '\t' || *p == ',') p++;
        const char* start = p;
        while (*p && *p != ',') p++;
        const char* end = p;
        while (end > start && (end[-1] == ' ' || end[-1] == '\t')) end--;
        if (end > start) out.push_back(string(start, end - start));
    }
}
```

- [ ] **Step 2: Load new sysdef values inside `niimx_load_config`**

Add at the end of the existing `get_sysdef_*` calls block:
```cpp
    get_sysdef_int("NIIMX.REMOTE_NCONN=",   &cfg.remote_nconn,   NIIMX_DEF_REMOTE_NCONN);
    get_sysdef_str("NIIMX.REMOTE_HOSTS=",   cfg.remote_hosts,    sizeof(cfg.remote_hosts)-1);
```

Add clamp:
```cpp
    if (cfg.remote_nconn < 0 || cfg.remote_nconn > 40) cfg.remote_nconn = NIIMX_DEF_REMOTE_NCONN;
```

Extend the final `Trc(0, "Config: ...")` to include the new values.

- [ ] **Step 3: Verify in interactive mode**

Run: `$BASE/3b2/bin/niimxd`
Expected: prints `NIIMX.REMOTE_NCONN=…` and `NIIMX.REMOTE_HOSTS=…` lines alongside existing config.

Also add the new printf lines in `main()` (the `isatty(STDIN_FILENO)` block ~line 1858):
```cpp
        printf("  NIIMX.REMOTE_NCONN=%d  — slots per remote host (0=disabled)\n", Cfg.remote_nconn);
        printf("  NIIMX.REMOTE_HOSTS=%s  — comma-separated remote host list\n", Cfg.remote_hosts);
```

- [ ] **Step 4: Build and run interactive smoke**

Run: `cd cnc/niimx/src && nmake && $BASE/3b2/bin/niimxd | grep -E 'REMOTE_(NCONN|HOSTS)'`
Expected: both lines present.

- [ ] **Step 5: Commit**

```bash
git add cnc/niimx/src/niimxd.cpp
git commit -m "niimxd: load NIIMX.REMOTE_NCONN and NIIMX.REMOTE_HOSTS from sysdef"
```

---

## Task 4: Pool allocation — size Bc[] for local + remote slots

**Files:**
- Modify: `cnc/niimx/src/niimxd.cpp` (`main()` allocation block ~line 1918, and `G.bc_capacity` semantics)

- [ ] **Step 1: Parse host list before allocation**

In `main()`, immediately after `niimx_load_config(Cfg);` (line 1854), add:
```cpp
    niimx_parse_remote_hosts(Cfg.remote_hosts, G.remote_host_list);
    for (const auto& h : G.remote_host_list)
        G.host_available[h] = true;   // optimistic; validated later
```

- [ ] **Step 2: Compute total bchannel capacity and allocate**

Replace:
```cpp
    G.bc_capacity = Cfg.nconn;
    Bc = new bchannel[Cfg.nconn];
    for (int i = 0; i < Cfg.nconn; i++) {
        Bc[i].m_slot  = i;
        Bc[i].m_login = Cfg.user;
    }
```
With:
```cpp
    int total_slots = Cfg.nconn + (int)G.remote_host_list.size() * Cfg.remote_nconn;
    G.bc_capacity = total_slots;
    Bc = new bchannel[total_slots];

    // Local slots [0 .. nconn-1]
    for (int i = 0; i < Cfg.nconn; i++) {
        Bc[i].m_slot   = i;
        Bc[i].m_login  = Cfg.user;
        Bc[i].m_remote = false;
    }

    // Remote slots [nconn .. total_slots-1], grouped by host
    int next = Cfg.nconn;
    for (const auto& h : G.remote_host_list) {
        for (int j = 0; j < Cfg.remote_nconn; j++, next++) {
            Bc[next].m_slot        = next;
            Bc[next].m_login       = Cfg.user;
            Bc[next].m_remote      = true;
            Bc[next].m_remote_host = h;
        }
    }

    Trc(0, "Pool: local=" << Cfg.nconn
           << " remote_hosts=" << G.remote_host_list.size()
           << " remote_nconn=" << Cfg.remote_nconn
           << " total_slots=" << total_slots << endl);
```

- [ ] **Step 3: Audit every `Cfg.nconn` loop**

Search and replace per-callsite, NOT a blanket replace:

```bash
grep -n 'i < Cfg.nconn' cnc/niimx/src/niimxd.cpp
```

For each match, decide:
- **Loops that walk the whole pool** (rebalance, dispatch, drain, timer, status, monitor, graceful_shutdown, config_reload) — change `Cfg.nconn` → `G.bc_capacity`.
- **Loops scoped to local only** (the existing "seed one warm" rebalance logic that becomes local-only in Task 8) — leave at `Cfg.nconn` until Task 8 rewrites them.

For Task 4 just change the loops in: `niimx_rebalance_pool`, `niimx_dispatch_requests`, `niimx_drain_done`, `niimx_handle_timer`, `niimx_monitor_user_growth`, `niimx_handle_config_reload`, `niimx_status_report`, `niimx_graceful_shutdown`.

Example — in `niimx_drain_done`:
```cpp
    for (int i = 0; i < G.bc_capacity; i++) {
```

- [ ] **Step 4: Build and run smoke**

Run: `cd cnc/niimx/src && nmake && $BASE/3b2/bin/niimxd` (kill with Ctrl-C after one tick)

With `NIIMX.REMOTE_HOSTS` unset, expect log: `Pool: local=12 remote_hosts=0 remote_nconn=0 total_slots=12`. Behavior unchanged from before.

- [ ] **Step 5: Commit**

```bash
git add cnc/niimx/src/niimxd.cpp
git commit -m "niimxd: size bchannel pool for local + per-host remote slots"
```

---

## Task 5: SSH connect / disconnect / key validation

**Files:**
- Modify: `cnc/niimx/src/niimxd.cpp`. Add three static functions before `niimx_spawn_user`.

- [ ] **Step 1: Add `niimx_ssh_disconnect`**

```cpp
/**
 * @brief Tears down the libssh session and channel on a remote bchannel.
 *
 * Closes and frees the channel, disconnects and frees the session, removes the
 * polled fd from epoll, and clears the m_remote_* / m_fd handles.  Safe to call
 * on a bchannel whose SSH handles are already null.
 *
 * @param bc  Remote bchannel to disconnect.
 */
static void niimx_ssh_disconnect(bchannel& bc) {
    if (bc.m_fd >= 0) {
        epoll_ctl(G.epfd, EPOLL_CTL_DEL, bc.m_fd, nullptr);
        G.fd_to_slot.erase(bc.m_fd);
        bc.m_fd = -1;
    }
    if (bc.m_ssh_channel) {
        ssh_channel_close(bc.m_ssh_channel);
        ssh_channel_free(bc.m_ssh_channel);
        bc.m_ssh_channel = nullptr;
    }
    if (bc.m_ssh_session) {
        ssh_disconnect(bc.m_ssh_session);
        ssh_free(bc.m_ssh_session);
        bc.m_ssh_session = nullptr;
    }
}
```

- [ ] **Step 2: Add `niimx_ssh_connect`**

```cpp
/**
 * @brief Opens an SSH session, authenticates, requests a PTY, and execs USER.
 *
 * On success the bchannel is left in starting state with its epoll fd registered
 * (obtained via ssh_get_fd) and its login deadline armed.  On any failure all
 * libssh resources are released and host_available is marked false.
 *
 * @param bc  Remote bchannel; bc.m_remote_host and bc.m_login must be set.
 * @return    true on success, false on connect/auth/channel/exec failure.
 */
static bool niimx_ssh_connect(bchannel& bc) {
    bc.m_ssh_session = ssh_new();
    if (!bc.m_ssh_session) {
        Err("ssh_new() failed for host=" << bc.m_remote_host << endl);
        return false;
    }

    ssh_options_set(bc.m_ssh_session, SSH_OPTIONS_HOST, bc.m_remote_host.c_str());
    ssh_options_set(bc.m_ssh_session, SSH_OPTIONS_USER, bc.m_login.c_str());
    int port = 22;
    ssh_options_set(bc.m_ssh_session, SSH_OPTIONS_PORT, &port);

    if (ssh_connect(bc.m_ssh_session) != SSH_OK) {
        Trc(0, "SSH connect failed host=" << bc.m_remote_host
            << " err=" << ssh_get_error(bc.m_ssh_session) << endl);
        G.host_available[bc.m_remote_host] = false;
        niimx_ssh_disconnect(bc);
        return false;
    }

    if (ssh_userauth_publickey_auto(bc.m_ssh_session, nullptr, nullptr) != SSH_AUTH_SUCCESS) {
        Trc(0, "SSH auth failed host=" << bc.m_remote_host
            << " err=" << ssh_get_error(bc.m_ssh_session) << endl);
        G.host_available[bc.m_remote_host] = false;
        niimx_ssh_disconnect(bc);
        return false;
    }

    bc.m_ssh_channel = ssh_channel_new(bc.m_ssh_session);
    if (!bc.m_ssh_channel || ssh_channel_open_session(bc.m_ssh_channel) != SSH_OK) {
        Err("SSH channel open failed host=" << bc.m_remote_host << endl);
        niimx_ssh_disconnect(bc);
        return false;
    }

    if (ssh_channel_request_pty(bc.m_ssh_channel) != SSH_OK) {
        Err("SSH PTY request failed host=" << bc.m_remote_host << endl);
        niimx_ssh_disconnect(bc);
        return false;
    }

    char cmd[512];
    snprintf(cmd, sizeof(cmd),
        "env NIIMX=yes IIMX=1 LOGNAME=%s IIMXSLOT=%d SESSION_TYPE=iimx MY_IP=IIMX /usr/cnc/bin/USER",
        bc.m_login.c_str(), bc.m_slot);
    if (ssh_channel_request_exec(bc.m_ssh_channel, cmd) != SSH_OK) {
        Err("SSH exec failed host=" << bc.m_remote_host << endl);
        niimx_ssh_disconnect(bc);
        return false;
    }

    int fd = ssh_get_fd(bc.m_ssh_session);
    if (fd < 0) {
        Err("ssh_get_fd returned " << fd << " host=" << bc.m_remote_host << endl);
        niimx_ssh_disconnect(bc);
        return false;
    }
    int flags = fcntl(fd, F_GETFL, 0);
    fcntl(fd, F_SETFL, flags | O_NONBLOCK);

    struct epoll_event ev{};
    ev.events  = EPOLLIN;
    ev.data.fd = fd;
    if (epoll_ctl(G.epfd, EPOLL_CTL_ADD, fd, &ev) < 0) {
        Err("epoll_ctl ADD ssh_fd=" << fd << " errno=" << errno << endl);
        niimx_ssh_disconnect(bc);
        return false;
    }
    G.fd_to_slot[fd] = bc.m_slot;

    bc.m_fd            = fd;
    bc.m_state         = bchannel::starting;
    bc.m_login_state   = bchannel::LS_WAIT_BANNER;
    bc.m_login_eom     = ':';
    bc.m_login_buf.clear();
    bc.m_connect_time  = time(nullptr);
    bc.m_msgcnt        = 0;
    bc.m_deadline      = Clock::now() + chrono::seconds(Cfg.login_tmout);

    G.host_available[bc.m_remote_host] = true;
    Trc(2, "SLOT" << bc.m_slot << " SSH connected host=" << bc.m_remote_host
        << " fd=" << fd << endl);
    return true;
}
```

- [ ] **Step 3: Add `niimx_ssh_validate_keys`**

```cpp
/**
 * @brief Tests SSH publickey auth to every configured remote host.
 *
 * For each host in G.remote_host_list, attempts a throwaway ssh_connect +
 * ssh_userauth_publickey_auto.  Reachable hosts get host_available = true; failed
 * hosts get host_available = false, an error log, and create niimx.sshproblem.
 * Removes niimx.sshproblem only when no host is unavailable AND at least one validated.
 */
static void niimx_ssh_validate_keys() {
    bool any_ok = false;
    bool any_bad = false;
    for (const auto& h : G.remote_host_list) {
        ssh_session s = ssh_new();
        if (!s) continue;
        ssh_options_set(s, SSH_OPTIONS_HOST, h.c_str());
        ssh_options_set(s, SSH_OPTIONS_USER, Cfg.user);
        int port = 22;
        ssh_options_set(s, SSH_OPTIONS_PORT, &port);

        bool ok = false;
        if (ssh_connect(s) == SSH_OK) {
            if (ssh_userauth_publickey_auto(s, nullptr, nullptr) == SSH_AUTH_SUCCESS)
                ok = true;
            else
                Trc(0, "SSH key validation: auth failed host=" << h
                    << " err=" << ssh_get_error(s) << endl);
            ssh_disconnect(s);
        } else {
            Trc(0, "SSH key validation: connect failed host=" << h
                << " err=" << ssh_get_error(s) << endl);
        }
        ssh_free(s);

        bool old = G.host_available[h];
        G.host_available[h] = ok;
        if (old != ok)
            Trc(0, "host=" << h << " availability " << old << " -> " << ok << endl);

        if (ok) any_ok = true; else any_bad = true;
    }
    if (any_bad) {
        FILE* fp = fopen("/usr/cnc/data/niimx.sshproblem", "w");
        if (fp) { fprintf(fp, "ssh key validation failed\n"); fclose(fp); }
    } else if (any_ok) {
        unlink("/usr/cnc/data/niimx.sshproblem");
    }
}
```

- [ ] **Step 4: Call validator at startup, after pool allocation**

In `main()`, after the pool allocation block from Task 4 and before `niimx_spawn_user(Bc[0])`:
```cpp
    niimx_ssh_validate_keys();
```

- [ ] **Step 5: Build**

Run: `cd cnc/niimx/src && nmake`
Expected: clean build.

- [ ] **Step 6: Smoke test against localhost**

Pre-condition: dev user's public key is in their own `~/.ssh/authorized_keys`; sshd is running locally.

```bash
echo 'NIIMX.REMOTE_HOSTS=localhost'       >> /usr/cnc/features/system  # or wherever sysdef lives
echo 'NIIMX.REMOTE_NCONN=1'               >> /usr/cnc/features/system
```
Run `$BASE/3b2/bin/niimxd` in the foreground; tail `/usr/cnc/trace/niimxd`.
Expected log: `host=localhost availability 1 -> 1` (or stays optimistic) and no `niimx.sshproblem` file written. With a bad host substituted, expect the file to appear.

- [ ] **Step 7: Commit**

```bash
git add cnc/niimx/src/niimxd.cpp
git commit -m "niimxd: add libssh connect/disconnect/key validation helpers"
```

---

## Task 6: Transport branching — read, write, terminate

**Files:**
- Modify: `cnc/niimx/src/niimxd.cpp` — `niimx_handle_pty`, `niimx_pty_write`, `niimx_terminate_bchannel`, and the inline `::write(bc.m_fd, ...)` calls in `niimx_pty_protocol` and `niimx_handle_working_byte`.

- [ ] **Step 1: SSH read path in `niimx_handle_pty`**

Replace the initial `::read(fd, buf, ...)` block. New shape:

```cpp
    char buf[1024];
    int n;
    if (bc.m_remote) {
        n = ssh_channel_read_nonblocking(bc.m_ssh_channel, buf, sizeof(buf), 0);
        if (n == SSH_AGAIN) return;
        if (n == SSH_ERROR || (n == 0 && ssh_channel_is_eof(bc.m_ssh_channel))) {
            Err("SLOT" << bc.m_slot << " SSH channel closed err="
                << (bc.m_ssh_session ? ssh_get_error(bc.m_ssh_session) : "?") << endl);
            if (!bc.m_zmq_identity.empty()) {
                niimx_zmq_send_response(bc.m_zmq_identity, bc.m_msgid,
                    "NIIMX^Internal Error: remote connection lost");
                bc.m_zmq_identity.clear();
            }
            niimx_terminate_bchannel(bc);
            return;
        }
        if (n == 0) return;   // no data right now
    } else {
        n = ::read(fd, buf, sizeof(buf));
        if (n <= 0) {
            // … existing local-PTY error/EOF handling unchanged …
        }
    }
```

Preserve the existing local-PTY error block verbatim inside the `else` branch (the WIFEXITED / WIFSIGNALED block). Do not duplicate the byte-processing for-loop — let it fall through identically for both transports.

- [ ] **Step 2: SSH write path in `niimx_pty_write`**

Replace body with:
```cpp
static void niimx_pty_write(bchannel& bc, const string& msg) {
    Trc(5, "[" << bc.m_slot << "] Writing \"" << msg << "\"" << endl);
    const char* p = msg.c_str();
    int remaining = (int)msg.size();
    int sent = 0;

    if (bc.m_remote) {
        while (remaining > 0) {
            int rc = ssh_channel_write(bc.m_ssh_channel, p + sent, remaining);
            if (rc == SSH_ERROR) {
                Err("niimx_pty_write SSH SLOT" << bc.m_slot
                    << " err=" << ssh_get_error(bc.m_ssh_session) << endl);
                return;
            }
            if (rc == 0) { usleep(1000); continue; }
            sent += rc;
            remaining -= rc;
        }
        return;
    }

    while (remaining > 0) {
        int rc = ::write(bc.m_fd, p + sent, remaining);
        if (rc < 0) {
            if (errno == EAGAIN || errno == EWOULDBLOCK) { usleep(1000); continue; }
            Err("niimx_pty_write SLOT" << bc.m_slot << " errno=" << errno << endl);
            break;
        }
        sent += rc;
        remaining -= rc;
    }
}
```

- [ ] **Step 3: SSH branch in `niimx_send_command_to_bchannel`**

The existing function uses raw `::write(bc.m_fd, ...)`. Either inline an SSH branch OR (cleaner) refactor the write loop to call `niimx_pty_write(bc, wire)` since `niimx_pty_write` now handles both transports. Choose the refactor:

Replace the trailing write loop in `niimx_send_command_to_bchannel`:
```cpp
    niimx_pty_write(bc, wire);
```

(Drop the local `p`/`remaining`/`sent`/`while` block. The function still returns `true` after the call; if you want failure detection, leave `niimx_pty_write` as void and trust state-machine error paths.)

- [ ] **Step 4: SSH branch in `niimx_pty_protocol`**

The IAC autoreply does `::write(bc.m_fd, bc.m_reply, 3);`. Replace with:
```cpp
        if (bc.m_remote) {
            ssh_channel_write(bc.m_ssh_channel, bc.m_reply, 3);
        } else {
            ::write(bc.m_fd, bc.m_reply, 3);
        }
```

- [ ] **Step 5: SSH branch in `niimx_handle_working_byte`**

The framed-protocol ACK does `::write(bc.m_fd, "\006\n", 2);`. Replace with the same pattern as Step 4.

- [ ] **Step 6: SSH branch in `niimx_terminate_bchannel`**

In the existing function, before the local PTY teardown block:
```cpp
    if (bc.m_remote) {
        niimx_ssh_disconnect(bc);
    } else {
        // existing local PTY graceful "exit\n" + close + kill block
    }
```

Note: the local block already sends `exit\n`, closes the fd, and kills the PID. Wrap the entire existing PTY/PID teardown in the `else`. The pending-response and field-reset code at the bottom remains shared.

Also: for remote bchannels there is no child PID owned by us, so skip the `kill(bc.m_pid, ...)` block entirely.

- [ ] **Step 7: Build**

Run: `cd cnc/niimx/src && nmake`
Expected: clean build.

- [ ] **Step 8: End-to-end localhost test**

Pre-cond from Task 5 step 6 in place. Start daemon. Connect with niimx client (host frame not added yet — so client still talks to a *local* bchannel — that's fine, we just want to confirm the daemon still works after the refactor):
```bash
$BASE/3b2/bin/niimx -c who
```
Expected: response prints the local USER's `who` output. No regression.

- [ ] **Step 9: Commit**

```bash
git add cnc/niimx/src/niimxd.cpp
git commit -m "niimxd: branch PTY read/write/terminate on bchannel.m_remote"
```

---

## Task 7: Wire protocol — add host frame to daemon parser

**Files:**
- Modify: `cnc/niimx/src/niimxd.cpp` — `niimx_handle_zmq` (~line 671).

- [ ] **Step 1: Parse the new frame order**

Replace the multipart receive block. New order:
`identity | empty | host | msgid(4) | mpid(4) | tmout(4) | command`

```cpp
        string identity, empty, host, s_msgid, s_mpid, s_tmout, command;
        bool more;

        if (!niimx_zmq_recv_frame(G.router, identity, more) || !more) continue;
        if (!niimx_zmq_recv_frame(G.router, empty,    more) || !more) continue;
        if (!niimx_zmq_recv_frame(G.router, host,     more) || !more) continue;
        if (!niimx_zmq_recv_frame(G.router, s_msgid,  more) || !more) continue;
        if (!niimx_zmq_recv_frame(G.router, s_mpid,   more) || !more) continue;
        if (!niimx_zmq_recv_frame(G.router, s_tmout,  more) || !more) continue;
        if (!niimx_zmq_recv_frame(G.router, command,  more))          continue;
        while (more) { string x; niimx_zmq_recv_frame(G.router, x, more); }
```

Populate `req.host = host;`.

- [ ] **Step 2: Reject unknown / unavailable hosts BEFORE enqueue**

After the existing `SystemUtilization >= Cfg.reject` and queue-full checks, add:
```cpp
        if (!req.host.empty()) {
            bool known = false;
            for (const auto& h : G.remote_host_list)
                if (h == req.host) { known = true; break; }
            if (!known) {
                niimx_zmq_send_response(req.identity, req.msgid,
                    "NIIMX^Unknown host: " + req.host);
                continue;
            }
            auto it = G.host_available.find(req.host);
            if (it != G.host_available.end() && !it->second) {
                niimx_zmq_send_response(req.identity, req.msgid,
                    "NIIMX^Host unavailable: " + req.host);
                continue;
            }
        }
```

- [ ] **Step 3: Update Doxygen header for `niimx_handle_zmq`**

Document the new 7-frame layout including `host`.

- [ ] **Step 4: Build**

Run: `cd cnc/niimx/src && nmake`
Expected: clean build.

- **Note:** Daemon will now misparse old-format clients. Client updates land in Tasks 12–13 of this plan. Do not test e2e until those tasks land — or only test by deliberately driving the daemon from an updated client built ad-hoc.

- [ ] **Step 5: Commit**

```bash
git add cnc/niimx/src/niimxd.cpp
git commit -m "niimxd: accept host frame in ZMQ wire format, reject unknown/unavailable"
```

---

## Task 8: Host-aware dispatch and per-group rebalance

**Files:**
- Modify: `cnc/niimx/src/niimxd.cpp` — `niimx_dispatch_requests` and `niimx_rebalance_pool`.

- [ ] **Step 1: Rewrite `niimx_dispatch_requests` to match on host**

```cpp
static void niimx_dispatch_requests() {
    if (G.requestQ.empty()) return;

    for (auto it = G.requestQ.begin(); it != G.requestQ.end(); ) {
        int slot = -1;
        for (int i = 0; i < G.bc_capacity; i++) {
            if (Bc[i].m_state != bchannel::ready) continue;
            if (it->host.empty()) {
                if (!Bc[i].m_remote) { slot = i; break; }
            } else {
                if (Bc[i].m_remote && Bc[i].m_remote_host == it->host) { slot = i; break; }
            }
        }
        if (slot < 0) { ++it; continue; }  // leave in queue, try next request

        Request req = move(*it);
        it = G.requestQ.erase(it);
        niimx_send_command_to_bchannel(Bc[slot], req);
    }
}
```

Note this changes FIFO semantics slightly: a request for an unavailable group no longer blocks subsequent requests for other groups. This is the intended behavior per the design's "leave request in queue for retry next cycle" wording.

- [ ] **Step 2: Rewrite `niimx_rebalance_pool` for per-host warm spare**

Replace the existing "ensure one warm" block. Helper logic:

```cpp
static void niimx_rebalance_pool() {
    auto now = Clock::now();

    auto warm_in_group = [&](bool remote, const string& host) -> int {
        int n = 0;
        for (int i = 0; i < G.bc_capacity; i++) {
            if (Bc[i].m_remote != remote) continue;
            if (remote && Bc[i].m_remote_host != host) continue;
            if (Bc[i].m_state == bchannel::starting ||
                Bc[i].m_state == bchannel::warming  ||
                Bc[i].m_state == bchannel::ready) n++;
        }
        return n;
    };

    auto active_in_group = [&](bool remote, const string& host) -> int {
        int n = 0;
        for (int i = 0; i < G.bc_capacity; i++) {
            if (Bc[i].m_remote != remote) continue;
            if (remote && Bc[i].m_remote_host != host) continue;
            if (Bc[i].m_state != bchannel::notconnected &&
                Bc[i].m_state != bchannel::error_state) n++;
        }
        return n;
    };

    auto seed_in_group = [&](bool remote, const string& host) {
        for (int i = 0; i < G.bc_capacity; i++) {
            if (Bc[i].m_remote != remote) continue;
            if (remote && Bc[i].m_remote_host != host) continue;
            if (Bc[i].m_state != bchannel::notconnected &&
                Bc[i].m_state != bchannel::error_state) continue;
            bool ok = remote ? niimx_ssh_connect(Bc[i]) : niimx_spawn_user(Bc[i]);
            if (ok) Trc(2, "rebalance seeded slot=" << i
                          << " remote=" << remote
                          << " host=" << host << endl);
            return;
        }
    };

    // Local group
    if (warm_in_group(false, "") < 1 && active_in_group(false, "") < Cfg.nconn)
        seed_in_group(false, "");

    // Each remote host group
    for (const auto& h : G.remote_host_list) {
        if (!G.host_available[h]) continue;
        if (warm_in_group(true, h) < 1 && active_in_group(true, h) < Cfg.remote_nconn)
            seed_in_group(true, h);
    }

    // Probe timedout channels — existing block; change loop bound to G.bc_capacity.
    for (int i = 0; i < G.bc_capacity; i++) {
        bchannel& bc = Bc[i];
        if (bc.m_state != bchannel::timedout) continue;
        if (now <= bc.m_deadline) continue;
        // … existing probe logic unchanged, but the read-drain uses ::read only for local;
        //   for remote, drain via ssh_channel_read_nonblocking in a short loop …
        // … see Step 3 below.
    }
}
```

- [ ] **Step 3: Probe-drain branch for remote in `niimx_rebalance_pool`**

In the timedout-probe block, replace the local-only drain with:
```cpp
        bool got_enddone = false;
        if (bc.m_remote) {
            char drain_buf[4096];
            string drained;
            int n;
            while ((n = ssh_channel_read_nonblocking(bc.m_ssh_channel,
                                                    drain_buf, sizeof(drain_buf), 0)) > 0)
                drained.append(drain_buf, n);
            if (drained.find("ENDDONE") != string::npos) got_enddone = true;
        } else if (bc.m_fd >= 0) {
            // existing local-PTY drain
        }
```

The subsequent `ozzy` probe write goes through `niimx_pty_write` which already routes by `m_remote`.

- [ ] **Step 4: Build**

Run: `cd cnc/niimx/src && nmake`
Expected: clean build, no warnings.

- [ ] **Step 5: Commit**

```bash
git add cnc/niimx/src/niimxd.cpp
git commit -m "niimxd: host-aware dispatch and per-group warm-spare rebalance"
```

---

## Task 9: Periodic host health re-validation

**Files:**
- Modify: `cnc/niimx/src/niimxd.cpp` — `niimx_monitor_user_growth` (which runs on `monitor_tfd`, 60s).

- [ ] **Step 1: Append a host-revalidation pass to the existing 60s callback**

At the bottom of `niimx_monitor_user_growth`, before the closing brace:
```cpp
    // Re-probe hosts that are currently marked unavailable.
    for (const auto& h : G.remote_host_list) {
        auto it = G.host_available.find(h);
        if (it == G.host_available.end() || it->second) continue;

        ssh_session s = ssh_new();
        if (!s) continue;
        ssh_options_set(s, SSH_OPTIONS_HOST, h.c_str());
        ssh_options_set(s, SSH_OPTIONS_USER, Cfg.user);
        int port = 22;
        ssh_options_set(s, SSH_OPTIONS_PORT, &port);
        bool ok = false;
        if (ssh_connect(s) == SSH_OK) {
            if (ssh_userauth_publickey_auto(s, nullptr, nullptr) == SSH_AUTH_SUCCESS)
                ok = true;
            ssh_disconnect(s);
        }
        ssh_free(s);

        if (ok) {
            Trc(0, "host=" << h << " recovered" << endl);
            G.host_available[h] = true;
            // If every host is now available, clear the status file.
            bool any_bad = false;
            for (const auto& hh : G.remote_host_list)
                if (!G.host_available[hh]) { any_bad = true; break; }
            if (!any_bad) unlink("/usr/cnc/data/niimx.sshproblem");
        } else {
            Trc(1, "host=" << h << " still unavailable" << endl);
        }
    }
```

- [ ] **Step 2: Build and commit**

```bash
cd cnc/niimx/src && nmake
git add cnc/niimx/src/niimxd.cpp
git commit -m "niimxd: periodically re-validate unavailable remote hosts"
```

---

## Task 10: Config hot-reload for remote_hosts / remote_nconn

**Files:**
- Modify: `cnc/niimx/src/niimxd.cpp` — `niimx_handle_config_reload`.

**Pre-existing issue introduced by Task 4 that this task must fix:**

The existing `nconn` reload upper bound `new_cfg.nconn <= G.bc_capacity` (~line 1581) was safe when `bc_capacity == nconn`, but after Task 4, `bc_capacity` includes remote slots. A raise of `NIIMX.NCONN` could grow `Cfg.nconn` into indices that already hold remote slots, stomping their `m_login`/`m_tmout` in the trailing `for (int i = old_nconn; i < Cfg.nconn; i++)` block.

Fix: change the upper bound from `G.bc_capacity` to the **local-slot count**. Add a new global `int local_capacity` to `NiimxGlobalState` set once in `main()` to `Cfg.nconn` at startup, and use `new_cfg.nconn <= G.local_capacity` here. Document `local_capacity` in the global state Doxygen block.

- [ ] **Step 1: Detect host-list changes**

After the existing safe-field copy block, add:

```cpp
    vector<string> new_hosts;
    niimx_parse_remote_hosts(new_cfg.remote_hosts, new_hosts);

    // Removed hosts: terminate their bchannels, drop availability entries
    for (const auto& old_h : G.remote_host_list) {
        bool kept = false;
        for (const auto& nh : new_hosts) if (nh == old_h) { kept = true; break; }
        if (kept) continue;
        Trc(0, "Config reload: host removed " << old_h << endl);
        for (int i = 0; i < G.bc_capacity; i++) {
            if (Bc[i].m_remote && Bc[i].m_remote_host == old_h &&
                Bc[i].m_state != bchannel::notconnected)
                niimx_terminate_bchannel(Bc[i]);
        }
        G.host_available.erase(old_h);
    }

    // Added hosts: try to claim free remote slots within capacity
    for (const auto& nh : new_hosts) {
        bool existed = false;
        for (const auto& old_h : G.remote_host_list) if (old_h == nh) { existed = true; break; }
        if (existed) continue;
        Trc(0, "Config reload: host added " << nh << endl);
        G.host_available[nh] = true;
        int assigned = 0;
        for (int i = 0; i < G.bc_capacity && assigned < new_cfg.remote_nconn; i++) {
            if (!Bc[i].m_remote) continue;
            if (!Bc[i].m_remote_host.empty()) continue;  // free slot
            Bc[i].m_remote_host = nh;
            assigned++;
        }
        if (assigned < new_cfg.remote_nconn)
            Trc(0, "Config reload: only " << assigned
                   << " free slots for new host " << nh << endl);
    }

    G.remote_host_list = new_hosts;
    Cfg.remote_nconn   = new_cfg.remote_nconn;
    strcpy(Cfg.remote_hosts, new_cfg.remote_hosts);
```

The "free slots" logic above relies on slots created beyond the initial host list being available. Because Task 4 sizes Bc[] based on the startup host-list size, hot-add of new hosts is best-effort within remaining capacity. Document this in the Doxygen header for the function.

- [ ] **Step 2: Build, smoke, commit**

```bash
cd cnc/niimx/src && nmake
# manually edit /usr/cnc/features/system: change REMOTE_HOSTS, watch trace for reload lines
git add cnc/niimx/src/niimxd.cpp
git commit -m "niimxd: hot-reload remote host list and per-host nconn"
```

---

## Task 11: Status report — show host label for remote bchannels

**Files:**
- Modify: `cnc/niimx/src/niimxd.cpp` — `niimx_status_report`.

- [ ] **Step 1: Add remote label to per-slot lines**

Inside the for-loop that prints `SLOT i pid=… cmds=… rss=…`, change the Trc to:
```cpp
            Trc(0, "  SLOT" << i
                << (bc.m_remote ? string(" [remote:") + bc.m_remote_host + "]" : string(""))
                << " pid=" << bc.m_pid
                << " cmds=" << bc.m_msgcnt
                << " rss=" << niimx_fmt_rss(rss, rssbuf, sizeof(rssbuf)) << endl);
```

Also expand the state-count loop bound to `G.bc_capacity`.

- [ ] **Step 2: Build, send SIGUSR1, verify, commit**

```bash
cd cnc/niimx/src && nmake
kill -USR1 $(cat /usr/cnc/procs/niimx/pid)   # or pgrep niimxd
grep '\[remote:' /usr/cnc/trace/niimxd
git add cnc/niimx/src/niimxd.cpp
git commit -m "niimxd: status report includes remote host label"
```

---

## Task 12: Update niimx test client — `-H host` flag

**Files:**
- Modify: `cnc/niimx/src/niimx.cpp`.

- [ ] **Step 1: Add `-H` to getopt and a `host` string**

In `main()` declare:
```cpp
const char* host = "";
```
Add `H:` to the getopt string and a case:
```cpp
case 'H': host = optarg; break;
```

- [ ] **Step 2: Update usage text**

Add: `  -H host       Target remote host (default: empty = local)\n` and update the synopsis line.

- [ ] **Step 3: Insert host frame between empty and msgid**

Replace:
```cpp
    send_frame(sock, "",      0,              true);
    send_frame(sock, &msgid,  4,              true);
```
With:
```cpp
    send_frame(sock, "",      0,                true);
    send_frame(sock, host,    strlen(host),     true);
    send_frame(sock, &msgid,  4,                true);
```

- [ ] **Step 4: Build, run end-to-end**

```bash
cd cnc/niimx/src && nmake
# Local target (host = "")
$BASE/3b2/bin/niimx -c who
# Remote target (requires Task 5 SSH setup against localhost)
$BASE/3b2/bin/niimx -H localhost -c who
```

Expected: both invocations print a `who` response. Trace for the second shows it dispatched to a `[remote:localhost]` slot.

- [ ] **Step 5: Commit**

```bash
git add cnc/niimx/src/niimx.cpp
git commit -m "niimx: add -H flag and host frame to wire format"
```

---

## Task 13: Update `niimxlib.c` API — host parameter

**Files:**
- Modify: `cnc/utility/src/niimxlib.c`, `include/utilmisc.h`.

- [ ] **Step 1: Change the API signature**

`include/utilmisc.h:108`:
```c
int niimx(const char *host, const char *command, char **rsp, int *nbytes, int tmout);
```

- [ ] **Step 2: Update `niimxlib.c`**

Function signature matches; insert one extra send call:
```c
int niimx(const char *host, const char *command, char **rsp, int *nbytes, int tmout)
{
    /* … existing setup unchanged … */
    {
        int32_t msgid = NIIMX_MSGID;
        int32_t mpid  = (int32_t)getpid();
        int32_t t     = (int32_t)tmout;
        const char *h = host ? host : "";

        /* Request: empty | host | msgid(4) | mpid(4) | tmout(4) | command */
        niimx_send_frame(sock, "",       0,               1);
        niimx_send_frame(sock, h,        strlen(h),       1);
        niimx_send_frame(sock, &msgid,   sizeof(msgid),   1);
        niimx_send_frame(sock, &mpid,    sizeof(mpid),    1);
        niimx_send_frame(sock, &t,       sizeof(t),       1);
        niimx_send_frame(sock, command,  strlen(command), 0);
    }
```

- [ ] **Step 3: Confirm no existing callers**

Run: `grep -rn '\bniimx(' --include='*.c' --include='*.cpp' --include='*.h' | grep -v 'niimx/src\|niimxlib\|niimx_\|//\|^[^:]*:\s*int niimx('`
Expected: empty. (Validated at plan-writing time — the only references are the prototype in `utilmisc.h` and the body in `niimxlib.c`.)

- [ ] **Step 4: Build the library**

```bash
cd cnc/utility/src && nmake -f util.mk
```
Expected: clean build.

- [ ] **Step 5: Commit**

```bash
git add cnc/utility/src/niimxlib.c include/utilmisc.h
git commit -m "niimxlib: add host parameter to niimx() API"
```

---

## Task 14: End-to-end verification matrix

No new code. Run each scenario and confirm.

| Scenario | Command | Expected |
|----------|---------|----------|
| Local request, daemon healthy | `niimx -c who` | who response |
| Remote request, host valid | `niimx -H localhost -c who` | who response via SSH; trace shows `[remote:localhost]` |
| Unknown host | `niimx -H bogus.example -c who` | `NIIMX^Unknown host: bogus.example` |
| Configured host with broken auth | `niimx -H badauth.example -c who` (configured but no key) | `NIIMX^Host unavailable: badauth.example` |
| Config hot-add a host | append to `NIIMX.REMOTE_HOSTS`, touch the file, `niimx -H newhost -c who` | response after the validator wakes up |
| Config hot-remove a host | shorten `NIIMX.REMOTE_HOSTS`, `niimx -H removed -c who` | `NIIMX^Unknown host: removed` |
| SSH key restored after failure | break, then restore key, wait 60s, `niimx -H host -c who` | response; `niimx.sshproblem` removed |
| Local USER process crash | kill the local USER pid manually | client gets `IIMX^USER process exited …`; spare reseeded |
| Remote SSH dropped mid-command | drop sshd while command running | client gets `NIIMX^Internal Error: remote connection lost`; pool reseeds |
| SIGUSR1 status report | `kill -USR1 niimxd_pid` | trace shows local + `[remote:…]` slot lines |

Capture trace excerpts for each row in `docs/superpowers/plans/2026-05-11-niimx-remote-ssh-results.md`.

- [ ] **Step 1: Run the matrix**

- [ ] **Step 2: Commit the results document**

```bash
git add docs/superpowers/plans/2026-05-11-niimx-remote-ssh-results.md
git commit -m "niimxd: e2e verification results for remote SSH bchannels"
```

---

## Self-review notes

- **Spec coverage:** all sections of the design doc map to a task:
  - Data model → Task 2
  - ZMQ protocol → Tasks 7, 12, 13
  - SSH lifecycle → Task 5
  - Read/write branching → Task 6
  - Routing & dispatch → Tasks 7, 8
  - Host management & health → Tasks 5, 9
  - Status report → Task 11
  - Client changes → Tasks 12, 13
  - Error messages → Task 7 (rejection text) + Task 6 (channel dropped text); table values match the design's table.
- **Loop bounds:** Tasks 4, 8, 10, 11 each ensure relevant loops switched from `Cfg.nconn` to `G.bc_capacity`. Local-only "warm spare" logic in Task 8 keeps the local check at `Cfg.nconn`.
- **Compatibility:** `gcc 4.8.5` supports range-for + lambdas used here; no `std::optional`, no structured bindings.
- **Risk note:** Hot-adding a host that wasn't in the startup list will only succeed up to the remaining unused slots in `Bc[]`. A future enhancement could reallocate the pool, but that's out of scope per the design's "within capacity" wording.
