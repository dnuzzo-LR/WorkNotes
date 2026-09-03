# Ping / Comm-Tunable Files (`*.ping`)

Reference for the `.ping` tunable files parsed by `load_comm_tunes()` in
`cnc/sdi/src/comm_tunable.c`. These files tune the LAN/serial comms behavior of
the `lan` family of daemons (`lan.c`, `lan_s.c`, `nt_lan.c`, `ruby_lan.c`,
`intrfc.c`, `g2out.c`): polling, pinging, login pacing, auto-recovery timers, and
per-vendor quirks.

---

## 1. Which file gets loaded

At startup each lan daemon picks a ping file **by NE type (`DCSType`)**, with
fallbacks (`lan.c:490`):

```
/usr/cnc/tables/T.<DCSType>.ping      # type-specific, tried first
```

If that does not exist, one generic fallback is used:

| Condition                | File                          |
|--------------------------|-------------------------------|
| `DCSType == SNC2000`     | `/usr/cnc/tables/snc.ping`    |
| serial connection open   | `/usr/cnc/tables/sgi.ping`    |
| otherwise (LAN)          | `/usr/cnc/tables/lan.ping`    |

If none open, the daemon logs `UNABLE TO LOAD TUNABLES` and keeps the values
compiled into the binary (the defaults below). Sample templates live in
`3b2/data/` (`lan.ping`, `def.ping`, `nxdi.ping`).

After a successful load, `FTPResponseTime` is derived as `PingResponseTime * 4`.

---

## 2. File format — two sections, in order

The parser reads the file top-to-bottom in **two phases**:

**Phase A — 10 positional integer lines (order matters).**
The first 10 non-comment lines are consumed *by position*, one integer per line.
A line whose first character is `#` is skipped. There is **no way to skip a
positional slot** — every non-`#` line is consumed by the next slot in sequence,
so you cannot reorder or omit them. To "keep the default" for a positional field,
either supply the default value or supply a value that fails the field's
constraint (the parser logs `... BOGUS` at trace level `D0` and keeps the
compiled default).

**Phase B — keyword lines (`KEY=VALUE`, any order).**
Every remaining non-comment line is matched as `KEY=VALUE`. Dispatch is by the
**first character** of the line, then an exact `strncmp`/`sscanf` match. Rules:

- Case-sensitive; must match exactly, no spaces around `=` (e.g. `LOGIN_TRYS=0`).
- Unrecognized lines are **silently ignored**.
- A `#` in column 1 comments the line out.

`DIFF_SECS_OFFSET` is *not* read from the ping file — it comes from system
defaults via `get_sysdef_int()` (default 15) at the top of `load_comm_tunes()`.

---

## 3. Phase A — positional fields (in order)

| # | Meaning              | Variable               | Compiled default        | Constraint (else default kept) |
|---|----------------------|------------------------|-------------------------|--------------------------------|
| 1 | Poll Cycle           | `PollCycle`            | `POLLING_CYCLE` = 15    | `> 9` (i.e. ≥ 10 secs)         |
| 2 | Ping Time            | `PingTime`             | `PING_TIME` = 300       | `≥ PollCycle`                  |
| 3 | Ping Response Time   | `PingResponseTime`     | `PING_RESPONSE` = 15    | `≥ PollCycle`                  |
| 4 | Ping Retries         | `PingRetries`          | `PING_RETRIES` = 1      | none enforced (intended ≥ 1)   |
| 5 | Ping GNE             | `PingGNE`              | `PING_GNE` = 1          | 1 = yes, 0 = no                |
| 6 | Max Msgs / Cycle     | `MaxMsgsPerCycle`      | `MAXMSGPERCYCLE` = 30   | `> 1` (intended ≥ 2)           |
| 7 | Wait Login Response  | `WaitForLogin`         | `WAITFORLOGIN` = 30 *   | `≥ PollCycle`                  |
| 8 | Dual GNE Login Attempts | `DualGNELoginAttempts` | `DUALGNETRIES` = 1  | any int (0 = never switch)     |
| 9 | Auto DNO Timer (secs)| `AutoDNOTimer`         | 300                     | non-zero (0 → kept, logged BOGUS) |
| 10| Login Delay (secs)   | `LoginDelay`           | 0                       | reset to 0 each load; only set if non-zero |

\* Macro `WAITFORLOGIN` is 30, but some modules (`lanalive.c`, `lanstat.c`)
initialize `WaitForLogin`/`PingTime`/`PingResponseTime` to different values
(60/60/10). The ping-file value overrides whatever the module started with.

All secs unless noted. "Compiled default" = value if the ping file is missing or
the field fails its constraint. Constraints come from `PING TIME`/`PING RESPONSE
TIME`/`WaitForLogin`/`MAX MSGS`/`POLLING CYCLE`/`AUTO DNO` checks in
`comm_tunable.c:172-315`.

---

## 4. Phase B — keyword fields (`KEY=VALUE`, any order)

### Polling / pinging

| Key                | Variable            | Default            | Meaning / constraint |
|--------------------|---------------------|--------------------|----------------------|
| `PINGPAUSESECS=`   | `PingPauseSecs`     | 60                 | Secs to pause pinging after a ping is sent. |
| `PING_RT=`         | `PingRT`            | 1                  | Ping remote terminals (RTs) as well as GNE. 1 = on. |
| `FORCE_PING=`      | `ForcePing`         | 0                  | Send a ping every cycle unconditionally. 1 = force. |
| `TELLABS_5500_PING=` | `Tellabs5500PingOvr` | 0              | Override ping behavior for Tellabs 5500. |
| `DMX_POLL_CYCLE=`  | `DMXPollCycle`      | 10                 | Poll cycle override when a DMX acts as GNE for many RTs. |
| `DDX_MAX_MSGS_CYCLE=` | `DMXMaxMsgsPerCycle` | 5             | Max msgs/cycle override for DMX-as-GNE rings (> 5 nodes). |
| `MAX_COMM_GRP_MSGS=` | `MaxCommGrpMsgs`  | 5                  | Max msgs sent to a comm group per ~2 s poll interval. Distinct from Max Msgs/Cycle. |

### Fast-poll (transient rapid polling)

| Key                 | Variable          | Default             | Meaning |
|---------------------|-------------------|---------------------|---------|
| `FASTPOLL_FEATURE=` | `FastPollFeature` | 1                   | Enable fast-poll feature. Comment convention: 0 = default, >1 = debug. |
| `FASTPOLL_DURATION=`| `FastPollDuration`| 5 (secs)            | How long a fast-poll burst lasts. |
| `FASTPOLL_TIMER=`   | `FastPollTimer`   | 500000 (microsecs = .5 s) | Inter-poll interval during fast poll, in **microseconds**. |

### Login / auth pacing

| Key                     | Variable              | Default | Meaning |
|-------------------------|-----------------------|---------|---------|
| `LOGIN_TRYS=`           | `Login_trys`          | 0       | Login attempts before auto-disable. 0 = no limit. |
| `REPEAT_LOGIN_ATTEMPT=` | `RepeatLoginAttempt`  | 15 (secs) | Secs between ACT-USER re-sends in the login loop. |
| `RTLOGINDELAY=`         | `RtLoginDelay`        | 0       | Delay (secs) before logging in each RT. |
| `GNERTLOGINGAP=`        | `GneRtLoginGap`       | 0       | Per-GNE gap (secs) pacing RT logins (issue 3614, DTN/DTN-X). 0 = disabled. **Clamped to [0, `GNE_RT_LOGIN_GAP_MAX`=10]**; out-of-range values are logged and clamped. |
| `MAX_RT_PER_GNE=`       | `MaxRtPerGne`         | 0       | Max RTs brought up under one GNE (0 = unlimited). |
| `READ_BEFORE_ACTUSER=`  | `ReadBeforeActUser`   | 0       | Drain/read input before sending ACT-USER. 1 = on. |
| `SEND_INTERFACE_CANC_USER=` | `Send_interface_canc_user` | 0 | Send CANC-USER to the GNE for TL1 "interface" NE types. Default off. |
| `SEND_CONTINUE_CHAR=`   | `SendContineChar`     | 0       | ASCII (decimal) continue char sent during login. 0 = none. |
| `TELLABS_TACACS=`       | `TlbsTacacs`          | 0       | Tellabs 530/532 auth mode. 0 = legacy inline auth; 1 = INS TACACS+ interactive `PASSWORD:` challenge. |

### Connection / transport

| Key                   | Variable             | Default | Meaning |
|-----------------------|----------------------|---------|---------|
| `TELNET_PORT=`        | `Telnet_port`        | 23      | TCP port for telnet sessions. |
| `TELNET_PSEUDO_TTY=`  | `TelnetUsePseudoTty` | 0       | Allocate a pseudo-tty for the telnet session. 1 = on. |
| `LAN_READ=`           | `LanRead`            | 1       | Read mode: 1 = read a block, 0 = read a single char. |
| `INSERT_CHAR=`        | `Insert_char`        | 0       | ASCII (decimal) char inserted where needed. Auto-set to 10 (newline) for `ALC_1830_PSS32` over SSH when left 0. |
| `SSH_HOST_KEY_ALGORITHMS=` | `HostKeyAlgorithms[100]` | "" (empty) | SSH host-key algorithm list string. Reset empty each load. |
| `MAC_SPEC=`           | `MacSpec`            | NULL    | Comma-separated SSH MAC algorithm list. ⚠️ `MacSpec` is a null `char *`; `sscanf("%s", MacSpec)` writes into NULL — setting this key as written is unsafe (latent bug). |
| `SSH_LOGIN_END_BANNER_PROMPT=` | `SSHLoginEndBannerPromptRegex` | NULL | POSIX extended regex marking end of SSH login banner. Compiled via `parse_regex_tune()`; trailing newline stripped; invalid regex is rejected and the previous value kept. |

### Auto-recovery / match timers (secs link must be down)

| Key                | Variable          | Default | Meaning |
|--------------------|-------------------|---------|---------|
| `AUTOPARMTIMER=`   | `AutoPARMTimer`   | 300     | Secs link down before Auto PARM match. |
| `AUTOXCONTIMER=`   | `AutoXCONTimer`   | 0       | Secs link down before Auto XCON match. 0 = off. |
| `AUTOALARMTIMER=`  | `AutoALARMTimer`  | 0       | Secs link down before Auto ALARM match. 0 = off. |
| `AUTO_DISABLE=`    | `AutoDisable`     | 0       | Enable auto-disable of a failing NE. |
| `AUTO_ENABLE=N/M`  | `AutoEnable` / `AutoEnableRetries` | 0 / 0 | Auto re-enable. Format `N/M`: N enables, M = retry count. |

### Alarms / message accounting

| Key                   | Variable          | Default | Meaning |
|-----------------------|-------------------|---------|---------|
| `ALARM_CNT=`          | `AlarmCnt`        | 0       | Alarm count threshold within the duration window. |
| `ALARM_CNT_DURATION=` | `AlarmCntDuration`| 0       | Window (secs) over which `ALARM_CNT` alarms are counted. |
| `INMSGALARM=`         | `InMsgAlarm`      | 14      | Threshold for the in-message alarm. |
| `NOMSGALARM=`         | `NoMsgAlarm`      | 2       | Threshold for the no-message alarm. |
| `BADTL1=`             | `BadTL1DescField` | 0       | NE emits `\r\n` inside TL1 DESC fields; enable workaround. 1 = on. |

### Level-2 (ethernet) ping — only if built with `PING_LEVEL2`

| Key          | Variable    | Default | Meaning |
|--------------|-------------|---------|---------|
| `LVL2_PING=` | `Lvl2_time` | 0       | Idle secs (no msgs from NE) before sending an L2 ping. 0 = disabled. |
| `LVL2_NUM=`  | `Lvl2_num`  | 10      | L2 ping tries before declaring failure. |
| `LVL2_WAIT=` | `Lvl2_wait` | 2       | Secs between L2 pings. |

### TID swap

| Key       | Variable                     | Default | Meaning |
|-----------|------------------------------|---------|---------|
| `TID_SWAP=<netFlexTID>^<machineTID>` | `netFlex_TID`, `machine_TID` | empty | Map the TID netFLEX uses to the TID on the machine. Format: two `^`-separated fields. If only one field parses, `netFlex_TID` is cleared. Not compiled in under `SMALLER_LINUX_LAN` (exits if used there). |

---

## 5. Worked example (`3b2/data/lan.ping`)

```
#Poll Cycle
20              # Phase A #1  PollCycle        (>9 OK)
# PingTime
300             # Phase A #2  PingTime         (>=20 OK)
# PingResponseTime
15              # Phase A #3  PingResponseTime (>=20? 15<20 -> BOGUS, keeps default)
# Ping Retries
1               # Phase A #4  PingRetries
# Ping GNE 1=YES, 0 = NO
1               # Phase A #5  PingGNE
# Max Message / Cycle
10              # Phase A #6  MaxMsgsPerCycle  (>1 OK)
# Login Response Time
15              # Phase A #7  WaitForLogin     (15<20 -> BOGUS, keeps default)
# Dual GNE Login Attempts
2               # Phase A #8  DualGNELoginAttempts
# Auto DNO Timer
300             # Phase A #9  AutoDNOTimer
# Login delay timer
0               # Phase A #10 LoginDelay       (0 -> stays 0)
LOGIN_TRYS=0    # Phase B keyword lines follow, any order
LVL2_PING=0
DMX_POLL_CYCLE=10
...
```

Note the trap in this shipped file: with `PollCycle=20`, the
`PingResponseTime=15` and `Login Response Time=15` lines are **below**
`PollCycle`, so they fail the `≥ PollCycle` constraint and the compiled defaults
are used instead (logged as `BOGUS` at trace `D0`). Keep positional values
`≥ PollCycle` where the constraint requires it.

---

## 6. Editing checklist

- Keep the **10 positional lines first**, in order, one integer each. Do not
  delete or reorder them; comment lines (`#`) between them are fine.
- Positional constraints that reference `PollCycle` (fields 2, 3, 7) must be
  `≥` your chosen Poll Cycle or they are silently dropped.
- Put all `KEY=VALUE` lines after the positional block. Order among them does
  not matter. Exact spelling, no spaces around `=`.
- To disable a keyword feature, either omit the line or comment it with `#`.
- Verify a load by watching the daemon trace: `LOAD TUNABLES:[<file>]` then the
  per-field `D2` echoes (and any `... BOGUS` at `D0`).

---

*Source of truth: `cnc/sdi/src/comm_tunable.c` (`load_comm_tunes`) and
`include/comm_tunable.h`. If this doc and the code disagree, the code wins —
update this file.*
