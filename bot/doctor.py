"""Deep AI troubleshooter — the AI watches and explains the WHOLE app.

Sections the AI can inspect: chart/candles, engine/scan, strategies,
backtest, paper/live trading, API health, exchange profile, AI providers.

Design (user req: "use the AI API for much more troubleshooting through app
use"): a per-section context collector builds a COMPACT, secret-free state
snapshot; a knowledge-base prompt teaches the AI this app's real architecture
and known failure modes; all configured AI providers race (first healthy
answer wins); the AI must answer with a diagnosis + prioritized suggestions,
some of which map to SAFE whitelisted actions the user can apply by click
(never auto-applied).
"""
from __future__ import annotations

import json
import logging
import re
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from .ai_strategy import AIProviderConfig, AIStrategyClient, AIProviderError

log = logging.getLogger("wallex.doctor")

ROOT = Path(__file__).resolve().parent.parent

# ── app knowledge base (compact architecture + known failure modes) ──
KB = """APP KNOWLEDGE (trading bot, FastAPI backend + one-page dashboard):
- Architecture: ExchangeAdapter (exchange client) -> Engine (scan loop per symbol, 4 TFs: 15m/1h/4h/1D)
  -> PaperBroker(spot long-only)/PaperMarginBroker(long+short, isolated-margin semantics)
  /LiveBroker(spot)/LiveMarginBroker(collateral+risk-coef). Storage=SQLite(data/profiles/<id>/bot.db).
  Strategies: legacy 8-criteria (locked in signal.py) + external AI artifacts (strategy_schema v1.1,
  whitelisted indicators, TFs 15/60/240/1D). Backtest shares the SAME condition dispatcher as the engine.
- Candle pipeline: client fetch -> aggregate-if-finer (whole-series spacing check) -> delta-first fetch
  with correctness gate -> disk cache data/profiles/<id>/history/<SYM>/<res>.json. Probe verifies true
  granularity via spacing histogram.
- Profiles: one exchange = one data dir (data/profiles/<id>/) = one port (registry.json). Profile.json
  defines base_url/auth/endpoints; GenericRESTAdapter executes it. Capability gates: margin refused when
  profile has no margin block.
- AI layer: providers race for insights (current -> config_id -> saved configs -> wizard -> default);
  truncated (finish_reason=length) is non-retryable; output budget 16384.

KNOWN FAILURE MODES (most likely causes first):
1. getaddrinfo failed / DNS errors -> wrong or geo-blocked host in profile base_url; try apiv2-style
   alternate hosts; a RUNNING backend keeps its OLD working profile until restart.
2. "Expecting value: line 1" on markets -> response is HTML/empty, often a POST-form endpoint called
   as GET, or a keyed-object response the field_map misses ( Nobitex /market/stats = POST, empty form
   body, object keyed by lowercase base-quote names).
3. 0 trades in backtest -> shallow history depth (ensure_depth preflight), or strategy min_confirmations
   too strict, or legacy placeholder routing (legacy must route external_strategy=None).
4. identical candles on two TFs -> cache-key collision or missing granularity aggregation.
5. stale prices -> engine stopped, or engine symbol list does not include the symbol.
6. 429/5xx storms -> rate limit; check min_gap_sec vs the exchange's documented limit.
7. margin engage refused -> profile capabilities.margin=false (spot-only exchange).
8. AI insight falls back to raw data -> all providers failed; see per-candidate errors.
9. wizard probe fails only on auth checks -> exchange API key missing/invalid, probe works public-only.
10. paper balance lost after restart -> paper_balance_<kind>_<quote> KV wiped by factory reset."""


class AppDoctor:
    """Collects section state, asks the racing AI providers, returns
    diagnosis + suggestions (+ whitelisted action proposals)."""

    def __init__(self, engine=None, client=None, storage=None, cmc=None,
                 wizard=None, data_dir: str = "", profile_id: str = "",
                 base_url: str = "", registry_read=None):
        self.engine = engine
        self.client = client
        self.storage = storage
        self.cmc = cmc
        self.wizard = wizard
        self.data_dir = data_dir
        self.profile_id = profile_id
        self.base_url = base_url
        self._registry_read = registry_read or (lambda: [])

    # ── context collectors (secret-free, compact) ────────────────────
    def _ctx_status(self) -> dict:
        try:
            st = {
                "running": self.engine.running,
                "mode": getattr(self.engine.broker, "name", "?"),
                "symbols": len(self.engine.symbols),
                "last_tick_age_min": round((time.time() - self.engine.last_tick_ts) / 60, 1)
                if self.engine.last_tick_ts else None,
                "connected": self.engine.connected,
                "open_positions": len(self.engine.positions),
            }
            return st
        except Exception as exc:
            return {"error": str(exc)[:120]}

    def _ctx_api_health(self) -> dict:
        try:
            log_rows = (self.client.api_log or [])[-25:]
            fails = [r for r in log_rows if (r.status and r.status >= 400) or r.error]
            return {
                "recent_calls": len(log_rows),
                "failures": [{"path": r.path, "status": r.status, "error": r.error[:80]}
                             for r in fails[-8:]],
            }
        except Exception as exc:
            return {"error": str(exc)[:120]}

    def _ctx_candles(self) -> dict:
        try:
            cache = getattr(self.engine, "_candle_cache", {}) or {}
            per_tf: Dict[str, int] = {}
            stale: List[str] = []
            now = time.time()
            for key, rows in list(cache.items())[:40]:
                tf = key.split(":")[-1]
                per_tf[tf] = per_tf.get(tf, 0) + 1
                if rows:
                    age_h = (now - rows[-1].ts) / 3600
                    if age_h > 6 and tf != "1D":
                        stale.append(f"{key} ({age_h:.0f}h old)")
            return {"cached_sets": len(cache), "sets_per_tf": per_tf,
                    "stale": stale[:6]}
        except Exception as exc:
            return {"error": str(exc)[:120]}

    def _ctx_strategies(self) -> dict:
        try:
            act = getattr(self.engine, "active_external_strategy", None)
            name = (act.get("name") if isinstance(act, dict) else getattr(act, "name", None)) or "legacy"
            ext = getattr(self.engine, "external_strategies", []) or []
            return {"active": name, "external_count": len(ext),
                    "enabled": [s.get("name") for s in ext if isinstance(s, dict) and s.get("enabled")][:8]}
        except Exception as exc:
            return {"error": str(exc)[:120]}

    def _ctx_profile(self) -> dict:
        try:
            p = Path(self.data_dir) / "profile.json"
            if not p.exists():
                return {"profile_id": self.profile_id, "note": "built-in template (wallex)"}
            prof = json.loads(p.read_text(encoding="utf-8"))
            return {
                "profile_id": prof.get("id"), "base_url": prof.get("base_url"),
                "auth": (prof.get("auth") or {}).get("scheme"),
                "has_margin": bool(prof.get("margin")),
                "quotes": prof.get("quotes"),
                "status": prof.get("status"),
                "limitations_count": len(prof.get("limitations") or []),
                "last_probe_critical_ok": bool((prof.get("last_probe") or {}).get("results")) and
                all(r.get("ok") for r in prof["last_probe"]["results"]
                    if str(r.get("check", "")).startswith(("markets", "candles"))),
            }
        except Exception as exc:
            return {"error": str(exc)[:120]}

    def _ctx_recent_errors(self) -> List[str]:
        out: List[str] = []
        try:
            logdir = ROOT / "Logs" / (self.profile_id or "")
            files = sorted(logdir.glob("*_events.jsonl")) if logdir.exists() else []
            if files:
                lines = files[-1].read_text(encoding="utf-8", errors="ignore").splitlines()[-40:]
                for l in lines:
                    try:
                        o = json.loads(l)
                        if o.get("kind") in ("error", "stale_candles", "insufficient_bars",
                                             "external_strategy_eval_failed"):
                            out.append(f"{o.get('kind')}: {str(o.get('detail'))[:110]}")
                    except Exception:
                        continue
        except Exception:
            pass
        return out[-8:]

    def collect(self, section: str = "all") -> dict:
        """Compact state snapshot for one section (or 'all')."""
        s = (section or "all").lower()
        ctx: Dict[str, Any] = {"profile_id": self.profile_id, "ts": time.strftime("%Y-%m-%d %H:%M")}
        if s in ("all", "engine"):
            ctx["engine"] = self._ctx_status()
        if s in ("all", "api"):
            ctx["api_health"] = self._ctx_api_health()
        if s in ("all", "chart", "candles"):
            ctx["candles"] = self._ctx_candles()
        if s in ("all", "strategies"):
            ctx["strategies"] = self._ctx_strategies()
        if s in ("all", "profile", "exchange"):
            ctx["exchange_profile"] = self._ctx_profile()
        if s in ("all", "events"):
            ctx["recent_error_events"] = self._ctx_recent_errors()
        return ctx

    # ── AI invocation (races all providers) ──────────────────────────
    def _collect_candidates(self) -> List[dict]:
        from .wizard import WizardEngine  # reuse masked-config readers
        cands: List[dict] = []
        try:
            prov_path = Path(self.data_dir) / "ai_providers.json"
            items = (json.loads(prov_path.read_text(encoding="utf-8")) or {}).get("items", []) if prov_path.exists() else []
            # FIX(user req): the ACTIVE config (بارگذاری شد) races FIRST —
            # the last loaded model is the preferred AI everywhere.
            _active = ""
            try:
                _active = str(self.store_get("active_ai_config") or "").strip()
            except Exception:
                pass
            def _prio(it):
                return (0 if it.get("id") == _active else 1,
                        -int(it.get("created", 0) or 0))
            for it in sorted(items, key=_prio):
                key = ""
                try:
                    key = self._decrypt_ai_key(it)
                except Exception:
                    pass
                if it.get("base_url"):
                    cands.append({"provider": str(it.get("provider", "custom")),
                                  "base_url": str(it["base_url"]), "api_key": key,
                                  "model": str(it.get("model") or "")})
        except Exception:
            pass
        try:
            w = self.wizard.ai_config_masked()
            if w.get("base_url"):
                cands.append({"provider": str(w.get("provider", "ollama")),
                              "base_url": str(w["base_url"]),
                              "api_key": self._wizard_key(),
                              "model": str(w.get("model") or "")})
        except Exception:
            pass
        # dedupe
        seen: set = set()
        out: List[dict] = []
        for c in cands:
            k = (c["provider"], c["base_url"], c["model"])
            if k not in seen:
                seen.add(k)
                out.append(c)
        return out[:5]

    def _decrypt_ai_key(self, item: dict) -> str:
        fn = getattr(self, "_key_decryptor", None)
        return fn(item.get("api_key")) if fn else ""

    def _wizard_key(self) -> str:
        try:
            return self.store_get("wizard_ai_key") or ""
        except Exception:
            return ""

    # ── rule-based local diagnosis (NO AI required) ──────────────────
    # The KB's failure modes are deterministic patterns over the collected
    # context — when AI is unavailable/not allowed, the doctor still works.
    def rule_diagnose(self, section: str, ctx: dict, lang: str = "fa") -> dict:
        en = str(lang).lower() == "en"
        T = (lambda fa, en_: en_) if en else (lambda fa, en_: fa)
        eng = ctx.get("engine") or {}
        apih = ctx.get("api_health") or {}
        cand = ctx.get("candles") or {}
        prof = ctx.get("exchange_profile") or {}
        lines: List[str] = []
        actions: List[str] = []
        findings = 0

        # 1) DNS / host failures (KB #1)
        fails = apih.get("failures") or []
        dns = [f for f in fails if "getaddrinfo" in str(f.get("error", "")) or "DNS" in str(f.get("error", ""))]
        if dns:
            findings += 1
            lines.append(T(
                "• خطای DNS در تماسهای API دیده شد — میزبان پروفایل قابل حل نیست (آدرس اشتباه یا مسدود جغرافیایی). میزبان جایگزین (مثلاً apiv2-style) را امتحان کن.",
                "• DNS failures detected on API calls — the profile host does not resolve (wrong address or geo-block). Try an alternate host (e.g. apiv2-style)."))
            actions.append("heal_profile")

        # 2) engine stopped + stale candles (KB #5)
        if eng.get("running") is False and cand.get("stale"):
            findings += 1
            lines.append(T(
                f"• موتور خاموش است و {len(cand['stale'])} کندل قدیمی شده — قیمتها تا روشن شدن موتور بروز نمیشوند.",
                f"• Engine is stopped and {len(cand['stale'])} candle sets are stale — prices will not refresh until the engine runs."))
            actions.append("engine_start")

        # 3) recent API failures (KB #2/#6)
        http_fails = [f for f in fails if f.get("status")]
        if http_fails:
            findings += 1
            codes = {}
            for f in http_fails:
                codes[f["status"]] = codes.get(f["status"], 0) + 1
            lines.append(T(
                f"• شکستهای HTTP اخیر: {codes} — 429 یعنی محدودیت نرخ (min_gap_sec را زیاد کن)، 5xx یعنی مشکل سمت صرافی، 401 یعنی کلید.",
                f"• Recent HTTP failures: {codes} — 429 = rate limit (raise min_gap_sec), 5xx = exchange-side, 401 = key problem."))

        # 4) profile never probed / errored (KB #9/#2)
        if prof.get("profile_id") and prof.get("profile_id") != "wallex":
            if prof.get("last_probe_critical_ok") is False:
                findings += 1
                lines.append(T(
                    "• آخرین تست زنده پروفایل حیاتیها را پاس نکرد — پروفایل را probe و در صورت نیاز با AI تعمیر کن.",
                    "• The profile's last live probe failed its critical checks — re-probe and repair if needed."))
                actions.append("probe_profile")
            if prof.get("limitations_count"):
                lines.append(T(
                    f"• {prof['limitations_count']} محدودیت ثبتشده در گزارش فنی پروفایل وجود دارد (فایل setup_report.md).",
                    f"• {prof['limitations_count']} recorded limitations exist in the profile's technical report (setup_report.md)."))

        # 5) margin mode on a marginless profile (KB #7)
        if prof.get("has_margin") is False and eng.get("mode") == "margin":
            findings += 1
            lines.append(T(
                "• مارجین روی صرافی بدون مارجین فعال است — این حالت مسدود شده است؛ به اسپات برگرد.",
                "• Margin is engaged on a marginless exchange — this is blocked; switch back to spot."))

        if not findings:
            lines.append(T(
                "• از دادههای زنده فعلی مشکل آشکاری دیده نشد — همه چکهای محلی سالم به نظر میرسند. اگر مشکلی میبینی، جزئیاتش را در یادداشت بنویس یا از حالت AI-Enhanced استفاده کن.",
                "• No obvious problem in the current live data — local checks look healthy. If you see an issue, describe it in the note or use AI-Enhanced mode."))

        mode_line = T("حالت: قوانین محلی (بدون AI)", "Mode: local rules (no AI)")
        head = T("تشخیص محلی بر اساس وضعیت زنده برنامه:", "Local diagnosis from live app state:")
        steps = "\n".join(lines)
        act_txt = ""
        if actions:
            act_txt = "\n" + T("اقدامات پیشنهادی:", "Suggested actions:") + "\n" + \
                "\n".join(f"ACTION:{a}" for a in dict.fromkeys(actions))
        return {"ok": True, "mode": "rules", "text": f"{head}\n{steps}{act_txt}",
                "provider": mode_line, "model": "", "actions": list(dict.fromkeys(actions))}

    def diagnose(self, section: str, user_note: str = "", lang: str = "fa",
                 ai_allowed: bool = True, ai_enhanced: bool = False) -> dict:
        """ai_allowed=False (or no AI configured) → deterministic local rules
        only; the app NEVER depends on AI. ai_enhanced=True → ALSO ask the AI
        for a deeper interpretation; rules answer is always included so the
        user gets a diagnosis even if every AI provider fails."""
        ctx = self.collect(section)
        base = self.rule_diagnose(section, ctx, lang=lang)
        if not ai_allowed or not ai_enhanced:
            base["context"] = ctx
            base["ai_skipped"] = not (ai_allowed and ai_enhanced)
            return base
        # AI-Enhanced path: race providers; on total failure return rules result
        ai_out = self._ai_diagnose(section, ctx, user_note, lang)
        if ai_out.get("ok"):
            return {**ai_out, "context": ctx, "rules_text": base["text"],
                    "mode": "ai_enhanced"}
        base["ai_error"] = ai_out.get("error", "")
        base["context"] = ctx
        base["ai_skipped"] = False
        return base

    def _ai_diagnose(self, section: str, ctx: dict, user_note: str, lang: str) -> dict:
        prompt = (
            "You are the resident diagnostics engineer INSIDE a crypto trading application. "
            "The user reports a problem in the section: " + section + ".\n"
            + (f"USER NOTE: {user_note}\n" if user_note else "")
            + KB + "\n\nLIVE APP STATE (secrets removed):\n"
            + json.dumps(ctx, ensure_ascii=False, default=str)[:9000]
            + "\n\nRespond in " + ("English" if lang == "en" else "Persian (Farsi)") + " — "
            "formal, professional. Structure your answer as:\n"
            "1) یک خط وضعیت (one-line assessment)\n"
            "2) علت محتمل (most likely cause) — pick from the knowledge base if it matches\n"
            "3) گامهای پیشنهادی به ترتیب اولویت (numbered action steps, max 5)\n"
            "4) اگر یک اقدام امن از فهرست ACTIONS قابل اجراست، آن را دقیقاً با شماره اقدام پیشنهاد کن (مثلاً ACTION:engine_start)\n\n"
            "ACTIONS the user can apply by clicking (recommend by exact id when appropriate):\n"
            "- ACTION:engine_start (start the scan engine)\n"
            "- ACTION:engine_stop\n"
            "- ACTION:refresh_markets (refresh the exchange market catalog)\n"
            "- ACTION:heal_profile (validate/self-heal the exchange profile)\n"
            "- ACTION:probe_profile (re-run the live connection probe)\n"
            "- ACTION:ai_diagnose_profile (let AI repair the exchange profile)\n"
            "NEVER invent state you cannot see. Never output JSON. No disclaimers."
        )
        cands = self._collect_candidates()
        if not cands:
            return {"ok": False, "error": "هیچ سرویس AI تنظیم نشده — ابتدا در تب استراتژیها یا ویزارد یک API اضافه کنید"}
        results: List[dict] = []

        def _run(c: dict) -> None:
            try:
                cli = AIStrategyClient(AIProviderConfig(
                    provider=c["provider"], base_url=c["base_url"],
                    api_key=c["api_key"], model=c["model"] or "llama3",
                    timeout_sec=240, retries=1))
                raw = cli._call_provider(prompt, max_tokens=4096)
                results.append({"ok": True, "text": _clean(raw)[:2600],
                                "provider": c["provider"], "model": c["model"],
                                "base_url": c["base_url"]})
            except Exception as exc:  # noqa: BLE001
                results.append({"ok": False, "error": str(exc)[:160],
                                "provider": c["provider"], "model": c["model"]})

        threads = [threading.Thread(target=_run, args=(c,), daemon=True) for c in cands]
        for t in threads:
            t.start()
        deadline = time.time() + 170
        while time.time() < deadline:
            oks = [r for r in results if r.get("ok")]
            if oks:
                return {"ok": True, "section": section, **oks[0], "tried": len(cands)}
            if len(results) == len(threads):
                break
            time.sleep(0.4)
        return {"ok": False, "section": section,
                "error": "; ".join(f"{r.get('provider')}: {r.get('error', 'timeout')}" for r in results)[:300],
                "tried": len(cands)}


def _clean(raw: str) -> str:
    """Strip accidental JSON wrappers from the diagnosis text."""
    t = (raw or "").strip().strip("`")
    t = re.sub(r"^json\s*", "", t, flags=re.I).strip()
    m = re.search(r"\{[\s\S]*\}", t)
    if m:
        try:
            data = json.loads(m.group(0))
            for key in ("diagnosis", "answer", "text", "analysis", "result"):
                if isinstance(data.get(key), str) and data[key].strip():
                    return data[key].strip()
            for v in data.values():
                if isinstance(v, list) and v and all(isinstance(x, str) for x in v):
                    return "\n".join("• " + str(x).strip() for x in v)
        except Exception:
            pass
    return t
