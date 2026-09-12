"""Download & cache historical candles from Wallex for backtesting.

Respects the client-side rate limit (>=12s between requests by default).
Data is cached as JSON under data/history/<symbol>/<resolution>.json so a
backtest never needs re-downloading.

PREFLIGHT (history sufficiency gate): before any backtest runs, ensure_depth()
verifies each symbol has ENOUGH history on disk in ALL 4 timeframes
(15m/1h/4h/1D) for the requested window. If not, it downloads the missing
depth first — so a backtest never silently runs on a shallow sample (the
root cause of "legacy always 0 trades").

WALLEX UDF GRANULARITY QUIRKS (verified by probe 2026-09-04):
  - res=15 returns 1-MINUTE bars (and only ~1 day of them per request)
  - res=5 / res=30 return s=error
  - res=240 returns 1-HOUR bars
  - res=1D works correctly
  Strategy: request the coarser reliable TF and aggregate up.
"""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Dict, List

from .backtest import RES_SEC, SymbolHistory, HistoryView
from .models import Candle
from .wallex_client import WallexClient

log = logging.getLogger("history")


def _cache_path(data_dir: str, symbol: str, resolution: str) -> Path:
    d = Path(data_dir) / "history" / symbol
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{resolution}.json"


def _load_cache(path: Path) -> List[Candle]:
    if not path.exists():
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        # truncated/corrupt cache (crash mid-write) must never brick the
        # backtest endpoints — return empty; download will heal the file.
        log.warning("history cache unreadable %s: %s", path, exc)
        return []
    try:
        return [Candle(ts=r[0], o=r[1], h=r[2], l=r[3], c=r[4], v=r[5]) for r in raw]
    except Exception as exc:
        log.warning("history cache malformed %s: %s", path, exc)
        return []


def _save_cache(path: Path, candles: List[Candle]) -> None:
    # atomic write: tmp + replace, so a crash mid-write can't leave a
    # truncated JSON that bricks _load_cache afterwards.
    import os
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps([[c.ts, c.o, c.h, c.l, c.c, c.v] for c in candles]), encoding="utf-8")
    os.replace(tmp, path)


def _aggregate(candles: List[Candle], bucket_sec: int, align_sec: int = 0) -> List[Candle]:
    """Aggregate candles into aligned buckets (o=first open, h=max, l=min, c=last close, v=sum)."""
    if not candles:
        return []
    buckets: Dict[int, List[Candle]] = {}
    for c in candles:
        if align_sec:
            bucket_ts = (c.ts // align_sec) * align_sec
        else:
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
            v=sum(getattr(x, "v", 0.0) for x in buf),
        ))
    return grouped


def _aggregate_1h_to_4h(candles: List[Candle]) -> List[Candle]:
    """Wallex returns 1h bars for resolution=240 — aggregate into UTC-aligned 4h buckets.

    Same math as the engine's _normalize_candles (00/04/08/12/16/20 UTC).
    Uses the FULL-series min spacing (a head-only check misses mixed
    1h+4h series where a raw-1h delta was merged into a 4h cache — the
    2026-09-05 LINKUSDT re-pollution)."""
    if len(candles) < 2:
        return candles
    if _candle_spacing(candles) > 2 * 3600:
        return candles  # already 4h or coarser across the WHOLE series
    return _aggregate(candles, 4 * 3600, align_sec=4 * 3600)


def _candle_spacing(candles: List[Candle]) -> int:
    """MINIMUM interior spacing across the WHOLE series (not just the first
    two bars). A poisoned cache can have clean 15m bars at the head and raw
    1m bars appended deeper in the series — sampling only [0],[1] then
    misses the contamination entirely (root cause of the 2026-09-05 mixed
    60s/900s 15m files). Full-series scan is microseconds even at 11.5k bars."""
    if len(candles) < 2:
        return 0
    m = candles[1].ts - candles[0].ts
    for i in range(1, len(candles) - 1):
        d = candles[i + 1].ts - candles[i].ts
        if 0 < d < m:
            m = d
    return m


def _normalize_for_res(res: str, candles: List[Candle]) -> List[Candle]:
    """Ensure cached data actually has the requested granularity (idempotent)."""
    if not candles:
        return candles
    exp = RES_SEC.get(res, 0)
    sp = _candle_spacing(candles)
    if res == "240":
        return _aggregate_1h_to_4h(candles)
    if exp and sp and sp < exp // 2:
        # finer than requested → aggregate up (15m file holding 1m bars, etc.)
        return _aggregate(candles, exp)
    return candles


def _fetch_ladder(client: WallexClient, symbol: str, res: str,
                  from_ts: int, to_ts: int) -> List[Candle]:
    """Fetch `res` candles, accounting for Wallex UDF quirks (verified 2026-09-04).

    15m: res=15 actually returns 1-MINUTE bars → aggregate to 15m buckets
    60m: res=60 direct
    240: res=240 returns 1h bars → _normalize_for_res aggregates to 4h
    1D : res=1D direct
    """
    if res == "15":
        raw = client.get_candles(symbol, "15", from_ts, to_ts)  # 1m bars
        return _aggregate(raw, 900)
    return client.get_candles(symbol, res, from_ts, to_ts)


def download_symbol(client: WallexClient, data_dir: str, symbol: str,
                    days: int = 120, resolutions=("15", "60", "240", "1D")) -> Dict[str, List[Candle]]:
    """Fetch `days` of history for each resolution, merging with cache.

    After download, each resolution is normalized to its true granularity
    (1h→4h aggregation, 1m→15m aggregation) BEFORE saving, so the disk cache
    always holds exactly what the label says.
    """
    out: Dict[str, List[Candle]] = {}
    now = int(time.time())
    _retries: Dict[str, int] = {}
    for res in resolutions:
        path = _cache_path(data_dir, symbol, res)
        cached = _load_cache(path)
        # normalize existing cache first (heal old wrong-granularity files)
        cached = _normalize_for_res(res, cached)
        need_from = now - days * 86400
        have_from = cached[0].ts if cached else None
        if have_from is not None and have_from <= need_from + RES_SEC[res] * 2:
            _save_cache(path, cached)  # persist healed cache
            out[res] = cached
            log.info("%s %s: cache hit (%d candles)", symbol, res, len(cached))
            continue
        from_ts = need_from if have_from is None else max(need_from, have_from - RES_SEC[res] * 10)
        to_ts = now
        # paginate. res=15 returns ~1 DAY of 1m bars per request (module header
        # quirk) — chunk it by ONE DAY, not 62 days, or the loop silently skips
        # ~61 days per chunk (multi-day holes in the cache).
        if res == "15":
            chunk_sec = 86400
        elif res == "60":
            chunk_sec = RES_SEC["60"] * 1500
        else:
            chunk_sec = RES_SEC[res] * 1500
        all_c: Dict[int, Candle] = {c.ts: c for c in cached}
        cursor = from_ts
        while cursor < to_ts:
            chunk_end = min(cursor + chunk_sec, to_ts)
            got = _fetch_ladder(client, symbol, res, cursor, chunk_end)
            for c in got:
                all_c[c.ts] = c
            if not got:
                # transient empty response: retry once, then SKIP FORWARD one
                # chunk instead of aborting the rest of the window (hole marker)
                if _retries.get(res, 0) < 2:
                    _retries[res] = _retries.get(res, 0) + 1
                    continue
                print(f"[download] {symbol} {res}: empty chunk at {cursor}, skipping forward", flush=True)
                cursor = min(cursor + chunk_sec, to_ts)
                _retries[res] = 0
                continue
            _retries[res] = 0
            # ADVANCE BY WHAT WAS ACTUALLY RETURNED (not by chunk_end): the
            # Wallex quirk means a chunk can return less than requested; the
            # next request must start where the data actually stopped.
            last_got = max(c.ts for c in got)
            nxt = max(last_got + RES_SEC[res], cursor + RES_SEC[res])
            if nxt >= to_ts or nxt <= cursor:
                break
            cursor = nxt
            if chunk_end >= to_ts:
                break
        merged = sorted(all_c.values(), key=lambda c: c.ts)
        merged = _normalize_for_res(res, merged)
        _save_cache(path, merged)
        out[res] = merged
        log.info("%s %s: downloaded %d candles", symbol, res, len(merged))
    return out


def depth_status(data_dir: str, symbols: List[str], days: int) -> Dict[str, dict]:
    """Check disk depth for each symbol across all 4 TFs WITHOUT downloading.

    Returns {symbol: {"ok": bool, "missing": {res: have_days}, "detail": str}}.
    A TF passes when its first cached candle covers the requested window
    (within 2 bars tolerance) AND its granularity matches the label.
    """
    now = int(time.time())
    need_from = now - days * 86400
    report: Dict[str, dict] = {}
    for sym in symbols:
        missing: Dict[str, float] = {}
        for res in ("15", "60", "240", "1D"):
            candles = _load_cache(_cache_path(data_dir, sym, res))
            if not candles:
                missing[res] = 0.0
                continue
            candles = _normalize_for_res(res, candles)
            sp = _candle_spacing(candles)
            exp = RES_SEC[res]
            if sp and sp < exp // 2:
                missing[res] = -1.0  # wrong granularity marker
                continue
            first_ts = candles[0].ts
            have_days = (now - first_ts) / 86400.0
            if first_ts > need_from + exp * 2:
                missing[res] = round(have_days, 1)
                continue
            # TAIL CHECK: the cache must also reach near the present. A cache
            # downloaded months ago passes the first-timestamp check forever —
            # backtests labeled "last N days" would silently run on stale data.
            if candles[-1].ts < now - 2 * exp:
                missing[res] = -2.0  # stale tail marker
        report[sym] = {
            "ok": not missing,
            "missing": missing,
            "detail": ("all 4 TFs sufficient" if not missing
                       else ", ".join(f"{r}:{d}" for r, d in sorted(missing.items()))),
        }
    return report


def ensure_depth(client: WallexClient, data_dir: str, symbols: List[str],
                 days: int = 120) -> dict:
    """PREFLIGHT: make sure every symbol has `days` of history in all 4 TFs.

    Downloads only what's missing, then re-verifies. Call before ANY backtest.
    Returns {"status": per-symbol depth_status AFTER top-up, "downloaded": [...]}.
    """
    before = depth_status(data_dir, symbols, days)
    needing = [s for s, r in before.items() if not r["ok"]]
    downloaded: List[str] = []
    if needing:
        log.info("preflight: topping up history for %d symbols (%s)",
                 len(needing), ", ".join(needing))
    for sym in needing:
        try:
            download_symbol(client, data_dir, sym, days=days)
            downloaded.append(sym)
        except Exception as e:
            log.warning("preflight top-up failed for %s: %s", sym, e)
    after = depth_status(data_dir, symbols, days)
    return {"status": after, "downloaded": downloaded}


def load_symbol_history(data_dir: str, symbol: str) -> SymbolHistory:
    def load(res: str) -> HistoryView:
        candles = _load_cache(_cache_path(data_dir, symbol, res))
        # normalize granularity on read too (idempotent; heals any legacy file)
        candles = _normalize_for_res(res, candles)
        return HistoryView(candles, RES_SEC[res])
    return SymbolHistory(h15=load("15"), h60=load("60"), h240=load("240"), h1d=load("1D"))
