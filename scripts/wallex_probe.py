"""Wallex API rate-capacity probe (SAFE, read-only, self-limiting).

Measures the REAL rate ceiling of api.wallex.ir public endpoints so the
engine's pacing can be tuned with evidence instead of guessing.

Safety guarantees:
  * Read-only public endpoint only (/v1/udf/history, tiny 10-bar window).
  * ASCII timeline (no ANSI) + exactlt one row per step printed.
  * Ramps modestly: 1 -> 2 -> 4 -> 8 rps, 8 requests per step (short).
  * STOPS ESCALATING at the first sustained failure / 429 / high error.
  * A hard total-request guard (default 64) so it can never hammer the API.
  * Each request has a short timeout so a hang cannot stall the run.

Usage:
  python scripts/wallex_probe.py --symbol BTCUSDT --rps 8 --write data/api_probe.json
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import httpx

BASE = "https://api.wallex.ir"
STOP = False


def time_now_ms():
    return time.time() * 1000


def probe_one(args, rps, idx):
    """Issue a single history request, return (ok, status, latency_ms, err)."""
    global STOP
    if STOP:
        return None
    from_ts = int(time.time()) - 10 * 60  # tiny 10-min window -> minimal payload
    try:
        t0 = time.time()
        with httpx.Client(
            timeout=args.timeout,
            headers={"User-Agent": "WallexProbe/0.1"},
            follow_redirects=True,
        ) as c:
            r = c.get(
                f"{BASE}/v1/udf/history",
                params={"symbol": args.symbol, "resolution": "1", "from": from_ts, "to": int(time.time())},
            )
            lat = (time.time() - t0) * 1000
        return (r.status_code == 200, r.status_code, lat, "" if r.status_code == 200 else r.text[:120])
    except (httpx.HTTPError, Exception) as e:  # noqa: BLE001
        return (False, 0, (time.time() - t0) * 1000, f"{type(e).__name__}: {str(e)[:120]}")


def run_step(args, rps, n):
    """Fire n requests concurrently (each ~1/rps-paced). Returns stats dict."""
    global STOP
    results = []
    # Pace: launch one thread, but sleep 1/rps between submissions so actual
    # outbound rate ~= rps (concurrency arms the pipe, sleep paces the rate).
    delay = 1.0 / rps
    with ThreadPoolExecutor(max_workers=max(2, int(rps * 4))) as pool:
        futs = []
        tl = 0.0
        for i in range(n):
            futs.append(pool.submit(probe_one, args, rps, i))
            if len(futs) < n:
                time.sleep(delay)
    for f in as_completed(futs):
        r = f.result()
        if r:
            results.append(r)
    ok = [r for r in results if r[0]]
    err = [r for r in results if not r[0]]
    lats = sorted(r[2] for r in results)
    statuses = {}
    for r in results:
        statuses[r[1]] = statuses.get(r[1], 0) + 1
    return {
        "rps": rps,
        "requested": n,
        "success": len(ok),
        "fail": len(err),
        "error_rate_pct": round(100 * len(err) / max(1, len(results)), 1),
        "lat_p50_ms": round(statistics.median(lats), 1) if lats else None,
        "lat_p95_ms": round(lats[int(len(lats) * 0.95) - 1], 1) if len(lats) * 0.95 >= 1 else None,
        "statuses": statuses,
        "first_error": (err[0][3] if err else ""),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default="BTCUSDT")
    ap.add_argument("--max-rps", type=float, default=8.0)
    ap.add_argument("--requests-per-step", type=int, default=8)
    ap.add_argument("--timeout", type=float, default=8.0)
    ap.add_argument("--total-guard", type=int, default=64)
    ap.add_argument("--write", default="data/api_probe.json")
    args = ap.parse_args()

    # reset guard so '--write' path can't exceed it
    total_budget = args.total_guard

    print(f"== Wallex probe: {args.symbol} @ up to {args.max_rps} rps ==", flush=True)
    print(f"   step size {args.requests_per_step} req, timeout {args.timeout}s, total-guard {total_budget}", flush=True)

    steps = []
    rps = 1.0
    sent = 0
    while rps <= args.max_rps + 1e-9 and sent < total_budget:
        batch = min(args.requests_per_step, total_budget - sent)
        st = run_step(args, rps, batch)
        sent += batch
        steps.append(st)
        line = (
            f"[rps={st['rps']:4.1f}] ok={st['success']:2d}/{st['requested']} "
            f"err_rate={st['error_rate_pct']:5.1f}% p50={st['lat_p50_ms']}ms p95={st['lat_p95_ms']}ms "
            f"statuses={st['statuses']}"
        )
        print(line, flush=True)
        # stop escalating if this step had any failure or a 429/5xx
        if st["fail"] > 0:
            print("   -> failures detected. NOT escalating (this is the observed ceiling).", flush=True)
            STOP = True
            break
        if 429 in st["statuses"] or any(s >= 500 for s in st["statuses"] if s):
            print("   -> HTTP 429/5xx seen. NOT escalating.", flush=True)
            STOP = True
            break
        rps = min(rps * 2.0, args.max_rps)

    # summary
    successful_steps = [s for s in steps if s["fail"] == 0]
    if successful_steps:
        fastest = max(successful_steps, key=lambda s: s["rps"])
        print(f"\nSUMMARY: sustained {fastest['rps']:.1f} rps clean "
              f"(p95 {fastest['lat_p95_ms']}ms) without failure.", flush=True)
        print(f"         Implied safe engine throttle: ~{fastest['rps']/2:.1f} rps "
              f"(50% headroom). Scan of 200 reqs => ~{200*2/(fastest['rps'] or 1):.1f}s at that rate.", flush=True)
    else:
        print("\nSUMMARY: even 1 rps failed — API unreachable or blocking right now. Re-run later.", flush=True)

    if args.write:
        import os
        os.makedirs(os.path.dirname(args.write) or ".", exist_ok=True)
        with open(args.write, "w", encoding="utf-8") as f:
            json.dump({"run_ts": time.time(), "symbol": args.symbol, "steps": steps}, f, indent=2)
        print(f"\nWrote JSON -> {args.write}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())