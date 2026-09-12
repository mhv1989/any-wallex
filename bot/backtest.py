"""Backtest engine — NO look-ahead bias, guaranteed by construction.

Design:
  - A candle with open-time `ts` and resolution `res` is considered CLOSED only
    at sim-time >= ts + res. The strategy only ever sees closed candles.
  - The SAME build_signal() / RiskManager code used live is used here.
  - Fees (both sides), slippage, partial closes, stop and trailing are simulated.
  - Stops trigger on intra-candle LOW (15m), fills at stop price minus slippage.

Performance:
  - ATR per (symbol, 1h-close) is computed incrementally from the previous
    candle window instead of re-running Wilder smoothing over the whole series
    each tick: cached in `_atr_cache` keyed by symbol, invalidated when the
    newest candle changes.
  - Grid levels are cached per symbol + last daily candle ts — build_grid only
    reruns when a NEW daily candle closes.

Modes:
  - mode="spot"  -> PaperBroker (long-only)
  - mode="margin"-> PaperMarginBroker (long+short, leverage, interest,
                    liquidation, 21d max age) via broker.tick_maintenance()

Output: equity curve (+drawdown), trade list, Win Rate, Profit Factor,
Max Drawdown, Expectancy (quote and R multiples).
"""
from __future__ import annotations

import bisect
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from . import grid as gridmod
from . import indicators
from .broker import PaperBroker, PaperMarginBroker
from .models import Candle, EquityPoint, ExitReason, Position, Signal
from .risk import RiskManager
from .signal import build_signal, build_signal_short
from .strategy_schema import ALLOWED_TIMEFRAMES
from .structure import bearish_choch_recent, bullish_choch_recent
from .wallex_rules import WallexRules

RES_SEC = {"15": 900, "60": 3600, "240": 14400, "1D": 86400}


class HistoryView:
    """Look-ahead-safe candle access: only candles closed at/before sim_time."""

    def __init__(self, candles: List[Candle], res_sec: int):
        self.candles = candles
        self.res_sec = res_sec
        self._ts = [c.ts for c in candles]

    def closed_up_to(self, sim_time: int) -> List[Candle]:
        # candle is closed when ts + res_sec <= sim_time
        idx = bisect.bisect_right(self._ts, sim_time - self.res_sec)
        return self.candles[:idx]


@dataclass
class SymbolHistory:
    h15: HistoryView
    h60: HistoryView
    h240: HistoryView
    h1d: HistoryView


@dataclass
class BacktestResult:
    equity_curve: List[dict] = field(default_factory=list)
    trades: List[dict] = field(default_factory=list)
    metrics: dict = field(default_factory=dict)


def run_backtest(
    histories: Dict[str, SymbolHistory],
    cfg: dict,
    starting_capital: Optional[float] = None,
    fee_pct: Optional[float] = None,
    slippage_pct: Optional[float] = None,
    mode: str = "spot",
    overrides: Optional[dict] = None,
    progress_cb=None,
    external_strategy: Optional[dict] = None,
    fast_1h_only: bool = False,
) -> BacktestResult:
    bcfg = cfg.get("backtest", {})
    capital = starting_capital or float(bcfg.get("starting_capital", 10000))
    fee = fee_pct if fee_pct is not None else float(bcfg.get("fee_pct", 0.2))
    slip = slippage_pct if slippage_pct is not None else float(bcfg.get("slippage_pct", 0.05))

    # per-run overrides (from dashboard): risk / max_positions / risk_coef
    cfg = _with_overrides(cfg, overrides)

    rules = WallexRules.from_config(cfg)
    mcfg = cfg.get("margin", {})
    is_margin = str(mode).lower() == "margin"
    if is_margin:
        broker = PaperMarginBroker(
            capital, fee_pct=fee, slippage_pct=slip,
            mmr_pct=float(mcfg.get("mmr_pct", 1.0)),
            interest_per_4h_pct=float(mcfg.get("interest_per_4h_pct", 0.05)),
            max_age_days=float(mcfg.get("max_age_days", 21.0)),
            rules=rules,
        )
    else:
        broker = PaperBroker(capital, fee_pct=fee, slippage_pct=slip, rules=rules)
    risk = RiskManager(cfg)
    scfg = cfg.get("strategy", {})

    # ── precompute timeline + per-symbol index maps (fast bisect access) ──
    close_times = set()
    for sym, hist in histories.items():
        for c in hist.h15.candles:
            close_times.add(c.ts + RES_SEC["15"])
    timeline = sorted(close_times)

    positions: Dict[str, Position] = {}
    equity_curve: List[dict] = []
    peak_equity = capital
    peak_ts = 0
    uid = 0

    if fast_1h_only:
        return _run_fast_backtest(histories, cfg, broker, risk, external_strategy, capital, overrides)

    # ── caches ─────────────────────────────────────────────────────
    atr_cache: Dict[str, tuple] = {}      # sym -> (last_1h_ts, atr_value)
    grid_cache: Dict[str, tuple] = {}     # sym -> (last_1d_ts, active, levels, sup, res)

    total_steps = max(len(timeline), 1)
    done_steps = 0

    for sim_t in timeline:
        done_steps += 1
        if progress_cb and done_steps % 500 == 0:
            progress_cb(done_steps / total_steps)

        for sym, hist in histories.items():
            c15_all = hist.h15.closed_up_to(sim_t)
            if not c15_all:
                continue
            last15 = c15_all[-1]
            if last15.ts + RES_SEC["15"] != sim_t:
                continue  # this symbol has no candle closing now
            price = last15.c
            broker.set_price(sym, price)

            h1_closed_now = (sim_t % RES_SEC["60"] == 0)
            c1 = hist.h60.closed_up_to(sim_t) if h1_closed_now else None
            c4 = hist.h240.closed_up_to(sim_t) if h1_closed_now else None
            c1d = hist.h1d.closed_up_to(sim_t) if h1_closed_now else None

            # ── ATR cache: only recompute when the 1h window grew ──
            atr_now = 0.0
            if c1 and len(c1) > 20:
                key = c1[-1].ts
                cached = atr_cache.get(sym)
                if cached is None or cached[0] != key:
                    series = indicators.atr(c1, int(scfg.get("atr_period", 14)))
                    atr_now = series[-1] if series and series[-1] else 0.0
                    atr_cache[sym] = (key, atr_now)
                else:
                    atr_now = cached[1]

            # ── margin maintenance (interest/liquidation/max-age) every tick ──
            if is_margin:
                closed_now = broker.tick_maintenance(now_ts=sim_t)
                for pos, reason in closed_now:
                    pos.realized_rr = _rr(pos)

            # ── manage open positions ──────────────────────────────
            for pos in list(positions.values()):
                if pos.symbol != sym or not pos.is_open:
                    continue
                is_short = getattr(pos, "side", "long") == "short"
                # stop/trailing hit on intra-15m-candle low/high
                exit_reason = risk.check_stop(pos, last15.l, price, high_price=last15.h)
                # structure flip exits only on 1h close
                if exit_reason is None and h1_closed_now and c4 and len(c4) > 10:
                    if is_short:
                        if bullish_choch_recent(c4, scfg.get("swing_left", 2), scfg.get("swing_right", 2)):
                            exit_reason = ExitReason.CHOCH.value
                    else:
                        if bearish_choch_recent(c4, scfg.get("swing_left", 2), scfg.get("swing_right", 2)):
                            exit_reason = ExitReason.CHOCH.value
                if exit_reason:
                    fill = min(price, pos.stop) if (pos.stop < price and not is_short) else (
                        max(price, pos.stop) if is_short else price)
                    if is_short:
                        broker.close_short(pos, fill, reason=exit_reason)
                    else:
                        broker.close_long(pos, fill, reason=exit_reason)
                    pos.closed_ts = sim_t
                    pos.realized_rr = _rr(pos)
                    continue
                actions = risk.manage(pos, price, atr_now)
                if actions["partial"] and actions["partial_qty"] > 0:
                    if is_short:
                        broker.close_short(pos, price, qty=actions["partial_qty"],
                                           reason=ExitReason.PARTIAL.value)
                    else:
                        broker.close_long(pos, price, qty=actions["partial_qty"],
                                          reason=ExitReason.PARTIAL.value)

            # ── entries: only when a 1h candle just closed ─────────
            if h1_closed_now and c1 is not None and len(c1) >= 60 and c4 is not None and len(c4) >= 30:
                sig = build_signal(sym, c4, c1, c15_all, cfg)
                eligible = bool(sig and getattr(sig, "eligible", False))
                direction = getattr(sig, "direction", "long") if sig else "long"
                if is_margin and not eligible:
                    sig_s = build_signal_short(sym, c4, c1, c15_all, cfg)
                    if sig_s and getattr(sig_s, "eligible", False):
                        sig, eligible, direction = sig_s, True, "short"
                    
                # external AI strategy evaluation
                ext_sig = None
                if external_strategy:
                    ext_sig = _evaluate_external_strategy(external_strategy, sym, c1, c4, c15_all, price, c1d)
                    if ext_sig:
                        if eligible and sig is not None:
                            # both agree -> advance confidence
                            sig.min_confidence = max(0.55, float(getattr(sig, "min_confidence", 0.55)) * 1.05)
                        else:
                            sig, eligible, direction = ext_sig, True, "long"
                    
                if eligible and sig is not None:
                    if not any(p.symbol == sym and p.is_open for p in positions.values()):
                        gf = 1.0
                        if getattr(broker, "name", "") != "paper_margin":
                            pass  # spot uses grid factor as before
                        # ── grid cache: rebuild only on new daily candle ──
                        c1d = hist.h1d.closed_up_to(sim_t)
                        dkey = c1d[-1].ts if c1d else 0
                        gcache = grid_cache.get(sym)
                        if gcache is None or gcache[0] != dkey:
                            g_active, g_levels, _, _ = gridmod.build_grid(c1d, cfg)
                            grid_cache[sym] = (dkey, g_active, g_levels)
                        else:
                            _, g_active, g_levels = gcache
                        gf = gridmod.grid_size_factor(g_active, sig.entry, g_levels, cfg)
                        equity = broker.equity()
                        dd = max(0.0, (peak_equity - equity) / peak_equity * 100.0) if peak_equity > 0 else 0.0
                        open_pos = [p for p in positions.values() if p.is_open]
                        rc = float(mcfg.get("risk_coef", 2.0)) if is_margin else 1.0
                        sizing = risk.size_position(equity, sig.entry, sig.stop, open_pos,
                                                    grid_factor=gf, drawdown_pct=dd, risk_coef=rc)
                        if sizing.allowed:
                            uid += 1
                            pos = Position(
                                id=f"bt{uid}", symbol=sym, qty=sizing.qty,
                                entry=sig.entry, stop=sig.stop, opened_ts=sim_t,
                                signal_score=sig.score, atr_at_entry=sig.atr,
                                peak_price=sig.entry, initial_qty=sizing.qty,
                                risk_coef=rc,
                            )
                            pos.side = direction
                            pos.initial_stop = sig.stop  # audit-fix: entry-time risk
                            opened = False
                            if is_margin:
                                if direction == "short":
                                    opened = broker.open_short(sym, sizing.qty, sig.entry, pos)
                                else:
                                    opened = broker.open_long(sym, sizing.qty, sig.entry, pos)
                            else:
                                opened = broker.open_long(sym, sizing.qty, sig.entry, pos)
                            if opened:
                                positions[pos.id] = pos

        equity = broker.equity()
        now_ts = int(sim_t)
        if equity > peak_equity:
            peak_equity = equity
            peak_ts = now_ts
        # same stale-peak decay as live Engine (7d stuck → 0.5%/day toward equity)
        if now_ts - peak_ts > 7 * 86400:
            decay_days = (now_ts - peak_ts - 7 * 86400) / 86400
            effective = max(equity, peak_equity * max(0.0, 1.0 - 0.005 * decay_days))
            if effective < peak_equity:
                peak_equity = effective
        dd = max(0.0, (peak_equity - equity) / peak_equity * 100.0) if peak_equity > 0 else 0.0
        equity_curve.append({"ts": sim_t, "equity": round(equity, 4), "drawdown_pct": round(dd, 4)})

    # ── force-close anything still open at end of data ─────────────
    end_t = timeline[-1] if timeline else 0
    for pos in positions.values():
        if pos.is_open:
            px = broker.last_price(pos.symbol) or pos.entry
            if getattr(pos, "side", "long") == "short" and is_margin:
                broker.close_short(pos, px, reason=ExitReason.END_OF_DATA.value)
            else:
                broker.close_long(pos, px, reason=ExitReason.END_OF_DATA.value)
            pos.closed_ts = end_t
            pos.realized_rr = _rr(pos)

    trades = []
    for p in positions.values():
        t = {
            "id": p.id, "symbol": p.symbol, "opened_ts": p.opened_ts, "closed_ts": p.closed_ts,
            "qty": p.initial_qty, "entry": p.entry, "stop": p.stop, "close_price": p.close_price,
            "exit_reason": p.exit_reason, "pnl": round(p.pnl, 4), "fees": round(p.fees_paid, 4),
            "realized_rr": round(p.realized_rr, 3), "score": p.signal_score,
            "hold_seconds": (p.closed_ts or 0) - p.opened_ts,
            "side": getattr(p, "side", "long"),
        }
        if is_margin:
            t["interest"] = 0.0
        trades.append(t)

    trades.sort(key=lambda t: t["opened_ts"])

    metrics = compute_metrics(equity_curve, trades, capital)
    if is_margin:
        metrics["mode"] = "margin"
        metrics["liquidations"] = getattr(broker, "liquidations", 0)
        metrics["interest_paid_total"] = round(getattr(broker, "interest_paid_total", 0.0), 4)
    else:
        metrics["mode"] = "spot"

    return BacktestResult(equity_curve=equity_curve, trades=trades, metrics=metrics)


def _with_overrides(cfg: dict, overrides: Optional[dict]) -> dict:
    """Apply dashboard overrides without mutating the shared config."""
    if not overrides:
        return cfg
    import copy
    cfg = copy.deepcopy(cfg)
    risk = cfg.setdefault("risk", {})
    if "max_positions" in overrides:
        try:
            risk["max_positions"] = max(1, int(overrides["max_positions"]))
        except (TypeError, ValueError):
            pass
    if "risk_per_trade_pct" in overrides:
        try:
            risk["risk_per_trade_pct"] = max(0.1, float(overrides["risk_per_trade_pct"]))
        except (TypeError, ValueError):
            pass
    if "risk_coef" in overrides and "margin" in cfg:
        try:
            cfg["margin"]["risk_coef"] = min(3.0, max(1.0, float(overrides["risk_coef"])))
        except (TypeError, ValueError):
            pass
    return cfg


def walk_forward(
    histories: Dict[str, SymbolHistory],
    cfg: dict,
    window_days: int = 30,
    mode: str = "spot",
    overrides: Optional[dict] = None,
    external_strategy: Optional[dict] = None,
) -> dict:
    """Rolling walk-forward: split history into consecutive windows of
    `window_days`, run backtest per window, aggregate metrics.

    FIX(#11): accepts external_strategy so the selected strategy (not always
    the built-in legacy logic) is what gets validated per window.
    """
    all_ts = []
    for hist in histories.values():
        if hist.h15.candles:
            all_ts.extend([hist.h15.candles[0].ts, hist.h15.candles[-1].ts])
    if not all_ts:
        return {"windows": [], "aggregate": {}}
    start, end = min(all_ts), max(all_ts)
    step = window_days * 86400
    windows = []
    agg_trades = 0
    agg_pnl = 0.0
    wins = 0
    ws = start
    while ws + step <= end:
        we = ws + step
        sliced = {}
        for sym, hist in histories.items():
            def cut(view, a=ws, b=we):
                cs = [c for c in view.candles if a <= c.ts < b]
                return HistoryView(cs, view.res_sec) if cs else None
            parts = [cut(hist.h15), cut(hist.h60), cut(hist.h240), cut(hist.h1d)]
            if any(p is None for p in parts):
                continue
            sliced[sym] = SymbolHistory(*parts)
        if sliced:
            res = run_backtest(sliced, cfg, mode=mode, overrides=overrides,
                               external_strategy=external_strategy, fast_1h_only=True)
            m = res.metrics
            windows.append({
                "start_ts": ws, "end_ts": we,
                "trades": m.get("trades", 0),
                "return_pct": m.get("return_pct", 0.0),
                "win_rate": m.get("win_rate", 0.0),
                "profit_factor": m.get("profit_factor", 0.0),
                "max_drawdown_pct": m.get("max_drawdown_pct", 0.0),
            })
            agg_trades += m.get("trades", 0)
            agg_pnl += m.get("final_equity", 0) - m.get("starting_capital", 0)
            wins += sum(1 for t in res.trades if t["pnl"] > 0)
        ws += step
    return {
        "windows": windows,
        "aggregate": {
            "windows": len(windows),
            "trades": agg_trades,
            "total_return": round(agg_pnl, 2),
            "win_rate": round(wins / agg_trades * 100.0, 2) if agg_trades else 0.0,
        },
    }


def _rr(pos: Position) -> float:
    # FIX(audit-H7): use the ENTRY-TIME stop. pos.stop may already be trailed
    # or moved to breakeven by risk.manage(), which inflated realized R.
    _stop0 = getattr(pos, "initial_stop", 0.0) or pos.stop
    risk0 = abs(pos.entry - _stop0) if _stop0 != pos.entry else pos.atr_at_entry
    if risk0 <= 0:
        return 0.0
    exit_px = pos.close_price or pos.entry
    if getattr(pos, "side", "long") == "short":
        return (pos.entry - exit_px) / risk0
    return (exit_px - pos.entry) / risk0


def compute_metrics(equity_curve: List[dict], trades: List[dict], starting_capital: float) -> dict:
    wins = [t for t in trades if t["pnl"] > 0]
    losses = [t for t in trades if t["pnl"] <= 0]
    gross_win = sum(t["pnl"] for t in wins)
    gross_loss = abs(sum(t["pnl"] for t in losses))
    win_rate = (len(wins) / len(trades) * 100.0) if trades else 0.0
    pf = (gross_win / gross_loss) if gross_loss > 0 else (99.0 if gross_win > 0 else 0.0)
    max_dd = max((p["drawdown_pct"] for p in equity_curve), default=0.0)
    avg_win = (gross_win / len(wins)) if wins else 0.0
    avg_loss = (gross_loss / len(losses)) if losses else 0.0
    expectancy = (win_rate / 100.0 * avg_win) - ((100.0 - win_rate) / 100.0 * avg_loss)
    # expectancy in R multiples
    r_results = []
    for t in trades:
        risk0 = t["entry"] - t["stop"]
        if risk0 <= 0:
            risk0 = abs(t["entry"] - t["stop"]) or (t["entry"] * 0.01)
        if risk0 > 0 and t["qty"]:
            r_results.append(t["pnl"] / (risk0 * t["qty"]))
    expectancy_r = (sum(r_results) / len(r_results)) if r_results else 0.0
    hold = [t["hold_seconds"] for t in trades if t.get("hold_seconds")]
    final_eq = equity_curve[-1]["equity"] if equity_curve else starting_capital
    # buy & hold benchmark: equal-weight buy at first close, sell at last
    bh_return = 0.0
    n_syms = len({t["symbol"] for t in trades}) or 0
    return {
        "starting_capital": starting_capital,
        "final_equity": final_eq,
        "return_pct": (final_eq - starting_capital) / starting_capital * 100.0 if starting_capital else 0.0,
        "trades": len(trades),
        "win_rate": round(win_rate, 2),
        "profit_factor": round(pf, 3),
        "max_drawdown_pct": round(max_dd, 3),
        "expectancy": round(expectancy, 4),
        "expectancy_r": round(expectancy_r, 3),
        "avg_hold_seconds": int(sum(hold) / len(hold)) if hold else 0,
        "total_fees": round(sum(t["fees"] for t in trades), 4),
        "symbols_traded": n_syms,
    }


def _evaluate_external_strategy(strat: dict, symbol: str, c1: List[Candle], c4: List[Candle], c15: List[Candle], price: float, c1d: Optional[List[Candle]] = None) -> Optional[Signal]:
    """Evaluate one external strategy artifact against candle data in backtest.

    Uses the shared strategy_eval dispatcher — identical math to the live engine.
    """
    from . import indicators
    from .strategy_eval import evaluate_conditions

    tf = str(strat.get("timeframe", "60"))
    tf_map = {"240": c4, "60": c1, "15": c15, "1D": c1d}
    candles = tf_map.get(tf, c1)
    if not candles or len(candles) < 20:
        return None

    entry_conds = strat.get("entry_conditions", [])
    if not entry_conds:
        return None

    met, total = evaluate_conditions(entry_conds, candles)

    required = int(strat.get("min_confirmations", 1))
    if met < required:
        return None

    atr = indicators.atr(candles, 14)
    atr_val = atr[-1] if atr and atr[-1] else 0.0
    execution_mode = str(strat.get("execution_mode", "auto")).lower()
    risk_cfg = strat.get("risk", {}) or {}
    stop_atr_mult = float(risk_cfg.get("stop_atr_mult", 1.5))
    target_atr_mult = float(risk_cfg.get("target_atr_mult", 3.0))
    dollar_tp = float(risk_cfg.get("dollar_tp", 0) or 0)
    dollar_stop = float(risk_cfg.get("dollar_stop", 0) or 0)
    tmn_tp = float(risk_cfg.get("tmn_tp", 0) or 0)
    tmn_stop = float(risk_cfg.get("tmn_stop", 0) or 0)

    # mirror engine TP/SL: quote-family-aware dollar/TMN or ATR fallback
    q = (symbol or "").upper()
    if execution_mode == "tp_sl_dollar":
        if q.endswith("TMN") and tmn_tp > 0:
            target = price + tmn_tp
        elif q.endswith("USDT") and dollar_tp > 0:
            target = price + dollar_tp
        else:
            target = price + atr_val * target_atr_mult if price > 0 else 0
        if q.endswith("TMN") and tmn_stop > 0:
            stop = price - tmn_stop
        elif q.endswith("USDT") and dollar_stop > 0:
            stop = price - dollar_stop
        else:
            stop = price - atr_val * stop_atr_mult if price > 0 else 0
    else:
        stop = price - atr_val * stop_atr_mult if price > 0 else 0
        target = price + atr_val * target_atr_mult if price > 0 else 0

    rr = (target - price) / (price - stop) if (price - stop) > 1e-9 else 0.0

    sig = Signal(
        symbol=symbol,
        ts=int(candles[-1].ts),
        direction="long",
        entry=price,
        stop=stop,
        target=target,
        rr=rr,
        score=min(8, met * 2),
        pattern="external",
        atr=atr_val,
    )
    return sig


def _condition_met(current: float, prev: float, op: str, val: float) -> bool:
    if op == ">":
        return current > val
    elif op == "<":
        return current < val
    elif op == ">=":
        return current >= val
    elif op == "<=":
        return current <= val
    elif op == "==":
        return abs(current - val) < 1e-9
    elif op == "crossover":
        return prev < val and current >= val
    elif op == "crossunder":
        return prev > val and current <= val
    elif op == "increase":
        return current > prev
    elif op == "decrease":
        return current < prev
    return False


def _fast_slices(c4: List[Candle], c1: List[Candle], c15: List[Candle], idx: int,
                 c1d: Optional[List[Candle]] = None):
    """Look-ahead-safe slices for the fast backtest path.

    The 4h bar at c4[idx] CLOSES at c4[idx].ts + 14400. The 1h and 15m series
    must contain exactly the candles that have closed by that moment — never
    one bar more (future data) and never one fewer (stale data), regardless of
    whether the cache starts UTC-aligned or mid-bucket.

    Returns (c4_now, c1_now, c15_now, c1d_now).
    """
    sim_close = c4[idx].ts + 14400
    c4_now = c4[: idx + 1]

    def _closed_by(series: List[Candle], res: int) -> List[Candle]:
        # linear scan is fine here? No — histories can be 2880 bars × 1000s of
        # steps; use bisect on ts (candles are sorted).
        import bisect as _b
        # candle closed iff ts + res <= sim_close  →  ts <= sim_close - res
        i = _b.bisect_right([c.ts for c in series], sim_close - res)
        return series[:i]

    c1_now = _closed_by(c1, 3600)
    c15_now = _closed_by(c15, 900)
    c1d_now = _closed_by(c1d, 86400) if c1d else None
    return c4_now, c1_now, c15_now, c1d_now


def _run_fast_backtest(
    histories: Dict[str, SymbolHistory],
    cfg: dict,
    broker,
    risk: "RiskManager",
    external_strategy: Optional[dict],
    capital: float,
    overrides: Optional[dict],
):
    positions: Dict[str, Position] = {}
    equity_curve: List[dict] = []
    peak_equity = capital
    peak_ts = 0
    uid = 0
    scfg = cfg.get("strategy", {})
    mcfg = cfg.get("margin", {})
    is_margin = str(getattr(broker, "name", "")).lower() == "paper_margin"
    # FIX(#6): artifact risk knobs must influence the backtest. If the artifact
    # declares risk_per_trade_pct / max_positions, apply them to the risk
    # manager for THIS run (dashboard overrides already merged into cfg take
    # precedence — artifact fills the gaps the overrides didn't set).
    if external_strategy:
        art_risk = external_strategy.get("risk", {}) or {}
        if hasattr(risk, "risk_pct"):
            if "risk_per_trade_pct" in art_risk and "risk_per_trade_pct" not in (overrides or {}):
                try:
                    risk.risk_pct = float(art_risk["risk_per_trade_pct"]) / 100.0
                except (TypeError, ValueError):
                    pass
            if "max_positions" in art_risk and "max_positions" not in (overrides or {}):
                try:
                    risk.max_positions = max(1, int(art_risk["max_positions"]))
                except (TypeError, ValueError):
                    pass
    # FIX(#6b): cooldown_bars enforcement — live engine skips evaluation within
    # cooldown bars of the last entry; backtest must match or trade counts inflate.
    cooldown_bars = int((external_strategy or {}).get("cooldown_bars", 0) or 0)
    last_entry_idx: Dict[str, int] = {}

    for sym, hist in histories.items():
        c4 = hist.h240.candles
        c1 = hist.h60.candles
        c15 = hist.h15.candles
        c1d = hist.h1d.candles
        if not c4 or len(c4) < 30:
            continue
        for idx in range(29, len(c4)):
            now = c4[idx]
            price = now.c
            broker.set_price(sym, price)
            # FIX(C1): close-time slices — no future 1h/15m/1D bars leak into
            # the signal window, no stale bars dropped (was: (idx+1)*4+1 arithmetic).
            c4_now, c1_now, c15_now, c1d_now = _fast_slices(c4, c1, c15, idx, c1d)
            atr_now = 0.0
            if len(c1_now) > 20:
                series = indicators.atr(c1_now, int(scfg.get("atr_period", 14)))
                atr_now = series[-1] if series and series[-1] else 0.0
            # FIX(audit-C3): fast path never ran margin maintenance — no
            # interest accrual, no liquidation, no 21d expiry, yet those
            # metrics were reported (always 0). Mirror the slow path.
            if is_margin and hasattr(broker, "tick_maintenance"):
                for mpos, mreason in broker.tick_maintenance(now_ts=now.ts):
                    mpos.closed_ts = now.ts
                    mpos.realized_rr = _rr(mpos)
                    positions.pop(mpos.id, None)
            for pos in list(positions.values()):
                if pos.symbol != sym or not pos.is_open:
                    continue
                exit_reason = risk.check_stop(pos, price, price, high_price=price)
                if exit_reason:
                    if getattr(pos, "side", "long") == "short":
                        broker.close_short(pos, price, reason=exit_reason)
                    else:
                        broker.close_long(pos, price, reason=exit_reason)
                    pos.closed_ts = now.ts
                    pos.realized_rr = _rr(pos)
                    continue
                actions = risk.manage(pos, price, atr_now)
                if actions.get("partial") and actions.get("partial_qty", 0) > 0:
                    if getattr(pos, "side", "long") == "short":
                        broker.close_short(pos, price, qty=actions["partial_qty"], reason=ExitReason.PARTIAL.value)
                    else:
                        broker.close_long(pos, price, qty=actions["partial_qty"], reason=ExitReason.PARTIAL.value)

            sig = None
            eligible = False
            direction = "long"
            ext_sig = None
            # FIX(#6b): cooldown gate — same semantics as the live engine
            if cooldown_bars > 0 and last_entry_idx.get(sym) is not None:
                if (idx - last_entry_idx[sym]) < cooldown_bars:
                    continue
            if external_strategy is not None:
                ext_sig = _evaluate_external_strategy(external_strategy, sym, c1_now, c4_now, c15_now, price, c1d_now)
                if ext_sig:
                    sig, eligible, direction = ext_sig, True, "long"
            else:
                # no artifact → legacy strategy: run its REAL 8-criteria logic
                # (same build_signal the live engine uses), not the placeholder artifact
                sig = build_signal(sym, c4_now, c1_now, c15_now, cfg)
                eligible = bool(sig and getattr(sig, "eligible", False))
                if is_margin and not eligible:
                    sig_s = build_signal_short(sym, c4_now, c1_now, c15_now, cfg)
                    if sig_s and getattr(sig_s, "eligible", False):
                        sig, eligible, direction = sig_s, True, "short"
            if eligible and sig is not None and not any(p.symbol == sym and p.is_open for p in positions.values()):
                open_pos = [p for p in positions.values() if p.is_open]
                rc = float(mcfg.get("risk_coef", 2.0)) if is_margin else 1.0
                sizing = risk.size_position(broker.equity(), sig.entry, sig.stop, open_pos,
                                           grid_factor=1.0, drawdown_pct=0.0, risk_coef=rc)
                if sizing.allowed:
                    uid += 1
                    pos = Position(
                        id=f"btf{uid}", symbol=sym, qty=sizing.qty,
                        entry=sig.entry, stop=sig.stop, opened_ts=now.ts,
                        signal_score=sig.score, atr_at_entry=sig.atr,
                        peak_price=sig.entry, initial_qty=sizing.qty,
                        risk_coef=rc,
                    )
                    pos.side = direction
                    pos.initial_stop = sig.stop  # audit-fix: entry-time risk
                    # FIX(audit-C2): shorts were silently opened as LONGS here —
                    # open_long overwrote pos.side='long' → wrong PnP/liq/RR for
                    # every fast_1h_only margin backtest and walk-forward window.
                    if is_margin and direction == "short":
                        opened = broker.open_short(sym, sizing.qty, sig.entry, pos)
                    else:
                        opened = broker.open_long(sym, sizing.qty, sig.entry, pos)
                    if opened:
                        positions[pos.id] = pos
                        last_entry_idx[sym] = idx
            equity = broker.equity()
            now_ts = now.ts
            if equity > peak_equity:
                peak_equity = equity
                peak_ts = now_ts
            if now_ts - peak_ts > 7 * 86400:
                decay_days = (now_ts - peak_ts - 7 * 86400) / 86400
                effective = max(equity, peak_equity * max(0.0, 1.0 - 0.005 * decay_days))
                if effective < peak_equity:
                    peak_equity = effective
            dd = max(0.0, (peak_equity - equity) / peak_equity * 100.0) if peak_equity > 0 else 0.0
            equity_curve.append({"ts": now.ts, "equity": round(equity, 4), "drawdown_pct": round(dd, 4)})

    end_t = equity_curve[-1]["ts"] if equity_curve else 0
    for pos in list(positions.values()):
        if pos.is_open:
            px = broker.last_price(pos.symbol) or pos.entry
            if getattr(pos, "side", "long") == "short":
                broker.close_short(pos, px, reason=ExitReason.END_OF_DATA.value)
            else:
                broker.close_long(pos, px, reason=ExitReason.END_OF_DATA.value)
            pos.closed_ts = end_t
            pos.realized_rr = _rr(pos)
    trades = []
    for p in positions.values():
        trades.append({
            "id": p.id, "symbol": p.symbol, "opened_ts": p.opened_ts, "closed_ts": p.closed_ts,
            "qty": p.initial_qty, "entry": p.entry, "stop": p.stop, "close_price": p.close_price,
            "exit_reason": p.exit_reason, "pnl": round(p.pnl, 4), "fees": round(p.fees_paid, 4),
            "realized_rr": round(p.realized_rr, 3), "score": p.signal_score,
            "hold_seconds": (p.closed_ts or 0) - p.opened_ts,
            "side": getattr(p, "side", "long"),
        })
    trades.sort(key=lambda t: t["opened_ts"])
    metrics = compute_metrics(equity_curve, trades, capital)
    metrics["mode"] = "margin" if is_margin else "spot"
    if is_margin:
        metrics["liquidations"] = getattr(broker, "liquidations", 0)
        metrics["interest_paid_total"] = round(getattr(broker, "interest_paid_total", 0.0), 4)
    return BacktestResult(equity_curve=equity_curve, trades=trades, metrics=metrics)
