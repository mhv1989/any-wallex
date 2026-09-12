"""Technical indicators — pure functions over Candle lists. No I/O, no globals."""
from __future__ import annotations

from typing import List, Optional

from .models import Candle


def sma(values: List[float], period: int) -> List[Optional[float]]:
    out: List[Optional[float]] = [None] * len(values)
    if period <= 0:
        return out
    s = 0.0
    for i, v in enumerate(values):
        s += v
        if i >= period:
            s -= values[i - period]
        if i >= period - 1:
            out[i] = s / period
    return out


def ema(values: List[float], period: int) -> List[Optional[float]]:
    out: List[Optional[float]] = [None] * len(values)
    if period <= 0 or not values:
        return out
    k = 2.0 / (period + 1)
    prev = values[0]
    out[0] = prev
    for i in range(1, len(values)):
        prev = values[i] * k + prev * (1 - k)
        out[i] = prev
    return out


def rsi(closes: List[float], period: int = 14) -> List[Optional[float]]:
    """Wilder's RSI."""
    out: List[Optional[float]] = [None] * len(closes)
    if len(closes) <= period:
        return out
    gains = 0.0
    losses = 0.0
    for i in range(1, period + 1):
        d = closes[i] - closes[i - 1]
        gains += max(d, 0.0)
        losses += max(-d, 0.0)
    avg_gain = gains / period
    avg_loss = losses / period
    out[period] = 100.0 if avg_loss == 0 else 100.0 - 100.0 / (1 + avg_gain / avg_loss)
    for i in range(period + 1, len(closes)):
        d = closes[i] - closes[i - 1]
        avg_gain = (avg_gain * (period - 1) + max(d, 0.0)) / period
        avg_loss = (avg_loss * (period - 1) + max(-d, 0.0)) / period
        out[i] = 100.0 if avg_loss == 0 else 100.0 - 100.0 / (1 + avg_gain / avg_loss)
    return out


def atr(candles: List[Candle], period: int = 14) -> List[Optional[float]]:
    """Wilder's ATR."""
    out: List[Optional[float]] = [None] * len(candles)
    if len(candles) <= period:
        return out
    trs: List[float] = [0.0]
    for i in range(1, len(candles)):
        c = candles[i]
        pc = candles[i - 1].c
        trs.append(max(c.h - c.l, abs(c.h - pc), abs(c.l - pc)))
    first = sum(trs[1 : period + 1]) / period
    out[period] = first
    prev = first
    for i in range(period + 1, len(candles)):
        prev = (prev * (period - 1) + trs[i]) / period
        out[i] = prev
    return out


def volume_ratio(candles: List[Candle], period: int = 10) -> Optional[float]:
    """Last candle volume / SMA(volume, period) of the PREVIOUS candles (no look-ahead)."""
    if len(candles) < period + 1:
        return None
    prev_vols = [c.v for c in candles[-(period + 1) : -1]]
    avg = sum(prev_vols) / period
    if avg <= 0:
        return None
    return candles[-1].v / avg


# ── extended set for external AI strategies (must mirror ALLOWED_INDICATORS) ──

def wma(values: List[float], period: int) -> List[Optional[float]]:
    out: List[Optional[float]] = [None] * len(values)
    if period <= 0:
        return out
    weights = list(range(1, period + 1))
    denom = sum(weights)
    for i in range(period - 1, len(values)):
        s = sum(values[i - period + 1 + j] * weights[j] for j in range(period))
        out[i] = s / denom
    return out


def _donchian_mid(candles: List[Candle], period: int) -> List[Optional[float]]:
    out: List[Optional[float]] = [None] * len(candles)
    for i in range(period - 1, len(candles)):
        w = candles[i - period + 1 : i + 1]
        out[i] = (max(c.h for c in w) + min(c.l for c in w)) / 2
    return out


def stochastic(candles: List[Candle], period: int = 14, smooth_k: int = 3, smooth_d: int = 3):
    """Returns (%K smoothed, %D) lists."""
    raw: List[Optional[float]] = [None] * len(candles)
    for i in range(period - 1, len(candles)):
        w = candles[i - period + 1 : i + 1]
        hh = max(c.h for c in w)
        ll = min(c.l for c in w)
        raw[i] = 50.0 if hh == ll else (candles[i].c - ll) / (hh - ll) * 100.0
    k = sma([v if v is not None else 0.0 for v in raw], smooth_k)
    k = [kv if rv is not None else None for kv, rv in zip(k, raw)]
    d = sma([kv if kv is not None else 0.0 for kv in k], smooth_d)
    d = [dv if kv is not None else None for dv, kv in zip(d, k)]
    return k, d


def macd(closes: List[float], fast: int = 12, slow: int = 26, signal: int = 9):
    """Returns (macd_line, signal_line, histogram) lists."""
    line = ema(closes, fast)
    slow_line = ema(closes, slow)
    macd_line = [
        (l - s) if (l is not None and s is not None) else None
        for l, s in zip(line, slow_line)
    ]
    valid = [v if v is not None else 0.0 for v in macd_line]
    sig = ema(valid, signal)
    sig = [sv if mv is not None else None for sv, mv in zip(sig, macd_line)]
    hist = [
        (mv - sv) if (mv is not None and sv is not None) else None
        for mv, sv in zip(macd_line, sig)
    ]
    return macd_line, sig, hist


def obv(candles: List[Candle]) -> List[float]:
    out: List[float] = []
    acc = 0.0
    for i, c in enumerate(candles):
        if i:
            if c.c > candles[i - 1].c:
                acc += c.v
            elif c.c < candles[i - 1].c:
                acc -= c.v
        out.append(acc)
    return out


def adx(candles: List[Candle], period: int = 14):
    """Returns (adx, +DI, -DI) lists."""
    n = len(candles)
    tr = [0.0]
    pdm = [0.0]
    ndm = [0.0]
    for i in range(1, n):
        up = candles[i].h - candles[i - 1].h
        dn = candles[i - 1].l - candles[i].l
        pdm.append(up if (up > dn and up > 0) else 0.0)
        ndm.append(dn if (dn > up and dn > 0) else 0.0)
        c = candles[i]
        pc = candles[i - 1].c
        tr.append(max(c.h - c.l, abs(c.h - pc), abs(c.l - pc)))

    def _smooth(arr: List[float]) -> List[Optional[float]]:
        s = 0.0
        out: List[Optional[float]] = [None] * len(arr)
        for i, v in enumerate(arr):
            if i < period:
                s += v
                if i == period - 1:
                    out[i] = s
            else:
                s = s - s / period + v
                out[i] = s
        return out

    trs = _smooth(tr)
    ps = _smooth(pdm)
    ns = _smooth(ndm)
    pdi = [100.0 * p / t if (t and p is not None) else None for t, p in zip(trs, ps)]
    ndi = [100.0 * nn / t if (t and nn is not None) else None for t, nn in zip(trs, ns)]
    dx: List[Optional[float]] = [None] * n
    for i in range(n):
        if pdi[i] is not None and ndi[i] is not None and (pdi[i] + ndi[i]) > 0:
            dx[i] = 100.0 * abs(pdi[i] - ndi[i]) / (pdi[i] + ndi[i])
    adx_out: List[Optional[float]] = [None] * n
    total = 0.0
    cnt = 0
    for i in range(n):
        if dx[i] is None:
            continue
        if cnt < period:
            total += dx[i]
            cnt += 1
            if cnt == period:
                adx_out[i] = total / period
        else:
            prev = adx_out[i - 1]
            adx_out[i] = ((prev if prev is not None else 0.0) * (period - 1) + dx[i]) / period
    return adx_out, pdi, ndi


def aroon(candles: List[Candle], period: int = 25):
    up: List[Optional[float]] = [None] * len(candles)
    dn: List[Optional[float]] = [None] * len(candles)
    for i in range(period, len(candles)):
        w = candles[i - period : i + 1]
        hi = 0
        lo = 0
        for j, c in enumerate(w):
            if c.h >= w[hi].h:
                hi = j
            if c.l <= w[lo].l:
                lo = j
        up[i] = 100.0 * hi / period
        dn[i] = 100.0 * lo / period
    return up, dn


def cci(candles: List[Candle], period: int = 20) -> List[Optional[float]]:
    tp = [(c.h + c.l + c.c) / 3 for c in candles]
    sma_tp = sma(tp, period)
    out: List[Optional[float]] = [None] * len(candles)
    for i in range(period - 1, len(candles)):
        s = sma_tp[i]
        if s is None:
            continue
        md = sum(abs(tp[j] - s) for j in range(i - period + 1, i + 1)) / period
        out[i] = (tp[i] - s) / (0.015 * md) if md > 0 else None
    return out


def williams_r(candles: List[Candle], period: int = 14) -> List[Optional[float]]:
    out: List[Optional[float]] = [None] * len(candles)
    for i in range(period - 1, len(candles)):
        w = candles[i - period + 1 : i + 1]
        hh = max(c.h for c in w)
        ll = min(c.l for c in w)
        out[i] = -50.0 if hh == ll else (hh - candles[i].c) / (hh - ll) * -100.0
    return out


def roc(candles: List[Candle], period: int = 12) -> List[Optional[float]]:
    out: List[Optional[float]] = [None] * len(candles)
    for i in range(period, len(candles)):
        prev = candles[i - period].c
        out[i] = (candles[i].c - prev) / prev * 100.0 if prev else None
    return out


def mfi(candles: List[Candle], period: int = 14) -> List[Optional[float]]:
    tp = [(c.h + c.l + c.c) / 3 for c in candles]
    out: List[Optional[float]] = [None] * len(candles)
    for i in range(period, len(candles)):
        pos = 0.0
        neg = 0.0
        for j in range(i - period + 1, i + 1):
            mf = tp[j] * candles[j].v
            pr = tp[j] - tp[j - 1]
            if pr > 0:
                pos += mf
            elif pr < 0:
                neg += mf
        out[i] = 100.0 if neg == 0 else 100.0 - 100.0 / (1 + pos / neg)
    return out


def cmf(candles: List[Candle], period: int = 20) -> List[Optional[float]]:
    out: List[Optional[float]] = [None] * len(candles)
    for i in range(period - 1, len(candles)):
        mfv = 0.0
        vv = 0.0
        for j in range(i - period + 1, i + 1):
            c = candles[j]
            rng = c.h - c.l
            m = ((c.c - c.l) - (c.h - c.c)) / rng if rng > 0 else 0.0
            mfv += m * c.v
            vv += c.v
        out[i] = mfv / vv if vv > 0 else None
    return out


def awesome_oscillator(candles: List[Candle]) -> List[Optional[float]]:
    med = [(c.h + c.l) / 2 for c in candles]
    s5 = sma(med, 5)
    s34 = sma(med, 34)
    return [
        (a - b) if (a is not None and b is not None) else None
        for a, b in zip(s5, s34)
    ]


def ultimate_oscillator(candles: List[Candle]) -> List[Optional[float]]:
    p1, p2, p3 = 7, 14, 28
    n = len(candles)
    bp: List[Optional[float]] = [None] * n
    tr: List[Optional[float]] = [None] * n
    for i in range(1, n):
        tl = min(candles[i].l, candles[i - 1].c)
        bp[i] = candles[i].c - tl
        tr[i] = max(candles[i].h, candles[i - 1].c) - tl
    out: List[Optional[float]] = [None] * n
    for i in range(p3, n):
        a1 = b1 = a2 = b2 = a3 = b3 = 0.0
        ok = True
        for j in range(i - p3 + 1, i + 1):
            if bp[j] is None or tr[j] is None:
                ok = False
                break
            a1 += bp[j]
            b1 += tr[j]
            if j > i - p1 + 1:
                a2 += bp[j]
                b2 += tr[j]
            if j > i - p2 + 1:
                a3 += bp[j]
                b3 += tr[j]
        if not ok or b1 == 0:
            continue
        out[i] = 100.0 * (4 * (a2 / b2) + 2 * (a3 / b3) + (a1 / b1)) / 7
    return out


def stochastic_rsi(candles: List[Candle], period: int = 14, smooth_k: int = 3, smooth_d: int = 3):
    closes = [c.c for c in candles]
    r = rsi(closes, period)
    raw: List[Optional[float]] = [None] * len(candles)
    for i in range(len(candles)):
        if r[i] is None or i < period - 1 or any(r[j] is None for j in range(i - period + 1, i + 1)):
            continue
        w = r[i - period + 1 : i + 1]
        lo = min(w)
        hi = max(w)
        raw[i] = None if hi == lo else (r[i] - lo) / (hi - lo) * 100.0
    k = sma([v if v is not None else 0.0 for v in raw], smooth_k)
    k = [kv if rv is not None else None for kv, rv in zip(k, raw)]
    d = sma([kv if kv is not None else 0.0 for kv in k], smooth_d)
    d = [dv if kv is not None else None for dv, kv in zip(d, k)]
    return k, d


# ── previously-missing implementations (schema promised them) ──

def vwma(closes: List[float], volumes: List[float], period: int = 20) -> List[Optional[float]]:
    out: List[Optional[float]] = [None] * len(closes)
    for i in range(period - 1, len(closes)):
        pv = 0.0
        vv = 0.0
        for j in range(i - period + 1, i + 1):
            v = volumes[j] or 0.0
            pv += closes[j] * v
            vv += v
        out[i] = pv / vv if vv > 0 else None
    return out


def vwap(closes: List[float], volumes: List[float]) -> List[Optional[float]]:
    """Cumulative VWAP over the whole series."""
    out: List[Optional[float]] = [None] * len(closes)
    pv = 0.0
    vv = 0.0
    for i, (c, v) in enumerate(zip(closes, volumes)):
        vol = v or 0.0
        pv += c * vol
        vv += vol
        out[i] = pv / vv if vv > 0 else None
    return out


def bollinger(closes: List[float], period: int = 20, mult: float = 2.0):
    """Returns (upper, middle, lower) lists."""
    mid = sma(closes, period)
    upper: List[Optional[float]] = [None] * len(closes)
    lower: List[Optional[float]] = [None] * len(closes)
    for i in range(period - 1, len(closes)):
        m = mid[i]
        if m is None:
            continue
        variance = sum((closes[j] - m) ** 2 for j in range(i - period + 1, i + 1)) / period
        sd = variance ** 0.5
        upper[i] = m + mult * sd
        lower[i] = m - mult * sd
    return upper, mid, lower


def keltner(candles: List[Candle], period: int = 20, mult: float = 2.0):
    """Returns (upper, middle(EMA), lower) lists."""
    closes = [c.c for c in candles]
    mid = ema(closes, period)
    atr_series = atr(candles, period)
    upper: List[Optional[float]] = [None] * len(candles)
    lower: List[Optional[float]] = [None] * len(candles)
    for i in range(len(candles)):
        if mid[i] is None or atr_series[i] is None:
            continue
        upper[i] = mid[i] + mult * atr_series[i]
        lower[i] = mid[i] - mult * atr_series[i]
    return upper, mid, lower


def donchian(candles: List[Candle], period: int = 20):
    """Returns (upper, middle, lower) lists."""
    upper: List[Optional[float]] = [None] * len(candles)
    lower: List[Optional[float]] = [None] * len(candles)
    for i in range(period - 1, len(candles)):
        w = candles[i - period + 1 : i + 1]
        upper[i] = max(c.h for c in w)
        lower[i] = min(c.l for c in w)
    mid = [(u + l) / 2 if (u is not None and l is not None) else None for u, l in zip(upper, lower)]
    return upper, mid, lower


def pivot(candles: List[Candle], period: int = 24) -> List[Optional[float]]:
    """Classic floor pivot (PP) from the LAST completed window of `period` bars.
    Repaints per window; value only valid on the last bar of each window."""
    out: List[Optional[float]] = [None] * len(candles)
    for i in range(period, len(candles) + 1):
        w = candles[i - period : i]
        out[min(i, len(candles)) - 1] = (max(c.h for c in w) + min(c.l for c in w) + w[-1].c) / 3
    return out


def fibonacci(candles: List[Candle], period: int = 60) -> List[Optional[float]]:
    """Fibonacci 0.382 retracement level of the lookback high-low range (nearest below price)."""
    out: List[Optional[float]] = [None] * len(candles)
    for i in range(period - 1, len(candles)):
        w = candles[i - period + 1 : i + 1]
        hi = max(c.h for c in w)
        lo = min(c.l for c in w)
        out[i] = hi - (hi - lo) * 0.382
    return out


def support_resistance(candles: List[Candle], period: int = 20) -> List[Optional[float]]:
    """Nearest pivot level: midpoint of prior-window high and low (classic S/R proxy)."""
    out: List[Optional[float]] = [None] * len(candles)
    for i in range(period, len(candles)):
        w = candles[i - period : i]
        out[i] = (max(c.h for c in w) + min(c.l for c in w)) / 2
    return out


def _true_body_dir(c: Candle) -> int:
    return 1 if c.c >= c.o else -1


def hma(closes: List[float], period: int = 21) -> List[Optional[float]]:
    """Hull Moving Average."""
    if period < 2:
        period = 2
    half = max(2, period // 2)
    sq = max(2, int(period ** 0.5))
    w1 = wma(closes, half)
    w2 = wma(closes, period)
    diff = [
        (2 * a - b) if (a is not None and b is not None) else None
        for a, b in zip(w1, w2)
    ]
    filled = [d if d is not None else 0.0 for d in diff]
    smoothed = wma(filled, sq)
    return [s if d is not None else None for s, d in zip(smoothed, diff)]


def tema(closes: List[float], period: int = 20) -> List[Optional[float]]:
    """Triple EMA: 3*e1 - 3*e2 + e3."""
    e1 = ema(closes, period)
    e2 = ema([v if v is not None else 0.0 for v in e1], period)
    e3 = ema([v if v is not None else 0.0 for v in e2], period)
    return [
        (3 * a - 3 * b + c) if (a is not None and b is not None and c is not None) else None
        for a, b, c in zip(e1, e2, e3)
    ]


def dema(closes: List[float], period: int = 20) -> List[Optional[float]]:
    """Double EMA: 2*e1 - e2."""
    e1 = ema(closes, period)
    e2 = ema([v if v is not None else 0.0 for v in e1], period)
    return [
        (2 * a - b) if (a is not None and b is not None) else None
        for a, b in zip(e1, e2)
    ]


def obv_c(candles: List[Candle]) -> List[float]:
    """OBV from candle objects (obv() kept for close/volume lists)."""
    out: List[float] = []
    acc = 0.0
    for i, c in enumerate(candles):
        if i:
            if c.c > candles[i - 1].c:
                acc += c.v
            elif c.c < candles[i - 1].c:
                acc -= c.v
        out.append(acc)
    return out


def obv(candles_or_closes, volumes: Optional[List[float]] = None):
    """OBV — accepts either Candle objects or (closes, volumes) lists."""
    if volumes is not None:
        closes = candles_or_closes
        out: List[float] = []
        acc = 0.0
        for i in range(len(closes)):
            if i:
                if closes[i] > closes[i - 1]:
                    acc += volumes[i] or 0.0
                elif closes[i] < closes[i - 1]:
                    acc -= volumes[i] or 0.0
            out.append(acc)
        return out
    return obv_c(candles_or_closes)


def engulfing(closes: List[float]) -> List[Optional[float]]:
    """Bullish engulfing = 1, bearish = -1, else 0. Needs candles, see engulfing_c()."""
    raise ValueError("engulfing requires candle data — use engine dispatcher")


def engulfing_c(candles: List[Candle]) -> List[Optional[float]]:
    out: List[Optional[float]] = [0.0] * len(candles)
    for i in range(1, len(candles)):
        prev, cur = candles[i - 1], candles[i]
        if prev.c < prev.o and cur.c > cur.o and cur.c >= prev.o and cur.o <= prev.c:
            out[i] = 1.0
        elif prev.c > prev.o and cur.c < cur.o and cur.c <= prev.o and cur.o >= prev.c:
            out[i] = -1.0
    return out


def pinbar_c(candles: List[Candle]) -> List[Optional[float]]:
    """Bullish pin (long lower wick) = 1, bearish pin = -1, else 0."""
    out: List[Optional[float]] = [0.0] * len(candles)
    for i, c in enumerate(candles):
        rng = c.h - c.l
        if rng <= 0:
            continue
        body = abs(c.c - c.o)
        lower_wick = min(c.c, c.o) - c.l
        upper_wick = c.h - max(c.c, c.o)
        if lower_wick > body * 2 and lower_wick / rng > 0.6:
            out[i] = 1.0
        elif upper_wick > body * 2 and upper_wick / rng > 0.6:
            out[i] = -1.0
    return out


def inside_bar_c(candles: List[Candle]) -> List[Optional[float]]:
    """1 when current candle is an inside bar (h/l within previous h/l)."""
    out: List[Optional[float]] = [0.0] * len(candles)
    for i in range(1, len(candles)):
        prev, cur = candles[i - 1], candles[i]
        if cur.h <= prev.h and cur.l >= prev.l:
            out[i] = 1.0
    return out


def envelope(candles_or_closes, period: int = 20, percent: float = 5.0):
    """Returns (upper, middle(SMA), lower). Accepts Candle objects or close list."""
    closes = [c.c for c in candles_or_closes] if candles_or_closes and hasattr(candles_or_closes[0], "c") else list(candles_or_closes)
    mid = sma(closes, period)
    pct = percent / 100.0
    upper: List[Optional[float]] = [None] * len(closes)
    lower: List[Optional[float]] = [None] * len(closes)
    for i, m in enumerate(mid):
        if m is None:
            continue
        upper[i] = m * (1 + pct)
        lower[i] = m * (1 - pct)
    return upper, mid, lower
