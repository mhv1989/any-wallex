"""Key levels: Support/Resistance, simple Order Blocks, 3-candle FVG.

All detection uses closed candles only. Levels are returned newest-first.
"""
from __future__ import annotations

from typing import List

from .models import Candle, Level, LevelKind


def find_sr_levels(candles: List[Candle], atr_val: float, lookback: int = 120, max_levels: int = 8) -> List[Level]:
    """Cluster swing highs/lows into S/R zones (tolerance = 0.5*ATR)."""
    from .structure import find_swings

    window = candles[-lookback:] if len(candles) > lookback else candles
    swings = find_swings(window, 2, 2)
    tol = max(atr_val * 0.5, 1e-12)
    zones: List[Level] = []
    for s in swings:
        merged = False
        for z in zones:
            if abs(z.mid - s.price) <= tol:
                z.top = max(z.top, s.price)
                z.bottom = min(z.bottom, s.price)
                z.touches += 1
                merged = True
                break
        if not merged:
            zones.append(Level(kind=LevelKind.SR, top=s.price, bottom=s.price,
                               direction="both", touches=1, ts=s.ts))
    zones.sort(key=lambda z: (-z.touches, z.ts))
    return zones[:max_levels]


def find_order_blocks(candles: List[Candle], atr_val: float, lookback: int = 60, max_levels: int = 4) -> List[Level]:
    """Simple bullish Order Block: the last bearish candle before a strong bullish
    impulse (impulse body > 1.5x ATR). Zone = that candle's body."""
    window = candles[-lookback:] if len(candles) > lookback else candles
    obs: List[Level] = []
    for i in range(1, len(window) - 1):
        prev, cur = window[i - 1], window[i]
        if prev.is_bear and cur.is_bull and cur.body >= 1.5 * atr_val:
            obs.append(Level(kind=LevelKind.OB, top=max(prev.o, prev.c),
                             bottom=min(prev.o, prev.c), direction="bull", ts=prev.ts))
    # dedupe overlapping zones, keep newest
    out: List[Level] = []
    for z in reversed(obs):
        if not any(abs(o.mid - z.mid) < atr_val * 0.5 for o in out):
            out.append(z)
    return out[:max_levels]


def find_fvg(candles: List[Candle], atr_val: float, lookback: int = 60, max_levels: int = 4) -> List[Level]:
    """3-candle Fair Value Gap (bullish): gap between candle[i-2].high and
    candle[i].low when the middle candle is a strong impulse."""
    window = candles[-lookback:] if len(candles) > lookback else candles
    fvgs: List[Level] = []
    for i in range(2, len(window)):
        a, b, c = window[i - 2], window[i - 1], window[i]
        if b.is_bull and c.l > a.h:
            gap_bottom, gap_top = a.h, c.l
            if gap_top - gap_bottom >= 0.1 * atr_val:  # ignore dust gaps
                fvgs.append(Level(kind=LevelKind.FVG, top=gap_top, bottom=gap_bottom,
                                  direction="bull", ts=b.ts))
    out: List[Level] = []
    for z in reversed(fvgs):
        if not any(abs(o.mid - z.mid) < atr_val * 0.3 for o in out):
            out.append(z)
    return out[:max_levels]


def nearest_support(levels: List[Level], price: float) -> float | None:
    below = [z for z in levels if z.mid <= price]
    return max(z.mid for z in below) if below else None


def nearest_resistance(levels: List[Level], price: float) -> float | None:
    above = [z for z in levels if z.mid >= price]
    return min(z.mid for z in above) if above else None


def price_at_bullish_level(levels: List[Level], price: float, atr_val: float, tol_mult: float = 0.5) -> bool:
    """True if price sits at/just above a bullish zone (OB/FVG/support) within tolerance."""
    tol = atr_val * tol_mult
    for z in levels:
        if z.direction in ("bull", "both") and z.bottom - tol <= price <= z.top + tol:
            return True
    return False


def find_order_blocks_bear(candles: List[Candle], atr_val: float, lookback: int = 60, max_levels: int = 4) -> List[Level]:
    """Simple bearish Order Block: the last bullish candle before a strong bearish
    impulse (impulse body > 1.5x ATR). Zone = that candle's body."""
    window = candles[-lookback:] if len(candles) > lookback else candles
    obs: List[Level] = []
    for i in range(1, len(window) - 1):
        prev, cur = window[i - 1], window[i]
        if prev.is_bull and cur.is_bear and cur.body >= 1.5 * atr_val:
            obs.append(Level(kind=LevelKind.OB, top=max(prev.o, prev.c),
                             bottom=min(prev.o, prev.c), direction="bear", ts=prev.ts))
    out: List[Level] = []
    for z in reversed(obs):
        if not any(abs(o.mid - z.mid) < atr_val * 0.5 for o in out):
            out.append(z)
    return out[:max_levels]


def find_fvg_bear(candles: List[Candle], atr_val: float, lookback: int = 60, max_levels: int = 4) -> List[Level]:
    """3-candle bearish Fair Value Gap: gap between candle[i-2].low and
    candle[i].high when the middle candle is a strong bearish impulse."""
    window = candles[-lookback:] if len(candles) > lookback else candles
    fvgs: List[Level] = []
    for i in range(2, len(window)):
        a, b, c = window[i - 2], window[i - 1], window[i]
        if b.is_bear and c.h < a.l:
            gap_top, gap_bottom = a.l, c.h
            if gap_top - gap_bottom >= 0.1 * atr_val:
                fvgs.append(Level(kind=LevelKind.FVG, top=gap_top, bottom=gap_bottom,
                                  direction="bear", ts=b.ts))
    out: List[Level] = []
    for z in reversed(fvgs):
        if not any(abs(o.mid - z.mid) < atr_val * 0.3 for o in out):
            out.append(z)
    return out[:max_levels]


def price_at_bearish_level(levels: List[Level], price: float, atr_val: float, tol_mult: float = 0.5) -> bool:
    """True if price sits at/just below a bearish zone (bear OB/FVG/resistance) within tolerance."""
    tol = atr_val * tol_mult
    for z in levels:
        if z.direction in ("bear", "both") and z.bottom - tol <= price <= z.top + tol:
            return True
    return False
