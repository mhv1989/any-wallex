"""Tests for the local Wallex rules layer and paper-broker funding."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot.broker import PaperBroker, PaperMarginBroker  # noqa: E402
from bot.models import Position  # noqa: E402
from bot.wallex_rules import WallexRules  # noqa: E402


def mk_pos(symbol="BTCUSDT", qty=1.0, entry=100.0, side="long", risk_coef=1.0):
    return Position(id=f"t-{symbol}-{side}", symbol=symbol, qty=qty, entry=entry,
                    stop=entry * 0.95, opened_ts=1_000_000, side=side, risk_coef=risk_coef)


# ── WallexRules ──────────────────────────────────────────────────────
def test_spot_min_order_rejected():
    r = WallexRules(min_order_usdt=10.0)
    assert r.check_spot_order("BTCUSDT", 0.05, 100.0) is not None   # 5 USDT < 10
    assert r.check_spot_order("BTCUSDT", 0.2, 100.0) is None        # 20 USDT ok


def test_spot_market_override():
    r = WallexRules(min_order_usdt=10.0, market_overrides={"BTCUSDT": {"min_order_usdt": 25}})
    assert r.check_spot_order("BTCUSDT", 0.2, 100.0) is not None    # 20 < 25 override
    assert r.check_spot_order("ETHUSDT", 0.2, 100.0) is None        # default 10


def test_margin_collateral_bounds():
    r = WallexRules(min_collateral_usdt=10.0, max_collateral_usdt=1000.0)
    assert r.check_margin_order("BTCUSDT", 5.0, 2.0, 100.0, 100.0) is not None     # below min
    assert r.check_margin_order("BTCUSDT", 5000.0, 2.0, 100.0, 100.0) is not None  # above max
    assert r.check_margin_order("BTCUSDT", 50.0, 2.0, 100.0, 100.0) is None


def test_margin_leverage_cap():
    r = WallexRules(max_risk_coef=3.0)
    assert r.check_margin_order("BTCUSDT", 50.0, 5.0, 100.0, 100.0) is not None  # 5x > 3x cap
    assert r.check_margin_order("BTCUSDT", 50.0, 3.0, 100.0, 100.0) is None
    assert r.clamp_risk_coef(9.0) == 3.0
    assert r.clamp_risk_coef(0.5) == 1.0


def test_price_band():
    r = WallexRules(price_band_pct=5.0)
    assert r.check_price_band("BTCUSDT", 106.0, 100.0) is not None  # +6% outside band
    assert r.check_price_band("BTCUSDT", 104.0, 100.0) is None      # +4% inside


def test_loan_and_liquidation_math():
    loan = WallexRules.loan_calc(100.0, 2.0)
    assert loan["notional"] == 200.0
    assert loan["loan"] == 100.0
    liq_long = WallexRules.liquidation_price(100.0, "long", 2.0, 0.01)
    liq_short = WallexRules.liquidation_price(100.0, "short", 2.0, 0.01)
    assert liq_long < 100.0 < liq_short
    assert abs(liq_long - 100.0 * (1 - 0.99 / 2)) < 1e-9


def test_from_config():
    cfg = {"margin": {"max_risk_coef": 4.0, "min_collateral_usdt": 20},
           "paper": {"min_order_usdt": 15}}
    r = WallexRules.from_config(cfg)
    assert r.max_risk_coef == 4.0
    assert r.min_collateral_usdt == 20.0
    assert r.min_order_usdt == 15.0


# ── PaperBroker funding ──────────────────────────────────────────────
def test_spot_deposit_withdraw():
    b = PaperBroker(starting_capital=1000.0)
    assert b.deposit(500.0) == 1500.0
    assert b.cash == 1500.0
    assert b.starting_capital == 1500.0
    assert b.withdraw(300.0) == 1200.0
    assert b.cash == 1200.0
    # withdraw never exceeds free cash
    assert b.withdraw(99999.0) == 1200.0
    assert b.cash == 1200.0


def test_spot_open_respects_min_order():
    b = PaperBroker(starting_capital=1000.0, rules=WallexRules(min_order_usdt=10.0))
    pos = mk_pos(qty=0.05, entry=100.0)  # 5 USDT < min
    assert b.open_long("BTCUSDT", 0.05, 100.0, pos) is False
    assert b.last_reject != ""


def test_spot_open_ok():
    b = PaperBroker(starting_capital=1000.0, rules=WallexRules(min_order_usdt=10.0))
    pos = mk_pos(qty=0.5, entry=100.0)  # 50 USDT
    assert b.open_long("BTCUSDT", 0.5, 100.0, pos) is True
    assert pos.entry > 100.0  # slippage applied
    assert b.cash < 1000.0


# ── PaperMarginBroker funding + rules ────────────────────────────────
def test_margin_deposit_withdraw():
    b = PaperMarginBroker(starting_capital=1000.0)
    assert b.deposit(500.0) == 1500.0
    assert b.withdraw(200.0) == 1300.0
    assert b.cash == 1300.0


def test_margin_open_rejects_below_min_collateral():
    b = PaperMarginBroker(starting_capital=1000.0, rules=WallexRules(min_collateral_usdt=10.0))
    pos = mk_pos(qty=0.05, entry=100.0, risk_coef=2.0)  # notional 5, collateral 2.5 < 10
    assert b.open_long("BTCUSDT", 0.05, 100.0, pos) is False
    assert b.last_reject != ""


def test_margin_open_ok_and_leverage_clamped():
    b = PaperMarginBroker(starting_capital=1000.0,
                          rules=WallexRules(min_collateral_usdt=10.0, max_risk_coef=3.0))
    pos = mk_pos(qty=1.0, entry=100.0, risk_coef=9.0)  # 9x -> clamped to 3x
    assert b.open_long("BTCUSDT", 1.0, 100.0, pos) is True
    assert pos.risk_coef == 3.0
    assert pos.meta.get("collateral", 0) > 0


def test_margin_short_open_ok():
    b = PaperMarginBroker(starting_capital=1000.0,
                          rules=WallexRules(min_collateral_usdt=10.0))
    pos = mk_pos(qty=1.0, entry=100.0, side="short", risk_coef=2.0)
    assert b.open_short("BTCUSDT", 1.0, 100.0, pos) is True
    assert pos.side == "short"
    assert pos.meta.get("liq_price", 0) > 100.0  # short liq above entry
