# niimxd Remote SSH — Deferred Follow-ups

Items surfaced by the final code review of branch `niimx-netflex` (commits `3c2da1f5b..56c4e89e8`) that were intentionally deferred from the initial landing. Captured here so they aren't lost.

Related docs:
- Design: `/home/dan/Git/niimx-netflex/2026-04-09-niimx-remote-ssh-design.md`
- Plan: `/home/dan/Git/niimx-netflex/docs/superpowers/plans/2026-05-11-niimx-remote-ssh.md`

---

## 1. Per-host backoff on channel/PTY/exec failure

**Severity:** Important — operational risk under sustained sshd misconfig.

**Where:** `niimx_ssh_connect` in `cnc/niimx/src/niimxd.cpp` (the post-auth failure paths: `ssh_channel_new`, `ssh_channel_open_session`, `ssh_channel_request_pty`, `ssh_channel_request_exec`, `ssh_get_fd`, `epoll_ctl`).

**Symptom:** When a host's TCP/auth is healthy but channel/PTY/exec consistently fails (e.g., sshd `MaxStartups` exhausted, `ForceCommand` denying exec, `PermitTTY no`), `niimx_rebalance_pool` calls `niimx_ssh_connect` once per tick (1 s). Each failure logs at `Err()` level. No backoff and no eventual "give up" — log spam at ~1 line/s indefinitely.

**Fix sketch:** Track a per-host consecutive-failure count + last-attempt timestamp. Either:
- After N consecutive failures (say 5), set `host_available = false` so the existing periodic re-validation in `niimx_monitor_user_growth` handles recovery, or
- Apply per-slot exponential backoff (cap at 5 min) on `seed_in_group` before calling `niimx_ssh_connect`.

Variant (b) preserves the "host_available reflects auth-level reachability" semantic — better. Probably want a small `unordered_map<string, struct { int fails; TimePoint next_try; }>` in `NiimxGlobalState`.

---

## 2. Emit design-spec specific error strings

**Severity:** Important — spec compliance.

**Where:** `niimx_handle_zmq` rejection block in `cnc/niimx/src/niimxd.cpp`.

**Design says** (table at design.md:199–207):

| Scenario | Spec | Emitted? |
|----------|------|----------|
| Unknown host | `NIIMX^Unknown host: <host>` | yes |
| Host unavailable | `NIIMX^Host unavailable: <host>` | yes |
| SSH connect failed | `NIIMX^Remote connection failed: <host>` | **no** — surfaces as "Host unavailable" |
| SSH auth failed | `NIIMX^Authentication failed: <host>` | **no** — surfaces as "Host unavailable" |
| SSH channel dropped | `NIIMX^Internal Error: remote connection lost` | yes (in `niimx_handle_pty`) |

The first failure of a host flips `host_available[host] = false`. Subsequent requests get the generic `Host unavailable: …` reply, losing the connect-vs-auth distinction.

**Fix sketch:** Replace `unordered_map<string,bool> host_available` with `unordered_map<string, enum class HostState { OK, ConnectFailed, AuthFailed, OtherUnavailable }>`. In `niimx_ssh_connect` (and `niimx_ssh_validate_keys`), record which failure happened. In `niimx_handle_zmq`, render the matching specific string.

Or simpler: keep the bool but add `unordered_map<string, string> host_last_error_string;` with the pre-built reply text. Daemon just looks up and sends.

**Alternative:** update the design doc to drop the two specific strings; let the daemon keep its terser uniform reply.

---

## 3. niimxlib endpoint bug (pre-existing)

**Severity:** Important but pre-existing — not introduced by this branch.

**Where:** `cnc/utility/src/niimxlib.c` lines 36–55.

Lines 36–42 load `niimx_endpoint` from sysdef (with macro fallback). Line 55 calls `zmq_connect(sock, NIIMX_ENDPOINT)` — the macro, not the loaded variable. The sysdef lookup is dead code.

**Fix:** one-liner replace `NIIMX_ENDPOINT` with `niimx_endpoint` on line 55.

```c
if (zmq_connect(sock, niimx_endpoint) != 0)
```

Worth landing in any commit that touches this file next — it was modified in this branch (Task 13), and a careful reader will be confused.

---

## 4. Doxygen drift (pre-existing)

**Severity:** Minor — readability only.

Stale doc-comments in `cnc/niimx/src/niimxd.cpp`:

- **`NiimxGlobalState::monitor_tfd` @var line (~line 332):** says "10-minute timerfd" — actual `NIIMX_MONITOR_INTERVAL = 60` is 60 s. The function-level Doxygen at ~line 714 correctly says "60-second."
- **`niimx_handle_timer` Doxygen (~line 2006):** says "Every 300 ticks runs the orphan-kill command" — that block does not exist in the function (removed at some point pre-this-branch).
- **`niimx_terminate_bchannel` Doxygen (~line 1383):** says "Sends SIGTERM to the child process group via incps." There is no `incps` call; it's `kill(bc.m_pid, SIGTERM)`.
- **Function-level Doxygen touched by Task 6** (transport branching): `niimx_pty_write`, `niimx_send_command_to_bchannel`, `niimx_terminate_bchannel`, `niimx_handle_pty` headers still describe local-PTY-only behavior despite now handling both transports.

**Fix:** one cleanup commit. Low value individually; bulk it.

---

## 5. End-to-end verification matrix

**Severity:** Important — verification gap.

The plan's Task 14 (10-scenario matrix) was deferred because this dev box has no live netFLEX environment (`/usr/cnc/.CNC_UP` missing, no remote SSH host, no `USER` binary, no real sysdef).

**To run** when on a real netFLEX host:

1. Build the branch: `cd $BASE/cnc/niimx/src && nmake && cd $BASE/cnc/utility/src && nmake -f util.mk ../../../3b2/lib/libinc.so`
2. Configure sysdef: add `NIIMX.REMOTE_HOSTS=<host1>,<host2>` and `NIIMX.REMOTE_NCONN=2`.
3. Ensure SSH key from netflex user is in `~/.ssh/authorized_keys` on each remote host.
4. Restart niimxd. Tail `/usr/cnc/trace/niimxd`. Verify:
   - `Pool: local=<n> remote_hosts=<N> remote_nconn=2 total_slots=<n+2N>`
   - `SSH key validation: <N>/<N> hosts authenticated`
   - One `rebalance seeded slot=… host=<h>` per remote host.

The full 10-scenario matrix (in the plan doc under Task 14) covers: local, remote-OK, unknown-host, broken-auth, hot-add, hot-remove, key-restored, USER crash, SSH drop mid-command, SIGUSR1 status report.

Capture trace excerpts into `docs/superpowers/plans/2026-05-11-niimx-remote-ssh-results.md` per the original plan.

---

## 6. Minor suggestions from final review (nice-to-have)

Not blockers. Pick up if/when the file is touched.

- **`niimx_handle_zmq` RX log doesn't include `host`** (~line 920). Adding `host=…` to the `Trc(3, "RX …")` line makes dispatch debugging easier.
- **`niimx_dispatch_requests` is O(qlen × bc_capacity) per cycle.** Fine at typical pool sizes; revisit if measurement shows otherwise. Caching `ready_index_by_group` would make it O(q).
- **`niimx_send_command_to_bchannel` return value unused.** Sole caller ignores it. Either make it `static void` or have the caller act on the result.
- **`niimx_ssh_disconnect` silently ignores `epoll_ctl EPOLL_CTL_DEL` errors.** Correct given idempotency, but the Doxygen should call that out explicitly so a reader doesn't think it's a bug.
- **`niimx_terminate_bchannel` doesn't reset `m_remote` / `m_remote_host`.** Intentional — those are slot-identity. Worth a one-line comment to make the intent explicit.
- **Health recovery doesn't trigger immediate seed.** `niimx_monitor_user_growth` flips `host_available=true` on recovery; the next main-loop tick handles rebalance naturally, but warm-up takes `remote_nconn` ticks. A direct call to `niimx_rebalance_pool` after a recovery flip would warm up faster.
- **`LOGNAME=%s` in `ssh_channel_request_exec` command string** — current `Cfg.user` is trusted, but if a future change ever derives login from request input, validate it matches `[A-Za-z0-9._-]+` at config load to prevent shell injection in the remote exec.

---

## Status

Critical drain bug (`6a7e540dc`) and orphan-slot bug (`56c4e89e8`) both fixed during review. Branch is functionally complete pending the matrix run in §5 above.
