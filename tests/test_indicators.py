"""Tests for indicators — RSI/ATR/SMA/EMA correctness."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot import indicators
from bot.models import Candle


def test_sma_basic():
    out = indicators.sma([1, 2, 3, 4, 5], 3)
    assert out[0] is None and out[1] is None
    assert out[2] == 2.0
    assert out[4] == 4.0


def test_ema_converges():
    vals = [10.0] * 50
    out = indicators.ema(vals, 10)
    assert abs(out[-1] - 10.0) < 1e-6


def test_rsi_all_gains_is_100():
    closes = [float(i) for i in range(1, 40)]
    out = indicators.rsi(closes, 14)
    assert out[-1] is not None and out[-1] > 99.9


def test_rsi_all_losses_is_low():
    closes = [float(100 - i) for i in range(40)]
    out = indicators.rsi(closes, 14)
    assert out[-1] is not None and out[-1] < 1.0


def test_rsi_range():
    import random
    random.seed(7)
    closes = [100.0]
    for _ in range(200):
        closes.append(closes[-1] + random.uniform(-2, 2))
    out = indicators.rsi(closes, 14)
    for v in out:
        if v is not None:
            assert 0.0 <= v <= 100.0


def test_atr_positive_and_none_prefix():
    cs = [Candle(ts=i * 3600, o=100 + i, h=102 + i, l=99 + i, c=101 + i, v=1) for i in range(40)]
    out = indicators.atr(cs, 14)
    assert out[13] is None
    assert out[14] is not None and out[14] > 0
    assert all(v is None or v > 0 for v in out)


def test_volume_ratio_no_lookahead():
    cs = [Candle(ts=i, o=1, h=1, l=1, c=1, v=10.0) for i in range(11)]
    cs[-1] = Candle(ts=10, o=1, h=1, l=1, c=1, v=20.0)
    r = indicators.volume_ratio(cs, 10)
    assert r is not None and abs(r - 2.0) < 1e-9
