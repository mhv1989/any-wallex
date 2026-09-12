"""ExchangeAdapter layer (Phase 1) — metadata, quote_of, capability gates."""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot.exchange.base import Capabilities, ExchangeAdapter
from bot.exchange.factory import build_adapter
from bot.exchange.wallex import WallexAdapter


class _MultiQuoteAdapter(ExchangeAdapter):
    """Fake adapter with several quote families (the ETHBTC/BTCUSDC case)."""
    id = "multi"
    display_name = "MultiQuote"

    @property
    def quote_currencies(self):
        return ["USDT", "USDC", "BTC", "ETH"]


class _SpotOnlyAdapter(ExchangeAdapter):
    id = "spotonly"

    @property
    def capabilities(self):
        return Capabilities(spot=True, margin=False)


def test_wallex_adapter_subclasses_client():
    a = build_adapter("wallex", "k", min_gap_sec=0.05)
    assert isinstance(a, WallexAdapter)
    from bot.wallex_client import WallexClient
    assert isinstance(a, WallexClient)  # full trading surface intact
    assert a.min_gap_sec == 0.05


def test_wallex_metadata():
    a = build_adapter("wallex", "")
    assert a.quote_currencies == ["USDT", "TMN"]
    assert a.capabilities.margin is True
    assert a.capabilities.margin_style == "isolated_margin"
    assert a.capabilities.udf_native is True
    assert a.true_res_map["15"] == "1" and a.true_res_map["240"] == "60"
    info = a.exchange_info()
    assert info["id"] == "wallex" and info["capabilities"]["margin"] is True


def test_quote_of_multi_quote_exchange():
    a = _MultiQuoteAdapter()
    assert a.quote_of("BTCUSDT") == "USDT"
    assert a.quote_of("ETHBTC") == "BTC"
    assert a.quote_of("BNBUSDC") == "USDC"
    assert a.quote_of("SOLBTC") == "BTC"
    # longest-suffix wins (USDT vs hypothetical single-letter)
    assert a.quote_of("BTCETH") == "ETH"


def test_base_adapter_default_identity_maps():
    a = _SpotOnlyAdapter()
    assert a.quote_of("BTCUSDT") == "USDT"  # base default families
    assert a.true_res_map["60"] == "60"
    assert a.tf_param_map["1D"] == "1D"
    assert a.normalize_symbol("btcusdt") == "BTCUSDT"
    assert a.capabilities.margin is False


def test_factory_unknown_profile_raises():
    # NOTE: 'nobitex' now EXISTS (the user's real wizard run created it) —
    # use a guaranteed-never profile id.
    try:
        build_adapter("no-such-profile-xyz")
        assert False, "should have raised"
    except NotImplementedError:
        pass


def test_wallex_quote_of_matches_catalog():
    a = build_adapter("wallex", "")
    assert a.quote_of("BTCUSDT") == "USDT"
    assert a.quote_of("BTCTMN") == "TMN"
    assert a.quote_of("BTCBOGUS") == ""
