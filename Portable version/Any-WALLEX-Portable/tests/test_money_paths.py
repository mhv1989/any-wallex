"""Money-path module tests: crypto_store (encrypted vault), storage (SQLite
WAL + prune), manual_orders (limit/stop/TP-SL fill logic). These three touch
real funds or the records that audit them — regression here is unacceptable.
Run with the rest of the suite: python -m pytest tests/ -q
"""
from __future__ import annotations

import os
import sys
import tempfile
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot.crypto_store import CryptoStore  # noqa: E402
from bot.storage import Storage  # noqa: E402
from bot.manual_orders import ManualOrderManager  # noqa: E402
from bot.models import Position, PosState  # noqa: E402


# ════════════════════════════════════════════════════════════════
# crypto_store — round-trip, wrong password, isolation, redaction
# ════════════════════════════════════════════════════════════════

class TestCryptoStore:
    def test_roundtrip_set_get_delete(self, tmp_path):
        cs = CryptoStore(str(tmp_path), "pw-test-123")
        cs.set("exchange_key:wallex", "sk-real-secret-42")
        assert cs.get("exchange_key:wallex") == "sk-real-secret-42"
        cs.delete("exchange_key:wallex")
        assert cs.get("exchange_key:wallex") is None
        # delete of a missing key is a no-op, not an error
        cs.delete("does-not-exist")

    def test_persistence_across_instances(self, tmp_path):
        """Same dir + same password must decrypt the stored payload."""
        cs1 = CryptoStore(str(tmp_path), "same-pw")
        cs1.set("k", "v1")
        cs2 = CryptoStore(str(tmp_path), "same-pw")
        assert cs2.get("k") == "v1"
        cs2.set("k", "v2")  # re-save keeps other keys too
        cs3 = CryptoStore(str(tmp_path), "same-pw")
        assert cs3.get("k") == "v2"

    def test_wrong_password_raises(self, tmp_path):
        cs = CryptoStore(str(tmp_path), "right-pw")
        cs.set("k", "v")
        cs2 = CryptoStore(str(tmp_path), "WRONG-pw")
        with pytest.raises(RuntimeError, match="WALLEX_KEY_PASSWORD"):
            cs2.get("k")

    def test_salt_reuse_rejects_other_password(self, tmp_path):
        """Two stores on the same dir with different passwords must not silently
        succeed — the wrong-password failure mode is a hard error, not empty data."""
        cs = CryptoStore(str(tmp_path), "alpha")
        cs.set("secret", "x")
        with pytest.raises(RuntimeError):
            CryptoStore(str(tmp_path), "beta").get("secret")

    def test_ciphertext_not_plaintext_on_disk(self, tmp_path):
        cs = CryptoStore(str(tmp_path), "pw")
        secret = "PLAINTEXT-CANARY-9f8e7d6c"
        cs.set("k", secret)
        raw = (tmp_path / "secrets.enc").read_bytes()
        assert secret.encode() not in raw
        assert b"PLAINTEXT-CANARY" not in raw

    def test_redact_removes_stored_secret_values(self, tmp_path):
        cs = CryptoStore(str(tmp_path), "pw")
        secret = "sk-live-abcdef1234567890"
        cs.set("exchange_key", secret)
        out = cs.redact(f"calling with key {secret} failed")
        assert secret not in out
        assert "***REDACTED***" in out
        # short values (<6 chars) are not redacted by design
        cs.set("short", "abc")
        assert "abc" in cs.redact("abc here")


# ════════════════════════════════════════════════════════════════
# storage — WAL concurrency, kv, prune, manual_orders table
# ════════════════════════════════════════════════════════════════

@pytest.fixture()
def storage(tmp_path):
    return Storage(str(tmp_path))


class TestStorage:
    def test_kv_roundtrip_and_overwrite(self, storage):
        storage.kv_set("paper_quote_spot", "TMN")
        assert storage.kv_get("paper_quote_spot") == "TMN"
        storage.kv_set("paper_quote_spot", "USDT")
        assert storage.kv_get("paper_quote_spot") == "USDT"
        assert storage.kv_get("never-set") is None

    def test_kv_persists_across_connections(self, tmp_path):
        s1 = Storage(str(tmp_path))
        s1.kv_set("k", "persistent")
        s2 = Storage(str(tmp_path))
        assert s2.kv_get("k") == "persistent"

    def test_kv_delete_wipes_row(self, storage):
        storage.kv_set("wizard_completed", "1")
        with storage._lock, storage._connect() as con:
            con.execute("DELETE FROM kv WHERE key = 'wizard_completed'")
        assert storage.kv_get("wizard_completed") is None

    def test_save_and_read_trade(self, storage):
        pos = Position(id="t1", symbol="BTCUSDT", qty=0.01, entry=100.0,
                       stop=95.0, opened_ts=1, state=PosState.CLOSED.value,
                       close_price=110.0, pnl=0.1, side="long")
        storage.save_trade(pos)
        rows = storage.closed_trades(limit=10)
        assert any(r["id"] == "t1" for r in rows)

    def test_events_ring(self, storage):
        for i in range(5):
            storage.log_event(1000 + i, "factory_reset", "SYM", f"detail {i}")
        rows = storage.events_recent(limit=3)
        assert len(rows) == 3
        assert rows[0]["detail"] == "detail 4"  # newest first

    def test_concurrent_writers_wal(self, tmp_path):
        """SQLite WAL + lock: N threads writing simultaneously must not
        corrupt or lose rows (the paper-reset wipe ran against this table)."""
        s = Storage(str(tmp_path))
        errs: list[Exception] = []

        def worker(idx: int):
            try:
                for i in range(20):
                    s.log_event(1000 + i, "concurrent_test", f"S{idx}", f"w{idx}-{i}")
            except Exception as exc:  # pragma: no cover
                errs.append(exc)

        threads = [threading.Thread(target=worker, args=(k,)) for k in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
        assert not errs, f"concurrent writes raised: {errs}"
        rows = s.events_recent(limit=1000)
        assert len(rows) == 6 * 20

    def test_prune_old_rows_keeps_recent(self, storage):
        now = int(time.time())
        old_ts, new_ts = now - 40 * 86400, now - 86400
        storage.log_event(old_ts, "old_event", "", "should be pruned")
        storage.log_event(new_ts, "new_event", "", "should survive")
        storage.save_manual_order({"id": "M1", "symbol": "BTCUSDT", "side": "buy",
                                   "kind": "limit", "qty": 1, "price": 1, "status": "pending",
                                   "created_ts": old_ts, "updated_ts": old_ts,
                                   "tp": 0, "sl": 0, "filled_price": None,
                                   "closed_price": None, "pnl": None, "note": ""})
        storage.prune_old_rows(max_age_days=30)
        kinds = [r["kind"] for r in storage.events_recent(limit=100)]
        assert "old_event" not in kinds
        assert "new_event" in kinds
        assert storage.kv_get("anything") is None  # kv untouched
        pend = storage.manual_orders(["pending"])
        assert any(r["id"] == "M1" for r in pend), "manual_orders must survive prune"

    def test_manual_orders_status_filter(self, storage):
        for sid in ("pending", "filled", "cancelled"):
            storage.save_manual_order({"id": f"M-{sid}", "symbol": "ETHUSDT",
                                       "side": "buy", "kind": "limit", "qty": 0.1,
                                       "price": 2000.0, "status": sid, "created_ts": 1,
                                       "updated_ts": 1, "tp": 0, "sl": 0,
                                       "filled_price": None, "closed_price": None,
                                       "pnl": None, "note": ""})
        pend = storage.manual_orders(["pending"])
        assert [r["id"] for r in pend] == ["M-pending"]


# ════════════════════════════════════════════════════════════════
# manual_orders — validation, fills, TP/SL triggers, spot guard
# ════════════════════════════════════════════════════════════════

def _open_count(mgr, symbol):
    return sum(1 for p in mgr.positions.get(symbol, []) if p.is_open)


class FakeBroker:
    """Spot-like broker stub (no open_short → forces the spot code path)."""
    name = "paper"

    def __init__(self):
        self.prices = {"BTCUSDT": 100.0, "ETHUSDT": 2000.0}
        self.positions: dict[str, Position] = {}

    def last_price(self, symbol):
        return self.prices.get(symbol, 0.0)

    def set_price(self, symbol, px):
        self.prices[symbol] = px

    def open_long(self, symbol, qty, price, pos):
        pos.entry = price
        self.positions[pos.id] = pos
        return True

    def close_long(self, pos, price, qty=None, reason=""):
        q = qty if qty is not None else pos.qty
        pnl = (price - pos.entry) * q
        if qty is not None and qty < pos.qty - 1e-12:
            pos.pnl = (pos.pnl or 0.0) + pnl  # partial close keeps it open
        else:
            pos.state = PosState.CLOSED.value
            pos.close_price = price
            pos.pnl = (pos.pnl or 0.0) + pnl
        return pnl

    def equity(self):
        return 0.0


@pytest.fixture()
def mom(tmp_path):
    st = Storage(str(tmp_path))
    return ManualOrderManager(FakeBroker(), st), st


class TestManualOrderManager:
    def test_validation_rejects_bad_input(self, mom):
        mgr, _ = mom
        assert mgr.place("BTCUSDT", "sideways", "market", 1)["ok"] is False
        assert mgr.place("BTCUSDT", "buy", "twap", 1)["ok"] is False
        assert mgr.place("BTCUSDT", "buy", "market", -5)["ok"] is False
        assert mgr.place("BTCUSDT", "buy", "limit", 1, price=None)["ok"] is False

    def test_market_buy_fills_immediately(self, mom):
        mgr, st = mom
        r = mgr.place("BTCUSDT", "buy", "market", qty=1.0)
        assert r["ok"] and r["order"]["status"] == "position"
        assert r["order"]["filled_price"] == 100.0
        assert _open_count(mgr, "BTCUSDT") == 1  # holding pair visible
        rows = st.manual_orders(["position"])
        assert rows[0]["id"] == r["order"]["id"]

    def test_limit_buy_fills_only_when_touched(self, mom):
        mgr, st = mom
        r = mgr.place("BTCUSDT", "buy", "limit", qty=1.0, price=90.0)
        assert r["ok"] and r["order"]["status"] == "pending"
        # price falls to touch the limit → fills
        mgr.check("BTCUSDT", low=89.0, high=91.0, last=90.5)
        rows = st.manual_orders(["position"])
        assert rows and rows[0]["filled_price"] == 90.0
        assert _open_count(mgr, "BTCUSDT") == 1

    def test_limit_buy_not_filled_above_market_rejected(self, mom):
        mgr, _ = mom
        r = mgr.place("BTCUSDT", "buy", "limit", qty=1.0, price=105.0)
        assert r["ok"] is False
        assert "Buy Limit" in r["error"]

    def test_stop_buy_triggers_on_breakout(self, mom):
        mgr, st = mom
        r = mgr.place("BTCUSDT", "buy", "stop", qty=1.0, price=110.0)
        assert r["ok"] and r["order"]["status"] == "pending"
        mgr.check("BTCUSDT", low=99.0, high=109.0, last=108.0)  # not touched
        assert st.manual_orders(["pending"])
        mgr.check("BTCUSDT", low=109.0, high=111.0, last=110.5)  # high >= 110
        assert st.manual_orders(["position"])
        assert not st.manual_orders(["pending"])

    def test_spot_sell_without_inventory_rejected(self, mom):
        mgr, st = mom
        r = mgr.place("BTCUSDT", "sell", "market", qty=1.0)
        # the order is accepted then rejected at fill time (no spot inventory)
        assert r["ok"]
        rows = st.manual_orders(["rejected"])
        assert rows and "موجودی" in rows[0]["note"]
        assert _open_count(mgr, "BTCUSDT") == 0

    def test_spot_sell_with_inventory_closes(self, mom):
        mgr, st = mom
        mgr.place("BTCUSDT", "buy", "market", qty=2.0)
        r = mgr.place("BTCUSDT", "sell", "market", qty=1.0)
        assert r["ok"]
        o = r["order"]
        assert o["status"] == "closed"
        assert o["pnl"] == pytest.approx(0.0)  # same price 100 → 100
        assert _open_count(mgr, "BTCUSDT") == 1  # 1 remaining unit long

    def test_tp_sl_bracket_direction_sanity(self, mom):
        mgr, _ = mom
        # long: TP above, SL below
        assert mgr.place("BTCUSDT", "buy", "limit", qty=1, price=95.0,
                         tp=110.0, sl=90.0)["ok"] is True
        # long with TP BELOW entry → rejected
        r = mgr.place("BTCUSDT", "buy", "limit", qty=1, price=95.0,
                      tp=90.0, sl=90.0)
        assert r["ok"] is False and "TP" in r["error"]
        # long with SL ABOVE entry → rejected
        r = mgr.place("BTCUSDT", "buy", "limit", qty=1, price=95.0,
                      tp=110.0, sl=99.0)
        assert r["ok"] is False and "SL" in r["error"]

    def test_tp_trigger_closes_position(self, mom):
        mgr, st = mom
        r = mgr.place("BTCUSDT", "buy", "limit", qty=1.0, price=95.0, tp=105.0)
        assert r["ok"]
        mgr.check("BTCUSDT", low=94.0, high=96.0, last=95.0)  # fill the limit
        assert _open_count(mgr, "BTCUSDT") == 1, "limit should have filled"
        mgr.check("BTCUSDT", low=104.0, high=106.0, last=105.5)  # touch TP
        assert _open_count(mgr, "BTCUSDT") == 0
        exits = [ev for ev in st.events_recent(limit=10) if ev["kind"] == "manual_exit"]
        assert exits and exits[0]["detail"].startswith("manual_tp")

    def test_sl_trigger_closes_before_tp(self, mom):
        mgr, _ = mom
        mgr.place("BTCUSDT", "buy", "limit", qty=1.0, price=95.0, tp=105.0, sl=90.0)
        mgr.check("BTCUSDT", low=94.0, high=96.0, last=95.0)
        # SL hit first (low pierces 90 while high is below TP)
        mgr.check("BTCUSDT", low=89.0, high=95.0, last=91.0)
        assert _open_count(mgr, "BTCUSDT") == 0

    def test_cancel_pending(self, mom):
        mgr, st = mom
        r = mgr.place("BTCUSDT", "buy", "limit", qty=1.0, price=90.0)
        oid = r["order"]["id"]
        c = mgr.cancel(oid)
        assert c["ok"]
        pend = st.manual_orders(["pending"])
        assert not any(x["id"] == oid for x in pend)
        assert any(x["id"] == oid and x["status"] == "cancelled"
                   for x in st.manual_orders(["cancelled"]))
