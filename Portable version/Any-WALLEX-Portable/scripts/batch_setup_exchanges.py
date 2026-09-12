"""Batch exchange setup: research -> probe -> activate for every catalog
exchange that does not yet have a READY profile.

Usage (from project root, backend NOT required — uses WizardEngine directly):
    python scripts/batch_setup_exchanges.py            # all missing
    python scripts/batch_setup_exchanges.py mexc kucoin  # only these
    python scripts/batch_setup_exchanges.py --list     # show plan, do nothing

Every result lands in the shared knowledge library
(data/profiles/exchange_knowledge.json) with a timestamped change history.
Requires a working AI config (wizard AI step) — set via env AI_PROVIDER/
AI_BASE_URL/AI_MODEL or an existing wizard state.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from bot.crypto_store import CryptoStore  # noqa: E402
from bot.wizard import WizardEngine, PROFILES_ROOT  # noqa: E402


def _make_engine() -> WizardEngine:
    """Same construction as server.create_app: CryptoStore over the active
    profile data dir; AI config comes from wizard state / env as usual."""
    import os
    # the wizard's AI config lives in the ACTIVE profile's bot.db (wallex default)
    os.environ.setdefault("BOT_PROFILE", "wallex")
    from bot.exchange import registry as _reg
    from bot.storage import Storage
    data_dir = str(_reg.resolve_data_dir())
    # FIX(audit-H1): same as server.py — env var, else persisted random key
    # (never the old public constant).
    _key_pw = os.environ.get("WALLEX_KEY_PASSWORD", "")
    if not _key_pw:
        from pathlib import Path as _P
        _pw_file = _P(data_dir) / ".key_password"
        if _pw_file.exists():
            _key_pw = _pw_file.read_text(encoding="utf-8").strip()
        if not _key_pw:
            import secrets as _secrets
            _key_pw = _secrets.token_urlsafe(32)
            _pw_file.write_text(_key_pw, encoding="utf-8")
    store = CryptoStore(data_dir, _key_pw)
    storage = Storage(data_dir)
    return WizardEngine(store=store, storage=storage)


def main(argv: list[str]) -> int:
    if "--list" in argv:
        argv = [a for a in argv if a != "--list"]
        plan_only = True
    else:
        plan_only = False
    eng = _make_engine()
    cat = eng.catalog().get("exchanges", [])
    done, missing = [], []
    for e in cat:
        pid = e.get("id")
        if not pid or pid == "wallex":
            continue
        prof_path = Path(PROFILES_ROOT) / pid / "profile.json"
        status = ""
        if prof_path.exists():
            try:
                status = str(json.loads(prof_path.read_text(encoding="utf-8")).get("status") or "")
            except Exception:
                status = ""
        (done if status in ("ready", "probed") else missing).append((pid, e.get("name", pid), status))
    print(f"catalog: {len(cat)} | ready/probed: {len(done)} | to setup: {len(missing)}")
    for pid, name, st in missing:
        print(f"  - {pid} ({name})")
    if plan_only:
        return 0
    only = set(argv)
    results = []
    for pid, name, _st in missing:
        if only and pid not in only:
            continue
        print(f"\n=== {pid} ({name}) ===", flush=True)
        rec: dict = {"id": pid, "ok": False}
        try:
            prof_path = Path(PROFILES_ROOT) / pid / "profile.json"
            # reuse an existing researched profile — only run AI research when absent
            if prof_path.exists():
                rec["profile_id"] = pid
                print("  • profile exists — reusing", flush=True)
            else:
                jid = eng.research(exchange_id=pid)
                while True:
                    job = eng.job_state(jid)
                    if job.get("status") != "running":
                        break
                    time.sleep(2)
                if job.get("status") != "done" or not (job.get("result") or {}).get("profile_id"):
                    _err = job.get("error") or json.dumps((job.get("result") or {}), ensure_ascii=False)[:300] or f"status={job.get('status')}"
                    rec["error"] = f"research: {str(_err)[:300]}"
                    results.append(rec)
                    print("  ✕ research failed:", rec["error"], flush=True)
                    continue
                rec["profile_id"] = job["result"]["profile_id"]
                print("  ✓ profile built:", rec["profile_id"], flush=True)
            # 2) probe (live)
            jid = eng.probe(rec["profile_id"])
            while True:
                job = eng.job_state(jid)
                if job.get("status") != "running":
                    break
                time.sleep(2)
            res = job.get("result") or {}
            rec["critical_ok"] = bool(res.get("critical_ok"))
            fails = [r["check"] for r in (res.get("results") or []) if not r.get("ok")]
            rec["failed_checks"] = fails
            print("  " + ("✓ probe passed" if rec["critical_ok"] else f"✕ probe failed: {fails}"), flush=True)
            # 3) activate only when critical checks pass
            if rec["critical_ok"]:
                try:
                    eng.activate(rec["profile_id"])
                    rec["activated"] = True
                    print("  ✓ activated", flush=True)
                except Exception as exc:
                    rec["activate_error"] = str(exc)[:200]
                    print("  ✕ activate:", rec["activate_error"], flush=True)
        except Exception as exc:
            rec["error"] = f"{type(exc).__name__}: {str(exc)[:200]}"
            print("  ✕", rec["error"], flush=True)
        results.append(rec)
        out = Path(PROFILES_ROOT) / "batch_setup_results.json"
        out.write_text(json.dumps({"ts": int(time.time()), "results": results},
                                  indent=1, ensure_ascii=False), encoding="utf-8")
    ok_n = sum(1 for r in results if r.get("activated"))
    print(f"\ndone: {ok_n}/{len(results)} activated — details: data/profiles/batch_setup_results.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
