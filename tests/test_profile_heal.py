"""Smart re-research + profile self-heal (user req): re-running the wizard
overwrites the AI template but PRESERVES keys/candles/strategies; corrupt or
invalid profiles are archived and flagged for re-research. Offline tests."""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot.exchange import profile as P


def _mk_profile(pid: str) -> dict:
    p = P.default_profile(pid, pid)
    p["base_url"] = f"https://api.{pid}.example"
    p["endpoints"] = {"candles": {"method": "GET", "path": "/k"},
                      "markets": {"method": "GET", "path": "/m"}}
    return p


def _save_state(pid: str, root: Path, *, with_history=True, with_secrets=True) -> None:
    d = root / pid
    d.mkdir(parents=True, exist_ok=True)
    P.save_profile(_mk_profile(pid), root)
    if with_history:
        h = d / "history" / "BTCUSDT"
        h.mkdir(parents=True, exist_ok=True)
        (h / "60.json").write_text("[]", encoding="utf-8")
    if with_secrets:
        (d / "secrets.enc").write_bytes(b"enc")
        (d / "bot.db").write_bytes(b"db")


def test_research_overwrites_profile_but_preserves_data():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        _save_state("exx", root)
        d = root / "exx"
        hist_before = (d / "history" / "BTCUSDT" / "60.json").stat().st_mtime_ns
        db_before = (d / "bot.db").stat().st_mtime_ns

        # simulate the smart re-research overwrite (same logic as _research_sync)
        previous = P.load_profile("exx", root)
        assert previous is not None
        P.archive_profile("exx", root, tag="prev")
        new = _mk_profile("exx")
        new["base_url"] = "https://api.exx-v2.example"   # AI's new answer
        P.save_profile(new, root)

        # profile replaced, data preserved
        assert P.load_profile("exx", root)["base_url"] == "https://api.exx-v2.example"
        assert (d / "history" / "BTCUSDT" / "60.json").stat().st_mtime_ns == hist_before
        assert (d / "bot.db").stat().st_mtime_ns == db_before
        assert (d / "secrets.enc").exists()
        archives = list(d.glob("profile.prev-*.json"))
        assert len(archives) == 1


def test_archive_keeps_only_three():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        _save_state("arc", root, with_history=False, with_secrets=False)
        for _ in range(5):
            P.archive_profile("arc", root, tag="prev")
        assert len(list((root / "arc").glob("profile.prev-*.json"))) == 3


def test_heal_missing_profile_json_flags_research():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        d = root / "ghost"
        d.mkdir()
        (d / "secrets.enc").write_bytes(b"enc")
        out = P.heal_profile("ghost", root)
        assert out["needs_research"] is True
        assert "secrets.enc" in out["data_preserved"]


def test_heal_corrupt_profile_archives_it():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        d = root / "bad"
        d.mkdir()
        (d / "profile.json").write_text("{ this is not json", encoding="utf-8")
        (d / "bot.db").write_bytes(b"db")
        out = P.heal_profile("bad", root)
        assert out["needs_research"] is True
        assert any("corrupt" in h for h in out["healed"])
        assert "bot.db" in out["data_preserved"]
        assert list(d.glob("profile.corrupt-*.json"))


def test_heal_invalid_profile_archives_it():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        d = root / "inv"
        d.mkdir()
        p = _mk_profile("inv")
        p["base_url"] = ""            # validation error
        P.save_profile(p, root)
        out = P.heal_profile("inv", root)
        assert out["needs_research"] is True
        assert any("invalid" in h for h in out["healed"])


def test_heal_healthy_profile_untouched():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        _save_state("okp", root)
        out = P.heal_profile("okp", root)
        assert out["needs_research"] is False
        assert any("valid" in h for h in out["healed"])
        assert len(list((root / "okp").glob("profile.*-*.json"))) == 0
