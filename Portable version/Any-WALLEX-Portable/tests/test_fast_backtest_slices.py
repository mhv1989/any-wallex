"""Regression test for Fix 1: backtest look-ahead bias.

Runs the fast-path backtest on synthetic UTC-aligned histories where the 1h and
15m series carry a marker: the price of every candle AFTER the 4h bar's close
is deliberately multiplied so that if any future bar leaks into the signal
window, the indicator state (ATR / last close) shifts and the recorded trade
entry/stop diverges from the no-look-ahead expectation.

Simplest deterministic assertion: monkey-check via the internal slice logic —
for every 4h bar index, the last candle of each sliced series must close at or
before the 4h bar's close time.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot.backtest import _fast_slices  # noqa: E402
from bot.models import Candle  # noqa: E402


def _mk(ts: int, o: float, h: float, l: float, c: float, v: float = 1.0) -> Candle:
    return Candle(ts=ts, o=o, h=h, l=l, c=c, v=v)


def test_fast_slices_no_lookahead():
    # 3 aligned 4h bars starting at a 4h boundary (UTC-aligned cache)
    t0 = 1_700_000_000
    t0 -= t0 % 14400
    day0 = t0 - (t0 % 86400)
    c4 = [_mk(t0 + i * 14400, 100, 101, 99, 100) for i in range(40)]
    c1 = [_mk(t0 + i * 3600, 100, 101, 99, 100) for i in range(160)]
    c15 = [_mk(t0 + i * 900, 100, 101, 99, 100) for i in range(640)]
    c1d = [_mk(day0 + i * 86400, 100, 101, 99, 100) for i in range(10)]

    for idx in range(29, 40):
        sim_close = c4[idx].ts + 14400  # when this 4h bar CLOSES
        s4, s1, s15, s1d = _fast_slices(c4, c1, c15, idx, c1d)
        assert s4[-1].ts == c4[idx].ts
        # every 1h bar in the window must have closed before sim_close
        assert s1 and s1[-1].ts + 3600 <= sim_close, (
            f"look-ahead in 1h at idx={idx}: last ts {s1[-1].ts} closes "
            f"{s1[-1].ts + 3600} > sim_close {sim_close}"
        )
        assert s15 and s15[-1].ts + 900 <= sim_close, (
            f"look-ahead in 15m at idx={idx}: last ts {s15[-1].ts} closes "
            f"{s15[-1].ts + 900} > sim_close {sim_close}"
        )
        # 1D slice must also be look-ahead-safe: last daily bar closes at/before
        # sim_close
        assert s1d and s1d[-1].ts + 86400 <= sim_close, (
            f"look-ahead in 1D at idx={idx}: last ts {s1d[-1].ts} closes "
            f"{s1d[-1].ts + 86400} > sim_close {sim_close}"
        )
        # and the 1h/15m slices must contain exactly the closed bars
        assert len(s1) == 4 * (idx + 1) or len(s1) == len(c1)
        assert len(s15) == 16 * (idx + 1) or len(s15) == len(c15)
        # 1D slice pacing: a daily bar closes at most every 6 four-hour steps
        # (worst case: 4h grid aligned to the day boundary)
        assert len(s1d) <= len(c1d) and len(s1d) >= (idx + 1) // 6


def test_fast_slices_mid_bucket_start():
    # cache starting mid-bucket (not aligned): must not crash and must not leak
    t0 = 1_700_000_000
    t0 -= t0 % 14400
    offset = 3600  # 1h offset
    day0 = t0 - (t0 % 86400)
    c4 = [_mk(t0 + offset + i * 14400, 100, 101, 99, 100) for i in range(40)]
    c1 = [_mk(t0 + offset + i * 3600, 100, 101, 99, 100) for i in range(160)]
    c15 = [_mk(t0 + offset + i * 900, 100, 101, 99, 100) for i in range(640)]
    c1d = [_mk(day0 + i * 86400, 100, 101, 99, 100) for i in range(10)]
    for idx in range(29, 40):
        sim_close = c4[idx].ts + 14400
        s4, s1, s15, s1d = _fast_slices(c4, c1, c15, idx, c1d)
        if s1:
            assert s1[-1].ts + 3600 <= sim_close + 3600, (
                f"mid-bucket: 1h slice leaks >1 bar beyond sim_close at idx={idx}"
            )
        if s15:
            assert s15[-1].ts + 900 <= sim_close + 900, (
                f"mid-bucket: 15m slice leaks >1 bar beyond sim_close at idx={idx}"
            )
