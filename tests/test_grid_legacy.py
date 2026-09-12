"""Tests for the Legacy Grid strategy module (bot/grid_legacy.py)."""
from __future__ import annotations

import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot.grid_legacy import (  # noqa: E402
    GridManager,
    GridRunner,
    build_levels,
    gap_pct,
    level_weights,
)
from bot.models import Position  # noqa: E402

PROF = {
    "symbol": "ICPUSDT", "mode": "spot", "direction": "long",
    "range_min": 2.0, "range_max": 5.0, "grid_count": 20, "spacing": "arithmetic",
    "total_quote": 1000.0, "allocation": "pyramid", "profit_reinvest": "per_grid",
    "activation_mode": "now", "ai_autofix": True,
}


class FakeBroker:
    def __init__(self, cash=1000.0):
        self.cash = cash
        self.fee = 0.002
        self.positions = {}
        self.last_reject = ""

    def set_price(self, s, p):
        self._px = p

    def last_price(self, s):
        return getattr(self, "_px", 0.0)

    def open_long(self, sym, qty, price, pos):
        cost = qty * price * (1 + self.fee)
        if cost > self.cash:
            self.last_reject = "insufficient cash"
            return False
        self.cash -= cost
        pos.entry = price
        pos.qty = qty
        self.positions[pos.id] = pos
        return True

    def close_long(self, pos, price, qty=None, reason=""):
        qty = qty or pos.qty
        proceeds = qty * price * (1 - self.fee)
        self.cash += proceeds
        pnl = (price - pos.entry) * qty - proceeds * self.fee
        pos.pnl += pnl
        pos.qty -= qty
        if pos.qty <= 1e-12:
            pos.qty = 0
            pos.state = "closed"
            self.positions.pop(pos.id, None)
        return pnl

    def open_short(self, *a, **k):
        return False


def test_levels_arithmetic_geometric():
    lv = build_levels(2.0, 5.0, 20, "arithmetic")
    assert len(lv) == 20 and abs(lv[0] - 2.0) < 1e-9 and abs(lv[-1] - 5.0) < 1e-9
    g = build_levels(2.0, 5.0, 15, "geometric")
    ratio = g[1] / g[0]
    assert all(abs(g[i + 1] / g[i] - ratio) < 1e-6 for i in range(len(g) - 2))
    w = level_weights(20, "pyramid")
    assert w[0] > w[-1]


def test_spot_grid_walk_completes_pairs():
    r = GridRunner(PROF)
    r.running = True
    b = FakeBroker()
    price = 3.5
    for i in range(600):
        price = 3.5 - i * 0.004 if i < 300 else 0.3 + (i - 300) * 0.0105
        price = max(2.05, min(4.9, price))
        r.tick(price, b)
    assert r.cum_profit > 0
    assert r.fills >= 1


def test_spillover_gap_marks_missed_then_recovers():
    r = GridRunner({**PROF, "grid_count": 10})
    r.running = True
    b = FakeBroker(cash=300)
    r.tick(3.5, b)
    r.tick(2.05, b)  # gap across ~7 levels in one tick
    missed = [m for m in r.missed if m["action"] == "buy"]
    assert len(missed) >= 1
    r.tick(4.0, b)  # recovery leg fills held levels
    assert r.fills >= 1


def test_activation_price_gate():
    r = GridRunner({**PROF, "activation_mode": "price", "activation_price": 2.5, "grid_count": 10})
    r.running = True
    b = FakeBroker()
    r.tick(3.0, b)
    assert not r.activated
    r.tick(2.4, b)
    assert r.activated


def test_margin_directions_rejected_on_spot():
    import pytest
    with pytest.raises(ValueError):
        GridRunner({**PROF, "direction": "short"})  # spot forbids short
    r = GridRunner({**PROF, "mode": "margin", "direction": "short", "leverage": 2})
    assert r.p["direction"] == "short" and r.p["leverage"] == 2


def test_backtest_metrics():
    rd = random.Random(1)
    p = 100.0
    cs = []
    for i in range(2000):
        o = p
        p = max(80, min(120, p + rd.uniform(-1.5, 1.5)))
        cs.append(type("C", (), {"ts": 1_700_000_000 + i * 3600, "o": o,
                                 "h": max(o, p) + 0.5, "l": min(o, p) - 0.5,
                                 "c": p, "v": 10})())
    rb = GridRunner({**PROF, "range_min": 80, "range_max": 120, "grid_count": 15,
                     "allocation": "even", "profit_reinvest": "none"})
    m = rb.backtest(cs, fee_pct=0.2)
    assert m["trades"] > 50
    assert 0 <= m["win_rate"] <= 100
    assert m["fee_share_pct"] > 0


class _B:
    fee = 0.002

    @staticmethod
    def last_price(s):
        return 0.0


class _Eng:
    broker = _B()
    client = None


def test_manager_crud_persist_and_legacy_undeletable(tmp_path):
    gm = GridManager(_Eng(), None, str(tmp_path), tick_sec=60)
    r, err = gm.create(PROF)
    assert not err, err
    # user-created grids are DELETABLE (only the strategies-library Legacy is protected)
    assert r.p["legacy"] is False
    ok, _ = gm.start(r.id)
    assert ok and gm.running_profiles()
    ok, derr = gm.delete(r.id)
    assert not ok and "در حال اجراست" in derr  # running guard still applies
    ok, _ = gm.stop(r.id)
    assert ok
    r2, uerr = gm.update(r.id, {"grid_count": 25})
    assert not uerr and r2.p["grid_count"] == 25
    assert r2.p["legacy"] is False  # identity preserved across edits
    gm.save()
    gm2 = GridManager(_Eng(), None, str(tmp_path), tick_sec=60)
    assert r2.id in gm2.runners
    # delete now succeeds on a stopped user grid
    ok, derr = gm2.delete(r2.id)
    assert ok, derr
    assert r2.id not in gm2.runners


def test_preview_fee_gap_warning():
    import tempfile
    gm = GridManager(_Eng(), None, tempfile.mkdtemp(), tick_sec=60)
    # dense grid + huge fee -> warning must fire
    pv = gm.preview({**PROF, "grid_count": 100}, price=3.5)
    assert pv["ok"]
    # fee 2% per side vs gap ~0.66% -> warning
    gm.engine.broker.fee = 0.02
    pv2 = gm.preview({**PROF, "grid_count": 100}, price=3.5)
    assert pv2["fee_gap_warning"]
