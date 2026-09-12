"""Shared strategy artifact schema for external AI strategies.

This is the contract between:
- AI provider output
- disk persistence
- backtest runner
- engine signal registry
- frontend strategy picker

Keep this file small and versioned. If you change it, bump STRATEGY_SCHEMA_VERSION.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

# Bump this when the artifact shape changes in a non-backwards-compatible way.
STRATEGY_SCHEMA_VERSION = "1.1"

# Timeframes allowed for external strategies.
# "1D" added: the engine already fetches/caches daily candles (grid TF), so a
# daily-swing strategy evaluates on the same closed-1D series the grid uses.
ALLOWED_TIMEFRAMES = ("15", "60", "240", "1D")

# Execution modes supported by the engine/frontend.
ALLOWED_EXECUTION_MODES = {"auto", "grid", "signal", "tp_sl_dollar"}

# Indicators the engine evaluator + chart can actually compute (keep in sync with bot/indicators.py).
ALLOWED_INDICATORS = {
    # trend/MA family
    "ema", "sma", "wma", "hma", "vwma", "tema", "dema",
    # momentum/oscillator
    "rsi", "stochastic", "stochastic_rsi", "macd", "obv", "adx", "aroon",
    "cci", "roc", "williams_r", "mfi", "ultimate_oscillator", "awesome_oscillator",
    # volatility
    "atr", "bollinger", "keltner", "donchian", "envelope",
    # volume
    "volume", "cmf", "vwap",
    # structure/levels
    "pivot", "fibonacci", "support_resistance",
    # candle patterns
    "engulfing", "pinbar", "inside_bar",
}

# Minimal required fields for any external strategy artifact.
REQUIRED_FIELDS = [
    "schema_version",
    "strategy_id",
    "name",
    "description",
    "source",
    "provider",
    "model",
    "timeframe",
    "execution_mode",
    "cooldown_bars",
    "min_confidence",
    "entry_conditions",
    "exit_conditions",
    "risk",
    "enabled",
]


def new_artifact(vibe: str, provider: str, model: str) -> Dict[str, Any]:
    """Return a blank external-strategy artifact ready for AI population."""
    return {
        "schema_version": STRATEGY_SCHEMA_VERSION,
        "strategy_id": "",
        "name": "",
        "description": "",
        "source": "external_ai",
        "provider": provider,
        "model": model,
        "timeframe": "60",
        "execution_mode": "auto",
        "cooldown_bars": 3,
        "min_confidence": 0.55,
        "entry_conditions": [],
        "exit_conditions": [],
        "risk": {
            "max_positions": 1,
            "risk_per_trade_pct": 1.0,
            "stop_atr_mult": 1.5,
            "target_atr_mult": 3.0,
            "dollar_tp": 0.0,
            "dollar_stop": 0.0,
            "tmn_tp": 0.0,
            "tmn_stop": 0.0,
            "grid_mode": "none",
            "grid_step_pct": 1.0,
            "grid_max_steps": 5,
        },
        "parameters": {},
        "created_ts": 0,
        "updated_ts": 0,
        "vibe_prompt": vibe,
        "enabled": True,
    }


def validate_artifact(data: Dict[str, Any]) -> None:
    """Raise ValueError if artifact is malformed or uses unsupported features."""
    missing = [k for k in REQUIRED_FIELDS if k not in data]
    if missing:
        raise ValueError(f"Missing required fields: {missing}")

    if data.get("schema_version") != STRATEGY_SCHEMA_VERSION:
        raise ValueError(f"Unsupported schema version: {data.get('schema_version')}")

    tf = str(data.get("timeframe", ""))
    if tf not in ALLOWED_TIMEFRAMES:
        raise ValueError(f"Invalid timeframe: {tf}. Allowed: {ALLOWED_TIMEFRAMES}")

    if "enabled" not in data:
        data["enabled"] = True
    if not isinstance(data.get("enabled"), bool):
        raise ValueError("enabled must be a boolean")

    execution_mode = str(data.get("execution_mode", "auto")).lower()
    if execution_mode not in ALLOWED_EXECUTION_MODES:
        raise ValueError(f"Invalid execution_mode: {execution_mode}. Allowed: {sorted(ALLOWED_EXECUTION_MODES)}")
    data["execution_mode"] = execution_mode

    risk = data.get("risk", {})
    if not isinstance(risk, dict):
        raise ValueError("risk must be a dict")
    for k in ("max_positions", "risk_per_trade_pct", "stop_atr_mult", "target_atr_mult"):
        if k not in risk or not isinstance(risk[k], (int, float)) or risk[k] <= 0:
            raise ValueError(f"Invalid risk.{k}: {risk.get(k)}")
    for k in ("dollar_tp", "dollar_stop"):
        if k not in risk:
            risk[k] = 0.0
    grid_mode = str(risk.get("grid_mode", "none")).lower()
    if grid_mode not in {"none", "long", "short", "both"}:
        raise ValueError(f"Invalid risk.grid_mode: {grid_mode}")
    risk["grid_mode"] = grid_mode

    for cond_list in ("entry_conditions", "exit_conditions"):
        conds = data.get(cond_list, [])
        if not isinstance(conds, list):
            raise ValueError(f"{cond_list} must be a list")
        for cond in conds:
            ind = str(cond.get("indicator", "")).lower()
            if ind not in ALLOWED_INDICATORS:
                raise ValueError(f"Unsupported indicator in {cond_list}: {ind}")
