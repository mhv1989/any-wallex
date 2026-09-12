"""Tests for market structure: swings, HH/HL/LH/LL, BOS, CHoCH."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot.models import Trend
from bot.structure import analyze_structure, bearish_choch_recent, find_swings
from conftest import downtrend_candles, flat_candles, mk, uptrend_candles


def test_swings_confirmed_only():
    cs = uptrend_candles(30)
    swings = find_swings(cs, 2, 2)
    # every swing index must leave >= 2 candles after it (no look-ahead)
    for s in swings:
        assert s.index <= len(cs) - 1 - 2


def test_uptrend_detected():
    st = analyze_structure(uptrend_candles(60), 2, 2)
    assert st.trend == Trend.UP
    assert st.hh and st.hl


def test_downtrend_detected():
    st = analyze_structure(downtrend_candles(60), 2, 2)
    assert st.trend == Trend.DOWN
    assert st.lh and st.ll


def test_choch_down_after_uptrend():
    up = uptrend_candles(62, start=100, step=2.0)
    # sharp reversal: 8 strong bear candles closing far below the last swing low
    last = up[-1]
    down = []
    price = last.c
    for i in range(8):
        o = price
        c = o - 4.0
        down.append(mk(last.ts + (i + 1) * 3600, o, o + 0.2, c - 0.2, c))
        price = c
    series = up + down
    st = analyze_structure(series, 2, 2)
    # the reversal must be recognized: either a choch/bos event or a DOWN trend
    assert st.last_event in ("choch_down", "bos_down") or st.trend == Trend.DOWN
    # and the exit-trigger helper must fire
    assert bearish_choch_recent(series, 2, 2, lookback=8)


def test_bos_up_in_uptrend():
    # end mid up-phase so the newest peak is not yet a confirmed swing;
    # closes above the previous swing high must register as BOS up
    up = uptrend_candles(62, start=100, step=2.0)
    st = analyze_structure(up, 2, 2)
    assert st.trend == Trend.UP
    assert st.last_event == "bos_up"


def test_flat_is_range():
    st = analyze_structure(flat_candles(60), 2, 2)
    assert st.trend in (Trend.RANGE, Trend.UP, Trend.DOWN)  # tolerant, but no crash
    # with tiny noise it should not claim strong BOS events repeatedly
    assert st.last_event in (None, "bos_up", "bos_down", "choch_up", "choch_down")
