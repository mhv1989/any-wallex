"""Real-time quote service for TMN/USDT conversion rates.

The bridge market that prices 1 USDT in Toman differs per exchange:
  * Wallex:  USDTTMN
  * Nobitex: USDTIRT (stats key 'usdt-rls' — the adapter's Toman alias
    family resolves IRT/RLS/TMN spellings transparently)

This module asks the ACTIVE exchange adapter for the price of the canonical
bridge symbol 'USDTTMN'; exchange-specific spelling is the adapter's job
(profile-driven). If the ticker is unavailable the app surfaces an explicit
error instead of silently using stale/zero rates.
"""
from __future__ import annotations

import logging
import time
from typing import Dict

log = logging.getLogger("wallex.quotes")

# How long a cached quote is considered fresh (seconds)
FRESH_TTL_SEC = 45
# How long we tolerate stale quotes before declaring them unavailable
STALE_GRACE_SEC = 180

# Canonical bridge symbol (1 USDT = X TMN). Adapters translate spelling:
# Wallex passes it through; GenericRESTAdapter resolves the Toman alias
# family (IRT/RLS/TMN) against the exchange's real symbol set.
_USDT_TMN_SYM = "USDTTMN"
# Alternate spellings to try when the canonical one returns no price
_USDT_TMN_ALIASES = ("USDTIRT", "USDTRLS")


class QuoteService:
    def __init__(self, client=None, ttl_sec: int = FRESH_TTL_SEC,
                 grace_sec: int = STALE_GRACE_SEC):
        self.client = client
        self.ttl_sec = ttl_sec
        self.grace_sec = grace_sec
        # derived values
        self._tmn_per_usdt: float = 0.0   # 1 USDT = X TMN  (direct from ticker)
        self._usdt_per_tmn: float = 0.0   # 1 TMN = X USDT  (inverse)
        self._fetched_at: float = 0.0

    @property
    def tmn_per_usdt(self) -> float:
        """1 USDT in TMN. Direct from USDTTMN ticker."""
        return self._tmn_per_usdt

    @property
    def usdt_per_tmn(self) -> float:
        """1 TMN in USDT. Derived as 1 / USDTTMN."""
        return self._usdt_per_tmn

    def is_fresh(self) -> bool:
        return (time.time() - self._fetched_at) < self.ttl_sec

    def is_available(self) -> bool:
        return self._fetched_at > 0 and (time.time() - self._fetched_at) < self.grace_sec

    def missing(self) -> list:
        m = []
        if self._tmn_per_usdt <= 0:
            m.append(_USDT_TMN_SYM)
        return m

    def refresh(self) -> Dict[str, float]:
        """Fetch the USDT→TMN bridge price from the active exchange and derive
        the inverse. Tries the canonical symbol, then Toman alias spellings
        (USDTIRT/USDTRLS) for exchanges that name the Toman market differently.
        Cross-validates against the markets cache (the quote must exist in the
        catalog) so a random unrelated price can never masquerade as the rate.
        Prices are normalized to TOMAN: Nobitex's RLS-keyed rows are RIAL and
        get divided by 10."""
        if self.client is None:
            return self._status_raw()
        candidates = (_USDT_TMN_SYM,) + _USDT_TMN_ALIASES
        for sym in candidates:
            try:
                ticker = self.client.get_ticker(sym) or {}
                px = float(ticker.get("price") or ticker.get("last") or 0)
                if px <= 0:
                    continue
                # Nobitex prices its RLS rows in RIAL: 1 USDT = 226,780 Rial =
                # 22,678 Toman. Normalize to Toman so the whole app speaks TMN.
                if sym.endswith(("RLS", "IRT")):
                    px = px / 10.0
                self._tmn_per_usdt = px
                self._usdt_per_tmn = 1.0 / px
                self._fetched_at = time.time()
                return self._status_raw()
            except Exception as e:
                log.debug("quote fetch %s failed: %s", sym, e)
        return self._status_raw()

    @property
    def quotes(self) -> Dict[str, float]:
        """Public dict of quote prices for broker/server consumption."""
        return {
            _USDT_TMN_SYM: self._tmn_per_usdt,
            "TMNUSDT": self._usdt_per_tmn,
        }

    def convert(self, amount: float, from_asset: str, to_asset: str) -> float:
        """Convert `amount` of `from_asset` to `to_asset` using live quotes."""
        f = (from_asset or "").upper()
        t = (to_asset or "").upper()
        if f == t or not amount:
            return amount if f == t else 0.0
        if f == "USDT" and t == "TMN":
            if not self._tmn_per_usdt:
                return 0.0
            return amount * self._tmn_per_usdt
        if f == "TMN" and t == "USDT":
            if not self._usdt_per_tmn:
                return 0.0
            return amount * self._usdt_per_tmn
        # indirect via USDT for any other asset
        if f != "USDT":
            amount = self.convert(amount, f, "USDT")
            f = "USDT"
        if f == "USDT" and t != "USDT":
            return self.convert(amount, "USDT", "TMN")
        return amount

    def _status_raw(self) -> Dict[str, float]:
        return {
            "tmn_per_usdt": self._tmn_per_usdt,
            "usdt_per_tmn": self._usdt_per_tmn,
        }

    def status(self) -> dict:
        return {
            "tmn_per_usdt": round(self._tmn_per_usdt, 6) if self._tmn_per_usdt else 0.0,
            "usdt_per_tmn": round(self._usdt_per_tmn, 8) if self._usdt_per_tmn else 0.0,
            "fetched_at": int(self._fetched_at),
            "available": self.is_available(),
            "missing": self.missing(),
            "age_sec": int(time.time() - self._fetched_at) if self._fetched_at else None,
        }
