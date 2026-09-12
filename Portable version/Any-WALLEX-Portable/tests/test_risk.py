"""Tests for the risk manager: sizing, drawdown rules, trailing, partials."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot.models import Position
from bot.risk import RiskManager

CFG = {"risk": {
    "risk_per_trade_pct": 1.0, "max_positions": 4, "max_exposure_pct": 12.0,
    "breakeven_rr": 1.5, "trailing_atr_mult": 1.2, "partial_rr": 2.5,
    "partial_close_pct": 50.0, "drawdown_half_pct": 8.0, "drawdown_stop_pct": 12.0,
}}


def make_pos(entry=100.0, stop=98.0, qty=1.0):
    return Position(id="t1", symbol="BTCUSDT", qty=qty, entry=entry, stop=stop,
                    opened_ts=0, atr_at_entry=2.0, peak_price=entry, initial_qty=qty)


def test_size_1pct_risk():
    rm = RiskManager(CFG)
    r = rm.size_position(equity=10_000, entry=100.0, stop=98.0, open_positions=[])
    assert r.allowed
    # risk 100 USDT / distance 2 => qty 50 => notional 5000, but exposure cap 12% = 1200
    assert r.notional <= 10_000 * 0.12 + 1e-6
    assert r.qty > 0


def test_size_respects_risk_when_exposure_room():
    rm = RiskManager(CFG)
    # wide stop => small notional, under exposure cap
    r = rm.size_position(equity=10_000, entry=100.0, stop=80.0, open_positions=[])
    assert r.allowed
    assert abs(r.qty - (100.0 / 20.0)) < 1e-9  # 1% = 100 / 20 distance = 5 units
    assert r.notional == 500.0


def test_max_positions_blocks():
    rm = RiskManager(CFG)
    open_pos = [make_pos() for _ in range(4)]
    r = rm.size_position(10_000, 100, 98, open_pos)
    assert not r.allowed and "پوزیشن" in r.reason


def test_drawdown_8pct_halves_size():
    rm = RiskManager(CFG)
    normal = rm.size_position(10_000, 100, 80, [], drawdown_pct=0.0)
    halved = rm.size_position(10_000, 100, 80, [], drawdown_pct=9.0)
    assert halved.allowed and abs(halved.qty - normal.qty / 2) < 1e-9


def test_drawdown_12pct_stops_entries():
    rm = RiskManager(CFG)
    r = rm.size_position(10_000, 100, 98, [], drawdown_pct=12.5)
    assert not r.allowed


def test_breakeven_at_rr_1_5():
    rm = RiskManager(CFG)
    p = make_pos(entry=100, stop=98)
    actions = rm.manage(p, price=103.0, atr_now=2.0)  # RR = 3/2 = 1.5
    assert actions["breakeven"]
    assert p.stop >= 100.0
    assert p.breakeven_done


def test_trailing_uses_1_2_atr():
    rm = RiskManager(CFG)
    p = make_pos(entry=100, stop=98)
    rm.manage(p, price=103.0, atr_now=2.0)   # trigger BE + trailing
    actions = rm.manage(p, price=110.0, atr_now=2.0)
    assert actions["trail_start"]
    # peak 110 - 1.2*2 = 107.6
    assert abs(p.stop - 107.6) < 1e-9


def test_partial_close_at_rr_2_5():
    rm = RiskManager(CFG)
    p = make_pos(entry=100, stop=98, qty=10.0)
    actions = rm.manage(p, price=105.0, atr_now=2.0)  # RR = 5/2 = 2.5
    assert actions["partial"]
    assert abs(actions["partial_qty"] - 5.0) < 1e-9
    assert p.partial_taken


def test_stop_hit_detection():
    rm = RiskManager(CFG)
    p = make_pos(entry=100, stop=98)
    assert rm.check_stop(p, low_price=97.5, close_price=97.9) == "stop"
    assert rm.check_stop(p, low_price=98.5, close_price=99.0) is None


def test_trailing_stop_label():
    rm = RiskManager(CFG)
    p = make_pos(entry=100, stop=98)
    p.trailing_on = True
    assert rm.check_stop(p, low_price=97.0, close_price=97.5) == "trailing_stop"
