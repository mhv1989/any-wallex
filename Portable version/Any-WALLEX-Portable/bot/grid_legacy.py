"""Legacy Grid strategy — per-symbol grid trading for spot AND margin.

A "Legacy" strategy in the app's TAB استراتژی‌های مختلف: it cannot be deleted,
its parameters are editable, and it participates in AI optimization as a
base (its artifact stays locked like signal.py's 8-criteria logic).

Design (mirrors MEXC / BingX grid bots):
  * Arithmetic (equal price gaps) or Geometric (percentage gaps) spacing.
  * Long / Short / Neutral direction (short + neutral need a margin broker).
  * Resting-order semantics: every idle buy level below the market price is
    an implicit resting order; when price touches it, it fills (at the
    FAVORABLE side: pay less than the grid price on buys, sell higher than
    the target on sells).
  * Activation gate: start "now" at market price, or wait until the market
    reaches a user activation price (long: price falls to it, short: rises).
  * Range gate: min/max price — levels only trade inside the appointed range.
  * Spillover handling ("dry-on"): when price gaps across several levels in
    one tick, every crossed level is evaluated innermost-first; each fill
    uses the favorable side, and levels that cannot be funded (or whose
    achievable fill would be WORSE than the appointed grid price beyond the
    spillover tolerance) are queued as `missed` — surfaced to the UI and to
    the AI spillover advisor (/api/grids/ai-advise) which recommends
    catch-up / wait / skip. Auto-fix mode applies catch-up fills when cash
    frees up.
  * Cumulative profit reinvest: realized pair profit can grow the next order
    size of the SAME grid (`per_grid`) or of the whole buy side (`all_grids`),
    or stay fixed (`none`). Capped by `max_order_quote`.
  * Multi-symbol: one GridProfile per (symbol, mode) run concurrently; the
    manager ticks them all from ONE markets request per cycle.

Lightweight by construction: one `GET /markets` per tick cycle prices ALL
grid symbols (no per-symbol candle fetches); fills happen only on level
crosses; state is a small JSON file under data/grids/.
"""
from __future__ import annotations

import json
import logging
import math
import threading
import time
import uuid
from pathlib import Path
from typing import Callable, Dict, List, Optional

from .models import Position

log = logging.getLogger("grid_legacy")

MIN_LEVELS = 2
MAX_LEVELS = 500
# A fill whose achievable price is worse than the appointed grid price by
# more than this percent is NOT taken (re-armed instead) — the "dry-on"
# guard: never chase a grid price that has run away.
DEFAULT_SPILLOVER_TOL_PCT = 0.5


# ── helpers ────────────────────────────────────────────────────────────
def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def _round_sig(v: float, sig: int = 10) -> float:
    if v <= 0 or not math.isfinite(v):
        return 0.0
    return round(v, sig - 1 - int(math.floor(math.log10(abs(v)))))


def build_levels(pmin: float, pmax: float, count: int, spacing: str) -> List[float]:
    """Grid price levels from pmin to pmax inclusive."""
    pmin, pmax = float(pmin), float(pmax)
    count = int(_clamp(int(count), MIN_LEVELS, MAX_LEVELS))
    if pmin <= 0 or pmax <= pmin or count < MIN_LEVELS:
        return []
    if spacing == "geometric":
        step = (pmax / pmin) ** (1.0 / (count - 1))
        levels = [pmin * (step ** i) for i in range(count)]
        levels[-1] = pmax
    else:  # arithmetic
        step = (pmax - pmin) / (count - 1)
        levels = [pmin + i * step for i in range(count)]
    return [_round_sig(v) for v in levels]


def level_weights(count: int, allocation: str) -> List[float]:
    """Capital weights per level (index 0 = lowest price).
    even: equal; pyramid: more capital on LOWER levels (lowers avg entry)."""
    n = int(count)
    if allocation == "pyramid" and n > 1:
        w = [float(n - i) for i in range(n)]
    else:
        w = [1.0] * n
    s = sum(w) or 1.0
    return [x / s for x in w]


def gap_pct(levels: List[float], idx: int) -> float:
    """Percentage gap from level idx to its pair level (idx+1 for long)."""
    if idx < 0 or idx + 1 >= len(levels) or levels[idx] <= 0:
        return 0.0
    return (levels[idx + 1] - levels[idx]) / levels[idx] * 100.0


# ── profile validation ─────────────────────────────────────────────────
def validate_profile(p: dict) -> tuple:
    """Returns (normalized_profile, error). error='' when valid."""
    out = dict(p or {})
    out["symbol"] = str(out.get("symbol", "")).strip().upper()
    if not out["symbol"]:
        return out, "symbol الزامی است"
    mode = str(out.get("mode", "spot")).lower()
    if mode not in ("spot", "margin"):
        return out, "mode باید spot یا margin باشد"
    out["mode"] = mode
    direction = str(out.get("direction", "long")).lower()
    if direction not in ("long", "short", "neutral"):
        return out, "direction باید long/short/neutral باشد"
    if mode == "spot" and direction != "long":
        return out, "در اسپات فقط گرید long ممکن است (short/neutral نیاز به مارجین دارد)"
    out["direction"] = direction
    try:
        pmin = float(out.get("range_min", 0))
        pmax = float(out.get("range_max", 0))
    except (TypeError, ValueError):
        return out, "بازه قیمت نامعتبر است"
    if pmin <= 0 or pmax <= pmin:
        return out, "بازه قیمت نامعتبر است (min < max)"
    out["range_min"], out["range_max"] = pmin, pmax
    count = int(out.get("grid_count", 20))
    if count < MIN_LEVELS or count > MAX_LEVELS:
        return out, f"تعداد گرید باید بین {MIN_LEVELS} تا {MAX_LEVELS} باشد"
    out["grid_count"] = count
    out["spacing"] = "geometric" if str(out.get("spacing", "arithmetic")).lower().startswith("geo") else "arithmetic"
    try:
        total = float(out.get("total_quote", 0))
    except (TypeError, ValueError):
        return out, "سرمایه گرید نامعتبر است"
    if total <= 0:
        return out, "سرمایه گرید باید بزرگتر از صفر باشد"
    out["total_quote"] = total
    out["allocation"] = "pyramid" if str(out.get("allocation", "even")).lower().startswith("pyr") else "even"
    reinvest = str(out.get("profit_reinvest", "none")).lower()
    out["profit_reinvest"] = reinvest if reinvest in ("none", "per_grid", "all_grids") else "none"
    try:
        out["max_order_quote"] = max(float(out.get("max_order_quote", 0) or 0), 0.0)
    except (TypeError, ValueError):
        out["max_order_quote"] = 0.0
    act_mode = str(out.get("activation_mode", "now")).lower()
    out["activation_mode"] = "price" if act_mode == "price" else "now"
    try:
        out["activation_price"] = float(out.get("activation_price", 0) or 0)
    except (TypeError, ValueError):
        out["activation_price"] = 0.0
    if out["activation_mode"] == "price" and out["activation_price"] <= 0:
        return out, "قیمت فعال‌سازی الزامی است (activation_mode=price)"
    try:
        lev = float(out.get("leverage", 1) or 1)
    except (TypeError, ValueError):
        lev = 1.0
    out["leverage"] = _clamp(lev, 1.0, 10.0) if mode == "margin" else 1.0
    out["trailing_up"] = bool(out.get("trailing_up", False))
    out["ai_autofix"] = bool(out.get("ai_autofix", False))
    try:
        out["spillover_tol_pct"] = _clamp(float(out.get("spillover_tol_pct", DEFAULT_SPILLOVER_TOL_PCT)), 0.05, 10.0)
    except (TypeError, ValueError):
        out["spillover_tol_pct"] = DEFAULT_SPILLOVER_TOL_PCT
    out["name"] = str(out.get("name", "")).strip() or f"Grid {out['symbol']}"
    return out, ""


def legs_of(direction: str) -> List[str]:
    return ["long", "short"] if direction == "neutral" else [direction]


# ── runner (pure logic + broker interface) ─────────────────────────────
class GridRunner:
    """One running grid profile. Talks to a broker through the standard
    open_long/open_short/close_long/close_short interface (paper or live)."""

    def __init__(self, profile: dict, state: Optional[dict] = None):
        self.p, err = validate_profile(profile)
        if err:
            raise ValueError(err)
        self.id: str = self.p.get("id") or ("G" + uuid.uuid4().hex[:10].upper())
        self.p["id"] = self.id
        self.running = False
        self.activated = self.p["activation_mode"] == "now"
        self.cum_profit = 0.0          # realized net profit, quote currency
        self.pair_profits: Dict[str, float] = {}   # f"{leg}:{level}" -> accumulated pair profit
        self.fills = 0                 # completed pairs
        # FIX(user req): execution stats for the Grids tab
        self.stat_buys = 0
        self.stat_sells = 0
        self.stat_buy_quote = 0.0
        self.stat_sell_quote = 0.0
        self.accumulated_seconds = 0   # active runtime before this session
        self.level_exec: dict = {}     # per-level execution counters
        self.missed: List[dict] = []   # spillover queue for the AI advisor
        self.events: List[dict] = []   # last N human-readable events
        self.last_price = 0.0
        self.started_ts = 0
        # leg -> per-level state list
        self.levels_state: Dict[str, List[dict]] = {}
        st = state or {}
        self._load_state(st)
        self._ensure_leg_states()

    # ── state (de)serialization ────────────────────────────────────
    def _ensure_leg_states(self) -> None:
        n = self.p["grid_count"]
        for leg in legs_of(self.p["direction"]):
            cur = self.levels_state.get(leg)
            if not cur or len(cur) != n:
                self.levels_state[leg] = [
                    {"state": "idle", "pos_id": "", "entry": 0.0, "qty": 0.0, "target": 0.0}
                    for _ in range(n)
                ]

    def _load_state(self, st: dict) -> None:
        if not st:
            return
        self.running = bool(st.get("running", False))
        self.activated = bool(st.get("activated", self.activated))
        self.cum_profit = float(st.get("cum_profit", 0.0) or 0.0)
        self.pair_profits = {k: float(v) for k, v in (st.get("pair_profits") or {}).items()}
        self.fills = int(st.get("fills", 0) or 0)
        self.missed = list(st.get("missed") or [])
        self.started_ts = int(st.get("started_ts", 0) or 0)
        self.stat_buys = int(st.get("stat_buys", 0) or 0)
        self.stat_sells = int(st.get("stat_sells", 0) or 0)
        self.stat_buy_quote = float(st.get("stat_buy_quote", 0.0) or 0.0)
        self.stat_sell_quote = float(st.get("stat_sell_quote", 0.0) or 0.0)
        self.accumulated_seconds = int(st.get("accumulated_seconds", 0) or 0)
        _le = st.get("level_exec") or {}
        self.level_exec = {k: int(v) for k, v in _le.items()} if isinstance(_le, dict) else {}
        ls = st.get("levels_state") or {}
        if isinstance(ls, dict):
            self.levels_state = ls

    def state(self) -> dict:
        return {
            "running": self.running,
            "stat_buys": self.stat_buys,
            "stat_sells": self.stat_sells,
            "stat_buy_quote": self.stat_buy_quote,
            "stat_sell_quote": self.stat_sell_quote,
            "accumulated_seconds": self.accumulated_seconds,
            "level_exec": self.level_exec,
            "activated": self.activated,
            "cum_profit": self.cum_profit,
            "pair_profits": self.pair_profits,
            "fills": self.fills,
            "missed": self.missed[-50:],
            "started_ts": self.started_ts,
            "levels_state": self.levels_state,
        }

    def snapshot(self) -> dict:
        """UI-facing summary."""
        levels = self.compute_levels()
        held = sum(
            1 for leg in self.levels_state.values() for lv in leg if lv.get("state") == "held"
        )
        return {
            "id": self.id,
            "name": self.p.get("name", ""),
            "symbol": self.p["symbol"],
            "mode": self.p["mode"],
            "direction": self.p["direction"],
            "running": self.running,
            "activated": self.activated,
            "range_min": self.p["range_min"],
            "range_max": self.p["range_max"],
            "grid_count": self.p["grid_count"],
            "spacing": self.p["spacing"],
            "total_quote": self.p["total_quote"],
            "allocation": self.p["allocation"],
            "profit_reinvest": self.p["profit_reinvest"],
            "leverage": self.p.get("leverage", 1.0),
            "trailing_up": self.p.get("trailing_up", False),
            "ai_autofix": self.p.get("ai_autofix", False),
            "activation_mode": self.p["activation_mode"],
            "activation_price": self.p.get("activation_price", 0.0),
            "last_price": self.last_price,
            "cum_profit": round(self.cum_profit, 8),
            "fills": self.fills,
            "held_levels": held,
            "missed_count": len(self.missed),
            "level_exec": self.level_exec,
            # FIX(user req): execution stats for the Grids tab
            "stat_buys": self.stat_buys,
            "stat_sells": self.stat_sells,
            "stat_buy_quote": round(self.stat_buy_quote, 4),
            "stat_sell_quote": round(self.stat_sell_quote, 4),
            "runtime_sec": self.accumulated_seconds + (
                (int(time.time()) - self.started_ts) if self.running and self.started_ts else 0),
            "levels": [round(v, 10) for v in levels],
            "levels_exec_buy": [self.level_exec.get(f"buy:long:{i}", 0) + self.level_exec.get(f"buy:short:{i}", 0)
                                 for i in range(len(levels))],
            "levels_exec_sell": [self.level_exec.get(f"sell:long:{i}", 0) + self.level_exec.get(f"sell:short:{i}", 0)
                                  for i in range(len(levels))],
            "events": self.events[-20:],
            "in_range": bool(levels and self.p["range_min"] <= self.last_price <= self.p["range_max"]),
        }

    # ── sizing ─────────────────────────────────────────────────────
    def compute_levels(self) -> List[float]:
        return build_levels(self.p["range_min"], self.p["range_max"],
                            self.p["grid_count"], self.p["spacing"])

    def _leg_capital(self, leg: str) -> float:
        n_legs = len(legs_of(self.p["direction"]))
        share = 1.0 / n_legs
        return self.p["total_quote"] * share

    def _base_value(self, leg: str, idx: int) -> float:
        weights = level_weights(self.p["grid_count"], self.p["allocation"])
        return self._leg_capital(leg) * weights[idx]

    def _order_value(self, leg: str, idx: int) -> float:
        """Order value with cumulative-profit reinvest applied."""
        base = self._base_value(leg, idx)
        mode = self.p["profit_reinvest"]
        cap = self.p.get("max_order_quote") or (base * 4.0)
        if mode == "per_grid":
            prof = self.pair_profits.get(f"{leg}:{idx}", 0.0)
            return _clamp(base + max(prof, 0.0), base * 0.5, cap)
        if mode == "all_grids":
            boost = 1.0 + max(self.cum_profit, 0.0) / max(self._leg_capital(leg), 1e-12)
            return _clamp(base * boost, base * 0.5, cap)
        return base

    # ── events / logging ───────────────────────────────────────────
    def _event(self, kind: str, detail: str) -> None:
        self.events.append({"ts": int(time.time()), "kind": kind, "detail": detail[:300]})
        self.events = self.events[-50:]
        try:
            from .file_logger import log as _fl
            _fl("orders", {"event": f"grid_{kind}", "grid_id": self.id,
                           "symbol": self.p["symbol"], "detail": detail[:300]})
        except Exception as e:
            log.debug(f"grid_legacy: suppressed {type(e).__name__}: {e} | ctx: {ctx[:80]}")

    # ── main tick ──────────────────────────────────────────────────
    def tick(self, price: float, broker, ts: Optional[int] = None,
             on_change: Optional[Callable[[], None]] = None) -> dict:
        """Process one price update. Returns {"changed": bool, "fills": [...]}."""
        if price <= 0:
            return {"changed": False, "fills": []}
        self.last_price = price
        if not self.running:
            return {"changed": False, "fills": []}
        # activation gate
        if not self.activated:
            ap = self.p["activation_price"]
            if self.p["direction"] == "short":
                self.activated = price >= ap
            else:  # long/neutral wait for a dip to the activation price
                self.activated = price <= ap
            if self.activated:
                self._event("activated", f"grid activated at {price:.10g} (activation={ap:.10g})")
            else:
                return {"changed": False, "fills": []}
        changed = False
        all_fills: List[dict] = []
        for leg in legs_of(self.p["direction"]):
            f = self._tick_leg(leg, price, broker, ts)
            if f["changed"]:
                changed = True
            all_fills.extend(f["fills"])
        # trailing: shift the whole grid up when price breaks out above range
        if changed or self.p.get("trailing_up"):
            if self._maybe_trail(price):
                changed = True
        # auto spillover fix: retry missed fills when cash freed up
        if self.missed and self.p.get("ai_autofix"):
            if self._retry_missed(price, broker):
                changed = True
        if changed and on_change:
            on_change()
        return {"changed": changed, "fills": all_fills}

    def _maybe_trail(self, price: float) -> bool:
        if not self.p.get("trailing_up") or price <= self.p["range_max"]:
            return False
        span = self.p["range_max"] - self.p["range_min"]
        self.p["range_min"] = _round_sig(self.p["range_min"] + span)
        self.p["range_max"] = _round_sig(self.p["range_max"] + span)
        # held positions keep their positions; targets shift with the grid
        for leg, arr in self.levels_state.items():
            levels = self.compute_levels()
            for i, lv in enumerate(arr):
                if lv.get("state") == "held":
                    if leg == "long":
                        lv["target"] = levels[min(i + 1, len(levels) - 1)]
                    else:
                        lv["target"] = levels[max(i - 1, 0)]
        self._event("trail", f"range rolled up to [{self.p['range_min']:.10g}, {self.p['range_max']:.10g}]")
        return True

    # dry-on guard: is the achievable fill acceptable vs the appointed grid price?
    def _fill_ok(self, side: str, grid_price: float, market: float) -> bool:
        if market <= 0 or grid_price <= 0:
            return False
        if side == "buy":
            # paying MORE than the grid price is the bad case
            return market <= grid_price * (1 + self.p["spillover_tol_pct"] / 100.0)
        # sell/cover: receiving LESS than the target is the bad case
        return market >= grid_price * (1 - self.p["spillover_tol_pct"] / 100.0)

    def _mark_missed(self, leg: str, idx: int, action: str, grid_price: float,
                     market: float, reason: str) -> None:
        self.missed.append({
            "ts": int(time.time()), "leg": leg, "level": idx,
            "action": action, "grid_price": grid_price,
            "market": market, "reason": reason[:200],
        })
        self.missed = self.missed[-100:]
        self._event("missed", f"{leg} L{idx} {action} @ {grid_price:.10g} skipped: {reason}")

    def _tick_leg(self, leg: str, price: float, broker, ts: Optional[int]) -> dict:
        levels = self.compute_levels()
        arr = self.levels_state.get(leg) or []
        changed = False
        fills: List[dict] = []
        ts = ts or int(time.time())
        # innermost-first ordering: nearest-to-price level first so limited
        # capital funds the most relevant fills before spillover extras.
        if leg == "long":
            order = sorted(range(len(levels)), key=lambda i: -levels[i])  # high→low
        else:
            order = sorted(range(len(levels)), key=lambda i: levels[i])   # low→high

        for idx in order:
            lv = arr[idx]
            P = levels[idx]
            if leg == "long":
                if lv["state"] == "idle" and price <= P:
                    if not self._fill_ok("buy", P, price):
                        # market gapped far BELOW the level: buying now is
                        # actually cheaper — always acceptable. Only a market
                        # ABOVE grid price + tol is bad, impossible here.
                        self._mark_missed(leg, idx, "buy", P, price, "price below grid beyond tolerance")
                        continue
                    ok = self._do_buy(leg, idx, P, price, broker, ts)
                    changed = changed or ok
                elif lv["state"] == "held" and price >= lv["target"]:
                    if not self._fill_ok("sell", lv["target"], price):
                        # price fell back below the sell target — re-arm, wait
                        continue
                    ok = self._do_sell(leg, idx, lv["target"], price, broker, ts)
                    changed = changed or ok
            else:  # short leg (margin only)
                if lv["state"] == "idle" and price >= P:
                    if not self._fill_ok("sell", P, price):
                        self._mark_missed(leg, idx, "sell", P, price, "price above grid beyond tolerance")
                        continue
                    ok = self._do_short_open(leg, idx, P, price, broker, ts)
                    changed = changed or ok
                elif lv["state"] == "held" and price <= lv["target"]:
                    if not self._fill_ok("buy", lv["target"], price):
                        continue
                    ok = self._do_short_cover(leg, idx, lv["target"], price, broker, ts)
                    changed = changed or ok
        return {"changed": changed, "fills": fills}

    # ── executions (long leg) ──────────────────────────────────────
    def _qty_for(self, leg: str, idx: int, price: float) -> float:
        value = self._order_value(leg, idx)
        lev = self.p.get("leverage", 1.0) if self.p["mode"] == "margin" else 1.0
        return _round_sig(value * lev / max(price, 1e-12), 8)

    def _do_buy(self, leg: str, idx: int, grid_price: float, market: float,
                broker, ts: int) -> bool:
        arr = self.levels_state[leg]
        qty = self._qty_for(leg, idx, market)
        if qty <= 0:
            return False
        pos = Position(
            id=f"{self.id}-{leg[0].upper()}{idx}-{uuid.uuid4().hex[:6]}",
            symbol=self.p["symbol"], qty=qty, entry=market, stop=grid_price * 0.5,
            opened_ts=ts, entry_reason="grid_legacy", initial_qty=qty, peak_price=market,
        )
        pos.initial_stop = pos.stop  # audit-fix: entry-time risk
        pos.meta = {"grid_id": self.id, "grid_leg": leg, "grid_level": idx}
        pos.risk_coef = self.p.get("leverage", 1.0) if self.p["mode"] == "margin" else 1.0
        if not broker.open_long(self.p["symbol"], qty, market, pos):
            self._mark_missed(leg, idx, "buy", grid_price, market,
                              getattr(broker, "last_reject", "") or "broker rejected / insufficient cash")
            return False
        levels = self.compute_levels()
        arr[idx] = {
            "state": "held", "pos_id": pos.id, "entry": pos.entry,
            "qty": pos.qty, "target": levels[min(idx + 1, len(levels) - 1)],
        }
        self._event("buy", f"long L{idx} filled {pos.qty:.8g} @ {pos.entry:.10g} target {arr[idx]['target']:.10g}")
        self.stat_buys += 1
        self.stat_buy_quote += float(pos.qty) * float(pos.entry)
        k = f"buy:{leg}:{idx}"
        self.level_exec[k] = self.level_exec.get(k, 0) + 1
        return True

    def _do_sell(self, leg: str, idx: int, target: float, market: float,
                 broker, ts: int) -> bool:
        arr = self.levels_state[leg]
        lv = arr[idx]
        try:
            pos = broker.positions.get(lv["pos_id"])
        except AttributeError:
            pos = None
        if pos is None or not getattr(pos, "is_open", True):
            # position vanished (liquidation / external close) — reconcile
            self._event("reconcile", f"long L{idx} position {lv['pos_id']} gone — level reset")
            arr[idx] = {"state": "idle", "pos_id": "", "entry": 0.0, "qty": 0.0, "target": 0.0}
            return True
        qty = min(pos.qty, lv.get("qty") or pos.qty)
        pnl = broker.close_long(pos, market, qty=qty, reason="grid_pair")
        arr[idx] = {"state": "idle", "pos_id": "", "entry": 0.0, "qty": 0.0, "target": 0.0}
        self._register_pair_profit(leg, idx, pnl)
        self.stat_sells += 1
        self.stat_sell_quote += float(qty) * float(market)
        k = f"sell:{leg}:{idx}"
        self.level_exec[k] = self.level_exec.get(k, 0) + 1
        self._event("sell", f"long L{idx} closed @ {market:.10g} pnl={pnl:.6f}")
        return True

    # ── executions (short leg, margin) ─────────────────────────────
    def _do_short_open(self, leg: str, idx: int, grid_price: float, market: float,
                       broker, ts: int) -> bool:
        if not hasattr(broker, "open_short"):
            self._mark_missed(leg, idx, "sell", grid_price, market,
                              "margin broker required for short grid — switch paper mode")
            return False
        arr = self.levels_state[leg]
        qty = self._qty_for(leg, idx, market)
        if qty <= 0:
            return False
        pos = Position(
            id=f"{self.id}-{leg[0].upper()}{idx}-{uuid.uuid4().hex[:6]}",
            symbol=self.p["symbol"], qty=qty, entry=market, stop=grid_price * 1.5,
            opened_ts=ts, entry_reason="grid_legacy_short", initial_qty=qty, peak_price=market,
        )
        pos.side = "short"
        pos.initial_stop = pos.stop  # audit-fix: entry-time risk
        pos.meta = {"grid_id": self.id, "grid_leg": leg, "grid_level": idx}
        pos.risk_coef = self.p.get("leverage", 1.0)
        if not broker.open_short(self.p["symbol"], qty, market, pos):
            self._mark_missed(leg, idx, "sell", grid_price, market,
                              getattr(broker, "last_reject", "") or "broker rejected / insufficient collateral")
            return False
        levels = self.compute_levels()
        arr[idx] = {
            "state": "held", "pos_id": pos.id, "entry": pos.entry,
            "qty": pos.qty, "target": levels[max(idx - 1, 0)],
        }
        self._event("short_open", f"short L{idx} opened {pos.qty:.8g} @ {pos.entry:.10g} target {arr[idx]['target']:.10g}")
        self.stat_sells += 1
        self.stat_sell_quote += float(pos.qty) * float(pos.entry)
        k = f"sell:{leg}:{idx}"
        self.level_exec[k] = self.level_exec.get(k, 0) + 1
        return True

    def _do_short_cover(self, leg: str, idx: int, target: float, market: float,
                        broker, ts: int) -> bool:
        arr = self.levels_state[leg]
        lv = arr[idx]
        try:
            pos = broker.positions.get(lv["pos_id"])
        except AttributeError:
            pos = None
        if pos is None or not getattr(pos, "is_open", True):
            self._event("reconcile", f"short L{idx} position {lv['pos_id']} gone — level reset")
            arr[idx] = {"state": "idle", "pos_id": "", "entry": 0.0, "qty": 0.0, "target": 0.0}
            return True
        qty = min(pos.qty, lv.get("qty") or pos.qty)
        pnl = broker.close_short(pos, market, qty=qty, reason="grid_pair")
        arr[idx] = {"state": "idle", "pos_id": "", "entry": 0.0, "qty": 0.0, "target": 0.0}
        self._register_pair_profit(leg, idx, pnl)
        self._event("short_cover", f"short L{idx} covered @ {market:.10g} pnl={pnl:.6f}")
        self.stat_buys += 1
        self.stat_buy_quote += float(qty) * float(market)
        k = f"buy:{leg}:{idx}"
        self.level_exec[k] = self.level_exec.get(k, 0) + 1
        return True

    def _register_pair_profit(self, leg: str, idx: int, pnl: float) -> None:
        self.cum_profit += pnl
        self.fills += 1
        key = f"{leg}:{idx}"
        self.pair_profits[key] = self.pair_profits.get(key, 0.0) + max(pnl, 0.0)

    # ── missed-fill recovery ───────────────────────────────────────
    def _retry_missed(self, price: float, broker) -> bool:
        """Auto-fix: retry pending missed fills at the CURRENT price when the
        achievable fill is now favorable (price retrace made it acceptable)."""
        changed = False
        remaining = []
        for m in self.missed:
            leg, idx, action = m["leg"], m["level"], m["action"]
            levels = self.compute_levels()
            if idx >= len(levels) or not self.running:
                continue
            P = levels[idx]
            arr = self.levels_state.get(leg) or []
            if idx >= len(arr) or arr[idx]["state"] != "idle":
                continue  # already filled or position gone
            if action == "buy" and price <= P and self._fill_ok("buy", P, price):
                if self._do_buy(leg, idx, P, price, broker, int(time.time())):
                    changed = True
                    continue
            elif action == "sell" and price >= P and self._fill_ok("sell", P, price):
                if self._do_short_open(leg, idx, P, price, broker, int(time.time())):
                    changed = True
                    continue
            remaining.append(m)
        self.missed = remaining[-100:]
        return changed

    # ── backtest (optimization) ────────────────────────────────────
    def backtest(self, candles: List, fee_pct: float = 0.2) -> dict:
        """Simulate this grid over historical candles (wick-aware).
        candles: objects/rows with (ts, o, h, l, c). Returns metrics."""
        levels = self.compute_levels()
        if not levels:
            return {"error": "no levels"}
        n = self.p["grid_count"]
        weights = level_weights(n, self.p["allocation"])
        leg_capital = self.p["total_quote"] / len(legs_of(self.p["direction"]))
        base_values = [leg_capital * w for w in weights]
        cum = 0.0
        trades = 0
        wins = 0
        gross_profit = 0.0
        gross_loss = 0.0
        fees_total = 0.0
        equity_curve: List[float] = []
        activated = self.p["activation_mode"] == "now"
        state: Dict[str, dict] = {}
        for leg in legs_of(self.p["direction"]):
            state[leg] = [{"state": "idle"} for _ in range(n)]
        fee = fee_pct / 100.0
        last_price = 0.0
        for c in candles:
            h, l, cl = float(c.h), float(c.l), float(c.c)
            px_probe = [(l, "buy"), (h, "sell"), (cl, "close")]  # pessimistic: buys first
            if not activated:
                ap = self.p["activation_price"]
                if self.p["direction"] == "short":
                    activated = h >= ap
                else:
                    activated = l <= ap
                if not activated:
                    continue
            for leg, arr in state.items():
                for idx in range(n):
                    st = arr[idx]
                    P = levels[idx]
                    if leg == "long":
                        if st["state"] == "idle" and l <= P:
                            fill = min(P, h)  # wick touched level; fill at level (conservative)
                            qty = base_values[idx] / max(P, 1e-12)
                            st.update({"state": "held", "entry": fill, "qty": qty,
                                       "target": levels[min(idx + 1, n - 1)]})
                        elif st.get("state") == "held" and h >= st["target"]:
                            sell_fill = st["target"]
                            qty = st["qty"]
                            fee_cost = (st["entry"] + sell_fill) * qty * fee
                            pnl = (sell_fill - st["entry"]) * qty - fee_cost
                            fees_total += fee_cost
                            cum += pnl
                            trades += 1
                            if pnl >= 0:
                                wins += 1
                                gross_profit += pnl
                            else:
                                gross_loss += abs(pnl)
                            arr[idx] = {"state": "idle"}
                    else:
                        if st["state"] == "idle" and h >= P:
                            fill = max(P, l)
                            qty = base_values[idx] / max(P, 1e-12)
                            st.update({"state": "held", "entry": fill, "qty": qty,
                                       "target": levels[max(idx - 1, 0)]})
                        elif st.get("state") == "held" and l <= st["target"]:
                            cover_fill = st["target"]
                            qty = st["qty"]
                            fee_cost = (st["entry"] + cover_fill) * qty * fee
                            pnl = (st["entry"] - cover_fill) * qty - fee_cost
                            fees_total += fee_cost
                            cum += pnl
                            trades += 1
                            if pnl >= 0:
                                wins += 1
                                gross_profit += pnl
                            else:
                                gross_loss += abs(pnl)
                            arr[idx] = {"state": "idle"}
            equity_curve.append(cum)
            last_price = cl
        open_held = sum(1 for arr in state.values() for s in arr if s.get("state") == "held")
        eq_min = min(equity_curve) if equity_curve else 0.0
        eq_max = max(equity_curve) if equity_curve else 0.0
        dd = (eq_max - eq_min) if eq_max > 0 else 0.0
        return {
            "trades": trades,
            "completed_fills": trades,
            "win_rate": round(wins / trades * 100, 2) if trades else 0.0,
            "profit": round(cum, 8),
            "profit_per_grid": round(cum / max(self.p["grid_count"], 1), 8),
            "return_pct": round(cum / max(self.p["total_quote"], 1e-12) * 100, 4),
            "profit_factor": round(gross_profit / gross_loss, 3) if gross_loss > 0 else (999.0 if gross_profit > 0 else 0.0),
            "fees_total": round(fees_total, 8),
            "fee_share_pct": round(fees_total / max(gross_profit + gross_loss, 1e-12) * 100, 2),
            "max_drawdown": round(dd, 8),
            "open_held": open_held,
            "bars": len(candles),
        }


# ── manager (multi-profile, engine-facing) ─────────────────────────────
class GridManager:
    """Owns all GridRunners. Ticks them from a lightweight price loop
    (ONE /markets request per cycle prices every grid symbol)."""

    def __init__(self, engine, storage, data_dir: str, tick_sec: int = 60):
        self.engine = engine
        self.storage = storage
        self.base = Path(data_dir) / "grids"
        self.base.mkdir(parents=True, exist_ok=True)
        self.tick_sec = int(tick_sec)
        self.runners: Dict[str, GridRunner] = {}
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        # FIX(audit-H11): save() runs from CRUD (holding _lock), the price
        # thread and start/stop — a dedicated lock serializes the tmp file
        # write without deadlocking the CRUD paths that already hold _lock.
        self._save_lock = threading.Lock()
        self._load()

    # ── persistence (single JSON, atomic write) ────────────────────
    def _path(self) -> Path:
        return self.base / "grids.json"

    def _load(self) -> None:
        p = self._path()
        if not p.exists():
            return
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except Exception as exc:
            # FIX(audit-H11): a corrupt file previously wiped ALL grid state
            # silently. Preserve the bad file for manual recovery instead.
            log.warning("grids load failed: %s", exc)
            try:
                bak = p.with_suffix(".json.corrupt")
                bak.write_bytes(p.read_bytes())
                log.warning("corrupt grids.json preserved as %s", bak.name)
            except Exception as e:
                log.debug(f"grid_legacy: suppressed {type(e).__name__}: {e} | ctx: {ctx[:80]}")
            return
        migrated = False
        for item in (data.get("grids") or {}).values():
            try:
                prof = item.get("profile") if isinstance(item, dict) else item
                st = item.get("state") if isinstance(item, dict) else None
                # one-time migration: grid profiles created before 2026-09-09
                # carried legacy=True (undeletable); ALL grid profiles are
                # user-deletable — only the strategies-library Legacy is not.
                if isinstance(prof, dict) and prof.get("legacy"):
                    prof["legacy"] = False
                    migrated = True
                r = GridRunner(prof, st)
                self.runners[r.id] = r
            except Exception as exc:
                log.warning("grid profile skipped: %s", exc)
        if migrated:
            self.save()

    def save(self) -> None:
        data = {"grids": {r.id: {"profile": r.p, "state": r.state()} for r in self.runners.values()}}
        with self._save_lock:
            tmp = self._path().with_suffix(".json.tmp")
            try:
                tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
                tmp.replace(self._path())
            except Exception as exc:
                log.warning("grids save failed: %s", exc)

    # ── CRUD ───────────────────────────────────────────────────────
    def create(self, profile: dict) -> tuple:
        try:
            r = GridRunner(profile)
        except ValueError as exc:
            return None, str(exc)
        r.p["legacy"] = False  # user-created grid profiles ARE deletable
        with self._lock:
            self.runners[r.id] = r
            self.save()
        return r, ""

    def get(self, gid: str) -> Optional[GridRunner]:
        return self.runners.get(gid)

    def update(self, gid: str, patch: dict) -> tuple:
        r = self.runners.get(gid)
        if r is None:
            return None, "grid not found"
        if r.running:
            return None, "گرید در حال اجراست — ابتدا توقفش کنید (stop)"
        merged = {**r.p, **(patch or {})}
        try:
            r2 = GridRunner(merged, r.state())
        except ValueError as exc:
            return None, str(exc)
        # preserve runtime flags + the deletable/legacy identity
        r2.p["legacy"] = bool(r.p.get("legacy", False))
        r2.running = False
        r2.activated = False
        with self._lock:
            self.runners[gid] = r2
            self.save()
        return r2, ""

    def delete(self, gid: str) -> tuple:
        r = self.runners.get(gid)
        if r is None:
            return False, "grid not found"
        if gid == "LEGACY" or r.p.get("legacy", False):
            # reserved protected slot — never deletable
            return False, "این گرید محافظتشده است و قابل حذف نیست"
        if r.running:
            return False, "گرید در حال اجراست — ابتدا توقفش کنید"
        self._broker_release(gid)   # release reserved capital
        with self._lock:
            self.runners.pop(gid, None)
            self.save()
        return True, ""

    def _broker_reserve(self, r: "GridRunner") -> tuple:
        """Lock the grid's allocated capital on the current broker so other
        strategies cannot spend it. Returns (ok, msg)."""
        broker = getattr(self.engine, "broker", None)
        if broker is None or not hasattr(broker, "reserve"):
            return True, ""
        amt = float(r.p.get("total_quote", 0.0) or 0.0)
        if amt <= 0:
            return True, ""
        already = getattr(broker, "_reserved", {}).get(f"grid:{r.id}", 0.0)
        if already >= amt - 1e-9:
            return True, ""
        if already > 0:
            broker.release(f"grid:{r.id}")
        if not broker.reserve(f"grid:{r.id}", amt):
            return False, getattr(broker, "last_reject", "") or "سرمایه آزاد کافی نیست"
        return True, ""

    def _broker_release(self, gid: str) -> None:
        broker = getattr(self.engine, "broker", None)
        if broker is not None and hasattr(broker, "release"):
            broker.release(f"grid:{gid}")

    def start(self, gid: str) -> tuple:
        r = self.runners.get(gid)
        if r is None:
            return False, "grid not found"
        # FIX(user req): lock the allocated capital BEFORE going live — the
        # remainder stays available for other strategies; stop releases it.
        ok, msg = self._broker_reserve(r)
        if not ok:
            return False, msg
        r.running = True
        r.started_ts = int(time.time())
        if r.p["activation_mode"] == "now":
            r.activated = True
        self._ensure_thread()
        self.save()
        return True, ""

    def resume_all(self) -> int:
        """HYBRID RESTORE (user req): after a backend restart, re-activate
        every grid that was running (state persisted in grids.json) —
        threads + capital re-reservation included. Returns count resumed."""
        n = 0
        for gid, r in list(self.runners.items()):
            if r.running:
                ok, msg = self._broker_reserve(r)
                if not ok:
                    log.warning("grid %s restore: capital lock failed (%s) — "
                                "grid stays running but may miss fills", gid, msg[:80])
                self._ensure_thread()
                n += 1
        if n:
            self.save()
            log.info("hybrid restore: %d grid(s) resumed", n)
        return n

    def stop(self, gid: str) -> tuple:
        r = self.runners.get(gid)
        if r is None:
            return False, "grid not found"
        r.running = False
        # FIX(user req): un-spent reserved capital returns to the available
        # balance immediately when the grid stops.
        self._broker_release(gid)
        self.save()
        return True, ""

    def reset_state(self, gid: str) -> tuple:
        r = self.runners.get(gid)
        if r is None:
            return False, "grid not found"
        if r.running:
            return False, "گرید در حال اجراست — ابتدا توقفش کنید"
        r.levels_state = {}
        r._ensure_leg_states()
        r.cum_profit = 0.0
        r.pair_profits = {}
        r.fills = 0
        r.missed = []
        r.activated = r.p["activation_mode"] == "now"
        self.save()
        return True, ""

    # ── price loop ─────────────────────────────────────────────────
    def running_profiles(self) -> List[GridRunner]:
        return [r for r in self.runners.values() if r.running]

    def _ensure_thread(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="grid-legacy", daemon=True)
        self._thread.start()

    def _loop(self) -> None:
        """Dedicated light loop: ONE markets request per cycle prices ALL grid
        symbols; ticks every runner. Exits when nothing is running."""
        while not self._stop.is_set():
            running = self.running_profiles()
            if not running:
                self._stop.wait(self.tick_sec)
                continue
            try:
                self._tick_all(running)
            except Exception as exc:
                log.exception("grid tick error: %s", exc)
                try:
                    from .file_logger import log_error
                    log_error(exc, context="grid_legacy_tick")
                except Exception as e:
                    log.debug(f"grid_legacy: suppressed {type(e).__name__}: {e} | ctx: {ctx[:80]}")
            self._stop.wait(self.tick_sec)

    def _prices_for(self, symbols: List[str]) -> Dict[str, float]:
        """Prices from the broker cache first; one /markets call tops up the rest."""
        prices: Dict[str, float] = {}
        broker = self.engine.broker
        need = []
        for s in symbols:
            px = 0.0
            try:
                px = float(broker.last_price(s) or 0.0)
            except Exception:
                px = 0.0
            if px > 0:
                prices[s] = px
            else:
                need.append(s)
        if need:
            try:
                markets = self.engine.client.get_markets()
                for m in markets:
                    sym = str(m.get("symbol") or "").upper()
                    if sym in need:
                        try:
                            px = float((m.get("ticker") or {}).get("price") or m.get("price") or 0)
                        except (TypeError, ValueError):
                            px = 0.0
                        if px > 0:
                            prices[sym] = px
                            try:
                                broker.set_price(sym, px)
                            except Exception as e:
                                log.debug(f"grid_legacy: suppressed {type(e).__name__}: {e} | ctx: {ctx[:80]}")
            except Exception as exc:
                log.warning("grid price fetch failed: %s", exc)
        return prices

    def _tick_all(self, running: List[GridRunner]) -> None:
        # respect the engine's tick lock — never race the engine's own tick
        tick_lock = getattr(self.engine, "_tick_lock", None)
        if tick_lock is not None and tick_lock.locked():
            return
        # FIX(reserve): broker may have been swapped (mode/paper-live switch) —
        # re-assert every running grid's capital lock on the current broker.
        for r in running:
            try:
                self._broker_reserve(r)
            except Exception as e:
                log.debug(f"grid_legacy: suppressed {type(e).__name__}: {e} | ctx: {ctx[:80]}")
        symbols = sorted({r.p["symbol"] for r in running})
        prices = self._prices_for(symbols)
        changed = False
        for r in running:
            px = prices.get(r.p["symbol"], 0.0)
            if px <= 0:
                continue
            res = r.tick(px, self.engine.broker, on_change=None)
            if res["changed"]:
                changed = True
        if changed:
            self.save()

    # ── API-facing helpers ─────────────────────────────────────────
    def summary(self) -> List[dict]:
        out = []
        for r in self.runners.values():
            snap = r.snapshot()
            # fee-spread warning: gap must be >= 3x taker fee to stay profitable
            fee_pct = float(getattr(self.engine.broker, "fee", 0.002) or 0.002) * 100.0
            levels = r.compute_levels()
            min_gap = min(gap_pct(levels, i) for i in range(len(levels) - 1)) if len(levels) > 1 else 0.0
            snap["min_gap_pct"] = round(min_gap, 4)
            snap["fee_pct"] = round(fee_pct, 4)
            snap["fee_gap_warning"] = bool(min_gap and min_gap < 3 * fee_pct)
            out.append(snap)
        out.sort(key=lambda x: x["id"])
        return out

    def preview(self, profile: dict, price: float = 0.0) -> dict:
        """Pre-launch preview: levels, per-order size, pair profit, warnings."""
        p, err = validate_profile(profile)
        if err:
            return {"ok": False, "error": err}
        levels = build_levels(p["range_min"], p["range_max"], p["grid_count"], p["spacing"])
        weights = level_weights(p["grid_count"], p["allocation"])
        fee_pct = float(getattr(self.engine.broker, "fee", 0.002) or 0.002) * 100.0
        rows = []
        n = len(levels)
        for i, P in enumerate(levels):
            legs = legs_of(p["direction"])
            share = 1.0 / len(legs)
            value = p["total_quote"] * share * weights[i]
            lev = p.get("leverage", 1.0) if p["mode"] == "margin" else 1.0
            qty = value * lev / max(P, 1e-12)
            gap = gap_pct(levels, i)
            rows.append({
                "idx": i, "price": round(P, 10),
                "order_quote": round(value, 6),
                "qty": round(qty, 8),
                "gap_pct": round(gap, 4),
                "pair_profit_est": round(value * lev * gap / 100.0, 8),
                "below_market": bool(price and P < price),
            })
        min_gap = min((r["gap_pct"] for r in rows if r["gap_pct"] > 0), default=0.0)
        fee_advice = None
        # broker.fee is a FRACTION (0.002 = 0.2%) — normalize to percent here
        # (summary() does the same ×100 for its own warning).
        if min_gap and min_gap < 3 * fee_pct:
            # Concrete fixes so "profit is not eaten by fees" — MEXC/BingX rule:
            # keep every level gap >= 3x the taker fee.
            target = 3.0 * fee_pct                       # required gap, percent
            span_pct = (p["range_max"] - p["range_min"]) / p["range_min"] * 100.0
            ratio = p["range_max"] / p["range_min"]
            # max grid counts that still satisfy gap >= target on this range;
            # shave one extra level to absorb floating-point rounding so the
            # suggestion always STRICTLY beats the failing configuration.
            n_arith = int(span_pct / target) + 1 if target > 0 else MAX_LEVELS
            if n_arith >= p["grid_count"]:
                n_arith = max(MIN_LEVELS, p["grid_count"] - 1)
            n_arith = max(min(n_arith, MAX_LEVELS), MIN_LEVELS)
            n_geo = int(math.log(ratio) / math.log(1.0 + target / 100.0)) + 1 if ratio > 1 and target > 0 else MAX_LEVELS
            if n_geo >= p["grid_count"]:
                n_geo = max(MIN_LEVELS, p["grid_count"] - 1)
            n_geo = max(min(n_geo, MAX_LEVELS), MIN_LEVELS)
            # range width needed to keep the CURRENT grid count profitable
            needed_span_pct = (p["grid_count"] - 1) * target
            fee_advice = {
                "target_gap_pct": round(target, 4),
                "net_pair_pct": round(min_gap - 2.0 * fee_pct, 4),   # per completed pair, after 2-sided fees
                "max_grids_arith": n_arith,
                "max_grids_geo": n_geo,
                "needed_span_pct": round(needed_span_pct, 2),
                "current_span_pct": round(span_pct, 2),
                "widen_helps": bool(needed_span_pct > span_pct * 1.01),
                "reduce_helps": bool(max(n_arith, n_geo) < p["grid_count"]),
                "better_spacing": "geometric" if ratio >= 1.5 else "arithmetic",
            }
        return {
            "ok": True,
            "levels": rows,
            "count": n,
            "min_gap_pct": round(min_gap, 4),
            "fee_pct": round(fee_pct, 4),
            "fee_gap_warning": bool(min_gap and min_gap < 3 * fee_pct),
            "fee_advice": fee_advice,
            "legs": legs,
            "leg_capital": round(p["total_quote"] / len(legs), 6),
            "price_used": price,
        }

    def ai_advise(self, gid: str, call_provider: Callable[[str], str]) -> dict:
        """Ask the AI provider for the best spillover approach for missed fills."""
        r = self.runners.get(gid)
        if r is None:
            return {"ok": False, "error": "grid not found"}
        if not r.missed:
            return {"ok": True, "advice": "no_missed", "text": "No missed fills — grid is clean.", "action": "none"}
        ctx = json.dumps({
            "symbol": r.p["symbol"], "mode": r.p["mode"], "direction": r.p["direction"],
            "last_price": r.last_price, "range": [r.p["range_min"], r.p["range_max"]],
            "spillover_tol_pct": r.p["spillover_tol_pct"],
            "missed": r.missed[-20:],
        }, ensure_ascii=False)
        prompt = (
            "You are a crypto grid-trading execution advisor. A grid bot missed some "
            "level fills because price gapped past them or the broker rejected the order. "
            "Decide the best approach for EACH missed fill.\n"
            "Allowed actions: catch_up_market (fill now at market if favorable), "
            "wait (re-arm, wait for price to come back to the grid price), skip (abandon this fill).\n"
            f"Context: {ctx}\n"
            'Reply with ONLY JSON: {"action": "catch_up_market"|"wait"|"skip", '
            '"text": "<short Persian explanation for the user>"}'
        )
        try:
            raw = call_provider(prompt)
            data = json.loads(raw)
            action = str(data.get("action", "wait")).lower()
            if action not in ("catch_up_market", "wait", "skip"):
                action = "wait"
            advice = {"ok": True, "advice": action, "text": str(data.get("text", ""))[:500],
                      "missed_count": len(r.missed)}
        except Exception as exc:
            return {"ok": False, "error": f"AI advise failed: {exc}"}
        # apply the advice
        if action == "catch_up_market" and r.running:
            broker = self.engine.broker
            px = r.last_price
            for m in list(r.missed):
                idx, leg, act = m["level"], m["leg"], m["action"]
                levels = r.compute_levels()
                if idx >= len(levels):
                    continue
                arr = r.levels_state.get(leg) or []
                if idx < len(arr) and arr[idx]["state"] == "idle":
                    if act == "buy" and px <= levels[idx]:
                        if r._do_buy(leg, idx, levels[idx], px, broker, int(time.time())):
                            r.missed.remove(m)
                    elif act == "sell" and px >= levels[idx]:
                        if r._do_short_open(leg, idx, levels[idx], px, broker, int(time.time())):
                            r.missed.remove(m)
        elif action == "skip":
            r.missed = []
        r._event("ai_advice", f"{action}: {advice.get('text', '')[:150]}")
        self.save()
        return advice
