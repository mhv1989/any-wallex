"""Tests for the real-time TMN/USDT quote service (Wallex-only USDTTMN market)."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot.quotes import QuoteService


class FakeClient:
    """Minimal stand-in for WallexClient with a controllable get_ticker."""

    def __init__(self, tickers):
        self._tickers = tickers

    def get_ticker(self, symbol):
        return {"price": self._tickers.get(symbol), "last": self._tickers.get(symbol)}


class TestQuotes:
    def test_missing_client_returns_empty(self):
        q = QuoteService(client=None)
        q.refresh()
        assert q.tmn_per_usdt == 0.0
        assert q.usdt_per_tmn == 0.0
        assert not q.is_available()

    def test_usdt_tmn_fetch_derives_inverse(self):
        q = QuoteService(client=FakeClient({"USDTTMN": 198000.0}))
        q.refresh()
        assert q.tmn_per_usdt == 198000.0
        assert abs(q.usdt_per_tmn - 1 / 198000.0) < 1e-12

    def test_zero_ticker_marks_unavailable(self):
        q = QuoteService(client=FakeClient({"USDTTMN": 0.0}))
        q.refresh()
        assert q.tmn_per_usdt == 0.0
        assert q.missing() == ["USDTTMN"]

    def test_status_presence(self):
        q = QuoteService(client=FakeClient({"USDTTMN": 197275.0}))
        q.refresh()
        st = q.status()
        assert st["tmn_per_usdt"] == 197275.0
        expected = 1 / 197275.0
        assert abs(st["usdt_per_tmn"] - expected) < max(1e-6, abs(expected) * 1e-3)

    def test_convert_usdt_to_tmn(self):
        q = QuoteService(client=FakeClient({"USDTTMN": 200000.0}))
        q.refresh()
        assert q.convert(1.0, "USDT", "TMN") == 200000.0
        assert q.convert(0.5, "USDT", "TMN") == 100000.0

    def test_convert_tmn_to_usdt(self):
        q = QuoteService(client=FakeClient({"USDTTMN": 200000.0}))
        q.refresh()
        assert abs(q.convert(200000.0, "TMN", "USDT") - 1.0) < 1e-12
        assert abs(q.convert(100000.0, "TMN", "USDT") - 0.5) < 1e-12

    def test_convert_same_asset(self):
        q = QuoteService(client=FakeClient({"USDTTMN": 200000.0}))
        q.refresh()
        assert q.convert(42.0, "USDT", "USDT") == 42.0
        assert q.convert(42.0, "TMN", "TMN") == 42.0

    def test_convert_zero_amount(self):
        q = QuoteService(client=FakeClient({"USDTTMN": 200000.0}))
        q.refresh()
        assert q.convert(0.0, "USDT", "TMN") == 0.0

    def test_quotes_property_shape(self):
        q = QuoteService(client=FakeClient({"USDTTMN": 123456.0}))
        q.refresh()
        d = q.quotes
        assert d["USDTTMN"] == 123456.0
        assert abs(d["TMNUSDT"] - 1 / 123456.0) < 1e-12

    def test_missing_list_empty_when_fresh(self):
        q = QuoteService(client=FakeClient({"USDTTMN": 1.0}))
        q.refresh()
        assert q.missing() == []
