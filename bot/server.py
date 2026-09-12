"""FastAPI server — REST API for the Persian RTL dashboard.

Security:
  - API keys are loaded from env, stored encrypted, NEVER sent to the client.
  - Live trading requires WALLEX_LIVE_ALLOWED=yes AND an explicit /api/live/enable
    call with a confirmation phrase. Default mode is PAPER.
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
import uuid
from pathlib import Path
from typing import List, Optional

import yaml
from fastapi import Body, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles

from .backtest import run_backtest, walk_forward, BacktestResult
from .broker import LiveBroker, LiveMarginBroker, PaperBroker, PaperMarginBroker
from .crypto_store import CryptoStore
from .engine import Engine
from .file_logger import FileLogger, set_active, log_error
from .grid_legacy import GridManager  # legacy grid strategies (spot+margin)

# Minimum model CONTEXT (tokens) for AI jobs that swallow whole config prompts
# (wizard profile extraction, strategy construct, optimizer). Below this, local
# models truncate (finish_reason=length) and the setup fails — warn the user.
_AI_CTX_MIN = 16_000
from .history import download_symbol, load_symbol_history, ensure_depth, depth_status
from .storage import Storage
from .wallex_client import WallexClient
from .wallex_rules import WallexRules
from .markets import MarketCatalog
from .quotes import QuoteService
from .ai_strategy import AIStrategyClient, AIProviderConfig, AIProviderError
from .strategy_schema import ALLOWED_INDICATORS, ALLOWED_TIMEFRAMES
from .strategy_store import StrategyStore

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger("server")

ROOT = Path(__file__).resolve().parent.parent


def load_config(path: Optional[str] = None) -> dict:
    p = Path(path or os.environ.get("BOT_CONFIG", ROOT / "config.yaml"))
    with open(p, encoding="utf-8") as f:
        return yaml.safe_load(f)


def _vibe_from_artifact(art: dict) -> str:
    """Auto-write a textbook vibe (strategy description) FROM an existing
    artifact — used when a strategy predates the vibe hook, so the optimizer's
    round-2 re-interpretation always has a source-of-truth description."""
    r = art.get("risk", {}) or {}
    conds = art.get("entry_conditions", []) or []
    exits = art.get("exit_conditions", []) or []

    def _fmt_cond(c: dict) -> str:
        ind = c.get("indicator", "?")
        op = c.get("operator", "")
        per = c.get("period")
        ptxt = f"({per})" if per else ""
        if c.get("compare") == "price":
            return f"قیمت نسبت به {ind}{ptxt} — عملگر {op}"
        val = c.get("value", "")
        return f"{ind}{ptxt} {op} {val}"

    exec_mode = art.get("execution_mode", "signal")
    exec_txt = {
        "grid": "اجرای شبکه‌ای (گرید) با پله‌های قیمت",
        "tp_sl_dollar": "حد سود/ضرر دلاری یا تومانی ثابت",
        "auto": "حالت اجرای خودکار — AI مناسب‌ترین شیوه را انتخاب میکند",
        "signal": "ورود سیگنالی با حد سود/ضرر مبتنی بر ATR",
    }.get(exec_mode, exec_mode)
    tf = art.get("timeframe", "60")
    tf_txt = {"15": "۱۵ دقیقه (اسکالپ/سریع)", "60": "۱ ساعته (میان‌مدت)", "240": "۴ ساعته (روندی/آرام)"}.get(tf, tf)
    lines = [
        f"استراتژی «{art.get('name', art.get('strategy_id', '?'))}» — بازتولید متنی خودکار از استراتژی موجود.",
        "",
        "## ایده اصلی",
        str(art.get("description", "") or "این استراتژی از روی یک استراتژی موجود بازتولید شده است."),
        "",
        f"## تایم‌فریم و اجرا",
        f"- تایم‌فریم: {tf_txt}",
        f"- شیوه اجرا: {exec_txt}",
        f"- حداقل تأییدیه‌های همزمان برای ورود: {art.get('min_confirmations', 1)}",
        f"- فاصله بین ورودها (cooldown): {art.get('cooldown_bars', 3)} کندل",
        "",
        "## شرایط ورود (هر کدام باید برقرار شود تا حد نصاب تأییدیه پر شود)",
    ]
    for c in conds:
        lines.append(f"- {_fmt_cond(c)}")
    lines.append("")
    lines.append("## شرایط خروج")
    for c in exits:
        lines.append(f"- {_fmt_cond(c)}")
    lines += [
        "",
        "## مدیریت ریسک",
        f"- حداکثر موقعیت‌های همزمان: {r.get('max_positions', 1)}",
        f"- ریسک هر معامله: {r.get('risk_per_trade_pct', 1.0)}٪ از موجودی",
        f"- حد ضرر: {r.get('stop_atr_mult', 1.5)} برابر ATR زیر قیمت ورود",
        f"- حد سود: {r.get('target_atr_mult', 3.0)} برابر ATR بالای قیمت ورود",
    ]
    gm = r.get("grid_mode", "none")
    if exec_mode == "grid" and gm and gm != "none":
        lines += [
            "",
            "## شبکه (گرید)",
            f"- جهت گرید: {gm} | فاصله پله‌ها: {r.get('grid_step_pct', 1.0)}٪ | حداکثر پله‌ها: {r.get('grid_max_steps', 5)}",
        ]
    lines += [
        "",
        "## هدف نهایی (برای بهینه‌ساز)",
        "این توضیح، خلاصه‌ای وفادار از استراتژی موجود است. هنگام بازتفسیر، روح ایده را حفظ کن اما اجازه داری منطق ورود/خروج را اثربخش‌تر بازطراحی کنی.",
    ]
    return "\n".join(lines)


def _user_job_lock_ttl(minutes: int = 15) -> None:
    """A+B hybrid: a USER job (backtest download/top-up) is now active — the
    background 15m-depth backfill must pause and yield Wallex to the user.
    The backfill script polls data/.user_job_busy and sleeps while fresh.
    The marker auto-expires after `minutes` (no stale permanent pause)."""
    try:
        import pathlib, time as _t
        p = pathlib.Path(DATA_DIR) / ".user_job_busy"
        p.write_text(f"{int(_t.time())}")
        log.info("[backfill-yield] user job started — background backfill paused up to %ss", minutes * 60)
    except Exception:
        pass


def create_app(cfg: Optional[dict] = None) -> FastAPI:
    cfg = cfg or load_config()
    # Multi-profile resolution (Phase 0): BOT_DATA_DIR (tests) > BOT_PROFILE
    # (data/profiles/<id>) > legacy ROOT/data. On the first profile-mode boot
    # the legacy data tree is copied into data/profiles/wallex/ (idempotent).
    from .exchange import registry as _reg
    if not os.environ.get("BOT_DATA_DIR"):
        try:
            _legacy_db = ROOT / "data" / "bot.db"
            _pid = os.environ.get("BOT_PROFILE", "").strip() or "wallex"
            # The legacy ROOT/data copy belongs to WALLEX only — a non-wallex
            # profile starts FRESH (its own strategies/history/keys), never a
            # copy of Wallex state (and must not flip the active profile).
            if _pid == "wallex" and _legacy_db.exists() and not (_reg.profiles_root() / _pid / "bot.db").exists():
                _mig = _reg.migrate_legacy_data(_pid)
                log.info("[profiles] legacy data migration: %s", _mig)
        except Exception as exc:
            log.warning("[profiles] legacy migration skipped: %s", exc)
        os.environ.setdefault("BOT_PROFILE", _pid)
    data_dir = str(_reg.resolve_data_dir())
    profile_id = _reg.resolve_profile_id()
    Path(data_dir).mkdir(parents=True, exist_ok=True)

    # FIX(audit-H1): the old fallback encrypted every stored API key under a
    # PUBLIC constant ("default-dev-password") — trivially decryptable by
    # anyone with file access. Now: env var if set, else a persisted RANDOM
    # key file (user-local). Existing stores encrypted under the legacy
    # constant are transparently MIGRATED (decrypted with the old password,
    # re-encrypted with the new one) so no user data is lost.
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
            log.warning(
                "WALLEX_KEY_PASSWORD not set — generated a random key password "
                "at %s. Set WALLEX_KEY_PASSWORD in your environment for proper "
                "at-rest security.", _pw_file)
    try:
        store = CryptoStore(data_dir, _key_pw)
        store._load()   # force decrypt check now, not on first get
    except RuntimeError:
        # Likely a pre-fix secrets.enc written under the legacy default.
        # Migrate: decrypt with the old password, re-encrypt with the new.
        try:
            _legacy = CryptoStore(data_dir, "default-dev-password")
            _data = _legacy._load()
            store = CryptoStore(data_dir, _key_pw)
            store._save(_data)
            log.warning(
                "secrets.enc migrated from the legacy default password to a "
                "per-install random key (%s). Set WALLEX_KEY_PASSWORD to pin it.",
                _P(data_dir) / ".key_password")
        except Exception:
            raise RuntimeError(
                "WALLEX_KEY_PASSWORD is wrong — cannot decrypt secrets.")
    key_material = os.environ.get("WALLEX_API_KEY") or store.get("wallex_api_key") or ""
    # Signed-key exchanges (e.g. Nobitex Ed25519) keep the PRIVATE seed as a
    # second credential, encrypted alongside the public key.
    key_secret = os.environ.get("WALLEX_API_SECRET") or store.get("wallex_api_secret") or ""
    # Phase 1: the client IS the exchange adapter (WallexAdapter subclasses
    # WallexClient — behavior-identical, but carries capabilities/quote
    # families/quirk map for the generic paths below).
    from .exchange.factory import build_adapter
    client = build_adapter(
        profile_id,
        key_material,
        min_gap_sec=float(cfg["engine"].get("api_min_gap_sec", 12)),
        max_retries=int(cfg["engine"].get("api_max_retries", 3)),
        retry_pause_sec=float(cfg["engine"].get("api_retry_pause_sec", 30)),
        redact_fn=store.redact,
        api_secret=key_secret,
    )
    storage = Storage(data_dir)
    strategy_store = StrategyStore(data_dir)

    # AI strategy client — configured from env/config, never hardcoded to one provider.
    ai_client = AIStrategyClient(
        AIProviderConfig(
            provider=os.environ.get("AI_PROVIDER", cfg.get("ai", {}).get("provider", "ollama")),
            base_url=os.environ.get("AI_BASE_URL", cfg.get("ai", {}).get("base_url", "http://localhost:11434")),
            api_key=os.environ.get("AI_API_KEY", cfg.get("ai", {}).get("api_key", "")),
            model=os.environ.get("AI_MODEL", cfg.get("ai", {}).get("model", "llama3")),
            timeout_sec=int(os.environ.get("AI_TIMEOUT", cfg.get("ai", {}).get("timeout_sec", 340))),
            retries=int(os.environ.get("AI_RETRIES", cfg.get("ai", {}).get("retries", 2))),
        )
    )

    # Live is authorized by a valid API key (proven by fetching the account
    # balance), NOT by a process-environment flag — so restarts/launchers never
    # re-block it. The key's validity is re-checked in _engage_live.
    live_allowed = bool(key_material)
    bcfg = cfg.get("backtest", {})
    mcfg = cfg.get("margin", {})
    markets = MarketCatalog(client=client, cache_path=Path(data_dir) / "markets_cache.json")
    quotes = QuoteService(client=client)

    def _paper_capital(kind: str, quote: str = "") -> float:
        """Persisted paper balance per sub-mode AND currency, falling back to config."""
        default = float(mcfg.get("starting_capital", bcfg.get("starting_capital", 10000)))
        q = (quote or "").upper()
        raw = storage.kv_get(f"paper_balance_{kind}_{q}") if q else None
        if raw is None:
            # one-time migrate from the legacy per-kind key
            raw = storage.kv_get(f"paper_balance_{kind}")
        try:
            return float(raw) if raw else default
        except (TypeError, ValueError):
            return default

    def _paper_quote(kind: str) -> str:
        """Persisted paper quote currency per sub-mode, validated against the
        ADAPTER's quote families (Wallex: USDT/TMN; other exchanges: their own)."""
        raw = storage.kv_get(f"paper_quote_{kind}")
        q = (raw or "USDT").upper()
        fams = [x.upper() for x in client.quote_currencies] or ["USDT"]
        return q if q in fams else (fams[0] if fams else "USDT")

    def _symbol_universe(quote: str) -> List[str]:
        """Symbols for the chart dropdown AND engine scan:
        the user's master selection ∩ quote family; if no selection,
        config.yaml defaults (never the full Wallex catalog)."""
        try:
            raw = storage.kv_get("symbols_master")
        except (TypeError, ValueError):
            raw = None
        if raw:
            try:
                master = json.loads(raw)
                sel = [s for s in master if s and client.quote_of(s) == quote]
                if sel:
                    return sel
            except (TypeError, ValueError):
                pass
        # Fallback to config.yaml defaults instead of the full Wallex catalog.
        try:
            defaults = [s for s in cfg.get("symbols", []) if client.quote_of(s) == quote]
            if defaults:
                return defaults
        except (TypeError, ValueError):
            pass
        return []

    def _active_profile() -> dict:
        """The active exchange's profile.json (rules source for paper mode);
        {} when the adapter is hand-coded (Wallex) — rules fall back to config."""
        try:
            from .exchange import profile as _P
            _prof = _P.load_profile(profile_id, ROOT / "data" / "profiles")
            return _prof or {}
        except Exception:
            return {}

    def _paper_rules() -> "WallexRules":
        """Paper-mode order rules for the ACTIVE exchange: the profile's own
        `rules` block (per-exchange, AI-verified) with the global config.yaml
        (Wallex-era) as fallback. Makes paper spot/margin respect each
        exchange's live-mode limits instead of Wallex's."""
        prof = _active_profile()
        return WallexRules.from_profile(prof, cfg) if prof.get("rules") else WallexRules.from_config(cfg)

    def _paper_fee_pct() -> float:
        """Per-exchange fee (profile `rules.fee_pct`) for paper fills/dry-run
        previews; falls back to the global backtest fee."""
        prof = _active_profile()
        try:
            return float((prof.get("rules") or {}).get("fee_pct", bcfg.get("fee_pct", 0.2)))
        except (TypeError, ValueError):
            return float(bcfg.get("fee_pct", 0.2))

    def _make_paper_spot(quote: str = "USDT") -> PaperBroker:
        return PaperBroker(
            starting_capital=_paper_capital("spot", quote),
            fee_pct=_paper_fee_pct(),
            slippage_pct=float(bcfg.get("slippage_pct", 0.05)),
            rules=_paper_rules(),
            quote_currency=quote or "USDT",
        )

    def _make_paper_margin(quote: str = "USDT") -> PaperMarginBroker:
        _pr = _paper_rules()
        return PaperMarginBroker(
            starting_capital=_paper_capital("margin", quote),
            fee_pct=_paper_fee_pct(),
            slippage_pct=float(bcfg.get("slippage_pct", 0.05)),
            mmr_pct=float(mcfg.get("mmr_pct", 1.0)),
            interest_per_4h_pct=_pr.interest_per_4h_pct,
            max_age_days=float(mcfg.get("max_age_days", 21.0)),
            rules=_pr,
            quote_currency=quote or "USDT",
        )

    # default paper mode: spot (long-only). Switchable via /api/paper-mode.
    paper_kind = os.environ.get("PAPER_MODE", "spot").strip().lower()
    _init_quote = _paper_quote(paper_kind)
    run_id = time.strftime("%Y%m%d_%H%M%S")
    # Phase 6: audit logs are per-profile (Logs/<profile_id>/...) so parallel
    # backends never interleave their decision/audit trails.
    file_logger = FileLogger(ROOT / "Logs" / profile_id, run_id, profile_id=profile_id)
    # Expose the per-profile logger to deep components (adapter, AI client,
    # wizard job threads, order paths) that can't take it as a constructor arg.
    set_active(file_logger)
    broker = _make_paper_margin(quote=_init_quote) if paper_kind == "margin" else _make_paper_spot(quote=_init_quote)
    engine = Engine(cfg, client, broker, storage, quote_service=quotes, file_logger=file_logger, data_dir=data_dir, strategy_store=strategy_store)
    try:
        engine.load_external_strategies()
    except Exception as exc:
        log.warning("external strategy startup load failed: %s", exc)
    # engage the symbol universe at startup: user's master list ∩ quote family,
    # or ALL pairs of the family when no selection (never just config.yaml's 8)
    try:
        _startup_syms = _symbol_universe(_init_quote)
        if _startup_syms:
            engine.set_symbols(_startup_syms)
    except Exception as exc:
        log.warning("startup symbol engagement failed: %s", exc)
    file_logger.write("settings", {
        "run_id": run_id,
        "ts": int(time.time()),
        "paper_kind": paper_kind,
        "quote": _init_quote,
        "starting_capital": _paper_capital(paper_kind, _init_quote),
        "trade_mode": "paper",
        "live_allowed": live_allowed,
        "engine_interval_seconds": int(cfg.get("engine", {}).get("scan_interval_minutes", 15)) * 60,
        "api_min_gap_sec": float(cfg.get("engine", {}).get("api_min_gap_sec", 12)),
        "fee_pct": float(cfg.get("backtest", {}).get("fee_pct", 0.2)),
        "slippage_pct": float(cfg.get("backtest", {}).get("slippage_pct", 0.05)),
        "margin_risk_coef": float(mcfg.get("risk_coef", 2.0)),
        "margin_mmr_pct": float(mcfg.get("mmr_pct", 1.0)),
        "margin_interest_per_4h_pct": float(mcfg.get("interest_per_4h_pct", 0.05)),
        "margin_max_age_days": float(mcfg.get("max_age_days", 21.0)),
        "symbols": _startup_syms,
        "profile_id": profile_id,
    })
    file_logger.write("server", {
        "event": "startup",
        "run_id": run_id,
        "pid": os.getpid(),
        "python": __import__("sys").version.split()[0],
        "port": int(cfg.get("server", {}).get("port", 8787)),
        "paper_kind": paper_kind,
        "quote": _init_quote,
        "live_allowed": live_allowed,
        "engine_symbols": len(_startup_syms),
    })

    # Provenance (Apache-2.0): identity travels with every /api/status so
    # forks/rebrands that strip attribution are immediately visible to users.
    app_version = "1.0.0"
    app = FastAPI(title="Any WALLEX — Multi-Exchange Trading Platform", version=app_version)
    # FIX(#15): the dashboard is same-origin — no cross-origin site needs API
    # access. A wildcard CORS on unauthenticated trading endpoints let ANY
    # visited website drive-by place orders / flip modes / wipe data. Only
    # localhost dashboard origins are allowed now.
    # FIX(audit-H6): CORS does not stop simple/no-cors CSRF POSTs, and DNS
    # rebinding defeats the origin regex. Validate Origin (when present)
    # and Host on every request: any cross-origin or rebound host is a 403.
    @app.middleware("http")
    async def _same_origin_guard(request: Request, call_next):
        host = (request.headers.get("host") or "").lower()
        if host and not host.split(":")[0] in ("127.0.0.1", "localhost", "[::1]", "::1"):
            return JSONResponse({"error": "invalid host"}, status_code=403)
        origin = request.headers.get("origin")
        if origin:
            o = origin.lower()
            m = re.match(r"^http://(127\.0\.0\.1|localhost|\[::1\])(:\d+)?/?$", o)
            if not m:
                return JSONResponse({"error": "cross-origin request blocked"}, status_code=403)
        return await call_next(request)

    app.add_middleware(
        CORSMiddleware,
        # multi-backend: any localhost port is a legit dashboard origin
        # (profiles run on 8787, 8788, 8789… simultaneously)
        allow_origin_regex=r"^http://(127\.0\.0\.1|localhost):\d+$",
        allow_methods=["GET", "POST", "PATCH", "DELETE"],
        allow_headers=["Content-Type"],
    )
    app.state.engine = engine
    app.state.profile_id = profile_id
    app.state.live_enabled = False
    app.state.live_allowed = live_allowed
    app.state.trade_mode = "paper"   # paper | spot | margin
    app.state.paper_kind = "margin" if paper_kind == "margin" else "spot"  # paper sub-mode
    app.state.boot_ts = time.time()

    # ── global error capture → per-profile `errors` log ─────────────
    # Any unhandled exception in an API handler is logged (with the request
    # path + full traceback) into Logs/<profile_id>/<run_id>_errors.jsonl,
    # then returned as a plain 500. This is the primary troubleshooting
    # trail for "why did the app fail" without console access.
    @app.exception_handler(Exception)
    async def _log_unhandled(_req, exc: Exception):
        import traceback
        try:
            log_error(exc, context=f"request {_req.method} {_req.url.path}",
                      exc_type=type(exc).__name__,
                      traceback=traceback.format_exc().replace("\n", " | ")[:4000])
        except Exception:
            pass
        return JSONResponse(status_code=500, content={"ok": False, "error": "internal error"})

    # ── clean shutdown: flush + record the stop in the `server` log ───
    @app.on_event("shutdown")
    def _on_shutdown():
        try:
            file_logger.write("server", {
                "event": "shutdown",
                "run_id": run_id,
                "pid": os.getpid(),
                "uptime_sec": int(time.time() - getattr(app.state, "boot_ts", time.time())),
                "trade_mode": app.state.trade_mode,
                "live_enabled": app.state.live_enabled,
            })
            file_logger.close_all()
        except Exception:
            pass

    # ── startup checkpoint restore ──────────────────────────────────
    # If the last session ended in live mode with a saved API key, re-engage
    # live on startup so the dashboard reflects the actual persisted state.
    # Retry up to 3 times with backoff in case Wallex is temporarily down.
    # NOTE: this runs AFTER `_engage_live` is defined below to avoid scoping errors.

    # ── dashboard data ─────────────────────────────────────────────
    @app.get("/favicon.ico", include_in_schema=False)
    def favicon():
        # 1x1 transparent PNG so browsers don't 404 /favicon.ico on every load.
        return Response(
            content=b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00\x01\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82",
            media_type="image/png",
        )

    @app.get("/api/status")
    def status():
        return {
            "running": engine.running,
            "mode": engine.broker.name,
            "paper_kind": app.state.paper_kind,
            "live_allowed": app.state.live_allowed,
            "live_enabled": app.state.live_enabled,
            "connected": engine.connected,
            "connected_map": engine.connected_map(),
            "last_tick_ts": engine.last_tick_ts,
            "symbols": engine.symbols,
            "stats": engine.stats(),
            "profile_id": app.state.profile_id,
            # FIX(reserve): capital accounting — grid-locked vs free balance
            "reserved": round(engine.broker.reserved_total(), 2),
            "app": "Any WALLEX",
            "version": app_version,
            "license": "Apache-2.0",
            "source": "https://github.com/mhv1989/any-wallex"
                        if hasattr(engine.broker, "reserved_total") else 0.0,
            "available": round(engine.broker.available(), 2)
                         if hasattr(engine.broker, "available") else None,
        }

    # ── multi-profile registry (Phase 0) ─────────────────────────────
    @app.get("/api/profiles")
    def profiles_list():
        from .exchange import registry as _reg
        rows = _reg.list_profiles()
        active = _reg.active_profile_id()
        last = _reg.last_launched_profile_id()
        for r in rows:
            r["is_active"] = (r["id"] == active)
            r["running_here"] = (r["id"] == app.state.profile_id)
            r["is_last_launched"] = (r["id"] == last)
        return {"profiles": rows, "active": active, "last_launched": last,
                "here": app.state.profile_id}

    @app.post("/api/profiles/{pid}/activate")
    def profiles_activate(pid: str):
        """Set the registry's active profile (used by launcher default boot)."""
        from .exchange import registry as _reg
        try:
            _reg.set_active_profile(pid)
        except KeyError:
            raise HTTPException(status_code=404, detail=f"unknown profile: {pid}")
        return {"ok": True, "active": _reg.active_profile_id()}

    @app.post("/api/profiles/{pid}/start")
    def profiles_start(pid: str):
        """Boot this profile's backend as a DETACHED process on its registry
        port (multi-backend from the dropdown). No-op if already running."""
        import socket as _socket
        import subprocess as _subprocess
        import sys as _sys
        from .exchange import registry as _reg
        info = _reg.get_profile(pid)
        if info is None:
            raise HTTPException(status_code=404, detail=f"unknown profile: {pid}")
        port = int(info.get("port") or 8788)
        if pid == app.state.profile_id:
            return {"ok": True, "port": port, "already": "this-instance"}
        with _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM) as _s:
            _s.settimeout(0.4)
            if _s.connect_ex(("127.0.0.1", port)) == 0:
                # already running elsewhere — switching to it still counts as
                # LAUNCHING it for the next boot default (user req: app starts
                # on the last profile the user switched to).
                try:
                    _reg.mark_last_launched(pid)
                except Exception:
                    pass
                return {"ok": True, "port": port, "already": "running"}
        env = {**os.environ, "BOT_PROFILE": pid}
        env.pop("BOT_DATA_DIR", None)   # never inherit another profile's data dir
        pdir = _reg.profiles_root() / pid
        pdir.mkdir(parents=True, exist_ok=True)
        logf = open(pdir / "backend.log", "ab")
        kwargs = {}
        if hasattr(_subprocess, "DETACHED_PROCESS"):
            kwargs["creationflags"] = _subprocess.DETACHED_PROCESS | _subprocess.CREATE_NEW_PROCESS_GROUP
        _subprocess.Popen(
            [_sys.executable, "-m", "uvicorn", "bot.server:app",
             "--host", "127.0.0.1", "--port", str(port)],
            cwd=str(ROOT), stdout=logf, stderr=_subprocess.STDOUT,
            env=env, **kwargs)
        log.info("[profiles] spawned backend for '%s' on port %d", pid, port)
        # FIX(user req): spawning a profile from the switcher counts as
        # LAUNCHING it — it becomes the next boot default.
        try:
            _reg.mark_last_launched(pid)
        except Exception:
            pass
        return {"ok": True, "port": port, "started": True}

    # ── exchange metadata (Phase 1) — drives UI capability gating ────
    @app.get("/api/exchange/info")
    def exchange_info():
        info = client.exchange_info()
        info["profile_id"] = app.state.profile_id
        return info

    # ── Phase 3: wizard + CMC services ───────────────────────────────
    from .cmc_service import CMCService
    from .wizard import WizardEngine
    cmc = CMCService(store=store, data_dir=data_dir)
    wizard = WizardEngine(store=store, storage=storage)

    @app.get("/api/wizard/state")
    def wizard_state():
        from .exchange import registry as _reg
        first = wizard.first_run()
        # ── FIX (2026-09-09, user bug: «switched to MEXC → app shows the
        # start-wizard page»): the wizard gate previously fired on EVERY fresh
        # backend boot, including when the user switched to a profile that was
        # ALREADY set up. A newly spawned MEXC backend reads MEXC's own storage
        # (where wizard_completed was never set — the wizard ran on the nobitex
        # host), so first_run() returned True and the wizard re-opened over the
        # dashboard. A profile whose status is probed/ready was already walked
        # through setup, so it must not re-offer the wizard on boot. Users can
        # still start it any time via the header "➕ Add new exchange…" button.
        try:
            prof = P_load(app.state.profile_id)
            if prof and prof.get("status") in ("probed", "ready"):
                first = False
        except Exception:
            pass
        return {
            "first_run": first,
            "profile_id": app.state.profile_id,
            "profiles": _reg.list_profiles(),
            "ai_config": wizard.ai_config_masked(),
            "search_config": wizard.search_config(),
            "cmc": cmc.masked_state(),
        }

    @app.get("/api/wizard/exchanges")
    def wizard_exchanges():
        return wizard.catalog()

    @app.post("/api/wizard/ai-config")
    def wizard_ai_config(payload: dict = Body(default={})):
        return wizard.set_ai_config(
            payload.get("provider", "ollama"), payload.get("base_url", ""),
            payload.get("api_key", ""), payload.get("model", ""))

    @app.post("/api/wizard/search-config")
    def wizard_search_config(payload: dict = Body(default={})):
        return wizard.set_search_config(payload.get("provider", "duckduckgo"),
                                        payload.get("api_key", ""))

    @app.post("/api/wizard/research")
    def wizard_research(payload: dict = Body(default={})):
        custom = payload.get("custom") or {}
        custom = {**custom, "lang": payload.get("lang", "fa")}
        try:
            jid = wizard.research(payload.get("exchange_id", ""), custom=custom)
        except RuntimeError as exc:
            # job-cap guard (audit-M8): a friendly 429, not a 500
            raise HTTPException(status_code=429, detail=str(exc))
        return {"job_id": jid}

    @app.post("/api/wizard/probe")
    def wizard_probe(payload: dict = Body(default={})):
        pid = payload.get("profile_id", "")
        if not pid:
            raise HTTPException(status_code=400, detail="profile_id required")
        return {"job_id": wizard.probe(pid, lang=payload.get("lang", "fa"))}

    @app.post("/api/wizard/calibrate")
    def wizard_calibrate(payload: dict = Body(default={})):
        """Self-improvement: with the profile's LIVE key, discover the
        exchange's real paper-mode requirements from its own API and write
        them into profile.json rules (+ shared knowledge base for future
        exchanges). Requires the API key to be set in Settings."""
        pid = payload.get("profile_id", "")
        if not pid:
            raise HTTPException(status_code=400, detail="profile_id required")
        return {"job_id": wizard.calibrate(pid)}

    @app.post("/api/wizard/diagnose")
    def wizard_diagnose(payload: dict = Body(default={})):
        pid = payload.get("profile_id", "")
        if not pid:
            raise HTTPException(status_code=400, detail="profile_id required")
        return {"job_id": wizard.diagnose(pid)}

    @app.post("/api/wizard/activate")
    def wizard_activate(payload: dict = Body(default={})):
        try:
            out = wizard.activate(payload.get("profile_id", ""))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        wizard.mark_completed()
        return out

    @app.post("/api/wizard/completed")
    def wizard_completed():
        """User finished OR skipped the startup wizard in this boot — suppress
        the gate until the backend restarts (reload must not re-open it)."""
        wizard.mark_completed()
        return {"ok": True}

    @app.get("/api/wizard/report/{pid}")
    def wizard_report(pid: str):
        prof = P_load(pid)
        return {"profile_id": pid, "status": (prof or {}).get("status", ""),
                "profile": prof, "report": wizard.read_report(pid)}

    def P_load(pid: str):
        from .exchange import profile as _P
        from pathlib import Path as _Path
        return _P.load_profile(pid, _Path(ROOT) / "data" / "profiles")

    @app.get("/api/wizard/job/{jid}")
    def wizard_job(jid: str):
        job = wizard.job_state(jid)
        if job is None:
            raise HTTPException(status_code=404, detail="unknown job id")
        return job

    @app.post("/api/profiles/{pid}/heal")
    def profiles_heal(pid: str):
        """Self-heal a profile dir (user req): validate profile.json, archive
        corrupt/invalid copies, report what's preserved and whether re-research
        is needed. NEVER deletes history/db/secrets/strategies."""
        from .exchange import profile as _P
        from pathlib import Path as _Path
        out = _P.heal_profile(pid, _Path(ROOT) / "data" / "profiles")
        # ensure the registry knows about a profile dir that exists on disk
        if out["dir_exists"] and not _reg_get(pid):
            from .exchange import registry as _reg
            _reg.register_profile(pid, name=pid, set_active=False)
            out["registry_registered"] = True
        return out

    def _reg_get(pid: str):
        from .exchange import registry as _reg
        return _reg.get_profile(pid)

    # ── Phase 7: Deep AI troubleshooter ──────────────────────────────
    # (AppDoctor construction is DEFERRED to after _ai_key_decrypt's
    # definition — skill #19: a nested def makes its NAME local to the whole
    # enclosing function, so referencing it earlier raises UnboundLocalError.)
    from .doctor import AppDoctor
    doctor: Optional[AppDoctor] = None

    DOCTOR_ACTIONS = ("engine_start", "engine_stop", "refresh_markets",
                      "heal_profile", "probe_profile", "ai_diagnose_profile")

    @app.get("/api/doctor/context")
    def doctor_context(section: str = "all"):
        """Compact secret-free state snapshot (also useful for bug reports)."""
        return {"ok": True, "section": section, "context": doctor.collect(section)}

    @app.post("/api/doctor/diagnose")
    def doctor_diagnose(payload: dict = Body(default={})):
        """Ask the doctor to diagnose a section. Modes (user req): the app
        NEVER depends on AI — with ai_allowed=false or no AI configured it
        answers with deterministic local rules; with ai_enhanced=true the AI
        adds a deeper interpretation on top (rules text always included on
        total AI failure)."""
        section = str(payload.get("section") or "all")
        allowed = ("all", "engine", "api", "chart", "candles", "strategies",
                   "profile", "exchange", "events")
        if section not in allowed:
            raise HTTPException(status_code=400, detail=f"section must be one of {allowed}")
        # permission gate: user's global choice AND the per-request toggle
        ai_allowed = str(storage.kv_get("doctor_ai_allowed") or "1") == "1"
        out = doctor.diagnose(
            section, user_note=str(payload.get("note") or "")[:500],
            lang=str(payload.get("lang") or "fa"),
            ai_allowed=ai_allowed,
            ai_enhanced=bool(payload.get("ai_enhanced", True)))
        return out

    @app.get("/api/doctor/settings")
    def doctor_settings():
        return {"ai_allowed": str(storage.kv_get("doctor_ai_allowed") or "1") == "1"}

    @app.post("/api/doctor/settings")
    def doctor_set_settings(payload: dict = Body(default={})):
        storage.kv_set("doctor_ai_allowed", "1" if payload.get("ai_allowed", True) else "0")
        return {"ai_allowed": str(storage.kv_get("doctor_ai_allowed") or "1") == "1"}

    @app.post("/api/doctor/action")
    def doctor_action(payload: dict = Body(default={})):
        """Apply a whitelisted action the AI recommended (user clicks — the
        AI never executes anything itself)."""
        act = str(payload.get("action") or "").strip()
        if act not in DOCTOR_ACTIONS:
            raise HTTPException(status_code=400, detail=f"action must be one of {DOCTOR_ACTIONS}")
        try:
            if act == "engine_start":
                engine.start()
                return {"ok": True, "detail": "engine started"}
            if act == "engine_stop":
                engine.stop()
                return {"ok": True, "detail": "engine stopped"}
            if act == "refresh_markets":
                markets.refresh(force=True)
                return {"ok": True, "detail": f"{len(markets.stats())} markets refreshed"}
            if act == "heal_profile":
                from .exchange import profile as _P
                from pathlib import Path as _Path
                heal = _P.heal_profile(app.state.profile_id, _Path(ROOT) / "data" / "profiles")
                return {"ok": True, "detail": "; ".join(heal["healed"]) or "checked",
                        "needs_research": heal["needs_research"]}
            if act == "probe_profile":
                jid = wizard.probe(app.state.profile_id, lang=payload.get("lang", "fa"))
                return {"ok": True, "detail": "probe started", "job_id": jid}
            if act == "ai_diagnose_profile":
                jid = wizard.diagnose(app.state.profile_id)
                return {"ok": True, "detail": "AI profile repair started", "job_id": jid}
        except Exception as exc:
            return {"ok": False, "error": str(exc)[:200]}
        raise HTTPException(status_code=400, detail="unhandled action")

    # AI auto-pair: curate symbols_master from the FULL catalog (user req)
    @app.post("/api/pairs/ai-curate")
    def pairs_ai_curate(payload: dict = Body(default={})):
        symbols = payload.get("symbols") or []
        if not symbols:
            try:
                symbols = markets.symbols(quote=(payload.get("quote") or ""))
            except Exception:
                symbols = []
        if not symbols:
            raise HTTPException(status_code=400, detail="no symbols to curate")
        job_id = wizard.curate_pairs(symbols, payload.get("quote", ""))
        return {"job_id": job_id, "candidate_count": len(symbols)}

    # ── CoinMarketCap (optional, key-activated) ──────────────────────
    @app.get("/api/cmc/status")
    def cmc_status():
        return cmc.masked_state()

    @app.post("/api/cmc/key")
    def cmc_set_key(payload: dict = Body(default={})):
        key = (payload.get("api_key") or "").strip()
        if key and "…" in key:
            key = ""   # masked echo — never store the preview back
        cmc.set_key(key)
        return {"ok": True, **cmc.masked_state()}

    @app.get("/api/cmc/global")
    def cmc_global():
        try:
            return {"ok": True, "data": cmc.global_metrics()}
        except Exception as exc:
            return {"ok": False, "error": str(exc)[:200]}

    @app.get("/api/cmc/context")
    def cmc_context(symbol: str = "BTCUSDT"):
        """Full chart-side context: coin stats + info + global metrics."""
        if not cmc.enabled:
            return {"ok": False, "error": "کوین‌مارکت‌کپ فعال نیست — کلید را در تنظیمات وارد کنید"}
        out: dict = {"ok": True, "symbol": symbol}
        base = symbol.upper().replace("USDT", "").replace("USDC", "").replace("TMN", "").replace("IRT", "") or symbol
        errs = []
        for name, fn in (("quotes", lambda: cmc.quotes(base)),
                         ("info", lambda: cmc.info(base)),
                         ("global", lambda: cmc.global_metrics())):
            try:
                out[name] = fn()
            except Exception as exc:
                errs.append(f"{name}: {exc}")
                out[name] = None
        if errs and not out.get("quotes"):
            out["ok"] = False
            out["error"] = "; ".join(errs)[:250]
        return out

    @app.get("/api/cmc/ai-insight")
    def cmc_ai_insight_get(symbol: str = "BTCUSDT", config_id: str = ""):
        return cmc_ai_insight_impl({"symbol": symbol, "config_id": config_id})

    @app.post("/api/cmc/ai-insight")
    def cmc_ai_insight(payload: dict = Body(default={})):
        """POST variant: the frontend may hand over the CURRENT AI-tab values
        (unsaved provider/base_url/api_key/model or a config_id selection) so
        the insight uses exactly the AI the user configured in the UI."""
        return cmc_ai_insight_impl(payload)

    def cmc_ai_insight_impl(payload: dict):
        """AI trading-context suggestion from CMC data. AI resolution order
        (user req): the config the strategy/AI tab already uses (current
        fields → config_id → most recent saved config) → the wizard AI → the
        default client config. Falls back to the solid CMC data when no AI works."""
        symbol = str(payload.get("symbol") or "BTCUSDT")
        base = symbol.upper().replace("USDT", "").replace("USDC", "").replace("TMN", "").replace("IRT", "") or symbol
        try:
            brief = cmc.context_brief(base)
        except Exception as exc:
            return {"ok": False, "error": str(exc)[:200], "source": "none"}

        def _active_strategy_name() -> str:
            act = getattr(engine, "active_external_strategy", None)
            if isinstance(act, dict):
                return act.get("name") or act.get("strategy_id") or "legacy"
            return getattr(act, "name", None) or "legacy"

        def _insight_with(provider: str, base_url: str, api_key: str, model: str) -> dict:
            from .ai_strategy import AIStrategyClient, AIProviderConfig
            lang_name = "English" if str(payload.get("lang", "fa")).lower() == "en" else "Persian (Farsi)"
            tf_guide = (
                "The bot evaluates signals on these timeframes: "
                "15-minute (short-term momentum filter), 1-hour (entry patterns and levels), "
                "4-hour (market structure/trend), and 1-day (long context). "
                "MATCH your momentum interpretation to these horizons: use the 1h and 24h data "
                "for the 15m/1h trading horizon, the 7d/30d for the 4h structure view, and "
                "60d/90d for the daily context. State explicitly which horizon each observation serves."
            )
            prompt = (
                "You are a senior crypto market analyst writing inside a professional trading application.\n\n"
                "Below you receive LIVE CoinMarketCap market data and the bot's active strategy name.\n\n"
                f"Write your commentary in {lang_name} — formal, polished, professional financial tone.\n"
                "Structure: 3 to 5 flowing bullet lines, each a complete analytical sentence.\n"
                "Content: interpret momentum across the given timeframes, the volume trend, "
                "market dominance and its day-over-day shift, and what these imply for the short-term bias.\n"
                f"{tf_guide}\n"
                "Weave the concrete numbers naturally into the sentences (for example: "
                "\"thirty-day momentum stands at +23.3 percent\").\n\n"
                "HARD RULES:\n"
                "- NEVER output JSON, dictionaries, code blocks, markdown headers, key-value pairs or field labels like \"asset\" or \"brief\".\n"
                "- NEVER use parentheses around codes, tickers or labels.\n"
                "- NEVER mention all-time highs, funding rates, on-chain metrics or news — ONLY what the data below shows; if a dimension is absent, do not bring it up.\n"
                "- NEVER invent numbers. No disclaimers, no greetings, no closing remarks.\n\n"
                "MARKET DATA:\n" + brief +
                f"\n\nACTIVE BOT STRATEGY: {_active_strategy_name()}"
            )
            c = AIStrategyClient(AIProviderConfig(
                provider=provider, base_url=base_url, api_key=api_key,
                model=model or "llama3", timeout_sec=340, retries=1))
            raw = c._call_provider(prompt)
            return {"ok": True, "source": "ai", "brief": brief,
                    "insight": _sanitize_insight(raw)[:1800],
                    "provider": provider, "model": model}

        def _sanitize_insight(raw: str) -> str:
            """Local models sometimes ignore the no-JSON rule — if the reply
            contains a JSON blob (any shape), flatten it into readable bullet
            lines. Known wrapper keys: brief/bullets/points/commentary/
            analysis/insight; generic list-of-strings fallback covers the rest."""
            t = (raw or "").strip().strip("`")
            t = re.sub(r"^json\s*", "", t, flags=re.I).strip()
            # the blob may be preceded by a chatty sentence — extract the first {...}
            m = re.search(r"\{[\s\S]*\}", t)
            if m:
                try:
                    data = json.loads(m.group(0))
                    if isinstance(data, dict):
                        # 1) first list-of-strings among known/any keys
                        for key in ("brief", "bullets", "points", "commentary",
                                    "analysis", "insight", "lines", "summary"):
                            v = data.get(key)
                            if isinstance(v, list) and v and all(isinstance(x, str) for x in v):
                                return "\n".join("• " + x.strip().lstrip("•-•* ").strip() for x in v if x.strip())
                        # 2) first plain-string wrapper
                        for key in ("insight", "text", "summary", "commentary", "analysis"):
                            v = data.get(key)
                            if isinstance(v, str) and v.strip():
                                return v.strip()
                        # 3) ANY list of strings anywhere in the object
                        for v in data.values():
                            if isinstance(v, list) and v and all(isinstance(x, str) for x in v):
                                return "\n".join("• " + x.strip() for x in v if x.strip())
                except Exception:
                    pass
            # strip stray wrappers if the model emitted them without valid JSON
            t = re.sub(r'^\s*\{"?[\w_]+"?\s*:\s*\[', "", t)
            t = t.replace("]}", "").strip()
            return t

        # ── multi-AI fallback (user req): the strategy manager may hold MANY
        # saved AI configs. Instead of walking them sequentially (a dead first
        # config would stall the insight for minutes), race ALL candidates in
        # parallel and take the first SUCCESSFUL narrative — priority order is
        # preserved via tiered workers, not via waiting.

        def _spawn_insight(provider: str, base_url: str, api_key: str,
                           model: str, tier: int, label: str, out: list):
            def _run():
                try:
                    res = _insight_with(provider, base_url, api_key, model)
                    res["_tier"] = tier
                    res["_label"] = label
                    out.append(res)
                except Exception as exc:  # noqa: BLE001
                    out.append({"_tier": tier, "_label": label, "_error": str(exc)[:160]})
            th = threading.Thread(target=_run, daemon=True)
            th.start()
            return th

        import threading
        candidates: list = []   # (tier, label, provider, base_url, api_key, model)
        cur = payload.get("current") or {}
        if isinstance(cur, dict) and cur.get("base_url"):
            candidates.append((0, "current", str(cur.get("provider", "custom")),
                               str(cur.get("base_url")), str(cur.get("api_key") or ""),
                               str(cur.get("model") or "")))
        ref = _resolve_active_ai(payload)
        if ref and ref.get("base_url"):
            candidates.append((1, f"config:{ref.get('id')}", str(ref.get("provider", "custom")),
                               str(ref.get("base_url")), str(ref.get("api_key") or ""),
                               str(ref.get("model") or "")))
        try:
            prov_path = Path(data_dir) / "ai_providers.json"
            items = (json.loads(prov_path.read_text(encoding="utf-8")) or {}).get("items", []) if prov_path.exists() else []
            for it in sorted(items, key=lambda x: x.get("created", 0), reverse=True):
                if not it.get("base_url"):
                    continue
                candidates.append((2, f"saved:{it.get('id')}", str(it.get("provider", "custom")),
                                   str(it.get("base_url")),
                                   str(_ai_key_decrypt(it.get("api_key")) or ""),
                                   str(it.get("model") or "")))
        except Exception:
            pass
        try:
            wcfg = wizard.ai_config_masked()
            if wcfg.get("base_url"):
                candidates.append((3, "wizard", str(wcfg.get("provider", "ollama")),
                                   str(wcfg.get("base_url")), (store.get("wizard_ai_key") or ""),
                                   str(wcfg.get("model") or "")))
        except Exception:
            pass
        if ai_client.cfg.base_url:
            candidates.append((4, "default", str(ai_client.cfg.provider),
                               str(ai_client.cfg.base_url),
                               str(ai_client.cfg.api_key or ""), str(ai_client.cfg.model or "")))

        # dedupe identical provider+base_url+model (same endpoint saved twice)
        seen: set = set()
        uniq: list = []
        for c in candidates:
            key = (c[2], c[3], c[5])
            if key not in seen:
                seen.add(key)
                uniq.append(c)
        candidates = uniq[:6]   # cap parallel calls (free-tier rate limits)

        results: list = []
        threads = [_spawn_insight(p, b, k, m, t, lb, results)
                   for (t, lb, p, b, k, m) in candidates]
        # best-case latency: the fastest HEALTHY provider; worst-case bounded
        # by each call's own timeout (insight calls run with retries=1).
        deadline = time.time() + 150
        while time.time() < deadline:
            ok = [r for r in results if r.get("ok")]
            if ok:
                best = min(ok, key=lambda r: r["_tier"])
                best.pop("_tier", None); best.pop("_label", None)
                return best
            if len([r for r in results if r.get("_error")]) == len(threads):
                break   # every candidate failed
            time.sleep(0.4)
        errs = "; ".join(f"{r['_label']}: {r.get('_error', 'timeout')}" for r in results)[:400]
        last_err = errs or "all AI candidates timed out"

        # no AI reachable → the CMC data itself (user req: solid data fallback)
        try:
            structured = {"quotes": cmc.quotes(base), "global": cmc.global_metrics()}
            return {"ok": True, "source": "cmc_data", "brief": brief,
                    "insight": "", "data": structured, "ai_error": last_err[:200]}
        except Exception as exc2:
            return {"ok": False, "error": str(exc2)[:200], "source": "none", "ai_error": last_err[:200]}

    @app.get("/api/opportunities")
    def opportunities():
        """Scan results for all symbols — the 'فرصت‌های واجد شرایط' panel."""
        out = []
        for sym, snap in engine.snapshots.items():
            sig = snap.signal
            out.append({
                "symbol": sym,
                "price": snap.price,
                "trend": snap.trend,
                "structure_event": snap.structure_event,
                "rsi_h1": snap.rsi_h1,
                "atr_h1": snap.atr_h1,
                "support": snap.support,
                "resistance": snap.resistance,
                "grid_active": snap.grid_active,
                "grid_levels": snap.grid_levels,
                "eligible": snap.eligible,
                "score": snap.score,
                "pattern": sig.pattern if sig else "",
                "entry": sig.entry if sig else None,
                "stop": sig.stop if sig else None,
                "target": sig.target if sig else None,
                "rr": sig.rr if sig else None,
                "confirmations": [
                    {"key": c.key, "label": c.label_fa, "ok": c.ok, "detail": c.detail}
                    for c in (sig.confirmations if sig else [])
                ],
                "updated_ts": snap.updated_ts,
            })
        out.sort(key=lambda x: (-int(x["eligible"]), -x["score"]))
        return out

    @app.get("/api/positions")
    def positions():
        out = []
        for p in engine.positions.values():
            if not p.is_open:
                continue
            cur_px = engine.broker.last_price(p.symbol) or (p.meta or {}).get("entry", p.entry)
            is_short = getattr(p, "side", "long") == "short"
            upnl = ((p.entry - cur_px) if is_short else (cur_px - p.entry)) * p.qty
            out.append({
                "id": p.id, "symbol": p.symbol, "qty": p.qty, "entry": p.entry,
                "stop": p.stop, "state": p.state, "pnl": p.pnl,
                "current_price": cur_px,
                "upnl": round(upnl, 6),
                "upnl_pct": round(upnl / max(p.entry * p.qty, 1e-12) * 100, 3),
                "side": getattr(p, "side", "long"),
                "risk_coef": getattr(p, "risk_coef", 1.0),
                "liq_price": (p.meta or {}).get("liq_price"),
                "collateral": (p.meta or {}).get("collateral"),
                "interest_accrued": (p.meta or {}).get("interest_accrued", 0.0),
                "breakeven_done": p.breakeven_done, "trailing_on": p.trailing_on,
                "partial_taken": p.partial_taken, "peak_price": p.peak_price,
                "opened_ts": p.opened_ts, "exit_reason": p.exit_reason,
                "score": p.signal_score,
            })
        return out

    @app.get("/api/trades")
    def trades(limit: int = 200):
        return storage.closed_trades(limit)

    @app.get("/api/equity")
    def equity():
        return storage.equity_series()

    @app.get("/api/audit")
    def audit_log(limit: int = 200):
        return storage.audit_recent(limit)

    @app.get("/api/grid")
    def grid_log(symbol: str = "", limit: int = 200):
        return storage.grid_snapshots_recent(symbol or None, limit)

    # ── Legacy Grid strategies (spot + margin, per-symbol profiles) ──
    grid_manager = GridManager(engine, storage, data_dir, tick_sec=int(cfg.get("grid", {}).get("tick_sec", 60)))

    @app.get("/api/grids")
    def grids_list():
        return {"items": grid_manager.summary()}

    @app.post("/api/grids/preview")
    def grids_preview(payload: dict):
        """Pre-launch preview: how the grid would work (levels + sizes)."""
        price = float(payload.get("price", 0) or 0)
        if price <= 0:
            try:
                price = float(engine.broker.last_price(str(payload.get("symbol", ""))) or 0)
            except Exception:
                price = 0.0
        if price <= 0:
            try:
                markets = client.get_markets()
                sym = str(payload.get("symbol", "")).upper()
                for m in markets:
                    if str(m.get("symbol") or "").upper() == sym:
                        try:
                            price = float(m.get("price") or (m.get("ticker") or {}).get("price") or 0)
                        except (TypeError, ValueError):
                            price = 0.0
                        break
            except Exception:
                price = 0.0
        return grid_manager.preview(payload or {}, price)

    @app.post("/api/grids")
    def grids_create(payload: dict):
        r, err = grid_manager.create(payload or {})
        if err:
            raise HTTPException(400, err)
        # seed the price cache immediately so the new card shows a real
        # "last" instead of 0 before the first grid tick
        try:
            px = float(engine.broker.last_price(r.p["symbol"]) or 0)
            if px <= 0:
                for m in client.get_markets():
                    if str(m.get("symbol") or "").upper() == r.p["symbol"]:
                        try:
                            px = float(m.get("price") or (m.get("ticker") or {}).get("price") or 0)
                        except (TypeError, ValueError):
                            px = 0.0
                        break
            if px > 0:
                r.last_price = px
                try:
                    engine.broker.set_price(r.p["symbol"], px)
                except Exception:
                    pass
                grid_manager.save()
        except Exception:
            pass
        storage.log_event(int(time.time()), "grid_created", r.p["symbol"], f"id={r.id}")
        return {"ok": True, "id": r.id, "item": r.snapshot()}

    @app.patch("/api/grids/{gid}")
    def grids_update(gid: str, payload: dict):
        r, err = grid_manager.update(gid, payload or {})
        if err:
            raise HTTPException(400, err)
        return {"ok": True, "item": r.snapshot()}

    @app.delete("/api/grids/{gid}")
    def grids_delete(gid: str):
        ok, err = grid_manager.delete(gid)
        if not ok:
            raise HTTPException(400, err)
        storage.log_event(int(time.time()), "grid_deleted", "", f"id={gid}")
        return {"ok": True}

    @app.post("/api/grids/{gid}/start")
    def grids_start(gid: str, payload: Optional[dict] = None):
        r = grid_manager.get(gid)
        if r is None:
            raise HTTPException(404, "grid not found")
        # execution-mode guard: live spot/margin orders only when actually engaged live
        live = bool(app.state.live_enabled)
        if live and not getattr(client, "api_key", ""):
            raise HTTPException(400, "live mode needs a valid API key")
        ok, err = grid_manager.start(gid)
        if not ok:
            raise HTTPException(400, err)
        storage.log_event(int(time.time()), "grid_started", r.p["symbol"],
                          f"id={r.id} mode={r.p['mode']} exec={'live' if live else 'paper'}")
        return {"ok": True, "item": r.snapshot(), "exec": "live" if live else "paper"}

    @app.post("/api/grids/{gid}/stop")
    def grids_stop(gid: str):
        ok, err = grid_manager.stop(gid)
        if not ok:
            raise HTTPException(400, err)
        r = grid_manager.get(gid)
        storage.log_event(int(time.time()), "grid_stopped", r.p["symbol"] if r else "", f"id={gid}")
        return {"ok": True}

    @app.post("/api/grids/{gid}/reset")
    def grids_reset(gid: str):
        ok, err = grid_manager.reset_state(gid)
        if not ok:
            raise HTTPException(400, err)
        return {"ok": True}

    def _sr_summary(symbol: str, res: str, lookback: int = 100) -> dict:
        """Support/resistance summary from the last `lookback` candles of a TF
        (1D / 4h): swing-pivot clusters + recent high/low. Cache-miss tolerant —
        returns {} when the TF has no data so the AI prompt degrades gracefully."""
        try:
            cs = engine._candle_cache.get(f"{symbol}:{res}", [])
            if len(cs) < lookback:
                # deep fetch: 1D needs ~100+ bars (engine caps 1D growth at
                # 13000, 4h at 3000 — well above the lookback)
                cs = engine._fetch_candles(symbol, res, max(lookback + 20, 120))
            cs = cs[-lookback:]
            if len(cs) < 30:
                return {}
            highs = [c.h for c in cs]
            lows = [c.l for c in cs]
            closes = [c.c for c in cs]
            # swing pivots: bar higher than 2 neighbors each side
            k = 2
            ph, pl = [], []
            for idx in range(k, len(cs) - k):
                if highs[idx] == max(highs[idx-k:idx+k+1]):
                    ph.append(highs[idx])
                if lows[idx] == min(lows[idx-k:idx+k+1]):
                    pl.append(lows[idx])
            def _clusters(vals, tol_pct=0.75):
                if not vals:
                    return []
                vals = sorted(vals)
                clusters, cur = [], [vals[0]]
                for v in vals[1:]:
                    if (v - cur[-1]) / cur[-1] * 100.0 <= tol_pct:
                        cur.append(v)
                    else:
                        clusters.append(cur); cur = [v]
                clusters.append(cur)
                # strongest zones: most touches, mid-price
                out = sorted(clusters, key=len, reverse=True)[:4]
                return [{"price": round(sum(c)/len(c), 8), "touches": len(c)}
                        for c in out]
            cur = closes[-1]
            res_levels = [z for z in _clusters(ph) if z["price"] > cur]
            sup_levels = [z for z in _clusters(pl) if z["price"] < cur]
            return {
                "tf": res,
                "candles_used": len(cs),
                "price": round(cur, 8),
                "period_high": round(max(highs), 8),
                "period_low": round(min(lows), 8),
                "resistance_above": res_levels,
                "support_below": sup_levels,
            }
        except Exception:
            return {}

    @app.post("/api/grids/ai-fill")
    def grids_ai_fill(payload: dict):
        """FIX(user req): 'پرکردن با AI' — AI prefills the Legacy-grid form.
        Uses the user's saved AI config (any provider); returns JSON parameters
        tuned for best profit/capital performance given whatever the user
        already filled. Never creates the grid — the user previews first."""
        symbol = str((payload or {}).get("symbol", "")).strip().upper()
        mode = str((payload or {}).get("mode", "spot")).strip().lower()
        user = dict((payload or {}).get("params") or {})
        if not symbol:
            raise HTTPException(400, "symbol required")

        # ── resolve AI config: explicit > first saved > env default ──
        cfg_ref = _resolve_active_ai(payload)
        provider = str((payload or {}).get("provider", ai_client.cfg.provider)).lower()
        model = str((payload or {}).get("model", ai_client.cfg.model)).strip()
        base_url = str((payload or {}).get("base_url", "")).strip()
        api_key = str((payload or {}).get("api_key", "")).strip()
        used_config = ""
        if cfg_ref:
            provider = str(cfg_ref.get("provider") or provider).lower()
            base_url = str(cfg_ref.get("base_url") or base_url).strip()
            api_key = str(cfg_ref.get("api_key") or api_key).strip()
            model = str(cfg_ref.get("model") or model).strip()
            used_config = str(cfg_ref.get("id") or cfg_ref.get("name") or "")
        else:
            # FIX(user bug): prefer the config the user ACTIVATED in the AI
            # settings section (بارگذاری شد), then the last saved, then env.
            _active_id = ""
            try:
                _active_id = str(storage.kv_get("active_ai_config") or "").strip()
            except Exception:
                pass
            try:
                _data = json.loads((Path(data_dir) / "ai_providers.json").read_text(encoding="utf-8"))
                items = _data.get("items", []) if isinstance(_data, dict) else []
                _pick = None
                if _active_id:
                    _pick = next((x for x in items if x.get("id") == _active_id), None)
                if _pick is None and items:
                    _pick = items[-1]   # most recently saved
                if _pick is not None:
                    it = _pick
                    provider = str(it.get("provider") or provider).lower()
                    base_url = str(it.get("base_url") or base_url).strip()
                    api_key = _ai_key_decrypt(it.get("api_key")) or api_key
                    model = str(it.get("model") or model).strip()
                    used_config = str(it.get("id") or it.get("name") or "")
            except Exception:
                pass
        if not api_key and provider not in ("ollama", "lmstudio", "llamafile"):
            raise HTTPException(400, "NO_AI_CONFIG")

        # ── live market price (cache-first, no hard failure) ──
        px = 0.0
        try:
            cached = engine._candle_cache.get(f"{symbol}:60", [])
            if cached:
                px = float(cached[-1].c)
        except Exception:
            pass
        if px <= 0:
            try:
                cs = engine._fetch_candles(symbol, "60", 30)
                if cs:
                    px = float(cs[-1].c)
            except Exception:
                pass
        if px <= 0:
            raise HTTPException(502, _friendly_fetch_error(
                Exception("no market price — fetch failed for this symbol")))

        fee_pct = 0.2
        try:
            fee_pct = float(cfg.get("backtest", {}).get("fee_pct", 0.2))
        except Exception:
            pass
        min_gap_pct = max(3.0 * fee_pct, 0.6)   # fee-gap rule + practical floor

        # FIX(user req): ground the AI in real structure — support/resistance
        # from at least the last 100 DAILY and 4h candles, so range_min/range_max
        # and grid_count decisions come from actual market structure, not guesswork.
        sr_d = _sr_summary(symbol, "1D", 100)
        sr_4h = _sr_summary(symbol, "240", 100)
        sr_txt = ""
        if sr_d:
            sr_txt += (f"\nDaily (1D) structure, last {sr_d['candles_used']} candles: "
                       f"period low {sr_d['period_low']}, high {sr_d['period_high']}. "
                       f"Support zones below price: {json.dumps(sr_d['support_below'])}. "
                       f"Resistance zones above price: {json.dumps(sr_d['resistance_above'])}.")
        if sr_4h:
            sr_txt += (f"\n4h structure, last {sr_4h['candles_used']} candles: "
                       f"period low {sr_4h['period_low']}, high {sr_4h['period_high']}. "
                       f"Support zones: {json.dumps(sr_4h['support_below'])}. "
                       f"Resistance zones: {json.dumps(sr_4h['resistance_above'])}.")
        if not sr_txt:
            sr_txt = "\n(No S/R history available — use a conservative ±6% range around price.)"

        prompt = f"""You are a crypto grid-trading parameter optimizer.
Exchange fee: {fee_pct}% per side. Minimum gap between grid levels must be >= {min_gap_pct}% (fee rule).
Current {symbol} price: {px}.
Market structure from candle history:{sr_txt}
User pre-filled parameters (JSON, empty = your choice): {json.dumps(user, ensure_ascii=False)}
Mode: {mode}.
Mode semantics — CRITICAL:
- spot: the bot only BUYS low and SELLS high within the range using real
  cash. There is NO direction/shorting — do not output direction, do not
  suggest leverage (always 1), and prefer ranges where price has support
  BELOW (the grid must hold the asset it buys).
- margin: the bot can go long, short, or neutral. Direction
  ('{user.get('direction', 'long')}') decides: long = buy-low/sell-high
  with leverage; short = sell-high/buy-back-lower (range bias UPWARD);
  neutral = profit both ways (range centered on price). Leverage 1-3
  conservative unless the S/R structure is strong and tight.
Return ONLY compact JSON, no markdown, with keys:
 range_min, range_max, grid_count, spacing ("arithmetic"|"geometric"),
 total_capital, allocation ("even"|"pyramid"), reinvest ("none"|"per_grid"|"all_grids"),
 trailing_up (true|false), leverage (number, 1 for spot),
 rationale (short reason, max 200 chars).
How to choose the BEST parameters:
- range_min/range_max: anchor the grid between the nearest STRONG support and
  resistance zones from the structure data above (not arbitrary percentages).
  The current price must sit inside the range. If price is near a support zone,
  bias the range downward (more buy levels below); near resistance, bias upward.
- grid_count: DENSER (more grids, smaller spacing) when the structure shows a
  tight sideways range (recent candles oscillating between close support and
  resistance); WIDER spacing with fewer grids when the recent range is wide or
  trending. Always keep every level gap >= {min_gap_pct}% (fee rule) — never
  exceed max_count = floor(range_span% / {min_gap_pct}%).
- spacing: "arithmetic" for ranges under ~25% wide; "geometric" for wider or
  volatile ranges.
- allocation: "pyramid" (heavier on lower levels) when price sits in the lower
  half of the range or in a downtrend; "even" in a clean sideways range.
- reinvest: "per_grid" to compound each level's profit into its own size;
  "all_grids" to boost the whole buy side from cumulative profit.
- total_capital: if the user did not specify, suggest a moderate amount suited
  to the grid count (per-level notional should be meaningful vs exchange
  minimums, roughly 20-50 per level for small accounts).
- trailing_up: true when the structure shows an upward drift/breakout.
- leverage (margin only): 1-3 conservative; higher only if the range is tight
  and well-defined by strong S/R.
Respect every user-filled value EXACTLY as given (do not change numbers the
user provided). range must bracket the current price."""
        try:
            eff = ai_client
            if base_url or api_key or provider != ai_client.cfg.provider or model != ai_client.cfg.model:
                eff = AIStrategyClient(AIProviderConfig(
                    provider=provider, base_url=base_url or ai_client.cfg.base_url,
                    api_key=api_key, model=model or ai_client.cfg.model,
                    timeout_sec=ai_client.cfg.timeout_sec, retries=1,
                ))
            raw = eff._call_provider(prompt, provider=provider, model=model, max_tokens=600)
        except Exception as e:
            # name the exact config/provider that failed so the user can fix
            # the right key in the AI settings section
            _which = used_config or f"{provider}/{model}"
            raise HTTPException(502, f"AI call failed [{_which}]: {str(e)[:200]}")
        import re as _re
        m = _re.search(r"\{[\s\S]*\}", raw or "")
        if not m:
            raise HTTPException(502, "AI returned no JSON")
        try:
            ai = json.loads(m.group(0))
        except Exception:
            raise HTTPException(502, "AI JSON unparseable")

        # ── validate + clamp; user-filled values always win ──
        def _f(v, default):
            try:
                return float(v)
            except (TypeError, ValueError):
                return default
        out = {}
        out["symbol"] = symbol
        out["mode"] = mode if mode in ("spot", "margin") else "spot"
        u_dir = str(user.get("direction", "")).lower()
        out["direction"] = u_dir if u_dir in ("long", "short", "neutral") else (
            "long" if out["mode"] == "spot" else "long")
        if out["direction"] in ("short", "neutral") and out["mode"] == "spot":
            out["direction"] = "long"
        rmin = _f(ai.get("range_min"), 0.0)
        rmax = _f(ai.get("range_max"), 0.0)
        if user.get("range_min"):
            rmin = _f(user["range_min"], rmin)
        if user.get("range_max"):
            rmax = _f(user["range_max"], rmin * 1.01 if rmin > 0 else rmax)
        # sanity: bracket price, min width
        if rmin <= 0 or rmax <= 0 or rmax <= rmin:
            rmin = px * 0.93
            rmax = px * 1.07
        if rmin > px:
            rmin, rmax = rmax, rmin
        if px < rmin or px > rmax:
            span = rmax - rmin
            rmin, rmax = px - span / 2, px + span / 2
        if (rmax - rmin) / rmin * 100.0 < min_gap_pct * 2:
            span = max(rmin * min_gap_pct * 4 / 100.0, px * 0.02)
            rmin, rmax = max(px - span, 0.00000001), px + span
        out["range_min"], out["range_max"] = round(rmin, 8), round(rmax, 8)
        gc = int(_f(ai.get("grid_count"), 20))
        if user.get("grid_count"):
            gc = int(_f(user["grid_count"], gc))
        gc = max(2, min(200, gc))
        # enforce fee-gap: count such that gap% >= min_gap_pct
        span_pct = (rmax - rmin) / rmin * 100.0
        max_count = max(2, int(span_pct / min_gap_pct))
        gc = min(gc, max_count)
        out["grid_count"] = gc
        sp = str(ai.get("spacing", "arithmetic")).lower()
        out["spacing"] = sp if sp in ("arithmetic", "geometric") else (
            "geometric" if span_pct > 25 else "arithmetic")
        cap = _f(ai.get("total_capital"), 0.0)
        if user.get("total_quote"):
            cap = _f(user["total_quote"], cap)
        out["total_quote"] = cap if cap > 0 else 100.0
        al = str(ai.get("allocation", "even")).lower()
        out["allocation"] = al if al in ("even", "pyramid") else "even"
        rv = str(ai.get("reinvest", "none")).lower()
        out["profit_reinvest"] = rv if rv in ("none", "per_grid", "all_grids") else "none"
        tr = ai.get("trailing_up")
        out["trailing_up"] = bool(tr) if tr is not None else False
        lev = _f(ai.get("leverage"), 1.0) if out["mode"] == "margin" else 1.0
        lev = max(1.0, min(5.0, lev))
        out["leverage"] = lev
        out["rationale"] = str(ai.get("rationale", ""))[:220]
        out["_ai_config"] = used_config
        # FIX(user req): trader-style clarification — WHY these parameters.
        # Built from the same facts the AI saw (S/R zones, fee math, price
        # position) so the end-user can audit the logic, not just trust it.
        _lang = str((payload or {}).get("lang", "fa")).lower()
        FA = _lang.startswith("fa")
        span_pct = (out["range_max"] - out["range_min"]) / out["range_min"] * 100.0
        gap_pct = span_pct / max(out["grid_count"] - 1, 1)
        pos = (px - out["range_min"]) / (out["range_max"] - out["range_min"]) * 100.0
        per_leg = out["total_quote"] / out["grid_count"]
        pair_net = per_leg * (gap_pct - 2 * fee_pct) / 100.0
        def _n(v):
            s = f"{v:,.4f}".rstrip("0").rstrip(".") if isinstance(v, float) else f"{v:,}"
            if FA:
                s = s.translate(str.maketrans("0123456789,. ", "۰۱۲۳۴۵۶۷۸۹٬٫ "))
            return s

        exp = []
        if FA:
            exp.append(f"بازه گرید {_n(round(span_pct,2))}٪ عرض دارد (از {out['range_min']} تا {out['range_max']}) — قیمت فعلی {_n(px)} داخل بازه است.")
            pos_fa = "پایین" if pos < 40 else ("میانه" if pos < 60 else "بالا")
            exp.append(f"قیمت در موقعیت {pos_fa} بازه ({round(pos,1)}٪) قرار دارد؛ الگوریتم بر همین اساس سطح خرید/فروش را چیدمان کرد.")
            if sr_4h.get("resistance_above"):
                z = sr_4h["resistance_above"][0]
                exp.append(f"مقاومت ۴ساعته نزدیک: {z['price']} (تاچ: {z['touches']} بار) — سقف بازه نزدیک این سطح چیده شد تا فروشها در مقاومت واقعی انجام شوند.")
            if sr_4h.get("support_below"):
                z = sr_4h["support_below"][0]
                exp.append(f"حمایت ۴ساعته نزدیک: {z['price']} (تاچ: {z['touches']} بار) — کف بازه پشت این حمایت است تا ریزشهای موقتی به بالای آن بخرند.")
            if sr_d.get("support_below") or sr_d.get("resistance_above"):
                exp.append("ساختار روزانه (۱D) هم بررسی شد تا بازه با روند بلندمدت همخوان باشد.")
            exp.append(f"فاصله پلهها {round(gap_pct,3)}٪ است — {round(gap_pct,2)}٪ در برابر کارمزد {fee_pct}٪ (قانون ۳برابر کارمزد رعایت شد؛ سقف پله {max(2, int(span_pct / max(min_gap_pct, 0.01)))}).")
            exp.append(("تخصیص هرمی: پلههای پایین سنگینترند تا میانگین خرید در ریزش پایینتر بیاید." if out["allocation"] == "pyramid" else "تخصیص مساوی: همه پلهها هموزناند — مناسب رنج تمیز و خنثی."))
            rv_fa = {"none": "بدون تجمیع — اندازه سفارشها ثابت میماند.",
                     "per_grid": "تجمیع سود هر پله در همان پله — پلههای سودده بزرگتر میشوند.",
                     "all_grids": "سود تجمعی همه پلهها سمت خرید تزریق میشود — قدرت خرید در ریزش بالا میرود."}
            exp.append("سود: " + rv_fa[out["profit_reinvest"]])
            exp.append(f"هر پله {_n(round(per_leg,2))} سرمایه دارد؛ سود خالص هر جفت معامله ≈ {_n(round(pair_net,4))} (بعد از کارمزد دو طرف).")
            if mode == "margin":
                exp.append(f"اهرم {out['leverage']}× — محافظهکارانه؛ نقدشوندگی از ساختار حمایت/مقاومت محاسبه شد.")
            if out["trailing_up"]:
                exp.append("تریلینگ فعال: اگر قیمت سقف بازه را بشکند، کل بازه یک گام بالا میرود (از دست دادن روند صفر).")
        else:
            exp.append(f"Grid range is {round(span_pct,2)}% wide ({out['range_min']} to {out['range_max']}) — current price {_n(px)} sits inside it.")
            pos_en = "lower" if pos < 40 else ("middle" if pos < 60 else "upper")
            exp.append(f"Price is in the {pos_en} part of the range ({round(pos,1)}%) — level placement follows that.")
            if sr_4h.get("resistance_above"):
                z = sr_4h["resistance_above"][0]
                exp.append(f"Nearest 4h resistance: {z['price']} ({z['touches']} touches) — range top placed there so sells execute at real resistance.")
            if sr_4h.get("support_below"):
                z = sr_4h["support_below"][0]
                exp.append(f"Nearest 4h support: {z['price']} ({z['touches']} touches) — range floor sits behind it so dips buy above real support.")
            if sr_d.get("support_below") or sr_d.get("resistance_above"):
                exp.append("Daily (1D) structure was also checked to keep the range consistent with the longer trend.")
            exp.append(f"Level spacing {round(gap_pct,3)}% vs fee {fee_pct}% (3x-fee rule respected; count cap {max(2, int(span_pct / max(min_gap_pct, 0.01)))}).")
            exp.append(("Pyramid allocation: lower levels are heavier, lowering average buy cost in dips." if out["allocation"] == "pyramid" else "Even allocation: all levels equal weight — fits a clean sideways range."))
            rv_en = {"none": "No reinvest — order sizes stay fixed.",
                     "per_grid": "Each level compounds its own profit — winning levels grow.",
                     "all_grids": "Cumulative profit feeds the whole buy side — more buying power in dips."}
            exp.append("Reinvest: " + rv_en[out["profit_reinvest"]])
            exp.append(f"Per-level capital {_n(round(per_leg,2))}; estimated net profit per completed pair ≈ {_n(round(pair_net,4))} (after both-side fees).")
            if mode == "margin":
                exp.append(f"Leverage {out['leverage']}x — conservative; liquidation was checked against the S/R structure.")
            if out["trailing_up"]:
                exp.append("Trailing up is on: if price breaks the range top, the whole grid shifts one span higher (no missed trend).")
        return {"ok": True, "params": out, "price": px, "provider": provider,
                "model": model, "config": used_config,
                "explanation": exp}

    @app.post("/api/grids/{gid}/ai-advise")
    def grids_ai_advise(gid: str, payload: dict):
        """AI spillover advisor: best approach for missed fills (+ optional apply)."""
        cfg_ref = _resolve_active_ai(payload)
        provider = str((payload or {}).get("provider", ai_client.cfg.provider)).lower()
        model = str((payload or {}).get("model", ai_client.cfg.model)).strip()
        base_url = str((payload or {}).get("base_url", "")).strip()
        api_key = str((payload or {}).get("api_key", "")).strip()
        if cfg_ref:
            provider = str(cfg_ref.get("provider") or provider).lower()
            base_url = str(cfg_ref.get("base_url") or base_url).strip()
            api_key = str(cfg_ref.get("api_key") or api_key).strip()
            if not model:
                model = str(cfg_ref.get("model") or "").strip()
        elif api_key and "…" in api_key:
            api_key = ""

        def _call(prompt: str) -> str:
            eff = ai_client
            if base_url or api_key or provider != ai_client.cfg.provider or model != ai_client.cfg.model:
                eff = AIStrategyClient(AIProviderConfig(
                    provider=provider, base_url=base_url or ai_client.cfg.base_url,
                    api_key=api_key, model=model or ai_client.cfg.model,
                    timeout_sec=ai_client.cfg.timeout_sec, retries=1,
                ))
            import re as _re
            import json as _json
            raw = eff._call_provider(prompt, provider=provider, model=model, max_tokens=300)
            m = _re.search(r"```(?:json)?\s*([\s\S]*?)```", raw or "")
            return (m.group(1) if m else raw).strip()

        return grid_manager.ai_advise(gid, _call)

    @app.post("/api/grids/{gid}/backtest")
    def grids_backtest(gid: str, payload: dict):
        """Paper-mode optimization: simulate over the symbol's 1h candles.
        Disk-first (engine cache / data/history) — the API fetch is a last
        resort so grid backtests never stall on the client rate throttle and
        give a PRECISE error when history is genuinely missing."""
        r = grid_manager.get(gid)
        if r is None:
            raise HTTPException(404, "grid not found")
        sym = r.p["symbol"]
        try:
            days = int((payload or {}).get("days", 60))
        except (TypeError, ValueError):
            days = 60
        res_from = "60"
        to_ts = int(time.time())
        from_ts = to_ts - days * 86400
        candles = []
        # 1) engine in-memory cache (zero I/O, always fresh from the tick loop)
        try:
            candles = list(engine._candle_cache.get(f"{sym}:60") or [])
        except Exception:
            candles = []
        # 2) disk history (deep backfill from previous backtests/charts)
        if len(candles) < days * 12:  # 1h bars ≈ 24/day; want at least half the window
            try:
                hist = load_symbol_history(data_dir, sym)
                disk = [c for c in hist.h60.candles if c.ts >= from_ts]
                if len(disk) > len(candles):
                    candles = disk
            except Exception:
                pass
        # 3) last resort: live API fetch (subject to the client rate gap)
        if len(candles) < days * 6:
            try:
                fetched = client.get_candles(sym, res_from, from_ts, to_ts)
                if len(fetched) > len(candles):
                    candles = fetched
            except Exception as exc:
                # surface the REAL failure instead of a generic 'backtest failed'
                raise HTTPException(502, f"candle fetch failed for {sym} 60m: {exc}")
        if len(candles) < 50:
            raise HTTPException(400, (
                f"تاریخچه کافی برای {sym} پیدا نشد ({len(candles)} کندل ۱ساعته). "
                "اول از تب بکتست «دانلود تاریخچه» را اجرا کن یا موتور را روشن بگذار تا کش پر شود."
            ))
        # trim to the requested window (disk cache may be wider)
        candles = [c for c in candles if c.ts >= from_ts]
        fee = _paper_fee_pct()
        out = r.backtest(candles, fee_pct=fee)
        out["symbol"] = sym
        out["days"] = days
        out["bars"] = len(candles)
        out["source"] = "cache" if candles else "none"
        try:
            grid_manager.save()
        except Exception:
            pass
        return out

    @app.get("/api/engine/state")
    def engine_state_get():
        return {
            "running": engine.running,
            "symbols": engine.symbols,
            "margin_mode": engine.margin_mode,
            "paper_kind": app.state.paper_kind,
            "last_tick_ts": engine.last_tick_ts,
        }

    @app.get("/api/events/stream")
    def events_stream():
        import json as _json

        def _poller():
            last_id = 0
            while True:
                try:
                    rows = storage.events_recent(200)
                except Exception:
                    rows = []
                new = []
                for r in rows:
                    rid = r.get("id")
                    if isinstance(rid, int) and rid > last_id:
                        new.append(r)
                if new:
                    last_id = max((r.get("id", 0) for r in new), default=last_id)
                    payload = _json.dumps({"events": new}, ensure_ascii=False)
                    yield f"data: {payload}\n\n"
                time.sleep(2)

        return StreamingResponse(_poller(), media_type="text/event-stream")

    @app.get("/api/events")
    def events(limit: int = 100):
        return storage.events_recent(limit)

    @app.get("/api/apilog")
    def apilog(limit: int = 200):
        # Merge live in-memory api_log (populated on every request, even before the
        # first tick_once persists to SQLite) with persisted rows. In-memory entries
        # are the source of truth for freshness; dedupe by (ts, path, status).
        try:
            mem = [
                {
                    "ts": e.ts,
                    "method": e.method,
                    "path": e.path,
                    "status": e.status,
                    "latency_ms": e.latency_ms,
                    "retries": e.retries,
                    "error": e.error,
                }
                for e in getattr(engine, "client", {}).api_log or []
            ]
        except Exception:
            mem = []
        seen = set()
        merged = []
        for row in mem + storage.api_log_recent(limit * 4):
            key = (round(row.get("ts", 0), 1), row.get("path", ""), row.get("status"))
            if key in seen:
                continue
            seen.add(key)
            merged.append(row)
        merged.sort(key=lambda r: r.get("ts", 0), reverse=True)
        return merged[:limit]

    @app.get("/api/candles/{symbol}")
    def candles(symbol: str, resolution: str = "60", limit: int = 500, fresh: bool = False,
                include_open: bool = False):
        key = f"{symbol}:{resolution}"
        cached = engine._candle_cache.get(key, [])
        # Serve from disk/memory cache FIRST — a page reload must never trigger
        # a network fetch. We only hit the network when the cache is stale
        # (last closed candle older than one bar) AND no fetch is already in
        # flight for this key. Otherwise serve cached (freshness is maintained
        # by the engine tick loop).
        res_sec = {"15": 900, "60": 3600, "240": 14400, "1D": 86400}.get(resolution, 3600)
        now = int(time.time())
        last_ts = cached[-1].ts if cached else 0
        stale = (now - last_ts) >= res_sec          # a new bar is due
        inflight = engine._fetch_inflight.get(key, 0.0) > now - 8  # fetch started <8s ago
        # FIX(reload-refetch): honor the engine's fetch backoff — a language
        # switch (full page reload) must not re-attempt a network fetch that
        # is already in exponential backoff (CoinEx blocked → every reload
        # burned retries). When the engine runs, it owns fetching anyway.
        in_backoff = now < engine._fetch_backoff.get(key, 0.0)
        if fresh or (not cached) or (stale and not inflight and not in_backoff
                                      and not engine.running):
            # engine OFF → nobody else refreshes the cache, so fetch here.
            # FIX(audit-M10): check-and-set under a lock — two concurrent
            # stale requests could both pass the inflight test and double-fetch.
            with _fetch_inflight_lock:
                if engine._fetch_inflight.get(key, 0.0) > now - 8:
                    inflight = True
                else:
                    engine._fetch_inflight[key] = float(now)
                    inflight = False
            if inflight:
                out = list(cached[-limit:])
                return out
            # engine OFF → nobody else refreshes the cache, so fetch here
            try:
                cached = engine._fetch_candles(symbol, resolution, max(limit, 500))
                engine._fetch_inflight.pop(key, None)
            except Exception as e:
                engine._fetch_inflight.pop(key, None)
                log.exception("candles endpoint fetch failed symbol=%s resolution=%s limit=%s: %s", symbol, resolution, limit, e)
                raise HTTPException(502, _friendly_fetch_error(e))
        out = list(cached[-limit:])
        # append/refresh the forming candle (chart only — strategy never sees it)
        if include_open:
            # FIX(stale-forming): the forming candle was frozen at whatever
            # OHLC the last network fetch saw (up to 1h on 60m / 4h on 240m
            # — user saw a "stale chart" vs the exchange's live candle).
            # Refresh/patch it from the cached live price snapshot (45s
            # TTL — zero extra API load).
            res_sec_oc = res_sec
            bucket = (now // res_sec_oc) * res_sec_oc   # forming bar open
            px = _price_cache["data"].get(symbol, 0.0)
            if px <= 0 and out:
                px = out[-1].c   # last resort: carry the last close
            oc = engine._open_candle.get(key)
            if oc is None or oc.ts != bucket:
                # synthesize the forming bar from the last closed bar
                if out:
                    prev = out[-1]
                    if prev.ts < bucket:
                        from bot.models import Candle as _C
                        oc = _C(ts=bucket, o=prev.c, h=max(prev.c, px),
                                l=min(prev.c, px), c=px, v=0.0)
                    else:
                        oc = None   # out[-1] IS the forming bucket already
                else:
                    oc = None
            if oc is not None and px > 0:
                from bot.models import Candle as _C
                oc = _C(ts=oc.ts, o=oc.o, h=max(oc.h, px), l=min(oc.l, px),
                        c=px, v=oc.v)
            if oc is not None:
                if out and out[-1].ts == oc.ts:
                    out[-1] = oc  # replace the (frozen) forming copy
                elif not out or oc.ts > out[-1].ts:
                    out.append(oc)
        resp = [
            {"ts": c.ts, "o": c.o, "h": c.h, "l": c.l, "c": c.c, "v": c.v}
            for c in out
        ]
        # FIX(stale-visibility): when the served data is behind AND the last
        # fetch failed, tell the client WHY via headers — the frontend toasts
        # "data is stale + network/region blocked" instead of silently
        # rendering an old chart (CoinEx case: disk cache served, exchange
        # DNS-blackholed, no error anywhere in the UI).
        try:
            _err = getattr(engine, "_fetch_err", {}).get(key, "")
            # stale = at least one FULL bar behind the current forming bar
            _stale = bool(out) and ((now // res_sec) * res_sec - out[-1].ts) >= res_sec
            if _err and _stale:
                resp_headers = {"X-Candle-Stale": "1",
                                "X-Candle-Fetch-Error": _friendly_fetch_error(
                                    Exception(_err))[:300]}
            else:
                resp_headers = None
        except Exception:
            resp_headers = None
        from fastapi.responses import JSONResponse as _JR
        if resp_headers:
            return _JR(content=resp, headers=resp_headers)
        return resp

    def _profile_base_url() -> str:
        try:
            import json as _json
            _p = _reg.profiles_root() / app.state.profile_id / "profile.json"
            return (_json.loads(_p.read_text(encoding="utf-8")).get("base_url") or "")
        except Exception:
            return ""

    def _friendly_fetch_error(e: Exception) -> str:
        """FIX(net-classify): network/geo failures get a REAL explanation
        (use VPN / blocked in region), not a bare exception dump."""
        from .exchange.generic import ExchangeNetworkError
        reason = getattr(e, "reason", "")
        txt = str(e)
        lang_fa = True
        if isinstance(e, ExchangeNetworkError) or reason:
            # DNS-blackhole upgrade: the censor resolves public exchange
            # hosts to a private IP (10.x) — report it as DNS blocked.
            if reason == "refused":
                try:
                    from urllib.parse import urlparse
                    import socket as _s
                    _host = urlparse(str(getattr(client, "base_url", "") or "")).hostname \
                        or urlparse(_profile_base_url()).hostname
                    if _host:
                        _ip = _s.gethostbyname(_host)
                        if _ip.startswith(("10.", "192.168.", "127.")) or \
                            (_ip.startswith("172.") and 16 <= int(_ip.split(".")[1]) <= 31):
                            reason = "dns"
                except Exception:
                    pass
            reasons = {
                "dns": ("این صرافی از شبکه شما در دسترس نیست (DNS مسدود است) — با VPN امتحان کنید",
                        "This exchange is unreachable from your network (DNS blocked) — try a VPN"),
                "refused": ("اتصال به این صرافی رد میشود — احتمالاً تحریم/فیلترینگ منطقه‌ای است؛ با VPN امتحان کنید",
                            "Connections to this exchange are refused — likely regional blocking; try a VPN"),
                "timeout": ("اتصال به این صرافی تایماوت میشود — شبکه/فیلترینگ جلوی دسترسی را گرفته؛ با VPN امتحان کنید",
                            "Connections to this exchange time out — network/filtering is blocking access; try a VPN"),
                "ssl": ("اتصال امن به این صرافی برقرار نمیشود (SSL) — احتمالاً دسترسی منطقه‌ای مسدود است",
                        "Secure connection to this exchange fails (SSL) — likely regional blocking"),
                "geo": ("این صرافی دسترسی از کشور شما را مسدود کرده (403) — با VPN کشور دیگر امتحان کنید",
                        "This exchange blocks your country (403) — try a VPN with another region"),
            }
            fa, en = reasons.get(reason, ("این صرافی از شبکه فعلی در دسترس نیست — با VPN امتحان کنید",
                                          "This exchange is unreachable from the current network — try a VPN"))
            return f"{fa} [{en} | {txt[:140]}]"
        # 403 HTML bodies from Cloudflare/CloudFront = geo wall
        low = txt.lower()
        if "403" in low and ("html" in low or "cloudflare" in low or "cloudfront" in low
                             or "blocked" in low or "forbidden" in low):
            return ("این صرافی دسترسی از کشور شما را مسدود کرده (403) — با VPN کشور دیگر امتحان کنید "
                    f"[{txt[:140]}]")
        # text-based fallback for re-wrapped errors (reason attr lost)
        from .exchange.generic import _net_fail_reason
        _r = _net_fail_reason(txt)
        if _r in ("dns", "refused", "timeout", "ssl"):
            _fa, _en = {
                "dns": ("DNS این صرافی مسدود شده — با VPN امتحان کنید",
                        "DNS blocked — try a VPN"),
                "refused": ("اتصال به این صرافی رد میشود — احتمالاً فیلترینگ منطقه‌ای؛ با VPN امتحان کنید",
                            "connections refused — likely regional blocking; try a VPN"),
                "timeout": ("اتصال تایماوت میشود — با VPN امتحان کنید",
                            "connection times out — try a VPN"),
                "ssl": ("اتصال امن برقرار نمیشود — احتمالاً مسدودسازی منطقه‌ای",
                        "SSL fails — likely regional blocking"),
            }[_r]
            return f"{_fa} [{_en} | {txt[:140]}]"
        return f"{type(e).__name__}: {e}"

    _fetch_inflight_lock = __import__("threading").Lock()
    _price_cache: dict = {"data": {}, "ts": 0.0}   # symbol -> (price, ts) via one markets snapshot

    @app.get("/api/price/{symbol}")
    def price_for_symbol(symbol: str):
        """Live last price for one symbol (drives the browser-tab price).
        FIX(audit-H12): each 10s tab poll used to trigger a FULL paced
        /markets call — one tab alone ate ~85% of the 12s API budget and
        starved engine candle fetches. Now ONE markets snapshot per 45s
        serves all price polls from memory (chart poll is 20–300s, so
        45s staleness on the tab title is invisible)."""
        now = time.time()
        if now - _price_cache["ts"] > 45:
            try:
                snap = {}
                for m in client.get_markets():
                    t = m.get("ticker", {}) or m
                    try:
                        snap[m.get("symbol", "")] = float(t.get("price") or t.get("last") or 0)
                    except (TypeError, ValueError):
                        continue
                if snap:
                    _price_cache["data"] = snap
                    _price_cache["ts"] = now
            except Exception as e:
                log.debug("price snapshot failed: %s", e)
        px = _price_cache["data"].get(symbol, 0.0)
        return {"symbol": symbol, "price": px}

    # ── settings ───────────────────────────────────────────────────
    @app.get("/api/settings")
    def get_settings():
        """Return settings state — NEVER expose the actual API key."""
        # FIX(net-degrade): CoinEx-style region blocks made /api/settings 500
        # on every page load. Degrade gracefully and surface the reason.
        try:
            stats = markets.stats()
        except Exception as e:
            stats = {"error": _friendly_fetch_error(e)}
        try:
            quotes.refresh()
        except Exception:
            pass
        _mkt_err = markets.last_error or (stats.get("error") if isinstance(stats, dict) else "")
        if _mkt_err:
            _mkt_err = _friendly_fetch_error(Exception(_mkt_err))
        return {
            "markets_error": _mkt_err,
            "has_api_key": bool(client.api_key),
            "has_api_secret": bool(getattr(client, "api_secret", "")),
            "mode": "live" if app.state.live_enabled else "paper",
            "live_allowed": app.state.live_allowed,
            "symbols": engine.symbols,
            "api_min_gap_sec": cfg["engine"].get("api_min_gap_sec", 12),
            "markets": stats,
            "quotes": quotes.status(),
        }

    @app.get("/api/quotes")
    def get_quotes():
        """Real-time TMN/USDT conversion rates + availability flag."""
        quotes.refresh()
        return quotes.status()

    @app.get("/api/markets")
    def get_markets(quote: str = "", spot_only: bool = True):
        """List available Wallex markets, optionally filtered by quote family."""
        try:
            entries = markets.entries(quote=quote or None)
            out = [
                {
                    "symbol": s,
                    "quote": e["quote"],
                    "fa_name": e["fa_name"],
                    "en_name": e["en_name"],
                    "is_margin": e["is_margin"],
                    "price": e.get("price") or 0.0,
                }
                for s, e in entries.items()
                if not spot_only or not e.get("is_margin")
            ]
            out.sort(key=lambda x: x["symbol"])
            return out
        except Exception as e:
            raise HTTPException(502, _friendly_fetch_error(e))

    @app.post("/api/markets/refresh")
    def post_markets_refresh():
        """Force re-fetch of the Wallex market catalog."""
        try:
            n = len(markets.refresh(force=True))
            return {"ok": True, "markets": markets.stats()}
        except Exception as e:
            raise HTTPException(502, str(e))

    @app.get("/api/markets/stats")
    def get_market_stats():
        return markets.stats()

    @app.post("/api/symbols")
    def set_symbols(payload: dict):
        """Hot-swap engine symbol list from the dashboard.
        The FULL user list is persisted as the master list — quote filtering
        happens at engagement time, never destroys the user's choices."""
        syms = payload.get("symbols") or []
        if not isinstance(syms, list):
            return {"ok": False, "error": "symbols must be a list"}
        raw = [str(s).upper().strip() for s in syms if str(s).strip()]
        # Only accept symbols that actually exist in the Wallex market catalog —
        # drop typos/permitted-but-nonexistent pairs like "BTDUSDT".
        # Fetch the catalog once (disk-cached), not once-per-symbol.
        try:
            available = markets.entries()
        except Exception as exc:
            log.warning("symbol validation catalog unavailable: %s", exc)
            available = {}
        valid = [s for s in raw if s in available]
        clean = list(dict.fromkeys(valid))  # preserve order, dedupe
        rejected = [s for s in raw if s not in clean]
        storage.kv_set("symbols_master", json.dumps(clean))
        engine.set_symbols(clean)
        storage.log_event(int(time.time()), "symbols_updated", "",
                          f"engine now watching {len(clean)} symbols; rejected {len(rejected)} invalid")
        return {"ok": True, "symbols": engine.symbols,
                "accepted": clean, "rejected": rejected,
                "error": (f"{len(rejected)} نماد نامعتبر حذف شدند: {', '.join(rejected)}" if rejected else None)}

    @app.post("/api/settings")
    def save_settings(payload: dict):
        """Save OR delete the API key (+ optional signed-key secret). Key is
        never returned. Pass {api_key: "..."} to set/update; {api_secret: "..."}
        to set the private/seed credential for Ed25519 exchanges; pass
        {delete_api_key: true} to remove both."""
        # delete path first
        if payload.get("delete_api_key") is True:
            if app.state.live_enabled:
                return {"ok": False, "error": "ابتدا حالت معامله را به Paper بازگردانید، سپس کلید را حذف کنید"}
            store.delete("wallex_api_key")         # remove the entry entirely from encrypted store
            store.delete("wallex_api_secret")
            client.api_key = ""
            client.api_secret = ""
            storage.log_event(int(time.time()), "api_key_deleted", "", "user deleted API key via dashboard")
            return {"ok": True, "has_api_key": False, "has_api_secret": False}
        new_key = payload.get("api_key", "").strip()
        new_secret = payload.get("api_secret", "").strip()
        if new_key or new_secret:
            if new_key:
                store.set("wallex_api_key", new_key)
                client.api_key = new_key
                storage.log_event(int(time.time()), "api_key_updated", "", "user updated API key via dashboard")
            if new_secret:
                store.set("wallex_api_secret", new_secret)
                client.api_secret = new_secret
                storage.log_event(int(time.time()), "api_secret_updated", "", "user updated API secret via dashboard")
            return {"ok": True, "has_api_key": bool(client.api_key),
                    "has_api_secret": bool(client.api_secret)}
        return {"ok": False, "error": "api_key is required"}

    @app.get("/api/live/balance")
    def live_balance():
        """Read-only live exchange balance summary (read-only, NO trading)."""
        if not client.api_key:
            return {"ok": False, "error": "کلید API تنظیم نشده است"}
        try:
            raw = client.get_balances()
        except Exception as e:
            raise HTTPException(502, f"دریافت موجودی از والکس ناموفق بود: {e}")
        coins = {}
        for asset, info in raw.items():
            try:
                free = float(info.get("available", info.get("value", 0)))
                locked = float(info.get("locked", 0))
            except (TypeError, ValueError):
                continue
            coins[asset] = {"available": free, "locked": locked}
        usdt_avail = coins.get("USDT", {}).get("available", 0.0)
        tmn_avail = coins.get("TMN", {}).get("available", 0.0)
        usdt_locked = coins.get("USDT", {}).get("locked", 0.0)
        tmn_locked = coins.get("TMN", {}).get("locked", 0.0)
        return {
            "ok": True,
            "usdt_available": round(usdt_avail, 6),
            "tmn_available": round(tmn_avail, 6),
            "usdt_locked": round(usdt_locked, 6),
            "tmn_locked": round(tmn_locked, 6),
            "assets": {k: {"available": round(v["available"], 8), "locked": round(v["locked"], 8)} for k, v in coins.items()},
        }

    @app.get("/api/manual/symbols")
    def manual_symbols():
        """Symbols for the manual trade dropdown.
        - Paper mode: engine symbols + any cached wallet symbols with balance
        - Live mode: engine symbols + wallet symbols with non-zero balance
        Smart: caches live balance to disk to avoid repeated API calls.
        """
        out = set(engine.symbols or [])
        try:
            if client.api_key:
                # Use disk-cached balance first to avoid extra API calls
                cached_assets = {}
                try:
                    c = storage.kv_get("manual_balance_cache")
                    if c and isinstance(c, dict):
                        cached_assets = c.get("assets", {})
                except Exception:
                    pass
                # Only fetch fresh if cache is empty or very old (>5 min)
                if not cached_assets:
                    try:
                        raw = client.get_balances()
                        for asset, info in raw.items():
                            try:
                                free = float(info.get("available", info.get("value", 0)))
                                locked = float(info.get("locked", 0))
                                if free > 0 or locked > 0:
                                    cached_assets[asset] = {"available": free, "locked": locked}
                            except (TypeError, ValueError):
                                continue
                        # Cache to disk
                        storage.kv_set("manual_balance_cache", {"ts": int(time.time()), "assets": cached_assets})
                    except Exception:
                        pass
                # Convert wallet assets to tradeable symbol format
                quote_family = getattr(engine.broker, "quote_currency", None) or "USDT"
                quote_family = quote_family.upper()
                for asset in cached_assets:
                    if asset == quote_family:
                        continue
                    sym = f"{asset}{quote_family}"
                    # Only add if it exists in Wallex catalog
                    try:
                        if markets.get(sym):
                            out.add(sym)
                    except Exception:
                        pass
        except Exception:
            pass
        return {"symbols": sorted(list(out))}

    # ── trading mode ───────────────────────────────────────────────
    @app.get("/api/mode")
    def get_mode():
        return {
            "mode": app.state.trade_mode,          # paper | spot | margin
            "live_allowed": app.state.live_allowed,
            "has_api_key": bool(client.api_key),
            # active exchange's leverage cap (profile rules), not the global cfg
            "max_risk_coef": _paper_rules().max_risk_coef,
            "live_margin_supported": bool(getattr(client, "live_margin_supported",
                                                    client.capabilities.margin)),
        }

    def _engage_live(mode: str) -> tuple:
        """Build a real LiveBroker, sync with the exchange, and swap engine.broker.
        Returns (ok, err_or_none). Live is authorized by PROVING the API key works:
        we fetch the real account balance below — if it returns data, the key is
        valid and live trading is permitted (no env-var / typed-confirmation gate)."""
        if not client.api_key:
            return (False, "ابتدا کلید API را در تب تنظیمات وارد کنید", {})
        # FIX(audit-H6b): WALLEX_LIVE_ALLOWED is a KILL-SWITCH again — set it
        # to "no" (env or config) to refuse engaging live even with a valid
        # key (shared machines / CI / incidents). Default stays enabled so
        # the documented key-proof flow is unchanged.
        if str(app.state.live_allowed is not False and
               os.environ.get("WALLEX_LIVE_ALLOWED", "yes")).strip().lower() == "no":
            return (False, "معامله واقعی غیرفعال است (WALLEX_LIVE_ALLOWED=no)", {})
        # Capability gate: live margin requires the adapter to actually EXECUTE
        # margin orders (margin_open endpoint wired). A profile where the
        # exchange HAS a margin product but the live endpoints aren't wired
        # (e.g. Nobitex — paper margin only) is refused here.
        if mode == "margin" and not getattr(client, "live_margin_supported", client.capabilities.margin):
            return (False, "این صرافی در پروفایل فعلی معاملات مارجین زنده ندارد؛ فقط اسپات. (پیپر مارجین در دسترس است.)", {})
        try:
            raw = client.get_balances()
            if not isinstance(raw, dict):
                return (False, "کلید API پاسخ حساب را برنگرداند؛ کلید را بررسی کنید", {})
        except Exception as e:
            return (False, f"کلید API نامعتبر است یا به حساب دسترسی ندارد: {e}", {})
        try:
            # FIX(C2-wiring): margin mode must use the MARGIN live broker — the
            # old code silently traded SPOT while the UI showed "margin".
            if mode == "margin":
                live_margin = LiveMarginBroker(client)
                live_margin.sync()  # reconcile margin positions from exchange
                live = live_margin
            else:
                live = LiveBroker(client)
                live.sync()  # reconcile real exchange state FIRST
        except Exception as e:
            return (False, f"اتصال به والکس برای معامله واقعی ناموفق بود: {e}", {})
        try:
            quotes.refresh()
            qst = quotes.status()
            live.update_quote_prices(
                tmn_usdt=float(qst.get("usdt_per_tmn") or 0.0),
                usdt_tmn=float(qst.get("tmn_per_usdt") or 0.0),
            )
        except Exception:
            pass
        engine.broker = live
        # FIX(audit-C4): paper positions must NOT survive the switch to live —
        # _manage_positions would place REAL exchange orders on their exits.
        engine.positions.clear()
        engine.snapshots.clear()
        app.state.live_enabled = True
        app.state.trade_mode = mode
        storage.kv_set("last_trade_mode", mode)
        storage.kv_set("last_paper_kind", app.state.paper_kind)
        try:
            eq = live.equity()
            if eq > 0:
                engine.peak_equity = eq
                if hasattr(engine, "_peak_ts"):
                    engine._peak_ts = int(time.time())
        except Exception:
            pass

        def _avail(asset: str) -> float:
            try:
                info = (raw or {}).get(asset, {}) or {}
                return float(info.get("available", info.get("value", 0)))
            except (TypeError, ValueError):
                return 0.0

        has_usdt = _avail("USDT") > 0
        has_tmn = _avail("TMN") > 0
        usdt_amt = _avail("USDT")
        tmn_amt = _avail("TMN")
        warnings: List[str] = []
        # Choose the pair-family base the user can actually spend. If they hold
        # both, prefer the larger balance (e.g. 1M TMN + dust USDT → TMN family).
        if has_tmn and (not has_usdt or tmn_amt > usdt_amt):
            live_family = "TMN"
            if has_usdt:
                warnings.append("کیف پول شما هم TMN و هم USDT دارد؛ موتور روی جفتهای TMN معامله میکند (هر بار فقط یک خانواده؛ ترجیح بر بزرگترین موجودی).")
            else:
                warnings.append("کیف پول شما فقط TMN دارد — موتور فقط روی جفتهای TMN معامله میکند.")
        elif has_usdt:
            live_family = "USDT"
            if has_tmn:
                warnings.append("کیف پول شما هم TMN و هم USDT دارد؛ موتور روی جفتهای USDT معامله میکند (هر بار فقط یک خانواده؛ ترجیح بر بزرگترین موجودی).")
        else:
            live_family = "USDT"
            warnings.append("کیف پول شما USDT/TMN ندارد؛ برای معامله واقعی ابتدا موجودی اضافه کنید.")
        setattr(live, "quote", live_family)
        setattr(live, "quote_currency", live_family)
        engaged = _symbol_universe(live_family)
        if engaged and engaged != engine.symbols:
            engine.set_symbols(engaged)
        storage.log_event(int(time.time()), "live_engaged", "",
                          f"real trading enabled ({mode}) quote={live_family} ({len(engaged)} syms)")
        return (True, None, {"has_usdt": has_usdt, "has_tmn": has_tmn, "quote_family": live_family,
                "warnings": warnings, "equity": round(live.equity(), 2), "symbols": engine.symbols})

    def _engage_paper(kind: str = "") -> None:
        """Swap back to a paper broker (fresh account, positions cleared)."""
        kind = kind or app.state.paper_kind
        q = _paper_quote(kind)
        new_broker = _make_paper_margin(quote=q) if kind == "margin" else _make_paper_spot(quote=q)
        engine.broker = new_broker
        engine.positions.clear()
        engine.snapshots.clear()
        engine.peak_equity = new_broker.equity()
        app.state.live_enabled = False
        app.state.trade_mode = "paper"
        storage.kv_set("last_trade_mode", "paper")
        storage.log_event(int(time.time()), "live_disengaged", "", "switched back to paper trading")

    # ── startup checkpoint restore ──────────────────────────────────
    # Runs AFTER `_engage_live` is defined so retries can actually call it.
    # If the last session ended in live mode with a saved API key, re-engage
    # live on startup so the dashboard reflects the actual persisted state.
    # Retry up to 3 times with backoff in case Wallex is temporarily down.
    _saved_mode = storage.kv_get("last_trade_mode") or "paper"
    _startup_restore_ok = False
    if _saved_mode in ("spot", "margin") and client.api_key:
        for attempt in range(3):
            try:
                ok, err, _info = _engage_live(_saved_mode)
                if ok:
                    log.info("startup restored live mode from checkpoint: %s", _saved_mode)
                    _startup_restore_ok = True
                    break
                else:
                    log.warning("startup live restore attempt %d failed: %s", attempt + 1, err)
            except Exception as exc:
                log.warning("startup live restore error attempt %d: %s", attempt + 1, exc)
            if attempt < 2:
                time.sleep(5 * (attempt + 1))
        if not _startup_restore_ok:
            log.warning("startup live restore failed after 3 attempts; staying in paper mode")

    @app.post("/api/mode")
    def set_mode(payload: dict):
        """Switch trading mode. Divert to paper, or genuinely engage the real
        exchange broker for spot/margin. Live modes require api key + live_allowed."""
        mode = payload.get("mode", "").strip().lower()
        if mode not in ("paper", "spot", "margin"):
            return {"ok": False, "error": "mode must be paper, spot or margin"}
        if mode == "paper":
            _engage_paper()
            storage.log_event(int(time.time()), "mode_changed", "", f"trading mode set to {mode}")
            return {"ok": True, "mode": mode, "broker": engine.broker.name, "live_enabled": app.state.live_enabled}
        # real spot / margin
        ok, err, info = _engage_live(mode)
        if not ok:
            return {"ok": False, "error": err}
        storage.log_event(int(time.time()), "mode_changed", "", f"real trading mode set to {mode}")
        return {"ok": True, "mode": mode, "broker": engine.broker.name, "live_enabled": app.state.live_enabled,
                "equity": round(engine.broker.equity(), 2), "quote_family": (info or {}).get("quote_family", "USDT"),
                "warnings": (info or {}).get("warnings", [])}

    # ── paper sub-mode: spot vs margin simulation ──────────────────
    @app.get("/api/paper-mode")
    def get_paper_mode():
        return {
            "paper_kind": app.state.paper_kind,   # spot | margin
            "broker": engine.broker.name,
            "shorts_enabled": isinstance(engine.broker, PaperMarginBroker),
            "risk_coef": float(cfg.get("margin", {}).get("risk_coef", 2.0)),
        }

    @app.post("/api/paper-mode")
    def set_paper_mode(payload: dict):
        """Switch the PAPER simulation between spot and margin.
        Resets the paper account (fresh capital) — open paper positions are closed.
        Also accepts an optional `quote` ('USDT' or 'TMN') to set the paper account currency.
        """
        if app.state.live_enabled:
            return {"ok": False, "error": "در حالت لایو نمی‌توان حالت پیپر را عوض کرد"}
        kind = payload.get("kind", "").strip().lower()
        if kind not in ("spot", "margin"):
            return {"ok": False, "error": "kind must be 'spot' or 'margin'"}
        # Capability gate: paper margin simulates the exchange's REAL margin
        # semantics — refused on exchanges without margin (spot-only profiles).
        if kind == "margin" and not client.capabilities.margin:
            return {"ok": False, "error": "این صرافی مارجین ندارد؛ حالت پیپر مارجین در دسترس نیست"}
        if kind == app.state.paper_kind and getattr(engine.broker, "quote_currency", "USDT") == (payload.get("quote") or _paper_quote(kind)).upper():
            # already active — but still re-sync symbols (master list may have
            # changed since; account is NOT reset)
            engaged = _symbol_universe((payload.get("quote") or _paper_quote(kind)).upper())
            if engaged and engaged != engine.symbols:
                engine.set_symbols(engaged)
            return {"ok": True, "paper_kind": kind, "quote_currency": getattr(engine.broker, "quote_currency", "USDT"),
                    "symbols": engine.symbols, "note": "already active"}
        quote = (payload.get("quote") or _paper_quote(kind)).upper()
        _fams = [x.upper() for x in client.quote_currencies] or ["USDT"]
        if quote not in _fams:
            quote = _fams[0]
        storage.kv_set(f"paper_quote_{kind}", quote)
        new_broker = _make_paper_margin(quote=quote) if kind == "margin" else _make_paper_spot(quote=quote)
        engine.broker = new_broker
        engine.positions.clear()
        engine.peak_equity = new_broker.equity()
        app.state.paper_kind = kind
        # ── engage symbols of the selected quote family ────────────────
        # master selection ∩ family; ALL family pairs if no selection.
        engaged = _symbol_universe(quote)
        if engaged:
            engine.set_symbols(engaged)
        storage.log_event(int(time.time()), "paper_mode_changed", "",
                          f"paper simulation switched to {kind} / {quote} "
                          f"({len(engaged)} {quote} symbols engaged, account reset)")
        return {"ok": True, "paper_kind": kind, "quote_currency": quote,
                "symbols": engine.symbols, "equity": new_broker.equity()}

    # ── paper balance: set / increase / decrease (persisted) ───────
    def _paper_balance_state() -> dict:
        b = engine.broker
        q = getattr(b, "quote_currency", None) or "USDT"
        is_live = isinstance(b, LiveBroker)
        if is_live:
            # Live wallet: show the ACTIVE quote-family available balance (TMN/USDT)
            # and its equity in that same currency — never a cross-conversion.
            balance = b.equity()
            equity = b.equity()
            capital = b.equity()
        else:
            balance = float(getattr(b, "cash", 0.0))
            equity = float(b.equity())
            capital = float(getattr(b, "starting_capital", 0.0))
        return {
            "paper_kind": app.state.paper_kind,
            "quote_currency": q,
            "mode": "live" if is_live else "paper",
            "live_enabled": app.state.live_enabled,
            "balance": round(balance, 2),
            "equity": round(equity, 2),
            "starting_capital": round(capital, 2),
            "open_positions": len(getattr(b, "positions", {}) or {}),
        }

    @app.get("/api/paper-balance")
    def get_paper_balance():
        return _paper_balance_state()

    @app.post("/api/paper-balance")
    def set_paper_balance(payload: dict):
        """Adjust the paper account balance locally.
        action: 'set' (new total), 'add' (increase), 'remove' (decrease).
        The balance is persisted per sub-mode (spot/margin) and survives restarts.
        'set' resets the account (closes open paper positions)."""
        if app.state.live_enabled:
            return {"ok": False, "error": "در حالت لایو موجودی پیپر قابل تغییر نیست"}
        action = str(payload.get("action", "set")).strip().lower()
        try:
            amount = float(payload.get("amount", 0))
        except (TypeError, ValueError):
            return {"ok": False, "error": "مبلغ نامعتبر است"}
        if action not in ("set", "add", "remove"):
            return {"ok": False, "error": "action must be set, add or remove"}
        if amount <= 0:
            return {"ok": False, "error": "مبلغ باید بزرگتر از صفر باشد"}
        kind = app.state.paper_kind
        cur = _paper_quote(kind)
        key = f"paper_balance_{kind}_{cur}"
        b = engine.broker
        if action == "set":
            # full reset with the new balance (Wallex-style fresh account)
            new_broker = _make_paper_margin(quote=cur) if kind == "margin" else _make_paper_spot(quote=cur)
            new_broker.cash = amount
            new_broker.starting_capital = amount
            engine.broker = new_broker
            engine.positions.clear()
            engine.snapshots.clear()
            storage.engine_state_del("opportunities_snapshot")  # forget restored opps (config changed)
            engine.peak_equity = new_broker.equity()
            storage.kv_set(key, str(amount))
            storage.log_event(int(time.time()), "paper_balance_set", "",
                              f"paper {kind}/{cur} balance reset to {amount}")
            return {"ok": True, **_paper_balance_state(), "note": "حساب با موجودی جدید بازنشانی شد"}
        if action == "add":
            new_eq = b.deposit(amount)
            storage.kv_set(key, str(b.starting_capital))
            storage.log_event(int(time.time()), "paper_balance_add", "", f"paper {kind}/{cur} +{amount}")
            return {"ok": True, **_paper_balance_state(), "equity": round(new_eq, 2)}
        # remove
        free = b.cash
        if amount > free:
            return {"ok": False, "error": f"موجودی آزاد {round(free, 2)} است؛ بیشتر از آن قابل برداشت نیست"}
        new_eq = b.withdraw(amount)
        storage.kv_set(key, str(b.starting_capital))
        storage.log_event(int(time.time()), "paper_balance_remove", "", f"paper {kind}/{cur} -{amount}")
        return {"ok": True, **_paper_balance_state(), "equity": round(new_eq, 2)}

    # ── paper dry-run: local mirror of exchange order pre-validation ─
    @app.post("/api/paper/dry-run")
    def paper_dry_run(payload: dict):
        """Local simulation of dry-run: validates an order against the ACTIVE
        exchange's own constraints (min size, collateral, price band, leverage
        cap from the profile rules) WITHOUT placing anything."""
        rules = _paper_rules()
        symbol = str(payload.get("symbol", "")).upper()
        side = str(payload.get("side", "long")).lower()
        try:
            qty = float(payload.get("qty", 0))
            price = float(payload.get("price", 0))
        except (TypeError, ValueError):
            return {"ok": False, "error": "qty/price نامعتبر"}
        if qty <= 0 or price <= 0:
            return {"ok": False, "error": "qty و price باید مثبت باشند"}
        kind = app.state.paper_kind
        if kind == "spot":
            err = rules.check_spot_order(symbol, qty, price)
            if err:
                return {"ok": False, "accepted": False, "reason": err}
            notional = qty * price
            return {"ok": True, "accepted": True, "mode": "spot",
                    "notional": round(notional, 2),
                    "fee": round(notional * _paper_fee_pct() / 100, 2)}
        # margin — validate the REQUESTED leverage against the exchange cap
        # (don't silently clamp: the preview must tell the user their value
        # is out of range, matching the paper broker's reject behavior).
        req_rc = float(payload.get("risk_coef", mcfg.get("risk_coef", 2.0)))
        if req_rc > rules.max_risk_coef:
            return {"ok": True, "accepted": False,
                    "reason": rules.check_margin_order(symbol, 1.0, req_rc, price, price)}
        risk_coef = rules.clamp_risk_coef(req_rc)
        notional = qty * price
        collateral = notional / risk_coef
        err = rules.check_margin_order(symbol, collateral, risk_coef, price, price)
        if err:
            return {"ok": True, "accepted": False, "reason": err}
        loan = max(notional - collateral, 0.0)
        mmr = float(mcfg.get("mmr_pct", 1.0)) / 100
        if side == "short":
            liq = price * (1 + (1 - mmr) / risk_coef)
        else:
            liq = price * (1 - (1 - mmr) / risk_coef)
        return {"ok": True, "accepted": True, "mode": "margin", "side": side,
                "risk_coef": risk_coef, "notional": round(notional, 2),
                "collateral": round(collateral, 2), "loan": round(loan, 2),
                "liquidation_price": round(liq, 2),
                "fee": round(notional * _paper_fee_pct() / 100, 2)}

    # ── spot trading ───────────────────────────────────────────────
    @app.get("/api/holdings")
    def holdings():
        """FIX(user req): 'Holding pair' — assets currently held.
        Paper: aggregated open spot positions (grid + manual buys) per symbol.
        Live: real wallet balances from the exchange API (non-quote assets).
        Used by the manual-trader section; persists per profile (paper state in
        SQLite, live read from the exchange)."""
        out = []
        b = engine.broker
        if getattr(b, "name", "") == "paper":
            agg: dict = {}
            for p in b.positions.values():
                if not p.is_open or p.qty <= 0:
                    continue
                a = agg.setdefault(p.symbol, {"symbol": p.symbol, "qty": 0.0, "cost": 0.0, "sources": set()})
                a["qty"] += p.qty
                a["cost"] += p.qty * p.entry
                a["sources"].add("grid" if p.entry_reason == "grid_legacy" else str(p.entry_reason))
            for sym, a in agg.items():
                px = b._prices.get(sym, 0.0) or (a["cost"] / a["qty"] if a["qty"] else 0.0)
                out.append({
                    "symbol": sym, "qty": round(a["qty"], 10),
                    "avg_entry": round(a["cost"] / a["qty"], 10) if a["qty"] else 0.0,
                    "price": round(px, 10),
                    "value": round(a["qty"] * px, 4),
                    "pnl": round(a["qty"] * px - a["cost"], 4),
                    "source": "paper",
                })
        elif hasattr(client, "get_balances"):
            try:
                raw = client.get_balances() or {}
                quote = getattr(b, "quote_currency", "USDT")
                for asset, info in (raw.items() if isinstance(raw, dict) else []):
                    try:
                        avail = float((info or {}).get("available", (info or {}).get("value", 0)) or 0)
                    except (TypeError, ValueError):
                        continue
                    if asset.upper() in (quote, "IRT", "RLS") or avail <= 0:
                        continue
                    out.append({"symbol": f"{asset.upper()}{quote}", "asset": asset.upper(),
                                "qty": round(avail, 10), "source": "live_wallet",
                                "avg_entry": 0.0, "price": 0.0, "value": 0.0, "pnl": 0.0})
            except Exception:
                pass
        return {"items": out, "quote": getattr(b, "quote_currency", "USDT"),
                "live": getattr(b, "name", "") == "live"}

    @app.get("/api/spot/balances")
    def spot_balances():
        if not client.api_key:
            raise HTTPException(401, "API key not configured")
        try:
            raw = client.get_balances()
        except Exception as e:
            raise HTTPException(502, str(e))
        # enrich each asset with a USDT-valued amount for the dashboard
        try:
            entries = markets.entries()
            prices = {sym: e.get("price") or 0.0 for sym, e in entries.items()}
        except Exception:
            prices = {}
        qp = getattr(engine.broker, "_quote_prices", {}) if isinstance(engine.broker, LiveBroker) else {}
        usdt_per_tmn = 0.0
        try:
            _qst = quotes.status()
            if (_qst.get("usdt_per_tmn") or 0) > 0:
                usdt_per_tmn = float(_qst["usdt_per_tmn"])   # 1 TMN in USDT
            elif (qp.get("TMN") or 0) > 0:
                usdt_per_tmn = float(qp["TMN"])
        except Exception:
            usdt_per_tmn = 0.0
        out = {}
        for asset, info in raw.items():
            try:
                free = float(info.get("available", info.get("value", 0)))
            except (TypeError, ValueError):
                continue
            sym_usdt = f"{asset}USDT"
            px = prices.get(sym_usdt, 0.0)
            if px:
                val_usdt = free * px
            elif asset.upper() == "TMN" and usdt_per_tmn:
                val_usdt = free * usdt_per_tmn
            elif asset.upper() == "USDT":
                val_usdt = free
            else:
                val_usdt = 0.0
            out[asset] = {
                "available": free,
                "locked": float(info.get("locked", 0)),
                "value": float(info.get("value", 0)),
                "value_usdt": round(val_usdt, 4),
            }
        return out

    @app.get("/api/spot/orders")
    def spot_orders():
        if not client.api_key:
            raise HTTPException(401, "API key not configured")
        try:
            return client.get_open_orders()
        except Exception as e:
            raise HTTPException(502, str(e))

    @app.get("/api/spot/history")
    def spot_history(market: str = "", page: int = 1):
        if not client.api_key:
            raise HTTPException(401, "API key not configured")
        try:
            return client.get_order_history(market=market or None, page=page)
        except Exception as e:
            raise HTTPException(502, str(e))

    # ── margin trading ─────────────────────────────────────────────
    @app.get("/api/margin/markets")
    def margin_markets():
        try:
            return client.margin_get_markets()
        except Exception as e:
            raise HTTPException(502, str(e))

    @app.get("/api/margin/ratio/{market}")
    def margin_ratio(market: str):
        try:
            return client.margin_get_ratio(market)
        except Exception as e:
            raise HTTPException(502, str(e))

    @app.post("/api/margin/loan")
    def margin_loan(payload: dict):
        """Min/max collateral + loan value for a market/risk_coef (pre-validation)."""
        market = payload.get("market", "")
        try:
            return client.margin_calculate_loan(
                market=market,
                side=payload.get("side", "long"),
                collateral=str(payload.get("collateral", "")),
                risk_coef=str(payload.get("risk_coef", "2")),
                open_price=str(payload.get("open_price", "")),
            )
        except Exception as e:
            raise HTTPException(502, str(e))

    @app.get("/api/margin/positions")
    def margin_positions(active: bool = True):
        if not client.api_key:
            raise HTTPException(401, "API key not configured")
        try:
            return client.margin_get_positions(active=active)
        except Exception as e:
            raise HTTPException(502, str(e))

    @app.get("/api/margin/pnl")
    def margin_pnl():
        if not client.api_key:
            raise HTTPException(401, "API key not configured")
        try:
            return client.margin_get_pnl()
        except Exception as e:
            raise HTTPException(502, str(e))

    @app.get("/api/margin/levels")
    def margin_levels(market: str = ""):
        if not client.api_key:
            raise HTTPException(401, "API key not configured")
        try:
            return client.margin_get_user_levels(market=market or None)
        except Exception as e:
            raise HTTPException(502, str(e))

    # ── ticker cache: avoids a markets fetch per dry-run/loan call ──
    _ticker_cache: dict = {"px": {}, "ts": 0.0}
    _TICKER_TTL = 30.0

    def _cached_price(market: str) -> str:
        now = time.time()
        if now - _ticker_cache["ts"] > _TICKER_TTL:
            try:
                entries = markets.entries()
                _ticker_cache["px"] = {s: (e.get("price") or 0.0) for s, e in entries.items()}
                _ticker_cache["ts"] = now
            except Exception:
                pass
        px = _ticker_cache["px"].get(market, 0.0)
        return str(px) if px else ""

    @app.post("/api/margin/dry-run")
    def margin_dry_run(payload: dict):
        """Preview a margin position BEFORE opening (liquidation price, fees).
        Auto-fetches current market price for open_price."""
        market = payload.get("market", "")
        try:
            # fetch current price so Wallex accepts open_price
            price = _cached_price(market)
            return client.margin_dry_run(
                market=market,
                side=payload.get("side", "long"),
                collateral=str(payload.get("collateral", "0")),
                risk_coef=str(payload.get("risk_coef", "1")),
                open_price=price,
                stop_loss=str(payload.get("stop_loss", "") or ""),
                take_profit=str(payload.get("take_profit", "") or ""),
            )
        except Exception as e:
            raise HTTPException(502, str(e))

    @app.post("/api/margin/open")
    def margin_open(payload: dict):
        """Open a REAL margin position — requires margin mode + live_allowed.
        Wallex requires a real open_price; auto-fetches live price if omitted."""
        if app.state.trade_mode != "margin":
            return {"ok": False, "error": "حالت معامله روی margin نیست"}
        if not app.state.live_allowed:
            return {"ok": False, "error": "معامله واقعی غیرمجاز است"}
        max_rc = cfg.get("margin", {}).get("max_risk_coef", 3.0)
        rc = min(float(payload.get("risk_coef", 1)), max_rc)
        market = payload.get("market", "")
        open_price = str(payload.get("open_price", "") or "")
        if not open_price or open_price == "0":
            try:
                open_price = str(client.get_ticker(market).get("price") or "")
            except Exception:
                open_price = ""
        if not open_price:
            return {"ok": False, "error": "قیمت باز کردن در دسترس نیست"}
        try:
            res = client.margin_open_position(
                market=market,
                side=payload.get("side", "long"),
                collateral=str(payload.get("collateral", "0")),
                risk_coef=f"{rc:.2f}",
                open_price=open_price,
                stop_loss=str(payload.get("stop_loss", "") or ""),
                take_profit=str(payload.get("take_profit", "") or ""),
            )
            storage.log_event(int(time.time()), "margin_open", market,
                              f"side={payload.get('side')} collateral={payload.get('collateral')} rc={rc}")
            return {"ok": True, "position": res}
        except Exception as e:
            raise HTTPException(502, str(e))

    @app.post("/api/margin/close/{position_id}")
    def margin_close(position_id: str, payload: Optional[dict] = Body(None)):
        """Close a REAL margin position. Wallex requires a real close price;
        auto-fetches live price for the position's market if omitted."""
        if app.state.trade_mode != "margin":
            return {"ok": False, "error": "حالت معامله روی margin نیست"}
        payload = payload or {}
        close_price = str(payload.get("price", "") or "")
        if not close_price or close_price == "0":
            # resolve market from the position, then fetch live price
            try:
                pos = client.margin_get_position(position_id)
                market = pos.get("market", "")
                if market:
                    close_price = str(client.get_ticker(market).get("price") or "")
            except Exception:
                close_price = ""
        if not close_price:
            return {"ok": False, "error": "قیمت بستن در دسترس نیست"}
        try:
            res = client.margin_close_position(position_id, price=close_price)
            storage.log_event(int(time.time()), "margin_close", position_id, "closed via dashboard")
            return {"ok": True, "position": res}
        except Exception as e:
            raise HTTPException(502, str(e))

    # ── control ────────────────────────────────────────────────────
    @app.get("/api/progress")
    def get_progress():
        """Real-time progress of long backend jobs (startup preload, forced refresh)
        plus per-TF candle freshness: how long ago each TF's last candle was WRITTEN
        to disk vs its bar size — the elapsed-time base the UI/refresh logic uses."""
        try:
            prog = dict(engine.progress or {"active": False, "phase": "idle", "done": 0, "total": 0})
        except Exception:
            prog = {"active": False, "phase": "idle", "done": 0, "total": 0}
        now = int(time.time())
        freshness = {}
        for sym in (engine.symbols or [])[:8]:
            per = {}
            for res, rsec in (("15", 900), ("60", 3600), ("240", 14400), ("1D", 86400)):
                candles = engine._candle_cache.get(f"{sym}:{res}", [])
                if not candles:
                    per[res] = {"last_write": None, "age_sec": None, "bars_due": None, "fresh": False}
                    continue
                last_ts = candles[-1].ts
                age = now - last_ts
                # bars_due excludes the still-forming bucket (same rule as the
                # engine's delta gate): a bar is "due" only when its bucket has
                # CLOSED. last_ts >= start of the previous closed bucket means
                # disk already holds the newest closed bar → 0 due.
                if last_ts >= ((now // rsec) * rsec - rsec):
                    due = 0
                else:
                    due = max(0, ((now // rsec) * rsec - rsec - last_ts) // rsec)
                per[res] = {
                    "last_write": last_ts,
                    "age_sec": age,
                    "bars_due": int(due),          # closed bars still missing from disk
                    "fresh": last_ts >= ((now // rsec) * rsec - rsec),  # newest closed bar already on disk
                }
            freshness[sym] = per
        prog["candle_freshness"] = freshness
        return prog

    @app.post("/api/engine/start")
    def start_engine():
        if not engine.running:
            engine.start()
        storage.kv_set("engine_running", "1")   # hybrid restore point
        return {"ok": True, "running": engine.running}

    @app.post("/api/engine/stop")
    def stop_engine():
        engine.stop()
        storage.kv_set("engine_running", "0")   # hybrid restore point
        return {"ok": True, "running": engine.running}

    # ── manual trading (market/limit/stop + TP/SL simulation) ──────
    @app.post("/api/trade/order")
    def trade_place(payload: dict):
        """Place a manual order. Paper: fully simulated. Live spot: mapped to
        Wallex LIMIT/MARKET/STOP_MARKET. Live margin: TP/SL simulated app-side."""
        symbol = str(payload.get("symbol", "")).upper().strip()
        side = str(payload.get("side", "")).lower()
        kind = str(payload.get("kind", "market")).lower()
        try:
            qty = float(payload.get("qty", 0))
            price = float(payload.get("price") or 0) or None
            tp = float(payload.get("tp") or 0)
            sl = float(payload.get("sl") or 0)
        except (TypeError, ValueError):
            return {"ok": False, "error": "qty/price/tp/sl نامعتبر"}
        if not symbol:
            return {"ok": False, "error": "نماد الزامی است"}
        if app.state.live_enabled:
            return _live_place_order(symbol, side, kind, qty, price, tp, sl)
        return engine.manual.place(symbol, side, kind, qty, price=price, tp=tp, sl=sl)

    def _live_place_order(symbol, side, kind, qty, price, tp, sl):
        """Live spot/margin mapping: LIMIT/MARKET/STOP_MARGIN with Dry-Run.
        Spot: native LIMIT/MARKET/STOP_MARKET.
        Margin: only market/limit supported via margin_open_position; TP/SL
        placed alongside if provided."""
        if kind in ("limit", "stop") and not price:
            return {"ok": False, "error": "برای limit/stop قیمت الزامی است"}
        try:
            qty_f = float(qty)
            price_f = float(price or 0)
            if price_f <= 0 and kind in ("limit", "stop"):
                return {"ok": False, "error": "قیمت نامعتبر"}
        except (TypeError, ValueError):
            return {"ok": False, "error": "qty/price نامعتبر"}
        side_w = "BUY" if side == "buy" else "SELL"
        if app.state.trade_mode == "margin":
            rules = _paper_rules()
            err = rules.check_margin_order(symbol, qty_f * price_f, 1.0, price_f, price_f)
            if err:
                return {"ok": False, "error": f"dry-run: {err}"}
            if price_f <= 0:
                price_f = float(_cached_price(symbol) or 0)
                if price_f <= 0:
                    return {"ok": False, "error": "قیمت بازار برای margin در دسترس نیست"}
            collateral = qty_f * price_f
            risk_coef = min(float(cfg.get("margin", {}).get("risk_coef", 2.0)), rules.max_risk_coef)
            try:
                dr = client.margin_dry_run(
                    market=symbol, side=side.lower(),
                    collateral=f"{collateral:.8f}",
                    risk_coef=f"{risk_coef:.2f}",
                    open_price=f"{price_f:.8f}",
                    stop_loss=f"{float(sl or 0):.8f}" if sl else "",
                    take_profit=f"{float(tp or 0):.8f}" if tp else "",
                )
                if not dr or (dr.get("id") is None and not dr.get("collateral")):
                    reason = dr.get("message") or dr.get("error") or "empty dry-run"
                    return {"ok": False, "error": f"margin dry-run rejected: {reason}"}
            except Exception as e:
                return {"ok": False, "error": f"margin dry-run failed: {e}"}
            try:
                res = client.margin_open_position(
                    market=symbol, side=side.lower(),
                    collateral=f"{collateral:.8f}",
                    risk_coef=f"{risk_coef:.2f}",
                    open_price=f"{price_f:.8f}",
                    stop_loss=f"{float(sl or 0):.8f}" if sl else "",
                    take_profit=f"{float(tp or 0):.8f}" if tp else "",
                )
            except Exception as e:
                return {"ok": False, "error": str(e)[:300]}
            storage.log_event(int(time.time()), "live_manual_margin", symbol,
                              f"{side} market collateral={collateral:.2f} risk_coef={risk_coef:.2f} tp={tp} sl={sl}")
            return {"ok": True, "order": res}
        order_type = {"market": "MARKET", "limit": "LIMIT", "stop": "STOP_MARKET"}[kind]
        try:
            rules = _paper_rules()
            err = rules.check_spot_order(symbol, qty_f, price_f if price_f > 0 else 0.0)
            if err:
                return {"ok": False, "error": f"dry-run: {err}"}
            body_price = f"{price_f:.8f}" if (kind == "limit" or order_type == "LIMIT") else None
            stop_price = f"{price_f:.8f}" if kind == "stop" else None
            res = client.place_order(
                symbol=symbol,
                side=side_w,
                order_type=order_type,
                quantity=f"{qty_f:.8f}",
                price=body_price,
                stop_price=stop_price,
                client_id=f"WLX_{uuid.uuid4().hex[:16].upper()}",
            )
            storage.log_event(int(time.time()), "live_manual_order", symbol,
                              f"{side} {kind} qty={qty_f} price={price_f} tp={tp} sl={sl}")
            return {"ok": True, "order": res}
        except Exception as e:
            return {"ok": False, "error": str(e)[:300]}

    @app.get("/api/trade/orders")
    def trade_orders():
        pending = [o for o in storage.manual_orders(["pending"]) ]
        history = storage.manual_orders(["filled", "position", "closed", "cancelled", "rejected"], limit=100)
        return {
            "pending": pending,
            "open_positions": engine.manual.open_summary(),
            "history": history,
        }

    @app.delete("/api/trade/orders/{oid}")
    def trade_cancel(oid: str):
        if app.state.live_enabled:
            try:
                client.cancel_order(oid)
                return {"ok": True, "note": "سفارش واقعی لغو شد"}
            except Exception as e:
                return {"ok": False, "error": str(e)[:200]}
        return engine.manual.cancel(oid)

    @app.post("/api/risk/reset")
    def reset_risk():
        """Manual drawdown reset — clears peak so DD=0 and entries resume."""
        try:
            engine.reset_drawdown()
            return {"ok": True, "peak_equity": engine.peak_equity, "equity": engine.broker.equity()}
        except Exception as e:
            raise HTTPException(500, str(e))

    def _reset_all_live():
            """LIVE-SAFE partial factory reset (user req): the real account must not
            be harmed and live routing must keep working. KEPT: trade-mode checkpoint
            (last_trade_mode/last_paper_kind), API keys, running grid workflows
            (runners re-read engine.broker every tick; live margin sync re-derives
            positions from the exchange), pending manual orders that map to real
            exchange orders, wallet holdings. RESET to defaults: local analytics
            only — trades, equity curve, events, api_log, AI caches, engine_state
            snapshots. No broker swap, no position close, no key deletion."""
            try:
                # 1) local analytics/history only
                with storage._lock, storage._connect() as con:
                    con.execute("DELETE FROM trades")
                    con.execute("DELETE FROM equity")
                    con.execute("DELETE FROM events")
                    con.execute("DELETE FROM api_log")
                    con.execute("DELETE FROM engine_state")  # opportunities snapshot
                    # NOTE: manual_orders + grid_snapshots KEPT (real-order state)
                # 2) AI model/provider caches (cosmetic; keys live in CryptoStore and
                # are intentionally NOT deleted in live mode)
                try:
                    for p in (Path(data_dir) / "ai_models_cache.json",):
                        if p.exists():
                            p.unlink()
                except Exception as e:
                    log.warning("live reset ai cache cleanup failed: %s", e)
                # 3) peak-equity baseline re-anchors to the CURRENT live equity
                try:
                    eq = engine.broker.equity()
                    if eq > 0:
                        engine.peak_equity = eq
                        if hasattr(engine, "_peak_ts"):
                            engine._peak_ts = int(time.time())
                except Exception:
                    pass
                # engine routing untouched: broker, positions, grids, symbols stay
                storage.log_event(int(time.time()), "factory_reset_live", "",
                                  "live-safe reset: analytics/history cleared; "
                                  "mode checkpoint, keys, grids, orders kept")
                return {"ok": True, "mode": "live-partial", "equity": round(engine.broker.equity(), 2),
                        "quote": getattr(engine.broker, "quote_currency", "USDT"),
                        "symbols": engine.symbols,
                        "grids_running": len(grid_manager.running_profiles()),
                        "note": "ریست ایمن لایو: فقط تاریخچه و آمار محلی پاک شد؛ حساب، کلیدها، گریدهای فعال و سفارشات دست نخورد"}
            except Exception as e:
                raise HTTPException(500, str(e))

    @app.post("/api/reset-all")
    def reset_all(payload: dict = Body(None)):
        """Factory reset: paper balances/kv to defaults, fresh broker,
        clear open positions AND all persisted records (trades, equity,
        events, api_log). Requires confirm phrase.
        LIVE mode: partial, account-safe reset — real-money state is kept
        (mode checkpoint, API keys, running grids, live margin sync, pending
        manual orders backed by real exchange orders, wallet holdings), while
        only local analytics/history fall back to defaults (trades, equity,
        events, api_log, AI caches, drawings)."""
        # LIVE: partial account-safe reset (see docstring). PAPER: full reset.
        if app.state.live_enabled:
            if not payload or payload.get("confirm") != "RESET-ALL":
                return {"ok": False, "error": "تأیید لازم است: confirm=RESET-ALL"}
            return _reset_all_live()
        if not payload or payload.get("confirm") != "RESET-ALL":
            return {"ok": False, "error": "تأیید لازم است: confirm=RESET-ALL"}
            return {"ok": False, "error": "تأیید لازم است: confirm=RESET-ALL"}
        try:
            default_capital = float(mcfg.get("starting_capital", bcfg.get("starting_capital", 10000)))
            # 0) FIX(audit): stop ALL running grids BEFORE swapping the broker —
            # runners hold broker refs; stopping releases their reservations so
            # unspent capital is never stranded on the dead broker.
            try:
                for gid in list(grid_manager.runners.keys()):
                    if grid_manager.runners[gid].running:
                        grid_manager.stop(gid)
                grid_manager.runners.clear()
                try:
                    (grid_manager.base / "grids.json").unlink()
                except OSError:
                    pass
            except Exception as e:
                log.warning("factory reset grid cleanup failed: %s", e)
            # 1) wipe persisted settings (paper balance + symbol master + API key).
            # Preserve the quote keys across the kv purge (read now, restore after)
            # so TMN users stay on TMN — the blanket DELETE would otherwise wipe them.
            _keep_quote = {f"paper_quote_{kind}": storage.kv_get(f"paper_quote_{kind}")
                           for kind in ("spot", "margin")
                           if storage.kv_get(f"paper_quote_{kind}")}
            for k in ("paper_balance_spot_USDT", "paper_balance_spot_TMN",
                      "paper_balance_margin_USDT", "paper_balance_margin_TMN",
                      "paper_balance_spot", "paper_balance_margin",
                      "symbols_master"):
                storage.kv_set(k, "")
            try:
                store.delete("wallex_api_key")
                store.delete("wallex_api_secret")
                client.api_key = ""
                client.api_secret = ""
                api_key_deleted = True
            except Exception as e:
                log.warning("factory reset API key delete failed: %s", e)
                api_key_deleted = False
            # 2) remove kv rows entirely
            with storage._lock, storage._connect() as con:
                con.execute("DELETE FROM kv")
                for _k2, _v2 in _keep_quote.items():           # restore quote family
                    con.execute("INSERT OR REPLACE INTO kv(key, value) VALUES(?, ?)", (_k2, _v2))
                con.execute("DELETE FROM trades")
                con.execute("DELETE FROM equity")
                con.execute("DELETE FROM events")
                con.execute("DELETE FROM api_log")
                con.execute("DELETE FROM engine_state")  # wipe restored opportunities etc.
                con.execute("DELETE FROM manual_orders") # pending/old manual orders die
                con.execute("DELETE FROM grid_snapshots") # grid history dies
            # 2b) wipe AI model cache + provider configs (part of "everything to defaults")
            # AND the encrypted key slots that referenced them (CryptoStore)
            try:
                for p in (Path(data_dir) / "ai_models_cache.json", Path(data_dir) / "ai_providers.json"):
                    if p.exists():
                        p.unlink()
                try:
                    _all = store._load()
                    for _k in [k for k in _all if k.startswith("ai_key:")]:
                        store.delete(_k)
                    # Phase 6: per-profile credential slots also die with the profile:
                    # CMC key, wizard AI/search keys, this profile's exchange key.
                    for _k in ("cmc_api_key", "wizard_ai_key", "wizard_search_key",
                               f"exchange_key:{app.state.profile_id}",
                               "wallex_api_key", "wallex_api_secret",
                               f"exchange_key_secret:{app.state.profile_id}"):
                        store.delete(_k)
                except Exception:
                    pass
            except Exception as e:
                log.warning("factory reset ai cache cleanup failed: %s", e)
            # 2c) delete ALL external strategies + reset active to legacy (legacy persists)
            try:
                removed = 0
                for sid in strategy_store.list_ids():
                    if sid != "legacy":
                        strategy_store.delete(sid)
                        removed += 1
                _set_active_strategy_id("legacy")
                try:
                    engine.external_strategies = [strategy_store.legacy()]
                    engine.active_external_strategy = None
                except Exception:
                    pass
                log.info("factory reset: %d external strategies removed, active reset to legacy", removed)
            except Exception as e:
                log.warning("factory reset strategy cleanup failed: %s", e)
            # 3) fresh default spot broker, using the user's preserved quote family
            # so TMN users stay on TMN after reset. Balance is reset; master list cleared.
            reset_quote = _paper_quote("spot")
            new_broker = _make_paper_spot(quote=reset_quote)
            new_broker.cash = default_capital
            new_broker.starting_capital = default_capital
            engine.broker = new_broker
            engine.positions.clear()
            engine.snapshots.clear()
            engine.peak_equity = new_broker.equity()
            # FIX(audit): rebind the manual-order manager to the fresh broker and
            # drop its in-memory open positions — otherwise manual MT rows point
            # at the dead broker and 'Holding pair' shows ghost assets.
            try:
                engine.manual.broker = new_broker
                engine.manual.positions.clear()
            except Exception as e:
                log.warning("factory reset manual cleanup failed: %s", e)
            if hasattr(engine, "_peak_ts"):
                engine._peak_ts = int(time.time())
            if hasattr(engine, "_api_log_idx"):
                engine._api_log_idx = len(client.api_log)
            app.state.paper_kind = "spot"
            app.state.trade_mode = "paper"
            # 4) re-engage config.yaml default symbols
            engine.set_symbols(list(cfg.get("symbols", [])))
            storage.log_event(int(time.time()), "factory_reset", "",
                              f"all records cleared; paper spot/{default_capital} restored; "
                              f"{len(engine.symbols)} default symbols engaged")
            return {"ok": True, "equity": new_broker.equity(),
                    "symbols": engine.symbols, "paper_kind": "spot", "quote": reset_quote,
                    "has_api_key": bool(client.api_key),
                    "note": "همه تنظیمات و رکوردها به پیشفرض بازگشت"}
        except Exception as e:
            raise HTTPException(500, str(e))

    @app.post("/api/engine/tick")
    def tick_now():
        """Run one scan immediately (for testing) — still closed-candle only."""
        quotes.refresh()
        quote_err = None
        if app.state.trade_mode in ("spot", "margin"):
            st = quotes.status()
            if not st["available"] or st["missing"]:
                quote_err = f"نرخ تبدیل TMN/USDT در دسترس نیست (فاقد: {', '.join(st['missing'])})"
        if quote_err and app.state.live_enabled:
            log.warning("live tick skipped: %s", quote_err)
            return {"ok": False, "error": quote_err, "quotes": quotes.status()}
        # push quote prices into the broker so conversions in _notional_usdt work
        try:
            for sym, px in quotes.quotes.items():
                if px > 0:
                    engine.broker.set_price(sym, px)
        except Exception:
            pass
        try:
            engine.tick_once()
            return {"ok": True, "last_tick_ts": engine.last_tick_ts, "quotes": quotes.status()}
        except Exception as e:
            log.exception("manual tick failed: %s", e)
            return {"ok": False, "error": str(e)}

    @app.post("/api/live/enable")
    def enable_live(payload: dict):
        """Arm live trading. Authorization = a working API key, proven by fetching
        the account balance in _engage_live. No env-var and no typed confirmation
        phrase needed (the real balance fetch is the proof)."""
        mode = str(payload.get("mode") or "spot").strip().lower()
        if mode not in ("spot", "margin"):
            mode = "spot"
        ok, err, info = _engage_live(mode)
        if not ok:
            raise HTTPException(400, err or "معامله واقعی فعال نشد")
        storage.log_event(int(time.time()), "live_enabled", "", f"live trading engaged ({mode})")
        return {"ok": True, "mode": "live", "broker": engine.broker.name,
                "live_enabled": app.state.live_enabled, "equity": round(engine.broker.equity(), 2),
                "quote_family": (info or {}).get("quote_family", "USDT"),
                "warnings": (info or {}).get("warnings", [])}

    @app.post("/api/live/restore-checkpoint")
    def restore_checkpoint():
        """Manually retry startup live-mode restore from the saved checkpoint.
        Useful if startup restore failed because Wallex was temporarily down."""
        mode = storage.kv_get("last_trade_mode") or "paper"
        result = {"saved_mode": mode, "api_key_present": bool(client.api_key), "restored": False}
        if mode not in ("spot", "margin") or not client.api_key:
            return result
        ok, err, info = _engage_live(mode)
        result["restored"] = bool(ok)
        result["error"] = err
        if ok:
            storage.log_event(int(time.time()), "live_restored", "", f"manual checkpoint restore ({mode})")
            result["mode"] = mode
            result["broker"] = engine.broker.name
            result["live_enabled"] = app.state.live_enabled
            result["equity"] = round(engine.broker.equity(), 2)
            result["quote_family"] = (info or {}).get("quote_family", "USDT")
            result["warnings"] = (info or {}).get("warnings", [])
        return result

    # ── backtest ───────────────────────────────────────────────────
    @app.post("/api/backtest/download")
    def bt_download(payload: dict):
        symbols = payload.get("symbols") or cfg.get("symbols", [])
        days = int(payload.get("days", 120))
        report = {}
        for sym in symbols:
            try:
                data = download_symbol(client, data_dir, sym, days=days)
                report[sym] = {res: len(cs) for res, cs in data.items()}
            except Exception as e:
                report[sym] = {"error": str(e)[:200]}
        return report

    @app.get("/api/backtest/status")
    def bt_status():
        """Which symbols already have downloaded history (for the picker)."""
        import json as _json
        out = {}
        base = Path(data_dir) / "history"
        if not base.exists():
            return out
        for sym_dir in base.iterdir():
            if not sym_dir.is_dir():
                continue
            f = sym_dir / "15.json"
            try:
                n = len(_json.loads(f.read_text(encoding="utf-8")))
                out[sym_dir.name] = n
            except Exception:
                out[sym_dir.name] = 0
        return out

    @app.post("/api/backtest/run")
    def bt_run(payload: dict):
        symbols = payload.get("symbols") or cfg.get("symbols", [])
        mode = str(payload.get("mode", "spot")).lower()
        overrides = {
            k: payload[k] for k in ("max_positions", "risk_per_trade_pct", "risk_coef")
            if k in payload
        }
        # ── PREFLIGHT: ensure enough history in all 4 TFs before testing ──
        bt_days = int(payload.get("days", 120))
        _user_job_lock_ttl()  # A+B hybrid: yield background backfill to this user job
        pre = ensure_depth(client, data_dir, symbols, days=bt_days)
        # FIX(#9): the gate must GATE — a symbol whose top-up failed (network,
        # throttle, corrupt cache) is excluded with a clear message instead of
        # silently running a backtest on shallow/holey data.
        shallow = {s: r["detail"] for s, r in (pre.get("status") or {}).items() if not r.get("ok")}
        usable_symbols = [s for s in symbols if s not in shallow]
        if not usable_symbols:
            raise HTTPException(503, f"history insufficient for all symbols: {shallow}")
        histories = {}
        for sym in usable_symbols:
            h = load_symbol_history(data_dir, sym)
            if h.h15.candles and h.h60.candles:
                histories[sym] = h
        if not histories:
            raise HTTPException(400, "no history downloaded — call /api/backtest/download first")
        external_strategy = None
        strategy_id = payload.get("strategy_id")
        if strategy_id and str(strategy_id) != "legacy":
            # legacy artifact is a blank placeholder — the REAL legacy logic
            # (8-criteria) runs inside run_backtest when external_strategy=None
            try:
                external_strategy = strategy_store.load(str(strategy_id))
            except Exception as exc:
                raise HTTPException(400, f"strategy load failed: {exc}") from exc
        result: BacktestResult = run_backtest(histories, cfg, mode=mode, overrides=overrides, external_strategy=external_strategy, fast_1h_only=True)
        # live-vs-backtest comparison (paper PF from engine stats)
        live_stats = engine.stats()
        return {
            "metrics": result.metrics,
            "equity_curve": result.equity_curve[:: max(1, len(result.equity_curve) // 2000)],
            "trades": result.trades[-500:],
            "preflight": {"days": bt_days, "shallow": shallow, "downloaded": pre["downloaded"]},
            "compare": {
                "paper_profit_factor": live_stats.get("profit_factor"),
                "paper_win_rate": live_stats.get("win_rate"),
                "paper_trades": live_stats.get("closed_trades"),
                "paper_return_pct": round(
                    (live_stats.get("equity", 0) - engine.broker.starting_capital)
                    / max(engine.broker.starting_capital, 1) * 100.0, 2),
            },
        }

    @app.post("/api/backtest/walkforward")
    def bt_walk_forward(payload: dict):
        symbols = payload.get("symbols") or cfg.get("symbols", [])
        days = int(payload.get("window_days", 30))
        mode = str(payload.get("mode", "spot")).lower()
        overrides = {
            k: payload[k] for k in ("max_positions", "risk_per_trade_pct", "risk_coef")
            if k in payload
        }
        # FIX(#11): thread the selected strategy through to each window run —
        # walk-forward previously ALWAYS validated the legacy logic.
        external_strategy = None
        strategy_id = payload.get("strategy_id")
        if strategy_id and str(strategy_id) != "legacy":
            external_strategy = strategy_store.load(str(strategy_id))
            if external_strategy is None:
                raise HTTPException(400, f"strategy load failed: {strategy_id}")
        # ── PREFLIGHT: ensure enough history in all 4 TFs before testing ──
        _user_job_lock_ttl()  # A+B hybrid: yield background backfill to this user job
        ensure_depth(client, data_dir, symbols, days=max(days + 30, 60))
        histories = {}
        for sym in symbols:
            h = load_symbol_history(data_dir, sym)
            if h.h15.candles and h.h60.candles:
                histories[sym] = h
        if not histories:
            raise HTTPException(400, "no history downloaded — call /api/backtest/download first")
        return walk_forward(histories, cfg, window_days=max(days, 7), mode=mode,
                            overrides=overrides, external_strategy=external_strategy)

    # ── strategy store API ──────────────────────────────────────────
    @app.get("/api/strategies")
    def list_strategies():
        items = strategy_store.list_all()
        legacy = strategy_store.legacy()
        return {
            "legacy": legacy,
            "external": items,
            "active_id": _active_strategy_id(),
        }

    def _active_strategy_id() -> str:
        key = "active_strategy_id"
        try:
            val = storage.kv_get(key)
            if val:
                return str(val)
        except Exception:
            pass
        return "legacy"

    def _set_active_strategy_id(strategy_id: str) -> None:
        storage.kv_set("active_strategy_id", strategy_id or "legacy")

    @app.get("/api/strategies/{strategy_id}")
    def get_strategy(strategy_id: str):
        data = strategy_store.load(strategy_id)
        if data is None:
            raise HTTPException(404, "strategy not found")
        return data

    @app.patch("/api/strategies/{strategy_id}")
    def update_strategy(strategy_id: str, payload: dict):
        data = strategy_store.load(strategy_id)
        if data is None:
            raise HTTPException(404, "strategy not found")
        if "enabled" in payload:
            data["enabled"] = bool(payload["enabled"])
        if "name" in payload and isinstance(payload["name"], str):
            data["name"] = payload["name"][:120]
        if "description" in payload and isinstance(payload["description"], str):
            data["description"] = payload["description"][:500]
        if "timeframe" in payload:
            tf = str(payload["timeframe"])
            if tf not in ALLOWED_TIMEFRAMES:
                raise HTTPException(400, f"timeframe must be {'/'.join(ALLOWED_TIMEFRAMES)}")
            data["timeframe"] = tf
        if "cooldown_bars" in payload:
            try:
                data["cooldown_bars"] = max(1, int(payload["cooldown_bars"]))
            except (TypeError, ValueError):
                pass
        if "execution_mode" in payload:
            em = str(payload["execution_mode"]).lower()
            if em in ("auto", "grid", "signal", "tp_sl_dollar"):
                data["execution_mode"] = em
        if "min_confirmations" in payload:
            try:
                data["min_confirmations"] = max(1, min(5, int(payload["min_confirmations"])))
            except (TypeError, ValueError):
                pass
        if "min_confidence" in payload:
            try:
                data["min_confidence"] = min(1.0, max(0.0, float(payload["min_confidence"])))
            except (TypeError, ValueError):
                pass
        if "entry_conditions" in payload and isinstance(payload["entry_conditions"], list):
            data["entry_conditions"] = payload["entry_conditions"]
        if "exit_conditions" in payload and isinstance(payload["exit_conditions"], list):
            data["exit_conditions"] = payload["exit_conditions"]
        if "risk" in payload and isinstance(payload["risk"], dict):
            data.setdefault("risk", {}).update(payload["risk"])
        strategy_store.save(data)
        storage.log_event(int(time.time()), "strategy_updated", "", f"id={strategy_id}")
        return data

    @app.post("/api/strategies")
    def create_strategy(payload: dict):
        if not payload.get("strategy_id"):
            raise HTTPException(400, "strategy_id required")
        try:
            from .strategy_schema import validate_artifact
            validate_artifact(payload)
        except Exception as exc:
            raise HTTPException(400, f"invalid strategy: {exc}") from exc
        strategy_store.save(payload)
        storage.log_event(int(time.time()), "strategy_created", "", f"id={payload.get('strategy_id')}")
        return {"ok": True, "strategy_id": payload.get("strategy_id")}

    @app.post("/api/strategies/construct")
    @app.post("/api/strategies/{strategy_id}/construct")
    def construct_strategy(strategy_id: str = "ai-generated", payload: Optional[dict] = None):
        if not payload:
            raise HTTPException(400, "body required")
        vibe = str(payload.get("vibe", "") or "").strip()
        if not vibe:
            raise HTTPException(400, "vibe prompt required")
        provider = str(payload.get("provider", ai_client.cfg.provider)).lower()
        model = str(payload.get("model", ai_client.cfg.model)).strip()
        base_url = str(payload.get("base_url", "")).strip()
        api_key = str(payload.get("api_key", "")).strip()
        # FIX(user req): the ACTIVE AI config (بارگذاری شد) is the preferred
        # provider for every AI feature — unless the payload carries a real
        # key typed in this section.
        _act = _resolve_active_ai(payload)
        if _act and not api_key:
            provider = str(_act.get("provider") or provider).lower()
            base_url = str(_act.get("base_url") or base_url).strip()
            api_key = str(_act.get("api_key") or api_key).strip()
            model = str(_act.get("model") or model).strip()
        constraints = {
            "allowed_timeframes": list(ALLOWED_TIMEFRAMES),
            "execution_modes": ["auto", "grid", "signal", "tp_sl_dollar"],
            "allowed_indicators": sorted(ALLOWED_INDICATORS),
            "quote_families": ["USDT", "TMN"],
            "active_quote_family": getattr(app.state, "paper_quote", None) or "USDT",
            "engine_symbols": engine.symbols[:20],
            "supported_operators": [">", "<", ">=", "<=", "==", "crossover", "crossunder", "cross_above", "cross_below", "increase", "decrease"],
            "max_positions": int(cfg.get("engine", {}).get("max_positions", 4)),
            "risk_per_trade_pct": float(cfg.get("risk", {}).get("risk_per_trade_pct", 1.0)),
            "stop_atr_mult": float(cfg.get("strategy", {}).get("stop_atr_mult", 1.5)),
            "vibe_summary": vibe[:400],
        }
        try:
            if base_url or api_key or provider != ai_client.cfg.provider or model != ai_client.cfg.model:
                # per-request provider override (frontend passes saved config)
                eff_cfg = AIProviderConfig(
                    provider=provider,
                    base_url=base_url or ai_client.cfg.base_url,
                    api_key=api_key or ai_client.cfg.api_key,
                    model=model or ai_client.cfg.model,
                    timeout_sec=ai_client.cfg.timeout_sec,
                    retries=ai_client.cfg.retries,
                    retry_pause_sec=ai_client.cfg.retry_pause_sec,
                )
                eff_client = AIStrategyClient(eff_cfg)
            else:
                eff_client = ai_client
            artifact = eff_client.construct_strategy(vibe, constraints, provider=provider, model=model)
        except AIProviderError as exc:
            raise HTTPException(502, f"AI strategy construction failed: {exc}") from exc
        artifact["strategy_id"] = artifact.get("strategy_id") or strategy_id
        artifact["source"] = "external_ai"
        artifact.setdefault("enabled", True)
        # schema 1.0 artifacts (missing execution_mode) are upgraded, not rejected
        try:
            from .strategy_schema import validate_artifact
            validate_artifact(artifact)
        except ValueError as exc:
            if "schema version" in str(exc).lower():
                artifact["schema_version"] = "1.1"
                from .strategy_schema import validate_artifact as _va
                _va(artifact)
            else:
                raise HTTPException(502, f"AI artifact invalid: {exc}") from exc
        strategy_store.save(artifact)
        storage.log_event(int(time.time()), "strategy_constructed", "", f"id={artifact['strategy_id']} provider={provider} model={model}")
        return {"ok": True, "artifact": artifact}

    @app.post("/api/strategies/{strategy_id}/optimize")
    def optimize_strategy(strategy_id: str, payload: dict):
        """AI OPTIMIZER with VERIFICATION LOOP:
        1) fresh backtest of the base strategy
        2) AI diagnoses results → improved branch (new strategy_id, parent kept)
        3) AUTO-BACKTEST the new branch
        4) VERDICT: if the branch didn't improve (or even traded worse), AI goes
           back to the strategy's vibe description and RE-INTERPRETS the idea
           from scratch (round 2 branch). All steps reported to the caller.
        LEGACY IS ALLOWED here: the hardcoded 8-criteria logic can never be
        edited/deleted, but the AI may derive EXTERNAL branch strategies from
        its behavior (e.g. relax min_confirmations, add trend filter).
        If no vibe text exists, one is AUTO-WRITTEN from the base artifact."""
        data = strategy_store.load(strategy_id)
        if data is None:
            raise HTTPException(404, "strategy not found")
        is_legacy = strategy_id == "legacy"
        symbols = payload.get("symbols") or cfg.get("symbols", [])
        mode = str(payload.get("mode", "spot")).lower()
        provider = str(payload.get("provider", ai_client.cfg.provider)).lower()
        model = str(payload.get("model", ai_client.cfg.model)).strip()
        base_url = str(payload.get("base_url", "")).strip()
        api_key = str(payload.get("api_key", "")).strip()
        # Server-side secret resolution: if the frontend only sent a config_id,
        # pull provider/base_url/api_key from ai_providers.json directly — the
        # raw key never needs to travel through the browser.
        cfg_ref = _resolve_active_ai(payload)
        _payload_key_real = bool(api_key) and "…" not in api_key
        if cfg_ref and not _payload_key_real:
            provider = str(cfg_ref.get("provider") or provider).lower()
            base_url = str(cfg_ref.get("base_url") or base_url).strip()
            api_key = str(cfg_ref.get("api_key") or api_key).strip()
            if not model:
                model = str(cfg_ref.get("model") or "").strip()
        elif api_key and "…" in api_key:
            # A masked preview string snuck in (stale client cache) — never send
            # that to the provider; fall back to the default client config.
            api_key = ""
        bt_days = int(payload.get("days", 120))
        user_vibe = str(payload.get("vibe", "") or "").strip()

        # ── vibe persistence: data/Vibe strategies/<AI-named file> ──
        vibe_dir = Path(data_dir) / "Vibe strategies"
        vibe_path = None
        try:
            vibe_dir.mkdir(parents=True, exist_ok=True)
            vibe_name = str(data.get("name") or strategy_id)
            import re as _re
            fname = _re.sub(r"[^A-Za-z0-9\u0600-\u06FF_-]+", "_", vibe_name).strip("_")[:60] or strategy_id
            vibe_path = vibe_dir / f"{fname}.txt"
            if user_vibe:
                vibe_path.write_text(user_vibe, encoding="utf-8")
            elif vibe_path.exists():
                user_vibe = vibe_path.read_text(encoding="utf-8").strip()
            elif not is_legacy:
                # No vibe text exists for this strategy (e.g. first optimization of
                # a strategy created before the vibe hook). AUTO-WRITE a textbook
                # vibe description FROM the existing artifact so round 2 always has
                # a source-of-truth description to re-interpret.
                auto_vibe = _vibe_from_artifact(data)
                vibe_path.write_text(auto_vibe, encoding="utf-8")
                user_vibe = auto_vibe
                log.info("[optimize] auto-generated vibe text for %s -> %s", strategy_id, vibe_path.name)
        except Exception as exc:
            log.warning("vibe persistence failed: %s", exc)
            vibe_path = None

        def _score(m: dict) -> float:
            """Composite ranking score: return per trade, penalize drawdown."""
            tr = max(float(m.get("trades", 0) or 0), 1)
            return (float(m.get("return_pct", 0) or 0) / max(tr / 10.0, 1.0)) - 0.25 * float(m.get("max_drawdown_pct", 0) or 0)

        # ── 1) fresh backtest of the base strategy ──
        _user_job_lock_ttl()  # A+B hybrid: yield background backfill to this user job
        ensure_depth(client, data_dir, symbols, days=bt_days)
        histories = {}
        for sym in symbols:
            h = load_symbol_history(data_dir, sym)
            if h.h15.candles and h.h60.candles and h.h240.candles:
                histories[sym] = h
        if not histories:
            raise HTTPException(400, "no history for backtest")
        try:
            res_parent = run_backtest(histories, cfg, mode=mode, external_strategy=data, fast_1h_only=True)
        except Exception as exc:
            raise HTTPException(500, f"backtest failed: {exc}") from exc
        # ── 2) per-request AI client (saved provider config override) ──
        if base_url or api_key or provider != ai_client.cfg.provider or model != ai_client.cfg.model:
            eff_cfg = AIProviderConfig(
                provider=provider,
                base_url=base_url or ai_client.cfg.base_url,
                api_key=api_key or ai_client.cfg.api_key,
                model=model or ai_client.cfg.model,
                timeout_sec=ai_client.cfg.timeout_sec,
                retries=ai_client.cfg.retries,
                retry_pause_sec=ai_client.cfg.retry_pause_sec,
            )
            eff_client = AIStrategyClient(eff_cfg)
        else:
            eff_client = ai_client

        def _validate_and_save(art: dict) -> dict:
            art["strategy_id"] = art.get("strategy_id") or f"{strategy_id}-branch"
            art.setdefault("enabled", True)
            from .strategy_schema import validate_artifact
            try:
                validate_artifact(art)
            except ValueError as exc:
                if "schema version" in str(exc).lower():
                    art["schema_version"] = "1.1"
                    from .strategy_schema import validate_artifact as _va
                    _va(art)
                else:
                    raise HTTPException(502, f"AI artifact invalid: {exc}") from exc
            strategy_store.save(art)
            return art

        loop_report = []
        # ── 3) round 1: AI diagnoses results and proposes the improved branch ──
        try:
            branch1 = eff_client.optimize_strategy(
                base_artifact=data,
                backtest_metrics=res_parent.metrics,
                sample_trades=res_parent.trades[-40:],
                symbols=symbols,
                mode=mode,
                provider=provider,
                model=model,
            )
        except AIProviderError as exc:
            raise HTTPException(502, f"AI optimization failed: {exc}") from exc
        branch1 = _validate_and_save(branch1)
        res_b1 = run_backtest(histories, cfg, mode=mode, external_strategy=branch1, fast_1h_only=True)
        s_parent, s_b1 = _score(res_parent.metrics), _score(res_b1.metrics)
        loop_report.append({
            "step": "round1_parameter_tuning",
            "branch_id": branch1["strategy_id"],
            "metrics": res_b1.metrics,
            "score": round(s_b1, 3),
            "better_than_parent": s_b1 > s_parent,
        })
        storage.log_event(int(time.time()), "strategy_optimized", "",
                          f"parent={strategy_id} branch={branch1['strategy_id']} "
                          f"parent_score={s_parent:.2f} branch1_score={s_b1:.2f}")

        # ── 4) verification: if round-1 branch did NOT beat the parent → round 2 ──
        final_art, final_res = branch1, res_b1
        verdict = "improved"
        if s_b1 <= s_parent:
            verdict = "reinterpreted_from_vibe"
            if is_legacy and not user_vibe:
                # Legacy's hardcoded logic has no vibe — write a behavioral
                # description of the 8-criteria grid system so the AI can
                # derive an EXTERNAL strategy from it (legacy itself stays
                # locked and untouched).
                user_vibe = (
                    "استراتژی Legacy والکس — سیستم تلاقی ۸ معیاری با شبکه (گرید):\n"
                    "ورود لانگ فقط وقتی ساختار روند صعودی تأیید شود (ساختار ۴ساعته، شکست ساختار اخیر)، قیمت روی یک سطح معنادار (حمایت/مقاومت) باشد، "
                    "یک الگوی کندلی تأییدی روی کندل بسته‌شده ۱ساعته دیده شود، RR حداقل ۱.۵ باشد، حجم از میانگین بالاتر باشد، و حداقل ۶ معیار از ۸ معیار همزمان تأیید کنند "
                    "(ساختار، سطح، الگو، RR، حجم، جهت EMA20، فیلتر نوسان ATR، عدم همپوشانی با موقعیت باز).\n"
                    "اندازه موقعیت بر اساس ریسک ۱٪ و فاکتور شبکه (نزدیکی قیمت به سطوح گرید روزانه) است.\n"
                    "خروج‌ها: حد ضرر زیر ساختار/ATR، انتقال به سربه‌سر در ۱.۵R، خروج پله‌ای ۵۰٪ در ۲.۵R، تریلینگ با ۱.۲×ATR، خروج ساختاری روی CHOCH معکوس.\n"
                    "این توضیح رفتاری است — هدف بهینه‌ساز این است که یک استراتژی خارجی با همین روح اما قابل‌تنظیم بسازد (مثلاً حد نصاب تأییدیه کمتر برای معاملات بیشتر، یا فیلتر روند قدرتمندتر برای دقت بالاتر)."
                )
                try:
                    vibe_path.write_text(user_vibe, encoding="utf-8")
                    log.info("[optimize] auto-generated behavioral vibe for legacy -> %s", vibe_path.name)
                except Exception:
                    pass
            attempts_summary = (
                f"original strategy score={s_parent:.2f} (metrics: {json.dumps(res_parent.metrics, ensure_ascii=False)[:400]})\n"
                f"parameter-tuned branch score={s_b1:.2f} (metrics: {json.dumps(res_b1.metrics, ensure_ascii=False)[:400]})\n"
                f"changes tried in round1: see branch artifact below. Parameter tuning did NOT produce a better strategy.\n"
                f"vibe available: {'yes' if user_vibe else 'no'}"
            )
            if user_vibe:
                try:
                    branch2 = eff_client.regenerate_from_vibe(
                        vibe=user_vibe,
                        base_artifact=branch1,
                        attempts_summary=attempts_summary,
                        constraints={
                            "allowed_timeframes": list(ALLOWED_TIMEFRAMES),
                            "execution_modes": ["auto", "grid", "signal", "tp_sl_dollar"],
                            "allowed_indicators": sorted(ALLOWED_INDICATORS),
                            "supported_operators": [">", "<", ">=", "<=", "==", "crossover", "crossunder", "cross_above", "cross_below", "increase", "decrease"],
                            "quote_families": ["USDT", "TMN"],
                            "note_from_legacy": ("The base is the app's LOCKED legacy 8-criteria grid strategy. "
                                                 "You cannot change legacy itself — derive an external, tunable strategy from its described behavior.") if is_legacy else "",
                        },
                        provider=provider,
                        model=model,
                    )
                    branch2 = _validate_and_save(branch2)
                    res_b2 = run_backtest(histories, cfg, mode=mode, external_strategy=branch2, fast_1h_only=True)
                    s_b2 = _score(res_b2.metrics)
                    loop_report.append({
                        "step": "round2_reinterpreted_from_vibe",
                        "branch_id": branch2["strategy_id"],
                        "metrics": res_b2.metrics,
                        "score": round(s_b2, 3),
                        "better_than_parent": s_b2 > s_parent,
                    })
                    # pick the best of the two branches (parent stays untouched either way)
                    if s_b2 > s_b1:
                        final_art, final_res = branch2, res_b2
                    storage.log_event(int(time.time()), "strategy_regenerated", "",
                                      f"parent={strategy_id} branch2={branch2['strategy_id']} score={s_b2:.2f}")
                except AIProviderError as exc:
                    loop_report.append({"step": "round2_failed", "error": str(exc)[:200]})
            else:
                loop_report.append({"step": "round2_skipped", "reason": "no vibe description available for this strategy"})
        for a in loop_report:
            log.info("[optimize-loop] %s=%s score=%s better=%s", a.get("step"), a.get("branch_id", a.get("error", ""))[:60], a.get("score"), a.get("better_than_parent"))
        return {
            "ok": True,
            "artifact": final_art,
            "parent_metrics": res_parent.metrics,
            "parent_id": strategy_id,
            "verdict": verdict,
            "loop_report": loop_report,
            "vibe_saved_to": str(vibe_path) if vibe_path else None,
        }

    @app.post("/api/strategies/{strategy_id}/backtest")
    def strategy_backtest(strategy_id: str, payload: dict):
        data = strategy_store.load(strategy_id)
        if data is None:
            raise HTTPException(404, "strategy not found")
        symbols = payload.get("symbols") or cfg.get("symbols", [])
        mode = str(payload.get("mode", "spot")).lower()
        overrides = {
            k: payload[k] for k in ("max_positions", "risk_per_trade_pct", "risk_coef")
            if k in payload
        }
        # ── PREFLIGHT: ensure enough history in all 4 TFs before testing ──
        bt_days = int(payload.get("days", 120))
        _user_job_lock_ttl()  # A+B hybrid: yield background backfill to this user job
        pre = ensure_depth(client, data_dir, symbols, days=bt_days)
        # FIX(#9): exclude symbols whose top-up failed instead of silently
        # backtesting on shallow data.
        shallow = {s: r["detail"] for s, r in (pre.get("status") or {}).items() if not r.get("ok")}
        usable_symbols = [s for s in symbols if s not in shallow]
        if not usable_symbols:
            raise HTTPException(503, f"history insufficient for all symbols: {shallow}")
        histories = {}
        for sym in usable_symbols:
            h = load_symbol_history(data_dir, sym)
            if h.h15.candles and h.h60.candles:
                histories[sym] = h
        if not histories:
            raise HTTPException(400, "no history downloaded — call /api/backtest/download first")
        try:
            # legacy artifact is a blank placeholder — pass None so run_backtest
            # uses the REAL legacy 8-criteria logic (build_signal/build_signal_short)
            ext = None if strategy_id == "legacy" else data
            result: BacktestResult = run_backtest(
                histories, cfg, mode=mode, overrides=overrides,
                external_strategy=ext, fast_1h_only=True,
            )
        except Exception as exc:
            raise HTTPException(500, f"backtest failed: {exc}") from exc
        out = {
            "metrics": result.metrics,
            "trades": result.trades[-200:],
        }
        storage.log_event(int(time.time()), "strategy_backtest", "", f"id={strategy_id} trades={result.metrics.get('trades', 0)}")
        return out

    @app.post("/api/strategies/{strategy_id}/activate")
    def activate_strategy(strategy_id: str):
        data = strategy_store.load(strategy_id)
        if data is None:
            raise HTTPException(404, "strategy not found")
        _set_active_strategy_id(strategy_id)
        # update running engine immediately so active strategy takes effect without restart
        # FIX(#1): no silent fallback — if the requested strategy is disabled or
        # missing, activation surfaces an error instead of picking another.
        try:
            eng = app.state.engine
            if strategy_id == "legacy":
                eng.active_external_strategy = None
            else:
                match = next((s for s in eng.external_strategies if s.get("strategy_id") == strategy_id and s.get("enabled", False)), None)
                if match is None:
                    raise HTTPException(400, f"strategy '{strategy_id}' is disabled or not loaded — enable it first")
                eng.active_external_strategy = match
        except HTTPException:
            raise
        except Exception:
            pass
        storage.log_event(int(time.time()), "strategy_activated", "", f"id={strategy_id}")
        return {"ok": True, "active_id": strategy_id}

    @app.post("/api/ai/test")
    def test_ai_connection(payload: dict):
        """Step 1 of model discovery: connection + auth check with diagnosis."""
        provider = str(payload.get("provider", ai_client.cfg.provider)).lower()
        base_url = str(payload.get("base_url", ai_client.cfg.base_url)).strip()
        api_key = str(payload.get("api_key", ai_client.cfg.api_key or "")).strip()
        cfg_ref = _resolve_active_ai(payload)
        if cfg_ref:
            provider = str(cfg_ref.get("provider") or provider).lower()
            base_url = str(cfg_ref.get("base_url") or base_url).strip()
            api_key = str(cfg_ref.get("api_key") or api_key).strip()
        elif api_key and "…" in api_key:
            api_key = ""  # masked preview never sent to a provider
        if not base_url:
            raise HTTPException(400, "آدرس API خالی است — آدرس پایه را وارد کنید")
        return ai_client.test_connection(provider, base_url, api_key)

    @app.post("/api/ai/models")
    def list_ai_models(payload: dict):
        provider = str(payload.get("provider", ai_client.cfg.provider)).lower()
        base_url = str(payload.get("base_url", ai_client.cfg.base_url)).strip()
        api_key = str(payload.get("api_key", ai_client.cfg.api_key or "")).strip()
        model = str(payload.get("model", ai_client.cfg.model)).strip()
        # Server-side secret resolution (same pattern as optimize): the frontend
        # may only reference a saved config by id instead of sending the key.
        cfg_ref = _resolve_active_ai(payload)
        if cfg_ref:
            provider = str(cfg_ref.get("provider") or provider).lower()
            base_url = str(cfg_ref.get("base_url") or base_url).strip()
            api_key = str(cfg_ref.get("api_key") or api_key).strip()
        elif api_key and "…" in api_key:
            api_key = ""  # masked preview never sent to a provider
        try:
            models = ai_client.discover_models(provider, base_url, api_key, model)
        except AIProviderError as exc:
            # fall back to cached models for this provider if discovery fails
            cached = _load_cached_models(provider, base_url)
            if cached:
                return {
                    "provider": provider, "base_url": base_url,
                    "models": cached, "cached": True,
                    "warning": f"{exc} — مدلهای ذخیرهشده قبلی نمایش داده میشود",
                }
            raise HTTPException(502, str(exc)) from exc
        # success: cache models to disk keyed by provider+base_url
        _save_cached_models(provider, base_url, models)
        return {"provider": provider, "base_url": base_url, "models": models, "cached": False}

    def _models_cache_path() -> Path:
        return Path(data_dir) / "ai_models_cache.json"

    def _cache_key(provider: str, base_url: str) -> str:
        return f"{provider}::{(base_url or '').strip().rstrip('/').lower()}"

    def _load_cached_models(provider: str, base_url: str) -> List[str]:
        try:
            path = _models_cache_path()
            if not path.exists():
                return []
            data = json.loads(path.read_text(encoding="utf-8"))
            return data.get(_cache_key(provider, base_url), [])
        except Exception:
            return []

    def _save_cached_models(provider: str, base_url: str, models: List[str]) -> None:
        try:
            path = _models_cache_path()
            data = {}
            if path.exists():
                try:
                    data = json.loads(path.read_text(encoding="utf-8"))
                except Exception:
                    data = {}
            data[_cache_key(provider, base_url)] = models
            data["_updated_ts"] = int(time.time())
            path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception as exc:
            log.warning("ai models cache save failed: %s", exc)

    @app.post("/api/ai/context-check")
    def ai_context_check(payload: dict):
        """Context-length probe for a model — used by the wizard + strategy
        generator/optimizer to warn when a local model's context is too small
        for the full configuration prompt (docs corpus, schema, history)."""
        provider = str(payload.get("provider", ai_client.cfg.provider)).lower()
        base_url = str(payload.get("base_url", ai_client.cfg.base_url)).strip()
        api_key = str(payload.get("api_key", ai_client.cfg.api_key or "")).strip()
        model = str(payload.get("model", payload.get("model_id", ""))).strip()
        cfg_ref = _resolve_active_ai(payload)
        if cfg_ref:
            provider = str(cfg_ref.get("provider") or provider).lower()
            base_url = str(cfg_ref.get("base_url") or base_url).strip()
            api_key = str(cfg_ref.get("api_key") or api_key).strip()
            if not model:
                model = str(cfg_ref.get("model") or "").strip()
        elif api_key and "…" in api_key:
            api_key = ""
        if not model:
            return {"ok": False, "context_len": 0, "source": "", "detail": "no model selected",
                    "recommended_min": _AI_CTX_MIN, "sufficient": False}
        res = ai_client.model_context_len(provider, base_url, api_key, model)
        res["recommended_min"] = _AI_CTX_MIN
        res["sufficient"] = bool(res.get("ok") and res.get("context_len", 0) >= _AI_CTX_MIN)
        return res

    @app.get("/api/ai/configs")
    def list_ai_configs():
        """List saved provider configs. api_key values are NEVER returned —
        only has_api_key (presence) and a short masked preview for UX."""
        path = Path(data_dir) / "ai_providers.json"
        if not path.exists():
            return {"items": []}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            items = data.get("items", []) if isinstance(data, dict) else []
        except Exception:
            return {"items": []}
        safe_items = []
        for it in items:
            k = str(it.get("api_key") or "")
            if k.startswith("$enc:"):
                k = _ai_key_decrypt(k)   # decrypt for the masked preview only
            safe_items.append({
                **{f: it.get(f) for f in ("id", "name", "provider", "base_url", "model")},
                "has_api_key": bool(k),
                "api_key_preview": (k[:4] + "…" + k[-3:]) if len(k) > 8 else ("…" if k else ""),
            })
        return {"items": safe_items}

    def _ai_key_token(config_id: str) -> str:
        """Stable CryptoStore slot per provider config — key rotation overwrites
        the same slot instead of leaking stale tokens."""
        return f"ai_key:{config_id}"

    def _ai_key_migrate() -> None:
        """FIX(security) one-time migration: move plaintext api_key rows in
        ai_providers.json into the encrypted CryptoStore; the file then holds
        only '$enc:<token>' references. Idempotent, safe on every boot."""
        path = Path(data_dir) / "ai_providers.json"
        if not path.exists():
            return
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            items = data.get("items", []) if isinstance(data, dict) else []
            changed = 0
            for it in items:
                k = str(it.get("api_key") or "")
                if k and not k.startswith("$enc:"):
                    try:
                        store.set(_ai_key_token(str(it.get("id"))), k)
                        it["api_key"] = f"$enc:{_ai_key_token(str(it.get('id')))}"
                        changed += 1
                    except Exception as e:
                        log.warning("ai key migrate failed for %s: %s", it.get("id"), e)
            if changed:
                path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
                log.info("migrated %d plaintext AI key(s) to encrypted storage", changed)
        except Exception as e:
            log.warning("ai key migration skipped: %s", e)

    def _ai_key_decrypt(stored: str) -> str:
        """Decrypt a '$enc:<token>' reference via CryptoStore; plain values
        (pre-migration leftovers) pass through as-is."""
        stored = str(stored or "")
        if not stored:
            return ""
        if stored.startswith("$enc:"):
            try:
                return store.get(stored[5:]) or ""
            except Exception:
                log.warning("ai key decrypt failed (wrong WALLEX_KEY_PASSWORD?)")
                return ""
        return stored

    def _resolve_ai_key(config_id: str) -> Optional[dict]:
        """Server-side secret resolution: the frontend only ever sends a config
        ID; the key itself is read from server-side storage (decrypted here)
        and never travels through the browser."""
        if not config_id:
            return None
        path = Path(data_dir) / "ai_providers.json"
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            for it in (data.get("items", []) if isinstance(data, dict) else []):
                if it.get("id") == config_id:
                    return {**it, "api_key": _ai_key_decrypt(it.get("api_key"))}
        except Exception:
            pass
        return None


    def _resolve_active_ai(payload: dict) -> Optional[dict]:
        """App-wide AI resolution: explicit config_id > the config the user
        activated in the AI settings (بارگذاری شد, persisted in kv) > None
        (caller falls back to env/default). Used by every AI feature so the
        last loaded model is the preferred AI everywhere."""
        cid = str((payload or {}).get("config_id", "")).strip()
        if not cid:
            try:
                cid = str(storage.kv_get("active_ai_config") or "").strip()
            except Exception:
                cid = ""
        if not cid:
            return None
        ref = _resolve_ai_key(cid)
        if ref is None:
            return None
        return {**ref, "config_id": cid}
    @app.post("/api/ai/config/save")
    def save_ai_config(payload: dict):
        name = str(payload.get("name", "")).strip()
        provider = str(payload.get("provider", ai_client.cfg.provider)).lower()
        base_url = str(payload.get("base_url", ai_client.cfg.base_url)).strip()
        api_key = str(payload.get("api_key", ai_client.cfg.api_key or "")).strip()
        model = str(payload.get("model", ai_client.cfg.model)).strip()
        if not name or not provider or not base_url or not model:
            raise HTTPException(400, "name/provider/base_url/model required")
        path = Path(data_dir) / "ai_providers.json"
        try:
            if path.exists():
                data = json.loads(path.read_text(encoding="utf-8"))
                items = data.get("items", []) if isinstance(data, dict) else []
            else:
                items = []
        except Exception:
            items = []
        token = _ai_key_token(name)
        stored_key = ""          # what the JSON file will hold ($enc: reference)
        if api_key and ("..." in api_key or api_key.startswith("$enc:")):
            # masked preview / re-saved reference → keep the EXISTING key
            for item in items:
                if item.get("id") == name and str(item.get("api_key") or "").startswith("$enc:"):
                    stored_key = str(item.get("api_key"))
                    break
            # no existing $enc: found → masked preview carries no real key
        elif api_key:
            # real key submitted → encrypt into the stable slot
            try:
                store.set(token, api_key)
            except Exception as e:
                raise HTTPException(500, f"could not encrypt API key: {e}")
            stored_key = f"$enc:{token}"
        else:
            stored_key = ""   # masked preview with nothing stored → no key
        for item in items:
            if item.get("id") == name:
                if stored_key:
                    item["api_key"] = stored_key
                item.update({"provider": provider, "base_url": base_url, "model": model})
                break
        else:
            items.append({"id": name, "provider": provider, "base_url": base_url,
                          "api_key": stored_key, "model": model})
        path.write_text(json.dumps({"items": items}, ensure_ascii=False, indent=2), encoding="utf-8")
        return {"ok": True, "id": name}

    @app.post("/api/ai/config/activate")
    def activate_ai_config(payload: dict):
        """FIX(user req): 'بارگذاری شد' must persist — the activated config
        becomes the app-wide default AI (used by grid ai-fill, ai-advise, etc.),
        not just form fields lost on reload."""
        cid = str((payload or {}).get("id", "")).strip()
        if not cid:
            raise HTTPException(400, "id required")
        path = Path(data_dir) / "ai_providers.json"
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            raise HTTPException(404, "no AI configs stored")
        items = data.get("items", []) if isinstance(data, dict) else []
        if not any(it.get("id") == cid for it in items):
            raise HTTPException(404, f"unknown AI config: {cid}")
        storage.kv_set("active_ai_config", cid)
        return {"ok": True, "active": cid}

    @app.get("/api/ai/config/active")
    def get_active_ai_config():
        return {"active": storage.kv_get("active_ai_config") or ""}

    @app.delete("/api/ai/configs/{config_id}")
    def delete_ai_config(config_id: str):
        path = Path(data_dir) / "ai_providers.json"
        if not path.exists():
            raise HTTPException(404, "no configs")
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            items = data.get("items", []) if isinstance(data, dict) else []
            items = [i for i in items if i.get("id") != config_id]
            path.write_text(json.dumps({"items": items}, ensure_ascii=False, indent=2), encoding="utf-8")
            # purge the encrypted key slot so a deleted config's key is gone
            try:
                store.delete(_ai_key_token(config_id))
            except Exception:
                pass
            return {"ok": True}
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(500, str(exc)) from exc

    @app.delete("/api/strategies/{strategy_id}")
    def delete_strategy(strategy_id: str):
        if strategy_id == "legacy":
            raise HTTPException(400, "cannot delete legacy strategy")
        if not strategy_store.delete(strategy_id):
            raise HTTPException(404, "strategy not found")
        storage.log_event(int(time.time()), "strategy_deleted", "", f"id={strategy_id}")
        # if we removed the active strategy, fall back to legacy
        try:
            cur = str(storage.kv_get("active_strategy_id") or "")
        except Exception:
            cur = ""
        if cur == strategy_id:
            storage.kv_set("active_strategy_id", "legacy")
            try:
                app.state.engine.active_external_strategy = None
            except Exception:
                pass
        return {"ok": True}

    # ── strategy compare ────────────────────────────────────────────
    @app.post("/api/strategies/compare")
    def compare_strategies(payload: dict):
        ids = payload.get("ids") or []
        if not ids:
            raise HTTPException(400, "ids required")
        symbols = payload.get("symbols") or cfg.get("symbols", [])
        mode = str(payload.get("mode", "spot")).lower()
        overrides = {k: payload[k] for k in ("max_positions", "risk_per_trade_pct", "risk_coef") if k in payload}
        # ── PREFLIGHT: ensure enough history in all 4 TFs before comparing ──
        bt_days = int(payload.get("days", 120))
        _user_job_lock_ttl()  # A+B hybrid: yield background backfill to this user job
        pre = ensure_depth(client, data_dir, symbols, days=bt_days)
        # FIX(#9): exclude symbols whose top-up failed instead of silently
        # comparing on shallow data.
        shallow = {s: r["detail"] for s, r in (pre.get("status") or {}).items() if not r.get("ok")}
        usable_symbols = [s for s in symbols if s not in shallow]
        if not usable_symbols:
            raise HTTPException(503, f"history insufficient for all symbols: {shallow}")
        histories = {}
        for sym in usable_symbols:
            h = load_symbol_history(data_dir, sym)
            if h.h15.candles and h.h60.candles:
                histories[sym] = h
        if not histories:
            raise HTTPException(400, "no history downloaded — call /api/backtest/download first")
        results = []
        for sid in ids:
            art = strategy_store.load(str(sid))
            if not art:
                continue
            try:
                # legacy artifact is a blank placeholder — pass None so
                # run_backtest runs the REAL legacy 8-criteria logic
                ext = None if str(sid) == "legacy" else art
                res = run_backtest(histories, cfg, mode=mode, overrides=overrides, external_strategy=ext, fast_1h_only=True)
                results.append({
                    "strategy_id": sid,
                    "name": art.get("name"),
                    "metrics": res.metrics,
                    "trades": res.trades[-100:],
                })
            except Exception as exc:
                results.append({"strategy_id": sid, "name": art.get("name"), "error": str(exc)[:200]})
        return {"items": results}

    # ── static dashboard ───────────────────────────────────────────
    web_dir = ROOT / "web"
    if web_dir.exists():
        app.mount("/static", StaticFiles(directory=str(web_dir)), name="static")

        @app.get("/")
        def index():
            return FileResponse(str(web_dir / "index.html"), headers={
                "Cache-Control": "no-cache, no-store, must-revalidate",
                "Pragma": "no-cache",
                "Expires": "0",
            })

        @app.get("/api/page_version")
        def page_version():
            """Tiny cache-buster the dashboard checks against its own build tag:
            if the browser holds a STALE cached page (old line numbers), it
            reloads itself automatically. Returns the file's mtime-based tag."""
            f = web_dir / "index.html"
            tag = str(int(f.stat().st_mtime))
            return {"version": tag}

    # FIX(security): one-time migration of any legacy plaintext AI provider
    # keys into the encrypted CryptoStore. Runs at app creation (before any
    # request is served); idempotent on subsequent boots.
    try:
        _ai_key_migrate()
    except Exception as exc:
        log.warning("ai key migration at boot failed: %s", exc)

    # ── Phase 7: construct the Deep AI Doctor (AFTER _ai_key_decrypt's
    # definition — see the deferred-construction note above).
    doctor = AppDoctor(
        engine=engine, client=client, storage=storage, cmc=cmc, wizard=wizard,
        data_dir=Path(data_dir), profile_id=app.state.profile_id,
        base_url=f"http://127.0.0.1:{cfg.get('server', {}).get('port', 8787)}",
    )
    doctor._key_decryptor = _ai_key_decrypt
    doctor.store_get = store.get

    # ── HYBRID RESTORE (user req): resume interrupted workflows after a
    # restart — engine + every running grid — without user action.
    try:
        _resumed = []
        if (storage.kv_get("engine_running") or "0") == "1" and not engine.running:
            engine.start()
            _resumed.append("engine")
        _grids = grid_manager.resume_all()
        if _grids:
            _resumed.append(f"{_grids} grid(s)")
        if _resumed:
            log.info("HYBRID RESTORE: resumed %s", ", ".join(_resumed))
            storage.log_event(int(time.time()), "hybrid_restore", "",
                              f"resumed: {', '.join(_resumed)}")
    except Exception as exc:
        log.warning("hybrid restore failed: %s", exc)

    return app


app = create_app()


def main():
    import uvicorn
    cfg = load_config()
    host = cfg.get("server", {}).get("host", "127.0.0.1")
    port = int(cfg.get("server", {}).get("port", 8787))
    uvicorn.run(app, host=host, port=port)


if __name__ == "__main__":
    main()
