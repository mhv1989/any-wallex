"""Profile registry — multi-exchange multi-backend management.

One profile = one exchange instance = one data dir = one backend port.

Layout (Phase 0):
  data/profiles/registry.json     # {"profiles": {id: {...}}, "active": "wallex"}
  data/profiles/wallex/...        # the (migrated) legacy data/ tree
  data/profiles/<id>/profile.json # exchange adapter profile (later phases)

Env resolution order for the data dir:
  BOT_DATA_DIR (explicit, tests)  >  BOT_PROFILE=wallex → data/profiles/wallex
  > legacy default ROOT/data (unchanged behavior when no profiles exist yet).

The registry itself lives in ROOT/data/profiles/registry.json (shared across
backends so every launcher instance sees all profiles and their ports).
"""
from __future__ import annotations

import json
import os
import shutil
import time
from pathlib import Path
from typing import Dict, List, Optional

ROOT = Path(__file__).resolve().parent.parent.parent  # project root (bot/exchange/registry.py → up 3)
REGISTRY_PATH = ROOT / "data" / "profiles" / "registry.json"
DEFAULT_PORT = 8787


def profiles_root() -> Path:
    return ROOT / "data" / "profiles"


def registry_path() -> Path:
    return REGISTRY_PATH


def _read_registry() -> dict:
    try:
        raw = json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))
        if isinstance(raw, dict) and isinstance(raw.get("profiles"), dict):
            return raw
    except Exception:
        pass
    return {"profiles": {}, "active": ""}


def _write_registry(reg: dict) -> None:
    REGISTRY_PATH.parent.mkdir(parents=True, exist_ok=True)
    REGISTRY_PATH.write_text(json.dumps(reg, indent=2, ensure_ascii=False), encoding="utf-8")


def list_profiles() -> List[dict]:
    reg = _read_registry()
    out = []
    for pid, info in reg["profiles"].items():
        row = dict(info)
        row["id"] = pid
        out.append(row)
    out.sort(key=lambda r: r.get("created", 0))
    return out


def get_profile(pid: str) -> Optional[dict]:
    reg = _read_registry()
    info = reg["profiles"].get(pid)
    if info is None:
        return None
    row = dict(info)
    row["id"] = pid
    return row


def active_profile_id() -> str:
    reg = _read_registry()
    act = str(reg.get("active") or "")
    if act and act in reg["profiles"]:
        return act
    return "wallex" if "wallex" in reg["profiles"] else ""


def mark_last_launched(pid: str) -> None:
    """Record the profile that was actually LAUNCHED most recently (launcher boot
    or multi-backend spawn). Boot default prefers this over `active`, which the
    wizard flips on every setup End (user req: app must start on the last
    profile LAUNCHED, not the last profile added/activated)."""
    if pid not in _read_registry().get("profiles", {}):
        return
    reg = _read_registry()
    reg["last_launched"] = pid
    reg["last_launched_at"] = int(time.time())
    _write_registry(reg)


def last_launched_profile_id() -> str:
    """The most recently launched profile id ("" when never recorded/unknown)."""
    reg = _read_registry()
    pid = str(reg.get("last_launched") or "")
    return pid if pid in reg.get("profiles", {}) else ""


def default_boot_profile_id() -> str:
    """Launcher default: last LAUNCHED profile, falling back to `active`,
    then wallex, then any registered profile."""
    pid = last_launched_profile_id()
    if pid:
        return pid
    pid = active_profile_id()
    if pid:
        return pid
    reg = _read_registry()
    return next(iter(reg["profiles"]), "")


def register_profile(pid: str, name: str = "", port: Optional[int] = None,
                     exchange_ref: str = "", set_active: bool = False) -> dict:
    """Create/refresh a registry entry. Does NOT touch profile data."""
    reg = _read_registry()
    existing = reg["profiles"].get(pid, {})
    entry = {
        "name": name or existing.get("name") or pid,
        "port": int(port or existing.get("port") or _next_free_port(reg)),
        "exchange_ref": exchange_ref or existing.get("exchange_ref", ""),
        "created": existing.get("created") or int(time.time()),
    }
    reg["profiles"][pid] = entry
    if set_active or not reg.get("active"):
        reg["active"] = pid
    _write_registry(reg)
    row = dict(entry)
    row["id"] = pid
    return row


def set_active_profile(pid: str) -> None:
    reg = _read_registry()
    if pid not in reg["profiles"]:
        raise KeyError(f"profile '{pid}' is not registered")
    reg["active"] = pid
    _write_registry(reg)


def _next_free_port(reg: dict) -> int:
    used = {int(v.get("port", 0)) for v in reg["profiles"].values()}
    port = DEFAULT_PORT
    while port in used:
        port += 1
    return port


def _next_free_port_os(start: int) -> int:
    """Find a port free on the OS (registry may be stale across machines)."""
    import socket
    port = start
    for _ in range(50):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.3)
            if s.connect_ex(("127.0.0.1", port)) != 0:
                return port
        port += 1
    return start


def _port_in_use(pid: str, port: int) -> bool:
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.3)
        return s.connect_ex(("127.0.0.1", port)) == 0


# ── data-dir resolution ──────────────────────────────────────────────

def resolve_data_dir() -> Path:
    """BOT_DATA_DIR wins; else BOT_PROFILE=<id> → its dir; else legacy."""
    env = os.environ.get("BOT_DATA_DIR")
    if env:
        return Path(env)
    pid = os.environ.get("BOT_PROFILE", "").strip()
    if pid:
        p = profiles_root() / pid
        p.mkdir(parents=True, exist_ok=True)
        return p
    return ROOT / "data"


def resolve_profile_id() -> str:
    explicit = os.environ.get("BOT_DATA_DIR")
    if explicit:
        return os.environ.get("BOT_PROFILE", "") or Path(explicit).name
    pid = os.environ.get("BOT_PROFILE", "").strip()
    return pid or "wallex"


# ── one-time legacy migration ────────────────────────────────────────

_MIGRATION_ITEMS = [
    "bot.db", "secrets.enc", "salt.bin", "ai_providers.json",
    "ai_models_cache.json", "markets_cache.json", "strategies",
    "history", "Vibe strategies",
]


def migrate_legacy_data(profile_id: str = "wallex", dry_run: bool = False) -> dict:
    """Copy the legacy ROOT/data state into data/profiles/<id>/ and register it.

    Idempotent: skips items that already exist in the target. The legacy
    originals stay in place (disk is cheap; a second copy is the rollback).
    Log/report/QA files are NOT migrated (per-run artifacts).
    """
    src = ROOT / "data"
    dst = profiles_root() / profile_id
    report: Dict[str, object] = {"migrated": [], "skipped": [], "errors": []}
    if dry_run:
        for item in _MIGRATION_ITEMS:
            s, d = src / item, dst / item
            if d.exists():
                report["skipped"].append(item)
            elif s.exists():
                report["migrated"].append(item)
        return report

    if not (src / "bot.db").exists() and not (src / "strategies").exists():
        # nothing to migrate (fresh checkout)
        register_profile(profile_id, name="Wallex", port=DEFAULT_PORT, set_active=True)
        return report

    for item in _MIGRATION_ITEMS:
        s, d = src / item, dst / item
        try:
            if d.exists():
                report["skipped"].append(item)
            elif s.is_dir():
                shutil.copytree(s, d)
                report["migrated"].append(item)
            elif s.exists():
                shutil.copy2(s, d)
                report["migrated"].append(item)
        except Exception as exc:  # noqa: BLE001
            report["errors"].append(f"{item}: {exc}")

    register_profile(profile_id, name="Wallex", port=DEFAULT_PORT, set_active=True)
    return report
