"""Exchange profile — the per-exchange "programmable blank template".

A profile.json fully describes ONE exchange for GenericRESTAdapter:
endpoints, auth scheme, symbol format, granularity quirks, rate gap,
rules, margin capability. The AI setup wizard (Phase 3) writes these;
anything the format cannot express goes to `limitations[]` — never silence.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

SCHEMA_VERSION = 1
AUTH_SCHEMES = ("none", "header", "hmac", "ed25519")
RESULT_FORMATS = ("udf", "arrays", "objects")
MARGIN_STYLES = ("", "isolated_margin", "futures", "cross_margin", "dex_perp")
STATUSES = ("draft", "probed", "ready", "error")

# Endpoints GenericRESTAdapter knows; candles+markets are REQUIRED for a
# usable profile — the rest are optional and raise ExchangeNotSupported.
ENDPOINT_KEYS = (
    "markets", "candles", "ticker", "depth", "balances", "fees",
    "place_order", "open_orders", "order", "cancel_order",
)
MARGIN_ENDPOINT_KEYS = (
    "margin_markets", "margin_positions", "margin_position",
    "margin_open", "margin_close", "margin_sltp",
    "margin_collateral", "margin_profit", "margin_dry_run",
)


def default_profile(pid: str, name: str = "") -> dict:
    return {
        "schema_version": SCHEMA_VERSION,
        "id": pid,
        "name": name or pid,
        "base_url": "",
        "auth": {"scheme": "none"},
        "symbol_format": {"separator": "", "case": "upper", "quote_suffixes": ["USDT"]},
        "endpoints": {},
        "margin": None,
        "tf_param_map": {"15": "15", "60": "60", "240": "240", "1D": "1D"},
        "true_res_map": {"15": "15", "60": "60", "240": "240", "1D": "1D"},
        "min_gap_sec": 1.0,
        "quotes": ["USDT"],
        "rules": {
            "min_order_usdt": 10.0, "min_collateral_usdt": 10.0,
            "max_collateral_usdt": 100000.0, "max_risk_coef": 3.0,
            "price_band_pct": 5.0, "qty_step": 8,
            "fee_pct": 0.2, "interest_per_4h_pct": 0.05,
        },
        "docs": {"source_urls": [], "researched_at": 0, "model": ""},
        "capabilities_override": None,
        "status": "draft",
        "limitations": [],
    }


def validate_profile(p: dict) -> Tuple[List[str], List[str]]:
    """Returns (errors, warnings). errors → profile must not go 'ready'."""
    errors: List[str] = []
    warnings: List[str] = []

    def _err(msg: str):
        errors.append(msg)

    def _warn(msg: str):
        warnings.append(msg)

    if not p.get("id"):
        _err("profile.id is required")
    if not str(p.get("base_url", "")).startswith(("http://", "https://")):
        _err("profile.base_url must be an http(s) URL")
    auth = p.get("auth") or {}
    if auth.get("scheme") not in AUTH_SCHEMES:
        _err(f"auth.scheme must be one of {AUTH_SCHEMES}")
    elif auth.get("scheme") == "header" and not auth.get("header_name"):
        _err("auth.header_name required for 'header' scheme")
    elif auth.get("scheme") == "hmac" and not (auth.get("hmac") or {}).get("headers"):
        _err("auth.hmac.headers required for 'hmac' scheme")
    if auth.get("scheme") == "ed25519":
        ed = auth.get("ed25519") or {}
        for need in ("key_header", "signature_header", "timestamp_header"):
            if not ed.get(need):
                _err(f"auth.ed25519.{need} required for 'ed25519' scheme")

    eps = p.get("endpoints") or {}
    for key in ("candles", "markets"):
        ep = eps.get(key)
        if not ep or not ep.get("path"):
            _err(f"endpoints.{key}.path is required")
    for key, ep in eps.items():
        if key not in ENDPOINT_KEYS:
            _warn(f"endpoints.{key} is not a known endpoint key (ignored by adapter)")
            continue
        if not isinstance(ep, dict) or not ep.get("path"):
            _err(f"endpoints.{key}.path missing")
        elif ep.get("result_format") and ep["result_format"] not in RESULT_FORMATS:
            _err(f"endpoints.{key}.result_format must be one of {RESULT_FORMATS}")

    m = p.get("margin")
    if m is not None:
        if not isinstance(m, dict) or not (m.get("endpoints") or {}):
            _err("margin must be null or {endpoints:{...}}")
        else:
            style = m.get("style", "")
            if style not in MARGIN_STYLES[1:]:
                _err(f"margin.style must be one of {MARGIN_STYLES[1:]}")

    trm = p.get("true_res_map") or {}
    for tf in ("15", "60", "240", "1D"):
        if tf not in trm:
            _warn(f"true_res_map missing '{tf}' (defaults to identity)")
    if not (p.get("quotes") or []):
        _err("quotes must list at least one quote currency")
    if p.get("status") not in STATUSES:
        _err(f"status must be one of {STATUSES}")
    return errors, warnings


def profile_path(pid: str, profiles_root: Path) -> Path:
    return Path(profiles_root) / pid / "profile.json"


def load_profile(pid: str, profiles_root: Path) -> Optional[dict]:
    p = profile_path(pid, profiles_root)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def save_profile(p: dict, profiles_root: Path) -> Path:
    """Atomic write with validator errors surfaced (still saves drafts)."""
    errors, _ = validate_profile(p)
    p.setdefault("schema_version", SCHEMA_VERSION)
    dst = profile_path(p["id"], profiles_root)
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_suffix(".tmp")
    tmp.write_text(json.dumps(p, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(dst)
    return dst


def add_limitation(p: dict, category: str, detail: str) -> None:
    """Record something the template format cannot express (user requirement:
    report it, never silently degrade)."""
    lims = p.setdefault("limitations", [])
    entry = {"category": category, "detail": detail, "ts": int(time.time())}
    if entry not in lims:
        lims.append(entry)


def archive_profile(pid: str, profiles_root: Path, tag: str = "prev") -> Optional[Path]:
    """Archive an existing profile.json as profile.<tag>-<ts>.json (keep 3).
    Used by smart re-research: the AI's new output REPLACES the old profile,
    but the previous version stays on disk for reference/rollback."""
    src = profile_path(pid, profiles_root)
    if not src.exists():
        return None
    d = src.parent
    # unique name even when multiple archives land in the same second
    ts = int(time.time())
    dst = d / f"profile.{tag}-{ts}.json"
    n = 1
    while dst.exists():
        dst = d / f"profile.{tag}-{ts}-{n}.json"
        n += 1
    try:
        dst.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
        # keep only the 3 newest archives
        archives = sorted(d.glob(f"profile.{tag}-*.json"), key=lambda p: p.stat().st_mtime)
        for old in archives[:-3]:
            old.unlink(missing_ok=True)
        return dst
    except Exception:
        return None


def heal_profile(pid: str, profiles_root: Path) -> dict:
    """Self-heal a profile directory (user req: fill missing/corrupted data).

    Checks, in order:
      - profile.json missing but registry/other data exist → needs_research
        (history/, bot.db, secrets are PRESERVED — only the AI template is gone)
      - profile.json corrupt/unparseable → archived as profile.corrupt-<ts>
        and needs_research (data preserved)
      - profile.json parses but fails validation → archived as profile.invalid-<ts>
        and needs_research
      - healthy → ok (nothing touched)
    The function NEVER deletes history/, bot.db, strategies/ or secrets —
    those are re-usable regardless of the AI template's state.
    """
    d = Path(profiles_root) / pid
    out: dict = {"profile_id": pid, "dir_exists": d.exists(),
                 "data_preserved": [], "healed": [], "needs_research": False,
                 "registry_registered": False}
    if not d.exists():
        out["needs_research"] = False   # nothing ever existed — clean slate
        return out
    for item in ("history", "bot.db", "strategies", "secrets.enc", "markets_cache.json"):
        if (d / item).exists():
            out["data_preserved"].append(item)
    pf = d / "profile.json"
    if not pf.exists():
        out["needs_research"] = True
        out["healed"].append("profile.json missing — flagged for re-research (data preserved)")
    else:
        try:
            prof = json.loads(pf.read_text(encoding="utf-8"))
            errs, _warns = validate_profile(prof)
            if errs:
                arch = archive_profile(pid, profiles_root, tag="invalid")
                out["needs_research"] = True
                out["healed"].append(f"invalid profile ({'; '.join(errs[:3])}) archived → {arch.name if arch else 'archive failed'}")
            else:
                out["healed"].append("profile valid")
        except Exception as exc:
            arch = archive_profile(pid, profiles_root, tag="corrupt")
            out["needs_research"] = True
            out["healed"].append(f"corrupt profile ({str(exc)[:80]}) archived → {arch.name if arch else 'archive failed'}")
    return out
