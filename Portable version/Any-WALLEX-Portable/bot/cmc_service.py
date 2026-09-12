"""CoinMarketCap data service — optional, key-activated market context.

The user supplies a CMC API key in Settings (stored ENCRYPTED in CryptoStore
alongside the exchange/AI keys, never returned to the client — masked GET
only). When active, the dashboard can enrich any chart symbol with live
market context (price stats, market cap, supply, ATH delta, global metrics)
and the AI can use this data for strategy/entry suggestions. If no AI is
configured, the CMC data itself is served as a structured fallback panel.

Free-tier endpoints used (verified 2026-09-06 with a test key):
  /v1/cryptocurrency/quotes/latest   (per-symbol stats)
  /v1/cryptocurrency/info            (logo/urls/description)
  /v1/global-metrics/quotes/latest   (total market cap, BTC dominance…)
  /v1/exchange/quotes/latest         (exchange stats — reserved)

Responses are cached in memory (TTL) AND persisted to data/cmc_cache.json so
the panel still shows solid (stale-flagged) data when the API is unreachable.
"""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any, Dict, Optional

import httpx

log = logging.getLogger("wallex.cmc")

BASE = "https://pro-api.coinmarketcap.com/v1"
TTL_QUOTE = 300        # 5 min per-symbol quotes
TTL_GLOBAL = 600       # 10 min global metrics
TTL_INFO = 24 * 3600   # static coin info
# CryptoStore key name
KEY_NAME = "cmc_api_key"


class CMCError(RuntimeError):
    pass


class CMCService:
    def __init__(self, store=None, data_dir: str = ""):
        self.store = store                 # CryptoStore (encrypted key at rest)
        self.cache_path = Path(data_dir) / "cmc_cache.json" if data_dir else None
        self._mem: Dict[str, tuple] = {}   # cachekey -> (ts, payload)
        self._http = httpx.Client(timeout=20.0, headers={
            "X-CMC_PRO_API_KEY": self.api_key or "",
            "Accept": "application/json",
        })

    # ── key management ───────────────────────────────────────────────
    @property
    def api_key(self) -> str:
        try:
            return (self.store.get(KEY_NAME) or "") if self.store else ""
        except Exception:
            return ""

    @property
    def enabled(self) -> bool:
        return bool(self.api_key)

    def set_key(self, key: str) -> None:
        if not self.store:
            raise CMCError("no encrypted store configured")
        key = (key or "").strip()
        if key:
            self.store.set(KEY_NAME, key)
        else:
            self.store.delete(KEY_NAME)
        self._http.headers["X-CMC_PRO_API_KEY"] = key

    def masked_state(self) -> dict:
        k = self.api_key
        return {
            "enabled": bool(k),
            "has_key": bool(k),
            "preview": (k[:4] + "…" + k[-4:]) if len(k) > 8 else bool(k),
        }

    # ── transport + cache ────────────────────────────────────────────
    def _get(self, path: str, params: Optional[dict] = None,
             ttl: int = 300, persist: bool = True) -> Any:
        if not self.enabled:
            raise CMCError("CoinMarketCap API key not set (Settings → بخش کوین‌مارکت‌کپ)")
        ckey = f"{path}?{sorted((params or {}).items())}"
        ts, data = self._mem.get(ckey, (0, None))
        if data is not None and time.time() - ts < ttl:
            return data
        try:
            r = self._http.get(BASE + path, params=params)
            body = r.json()
            status = body.get("status", {})
            if r.status_code != 200 or status.get("error_code", 0) != 0:
                raise CMCError(f"CMC error {status.get('error_code')}: {status.get('error_message')}")
            data = body.get("data")
            self._mem[ckey] = (time.time(), data)
            if persist:
                self._persist(ckey, data)
            return data
        except CMCError:
            raise
        except Exception as exc:
            # network/API down → serve last persisted (stale-flagged) copy
            stale = self._stale(ckey)
            if stale is not None:
                return stale
            raise CMCError(f"CoinMarketCap unreachable: {exc}") from exc

    def _persist(self, ckey: str, data: Any) -> None:
        if not self.cache_path:
            return
        try:
            blob = json.loads(self.cache_path.read_text(encoding="utf-8")) \
                if self.cache_path.exists() else {}
            blob[ckey] = {"ts": time.time(), "data": data}
            # keep the cache bounded (40 freshest entries)
            if len(blob) > 40:
                blob = dict(sorted(blob.items(), key=lambda kv: -kv[1]["ts"])[:40])
            self.cache_path.write_text(json.dumps(blob)[:2_000_000], encoding="utf-8")
        except Exception as exc:
            log.debug("cmc cache persist failed: %s", exc)

    def _stale(self, ckey: str) -> Any:
        if not self.cache_path or not self.cache_path.exists():
            return None
        try:
            blob = json.loads(self.cache_path.read_text(encoding="utf-8"))
            entry = blob.get(ckey)
            if entry:
                out = dict(entry["data"]) if isinstance(entry["data"], dict) else entry["data"]
                if isinstance(out, dict):
                    out["_stale"] = True
                    out["_cached_ts"] = entry["ts"]
                return out
        except Exception:
            return None
        return None

    # ── public data surface ──────────────────────────────────────────
    def quotes(self, symbol: str, convert: str = "USD") -> dict:
        """Per-symbol stats: price, multi-horizon changes, mcap, volume, FDV."""
        d = self._get("/cryptocurrency/quotes/latest",
                      {"symbol": symbol.upper(), "convert": convert}, ttl=TTL_QUOTE)
        row = (d or {}).get(symbol.upper()) or {}
        q = ((row.get("quote") or {}).get(convert) or {})
        return {
            "symbol": symbol.upper(),
            "name": row.get("name"), "slug": row.get("slug"),
            "rank": row.get("cmc_rank"),
            "price": q.get("price"),
            "pct_1h": q.get("percent_change_1h"),
            "pct_24h": q.get("percent_change_24h"),
            "pct_7d": q.get("percent_change_7d"),
            "pct_30d": q.get("percent_change_30d"),
            "pct_60d": q.get("percent_change_60d"),
            "pct_90d": q.get("percent_change_90d"),
            "volume_change_24h": q.get("volume_change_24h"),
            "market_cap": q.get("market_cap"),
            "fdv": q.get("fully_diluted_market_cap"),
            "volume_24h": q.get("volume_24h"),
            "dominance": q.get("market_cap_dominance"),
            "circulating": row.get("circulating_supply"),
            "total_supply": row.get("total_supply"),
            "last_updated": q.get("last_updated"),
        }

    def info(self, symbol: str) -> dict:
        d = self._get("/cryptocurrency/info", {"symbol": symbol.upper()}, ttl=TTL_INFO)
        row = (d or {}).get(symbol.upper()) or {}
        return {
            "symbol": symbol.upper(), "name": row.get("name"),
            "category": row.get("category"), "logo": row.get("logo"),
            "description": (row.get("description") or "")[:600],
            "website": ((row.get("urls") or {}).get("website") or [None])[0],
            "tags": (row.get("tags") or [])[:12],
        }

    def global_metrics(self, convert: str = "USD") -> dict:
        d = self._get("/global-metrics/quotes/latest", {"convert": convert}, ttl=TTL_GLOBAL)
        q = ((d or {}).get("quote") or {}).get(convert) or {}
        return {
            "total_market_cap": q.get("total_market_cap"),
            "total_volume_24h": q.get("total_volume_24h"),
            # dominance lives at the data top level (verified against live API)
            "btc_dominance": d.get("btc_dominance"),
            "eth_dominance": d.get("eth_dominance"),
            "btc_dominance_yesterday": d.get("btc_dominance_yesterday"),
            "mcap_change_24h": q.get("total_market_cap_yesterday_percentage_change"),
            "active_cryptos": d.get("active_cryptocurrencies"),
            "active_exchanges": d.get("active_exchanges"),
            "altcoin_volume_24h": q.get("altcoin_volume_24h"),
            "defi_volume_24h": q.get("defi_volume_24h"),
            "defi_change_24h": q.get("defi_24h_percentage_change"),
            "stablecoin_volume_24h": q.get("stablecoin_volume_24h"),
            "stablecoin_change_24h": q.get("stablecoin_24h_percentage_change"),
        }

    def exchange_quotes(self, slug: str, convert: str = "USD") -> dict:
        """Exchange-level stats (reserved for the exchange-info panel)."""
        d = self._get("/exchange/quotes/latest", {"slug": slug, "convert": convert}, ttl=600)
        rows = (d or {}).get(slug) or {}
        return {"name": rows.get("name"), "rank": rows.get("rank"),
                "quote": (rows.get("quote") or {}).get(convert, {})}

    # ── AI-context builder (used by /api/cmc/ai-insight) ─────────────
    def context_brief(self, symbol: str) -> str:
        """Rich English context block about one base asset for AI prompts.
        ASCII-formatted numbers (prompt-side); only fields the CMC plan
        actually returns (verified 2026-09-07: no ATH on this tier)."""
        s = (symbol or "").upper()
        base = s
        for suffix in ("USDT", "USDC", "USD", "TMN", "IRT"):
            if base.endswith(suffix) and len(base) > len(suffix):
                base = base[: -len(suffix)]
                break
        try:
            q = self.quotes(base)
        except CMCError:
            q = {}
        g = {}
        try:
            g = self.global_metrics()
        except Exception:
            pass
        if not q or not q.get("price"):
            return f"Asset: {base} — no market data available."

        def _pc(v):
            try:
                return f"{float(v):+.2f}%"
            except (TypeError, ValueError):
                return "n/a"

        def _usd(v):
            try:
                x = float(v)
                return f"${x:,.0f}" if x >= 10 else f"${x:,.6f}"
            except (TypeError, ValueError):
                return "n/a"

        lines = [
            f"ASSET: {q.get('name') or base} ({base}) — CMC rank #{q.get('rank') or 'n/a'}, price {_usd(q.get('price'))}",
            (f"MOMENTUM 1h {_pc(q.get('pct_1h'))} | 24h {_pc(q.get('pct_24h'))} | 7d {_pc(q.get('pct_7d'))} | "
             f"30d {_pc(q.get('pct_30d'))} | 60d {_pc(q.get('pct_60d'))} | 90d {_pc(q.get('pct_90d'))}"),
        ]
        if q.get("volume_24h"):
            vc = _pc(q.get("volume_change_24h"))
            lines.append(f"VOLUME: 24h {_usd(q.get('volume_24h'))} (change vs prior day {vc})")
        if q.get("market_cap"):
            fdv = q.get("fdv")
            fdv_txt = f", fully-diluted {_usd(fdv)}" if fdv else ""
            lines.append(f"MARKET CAP: {_usd(q.get('market_cap'))}{fdv_txt}, BTC-market dominance {_pc(q.get('dominance'))}")
        if g:
            dom_now = g.get("btc_dominance")
            dom_y = g.get("btc_dominance_yesterday")
            dom_txt = _pc(dom_now)
            if dom_now is not None and dom_y:
                try:
                    dom_txt = f"{float(dom_now):.2f}% (yesterday {float(dom_y):.2f}%)"
                except (TypeError, ValueError):
                    pass
            lines.append(
                f"GLOBAL: total market cap {_usd(g.get('total_market_cap'))} "
                f"({_pc(g.get('mcap_change_24h'))} in 24h), BTC dominance {dom_txt}, "
                f"DeFi volume {_pc(g.get('defi_change_24h'))} 24h, stablecoin volume {_pc(g.get('stablecoin_change_24h'))} 24h"
            )
        return "\n".join(lines)


def _p(v) -> str:
    try:
        return f"{float(v):+.2f}"
    except (TypeError, ValueError):
        return "?"
