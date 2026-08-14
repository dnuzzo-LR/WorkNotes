# MD rdr/wtr ZMQ Connection-Monitoring Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** In ZMQ mode, make the MD FEP↔BEP reader/writer processes treat a recv/send timeout as an observability + shared-memory status event (warn + `set_bep_socket_stat DOWN/UP`) and let ZMQ own reconnection — instead of killing writers, ssh-ing `md_lostconn`, exiting, and respawning.

**Architecture:** Each change is guarded by `if (MdUseZmq == 1)` so the legacy TCP path is untouched. Readers gain a per-process `link_down` edge flag: on recv timeout they warn once + mark the socket DOWN in `BepSocketStatus` and keep looping (the PULL stays bound; ZMQ reconnects the peer's PUSH); on the next successful recv they mark UP + log recovery. Writers stop exiting on send failure and stop acting on the `.lostconn` flag file in ZMQ mode.

**Tech Stack:** C/C++ (gcc 4.8.5 baseline), Lucent nmake build, ZeroMQ PUSH/PULL via `l_md_sock.c` helpers, SysV shm status via `cnc/utility/src/atch_bep.c` (`set_bep_socket_stat` / `set_bep_socket_stat_idx`).

**Spec:** `~/WorkNotes/specs/2026-08-14-md-zmq-connection-monitoring-design.md`

---

## Scope / File Structure

Eight files under `cnc/md/src`, no new files. Shared helper `l_md_sock.c` is **not** modified.

| File | Prefix | Timeout site today | Status call used |
|------|--------|--------------------|------------------|
| `l_fep_bep_msg_rdr.cpp` | `fbmr_` | inline ssh+exit in loop (`:423`) | `set_bep_socket_stat_idx(MsgRdrIdx,…,RDR_DATA,Port)` |
| `l_bep_fep_msg_rdr.cpp` | `bfmr_` | `break` in loop (`:434`) → main returns | `set_bep_socket_stat(MateNum,Port,…,MSG_TYPE,RDR_DATA)` |
| `l_fep_bep_frm_rdr.cpp` | `fbfr_` | inline ssh+exit in loop (`:384`) | `set_bep_socket_stat_idx(FrmRdrIdx,…,RDR_DATA,Port)` |
| `l_bep_fep_frm_rdr.cpp` | `bffr_` | inline log+exit in loop (`:388`) | `set_bep_socket_stat(mate_num,Port,…,FRM_TYPE,RDR_DATA)` |
| `l_fep_bep_msg_wtr.cpp` | `fbmw_` | `md_zmqsend`→`fbmw_exit(1)` (`:454`), `.lostconn`→exit (`:445`) | n/a (writer) |
| `l_bep_fep_msg_wtr.cpp` | `bfmw_` | send-fail exit (`:421`,`:719`), `.lostconn` (`:413`) | n/a |
| `l_fep_bep_frm_wtr.cpp` | `fbfw_` | send-fail exit (`:556`,`:595`,`:683`) | n/a |
| `l_bep_fep_frm_wtr.cpp` | `bffw_` | send-fail exit (`:513`,`:1015`), `.lostconn`→exit (`:402`) | n/a |

## Conventions used in every task

**Guard:** all new logic sits inside `if (1 == MdUseZmq) { … }`. `MdUseZmq` is a global `int32_t` already set in each `main()` (`if (1 == nfmd) MdUseZmq=1;`).

**Warn (down edge):** `TRACE(0, …)` (always logged regardless of debug level) plus `MD_LOG(ProcName, MD_ERR, …)`. The operator-visible signal is the shm status via `dacstat`; no new alarm API is introduced.

**Recovery (up edge):** `TRACE(0, …)` + `MD_LOG(ProcName, MD_INFO, …)`.

**Edge flag:** a file-scope `static int LinkDown = 0;` added near the other file-scope globals in each reader.

## Testing approach

These are long-running daemons with **no unit-test harness** in `cnc/md/src`. Each code task is verified by: (1) a clean `nmake` build, and (2) a scripted runtime check after install. The runtime check is one shared task (Task 9) run once at the end, plus a per-task build. Do **not** invent a unit-test framework.

Build prerequisite (per project rules): `BASE` = repo root and `VPATH[0]` = `$BASE` must be set, or the build pulls wrong headers. Verify before building:
```bash
test "$BASE" = "$(git -C /home/dan/Git/netflex rev-parse --show-toplevel)" && echo BASE_OK || echo BASE_BAD
```

---

### Task 1: `l_fep_bep_msg_rdr.cpp` (canonical reader)

**Files:**
- Modify: `cnc/md/src/l_fep_bep_msg_rdr.cpp` (add `LinkDown` flag; loop `:416`-`:438`)

- [ ] **Step 1: Add the edge flag**

Near the other file-scope globals (top of file, after the existing `char ServiceName[20];` at line ~74), add:

```c
static int LinkDown = 0;   /* ZMQ mode: 1 while inbound link is silent */
```

- [ ] **Step 2: Replace the recv-timeout reaction**

Current (`l_fep_bep_msg_rdr.cpp:423`-`:438`):

```c
		if (!(size = fbmr_recv_data(nfd, (char *)sptr, sizeof(BEP_FEP_SOCK_MSG), mdPingFreq)) || -1 == size) {

			fbmr_bulk_links_down(mate_num);

			log_msg("Lost Connection with Remote Machine. Restarting.", STATUS_MSG, ProcName, MD_RDR_PROC, 0);
			TRACE(FL, "Lost Connection with Remote Machine. fbmr_recv_data FAILED.\n");

			char rbuf[1000];
			sprintf(rbuf,"/usr/cnc/bin/scmd ssh %s -o BatchMode=yes -- /usr/cnc/bin/md_lostconn fep_bep_msg_rdr %d %d 2>&1",RemoteHost, MachNum,MateNum);
			TRACE(0,"[%s]\n",rbuf);
			system(rbuf);

			MD_LOG(ProcName, MD_ERR, "Lost Connection. fbmr_recv_data FAILED.\n");

			fbmr_exit(15);
		}
```

Replace with:

```c
		if (!(size = fbmr_recv_data(nfd, (char *)sptr, sizeof(BEP_FEP_SOCK_MSG), mdPingFreq)) || -1 == size) {

			if (1 == MdUseZmq) {
				/* ZMQ owns reconnect: warn + mark link down, keep waiting. */
				if (!LinkDown) {
					LinkDown = 1;
					TRACE(0, "#WARN: no data from %s in %ds; awaiting ZMQ reconnect.\n", RemoteHost, mdPingFreq);
					MD_LOG(ProcName, MD_ERR, "No data from %s in %ds; awaiting ZMQ reconnect.\n", RemoteHost, mdPingFreq);
					set_bep_socket_stat_idx(MsgRdrIdx, BEP_SOCKET_DOWN, RDR_DATA, Port);
				}
				continue;
			}

			fbmr_bulk_links_down(mate_num);

			log_msg("Lost Connection with Remote Machine. Restarting.", STATUS_MSG, ProcName, MD_RDR_PROC, 0);
			TRACE(FL, "Lost Connection with Remote Machine. fbmr_recv_data FAILED.\n");

			char rbuf[1000];
			sprintf(rbuf,"/usr/cnc/bin/scmd ssh %s -o BatchMode=yes -- /usr/cnc/bin/md_lostconn fep_bep_msg_rdr %d %d 2>&1",RemoteHost, MachNum,MateNum);
			TRACE(0,"[%s]\n",rbuf);
			system(rbuf);

			MD_LOG(ProcName, MD_ERR, "Lost Connection. fbmr_recv_data FAILED.\n");

			fbmr_exit(15);
		}
```

- [ ] **Step 3: Add the recovery (up edge) immediately after a good recv**

Immediately after the closing `}` of the block from Step 2 (i.e. once a message has been received), insert:

```c
		if (1 == MdUseZmq && LinkDown) {
			LinkDown = 0;
			TRACE(0, "#INFO: data resumed from %s; link back up.\n", RemoteHost);
			MD_LOG(ProcName, MD_INFO, "Data resumed from %s; link back up.\n", RemoteHost);
			set_bep_socket_stat_idx(MsgRdrIdx, BEP_SOCKET_UP, RDR_DATA, Port);
		}
```

- [ ] **Step 4: Build**

Run (from `cnc/md/src`, with `BASE`/`VPATH` set): `nmake l_fep_bep_msg_rdr`
Expected: compiles and links with no errors.

- [ ] **Step 5: Commit**

```bash
git add cnc/md/src/l_fep_bep_msg_rdr.cpp
git commit -m "md: fep_bep_msg_rdr ZMQ mode warns+marks-down instead of tearing down"
```

---

### Task 2: `l_bep_fep_msg_rdr.cpp`

This reader `break`s out of the main loop on timeout and `main()` returns (no ssh/exit call in the loop; the `md_lostconn` ssh lives in the TCP branch of `bfmr_recv_data`, which ZMQ never reaches). We replace the `break` with warn+down+continue.

**Files:**
- Modify: `cnc/md/src/l_bep_fep_msg_rdr.cpp` (add flag; loop `:434`-`:439`; recovery after `:441`)

- [ ] **Step 1: Add the edge flag** near the file-scope globals (after `char ServiceName[20];`):

```c
static int LinkDown = 0;   /* ZMQ mode: 1 while inbound link is silent */
```

- [ ] **Step 2: Replace the timeout `break`**

Current (`l_bep_fep_msg_rdr.cpp:434`-`:439`):

```c
		if(!( size = bfmr_recv_data( nfd, (char*)sptr, sizeof( FEP_BEP_SOCK_MSG ), mdPingFreq )) || FAILURE == size )
		{
			TRACE( D1, "**** NO MORE DATA *****\n" );

			break;
		}
```

Replace with:

```c
		if(!( size = bfmr_recv_data( nfd, (char*)sptr, sizeof( FEP_BEP_SOCK_MSG ), mdPingFreq )) || FAILURE == size )
		{
			TRACE( D1, "**** NO MORE DATA *****\n" );

			if (1 == MdUseZmq) {
				if (!LinkDown) {
					LinkDown = 1;
					TRACE(0, "#WARN: no data from %s in %ds; awaiting ZMQ reconnect.\n", RemoteHost, mdPingFreq);
					MD_LOG(ProcName, MD_ERR, "No data from %s in %ds; awaiting ZMQ reconnect.\n", RemoteHost, mdPingFreq);
					set_bep_socket_stat(MateNum, Port, BEP_SOCKET_DOWN, MSG_TYPE, RDR_DATA);
				}
				continue;
			}

			break;
		}
```

- [ ] **Step 3: Add recovery after a good recv**

Immediately after the block above (before `add_sock_msg_cnt_idx( MsgRdrIdx, RDR_DATA );` at `:441`), insert:

```c
		if (1 == MdUseZmq && LinkDown) {
			LinkDown = 0;
			TRACE(0, "#INFO: data resumed from %s; link back up.\n", RemoteHost);
			MD_LOG(ProcName, MD_INFO, "Data resumed from %s; link back up.\n", RemoteHost);
			set_bep_socket_stat(MateNum, Port, BEP_SOCKET_UP, MSG_TYPE, RDR_DATA);
		}
```

- [ ] **Step 4: Build** — Run: `nmake l_bep_fep_msg_rdr` — Expected: clean build.

- [ ] **Step 5: Commit**

```bash
git add cnc/md/src/l_bep_fep_msg_rdr.cpp
git commit -m "md: bep_fep_msg_rdr ZMQ mode warns+marks-down instead of exiting"
```

---

### Task 3: `l_fep_bep_frm_rdr.cpp`

**Files:**
- Modify: `cnc/md/src/l_fep_bep_frm_rdr.cpp` (add flag; loop `:384`-`:~395`; recovery after good recv)

- [ ] **Step 1: Add the edge flag** near file-scope globals:

```c
static int LinkDown = 0;   /* ZMQ mode: 1 while inbound link is silent */
```

- [ ] **Step 2: Replace the recv-timeout reaction**

Current (`l_fep_bep_frm_rdr.cpp:384`-`:395`):

```c
		if ((ret = fbfr_recv_data(nfd, &smsg, sizeof(BEP_FEP_FRM_SOCK_MSG), mdPingFreq)) == FAILURE) {

			sprintf(rbuf,"/usr/cnc/bin/scmd ssh %s -o BatchMode=yes -- /usr/cnc/bin/md_lostconn fep_bep_frm_rdr %d %d 2>&1",RemoteHost, MachNum,MateNum);
			TRACE(0,"[%s]\n",rbuf);
			system(rbuf);

			log_msg("Lost Connection with Remote Machine. Restarting.", STATUS_MSG, ProcName, MD_RDR_PROC, 0);
			TRACEF(D0, "Lost Connection. recv_data FAILED.\n");
			MD_LOG(ProcName, MD_ERR, "Lost Connection. recv_data FAILED.\n");

			fbfr_exit(1);
		}
```

Replace with (keep the existing declaration of `rbuf` above the block as-is):

```c
		if ((ret = fbfr_recv_data(nfd, &smsg, sizeof(BEP_FEP_FRM_SOCK_MSG), mdPingFreq)) == FAILURE) {

			if (1 == MdUseZmq) {
				if (!LinkDown) {
					LinkDown = 1;
					TRACE(0, "#WARN: no frm data from %s in %ds; awaiting ZMQ reconnect.\n", RemoteHost, mdPingFreq);
					MD_LOG(ProcName, MD_ERR, "No frm data from %s in %ds; awaiting ZMQ reconnect.\n", RemoteHost, mdPingFreq);
					set_bep_socket_stat_idx(FrmRdrIdx, BEP_SOCKET_DOWN, RDR_DATA, Port);
				}
				continue;
			}

			sprintf(rbuf,"/usr/cnc/bin/scmd ssh %s -o BatchMode=yes -- /usr/cnc/bin/md_lostconn fep_bep_frm_rdr %d %d 2>&1",RemoteHost, MachNum,MateNum);
			TRACE(0,"[%s]\n",rbuf);
			system(rbuf);

			log_msg("Lost Connection with Remote Machine. Restarting.", STATUS_MSG, ProcName, MD_RDR_PROC, 0);
			TRACEF(D0, "Lost Connection. recv_data FAILED.\n");
			MD_LOG(ProcName, MD_ERR, "Lost Connection. recv_data FAILED.\n");

			fbfr_exit(1);
		}
```

- [ ] **Step 3: Add recovery after the block** (once a frame has been received):

```c
		if (1 == MdUseZmq && LinkDown) {
			LinkDown = 0;
			TRACE(0, "#INFO: frm data resumed from %s; link back up.\n", RemoteHost);
			MD_LOG(ProcName, MD_INFO, "Frm data resumed from %s; link back up.\n", RemoteHost);
			set_bep_socket_stat_idx(FrmRdrIdx, BEP_SOCKET_UP, RDR_DATA, Port);
		}
```

- [ ] **Step 4: Build** — Run: `nmake l_fep_bep_frm_rdr` — Expected: clean build.

- [ ] **Step 5: Commit**

```bash
git add cnc/md/src/l_fep_bep_frm_rdr.cpp
git commit -m "md: fep_bep_frm_rdr ZMQ mode warns+marks-down instead of tearing down"
```

---

### Task 4: `l_bep_fep_frm_rdr.cpp`

**Files:**
- Modify: `cnc/md/src/l_bep_fep_frm_rdr.cpp` (add flag; loop `:388`-`:395`; recovery after good recv)

- [ ] **Step 1: Add the edge flag** near file-scope globals:

```c
static int LinkDown = 0;   /* ZMQ mode: 1 while inbound link is silent */
```

- [ ] **Step 2: Replace the recv-timeout reaction**

Current (`l_bep_fep_frm_rdr.cpp:388`-`:395`):

```c
		if (bffr_recv_data(nfd, (char *)sptr, sizeof(FEP_FEP_SOCK_MSG), mdPingFreq) == FAILURE) {

			log_msg("Lost Connection with Remote FEP Restarting.", STATUS_MSG, ProcName, MD_RDR_PROC, 0);
			TRACE(D0, "Lost Connection with Remote FEP Machine. bffr_recv_data FAILED.\n");
			MD_LOG(ProcName, MD_ERR,
				"Lost Connection with Remote FEP Machine. bffr_recv_data FAILED.\n");
			bffr_exit(1);
		}
```

Replace with:

```c
		if (bffr_recv_data(nfd, (char *)sptr, sizeof(FEP_FEP_SOCK_MSG), mdPingFreq) == FAILURE) {

			if (1 == MdUseZmq) {
				if (!LinkDown) {
					LinkDown = 1;
					TRACE(0, "#WARN: no frm data from %s in %ds; awaiting ZMQ reconnect.\n", RemoteHost, mdPingFreq);
					MD_LOG(ProcName, MD_ERR, "No frm data from %s in %ds; awaiting ZMQ reconnect.\n", RemoteHost, mdPingFreq);
					set_bep_socket_stat(mate_num, Port, BEP_SOCKET_DOWN, FRM_TYPE, RDR_DATA);
				}
				continue;
			}

			log_msg("Lost Connection with Remote FEP Restarting.", STATUS_MSG, ProcName, MD_RDR_PROC, 0);
			TRACE(D0, "Lost Connection with Remote FEP Machine. bffr_recv_data FAILED.\n");
			MD_LOG(ProcName, MD_ERR,
				"Lost Connection with Remote FEP Machine. bffr_recv_data FAILED.\n");
			bffr_exit(1);
		}
```

> Note: confirm `mate_num` is in scope at this point (it is used by the existing `set_bep_socket_stat(mate_num, …)` at `:377`). If the loop is in a different function than `:377`, use the same machine argument that the nearby `add_sock` / status calls in this loop use.

- [ ] **Step 3: Add recovery after the block:**

```c
		if (1 == MdUseZmq && LinkDown) {
			LinkDown = 0;
			TRACE(0, "#INFO: frm data resumed from %s; link back up.\n", RemoteHost);
			MD_LOG(ProcName, MD_INFO, "Frm data resumed from %s; link back up.\n", RemoteHost);
			set_bep_socket_stat(mate_num, Port, BEP_SOCKET_UP, FRM_TYPE, RDR_DATA);
		}
```

- [ ] **Step 4: Build** — Run: `nmake l_bep_fep_frm_rdr` — Expected: clean build.

- [ ] **Step 5: Commit**

```bash
git add cnc/md/src/l_bep_fep_frm_rdr.cpp
git commit -m "md: bep_fep_frm_rdr ZMQ mode warns+marks-down instead of exiting"
```

---

### Task 5: `l_fep_bep_msg_wtr.cpp` (canonical writer)

Two sites: the send-failure exit and the `.lostconn` self-exit, both in the ping/not-active path.

**Files:**
- Modify: `cnc/md/src/l_fep_bep_msg_wtr.cpp` (`:445`-`:449`, `:454`-`:460`)

- [ ] **Step 1: Neutralize the `.lostconn` self-exit in ZMQ mode**

Current (`l_fep_bep_msg_wtr.cpp:445`-`:450`):

```c
						if (0==access(LostConnectionFile,R_OK)) {
							TRACE(0,"#ERROR: Remote Lost Connection [%s]\n",LostConnectionFile);
							MD_LOG( ProcName, MD_ERR, "%s LostConnection.\n", LostConnectionFile );
							unlink(LostConnectionFile);
							fbmw_exit( 1 );
						}
```

Replace with:

```c
						if (1 != MdUseZmq && 0==access(LostConnectionFile,R_OK)) {
							TRACE(0,"#ERROR: Remote Lost Connection [%s]\n",LostConnectionFile);
							MD_LOG( ProcName, MD_ERR, "%s LostConnection.\n", LostConnectionFile );
							unlink(LostConnectionFile);
							fbmw_exit( 1 );
						}
```

- [ ] **Step 2: Stop exiting on send failure in ZMQ mode**

Current (`l_fep_bep_msg_wtr.cpp:453`-`:460`):

```c
						if (1==MdUseZmq) {
							if (-1 == md_zmqsend(ZmqSocket,(char *)pptr, pptr->ssize)) {
								TRACE(D0, "Socket is Dead.\n");
								MD_LOG(ProcName, MD_ERR, "Socket is Dead.\n");
								md_zmqclose(ZmqSocket);
								ZmqSocket=NULL;
								fbmw_exit(1);
							}

						} else {
```

Replace with:

```c
						if (1==MdUseZmq) {
							if (-1 == md_zmqsend(ZmqSocket,(char *)pptr, pptr->ssize)) {
								/* ZMQ owns reconnect: warn and keep the socket; retry next ping. */
								TRACE(0, "#WARN: ping send to %s deferred; awaiting ZMQ reconnect.\n", HostName);
								MD_LOG(ProcName, MD_ERR, "Ping send deferred; awaiting ZMQ reconnect.\n");
							}

						} else {
```

> `HostName` is the connect target already logged at startup (`ServiceName`/`IPv4` block). If `HostName` is not in scope here, use the literal `"remote"` — the message is informational.

- [ ] **Step 3: Build** — Run: `nmake l_fep_bep_msg_wtr` — Expected: clean build.

- [ ] **Step 4: Commit**

```bash
git add cnc/md/src/l_fep_bep_msg_wtr.cpp
git commit -m "md: fep_bep_msg_wtr ZMQ mode keeps socket on send-fail, ignores .lostconn"
```

---

### Task 6: `l_bep_fep_msg_wtr.cpp`

Send happens through `bfmw_md_send` inside `bfmw_sendmsg` and the ping path; the send-fail exit is in `bfmw_sendmsg` (`:502` region) and the ping send at `:719`. The `.lostconn` check is at `:413`.

**Files:**
- Modify: `cnc/md/src/l_bep_fep_msg_wtr.cpp` (`:413`, `:500`-`:~505`, `:719`-`:~723`)

- [ ] **Step 1: Read the three sites** to capture exact current text:

Run: `sed -n '408,418p;495,510p;715,726p' cnc/md/src/l_bep_fep_msg_wtr.cpp`

- [ ] **Step 2: Neutralize the `.lostconn` self-exit** — wrap its `access(LostConnectionFile,R_OK)` condition (at `:413`) with `1 != MdUseZmq &&`, exactly as in Task 5 Step 1 (same pattern, `bfmw_exit` instead of `fbmw_exit`).

- [ ] **Step 3: Stop exiting on send failure** — at the `bfmw_sendmsg` failure branch (the `if (bfmw_md_send(...) <= 0)` at `:502`) and the ping-send failure at `:719`, wrap the `bfmw_exit(1)` / `bfmw_ender(1)` call so that in ZMQ mode it warns and returns/continues instead of exiting:

```c
		if (1 == MdUseZmq) {
			TRACE(0, "#WARN: send deferred; awaiting ZMQ reconnect.\n");
			MD_LOG(ProcName, MD_ERR, "Send deferred; awaiting ZMQ reconnect.\n");
			return;   /* or `continue;` if inside the ping loop at :719 */
		}
```

Place this guard immediately before the existing `TRACE("Socket is Dead")`/`bfmw_exit` teardown at each site, so the legacy path is unchanged when `MdUseZmq != 1`. Use `return;` inside `bfmw_sendmsg` (void function) and `continue;` inside the main ping loop.

- [ ] **Step 4: Build** — Run: `nmake l_bep_fep_msg_wtr` — Expected: clean build.

- [ ] **Step 5: Commit**

```bash
git add cnc/md/src/l_bep_fep_msg_wtr.cpp
git commit -m "md: bep_fep_msg_wtr ZMQ mode keeps socket on send-fail, ignores .lostconn"
```

---

### Task 7: `l_fep_bep_frm_wtr.cpp`

Three identical send-fail blocks at `:556`, `:595`, `:683`, plus the `.lostconn` check at `:236` (startup) — note this writer's `.lostconn` is only cleared at startup (`:236`), there is no in-loop `.lostconn` self-exit, so only the send-fail blocks need changing.

**Files:**
- Modify: `cnc/md/src/l_fep_bep_frm_wtr.cpp` (`:556`-`:561`, `:595`-`:600`, `:683`-`:~688`)

- [ ] **Step 1: Read the three blocks** to confirm they are byte-identical:

Run: `sed -n '554,562p;593,601p;681,689p' cnc/md/src/l_fep_bep_frm_wtr.cpp`
Expected: each is the same block:
```c
						if( fbfw_md_send( Sd, (char*)sptr, sizeof( FEP_FEP_SOCK_MSG ), sptr->type ) == FAILURE )
						{
							TRACE( D0, "Socket is Dead.\n" );
							MD_LOG( ProcName, MD_INFO, "Socket is Dead.\n" );
							…
							fbfw_exit( 1 );
						}
```

- [ ] **Step 2: At each of the three sites**, insert a ZMQ-mode guard as the first statement inside the `if(... == FAILURE)` block, before the existing `TRACE`/`fbfw_exit`:

```c
							if (1 == MdUseZmq) {
								TRACE(0, "#WARN: frm send deferred; awaiting ZMQ reconnect.\n");
								MD_LOG(ProcName, MD_ERR, "Frm send deferred; awaiting ZMQ reconnect.\n");
								continue;   /* stay in the send loop; do not exit */
							}
```

Apply the identical insertion at all three line ranges (`:556`, `:595`, `:683`). Confirm `continue` targets the intended enclosing loop at each site (each is inside the per-frame send loop); if a site is not inside a loop, use `break` out of the send attempt instead of `continue`.

- [ ] **Step 3: Build** — Run: `nmake l_fep_bep_frm_wtr` — Expected: clean build.

- [ ] **Step 4: Commit**

```bash
git add cnc/md/src/l_fep_bep_frm_wtr.cpp
git commit -m "md: fep_bep_frm_wtr ZMQ mode keeps socket on send-fail"
```

---

### Task 8: `l_bep_fep_frm_wtr.cpp`

Send-fail blocks at `:513` and `:1015`; in-loop `.lostconn` self-exit at `:402`-`:406`.

**Files:**
- Modify: `cnc/md/src/l_bep_fep_frm_wtr.cpp` (`:402`-`:406`, `:513`-`:518`, `:1015`-`:~1020`)

- [ ] **Step 1: Read the three sites:**

Run: `sed -n '400,408p;511,519p;1013,1021p' cnc/md/src/l_bep_fep_frm_wtr.cpp`

- [ ] **Step 2: Neutralize the in-loop `.lostconn` self-exit** at `:402` — wrap its `access(LostConnectionFile,R_OK)` condition with `1 != MdUseZmq &&`, same pattern as Task 5 Step 1 (with `bffw_exit`).

- [ ] **Step 3: Stop exiting on send failure** at `:513` and `:1015` — insert as the first statement inside each `if(... == FAILURE)` block, before the existing `TRACE`/`bffw_exit`:

```c
						if (1 == MdUseZmq) {
							TRACE(0, "#WARN: frm send deferred; awaiting ZMQ reconnect.\n");
							MD_LOG(ProcName, MD_ERR, "Frm send deferred; awaiting ZMQ reconnect.\n");
							continue;   /* stay in the send loop; do not exit */
						}
```

Confirm `continue` targets the enclosing send loop at each site; the `:1015` site is in a separate function — if it is not inside a loop, use `return;`/`break;` matching that function's signature instead.

- [ ] **Step 4: Build** — Run: `nmake l_bep_fep_frm_wtr` — Expected: clean build.

- [ ] **Step 5: Commit**

```bash
git add cnc/md/src/l_bep_fep_frm_wtr.cpp
git commit -m "md: bep_fep_frm_wtr ZMQ mode keeps socket on send-fail, ignores .lostconn"
```

---

### Task 9: Build all + runtime verification

**Files:** none (build + install + observe)

- [ ] **Step 1: Full component build**

Run (from `cnc/md/src`, `BASE`/`VPATH` set): `nmake`
Expected: all 8 binaries compile and link cleanly.

- [ ] **Step 2: Install the 8 binaries and restart MD** on a test box (per the site's install flow; these live in `/usr/cnc/bin`). A stale binary will make the trace contradict the source, so confirm the new build is what's running (check the `SOFTWARE VERSION` / build stamp in the process trace header).

- [ ] **Step 3: Induce a transient outage** — on the peer, stop the corresponding writer (e.g. `bep_fep_msg_wtr`) for ~90s, then restart it.

Expected in the reader trace (`/usr/cnc/trace/fep_bep_msg_rdr.*`):
- one `#WARN: no data from … awaiting ZMQ reconnect` on the down edge,
- **no** `Killing Mate`, **no** `md_lostconn` ssh line, **no** `EXITING`,
- `dacstat` shows that reader's socket `DOWN` during the gap,
- on peer return: `#INFO: data resumed … link back up`, `dacstat` shows `UP`.

- [ ] **Step 4: Confirm no restart storm** — the reader/writer PIDs are unchanged across the outage (`ps -ef | grep -E 'fep_bep_msg|bep_fep_msg'`), i.e. no respawn.

- [ ] **Step 5: Regression — legacy TCP path unchanged** — on a box configured with `MdUseZmq != 1` (or a unit with `nfmd != 1`), confirm a peer outage still produces the old behavior (`Lost Connection` → `md_lostconn` → exit/respawn). Code inspection is acceptable if no TCP-mode box is available: verify every new block is inside `1 == MdUseZmq` (or the `.lostconn` guards are `1 != MdUseZmq &&`).

- [ ] **Step 6: Commit any build-file touch-ups** (only if the build required a Makefile change):

```bash
git add cnc/md/src/Makefile
git commit -m "md: build touch-ups for ZMQ connection-monitoring change"
```

---

## Self-Review

**Spec coverage:**
- "warn + set DOWN on timeout, UP on resume, no teardown, ZMQ only" → Tasks 1-4 (readers). ✅
- "writer stops exiting on send-fail, ignores .lostconn in ZMQ mode" → Tasks 5-8 (writers). ✅
- "all 8 files, guarded by MdUseZmq==1, TCP path unchanged" → every task guards on `MdUseZmq`; Task 9 Step 5 regresses TCP. ✅
- "edge-based warnings" → `LinkDown` flag in each reader (Tasks 1-4). ✅
- Deferred (per spec): shm re-init bug, GR failover — intentionally not in scope. ✅

**Placeholder scan:** Tasks 6, 7, 8 include a `sed` read step because those files have multiple/near-duplicate send sites whose surrounding lines were not all quoted verbatim here; the read step yields the exact text before editing. The transformation and inserted code are fully specified. No `TBD`/`add error handling`/`similar to`.

**Type/name consistency:** flag named `LinkDown` in all readers; guard `1 == MdUseZmq` everywhere; status calls match each file's startup call (`set_bep_socket_stat_idx` for fep_bep, `set_bep_socket_stat` for bep_fep); exit-prefixes match per file (`fbmr_/bfmr_/fbfr_/bffr_/fbmw_/bfmw_/fbfw_/bffw_`).

**Open follow-ups (not blocking):** optional writer-side status mirroring; optional periodic "still down" reminder tunable; the separate `BEP_KEY`/`MUX_KEY` shm re-init and GR failover-detection defects.
