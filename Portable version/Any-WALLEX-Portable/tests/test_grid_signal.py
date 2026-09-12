"""Tests for grid + signal scoring + backtest no-look-ahead property."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot import grid as gridmod
from bot.backtest import HistoryView
from bot.models import Candle
from conftest import mk


def test_grid_25_levels():
    cs = [mk(i * 86400, 100, 100 + (i % 7), 95 + (i % 5), 99 + (i % 6)) for i in range(40)]
    active, levels, sup, res = gridmod.build_grid(cs, {"grid": {"levels": 25, "lookback_days": 30, "min_range_pct": 4.0}})
    assert active
    assert len(levels) == 25
    assert abs(levels[0] - sup) < 1e-9
    assert abs(levels[-1] - res) < 1e-9


def test_grid_disabled_under_4pct():
    # range ~2% -> disabled
    cs = [mk(i * 86400, 100, 101, 99, 100) for i in range(40)]
    active, levels, sup, res = gridmod.build_grid(cs, {"grid": {"levels": 25, "lookback_days": 30, "min_range_pct": 4.0}})
    assert not active
    assert levels == []


def test_grid_size_factor_bounded():
    f = gridmod.grid_size_factor(True, 100.0, [90.0 + i for i in range(25)], {"grid": {"bands_per_position": 3}})
    assert 0.05 <= f <= 1.0


def test_history_view_no_lookahead():
    """A candle must not be visible before its close time (ts + res)."""
    cs = [mk(i * 900, 1, 1, 1, 1) for i in range(10)]
    hv = HistoryView(cs, 900)
    # at sim time = open of candle 5 + 899s, candle 5 is NOT closed yet
    visible = hv.closed_up_to(5 * 900 + 899)
    assert all(c.ts <= 4 * 900 for c in visible)
    # at exactly close time, it IS visible
    visible2 = hv.closed_up_to(5 * 900 + 900)
    assert visible2[-1].ts == 5 * 900


def test_signal_rejects_insufficient_data():
    from bot.signal import build_signal
    cfg = {"strategy": {"min_confirmations": 6, "min_rr": 1.5, "rsi_period": 14,
                        "atr_period": 14, "swing_left": 2, "swing_right": 2}}
    few = [mk(i * 3600, 100, 101, 99, 100) for i in range(5)]
    assert build_signal("X", few, few, few, cfg) is None
