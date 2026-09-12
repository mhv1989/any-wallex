"""Grid module — 25 levels between the 30-day main support/resistance.

The grid is NEVER an entry signal. It only:
  - splits capital into bands to cap position size, and
  - is disabled when the S/R range is under 4% (too tight).
"""
from __future__ import annotations

from typing import List, Optional, Tuple

from .models import Candle


def main_sr_30d(candles_1d: List[Candle], lookback_days: int = 30) -> Tuple[Optional[float], Optional[float]]:
    """Main support/resistance from the last N daily candles (closed only)."""
    window = candles_1d[-lookback_days:] if len(candles_1d) > lookback_days else candles_1d
    if len(window) < 5:
        return None, None
    return min(c.l for c in window), max(c.h for c in window)


def build_grid(
    candles_1d: List[Candle],
    cfg: dict,
) -> Tuple[bool, List[float], float, float]:
    """Returns (active, levels, support, resistance)."""
    gcfg = cfg.get("grid", {})
    n_levels = int(gcfg.get("levels", 25))
    lookback = int(gcfg.get("lookback_days", 30))
    min_range_pct = float(gcfg.get("min_range_pct", 4.0))

    support, resistance = main_sr_30d(candles_1d, lookback)
    if support is None or resistance is None or support <= 0:
        return False, [], 0.0, 0.0

    range_pct = (resistance - support) / support * 100.0
    if range_pct < min_range_pct:
        return False, [], support, resistance  # range too tight -> grid disabled

    step = (resistance - support) / (n_levels - 1)
    grid = [support + i * step for i in range(n_levels)]
    return True, grid, support, resistance


def grid_size_factor(grid_active: bool, price: float, grid_levels: List[float], cfg: dict) -> float:
    """Capital-split factor: each position may use at most
    (grid_band_value / capital) of equity. With 25 levels and
    bands_per_position=3, one position spans ~3/25 = 12% of the grid range.

    Returns a multiplier in (0, 1] applied to the risk-based size.
    """
    if not grid_active or len(grid_levels) < 2:
        return 1.0
    gcfg = cfg.get("grid", {})
    bands = int(gcfg.get("bands_per_position", 3))
    span = grid_levels[-1] - grid_levels[0]
    if span <= 0:
        return 1.0
    step = span / (len(grid_levels) - 1)
    band_value = bands * step
    # factor = band width as fraction of price (approx notional cap per position)
    factor = band_value / price if price > 0 else 1.0
    return max(min(factor, 1.0), 0.05)
