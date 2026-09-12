
"""Functional checks for the per-profile logging framework (2026-09-09).

Covers: per-category 5-file + 10MB cap with roll-out of oldest, auto ts+profile
stamping, the process-wide accessor, and the thread override used by wizard
jobs (so a "research MEXC" job logs into Logs/mexc/ even from a wallex backend).
"""
import time, json, threading, os, sys
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import pytest
from bot.file_logger import (
    FileLogger, CATEGORIES, MAX_FILES_PER_CATEGORY,
    set_active, get_active, set_thread_override, log, log_error,
)


def _new_logger(tmp_path):
    return FileLogger(tmp_path, "20260101_000000", profile_id="wallex")


def test_categories_include_troubleshooting():
    for c in ("wizard", "ai", "orders", "errors", "server"):
        assert c in CATEGORIES


def test_write_stamps_ts_and_profile(tmp_path):
    fl = _new_logger(tmp_path)
    fl.write("events", {"note": "hello"})
    p = list(tmp_path.glob("*_events.jsonl"))[0]
    rec = json.loads(p.read_text().strip().splitlines()[-1])
    assert rec["ts"] > 1_700_000_000          # auto epoch ts
    assert rec["profile"] == "wallex"          # profile tag
    assert rec["note"] == "hello"
    fl.close_all()


def test_per_category_file_cap_rolls_oldest(tmp_path):
    # Simulate 9 prior boots each writing a settings file (distinct run_ids).
    now = time.time()
    for i in range(9):
        age = 9 - i                          # i=8 -> newest (age 1)
        fname = tmp_path / f"{20250100 + i}_000000_settings.jsonl"
        fname.write_text(f'{{"n": {i}}}')
        os.utime(fname, (now - age * 3600, now - age * 3600))
    # current run id is DISTINCT (2026...) so it is unambiguously the newest
    fl = FileLogger(tmp_path, "20260101_000000", profile_id="wallex")
    fl.write("settings", {"n": "current"})    # triggers prune for settings
    files = sorted(p.name for p in tmp_path.glob("*_settings.jsonl"))
    # 9 prior + 1 current = 10 -> keep only newest MAX_FILES_PER_CATEGORY
    assert len(files) == MAX_FILES_PER_CATEGORY, files
    # current must survive (it is the newest)
    assert "20260101_000000_settings.jsonl" in files
    # the 4 oldest of the 10 are rolled out
    assert "20250100_000000_settings.jsonl" not in files
    assert "20250101_000000_settings.jsonl" not in files
    assert "20250102_000000_settings.jsonl" not in files
    assert "20250103_000000_settings.jsonl" not in files
    fl.close_all()


def test_size_cap_drops_oldest_when_over_10mb(tmp_path):
    import bot.file_logger as fl_mod
    orig = fl_mod.MAX_BYTES_PER_CATEGORY
    try:
        fl_mod.MAX_BYTES_PER_CATEGORY = 1000   # tiny cap for the test
        now = time.time()
        # 3 OLD files (distinct run_ids, oldest->newest), each 800 bytes.
        for i in range(3):
            f = tmp_path / f"{20250100 + i}_000000_api.jsonl"
            f.write_text("x" * 800)
            os.utime(f, (now - (3 - i) * 60, now - (3 - i) * 60))
        # current run id is DISTINCT (2026...) so it is unambiguously newest
        fl = FileLogger(tmp_path, "20260101_000000", profile_id="wallex")
        fl.write("api", {"n": "cur"})
        files = sorted(p.name for p in tmp_path.glob("*_api.jsonl"))
        total = sum((tmp_path / f).stat().st_size for f in files)
        # current survives; oldest dropped until under the (1000) cap
        assert "20260101_000000_api.jsonl" in files, files
        assert total <= 1000, (files, total)
        fl.close_all()
    finally:
        fl_mod.MAX_BYTES_PER_CATEGORY = orig


def test_accessors_and_thread_override(tmp_path):
    host = _new_logger(tmp_path)
    set_active(host)
    try:
        # main thread -> host
        log("errors", {"msg": "boom"})
        assert any(tmp_path.glob("*_errors.jsonl"))
        # another "profile" via thread override (wizard-style)
        mexc_dir = tmp_path / "mexc"
        mexc = FileLogger(mexc_dir, "20260101_000000", profile_id="mexc")
        def worker():
            set_thread_override(mexc)
            try:
                log("wizard", {"event": "research_start"})
            finally:
                set_thread_override(None)
        t = threading.Thread(target=worker)
        t.start(); t.join()
        # the wizard log went to the OVERRIDE dir, not the host dir
        assert list(mexc_dir.glob("*_wizard.jsonl")), "wizard log should be in override dir"
        host_wizard = list(tmp_path.glob("*_wizard.jsonl"))
        assert not host_wizard, "wizard log must NOT leak to host dir"
    finally:
        set_active(None)
    host.close_all(); mexc.close_all()


def test_log_error_structured(tmp_path):
    host = _new_logger(tmp_path)
    set_active(host)
    try:
        try:
            raise ValueError("kaboom 42")
        except ValueError:
            log_error(sys.exc_info()[1], context="adapter._request")
        p = list(tmp_path.glob("*_errors.jsonl"))[0]
        rec = json.loads(p.read_text().strip().splitlines()[-1])
        assert rec["type"] == "ValueError"
        assert "kaboom 42" in rec["detail"]
        assert rec["context"] == "adapter._request"
        assert rec["profile"] == "wallex"
    finally:
        set_active(None)
    host.close_all()
