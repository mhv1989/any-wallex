"""SQLite persistence — trades, equity curve, API log, audit/grid snapshots."""
from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import List, Optional

from .models import EquityPoint, Position

SCHEMA = """
CREATE TABLE IF NOT EXISTS trades (
    id TEXT PRIMARY KEY,
    symbol TEXT, opened_ts INTEGER, closed_ts INTEGER,
    qty REAL, entry REAL, stop REAL, close_price REAL,
    entry_reason TEXT, exit_reason TEXT,
    pnl REAL, fees REAL, realized_rr REAL, score INTEGER,
    pattern TEXT, hold_seconds INTEGER, state TEXT
);
CREATE TABLE IF NOT EXISTS equity (
    ts INTEGER PRIMARY KEY, equity REAL, drawdown_pct REAL
);
CREATE TABLE IF NOT EXISTS api_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL, method TEXT, path TEXT, status INTEGER,
    latency_ms REAL, retries INTEGER, error TEXT
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts INTEGER, kind TEXT, symbol TEXT, detail TEXT
);
CREATE TABLE IF NOT EXISTS audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts INTEGER,
    kind TEXT,
    symbol TEXT,
    data TEXT
);
CREATE TABLE IF NOT EXISTS grid_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts INTEGER,
    symbol TEXT,
    support REAL,
    resistance REAL,
    grid_levels TEXT,
    opportunity_json TEXT
);
CREATE TABLE IF NOT EXISTS kv (
    key TEXT PRIMARY KEY, value TEXT
);
CREATE TABLE IF NOT EXISTS manual_orders (
    id TEXT PRIMARY KEY,
    symbol TEXT, side TEXT, kind TEXT, qty REAL, price REAL,
    tp REAL, sl REAL, status TEXT, filled_price REAL, closed_price REAL,
    pnl REAL, created_ts INTEGER, updated_ts INTEGER, note TEXT
);
CREATE TABLE IF NOT EXISTS engine_state (
    key TEXT PRIMARY KEY,
    value TEXT
);
"""


class Storage:
    def __init__(self, data_dir: str):
        Path(data_dir).mkdir(parents=True, exist_ok=True)
        self.path = str(Path(data_dir) / "bot.db")
        self._lock = threading.Lock()
        self._last_prune = 0.0
        with self._connect() as con:
            con.executescript(SCHEMA)
            # migrate legacy DBs missing newer columns
            cols = {r["name"] for r in con.execute("PRAGMA table_info(trades)").fetchall()}
            if "pattern" not in cols:
                con.execute("ALTER TABLE trades ADD COLUMN pattern TEXT DEFAULT ''")

    def _connect(self) -> sqlite3.Connection:
        con = sqlite3.connect(self.path, timeout=10)
        con.row_factory = sqlite3.Row
        # FIX(audit-M12): WAL journal → readers (SSE poller every 2s) no
        # longer block writers (engine tick) and vice versa; busy_timeout
        # absorbs the remaining contention instead of raising immediately.
        try:
            con.execute("PRAGMA journal_mode=WAL")
            con.execute("PRAGMA busy_timeout=5000")
            con.execute("PRAGMA synchronous=NORMAL")
        except sqlite3.Error:
            pass
        return con

    # ── trades ─────────────────────────────────────────────────────
    def save_trade(self, p: Position) -> None:
        with self._lock, self._connect() as con:
            con.execute(
                """INSERT OR REPLACE INTO trades
                   (id,symbol,opened_ts,closed_ts,qty,entry,stop,close_price,
                    entry_reason,exit_reason,pnl,fees,realized_rr,score,
                    pattern,hold_seconds,state)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (p.id, p.symbol, p.opened_ts, p.closed_ts, p.qty or p.initial_qty,
                 p.entry, p.stop, p.close_price, p.entry_reason, p.exit_reason,
                 p.pnl, p.fees_paid, p.realized_rr, p.signal_score,
                 getattr(p, "pattern", "") or "",
                 (p.closed_ts or 0) - p.opened_ts, p.state),
            )

    def closed_trades(self, limit: int = 500) -> List[dict]:
        with self._connect() as con:
            rows = con.execute(
                "SELECT * FROM trades WHERE state='closed' ORDER BY closed_ts DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(r) for r in rows]

    # ── equity ─────────────────────────────────────────────────────
    def save_equity(self, pt: EquityPoint) -> None:
        with self._lock, self._connect() as con:
            con.execute("INSERT OR REPLACE INTO equity VALUES (?,?,?)",
                        (pt.ts, pt.equity, pt.drawdown_pct))

    def equity_series(self) -> List[dict]:
        with self._connect() as con:
            rows = con.execute("SELECT * FROM equity ORDER BY ts").fetchall()
        return [dict(r) for r in rows]

    # ── api log ────────────────────────────────────────────────────
    def save_api_log(self, entries: list) -> None:
        if not entries:
            return
        with self._lock, self._connect() as con:
            con.executemany(
                "INSERT INTO api_log (ts, method, path, status, latency_ms, retries, error) VALUES (?,?,?,?,?,?,?)",
                [(e.ts, e.method, e.path, e.status, e.latency_ms, e.retries, e.error) for e in entries],
            )

    def api_log_recent(self, limit: int = 200) -> List[dict]:
        with self._connect() as con:
            rows = con.execute("SELECT * FROM api_log ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]

    # ── events ─────────────────────────────────────────────────────
    def log_event(self, ts: int, kind: str, symbol: str, detail: str) -> None:
        with self._lock, self._connect() as con:
            con.execute("INSERT INTO events (ts, kind, symbol, detail) VALUES (?,?,?,?)",
                        (ts, kind, symbol, detail[:500]))

    def events_recent(self, limit: int = 100) -> List[dict]:
        with self._connect() as con:
            rows = con.execute("SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]

    # ── audit (lightweight strategy behavior log) ──────────────────
    def save_audit(self, kind: str, symbol: str, data: dict) -> None:
        with self._lock, self._connect() as con:
            con.execute("INSERT INTO audit (ts, kind, symbol, data) VALUES (?,?,?,?)",
                        (int(__import__("time").time()), kind, symbol, json.dumps(data, ensure_ascii=False)[:2000]))

    def audit_recent(self, limit: int = 200) -> List[dict]:
        with self._connect() as con:
            rows = con.execute("SELECT * FROM audit ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]

    # ── grid snapshots ─────────────────────────────────────────────
    def save_grid_snapshot(self, symbol: str, support: float, resistance: float,
                           grid_levels: list, opportunity: dict) -> None:
        with self._lock, self._connect() as con:
            con.execute(
                "INSERT INTO grid_snapshots (ts, symbol, support, resistance, grid_levels, opportunity_json) VALUES (?,?,?,?,?,?)",
                (int(__import__("time").time()), symbol, support, resistance,
                 json.dumps(grid_levels, ensure_ascii=False)[:2000],
                 json.dumps(opportunity, ensure_ascii=False)[:4000]),
            )

    def prune_old_rows(self, max_age_days: float = 30.0, cap: int = 200000) -> None:
        """FIX(audit-M12): grid_snapshots/api_log/events/equity grew forever
        (~768+ rows/day). Drop rows older than max_age_days and hard-cap
        api_log; called at most once per hour from the tick path."""
        import time as _t
        cutoff = int(_t.time() - max_age_days * 86400)
        with self._lock, self._connect() as con:
            for tbl in ("grid_snapshots", "events", "equity"):
                try:
                    con.execute(f"DELETE FROM {tbl} WHERE ts < ?", (cutoff,))
                except sqlite3.Error:
                    pass
            try:
                con.execute(
                    "DELETE FROM api_log WHERE id NOT IN "
                    "(SELECT id FROM api_log ORDER BY ts DESC LIMIT ?)", (cap,))
            except sqlite3.Error:
                pass

    def grid_snapshots_recent(self, symbol: Optional[str] = None, limit: int = 200) -> List[dict]:
        q = "SELECT * FROM grid_snapshots"
        args: list = []
        if symbol:
            q += " WHERE symbol=?"
            args.append(symbol)
        q += " ORDER BY id DESC LIMIT ?"
        args.append(limit)
        with self._connect() as con:
            return [dict(r) for r in con.execute(q, args).fetchall()]

    # ── kv (paper balance etc.) ────────────────────────────────────
    def kv_get(self, key: str, default: Optional[str] = None) -> Optional[str]:
        with self._connect() as con:
            row = con.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

    def kv_set(self, key: str, value: str) -> None:
        with self._lock, self._connect() as con:
            con.execute("INSERT OR REPLACE INTO kv (key, value) VALUES (?,?)", (key, value))

    # ── engine state ───────────────────────────────────────────────
    def engine_state_get(self, key: str, default: Optional[str] = None) -> Optional[str]:
        with self._connect() as con:
            row = con.execute("SELECT value FROM engine_state WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

    def engine_state_set(self, key: str, value: str) -> None:
        with self._lock, self._connect() as con:
            con.execute("INSERT OR REPLACE INTO engine_state (key, value) VALUES (?,?)", (key, value))

    def engine_state_del(self, key: str) -> None:
        with self._lock, self._connect() as con:
            con.execute("DELETE FROM engine_state WHERE key=?", (key,))

    # ── manual orders ──────────────────────────────────────────────
    def save_manual_order(self, o: dict) -> None:
        with self._lock, self._connect() as con:
            con.execute(
                """INSERT OR REPLACE INTO manual_orders
                   (id,symbol,side,kind,qty,price,tp,sl,status,filled_price,closed_price,pnl,created_ts,updated_ts,note)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (o["id"], o["symbol"], o["side"], o["kind"], o["qty"], o.get("price"),
                 o.get("tp"), o.get("sl"), o["status"], o.get("filled_price"),
                 o.get("closed_price"), o.get("pnl"), o["created_ts"],
                 o.get("updated_ts"), o.get("note", "")),
            )

    def manual_orders(self, statuses: Optional[List[str]] = None, limit: int = 200) -> List[dict]:
        q = "SELECT * FROM manual_orders"
        args: list = []
        if statuses:
            q += " WHERE status IN (%s)" % ",".join("?" * len(statuses))
            args = list(statuses)
        q += " ORDER BY created_ts DESC LIMIT ?"
        args.append(limit)
        with self._connect() as con:
            return [dict(r) for r in con.execute(q, args).fetchall()]

    def clear_manual_orders(self) -> None:
        with self._lock, self._connect() as con:
            con.execute("DELETE FROM manual_orders")
