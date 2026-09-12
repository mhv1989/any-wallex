"""Setup wizard engine — AI-driven exchange onboarding.

Flow (per the multi-exchange plan §2.5):
  research: catalog/docs URL → fetch+search doc pages → LLM constrained-JSON
            extraction → profile.json (validated, ≤3 retries) → setup report
  probe:    live probe suite (markets + candles per TF with spacing-histogram
            granularity truth) → status probed/error → report update
  diagnose: AI re-reads report + logs → corrected profile → re-probe
  curate:   AI auto-pair — full catalog → curated symbols_master per quote

All long work runs in background jobs ({id: status/phase/result}); the
frontend polls. Secrets (AI key, search key) live in CryptoStore, masked on
every GET. Anything the profile format cannot express is recorded in the
profile's limitations[] and the setup report — never silently dropped.
"""
from __future__ import annotations

import json
import logging
import re
import threading
import time
import uuid
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse, urljoin

import httpx

from .ai_strategy import AIStrategyClient, AIProviderConfig, AIProviderError
from .crypto_store import CryptoStore
from .exchange import profile as P
from .exchange.generic import ExchangeNotSupported, GenericRESTAdapter

log = logging.getLogger("wallex.wizard")

ROOT = Path(__file__).resolve().parent.parent
PROFILES_ROOT = ROOT / "data" / "profiles"
CATALOG_PATH = ROOT / "data" / "exchange_catalog.json"

DOC_FETCH_BUDGET_PAGES = 8
DOC_FETCH_MAX_BYTES = 60_000
CANDLES_PROBE_N = 5


# ── doc fetching ─────────────────────────────────────────────────────

class DocFetcher:
    """Fetch official docs pages (direct URLs + same-domain crawl + optional
    web search). Produces a compact text corpus for the LLM."""

    def __init__(self, search_provider: str = "duckduckgo", search_key: str = ""):
        self.search_provider = search_provider
        self.search_key = search_key
        self._http = httpx.Client(timeout=25.0, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AnyWallexWizard/0.1",
            "Accept": "text/html,application/json;q=0.9,*/*;q=0.8",
        })
        self.pages: List[dict] = []   # {url, ok, text}
        self.log: List[str] = []

    def close(self) -> None:
        """FIX(audit-M8): release the httpx pool — one DocFetcher leaked
        two sockets per research job before."""
        try:
            self._http.close()
        except Exception:
            pass

    def _strip_html(self, html: str) -> str:
        html = re.sub(r"<script[\s\S]*?</script>", " ", html, flags=re.I)
        html = re.sub(r"<style[\s\S]*?</style>", " ", html, flags=re.I)
        html = re.sub(r"<[^>]+>", " ", html)
        html = re.sub(r"&nbsp;|&#160;", " ", html)
        html = re.sub(r"&amp;", "&", html)
        html = re.sub(r"&lt;", "<", html)
        html = re.sub(r"&gt;", ">", html)
        html = re.sub(r"&quot;", '"', html)
        html = re.sub(r"[ \t]+", " ", html)
        html = re.sub(r"\n\s*\n+", "\n", html)
        return html.strip()

    def fetch(self, url: str) -> Optional[str]:
        try:
            r = self._http.get(url, follow_redirects=True)
            ct = r.headers.get("content-type", "")
            if r.status_code != 200:
                self.log.append(f"fetch {url} -> HTTP {r.status_code}")
                return None
            if "json" in ct or url.rstrip("/").endswith(("openapi.json", "swagger.json")):
                try:
                    return json.dumps(r.json())[:DOC_FETCH_MAX_BYTES]
                except Exception:
                    return None
            if "html" in ct or "text" in ct:
                return self._strip_html(r.text)[:DOC_FETCH_MAX_BYTES]
            return None
        except Exception as exc:
            self.log.append(f"fetch {url} failed: {exc}")
            return None

    def discover_links(self, base_url: str, html_text: str, limit: int = 12) -> List[str]:
        """Same-domain links that look like API docs sub-pages."""
        host = urlparse(base_url).netloc
        found, seen = [], set()
        for m in re.finditer(r'href=["\']([^"\'#]+)["\']', html_text or ""):
            href = m.group(1)
            if href.startswith(("javascript:", "mailto:")):
                continue
            full = urljoin(base_url, href)
            p = urlparse(full)
            if p.netloc != host or p.scheme not in ("http", "https"):
                continue
            low = full.lower()
            if not any(k in low for k in ("doc", "api", "reference", "guide", "endpoint", "swagger", "openapi")):
                continue
            if full in seen:
                continue
            seen.add(full)
            found.append(full)
            if len(found) >= limit:
                break
        return found

    def docs_url_alive(self, url: str) -> bool:
        """Does a docs URL actually serve content? (2026-09-07: catalog
        entries rot — MEXC's mexc.com/api is a 404 HTML page, Bybit's
        /docs/v5/ is a 404, Kraken's /rest/ is a 404. A 404 page can be
        200KB of markup, so the check requires 200 + substantial body,
        not just a response.)"""
        try:
            r = self._http.get(url, timeout=12, follow_redirects=True)
            ok = r.status_code == 200 and len(r.text or "") > 2000
            if not ok:
                self.log.append(f"docs URL dead: {url} -> HTTP {r.status_code}/{len(r.text or '')} chars")
            return ok
        except Exception as exc:
            self.log.append(f"docs URL unreachable: {url} -> {type(exc).__name__}")
            return False

    def web_search(self, query: str, limit: int = 6) -> List[str]:
        """Pluggable search: serpapi (key) | multi-engine no-key fallback.

        No-key mode tries engines IN ORDER until one yields links
        (2026-09-07: DDG html endpoint returned 200 but zero parsed links
        from some networks — a single-engine search silently produced empty
        results and killed docs discovery). Order: html.duckduckgo →
        lite.duckduckgo → bing. All three verified reachable no-key."""
        if self.search_provider == "none":
            return []
        if self.search_provider == "serpapi" and self.search_key:
            try:
                r = self._http.get("https://serpapi.com/search.json",
                                   params={"q": query, "api_key": self.search_key, "num": limit})
                data = r.json()
                links = [x.get("link") for x in (data.get("organic_results") or [])[:limit]
                         if x.get("link")]
                self.log.append(f"search(serpapi) '{query}' -> {len(links)} results")
                return links
            except Exception as exc:
                self.log.append(f"search(serpapi) failed: {exc}")
                # fall through to no-key engines rather than dying
        from urllib.parse import unquote, parse_qs
        ua = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AnyWallexWizard/0.1"}

        def _ddg_links(text: str, use_result_class: bool = True) -> List[str]:
            """DDG html endpoint uses class="result__a"; the lite endpoint
            uses plain <a href> anchors (no class) — parse accordingly."""
            pat = (r'class="result__a"[^>]*href="([^"]+)"' if use_result_class
                   else r'<a[^>]+href="(https?://[^"]+)"[^>]*>')
            links = []
            for m in re.finditer(pat, text):
                href = m.group(1)
                if href.startswith("//duckduckgo.com/l/?uddg="):
                    q = parse_qs(urlparse("https:" + href).query).get("uddg", [""])[0]
                    href = unquote(q) if q else href
                if "duckduckgo.com" in href:
                    continue
                if href.startswith("http"):
                    links.append(href)
                if len(links) >= limit:
                    break
            return links

        engines = [
            ("duckduckgo-html", "https://html.duckduckgo.com/html/",
             lambda: self._http.post("https://html.duckduckgo.com/html/",
                                     data={"q": query}, follow_redirects=True, headers=ua)),
            ("duckduckgo-lite", "https://lite.duckduckgo.com/lite/",
             lambda: self._http.post("https://lite.duckduckgo.com/lite/",
                                     data={"q": query}, follow_redirects=True, headers=ua)),
            ("bing", "https://www.bing.com/search",
             lambda: self._http.get("https://www.bing.com/search",
                                    params={"q": query}, follow_redirects=True, headers=ua)),
        ]
        for name, _url, do in engines:
            try:
                r = do()
                if name == "bing":
                    links = []
                    for m in re.finditer(r'<h2><a[^>]*href="(https?://[^"]+)"', r.text):
                        href = m.group(1)
                        if "bing.com" in href or "microsoft.com" in href:
                            continue
                        links.append(href)
                        if len(links) >= limit:
                            break
                elif name == "duckduckgo-lite":
                    links = _ddg_links(r.text, use_result_class=False)
                else:
                    links = _ddg_links(r.text, use_result_class=True)
                if links:
                    self.log.append(f"search({name}) '{query}' -> {len(links)} results")
                    return links
                self.log.append(f"search({name}) '{query}' -> 0 results (engine alive, no links)")
            except Exception as exc:
                self.log.append(f"search({name}) failed: {exc}")
        self.log.append(f"search: all engines returned nothing for '{query}'")
        return []

    def build_corpus(self, docs_urls: List[str], exchange_name: str,
                     custom_query: str = "") -> str:
        """Direct fetch + link discovery + optional search → combined corpus."""
        pages_done = 0
        queue = list(dict.fromkeys(docs_urls))
        tried: set = set()

        # search first when the primary docs URL is unknown/unreachable
        if custom_query:
            for u in self.web_search(f"{custom_query} {exchange_name} REST API documentation")[:4]:
                queue.append(u)

        # probe standard spec files
        for d in list(docs_urls):
            u = urlparse(d)
            spec = f"{u.scheme}://{u.netloc}/openapi.json"
            if spec not in queue:
                queue.append(spec)

        corpus_parts: List[str] = []
        while queue and pages_done < DOC_FETCH_BUDGET_PAGES:
            url = queue.pop(0).split("#")[0]
            if url in tried or not url.startswith("http"):
                continue
            tried.add(url)
            text = self.fetch(url)
            pages_done += 1
            if not text:
                continue
            self.pages.append({"url": url, "ok": True, "chars": len(text)})
            corpus_parts.append(f"===== PAGE: {url} =====\n{text[:DOC_FETCH_MAX_BYTES]}")
            if len(corpus_parts) == 1 and "<" in text[:2000]:
                # first page was HTML — discover same-domain sub-pages
                for link in self.discover_links(url, text)[:6]:
                    if link not in tried:
                        queue.append(link)

        self.log.append(f"corpus: {len(corpus_parts)} pages, {pages_done} fetched")
        return "\n\n".join(corpus_parts)[:240_000]


# ── LLM extraction ───────────────────────────────────────────────────

_REFERENCE_PROFILE = """{
  "id": "okx", "name": "OKX", "base_url": "https://www.okx.com",
  "auth": {"scheme": "none"},
  "symbol_format": {"separator": "-", "case": "upper", "quote_suffixes": ["USDT","USDC","BTC"]},
  "endpoints": {
    "candles": {"method": "GET", "path": "/api/v5/market/candles",
                "params": {"instId": "{symbol}", "bar": "{tf}", "after": "{to_ms}", "limit": "{limit}"},
                "result_format": "arrays", "result_path": "data"},
    "markets": {"method": "GET", "path": "/api/v5/market/tickers",
                "params": {"instType": "SPOT"}, "result_path": "data", "result_format": "objects",
                "field_map": {"symbol": "instId", "base": "baseCcy", "price": "last"}}
  },
  "tf_param_map": {"15": "15m", "60": "1H", "240": "4H", "1D": "1Dutc"},
  "true_res_map": {"15": "15", "60": "60", "240": "240", "1D": "1D"},
  "min_gap_sec": 0.3, "quotes": ["USDT", "USDC", "BTC"], "margin": null
}"""

EXTRACTION_PROMPT = """You are configuring a trading-bot exchange adapter. Study the API DOCUMENTATION below and output ONE JSON object describing the exchange.

OUTPUT CONTRACT (all fields required unless marked optional):
{{
  "base_url": "https://...",            // REST API base for PUBLIC market data
  "auth": {{"scheme": "none|header|hmac", "header_name": "...",        // header_name only for scheme=header
           "hmac": {{"param_order": ["method","path","query","body","expires","api_key"], "headers": {{"Header": "{{key}}|{{sig}}|{{expires}}|{{passphrase}}"}}, "algo": "sha256"}}}},
  "symbol_format": {{"separator": ""|"-"/"_"/"/", "case": "upper|lower", "quote_suffixes": ["USDT", ...]}},
  "endpoints": {{
    "candles": {{"method": "GET", "path": "/path/{{path_param}}", "params": {{"k": "{{symbol}}|{{tf}}|{{from}}|{{to}}|{{from_ms}}|{{to_ms}}|{{limit}}"}},
                "result_format": "udf|arrays|objects", "result_path": "optional.dot.path",
                "field_map": {{"ts": "...", "open": "...", "high": "...", "low": "...", "close": "...", "volume": "..."}}},
    "markets": {{...same shape...}},
    "ticker": {{...}},          // optional; omit if docs lack it
    "depth": {{...}}            // optional
  }},
  "margin": null,                        // null if no margin trading API
  "tf_param_map": {{"15": "...", "60": "...", "240": "...", "1D": "..."}},   // exchange's interval values
  "min_gap_sec": 1.0,                    // from the docs' rate limit
  "quotes": ["USDT", ...],               // quote currencies the exchange supports
  "rules": {{                             // per-exchange order limits — paper trading mirrors these
    "min_order_usdt": 10.0,              // minimum spot order value (USDT-equiv); 0 = none stated
    "min_collateral_usdt": 10.0,         // margin: minimum collateral per position
    "max_collateral_usdt": 100000.0,     // margin: maximum collateral
    "max_risk_coef": 3.0,                // margin: max leverage (e.g. 5 for 5x)
    "price_band_pct": 5.0,               // limit/marketable price band; 0 = wide/unstated (no local check)
    "qty_step": 8,                       // quantity decimal precision
    "fee_pct": 0.2,                      // spot taker fee per side, in percent
    "interest_per_4h_pct": 0.05          // margin renewal/position fee per 4h, in percent
  }}
}}

HARD RULES:
1. Use ONLY what the documentation states. Never guess an endpoint path, parameter, or response shape — omit it and add a limitation instead.
2. limitations: array of {{"category": "auth_scheme|response_shape|order_type|margin_model|docs_unreadable|rules", "detail": "..."}} for EVERYTHING the template above cannot express.
3. margin_style (top level, optional): "isolated_margin" | "futures" | "cross_margin" | "dex_perp" — classify from the docs; Iranian collateral/leverage models are "isolated_margin"; perpetual/funding-rate models are "futures".
4. candles result_format: "udf" for TradingView UDF {{s,t,o,h,l,c,v}}; "arrays" for columnar arrays or [[ts,o,h,l,c,v],...]; "objects" for lists of JSON objects.
5. Timestamps: note if the API uses milliseconds (from_ms/to_ms templates handle it).
6. rules: fill ONLY from stated values (fee tables, "minimum order", leverage caps, precision). For any value the docs do NOT state, keep the default shown and add a limitation {{category:"rules", detail:"<field> not documented — using conservative default <v>"}}. price_band_pct=0 when the docs state no price band.
7. Output ONLY the JSON object — no markdown fences, no commentary.

REFERENCE EXAMPLE (shape only — values are OKX's, not this exchange's):
{ref}

DOCUMENTATION:
{corpus}

OUTPUT BUDGET RULES (critical for small-context models):
- Output COMPACT JSON: no markdown, no explanations, no comments.
- OMIT any field you could not verify from the docs — never invent values.
- Keep strings short; one-line values only.

KNOWLEDGE LIBRARY: if a VERIFIED KNOWLEDGE BASE block is present below, treat
it as prior expectations from previously-connected exchanges (endpoint shapes,
auth schemes, symbol naming, pagination). Reuse the closest matching convention
for this exchange, then verify against this exchange's docs. Also search the
web for the exchange's official API docs when the provided corpus is incomplete
(rate limits, auth signature scheme, candle endpoint path)."""


class WizardEngine:
    def __init__(self, store: CryptoStore, storage=None):
        self.store = store
        self.storage = storage
        self.jobs: Dict[str, dict] = {}
        self._lock = threading.Lock()
        # Per-job logger (thread-local): research/probe/calibrate/diagnose run
        # on worker threads and target a SPECIFIC exchange, so each job sets a
        # FileLogger rooted at that profile's Logs/<pid>/ folder. DocFetcher
        # and the job fn read the same thread-local, no constructor threading.
        self._tls = threading.local()

    def _job_logger(self, profile_id: str):
        """FileLogger for the target profile (reuses one per job thread).

        Also sets the file_logger thread override so AI calls made DURING the
        job (via ai_strategy._ai_log → get_active()) land in the SAME target
        profile folder, not the host backend's."""
        pid = profile_id or "unknown"
        fl = getattr(self._tls, "logger", None)
        if fl is None or getattr(fl, "profile_id", None) != pid:
            try:
                from .file_logger import FileLogger, set_thread_override
                run_id = time.strftime("%Y%m%d_%H%M%S")
                fl = FileLogger(ROOT / "Logs" / pid, run_id + "_wiz", profile_id=pid)
                self._tls.logger = fl
                set_thread_override(fl)   # redirect this job thread's AI logs too
            except Exception:
                return None
        return fl

    def _wlog(self, profile_id: str, data: dict) -> None:
        """Best-effort structured wizard log to the target profile's folder."""
        try:
            fl = self._job_logger(profile_id)
            if fl is not None:
                fl.write("wizard", data)
        except Exception:
            pass

    # ── job runner ───────────────────────────────────────────────────
    def _start_job(self, kind: str, fn, *args) -> str:
        jid = uuid.uuid4().hex[:10]
        job = {"id": jid, "kind": kind, "status": "running", "phase": "start",
               "started": time.time(), "result": None, "error": None}
        with self._lock:
            # FIX(audit-M8): cap concurrent jobs — each job spawns doc crawls
            # + 3× AI calls; unbounded double-clicks piled up full pipelines.
            running = [j for j in self.jobs.values()
                       if j["status"] == "running"]
            if len(running) >= 3:
                raise RuntimeError(
                    "موجودیت کاری فعال زیاد است — صبر کنید تا کارهای جاری تمام شوند")
            if any(j["kind"] == kind for j in running):
                raise RuntimeError("همین کار در حال اجراست — لطفاً صبر کنید")
            self.jobs[jid] = job
            if len(self.jobs) > 40:   # bound
                for k in list(self.jobs)[:-20]:
                    if self.jobs[k]["status"] != "running":
                        del self.jobs[k]

        def _run():
            try:
                job["result"] = fn(*args)
                job["status"] = "done"
                job["phase"] = "complete"
            except Exception as exc:  # noqa: BLE001
                log.exception("wizard job %s failed", jid)
                job["status"] = "error"
                job["error"] = str(exc)[:500]
                job["phase"] = "failed"
                # safety net: catch failures that escaped the job's own logging
                try:
                    from . import file_logger as _fl
                    _fl.log_error(f"wizard job '{kind}' failed",
                                  context="wizard._start_job",
                                  where="wizard._start_job",
                                  job_id=jid, kind=kind,
                                  profile_id=(str(args[0]) if args else ""),
                                  error=str(exc)[:500])
                except Exception:
                    pass
        threading.Thread(target=_run, daemon=True).start()
        return jid

    def job_state(self, jid: str) -> Optional[dict]:
        return self.jobs.get(jid)

    # ── wizard AI config ─────────────────────────────────────────────
    def set_ai_config(self, provider: str, base_url: str, api_key: str, model: str) -> dict:
        cfg = {"provider": provider or "ollama", "base_url": (base_url or "").strip(),
               "model": model or ""}
        if self.storage:
            self.storage.kv_set("wizard_ai_cfg", json.dumps(cfg))
        if api_key and "$" not in api_key[:5]:   # never store a masked echo
            self.store.set("wizard_ai_key", api_key)
        return self.ai_config_masked()

    def ai_config_masked(self) -> dict:
        cfg = {}
        if self.storage:
            try:
                cfg = json.loads(self.storage.kv_get("wizard_ai_cfg") or "{}")
            except Exception:
                cfg = {}
        key = ""
        try:
            key = self.store.get("wizard_ai_key") or ""
        except Exception:
            pass
        return {**cfg, "has_api_key": bool(key),
                "api_key_preview": (key[:4] + "…" + key[-4:]) if len(key) > 8 else bool(key)}

    def _ai_client(self) -> AIStrategyClient:
        cfg = self.ai_config_masked()
        if not cfg.get("base_url"):
            raise AIProviderError("هوش مصنوعی تنظیم نشده — ابتدا در گام دوم ویزارد API را تنظیم کنید")
        key = ""
        try:
            key = self.store.get("wizard_ai_key") or ""
        except Exception:
            pass
        pc = AIProviderConfig(provider=cfg.get("provider", "ollama"),
                              base_url=cfg.get("base_url", "http://localhost:11434"),
                              api_key=key, model=cfg.get("model", ""),
                              timeout_sec=340, retries=2,
                              stream=True)   # keep long generations alive past
                                             # Cloudflare's non-streaming idle-cut
        return AIStrategyClient(pc)

    def _search_key(self) -> str:
        try:
            return self.store.get("wizard_search_key") or ""
        except Exception:
            return ""

    def set_search_config(self, provider: str, api_key: str = "") -> dict:
        if self.storage:
            self.storage.kv_set("wizard_search_provider", provider or "duckduckgo")
        if api_key:
            self.store.set("wizard_search_key", api_key)
        return {"provider": provider or "duckduckgo", "has_key": bool(self._search_key())}

    def search_config(self) -> dict:
        prov = "duckduckgo"
        if self.storage:
            prov = self.storage.kv_get("wizard_search_provider") or "duckduckgo"
        return {"provider": prov, "has_key": bool(self._search_key())}

    # ── catalog ──────────────────────────────────────────────────────
    @staticmethod
    def catalog() -> dict:
        try:
            return json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
        except Exception:
            return {"exchanges": []}

    @staticmethod
    def catalog_entry(exchange_id: str) -> Optional[dict]:
        for e in WizardEngine.catalog().get("exchanges", []):
            if e.get("id") == exchange_id:
                return e
        return None

    # ── research ─────────────────────────────────────────────────────
    def research(self, exchange_id: str = "", custom: Optional[dict] = None) -> str:
        return self._start_job("research", self._research_sync, exchange_id, custom or {})

    def _research_sync(self, exchange_id: str, custom: dict) -> dict:
        t0 = time.time()
        name = custom.get("name") or exchange_id
        # Initialize this job thread's logger (target profile folder) + log start.
        _pid_hint = exchange_id or self._slug(name)
        self._wlog(_pid_hint, {"event": "research_start", "exchange_id": exchange_id,
                               "name": name, "custom": bool(custom.get("docs_url")),
                               "search_provider": (self.search_config().get("provider", "duckduckgo"))})
        docs_urls: List[str] = []
        query = ""
        f0 = None   # catalog pre-check fetcher (only set for catalog exchanges)
        if exchange_id:
            entry = self.catalog_entry(exchange_id)
            if not entry:
                raise ValueError(f"unknown exchange id: {exchange_id}")
            name = entry["name"]
            if exchange_id == "wallex":
                raise ValueError("Wallex is the built-in default template — no wizard setup needed")
            # catalog docs URL: verify it serves real content first. Docs
            # MOVE (MEXC mexc.com/api 404, Bybit /docs/v5/ 404, ...) — when
            # the catalog entry is stale, the built-in search replaces it
            # (user req 2026-09-07). The search query is ALWAYS
            # docs-focused; the KYC/limits prose in apiManagementNotes is
            # NOT a useful search query (it surfaced review sites + KYC
            # announcements instead of API docs).
            scfg0 = self.search_config()
            f0 = DocFetcher(search_provider=scfg0.get("provider", "duckduckgo"),
                            search_key=self._search_key())
            query = f"{name} official API documentation REST"
            cat_url = entry.get("officialDocs") or ""
            if cat_url and f0.docs_url_alive(cat_url):
                docs_urls.append(cat_url)
            else:
                if cat_url:
                    f0.log.append(f"catalog docs URL {cat_url} dead — replacing via search")
                # self-heal fired: record WHICH dead URL we replaced + why
                self._wlog(exchange_id, {"event": "catalog_url_dead_self_heal",
                                         "catalog_url": cat_url,
                                         "reason": f0.log[-1] if f0.log else ""})
                for u in f0.web_search(query, limit=6):
                    if u not in docs_urls:
                        docs_urls.append(u)
        else:
            if custom.get("docs_url"):
                docs_urls.append(custom["docs_url"])
            query = custom.get("search_query", "") or f"{name} official API documentation REST"
            if custom.get("site_url") and custom.get("site_url") not in query:
                query += f" {custom['site_url']}"

        if f0 is not None:
            f0.close()   # FIX(audit-M8): pre-check fetcher no longer needed
        scfg = self.search_config()
        fetcher = DocFetcher(search_provider=scfg.get("provider", "duckduckgo"),
                             search_key=self._search_key())
        # carry the pre-check log so the report shows WHY the catalog URL
        # was dropped (self-heal transparency) — f0 exists only for catalog
        # exchanges (custom-exchange path never sets it)
        if f0 is not None:
            fetcher.log.extend(f0.log)
        # docs_urls already carries the (verified or search-found) doc pages
        # for catalog exchanges — don't let build_corpus re-search. Only pass
        # the query for the no-URL case (custom exchange, user gave no docs).
        corpus = fetcher.build_corpus(docs_urls, name,
                                      custom_query="" if docs_urls else query)
        # corpus assembled: record pages fetched + size (the #1 troubleshooting
        # signal — was the docs actually read, and how much of it?)
        self._wlog(_pid_hint, {"event": "docs_fetched",
                               "corpus_chars": len(corpus),
                               "pages": [{"url": p.get("url"), "chars": p.get("chars", 0)}
                                         for p in getattr(fetcher, "pages", [])][:10],
                               "fetch_log": fetcher.log[:12]})
        if len(corpus) < 400:
            P.add_limitation(pdraft := P.default_profile(self._slug(name, exchange_id), name),
                             "docs_unreadable",
                             f"documentation pages could not be fetched: {fetcher.log[:6]}")
            self._wlog(_pid_hint, {"event": "docs_unreadable", "corpus_chars": len(corpus),
                                   "fetch_log": fetcher.log[:12]})
            return {"profile_id": None, "error":
                    "مستندات صرافی قابل خواندن نبود — آدرس مستندات را دستی وارد کنید یا از جستجو استفاده کنید",
                    "fetch_log": fetcher.log}

        ai = self._ai_client()
        prompt = EXTRACTION_PROMPT.replace("{ref}", _REFERENCE_PROFILE).replace("{corpus}", corpus)
        # warm-start: verified knowledge from previously-connected exchanges
        kb_block = self._knowledge_block(exclude_pid=exchange_id or self._slug(name))
        if kb_block:
            prompt = prompt + kb_block
        profile: Optional[dict] = None
        last_err = ""
        _ai_cfg = self.ai_config_masked()
        # Log the prompt shape up-front: for the connection-drops-at-~30s class
        # of failures (Cloudflare non-streaming idle-cut on long prompts) the
        # corpus_chars + prompt_chars tell you immediately it was a
        # long-generation problem, not auth/key/URL (those fail in <3s).
        self._wlog(_pid_hint, {"event": "ai_call_pre",
                               "ai": f"{_ai_cfg.get('provider')}/{_ai_cfg.get('model')}",
                               "stream": True, "base_url": _ai_cfg.get('base_url', ''),
                               "corpus_chars": len(corpus), "prompt_chars": len(prompt),
                               "kb_block": bool(kb_block)})
        for attempt in range(1, 4):
            try:
                raw = ai._call_provider(prompt if attempt == 1 else prompt + f"\n\nPREVIOUS ATTEMPT FAILED VALIDATION — fix these errors and output ONLY corrected JSON:\n{last_err}")
                data = ai._parse_json_loose(raw)
                profile = self._coerce_profile(data, name, exchange_id or self._slug(name), fetcher)
                errs, warns = P.validate_profile(profile)
                if not errs:
                    profile["_warnings"] = warns
                    self._wlog(_pid_hint, {"event": "profile_extracted", "attempt": attempt,
                                           "ai": f"{_ai_cfg.get('provider')}/{_ai_cfg.get('model')}",
                                           "raw_chars": len(raw or "")})
                    break
                last_err = "; ".join(errs)
                self._wlog(_pid_hint, {"event": "validation_failed", "attempt": attempt,
                                       "errors": last_err[:600]})
                profile = None
            except AIProviderError as exc:
                self._wlog(_pid_hint, {"event": "ai_error", "attempt": attempt,
                                       "ai": f"{_ai_cfg.get('provider')}/{_ai_cfg.get('model')}",
                                       "error": str(exc)[:400]})
                # ── Salvage on truncation (2026-09-09, user bug: «AI output
                # truncated (finish_reason=length)» on qwen3.8-27b). The model
                # hit its own context/output cap MID-JSON, but it already
                # emitted most of the profile. Throwing that away and failing
                # the whole job was wasteful: parse the partial text; if it
                # yields a coherent profile, keep it. (The 03:45 run proved
                # the same model completes in 7,275 chars when the output
                # stays lean — the 03:41 run cut off just short.)
                if "finish_reason=length" in str(exc) or "truncated" in str(exc).lower():
                    partial = getattr(exc, "partial_text", "") or ""
                    try:
                        data = ai._parse_json_loose(partial)
                        cand = self._coerce_profile(data, name,
                                                     exchange_id or self._slug(name), fetcher)
                        e2, w2 = P.validate_profile(cand)
                        if not e2:
                            cand["_warnings"] = w2
                            self._wlog(_pid_hint, {"event": "profile_salvaged_from_truncation",
                                                   "attempt": attempt, "partial_chars": len(partial)})
                            profile = cand
                            break
                        last_err = "; ".join(e2)[:400]
                        self._wlog(_pid_hint, {"event": "salvage_incomplete", "attempt": attempt,
                                               "errors": last_err})
                    except Exception as pe:
                        self._wlog(_pid_hint, {"event": "salvage_parse_error", "attempt": attempt,
                                               "error": f"{type(pe).__name__}: {pe}"})
                    if attempt >= 3:
                        raise
                    continue
                raise
            except Exception as exc:
                last_err = f"parse/validate: {exc}"
                self._wlog(_pid_hint, {"event": "parse_error", "attempt": attempt,
                                       "error": str(exc)[:400]})
        if profile is None:
            self._wlog(_pid_hint, {"event": "research_failed", "reason": "no_valid_profile",
                                   "last_err": last_err[:400]})
            return {"profile_id": None,
                    "error": f"AI نتوانست پروفایل معتبری تولید کند بعد از ۳ تلاش: {last_err[:300]}"}

        pid = profile["id"]
        # Bind the thread's logger to the REAL profile id (the AI may slug it
        # differently than the exchange id) so all subsequent writes + the AI
        # override land in Logs/<pid>/.
        _pid_hint = pid
        self._wlog(pid, {"event": "research_saved", "profile_id": pid,
                         "status": profile.get("status"),
                         "elapsed_sec": round(time.time() - t0, 1)})
        # ── SMART re-research (user req): re-running the wizard for an
        # exchange whose data already exists must OVERWRITE the AI template
        # but PRESERVE everything accumulated on disk: the exchange API key,
        # candle history, strategies, DB, secrets. The previous profile.json
        # is archived (keep 3) for rollback, and any fields the AI left
        # unknown are carried over from the previous working profile.
        previous: Optional[dict] = P.load_profile(pid, PROFILES_ROOT)
        reused: List[str] = []
        if previous:
            P.archive_profile(pid, PROFILES_ROOT, tag="prev")
            # carry over verified knowledge the new AI pass may have missed
            if not profile.get("margin") and previous.get("margin"):
                profile["margin"] = previous["margin"]
                reused.append("margin block")
            if previous.get("true_res_map"):
                # probe-verified granularity beats AI guesses; probe will re-verify
                profile["true_res_map"] = {**(profile.get("true_res_map") or {}),
                                           **previous["true_res_map"]}
                reused.append("true_res_map")
            # ── base_url: the PREVIOUS profile may have been probe-verified
            # (its host actually answered). A fresh AI pass re-guesses the host
            # from docs alone and can regress to a DNS-blocked domain (the
            # Nobitex api.nobitex.ir vs apiv2.nobitex.ir case). Policy: prefer
            # the previous host when the AI's new host only differs in domain
            # subdomain guesses; verify both quickly and keep the one that
            # answers. When neither can be verified here, keep the previous
            # (it was working) and note the change for the probe to re-check.
            new_url = (profile.get("base_url") or "").strip()
            old_url = (previous.get("base_url") or "").strip()
            if old_url and new_url and self._url_host(new_url) != self._url_host(old_url):
                winner = self._prefer_working_host(old_url, new_url)
                if winner == old_url and new_url:
                    reused.append(f"base_url (kept previous working host {old_url} over AI's {new_url})")
                profile["base_url"] = winner
            elif not new_url and old_url:
                profile["base_url"] = old_url
                reused.append("base_url")
            # keep earlier limitations (accumulated knowledge), deduped
            for lim in previous.get("limitations", []):
                P.add_limitation(profile, lim.get("category", "unknown"),
                                 lim.get("detail", ""))
            reused.append("limitations")
            # ── rules: live-calibrated values (from a previous calibrate
            # run) beat the AI's doc guesses — but values the AI EXPLICITLY
            # stated win over inheritance (the docs are the authority when
            # the AI actually read them). Fields the AI left at default
            # inherit the previous profile's verified values.
            prev_rules = previous.get("rules") or {}
            if prev_rules:
                ai_stated = set(profile.pop("_rules_from_ai", []) or [])
                new_rules = dict(profile.get("rules") or {})
                inherited = []
                for k, v in prev_rules.items():
                    if k in ai_stated or not isinstance(v, (int, float)):
                        continue
                    if new_rules.get(k) != v:
                        inherited.append(k)
                    new_rules[k] = v
                profile["rules"] = new_rules
                if inherited:
                    reused.append("rules (inherited live-calibrated: "
                                  + ", ".join(sorted(inherited)) + ")")
            else:
                profile.pop("_rules_from_ai", None)
            if previous.get("last_probe"):
                profile["_note_prev_probe"] = "پروفایل قبلاً probe شده — پس از ذخیره، تست زنده دوباره اجرا شود"
        P.save_profile(profile, PROFILES_ROOT)
        self._register(pid, profile)
        report = self._write_report(pid, profile, fetcher, ai_cfg=self.ai_config_masked(),
                                    elapsed=time.time() - t0)
        if reused:
            report += ("\n\n## Smart re-research\n"
                       f"- previous profile archived as profile.prev-*.json\n"
                       f"- reused from previous profile: {', '.join(reused)}\n"
                       "- preserved on disk (untouched): exchange API key (secrets.enc), "
                       "candle history/, strategies/, bot.db\n")
        fetcher.close()   # FIX(audit-M8): research done — release the pool
        return {"profile_id": pid, "status": profile["status"], "report": report,
                "summary": self.capability_summary(
                    pid, profile, ai_cfg=self.ai_config_masked(),
                    lang=str(custom.get("lang") or "fa")),
                "fetch_log": fetcher.log, "warnings": profile.pop("_warnings", []),
                "limitations": profile.get("limitations", []),
                "reused": reused, "re_research": bool(previous)}

    @staticmethod
    def _slug(name: str, fallback: str = "") -> str:
        s = re.sub(r"[^a-z0-9]+", "-", (name or fallback or "exchange").lower()).strip("-")
        return s or fallback or f"exchange-{int(time.time())}"

    @staticmethod
    def _url_host(url: str) -> str:
        m = re.match(r"^(https?://[^/]+)", (url or "").strip())
        return (m.group(1).lower() if m else (url or "").strip().lower()).rstrip("/")

    def _prefer_working_host(self, old_url: str, new_url: str) -> str:
        """Probe both candidate hosts with a tiny markets-style GET and keep
        the one that actually answers (any HTTP status means the host RESOLVES
        and serves — DNS failure means it does not). Falls back to the previous
        host on ambiguity (it was the working one)."""
        import httpx
        def _resolves(url: str) -> bool:
            for probe_path in ("/market/udf/history", "/v1/models", "/api/v3/ping"):
                try:
                    with httpx.Client(timeout=8.0) as c:
                        r = c.get(url.rstrip("/") + probe_path,
                                  params={"symbol": "BTCUSDT", "resolution": "60",
                                          "from": int(time.time()) - 7200, "to": int(time.time())},
                                  follow_redirects=True)
                        if r.status_code > 0:
                            return True
                except Exception:
                    continue
            return False
        old_ok = _resolves(old_url)
        new_ok = _resolves(new_url)
        if old_ok and not new_ok:
            return old_url
        if new_ok and not old_ok:
            return new_url
        return old_url  # ambiguity → previous (was working)

    @staticmethod
    def _coerce_profile(data: dict, name: str, pid: str, fetcher: DocFetcher) -> dict:
        """Merge AI output over defaults; tolerate missing optional blocks."""
        p = P.default_profile(P.slugify(pid) if hasattr(P, "slugify") else WizardEngine._slug(pid), name)
        for key in ("base_url", "auth", "symbol_format", "endpoints", "margin",
                    "tf_param_map", "true_res_map", "min_gap_sec", "quotes",
                    "margin_style", "capabilities_override"):
            if key in data and data[key] not in (None, "", {}):
                p[key] = data[key]
        # per-exchange order rules: merge AI values over the conservative
        # defaults (paper mode consumes this block; unknown fields dropped).
        # Track WHICH fields the AI actually stated — re-research uses that to
        # inherit live-verified values for the rest (calibrated > AI default).
        if isinstance(data.get("rules"), dict):
            merged = dict(p.get("rules") or {})
            _num_keys = ("min_order_usdt", "min_collateral_usdt", "max_collateral_usdt",
                         "max_risk_coef", "price_band_pct", "qty_step",
                         "fee_pct", "interest_per_4h_pct")
            ai_keys = []
            for k in _num_keys:
                if k not in data["rules"]:
                    continue
                try:
                    merged[k] = float(data["rules"][k])
                    ai_keys.append(k)
                except (TypeError, ValueError):
                    pass
            p["rules"] = merged
            p["_rules_from_ai"] = ai_keys
        # strip unknown endpoint keys (validator warns; adapter ignores)
        known = set(P.ENDPOINT_KEYS)
        p["endpoints"] = {k: v for k, v in (p.get("endpoints") or {}).items() if k in known}
        if p.get("margin") and isinstance(p["margin"], dict):
            meps = p["margin"].get("endpoints") or {}
            p["margin"]["endpoints"] = {k: v for k, v in meps.items()
                                        if k in P.MARGIN_ENDPOINT_KEYS}
        p["id"] = p["id"] or WizardEngine._slug(name)
        p["name"] = name
        p["docs"] = {"source_urls": [pg["url"] for pg in fetcher.pages] or fetcher.log[:5],
                     "researched_at": int(time.time()), "model": "wizard"}
        p["status"] = "draft"
        if data.get("limitations"):
            for lim in data["limitations"]:
                if isinstance(lim, dict) and lim.get("detail"):
                    P.add_limitation(p, str(lim.get("category", "unknown")), str(lim["detail"]))
        return p

    def _register(self, pid: str, profile: dict) -> None:
        from .exchange import registry as reg
        reg.register_profile(pid, name=profile.get("name", pid), set_active=False)

    # ── probe suite ──────────────────────────────────────────────────
    def probe(self, profile_id: str, lang: str = "fa") -> str:
        return self._start_job("probe", self._probe_sync_lang, profile_id, lang)

    def _probe_sync_lang(self, profile_id: str, lang: str = "fa") -> dict:
        out = self._probe_sync(profile_id)
        # regenerate the summary in the requested language
        prof = P.load_profile(profile_id, PROFILES_ROOT)
        if prof:
            out["summary"] = self.capability_summary(
                profile_id, prof, probe=prof.get("last_probe"), lang=lang)
        return out

    def _probe_sync(self, profile_id: str) -> dict:
        _pt0 = time.time()
        self._wlog(profile_id, {"event": "probe_start"})
        prof = P.load_profile(profile_id, PROFILES_ROOT)
        if not prof:
            raise ValueError(f"profile '{profile_id}' not found — run research first")
        results: List[dict] = []
        key = ""
        secret = ""
        try:
            key = self.store.get(f"exchange_key:{profile_id}") or self.store.get("wallex_api_key") or ""
            secret = self.store.get(f"exchange_key_secret:{profile_id}") or self.store.get("wallex_api_secret") or ""
        except Exception:
            pass
        adapter = GenericRESTAdapter(prof, api_key=key, api_secret=secret)

        # 1) markets
        markets: List[dict] = []   # ALWAYS bound — the except branch must not
        try:                       # leave it unassigned for the sample logic below
            markets = adapter.get_markets()
            ok = len(markets) > 0
            results.append({"check": "markets", "ok": ok,
                            "detail": f"{len(markets)} markets; sample: {[m['symbol'] for m in markets[:5]]}"})
        except Exception as exc:
            results.append({"check": "markets", "ok": False, "detail": str(exc)[:250]})

        # 2) candles per TF + spacing-histogram granularity truth
        now = int(time.time())
        true_map = dict(prof.get("true_res_map") or {})
        # prefer a liquid BTC pair for the probe sample; fall back to first market
        syms = [m["symbol"] for m in (markets if isinstance(markets, list) else [])]
        btc = [s for s in syms if s.startswith("BTC") and s.endswith(("USDT", "USD"))]
        sample = btc[0] if btc else (syms[0] if syms else "BTCUSDT")
        for tf in ("15", "60", "240", "1D"):
            try:
                from .models import Candle  # noqa: F401
                window = 12 * 86400 if tf == "1D" else 3 * 86400
                cs = adapter.get_candles(sample, tf, now - window, now)
                if len(cs) < 3:
                    results.append({"check": f"candles_{tf}", "ok": False,
                                    "detail": f"only {len(cs)} bars returned"})
                    continue
                gaps = Counter(cs[i + 1].ts - cs[i].ts for i in range(len(cs) - 1))
                dom = gaps.most_common(1)[0][0] if gaps else 0
                expected = {"15": 900, "60": 3600, "240": 14400, "1D": 86400}[tf]
                if dom and dom < expected // 2:
                    true_gran = {60: "1", 900: "15", 3600: "60", 14400: "240"}.get(dom, str(dom))
                    true_map[tf] = true_gran
                    results.append({"check": f"candles_{tf}", "ok": True,
                                    "detail": f"{len(cs)} bars; RETURNS FINER GRANULARITY ({dom}s) → true_res_map[{tf}]='{true_gran}' (auto-corrected, normalizer aggregates)"})
                else:
                    true_map[tf] = tf
                    results.append({"check": f"candles_{tf}", "ok": dom == expected,
                                    "detail": f"{len(cs)} bars; dominant spacing {dom}s (expected {expected}s)"})
            except ExchangeNotSupported as exc:
                results.append({"check": f"candles_{tf}", "ok": False,
                                "detail": f"NOT IMPLEMENTABLE: {exc}", "limitation": True})
            except Exception as exc:
                results.append({"check": f"candles_{tf}", "ok": False, "detail": str(exc)[:250]})

        # 3) ticker/depth (optional endpoints)
        for key_name, fn in (("ticker", lambda: adapter.get_ticker(sample)),
                             ("depth", lambda: adapter.get_depth(sample))):
            try:
                data = fn()
                results.append({"check": key_name, "ok": bool(data), "detail": str(data)[:120]})
            except ExchangeNotSupported as exc:
                results.append({"check": key_name, "ok": True,
                                "detail": f"optional endpoint absent (OK): {exc}"})
            except Exception as exc:
                results.append({"check": key_name, "ok": False, "detail": str(exc)[:200]})

        critical_ok = all(r["ok"] for r in results if r["check"].startswith(("markets", "candles")))
        prof["true_res_map"] = true_map
        prof["status"] = "probed" if critical_ok else "error"
        probe_rec = {"checked_at": int(time.time()), "results": results}
        prof["last_probe"] = probe_rec
        P.save_profile(prof, PROFILES_ROOT)
        # KB snapshot from live probe: any config the exchange CHANGED since the
        # last probe/calibrate lands in the timestamped history automatically.
        try:
            self._knowledge_append(profile_id, {
                "_source": "probe",
                "name": prof.get("name", profile_id),
                "base_url": (prof.get("api") or {}).get("base_url") or prof.get("base_url", ""),
                "auth_scheme": (prof.get("auth") or {}).get("scheme", "unknown"),
                "last_probe_ok": bool(critical_ok),
                "true_res_map": true_map,
                "probed_at": int(time.time()),
            })
        except Exception:
            pass
        kb_history = (self._knowledge_read().get(profile_id) or {}).get("history") or []
        self._wlog(profile_id, {"event": "probe_done", "critical_ok": critical_ok,
                                "status": prof["status"],
                                "elapsed_sec": round(time.time() - _pt0, 1),
                                "results": [{"check": r["check"], "ok": r["ok"],
                                             "detail": r["detail"][:160]} for r in results]})
        report = self._write_report(profile_id, prof, None, probe=probe_rec)
        try:
            adapter.close()   # FIX(audit-M8)
        except Exception:
            pass
        return {"profile_id": profile_id, "status": prof["status"],
                "critical_ok": critical_ok, "results": results, "report": report,
                "config_changes": kb_history[-10:],
                "summary": self.capability_summary(profile_id, prof, probe=probe_rec,
                                                   lang=str(prof.get("_lang") or "fa")),
                "note": "کلید صرافی را در تنظیمات وارد و دوباره probe بگیرید تا احراز هویت هم آزموده شود"}

    # ── diagnose (AI self-repair) ────────────────────────────────────
    def diagnose(self, profile_id: str) -> str:
        return self._start_job("diagnose", self._diagnose_sync, profile_id)

    def _diagnose_sync(self, profile_id: str) -> dict:
        _dt0 = time.time()
        self._wlog(profile_id, {"event": "diagnose_start"})
        prof = P.load_profile(profile_id, PROFILES_ROOT)
        if not prof:
            raise ValueError(f"profile '{profile_id}' not found")
        probe = prof.get("last_probe") or {}
        failed = [r for r in probe.get("results", []) if not r.get("ok")]
        self._wlog(profile_id, {"event": "diagnose_failed_checks",
                                "count": len(failed),
                                "checks": [r["check"] for r in failed][:20]})
        if not failed:
            self._wlog(profile_id, {"event": "diagnose_noop", "elapsed_sec": round(time.time() - _dt0, 1)})
            return {"profile_id": profile_id, "note": "پروفایل مشکلی ندارد — probe آخر همه چکهای حیاتی را پاس کرده", "changed": False}
        ai = self._ai_client()
        kb_entry = self._knowledge_read().get(profile_id) or {}
        kb_hist = kb_entry.get("history") or []
        prompt = ("You are repairing an exchange API profile for a trading bot. "
                  "The probe failed these checks:\n"
                  + json.dumps(failed, indent=1)[:4000]
                  + "\n\nCURRENT PROFILE:\n" + json.dumps(prof, indent=1)[:8000])
        if kb_hist:
            prompt += ("\n\nCONFIG CHANGE HISTORY (timestamped differences this exchange showed "
                       "since it last worked — treat recent changes as prime suspects):\n"
                       + json.dumps(kb_hist[-10:], indent=1)[:2500])
        prompt += ("\n\nVERIFIED KNOWLEDGE (this exchange's previously-confirmed values):\n"
                   + json.dumps({k: v for k, v in kb_entry.items()
                                 if not k.startswith("_") and k != "history"}, indent=1)[:2500]
                   + "\n\nReturn ONLY a corrected JSON profile in the same schema. "
                     "If a failure is unfixable, keep it and explain in limitations[].")
        raw = ai._call_provider(prompt)
        data = ai._parse_json_loose(raw)
        fixed = self._coerce_profile(data, prof.get("name", profile_id), profile_id,
                                     _FakeFetcher(prof))
        fixed["id"] = profile_id
        fixed["status"] = "draft"
        P.save_profile(fixed, PROFILES_ROOT)
        report = self._write_report(profile_id, fixed, None, diagnosed=True)
        # chain re-probe
        probe_result = self._probe_sync(profile_id)
        self._wlog(profile_id, {"event": "diagnose_done", "changed": True,
                                "reprobe_status": probe_result.get("status"),
                                "elapsed_sec": round(time.time() - _dt0, 1)})
        return {"profile_id": profile_id, "changed": True, "probe": probe_result,
                "report": report}

    # ── live calibration (self-improvement, user req) ────────────────
    # With a live API key the bot can DISCOVER the exchange's real paper-mode
    # requirements from its own API (margin leverage/fees, min order size via
    # safe far-from-market limit probes, price-band width, error codes) and
    # write them into profile.json `rules`. Every discovery is also appended
    # to a shared knowledge file that warm-starts the AI research of the NEXT
    # exchange — the more exchanges connect, the better the AI configures them.
    _RULE_FIELDS = ("min_order_usdt", "min_collateral_usdt", "max_collateral_usdt",
                    "max_risk_coef", "price_band_pct", "qty_step",
                    "fee_pct", "interest_per_4h_pct")

    @staticmethod
    def _knowledge_path() -> Path:
        return PROFILES_ROOT / "exchange_knowledge.json"

    def _knowledge_read(self) -> dict:
        p = self._knowledge_path()
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
            return d if isinstance(d, dict) else {}
        except Exception:
            return {}

    def _knowledge_append(self, pid: str, entry: dict) -> None:
        """Merge one exchange's verified findings into the shared knowledge
        base (idempotent per field — latest verified value wins).

        Every append that CHANGES a value records a timestamped history row
        {ts, source, changed:{field:{old,new}}} (last 20 kept) so a later
        probe/diagnose can tell WHICH configs the exchange changed and when
        (troubleshooting: 'worked at calibrate time, broken now')."""
        d = self._knowledge_read()
        prev = d.get(pid) or {}
        changed: Dict[str, dict] = {}
        for k, v in entry.items():
            if v is None or k.startswith("_") or k in ("activated_at", "calibrated_at"):
                continue
            old_v = prev.get(k)
            if old_v is not None and old_v != v:
                changed[k] = {"old": old_v, "new": v}
        merged = {**prev, **{k: v for k, v in entry.items() if v is not None}}
        if changed:
            hist = list(prev.get("history") or [])
            hist.append({"ts": int(time.time()), "source": str(entry.get("_source") or "setup"),
                         "changed": changed})
            merged["history"] = hist[-20:]
        d[pid] = merged
        d["_updated"] = int(time.time())
        self._knowledge_path().write_text(json.dumps(d, indent=2, ensure_ascii=False),
                                          encoding="utf-8")

    def _knowledge_block(self, exclude_pid: str = "") -> str:
        """Verified-knowledge digest for the AI research prompt of a NEW
        exchange (everything learned from previously connected exchanges)."""
        d = self._knowledge_read()
        lines: List[str] = []
        for pid, info in d.items():
            if pid.startswith("_") or pid == exclude_pid:
                continue
            if not isinstance(info, dict) or not info:
                continue
            bits = []
            for k, v in info.items():
                if k.startswith("_") or k in ("name", "base_url"):
                    continue
                bits.append(f"{k}={v}")
            if bits:
                lines.append(f"- {info.get('name', pid)} (base_url={info.get('base_url', '?')}): "
                             + ", ".join(bits[:10]))
        if not lines:
            return ""
        return ("\nVERIFIED KNOWLEDGE BASE (what previously-connected exchanges revealed — "
                "use these as prior expectations for common exchange conventions; still verify "
                "against THIS exchange's docs/API, and record any difference in limitations):\n"
                + "\n".join(lines[:12]))

    def _cred_pair(self, profile_id: str) -> tuple:
        key, secret = "", ""
        try:
            key = self.store.get(f"exchange_key:{profile_id}") or self.store.get("wallex_api_key") or ""
            secret = (self.store.get(f"exchange_key_secret:{profile_id}")
                      or self.store.get("wallex_api_secret") or "")
        except Exception:
            pass
        return key, secret

    def calibrate(self, profile_id: str) -> str:
        return self._start_job("calibrate", self._calibrate_sync, profile_id)

    def _calibrate_sync(self, profile_id: str) -> dict:
        _ct0 = time.time()
        self._wlog(profile_id, {"event": "calibrate_start"})
        prof = P.load_profile(profile_id, PROFILES_ROOT)
        if not prof:
            raise ValueError(f"profile '{profile_id}' not found")
        key, secret = self._cred_pair(profile_id)
        if not key:
            raise ValueError("کلید API صرافی تنظیم نشده — ابتدا در تب تنظیمات وارد کنید")
        # public data needs no auth; private needs the profile's auth scheme
        adapter = GenericRESTAdapter(prof, api_key=key, api_secret=secret)
        rules = dict(prof.get("rules") or {})
        findings: Dict[str, Any] = {}
        notes: List[str] = []
        base = prof.get("base_url", "")

        # ── 1) live price + a liquid USDT pair for safe order probes ───
        # The min-order probe measures NOTIONAL in the pair's quote — the
        # rules are in USDT, so we MUST use a USDT-quoted pair (BTCUSDT/
        # ETHUSDT). A rial pair would measure in rials (177M px) and the
        # result is meaningless as "USDT".
        sample, px, px_rls = "BTCUSDT", 0.0, 0.0
        try:
            for m in adapter.get_markets():
                sym = str(m.get("symbol", ""))
                if sym == "BTCUSDT" and px <= 0:
                    px = float(m.get("price") or 0)
                elif sym == "ETHUSDT" and px <= 0:
                    px = float(m.get("price") or 0)
                    sample = "ETHUSDT"
                elif sym == "BTCRLS":
                    px_rls = float(m.get("price") or 0)
            if px <= 0:
                sample = "BTCUSDT"
                px = float(adapter.get_ticker(sample).get("price") or 0)
        except Exception as exc:
            notes.append(f"markets/ticker: {str(exc)[:120]}")
        probe_usd_pair = (px > 0)   # True only when px came from a USDT pair

        # ── 2) margin market params (public on most exchanges; some
        # require READ auth — retry signed when the unsigned call fails) ──
        max_lev, pos_fee = None, None
        try:
            ep = (prof.get("margin") or {}).get("endpoints", {}).get("margin_markets") \
                or {"path": "/margin/markets/list", "method": "GET"}
            try:
                raw = adapter._request(ep)
            except Exception:
                raw = adapter._request(ep, auth=True)
            mkts = raw.get("markets") if isinstance(raw, dict) else raw
            if isinstance(mkts, dict) and mkts:
                levs, fees = [], []
                for sym, m in mkts.items():
                    if not isinstance(m, dict):
                        continue
                    for f in ("maxLeverage", "max_leverage", "max_risk_coef"):
                        try:
                            levs.append(float(m.get(f)))
                            break
                        except (TypeError, ValueError):
                            continue
                    for f in ("positionFeeRate", "position_fee_rate"):
                        try:
                            fees.append(float(m.get(f)))
                            break
                        except (TypeError, ValueError):
                            continue
                if levs:
                    max_lev = max(levs)
                if fees:
                    pos_fee = sum(fees) / len(fees)
                notes.append(f"margin markets: {len(mkts)} markets, "
                             f"maxLeverage={max_lev}, avg positionFeeRate={pos_fee}")
        except ExchangeNotSupported:
            notes.append("margin markets: endpoint not configured")
        except Exception as exc:
            notes.append(f"margin markets: {str(exc)[:120]}")
        if max_lev:
            findings["max_risk_coef"] = float(max_lev)
        if pos_fee:
            # assume DAILY position fee (Nobitex convention) → per-4h
            findings["interest_per_4h_pct"] = round(float(pos_fee) * 100 / 6.0, 6)

        # ── 3) min order size: safe limit probes on a USDT pair ────────
        # A resting limit buy at ~0.1% of market price cannot fill; the
        # exchange answers SmallOrder (below min) vs a placement/funds error
        # (above min) WITHOUT spending anything. We cancel any order that
        # actually rests. ONLY run when the probe pair is USDT-quoted — the
        # size check measures notional in the quote, so a rial pair would
        # corrupt the "USDT" result.
        ep_place = (prof.get("endpoints") or {}).get("place_order")
        ep_cancel = (prof.get("endpoints") or {}).get("cancel_order")
        if probe_usd_pair and px > 0 and ep_place and ep_cancel:
            try:
                # The exchange's size check tests the ORDER NOTIONAL
                # (qty × limit_price). So set qty = mid / price_probe and the
                # checked notional == mid exactly. price_probe is far below
                # market (0.1%) so a resting order can never fill; any order
                # that actually rests is canceled immediately. Boundary codes:
                # SmallOrder / AmountTooLow = below min (raise floor);
                # OverValueOrder (funds) or a resting order = size accepted.
                lo, hi = 0.1, 500.0
                placed: List[str] = []
                for _ in range(10):
                    if hi - lo < 0.1:
                        break
                    mid = round((lo + hi) / 2, 3)
                    price_probe = px * 0.001
                    amt = f"{(mid / price_probe):.10f}"
                    cid = f"CALB{int(time.time() * 1000) % 10**10}"
                    res, msg = None, ""
                    try:
                        res = adapter.place_order(sample, "BUY", "LIMIT", quantity=amt,
                                                  price=f"{price_probe:.6f}", client_id=cid)
                    except Exception as exc:
                        msg = str(exc)
                    placed_ok = (isinstance(res, dict)
                                 and (res.get("status") == "ok"
                                      or (res.get("clientOrderId") and not res.get("code")
                                          and res.get("status") not in ("failed",))))
                    low = ""
                    if isinstance(res, dict):
                        low = f"{res.get('code', '')} {res.get('message', '')}".lower()
                    low = f"{low} {msg}".lower()
                    if placed_ok:
                        # order RESTED → size accepted → min ≤ mid; cancel it
                        placed.append(str(res.get("clientOrderId") or cid))
                        hi = mid
                    elif any(t in low for t in ("smallorder", "small order", "amounttoolow",
                                                "amount too low", "حداقل", "min_order",
                                                "minimum order")):
                        lo = mid            # below minimum → min > mid
                    else:
                        hi = mid            # size accepted, rejected later (funds/band)
                for c in placed:            # ALWAYS clean up resting orders
                    try:
                        adapter.cancel_order(c)
                    except Exception as exc:
                        notes.append(f"cleanup cancel {c} failed: {str(exc)[:100]}")
                # trust the estimate only if the boundary was found (hi moved
                # below the cap); otherwise the min is > 500 USDT — report it
                if hi < 500.0:
                    min_usdt = round(hi, 2)
                    findings["min_order_usdt"] = min_usdt
                    findings["min_collateral_usdt"] = min_usdt
                    # the floor is usually PER-MARKET (min-quantity × market
                    # price); record the probe pair's exact value as an
                    # override, keep the probed value as the global floor
                    ov = dict(rules.get("market_overrides") or {})
                    ov[sample] = {"min_order_usdt": min_usdt}
                    findings["market_overrides"] = ov
                    notes.append(f"min order notional ~{min_usdt} USDT on {sample} (safe "
                                 f"limit-probe binary search @ {px:.2f}; {len(placed)} probe "
                                 f"order(s) canceled) — floor is per-market (min-qty × price)")
            except Exception as exc:
                notes.append(f"min-order probe: {str(exc)[:150]}")

        # ── 4) write verified findings into profile rules (keep AI/manual
        # values for fields the live API didn't confirm) ─────────────────
        changed = []
        for k, v in findings.items():
            if rules.get(k) != v:
                changed.append(f"{k}: {rules.get(k)} → {v}")
            rules[k] = v
        prof["rules"] = rules
        prof["status"] = "probed" if prof.get("status") in ("draft", "error") else prof.get("status")
        P.save_profile(prof, PROFILES_ROOT)

        # ── 5) shared knowledge base (feeds the next exchange's AI) ─────
        # includes live-verified endpoint/shape knowledge beyond the rules:
        ep = (prof.get("api") or prof.get("endpoints") or {})
        ep_bits = {}
        try:
            if isinstance(ep, dict):
                for key in ("candles", "markets", "depth", "ticker"):
                    row = ep.get(key)
                    if isinstance(row, dict) and row.get("path"):
                        ep_bits[f"endpoint_{key}"] = row.get("path")
        except Exception:
            ep_bits = {}
        self._knowledge_append(profile_id, {
            "_source": "calibrate",
            "name": prof.get("name", profile_id),
            "base_url": base,
            **{k: findings.get(k) for k in self._RULE_FIELDS if findings.get(k) is not None},
            "auth_scheme": (prof.get("auth") or {}).get("scheme", "unknown"),
            **ep_bits,
            "calibrated_at": int(time.time()),
        })
        self._wlog(profile_id, {"event": "calibrate_done", "changed": changed,
                                "findings": findings,
                                "elapsed_sec": round(time.time() - _ct0, 1)})
        try:
            adapter.close()   # FIX(audit-M8)
        except Exception:
            pass
        return {
            "profile_id": profile_id, "changed": changed, "findings": findings,
            "rules": prof.get("rules"), "notes": notes,
            "summary": self._calibrate_summary(prof, findings, changed),
        }

    def _calibrate_summary(self, prof: dict, findings: dict, changed: List[str],
                           lang: str = "fa") -> str:
        en = str(lang).lower() == "en"
        L = (lambda fa, en_: en_) if en else (lambda fa, en_: fa)
        head = L(" Paper mode auto-configured from the exchange's live API",
                 "🔧 Paper mode auto-configured from the exchange's live API")
        if not findings:
            return head + " — " + L("no live API data was reachable (check the key/permissions)",
                                    "no live API data was reachable (check the key/permissions)")
        lines = [f"  • {L('حداقل سفارش', 'Min order')}: {findings.get('min_order_usdt', '—')} USDT"
                 if findings.get("min_order_usdt") else ""]
        lines.append(f"  • {L('حداکثر اهرم', 'Max leverage')}: {findings.get('max_risk_coef', '—')}×"
                     if findings.get("max_risk_coef") else "")
        lines.append(f"  • {L('کارمزد تمدید مارجین (۴ساعته)', 'Margin renewal (per 4h)')}: "
                     f"{findings.get('interest_per_4h_pct', '—')}%"
                     if findings.get("interest_per_4h_pct") else "")
        body = "\n".join(x for x in lines if x)
        foot = (L("این مقادیر در پروفایل ذخیره شد و برای تنظیم صرافی‌های بعدی به هوش مصنوعی داده میشود.",
                  "These values were saved to the profile and will warm-start the AI for the next exchange.")
                )
        return f"{head}\n{body}\n{foot}"

    # ── activate ─────────────────────────────────────────────────────
    def activate(self, profile_id: str) -> dict:
        prof = P.load_profile(profile_id, PROFILES_ROOT)
        if not prof:
            raise ValueError(f"profile '{profile_id}' not found")
        if prof.get("status") not in ("probed", "ready"):
            raise ValueError(f"profile status is '{prof.get('status')}' — probe must pass before activation")
        prof["status"] = "ready"
        P.save_profile(prof, PROFILES_ROOT)
        from .exchange import registry as reg
        reg.register_profile(profile_id, name=prof.get("name", profile_id), set_active=True)
        # every successful setup feeds the shared knowledge library so future
        # exchange setups start from verified conventions (user requirement:
        # "every successful setup should update the knowledge library")
        try:
            self._knowledge_append(profile_id, {
                "_source": "activate",
                "name": prof.get("name", profile_id),
                "base_url": (prof.get("api") or {}).get("base_url") or prof.get("base_url", ""),
                "auth_scheme": (prof.get("auth") or {}).get("scheme", "unknown"),
                "status": "activated",
                "activated_at": int(time.time()),
            })
        except Exception:
            pass
        return {"ok": True, "active_profile": profile_id,
                "note": f"پروفایل فعال شد. بک‌اند را با --profile {profile_id} اجرا کنید"}

    # ── AI auto-pair ─────────────────────────────────────────────────
    def curate_pairs(self, symbols: List[str], quote: str = "") -> str:
        return self._start_job("curate", self._curate_sync, symbols, quote)

    def _curate_sync(self, symbols: List[str], quote: str) -> dict:
        fams: Dict[str, List[str]] = {}
        for s in symbols:
            for q in sorted([x for x in ("USDT", "USDC", "BTC", "ETH", "TMN", "IRT", "USD")],
                            key=len, reverse=True):
                if s.upper().endswith(q) and len(s) > len(q):
                    fams.setdefault(q, []).append(s.upper())
                    break
        listing = "\n".join(f"{q}: {', '.join(v[:60])}{' …' if len(v) > 60 else ''}"
                            for q, v in sorted(fams.items()))
        try:
            ai = self._ai_client()
            prompt = (
                "You curate the trading watchlist for a crypto trading bot. "
                "Below are ALL available pairs on this exchange, grouped by quote currency.\n"
                f"{listing}\n\n"
                "Select the pairs worth auto-trading: high-liquidity majors and their "
                "stablecoin markets, across EACH quote family present. Include 8-25 per major "
                "quote family (more for USDT/USD families, fewer for BTC/ETH cross pairs). "
                'Return ONLY JSON: {"symbols": ["BTCUSDT", ...], "reasoning": "one short paragraph"}'
            )
            raw = ai._call_provider(prompt)
            data = ai._parse_json_loose(raw)
            picked = [str(s).upper() for s in (data.get("symbols") or [])]
            valid = [s for s in picked if s in {x.upper() for x in symbols}]
            if not valid:
                raise ValueError("AI returned no valid symbols")
            return {"symbols": valid, "per_quote_count": len({quote_of(s) for s in valid}),
                    "reasoning": str(data.get("reasoning", ""))[:400], "source": "ai",
                    "valid_total": len(symbols)}
        except Exception as exc:
            # fallback: rule-based majors (user requirement: solid data when AI absent)
            majors = {"BTC", "ETH", "SOL", "BNB", "XRP", "DOGE", "ADA", "LINK", "AVAX", "TRX"}
            valid = [s for s in symbols if any(s.upper().startswith(m) for m in majors)]
            return {"symbols": valid, "reasoning": f"AI unavailable ({str(exc)[:120]}) — rule-based majors used",
                    "source": "rule_fallback", "valid_total": len(symbols)}

    # ── end-user summary (wizard view) ───────────────────────────────
    # The FULL technical report (fetch log, limitations) stays on disk in
    # setup_report.md; the wizard shows only this capability card.
    def capability_summary(self, pid: str, prof: dict,
                           fetcher=None, probe: Optional[dict] = None,
                           ai_cfg: Optional[dict] = None,
                           lang: str = "fa") -> str:
        cap = self._caps_of(prof)
        en = str(lang).lower() == "en"
        L = (lambda fa, en_: en_) if en else (lambda fa, en_: fa)
        lines = [f"## {prof.get('name', pid)} — {L('قابلیتها و وضعیت اتصال', 'Capabilities & connection')}"]
        url = prof.get("base_url", "")
        lines.append(f"- {L('آدرس API', 'API base')}: {url}")
        auth = (prof.get("auth") or {}).get("scheme", "none")
        auth_txt = {"none": L("عمومی (بدون کلید)", "public (no key)"),
                    "header": L("هدر با کلید API", "API-key header"),
                    "hmac": L("امضای HMAC", "HMAC-signed")}.get(auth, auth)
        lines.append(f"- {L('احراز هویت', 'Auth')}: {auth_txt}")
        fams = ", ".join(prof.get("quotes") or [])
        lines.append(f"- {L('خانوادههای ارز', 'Quote families')}: {fams}")
        if cap.get("margin"):
            lines.append(f"- {L('مارجین', 'Margin')}: {L('پشتیبانی میشود', 'supported')} ({cap.get('margin_style')})")
        else:
            lines.append(f"- {L('مارجین', 'Margin')}: {L('ندارد — فقط اسپات (پیپر مارجین غیرفعال)', 'not available — spot only (paper margin disabled)')}")
        lines.append(f"- {L('تایمفریمها', 'Timeframes')}: {'/'.join((prof.get('tf_param_map') or {}).keys())}")
        g = prof.get("min_gap_sec")
        if g:
            lines.append(f"- {L('فاصله ایمن درخواستها', 'Request pacing')}: ~{g}s")
        if probe:
            okc = sum(1 for r in probe.get("results", []) if r.get("ok"))
            tot = len(probe.get("results", []))
            lines.append(f"- {L('تست زنده', 'Live test')}: {okc}/{tot} " + L('موفق', 'passed'))
        n_lim = len(prof.get("limitations") or [])
        if n_lim:
            lines.append(f"- {L('موارد نیازمند توجه', 'Items needing attention')}: {n_lim} — " +
                         L('گزارش فنی کامل در دیسک ذخیره شد', 'full technical report saved on disk'))
        lines.append("")
        lines.append(L('گزارش فنی کامل (لاگ مستندات و محدودیتها) در فایل setup_report.md کنار پروفایل ذخیره شد.',
                       'Full technical report (doc fetch log & limitations) saved as setup_report.md next to the profile.'))
        return "\n".join(lines)

    def _caps_of(self, prof: dict) -> dict:
        ov = prof.get("capabilities_override") or {}
        m = prof.get("margin")
        return {"margin": bool(ov.get("margin", bool(m))),
                "margin_style": ov.get("margin_style", (m or {}).get("style", ""))}

    # ── setup report (persistent, for later AI troubleshooting) ──────
    def _write_report(self, pid: str, prof: dict, fetcher=None,
                      probe: Optional[dict] = None, diagnosed: bool = False,
                      ai_cfg: Optional[dict] = None, elapsed: float = 0.0) -> str:
        d = PROFILES_ROOT / pid
        d.mkdir(parents=True, exist_ok=True)
        lines = [
            f"# Setup report — {prof.get('name', pid)} ({pid})",
            f"- generated: {time.strftime('%Y-%m-%d %H:%M:%S')}",
            f"- status: {prof.get('status')}",
            f"- base_url: {prof.get('base_url')}",
            f"- auth scheme: {(prof.get('auth') or {}).get('scheme')}",
            f"- quotes: {', '.join(prof.get('quotes') or [])}",
            f"- margin: {json.dumps(prof.get('margin'))[:200]}",
        ]
        if ai_cfg:
            lines.append(f"- AI: {ai_cfg.get('provider')}/{ai_cfg.get('model')} @ {ai_cfg.get('base_url')}")
        if elapsed:
            lines.append(f"- research elapsed: {elapsed:.1f}s")
        if fetcher is not None:
            lines += ["", "## Documentation fetch log", *[f"- {x}" for x in fetcher.log[:20]],
                      *[f"- page: {p['url']} ({p.get('chars', 0)} chars)" for p in fetcher.pages]]
            lines.append(f"- docs source_urls: {prof.get('docs', {}).get('source_urls', [])}")
        if probe:
            lines += ["", "## Probe results"]
            for r in probe.get("results", []):
                mark = "✅" if r.get("ok") else ("⚠️" if r.get("limitation") else "❌")
                lines.append(f"- {mark} {r['check']}: {r['detail']}")
        if diagnosed:
            lines += ["", "## Diagnose", "- AI regenerated the profile from the failed probe results and re-probed."]
        lims = prof.get("limitations") or []
        if lims:
            lines += ["", "## Limitations (not implementable in template format)"]
            lines += [f"- [{l.get('category')}] {l.get('detail')}" for l in lims]
        warnings = prof.pop("_warnings", None)
        if warnings:
            lines += ["", "## Validator warnings", *[f"- {w}" for w in warnings]]
        report = "\n".join(lines)
        try:
            (d / "setup_report.md").write_text(report, encoding="utf-8")
        except Exception as exc:
            log.warning("setup report write failed: %s", exc)
        self._wlog(pid, {"event": "setup_report_written", "diagnosed": bool(diagnosed),
                         "chars": len(report), "limits": len(lims),
                         "report_file": str(d / "setup_report.md")})
        return report

    def read_report(self, pid: str) -> str:
        p = PROFILES_ROOT / pid / "setup_report.md"
        if not p.exists():
            return ""
        try:
            return p.read_text(encoding="utf-8")
        except Exception:
            return ""

    # ── startup-selection gate (user req 2026-09-07) ─────────────────
    def first_run(self) -> bool:
        """Show the wizard ONCE PER BACKEND BOOT (user requirement: at start
        the user chooses the default Wallex or another exchange). After the
        user finishes/skips the wizard in this boot's tab, a page reload must
        NOT re-open it (user bug: «پایان» → reload → wizard again). A backend
        restart resets the marker, so the choice reappears next launch.
        Suppressed permanently when the user disabled the startup gate in
        Settings (kv: wizard_startup_gate = '0')."""
        try:
            if self.storage and (self.storage.kv_get("wizard_startup_gate") or "1") == "0":
                return False
        except Exception:
            pass
        self._boot_offer_key = getattr(self, "_boot_offer_key", "") or \
            f"wizard_offered:{int(time.time())}"
        # marker set at boot time: after 'mark_completed'/'skip' within the
        # same process lifetime, first_run() goes False until restart.
        return not getattr(self, "_wizard_shown_this_boot", False)

    def mark_completed(self) -> None:
        if self.storage:
            self.storage.kv_set("wizard_completed", time.strftime("%Y-%m-%d %H:%M:%S"))
        # suppress the startup gate for the rest of this backend boot (user:
        # «پایان»/skip must not re-open the wizard on reload)
        self._wizard_shown_this_boot = True


class _FakeFetcher:
    """Diagnose reuses _coerce_profile's docs bookkeeping without refetching."""
    pages: List[dict] = []
    log: List[str] = []


def quote_of(symbol: str) -> str:
    s = symbol.upper()
    for q in sorted(("USDT", "USDC", "BTC", "ETH", "TMN", "IRT", "USD"), key=len, reverse=True):
        if s.endswith(q) and len(s) > len(q):
            return q
    return ""
