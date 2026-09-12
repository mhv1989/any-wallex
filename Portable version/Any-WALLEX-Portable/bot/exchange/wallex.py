"""WallexAdapter — Phase 1 zero-regression adapter.

SUBCLASSES the existing WallexClient, so every attribute and method the
engine/brokers/server already use (get_candles, api_log, api_key, margin_*,
_throttle, ...) behaves exactly as before — the adapter only ADDS exchange
metadata (capabilities, quote families, quirk map, symbol info).
"""
from __future__ import annotations

from typing import Dict, List

from .base import Capabilities, ExchangeAdapter
from ..markets import MarketCatalog
from ..wallex_client import WallexClient


class WallexAdapter(WallexClient, ExchangeAdapter):
    id = "wallex"
    display_name = "Wallex (والکس)"

    # ── ExchangeAdapter metadata ─────────────────────────────────────
    @property
    def capabilities(self) -> Capabilities:
        return Capabilities(
            spot=True,
            margin=True,
            margin_style="isolated_margin",  # collateral + risk_coef model (NOT futures)
            futures=False,
            udf_native=True,
            notes="Wallex margin = collateral/risk-coef model with SL/TP, "
                  "21-day max age, 4-hourly interest — not futures.",
        )

    @property
    def quote_currencies(self) -> List[str]:
        return ["USDT", "TMN"]

    @property
    def true_res_map(self) -> Dict[str, str]:
        # Verified 2026-09-04 (skill #23): res=15 returns 1m bars,
        # res=240 returns 1h bars; 60 and 1D are truthful.
        return {"15": "1", "60": "60", "240": "60", "1D": "1D"}

    # ── symbol helpers ───────────────────────────────────────────────
    def quote_of(self, symbol: str) -> str:
        # Catalog quoting knows the Wallex families exactly (USDT/TMN).
        return MarketCatalog.quote_of(symbol)
