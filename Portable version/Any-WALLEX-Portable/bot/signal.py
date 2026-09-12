"""Signal engine — the 8-criteria scoring system.

Vital conditions (hard gates, must ALL pass before scoring even matters):
  1. Suitable structure (4h bullish bias)
  2. Valid location (at/near support / OB / FVG)
  3. Entry confirmation (a bullish pattern on closed 1h candle)
  4. RR >= 1.5
  5. Valid stop (0.8..4 x ATR below entry)

Entry requires: all vital conditions AND score >= 6 of 8.

The 8 scored criteria:
  1. structure   — 4h trend bullish (HH/HL or BOS/CHoCH up)
  2. location    — price at bullish level (SR/OB/FVG) within 0.5*ATR
  3. entry       — bullish pattern detected on closed 1h candle
  4. momentum    — 15m filter: RSI>50 and close above EMA20
  5. rsi         — 1h RSI(14) in healthy zone [40, 70]
  6. volume      — last 1h volume >= 1.2x SMA(10) of prior volumes
  7. stop        — stop distance within [0.8, 4.0] x ATR
  8. rr          — reward/risk >= 1.5 to nearest resistance

PURE: takes candle lists + config, returns Signal or None. No I/O.
"""
from __future__ import annotations

from typing import List, Optional

from . import indicators, levels as lvl, patterns
from .models import Candle, Confirmation, Level, Signal, Structure
from .structure import analyze_structure


def build_signal(
    symbol: str,
    candles_4h: List[Candle],
    candles_1h: List[Candle],
    candles_15m: List[Candle],
    cfg: dict,
) -> Optional[Signal]:
    scfg = cfg["strategy"]
    min_conf = int(scfg.get("min_confirmations", 6))
    min_rr = float(scfg.get("min_rr", 1.5))

    if len(candles_4h) < 30 or len(candles_1h) < 60 or len(candles_15m) < 30:
        return None

    # ── 1) 4h structure ────────────────────────────────────────────
    st: Structure = analyze_structure(
        candles_4h, scfg.get("swing_left", 2), scfg.get("swing_right", 2)
    )
    structure_ok = st.bullish_bias

    # ── 2) 1h indicators & levels ──────────────────────────────────
    closes_1h = [c.c for c in candles_1h]
    rsi_period = int(scfg.get("rsi_period", 14))
    atr_period = int(scfg.get("atr_period", 14))
    rsi_series = indicators.rsi(closes_1h, rsi_period)
    atr_series = indicators.atr(candles_1h, atr_period)
    rsi_now = rsi_series[-1]
    atr_now = atr_series[-1]
    if rsi_now is None or atr_now is None or atr_now <= 0:
        return None

    price = candles_1h[-1].c
    sr = lvl.find_sr_levels(candles_1h, atr_now, int(scfg.get("sr_lookback", 120)))
    obs = lvl.find_order_blocks(candles_1h, atr_now)
    fvgs = lvl.find_fvg(candles_1h, atr_now)
    all_levels: List[Level] = sr + obs + fvgs

    tol_mult = float(scfg.get("level_atr_tolerance", 0.5))
    location_ok = lvl.price_at_bullish_level(all_levels, price, atr_now, tol_mult)

    # ── 3) entry pattern on closed 1h candle ───────────────────────
    pattern = patterns.detect_pattern(candles_1h, scfg)
    entry_ok = pattern is not None

    # ── 4) 15m momentum filter ─────────────────────────────────────
    mcfg = scfg.get("momentum", {})
    closes_15 = [c.c for c in candles_15m]
    rsi_15 = indicators.rsi(closes_15, rsi_period)[-1]
    ema_15 = indicators.ema(closes_15, int(mcfg.get("ema_period", 20)))[-1]
    momentum_ok = (
        rsi_15 is not None
        and ema_15 is not None
        and rsi_15 >= float(mcfg.get("rsi_min", 50))
        and closes_15[-1] > ema_15
    )

    # ── stop / target / RR ─────────────────────────────────────────
    stop_dist = float(scfg.get("stop_atr_mult", 1.5)) * atr_now
    entry = price
    stop = entry - stop_dist
    # structural stop refinement: just below nearest support/OB/FVG if tighter-but-valid
    support = lvl.nearest_support(all_levels, entry)
    if support is not None and entry - support < stop_dist and entry - support >= 0.3 * atr_now:
        stop = support - 0.25 * atr_now

    resistance = lvl.nearest_resistance(all_levels, entry)
    risk = entry - stop
    if risk <= 0:
        return None
    target = resistance if (resistance and resistance > entry) else entry + min_rr * risk
    rr = (target - entry) / risk

    # ── 5) RSI healthy ─────────────────────────────────────────────
    rsi_ok = float(scfg.get("rsi_min", 40)) <= rsi_now <= float(scfg.get("rsi_max", 70))

    # ── 6) volume ──────────────────────────────────────────────────
    vol_ratio = indicators.volume_ratio(candles_1h, int(scfg.get("volume_ma_period", 10)))
    volume_ok = vol_ratio is not None and vol_ratio >= float(scfg.get("volume_min_ratio", 1.2))

    # ── 7) valid stop ──────────────────────────────────────────────
    stop_atrs = risk / atr_now
    stop_ok = float(scfg.get("stop_min_atr", 0.8)) <= stop_atrs <= float(scfg.get("stop_max_atr", 4.0))

    # ── 8) RR ──────────────────────────────────────────────────────
    rr_ok = rr >= min_rr

    confs = [
        Confirmation("structure", "ساختار صعودی ۴ساعته", structure_ok, st.last_event or st.trend.value),
        Confirmation("location", "محل معتبر (سطح/OB/FVG)", location_ok),
        Confirmation("entry", "تأیید ورود (الگوی کندلی)", entry_ok, pattern or ""),
        Confirmation("momentum", "مومنتوم ۱۵ دقیقه", momentum_ok),
        Confirmation("rsi", f"RSI سالم ({rsi_period})", rsi_ok, f"{rsi_now:.1f}"),
        Confirmation("volume", "حجم ≥ میانگین", volume_ok, f"{(vol_ratio or 0):.2f}x"),
        Confirmation("stop", "استاپ معتبر", stop_ok, f"{stop_atrs:.2f}×ATR"),
        Confirmation("rr", f"RR ≥ {min_rr}", rr_ok, f"{rr:.2f}"),
    ]
    score = sum(1 for c in confs if c.ok)

    # ── vital gates ────────────────────────────────────────────────
    vital_ok = structure_ok and location_ok and entry_ok and rr_ok and stop_ok
    if not vital_ok or score < min_conf:
        # still return the signal object (marked ineligible) so the dashboard
        # can show WHY it was rejected — engine decides not to trade it.
        sig = Signal(symbol=symbol, ts=candles_1h[-1].ts, direction="long",
                     entry=entry, stop=stop, target=target, rr=rr, score=score,
                     confirmations=confs, pattern=pattern or "", atr=atr_now)
        sig.eligible = False  # type: ignore[attr-defined]
        return sig

    sig = Signal(symbol=symbol, ts=candles_1h[-1].ts, direction="long",
                 entry=entry, stop=stop, target=target, rr=rr, score=score,
                 confirmations=confs, pattern=pattern or "", atr=atr_now)
    sig.eligible = True  # type: ignore[attr-defined]
    return sig


def build_signal_short(
    symbol: str,
    candles_4h: List[Candle],
    candles_1h: List[Candle],
    candles_15m: List[Candle],
    cfg: dict,
) -> Optional[Signal]:
    """Mirror of build_signal for SHORT entries (margin only).

    Vital gates: bearish 4h structure, price at a bearish level, a bearish
    pattern on the closed 1h candle, RR >= min_rr, valid stop.
    """
    scfg = cfg["strategy"]
    min_conf = int(scfg.get("min_confirmations", 6))
    min_rr = float(scfg.get("min_rr", 1.5))

    if len(candles_4h) < 30 or len(candles_1h) < 60 or len(candles_15m) < 30:
        return None

    # ── 1) 4h structure (bearish bias) ─────────────────────────────
    st: Structure = analyze_structure(
        candles_4h, scfg.get("swing_left", 2), scfg.get("swing_right", 2)
    )
    structure_ok = st.bearish_bias

    # ── 2) 1h indicators & levels ──────────────────────────────────
    closes_1h = [c.c for c in candles_1h]
    rsi_period = int(scfg.get("rsi_period", 14))
    atr_period = int(scfg.get("atr_period", 14))
    rsi_series = indicators.rsi(closes_1h, rsi_period)
    atr_series = indicators.atr(candles_1h, atr_period)
    rsi_now = rsi_series[-1]
    atr_now = atr_series[-1]
    if rsi_now is None or atr_now is None or atr_now <= 0:
        return None

    price = candles_1h[-1].c
    sr = lvl.find_sr_levels(candles_1h, atr_now, int(scfg.get("sr_lookback", 120)))
    obs = lvl.find_order_blocks_bear(candles_1h, atr_now)
    fvgs = lvl.find_fvg_bear(candles_1h, atr_now)
    all_levels: List[Level] = sr + obs + fvgs

    tol_mult = float(scfg.get("level_atr_tolerance", 0.5))
    location_ok = lvl.price_at_bearish_level(all_levels, price, atr_now, tol_mult)

    # ── 3) bearish entry pattern on closed 1h candle ───────────────
    pattern = patterns.detect_pattern_bearish(candles_1h, scfg)
    entry_ok = pattern is not None

    # ── 4) 15m momentum filter (bearish) ───────────────────────────
    mcfg = scfg.get("momentum", {})
    closes_15 = [c.c for c in candles_15m]
    rsi_15 = indicators.rsi(closes_15, rsi_period)[-1]
    ema_15 = indicators.ema(closes_15, int(mcfg.get("ema_period", 20)))[-1]
    momentum_ok = (
        rsi_15 is not None
        and ema_15 is not None
        and rsi_15 <= (100.0 - float(mcfg.get("rsi_min", 50)))
        and closes_15[-1] < ema_15
    )

    # ── stop / target / RR (short: stop above entry) ───────────────
    stop_dist = float(scfg.get("stop_atr_mult", 1.5)) * atr_now
    entry = price
    stop = entry + stop_dist
    resistance = lvl.nearest_resistance(all_levels, entry)
    if resistance is not None and resistance - entry < stop_dist and resistance - entry >= 0.3 * atr_now:
        stop = resistance + 0.25 * atr_now
    support = lvl.nearest_support(all_levels, entry)
    risk = stop - entry
    if risk <= 0:
        return None
    target = support if (support and support < entry) else entry - min_rr * risk
    rr = (entry - target) / risk

    # ── 5) RSI healthy (bearish zone) ──────────────────────────────
    rsi_ok = (100.0 - float(scfg.get("rsi_max", 70))) <= rsi_now <= (100.0 - float(scfg.get("rsi_min", 40)))

    # ── 6) volume ──────────────────────────────────────────────────
    vol_ratio = indicators.volume_ratio(candles_1h, int(scfg.get("volume_ma_period", 10)))
    volume_ok = vol_ratio is not None and vol_ratio >= float(scfg.get("volume_min_ratio", 1.2))

    # ── 7) valid stop ──────────────────────────────────────────────
    stop_atrs = risk / atr_now
    stop_ok = float(scfg.get("stop_min_atr", 0.8)) <= stop_atrs <= float(scfg.get("stop_max_atr", 4.0))

    # ── 8) RR ──────────────────────────────────────────────────────
    rr_ok = rr >= min_rr

    confs = [
        Confirmation("structure", "ساختار نزولی ۴ساعته", structure_ok, st.last_event or st.trend.value),
        Confirmation("location", "محل معتبر نزولی (سطح/OB/FVG)", location_ok),
        Confirmation("entry", "تأیید ورود نزولی (الگوی کندلی)", entry_ok, pattern or ""),
        Confirmation("momentum", "مومنتوم نزولی ۱۵ دقیقه", momentum_ok),
        Confirmation("rsi", f"RSI نزولی سالم ({rsi_period})", rsi_ok, f"{rsi_now:.1f}"),
        Confirmation("volume", "حجم ≥ میانگین", volume_ok, f"{(vol_ratio or 0):.2f}x"),
        Confirmation("stop", "استاپ معتبر", stop_ok, f"{stop_atrs:.2f}×ATR"),
        Confirmation("rr", f"RR ≥ {min_rr}", rr_ok, f"{rr:.2f}"),
    ]
    score = sum(1 for c in confs if c.ok)

    vital_ok = structure_ok and location_ok and entry_ok and rr_ok and stop_ok
    sig = Signal(symbol=symbol, ts=candles_1h[-1].ts, direction="short",
                 entry=entry, stop=stop, target=target, rr=rr, score=score,
                 confirmations=confs, pattern=pattern or "", atr=atr_now)
    sig.eligible = bool(vital_ok and score >= min_conf)  # type: ignore[attr-defined]
    return sig
