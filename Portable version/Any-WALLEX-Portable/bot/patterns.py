"""Candlestick entry patterns on CLOSED candles only (1h timeframe).

Implemented (bullish, spot long-only):
  - Bullish Engulfing
  - Bullish Pin Bar (long lower wick)
  - Hammer (after a short downtrend)
  - Morning Star (3-candle reversal)
"""
from __future__ import annotations

from typing import List, Optional

from .models import Candle


def _body(c: Candle) -> float:
    return max(c.body, c.range * 1e-6)  # avoid div-by-zero on dojis


def is_bullish_engulfing(candles: List[Candle]) -> bool:
    if len(candles) < 2:
        return False
    prev, cur = candles[-2], candles[-1]
    return (
        prev.is_bear
        and cur.is_bull
        and cur.c >= prev.o
        and cur.o <= prev.c
        and cur.body > prev.body
    )


def is_bullish_pinbar(candles: List[Candle], wick_ratio: float = 2.0, max_opp: float = 0.5) -> bool:
    if not candles:
        return False
    c = candles[-1]
    body = _body(c)
    return c.lower_wick >= wick_ratio * body and c.upper_wick <= max_opp * body


def is_hammer(candles: List[Candle], lookback: int = 5, wick_ratio: float = 2.0) -> bool:
    """Hammer: pin-bar shape that appears after a decline within `lookback` candles."""
    if len(candles) < lookback + 1:
        return False
    c = candles[-1]
    body = _body(c)
    if not (c.lower_wick >= wick_ratio * body and c.upper_wick <= body * 0.5):
        return False
    window = candles[-(lookback + 1) : -1]
    decline = window[0].c > window[-1].c  # price was falling into the hammer
    lowest = min(x.l for x in window)
    near_low = c.l <= lowest * 1.005
    return decline or near_low


def is_morning_star(candles: List[Candle], mid_ratio: float = 0.3) -> bool:
    if len(candles) < 3:
        return False
    a, b, c = candles[-3], candles[-2], candles[-1]
    if not (a.is_bear and c.is_bull):
        return False
    if a.body <= 0:
        return False
    if b.body > mid_ratio * a.body:
        return False
    # b gaps down (body below a's body) and c closes into upper half of a
    if max(b.o, b.c) >= min(a.o, a.c):
        return False
    return c.c >= (a.o + a.c) / 2


def detect_pattern(candles: List[Candle], cfg: dict) -> Optional[str]:
    """Return the first matching pattern name, or None. Order matters:
    more specific patterns first."""
    p = cfg.get("patterns", {})
    if is_morning_star(candles, p.get("morning_star_mid", 0.3)):
        return "morning_star"
    if is_bullish_engulfing(candles):
        return "bullish_engulfing"
    if is_hammer(candles, p.get("hammer_lookback", 5), p.get("pinbar_wick_ratio", 2.0)):
        return "hammer"
    if is_bullish_pinbar(candles, p.get("pinbar_wick_ratio", 2.0), p.get("pinbar_max_opp", 0.5)):
        return "pin_bar"
    return None


# ─────────────────────────── bearish patterns (margin short) ────────────────
def is_bearish_engulfing(candles: List[Candle]) -> bool:
    if len(candles) < 2:
        return False
    prev, cur = candles[-2], candles[-1]
    return (
        prev.is_bull
        and cur.is_bear
        and cur.o >= prev.c
        and cur.c <= prev.o
        and cur.body > prev.body
    )


def is_bearish_pinbar(candles: List[Candle], wick_ratio: float = 2.0, max_opp: float = 0.5) -> bool:
    """Shooting-star shape: long UPPER wick, small lower wick."""
    if not candles:
        return False
    c = candles[-1]
    body = _body(c)
    return c.upper_wick >= wick_ratio * body and c.lower_wick <= max_opp * body


def is_shooting_star(candles: List[Candle], lookback: int = 5, wick_ratio: float = 2.0) -> bool:
    """Shooting star: pin-bar shape that appears after an advance within `lookback` candles."""
    if len(candles) < lookback + 1:
        return False
    c = candles[-1]
    body = _body(c)
    if not (c.upper_wick >= wick_ratio * body and c.lower_wick <= body * 0.5):
        return False
    window = candles[-(lookback + 1) : -1]
    advance = window[0].c < window[-1].c  # price was rising into the star
    highest = max(x.h for x in window)
    near_high = c.h >= highest * 0.995
    return advance or near_high


def is_evening_star(candles: List[Candle], mid_ratio: float = 0.3) -> bool:
    if len(candles) < 3:
        return False
    a, b, c = candles[-3], candles[-2], candles[-1]
    if not (a.is_bull and c.is_bear):
        return False
    if a.body <= 0:
        return False
    if b.body > mid_ratio * a.body:
        return False
    # b gaps up (body above a's body) and c closes into lower half of a
    if min(b.o, b.c) <= max(a.o, a.c):
        return False
    return c.c <= (a.o + a.c) / 2


def detect_pattern_bearish(candles: List[Candle], cfg: dict) -> Optional[str]:
    """Return the first matching BEARISH pattern name, or None."""
    p = cfg.get("patterns", {})
    if is_evening_star(candles, p.get("morning_star_mid", 0.3)):
        return "evening_star"
    if is_bearish_engulfing(candles):
        return "bearish_engulfing"
    if is_shooting_star(candles, p.get("hammer_lookback", 5), p.get("pinbar_wick_ratio", 2.0)):
        return "shooting_star"
    if is_bearish_pinbar(candles, p.get("pinbar_wick_ratio", 2.0), p.get("pinbar_max_opp", 0.5)):
        return "pin_bar_bear"
    return None
