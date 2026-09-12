"""Any-WALLEX launcher — profile-aware multi-backend bootstrap.

Starts the FastAPI/uvicorn server for the ACTIVE profile (or --profile <id>)
and opens the dashboard in the default browser.

  python launcher.py                     # active profile (registry default: wallex)
  python launcher.py --profile nobitex   # a specific profile
  python launcher.py --profile nobitex --port 8788
  python launcher.py --list              # show profiles + ports

The launcher kills only the process listening on ITS OWN port, so several
profiles can run simultaneously on different ports.
"""
import argparse
import json
import os
import socket
import subprocess
import sys
import threading
import time
import webbrowser

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
HOST = "127.0.0.1"


def port_in_use(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex((HOST, port)) == 0


def kill_old_instance(port: int) -> None:
    """Kill whatever is still listening on the port (a previous run).
    Exact-match on ':port ' to never hit substring ports (878 vs 8787)."""
    try:
        out = subprocess.check_output(["netstat", "-ano"], text=True)
    except Exception:
        return
    pids = set()
    for line in out.splitlines():
        # netstat columns: Proto Local-Address Foreign-Address State PID
        parts = line.split()
        if len(parts) >= 5 and parts[3] == "LISTENING" \
                and parts[1].endswith(f":{port}"):
            pids.add(parts[-1])
    for pid in pids:
        subprocess.run(["taskkill", "/F", "/PID", pid], capture_output=True)


def open_browser_when_ready(url: str, timeout_s: float = 30.0) -> None:
    """Open the dashboard ONLY after the backend answers /api/status (user
    complaint: Edge opened a blank/stale page because the browser launched
    before uvicorn was listening). Falls back to opening after timeout."""
    import urllib.request
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url + "api/status", timeout=2) as r:
                if r.status == 200:
                    break
        except Exception:
            time.sleep(0.5)
    if not _open_edge_or_default(url):
        webbrowser.open(url)


_EDGE_CANDIDATES = (
    os.path.join(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"),
                 "Microsoft", "Edge", "Application", "msedge.exe"),
    os.path.join(os.environ.get("ProgramFiles", r"C:\Program Files"),
                 "Microsoft", "Edge", "Application", "msedge.exe"),
    os.path.join(os.environ.get("LOCALAPPDATA", ""), "Google", "Chrome",
                 "Application", "chrome.exe"),
    os.path.join(os.environ.get("ProgramFiles", r"C:\Program Files"),
                 "Google", "Chrome", "Application", "chrome.exe"),
)


def _open_edge_or_default(url: str) -> bool:
    """Explicit Edge/Chrome launch — bypasses Windows' broken default-browser
    association (user saw 'open with nonexistent application' prompts)."""
    for exe in _EDGE_CANDIDATES:
        if exe and os.path.isfile(exe):
            try:
                subprocess.Popen([exe, "--new-window", url], close_fds=True)
                return True
            except Exception:
                continue
    return False


def open_browser_later(delay: float, url: str) -> None:
    time.sleep(delay)
    webbrowser.open(url)


def ensure_dependencies() -> None:
    try:
        import fastapi  # noqa: F401
        import uvicorn  # noqa: F401
    except ImportError:
        print("[INFO] Installing dependencies, please wait...")
        subprocess.run(
            [sys.executable, "-m", "pip", "install", "-r",
             os.path.join(HERE, "requirements.txt")],
            check=False,
        )


def _load_registry():
    """Import the registry (after sys.path is set)."""
    from bot.exchange import registry as reg
    return reg


def main() -> int:
    parser = argparse.ArgumentParser(description="Any-WALLEX launcher")
    parser.add_argument("--profile", default="", help="profile id (default: registry active)")
    parser.add_argument("--port", type=int, default=0, help="override port for this run")
    parser.add_argument("--list", action="store_true", help="list profiles and ports")
    args, _unknown = parser.parse_known_args()

    reg = _load_registry()

    if args.list:
        rows = reg.list_profiles()
        active = reg.active_profile_id()
        if not rows:
            print("No profiles registered yet (boot once to create 'wallex').")
        for r in rows:
            mark = "*" if r["id"] == active else " "
            alive = "UP " if port_in_use(int(r.get("port", 0))) else "   "
            print(f" {mark} {alive} {r['id']:<16} port={r.get('port', '?'):<6} {r.get('name', '')}")
        return 0

    ensure_dependencies()

    os.chdir(HERE)
    print("=" * 50)
    print("   Any-WALLEX Trading Platform")
    print("=" * 50)

    # ── resolve profile + port ───────────────────────────────────────
    # FIX(user req): the app must start on the LAST PROFILE LAUNCHED — not the
    # last one the wizard activated (which used to be the boot default and
    # effectively meant "last profile added"). default_boot_profile_id()
    # prefers the recorded last_launched, falling back to the old behavior.
    pid = args.profile.strip() or reg.default_boot_profile_id() or "wallex"
    info = reg.get_profile(pid)
    if info is None:
        # Unknown profile id: register it on the spot so a fresh profile can
        # be booted directly (its data dir is created empty by the server).
        if args.port:
            port = args.port
        else:
            port = reg._next_free_port_os(8788)
        info = reg.register_profile(pid, name=pid, port=port)
        print(f"[NEW ] Profile '{pid}' registered on port {port}")
    else:
        port = int(args.port or info.get("port") or 8787)

    os.environ["BOT_PROFILE"] = pid
    reg.mark_last_launched(pid)   # this boot becomes the next default

    print(f"   Profile: {pid}   Port: {port}")

    if port_in_use(port):
        print(f"[INFO] Port {port} is busy - stopping the old instance...")
        kill_old_instance(port)
        time.sleep(1)

    # Open the browser only once the backend actually answers (never a blank
    # or cached-stale page from a server that is still booting).
    url = f"http://{HOST}:{port}/"
    threading.Thread(target=open_browser_when_ready, args=(url,), daemon=True).start()

    print(f"[RUN ] Starting server on {url}")
    print("       The dashboard will open in your browser automatically.")
    print("       Keep this window open while using the bot.")

    import uvicorn
    from bot.server import app

    try:
        uvicorn.run(app, host=HOST, port=port, log_level="info")
    except KeyboardInterrupt:
        print("\n[STOP] Server stopped by user.")
    except Exception as exc:  # noqa: BLE001
        print(f"\n[ERROR] Server crashed: {exc}")
        return 1

    print("\n[STOP] Server stopped.")
    return 0


if __name__ == "__main__":
    try:
        code = main()
    except Exception as exc:  # noqa: BLE001
        print(f"[FATAL] {exc}")
        code = 1
    # Never let the console close silently.
    try:
        input("\nPress Enter to close this window...")
    except (EOFError, KeyboardInterrupt):
        pass
    sys.exit(code)
