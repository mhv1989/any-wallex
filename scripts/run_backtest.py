"""Backtest CLI — download history and run the backtest from the terminal.

Usage:
  python scripts/run_backtest.py --days 120 --symbols BTCUSDT ETHUSDT
  python scripts/run_backtest.py --run-only          # use cached history
"""
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from bot.backtest import run_backtest
from bot.history import download_symbol, load_symbol_history
from bot.server import load_config
from bot.wallex_client import WallexClient


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=120)
    ap.add_argument("--symbols", nargs="*", default=None)
    ap.add_argument("--run-only", action="store_true")
    ap.add_argument("--out", default=str(ROOT / "data" / "backtest_result.json"))
    args = ap.parse_args()

    cfg = load_config(ROOT / "config.yaml")
    symbols = args.symbols or cfg.get("symbols", [])
    data_dir = str(ROOT / "data")

    if not args.run_only:
        client = WallexClient(min_gap_sec=cfg["engine"].get("api_min_gap_sec", 12))
        for sym in symbols:
            print(f"downloading {sym} ({args.days} days, rate-limited)…")
            try:
                data = download_symbol(client, data_dir, sym, days=args.days)
                print("  " + ", ".join(f"{r}:{len(c)}" for r, c in data.items()))
            except Exception as e:
                print(f"  FAILED: {e}")

    histories = {}
    for sym in symbols:
        h = load_symbol_history(data_dir, sym)
        if h.h15.candles and h.h60.candles and h.h240.candles:
            histories[sym] = h
            print(f"loaded {sym}: 15m={len(h.h15.candles)} 1h={len(h.h60.candles)} 4h={len(h.h240.candles)} 1d={len(h.h1d.candles)}")
        else:
            print(f"skipping {sym}: insufficient history")

    if not histories:
        print("no history available — run without --run-only first")
        sys.exit(1)

    print("\nrunning backtest (no look-ahead, fees + slippage applied)…")
    result = run_backtest(histories, cfg)

    print("\n══════════ METRICS ══════════")
    for k, v in result.metrics.items():
        print(f"  {k:22s}: {v}")
    print(f"\n  trades: {len(result.trades)}")
    for t in result.trades[-10:]:
        print(f"    {t['symbol']:10s} pnl={t['pnl']:+9.4f} rr={t['realized_rr']:+6.2f} exit={t['exit_reason']}")

    out = {"metrics": result.metrics, "equity_curve": result.equity_curve, "trades": result.trades}
    Path(args.out).write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
    print(f"\nsaved -> {args.out}")


if __name__ == "__main__":
    main()
