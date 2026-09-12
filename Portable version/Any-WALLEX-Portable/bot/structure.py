"""Market structure analysis: swing points, HH/HL/LH/LL, BOS and CHoCH.

Pure functions — input: closed candles only. A swing is confirmed only after
`right` candles close beyond it, so nothing here can look ahead.
"""
from __future__ import annotations

from typing import List, Optional

from .models import Candle, Structure, Swing, Trend


def find_swings(candles: List[Candle], left: int = 2, right: int = 2) -> List[Swing]:
    """Confirmed swing highs/lows. A pivot at index i is confirmed only when
    index i+right exists (i.e. `right` candles have CLOSED after it)."""
    swings: List[Swing] = []
    n = len(candles)
    for i in range(left, n - right):
        c = candles[i]
        is_high = all(c.h >= candles[j].h for j in range(i - left, i + right + 1) if j != i)
        is_low = all(c.l <= candles[j].l for j in range(i - left, i + right + 1) if j != i)
        if is_high:
            swings.append(Swing(ts=c.ts, price=c.h, kind="high", index=i))
        if is_low:
            swings.append(Swing(ts=c.ts, price=c.l, kind="low", index=i))
    swings.sort(key=lambda s: s.ts)
    return swings


def analyze_structure(candles: List[Candle], left: int = 2, right: int = 2) -> Structure:
    """Build market structure from confirmed swings + close-based breaks.

    Rules:
      - HH + HL  -> uptrend;  LH + LL -> downtrend; else range.
      - BOS:  close breaks the last swing in trend direction (continuation).
      - CHoCH: close breaks the last swing AGAINST the trend (reversal).
    Breaks are measured on candle CLOSE only (never intra-bar wicks).
    """
    st = Structure()
    swings = find_swings(candles, left, right)
    highs = [s for s in swings if s.kind == "high"]
    lows = [s for s in swings if s.kind == "low"]

    if len(highs) >= 2:
        st.prev_high, st.last_high = highs[-2].price, highs[-1].price
        st.hh = highs[-1].price > highs[-2].price
        st.lh = highs[-1].price < highs[-2].price
    elif highs:
        st.last_high = highs[-1].price
    if len(lows) >= 2:
        st.prev_low, st.last_low = lows[-2].price, lows[-1].price
        st.hl = lows[-1].price > lows[-2].price
        st.ll = lows[-1].price < lows[-2].price
    elif lows:
        st.last_low = lows[-1].price

    if st.hh and st.hl:
        st.trend = Trend.UP
    elif st.lh and st.ll:
        st.trend = Trend.DOWN
    else:
        st.trend = Trend.RANGE

    # Walk closes after the last confirmed swings to detect BOS / CHoCH.
    last_idx = max([s.index for s in swings], default=-1)
    ref_high = st.last_high
    ref_low = st.last_low
    for i in range(last_idx + 1, len(candles)):
        c = candles[i]
        if ref_high is not None and c.c > ref_high:
            if st.trend == Trend.UP:
                st.last_event = "bos_up"
                st.bos_level = ref_high
            else:
                st.last_event = "choch_up"
                st.choch_level = ref_high
                st.trend = Trend.UP
            ref_high = None  # consumed — wait for next confirmed swing
        if ref_low is not None and c.c < ref_low:
            if st.trend == Trend.DOWN:
                st.last_event = "bos_down"
                st.bos_level = ref_low
            else:
                st.last_event = "choch_down"
                st.choch_level = ref_low
                st.trend = Trend.DOWN
            ref_low = None
    return st


def bearish_choch_recent(candles: List[Candle], left: int = 2, right: int = 2, lookback: int = 6) -> bool:
    """True if a bearish CHoCH (close below last swing low while not in downtrend)
    happened within the last `lookback` candles — used as an exit trigger."""
    if len(candles) < left + right + lookback + 2:
        return False
    base = candles[:-lookback]
    st_base = analyze_structure(base, left, right)
    ref_low = st_base.last_low
    if ref_low is None:
        return False
    for c in candles[-lookback:]:
        if c.c < ref_low and not st_base.trend == Trend.DOWN:
            return True
    return False


def bullish_choch_recent(candles: List[Candle], left: int = 2, right: int = 2, lookback: int = 6) -> bool:
    """True if a bullish CHoCH (close above last swing high while not in uptrend)
    happened within the last `lookback` candles — exit trigger for SHORTS."""
    if len(candles) < left + right + lookback + 2:
        return False
    base = candles[:-lookback]
    st_base = analyze_structure(base, left, right)
    ref_high = st_base.last_high
    if ref_high is None:
        return False
    for c in candles[-lookback:]:
        if c.c > ref_high and not st_base.trend == Trend.UP:
            return True
    return False
