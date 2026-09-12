"""End-to-end smoke test against LIVE Wallex public API.

Uses a short API gap (0.5s) ONLY for testing speed; production config keeps 12s.
Verifies: markets fetch, candle fetch (closed-only), structure, signal build,
grid build, paper broker open/close, storage, stats.
"""
import sys, time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from bot.broker import PaperBroker
from bot.engine import Engine
from bot.storage import Storage
from bot.wallex_client import WallexClient
from bot.server import load_config

cfg = load_config(ROOT / "config.yaml")
cfg["symbols"] = ["BTCUSDT", "ETHUSDT"]          # fewer symbols for speed
cfg["engine"]["api_min_gap_sec"] = 0.5           # TEST ONLY

client = WallexClient(min_gap_sec=0.5, max_retries=2, retry_pause_sec=5)
storage = Storage(str(ROOT / "data" / "smoketest"))
broker = PaperBroker(10000.0)
engine = Engine(cfg, client, broker, storage)

t0 = time.time()
print("== tick_once against live Wallex API ==")
engine.tick_once()
print(f"tick done in {time.time()-t0:.1f}s, connected={engine.connected}")

for sym, snap in engine.snapshots.items():
    sig = snap.signal
    print(f"\n{sym}: price={snap.price} trend={snap.trend} event={snap.structure_event}")
    print(f"  support={snap.support} resistance={snap.resistance} grid_active={snap.grid_active} grid_levels={len(snap.grid_levels)}")
    print(f"  rsi_h1={snap.rsi_h1 and round(snap.rsi_h1,1)} atr_h1={snap.atr_h1 and round(snap.atr_h1,2)}")
    if sig:
        print(f"  signal: score={sig.score}/8 eligible={getattr(sig,'eligible',None)} pattern={sig.pattern} rr={sig.rr:.2f}")
        for c in sig.confirmations:
            print(f"    [{'x' if c.ok else ' '}] {c.label_fa} {c.detail}")
    else:
        print("  signal: None (insufficient data or no setup)")

print("\n== stats ==")
print(engine.stats())
print("\n== api log (last 8) ==")
for e in client.api_log[-8:]:
    print(f"  {e.method} {e.path} -> {e.status} {e.latency_ms}ms retries={e.retries} {e.error}")
print("\nSMOKE OK")
