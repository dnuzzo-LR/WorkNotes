# SDD ledger — plan: /home/dan/WorkNotes/plans/2026-10-20-5726-mqorderd-plan.md
Spec: /home/dan/WorkNotes/plans/2026-10-14-5726-mqorderd-design.md
Branch: issue-5726-mq-circuit-feed, start 938608f2a

## Pre-flight scan
| Pair / task | Produces vs consumes | Finding |
|---|---|---|
| T1 ↔ T5,T6,T7 | config.Config/MQ/Orders fields | consistent |
| T1 ↔ T6 | T1 `go get` mq-golang; tidy drops it until imported | T6 must re-`go get` v5.7.3 before tidy/vendor |
| T2 ↔ T3,T5 | pages.Kind/Prefix/Split/Classify | consistent |
| T3 ↔ T5 | Writer.Write/KeepFailed/KeepRaw | T5 needs to keep *unwritten* remaining pages after exec failure; no API → add KeepPage |
| T3 ↔ spec (capture_only) | spec: capture writes junk too to orders.dir; T3 sends junk to JunkDir | conflict → ruling R2 |
| T4 ↔ T5 | journal Begin/MarkStarted/Load/Clear | consistent |
| T5 ↔ T6 | proc.Message/Queue/ErrNoMessage implemented by mqconn | consistent (mqconn imports proc, no cycle) |
| T5 ↔ T7 | Processor.Handle, Outcome | consistent; ctx checked only between messages matches StopFinishesMessage |
| T6 ↔ T7 | mqconn.Dial/Depth/IsConnectionError/Reason | consistent |
| T7 ↔ T8 | GO_SRC list main.go loop.go internal | consistent |
| T8 ↔ T9 | T8 step6 finds packaging; T9 adds data files same place | consistent |
| T1 self | tests vs interface | consistent |
| T2 self | tests vs rules (traced lterm by hand for EndOfOrder/CRLF cases) | consistent |
| T3 self | KeepJunk-after-ocy re-runs orderprint on ocy file = lterm behaviour | consistent |
| T4 self | ok | consistent |
| T5 self | KeepFailed may fail (file gone both places) — unspecified | ruling R4 |
| T6 self | ok | consistent |
| T7 self | sleeps [5s,10s,5s] with 4 dials | consistent |
| T8/T9 self | ok | consistent |
| T10 | needs a real QM from user | ruling R5 |
| Global | commit trailer text | ruling R1 |

Ruling R1: commit trailer = `Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>` — latest harness attribution supersedes plan text — cost if wrong: trailer wording only.
Ruling R2: capture_only junk pages go to orders.Dir as `junk.<printer>.0.<epoch>.<pid%06d>.<seq%02d>`, runOrderprint=false (spec "split and write every page (junk too) to orders.dir") — implemented in spool.Writer via Orders.CaptureOnly — cost if wrong: capture files in different dir.
Ruling R3: add `func (w *Writer) KeepPage(page []byte, msgID string, pageNo int) (string, error)` writing `<FailedDir>/page.<printer>.<msgID>.p<N>` for pages never written/handed over (exec failure after handover) — spec says keep unprocessed pages in failed_dir — cost if wrong: one extra method.
Ruling R4: if KeepFailed can't find the file in either place, trace an error naming both paths and continue (never crash, never back out) — orderprint already ran, so the page is handed over — cost if wrong: page only in trace.
Ruling R5: Task 10 (live QM e2e) is not run in SDD; it is handed to the user at finish since it needs a QM they must supply — cost if wrong: e2e delayed.
Ruling R6: Go code uses godoc comments, not Doxygen (CLAUDE.md Doxygen rule is C/C++) — cost if wrong: comment style.
Task 1: dispatched (implementer sonnet, BASE 938608f2a)
Task 1: review — Important: explicit log_level:0 / max_backout:0 overridden by defaults (part plan-mandated). Minor deferred: no positive decode test for capture_only/keepjunk (folded into fix); Validate returns first error only.
Ruling R7: keep plain int fields in Config (interface unchanged); detect presence during decode so explicit `log_level: 0` is honored; `max_backout` must be >=1 when present (0/negative → validation error) — spec says poison guard needs a positive count — cost if wrong: small decode change.
Task 1: minor (deferred): Validate reports only the first missing required field.
Task 1: fix round 1/5 (2 addressed, 0 open; commits 057e87c..4e00512)
Task 1: complete (commits 938608f..4e00512, review clean)
Task 2: dispatched (BASE 4e0051276)
Ruling R8: Split ends a page at every FF including the first even when data precedes it ("A\fB" → ["A","B"], as the brief's test mandates). lterm's pageCnt==0 rule would instead merge pre-first-FF data into the next page; for the expected leading-FF stream ("\fA\fB\f") both agree. Merging two orders' pages is worse than splitting — cost if wrong: a stream without a leading FF yields one extra page boundary vs lterm.
Task 2: minor (deferred): fixture test asserts counts/kinds not exact page bytes
Task 2: minor (deferred): no regression tests for empty input, lone CR ("A\rB"→"AB"), FF after EOO latch (behaviour verified correct by reviewer)
Task 2: minor (deferred): O(n²) bytes.Contains at every CR — run anchored scanPGRange first
Task 2: minor (deferred): Atoi overflow / NUL-byte divergences from C sscanf/strstr (pathological)
Task 2: complete (commits 4e00512..cd43b75, review clean)
Task 3: dispatched (BASE cd43b75)
Ruling R9: when CaptureOnly and KeepJunk are both set, CaptureOnly wins for junk pages (all junk to orders.Dir, never appended to ocy) — capture mode must not alter files; cost if wrong: capture output differs for keepjunk sites.
Task 3: review — Important: no test for R9 precedence. Minors folded into fix: drop unrequested/inconsistent sync.Mutex; check Close error in writeFile.
Task 3: minor (deferred): msgID interpolated into filenames unsanitized (msgID is hex from proc.Message.ID(), so safe today)
Task 3: fix round 1/5 (3 addressed, 0 open; commits dd5fcd4..5a7e7ef)
Task 3: complete (commits cd43b75..5a7e7ef, review clean)
Task 4: dispatched (BASE 5a7e7ef)
Task 4: minor (deferred): MarkStarted duplicates not deduped (proc must treat Started as a set)
Task 4: minor (deferred): Begin skips dir fsync on non-ENOENT Stat error (error still surfaces via OpenFile)
Task 4: minor (deferred): Open's MkdirAll not fsynced up the chain
Task 4: complete (commits 5a7e7ef..2f69a56, review clean)
Task 5: dispatched (BASE 2f69a56)
Ruling R10: on journal resume, the unknown-outcome page (max Started) is preserved with KeepPage(pageBytes) from the re-split message, not KeepFailed — the pre-crash file path (epoch/seq in name) is unknowable — cost if wrong: copy named page.* instead of lterm name.
Ruling R11: poison guard order = KeepRaw (disk) → PutBackout → Commit; a PutBackout error is traced but still commits because the message is safe on disk; a KeepRaw error → Backout (nothing preserved yet) — cost if wrong: poison message on disk but not on BOQ.
Ruling R12: Started is treated as a set (dedupe), per Task 4 minor.
Ruling R13 (accepting implementer gap-fills in T5): journal read error → Backout, journal untouched; Commit failure → (Committed, err) and journal kept so redelivery skips handed pages; stale journal traced at level 0; pre-handover backout removes the unused page file from Dir; orderprint-failed page stays in Dir as well as the FailedDir copy (lterm left failed files in place) — cost if wrong: extra files in printer dir / extra backouts.
Note for Task 7: treat any Handle error as reconnect; don't rely on Outcome when err != nil.
Task 5: review — Needs fixes. I1 keepjunk append re-runs orderprint on a failed ocy file; I2 double-failure commit paths lose pages (trace claims bytes it doesn't log); I3 poison path drops journal record; I4 stale-journal clear (plan-mandated).
Ruling R14 (I1): proc must never run orderprint on a path it already ran on whose file still exists (orderprint failed / left it). When spool appends a junk page to such a path, don't run; copy the appended file to FailedDir via KeepFailed and warn level 1 — spec invariant "never run twice" outranks lterm keepjunk parity — cost if wrong: keepjunk junk after a failed ocy isn't re-fed.
Ruling R15 (I2): on every double-failure path (write err + KeepRaw err, KeepPage err after handover, KeepPage err on resume) trace the page bytes at level 0 before committing — matches R4's accepted cost "page only in trace" — cost if wrong: large trace lines.
Ruling R16 (I3): poison path loads the journal; if MsgID matches, trace the started pages, KeepPage the unknown-outcome page, and word the trace "pages X already handed to orderprint; raw message kept" — cost if wrong: extra trace.
Ruling R17 (I4): accept the stale-journal clear (plan-mandated). Source puts orders at one priority and MQ returns a backed-out message to its queue position, so the crashed message is redelivered first; document the residual mixed-priority risk in a code comment and design open risks — cost if wrong: duplicate pages after a crash with mixed-priority messages.
Task 5: minor folded into fix: warn for appended junk (Minor 1); StaleJournal test must require level 0 (Minor 2).
Task 5: minor (deferred): orphan/duplicate files in Dir on pre-handover write error / crash between Write and MarkStarted / capture-mode redelivery
Task 5: minor (deferred): junk duplicated on redelivery (O_APPEND, deterministic names)
Task 5: minor (deferred): ExecRunner has no timeout; hung orderprint blocks shutdown (consider in Task 7 / final review)
Task 5: minor (deferred): KeepFailed fallback path is a no-op since Dir == printer_path
Task 5: fix round 1/5 (6 addressed, 0 open; commits 9df2b8a..919b62e)
Task 5: minor (deferred): pruneLeftBehind only before junk writes (clock-rollback name collision, theoretical)
Task 5: minor (deferred): poison loop appends duplicate copy of kept unknown-outcome page
Task 5: complete (commits 2f69a56..919b62e, review clean)
Task 6: dispatched (BASE 919b62e)
Ruling R18: accept T6 choices — a get completing with a non-truncation warning (e.g. 2119 not converted) returns the message with its unconverted bytes (it is already taken under syncpoint; proc will process/keep it); Close() backs out before MQDISC because a normal disconnect would commit an open UOW — cost if wrong: unconverted bytes reach orderprint.
Note for Task 7: never call methods on a nil *Conn; use /usr/bin/git (devtools git broken).
Task 6: review — Needs fixes. I1 connectionReasons has 2539 labelled 2537, real 2537 missing; I2 Connx MQCC_WARNING leaks a live hConn and reports failure.
Ruling R19: connectionReasons = brief list (with correct 2537) plus 2018 HCONN_ERROR (stale hConn after partial Close) and keep 2539 CHANNEL_CONFIG_ERROR out — 2539 is a config error that reconnect won't fix, but Task 7 retries all dial failures anyway — cost if wrong: one code classified differently.
Ruling R20: Connx MQCC_WARNING = success; return the Conn and surface the warning via a Warning field/trace hook is YAGNI — just treat as success (MQOPEN follows) — cost if wrong: warning not logged.
Task 6: minors folded into fix: Close order back-out→close→disconnect; cap buffer growth at 100 MiB; config.Validate: user set requires password_file; doc note on nil *Conn; 30*time.Second in test.
Task 6: fix round 1/5 (6 addressed, 0 open; commits 7878d90..be67ca2)
Task 6: complete (commits 919b62e..be67ca2, review clean)
Ruling R21: no orderprint timeout in ExecRunner (spec: never kill orderprint mid-run); systemd TimeoutStopSec bounds shutdown — cost if wrong: a hung orderprint blocks the daemon until someone intervenes.
Task 7: dispatched (BASE be67ca2)
Ruling R22: accept throttle reset only after first completed Get on a connection (prevents endless log lines on flap / GET_INHIBITED) — cost if wrong: a recovery line might be suppressed until a Get completes.
Note for Task 8: add -ldflags -X version stamp so `mqorderd -v` isn't "(devel)".
Task 7: review — Approved but Important (plan-mandated): -v prints "(devel)".
Ruling R23: fix in Task 7 — `var version string` in main, set by `-ldflags "-X main.version=..."` (Task 8 wires it to the INC build version), fallback to ReadBuildInfo then "(devel)" — spec requires -v to print the version — cost if wrong: none.
Task 7: minors folded into fix: recovery line after get-failure run; non-MQ dial error prints NOT_AN_MQ_ERROR; one stderr line on first trace write failure.
Task 7: minor (deferred): Throttle drops pending repeat count on key change/Reset
Task 7: minor (deferred): runLoop always returns nil (dead stopped branch); journal never closed
Task 7: minor (deferred): trace fd not reopened if netFLEX rotates /usr/cnc/trace files (unverified whether it does)
Ruling R24: Task 8 stamps main.version from the first include/version.h found on VPATH (non-TEMPLATE OVINC_VERSION line, strip '@(#)SOFTWARE VERSION: ' → e.g. '55.0.13 10/19/2026 13:47:32'); if none found, 'unknown' — matches how the rest of INC reports versions — cost if wrong: -v string format.
Task 7: fix round 1/5 (4 addressed, 0 open; commits a046f45..7e44da0)
Task 7: minor (deferred): local `version` flag var shadows package-level `version` in run()
Task 7: complete (commits be67ca2..7e44da0, review clean)
Task 8: dispatched (BASE 7e44da0)
Task 8: concern resolved — libmqm_r is in the MQ redist client lib64; mqconn sets MQCNO_CLIENT_BINDING so it connects in client mode.
Task 8: minor (deferred): fetch_vpath_src `cp ... 2>/dev/null` hides real copy errors (route_oms doesn't redirect stderr)
Task 8: complete (commits 7e44da0..f19b18d, review clean)
Task 9: dispatched (BASE f19b18d)
Ruling R25: accept T9 re-anchoring — plan's stop anchor (~1930) was in the boot path; stop placed in shutdown branch at boot_nms:3058 (after confirmation, before kill -SYS); start at boot_nms:2941; options 10 (MD toggle) and 2 (reboot) get no call; unit ships via install_cnc's existing *.service loop; data/mqorderd added to DIRLIST — plan line numbers were approximate, spec intent (start after app up, stop before app down) governs — cost if wrong: service not started/stopped on some boot_nms menu path.
Task 9: review — Needs fixes. I1 boot_nms option 10 `systemctl | grep netFLEX | grep running` now matches mqorderd units → MD toggle misreports.
Task 9: minors folded into fix: stop all instances in one `systemctl stop u1 u2...` (parallel); example YAML comment: printer name must be plain [A-Za-z0-9_-]; start_mqorderd skips yaml names with other chars (with a logged warning).
Task 9: minor (deferred): copy_cnc `cp -r $OCNC/data/*` overwrites shipped unit/example with previous release's on upgrade (pre-existing, affects netFLEX-md.service too) — follow-up note
Task 9: minor (deferred): example installed from cwd not via vfind (consistent with *.service loop)
Task 9: minor (deferred): RHEL7 systemd 219 list-units glob/--plain untested (case filter makes ignored pattern harmless)
Task 9: fix round 1/5 (3 addressed, 0 open; commits fc7d130..c8a350d)
Task 9: complete (commits f19b18d..c8a350d, review clean)
Task 10: deferred to user per R5 (needs a live QM)
Final review: dispatched (opus, 938608f..HEAD excl. vendor)
Final review: With fixes — C1 unit lacks KillMode=mixed (SIGTERM hits orderprint); I2 keepjunk docs backwards + R14 blocks lterm multi-page OCY accumulation; I3 journal keyed by MsgId only; I4 orderprint env under systemd; I5 poison guard drains queue on environment failures; I6 --check MQ-only; I7 2035 retried every 60s (account lockout); I8 build hosts Go toolchain.
Ruling R26 (C1): KillMode=mixed + comment.
Ruling R27 (I2): fix keepjunk doc strings; with keepjunk=true, allow re-running orderprint on a left-behind `ocy.*` path (lterm OCY accumulation) ONLY if orderprint.c's OCY branch is verified to have no cac/DB side effects before its exit; otherwise keep R14 and report — cost if wrong: duplicate OCY processing vs OCY reports stuck in failed_dir.
Ruling R28 (I3): journal stores sha256 of message data; resume only when MsgId AND hash match — cost if wrong: none.
Ruling R29 (I4): add Environment=LOGNAME=cmm (lterm sets it); full env diff vs lterm is a Task 10 / go-live item for the user.
Ruling R30 (I5): hold mode — after pre-handover backouts on 2 distinct MsgIds in a row, suspend the poison guard, keep backing out with the 60s pause, log level 0 "holding: environment problem"; leave hold after any Committed — spec's poison guard is for bad messages, not a broken box — cost if wrong: a genuinely poisonous pair of messages blocks the queue until fixed.
Ruling R31 (I6): --check also verifies dirs writable, orderprint executable, trace dir writable.
Ruling R32 (I7): reason 2035 on connect with mq.user set → 15-minute backoff, traced.
Ruling R33 (I8): build-host toolchain is a user confirmation item, not code; add a loud failure message in the Makefile if /opt/devtools/bin/go is missing.
Final fix wave also includes minors: O_EXCL for ckt/ocy + seq bump on EEXIST; KeepFailed fallback to /usr/cnc/orders/printer/<printer>; reset reconnect backoff only after a Get succeeds; trace 2119/2120 get warnings; printer name validated in config; example YAML: separate orders.dir for capture_only + which copy to re-feed; pages O(n²) fix.
Final review minors declined (deferred): shutdown during dial not interruptible; no After=netFLEX.service; orderprint stdout/stderr not captured; exec-fail+commit-fail double failure.
Final fix wave: commits c8a350d..407c416 (DONE_WITH_CONCERNS)
Ruling R34: accept the conditional OCY re-run gate (keepjunk on, ocy.* path, last exit 1-127, file lacks " ORD "+PG C/W) as the faithful reading of R27 — orderprint.c re-appends to cac only when getpinfo finds order info — cost if wrong: a multi-page OCY report that mentions ORD and PG C goes to failed_dir.
Ruling R35: pre-hash journals treated as stale is acceptable — mqorderd has never shipped, so no old journals exist in the field — cost if wrong: none for first release.
Note: `nmake test` previously exited 0 on failures (.DONTCARE); fixed with `test : .CLEAR`. Earlier per-task test evidence came from direct `go test`, not nmake, so it stands.
Final fix wave: re-review all 15 addressed, no new Critical/Important (commits c8a350d..407c416)
Final: deferred minor: hold mode never poisons a genuinely bad head message while holding (accepted cost of R30)
Final: deferred minor: intermediate OCY runs leave level-0 trace + failed_dir copy (noise)
Final: deferred minor: R28 sum is post-MQGMO_CONVERT; a converted-then-unconverted redelivery would mismatch (very unlikely)
Final review clean — workspace archived to WorkNotes
