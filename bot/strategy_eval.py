"""Shared external-strategy condition dispatcher.

Single source of truth for evaluating strategy-artifact conditions against
candle data. Used by BOTH the live engine (bot/engine.py) and the backtest
(bot/backtest.py) so live and backtest can never diverge.

Every indicator name in strategy_schema.ALLOWED_INDICATORS has a branch here.
Price-scale indicators (vwap, bollinger, keltner, donchian, pivot, fibonacci,
support_resistance, ema/sma/...) need `compare:"price"` in the condition to be
evaluated against candle close; otherwise they'd be compared against `value`
raw. Oscillators (rsi, cci, adx, ...) compare against `value` directly.
"""
from __future__ import annotations

from typing import List, Optional, Tuple

from . import indicators
from .models import Candle


def _condition_met(current: float, prev: float, op: str, val: float) -> bool:
    if op == ">":
        return current > val
    if op == "<":
        return current < val
    if op == ">=":
        return current >= val
    if op == "<=":
        return current <= val
    if op == "==":
        return abs(current - val) < 1e-9
    if op == "crossover":
        return prev < val and current >= val
    if op == "crossunder":
        return prev > val and current <= val
    if op == "cross_above":
        return prev <= val and current > val
    if op == "cross_below":
        return prev >= val and current < val
    if op == "increase":
        return current > prev
    if op == "decrease":
        return current < prev
    return False


def indicator_series(
    name: str,
    candles: List[Candle],
    period: Optional[int] = None,
    value: float = 0.0,
) -> Optional[List[Optional[float]]]:
    """Compute one indicator series from candle objects.

    Returns None for unknown names (caller skips the condition and logs).
    """
    closes = [c.c for c in candles]
    highs = [c.h for c in candles]
    lows = [c.l for c in candles]
    volumes = [c.v for c in candles]
    p = period or None

    if name == "rsi":
        return indicators.rsi(closes, p or 14)
    if name == "ema":
        return indicators.ema(closes, p or 20)
    if name == "sma":
        return indicators.sma(closes, p or 20)
    if name == "wma":
        return indicators.wma(closes, p or 20)
    if name == "hma":
        return indicators.hma(closes, p or 21)
    if name == "tema":
        return indicators.tema(closes, p or 20)
    if name == "dema":
        return indicators.dema(closes, p or 20)
    if name == "vwma":
        return indicators.vwma(closes, volumes, p or 20)
    if name == "vwap":
        return indicators.vwap(closes, volumes)
    if name == "obv":
        return indicators.obv(candles)
    if name == "macd":
        line, _sig, _hist = indicators.macd(closes)
        return line
    if name == "stochastic":
        k, _d = indicators.stochastic(candles, p or 14)
        return k
    if name == "stochastic_rsi":
        k, _d = indicators.stochastic_rsi(candles, p or 14)
        return k
    if name == "adx":
        a, _pdi, _ndi = indicators.adx(candles, p or 14)
        return a
    if name == "aroon":
        up, _dn = indicators.aroon(candles, p or 25)
        return up
    if name == "cci":
        return indicators.cci(candles, p or 20)
    if name == "roc":
        return indicators.roc(candles, p or 12)
    if name == "williams_r":
        return indicators.williams_r(candles, p or 14)
    if name == "mfi":
        return indicators.mfi(candles, p or 14)
    if name == "ultimate_oscillator":
        return indicators.ultimate_oscillator(candles)
    if name == "awesome_oscillator":
        return indicators.awesome_oscillator(candles)
    if name == "atr":
        return indicators.atr(candles, p or 14)
    if name == "bollinger":
        _u, mid, _l = indicators.bollinger(closes, p or 20)
        return mid
    if name == "keltner":
        _u, mid, _l = indicators.keltner(candles, p or 20)
        return mid
    if name == "donchian":
        _u, mid, _l = indicators.donchian(candles, p or 20)
        return mid
    if name == "envelope":
        _u, mid, _l = indicators.envelope(candles, p or 20)
        return mid
    if name == "volume":
        return volumes
    if name == "cmf":
        return indicators.cmf(candles, p or 20)
    if name == "pivot":
        return indicators.pivot(candles, p or 24)
    if name == "fibonacci":
        return indicators.fibonacci(candles, p or 60)
    if name == "support_resistance":
        return indicators.support_resistance(candles, p or 20)
    if name == "engulfing":
        return indicators.engulfing_c(candles)
    if name == "pinbar":
        return indicators.pinbar_c(candles)
    if name == "inside_bar":
        return indicators.inside_bar_c(candles)
    return None


# Indicators whose natural output is a PRICE (same scale as candle close).
PRICE_SCALE = {
    "ema", "sma", "wma", "hma", "vwma", "vwap",
    "bollinger", "keltner", "donchian", "pivot",
    "fibonacci", "support_resistance", "atr",
}

# Indicators producing bounded/unitless outputs (compare against raw value).
OSCILLATOR_SCALE = {
    "rsi", "stochastic", "stochastic_rsi", "williams_r", "mfi",
    "adx", "aroon", "cci", "roc", "ultimate_oscillator",
    "awesome_oscillator", "obv", "volume", "cmf",
    "engulfing", "pinbar", "inside_bar",
}


def evaluate_condition(cond: dict, candles: List[Candle]) -> Optional[bool]:
    """Evaluate one strategy condition object against candles.

    Returns None if the indicator/series is unavailable (caller counts as
    not-met and may log). `compare:"price"` evaluates the indicator against
    the candle close (e.g. vwap crossunder close), `compare:"value"` (default)
    against cond.value.
    """
    name = str(cond.get("indicator", "")).lower()
    op = str(cond.get("operator", "")).lower()
    period = cond.get("period")
    try:
        period = int(period) if period else None
    except (TypeError, ValueError):
        period = None
    val_raw = cond.get("value", 0)
    try:
        val = float(val_raw)
    except (TypeError, ValueError):
        val = 0.0
    compare = str(cond.get("compare", "value")).lower()

    series = indicator_series(name, candles, period=period, value=val)
    if not series or len(series) < 2:
        return None
    if series[-1] is None or series[-2] is None:
        return None

    if compare == "price" or (compare not in ("value", "price") and name in PRICE_SCALE):
        ref = [c.c for c in candles]
        current = series[-1] - ref[-1]
        prev = series[-1 - 1] - ref[-2]
        return _condition_met(current, prev, op, 0.0)
    current = series[-1]
    prev = series[-2]
    return _condition_met(current, prev, op, val)


def evaluate_conditions(
    conditions: List[dict],
    candles: List[Candle],
    min_required: int = 1,
) -> Tuple[int, int]:
    """Evaluate a condition list; returns (met_count, total_evaluable)."""
    met = 0
    total = 0
    for cond in conditions:
        r = evaluate_condition(cond, candles)
        if r is None:
            continue
        total += 1
        if r:
            met += signal_weight(cond)
    return met, total


def signal_weight(cond: dict) -> int:
    """Candle-pattern conditions count double (they are rarer, higher-conviction)."""
    try:
        return 2 if int(cond.get("weight", 1)) >= 2 else 1
    except (TypeError, ValueError):
        return 1