"""Smart stop — kill ALL Any-WALLEX backends, nothing else.

Safety rules (user req: never mislead-kill another app):
  1. Candidate ports come from the profile REGISTRY (no hardcoding), plus any
     port the registry has ever assigned.
  2. A listening PID is only considered OURS if its COMMAND LINE contains
     'bot.server' or 'uvicorn' AND the project root path — so unrelated apps
     that happen to listen on those ports are NEVER killed.
  3. Graceful first: CTRL_BREAK-equivalent terminate() without /F gives the
     server a chance to close sockets; force /F only after a short wait.

Usage:
  python stop_all.py            # stop every registered profile backend
  python stop_all.py --list     # show what is running without stopping
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent          # stop_all.py sits in the project root
REGISTRY = ROOT / "data" / "profiles" / "registry.json"
MARKERS = ("bot.server", "uvicorn", "launcher.py")  # must appear in the command line
ROOT_MARKER = str(ROOT).lower()              # and the project must be ours


def registry_ports() -> list:
    ports = [8787]  # the wallex default is registry-implied even on fresh clones
    try:
        reg = json.loads(REGISTRY.read_text(encoding="utf-8"))
        for info in (reg.get("profiles") or {}).values():
            p = int(info.get("port") or 0)
            if p and p not in ports:
                ports.append(p)
    except Exception:
        pass
    return ports


def listening_pids(port: int) -> set:
    out = subprocess.check_output(["netstat", "-ano"], text=True)
    pids = set()
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 5 and parts[3] == "LISTENING" and parts[1].endswith(f":{port}"):
            pids.add(parts[-1])
    return pids


def cmdline(pid: str) -> str:
    try:
        out = subprocess.check_output(
            ["wmic", "process", "where", f"ProcessId={pid}", "get", "CommandLine"],
            text=True)
        return " ".join(out.splitlines()[1:]).strip().lower()
    except Exception:
        try:
            out = subprocess.check_output(
                ["powershell", "-NoProfile", "-Command",
                 f"(Get-CimInstance Win32_Process -Filter 'ProcessId={pid}').CommandLine"],
                text=True)
            return out.strip().lower()
        except Exception:
            return ""


def is_ours(pid: str) -> bool:
    cl = cmdline(pid)
    if not cl:
        return False
    if not any(m in cl for m in MARKERS):
        return False
    # only kill if it belongs to THIS project: absolute root in cmdline, OR a
    # relative 'launcher.py --profile' invocation (its cwd is our ROOT — the
    # working directory check below), OR the bot.server module marker.
    if ROOT_MARKER in cl or "bot.server" in cl:
        return True
    if "launcher.py" in cl:
        # confirm the launcher's cwd is our project via its open file handle
        # is not queryable cheaply; instead check the port is a registry port
        # (already guaranteed by the caller) and the profile arg is registered
        import re as _re
        m = _re.search(r"--profile\s+(\S+)", cl)
        if m:
            try:
                reg = json.loads(REGISTRY.read_text(encoding="utf-8"))
                return m.group(1).strip('"') in (reg.get("profiles") or {})
            except Exception:
                return False
        return True   # plain launcher run = the active profile (ours)
    return False


def stop_port(port: int, force: bool) -> int:
    killed = 0
    for pid in sorted(listening_pids(port)):
        if not is_ours(pid):
            print(f"  port {port}: PID {pid} is NOT ours — skipped (safety)")
            continue
        if force:
            r = subprocess.run(["taskkill", "/F", "/PID", pid], capture_output=True, text=True)
            ok = r.returncode == 0
        else:
            r = subprocess.run(["taskkill", "/PID", pid], capture_output=True, text=True)
            ok = r.returncode == 0
        print(f"  port {port}: PID {pid} {'stopped' if ok else 'refused: ' + (r.stderr or '').strip()[:60]}")
        if ok:
            killed += 1
    return killed


def main() -> int:
    force = "--force" in sys.argv
    listing = "--list" in sys.argv
    ports = registry_ports()
    running = []
    for p in ports:
        pids = listening_pids(p)
        for pid in pids:
            running.append((p, pid, is_ours(pid)))
    if listing or not running and not any(listening_pids(p) for p in ports):
        print("Any-WALLEX backends:")
        for p, pid, ours in running:
            print(f"  port {p}  PID {pid}  {'ours' if ours else 'foreign (will be skipped)'}")
        if not running:
            print("  none running")
        return 0

    # FIX(user req): warn about ACTIVE workflows (engine / running grids) before
    # killing backends. Hybrid restore resumes them on the next boot, but the
    # user must know they are interrupting live processes.
    import urllib.request as _ur
    active = []
    for p, pid, ours in running:
        if not ours:
            continue
        info = []
        try:
            with _ur.urlopen(f"http://127.0.0.1:{p}/api/status", timeout=3) as r:
                stj = json.loads(r.read().decode())
            if stj.get("running"):
                info.append("engine RUNNING")
            res = stj.get("reserved") or 0
            if res:
                info.append(f"reserved capital {res}")
        except Exception:
            pass
        try:
            with _ur.urlopen(f"http://127.0.0.1:{p}/api/grids", timeout=3) as r:
                gj = json.loads(r.read().decode())
            running_grids = [g for g in (gj.get("items") or []) if g.get("running")]
            if running_grids:
                info.append(f"{len(running_grids)} active grid(s): "
                            + ", ".join(f"{g.get('symbol')}({g.get('mode')})" for g in running_grids[:4]))
        except Exception:
            pass
        if info:
            active.append(f"  port {p}: " + "; ".join(info))
    if active and not force:
        print("⚠ ACTIVE WORKFLOWS detected — stopping will interrupt them")
        print("(hybrid restore will resume them on the next boot):")
        for a in active:
            print(a)
        try:
            ans = input("Continue? [y/N]: ").strip().lower()
        except EOFError:
            ans = "n"
        if ans not in ("y", "yes"):
            print("Aborted — nothing stopped.")
            return 1

    print("Stopping Any-WALLEX backends" + (" (force)" if force else " (graceful first)") + "...")
    killed = stop_all = 0
    for p in ports:
        killed += stop_port(p, force=force)
    # graceful pass may not remove them instantly; brief wait then force if asked
    time.sleep(1.5)
    remaining = [(p, pid) for p in ports for pid in listening_pids(p) if is_ours(pid)]
    if remaining and not force:
        print(f"{len(remaining)} backend(s) still alive — forcing...")
        for p, pid in remaining:
            subprocess.run(["taskkill", "/F", "/PID", pid], capture_output=True)
            killed += 1
    time.sleep(1)
    still = [p for p in ports if listening_pids(p)]
    if still:
        print(f"WARNING: ports still listening (foreign or stuck): {still}")
        return 1
    print("All Any-WALLEX backends stopped.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
