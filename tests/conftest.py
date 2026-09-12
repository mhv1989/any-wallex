"""Shared test fixtures: synthetic candle builders."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot.models import Candle  # noqa: E402


def mk(ts, o, h, l, c, v=100.0):
    return Candle(ts=ts, o=o, h=h, l=l, c=c, v=v)


def uptrend_candles(n=62, start=100.0, step=2.0, ts0=1_000_000, res=3600):
    """Zigzag uptrend: 4 up candles (+step), 2 down candles (-0.7*step).
    Produces confirmed HH/HL swing structure with left=right=2."""
    out = []
    price = start
    i = 0
    while len(out) < n:
        phase = i % 6
        if phase < 4:  # up leg
            o = price
            c = o + step
            h = c + 0.3
            l = o - 0.2
        else:  # pullback leg
            o = price
            c = o - step * 0.7
            l = c - 0.3
            h = o + 0.2
        out.append(mk(ts0 + len(out) * res, o, h, l, c))
        price = c
        i += 1
    return out


def downtrend_candles(n=62, start=300.0, step=2.0, ts0=1_000_000, res=3600):
    """Mirror zigzag: 4 down candles, 2 up candles -> LH/LL structure."""
    out = []
    price = start
    i = 0
    while len(out) < n:
        phase = i % 6
        if phase < 4:  # down leg
            o = price
            c = o - step
            l = c - 0.3
            h = o + 0.2
        else:  # bounce leg
            o = price
            c = o + step * 0.7
            h = c + 0.3
            l = o - 0.2
        out.append(mk(ts0 + len(out) * res, o, h, l, c))
        price = c
        i += 1
    return out


def flat_candles(n=60, price=100.0, ts0=1_000_000, res=3600, noise=0.3):
    out = []
    for i in range(n):
        d = noise if i % 2 == 0 else -noise
        o = price
        c = price + d
        out.append(mk(ts0 + i * res, o, max(o, c) + 0.1, min(o, c) - 0.1, c))
    return out
