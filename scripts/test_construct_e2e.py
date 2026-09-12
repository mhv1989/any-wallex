"""End-to-end test: build strategy from the user's real Persian vibe via qwen3.8-27b."""
import json
import time
import requests

BASE = "http://127.0.0.1:8787"

# wait for server
for _ in range(20):
    try:
        requests.get(BASE + "/api/status", timeout=2)
        break
    except Exception:
        time.sleep(1)

cfg = json.load(open("data/ai_providers.json", encoding="utf-8"))["items"][0]

VIBE = """استراتژی همگرایی پویا با ورود مجدد هوشمند (DCR):
تایم فریم ۴ ساعته روی جفتارزهای USDT.
فاز ۱: خرید فقط مجاز است وقتی قیمت بسته شده بالای یا مساوی خط میانی دونچیان ۲۰ باشد.
فاز ۲: سیگنال ورود وقتی Close بالای باند بالایی دونچیان ۲۰ و بالای باند بالایی پوشش درصدی ۵٪ (Envelope Upper) برود.
فاز ۳: نسبت نوسان R = پهنای بولیگر تقسیم بر پهنای کلتنر (هر دو دوره ۲۰، کلتنر ضریب ۱.۵). اگر R بین ۰.۸ تا ۱.۲ بود ورود فوری با حد ضرر باند پایین کلتنر. اگر R بالای ۱.۲ بود (هیجان) سفارش محدود روی خط میانی کلتنر بگذار و صبر کن اصلاح بیاید. اگر R زیر ۰.۸ بود سیگنال لغو.
فاز ۴: خروج — نصف پوزیشن روی باند بالایی پوشش درصدی، نصف دیگر تا وقتی قیمت بالای خط میانی کلتنر (EMA 20) بماند تریل شود؛ بسته شدن کندل زیر خط میانی کلتنر یعنی خروج کامل. اگر قیمت زیر خط میانی دونچیان برود خروج اضطراری."""

print("=== 1) connection test ===")
r = requests.post(BASE + "/api/ai/test", json={
    "provider": cfg["provider"], "base_url": cfg["base_url"], "api_key": cfg["api_key"],
}, timeout=30)
print(r.status_code, json.dumps(r.json(), ensure_ascii=False))

print("=== 2) construct strategy via qwen3.8-27b ===")
t0 = time.time()
r = requests.post(BASE + "/api/strategies/construct", json={
    "vibe": VIBE,
    "provider": cfg["provider"],
    "base_url": cfg["base_url"],
    "api_key": cfg["api_key"],
    "model": cfg["model"],
}, timeout=420)
print(f"HTTP {r.status_code} in {time.time()-t0:.1f}s")
out = r.json()
if not r.ok or not out.get("ok"):
    print("FAILED:", json.dumps(out, ensure_ascii=False)[:600])
    raise SystemExit(1)

art = out["artifact"]
print("=== 3) resulting artifact ===")
print("strategy_id:", art.get("strategy_id"))
print("timeframe:", art.get("timeframe"), "| execution_mode:", art.get("execution_mode"),
      "| min_confirmations:", art.get("min_confirmations"), "| cooldown:", art.get("cooldown_bars"))
print("entry_conditions:")
for c in art.get("entry_conditions", []):
    print("  ", json.dumps(c, ensure_ascii=False))
print("exit_conditions:")
for c in art.get("exit_conditions", []):
    print("  ", json.dumps(c, ensure_ascii=False))
print("risk:", json.dumps(art.get("risk", {}), ensure_ascii=False)[:300])

print("=== 4) listed in strategy library? ===")
r = requests.get(BASE + "/api/strategies", timeout=10)
ids = [s.get("strategy_id") for s in r.json().get("external", [])]
print("library ids:", ids, "| new strategy present:", art.get("strategy_id") in ids)

print("=== 5) live-engine evaluability of every condition ===")
import random
from bot.models import Candle
from bot.strategy_eval import evaluate_condition
random.seed(3)
px = 100.0
candles = []
for i in range(200):
    o = px
    px += random.uniform(-1.5, 1.6)
    c = px
    h = max(o, c) + abs(random.uniform(0, 0.8))
    l = min(o, c) - abs(random.uniform(0, 0.8))
    candles.append(Candle(ts=1700000000 + i * 3600, o=o, h=h, l=l, c=c, v=random.uniform(500, 5000)))
for c in art.get("entry_conditions", []) + art.get("exit_conditions", []):
    res = evaluate_condition(c, candles)
    name = c.get("indicator")
    print(f"  {name:22s} -> {'evaluable' if res is not None else 'NOT EVALUABLE'}")
