"""Wallex REST client.

- Base URL: https://api.wallex.ir
- Auth: header `x-api-key` (private endpoints only)
- Rate limit: minimum gap between requests (default 12 s) enforced client-side
- Retry: up to 3 retries with 30 s pause on 429/5xx/network errors
- All requests/responses logged (latency, retries, errors) — keys NEVER logged.

Verified endpoints (live-tested 2026-08-21):
  GET  /hector/web/v1/markets            -> all markets (is_spot, precisions, volumes)
  GET  /v1/udf/history?symbol&resolution&from&to -> OHLCV candles {s,t,o,h,l,c,v}
  GET  /v1/depth?symbol                  -> order book {result:{ask,bid}}
  GET  /v1/trades?symbol                 -> latest trades
  GET  /v1/account/balances              -> wallets (x-api-key)
  GET  /v1/account/fee                   -> maker/taker fees per market (x-api-key)
  POST /v1/account/orders                -> place order (LIMIT/MARKET/STOP_LIMIT/STOP_MARKET)
  GET  /v1/account/openOrders            -> active orders
  GET  /v1/account/orders                -> order history
  GET  /v1/account/orders/{client_id}    -> single order detail
  DELETE /v1/account/orders/{client_id}  -> cancel order
"""
from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import httpx

from .models import Candle

log = logging.getLogger("wallex.client")

BASE_URL = "https://api.wallex.ir"


@dataclass
class ApiLogEntry:
    ts: float
    method: str
    path: str
    status: Optional[int]
    latency_ms: float
    retries: int
    error: str = ""


class WallexClient:
    def __init__(
        self,
        api_key: str = "",
        min_gap_sec: float = 12.0,
        max_retries: int = 3,
        retry_pause_sec: float = 30.0,
        timeout_sec: float = 30.0,
        redact_fn=None,
    ):
        self.api_key = api_key
        self.min_gap_sec = min_gap_sec
        self.max_retries = max_retries
        self.retry_pause_sec = retry_pause_sec
        self.timeout_sec = timeout_sec
        self._redact = redact_fn or (lambda s: s)
        self._lock = threading.Lock()
        self._last_request_ts = 0.0
        self.api_log: List[ApiLogEntry] = []
        self._http = httpx.Client(timeout=timeout_sec, headers={"User-Agent": "WallexBot/0.1"})

    # ── rate limiting ──────────────────────────────────────────────
    def _throttle(self) -> None:
        # FIX(freeze): sleep OUTSIDE the lock — the old code held the lock
        # during the 12s sleep, so a boot burst (preload 32 paced calls)
        # serialized EVERY client call behind it for minutes and froze the
        # whole dashboard (lock convoy).
        while True:
            with self._lock:
                wait = self.min_gap_sec - (time.time() - self._last_request_ts)
                if wait <= 0:
                    self._last_request_ts = time.time()
                    return
            time.sleep(min(wait, 1.0))

    # ── core request with retry ────────────────────────────────────
    def _request(
        self,
        method: str,
        path: str,
        params: Optional[dict] = None,
        json_body: Optional[dict] = None,
        auth: bool = False,
    ) -> dict:
        headers = {"Content-Type": "application/json"}
        if auth:
            if not self.api_key:
                raise RuntimeError("API key not configured (WALLEX_API_KEY).")
            headers["x-api-key"] = self.api_key

        url = BASE_URL + path
        last_err = ""
        for attempt in range(self.max_retries + 1):
            self._throttle()
            t0 = time.time()
            status = None
            try:
                resp = self._http.request(method, url, params=params, json=json_body, headers=headers)
                status = resp.status_code
                latency = (time.time() - t0) * 1000
                if status == 429 or status >= 500:
                    last_err = f"HTTP {status}"
                    self._log(method, path, status, latency, attempt, last_err)
                    if attempt < self.max_retries:
                        log.warning("retry %d/%d after %s: %s", attempt + 1, self.max_retries, last_err, path)
                        # FIX(#5): honor the configured retry pause (with
                        # exponential backoff) instead of clamping to 2s —
                        # hammering a rate-limited/outaged API risks an IP ban.
                        time.sleep(self.retry_pause_sec * (2 ** attempt))
                        continue
                    raise RuntimeError(f"Wallex API failed after retries: {last_err}")
                data = resp.json()
                self._log(method, path, status, latency, attempt)
                if status >= 400:
                    raise RuntimeError(self._redact(f"Wallex API error {status}: {resp.text[:300]}"))
                return data
            except (httpx.TransportError, json.JSONDecodeError) as e:
                latency = (time.time() - t0) * 1000
                last_err = self._redact(str(e))
                self._log(method, path, status, latency, attempt, last_err)
                if attempt < self.max_retries:
                    log.warning("retry %d/%d after %s: %s", attempt + 1, self.max_retries, last_err, path)
                    # FIX(#5): same backoff here — transport errors deserve the
                    # configured pause too.
                    time.sleep(self.retry_pause_sec * (2 ** attempt))
                    continue
                raise RuntimeError(f"Wallex API network failure: {last_err}") from e
        raise RuntimeError(f"unreachable: {last_err}")

    def _log(self, method, path, status, latency_ms, retries, error=""):
        self.api_log.append(ApiLogEntry(time.time(), method, path, status, round(latency_ms, 1), retries, error))
        if len(self.api_log) > 2000:
            self.api_log = self.api_log[-1000:]

    # ── public endpoints ───────────────────────────────────────────
    def get_markets(self) -> List[dict]:
        data = self._request("GET", "/hector/web/v1/markets")
        return data.get("result", {}).get("markets", [])

    def get_ticker(self, symbol: str) -> dict:
        """Return ticker info for one symbol (last price etc)."""
        for m in self.get_markets():
            if m.get("symbol") == symbol:
                return m.get("ticker", {}) or m
        return {}

    def get_candles(self, symbol: str, resolution: str, from_ts: int, to_ts: int) -> List[Candle]:
        data = self._request(
            "GET", "/v1/udf/history",
            params={"symbol": symbol, "resolution": resolution, "from": from_ts, "to": to_ts},
        )
        if data.get("s") != "ok":
            return []
        t, o, h, l, c, v = data["t"], data["o"], data["h"], data["l"], data["c"], data["v"]
        return [
            Candle(ts=int(t[i]), o=float(o[i]), h=float(h[i]), l=float(l[i]), c=float(c[i]), v=float(v[i]))
            for i in range(len(t))
        ]

    def get_depth(self, symbol: str) -> dict:
        return self._request("GET", "/v1/depth", params={"symbol": symbol}).get("result", {})

    def get_latest_trades(self, symbol: str) -> list:
        return self._request("GET", "/v1/trades", params={"symbol": symbol}).get("result", {}).get("latestTrades", [])

    # ── private endpoints ──────────────────────────────────────────
    def get_balances(self) -> dict:
        return self._request("GET", "/v1/account/balances", auth=True).get("result", {}).get("balances", {})

    def get_fees(self) -> dict:
        return self._request("GET", "/v1/account/fee", auth=True).get("result", {})

    def place_order(
        self,
        symbol: str,
        side: str,
        order_type: str,
        quantity: str,
        price: Optional[str] = None,
        stop_price: Optional[str] = None,
        client_id: Optional[str] = None,
    ) -> dict:
        body: Dict[str, Any] = {"symbol": symbol, "side": side, "type": order_type, "quantity": quantity}
        if price is not None:
            body["price"] = price
        if stop_price is not None:
            body["stop_Price"] = stop_price
        if client_id:
            body["client_id"] = client_id
        return self._request("POST", "/v1/account/orders", json_body=body, auth=True).get("result", {})

    def get_open_orders(self, symbol: Optional[str] = None) -> dict:
        params = {"per_page": 100}
        if symbol:
            params["symbol"] = symbol
        return self._request("GET", "/v1/account/openOrders", params=params, auth=True).get("result", {})

    def get_order(self, client_id: str) -> dict:
        return self._request("GET", f"/v1/account/orders/{client_id}", auth=True).get("result", {})

    def cancel_order(self, client_id: str) -> dict:
        return self._request("DELETE", f"/v1/account/orders/{client_id}", auth=True).get("result", {})

    def get_order_history(self, market: Optional[str] = None, page: int = 1) -> dict:
        params = {"page": page, "per_page": 100}
        if market:
            params["market"] = market
        return self._request("GET", "/v1/account/orders", params=params, auth=True).get("result", {})

    # ── margin-trade endpoints (Swagger: Wallex Margin-trade API v1.0) ──
    # Auth: x-api-key header (same key as spot)

    def margin_get_markets(self) -> List[dict]:
        """GET /margin-trade/v1/public/markets — active margin markets."""
        data = self._request("GET", "/margin-trade/v1/public/markets")
        return data.get("result", [])

    def margin_get_ratio(self, market: str) -> dict:
        """GET /margin-trade/v1/public/ratio/{market} — long/short ratio."""
        return self._request("GET", f"/margin-trade/v1/public/ratio/{market}").get("result", {})

    def margin_calculate_loan(self, market: str, side: str, collateral: str, risk_coef: str, open_price: str = "") -> dict:
        """POST /margin-trade/v1/public/loan — min/max collateral and loan.

        Wallex rejects open_price="0" (requires a real market price within ±5%),
        so when open_price is empty/zero we auto-fetch the live ticker price.
        """
        if not open_price or open_price == "0":
            open_price = str(self.get_ticker(market).get("price") or "")
        body = {"market": market, "side": side, "collateral": collateral, "risk_coef": risk_coef}
        if open_price:
            body["open_price"] = open_price
        return self._request("POST", "/margin-trade/v1/public/loan", json_body=body).get("result", {})

    def margin_dry_run(self, market: str, side: str, collateral: str, risk_coef: str,
                       open_price: str = "", stop_loss: str = "", take_profit: str = "") -> dict:
        """POST /margin-trade/v1/public/dry-run — preview position before creation.

        Wallex REQUIRES open_price within the current valid range (±5% of market),
        so callers should fetch the live ticker price first.
        """
        body: Dict[str, Any] = {"market": market, "side": side, "collateral": collateral,
                                "risk_coef": risk_coef}
        if open_price:
            body["open_price"] = open_price
        if stop_loss:
            body["stop_loss"] = stop_loss
        if take_profit:
            body["take_profit"] = take_profit
        return self._request("POST", "/margin-trade/v1/public/dry-run", json_body=body).get("result", {})

    def margin_get_positions(self, active: Optional[bool] = None, market: Optional[str] = None,
                             position_side: Optional[str] = None) -> List[dict]:
        """GET /margin-trade/v1/positions — list margin positions."""
        params: Dict[str, Any] = {}
        if active is not None:
            params["active"] = str(active).lower()
        if market:
            params["market"] = market
        if position_side:
            params["position_side"] = position_side
        return self._request("GET", "/margin-trade/v1/positions", params=params, auth=True).get("result", [])

    def margin_get_position(self, position_id: str) -> dict:
        """GET /margin-trade/v1/positions/{id} — single position."""
        return self._request("GET", f"/margin-trade/v1/positions/{position_id}", auth=True).get("result", {})

    def margin_open_position(self, market: str, side: str, collateral: str, risk_coef: str,
                             open_price: str = "0", stop_loss: str = "", take_profit: str = "") -> dict:
        """POST /margin-trade/v1/positions — open a margin position."""
        body: Dict[str, Any] = {"market": market, "side": side, "collateral": collateral,
                                "risk_coef": risk_coef, "open_price": open_price}
        if stop_loss:
            body["stop_loss"] = stop_loss
        if take_profit:
            body["take_profit"] = take_profit
        return self._request("POST", "/margin-trade/v1/positions", json_body=body, auth=True).get("result", {})

    def margin_close_position(self, position_id: str, price: str = "0") -> dict:
        """PATCH /margin-trade/v1/positions/{id}/close — close position."""
        return self._request("PATCH", f"/margin-trade/v1/positions/{position_id}/close",
                             json_body={"price": price}, auth=True).get("result", {})

    def margin_update_sltp(self, position_id: str, stop_loss: str = "", take_profit: str = "") -> dict:
        """PATCH /margin-trade/v1/positions/{id}/sltp — update SL/TP."""
        body: Dict[str, str] = {}
        if stop_loss:
            body["stop_loss"] = stop_loss
        if take_profit:
            body["take_profit"] = take_profit
        return self._request("PATCH", f"/margin-trade/v1/positions/{position_id}/sltp",
                             json_body=body, auth=True).get("result", {})

    def margin_add_collateral(self, position_id: str, change: str) -> dict:
        """PATCH /margin-trade/v1/positions/{id}/collateral — add/remove collateral."""
        return self._request("PATCH", f"/margin-trade/v1/positions/{position_id}/collateral",
                             json_body={"change": change}, auth=True).get("result", {})

    def margin_calculate_profit(self, position_id: str, close_price: str, strategy: str = "") -> dict:
        """POST /margin-trade/v1/positions/{id}/profit — calculate profit at price."""
        body: Dict[str, str] = {"close_price": close_price}
        if strategy:
            body["strategy"] = strategy
        return self._request("POST", f"/margin-trade/v1/positions/{position_id}/profit",
                             json_body=body, auth=True).get("result", {})

    def margin_get_orders(self, active: Optional[bool] = None, market: Optional[str] = None,
                          side: Optional[str] = None) -> List[dict]:
        """GET /margin-trade/v1/orders — list margin orders."""
        params: Dict[str, Any] = {}
        if active is not None:
            params["active"] = str(active).lower()
        if market:
            params["market"] = market
        if side:
            params["side"] = side
        return self._request("GET", "/margin-trade/v1/orders", params=params, auth=True).get("result", [])

    def margin_get_user_levels(self, market: Optional[str] = None) -> dict:
        """GET /margin-trade/v1/user/levels[/{market}] — user margin levels/limits."""
        path = f"/margin-trade/v1/user/levels/{market}" if market else "/margin-trade/v1/user/levels"
        return self._request("GET", path, auth=True).get("result", {})

    def margin_get_pnl(self) -> dict:
        """GET /margin-trade/v1/user/pnl — user margin PNL summary."""
        return self._request("GET", "/margin-trade/v1/user/pnl", auth=True).get("result", {})
