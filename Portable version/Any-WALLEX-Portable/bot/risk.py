"""Risk manager — position sizing, exposure caps, drawdown rules, trailing.

PURE: all methods take explicit state and return decisions. No I/O.

Rules implemented:
  - risk per trade: max 1% of equity (configurable)
  - size = risk_amount / (entry - stop)
  - max 4 concurrent positions, max 12% of equity deployed
  - drawdown >= 8%  -> new size halved
  - drawdown >= 12% -> no new entries at all
  - at RR=1.5 -> stop moves to breakeven, trailing starts (1.2 x ATR)
  - at RR=2.5 -> close 50%, trail the rest
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional

from .models import Position


@dataclass
class SizingResult:
    allowed: bool
    qty: float = 0.0
    notional: float = 0.0
    reason: str = ""
    size_factor: float = 1.0


class RiskManager:
    def __init__(self, cfg: dict):
        r = cfg["risk"]
        self.risk_pct = float(r.get("risk_per_trade_pct", 1.0)) / 100.0
        self.max_positions = int(r.get("max_positions", 4))
        # margin uses max_exposure_pct (collateral), spot uses max_exposure_spot_pct if present
        self.max_exposure_pct = float(r.get("max_exposure_pct", 12.0)) / 100.0
        self.max_exposure_spot_pct = float(r.get("max_exposure_spot_pct", self.max_exposure_pct * 100)) / 100.0
        self.be_rr = float(r.get("breakeven_rr", 1.5))
        self.trail_atr = float(r.get("trailing_atr_mult", 1.2))
        self.partial_rr = float(r.get("partial_rr", 2.5))
        self.partial_pct = float(r.get("partial_close_pct", 50.0)) / 100.0
        self.dd_half = float(r.get("drawdown_half_pct", 8.0))
        self.dd_stop = float(r.get("drawdown_stop_pct", 12.0))

    # ── entry sizing ───────────────────────────────────────────────
    def size_position(
        self,
        equity: float,
        entry: float,
        stop: float,
        open_positions: List[Position],
        grid_factor: float = 1.0,
        drawdown_pct: float = 0.0,
        min_qty: float = 0.0,
        risk_coef: float = 1.0,
    ) -> SizingResult:
        if drawdown_pct >= self.dd_stop:
            return SizingResult(False, reason=f"drawdown {drawdown_pct:.1f}% ≥ {self.dd_stop}% — ورود جدید متوقف")
        if len(open_positions) >= self.max_positions:
            return SizingResult(False, reason=f"حداکثر {self.max_positions} پوزیشن همزمان")

        # Margin-aware exposure: deployed collateral (not notional) is what locks free margin.
        rc = max(float(risk_coef or 1.0), 1.0)
        is_margin = rc > 1.01
        deployed = sum((p.notional or p.qty * entry) / max(float(getattr(p, "risk_coef", 1.0) or 1.0), 1.0) if getattr(p, "risk_coef", 1.0) > 1.0 else p.qty * entry for p in open_positions)
        max_deployed = equity * (self.max_exposure_pct if is_margin else self.max_exposure_spot_pct)
        if deployed >= max_deployed:
            return SizingResult(False, reason=f"سرمایه درگیر ≥ {self.max_exposure_pct*100:.0f}%")

        risk_amount = equity * self.risk_pct
        if drawdown_pct >= self.dd_half:
            risk_amount *= 0.5  # half size in drawdown

        risk_dist = abs(entry - stop)  # works for long (stop<entry) and short (stop>entry)
        if risk_dist <= 0:
            return SizingResult(False, reason="فاصله ورود تا استاپ نامعتبر")

        qty = risk_amount / risk_dist
        notional = qty * entry

        # exposure cap (collateral-aware)
        room = max_deployed - deployed
        collateral_needed = notional / rc
        if collateral_needed > room:
            # cap by collateral room, convert back to qty
            qty = room * rc / entry
            notional = qty * entry
            collateral_needed = notional / rc

        # grid capital-split cap
        grid_cap = equity * grid_factor
        if notional > grid_cap:
            qty = grid_cap / entry
            notional = qty * entry

        if qty <= 0 or notional <= 0:
            return SizingResult(False, reason="سایز محاسبه‌شده صفر")
        if min_qty and qty < min_qty:
            return SizingResult(False, reason=f"حجم کمتر از حداقل صرافی ({min_qty})")

        factor = 0.5 if drawdown_pct >= self.dd_half else 1.0
        return SizingResult(True, qty=qty, notional=notional, size_factor=factor)

    # ── open-position management ───────────────────────────────────
    def manage(self, pos: Position, price: float, atr_now: float) -> dict:
        """Returns actions: {breakeven: bool, trail_start: bool, trail_stop: float|None,
        partial: bool, partial_qty: float}. Direction-aware (long & short)."""
        out = {"breakeven": False, "trail_start": False, "trail_stop": None,
               "partial": False, "partial_qty": 0.0}
        if not pos.is_open:
            return out

        is_short = getattr(pos, "side", "long") == "short"

        # peak_price tracks the BEST price since entry (high for long, low for short)
        if is_short:
            pos.peak_price = min(pos.peak_price, price) if pos.peak_price > 0 else price
        else:
            pos.peak_price = max(pos.peak_price, price)

        risk0 = abs(pos.entry - pos.stop) if pos.stop != pos.entry else max(pos.atr_at_entry, 1e-12)
        if risk0 <= 0:
            risk0 = max(pos.atr_at_entry, 1e-12)
        if is_short:
            rr_now = (pos.entry - price) / risk0
        else:
            rr_now = (price - pos.entry) / risk0

        # 1) breakeven at RR=1.5
        if not pos.breakeven_done and rr_now >= self.be_rr:
            pos.breakeven_done = True
            if is_short:
                pos.stop = min(pos.stop, pos.entry)
            else:
                pos.stop = max(pos.stop, pos.entry)
            out["breakeven"] = True

        # 2) trailing stop (1.2 x ATR) once breakeven reached
        if pos.breakeven_done and atr_now > 0:
            pos.trailing_on = True
            if is_short:
                trail = pos.peak_price + self.trail_atr * atr_now
                out["trail_start"] = True
                out["trail_stop"] = trail
                if trail < pos.stop:
                    pos.stop = trail
            else:
                trail = pos.peak_price - self.trail_atr * atr_now
                out["trail_start"] = True
                out["trail_stop"] = trail
                if trail > pos.stop:
                    pos.stop = trail

        # 3) partial close 50% at RR=2.5
        if not pos.partial_taken and rr_now >= self.partial_rr:
            pos.partial_taken = True
            out["partial"] = True
            out["partial_qty"] = pos.qty * self.partial_pct

        return out

    def check_stop(self, pos: Position, low_price: float, close_price: float,
                   high_price: Optional[float] = None) -> Optional[str]:
        """Stop hit? Long: candle LOW <= stop. Short: candle HIGH >= stop.
        Returns exit reason or None."""
        if not pos.is_open:
            return None
        is_short = getattr(pos, "side", "long") == "short"
        if is_short:
            hi = high_price if high_price is not None else close_price
            if hi >= pos.stop:
                return "short_stop" if not pos.trailing_on else "short_trailing_stop"
            return None
        if low_price <= pos.stop:
            return "stop" if not pos.trailing_on else "trailing_stop"
        return None
