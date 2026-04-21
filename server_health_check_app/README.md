# Server First-Level Health Check

Cross-platform desktop application for Windows and Linux that automates first-level diagnostics when connected directly to a customer server at `172.16.50.254`.

## Stack
- Python 3.11+
- PySide6 (desktop GUI)
- Paramiko (SSH checks)
- psutil (adapter inspection)
- requests (HTTP/HTTPS checks)

## Features
- Cross-platform GUI for Windows and Linux
- Dependency-aware diagnostic flow
- Root-cause analysis for connectivity failures
- Parallel execution of steps 2-6 after connectivity passes
- SSH-based service, Docker, and log checks
- Professional status labels:
  - 🟢 Passed
  - 🔴 Failed
  - 🟡 Warning
  - ⏸ Blocked
  - 🔄 Running

## Dependency Logic
- **Step 1 (Network Connection Verification)** is the root dependency.
- If Step 1 fails, the app performs sub-diagnostics to find the real cause.
- Steps 2-6 are marked **Blocked**, not failed.
- Once Step 1 passes, steps 2-6 run independently and in parallel.
- A failure in any later step never blocks the others.

## Implemented Checks
1. Network Connection Verification
   - No Ethernet adapter found
   - Ethernet cable unplugged
   - Adapter disabled
   - No IPv4 assigned
   - Wrong subnet / wrong static IP for `172.16.50.x/24`
   - Ping failure
   - Duplicate IP suspicion / conflict hint
   - ICMP blocked warning
   - DNS not required reminder
2. SSH service check
   - `systemctl is-active fbs_modbus`
   - `systemctl is-active modbus_server`
3. Docker check
   - `systemctl is-active docker`
   - `docker ps`
4. Main Web UI check
   - `http://172.16.50.254`
5. Secure Port 5000 UI check
   - `https://172.16.50.254:5000`
6. Log download
   - Copies files from `/var/logs/` via SFTP into a local folder

## Assumptions
- Steps 2, 3, and 6 require SSH access to the server.
- You will provide either username/password or an SSH private key.
- The server allows SSH and SFTP.
- `ping` might be blocked even when the server is online, so the app distinguishes this as a **warning** when HTTP/SSH are reachable.

## Quick Start
```bash
python -m venv .venv
source .venv/bin/activate  # Linux
# .venv\Scripts\activate   # Windows PowerShell
pip install -r requirements.txt
python main.py
```

## Packaging
### Windows
```bash
pyinstaller --name ServerHealthCheck --windowed --onefile main.py
```

### Linux
```bash
pyinstaller --name server-health-check --windowed --onefile main.py
```

## Notes
- On Linux, reading some adapter details may work best when tools like `ip`, `ethtool`, and `nmcli` are available.
- On Windows, additional diagnosis is improved if `netsh` and PowerShell are available.
- The app degrades gracefully when some OS-specific utilities are missing.
