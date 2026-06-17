# Telmar 1633 SX – Telnet Headless Connection Troubleshooting

**Topic:** Diagnosing and resolving telnet connectivity issues between a headless application and a Telmar 1633 SX network element using TL1 commands.

---

## The Problem

A headless application communicates with a Telmar 1633 SX via telnet. When connecting manually with an interactive login shell, the connection works fine and the `ACT-USER` TL1 command is acknowledged. However, when the headless application attempts to connect:

- The `ACT-USER` command is never acknowledged by the device
- The equipment locks up and requires a restart

---

## Root Cause Analysis

### Why This Happens

Telnet wasn't designed for headless use. The protocol performs **option negotiation** at connection time — things like terminal type, echo mode, and line mode vs character mode. An interactive shell client responds to these negotiations automatically. A headless client typically doesn't, and the Telmar 1633 SX (like most telecom/network equipment) likely **stalls or locks waiting for negotiation to complete** before it processes commands.

The `ACT-USER` command being ignored is a strong signal the device is still mid-negotiation when the application fires the command.

### Most Likely Root Causes

1. **Telnet Option Negotiation Not Handled** — The device sends `IAC WILL ECHO`, `IAC DO TERMINAL-TYPE`, etc. The headless app ignores these. The device waits, then the app sends `ACT-USER` into a connection that isn't ready.

2. **No Delay / Timing Issue** — Interactive sessions have human latency, so the device finishes its banner and negotiation before a user types. Headless apps send commands immediately, racing the device's ready state.

3. **Terminal Type Not Declared** — The device may require a terminal type declaration (`IAC SB TERMINAL-TYPE IS VT100 IAC SE`) before it accepts login commands.

4. **Line Ending Mismatch** — Telnet spec requires `\r\n`. If the app sends only `\n` or only `\r`, some equipment ignores or corrupts the command.

5. **Login Banner / Prompt Not Consumed** — If the device sends a welcome banner and the app doesn't read/drain it before sending `ACT-USER`, the receive buffer fills, the device stalls, and it can lock up.

---

## Replicating the Issue

Use `ncat` to simulate the headless app's raw behavior:

```bash
# Raw TCP connection (no telnet negotiation) - simulates broken client
ncat <device-ip> 23
# Then immediately type ACT-USER without waiting
```

Or with a Python script:

```python
import socket, time

s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
s.connect(('DEVICE_IP', 23))

# Don't handle ANY negotiation - simulates broken headless client
time.sleep(0.5)  # try with 0, 0.5, 2 seconds to isolate timing
s.sendall(b'ACT-USER:uid,ctag,,pid;\r\n')

response = s.recv(4096)
print(repr(response))
```

Compare against a proper telnet negotiation client:

```python
import telnetlib, time

tn = telnetlib.Telnet('DEVICE_IP', 23, timeout=10)
tn.set_debuglevel(2)  # logs all IAC negotiation

banner = tn.read_until(b'<', timeout=5)  # read until TL1 prompt char
print("Banner:", banner)

tn.write(b'ACT-USER:uid,ctag,,pid;\r\n')
response = tn.read_until(b'COMPLD', timeout=5)
print("Response:", response)
tn.close()
```

> **Tip:** `set_debuglevel(2)` output will show exactly what the device is negotiating and whether your app is responding.

---

## The Key Discovery: Fork/Exec Telnet

The application uses **fork/exec of the system `telnet` binary** rather than a telnet library. This is the root cause.

### Why Fork/Exec Telnet Fails

When you fork/exec the system `telnet` binary, it detects whether it's running in an **interactive terminal (TTY) or not**:

```
Your App (no TTY)
    └── fork/exec telnet
            └── telnet detects: "I have no TTY"
                    └── changes behavior significantly
```

When `telnet` has **no TTY attached**, it:
- Disables interactive terminal negotiation
- May not properly respond to `DO TERMINAL-TYPE` / `NAWS` etc.
- Buffers I/O differently (fully buffered vs line buffered)
- Behaves almost identically to a raw socket — **defeating the whole point of using it**

### Proof — Test This

```bash
# Interactive (has TTY) - what works
telnet DEVICE_IP 23

# Non-interactive (no TTY) - what your app does
echo "" | telnet DEVICE_IP 23

# Pipe commands like a headless process would
printf "ACT-USER:uid,ctag,,pid;\r\n" | telnet DEVICE_IP 23
```

The piped version will likely get no response or lock up — **reproducing the exact bug**.

### The Deeper Issue — stdin/stdout Piping

When you fork/exec telnet and communicate via pipes:

```
Your App
  ├── writes to  → [pipe] → telnet stdin  → device
  └── reads from ← [pipe] ← telnet stdout ← device
```

**Pipes have no TTY**, so:
- `telnet` may buffer output and the app never sees the prompt
- `telnet` may not flush writes immediately
- The device's IAC negotiations may go unanswered because `telnet` is in a degraded mode
- **Race conditions** between the app writing and telnet being ready are very real

---

## Solutions

### Option 1 — Use a PTY (Pseudo-Terminal) *(Best Fix for Fork/Exec)*

A PTY fakes a real terminal, making `telnet` think it's interactive:

```python
import pty, os, subprocess, select, time

master_fd, slave_fd = pty.openpty()

proc = subprocess.Popen(
    ['telnet', 'DEVICE_IP', '23'],
    stdin=slave_fd,
    stdout=slave_fd,
    stderr=slave_fd,
    close_fds=True
)

os.close(slave_fd)

# Read banner/negotiation first
time.sleep(2)
banner = b''
while select.select([master_fd], [], [], 0.5)[0]:
    banner += os.read(master_fd, 1024)

print("Banner:", banner)

# NOW send the command
os.write(master_fd, b'ACT-USER:uid,ctag,,pid;\r\n')

# Read response
response = b''
while select.select([master_fd], [], [], 2)[0]:
    response += os.read(master_fd, 1024)

print("Response:", response)
```

Or use **`pexpect`** which wraps PTY handling:

```python
import pexpect

child = pexpect.spawn('telnet DEVICE_IP 23')
child.logfile = open('telnet_debug.log', 'wb')  # logs everything

child.expect(r'[Ll]ogin|>|;', timeout=10)  # wait for prompt
child.sendline('ACT-USER:uid,ctag,,pid;')
child.expect('COMPLD', timeout=10)
print(child.before)
```

---

### Option 2 — Use `expect` Directly as the Wrapper

Offload the whole interaction to `expect`, which was built for exactly this:

```tcl
#!/usr/bin/expect -f

set timeout 15
set device_ip [lindex $argv 0]

spawn telnet $device_ip 23

# Wait for and consume the login prompt
expect {
    timeout { puts "ERROR: No prompt received"; exit 1 }
    -re {[Ll]ogin|>\s*$|;\s*$} { }
}

send "ACT-USER:uid,ctag,,pid;\r"

expect {
    timeout { puts "ERROR: No COMPLD received"; exit 1 }
    "COMPLD" { puts "Login successful" }
    "DENY"   { puts "Login denied" }
}
```

Call it from the app:

```bash
./login.exp DEVICE_IP
```

---

### Option 3 — Switch to a Real Telnet Library *(Cleanest Long-Term Fix)*

Drop the fork/exec entirely and use a library in the app's native language that handles negotiation in-process:

| Language | Library |
|---|---|
| Python | `telnetlib` (built-in) or `telnetlib3` |
| Java | Apache Commons Net `TelnetClient` |
| Node.js | `telnet-client` npm package |
| .NET | `NetworkStream` with a telnet negotiation handler |

---

### Option 4 — Respond to IAC Manually *(If Staying With Raw Sockets)*

```python
import socket

IAC  = bytes([255])
DONT = bytes([254])
DO   = bytes([253])
WONT = bytes([252])
WILL = bytes([251])

def negotiate(sock, data):
    """Strip IAC sequences, respond with WONT/DONT to everything"""
    result = b''
    i = 0
    while i < len(data):
        if data[i:i+1] == IAC:
            cmd = data[i+1:i+2]
            if cmd in (DO, WILL):
                sock.sendall(IAC + WONT + data[i+2:i+3])
            i += 3
        else:
            result += data[i:i+1]
            i += 1
    return result
```

---

## Why the Equipment Locks Up

The device shouldn't lock up from a bad session alone. Most likely:

- The app connects and sends a malformed or premature `ACT-USER`
- The device enters a **partial auth state** and holds a session lock
- The session never cleanly closes (no `CANC-USER` or clean TCP FIN)
- The device has a **limited session table** and the zombie session holds a slot

**Always ensure the app sends a cleanup command and closes cleanly — even on error paths:**

```
CANC-USER:uid,ctag,,pid;   ← TL1 logout command
```

---

## Diagnostic Checklist

| Check | How |
|---|---|
| Is negotiation being handled? | Run `telnetlib` with `set_debuglevel(2)` |
| Is timing the issue? | Add `sleep(2)` before `ACT-USER` and test |
| Is line ending wrong? | Confirm sending `\r\n` not just `\n` |
| Is banner being drained? | Print everything received before sending command |
| What does the device expect? | Capture a working interactive session with Wireshark |

> A **Wireshark capture** of a working interactive session vs. the headless session side-by-side will definitively show where the two diverge. That's the fastest path to a root cause.

---

## Summary

| Problem | Cause |
|---|---|
| Negotiation not working | `telnet` binary in non-TTY mode behaves like a raw socket |
| Commands ignored | App sends before device is ready; no prompt detection |
| Device locks up | Session left open/zombie with no clean logout |

**Recommended fix:** Use `pexpect` (Python) or a native telnet library, wait for the device prompt before sending `ACT-USER`, and always send `CANC-USER` on exit.
