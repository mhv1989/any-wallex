"""GenericRESTAdapter + profile schema (Phase 2) — offline fixture tests.

No network: every test feeds recorded-style payloads through parsers or
monkeypatches the transport.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot.exchange.generic import (ExchangeNotSupported, GenericRESTAdapter,
                                  _parse_candles_arrays, _parse_candles_objects,
                                  _parse_candles_udf)
from bot.exchange.profile import (default_profile, validate_profile,
                                  add_limitation)

# ── candle parsers ───────────────────────────────────────────────────

def test_parse_udf():
    data = {"s": "ok", "t": [100, 200], "o": [1.0, 2.0], "h": [1.5, 2.5],
            "l": [0.5, 1.5], "c": [1.2, 2.2], "v": [10, 20]}
    rows = _parse_candles_udf(data)
    assert rows[0] == {"ts": 100, "open": 1.0, "high": 1.5, "low": 0.5,
                       "close": 1.2, "volume": 10.0}
    assert _parse_candles_udf({"s": "error"}) == []


def test_parse_arrays_bare_rows():
    rows = _parse_candles_arrays([[100, 1, 2, 0.5, 1.2, "10"], [200, 2, 3, 1.5, 2.2, "20"]])
    assert rows[1]["ts"] == 200 and rows[1]["volume"] == 20.0


def test_parse_arrays_columnar():
    data = {"time": [100], "open": [1], "high": [2], "low": [0.5], "close": [1.5], "volume": [7]}
    rows = _parse_candles_arrays(data)
    assert rows[0]["close"] == 1.5


def test_parse_objects_custom_field_map():
    data = [{"time": 100, "open": "1", "high": "2", "low": "0.5", "close": "1.5", "volume": "7"}]
    rows = _parse_candles_objects(data)
    assert rows[0]["ts"] == 100 and rows[0]["open"] == 1.0


def _mk_adapter(overrides: dict = None) -> GenericRESTAdapter:
    p = default_profile("testex", "TestEx")
    p["base_url"] = "https://api.testex.example"
    p["endpoints"] = {
        "candles": {
            "method": "GET", "path": "/v1/klines",
            "params": {"symbol": "{symbol}", "interval": "{tf}",
                       "startTime": "{from_ms}", "endTime": "{to_ms}"},
            "result_format": "arrays",
        },
        "markets": {"method": "GET", "path": "/v1/exchangeInfo",
                    "result_format": "objects"},
    }
    p.update(overrides or {})
    return GenericRESTAdapter(p, api_key="TESTKEY", api_secret="SECRET")


def test_symbol_transform_separator():
    a = _mk_adapter({"symbol_format": {"separator": "_", "case": "upper",
                                       "quote_suffixes": ["USDT", "BTC"]}})
    assert a.to_exchange_symbol("BTCUSDT") == "BTC_USDT"
    assert a.to_exchange_symbol("ETHBTC") == "ETH_BTC"
    assert a.normalize_symbol("btc_usdt") == "BTCUSDT"
    # quote_of over multi families
    assert a.quote_of("BTCUSDT") == "USDT"
    assert a.quote_of("ETHBTC") == "BTC"


def test_candles_request_template_and_parse():
    a = _mk_adapter()
    captured = {}

    def fake_request(ep, path_params=None, extra_params=None, json_body=None, auth=False, return_on_http_error=False, tmpl_context=None):
        captured.update(extra_params or {})
        return [[1700000000000, "1", "2", "0.5", "1.5", "10"]]

    a._request = fake_request
    out = a.get_candles("BTCUSDT", "60", 1700000000, 1700003600)
    assert captured["symbol"] == "BTCUSDT"
    assert captured["interval"] == "60"
    assert captured["startTime"] == "1700000000000"  # ms template
    assert out[0].ts == 1700000000 and abs(out[0].c - 1.5) < 1e-9
    # 1D param mapping
    a._request = lambda *e, **k: []
    a.get_candles("BTCUSDT", "1D", 0, 1)
    # tf_param_map default identity checked via captured above


def test_market_data_and_capabilities():
    a = _mk_adapter()
    a._request = lambda ep, **k: [
        {"symbol": "BTC_USDT", "base": "BTC", "price": "42000", "is_spot": True},
        {"symbol": "ETH_BTC", "base": "ETH", "price": "0.05", "is_spot": True},
    ]
    ms = a.get_markets()
    assert ms[0]["symbol"] == "BTCUSDT"          # normalized to canonical
    caps = a.capabilities
    assert caps.spot is True and caps.margin is False  # no margin block
    assert caps.udf_native is False              # arrays format
    assert a.get_ticker("BTCUSDT")["price"] == 42000.0  # derived from markets


def test_margin_capability_from_profile():
    p = default_profile("mex", "Mex")
    p["base_url"] = "https://api.mex.example"
    p["endpoints"] = {"candles": {"method": "GET", "path": "/k",
                                  "params": {}, "result_format": "udf"},
                      "markets": {"method": "GET", "path": "/m"}}
    p["margin"] = {"style": "futures",
                   "endpoints": {"margin_markets": {"method": "GET", "path": "/fapi/m"}}}
    a = GenericRESTAdapter(p)
    assert a.capabilities.margin is True
    assert a.capabilities.margin_style == "futures"
    assert a.capabilities.udf_native is True
    # configured margin endpoint reachable, unconfigured one raises explicitly
    assert a._margin_ep("margin_markets")["path"] == "/fapi/m"
    try:
        a._margin_ep("margin_open")
        assert False
    except ExchangeNotSupported:
        pass


def test_missing_endpoint_raises_not_supported():
    a = _mk_adapter()
    try:
        a.get_balances()
        assert False
    except ExchangeNotSupported:
        pass


def test_hmac_signature_headers():
    a = _mk_adapter({"auth": {"scheme": "hmac", "hmac": {
        "param_order": ["api_key", "expires"],
        "headers": {"API-KEY": "{key}", "SIGN": "{sig}", "TS": "{expires}"},
    }}})
    headers = {}
    a._sign("GET", "https://api.testex.example/v1/x", {"a": "b"}, None, headers)
    assert headers["API-KEY"] == "TESTKEY"
    assert len(headers["SIGN"]) == 64  # sha256 hex
    assert headers["TS"].isdigit()
    # hmac with no secret must fail loudly
    p2 = default_profile("nh", "NH")
    p2["base_url"] = "https://api.testex.example"
    p2["auth"] = {"scheme": "hmac", "hmac": {"headers": {"S": "{sig}"}}}
    a2 = GenericRESTAdapter(p2, api_key="K", api_secret="")
    try:
        a2._sign("GET", "https://x/v1", {}, None, {})
        assert False
    except ExchangeNotSupported:
        pass


def test_header_auth_passphrase():
    a = _mk_adapter({"auth": {"scheme": "header", "header_name": "X-API",
                              "passphrase_header": "X-PH"}})
    h = {}
    a._sign("GET", "https://x/v1", {}, None, h)
    assert h["X-API"] == "TESTKEY" and h["X-PH"] == ""


# ── profile validator ────────────────────────────────────────────────

def test_validate_profile_errors_and_warnings():
    p = default_profile("x", "X")
    errs, _ = validate_profile(p)
    assert any("base_url" in e for e in errs)
    assert any("candles" in e for e in errs)
    p2 = default_profile("y", "Y")
    p2["base_url"] = "https://api.y.example"
    p2["endpoints"] = {"candles": {"method": "GET", "path": "/k"},
                       "markets": {"method": "GET", "path": "/m"}}
    errs2, _ = validate_profile(p2)
    assert errs2 == []
    # AI-written profile missing a true_res_map entry → warning, not error
    del p2["true_res_map"]["1D"]
    errs3, warns3 = validate_profile(p2)
    assert errs3 == [] and any("true_res_map" in w for w in warns3)


def test_add_limitation_dedupes():
    p = default_profile("l", "L")
    add_limitation(p, "margin_model", "cross margin not expressible")
    add_limitation(p, "margin_model", "cross margin not expressible")
    assert len(p["limitations"]) == 1
