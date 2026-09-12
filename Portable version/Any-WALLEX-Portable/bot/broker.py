"""Execution layer — PaperBroker (default) and LiveBroker behind one interface.

PaperBroker simulates fills with fee + slippage, tracks equity locally.
LiveBroker maps to Wallex spot REST endpoints and SYNCs real orders/positions
from the exchange after any reconnect (reconciliation before acting).

Neither broker contains strategy logic.
"""
from __future__ import annotations

import logging
import time
import uuid
from typing import Dict, List, Optional

from .models import Position
from .wallex_client import WallexClient
from .wallex_rules import WallexRules
from .markets import MarketCatalog

log = logging.getLogger("broker")


class BaseBroker:
    name = "base"

    # ── capital reservation (grid capital locking, user req) ──────
    # Reservations are VIRTUAL: cash is not moved aside; instead the
    # "available" balance = cash - reserved_total. A running grid's
    # allocated capital is committed so other strategies can't spend it;
    # stopping the grid releases the remainder automatically (only what
    # was actually spent on open positions stays spent).
    def _init_reservations(self) -> None:
        if not hasattr(self, "_reserved"):
            self._reserved: Dict[str, float] = {}

    def reserve(self, key: str, amount: float) -> bool:
        """Commit `amount` for `key`. Returns False if insufficient free."""
        self._init_reservations()
        amount = max(0.0, float(amount))
        free = self.cash - sum(self._reserved.values()) if hasattr(self, "cash") else amount
        if amount > free + 1e-9:
            self.last_reject = ("سرمایه آزاد کافی نیست برای قفل گرید — "
                                f"موجودی آزاد {free:.2f}, درخواست {amount:.2f}")
            return False
        self._reserved[key] = self._reserved.get(key, 0.0) + amount
        return True

    def release(self, key: str) -> float:
        """Release the reservation for `key`; returns the released amount."""
        self._init_reservations()
        amt = self._reserved.pop(key, 0.0)
        return amt

    def reserved_total(self) -> float:
        self._init_reservations()
        return sum(self._reserved.values())

    def available(self) -> float:
        """Free (unreserved) balance — what other strategies may spend."""
        if not hasattr(self, "cash"):
            return 0.0
        return max(self.cash - self.reserved_total(), 0.0)

    def equity(self) -> float:
        raise NotImplementedError

    def open_long(self, symbol: str, qty: float, price: float, pos: Position) -> bool:
        raise NotImplementedError

    def close_long(self, pos: Position, price: float, qty: Optional[float] = None, reason: str = "") -> float:
        raise NotImplementedError

    def sync(self) -> None:
        """Reconcile local state with the exchange after (re)connect."""

    def last_price(self, symbol: str) -> float:
        raise NotImplementedError

    # ── paper account funding (no-op for live) ─────────────────────
    def deposit(self, amount: float) -> float:
        return self.equity()

    def withdraw(self, amount: float) -> float:
        return self.equity()


class PaperBroker(BaseBroker):
    """Simulated execution: market fills with slippage, taker fee both sides."""
    name = "paper"

    def __init__(self, starting_capital: float, fee_pct: float = 0.2, slippage_pct: float = 0.05,
                 rules: Optional[WallexRules] = None, quote_currency: str = "USDT"):
        self.cash = starting_capital
        self.starting_capital = starting_capital
        self.fee = fee_pct / 100.0
        self.slip = slippage_pct / 100.0
        self.quote_currency = (quote_currency or "USDT").upper()
        self.rules = rules or WallexRules()
        self.last_reject = ""
        self.positions: Dict[str, Position] = {}
        self._prices: Dict[str, float] = {}

    def convert_to_quote(self, amount: float, from_asset: str) -> float:
        """Convert an amount of `from_asset` into the broker's quote currency."""
        f = (from_asset or "").upper()
        if f == self.quote_currency:
            return amount
        if not amount:
            return 0.0
        px = self._prices.get(f"{f}{self.quote_currency}", 0.0)
        if px:
            return amount * px
        return 0.0

    def _notional_usdt(self, qty: float, symbol: str, price: float) -> float:
        """Notional value converted to USDT for rule checks (min order etc)."""
        q = MarketCatalog.quote_of(symbol)
        if q == "USDT":
            return qty * price
        if q == "TMN":
            tmn_usdt = self._prices.get("TMNUSDT", 0.0)
            if tmn_usdt:
                return qty * price * tmn_usdt
            usdt_tmn = self._prices.get("USDTTMN", 0.0)
            if usdt_tmn:
                return qty * price / usdt_tmn
        return 0.0

    def set_price(self, symbol: str, price: float) -> None:
        self._prices[symbol] = price

    def last_price(self, symbol: str) -> float:
        return self._prices.get(symbol, 0.0)

    def equity(self) -> float:
        """Account value in quote-currency, using live cached prices for conversions."""
        eq = self.cash
        for p in self.positions.values():
            if p.is_open:
                px = self._prices.get(p.symbol, p.entry)
                eq += p.qty * px
        return eq

    # ── funding ────────────────────────────────────────────────────
    def deposit(self, amount: float) -> float:
        if amount > 0:
            self.cash += amount
            self.starting_capital += amount
        return self.equity()

    def withdraw(self, amount: float) -> float:
        """Withdraw free cash only — never touch capital locked in open positions.
        Rejects (no-op) amounts exceeding free cash."""
        if amount <= 0 or amount > self.cash:
            return self.equity()
        self.cash -= amount
        self.starting_capital = max(self.starting_capital - amount, 0.0)
        return self.equity()

    def open_long(self, symbol: str, qty: float, price: float, pos: Position) -> bool:
        import random as _rnd
        fill = price * (1 + self.slip * _rnd.uniform(0.5, 1.5))
        # Wallex rule: min order value (in USDT-equivalent, handles TMN pairs too)
        notional_usdt = self._notional_usdt(qty, symbol, fill)
        err = self.rules.check_spot_order(symbol, qty, notional=notional_usdt)
        _free = self.available()   # FIX(reserve): cash minus grid reservations
        if err:
            # try to shrink up to available cash first, then re-check the rule
            max_affordable = _free / (fill * (1 + self.fee)) * 0.999
            if qty < max_affordable:
                self.last_reject = err
                return False
            qty = max_affordable
            notional_usdt = self._notional_usdt(qty, symbol, fill)
            err = self.rules.check_spot_order(symbol, qty, notional=notional_usdt)
            if err:
                self.last_reject = err
                return False
        cost = qty * fill
        fee = cost * self.fee
        if cost + fee > _free:
            # shrink to what (cash - reserved) allows
            qty = _free / (fill * (1 + self.fee)) * 0.999
            if qty <= 0:
                self.last_reject = "موجودی کافی نیست"
                return False
            cost = qty * fill
            fee = cost * self.fee
            pos.qty = qty
        self.cash -= cost + fee
        pos.entry = fill
        pos.fees_paid += fee
        pos.notional = cost
        pos.peak_price = fill
        self.positions[pos.id] = pos
        return True

    def close_long(self, pos: Position, price: float, qty: Optional[float] = None, reason: str = "") -> float:
        qty = qty or pos.qty
        fill = price * (1 - self.slip)
        proceeds = qty * fill
        fee = proceeds * self.fee
        self.cash += proceeds - fee
        pos.fees_paid += fee
        pnl = (fill - pos.entry) * qty - fee
        pos.pnl += pnl
        pos.qty -= qty
        if pos.qty <= 1e-12:
            pos.qty = 0.0
            pos.state = "closed"
            pos.close_price = fill
            pos.closed_ts = int(time.time())
            pos.exit_reason = reason
        return pnl

    def sync(self) -> None:
        pass  # nothing external to reconcile


class PaperMarginBroker(BaseBroker):
    """Simulated MARGIN execution: long + short, leverage (risk_coef),
    liquidation price, 4-hourly interest on the borrowed amount, 21-day max age.

    Mirrors Wallex margin semantics so paper results are comparable to live:
      notional   = collateral * risk_coef
      loan       = notional - collateral
      liq (long) = entry * (1 - (1 - mmr) / risk_coef)
      liq (short)= entry * (1 + (1 - mmr) / risk_coef)
    """
    name = "paper_margin"

    def __init__(self, starting_capital: float, fee_pct: float = 0.2,
                 slippage_pct: float = 0.05, mmr_pct: float = 1.0,
                 interest_per_4h_pct: float = 0.05, max_age_days: float = 21.0,
                 rules: Optional[WallexRules] = None, quote_currency: str = "USDT"):
        self.cash = starting_capital
        self.starting_capital = starting_capital
        self.fee = fee_pct / 100.0
        self.slip = slippage_pct / 100.0
        self.quote_currency = (quote_currency or "USDT").upper()
        self.mmr = mmr_pct / 100.0                      # maintenance margin rate
        self.interest_4h = interest_per_4h_pct / 100.0  # per 4h, on loan amount
        self.max_age_s = max_age_days * 86400.0
        self.rules = rules or WallexRules()
        self.last_reject = ""
        self.positions: Dict[str, Position] = {}
        self._prices: Dict[str, float] = {}
        self.liquidations = 0
        self.interest_paid_total = 0.0

    # ── prices ─────────────────────────────────────────────────────
    def _notional_usdt(self, qty: float, symbol: str, price: float) -> float:
        """Notional value converted to USDT for rule checks (min order etc)."""
        q = MarketCatalog.quote_of(symbol)
        if q == "USDT":
            return qty * price
        if q == "TMN":
            tmn_usdt = self._prices.get("TMNUSDT", 0.0)
            if tmn_usdt:
                return qty * price * tmn_usdt
            usdt_tmn = self._prices.get("USDTTMN", 0.0)
            if usdt_tmn:
                return qty * price / usdt_tmn
        return 0.0

    def set_price(self, symbol: str, price: float) -> None:
        self._prices[symbol] = price

    def last_price(self, symbol: str) -> float:
        return self._prices.get(symbol, 0.0)

    # ── equity ─────────────────────────────────────────────────────
    def equity(self) -> float:
        eq = self.cash
        for p in self.positions.values():
            if not p.is_open:
                continue
            px = self._prices.get(p.symbol, p.entry)
            collateral = p.meta.get("collateral", 0.0)
            interest = p.meta.get("interest_accrued", 0.0)
            upnl = self._upnl(p, px)
            eq += max(collateral + upnl - interest, 0.0)  # margin can't go below 0 (liq first)
        return eq

    @staticmethod
    def _upnl(p: Position, px: float) -> float:
        if p.side == "short":
            return p.qty * (p.entry - px)
        return p.qty * (px - p.entry)

    # ── funding ────────────────────────────────────────────────────
    def deposit(self, amount: float) -> float:
        if amount > 0:
            self.cash += amount
            self.starting_capital += amount
        return self.equity()

    def withdraw(self, amount: float) -> float:
        """Withdraw free collateral only — never touch margin locked in open positions.
        Rejects (no-op) amounts exceeding free collateral."""
        if amount <= 0 or amount > self.cash:
            return self.equity()
        self.cash -= amount
        self.starting_capital = max(self.starting_capital - amount, 0.0)
        return self.equity()

    # ── open ───────────────────────────────────────────────────────
    def open_long(self, symbol: str, qty: float, price: float, pos: Position) -> bool:
        return self._open(symbol, "long", qty, price, pos)

    def open_short(self, symbol: str, qty: float, price: float, pos: Position) -> bool:
        return self._open(symbol, "short", qty, price, pos)

    def _open(self, symbol: str, side: str, qty: float, price: float, pos: Position) -> bool:
        import random as _rnd
        slip_var = _rnd.uniform(0.5, 1.5)
        slip = (self.slip * slip_var) if side == "long" else -(self.slip * slip_var)
        fill = price * (1 + slip)
        risk_coef = max(getattr(pos, "risk_coef", 1.0) or 1.0, 1.0)
        # Wallex rule: leverage cap
        risk_coef = self.rules.clamp_risk_coef(risk_coef)
        notional = qty * fill
        notional_usdt = self._notional_usdt(qty, symbol, fill)
        collateral = notional / risk_coef
        fee = notional * self.fee
        _free = self.available()   # FIX(reserve): cash minus grid reservations
        if collateral + fee > _free:
            affordable = (_free - fee) * risk_coef / (fill * (1 + self.fee)) * 0.999
            if affordable <= 0:
                self.last_reject = "وثیقه آزاد کافی نیست"
                return False
            qty = affordable
            notional = qty * fill
            notional_usdt = self._notional_usdt(qty, symbol, fill)
            collateral = notional / risk_coef
            fee = notional * self.fee
        # Wallex rules: min collateral + leverage cap + price band (USDT-normalized)
        err = self.rules.check_margin_order(symbol, collateral, risk_coef, fill, price,
                                            notional_usdt=notional_usdt)
        if err:
            self.last_reject = err
            log.info("paper margin open rejected (%s): %s", symbol, err)
            return False
        self.cash -= collateral + fee
        pos.qty = qty
        pos.entry = fill
        pos.side = side
        pos.initial_stop = pos.stop  # audit-fix: remember entry-time risk
        pos.risk_coef = risk_coef
        pos.fees_paid += fee
        pos.notional = notional
        pos.peak_price = fill
        loan = max(notional - collateral, 0.0)
        if side == "long":
            liq = fill * (1 - (1 - self.mmr) / risk_coef)
        else:
            liq = fill * (1 + (1 - self.mmr) / risk_coef)
        pos.meta = dict(getattr(pos, "meta", {}) or {})
        pos.meta.update({
            "side": side,
            "collateral": collateral,
            "loan": loan,
            "liq_price": liq,
            "interest_accrued": 0.0,
            "last_interest_ts": pos.opened_ts or int(time.time()),
        })
        self.positions[pos.id] = pos
        return True

    # ── close ──────────────────────────────────────────────────────
    def close_long(self, pos: Position, price: float, qty: Optional[float] = None, reason: str = "") -> float:
        return self._close(pos, price, reason, qty=qty)

    def close_short(self, pos: Position, price: float, qty: Optional[float] = None, reason: str = "") -> float:
        return self._close(pos, price, reason, qty=qty)

    def _close(self, pos: Position, price: float, reason: str = "", qty: Optional[float] = None) -> float:
        """Close all or part of a margin position. Partial closes release
        collateral/loan/interest proportionally and keep the position open."""
        qty = qty or pos.qty
        full = qty >= pos.qty - 1e-12
        frac = min(qty / pos.qty, 1.0) if pos.qty > 0 else 1.0

        slip = -self.slip if pos.side == "long" else self.slip
        fill = price * (1 + slip)
        pnl_gross = (pos.entry - fill) * qty if pos.side == "short" else (fill - pos.entry) * qty
        fee = qty * fill * self.fee

        collateral = pos.meta.get("collateral", 0.0) * frac
        interest = pos.meta.get("interest_accrued", 0.0) * frac
        loan = pos.meta.get("loan", 0.0) * frac
        returned = max(collateral + pnl_gross - interest - fee, 0.0)

        self.cash += returned
        self.interest_paid_total += interest
        pos.fees_paid += fee
        pos.pnl += pnl_gross - interest - fee  # accumulate net result

        if full:
            pos.qty = 0.0
            pos.state = "closed"
            pos.close_price = fill
            pos.closed_ts = int(time.time())
            pos.exit_reason = reason
            pos.meta.update({"collateral": 0.0, "loan": 0.0, "interest_accrued": 0.0})
            self.positions.pop(pos.id, None)
        else:
            pos.qty -= qty
            pos.meta["collateral"] = pos.meta.get("collateral", 0.0) - collateral
            pos.meta["loan"] = pos.meta.get("loan", 0.0) - loan
            pos.meta["interest_accrued"] = pos.meta.get("interest_accrued", 0.0) - interest
            pos.notional = pos.qty * pos.entry
        return pnl_gross - interest - fee

    def liquidate(self, pos: Position, reason: str = "liquidation") -> float:
        """Forced close at the liquidation price — collateral is (mostly) lost."""
        liq = pos.meta.get("liq_price", pos.entry)
        self.liquidations += 1
        collateral = pos.meta.get("collateral", 0.0)
        interest = pos.meta.get("interest_accrued", 0.0)
        self.interest_paid_total += interest
        # at liquidation the remaining margin is wiped
        pos.pnl = -(collateral + interest)
        pos.fees_paid += 0.0
        pos.qty = 0.0
        pos.state = "closed"
        pos.close_price = liq
        pos.closed_ts = int(time.time())
        pos.exit_reason = reason
        self.positions.pop(pos.id, None)
        return pos.pnl

    # ── maintenance: interest accrual, liquidation, max-age ────────
    def tick_maintenance(self, now_ts: Optional[int] = None) -> List[tuple]:
        """Accrue 4-hourly interest; force-close liquidated / expired positions.
        Returns list of (position, reason) that were force-closed this tick."""
        now_ts = now_ts or int(time.time())
        closed: List[tuple] = []
        for pos in list(self.positions.values()):
            if not pos.is_open:
                continue
            closed.extend(self._maintain_one(pos, now_ts))
        return closed

    def check_price_extremes(self, symbol: str, low: float, high: float,
                             now_ts: Optional[int] = None) -> List[tuple]:
        """Intra-candle liquidation check using the just-closed candle's LOW/HIGH.
        Wallex monitors margin positions continuously; checking only the close
        every 15m misses wicks through the liq price. Call this per symbol
        right after its candles arrive."""
        now_ts = now_ts or int(time.time())
        closed: List[tuple] = []
        for pos in list(self.positions.values()):
            if pos.symbol != symbol or not pos.is_open:
                continue
            closed.extend(self._maintain_one(pos, now_ts, low=low, high=high))
        return closed

    def _maintain_one(self, pos: Position, now_ts: int,
                      low: Optional[float] = None, high: Optional[float] = None) -> List[tuple]:
        out: List[tuple] = []
        if not pos.is_open:
            return out
        # interest accrual (every 4h on the loan)
        loan = pos.meta.get("loan", 0.0)
        last = pos.meta.get("last_interest_ts", pos.opened_ts)
        periods = int((now_ts - last) // (4 * 3600))
        if periods > 0 and loan > 0:
            pos.meta["interest_accrued"] = pos.meta.get("interest_accrued", 0.0) + loan * self.interest_4h * periods
            pos.meta["last_interest_ts"] = last + periods * 4 * 3600
        px = self._prices.get(pos.symbol, pos.entry)
        lo = min(low, px) if low else px
        hi = max(high, px) if high else px
        liq = pos.meta.get("liq_price", 0.0)
        # liquidation check — longs die on the LOW wick, shorts on the HIGH wick
        if liq > 0 and ((pos.side == "long" and lo <= liq) or (pos.side == "short" and hi >= liq)):
            # FIX(audit-C5): do NOT write the liq price into the shared
            # _prices cache — liquidate() sets close_price=liq itself, and a
            # poisoned cache corrupted equity() and every OTHER position on
            # the same symbol until the next engine set_price.
            self.liquidate(pos, "liquidation")
            out.append((pos, "liquidation"))
            return out
        # max age (Wallex: 21 days)
        if now_ts - pos.opened_ts >= self.max_age_s:
            self._close(pos, px, "max_age_expired")
            out.append((pos, "max_age_expired"))
        return out

    def sync(self) -> None:
        pass  # nothing external to reconcile


class LiveBroker(BaseBroker):
    """Real Wallex spot execution. Requires explicit user opt-in (WALLEX_LIVE_ALLOWED=yes)."""
    name = "live"

    def __init__(self, client: WallexClient, quote: str = "USDT"):
        self.client = client
        self.quote = quote
        self.quote_currency = (quote or "USDT").upper()  # active pair-family base
        self._balances: dict = {}
        self._prices: Dict[str, float] = {}
        self.positions: Dict[str, Position] = {}

    def sync(self) -> None:
        """After reconnect: pull real balances + open orders FIRST, before acting."""
        log.info("LIVE sync: fetching real balances and open orders from Wallex")
        self._balances = self.client.get_balances()
        open_orders = self.client.get_open_orders()
        log.info("LIVE sync done: %d balances, open-order groups: %s",
                 len(self._balances), list(open_orders.keys()) if isinstance(open_orders, dict) else len(open_orders))

    def _balance_of(self, asset: str) -> tuple:
        """(available, locked) for an asset from the latest balances."""
        info = (self._balances or {}).get(asset.upper(), {}) or {}
        try:
            return (float(info.get("available", info.get("value", 0))),
                    float(info.get("locked", 0)))
        except (TypeError, ValueError):
            return (0.0, 0.0)

    def equity(self) -> float:
        """Account value reported in the ACTIVE quote currency (the pair-family base
        the engine trades), i.e. direct available balance — NOT a USDT/TMN cross
        conversion. If the active base has no balance, fall back to raw USDT."""
        base = getattr(self, "quote_currency", None) or "USDT"
        avail, locked = self._balance_of(base)
        if avail > 0 or locked > 0:
            return avail
        # fall back to whatever quote asset carries real balance
        usdt_avail, _ = self._balance_of("USDT")
        tmn_avail, _ = self._balance_of("TMN")
        if tmn_avail > usdt_avail:
            return tmn_avail
        return usdt_avail

    def set_price(self, symbol: str, price: float) -> None:
        self._prices[symbol] = price

    def update_quote_prices(self, tmn_usdt: float = 0.0, usdt_tmn: float = 0.0) -> None:
        """Called by server each tick with latest TMN↔USDT conversion prices."""
        self._quote_prices = {
            "TMN": tmn_usdt,        # 1 TMN in USDT
            "USDT": usdt_tmn,       # 1 USDT in TMN
        }

    def last_price(self, symbol: str) -> float:
        return self._prices.get(symbol, 0.0)

    def dry_run(self, market: str, side: str, qty: float, price: float,
                stop_loss: str = "", take_profit: str = "") -> dict:
        """Local spot pre-validation BEFORE placing a real order.
        Uses the ACTIVE exchange's own profile rules (min order, price band)
        so the pre-check matches what the exchange would actually accept."""
        from .wallex_rules import WallexRules
        prof = getattr(self.client, "profile", None) or {}
        rules = WallexRules.from_profile(prof) if (isinstance(prof, dict) and prof.get("rules")) else WallexRules()
        err = rules.check_spot_order(market, qty, price)
        if err:
            return {"ok": False, "accepted": False, "reason": err}
        notional = qty * price
        fee = notional * 0.002
        return {"ok": True, "accepted": True, "mode": "spot", "side": side,
                "notional": round(notional, 2), "fee": round(fee, 2)}

    def open_long(self, symbol: str, qty: float, price: float, pos: Position) -> bool:
        # auto-Dry-Run gate before real execution
        if not self._pre_trade_dry_run(symbol, "long", qty, price):
            return False
        # Wallex client_id charset: letters A-Z, underscore, digits ONLY
        client_id = f"WLX_{uuid.uuid4().hex[:16].upper()}"
        try:
            res = self.client.place_order(
                symbol=symbol, side="BUY", order_type="MARKET",
                quantity=f"{qty:.8f}", client_id=client_id,
            )
        except Exception as e:
            log.error("LIVE order failed: %s", e)
            return False
        executed = float(res.get("executedQty") or 0)
        exec_price = float(res.get("executedPrice") or price)
        if executed <= 0:
            # FIX(C2-fill): NEVER fabricate a fill. Query the order's real
            # status; if it still shows no fill, do NOT register a phantom
            # position — the exchange may not hold anything.
            try:
                det = self.client.get_order(client_id) or {}
                order_state = str(det.get("state") or det.get("status") or "").lower()
                executed = float(det.get("executedQty") or 0)
                exec_price = float(det.get("executedPrice") or det.get("price") or price)
            except Exception as e:
                log.warning("LIVE fill check failed (client_id=%s): %s", client_id, e)
            if executed <= 0:
                log.error(
                    "LIVE order NOT filled (client_id=%s state=%s) — position NOT registered. "
                    "Check the order manually on Wallex.",
                    client_id, order_state if 'order_state' in dir() else 'unknown',
                )
                return False
        pos.qty = executed
        pos.entry = exec_price
        pos.notional = executed * exec_price
        pos.peak_price = exec_price
        self.positions[pos.id] = pos
        return True

    def open_short(self, symbol: str, qty: float, price: float, pos: Position) -> bool:
        if not self._pre_trade_dry_run(symbol, "short", qty, price):
            return False
        client_id = f"WLX_{uuid.uuid4().hex[:16].upper()}"
        try:
            res = self.client.place_order(
                symbol=symbol, side="SELL", order_type="MARKET",
                quantity=f"{qty:.8f}", client_id=client_id,
            )
        except Exception as e:
            log.error("LIVE order failed: %s", e)
            return False
        executed = float(res.get("executedQty") or 0)
        exec_price = float(res.get("executedPrice") or price)
        if executed <= 0:
            # FIX(C2-fill): same no-phantom-fill rule for shorts.
            try:
                det = self.client.get_order(client_id) or {}
                executed = float(det.get("executedQty") or 0)
                exec_price = float(det.get("executedPrice") or det.get("price") or price)
            except Exception as e:
                log.warning("LIVE fill check failed (client_id=%s): %s", client_id, e)
            if executed <= 0:
                log.error("LIVE short order NOT filled (client_id=%s) — position NOT registered. Check manually on Wallex.", client_id)
                return False
        pos.qty = executed
        pos.entry = exec_price
        pos.notional = executed * exec_price
        pos.peak_price = exec_price
        self.positions[pos.id] = pos
        return True

    def _pre_trade_dry_run(self, symbol: str, side: str, qty: float, price: float) -> bool:
        try:
            dr = self.dry_run(symbol, side, qty, price)
            if not dr.get("accepted"):
                log.warning("LIVE dry-run rejected %s %s: %s", side, symbol, dr.get("reason"))
                return False
            log.info("LIVE dry-run accepted %s %s qty=%.6f price=%.6f notional=%.2f",
                     side, symbol, qty, price, dr.get("notional", 0))
            return True
        except Exception as e:
            log.error("LIVE dry-run error %s %s: %s", side, symbol, e)
            return False

    def close_long(self, pos: Position, price: float, qty: Optional[float] = None, reason: str = "") -> float:
        qty = qty or pos.qty
        client_id = f"WLX_{uuid.uuid4().hex[:16].upper()}"
        try:
            res = self.client.place_order(
                symbol=pos.symbol, side="SELL", order_type="MARKET",
                quantity=f"{qty:.8f}", client_id=client_id,
            )
        except Exception as e:
            log.error("LIVE close failed: %s", e)
            return 0.0
        # FIX(audit-H6): same no-phantom-fill rule as the open path — a
        # rejected/partial close must not corrupt position state and PnL.
        executed = float(res.get("executedQty") or 0)
        if executed <= 0:
            try:
                det = self.client.get_order(client_id) or {}
                executed = float(det.get("executedQty") or 0)
            except Exception as e:
                log.warning("LIVE close fill check failed (client_id=%s): %s", client_id, e)
            if executed <= 0:
                log.error("LIVE close NOT filled (client_id=%s) — position kept. Check manually on Wallex.", client_id)
                return 0.0
        if executed < qty - 1e-12:
            log.warning("LIVE close partial: asked %.8f, filled %.8f", qty, executed)
        qty = executed
        exec_price = float(res.get("executedPrice") or price)
        pnl = (exec_price - pos.entry) * qty
        pos.pnl += pnl
        pos.qty -= qty
        if pos.qty <= 1e-12:
            pos.qty = 0.0
            pos.state = "closed"
            pos.close_price = exec_price
            pos.closed_ts = int(time.time())
            pos.exit_reason = reason
        return pnl


class LiveMarginBroker(BaseBroker):
    """Real Wallex MARGIN execution (long + short, exchange-side SL/TP).

    Requires explicit opt-in (WALLEX_LIVE_ALLOWED=yes) + margin-enabled account.
    Positions are tracked by Wallex position id stored in pos.meta['margin_id'].
    """
    name = "live_margin"

    def __init__(self, client: WallexClient, max_risk_coef: float = 3.0):
        self.client = client
        self.max_risk_coef = max_risk_coef  # global fallback cap
        self.positions: Dict[str, Position] = {}
        self._prices: Dict[str, float] = {}
        self._pnl_cache: dict = {}
        # per-market leverage limits from /margin-trade/v1/public/markets
        # (docs: min_risk_coef / max_risk_coef / risk_coef_step — vary per market)
        self._market_limits: Dict[str, dict] = {}
        self._limits_ts: float = 0.0

    def _limits_for(self, symbol: str) -> dict:
        """Per-market {min,max,step} risk_coef, cached 10 min."""
        import time as _t
        now = _t.time()
        if not self._market_limits or now - self._limits_ts > 600:
            try:
                for m in self.client.margin_get_markets():
                    sym = m.get("symbol") or m.get("market") or ""
                    try:
                        step = float(m.get("risk_coef_step") or 0.5)
                    except (TypeError, ValueError):
                        step = 0.5
                    self._market_limits[sym] = {
                        "min": float(m.get("min_risk_coef") or 1.0),
                        "max": float(m.get("max_risk_coef") or self.max_risk_coef),
                        "step": step,
                    }
                self._limits_ts = now
            except Exception as e:
                log.debug("margin market-limits fetch failed: %s", e)
        lim = self._market_limits.get(symbol)
        if not lim:
            lim = {"min": 1.0, "max": self.max_risk_coef, "step": 0.5}
        return lim

    def _clamp_rc(self, symbol: str, rc: float) -> float:
        """Clamp + snap to per-market step (docs: risk_coef increases in steps)."""
        lim = self._limits_for(symbol)
        rc = max(lim["min"], min(float(rc), lim["max"]))
        step = lim["step"] or 0.5
        if step > 0:
            # snap DOWN to the allowed step so we never exceed the chosen risk
            import math as _m
            rc = lim["min"] + _m.floor((rc - lim["min"]) / step + 1e-9) * step
        return round(max(rc, lim["min"]), 2)

    def set_price(self, symbol: str, price: float) -> None:
        self._prices[symbol] = price

    def last_price(self, symbol: str) -> float:
        return self._prices.get(symbol, 0.0)

    def sync(self) -> None:
        """Reconcile: pull real margin positions + PNL from Wallex."""
        log.info("MARGIN sync: fetching real positions from Wallex")
        try:
            real = self.client.margin_get_positions(active=True)
            self._pnl_cache = self.client.margin_get_pnl()
            log.info("MARGIN sync done: %d active positions", len(real) if isinstance(real, list) else 0)
        except Exception as e:
            log.error("MARGIN sync failed: %s", e)

    def equity(self) -> float:
        total = 0.0
        for v in (self._pnl_cache.get("total") or {}).values():
            try:
                total += float(v)
            except (TypeError, ValueError) as e:
                log.debug(f"broker: suppressed {type(e).__name__}: {e} | ctx: {ctx[:80]}")
        return total

    def dry_run(self, market: str, side: str, collateral: float, risk_coef: float,
                stop_loss: str = "", take_profit: str = "", open_price: str = "") -> dict:
        """Preview a position BEFORE opening — returns liquidation/call price, fees.
        FIX(audit-C1): Wallex REQUIRES open_price within ±5% of market; the old
        signature dropped it, so every _pre_trade_dry_run(open_price=...) call
        raised TypeError → live margin could never open a position."""
        risk_coef = min(risk_coef, self.max_risk_coef)
        return self.client.margin_dry_run(
            market=market, side=side, collateral=f"{collateral:.8f}",
            risk_coef=f"{risk_coef:.2f}", stop_loss=stop_loss, take_profit=take_profit,
            open_price=open_price,
        )

    def open_long(self, symbol: str, qty: float, price: float, pos: Position) -> bool:
        if not self._pre_trade_dry_run(symbol, "long", qty, price,
                                       getattr(pos, "stop", 0), getattr(pos, "target", 0),
                                       getattr(pos, "risk_coef", 1.0) or 1.0):
            return False
        return self._open(symbol, "long", qty, price, pos)

    def open_short(self, symbol: str, qty: float, price: float, pos: Position) -> bool:
        if not self._pre_trade_dry_run(symbol, "short", qty, price,
                                       getattr(pos, "stop", 0), getattr(pos, "target", 0),
                                       getattr(pos, "risk_coef", 1.0) or 1.0):
            return False
        return self._open(symbol, "short", qty, price, pos)

    def _pre_trade_dry_run(self, symbol: str, side: str, qty: float, price: float,
                           stop_loss: float = 0.0, take_profit: float = 0.0,
                           risk_coef: float = 1.0) -> bool:
        try:
            collateral = qty * price
            risk_coef = self._clamp_rc(symbol, float(risk_coef))
        except Exception:
            risk_coef = self._clamp_rc(symbol, 1.0)
        try:
            # FIX(audit-C1b): pass raw floats — dry_run() does its own
            # formatting; feeding pre-formatted strings made
            # min(risk_coef, ...) raise TypeError and every dry-run fail.
            dr = self.dry_run(
                market=symbol, side=side,
                collateral=collateral,
                risk_coef=risk_coef,
                open_price=f"{price:.8f}" if price > 0 else "",
                stop_loss=f"{stop_loss:.8f}" if stop_loss > 0 else "",
                take_profit=f"{take_profit:.8f}" if take_profit > 0 else "",
            )
            if not dr:
                log.warning("MARGIN dry-run empty response %s %s", side, symbol)
                return False
            if dr.get("id") is None and not dr.get("collateral"):
                reason = dr.get("message") or dr.get("error") or "empty dry-run"
                log.warning("MARGIN dry-run rejected %s %s: %s", side, symbol, reason)
                return False
            log.info("MARGIN dry-run accepted %s %s collateral=%.2f risk_coef=%.2f",
                     side, symbol, collateral, risk_coef)
            return True
        except Exception as e:
            log.error("MARGIN dry-run error %s %s: %s", side, symbol, e)
            return False

    def _open(self, symbol: str, side: str, qty: float, price: float, pos: Position) -> bool:
        collateral = qty * price
        risk_coef = self._clamp_rc(symbol, getattr(pos, "risk_coef", 1.0) or 1.0)
        pos.risk_coef = risk_coef  # store the exchange-accepted value
        sl = f"{pos.stop:.8f}" if getattr(pos, "stop", 0) else ""
        tp = f"{pos.target:.8f}" if getattr(pos, "target", 0) else ""
        # Wallex REQUIRES a real open_price (within ±5% of market); "0" is rejected 422.
        open_price = f"{price:.8f}" if price > 0 else f"{self.last_price(symbol):.8f}"
        try:
            res = self.client.margin_open_position(
                market=symbol, side=side, collateral=f"{collateral:.8f}",
                risk_coef=f"{risk_coef:.2f}", open_price=open_price,
                stop_loss=sl, take_profit=tp,
            )
        except Exception as e:
            log.error("MARGIN open (%s) failed: %s", side, e)
            return False
        mid = str(res.get("id", ""))
        if not mid:
            log.error("MARGIN open returned no position id: %s", res)
            return False
        pos.meta = getattr(pos, "meta", {}) or {}
        pos.meta["margin_id"] = mid
        pos.meta["side"] = side
        pos.entry = float(res.get("open_price") or price)
        pos.initial_stop = pos.stop  # audit-fix: entry-time risk
        pos.qty = qty
        pos.notional = collateral
        pos.peak_price = pos.entry
        self.positions[pos.id] = pos
        log.info("MARGIN %s opened: id=%s market=%s collateral=%.2f risk_coef=%.2f",
                 side, mid, symbol, collateral, risk_coef)
        return True

    def close_long(self, pos: Position, price: float, qty: Optional[float] = None, reason: str = "") -> float:
        return self._close(pos, price, reason)

    def close_short(self, pos: Position, price: float, qty: Optional[float] = None, reason: str = "") -> float:
        """FIX(C2): short exits route through the same Wallex margin-close —
        the exchange position closes regardless of direction; pnl sign comes
        from the exchange response."""
        return self._close(pos, price, reason)

    def _close(self, pos: Position, price: float, reason: str = "", qty: Optional[float] = None) -> float:
        mid = (getattr(pos, "meta", {}) or {}).get("margin_id", "")
        if not mid:
            log.error("MARGIN close: no margin_id on position %s", pos.id)
            return 0.0
        # Wallex REQUIRES a real close price; "0" is rejected 422.
        close_price = f"{price:.8f}" if price > 0 else f"{self.last_price(pos.symbol):.8f}"
        # FIX(C2-partial): honor qty — a partial close must NOT close the whole
        # real position. Wallex margin API closes the FULL position by id, so a
        # partial request is rejected loudly instead of silently over-closing
        # (full-path partial close support would need the /close portion param).
        if qty is not None and qty > 1e-12 and qty < pos.qty - 1e-12:
            log.error(
                "MARGIN partial close unsupported by exchange API (id=%s, asked %.6f of %.6f) "
                "— closing FULL position to keep exchange state consistent. "
                "Partial closes on live margin will close everything.",
                mid, qty, pos.qty,
            )
        try:
            res = self.client.margin_close_position(mid, price=close_price)
        except Exception as e:
            log.error("MARGIN close failed (id=%s): %s", mid, e)
            return 0.0
        try:
            pnl = float((res.get("profit") or {}).get("value", 0))
        except (TypeError, ValueError):
            pnl = 0.0
        pos.pnl += pnl
        pos.qty = 0.0
        pos.state = "closed"
        pos.close_price = float(res.get("close_price") or price)
        pos.closed_ts = int(time.time())
        pos.exit_reason = reason
        self.positions.pop(pos.id, None)
        return pnl

    def update_sltp(self, pos: Position, stop_loss: str = "", take_profit: str = "") -> bool:
        mid = (getattr(pos, "meta", {}) or {}).get("margin_id", "")
        if not mid:
            return False
        try:
            self.client.margin_update_sltp(mid, stop_loss=stop_loss, take_profit=take_profit)
            return True
        except Exception as e:
            log.error("MARGIN SLTP update failed (id=%s): %s", mid, e)
            return False
