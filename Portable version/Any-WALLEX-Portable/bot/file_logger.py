"""Lightweight rolling JSONL logger for the Wallex app.

Each engine run creates timestamped files under `Logs/<profile_id>/`.
Per-profile folders keep parallel backends from interleaving their trails.

Categories:
  settings, events, opportunities, open_positions,
  closed_positions, api, decisions, balance
  + troubleshooting categories (2026-09-09):
  wizard (doc research / probe / calibrate / diagnose),
  ai (every AI provider call: status, attempts, latency, error),
  orders (manual + engine order placement / fills / rejections),
  errors (unhandled server exceptions + notable warnings),
  server (startup / shutdown / config / profile lifecycle).

Files roll over at 10 MB and keep at most 5 historical backups
(oldest dropped), so each category is capped at 5 x 10 MB on disk.

Deep components (the exchange adapter, AI client, wizard job threads,
order paths) run in the same backend process as the engine, so they reach
the per-profile logger through the process-wide accessor below
(`set_active` / `get_active`) rather than by constructor injection.
"""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional

MAX_BYTES = 10 * 1024 * 1024
MAX_BACKUPS = 5
# Per-category retention across all boots (the "5 file cap / 10MB / roll out
# oldest for each category" requirement): a profile dir keeps at most
# MAX_FILES_PER_CATEGORY newest `*_<category>.jsonl` files and at most
# MAX_BYTES total per category; older files are rolled out (deleted) on each
# boot. Backups (`file.jsonl.1` ... `.5`) are a separate per-file mechanism.
MAX_FILES_PER_CATEGORY = 5
MAX_BYTES_PER_CATEGORY = 10 * 1024 * 1024
CATEGORIES = [
    "settings",
    "events",
    "opportunities",
    "open_positions",
    "closed_positions",
    "api",
    "decisions",
    "balance",
    # troubleshooting (added 2026-09-09)
    "wizard",
    "ai",
    "orders",
    "errors",
    "server",
]

# ── process-wide accessor ────────────────────────────────────────────
# One FileLogger per backend process (created in server.create_app). Deep
# components that are constructed in many places / on worker threads can't
# easily take the logger as a constructor arg, so they call get_active().
#
# Wizard jobs are the special case: they run on worker threads and target a
# SPECIFIC exchange (often a different profile than the host backend). A
# thread-local override (set_thread_override) redirects this thread's logging
# to the target profile's folder, so "research MEXC" lands in Logs/mexc/ even
# when run from the wallex backend.
_ACTIVE: Optional["FileLogger"] = None
_ACTIVE_LOCK = threading.Lock()
_TL = threading.local()


def set_active(logger: Optional["FileLogger"]) -> None:
    """Register this process's per-profile logger for deep components."""
    global _ACTIVE
    with _ACTIVE_LOCK:
        _ACTIVE = logger


def set_thread_override(logger: Optional["FileLogger"]) -> None:
    """Redirect THIS thread's logging to `logger` (wizard jobs). None clears."""
    _TL.logger = logger


def get_active() -> Optional["FileLogger"]:
    ov = getattr(_TL, "logger", None)
    if ov is not None:
        return ov
    with _ACTIVE_LOCK:
        return _ACTIVE


def log(category: str, data: Dict[str, Any]) -> None:
    """Fire-and-forget write to this process's active per-profile logger.

    Safe to call from anywhere; a no-op when no logger is active (e.g. in
    unit tests) or the category is unknown. Never raises.
    """
    logger = get_active()
    if logger is None:
        return
    logger.write(category, data)


def log_error(exc_or_msg: Any, context: str = "", **extra: Any) -> None:
    """Convenience: write a structured error record to the `errors` category."""
    payload: Dict[str, Any] = {
        "ts": int(time.time()),
        "context": context,
    }
    if isinstance(exc_or_msg, BaseException):
        payload["type"] = type(exc_or_msg).__name__
        payload["detail"] = str(exc_or_msg)[:1000]
    else:
        payload["detail"] = str(exc_or_msg)[:1000]
    payload.update(extra)
    log("errors", payload)


class FileLogger:
    def __init__(self, root: str | Path, run_id: str, profile_id: str = "") -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.run_id = str(run_id)
        self.profile_id = str(profile_id or "")
        self._lock = threading.Lock()
        self._handles: Dict[str, Any] = {}
        self._pruned: set = set()

    def _prune_category(self, category: str) -> None:
        """Enforce per-category retention: <= MAX_FILES_PER_CATEGORY newest
        `*_<category>.jsonl` files AND <= MAX_BYTES_PER_CATEGORY total size.
        Oldest (by mtime, then name) files are rolled out (deleted) first.
        Backups (`.1`..`.5`) are left to the per-file rotation. Runs once per
        category per logger instance. Caller must hold self._lock."""
        if category in self._pruned:
            return
        self._pruned.add(category)
        suffix = f"_{category}.jsonl"
        try:
            files = [p for p in self.root.glob(f"*{suffix}")]
        except OSError:
            return
        # oldest first
        files.sort(key=lambda p: (p.stat().st_mtime, p.name))
        # (a) keep at most MAX_FILES_PER_CATEGORY newest
        for p in files[:-MAX_FILES_PER_CATEGORY]:
            try:
                p.unlink()
            except OSError:
                pass
        # (b) enforce the 10 MB total cap (drop oldest until under)
        kept = files[-MAX_FILES_PER_CATEGORY:]
        total = 0
        for p in kept:
            try:
                total += p.stat().st_size
            except OSError:
                pass
        if total > MAX_BYTES_PER_CATEGORY:
            for p in kept:
                if total <= MAX_BYTES_PER_CATEGORY:
                    break
                try:
                    total -= p.stat().st_size
                    p.unlink()
                except OSError:
                    pass

    def _base(self, category: str) -> str:
        return str(self.root / f"{self.run_id}_{category}.jsonl")

    def _rotate(self, path: str) -> None:
        if not os.path.exists(path):
            return
        if os.path.getsize(path) < MAX_BYTES:
            return
        # shift backups .5 -> delete, .4 -> .5, ..., .1 -> .2, current -> .1
        for i in range(MAX_BACKUPS, 0, -1):
            src = f"{path}.{i}"
            if i == MAX_BACKUPS:
                try:
                    os.remove(src)
                except FileNotFoundError:
                    pass
            else:
                dst = f"{path}.{i + 1}"
                try:
                    os.replace(src, dst)
                except FileNotFoundError:
                    pass
        # move active to .1
        try:
            os.replace(path, f"{path}.1")
        except FileNotFoundError:
            pass

    def _get_handle(self, category: str):
        path = self._base(category)
        self._rotate(path)
        if category not in self._handles:
            try:
                fh = open(path, "a", encoding="utf-8")
            except OSError:
                return None
            self._handles[category] = fh
        else:
            # re-check rotation after possible writes
            fh = self._handles[category]
            try:
                cur = fh.name
                if os.path.exists(cur) and os.path.getsize(cur) >= MAX_BYTES:
                    fh.close()
                    self._rotate(cur)
                    try:
                        fh = open(cur, "a", encoding="utf-8")
                    except OSError:
                        return None
                    self._handles[category] = fh
            except OSError:
                pass
        # Enforce the per-category file-count + size cap ONCE, AFTER the
        # current file exists so it is counted among the newest and survives
        # (otherwise the cap would oscillate at N+1).
        self._prune_category(category)
        return self._handles[category]

    def write(self, category: str, data: Dict[str, Any]) -> None:
        if category not in CATEGORIES:
            return
        rec = dict(data)
        rec.setdefault("ts", int(time.time()))
        if self.profile_id:
            rec.setdefault("profile", self.profile_id)
        payload = json.dumps(rec, ensure_ascii=False, default=str)
        with self._lock:
            fh = self._get_handle(category)
            if fh is None:
                return
            try:
                fh.write(payload + "\n")
                fh.flush()
            except OSError:
                pass

    def close_all(self) -> None:
        for fh in list(self._handles.values()):
            try:
                fh.close()
            except OSError:
                pass
        self._handles.clear()
