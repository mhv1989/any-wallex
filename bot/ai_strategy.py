"""AI strategy adapter — local + cloud provider abstraction."""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

import re
import requests

log = logging.getLogger("ai_strategy")


def _ai_log(payload: Dict[str, Any]) -> None:
    """Best-effort write to the active per-profile `ai` log (no-op in tests)."""
    try:
        from .file_logger import log as _fl
        _fl("ai", payload)
    except Exception:
        pass


def _url_host_of(url: str) -> str:
    """scheme://host[:port] of a URL — log-safe, no query/path/credentials."""
    u = (url or "").split("://", 1)
    if len(u) < 2:
        return (url or "")[:80]
    host = u[1].split("/", 1)[0]
    return u[0] + "://" + host

# Output budget for JSON-generation calls. 4096 was too small for the wizard's
# profile-extraction output (a full profile JSON) on reasoning-capable models —
# they hit finish_reason=length. Override with env AI_MAX_TOKENS if needed.
GENERATION_MAX_TOKENS = int(os.environ.get("AI_MAX_TOKENS", "16384"))

STRATEGY_SCHEMA_VERSION = "1.1"

EXAMPLE_ARTIFACT: Dict[str, Any] = {
    "schema_version": STRATEGY_SCHEMA_VERSION,
    "strategy_id": "",
    "name": "",
    "description": "",
    "source": "external_ai",
    "provider": "",
    "model": "",
    "timeframe": "60",
    "execution_mode": "auto",
    "cooldown_bars": 3,
    "min_confidence": 0.55,
    "entry_conditions": [],
    "exit_conditions": [],
    "risk": {
        "max_positions": 1,
        "risk_per_trade_pct": 1.0,
        "stop_atr_mult": 1.5,
        "target_atr_mult": 3.0,
        "dollar_tp": 0.0,
        "dollar_stop": 0.0,
        "grid_mode": "none",
        "grid_step_pct": 1.0,
        "grid_max_steps": 5,
    },
    "parameters": {},
    "created_ts": 0,
    "updated_ts": 0,
    "vibe_prompt": "",
    "enabled": True,
}


class AIProviderError(Exception):
    """Raised when the AI provider is unreachable or returns invalid data."""


@dataclass
class AIProviderConfig:
    provider: str = "ollama"
    base_url: str = "http://localhost:11434"
    api_key: str = ""
    model: str = "llama3"
    timeout_sec: int = 120
    retries: int = 2
    retry_pause_sec: int = 2
    # Stream chat completions. Non-streaming POSTs that generate for >~30s get
    # their connection cut by Cloudflare-fronted routers (e.g. router.bynara.id)
    # before any byte is sent -> RemoteDisconnected; streaming keeps the socket
    # active with a continuous byte flow so the proxy never sees an idle conn.
    stream: bool = True


def _smart_truncate_vibe(vibe: str, max_chars: int = 6000) -> str:
    """Long/complex vibe handling: keep the head (setup + rules usually) and the
    tail (conclusions often), drop the middle examples/psychology filler."""
    if len(vibe) <= max_chars:
        return vibe
    head = vibe[: int(max_chars * 0.65)]
    tail = vibe[-int(max_chars * 0.30):]
    return head + "\n\n[... بخش میانی توضیح برای صرفهجویی توکن حذف شد — قوانین عددی را که دیدی حفظ کن ...]\n\n" + tail


def build_strategy_prompt(vibe: str, constraints: Dict[str, Any]) -> str:
    vibe = _smart_truncate_vibe(vibe)
    return (
        "You are an expert trading strategy constructor for the Wallex trading bot (Iranian exchange, USDT and TMN quote pairs).\n"
        "Your job: read the user's request (often Persian, informal), map it onto the app's strategy framework, and output ONE valid JSON artifact.\n\n"
        "=== USER REQUEST ===\n"
        f"{vibe}\n\n"
        "=== APP CAPABILITIES (all of these are real and executable — prefer using the richest set that matches the request) ===\n"
        f"{json.dumps(constraints, ensure_ascii=False, indent=2)}\n\n"
        "Supported indicators (use EXACTLY these keys in conditions — all are executable by the engine AND drawn on the chart):\n"
        "- Trend/MAs: ema, sma, wma, hma, vwma, tema, dema (period optional, default 20)\n"
        "- Momentum: rsi(14), stochastic(14), stochastic_rsi(14), macd, obv, adx(14, returns ADX strength), aroon(25), cci(20), roc(12), williams_r(14), mfi(14), ultimate_oscillator, awesome_oscillator\n"
        "- Volatility: atr(14), bollinger, keltner, donchian\n"
        "- Volume flow: volume, cmf(20), vwap\n"
        "- Levels/structure: pivot, fibonacci, support_resistance\n"
        "- Candle patterns: engulfing, pinbar, inside_bar\n"
        "- Typical uses: 'ADX > 25' = strong trend filter; 'cci < -100' = oversold; 'mfi < 20' = oversold volume; 'williams_r < -80' = oversold; 'stochastic_rsi < 20' = momentum oversold; 'cmf > 0' = buying pressure; 'awesome_oscillator crossover 0' = momentum shift; 'roc > 5' = strong rally\n\n"
        "Condition DSL (each entry/exit condition is an object):\n"
        '{"indicator": "<name>", "operator": ">", "value": 30, "period": 14}\n'
        "operators: > < >= <= == crossover crossunder cross_above cross_below increase decrease\n"
        "- 'period' is optional; only period-based indicators use it.\n"
        "- 'compare' is optional and controls what the indicator is compared to:\n"
        "  • compare:\"value\" (default) — indicator vs the numeric `value` (oscillators like rsi, cci, mfi, adx use this)\n"
        "  • compare:\"price\" — indicator vs the CANDLE CLOSE (price-scale indicators: ema, sma, vwap, bollinger, keltner, donchian, pivot, fibonacci, support_resistance). Example: close above VWAP = {\"indicator\":\"vwap\",\"operator\":\"cross_above\",\"compare\":\"price\"} — value ignored.\n"
        "- 'min_confirmations' (top-level, optional, default 1): how many entry conditions must be true together.\n\n"
        "=== OUTPUT: ONLY this JSON (no markdown, no fences, no commentary) ===\n"
        "{\n"
        '  "schema_version": "1.1",\n'
        '  "strategy_id": "<short-kebab-slug>",\n'
        '  "name": "<human readable>",\n'
        '  "description": "<1-2 sentences, can be Persian>",\n'
        '  "source": "external_ai",\n'
        '  "provider": "<from constraints>",\n'
        '  "model": "<from constraints>",\n'
        '  "timeframe": "15" | "60" | "240" | "1D",\n'
        '  "execution_mode": "auto" | "grid" | "signal" | "tp_sl_dollar",\n'
        '  "min_confirmations": 1,\n'
        '  "cooldown_bars": 3,\n'
        '  "min_confidence": 0.55,\n'
        '  "entry_conditions": [ ...condition objects... ],\n'
        '  "exit_conditions": [ ...condition objects... ],\n'
        '  "risk": {\n'
        '    "max_positions": 1,\n'
        '    "risk_per_trade_pct": 1.0,\n'
        '    "stop_atr_mult": 1.5,\n'
        '    "target_atr_mult": 3.0,\n'
        '    "dollar_tp": 0.0, "dollar_stop": 0.0,\n'
        '    "tmn_tp": 0.0, "tmn_stop": 0.0,\n'
        '    "grid_mode": "none" | "long" | "short" | "both",\n'
        '    "grid_step_pct": 1.0, "grid_max_steps": 5\n'
        "  },\n"
        '  "parameters": {},\n'
        '  "enabled": true\n'
        "}\n\n"
        "=== DECISION GUIDE (one-shot mapping from user words to artifact) ===\n"
        "- Persian/English clues → choices:\n"
        "  • 'گرید', 'پلکانی', 'grid', 'laddered' → execution_mode=grid; set grid_mode (long/short/both), grid_step_pct (0.5-3 typical), grid_max_steps (3-10)\n"
        "  • 'TP دلاری/دلار', 'سود دلاری', 'dollar TP', 'failsafe' → execution_mode=tp_sl_dollar; fill dollar_tp/dollar_stop (>0)\n"
        "  • 'تومان', 'TMN', 'میلیون تومان' → execution_mode=tp_sl_dollar; fill tmn_tp/tmn_stop in TMN units (NOT dollars)\n"
        "  • 'شورت', 'فروش', 'short' → grid_mode includes short, or note in description (spot engine is long-only; margin supports short)\n"
        "  • indicator phrases: 'RSI زیر ۳۰' → {rsi < 30}; 'MACD کراس' → macd crossover; 'بالاتر از میانگین متحرک' → ema/sma conditions; 'حجم بالا' → volume increase; 'قدرت روند/ADX' → adx > 25; 'اشباع فروش CCI' → cci < -100; 'MFI کم' → mfi < 20\n"
        "  • 'سریع/اسکالپ' → timeframe=15, cooldown_bars=1-2; 'روزانه/آرام/سوئینگ' → timeframe=1D or 240, cooldown_bars=4-8; default 60\n"
        "  • 'محافظه‌کار' → risk_per_trade_pct=0.5, stop_atr_mult=2.0; 'تهاجمی' → risk_per_trade_pct=2.0\n"
        "  • If execution style is unclear → execution_mode=signal with ATR-based TP/SL (safest default)\n"
        "- COMPLEX multi-phase descriptions: map ONLY the core entry/exit logic into conditions. Anything the DSL cannot express (conditional re-entry orders, limit-order ladders, R-ratio gates, psychology notes) → summarize faithfully in 'description' and translate its SPIRIT into the closest supported conditions (e.g. 'entry only on breakout after quiet market' → donchian cross_above price + adx > 20 as a calm-market filter). NEVER output conditions with invented syntax.\n"
        "- LONG descriptions: rules stated with numbers win over examples/stories. If phases conflict, later specific rules override earlier general ones.\n"
        "- TMN vs USDT: TMN values are in Toman (large numbers, e.g. 2,000,000 TMN ≈ typical account risk); dollar values are small (e.g. 5-50 USDT). NEVER convert between them.\n"
        "- Keep entry_conditions to 2-5 concrete checks; exit_conditions 1-3. Every condition must be evaluable by the DSL above.\n"
        "- If the user request mixes several ideas, pick the dominant one and mention the rest in 'description'.\n\n"
        "=== HARD RULES ===\n"
        "- timeframe MUST be 15, 60, 240, or 1D (1D = daily candles; use for slow swing strategies; cooldown_bars on 1D counts DAYS)\n"
        "- Only indicators from the list above — do not invent names or syntax\n"
        "- Output raw JSON only — no markdown fences, no explanations outside JSON\n"
        "- All numeric fields must be numbers, not strings\n"
    )


class AIStrategyClient:
    def __init__(self, cfg: AIProviderConfig):
        self.cfg = cfg
        self.session = requests.Session()
        if cfg.api_key:
            self.session.headers.update({"Authorization": f"Bearer {cfg.api_key}"})

    def construct_strategy(self, vibe: str, constraints: Dict[str, Any], provider: Optional[str] = None, model: Optional[str] = None) -> Dict[str, Any]:
        prompt = build_strategy_prompt(vibe, constraints)
        provider = provider or self.cfg.provider
        model = model or self.cfg.model
        try:
            raw = self._call_provider(prompt, provider=provider, model=model)
        except AIProviderError:
            raise
        except Exception as exc:
            raise AIProviderError(f"ارتباط با مدل برقرار نشد: {type(exc).__name__}: {str(exc)[:200]}") from exc
        data = self._parse_json_loose(raw)
        data.setdefault("provider", provider)
        data.setdefault("model", model)
        self._validate_artifact(data)
        return data

    def _parse_json_loose(self, raw: str) -> Dict[str, Any]:
        data = self._parse_json_loose_inner(raw)
        # FIX(minor): a valid-but-not-dict response (array/string) previously
        # crashed with AttributeError → HTTP 500; surface a clean 502 instead.
        if not isinstance(data, dict):
            raise AIProviderError(
                f"AI returned valid JSON but not an object (got {type(data).__name__}). "
                "Prompt must yield a single strategy artifact object."
            )
        return data

    def _parse_json_loose_inner(self, raw: str):
        # original loose parser, plus explicit truncation diagnosis
        import re as _re
        txt = (raw or "").strip()
        # strip markdown fences if present
        m = _re.search(r"```(?:json)?\s*([\s\S]*?)```", txt)
        if m:
            txt = m.group(1).strip()
        try:
            return json.loads(txt)
        except json.JSONDecodeError:
            pass
        # outermost-brace grab
        start = txt.find("{")
        if start == -1:
            raise AIProviderError(f"AI returned invalid JSON: raw={raw[:300]}")
        depth = 0
        end = -1
        for i in range(start, len(txt)):
            if txt[i] == "{":
                depth += 1
            elif txt[i] == "}":
                depth -= 1
                if depth == 0:
                    end = i
                    break
        if end == -1:
            # unbalanced braces = the output was CUT mid-artifact
            raise AIProviderError(
                "AI output truncated mid-JSON (unbalanced braces, no closing '}'). "
                "Raise max_tokens or simplify the description."
            )
        frag = txt[start : end + 1]
        return json.loads(frag)

    @staticmethod
    def _base_candidates(base_url: str) -> List[str]:
        """Candidate API roots generated from whatever the user typed.

        Users paste all shapes: `api.b.ai`, `https://api.b.ai/v1/`,
        `https://api.b.ai/v1/api/v1`, `https://api.b.ai/models/v1`, … Instead
        of trusting one normalization, generate an ordered candidate list and
        let discovery/test try each until one answers correctly."""
        raw = (base_url or "").strip()
        if not raw:
            return []
        if not raw.startswith(("http://", "https://")):
            raw = "https://" + raw
        raw = raw.rstrip("/")
        # scheme + host (+ optional port), ignore any junk path the user typed
        m = re.match(r"^(https?://[^/]+)(/.*)?$", raw)
        host = m.group(1) if m else raw
        path = (m.group(2) or "") if m else ""
        path = path.rstrip("/")

        cands: List[str] = []
        # 1) exactly what the user typed when it carries a path (maybe correct)
        if path:
            cands.append(host + path)
        # 2) host + /v1 — the OpenAI-compatible standard (first for bare hosts)
        cands.append(host + "/v1")
        # 3) host as-is (ollama/vllm style where /v1 is added per-endpoint)
        cands.append(host)
        # 4) common alternates
        cands.append(host + "/api/v1")
        cands.append(host + "/api")
        # 5) the user's path with /v1 appended — only when the path does not
        #    already embed a version segment (mangles like /v1/api/v1 are junk)
        if path and "v1" not in path and path not in ("/api",):
            cands.append(host + path + "/v1")
        # dedupe, preserve order
        seen: set = set()
        out: List[str] = []
        for c in cands:
            c = c.rstrip("/")
            if c and c not in seen:
                seen.add(c)
                out.append(c)
        return out

    def _normalize_base_url(self, base_url: str) -> str:
        """Normalize a user-entered base URL: strip trailing slashes and any
        trailing /v1 (we add version paths ourselves)."""
        u = (base_url or "").strip().rstrip("/")
        if u.endswith("/v1"):
            u = u[: -3]
        return u

    def _diag_http_error(self, resp) -> str:
        """Human-readable diagnosis for model-discovery / connection failures."""
        code = resp.status_code
        body = (resp.text or "")[:300]
        if code == 401:
            return "کلید API نامعتبر است (HTTP 401 — unauthorized). کلید را بررسی کنید."
        if code == 403:
            if "telegram" in body.lower():
                return "این سرویس نیاز به اتصال حساب تلگرام دارد (HTTP 403). در سایت سرویس به Settings/Bind Telegram بروید و دوباره تلاش کنید."
            return "دسترسی رد شد (HTTP 403). کلید یا پلن حساب اجازه دسترسی به این endpoint را ندارد."
        if code == 404:
            return "آدرس endpoint پیدا نشد (HTTP 404). آدرس base URL را بررسی کنید (مثال درست: https://api.example.com یا https://api.example.com/v1)."
        if code == 429:
            return "محدودیت نرخ درخواست (HTTP 429). چند لحظه بعد دوباره تلاش کنید."
        if code >= 500:
            return f"خطای سرور سرویسدهنده (HTTP {code}). سرویس موقتا در دسترس نیست."
        return f"HTTP {code}: {body}"

    def test_connection(self, provider: str, base_url: str, api_key: str) -> dict:
        """Connectivity probe across ALL plausible API-root candidates (user
        req: wrong/mangled URLs like /v1/api/v1 get auto-corrected by trying
        the combination dictionary). Returns {ok, latency_ms, step, detail,
        base_url_used}."""
        import time as _t
        provider = provider.lower()
        candidates = self._base_candidates(base_url)
        if not candidates:
            return {"ok": False, "latency_ms": 0, "step": "connect",
                    "detail": "آدرس API خالی است — آدرس پایه را وارد کنید"}
        headers = {}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        last = {"ok": False, "latency_ms": 0, "step": "connect",
                "detail": "no candidates", "base_url_used": ""}
        for cand in candidates:
            url = cand + "/models"
            t0 = _t.time()
            try:
                resp = self.session.get(url, headers=headers,
                                        timeout=max(10, self.cfg.timeout_sec // 2))
            except Exception as exc:
                last = {"ok": False, "latency_ms": 0, "step": "connect",
                        "detail": f"اتصال برقرار نشد: {type(exc).__name__}: {str(exc)[:200]}",
                        "base_url_used": cand}
                continue
            latency = int((_t.time() - t0) * 1000)
            if resp.status_code == 200 and self._looks_like_model_json(resp):
                # FIX(openrouter): a Management/Provisioning key lists models
                # but CANNOT generate — warn at save time, not at first use.
                if "openrouter" in cand.lower():
                    try:
                        kr = self.session.get(cand + "/key", headers=headers, timeout=10)
                        kd = kr.json().get("data", {}) if kr.status_code == 200 else {}
                        if kd.get("is_management_key") or kd.get("is_provisioning_key"):
                            return {"ok": False, "latency_ms": latency, "step": "auth",
                                    "detail": "این کلید OpenRouter از نوع Management/Provisioning است و "
                                              "نمیتواند متن تولید کند — یک «API Key» معمولی بسازید. "
                                              "(This is an OpenRouter Management/Provisioning key — "
                                              "create a regular API Key instead.)",
                                    "base_url_used": cand}
                    except Exception:
                        pass
                return {"ok": True, "latency_ms": latency, "step": "auth",
                        "detail": f"اتصال و احراز هویت موفق ({latency}ms) — آدرس: {cand}",
                        "base_url_used": cand}
            if resp.status_code == 401 and api_key:
                # endpoint EXISTS but key rejected → this is the right URL
                return {"ok": False, "latency_ms": latency, "step": "auth",
                        "detail": self._diag_http_error(resp), "base_url_used": cand}
            last = {"ok": False, "latency_ms": latency, "step": "auth",
                    "detail": self._diag_http_error(resp), "base_url_used": cand}
        return last

    @staticmethod
    def _looks_like_model_json(resp) -> bool:
        """True when the response parses as an OpenAI-style model list —
        guards against HTML error pages that return 200."""
        try:
            data = resp.json()
        except Exception:
            return False
        if isinstance(data, dict):
            return isinstance(data.get("data"), list) or isinstance(data.get("models"), list)
        return isinstance(data, list)

    def model_context_len(self, provider: str, base_url: str, api_key: str, model: str) -> dict:
        """Best-effort CONTEXT LENGTH probe for a model (local providers mostly).

        - Ollama: POST /api/show {model} → context_length in model_info.
        - LM Studio / vLLM / OpenAI-compatible: GET /models carries
          context_length / max_model_len / context_window on some servers.
        Returns {ok, context_len, source, detail}; ok=False means unknown —
        the caller then warns generically instead of blocking.
        """
        provider = (provider or "").lower()
        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        model = (model or "").strip()
        if not model:
            return {"ok": False, "context_len": 0, "source": "", "detail": "no model selected"}
        # 1) Ollama native /api/show (richest)
        if provider == "ollama":
            base = self._normalize_base_url(base_url).replace("/v1", "")
            try:
                r = self.session.post(base.rstrip("/") + "/api/show",
                                      json={"model": model}, headers=headers,
                                      timeout=max(10, self.cfg.timeout_sec // 2))
                if r.status_code == 200:
                    d = r.json()
                    mi = d.get("model_info") or {}
                    for k, v in mi.items():
                        if k.endswith("context_length"):
                            try:
                                return {"ok": True, "context_len": int(v), "source": "ollama:/api/show",
                                        "detail": f"{model}: context {int(v):,} tokens"}
                            except (TypeError, ValueError):
                                pass
                    # parameters string sometimes holds num_ctx
                    params = str(d.get("parameters") or "")
                    import re as _re
                    mnum = _re.search(r"num_ctx\s*=\s*(\d+)", params)
                    if mnum:
                        return {"ok": True, "context_len": int(mnum.group(1)),
                                "source": "ollama:num_ctx", "detail": f"{model}: num_ctx {mnum.group(1)}"}
                    return {"ok": False, "context_len": 0, "source": "ollama:/api/show",
                            "detail": "model_info had no context_length key"}
            except Exception as exc:
                return {"ok": False, "context_len": 0, "source": "ollama:/api/show",
                        "detail": f"probe failed: {type(exc).__name__}"}
        # 2) OpenAI-compatible /models metadata (LM Studio / vLLM / custom)
        for cand in self._base_candidates(base_url):
            try:
                r = self.session.get(cand + "/models", headers=headers,
                                     timeout=max(8, self.cfg.timeout_sec // 3))
                if r.status_code != 200:
                    continue
                d = r.json()
                rows = (d.get("data") if isinstance(d, dict) else None) or d.get("models") or (d if isinstance(d, list) else [])
                for item in rows:
                    if not isinstance(item, dict):
                        continue
                    mid = str(item.get("id") or item.get("name") or "")
                    if mid != model:
                        continue
                    for key in ("context_length", "max_model_len", "context_window",
                                "max_context_length", "max_sequence_length"):
                        if item.get(key):
                            try:
                                return {"ok": True, "context_len": int(item[key]),
                                        "source": f"{key}@/models", "detail": f"{model}: context {int(item[key]):,} tokens"}
                            except (TypeError, ValueError):
                                pass
            except Exception:
                continue
        return {"ok": False, "context_len": 0, "source": "", "detail": "context length unknown for this server"}

    def discover_models(self, provider: str, base_url: str, api_key: str, default_model: str) -> List[str]:
        provider = provider.lower()
        if provider not in {"ollama", "lmstudio", "vllm", "openai", "custom"}:
            raise AIProviderError(f"Unsupported provider for model discovery: {provider}")
        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        candidates = self._base_candidates(base_url)
        last_diag = ""
        for cand in candidates:
            url = cand + "/models"
            try:
                resp = self.session.get(url, headers=headers, timeout=self.cfg.timeout_sec)
            except Exception as exc:
                last_diag = f"اتصال به سرور AI برقرار نشد ({cand}): {type(exc).__name__}: {str(exc)[:150]}"
                continue
            if resp.status_code == 401 and api_key:
                # right endpoint, wrong key — no point trying other URLs
                raise AIProviderError(self._diag_http_error(resp))
            if resp.status_code != 200:
                last_diag = self._diag_http_error(resp)
                continue
            try:
                data = resp.json()
            except Exception:
                last_diag = f"پاسخ نامعتبر از سرور ({cand}) — JSON قابل خواندن نبود"
                continue
            models: List[str] = []
            if isinstance(data, dict):
                rows = data.get("data") or data.get("models") or []
            elif isinstance(data, list):
                rows = data
            else:
                rows = []
            for item in rows:
                if isinstance(item, dict):
                    mid = item.get("id") or item.get("name") or item.get("model")
                    if mid:
                        models.append(str(mid))
                elif isinstance(item, str):
                    models.append(item)
            if models:
                return models[:200]
            last_diag = f"پاسخ 200 ولی فهرست مدل خالی بود ({cand})"
        if last_diag:
            raise AIProviderError(last_diag)
        raise AIProviderError("هیچ آدرس کاندید پاسخ نداد — آدرس پایه را بررسی کنید")

    def _call_provider(self, prompt: str, provider: Optional[str] = None, model: Optional[str] = None,
                       max_tokens: Optional[int] = None) -> str:
        provider = provider or self.cfg.provider
        model = model or self.cfg.model
        url, payload, headers = self._build_request(provider, prompt, model=model)
        if max_tokens:
            payload["max_tokens"] = max_tokens
        # free routers often need more than 2 tries; drop the connection between
        # attempts so a half-closed keep-alive socket can't poison the retry
        max_attempts = max(self.cfg.retries, 3)
        last_err: Optional[Exception] = None
        _t0 = time.time()
        _url_host = _url_host_of(url)
        for attempt in range(1, max_attempts + 1):
            try:
                log.info("AI provider %s (%s) attempt %d/%d", provider, model, attempt, max_attempts)
                _a0 = time.time()
                self.session.close()   # fresh socket each attempt
                text, finish_reason = self._post_generation(url, payload, headers)
                _lat = int((time.time() - _a0) * 1000)
                _ai_log({"event": "ok", "provider": provider, "model": model,
                         "base_url": _url_host, "attempt": attempt,
                         "stream": bool(payload.get("stream")),
                         "finish_reason": finish_reason,
                         "latency_ms": _lat, "elapsed_ms": int((time.time() - _t0) * 1000),
                         "resp_chars": len(text)})
                return text
            except AIProviderError as exc:
                # finish_reason=length is DETERMINISTIC (the model hit its cap
                # on this exact prompt) — retrying wastes minutes and can never
                # succeed. Fail immediately with the actionable diagnosis.
                if "finish_reason=length" in str(exc) or "truncated" in str(exc).lower():
                    _ai_log({"event": "fatal", "provider": provider, "model": model,
                             "base_url": _url_host, "attempt": attempt,
                             "reason": "truncated", "error": str(exc)[:300]})
                    raise
                # AUTH errors (401/403) are deterministic too — the key is
                # rejected, no number of retries fixes it. Fail fast with a
                # clear, actionable message instead of "failed after 3 attempts"
                # over a cryptic raw body (MEXC wizard: user saw
                # 'HTTP 401 {"error":{"message":"User not found"}}' x3).
                m_auth = re.search(r"HTTP (401|403):", str(exc))
                if m_auth:
                    code = m_auth.group(1)
                    # FIX(openrouter): a MANAGEMENT/PROVISIONING key passes /key
                    # but CANNOT generate — OpenRouter answers 401
                    # {"error":{"message":"User not found"}}. Tell the user
                    # exactly which key type to create instead of a generic
                    # "key rejected".
                    if "User not found" in str(exc) or "user not found" in str(exc).lower():
                        hint = ("این کلید OpenRouter از نوع Management/Provisioning است و نمیتواند "
                                "متن تولید کند — در OpenRouter به Keys بروید و یک «API Key» معمولی "
                                "بسازید و همان را وارد کنید. "
                                "(This OpenRouter key is a Management/Provisioning key which cannot "
                                "generate text — create a regular API Key in OpenRouter's Keys page "
                                "and use that instead.)")
                    else:
                        hint = ("کلید API رد شد (HTTP 401) — کلید را بررسی کنید: "
                                "شاید برای این آدرس/پلن درست نباشد یا حساب هنوز فعال نشده. "
                                "همین خطا یعنی مشکل از کلید است، نه اتصال. "
                                "(The API key was rejected — it's not valid for this "
                                "base URL / plan, or the account isn't activated. "
                                "This is a key problem, not a connection problem.)")
                    raise AIProviderError(hint) from exc
                # HTTP-level errors: retry, but keep the informative message
                last_err = exc
                _ai_log({"event": "attempt_failed", "provider": provider, "model": model,
                         "base_url": _url_host, "attempt": attempt,
                         "latency_ms": int((time.time() - _a0) * 1000),
                         "http": code if (m_auth) else None, "error": str(exc)[:300]})
                log.warning("AI provider attempt %d failed: %s", attempt, exc)
                if attempt < max_attempts:
                    time.sleep(self.cfg.retry_pause_sec * attempt)  # linear backoff 2s, 4s, ...
            except Exception as exc:
                last_err = exc
                _ai_log({"event": "attempt_failed", "provider": provider, "model": model,
                         "base_url": _url_host, "attempt": attempt,
                         "latency_ms": int((time.time() - _a0) * 1000),
                         "exc_type": type(exc).__name__, "error": str(exc)[:300]})
                log.warning("AI provider attempt %d failed: %s", attempt, exc)
                if attempt < max_attempts:
                    time.sleep(self.cfg.retry_pause_sec * attempt)
        _ai_log({"event": "failed", "provider": provider, "model": model,
                 "base_url": _url_host, "attempts": max_attempts,
                 "elapsed_ms": int((time.time() - _t0) * 1000),
                 "error": str(last_err)[:300]})
        raise AIProviderError(f"AI provider failed after {max_attempts} attempts: {last_err}")

    def _post_generation(self, url: str, payload: dict, headers: dict) -> tuple:
        """POST /chat/completions (streaming or not) → (text, finish_reason).

        Streaming is the fix for Cloudflare-fronted routers (e.g.
        router.bynara.id): a NON-streaming request whose upstream generation
        takes >~30s gets its connection dropped before a single byte is sent
        ('Remote end closed connection without response'). Streaming makes the
        origin emit SSE chunks as tokens are produced, so bytes flow
        continuously and the proxy never sees an idle connection. A numeric
        `timeout` on the session is then a *per-chunk gap* limit, not a
        total-request limit.
        """
        streaming = bool(payload.get("stream"))
        resp = self.session.post(url, json=payload, headers=headers,
                                 timeout=self.cfg.timeout_sec, stream=True)
        try:
            if resp.status_code != 200:
                body = (resp.text or "")[:300]
                raise AIProviderError(f"AI provider returned HTTP {resp.status_code}: {body}")
            finish_reason = None
            if streaming:
                parts: List[str] = []
                for raw_line in resp.iter_lines(decode_unicode=True):
                    if not raw_line:
                        continue
                    line = raw_line.strip()
                    if not line.startswith("data:"):
                        continue
                    data = line[len("data:"):].strip()
                    if data == "[DONE]":
                        break
                    try:
                        chunk = json.loads(data)
                    except json.JSONDecodeError:
                        continue
                    choices = chunk.get("choices") or []
                    if not choices:
                        continue
                    c0 = choices[0]
                    if c0.get("finish_reason"):
                        finish_reason = c0["finish_reason"]
                    piece = (c0.get("delta") or {}).get("content")
                    if piece:
                        parts.append(piece)
                text = "".join(parts)
            else:
                data = resp.json()
                choice = data["choices"][0]
                finish_reason = choice.get("finish_reason")
                text = choice["message"]["content"]
        finally:
            try:
                resp.close()
            except Exception:
                pass
        if finish_reason == "length":
            err = AIProviderError(
                "AI output truncated (finish_reason=length, max_tokens cap hit). "
                "Simplify the strategy description or use a model with a larger output budget.")
            # Expose the partial text so callers (the wizard) can attempt to
            # salvage a near-complete profile instead of throwing it away.
            err.partial_text = text  # type: ignore[attr-defined]
            raise err
        return text, finish_reason

    def _build_request(self, provider: str, prompt: str, model: Optional[str] = None):
        model = model or self.cfg.model
        # FIX(#13): generation calls MUST go through _normalize_base_url like
        # test_connection/discover_models — a '/v1'-suffixed base URL otherwise
        # produces '<base>/v1/v1/chat/completions' 404s only at generation.
        if provider in ("ollama", "lmstudio", "vllm"):
            base = self._normalize_base_url(self.cfg.base_url)
            url = f"{base}/v1/chat/completions"
            payload = {
                "model": model,
                "messages": [
                    {"role": "system", "content": "You are a strict JSON-generating trading strategy constructor. Output only JSON."},
                    {"role": "user", "content": prompt},
                ],
                "temperature": 0.2,
                "max_tokens": GENERATION_MAX_TOKENS,
                "stream": self.cfg.stream,
            }
            return url, payload, {"Content-Type": "application/json"}
        if provider == "openai":
            # FIX(ai-fill): an explicit non-default base_url (OpenAI-
            # compatible router) must be honored; the hardcoded official
            # endpoint only applies when no override was configured.
            _bu = (self.cfg.base_url or "").strip()
            if _bu and "api.openai.com" not in _bu:
                url = self._normalize_base_url(_bu) + "/chat/completions"
            else:
                url = "https://api.openai.com/v1/chat/completions"
            payload = {
                "model": model,
                "messages": [
                    {"role": "system", "content": "Output only valid JSON strategy artifact."},
                    {"role": "user", "content": prompt},
                ],
                "temperature": 0.2,
                "max_tokens": GENERATION_MAX_TOKENS,
                "stream": self.cfg.stream,
            }
            return url, payload, {"Content-Type": "application/json"}
        if provider == "custom":
            # honor the base-URL candidate dictionary: a mangled root is
            # resolved against /chat/completions the same way discovery is.
            base = self._resolve_generation_base(self.cfg.base_url)
            url = base + "/chat/completions"
            payload = {
                "model": model,
                "messages": [
                    {"role": "system", "content": "Output only valid JSON strategy artifact."},
                    {"role": "user", "content": prompt},
                ],
                "temperature": 0.2,
                "max_tokens": GENERATION_MAX_TOKENS,
                "stream": self.cfg.stream,
            }
            return url, payload, {"Content-Type": "application/json"}
        raise AIProviderError(f"Unsupported AI provider: {provider}")

    def _resolve_generation_base(self, base_url: str) -> str:
        """Pick the API root for /chat/completions using the same candidate
        dictionary as discovery: probe each root with a HEAD on /models and
        return the first root that answers like an OpenAI-compatible API.
        Falls back to the standard host/v1 normalization."""
        import time as _t
        cands = self._base_candidates(base_url)
        if not cands:
            return self._normalize_base_url(base_url)
        for cand in cands:
            try:
                r = self.session.get(cand + "/models", timeout=8)
                if r.status_code in (200, 401, 403):
                    return cand   # endpoint exists (auth state is a later problem)
            except Exception:
                continue
        # nothing answered within the quick probe — standard normalization
        return self._normalize_base_url(base_url)

    def _extract_text(self, provider: str, resp: requests.Response) -> str:
        data = resp.json()
        try:
            choice = data["choices"][0]
            # FIX(#14): a 'length' finish means the JSON was cut mid-artifact —
            # fail fast with a precise diagnosis instead of parsing a fragment.
            if str(choice.get("finish_reason", "")).lower() == "length":
                raise AIProviderError(
                    "AI output truncated (finish_reason=length, max_tokens cap hit). "
                    "Simplify the strategy description or use a model with a larger output budget."
                )
            return choice["message"]["content"]
        except AIProviderError:
            raise
        except (KeyError, IndexError) as exc:
            raise AIProviderError(f"Unexpected AI response shape: {data}") from exc

    def _validate_artifact(self, data: Dict[str, Any]) -> None:
        required = [
            "schema_version",
            "strategy_id",
            "name",
            "source",
            "provider",
            "model",
            "timeframe",
            "entry_conditions",
            "exit_conditions",
            "risk",
        ]
        missing = [k for k in required if k not in data]
        if missing:
            raise AIProviderError(f"Strategy artifact missing required fields: {missing}")
        from .strategy_schema import ALLOWED_TIMEFRAMES
        tf = str(data.get("timeframe", ""))
        if tf not in ALLOWED_TIMEFRAMES:
            raise AIProviderError(f"Invalid timeframe: {tf}. Only {'/'.join(ALLOWED_TIMEFRAMES)} allowed.")
        risk = data.get("risk", {})
        if not isinstance(risk, dict):
            raise AIProviderError("Risk block must be a dict")
        for k in ("max_positions", "risk_per_trade_pct", "stop_atr_mult", "target_atr_mult"):
            if k not in risk or not isinstance(risk[k], (int, float)) or risk[k] <= 0:
                raise AIProviderError(f"Invalid risk.{k}: {risk.get(k)}")

    # ── AI OPTIMIZER: backtest-results → improved strategy branch ──
    def optimize_strategy(
        self,
        base_artifact: Dict[str, Any],
        backtest_metrics: Dict[str, Any],
        sample_trades: List[Dict[str, Any]],
        symbols: List[str],
        mode: str,
        provider: Optional[str] = None,
        model: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Send the base strategy + its backtest results to the AI and get an
        OPTIMIZED variant (new strategy_id branch). The AI may change parameters
        (risk, TF, cooldown, confirmations) or re-architect entry/exit conditions,
        but must stay inside the framework schema."""
        summary = self._summarize_backtest(base_artifact, backtest_metrics, sample_trades, symbols, mode)
        prompt = self._build_optimizer_prompt(base_artifact, summary)
        raw = self._call_provider(prompt, provider=provider, model=model)
        data = self._parse_json_loose(raw)
        data.setdefault("provider", provider or self.cfg.provider)
        data.setdefault("model", model or self.cfg.model)
        self._validate_artifact(data)
        # guarantee a NEW branch id derived from the base, and lineage metadata
        base_id = str(base_artifact.get("strategy_id") or "strategy")
        new_id = str(data.get("strategy_id") or "").strip()
        if not new_id or new_id == base_id or new_id == "legacy":
            new_id = self._branch_id(base_id, data.get("name", ""))
        data["strategy_id"] = new_id
        data["source"] = "ai_optimized"
        data["optimized_from"] = base_id
        data["parent_metrics"] = {
            k: backtest_metrics.get(k)
            for k in ("trades", "return_pct", "win_rate", "profit_factor", "max_drawdown_pct", "expectancy")
            if k in backtest_metrics
        }
        return data

    @staticmethod
    def _branch_id(base_id: str, name: str = "") -> str:
        import re as _re
        import time as _time
        slug = _re.sub(r"[^a-z0-9]+", "-", (name or "opt").lower()).strip("-")[:24] or "opt"
        return f"{base_id}-opt-{slug}-{int(_time.time()) % 100000}"[:80]

    @staticmethod
    def _summarize_backtest(artifact, metrics, trades, symbols, mode) -> str:
        """Compact, information-dense summary the model can reason over."""
        lines = [
            f"mode={mode} symbols={','.join(symbols[:8])}",
            f"metrics: trades={metrics.get('trades', 0)} return%={metrics.get('return_pct', 0)} "
            f"win_rate={metrics.get('win_rate', 0)}% PF={metrics.get('profit_factor', 0)} "
            f"maxDD%={metrics.get('max_drawdown_pct', 0)} expectancy={metrics.get('expectancy', 0)}",
        ]
        # exit reason distribution → tells the model WHERE the strategy bleeds
        reasons: Dict[str, int] = {}
        for t in trades or []:
            r = str(t.get("exit_reason", "?"))
            reasons[r] = reasons.get(r, 0) + 1
        if reasons:
            lines.append("exit_reasons: " + ", ".join(f"{k}={v}" for k, v in sorted(reasons.items(), key=lambda kv: -kv[1])))
        # per-trade R and hold time stats
        rrs = [float(t.get("realized_rr", 0) or 0) for t in trades or []]
        if rrs:
            import statistics as _st
            lines.append(f"R multiples: mean={_st.mean(rrs):.2f} median={_st.median(rrs):.2f} best={max(rrs):.2f} worst={min(rrs):.2f}")
        holds = [int(t.get("hold_seconds", 0) or 0) for t in trades or []]
        if holds:
            import statistics as _st
            lines.append(f"hold hours: mean={_st.mean(holds)/3600:.1f} median={_st.median(holds)/3600:.1f}")
        tf = artifact.get("timeframe")
        lines.append(f"current: tf={tf} exec={artifact.get('execution_mode')} min_conf={artifact.get('min_confirmations')} "
                     f"risk%={artifact.get('risk', {}).get('risk_per_trade_pct')} stop_atr={artifact.get('risk', {}).get('stop_atr_mult')} "
                     f"tp_atr={artifact.get('risk', {}).get('target_atr_mult')}")
        # sample trades (most recent 8)
        for t in (trades or [])[-8:]:
            lines.append(f"trade {t.get('symbol')}: entry={t.get('entry')} exit={t.get('close_price')} "
                         f"R={t.get('realized_rr')} reason={t.get('exit_reason')}")
        return "\n".join(lines)

    def _build_optimizer_prompt(self, artifact: Dict[str, Any], summary: str) -> str:
        return (
            "You are an expert quantitative strategy OPTIMIZER for the Wallex trading bot.\n"
            "You receive: (1) an existing strategy artifact in the app's JSON framework, and (2) its "
            "backtest results (metrics, exit-reason distribution, R multiples, hold times, sample trades).\n"
            "Your job: diagnose WHY performance is weak and output ONE improved variant as a NEW JSON artifact.\n\n"
            "=== CURRENT STRATEGY ARTIFACT ===\n"
            f"{json.dumps(artifact, ensure_ascii=False, indent=1)}\n\n"
            "=== BACKTEST RESULTS ===\n"
            f"{summary}\n\n"
            "=== DIAGNOSIS GUIDE (map results to causes) ===\n"
            "- few trades (<10 over the window): entry too strict → lower min_confirmations by 1, or drop the weakest condition, or shorten timeframe one step (1D→240→60→15)\n"
            "- many trades + low win_rate (<35%): entries too loose → add a trend filter (adx > 20-25, or ema price-compare in trend direction)\n"
            "- high maxDD: stops too wide or risk too high → reduce risk_per_trade_pct (1.0→0.5), widen stop_atr_mult if stopped out early, add cooldown_bars\n"
            "- exit_reason 'stop' dominates: stop_atr_mult too tight → try 1.5→2.0, or add volume/ATR filter to avoid entering in chop\n"
            "- exit_reason 'target' dominates but RR low: target_atr_mult too small → raise 3.0→4.0, or trail with ema\n"
            "- exit_reason 'choch'/'structure' dominates: exits fine, entries bad → improve entry timing (stochastic_rsi/cci oversold in uptrend)\n"
            "- expectancy near zero or negative with decent WR: fees/slippage eat it → fewer trades (higher min_confirmations, longer cooldown) or bigger targets\n"
            "- liquidations present (margin): risk_coef/max_positions too aggressive → cut both\n"
            "Pick the ONE or TWO highest-impact changes — do not rewrite everything at once.\n\n"
            "=== OUTPUT: ONLY the improved JSON artifact (same schema as input) ===\n"
            "- strategy_id: NEW unique kebab id — the base id + '-opt' style suffix (it will be saved as a NEW branch, parent is kept)\n"
            "- name: short Persian/English name marking it as the improved variant\n"
            "- description: 1-3 sentences IN PERSIAN explaining WHAT you changed and WHY (based on the results)\n"
            "- keep the same schema_version/timeframe rules/indicator DSL as the framework (15/60/240/1D only, listed indicators only)\n"
            "- change parameters and/or conditions — but every condition must use the exact DSL of the input artifact\n"
            "- Output raw JSON only — no markdown, no commentary outside the JSON\n"
        )

    # ── VERIFICATION LOOP support: regenerate from the ORIGINAL user vibe ──
    def regenerate_from_vibe(
        self,
        vibe: str,
        base_artifact: Dict[str, Any],
        attempts_summary: str,
        constraints: Dict[str, Any],
        provider: Optional[str] = None,
        model: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Round 2 of optimization: parameter tweaks failed → go back to the
        user's ORIGINAL vibe description and re-interpret it from scratch.
        The AI sees what was already tried and why it failed, then produces a
        fresh strategy that captures the user's INTENT better."""
        prompt = self._build_regenerate_prompt(vibe, base_artifact, attempts_summary, constraints)
        raw = self._call_provider(prompt, provider=provider, model=model)
        data = self._parse_json_loose(raw)
        data.setdefault("provider", provider or self.cfg.provider)
        data.setdefault("model", model or self.cfg.model)
        self._validate_artifact(data)
        base_id = str(base_artifact.get("strategy_id") or "strategy")
        new_id = str(data.get("strategy_id") or "").strip()
        if not new_id or new_id == base_id or new_id == "legacy":
            new_id = self._branch_id(base_id, data.get("name", "re"))
        data["strategy_id"] = new_id
        data["source"] = "ai_regenerated"
        data["optimized_from"] = base_id
        data["regenerated_from_vibe"] = True
        return data

    def _build_regenerate_prompt(self, vibe: str, artifact: Dict[str, Any],
                                 attempts_summary: str, constraints: Dict[str, Any]) -> str:
        vibe = _smart_truncate_vibe(vibe)
        return (
            "You are an expert quantitative strategy ARCHITECT for the Wallex trading bot.\n"
            "A strategy was built from a user's description. Its backtest results were poor. "
            "Then an optimizer made small parameter/condition tweaks — and the backtest shows the tweaks "
            "did NOT fix it. Conclusion: the base INTERPRETATION of the user's idea is likely flawed, "
            "not just its parameters.\n"
            "Your job: go back to the user's ORIGINAL description, re-think the CORE IDEA, and build a "
            "FRESH strategy from scratch that captures the user's INTENT more effectively — different "
            "entry logic, possibly different timeframe or execution mode. Keep what clearly works, "
            "replace what clearly doesn't.\n\n"
            "=== ORIGINAL USER DESCRIPTION (the source of truth) ===\n"
            f"{vibe}\n\n"
            "=== PREVIOUS INTERPRETATION (the one that failed) ===\n"
            f"{json.dumps(artifact, ensure_ascii=False, indent=1)}\n\n"
            "=== WHAT WAS ALREADY TRIED AND THE RESULTS ===\n"
            f"{attempts_summary}\n\n"
            "=== APP CAPABILITIES (framework constraints — identical rules as the generator) ===\n"
            f"{json.dumps(constraints, ensure_ascii=False, indent=1)}\n\n"
            "=== THINKING GUIDE ===\n"
            "- Re-read the user's intent: what market behavior did they actually want to capture?\n"
            "- Compare with the failed interpretation: which part misread the intent? Which part matches?\n"
            "- If the failed version barely traded, the entry filter chain is wrong for the chosen timeframe.\n"
            "- If it traded a lot and lost, the signal is noise → demand stronger confirmation or a different regime filter.\n"
            "- Do NOT simply re-submit the previous artifact with tiny tweaks — that path is proven broken.\n\n"
            "=== OUTPUT: ONLY the new JSON artifact (framework schema, same rules) ===\n"
            "- strategy_id: NEW unique kebab id (base id + '-re' style suffix)\n"
            "- name + Persian description: explain your new interpretation of the user's idea and what you changed\n"
            "- timeframe 15/60/240/1D, execution_mode auto/grid/signal/tp_sl_dollar, exact indicator DSL only\n"
            "- Output raw JSON only — no markdown, no commentary outside the JSON\n"
        )
