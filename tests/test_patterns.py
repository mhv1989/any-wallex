"""Tests for candlestick patterns."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot import patterns
from conftest import mk

CFG = {"patterns": {"pinbar_wick_ratio": 2.0, "pinbar_max_opp": 0.5,
                    "morning_star_mid": 0.3, "hammer_lookback": 5}}


def test_bullish_engulfing():
    prev = mk(1, 105, 106, 99, 100)      # bearish
    cur = mk(2, 99, 107, 98.5, 106)       # bullish, engulfs
    assert patterns.is_bullish_engulfing([prev, cur])


def test_not_engulfing_when_smaller_body():
    prev = mk(1, 108, 109, 99, 100)       # big bear body
    cur = mk(2, 100, 103, 99.5, 102)      # small bull body
    assert not patterns.is_bullish_engulfing([prev, cur])


def test_bullish_pinbar():
    c = mk(1, 100, 100.7, 95, 100.5)      # lower wick 5 vs body 0.5, upper wick 0.2
    assert patterns.is_bullish_pinbar([c], 2.0, 0.5)


def test_pinbar_rejects_long_upper_wick():
    c = mk(1, 100, 106, 99, 100.5)        # long UPPER wick
    assert not patterns.is_bullish_pinbar([c], 2.0, 0.5)


def test_hammer_after_decline():
    cs = [mk(i, 110 - i * 2, 111 - i * 2, 108 - i * 2, 109 - i * 2) for i in range(5)]
    h = mk(9, 100, 100.6, 95, 100.4)      # hammer at the lows
    assert patterns.is_hammer(cs + [h], lookback=5, wick_ratio=2.0)


def test_morning_star():
    a = mk(1, 110, 111, 104, 105)         # big bear
    b = mk(2, 104.5, 105, 103.8, 104.2)   # tiny body below a's body
    c = mk(3, 104.5, 110, 104, 109)       # strong bull into upper half of a
    assert patterns.is_morning_star([a, b, c], 0.3)


def test_detect_pattern_priority():
    a = mk(1, 110, 111, 104, 105)
    b = mk(2, 104.5, 105, 103.8, 104.2)
    c = mk(3, 104.5, 110, 104, 109)
    assert patterns.detect_pattern([a, b, c], CFG) == "morning_star"


def test_no_pattern_on_plain_candles():
    cs = [mk(i, 100 + i, 101 + i, 99 + i, 100.5 + i) for i in range(6)]
    assert patterns.detect_pattern(cs, CFG) is None
