"""Trading engine — the 15-minute scheduler and position manager.

Flow per tick (only after the 15m candle CLOSES):
  for each symbol (>=12s between API calls, enforced by the client):
    1. fetch 4h / 1h / 15m / 1d candles (closed only)
    2. build signal (8 criteria) + grid
    3. manage open positions (BE, trailing, partial, exits)
    4. if eligible signal and risk allows -> open position
    5. persist equity, trades, events

Reconnect safety: broker.sync() is called before the first tick after any
connection failure, so real exchange state is reconciled first.
"""
from __future__ import annotations

from pathlib import Path

import json
import logging
import threading
import time
import uuid
from typing import Dict, List, Optional

from . import grid as gridmod
from .broker import BaseBroker, LiveBroker, LiveMarginBroker, PaperBroker, PaperMarginBroker
from .models import Candle, Confirmation, EquityPoint, ExitReason, Position, Signal, SymbolSnapshot
from .risk import RiskManager
from .signal import build_signal, build_signal_short
from .storage import Storage
from .strategy_store import StrategyStore
from .structure import bearish_choch_recent, bullish_choch_recent
from .wallex_client import WallexClient

log = logging.getLogger("engine")

TF_TREND = "240"
TF_ENTRY = "60"
TF_FILTER = "15"
TF_GRID = "1D"


class Engine:
    def __init__(self, cfg: dict, client: WallexClient, broker: BaseBroker, storage: Storage, quote_service=None, file_logger=None, data_dir: str = "", strategy_store=None):
        self.cfg = cfg
        self.client = client
        self.broker = broker
        self.storage = storage
        self.risk = RiskManager(cfg)
        self._quote_service = quote_service
        self.file_logger = file_logger
        self.data_dir = data_dir or ""
        self.symbols: List[str] = list(cfg.get("symbols", []))
        self.snapshots: Dict[str, SymbolSnapshot] = {}
        self.positions: Dict[str, Position] = {}
        self.peak_equity = broker.equity()
        self.running = False
        self._thread: Optional[threading.Thread] = None
        self.last_tick_ts = 0
        self.connected = True
        self._candle_cache: Dict[str, List[Candle]] = {}
        self._open_candle: Dict[str, Candle] = {}
        # (B) time-stagger: last full-window fetch ts per symbol:resolution for slow TFs
        self._last_fetch: Dict[str, float] = {}
        self._fetch_backoff: Dict[str, float] = {}
        self._fetch_connected: Dict[str, bool] = {}
        self._fetch_inflight: Dict[str, float] = {}   # key -> ts when a fetch started (chart endpoint dedupe)
        self._fetch_err: Dict[str, str] = {}          # key -> last fetch error (chart endpoint surfaces stale+why)
        self._candle_mtime: Dict[str, float] = {}     # key -> last-rebased file mtime (disk rebase guard)
        self._last_closed_ts: Dict[str, int] = {}
        self._connected = True
        # FIX(#4): tick reentrancy guard — /api/engine/tick must not run
        # tick_once concurrently with the engine's own run loop.
        self._tick_lock = threading.Lock()
        # external AI strategies loaded from disk
        self.strategy_store = strategy_store
        self.external_strategies: List[dict] = []
        self.active_external_strategy: Optional[dict] = None
        self._external_signal_cache: Dict[str, List[dict]] = {}
        # progress monitor for long synchronous jobs (startup preload, ticks)
        self.progress: Dict[str, dict] = {"active": False, "phase": "", "done": 0, "total": 0, "started_ts": 0}
        # manual user orders (limit/stop/TP/SL simulation)
        from .manual_orders import ManualOrderManager
        self.manual = ManualOrderManager(broker, storage)

    def load_external_strategies(self) -> None:
        """Load external strategies from disk, filter to allowed timeframes, ensure one active."""
        if not self.strategy_store:
            return
        try:
            all_ext = self.strategy_store.list_all()
        except Exception as exc:
            log.warning("external strategy load failed: %s", exc)
            return
        # include every schema-allowed TF (legacy engine set + "1D" for
        # daily-swing external strategies — the engine already caches 1D)
        from .strategy_schema import ALLOWED_TIMEFRAMES
        allowed = set(ALLOWED_TIMEFRAMES)
        self.external_strategies = [
            s for s in all_ext
            if str(s.get("timeframe", "")).strip() in allowed
        ]
        # ensure legacy is always present in the list but respect its enabled flag
        legacy = self.strategy_store.legacy()
        if legacy:
            if legacy not in self.external_strategies:
                self.external_strategies.insert(0, legacy)
        # if everything disabled, enable legacy as fallback
        enabled = [s for s in self.external_strategies if s.get("enabled", False)]
        if not enabled and legacy:
            legacy.setdefault("enabled", True)
            try:
                self.strategy_store.save(legacy)
            except Exception as e:
                log.debug(f"engine: suppressed {type(e).__name__}: {e} | ctx: {ctx[:80]}")
            self.external_strategies.insert(0, legacy)
        # pick active from KV; if missing or disabled, fall back to first enabled
        active_id = ""
        try:
            active_id = str(self.storage.kv_get("active_strategy_id") or "")
        except Exception as e:
            log.debug(f"engine: suppressed {type(e).__name__}: {e} | ctx: {ctx[:80]}")
        if active_id == "legacy":
            self.active_external_strategy = None
        else:
            match = next((s for s in self.external_strategies if s.get("strategy_id") == active_id and s.get("enabled", False)), None)
            self.active_external_strategy = match
        # FIX(#1): "one active" means exactly that. Do NOT silently fall back to
        # an arbitrary enabled strategy — respect the user's explicit choice
        # (legacy active → None → no external signals; an orphaned id → None).
        log.info("loaded %d external strategies, active=%s", len(self.external_strategies), getattr(self.active_external_strategy, 'get', lambda k, d=None: d)("strategy_id") if self.active_external_strategy else "legacy")

    def set_symbols(self, symbols: List[str]) -> None:
        """Hot-swap the active symbol list (used by /api/symbols endpoint)."""
        self.symbols = list(symbols or [])
        if self.data_dir:
            try:
                self.preload_symbols_from_disk()
            except Exception as e:
                log.warning("symbol-change disk preload failed: %s", e)
        log.info("engine symbol list updated: %d symbols", len(self.symbols))

    @property
    def connected(self) -> bool:
        """Overall connectivity: True if at least one symbol:resolution is connected."""
        return self._connected

    @connected.setter
    def connected(self, value: bool) -> None:
        self._connected = bool(value)

    def connected_map(self) -> Dict[str, bool]:
        """Per-symbol connectivity map for diagnostics."""
        out: Dict[str, bool] = {}
        for key, val in self._fetch_connected.items():
            sym = key.split(":")[0]
            out[sym] = out.get(sym, False) or val
        return out

    @property
    def margin_mode(self) -> bool:
        """True when the active broker supports margin (shorts allowed).
        FIX(C2): LiveMarginBroker is a margin broker too — live margin previously
        kept margin_mode False, so short signals never fired in live margin."""
        return isinstance(self.broker, (PaperMarginBroker, LiveMarginBroker))

    def candle_cache_has(self, symbol: str) -> bool:
        return f"{symbol}:15" in self._candle_cache

    @staticmethod
    def _cache_path(data_dir: str, symbol: str, resolution: str) -> Path:
        d = Path(data_dir) / "history" / symbol
        d.mkdir(parents=True, exist_ok=True)
        return d / f"{resolution}.json"

    def _load_candle_disk(self, symbol: str, resolution: str) -> List[Candle]:
        if not self.data_dir:
            return []
        try:
            path = self._cache_path(self.data_dir, symbol, resolution)
            if not path.exists():
                return []
            raw = json.loads(path.read_text(encoding="utf-8"))
            candles = [Candle(ts=r[0], o=r[1], h=r[2], l=r[3], c=r[4], v=r[5]) for r in raw]
            normalized = self._normalize_candles(symbol, resolution, candles)
            if normalized:
                # CRITICAL: if we just re-aggregated hourly bars into 4h, the disk
                # file holds the WRONG granularity. Rewrite it immediately so the
                # mislabeled data never serves the chart again.
                if resolution == "240" and candles and len(candles) != len(normalized):
                    try:
                        self._save_candle_disk(symbol, resolution, normalized)
                        log.info("re-aggregated stale %s %s disk cache: %d -> %d bars",
                                 symbol, resolution, len(candles), len(normalized))
                    except Exception as e:
                        log.debug(f"engine: suppressed {type(e).__name__}: {e} | ctx: {ctx[:80]}")
                key = f"{symbol}:{resolution}"
                res_sec = {"15": 900, "60": 3600, "240": 14400, "1D": 86400}[resolution]
                if int(time.time()) - normalized[-1].ts < res_sec:
                    self._open_candle[key] = normalized[-1]
                    return normalized[:-1]
                self._open_candle.pop(key, None)
            return normalized
        except Exception as e:
            log.warning("disk cache load failed %s %s: %s", symbol, resolution, e)
            return []

    def _save_candle_disk(self, symbol: str, resolution: str, candles: List[Candle]) -> None:
        if not self.data_dir or not candles:
            return
        try:
            # ALWAYS aggregate to the requested granularity before writing —
            # a full refetch returns RAW 1h bars for res=240 (Wallex quirk) and
            # writing them unnormalized poisons the disk cache (root cause of
            # the delta gate failing on every startup: disk interval ≠ 14400).
            normalized = self._normalize_candles(symbol, resolution, candles)
            path = self._cache_path(self.data_dir, symbol, resolution)
            # FIX(audit-M5): atomic write (tmp + os.replace) — a kill mid-write
            # corrupted the cache file and silently triggered a full backfill.
            tmp = path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps([[c.ts, c.o, c.h, c.l, c.c, c.v] for c in normalized]), encoding="utf-8")
            tmp.replace(path)
            self._candle_mtime[f"{symbol}:{resolution}"] = path.stat().st_mtime
        except Exception as e:
            log.warning("disk cache save failed %s %s: %s", symbol, resolution, e)

    def preload_symbols_from_disk(self) -> None:
        """Load cached candles from disk for all current symbols on startup."""
        if not self.data_dir or not self.symbols:
            return
        resolutions = ["15", "60", "240", "1D"]
        # progress monitor: disk load + forced refetch, visible via /api/progress
        total_units = len(self.symbols) * len(resolutions) + len(self.symbols) * 2
        self.progress = {"active": True, "phase": "startup_preload", "done": 0,
                         "total": total_units, "started_ts": int(time.time()),
                         "detail": "خواندن کش دیسک…"}
        loaded = 0
        stale_slow: set = set()
        for symbol in self.symbols:
            for res in resolutions:
                candles = self._load_candle_disk(symbol, res)
                if candles:
                    key = f"{symbol}:{res}"
                    self._candle_cache[key] = candles
                    self._last_fetch[key] = float(time.time())
                    loaded += 1
                    # mark stale slow TFs so the forced refresh below isn't
                    # blocked by the stagger guard (last_fetch was just set)
                    res_sec = {"15": 900, "60": 3600, "240": 14400, "1D": 86400}[res]
                    if res in ("240", "1D") and time.time() - candles[-1].ts > res_sec:
                        stale_slow.add(key)
                self.progress["done"] += 1
                self.progress["detail"] = f"{symbol} · {res}"
        if loaded:
            log.info("startup disk cache loaded %d candle sets for %d symbols", loaded, len(self.symbols))
        # ── SMART refresh (delta-first, fallback-full) for stale slow TFs.
        # This network work NEVER blocks server startup: preload is allowed to
        # run in a background thread while the dashboard comes up immediately.
        # The dashboard's progress bar reflects this job in real time.
        # NOTE: the delta fetch already does full-window fallback internally;
        # here we REPLACE the cache with the fetched window (not merge) so any
        # poisoned/legacy rows on disk can never re-enter the live dataset.
        def _refresh_job(stale: set):
            forced = 0
            for symbol in self.symbols:
                for res in ("240", "1D"):
                    key = f"{symbol}:{res}"
                    if key not in stale:
                        continue
                    try:
                        self._last_fetch[key] = 0.0   # defeat the stagger guard
                        self.progress["phase"] = "startup_refresh"
                        self.progress["detail"] = f"به‌روزرسانی {symbol} · {res}"
                        # DEPTH PRESERVATION: never let a short chart window
                        # replace a deep history cache. Request at least as many
                        # bars as the cache already holds (plus a small margin),
                        # so startup refresh only HEALS staleness — it must not
                        # shrink backtest depth (720 4h bars → 200 regression).
                        try:
                            have = len(self._candle_cache.get(key, []))
                        except Exception:
                            have = 0
                        want = 200 if res == "240" else 210 if res == "1D" else 45
                        if res in ("240", "1D"):
                            # depth preservation: never let the refresh replace
                            # a deep history cache with a shallower window
                            want = max(want, min(have + 20, 1500))
                        fresh = self._fetch_window(symbol, res, want)
                        if fresh:
                            # drop open candle, then REPLACE (self-healing)
                            res_sec = 14400 if res == "240" else 86400
                            now_ts = int(time.time())
                            if fresh and now_ts - fresh[-1].ts < res_sec:
                                self._open_candle[key] = fresh[-1]
                                fresh = fresh[:-1]
                            # 240 raw bars may come back as 1h — aggregate
                            if res == "240" and fresh and len(fresh) >= 2 and fresh[1].ts - fresh[0].ts <= 2 * 3600:
                                fresh = self._aggregate_buckets(fresh, 14400)
                            # if the provider returned FEWER bars than we already
                            # had, keep the deeper cache and only refresh its tail
                            # (same-ts rows are overwritten by the newer close).
                            old = self._candle_cache.get(key) or []
                            if old and len(fresh) < len(old):
                                tail_start = fresh[0].ts if fresh else 0
                                dedup = {c.ts: c for c in old if c.ts < tail_start}
                                for c in fresh:
                                    dedup[c.ts] = c   # fresh wins on overlap
                                fresh = [dedup[ts] for ts in sorted(dedup)]
                            self._candle_cache[key] = fresh
                            self._save_candle_disk(symbol, res, fresh)
                            self._last_closed_ts[key] = fresh[-1].ts if fresh else 0
                            forced += 1
                        # else: [] = disk already at the newest closed bar — nothing to do
                    except Exception as e:
                        log.warning("startup smart refresh failed %s %s: %s", symbol, res, e)
                    self.progress["done"] += 1
            self.progress = {"active": False, "phase": "idle",
                             "done": total_units, "total": total_units,
                             "started_ts": self.progress.get("started_ts", 0), "detail": ""}
            if forced:
                log.info("startup smart refresh replaced %d stale slow-TF candle sets", forced)
        if stale_slow:
            threading.Thread(target=_refresh_job, args=(set(stale_slow),),
                             daemon=True, name="startup-refresh").start()
        else:
            self.progress = {"active": False, "phase": "idle", "done": total_units,
                             "total": total_units, "started_ts": self.progress.get("started_ts", 0),
                             "detail": ""}
        if loaded:
            log.info("startup disk cache loaded %d candle sets for %d symbols", loaded, len(self.symbols))

    def _normalize_candles(self, symbol: str, resolution: str, candles: List[Candle]) -> List[Candle]:
        """Apply granularity normalization only; do NOT staleness-check here."""
        if not candles:
            return []
        res_sec = {"15": 900, "60": 3600, "240": 14400, "1D": 86400}.get(resolution, 0)
        if not res_sec or len(candles) < 2:
            return candles
        # Measure TRUE bar spacing as the MINIMUM over the WHOLE series — a
        # single outlier gap (missing bar) must not stop normalization, and a
        # HEAD-ONLY sample (first 60 gaps) misses mixed series where raw 1h
        # bars were merged into the TAIL of a 4h cache (2026-09-05 LINKUSDT
        # re-pollution). Full scan is microseconds even at 11.5k bars.
        raw_interval = min(candles[i + 1].ts - candles[i].ts
                           for i in range(len(candles) - 1))
        if 0 < raw_interval < res_sec // 2:
            candles = self._aggregate_buckets(candles, res_sec)
        return candles

    def _drop_open_candle(self, symbol: str, resolution: str, candles: List[Candle]) -> List[Candle]:
        res_sec = {"15": 900, "60": 3600, "240": 14400, "1D": 86400}[resolution]
        now = int(time.time())
        if candles:
            last_open_time = candles[-1].ts
            if now - last_open_time < res_sec:
                return candles[:-1]
        return candles

    def _staleness_filter(self, symbol: str, resolution: str, candles: List[Candle]) -> List[Candle]:
        if not candles:
            return []
        res_sec = {"15": 900, "60": 3600, "240": 14400, "1D": 86400}[resolution]
        now = int(time.time())
        key = f"{symbol}:{resolution}"
        last_closed_ts = candles[-1].ts
        self._last_closed_ts[key] = last_closed_ts
        staleness = now - last_closed_ts
        max_staleness = 3 * res_sec
        if staleness > max_staleness:
            log.warning("stale cache %s %s: last_closed=%d age=%ds > max=%ds — skipping cache",
                        symbol, resolution, last_closed_ts, staleness, max_staleness)
            return []
        return candles

    # ── data ───────────────────────────────────────────────────────
    def _aggregate_buckets(self, candles: List[Candle], bucket_sec: int) -> List[Candle]:
        """Aggregate candles into aligned buckets (o=first, h=max, l=min, c=last, v=sum)."""
        buckets: Dict[int, List[Candle]] = {}
        for c in candles:
            bucket_ts = (c.ts // bucket_sec) * bucket_sec
            buckets.setdefault(bucket_ts, []).append(c)
        grouped = []
        for bucket_ts in sorted(buckets):
            buf = buckets[bucket_ts]
            if not buf:
                continue
            grouped.append(Candle(
                ts=buf[0].ts,
                o=buf[0].o,
                h=max(x.h for x in buf),
                l=min(x.l for x in buf),
                c=buf[-1].c,
                v=sum(getattr(x, 'v', 0.0) for x in buf),
            ))
        return grouped

    def _fill_gap_filler(self, symbol: str, candles: List[Candle], full_from: int, now: int) -> List[Candle]:
        """1D fallback path only: backfill interior zero-volume day bars
        reconstructed from their 240m parts (Wallex 1D sometimes returns
        zero-volume bars for interior days)."""
        try:
            c240 = self.client.get_candles(symbol, "240", full_from, now)
            if not c240:
                return candles
            have = {c.ts: c for c in candles}
            by240: Dict[int, List[Candle]] = {}
            for c in c240:
                by240.setdefault((c.ts // 14400) * 14400, []).append(c)
            for ts in sorted(have):
                if getattr(have[ts], "v", 0.0) > 1e-12:
                    continue
                base = (ts // 86400) * 86400
                parts = by240.get(base, [])
                if not parts or parts[0].ts == ts:
                    continue  # no earlier 240m bar inside that day
                have[ts] = Candle(ts=ts, o=parts[0].o, h=max(p.h for p in parts),
                                  l=min(p.l for p in parts), c=parts[-1].c,
                                  v=sum(getattr(p, "v", 0.0) for p in parts))
            return [have[ts] for ts in sorted(have)]
        except Exception:
            return candles

    def _fetch_window(self, symbol: str, resolution: str, count: int) -> List[Candle]:
        """One exchange round-trip for the wanted window, with a reliability gate.

        TIME-AWARE (user requirement): if the last CLOSED candle on disk is
        still forming on the exchange (no new bar has elapsed), there is
        NOTHING new to fetch — return [] immediately, no network call.
        Otherwise: try SMART incremental (only the delta since the last cached
        candle); validate the delta (alignment, no gaps, plausible volume); on
        ANY doubt fall back to the FULL window refetch (always correct).
        """
        now = int(time.time())
        res_sec = {"15": 900, "60": 3600, "240": 14400, "1D": 86400}[resolution]
        key = f"{symbol}:{resolution}"
        cached = self._candle_cache.get(key, [])
        # REBASE from DISK before any gate/fetch (conflict fix, A+B hybrid):
        # the in-memory snapshot can be STALE-THIN vs disk (engine restarted
        # earlier, or the background 15m-depth backfill deepened the file
        # behind us). If disk holds MORE bars than memory, merging from the
        # thin memory + a forward delta would CLOBBER the deep disk file with
        # a shallow window. Re-read disk only when the file's mtime changed
        # since our last rebase (cheap stat, not a JSON parse, every call).
        if self.data_dir:
            try:
                p = self._cache_path(self.data_dir, symbol, resolution)
                if p.exists():
                    mtime = p.stat().st_mtime
                    if self._candle_mtime.get(key, 0.0) < mtime:
                        disk_now = self._load_candle_disk(symbol, resolution)
                        self._candle_mtime[key] = mtime
                        if len(disk_now) > len(cached):
                            cached = disk_now
            except Exception as e:
                log.debug(f"engine: suppressed {type(e).__name__}: {e} | ctx: {ctx[:80]}")
        count = max(count, len(cached))

        # ── DEPTH BACKFILL (before the bar-elapsed gate): if the cache holds far
        # fewer bars than the requested window, a delta would never fill the gap
        # (deltas only move forward). Fetch the FULL requested window instead —
        # one ladder request for 15m (1m bars) or 240m (1h bars) backfills depth.
        # "1D" uses a deeper trigger (200): daily external strategies evaluate
        # long-window indicators, so top the daily cache up toward the requested
        # 210 bars — the fetch is still once per day (stagger) and this runs at
        # most once per symbol (afterwards cached >= 200 skips the branch).
        backfill_trigger = min(count, 200) if resolution == "1D" else min(count, 100)
        if len(cached) < backfill_trigger:
            try:
                full_from = now - res_sec * (count + 3)
                candles = self.client.get_candles(symbol, resolution, full_from, now)
                if candles and len(candles) >= 2 and (candles[1].ts - candles[0].ts) < res_sec // 2:
                    candles = self._aggregate_buckets(candles, res_sec)
                if resolution == "1D" and candles:
                    candles = self._fill_gap_filler(symbol, candles, full_from, now)
                log.info("[depth-backfill] %s %s: %d bars fetched (cache was %d)", symbol, resolution, len(candles), len(cached))
                return candles[-(count + 3):]
            except Exception:
                self._fetch_connected[key] = False
                raise

        # ── BAR-ELAPSED GATE: has a new bar actually closed since the cache? ──
        if cached:
            last_closed = cached[-1].ts
            current_bucket = (now // res_sec) * res_sec   # the bar forming right now
            # exchange "now bar" opens at current_bucket; the last CLOSED bar is
            # current_bucket - res_sec. If cache already has it → nothing new.
            if last_closed >= current_bucket - res_sec:
                return []   # disk is already at the newest closed bar — zero work

        from_ts = (cached[-1].ts if cached else now - res_sec * (count + 3)) + res_sec
        from_ts = min(from_ts, now - res_sec)
        candles: List[Candle] = []
        try:
            candles = self.client.get_candles(symbol, resolution, from_ts, now)
            self._fetch_connected[key] = True
            # FIX(#3): reconnect-safety — a successful fetch after failures
            # flips the engine-level connected flag back on so the pre-tick
            # broker.sync() reconciliation runs on the NEXT tick boundary.
            if not self.connected and all(self._fetch_connected.values()):
                self.connected = True
                log.info("engine reconnected: all TF fetches healthy again")
            # ── Wallex granularity pre-normalization (BEFORE the gate) ──
            # res=15 returns 1-MINUTE bars; res=240 returns 1-HOUR bars. If we
            # gate the raw response, spacing checks always fail → pointless
            # fallback every time. Aggregate to the requested TF first, then
            # validate the resulting 15m/240m bars.
            if candles and len(candles) >= 2:
                raw_sp = candles[1].ts - candles[0].ts
                if raw_sp < res_sec // 2:
                    candles = self._aggregate_buckets(candles, res_sec)
            # ── correctness gate on the (now normalized) delta ──
            delta_ok = True
            if cached and candles:
                if candles[0].ts != cached[-1].ts + res_sec:
                    delta_ok = False  # first delta bar isn't the next bar after cache
                elif len(candles) >= 2:
                    for i in range(len(candles) - 1):
                        if candles[i + 1].ts - candles[i].ts != res_sec:
                            delta_ok = False; break  # interior gap → wrong granularity
                if delta_ok and len(candles) >= 3:
                    vols = [getattr(c, "v", 0.0) for c in candles[:-1]]
                    if vols and all(v <= 1e-12 for v in vols):
                        delta_ok = False  # all-zero volumes = garbage (e.g. 1D bars)
                if cached:
                    # expected full bars between cache edge and NOW, EXCLUDING the
                    # still-forming current bucket (it can never be in the delta
                    # as a closed bar — this false-positive forced fallbacks when
                    # the disk was actually already at the newest closed bar)
                    expected = ((now // res_sec) * res_sec - cached[-1].ts) // res_sec - 1
                    if expected > 4 and len(candles) < expected - 2:
                        delta_ok = False  # suspiciously few bars vs elapsed window
            if cached and not delta_ok:
                # ── FALLBACK: full-window refetch (always correct) ──
                log.info("[smart-fetch] %s %s: delta gate FAILED → full-window fallback", symbol, resolution)
                full_from = now - res_sec * (count + 3)
                candles = self.client.get_candles(symbol, resolution, full_from, now)
                if resolution == "1D" and candles:
                    candles = self._fill_gap_filler(symbol, candles, full_from, now)
                candles = candles[-(count + 3):]
        except Exception:
            self._fetch_connected[key] = False
            raise
        return candles

    def _fetch_candles(self, symbol: str, resolution: str, count: int) -> List[Candle]:
        now = int(time.time())
        res_sec = {"15": 900, "60": 3600, "240": 14400, "1D": 86400}[resolution]
        key = f"{symbol}:{resolution}"

        # Minimum bar count guard: if cache is too thin, we can't generate reliable
        # signals for this resolution. Return cache if available, otherwise force a fetch.
        cached = self._candle_cache.get(key, [])
        min_bars = {"15": 30, "60": 50, "240": 50, "1D": 30}.get(resolution, 50)
        if len(cached) < min_bars:
            # Not enough bars yet — try fetch, but don't block strategy if it fails
            try:
                candles = self._fetch_window(symbol, resolution, count)
                self._last_fetch[key] = float(now)
            except Exception as e:
                log.warning("candle fetch insufficient bars %s %s: %s", symbol, resolution, e)
                self._fetch_connected[key] = False
                self._fetch_err[key] = str(e)[:300]
                return cached
        # (B) time-stagger: slow timeframes refetch only when a new bar is due.
        #     Between their natural close intervals the cached candles do not
        #     change, so skipping the network call is safe.
        stagger_sec = {"15": 0, "60": 0, "240": 4 * 3600, "1D": 86400}[resolution]
        last_fetch = self._last_fetch.get(key, 0.0)
        if cached and stagger_sec and (now - last_fetch) < stagger_sec:
            return cached  # not due yet — no request, serve cache

        # Exponential backoff on repeated failures: 15s → 30s → 60s → max 300s
        backoff_until = self._fetch_backoff.get(key, 0.0)
        if now < backoff_until:
            log.debug("candle fetch backoff active %s %s (waiting %ds)", symbol, resolution, int(backoff_until - now))
            return cached

        # SMART incremental with full-window fallback (reliability gated):
        # delta-first, correctness-checked, fallback refetch on any doubt.
        try:
            candles = self._fetch_window(symbol, resolution, count)
            self._last_fetch[key] = float(now)
            # Reset backoff on success
            self._fetch_backoff.pop(key, None)
            self._fetch_err.pop(key, None)
        except Exception as e:
            log.error("candle fetch failed %s %s: %s", symbol, resolution, e)
            self._fetch_connected[key] = False
            self._fetch_err[key] = str(e)[:300]   # surfaced by /api/candles
            # Exponential backoff: 15, 30, 60, 120, 300 seconds
            backoff_levels = [15, 30, 60, 120, 300]
            current_backoff = self._fetch_backoff.get(key, 15.0)
            next_idx = min(backoff_levels.index(current_backoff) + 1, len(backoff_levels) - 1) if current_backoff in backoff_levels else 0
            self._fetch_backoff[key] = float(backoff_levels[next_idx])
            stale = self._candle_cache.get(key, [])
            if self.file_logger:
                try:
                    self.file_logger.write("events", {
                        "ts": int(time.time()),
                        "kind": "candle_fetch_failed",
                        "symbol": symbol,
                        "resolution": resolution,
                        "error": str(e)[:200],
                        "stale_cache_count": len(stale),
                        "backoff_sec": self._fetch_backoff[key],
                    })
                except Exception as e:
                    log.debug(f"engine: suppressed {type(e).__name__}: {e} | ctx: {ctx[:80]}")
            return stale

        # ── shared pipeline: aggregate → drop open → merge → persist ──
        candles = self._merge_fetched(symbol, resolution, candles)
        return candles

    def _merge_fetched(self, symbol: str, resolution: str, candles: List[Candle]) -> List[Candle]:
        """Shared post-fetch pipeline: aggregate-if-needed, drop open candle,
        merge onto cache, persist to disk. Returns the updated cache."""
        now = int(time.time())
        res_sec = {"15": 900, "60": 3600, "240": 14400, "1D": 86400}[resolution]
        key = f"{symbol}:{resolution}"
        cached = self._candle_cache.get(key, [])
        # Wallex granularity fix (res=240 returns 1h bars)
        if resolution == "240" and candles:
            raw_interval = candles[1].ts - candles[0].ts if len(candles) >= 2 else res_sec
            if raw_interval <= 2 * 3600:
                log.info("[raw-aggregate] symbol=%s resolution=%s agg_from=%d bars=%d first=%d last=%d",
                         symbol, resolution, raw_interval, len(candles), candles[0].ts, candles[-1].ts)
                candles = self._aggregate_buckets(candles, 4 * 3600)
        # drop the still-open last candle: only CLOSED candles feed the strategy
        if candles:
            last_open_time = candles[-1].ts
            if now - last_open_time < res_sec:
                self._open_candle[key] = candles[-1]
                candles = candles[:-1]
            else:
                self._open_candle.pop(key, None)
        # merge delta onto cache (dedupe by ts). Depth preservation: keep at
        # least as many bars as the cache already holds, capped — a chart
        # fetch (limit=500) must NEVER shrink a deep history cache.
        merged: Dict[int, Candle] = {c.ts: c for c in cached}
        for c in candles:
            merged[c.ts] = c
        keep_floor = len(cached)
        keep = max(
            keep_floor,                                   # never shrink below current depth
            210 if resolution == "240" else (48 if resolution == "1D" else 310),
        )
        # cap: growth cap for the CHART window, but a deep backfill (15m ≈ 11.5k
        # bars / 1D ≈ 120) must be preserved — cap scales with current floor.
        growth_cap = 13000 if resolution in ("15", "1D") else 3000
        keep = min(keep, max(growth_cap, keep_floor))
        result = sorted(merged.values(), key=lambda x: x.ts)[-keep:]
        if result:
            self._last_closed_ts[key] = result[-1].ts
        self._candle_cache[key] = result
        self._save_candle_disk(symbol, resolution, result)
        return result

    # ── per-symbol tick ────────────────────────────────────────────
    def process_symbol(self, symbol: str) -> None:
        scfg = self.cfg["strategy"]
        c4 = self._fetch_candles(symbol, TF_TREND, 200)
        c1 = self._fetch_candles(symbol, TF_ENTRY, 300)
        c15 = self._fetch_candles(symbol, TF_FILTER, 200)
        # 210 daily bars: deep enough for long-window indicators (sma/ema 100+,
        # donchian 100, rsi/stoch warmup) when a 1D external strategy evaluates.
        # Still ONE fetch per day (1D stagger) — the depth is free.
        c1d = self._fetch_candles(symbol, TF_GRID, 210)
        if not c1 or not c4:
            return

        # Minimum bar count check: skip signal generation if insufficient data
        if len(c1) < 50 or len(c4) < 50:
            log.warning("insufficient bars %s: c1=%d c4=%d — skipping signal generation", symbol, len(c1), len(c4))
            if self.file_logger:
                try:
                    self.file_logger.write("decisions", {
                        "ts": int(time.time()),
                        "kind": "insufficient_bars",
                        "symbol": symbol,
                        "c1_count": len(c1),
                        "c4_count": len(c4),
                        "detail": "minimum bar count not met",
                    })
                except Exception as e:
                    log.debug(f"engine: suppressed {type(e).__name__}: {e} | ctx: {ctx[:80]}")
            return

        price = c1[-1].c
        if isinstance(self.broker, (PaperBroker, PaperMarginBroker, LiveBroker)):
            self.broker.set_price(symbol, price)

        # intra-candle liquidation check (Wallex monitors continuously, we only
        # tick every 15m — use the just-closed 15m candle's LOW/HIGH so wick
        # touches of the liq price are not missed)
        if isinstance(self.broker, PaperMarginBroker) and self.candle_cache_has(symbol):
            c15_last = self._candle_cache.get(f"{symbol}:15", [])
            if c15_last:
                last15 = c15_last[-1]
                for pos, reason in self.broker.check_price_extremes(symbol, last15.l, last15.h):
                    self.positions.pop(pos.id, None)
                    pos.realized_rr = self._realized_rr(pos)
                    self.storage.save_trade(pos)
                    self.storage.log_event(int(time.time()), "exit", symbol,
                                           f"side={pos.side} reason={reason} pnl={pos.pnl:.4f} (intra-candle)")

        # grid (capital split only — never a signal)
        grid_active, grid_levels, support, resistance = gridmod.build_grid(c1d, self.cfg)
        try:
            self.storage.save_grid_snapshot(symbol, support or 0.0, resistance or 0.0,
                                            list(grid_levels or []),
                                            self.snapshots.get(symbol, {}).__dict__ if hasattr(self.snapshots.get(symbol), "__dict__") else {})
        except Exception as e:
            log.debug(f"engine: suppressed {type(e).__name__}: {e} | ctx: {ctx[:80]}")

        # manage open positions for this symbol FIRST
        self._manage_positions(symbol, c1, c4)

        # manual orders: pending triggers + TP/SL on 15m extremes (wick-aware)
        try:
            if c15:
                self.manual.check(symbol, c15[-1].l, c15[-1].h, price)
        except Exception as e:
            log.warning("manual check %s failed: %s", symbol, e)

        # signal — margin paper also evaluates SHORT setups
        sig = build_signal(symbol, c4, c1, c15, self.cfg)
        eligible = bool(sig and getattr(sig, "eligible", False))
        if self.margin_mode and not eligible:
            sig_s = build_signal_short(symbol, c4, c1, c15, self.cfg)
            if sig_s and getattr(sig_s, "eligible", False):
                sig, eligible = sig_s, True

        # external AI strategy signals — evaluated alongside legacy
        ext_sig = self._evaluate_external_strategies(symbol, c4, c1, c15, price, c1d=c1d)
        if ext_sig:
            gating = (self.cfg.get("signal_gating") or {})
            bias = str(gating.get("external_bias", "neutral")).lower()
            # FIX(#2): gating must gate. The Signal eligibility was already
            # decided (met confirmations vs min_confirmations). We apply the
            # bias by REQUIRING extra/relaxing confirmations:
            #   advance → require min_confirmations + N (stricter, acts earlier
            #             in trend is NOT possible post-hoc; stricter it is)
            #   delay   → require -N confirmations (looser)
            # implemented by re-checking the artifact's condition count.
            def _bias_gate(s: "Signal", strict_add: int) -> "Signal":
                if strict_add <= 0:
                    return s
                strat = self.active_external_strategy or {}
                needed = int(strat.get("min_confirmations", 1)) + strict_add
                cond_count = len(strat.get("entry_conditions", []) or [])
                if cond_count and needed > cond_count:
                    needed = cond_count
                # approximate re-gate: met-conditions are encoded in score
                # (score = min(8, met*2)) → met = score // 2
                met = max(1, int(getattr(s, "score", 0)) // 2)
                if met < needed:
                    return None
                return s
            if bias == "advance":
                pct = float(gating.get("external_advance_pct", 5))
                add = 1 if pct > 0 else 0
                ext_sig = _bias_gate(ext_sig, add)
            elif bias == "delay":
                pct = float(gating.get("external_delay_pct", 10))
                if pct <= 0:
                    ext_sig = None
            if ext_sig is not None:
                if eligible and sig is not None:
                    # both legacy and external agree → external wins (bias may
                    # have relaxed it); log agreement for visibility
                    sig, eligible = ext_sig, True
                else:
                    sig, eligible = ext_sig, True

        snap = SymbolSnapshot(
            symbol=symbol, price=price,
            trend="", structure_event="",
            support=support, resistance=resistance,
            grid_active=grid_active, grid_levels=grid_levels,
            signal=sig, eligible=eligible,
            score=sig.score if sig else 0, updated_ts=int(time.time()),
        )
        if sig:
            from .structure import analyze_structure
            st = analyze_structure(c4, scfg.get("swing_left", 2), scfg.get("swing_right", 2))
            snap.trend = st.trend.value
            snap.structure_event = st.last_event or ""
            from . import indicators
            rsi_h1 = indicators.rsi([c.c for c in c1], int(scfg.get("rsi_period", 14)))[-1]
            atr_h1 = indicators.atr(c1, int(scfg.get("atr_period", 14)))[-1]
            snap.rsi_h1 = rsi_h1
            snap.atr_h1 = atr_h1

        self.snapshots[symbol] = snap

        # entry
        if eligible and sig is not None:
            self._try_enter(sig, grid_active, grid_levels)
        else:
            if self.file_logger and sig is None:
                try:
                    self.file_logger.write("decisions", {
                        "ts": int(time.time()),
                        "kind": "no_signal",
                        "symbol": symbol,
                        "price": price,
                        "detail": "build_signal returned None",
                    })
                except Exception as e:
                    log.debug(f"engine: suppressed {type(e).__name__}: {e} | ctx: {ctx[:80]}")
            elif self.file_logger and not eligible:
                try:
                    self.file_logger.write("decisions", {
                        "ts": int(time.time()),
                        "kind": "ineffective",
                        "symbol": symbol,
                        "price": price,
                        "score": getattr(sig, "score", 0),
                        "pattern": getattr(sig, "pattern", ""),
                        "confirmations": [
                            {"key": c.key, "label": c.label_fa, "ok": c.ok, "detail": c.detail}
                            for c in getattr(sig, "confirmations", [])
                        ],
                        "detail": "signal not eligible",
                    })
                except Exception as e:
                    log.debug(f"engine: suppressed {type(e).__name__}: {e} | ctx: {ctx[:80]}")

    def _evaluate_external_strategies(self, symbol: str, c4: List[Candle], c1: List[Candle], c15: List[Candle], price: float, c1d: Optional[List[Candle]] = None) -> Optional[Signal]:
        """Evaluate external AI strategies for this symbol.

        FIX(#1) one-active enforcement at SIGNAL TIME: only the ACTIVE strategy
        is evaluated (active_external_strategy). When legacy is active this is
        None and no external signal is produced at all — activating a strategy
        in the dashboard now truly changes what trades.
        """
        # legacy active → external evaluation off
        if self.active_external_strategy is None:
            return None
        if not self.external_strategies:
            return None

        # tf_map covers ALL schema-allowed TFs; c1d (1D) is fetched in
        # process_symbol for the grid and now also feeds 1D external strategies.
        tf_map = {"240": c4, "60": c1, "15": c15, "1D": c1d}
        strat = self.active_external_strategy
        if not strat.get("enabled", False):
            return None
        tf = str(strat.get("timeframe", "60"))
        candles = tf_map.get(tf)
        if not candles or len(candles) < 20:
            return None

        try:
            sig = self._evaluate_single_strategy(strat, symbol, candles, price)
            if sig:
                return sig
        except Exception as exc:
            log.warning("external strategy eval failed %s: %s", strat.get("strategy_id"), exc)
        return None
    
    @staticmethod
    def _quote_of(symbol: str) -> str:
        s = (symbol or "").upper()
        if s.endswith("TMN"):
            return "TMN"
        if s.endswith("USDT"):
            return "USDT"
        return ""

    def _evaluate_single_strategy(self, strat: dict, symbol: str, candles: List[Candle], price: float) -> Optional[Signal]:
        """Evaluate one external strategy artifact against candle data.

        Uses the shared strategy_eval dispatcher — identical math to the
        backtest, so live and backtest can never diverge.
        """
        from . import indicators
        from .strategy_eval import evaluate_conditions

        entry_conds = strat.get("entry_conditions", [])
        if not entry_conds:
            return None

        # cooldown check
        cooldown = int(strat.get("cooldown_bars", 3))
        cache_key = f"{strat.get('strategy_id')}:{symbol}"
        cached = self._external_signal_cache.get(cache_key, [])
        if cached and len(candles) - cached[-1][0] < cooldown:
            return None

        met, total = evaluate_conditions(entry_conds, candles)

        required = int(strat.get("min_confirmations", 1))
        if met < required:
            return None

        # build signal with execution_mode-aware TP/SL
        atr = indicators.atr(candles, 14)
        atr_val = atr[-1] if atr and atr[-1] else 0.0
        execution_mode = str(strat.get("execution_mode", "auto")).lower()
        risk_cfg = strat.get("risk", {}) or {}
        stop_atr_mult = float(risk_cfg.get("stop_atr_mult", 1.5))
        target_atr_mult = float(risk_cfg.get("target_atr_mult", 3.0))
        dollar_tp = float(risk_cfg.get("dollar_tp", 0) or 0)
        dollar_stop = float(risk_cfg.get("dollar_stop", 0) or 0)
        tmn_tp = float(risk_cfg.get("tmn_tp", 0) or 0)
        tmn_stop = float(risk_cfg.get("tmn_stop", 0) or 0)

        # Auto-detect pair quote family and use matching TP/SL fields directly
        quote = self._quote_of(symbol)
        if execution_mode == "tp_sl_dollar":
            if quote == "TMN" and tmn_tp > 0:
                target = price + tmn_tp
            elif quote == "USDT" and dollar_tp > 0:
                target = price + dollar_tp
            else:
                target = price + atr_val * target_atr_mult if price > 0 else 0
            if quote == "TMN" and tmn_stop > 0:
                stop = price - tmn_stop
            elif quote == "USDT" and dollar_stop > 0:
                stop = price - dollar_stop
            else:
                stop = price - atr_val * stop_atr_mult if price > 0 else 0
        else:
            stop = price - atr_val * stop_atr_mult if price > 0 else 0
            target = price + atr_val * target_atr_mult if price > 0 else 0

        rr = (target - price) / (price - stop) if (price - stop) > 1e-9 else 0.0

        sig = Signal(
            symbol=symbol,
            ts=int(candles[-1].ts),
            direction="long",
            entry=price,
            stop=stop,
            target=target,
            rr=rr,
            score=min(8, met * 2),
            pattern="external",
            atr=atr_val,
        )

        # update cache
        if cache_key not in self._external_signal_cache:
            self._external_signal_cache[cache_key] = []
        self._external_signal_cache[cache_key].append((len(candles), sig.ts))
        if len(self._external_signal_cache[cache_key]) > 20:
            self._external_signal_cache[cache_key] = self._external_signal_cache[cache_key][-20:]

        return sig
    
    def _condition_met(self, current: float, prev: float, op: str, val: float) -> bool:
        """Evaluate a single condition against current and previous indicator values."""
        if op == ">":
            return current > val
        elif op == "<":
            return current < val
        elif op == ">=":
            return current >= val
        elif op == "<=":
            return current <= val
        elif op == "==":
            return abs(current - val) < 1e-9
        elif op == "crossover":
            return prev < val and current >= val
        elif op == "crossunder":
            return prev > val and current <= val
        elif op == "increase":
            return current > prev
        elif op == "decrease":
            return current < prev
        return False

    def _try_enter(self, sig, grid_active: bool, grid_levels: List[float]) -> None:
        symbol = sig.symbol
        if any(p.symbol == symbol and p.is_open for p in self.positions.values()):
            return  # one position per symbol
        equity = self.broker.equity()
        dd = self._drawdown_pct(equity)
        gf = gridmod.grid_size_factor(grid_active, sig.entry, grid_levels, self.cfg)
        open_pos = [p for p in self.positions.values() if p.is_open]
        rc = float(self.cfg.get("margin", {}).get("risk_coef", 2.0)) if self.margin_mode else 1.0
        sizing = self.risk.size_position(equity, sig.entry, sig.stop, open_pos,
                                         grid_factor=gf, drawdown_pct=dd, risk_coef=rc)
        if not sizing.allowed:
            self.storage.log_event(sig.ts, "entry_blocked", symbol, sizing.reason)
            if self.file_logger:
                try:
                    self.file_logger.write("decisions", {
                        "ts": int(sig.ts),
                        "kind": "entry_blocked",
                        "symbol": symbol,
                        "price": sig.entry,
                        "stop": sig.stop,
                        "score": getattr(sig, "score", 0),
                        "pattern": getattr(sig, "pattern", ""),
                        "reason": sizing.reason,
                        "drawdown_pct": dd,
                    })
                except Exception as e:
                    log.debug(f"engine: suppressed {type(e).__name__}: {e} | ctx: {ctx[:80]}")
            return

        pos = Position(
            id=uuid.uuid4().hex[:12], symbol=symbol, qty=sizing.qty,
            entry=sig.entry, stop=sig.stop, opened_ts=sig.ts,
            signal_score=sig.score, atr_at_entry=sig.atr,
            peak_price=sig.entry, initial_qty=sizing.qty,
        )
        direction = getattr(sig, "direction", "long")
        pos.side = direction
        pos.initial_stop = sig.stop  # audit-fix: entry-time risk
        # Paper/margin simulation needs risk_coef on the position; spot must stay 1x.
        if self.margin_mode:
            pos.risk_coef = float(self.cfg.get("margin", {}).get("risk_coef", 2.0))
        else:
            pos.risk_coef = 1.0
        if direction == "short":
            ok = self.broker.open_short(symbol, sizing.qty, sig.entry, pos)
        else:
            ok = self.broker.open_long(symbol, sizing.qty, sig.entry, pos)
        if ok:
            self.positions[pos.id] = pos
            self.storage.log_event(sig.ts, "entry", symbol,
                                   f"side={direction} qty={pos.qty:.6f} entry={pos.entry:.6f} stop={pos.stop:.6f} "
                                   f"score={sig.score}/8 pattern={sig.pattern} rr={sig.rr:.2f}")
            self.storage.save_trade(pos)
            self.storage.save_audit("entry", symbol, {
                "id": pos.id,
                "side": direction,
                "qty": round(pos.qty, 6),
                "entry": round(pos.entry, 6),
                "stop": round(pos.stop, 6),
                "score": sig.score,
                "pattern": sig.pattern,
                "rr": round(sig.rr, 2),
                "atr": round(sig.atr, 2),
                "risk_coef": round(getattr(pos, "risk_coef", 1.0), 2),
                "sizing": {
                    "qty": round(sizing.qty, 6),
                    "notional": round(getattr(sizing, "notional", 0.0), 4),
                    "size_factor": getattr(sizing, "size_factor", 1.0),
                },
                "confirmations": [
                    {"key": c.key, "label": c.label_fa, "ok": c.ok, "detail": c.detail}
                    for c in getattr(sig, "confirmations", [])
                ],
            })
            if self.file_logger:
                try:
                    self.file_logger.write("decisions", {
                        "ts": int(sig.ts),
                        "kind": "entry",
                        "symbol": symbol,
                        "side": direction,
                        "qty": round(pos.qty, 6),
                        "entry": round(pos.entry, 6),
                        "stop": round(pos.stop, 6),
                        "score": sig.score,
                        "pattern": sig.pattern,
                        "rr": round(sig.rr, 2),
                        "atr": round(sig.atr, 2),
                        "risk_coef": round(getattr(pos, "risk_coef", 1.0), 2),
                        "grid_active": grid_active,
                    })
                    self.file_logger.write("open_positions", {
                        "ts": int(sig.ts),
                        "kind": "opened",
                        "id": pos.id,
                        "symbol": symbol,
                        "side": direction,
                        "qty": round(pos.qty, 6),
                        "entry": round(pos.entry, 6),
                        "stop": round(pos.stop, 6),
                        "risk_coef": round(getattr(pos, "risk_coef", 1.0), 2),
                    })
                except Exception as e:
                    log.debug(f"engine: suppressed {type(e).__name__}: {e} | ctx: {ctx[:80]}")

    def _manage_positions(self, symbol: str, c1: List[Candle], c4: List[Candle]) -> None:
        from . import indicators
        scfg = self.cfg["strategy"]
        atr_series = indicators.atr(c1, int(scfg.get("atr_period", 14)))
        atr_now = atr_series[-1] if atr_series and atr_series[-1] else 0.0
        price = c1[-1].c

        for pos in list(self.positions.values()):
            if pos.symbol != symbol or not pos.is_open:
                continue

            is_short = getattr(pos, "side", "long") == "short"

            # 1) stop / trailing stop hit (long: candle low, short: candle high)
            exit_reason = self.risk.check_stop(pos, c1[-1].l, price, high_price=c1[-1].h)

            # 2) structure flip on 4h -> exit (bearish CHoCH for longs, bullish CHoCH for shorts)
            if exit_reason is None:
                if is_short:
                    if bullish_choch_recent(c4, scfg.get("swing_left", 2), scfg.get("swing_right", 2)):
                        exit_reason = ExitReason.CHOCH.value
                else:
                    if bearish_choch_recent(c4, scfg.get("swing_left", 2), scfg.get("swing_right", 2)):
                        exit_reason = ExitReason.CHOCH.value

            # 3) level confirmation against the position -> exit
            if exit_reason is None:
                if is_short:
                    exit_reason = self._bullish_level_confirm(symbol, c1, price, atr_now)
                else:
                    exit_reason = self._bearish_level_confirm(symbol, c1, price, atr_now)

            if exit_reason:
                if is_short:
                    self.broker.close_short(pos, price, reason=exit_reason)
                else:
                    self.broker.close_long(pos, price, reason=exit_reason)
                pos.realized_rr = self._realized_rr(pos)
                self.storage.save_trade(pos)
                self.storage.log_event(int(time.time()), "exit", symbol,
                                       f"side={pos.side} reason={exit_reason} pnl={pos.pnl:.4f} rr={pos.realized_rr:.2f}")
                if self.file_logger:
                    try:
                        self.file_logger.write("decisions", {
                            "ts": int(time.time()),
                            "kind": "exit",
                            "symbol": symbol,
                            "side": pos.side,
                            "reason": exit_reason,
                            "entry": round(pos.entry, 6),
                            "close_price": round(getattr(pos, "close_price", price), 6),
                            "qty": round(pos.qty, 6),
                            "pnl": round(pos.pnl, 6),
                            "rr": round(pos.realized_rr, 2),
                            "fees_paid": round(pos.fees_paid, 6),
                        })
                        self.file_logger.write("closed_positions", {
                            "ts": int(time.time()),
                            "id": pos.id,
                            "symbol": symbol,
                            "side": pos.side,
                            "entry": round(pos.entry, 6),
                            "close_price": round(getattr(pos, "close_price", price), 6),
                            "qty": round(pos.qty, 6),
                            "pnl": round(pos.pnl, 6),
                            "rr": round(pos.realized_rr, 2),
                            "exit_reason": exit_reason,
                            "fees_paid": round(pos.fees_paid, 6),
                            "opened_ts": pos.opened_ts,
                            "closed_ts": pos.closed_ts,
                            "hold_seconds": (pos.closed_ts or 0) - pos.opened_ts,
                            "risk_coef": round(getattr(pos, "risk_coef", 1.0), 2),
                        })
                    except Exception as e:
                        log.debug(f"engine: suppressed {type(e).__name__}: {e} | ctx: {ctx[:80]}")
                continue

            # 4) BE / trailing / partial management (direction-aware inside risk manager)
            actions = self.risk.manage(pos, price, atr_now)
            if actions["partial"] and actions["partial_qty"] > 0:
                if is_short:
                    self.broker.close_short(pos, price, qty=actions["partial_qty"],
                                            reason=ExitReason.PARTIAL.value)
                else:
                    self.broker.close_long(pos, price, qty=actions["partial_qty"],
                                           reason=ExitReason.PARTIAL.value)
                self.storage.log_event(int(time.time()), "partial", symbol,
                                       f"closed {actions['partial_qty']:.6f} at RR={self.cfg['risk'].get('partial_rr', 2.5)}")
                if self.file_logger:
                    try:
                        self.file_logger.write("decisions", {
                            "ts": int(time.time()),
                            "kind": "partial",
                            "symbol": symbol,
                            "side": pos.side,
                            "qty_closed": round(actions["partial_qty"], 6),
                            "price": round(price, 6),
                            "entry": round(pos.entry, 6),
                            "pnl": round(pos.pnl, 6),
                            "rr": round(self._realized_rr(pos), 2),
                        })
                        self.file_logger.write("open_positions", {
                            "ts": int(time.time()),
                            "kind": "partial",
                            "id": pos.id,
                            "symbol": symbol,
                            "side": pos.side,
                            "qty": round(pos.qty, 6),
                            "entry": round(pos.entry, 6),
                            "stop": round(pos.stop, 6),
                            "risk_coef": round(getattr(pos, "risk_coef", 1.0), 2),
                        })
                    except Exception as e:
                        log.debug(f"engine: suppressed {type(e).__name__}: {e} | ctx: {ctx[:80]}")
            if actions["breakeven"]:
                self.storage.log_event(int(time.time()), "breakeven", symbol, "stop -> entry")
                if self.file_logger:
                    try:
                        self.file_logger.write("decisions", {
                            "ts": int(time.time()),
                            "kind": "breakeven",
                            "symbol": symbol,
                            "side": pos.side,
                            "entry": round(pos.entry, 6),
                            "stop": round(pos.stop, 6),
                            "peak_price": round(pos.peak_price, 6),
                        })
                    except Exception as e:
                        log.debug(f"engine: suppressed {type(e).__name__}: {e} | ctx: {ctx[:80]}")
            self.storage.save_trade(pos)

    def _bearish_level_confirm(self, symbol: str, c1: List[Candle], price: float, atr_now: float) -> Optional[str]:
        """Exit if the last closed 1h candle is bearish AND closes at/under a
        resistance / bearish OB / FVG zone (rejection confirmation)."""
        from . import levels as lvl
        if atr_now <= 0 or not c1 or not c1[-1].is_bear:
            return None
        sr = lvl.find_sr_levels(c1, atr_now, int(self.cfg["strategy"].get("sr_lookback", 120)))
        res = lvl.nearest_resistance(sr, price * 1.02)
        if res is not None and abs(price - res) <= atr_now * 0.5:
            return ExitReason.LEVEL_CONFIRM.value
        return None

    def _bullish_level_confirm(self, symbol: str, c1: List[Candle], price: float, atr_now: float) -> Optional[str]:
        """Exit SHORTS if the last closed 1h candle is bullish AND closes at/above a
        support / bullish OB / FVG zone (rejection confirmation)."""
        from . import levels as lvl
        if atr_now <= 0 or not c1 or not c1[-1].is_bull:
            return None
        sr = lvl.find_sr_levels(c1, atr_now, int(self.cfg["strategy"].get("sr_lookback", 120)))
        sup = lvl.nearest_support(sr, price * 0.98)
        if sup is not None and abs(price - sup) <= atr_now * 0.5:
            return ExitReason.LEVEL_CONFIRM.value
        return None

    def _realized_rr(self, pos: Position) -> float:
        # FIX(audit-H7): entry-time stop, not the (possibly trailed) current one
        _stop0 = getattr(pos, "initial_stop", 0.0) or pos.stop
        risk0 = abs(pos.entry - _stop0) if _stop0 != pos.entry else pos.atr_at_entry
        if risk0 <= 0:
            return 0.0
        exit_px = pos.close_price or pos.entry
        if getattr(pos, "side", "long") == "short":
            return (pos.entry - exit_px) / risk0
        return (exit_px - pos.entry) / risk0

    def _drawdown_pct(self, equity: float) -> float:
        # Decay peak if stuck at drawdown_stop level for >7 days without new high:
        # otherwise dd>=12% blocks entries forever even after recovery.
        # We slowly pull peak down toward equity when no new high for stale_peak_sec.
        now = int(time.time())
        if not hasattr(self, "_peak_ts"):
            self._peak_ts = now
        if equity > self.peak_equity:
            self.peak_equity = equity
            self._peak_ts = now
        else:
            stale_sec = now - getattr(self, "_peak_ts", now)
            # after 7 days stuck, decay peak 0.5% per day toward current equity
            if stale_sec > 7 * 86400:
                decay_days = (stale_sec - 7 * 86400) / 86400
                decay_factor = max(0.0, 1.0 - 0.005 * decay_days)
                # effective peak decays but never below equity
                effective_peak = max(equity, self.peak_equity * decay_factor)
                if effective_peak < self.peak_equity:
                    self.peak_equity = effective_peak
        if self.peak_equity <= 0:
            return 0.0
        return max(0.0, (self.peak_equity - equity) / self.peak_equity * 100.0)

    def reset_drawdown(self) -> None:
        """Manual recovery: reset peak to current equity (exposed via /api/risk/reset)."""
        self.peak_equity = self.broker.equity()
        self._peak_ts = int(time.time())
        self.storage.log_event(int(time.time()), "drawdown_reset", "", f"peak reset to {self.peak_equity:.2f}")

    # ── main loop ──────────────────────────────────────────────────
    def tick_once(self) -> None:
        # FIX(#4): non-blocking reentrancy guard — the run loop and a manual
        # /api/engine/tick must never interleave (double opens / cash corruption).
        # FIX(audit-M9): locked()+acquire is TOCTOU — the loser BLOCKED on
        # acquire and then ran a SECOND full tick. Non-blocking acquire:
        # exactly one tick runs, the loser skips.
        if not self._tick_lock.acquire(blocking=False):
            log.info("tick_once skipped: another tick is already running")
            return
        try:
            self._tick_once_inner()
        finally:
            self._tick_lock.release()

    def _tick_once_inner(self) -> None:
        self.broker.sync() if not self.connected else None
        # Refresh TMN/USDT quotes each tick and push to brokers so TMN pair equity/notional is not zero
        try:
            from .server import QuoteService as _QS  # avoid circular; quotes injected via client if available
        except Exception as e:
            log.debug(f"engine: suppressed {type(e).__name__}: {e} | ctx: {ctx[:80]}")
        # Ensure paper brokers have latest quote prices for TMN conversion
        try:
            if hasattr(self, "_quote_service") and self._quote_service:
                self._quote_service.refresh()
                for sym, px in self._quote_service.quotes.items():
                    if px > 0:
                        self.broker.set_price(sym, px)
        except Exception as e:
            log.debug(f"engine: suppressed {type(e).__name__}: {e} | ctx: {ctx[:80]}")
        # margin maintenance: interest accrual, liquidation, max-age expiry
        if isinstance(self.broker, PaperMarginBroker):
            for pos, reason in self.broker.tick_maintenance():
                self.positions.pop(pos.id, None)
                pos.realized_rr = self._realized_rr(pos)
                self.storage.save_trade(pos)
                self.storage.log_event(int(time.time()), "exit", pos.symbol,
                                       f"side={pos.side} reason={reason} pnl={pos.pnl:.4f}")
                if self.file_logger:
                    try:
                        self.file_logger.write("closed_positions", {
                            "ts": int(time.time()),
                            "id": pos.id,
                            "symbol": pos.symbol,
                            "side": getattr(pos, "side", "long"),
                            "entry": round(pos.entry, 6),
                            "close_price": round(getattr(pos, "close_price", 0), 6),
                            "qty": round(pos.qty, 6),
                            "pnl": round(pos.pnl, 6),
                            "rr": round(pos.realized_rr, 2) if pos.realized_rr is not None else None,
                            "exit_reason": reason,
                            "fees_paid": round(getattr(pos, "fees_paid", 0.0), 6),
                            "opened_ts": pos.opened_ts,
                            "closed_ts": pos.closed_ts,
                            "hold_seconds": (pos.closed_ts or 0) - pos.opened_ts,
                            "risk_coef": round(getattr(pos, "risk_coef", 1.0), 2),
                        })
                    except Exception as e:
                        log.debug(f"engine: suppressed {type(e).__name__}: {e} | ctx: {ctx[:80]}")
        for symbol in self.symbols:
            try:
                self.process_symbol(symbol)
            except Exception as e:
                log.exception("tick error %s: %s", symbol, e)
                self.storage.log_event(int(time.time()), "error", symbol, str(e)[:300])
        equity = self.broker.equity()
        dd = self._drawdown_pct(equity)
        pt = EquityPoint(ts=int(time.time()), equity=equity, drawdown_pct=dd)
        self.storage.save_equity(pt)
        # FIX(audit-M12): hourly DB prune — tables no longer grow forever
        try:
            if time.time() - getattr(self.storage, "_last_prune", 0.0) > 3600:
                self.storage._last_prune = time.time()
                self.storage.prune_old_rows()
        except Exception as e:
            log.debug(f"engine: suppressed {type(e).__name__}: {e} | ctx: {ctx[:80]}")
        # Dedup: only persist api_log entries not yet saved
        if not hasattr(self, "_api_log_idx"):
            self._api_log_idx = 0
        new_entries = self.client.api_log[self._api_log_idx:]
        if new_entries:
            self.storage.save_api_log(new_entries)
            self._api_log_idx = len(self.client.api_log)
            if self.file_logger:
                try:
                    self.file_logger.write("api", {
                        "ts": int(time.time()),
                        "entries": [
                            {
                                "ts": round(e.ts, 3),
                                "method": e.method,
                                "path": e.path,
                                "status": e.status,
                                "latency_ms": e.latency_ms,
                                "retries": e.retries,
                                "error": e.error,
                            }
                            for e in new_entries
                        ],
                    })
                except Exception as e:
                    log.debug(f"engine: suppressed {type(e).__name__}: {e} | ctx: {ctx[:80]}")
        if self.file_logger:
            try:
                self.file_logger.write("balance", {
                    "ts": int(time.time()),
                    "kind": "tick_balance",
                    "cash": getattr(self.broker, "cash", None),
                    "equity": equity,
                    "drawdown_pct": dd,
                    "starting_capital": getattr(self.broker, "starting_capital", None),
                    "mode": self.broker.name,
                })
                opps = []
                for sym, snap in self.snapshots.items():
                    sig = snap.signal
                    opps.append({
                        "symbol": sym,
                        "price": snap.price,
                        "eligible": snap.eligible,
                        "score": snap.score,
                        "entry": sig.entry if sig else None,
                        "stop": sig.stop if sig else None,
                        "target": sig.target if sig else None,
                        "rr": sig.rr if sig else None,
                        "pattern": sig.pattern if sig else "",
                        "direction": getattr(sig, "direction", "long") if sig else "",
                        "trend": snap.trend,
                        "support": snap.support,
                        "resistance": snap.resistance,
                        "grid_active": snap.grid_active,
                        "confirmations": [
                            {"key": c.key, "label": c.label_fa, "ok": c.ok, "detail": c.detail}
                            for c in (sig.confirmations if sig else [])
                        ],
                        "updated_ts": snap.updated_ts,
                    })
                opps.sort(key=lambda x: (-int(x["eligible"]), -x["score"]))
                self.file_logger.write("opportunities", {"ts": int(time.time()), "opportunities": opps})
                self._persist_opportunities(opps)
                open_positions = []
                for p in self.positions.values():
                    if not p.is_open:
                        continue
                    cur_px = self.broker.last_price(p.symbol) or getattr(p, "entry", 0.0)
                    is_short = getattr(p, "side", "long") == "short"
                    upnl = ((p.entry - cur_px) if is_short else (cur_px - p.entry)) * p.qty
                    open_positions.append({
                        "id": p.id,
                        "symbol": p.symbol,
                        "side": getattr(p, "side", "long"),
                        "qty": p.qty,
                        "entry": p.entry,
                        "stop": p.stop,
                        "target": getattr(p, "target", None),
                        "pnl": p.pnl,
                        "upnl": round(upnl, 6),
                        "risk_coef": getattr(p, "risk_coef", 1.0),
                        "liq_price": (getattr(p, "meta", {}) or {}).get("liq_price"),
                        "collateral": (getattr(p, "meta", {}) or {}).get("collateral"),
                        "interest_accrued": (getattr(p, "meta", {}) or {}).get("interest_accrued", 0.0),
                        "breakeven_done": p.breakeven_done,
                        "trailing_on": p.trailing_on,
                        "partial_taken": p.partial_taken,
                        "peak_price": p.peak_price,
                        "opened_ts": p.opened_ts,
                        "score": p.signal_score,
                    })
                self.file_logger.write("open_positions", {"ts": int(time.time()), "open_positions": open_positions})
            except Exception as e:
                log.warning("file_logger tick write failed: %s", e)
        self._persist_balance_kv()
        self.last_tick_ts = int(time.time())

    def _persist_balance_kv(self) -> None:
        """Persist the current paper cash back to the storage kv key so the
        balance survives a restart. Trade closes reduce broker.cash in memory; without
        writing it here, a restart re-reads the stale manually-set value and loses the
        realized P&L from closed positions.

        Live brokers do not have a paper cash balance, so this is intentionally
        paper-only. The misleading "could not persist paper balance" warning in
        live mode was caused by this method being called unconditionally.
        """
        try:
            b = self.broker
            if not isinstance(b, (PaperBroker, PaperMarginBroker)):
                return
            cur = (getattr(b, "quote_currency", None) or "USDT").upper()
            kind = "margin" if isinstance(b, PaperMarginBroker) else "spot"
            self.storage.kv_set(f"paper_balance_{kind}_{cur}", str(round(b.cash, 8)))
        except Exception as e:
            log.warning("could not persist paper balance: %s", e)

    def _opp_fingerprint(self) -> dict:
        b = self.broker
        return {
            "paper_kind": "margin" if isinstance(b, PaperMarginBroker) else "spot",
            "quote": (getattr(b, "quote_currency", None) or "USDT").upper(),
            "mode": self.broker.name,
            "starting_capital": round(getattr(b, "starting_capital", 0.0), 2),
        }

    def _persist_opportunities(self, opps: List[dict]) -> None:
        """Store the latest opportunities snapshot in SQLite so the 'فرصت‌های
        واجد شرایط' panel can be restored after a restart or brief disconnect."""
        try:
            payload = {
                "ts": int(time.time()),
                "cfg": self._opp_fingerprint(),
                "symbols": list(self.symbols),
                "opportunities": opps,
            }
            self.storage.engine_state_set("opportunities_snapshot",
                                          json.dumps(payload, ensure_ascii=False))
        except Exception as e:
            log.warning("could not persist opportunities: %s", e)

    def restore_opportunities(self, max_age_sec: int = 6 * 3600) -> int:
        """Rebuild in-memory snapshots from the last persisted snapshot, respecting
        gates so stale/removed/config-mismatched opportunities never surface:

        - freshness: only restore entries newer than max_age_sec (default 6h). This
          keeps 'فرصت واجد شرایط' from showing an opportunity captured long ago.
        - symbol gate: drop any symbol no longer in the engine's active list
          (user removed it from selection).
        - config gate: drop everything if paper kind / quote differ from the current
          engine config (user restarted with a different balance/mode/symbol set).
        Returns the number of restored snapshots.
        """
        try:
            raw = self.storage.engine_state_get("opportunities_snapshot")
            if not raw:
                return 0
            snap = json.loads(raw)
        except Exception as e:
            log.warning("opportunity restore load failed: %s", e)
            return 0
        if not isinstance(snap, dict):
            return 0
        now = int(time.time())
        # config gate: mismatched paper kind/quote => don't reuse (different run config)
        if snap.get("cfg") != self._opp_fingerprint():
            log.info("opportunity restore skipped: config fingerprint differs")
            return 0
        # freshness gate
        saved_ts = int(snap.get("ts") or 0)
        if not saved_ts or (now - saved_ts) > max_age_sec:
            log.info("opportunity restore skipped: snapshot too old (%ds)", now - saved_ts)
            return 0
        active = set(self.symbols)
        restored = 0
        for o in snap.get("opportunities", []):
            sym = o.get("symbol")
            if sym not in active:      # symbol gate — user removed it
                continue
            try:
                confs = [Confirmation(key=c.get("key", ""), label_fa=c.get("label", ""),
                                      ok=bool(c.get("ok")), detail=c.get("detail", ""))
                         for c in (o.get("confirmations") or [])]
                sig = None
                if o.get("entry") is not None:
                    sig = Signal(
                        symbol=sym,
                        ts=int(o.get("updated_ts") or saved_ts),
                        direction=o.get("direction") or "long",
                        entry=float(o.get("entry")),
                        stop=float(o.get("stop") or 0.0),
                        target=float(o.get("target") or 0.0),
                        rr=float(o.get("rr") or 0.0),
                        score=int(o.get("score") or 0),
                        confirmations=confs,
                        pattern=o.get("pattern") or "",
                    )
                snap_obj = SymbolSnapshot(
                    symbol=sym,
                    price=float(o.get("price") or 0.0),
                    trend=o.get("trend") or "range",
                    structure_event=o.get("structure_event") or "",
                    rsi_h1=o.get("rsi_h1"),
                    atr_h1=o.get("atr_h1"),
                    support=o.get("support"),
                    resistance=o.get("resistance"),
                    grid_active=bool(o.get("grid_active")),
                    grid_levels=o.get("grid_levels") or [],
                    signal=sig,
                    eligible=bool(o.get("eligible")),
                    score=int(o.get("score") or 0),
                    updated_ts=int(o.get("updated_ts") or saved_ts),
                )
                self.snapshots[sym] = snap_obj
                restored += 1
            except Exception as e:
                log.warning("opportunity restore skip %s: %s", sym, e)
        if restored:
            log.info("restored %d opportunity snapshot(s) from disk", restored)
        return restored

    def run(self) -> None:
        """Blocking loop: wait for each 15m candle close, then tick."""
        interval = int(self.cfg["engine"].get("scan_interval_minutes", 15)) * 60
        delay = int(self.cfg["engine"].get("candle_close_delay_sec", 10))
        self.running = True
        log.info("engine started (interval=%ds, mode=%s)", interval, self.broker.name)
        if self.file_logger:
            try:
                self.file_logger.write("events", {
                    "ts": int(time.time()),
                    "kind": "engine_started",
                    "symbol": "",
                    "detail": f"interval={interval}s mode={self.broker.name} paper={getattr(getattr(self.broker, 'quote_currency', ''), '', '')}",
                    "interval_seconds": interval,
                    "mode": self.broker.name,
                })
                bal = self.broker.equity()
                self.file_logger.write("balance", {
                    "ts": int(time.time()),
                    "kind": "start_balance",
                    "cash": getattr(self.broker, "cash", None),
                    "equity": bal,
                    "starting_capital": getattr(self.broker, "starting_capital", None),
                    "mode": self.broker.name,
                })
                self.file_logger.write("settings", {
                    "ts": int(time.time()),
                    "kind": "run_settings",
                    "scan_interval_seconds": interval,
                    "candle_close_delay_sec": int(self.cfg.get("engine", {}).get("candle_close_delay_sec", 10)),
                    "api_min_gap_sec": float(self.cfg.get("engine", {}).get("api_min_gap_sec", 12)),
                    "api_max_retries": int(self.cfg.get("engine", {}).get("api_max_retries", 3)),
                    "fee_pct": float(self.cfg.get("backtest", {}).get("fee_pct", 0.2)),
                    "slippage_pct": float(self.cfg.get("backtest", {}).get("slippage_pct", 0.05)),
                    "margin_risk_coef": float(self.cfg.get("margin", {}).get("risk_coef", 2.0)) if getattr(self, "margin_mode", False) else 1.0,
                    "margin_mmr_pct": float(self.cfg.get("margin", {}).get("mmr_pct", 1.0)),
                    "margin_interest_per_4h_pct": float(self.cfg.get("margin", {}).get("interest_per_4h_pct", 0.05)),
                    "margin_max_age_days": float(self.cfg.get("margin", {}).get("max_age_days", 21.0)),
                    "symbols": list(self.symbols),
                    "mode": self.broker.name,
                })
            except Exception as e:
                log.warning("file_logger startup write failed: %s", e)
        while self.running:
            now = time.time()
            next_boundary = (int(now) // interval + 1) * interval + delay
            sleep_s = next_boundary - now
            # sleep in small slices so stop() is responsive
            end = time.time() + sleep_s
            while self.running and time.time() < end:
                time.sleep(min(2.0, end - time.time()))
            if not self.running:
                break
            try:
                self.tick_once()
            except Exception as e:
                log.exception("tick failed: %s", e)

    def start(self) -> None:
        # set synchronously BEFORE returning so /api/engine/start reports
        # running=true even if the thread hasn't been scheduled yet (restart race)
        self.running = True
        if self._thread and self._thread.is_alive():
            return
        if self.file_logger:
            try:
                self.file_logger.write("events", {
                    "ts": int(time.time()),
                    "kind": "engine_start",
                    "symbol": "",
                    "detail": "start() called",
                    "mode": self.broker.name,
                })
            except Exception as e:
                log.debug(f"engine: suppressed {type(e).__name__}: {e} | ctx: {ctx[:80]}")
        # load cached candle data from disk for current symbols
        try:
            self.preload_symbols_from_disk()
        except Exception as e:
            log.warning("startup disk preload failed: %s", e)
        # restore last opportunity snapshot (gated: fresh + config-match + active symbols)
        try:
            self.restore_opportunities()
        except Exception as e:
            log.warning("opportunity restore on start failed: %s", e)
        self._thread = threading.Thread(target=self.run, daemon=True, name="engine")
        self._thread.start()

    def stop(self) -> None:
        if self.file_logger:
            try:
                closed = []
                for p in list(self.positions.values()):
                    if p.is_open:
                        closed.append({
                            "id": p.id,
                            "symbol": p.symbol,
                            "side": getattr(p, "side", "long"),
                            "qty": round(p.qty, 6),
                            "entry": round(p.entry, 6),
                            "pnl": round(p.pnl, 6),
                            "reason": "engine_stop",
                        })
                self.file_logger.write("closed_positions", {
                    "ts": int(time.time()),
                    "kind": "engine_stop_snapshot",
                    "closed_positions": closed,
                    "cash": getattr(self.broker, "cash", None),
                    "equity": self.broker.equity(),
                })
                self.file_logger.write("events", {
                    "ts": int(time.time()),
                    "kind": "engine_stop",
                    "symbol": "",
                    "detail": "stop() called",
                    "mode": self.broker.name,
                })
            except Exception as e:
                log.debug(f"engine: suppressed {type(e).__name__}: {e} | ctx: {ctx[:80]}")
        self.running = False

    # ── dashboard helpers ──────────────────────────────────────────
    def stats(self) -> dict:
        trades = self.storage.closed_trades(10000)
        wins = [t for t in trades if (t["pnl"] or 0) > 0]
        losses = [t for t in trades if (t["pnl"] or 0) <= 0]
        gross_win = sum(t["pnl"] for t in wins)
        gross_loss = abs(sum(t["pnl"] for t in losses))
        eq = self.storage.equity_series()
        max_dd = max((p["drawdown_pct"] for p in eq), default=0.0)
        hold = [t["hold_seconds"] for t in trades if t.get("hold_seconds")]
        return {
            "closed_trades": len(trades),
            "win_rate": (len(wins) / len(trades) * 100.0) if trades else 0.0,
            "profit_factor": (gross_win / gross_loss) if gross_loss > 0 else (99.0 if gross_win > 0 else 0.0),
            "max_drawdown_pct": max_dd,
            "total_pnl": sum(t["pnl"] or 0 for t in trades),
            "avg_hold_seconds": (sum(hold) / len(hold)) if hold else 0.0,
            "equity": self.broker.equity(),
            "equity_currency": getattr(self.broker, "quote_currency", None) or "USDT",
            "drawdown_pct": self._drawdown_pct(self.broker.equity()),
            "open_positions": len([p for p in self.positions.values() if p.is_open]),
            "mode": self.broker.name,
            "margin": self._margin_stats() if self.margin_mode else None,
        }

    def _margin_stats(self) -> dict:
        """Margin-paper specific metrics for the dashboard."""
        b = self.broker
        open_pos = [p for p in self.positions.values() if p.is_open]
        total_interest = sum((p.meta or {}).get("interest_accrued", 0.0) for p in open_pos)
        total_loan = sum((p.meta or {}).get("loan", 0.0) for p in open_pos)
        # projected 7d cost at current loan: loan * 0.05% * (24/4)*7
        daily_rate = float(self.cfg.get("margin", {}).get("interest_per_4h_pct", 0.05)) / 100.0 * 6
        projected_7d = total_loan * daily_rate * 7
        return {
            "liquidations": getattr(b, "liquidations", 0),
            "interest_paid_total": getattr(b, "interest_paid_total", 0.0),
            "interest_accrued_open": round(total_interest, 4),
            "total_loan": round(total_loan, 2),
            "projected_7d_interest": round(projected_7d, 4),
            "projected_7d_pct_of_equity": round(projected_7d / max(b.equity(), 1) * 100, 3) if b.equity() else 0.0,
            "open_longs": len([p for p in open_pos if getattr(p, "side", "long") == "long"]),
            "open_shorts": len([p for p in open_pos if getattr(p, "side", "long") == "short"]),
            "free_collateral": getattr(b, "cash", 0.0),
        }
