"""ExchangeAdapter — the exchange-agnostic client contract.

Everything above the adapter (engine, brokers, server, history) already
duck-types `self.client.*`. The adapter IS that client: it carries the
exchange metadata (capabilities, quote families, symbol format, granularity
quirk map) AND implements/forwards the trading API.

Phase 1 ships the contract + the Wallex implementation. Phase 2 adds
`GenericRESTAdapter`, which executes a per-exchange profile.json so any
catalog exchange can be added without code changes.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional

# Internal canonical timeframes (engine/history contract)
INTERNAL_TFS = ("15", "60", "240", "1D")


@dataclass
class Capabilities:
    """What this exchange actually supports — drives UI gating + live-mode rules."""
    spot: bool = True
    margin: bool = False          # Wallex-style isolated margin OR futures-style margin
    margin_style: str = ""        # "" | "isolated_margin" (wallex) | "futures" | "cross_margin"
    futures: bool = False
    udf_native: bool = False      # has a TradingView UDF-compatible candle endpoint
    notes: str = ""               # bilingual note surfaced in the wizard/report


class ExchangeAdapter:
    """Base class. Concrete adapters either wrap a hand-coded client
    (WallexAdapter) or execute a profile (GenericRESTAdapter, Phase 2)."""

    id: str = "unknown"
    display_name: str = "Unknown Exchange"

    # ── metadata ────────────────────────────────────────────────────
    @property
    def capabilities(self) -> Capabilities:
        return Capabilities()

    @property
    def live_margin_supported(self) -> bool:
        """Can this adapter EXECUTE live margin orders (vs only simulate paper
        margin)? True for hand-coded adapters that implement margin; a
        profile-driven adapter must have a margin block with a `margin_open`
        endpoint. Lets paper margin (exchange HAS a margin product) be offered
        even when live margin endpoints aren't wired."""
        return self.capabilities.margin

    @property
    def quote_currencies(self) -> List[str]:
        """Quote families this exchange offers (drives paper quote + UI).
        Base default: the USDT family only."""
        return ["USDT"]

    @property
    def true_res_map(self) -> Dict[str, str]:
        """requested internal TF -> granularity the exchange ACTUALLY returns.
        Identity by default (honest exchange). Adapters with known quirks
        (e.g. Wallex res=15 -> 1m bars) override this; the generic
        finer-than-requested -> aggregate logic in engine/history stays the
        real safety net — this map is documentation + probe verification."""
        return {tf: tf for tf in INTERNAL_TFS}

    @property
    def tf_param_map(self) -> Dict[str, str]:
        """internal TF -> exchange API parameter value (identity default).
        e.g. an exchange whose candles endpoint wants "1d" for 1D."""
        return {tf: tf for tf in INTERNAL_TFS}

    # ── symbol canonicalization ──────────────────────────────────────
    def quote_of(self, symbol: str) -> str:
        """Quote currency of a canonical symbol by suffix matching against
        quote_currencies (longest suffix wins so BTCUSDC beats C-like noise).
        Works for multi-quote exchanges: ETHBTC -> BTC when BTC is listed."""
        s = (symbol or "").upper().strip()
        for q in sorted(self.quote_currencies, key=len, reverse=True):
            if s.endswith(q.upper()) and len(s) > len(q):
                return q.upper()
        return ""

    def normalize_symbol(self, exchange_symbol: str) -> str:
        """exchange-native symbol -> internal canonical (identity default)."""
        return (exchange_symbol or "").upper().strip()

    def to_exchange_symbol(self, canonical: str) -> str:
        """internal canonical -> exchange-native symbol (identity default)."""
        return (canonical or "").upper().strip()

    # ── surface for status / wizard / UI gating ──────────────────────
    def exchange_info(self) -> dict:
        cap = self.capabilities
        return {
            "id": self.id,
            "name": self.display_name,
            "capabilities": {
                "spot": cap.spot,
                "margin": cap.margin,
                "margin_style": cap.margin_style,
                "futures": cap.futures,
                "udf_native": cap.udf_native,
                "notes": cap.notes,
            },
            "quote_families": list(self.quote_currencies),
            "true_res_map": dict(self.true_res_map),
            "tf_param_map": dict(self.tf_param_map),
        }
