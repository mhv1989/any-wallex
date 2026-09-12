"""Continuous QA monitor for the Wallex paper bot.

Polls the live backend and the Logs folder, detects anomalies, and appends
findings to a QA report file that the agent/user can review.

Modes of monitoring:
  - endpoint availability / HTTP status
  - engine running state, connection, last tick ts
  - opportunities appearing / disappearing unexpectedly
  - open / closed positions and PnL movements
  - paper balance / equity integrity (starting 1000 USDT)
  - Logs folder file growth (settings/events/balance/decisions/etc)
  - backend log for error/exception/traceback lines

Writes to:
  - data/qa_monitor.log  (raw polled state snapshots)
  - data/qa_report.md    (accumulated findings)
"""
import json
import os
import time
import glob
import subprocess
import sys
from datetime import datetime, timezone

BASE = os.environ.get("QA_BASE_URL", "http://127.0.0.1:8787")
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)  # project root (scripts/ -> project/)
LOGS_DIR = os.path.join(ROOT, "Logs")   # Phase 6: per-profile dirs Logs/<pid>/
MONITOR_LOG = os.path.join(ROOT, "data", "qa_monitor.log")
REPORT = os.path.join(ROOT, "data", "qa_report.md")

POLL_SEC = 45
BACKEND_LOG = os.path.join(ROOT, "data", "qa_backend.log")


def http_json(path, timeout=15):
    import urllib.request
    req = urllib.request.Request(BASE + path, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def now_iso():
    return datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S")


def append_monitor(line):
    with open(MONITOR_LOG, "a", encoding="utf-8") as f:
        f.write(f"[{now_iso()}] {line}\n")


def append_report(md):
    with open(REPORT, "a", encoding="utf-8") as f:
        f.write(md + "\n")


def tail(path, n):
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            lines = f.read().splitlines()
        return lines[-n:]
    except Exception:
        return []


def main():
    os.makedirs(os.path.join(ROOT, "data"), exist_ok=True)
    # track state
    seen_opp_keys = set()
    seen_pos_ids = set()
    known_event_kinds = set()
    prev_equity = None
    monitor_tick = 0

    append_report(f"## QA Monitoring session started {now_iso()}")
    append_report("Setup: paper spot, 1000 USDT, 50 high-liquidity USDT symbols, engine running on :8787")

    while True:
        monitor_tick += 1
        state = {}

        # ── 1. engine status ─────────────────────────────────────────
        try:
            st = http_json("/api/status", timeout=15)
            state["running"] = st.get("running")
            state["connected"] = st.get("connected")
            state["last_tick_ts"] = st.get("last_tick_ts")
            state["n_symbols"] = len(st.get("symbols") or [])
            stats = st.get("stats") or {}
            state["equity"] = stats.get("equity")
            state["open_positions"] = stats.get("open_positions")
            state["closed_trades"] = stats.get("closed_trades")
        except Exception as e:
            append_report(f"### ⚠️ {now_iso()} `/api/status` FAILED: {e}")
            append_monitor(f"status_FAIL {e}")
            time.sleep(POLL_SEC)
            continue

        # engine stopped unexpectedly? (report only once, not every poll)
        if state["running"] is False:
            if not getattr(state, "_reported_stopped", False):
                state["_reported_stopped"] = True
                append_report(f"### ❌ {now_iso()} ENGINE NOT RUNNING (expected running=True). INVESTIGATE.")
        elif getattr(state, "_reported_stopped", False):
            state["_reported_stopped"] = False
        if state["last_tick_ts"] in (0, None):
            now_int = int(time.time())
            first_seen = state.setdefault("_first_tick0_seen", now_int)
            if now_int - first_seen >= 3600 and not getattr(state, "_reported_tick0_long", False):
                state["_reported_tick0_long"] = True
                append_report(f"- {now_iso()} last_tick_ts=0 for >1h: first scan slower than expected on {state['n_symbols']} symbols. INVESTIGATE PACING.")

        # ── 2. paper balance / equity integrity ─────────────────────
        try:
            bal = http_json("/api/paper-balance", timeout=15)
            q = bal.get("quote_currency")
            if bal.get("paper_kind") != "spot":
                append_report(f"### ⚠️ {now_iso()} paper_kind={bal.get('paper_kind')} — expected spot!")
            if q not in ("USDT",):
                append_report(f"### ⚠️ {now_iso()} quote_currency={q} — expected USDT")
            eq = bal.get("equity")
            if prev_equity is not None and eq is not None and abs(eq - prev_equity) > 1e-6:
                append_report(f"- {now_iso()} equity change: {prev_equity} → {eq} (Δ {round(eq-prev_equity,4)})")
            prev_equity = eq
        except Exception as e:
            append_report(f"### ⚠️ {now_iso()} `/api/paper-balance` FAILED: {e}")

        # ── 3. opportunities ─────────────────────────────────────────
        try:
            opps = http_json("/api/opportunities", timeout=15)
            elig = [o for o in opps if o.get("eligible")]
            # new eligible opportunities
            for o in elig:
                key = json.dumps({k: o.get(k) for k in ("symbol", "score", "pattern")}, sort_keys=True)
                if key not in seen_opp_keys:
                    seen_opp_keys.add(key)
                    append_report(f"- {now_iso()} 🎯 NEW ELIGIBLE opp: {o.get('symbol')} score={o.get('score')} pattern={o.get('pattern')} entry={o.get('entry')} stop={o.get('stop')} rr={o.get('rr')}")
            # cleared?
            current_keys = set(json.dumps({k: o.get(k) for k in ("symbol", "score", "pattern")}, sort_keys=True) for o in elig)
            for old in list(seen_opp_keys):
                if old and old not in current_keys:
                    # only complain if it was genuinely eligible before
                    seen_opp_keys.discard(old)
        except Exception as e:
            append_report(f"### ⚠️ {now_iso()} `/api/opportunities` FAILED: {e}")

        # ── 4. positions (open) ──────────────────────────────────────
        try:
            poss = http_json("/api/positions", timeout=15)
            for p in poss:
                pid = p.get("id")
                if pid and pid not in seen_pos_ids:
                    seen_pos_ids.add(pid)
                    append_report(f"- {now_iso()} ✅ ENTRY: {p.get('symbol')} {p.get('side')} qty={p.get('qty')} entry={p.get('entry')} stop={p.get('stop')} risk_coef={p.get('risk_coef')} state={p.get('state')}")
                # spot must never be short or leveraged
                if p.get("side") == "short":
                    append_report(f"### ⚠️ {now_iso()} SPOT SHORT POSITION detected: {p.get('symbol')} side={p.get('side')}")
                rc = p.get("risk_coef")
                if rc is not None and abs(rc - 1.0) > 1e-9:
                    append_report(f"### ⚠️ {now_iso()} PAPER SPOT position with risk_coef={rc} (expected 1.0 for spot): {p.get('symbol')} id={pid}")
        except Exception as e:
            append_report(f"### ⚠️ {now_iso()} `/api/positions` FAILED: {e}")

        # ── 5. events (رویداد ها) — watch for errors/logic flags ────
        try:
            evs = http_json("/api/events?limit=50", timeout=15)
            for e in evs:
                kind = e.get("type") or e.get("kind") or e.get("event")
                detail = e.get("detail") or e.get("message") or ""
                kstr = f"{kind}|{detail}"
                if kind in ("error", "exit", "entry_blocked", "paper_mode_changed", "mode_changed") or "error" in (kind or "").lower():
                    if kstr not in known_event_kinds:
                        known_event_kinds.add(kstr)
                        append_report(f"- {now_iso()} EVENT [{kind}]: {detail}")
        except Exception as e:
            append_report(f"### ⚠️ {now_iso()} `/api/events` FAILED: {e}")

        # ── 6. backend log errors ────────────────────────────────────
        if os.path.exists(BACKEND_LOG) and monitor_tick % 6 == 0:
            errs = [l for l in tail(BACKEND_LOG, 300) if any(w in l.lower() for w in ("traceback", " error ", "exception", "wallex api network failure"))]
            # dedupe against last reported set
            sig = tuple(errs[-6:])
            if errs and sig != state.get("_last_errors"):
                state["_last_errors"] = sig
                append_report(f"### 🔧 {now_iso()} backend log errors ({len(errs)} recent):")
                for l in errs[-5:]:
                    append_report(f"      `{l.strip()[:200]}`")

        # ── 7. Logs folder inventory (per-profile: Logs/<pid>/*.jsonl) ──
        try:
            files = sorted(glob.glob(os.path.join(LOGS_DIR, "*", "*.jsonl")) or
                           glob.glob(os.path.join(LOGS_DIR, "*.jsonl")))
            total_bytes = sum(os.path.getsize(f) for f in files)
            cats = sorted({os.path.basename(f).split("_", 2)[-1].split(".")[0] for f in files})
            state["log_categories"] = cats
            state["log_bytes"] = total_bytes
        except Exception as e:
            append_report(f"### ⚠️ {now_iso()} Logs folder scan failed: {e}")

        # ── 8. granular Logs content check (decisions/open/closed) ──
        try:
            for cat in ("decisions", "open_positions", "closed_positions", "opportunities"):
                for f in (glob.glob(os.path.join(LOGS_DIR, "*", f"*_{cat}.jsonl")) or
                          glob.glob(os.path.join(LOGS_DIR, f"*_{cat}.jsonl"))):
                    lines = tail(f, 5)
                    for l in lines:
                        try:
                            obj = json.loads(l)
                            kind = obj.get("kind")
                            if kind in ("entry_blocked",) and obj not in getattr(state, "_seen_blocked", []):
                                state.setdefault("_seen_blocked", []).append(obj)
                                append_report(f"- {now_iso()} DECISION [entry_blocked] {obj.get('symbol')} reason={obj.get('reason', obj.get('detail'))}")
                        except Exception:
                            pass
        except Exception as e:
            pass

        # ── periodic summary line ────────────────────────────────────
        if monitor_tick % 4 == 0:
            append_monitor(
                f"TICK={monitor_tick} running={state.get('running')} "
                f"connected={state.get('connected')} last_tick={state.get('last_tick_ts')} "
                f"eq={state.get('equity')} open_pos={state.get('open_positions')} "
                f"closed_trades={state.get('closed_trades')} symbols={state.get('n_symbols')} "
                f"opps_rows={len(opps) if 'opps' in dir() else '?'} cats={state.get('log_categories')} "
                f"log_bytes={state.get('log_bytes')}"
            )

        time.sleep(POLL_SEC)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        append_report(f"## QA monitoring stopped by user {now_iso()}")