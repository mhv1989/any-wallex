"""GenericRESTAdapter — executes any exchange profile.json.

Reads candles/markets/ticker/orders through profile-driven endpoints with
pluggable response parsers (UDF, columnar arrays, object lists), the common
auth families (none / static header / HMAC-signed), and symbol-format
transforms (BTC_USDT ↔ BTCUSDT canonical form).

Anything the profile cannot express raises ExchangeNotSupported — callers
treat it as an explicit limitation, never a silent wrong answer.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import threading
import time
from typing import Any, Dict, List, Optional
from urllib.parse import urlencode

import httpx

from .base import Capabilities, ExchangeAdapter

log = logging.getLogger("wallex.generic")


class ExchangeNotSupported(RuntimeError):
    """The profile does not implement this capability — report, don't guess."""


class ExchangeNetworkError(RuntimeError):
    """The exchange is unreachable from THIS network — DNS blackhole, connection
    refused/timeout, or a geo/bot wall (403 HTML from Cloudflare/CloudFront).
    Carries a `reason` for the UI: 'dns' | 'refused' | 'timeout' | 'geo' | 'ssl'.
    The dashboard must show these as "network/region blocked" (use VPN), never
    as a generic app error."""

    def __init__(self, message: str, reason: str = "network"):
        super().__init__(message)
        self.reason = reason


def _net_fail_reason(err_text: str) -> str:
    """Classify a transport failure for the UI. Iranian-censor DNS blackholes
    resolve exchange hosts to 10.10.34.34 — treat private/loopback answers for a
    public host as a region block, same as getaddrinfo failures."""
    t = (err_text or "").lower()
    if "getaddrinfo" in t or "name or service not known" in t:
        return "dns"
    if "unconditionally" in t or "actively refused" in t or "connectionreset" in t \
            or "10061" in t or "10054" in t:
        return "refused"
    if "timed out" in t or "timeout" in t:
        return "timeout"
    if "ssl" in t or "certificate" in t:
        return "ssl"
    return "network"


class ApiLogEntry:
    __slots__ = ("ts", "method", "path", "status", "latency_ms", "retries", "error")

    def __init__(self, ts, method, path, status, latency_ms, retries, error=""):
        self.ts = ts
        self.method = method
        self.path = path
        self.status = status
        self.latency_ms = latency_ms
        self.retries = retries
        self.error = error


def _dig(obj: Any, dotted: str) -> Any:
    """Navigate a nested dict/list by dot path. 'result.markets' etc."""
    cur = obj
    for part in (dotted or "").split("."):
        if not part:
            continue
        if isinstance(cur, list):
            try:
                cur = cur[int(part)]
                continue
            except (ValueError, IndexError):
                return None
        if isinstance(cur, dict):
            cur = cur.get(part)
        else:
            return None
    return cur


def _row_get(row: dict, key: str):
    """field_map value with dots digs nested objects: 'currency1.code'.
    '+' concatenates multiple dotted paths: 'base.x.en+quote.y.en' (Ramzinex
    builds its symbol from two nested currency names)."""
    if "+" in key:
        parts = [_row_get(row, k) for k in key.split("+")]
        if any(v is None for v in parts):
            return None
        return "".join(str(v) for v in parts)
    if "." not in key:
        return row.get(key)
    cur = row
    for part in key.split("."):
        if not isinstance(cur, dict):
            return None
        cur = cur.get(part)
    return cur


def _norm_ts(v) -> int:
    """Seconds-or-milliseconds → seconds (Binance returns ms, UDF seconds).
    ISO-8601 strings (BitMEX 'timestamp', dYdX fromISO) are parsed too."""
    try:
        t = int(float(v))
        return t // 1000 if t > 10**12 else t
    except (TypeError, ValueError):
        s = str(v).strip()
        if s:
            try:
                from datetime import datetime, timezone
                dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                return int(dt.timestamp())
            except Exception:
                pass
    return 0


def _parse_candles_udf(data: dict, field_map: Optional[dict] = None) -> List[dict]:
    """TradingView UDF: {s:'ok', t:[], o:[], h:[], l:[], c:[], v:[]}."""
    fm = {"ts": "t", "open": "o", "high": "h", "low": "l", "close": "c", "volume": "v"}
    fm.update(field_map or {})
    if str(data.get("s", "ok")) != "ok":
        return []
    t = data.get(fm["ts"]) or []
    out = []
    for i in range(len(t)):
        out.append({
            "ts": _norm_ts(t[i]),
            "open": float(data[fm["open"]][i]),
            "high": float(data[fm["high"]][i]),
            "low": float(data[fm["low"]][i]),
            "close": float(data[fm["close"]][i]),
            "volume": float(data.get(fm["volume"], [0] * len(t))[i] or 0),
        })
    return out


def _parse_candles_arrays(data: Any, field_map: Optional[dict] = None) -> List[dict]:
    """Columnar non-UDF: {'time':[], 'open':[], ...} or bare [[ts,o,h,l,c,v],...]."""
    fm = {"ts": "time", "open": "open", "high": "high", "low": "low", "close": "close", "volume": "volume"}
    fm.update(field_map or {})
    out: List[dict] = []
    if isinstance(data, list):
        # bare rows: [ts, o, h, l, c, v] (Binance klines style)
        for row in data:
            if isinstance(row, (list, tuple)) and len(row) >= 6:
                out.append({"ts": _norm_ts(row[0]), "open": float(row[1]), "high": float(row[2]),
                            "low": float(row[3]), "close": float(row[4]), "volume": float(row[5] or 0)})
        return out
    t = data.get(fm["ts"]) or []
    o = data.get(fm["open"]) or []
    h = data.get(fm["high"]) or []
    l = data.get(fm["low"]) or []
    c = data.get(fm["close"]) or []
    v = data.get(fm["volume"]) or [0] * len(t)
    for i in range(len(t)):
        out.append({"ts": _norm_ts(t[i]), "open": float(o[i]), "high": float(h[i]),
                    "low": float(l[i]), "close": float(c[i]), "volume": float(v[i] or 0)})
    return out


def _parse_candles_objects(data: Any, field_map: Optional[dict] = None) -> List[dict]:
    """List of objects: [{'time': ..., 'open': ...}, ...] (Nobitex style)."""
    fm = {"ts": "time", "open": "open", "high": "high", "low": "low", "close": "close", "volume": "volume"}
    fm.update(field_map or {})
    rows = data if isinstance(data, list) else (data.get("candles") or data.get("result") or [])
    out: List[dict] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        out.append({
            "ts": _norm_ts(row.get(fm["ts"]) or 0),
            "open": float(row.get(fm["open"]) or 0),
            "high": float(row.get(fm["high"]) or 0),
            "low": float(row.get(fm["low"]) or 0),
            "close": float(row.get(fm["close"]) or 0),
            "volume": float(row.get(fm["volume"]) or 0),
        })
    return out


_PARSERS = {"udf": _parse_candles_udf, "arrays": _parse_candles_arrays, "objects": _parse_candles_objects}


class GenericRESTAdapter(ExchangeAdapter):
    id = "generic"
    display_name = "Generic Exchange"

    def __init__(self, profile: dict, api_key: str = "",
                 api_secret: str = "", passphrase: str = "",
                 redact_fn=None):
        self.profile = profile
        self.id = profile.get("id", "generic")
        self.display_name = profile.get("name", self.id)
        self.api_key = api_key
        self.api_secret = api_secret or ""
        self.passphrase = passphrase or ""
        _auth = profile.get("auth") or {}
        self._auth_scheme = _auth.get("scheme", "none")
        self._auth = _auth
        self._redact = redact_fn or (lambda s: s)
        self._lock = threading.Lock()
        self._last_request_ts = 0.0
        self._ed_priv = None      # lazy-loaded Ed25519PrivateKey (seed = api_secret)
        self.api_log: List[ApiLogEntry] = []
        self._http = httpx.Client(timeout=30.0,
                                  headers={"User-Agent": "AnyWallexBot/0.1"})
        self._markets_cache: Optional[List[dict]] = None
        self._markets_cache_ts = 0.0

    def close(self) -> None:
        """FIX(audit-M8): release the httpx pool (wizard probe/calibrate
        created one adapter per job and never closed it)."""
        try:
            self._http.close()
        except Exception:
            pass

    # ── metadata ─────────────────────────────────────────────────────
    @property
    def capabilities(self) -> Capabilities:
        ov = self.profile.get("capabilities_override") or {}
        m = self.profile.get("margin")
        return Capabilities(
            spot=bool(ov.get("spot", True)),
            margin=bool(ov.get("margin", bool(m))),
            margin_style=str(ov.get("margin_style", (m or {}).get("style", ""))),
            futures=bool(ov.get("futures", False)),
            udf_native=bool(ov.get("udf_native",
                                   (self.profile.get("endpoints", {}).get("candles", {})
                                    .get("result_format")) == "udf")),
            notes=str(ov.get("notes", "")),
        )

    @property
    def live_margin_supported(self) -> bool:
        """Profile adapters can execute live margin ONLY when the margin block
        actually defines an open-position endpoint (Nobitex: margin is a
        separate order-based product — paper simulation is allowed, live
        execution is not wired)."""
        m = self.profile.get("margin") or {}
        eps = (m.get("endpoints") or {}) if isinstance(m, dict) else {}
        return bool(eps.get("margin_open", {}).get("path"))

    @property
    def quote_currencies(self) -> List[str]:
        qs = [str(q).upper() for q in (self.profile.get("quotes") or ["USDT"])]
        # symbol_format.quote_suffixes participate in symbol parsing too
        # (an exchange may trade ETHBTC without listing BTC in `quotes`).
        for q in ((self.profile.get("symbol_format") or {}).get("quote_suffixes") or []):
            u = str(q).upper()
            if u and u not in qs:
                qs.append(u)
        # Iranian-Toman alias family: an exchange may label pairs IRT/TMN while
        # keying rows RLS (Nobitex). Include all three so quote-tagging, filters
        # and ticker alias lookups always see the whole family.
        for alias in ("RLS", "IRT", "TMN"):
            if any(str(m.get("symbol", "")).upper().endswith(alias)
                   for m in (self._markets_cache or [])):
                if alias not in qs:
                    qs.append(alias)
        return qs or ["USDT"]

    @property
    def tf_param_map(self) -> Dict[str, str]:
        return {tf: str(v) for tf, v in (self.profile.get("tf_param_map") or
                                         {t: t for t in ("15", "60", "240", "1D")}).items()}

    @property
    def true_res_map(self) -> Dict[str, str]:
        return {tf: str(v) for tf, v in (self.profile.get("true_res_map") or
                                         {t: t for t in ("15", "60", "240", "1D")}).items()}

    # ── symbol format ────────────────────────────────────────────────
    def to_exchange_symbol(self, canonical: str) -> str:
        """BTCUSDT -> exchange-native form (separator/case per profile)."""
        fmt = self.profile.get("symbol_format") or {}
        # FIX(edgex): profiles whose candle/market endpoints key on an
        # exchange-internal id (EdgeX numeric contractId) declare
        # symbol_map: {BTCUSDT: "30000001", ...} — consulted first.
        smap = self.profile.get("symbol_map") or {}
        if smap:
            up = (canonical or "").upper()
            hit = smap.get(up)
            if hit:
                return str(hit)
            # quote-family fallback: exchanges quoting USDC (EdgeX) have no
            # USDT product — map BTCUSDT → BTCUSDC by suffix swap when the
            # exact key misses and a USDC twin exists in the map.
            if up.endswith("USDT"):
                hit = smap.get(up[:-4] + "USDC")
                if hit:
                    return str(hit)
        # canonical form is pure alphanumeric (BTCUSDT) — strip any separators
        # the caller included ('BTC-USDT', 'BTC_USDT') so suffix-splitting can't
        # produce doubled separators ('BTC--USD')
        import re as _re
        s = _re.sub(r"[^A-Z0-9]", "", (canonical or "").upper().strip())
        # split by known quote suffixes (longest first)
        for q in sorted([str(x).upper() for x in (fmt.get("quote_suffixes") or self.quote_currencies)],
                        key=len, reverse=True):
            if s.endswith(q) and len(s) > len(q):
                base = s[: -len(q)]
                sep = str(fmt.get("separator", ""))
                out = base + sep + q
                if str(fmt.get("case", "upper")).lower() == "lower":
                    out = out.lower()
                return out
        return s if str(fmt.get("case", "upper")).lower() != "lower" else s.lower()

    def normalize_symbol(self, exchange_symbol: str) -> str:
        fmt = self.profile.get("symbol_format") or {}
        s = str(exchange_symbol or "").strip()
        if str(fmt.get("case", "upper")).lower() == "lower":
            s = s.upper()
        # canonical internal form is pure alphanumeric concatenation
        # (BTC_USDT / BTC-USDT / btcusdt all -> BTCUSDT)
        return "".join(ch for ch in s.upper() if ch.isalnum())

    # ── auth ─────────────────────────────────────────────────────────
    def _sign(self, method: str, url: str, params: dict, body: Optional[dict],
              headers: Dict[str, str]) -> None:
        """Apply the profile's auth scheme to the outgoing request."""
        a = self._auth
        scheme = self._auth_scheme
        if scheme == "none":
            return
        if not self.api_key:
            raise ExchangeNotSupported("credential required: profile auth is '" + scheme + "' but no API key is set")
        if scheme == "header":
            headers[a.get("header_name", "x-api-key")] = self.api_key
            if a.get("passphrase_header"):
                headers[a["passphrase_header"]] = self.passphrase or ""
            return
        if scheme == "hmac":
            hc = a.get("hmac") or {}
            if not self.api_secret:
                raise ExchangeNotSupported("hmac auth requires an API secret (none set)")
            algo = hc.get("algo", "sha256").replace("-", "")
            expires = str(int(time.time() * 1000) + int(hc.get("ttl_ms", 5000)))
            pre = hc.get("param_order") or ["method", "path", "query", "body", "expires"]
            query = urlencode(sorted((params or {}).items()))
            body_s = json.dumps(body, separators=(",", ":")) if body is not None else ""
            base = self.profile.get("base_url", "").rstrip("/")
            req_path = "/" + url[len(base):].lstrip("/").split("?")[0] if url.startswith(base) else url
            parts = []
            for p in pre:
                if p == "method":
                    parts.append(method.upper())
                elif p == "path":
                    parts.append(req_path)
                elif p == "query":
                    parts.append(query)
                elif p == "body":
                    parts.append(body_s)
                elif p == "expires":
                    parts.append(expires)
                elif p == "api_key":
                    parts.append(self.api_key)
            prehash = hc.get("separator", "").join(parts)
            digest = hmac.new(self.api_secret.encode(), prehash.encode(),
                              getattr(hashlib, algo)).hexdigest()
            for hname, template in (hc.get("headers") or {}).items():
                headers[hname] = (template
                                  .replace("{key}", self.api_key)
                                  .replace("{sig}", digest)
                                  .replace("{expires}", expires)
                                  .replace("{passphrase}", self.passphrase))
            return
        if scheme == "ed25519":
            # Ed25519 request signing (Nobitex v2): the private KEY is the
            # api_secret (32-byte url-safe-base64 seed); api_key is the public
            # key sent in the key header. Signature payload =
            #   timestamp + METHOD + full_path(+query) + raw_body
            # The actual bytes are sent by _request (which must sign the EXACT
            # compact-JSON body it transmits), so only the public key + scheme
            # validation happen here.
            ed = a.get("ed25519") or {}
            if not self.api_secret:
                raise ExchangeNotSupported(
                    "ed25519 auth requires the private key (API secret) — set it in تنظیمات")
            if not all(ed.get(k) for k in ("key_header", "signature_header", "timestamp_header")):
                raise ExchangeNotSupported("auth.ed25519 headers incomplete (key/signature/timestamp)")
            headers[ed["key_header"]] = self.api_key
            return
        raise ExchangeNotSupported(f"auth scheme '{scheme}' not implementable in template format")

    def _ed25519_sign(self, method: str, full_path: str, body_bytes: bytes) -> str:
        """Sign timestamp+METHOD+full_path+raw_body → url-safe base64 signature."""
        try:
            from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        except ImportError as exc:  # pragma: no cover
            raise ExchangeNotSupported("ed25519 auth requires the 'cryptography' package") from exc
        if self._ed_priv is None:
            raw = self.api_secret
            pad = "=" * (-len(raw) % 4)
            try:
                self._ed_priv = Ed25519PrivateKey.from_private_bytes(
                    base64.urlsafe_b64decode(raw + pad))
            except Exception as exc:
                raise ExchangeNotSupported(
                    f"API secret is not a valid 32-byte Ed25519 key: {exc}") from exc
        ts = str(int(time.time()))
        payload = f"{ts}{method.upper()}{full_path}{body_bytes.decode('utf-8', 'replace')}".encode()
        return base64.urlsafe_b64encode(self._ed_priv.sign(payload)).decode()

    # ── core request ─────────────────────────────────────────────────
    def _request(self, ep: dict, path_params: Optional[dict] = None,
                 extra_params: Optional[dict] = None,
                 json_body: Optional[dict] = None, auth: bool = False,
                 return_on_http_error: bool = False,
                 tmpl_context: Optional[dict] = None) -> Any:
        method = str(ep.get("method", "GET")).upper()
        path = str(ep.get("path", ""))
        for k, v in (path_params or {}).items():
            path = path.replace("{" + k + "}", str(v))
        url = self.profile.get("base_url", "").rstrip("/") + path
        params = dict(ep.get("params") or {})
        params.update(extra_params or {})
        # form-encoded bodies (user req: Nobitex market/stats is POST-form).
        # body_form: {"srcCurrency": "literal text", "dstCurrency": "{quote}"} —
        # values may embed {symbol}/{base}/{quote} tokens resolved from
        # path_params/extra_params context.
        body: Optional[dict] = json_body
        send_form = False
        if body is None and method in ("POST", "PUT", "PATCH"):
            bf = ep.get("body_form")
            if isinstance(bf, dict):
                ctx: Dict[str, str] = {}
                for v in (path_params or {}).values():
                    ctx[str(v)] = str(v)
                for v in (extra_params or {}).values():
                    ctx[str(v)] = str(v)
                body = {}
                for k, v in bf.items():
                    sv = str(v)
                    for tok, val in ctx.items():
                        sv = sv.replace("{" + tok + "}", val)
                    body[k] = sv
                send_form = True
            elif ep.get("body_json") is not None:
                # static JSON body (Hyperliquid-style POST APIs). Values may
                # embed {symbol}/{tf}/{from_ms}... tokens. Context comes from
                # tmpl_context (the canonical template values: '{symbol}' ->
                # resolved symbol) plus extra_params.
                ctx: Dict[str, Any] = {}
                for k, v in (tmpl_context or {}).items():
                    ctx[k] = v
                for k, v in (extra_params or {}).items():
                    ctx["{" + k + "}"] = v
                    ctx[str(v)] = str(v)
                bj = json.loads(json.dumps(ep["body_json"]))  # deep copy
                def _sub(v):
                    if isinstance(v, str):
                        # whole-string token → substitute PRESERVING type (numeric
                        # ctx values stay numbers: Hyperliquid rejects "startTime":
                        # "1788..." as a string with 422)
                        whole = next((val for tok, val in ctx.items()
                                      if v == tok), None)
                        if whole is not None:
                            s = str(whole)
                            if s.lstrip("-").isdigit():
                                return int(s)
                            try:
                                return float(s)
                            except ValueError:
                                return whole
                        for tok, val in ctx.items():
                            v = v.replace("{" + tok + "}", str(val))
                        return v
                    if isinstance(v, dict):
                        return {k: _sub(x) for k, x in v.items()}
                    if isinstance(v, list):
                        return [_sub(x) for x in v]
                    return v
                body = _sub(bj)
            elif ep.get("body_empty"):
                body = {}
        headers: Dict[str, str] = {"Content-Type": "application/json"}
        if auth:
            self._sign(method, url, params, body, headers)
        if send_form:
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        # ed25519 scheme: the signature must cover the EXACT bytes sent, so the
        # body is serialized ONCE here and transmitted via content= (httpx's
        # json= uses the identical compact-JSON encoding: separators (",",":"),
        # ensure_ascii=False, insertion order — verified against Nobitex).
        ed25519_auth = auth and self._auth_scheme == "ed25519"
        ed_body_bytes: Optional[bytes] = None
        if ed25519_auth:
            if body is not None and not send_form:
                ed_body_bytes = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            elif body is not None and send_form:
                ed_body_bytes = urlencode(body).encode("utf-8")

        gap = float(self.profile.get("min_gap_sec", 1.0))
        last_err = ""
        max_retries = 2
        for attempt in range(max_retries + 1):
            # FIX(freeze): reserve the slot under the lock, but sleep OUTSIDE
            # it — sleeping while holding the lock serialized every other
            # client call (UI endpoints included) behind each 12s gap.
            while True:
                with self._lock:
                    wait = gap - (time.time() - self._last_request_ts)
                    if wait <= 0:
                        self._last_request_ts = time.time()
                        break
                time.sleep(min(wait, 1.0))
            t0 = time.time()
            status: Optional[int] = None
            try:
                if ed25519_auth:
                    # full path includes the query string exactly as sent
                    # (httpx url-encodes params in insertion order — verified).
                    full_path = path + ("?" + urlencode(params) if params else "")
                    sig = self._ed25519_sign(method, full_path, ed_body_bytes or b"")
                    ed = self._auth.get("ed25519") or {}
                    headers[ed["signature_header"]] = sig
                    headers[ed["timestamp_header"]] = str(int(time.time()))
                    if ed_body_bytes is not None:
                        resp = self._http.request(method, url, params=params,
                                                  content=ed_body_bytes, headers=headers)
                    else:
                        resp = self._http.request(method, url, params=params, headers=headers)
                # form bodies go url-encoded; JSON bodies via json=
                elif send_form and body is not None:
                    resp = self._http.request(method, url, params=params,
                                              data=body, headers=headers)
                else:
                    resp = self._http.request(method, url, params=params,
                                              json=body if method in ("POST", "PATCH", "PUT", "DELETE") else None,
                                              headers=headers)
                status = resp.status_code
                latency = (time.time() - t0) * 1000
                if status == 429 or status >= 500:
                    last_err = f"HTTP {status}"
                    self._log(method, path, status, latency, attempt, last_err)
                    if attempt < max_retries:
                        time.sleep(2.0 * (2 ** attempt))
                        continue
                    raise RuntimeError(f"{self.display_name} API failed after retries: {last_err}")
                body = resp.text or ""
                try:
                    data = resp.json()
                except (json.JSONDecodeError, ValueError) as je:
                    # A 4xx/5xx with a non-JSON body (HTML error page, gateway
                    # 404, Cloudflare block page). Retrying a deterministic 404
                    # is pure waste, and the old code masked it as "network
                    # failure: Expecting value: line 1 column 1 (char 0)" —
                    # which hid the REAL cause (a wrong/dead endpoint path)
                    # behind a bogus "network" label. Say what actually
                    # happened: the status + the path that was hit.
                    detail = (f"{self.display_name} API returned HTTP {status} "
                              f"with a non-JSON body for {method} {path} "
                              f"(body[:120]={body[:120]!r}). Endpoint path or "
                              f"base_url is likely wrong — check the profile "
                              f"endpoints against the exchange's live API.")
                    self._log(method, path, status, latency, attempt,
                              f"HTTP {status} non-JSON body")
                    raise RuntimeError(self._redact(detail)) from je
                self._log(method, path, status, latency, attempt)
                if status >= 400:
                    if return_on_http_error:
                        # alias-retry paths need to SEE the error body (e.g.
                        # Nobitex InvalidSymbol) to try another spelling
                        return data if isinstance(data, dict) else {"_http_status": status}
                    # FIX(net-classify): 403 with an HTML body = geo/bot wall
                    # (Cloudflare/CloudFront block page) — tell the user the
                    # truth (region blocked) instead of "endpoint wrong".
                    _body_head = body[:300].lstrip().lower()
                    if status == 403 and (_body_head.startswith("<!doctype")
                            or _body_head.startswith("<html")
                            or "cloudflare" in _body_head
                            or "cloudfront" in _body_head
                            or "blocked" in _body_head
                            or "forbidden" in _body_head):
                        raise ExchangeNetworkError(
                            self._redact(f"{self.display_name} blocked this region/network "
                                         f"(HTTP 403): {body[:200]}"), reason="geo")
                    raise RuntimeError(self._redact(f"{self.display_name} API error {status}: {body[:300]}"))
                rp = ep.get("result_path")
                return _dig(data, rp) if rp else data
            except json.JSONDecodeError as e:
                latency = (time.time() - t0) * 1000
                last_err = self._redact(str(e))
                self._log(method, path, status, latency, attempt, last_err)
                if attempt < max_retries:
                    time.sleep(2.0 * (2 ** attempt))
                    continue
                raise RuntimeError(f"{self.display_name} network failure: {last_err}") from e
            except httpx.TransportError as e:
                # FIX(net-classify): distinguish REGION/NETWORK blocks from
                # generic failures so the UI can say "use VPN / blocked in
                # your region" instead of a bare error.
                latency = (time.time() - t0) * 1000
                last_err = self._redact(str(e))
                self._log(method, path, status, latency, attempt, last_err)
                if attempt < max_retries:
                    time.sleep(2.0 * (2 ** attempt))
                    continue
                raise ExchangeNetworkError(
                    f"{self.display_name} network failure: {last_err}",
                    reason=_net_fail_reason(last_err)) from e
        raise RuntimeError(f"unreachable: {last_err}")

    def _log(self, method, path, status, latency_ms, retries, error=""):
        self.api_log.append(ApiLogEntry(time.time(), method, path, status,
                                        round(latency_ms, 1), retries, error))
        if len(self.api_log) > 2000:
            self.api_log = self.api_log[-1000:]
        # Persist ONLY failures to the per-profile `api` log. Successful
        # candle/stats polls are far too chatty for a 5×10MB disk cap;
        # every error (HTTP >=400, retry-exhaustion, network) is kept so
        # live API breakage is diagnosable after the fact.
        if error or (status is not None and status >= 400):
            try:
                from ..file_logger import log as _fl
                _fl("api", {"event": "api_error", "method": method, "path": path,
                            "status": status, "latency_ms": round(latency_ms, 1),
                            "retries": retries, "error": self._redact(error)[:400]})
            except Exception:
                pass

    def _ep(self, key: str, margin: bool = False) -> dict:
        src = (self.profile.get("margin") or {}).get("endpoints", {}) if margin \
            else self.profile.get("endpoints", {})
        ep = src.get(key)
        if not ep or not ep.get("path"):
            raise ExchangeNotSupported(f"endpoint '{key}' not configured in profile '{self.id}'")
        return ep

    # ── public market data ───────────────────────────────────────────
    def get_candles(self, symbol: str, resolution: str, from_ts: int, to_ts: int) -> List[Any]:
        """Returns bot.models.Candle-compatible dicts via the profile parser.

        The engine consumes .ts/.o/.h/.l/.c/.v attributes, so we materialize
        lightweight objects (types.SimpleNamespace) from parsed rows.
        """
        from ..models import Candle

        ep = self._ep("candles")
        tf_param = self.tf_param_map.get(str(resolution), str(resolution))
        sym = self.to_exchange_symbol(symbol)
        # Toman alias fallback (2026-09-07): some exchanges name candle symbols
        # differently from stats keys (Nobitex: candles want USDTIRT, stats
        # keys are 'usdt-rls'). If the first try 404s/InvalidSymbol, retry the
        # alias spellings before giving up.
        _alias_tails = {"IRT": ("RLS", "TMN"), "RLS": ("IRT", "TMN"), "TMN": ("IRT", "RLS")}
        sym_candidates = [sym]
        for q in sorted({str(x).upper() for x in self.quote_currencies}, key=len, reverse=True):
            if sym.endswith(q) and len(sym) > len(q) and sym[-3:] in _alias_tails:
                base = sym[:-len(q)]
                sym_candidates += [base + al for al in _alias_tails[sym[-3:]]]
                break
        tmpl = {
            "{symbol}": sym, "{tf}": tf_param, "{from}": str(int(from_ts)),
            "{to}": str(int(to_ts)), "{from_ms}": str(int(from_ts) * 1000),
            "{to_ms}": str(int(to_ts) * 1000), "{limit}": "1000",
        }
        # resolve {symbol}/{tf} placeholders embedded in the PATH itself
        # (dYdX style: /v4/candles/perpetualMarkets/{symbol})
        path = ep.get("path", "")
        for ph, val in tmpl.items():
            path = path.replace(ph, val)
        ep = {**ep, "path": path}
        params = {}
        for k, v in (ep.get("params") or {}).items():
            sk = str(v).replace("{{", "{").replace("}}", "}")   # tolerate {{x}} style
            for ph, val in tmpl.items():
                sk = sk.replace(ph, val)
            params[k] = sk
        raw = None
        last_err = None
        for cand in sym_candidates:
            cand_params = {k: v.replace(sym, cand) for k, v in params.items()}
            cand_tmpl = {k: (val.replace(sym, cand) if isinstance(val, str) else val)
                         for k, val in tmpl.items()}
            raw = self._request(ep, extra_params=cand_params,
                                return_on_http_error=True,
                                tmpl_context=cand_tmpl)
            if isinstance(raw, dict) and raw.get("status") == "failed" and raw.get("code") == "InvalidSymbol":
                last_err = raw
                continue
            break
        if raw is None:
            raise ExchangeNotSupported(f"candles failed for all symbol aliases: {last_err}")
        fmt = ep.get("result_format", "objects")
        parser = _PARSERS.get(fmt)
        if parser is None:
            raise ExchangeNotSupported(f"result_format '{fmt}' not implementable in template format")
        payload = raw
        rp = str(ep.get("result_path") or "").strip()
        if rp:
            drilled = _dig(raw, rp)
            payload = drilled if drilled is not None else raw
        # Kraken/Apex style: a dict keyed by pair name ({XXBTZUSD: [...]} or
        # {'data': {'BTCUSDT': [...]}}). Unwrap dict-of-lists recursively (max 3
        # levels) until a list of rows is found.
        # FIX(regression): SKIP for udf — a UDF payload {s,t:[],o:[],...} IS a
        # dict-of-lists; unwrapping it fed the bare t[] array into the UDF
        # parser (exir/nobitex: 'list' has no attribute get) and killed every
        # UDF-profile chart after the Kraken unwrap was added.
        for _ in range(3 if fmt != "udf" else 0):
            if isinstance(payload, dict) and payload:
                first_list = next((v for v in payload.values() if isinstance(v, list)), None)
                if first_list is None:
                    # no list values — drill into the first dict value instead
                    first_dict = next((v for v in payload.values() if isinstance(v, dict)), None)
                    payload = first_dict if first_dict is not None else payload
                    continue
                payload = first_list
                break
            break
        rows = parser(payload, ep.get("field_map"))
        # some exchanges (OKX v5, CoinEx v2) return newest-first — the whole
        # engine/backtest contract is ascending, so normalize here once.
        rows.sort(key=lambda r: r["ts"])
        out: List[Candle] = []
        for r in rows:
            if r.get("ts"):
                out.append(Candle(ts=r["ts"], o=r["open"], h=r["high"],
                                  l=r["low"], c=r["close"], v=r["volume"]))
        return out

    def get_markets(self) -> List[dict]:
        ep = self._ep("markets")
        now = time.time()
        if self._markets_cache and now - self._markets_cache_ts < 300:
            return self._markets_cache
        raw = self._request(ep)
        rows = raw if isinstance(raw, list) else (_dig(raw, ep.get("result_path") or "") or raw)
        if isinstance(rows, dict):
            # object keyed by market name (Nobitex stats style: {"btc-usdt": {...}})
            if "markets" in rows or "result" in rows:
                rows = rows.get("markets") or rows.get("result") or []
            else:
                rows = [{"key": k, **(v if isinstance(v, dict) else {})}
                        for k, v in rows.items()]
        fmt = ep.get("result_format", "objects")
        fm = ep.get("field_map") or {}
        # Quote suffixes drive quote-asset tagging: a Nobitex stats row key
        # 'usdt-rls' normalizes to USDTRLS — its quote is RLS (IRT/Toman).
        # First pass has no cache yet, so derive suffixes from the RAW keys
        # ('usdt-rls' → RLS) and add the Iranian-Toman alias family outright:
        # IRT / RLS / TMN are the same Toman unit under different notations.
        _qs = {str(q).upper() for q in self.quote_currencies}
        _raw_keys: list = []
        if isinstance(rows, dict):
            _raw_keys = list(rows.keys())
        else:
            for r in rows:
                if isinstance(r, dict):
                    v = _row_get(r, fm.get("symbol", "symbol"))
                    if v:
                        _raw_keys.append(str(v))
        for k in _raw_keys:
            ku = k.upper().replace("-", "").replace("_", "")
            for alias in ("IRT", "RLS", "TMN"):
                if ku.endswith(alias):
                    _qs.add(alias)
        _qs = sorted(_qs, key=len, reverse=True)
        out: List[dict] = []
        for row in rows:
            if isinstance(row, dict):
                sym = str(_row_get(row, fm.get("symbol", "symbol")) or "")
                norm = self.normalize_symbol(sym)
                quote = str(_row_get(row, fm.get("quote", "quote")) or "")
                if not quote:
                    for q in _qs:
                        if norm.endswith(q) and len(norm) > len(q):
                            quote = q
                            break
                out.append({
                    "symbol": norm,
                    "base_asset": str(_row_get(row, fm.get("base", "base")) or sym[:-len(quote)] if quote and sym.endswith(quote) else (_row_get(row, fm.get("base", "base")) or sym)),
                    "quote_asset": quote,
                    "is_spot": bool(row.get(fm.get("is_spot", "is_spot"), True)),
                    "is_margin": bool(row.get(fm.get("is_margin", "is_margin"), False)),
                    "price": float(row.get(fm.get("price", "price")) or 0),
                    "stats": row,
                })
            elif isinstance(row, (list, tuple)) and row:
                out.append({"symbol": self.normalize_symbol(str(row[0])),
                            "base_asset": str(row[0]), "is_spot": True,
                            "is_margin": False, "price": 0.0, "stats": {}})
        self._markets_cache = out
        self._markets_cache_ts = now
        return out

    def get_ticker(self, symbol: str) -> dict:
        key = "ticker"
        if key not in (self.profile.get("endpoints") or {}):
            # derive from markets list if the profile has no ticker endpoint
            sym = self.normalize_symbol(symbol)
            for m in self.get_markets():
                if m["symbol"] == sym:
                    return {"price": m.get("price") or 0, "symbol": sym}
            return {}
        ep = self._ep(key)
        params = {k: str(v).replace("{{", "{").replace("}}", "}")
                       .replace("{symbol}", self.to_exchange_symbol(symbol))
                  for k, v in (ep.get("params") or {}).items()}
        data = self._request(ep, path_params={"symbol": self.to_exchange_symbol(symbol)},
                             extra_params=params)
        fm = ep.get("field_map") or {}
        # list responses (Bitunix /api/v1/futures/market/tickers): find the row
        # whose field-mapped symbol matches the requested symbol.
        if isinstance(data, list) and fm:
            ex_sym = self.to_exchange_symbol(symbol)
            for row in data:
                if isinstance(row, dict) and str(row.get(fm.get("symbol", "symbol"), "")) == ex_sym:
                    data = row
                    break
        if isinstance(data, dict):
            px = data.get(fm.get("price", "price")) or data.get("last") or data.get("price") or 0
            if px:
                return {"price": float(px or 0), "symbol": symbol, "raw": data}
            # keyed-object response (Nobitex stats style: {"btc-usdt": {...}}):
            # find the row whose key matches this symbol and read its price field.
            ex_sym = self.to_exchange_symbol(symbol)
            # quote aliases: an exchange may SAY IRT/TMN but key rows as RLS
            # (Nobitex: symbol USDTIRT, stats key 'usdt-rls'). Canonical alias
            # chain: IRT↔RLS↔TMN (all = Iranian Toman, 1 IRT = 10 RLS).
            _aliases = {"IRT": ("RLS", "TMN"), "RLS": ("IRT", "TMN"), "TMN": ("IRT", "RLS")}
            # trim the quote suffix safely (longest matching suffix first) instead
            # of a fixed -3 slice (some quotes are 4+ chars: USDT, USDC, 1MBONK…)
            base = None
            for q in sorted({str(x).upper() for x in self.quote_currencies}, key=len, reverse=True):
                if ex_sym.endswith(q) and len(ex_sym) > len(q):
                    base = ex_sym[:-len(q)]
                    break
            candidates = [ex_sym]
            if base and ex_sym[-3:] in _aliases:
                for al in _aliases.get(ex_sym[-3:], ()):
                    candidates.append(base + al)
            wanted = {self.normalize_symbol(x) for x in ([ex_sym] + candidates)}
            for k, v in data.items():
                # scalar values keyed by symbol (allMids style: {'BTC': '78864'}):
                # a matching key IS the price.
                if not isinstance(v, dict):
                    if self.normalize_symbol(str(k)) in wanted and v:
                        try:
                            return {"price": float(v), "symbol": symbol, "raw": {k: v}}
                        except (TypeError, ValueError):
                            continue
                    continue
                # match: row identity (field-mapped symbol value or the raw key)
                # against the requested symbol OR any of its quote aliases.
                row_sym = str(v.get(fm.get("symbol", ""), "") or k)
                norm_row = self.normalize_symbol(row_sym)
                norm_key = self.normalize_symbol(str(k))
                if norm_row in wanted or norm_key in wanted:
                    px = v.get(fm.get("price", "price")) or v.get("latest") or v.get("last") or 0
                    return {"price": float(px or 0), "symbol": symbol, "raw": {k: v}}
            # last resort: derive from the markets cache (already keyed+normalized)
            sym = self.normalize_symbol(symbol)
            for m in self.get_markets():
                if m["symbol"] == sym:
                    return {"price": m.get("price") or 0, "symbol": sym}
        return {}

    def get_depth(self, symbol: str, limit: int = 50) -> dict:
        ep = self._ep("depth")
        ex_sym = self.to_exchange_symbol(symbol)
        # {limit} is not a path placeholder — resolve it here (the probe and
        # engine call get_depth(symbol) with no explicit depth level).
        extra = {k: str(v).replace("{{", "{").replace("}}", "}")
                      .replace("{symbol}", ex_sym).replace("{limit}", str(limit))
                 for k, v in (ep.get("params") or {}).items()}
        data = self._request(ep, path_params={"symbol": ex_sym}, extra_params=extra)
        return data if isinstance(data, dict) else {}

    # ── private account/trading (optional endpoints) ─────────────────
    def get_balances(self) -> dict:
        ep = self._ep("balances")
        data = self._request(ep, auth=True)
        if not isinstance(data, dict):
            return {}
        fm = ep.get("field_map") or {}
        wallets = data.get(fm.get("wallets", "wallets")) if fm.get("wallets") else data
        wallets = wallets if isinstance(wallets, dict) else data
        # normalize per-asset shape to {available, locked, value} (broker +
        # /api/live/balance contract). Handles both flat strings (Nobitex
        # {balance, blocked}) and nested {available, locked}/{free, used}.
        out: Dict[str, dict] = {}
        for asset, info in wallets.items():
            if isinstance(info, dict):
                a = fm.get("available", "available")
                l = fm.get("locked", "locked")
                free = info.get(a, info.get("balance", info.get("free", info.get("value", 0))))
                locked = info.get(l, info.get("blocked", info.get("used", 0)))
            elif isinstance(info, (int, float, str)):
                free, locked = info, 0
            else:
                continue
            try:
                free_f, locked_f = float(free), float(locked or 0)
            except (TypeError, ValueError):
                continue
            key = str(asset).upper()
            out[key] = {"available": free_f, "locked": locked_f, "value": free_f + locked_f}
        return out

    def get_fees(self) -> dict:
        return self._request(self._ep("fees"), auth=True)

    def _split_base_quote(self, symbol: str) -> tuple:
        """Canonical 'BTCUSDT' -> ('BTC', 'USDT') using known quote suffixes."""
        s = self.normalize_symbol(symbol)
        for q in sorted({str(x).upper() for x in self.quote_currencies}, key=len, reverse=True):
            if s.endswith(q) and len(s) > len(q):
                return s[: -len(q)], q
        return s, ""

    def _map_client_id(self, client_id: str) -> str:
        """Apply the profile's clientOrderId charset transform (both sides:
        placement AND status/cancel queries, so the round-trip matches)."""
        mode = str(self.profile.get("client_id_map") or "none")
        cid = client_id or ""
        if mode == "dash":
            return cid.replace("_", "-")
        if mode == "alnum":
            return "".join(ch for ch in cid if ch.isalnum())
        return cid

    def _sub_body(self, ep: dict, subs: Dict[str, str]) -> dict:
        body: Dict[str, Any] = {}
        for k, v in (ep.get("body") or {}).items():
            sv = str(v)
            for ph, val in subs.items():
                sv = sv.replace(ph, val)
            if ep.get("strip_empty") and sv == "":
                continue
            body[k] = sv
        return body

    def _map_result(self, raw: Any, ep: dict) -> Any:
        """Optional result_path dig + broker-facing field aliasing
        (result_map: app-field -> exchange-field, shallow; dot paths ok).
        Falls back to the raw object when result_path yields nothing (e.g. a
        failed order has no 'order' key) so the caller still sees status/code."""
        rp = ep.get("result_path")
        if rp:
            dug = _dig(raw, rp)
            if dug not in (None, {}, []):
                raw = dug
        rm = ep.get("result_map") or {}
        if isinstance(raw, dict) and rm:
            out = dict(raw)
            for app_f, ex_f in rm.items():
                if app_f not in out:
                    out[app_f] = _dig(raw, ex_f) if "." in str(ex_f) else raw.get(ex_f)
            return out
        return raw

    def place_order(self, symbol: str, side: str, order_type: str,
                    quantity: str, price: Optional[str] = None,
                    stop_price: Optional[str] = None,
                    client_id: Optional[str] = None) -> dict:
        ep = self._ep("place_order")
        base, quote = self._split_base_quote(symbol)
        ot_map = ep.get("order_type_map") or {}
        execution = str(ot_map.get(str(order_type).upper(), str(order_type).lower()))
        subs = {"{symbol}": self.to_exchange_symbol(symbol), "{side}": side,
                "{type}": order_type, "{execution}": execution,
                "{qty}": quantity,
                "{price}": price or "", "{stop}": stop_price or "",
                "{client_id}": self._map_client_id(client_id or ""),
                "{base}": base, "{quote}": quote,
                "{base_lc}": base.lower(), "{quote_lc}": quote.lower(),
                "{side_lower}": str(side).lower()}
        body = self._sub_body(ep, subs)
        if not body:
            raise ExchangeNotSupported("place_order.body template missing in profile")
        return self._map_result(self._request(ep, json_body=body, auth=True), ep)

    def get_open_orders(self, symbol: Optional[str] = None) -> dict:
        ep = self._ep("open_orders")
        params = {k: str(v).replace("{symbol}", self.to_exchange_symbol(symbol or ""))
                  for k, v in (ep.get("params") or {}).items()}
        return self._map_result(self._request(ep, extra_params=params, auth=True), ep)

    def get_order(self, client_id: str) -> dict:
        ep = self._ep("order")
        subs = {"{client_id}": self._map_client_id(client_id)}
        body = self._sub_body(ep, subs) if ep.get("body") else None
        return self._map_result(self._request(ep, path_params={"client_id": subs["{client_id}"]},
                                              json_body=body, auth=True), ep)

    def cancel_order(self, client_id: str) -> dict:
        ep = self._ep("cancel_order")
        subs = {"{client_id}": self._map_client_id(client_id)}
        body = self._sub_body(ep, subs) if ep.get("body") else None
        if body is None and ep.get("method", "DELETE").upper() in ("POST", "PATCH", "DELETE"):
            body = {}
        return self._map_result(self._request(ep, path_params={"client_id": subs["{client_id}"]},
                                              json_body=body, auth=True), ep)

    # ── margin (only when profile.margin exists) ─────────────────────
    def _margin_ep(self, key: str) -> dict:
        m = self.profile.get("margin")
        if not m:
            raise ExchangeNotSupported(f"profile '{self.id}' has no margin block")
        ep = (m.get("endpoints") or {}).get(key)
        if not ep or not ep.get("path"):
            raise ExchangeNotSupported(f"margin endpoint '{key}' not configured — capability limited, reported in setup")
        return ep

    def margin_get_markets(self) -> List[dict]:
        return self._request(self._margin_ep("margin_markets"))

    def margin_get_positions(self, active: Optional[bool] = None,
                             market: Optional[str] = None,
                             position_side: Optional[str] = None) -> List[dict]:
        ep = self._margin_ep("margin_positions")
        params: Dict[str, Any] = {}
        if active is not None:
            params["active"] = str(active).lower()
        if market:
            params["market"] = self.to_exchange_symbol(market)
        return self._request(ep, extra_params=params, auth=True)

    def margin_open_position(self, market: str, side: str, collateral: str,
                             risk_coef: str, open_price: str = "0",
                             stop_loss: str = "", take_profit: str = "") -> dict:
        ep = self._margin_ep("margin_open")
        body = {k: str(v).replace("{market}", self.to_exchange_symbol(market))
                .replace("{side}", side).replace("{collateral}", collateral)
                .replace("{risk_coef}", risk_coef).replace("{open_price}", open_price)
                .replace("{sl}", stop_loss).replace("{tp}", take_profit)
                for k, v in (ep.get("body") or {}).items()}
        return self._request(ep, json_body=body, auth=True)

    def margin_close_position(self, position_id: str, price: str = "0") -> dict:
        ep = self._margin_ep("margin_close")
        return self._request(ep, path_params={"position_id": position_id},
                             json_body={"price": price}, auth=True)

    def margin_update_sltp(self, position_id: str, stop_loss: str = "",
                           take_profit: str = "") -> dict:
        ep = self._margin_ep("margin_sltp")
        body = {k: str(v).replace("{sl}", stop_loss).replace("{tp}", take_profit)
                for k, v in (ep.get("body") or {}).items()}
        return self._request(ep, path_params={"position_id": position_id},
                             json_body=body, auth=True)

    def margin_dry_run(self, market: str, side: str, collateral: str,
                       risk_coef: str, open_price: str = "",
                       stop_loss: str = "", take_profit: str = "") -> dict:
        ep = self._margin_ep("margin_dry_run")
        body = {k: str(v).replace("{market}", self.to_exchange_symbol(market))
                .replace("{side}", side).replace("{collateral}", collateral)
                .replace("{risk_coef}", risk_coef).replace("{open_price}", open_price)
                .replace("{sl}", stop_loss).replace("{tp}", take_profit)
                for k, v in (ep.get("body") or {}).items()}
        return self._request(ep, json_body=body)
