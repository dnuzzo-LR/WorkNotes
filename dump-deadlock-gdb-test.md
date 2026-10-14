# dump signal-handler deadlock: gdb test

This reproduces, on demand, the `dump` hang that blocks shutdown at
"Waiting for Database to be Saved to Disk". Run it once against the stock
`dump` (it should hang) and once against the patched build from branch
`55.0/fix/dump-signal-deadlock` (it should exit cleanly).

## How it works

`dump`'s main loop calls `localtime()` in `set_sleep()` on every iteration,
and the signal handler can still run at that point. `localtime()` takes
glibc's internal `tzset_lock`, which is not reentrant. The handler,
`caught_sig()`, logs `TRACE(D0, "EQUIP CHG IGNORED!...")`, and the old
`trace3()` also calls `localtime()`. So a SIGUSR2 delivered while `main()` is
inside `localtime()` deadlocks for good. The handler's `sa_mask` is full, so
SIGTERM can't get through after that.

## One-time setup

`dump` is a 32-bit binary, so gdb needs the i686 glibc symbols:

```
dnf debuginfo-install glibc.i686
```

## Steps

**Shell 1: attach**
```
pid=$(pgrep -x dump); echo $pid
gdb -p $pid
```

**In gdb: let the normal signals pass, and stop in `set_sleep()`**
```
(gdb) handle SIGUSR2 nostop noprint pass
(gdb) handle SIGALRM nostop noprint pass
(gdb) break set_sleep
(gdb) continue
```

**Shell 2: wake the main loop up**
```
kill -USR2 <pid>
```
The handler runs normally, the loop comes round, and gdb stops in `set_sleep`.

**In gdb: stop inside `localtime()`, while it holds `tzset_lock`**
```
(gdb) break __offtime
(gdb) continue
(gdb) bt
```
Check that the backtrace is `__offtime <- __tz_convert <- localtime <- set_sleep <- main`
and that **no `caught_sig`** frame appears. If `caught_sig` is there, run
`continue` again until it's gone.

**Send the signal at that point, then let it run**
```
(gdb) shell kill -USR2 <pid>
(gdb) delete
(gdb) continue
```

## Expected results

### Stock binary: it hangs

Wait a few seconds, press `Ctrl-C`, then run:
```
(gdb) thread apply all bt
```
You should see roughly:
```
__lll_lock_wait <- __tz_convert <- localtime <- trace3 <- caught_sig
  <- <signal handler called>
  <- __offtime <- __tz_convert <- localtime <- set_sleep <- main
```
Save that output for the PR and for issue #6785.

To confirm it's the same hang seen on holmvm02:
```
(gdb) detach
(gdb) quit
strace -p <pid>         # futex(..., FUTEX_WAIT_PRIVATE, 2, NULL
kill -TERM <pid>        # does nothing
kill -9 <pid>           # cleanup
```

### Patched binary: no hang

The SIGUSR2 waits while `set_sleep()` finishes, and `caught_sig` runs once the
loop reaches `sigsuspend()`. A new `EQUIP CHG IGNORED! (n/100)` line appears in
`/usr/cnc/trace/dump`. Then:
```
(gdb) detach
(gdb) quit
kill -TERM <pid>        # dump writes /usr/cnc/backup and exits
pgrep -x dump           # nothing (or a fresh pid, if something restarts it)
```

## If something doesn't work

- **`break __offtime` says the function isn't defined:** the glibc debuginfo
  isn't loaded. Check `info sharedlibrary` in gdb and the debuginfo-install
  output.
- **`break set_sleep` fails:** the `dump` binary has no symbols. Use
  `break localtime` instead and check with `bt` that the stop is from
  `set_sleep`, not `caught_sig`. You still need `break __offtime` (and so the
  glibc debuginfo), because `tzset_lock` is taken further inside `localtime()`.
- **The process exits early:** `EqptChgsHour` is 100, so a hundred
  `EQUIP CHG IGNORED` signals within the hour make `dump` save and exit.
  Restart `dump` if the count gets near 100.

## Scope

This test exercises one lock: `tzset_lock`, reached through TRACE. The handler
also calls `system()` (fork and the malloc locks) and libpq (the RDB flush), which
can deadlock the same way. The patch covers all of these, because `caught_sig()`
can no longer interrupt `main()` anywhere except while it's waiting.
