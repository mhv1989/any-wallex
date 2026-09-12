"""Wallex market catalog — real available pairs fetched from the exchange.

Wallex exposes ~485 spot markets in two quote families:
  * USDT pairs (e.g. BTCTMN-style symbol "BTCUSDT")   -> is_usdt_based
  * TMN  pairs (e.g. "BTCTMN", quote = Iranian Toman) -> is_tmn_based

The catalog fetches the live list from GET /hector/web/v1/markets, caches it on
disk (TTL), and answers queries without hitting the API again. The engine's
symbol universe is then built from this catalog instead of the hardcoded
config.yaml list (which only had 8 USDT pairs).
"""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Dict, List, Optional

log = logging.getLogger("wallex.markets")

TMN = "TMN"
USDT = "USDT"
# Iranian-Toman alias family (2026-09-07): Nobitex (and other Iranian CEXs)
# write the Toman quote as IRT or RLS in the SYMBOL while the app presents it
# as TMN. quote_of() must treat all three as the TMN quote family.
TOMAN_SUFFIXES = ("TMN", "IRT", "RLS")
DEFAULT_TTL_SEC = 6 * 3600  # refresh markets every 6h


class MarketCatalog:
    """Fetch + cache + filter Wallex markets (both TMN and USDT quotes)."""

    def __init__(self, client=None, cache_path: Optional[Path] = None,
                 ttl_sec: int = DEFAULT_TTL_SEC):
        self.client = client
        # Phase 6: default cache INSIDE the project data dir — a bare
        # MarketCatalog() must never write to the user's home dir (skill #45).
        self.cache_path = Path(cache_path) if cache_path else (
            Path(__file__).resolve().parent.parent / "data" / "markets_cache.json")
        self.ttl_sec = ttl_sec
        self._cache: Optional[List[dict]] = None
        self.last_error: str = ""   # last live-fetch failure (for UI warnings)

    # ── loading ──────────────────────────────────────────────────────
    def _fetch_live(self) -> List[dict]:
        if self.client is None:
            raise RuntimeError("MarketCatalog has no client to fetch markets")
        markets = self.client.get_markets()
        if not markets:
            raise RuntimeError("Wallex returned empty market list")
        return markets

    def _load_cache(self) -> Optional[List[dict]]:
        try:
            raw = json.loads(self.cache_path.read_text(encoding="utf-8"))
            if time.time() - float(raw.get("fetched_at", 0)) < self.ttl_sec and raw.get("markets"):
                return raw["markets"]
        except Exception:
            pass
        return None

    def _save_cache(self, markets: List[dict]) -> None:
        try:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            self.cache_path.write_text(
                json.dumps({"fetched_at": time.time(), "markets": markets},
                           ensure_ascii=False),
                encoding="utf-8")
        except OSError as e:
            log.warning("market cache save failed: %s", e)

    def refresh(self, force: bool = False) -> List[dict]:
        """Return full market list; refetch when cache is stale or missing.
        FIX(net-degrade): when the live fetch fails (network/region block),
        fall back to the EXPIRED disk cache instead of raising — a symbol
        list that is hours old is far better than a 500 on /api/settings.
        The failure is recorded in .last_error so endpoints can warn."""
        if not force:
            cached = self._load_cache()
            if cached:
                self._cache = cached
                self.last_error = ""
                return cached
        # dead-zone: after a failed live fetch, don't hammer the (blocked)
        # API on every endpoint call for 60s — serve stale immediately.
        if not force and time.time() - getattr(self, "_dead_until", 0.0) < 60 \
                and getattr(self, "last_error", ""):
            stale = self._load_cache_any_age()
            if stale:
                self._cache = stale
                return stale
        try:
            markets = self._fetch_live()
        except Exception as e:
            self._dead_until = time.time()
            # stale disk fallback (any age) before giving up
            stale = self._load_cache_any_age()
            self.last_error = str(e)
            if stale:
                log.warning("markets fetch failed (%s) — serving stale disk cache "
                            "(%d markets)", str(e)[:120], len(stale))
                self._cache = stale
                return stale
            raise
        self.last_error = ""
        self._cache = markets
        self._save_cache(markets)
        log.info("market catalog refreshed: %d markets", len(markets))
        return markets

    def _load_cache_any_age(self) -> Optional[List[dict]]:
        """Disk cache regardless of TTL — the network-degraded fallback."""
        try:
            raw = json.loads(self.cache_path.read_text(encoding="utf-8"))
            return raw.get("markets") or None
        except Exception:
            return None

    def all(self, force: bool = False) -> List[dict]:
        return self.refresh(force=force)

    # ── filters ──────────────────────────────────────────────────────
    @staticmethod
    def quote_of(symbol: str) -> str:
        s = (symbol or "").upper()
        # Toman family: TMN / IRT / RLS suffixes all mean the Toman quote
        # (Nobitex writes RLS/IRT; the app presents TMN) — check BEFORE USDT
        # since no Toman suffix is a prefix of USDT, order is still safe.
        for suf in TOMAN_SUFFIXES:
            if s.endswith(suf) and len(s) > len(suf):
                return TMN
        if s.endswith(USDT):
            return USDT
        return ""

    def symbols(self, quote: str = "", spot_only: bool = True,
                margin_only: bool = False, force: bool = False) -> List[str]:
        """Sorted symbol list. quote='TMN'/'USDT'/'' (both)."""
        q = (quote or "").upper()
        out = []
        for m in self.refresh(force=force):
            sym = str(m.get("symbol") or "").upper()
            if not sym:
                continue
            if spot_only and not m.get("is_spot", True):
                continue
            if margin_only and not m.get("is_margin", False):
                continue
            mq = self.quote_of(sym)
            if q and mq != q:
                continue
            out.append((sym, str(m.get("fa_base_asset") or m.get("base_asset") or sym)))
        out.sort(key=lambda x: x[0])
        return [s for s, _ in out]

    def entries(self, quote: str = "") -> Dict[str, dict]:
        """symbol -> {quote, fa_name, en_name, is_margin, price} for UI pickers."""
        q = (quote or "").upper()
        out: Dict[str, dict] = {}
        for m in self.refresh():
            sym = str(m.get("symbol") or "").upper()
            if not sym:
                continue
            mq = self.quote_of(sym)
            if q and mq != q:
                continue
            out[sym] = {
                "symbol": sym,
                "quote": mq,
                "base": str(m.get("base_asset") or ""),
                "fa_name": str(m.get("fa_base_asset") or ""),
                "en_name": str(m.get("en_base_asset") or ""),
                "is_margin": bool(m.get("is_margin", False)),
                "price_precision": m.get("price_precision"),
                "price": float(m.get("price") or 0),
            }
        return out

    def get(self, symbol: str) -> Optional[dict]:
        return self.entries().get((symbol or "").upper())

    def quote(self, symbol: str) -> str:
        return self.quote_of(symbol)

    def stats(self) -> dict:
        syms = self.symbols()
        return {
            "total": len(syms),
            "tmn": sum(1 for s in syms if self.quote_of(s) == TMN),
            "usdt": sum(1 for s in syms if self.quote_of(s) == USDT),
        }
