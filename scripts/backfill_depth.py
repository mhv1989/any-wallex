"""Smart deep-history backfill (idle-aware).

Rebuilds missing depth in data/history/<SYM>/{60,240}.json WITHOUT re-downloading
what's already on disk:
  - incremental: only fetches the span between the oldest cached bar and the
    requested window start (the "back span"), then merges (old rows win on
    overlap; fresh closes win on the newest bar),
  - idle gate: pauses whenever the engine is RUNNING (chart/tick traffic has
    priority; backfill waits for idle), and
  - pacing: all requests go through WallexClient's built-in 12s throttle.

Usage:
  python scripts/backfill_depth.py            # top up to 120 days
  python scripts/backfill_depth.py --days 180
  python scripts/backfill_depth.py --symbols BTCUSDT,ETHUSDT
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from bot.history import _cache_path, _load_cache, _save_cache, _normalize_for_res, RES_SEC  # noqa: E402
from bot.wallex_client import WallexClient  # noqa: E402
from bot.storage import Storage  # noqa: E402
from bot.models import Candle  # noqa: E402

DATA_DIR = str(ROOT / "data")
STATUS_URL = "http://127.0.0.1:8787/api/status"


def engine_busy() -> bool:
    """True while the engine is scanning — backfill must yield."""
    try:
        import requests
        return bool(requests.get(STATUS_URL, timeout=3).json().get("running"))
    except Exception:
        return False  # backend down = idle, safe to fetch


def user_job_busy() -> bool:
    """A+B hybrid: True while a USER job (backtest top-up/download) holds the
    yield marker data/.user_job_busy fresh (< 15 min). User requests always
    have priority over this background backfill."""
    try:
        p = Path(DATA_DIR) / ".user_job_busy"
        if p.exists() and (time.time() - p.stat().st_mtime) < 15 * 60:
            return True
    except Exception:
        pass
    return False


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=120)
    ap.add_argument("--symbols", type=str, default="")
    ap.add_argument("--gap", type=float, default=12.0)
    args = ap.parse_args()

    symbols = ([s.strip().upper() for s in args.symbols.split(",") if s.strip()]
               or _engine_symbols())
    client = WallexClient(min_gap_sec=args.gap)
    now = int(time.time())
    need_from = now - args.days * 86400
    print(f"backfill target: {args.days}d for {len(symbols)} symbols "
          f"(idle-aware, gap={args.gap}s)", flush=True)

    for sym in symbols:
        # res=15 needs ~1 request/day → 120d ≈ 120 reqs × 12s ≈ 25 min/symbol.
        # Only top it up if SEVERELY thin (<10 days); the backtest gate (fix #9)
        # otherwise just excludes the symbol from the run.
        for res in ("60", "240", "15", "1D"):
            path = _cache_path(DATA_DIR, sym, res)
            cached = _normalize_for_res(res, _load_cache(path))
            if not cached:
                print(f"  {sym} {res}: cache empty — full download delegated to "
                      f"ensure_depth at next backtest", flush=True)
                continue
            oldest = cached[0].ts
            # 15m: ~1 request/day — only heal SEVERELY thin caches (<10 days)
            # to keep the request count sane; the backtest gate (fix #9)
            # otherwise just excludes the symbol from the run.
            # 1D: ONE request covers a year — always fill to full depth.
            have_days = (now - oldest) / 86400.0
            if res == "15":
                if oldest <= need_from + RES_SEC[res] * 2 or have_days >= 10:
                    print(f"  {sym} {res}: ok ({len(cached)} bars, {have_days:.1f}d)", flush=True)
                    continue
            elif oldest <= need_from + RES_SEC[res] * 2:
                print(f"  {sym} {res}: ok ({len(cached)} bars, {have_days:.1f}d)", flush=True)
                continue
            # incremental BACK-SPAN only: oldest_cache - margin .. oldest_cache
            back_from = need_from
            back_to = oldest + RES_SEC[res] * 2
            rsec = RES_SEC[res] / 3600.0
            est_req = max(1, int((back_to - back_from) / 3600 / 1500) + 1)
            print(f"  {sym} {res}: backfilling {len(cached)}->{args.days}d "
                  f"({(back_to - back_from) / 86400:.0f}d span, ~{est_req} req "
                  f"× {args.gap}s ≈ {est_req * args.gap / 60:.0f} min)", flush=True)
            fetched: dict = {c.ts: c for c in cached}
            # res=15 returns ~1 day per request — chunk by ONE DAY for it.
            chunk_len = 86400 if res == "15" else int(1500 * 3600)
            cursor = back_from
            chunk_retries = 0
            while cursor < back_to:
                # A+B hybrid: a USER job holds priority → yield Wallex, resume
                # later. (No engine_busy gate: the engine runs 24/7 but is
                # network-light thanks to the bar-elapsed gate — coexistence is
                # safe now that the engine rebases from a deeper disk file via
                # mtime + atomic writes + keep-floor. An engine gate would pause
                # this backfill forever.)
                if user_job_busy():
                    print("    … USER job active — background backfill yielding "
                          "(user priority)", flush=True)
                    time.sleep(60)
                    continue
                chunk_end = min(cursor + chunk_len, back_to)
                try:
                    got = client.get_candles(sym, res, cursor, chunk_end)
                    chunk_retries = 0
                except Exception as e:
                    # FIX: transient 500/504 must not kill the whole backfill —
                    # wait and retry this chunk a few times, then skip forward.
                    chunk_retries += 1
                    if chunk_retries <= 3:
                        print(f"    chunk error ({str(e)[:60]}) — retry {chunk_retries}/3 "
                              f"after 30s", flush=True)
                        time.sleep(30)
                        continue
                    print(f"    chunk skipped after 3 retries: {str(e)[:60]}", flush=True)
                    chunk_retries = 0
                    cursor = min(cursor + chunk_len, back_to)
                    continue
                for c in got:
                    fetched[c.ts] = c
                print(f"    +{len(got)} raw bars", flush=True)
                if not got:
                    cursor = min(cursor + chunk_len, back_to)
                    continue
                # advance by what was actually returned (Wallex may return less)
                nxt = max(max(c.ts for c in got) + RES_SEC[res], cursor + RES_SEC[res])
                if nxt <= cursor or nxt >= back_to:
                    break
                cursor = nxt
            merged = sorted(fetched.values(), key=lambda c: c.ts)
            merged = _normalize_for_res(res, merged)
            _save_cache(path, merged)
            print(f"  {sym} {res}: saved {len(merged)} bars "
                  f"(oldest {merged[0].ts})", flush=True)
    print("backfill complete", flush=True)


def _engine_symbols() -> list:
    """Default symbol set = engine symbols (8) via /api/status; fall back to config."""
    try:
        import requests
        return list(requests.get(STATUS_URL, timeout=3).json().get("symbols") or [])
    except Exception:
        import yaml
        cfg = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
        return list(cfg.get("symbols") or [])


if __name__ == "__main__":
    main()
