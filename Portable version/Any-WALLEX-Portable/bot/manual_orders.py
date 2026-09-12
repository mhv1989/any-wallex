"""Manual order manager — user-placed orders beyond the strategy engine.

Order types:
  market buy/sell          — fills immediately at current price ± slippage
  limit  buy/sell          — pending until price touches `price`
                             (buy: candle LOW <= price, sell: HIGH >= price)
  stop   buy/sell          — trigger on breakout
                             (buy stop: HIGH >= price, sell stop: LOW <= price)

TP/SL simulation:
  Wallex SPOT supports LIMIT / MARKET / STOP_LIMIT / STOP_MARKET natively —
  live mode maps directly. Wallex MARGIN has no resting-order API, so TP/SL is
  simulated app-side (checked every tick against the closed 15m LOW/HIGH) and
  executed via margin_close_position.

Positions opened manually are tracked here (separate from engine signals),
executed through the SAME broker so paper balance/margin accounting stays true.
"""
from __future__ import annotations

import logging
import time
import uuid
from typing import Dict, List, Optional

from .models import Position

log = logging.getLogger("manual")


def _olog(payload: dict) -> None:
    """Fire-and-forget write to the active per-profile `orders` log."""
    try:
        from .file_logger import log as _fl
        _fl("orders", payload)
    except Exception:
        pass


class ManualOrderManager:
    def __init__(self, broker, storage):
        self.broker = broker
        self.storage = storage
        # symbol -> list of open manual positions (Position models)
        self.positions: Dict[str, List[Position]] = {}

    # ── validation & placement ─────────────────────────────────────
    def place(self, symbol: str, side: str, kind: str, qty: float,
              price: Optional[float] = None, tp: float = 0.0, sl: float = 0.0) -> dict:
        side = side.lower()
        kind = kind.lower()
        if side not in ("buy", "sell"):
            return {"ok": False, "error": "side باید buy یا sell باشد"}
        if kind not in ("market", "limit", "stop"):
            return {"ok": False, "error": "kind باید market، limit یا stop باشد"}
        if qty <= 0:
            return {"ok": False, "error": "حجم نامعتبر است"}
        last = self.broker.last_price(symbol)
        if kind in ("limit", "stop") and not price:
            return {"ok": False, "error": "برای سفارش limit/stop قیمت الزامی است"}
        if kind == "market" and not price and not last:
            return {"ok": False, "error": "قیمت بازار در دسترس نیست — ابتدا اسکن بزنید"}

        o = {
            "id": f"M{uuid.uuid4().hex[:10].upper()}",
            "symbol": symbol.upper(), "side": side, "kind": kind,
            "qty": qty, "price": price or 0.0, "tp": tp or 0.0, "sl": sl or 0.0,
            "status": "pending" if kind in ("limit", "stop") else "filled",
            "filled_price": None, "closed_price": None, "pnl": None,
            "created_ts": int(time.time()), "updated_ts": int(time.time()),
            "note": "",
        }
        # sanity vs current market price (reject obviously wrong-side triggers)
        ref = last or price
        if kind == "limit":
            if side == "buy" and price > ref * 1.001:
                return {"ok": False, "error": f"Buy Limit باید زیر قیمت بازار ({ref:.6g}) باشد — برای بالای بازار از Buy Stop استفاده کنید"}
            if side == "sell" and price < ref * 0.999:
                return {"ok": False, "error": f"Sell Limit باید بالای قیمت بازار ({ref:.6g}) باشد — برای پایین بازار از Sell Stop استفاده کنید"}
        if kind == "stop":
            if side == "buy" and price < ref * 0.999:
                return {"ok": False, "error": f"Buy Stop باید بالای قیمت بازار ({ref:.6g}) باشد"}
            if side == "sell" and price > ref * 1.001:
                return {"ok": False, "error": f"Sell Stop باید زیر قیمت بازار ({ref:.6g}) باشد"}
        # TP/SL direction sanity: long = SL below / TP above; short = mirrored
        ref2 = price or ref
        if tp and ((side == "buy" and tp <= ref2) or (side == "sell" and tp >= ref2)):
            return {"ok": False, "error": f"TP برای {side} باید {'بالای' if side=='buy' else 'زیر'} قیمت ورود (~{ref2:.6g}) باشد"}
        if sl and ((side == "buy" and sl >= ref2) or (side == "sell" and sl <= ref2)):
            return {"ok": False, "error": f"SL برای {side} باید {'زیر' if side=='buy' else 'بالای'} قیمت ورود (~{ref2:.6g}) باشد"}

        self.storage.save_manual_order(o)
        if kind == "market":
            self._fill(o, ref)
        log.info("manual %s %s %s qty=%s price=%s tp=%s sl=%s -> %s",
                 kind, side, symbol, qty, price, tp, sl, o["status"])
        _olog({"event": "manual_order", "id": o["id"], "symbol": o["symbol"],
               "side": side, "kind": kind, "qty": qty, "price": price,
               "tp": o["tp"], "sl": o["sl"], "status": o["status"],
               "filled_price": o.get("filled_price"),
               "note": o.get("note") or ""})
        return {"ok": True, "order": o}

    def cancel(self, oid: str) -> dict:
        for o in self.storage.manual_orders(["pending"]):
            if o["id"] == oid:
                o["status"] = "cancelled"
                o["updated_ts"] = int(time.time())
                self.storage.save_manual_order(o)
                return {"ok": True, "order": o}
        return {"ok": False, "error": "سفارش در انتظار یافت نشد"}

    # ── fill execution through the shared broker ───────────────────
    def _fill(self, o: dict, trigger_price: float) -> None:
        from .models import PosState
        sym, side, qty = o["symbol"], o["side"], o["qty"]
        pos = Position(
            id=o["id"], symbol=sym, qty=qty, entry=trigger_price,
            stop=o.get("sl") or trigger_price, opened_ts=int(time.time()),
            entry_reason=f"manual_{o['kind']}", initial_qty=qty, peak_price=trigger_price,
        )
        pos.initial_stop = pos.stop  # audit-fix: entry-time risk
        ok = False
        if hasattr(self.broker, "open_short"):
            # margin broker: sell = open short; buy = open long.
            # If an opposite manual position exists AND no TP/SL requested,
            # treat as a pure exit and reduce it. With TP/SL, open a new
            # directional position so the bracket is managed going forward.
            wants_bracket = bool(o.get("tp") or o.get("sl"))
            opposite = self._find_open(sym, "long" if side == "sell" else "short")
            if opposite and not wants_bracket and opposite.qty >= qty - 1e-12:
                close_fn = self.broker.close_long if opposite.side == "long" else self.broker.close_short
                pnl = close_fn(opposite, trigger_price, qty=qty, reason="manual_tp_sl" if o.get("tp") or o.get("sl") else "manual")
                self._reduce(opposite, qty)
                o.update({"status": "closed", "filled_price": trigger_price,
                          "closed_price": trigger_price, "pnl": round(pnl, 6),
                          "updated_ts": int(time.time())})
                self.storage.save_manual_order(o)
                return
            ok = self.broker.open_long(sym, qty, trigger_price, pos) if side == "buy" \
                else self.broker.open_short(sym, qty, trigger_price, pos)
        else:
            # spot broker: buy opens long; sell requires inventory
            if side == "sell":
                held = self._find_open(sym, "long")
                if not held or held.qty + 1e-12 < qty:
                    o["status"] = "rejected"
                    o["note"] = "فروش بدون موجودی کافی (اسپات فقط خرید دارد)"
                    o["updated_ts"] = int(time.time())
                    self.storage.save_manual_order(o)
                    return
                pnl = self.broker.close_long(held, trigger_price, qty=qty, reason="manual")
                self._reduce(held, qty)
                o.update({"status": "closed", "filled_price": trigger_price,
                          "closed_price": trigger_price, "pnl": round(pnl, 6),
                          "updated_ts": int(time.time())})
                self.storage.save_manual_order(o)
                return
            ok = self.broker.open_long(sym, qty, trigger_price, pos)
        if not ok:
            o["status"] = "rejected"
            o["note"] = getattr(self.broker, "last_reject", "") or "رد شدن سفارش"
            o["updated_ts"] = int(time.time())
            self.storage.save_manual_order(o)
            return
        pos.meta["tp"] = o.get("tp") or 0.0
        pos.meta["sl"] = o.get("sl") or 0.0
        pos.state = PosState.OPEN.value
        self.positions.setdefault(sym, []).append(pos)
        o["status"] = "position"
        o["filled_price"] = pos.entry
        o["updated_ts"] = int(time.time())
        self.storage.save_manual_order(o)

    @staticmethod
    def _reduce(pos: Position, qty: float) -> None:
        pos.qty -= qty
        if pos.qty <= 1e-12:
            pos.qty = 0.0

    def _find_open(self, symbol: str, side: str) -> Optional[Position]:
        for p in self.positions.get(symbol, []):
            if p.is_open and getattr(p, "side", "long") == side:
                return p
        return None

    # ── per-tick check: triggers + TP/SL ───────────────────────────
    def check(self, symbol: str, low: float, high: float, last: float) -> None:
        now = int(time.time())
        # 1) pending triggers (use intra-candle extremes)
        for o in self.storage.manual_orders(["pending"]):
            if o["symbol"] != symbol:
                continue
            px = o["price"]
            hit = (
                (o["side"] == "buy" and o["kind"] == "limit" and low <= px) or
                (o["side"] == "sell" and o["kind"] == "limit" and high >= px) or
                (o["side"] == "buy" and o["kind"] == "stop" and high >= px) or
                (o["side"] == "sell" and o["kind"] == "stop" and low <= px)
            )
            if hit:
                self._fill(o, px)
        # 2) TP/SL on open manual positions (direction-aware, wick-aware)
        for pos in list(self.positions.get(symbol, [])):
            if not pos.is_open:
                continue
            meta = pos.meta or {}
            tp, sl = meta.get("tp", 0.0), meta.get("sl", 0.0)
            if not tp and not sl:
                continue
            is_short = pos.side == "short"
            exit_px = None
            reason = ""
            if is_short:
                if sl and high >= sl:
                    exit_px, reason = sl, "manual_sl"
                elif tp and low <= tp:
                    exit_px, reason = tp, "manual_tp"
            else:
                if sl and low <= sl:
                    exit_px, reason = sl, "manual_sl"
                elif tp and high >= tp:
                    exit_px, reason = tp, "manual_tp"
            if exit_px is None:
                continue
            close_fn = self.broker.close_short if is_short else self.broker.close_long
            pnl = close_fn(pos, exit_px, reason=reason)
            pos.realized_rr = 0.0
            self.storage.save_trade(pos)
            self.storage.log_event(now, "manual_exit", symbol,
                                   f"{reason} pnl={pnl:.4f} qty={pos.initial_qty}")
            _olog({"event": "manual_exit", "id": pos.id, "symbol": symbol,
                   "reason": reason, "exit_price": exit_px, "pnl": round(pnl, 6),
                   "qty": pos.initial_qty, "entry": pos.entry})
            o = {"id": pos.id, "symbol": symbol, "side": "sell" if is_short else "buy",
                 "kind": "market", "qty": pos.initial_qty, "price": pos.entry,
                 "tp": tp, "sl": sl, "status": "closed",
                 "filled_price": pos.entry, "closed_price": exit_px, "pnl": round(pnl, 6),
                 "created_ts": pos.opened_ts, "updated_ts": now, "note": reason}
            self.storage.save_manual_order(o)

    def open_summary(self) -> List[dict]:
        out = []
        for sym, lst in self.positions.items():
            for p in lst:
                if not p.is_open:
                    continue
                px = self.broker.last_price(sym) or p.entry
                upnl = ((px - p.entry) if p.side == "long" else (p.entry - px)) * p.qty
                out.append({
                    "id": p.id, "symbol": sym, "side": p.side, "qty": p.qty,
                    "entry": p.entry, "current_price": px, "upnl": round(upnl, 6),
                    "tp": (p.meta or {}).get("tp"), "sl": (p.meta or {}).get("sl"),
                    "opened_ts": p.opened_ts,
                })
        return out
