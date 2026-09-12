"""Local mirror of Wallex exchange constraints for near-real paper simulation.

The paper brokers (spot + margin) validate every order against these rules so
that a paper trade is only opened when Wallex would accept the equivalent live
order. Values mirror the Wallex Margin-trade API v1.0 contract already used in
`wallex_client.py` (collateral + risk_coef model, ±5% open-price band, loan
calculation, 21-day max age, 4-hourly interest).

Anything not published is kept conservative and overridable from config.yaml
(`margin:` / `paper:` sections) so the user can tune it to Wallex's current
limits without code changes.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, Optional

# ── defaults (conservative approximations of Wallex limits) ─────────
DEFAULT_MIN_ORDER_USDT = 10.0      # Wallex spot: min order value ~10 USDT
DEFAULT_MIN_COLLATERAL_USDT = 10.0  # margin: min collateral per position
DEFAULT_MAX_COLLATERAL_USDT = 100_000.0
DEFAULT_MAX_RISK_COEF = 3.0        # Wallex margin leverage cap
DEFAULT_PRICE_BAND_PCT = 5.0       # open_price must be within ±5% of market
DEFAULT_QTY_STEP = 8               # decimal precision for quantities
DEFAULT_INTEREST_4H_PCT = 0.05     # Wallex margin: 0.05% interest per 4h on loan


@dataclass
class WallexRules:
    """Exchange-side constraints applied to paper orders (local simulation).

    `exchange_name` keeps error messages exchange-accurate — Wallex profiles
    pass "والکس" (the default, zero regression); other profiles pass their own
    name via `from_profile`.
    """
    min_order_usdt: float = DEFAULT_MIN_ORDER_USDT
    min_collateral_usdt: float = DEFAULT_MIN_COLLATERAL_USDT
    max_collateral_usdt: float = DEFAULT_MAX_COLLATERAL_USDT
    max_risk_coef: float = DEFAULT_MAX_RISK_COEF
    price_band_pct: float = DEFAULT_PRICE_BAND_PCT
    qty_step: int = DEFAULT_QTY_STEP
    interest_per_4h_pct: float = DEFAULT_INTEREST_4H_PCT
    exchange_name: str = "والکس"
    # per-market overrides, e.g. {"BTCUSDT": {"min_order_usdt": 20}}
    market_overrides: Dict[str, dict] = field(default_factory=dict)

    @classmethod
    def from_config(cls, cfg: dict) -> "WallexRules":
        mcfg = cfg.get("margin", {}) or {}
        pcfg = cfg.get("paper", {}) or {}
        return cls(
            min_order_usdt=float(pcfg.get("min_order_usdt", DEFAULT_MIN_ORDER_USDT)),
            min_collateral_usdt=float(mcfg.get("min_collateral_usdt", DEFAULT_MIN_COLLATERAL_USDT)),
            max_collateral_usdt=float(mcfg.get("max_collateral_usdt", DEFAULT_MAX_COLLATERAL_USDT)),
            max_risk_coef=float(mcfg.get("max_risk_coef", DEFAULT_MAX_RISK_COEF)),
            price_band_pct=float(mcfg.get("price_band_pct", DEFAULT_PRICE_BAND_PCT)),
            qty_step=int(pcfg.get("qty_step", DEFAULT_QTY_STEP)),
            interest_per_4h_pct=float(mcfg.get("interest_per_4h_pct", DEFAULT_INTEREST_4H_PCT)),
            market_overrides=dict(pcfg.get("market_overrides", {}) or {}),
        )

    @classmethod
    def from_profile(cls, profile: dict, cfg: Optional[dict] = None) -> "WallexRules":
        """Paper rules for the ACTIVE exchange (user req: paper must mirror the
        exchange's own limits, not Wallex's).

        Precedence: profile `rules` (per-exchange, AI-verified) > global config
        (Wallex-era defaults) > module defaults. A profile that ships no `rules`
        block (hand-coded adapters like Wallex, or un-researched profiles)
        falls back to the global config — identical to the pre-existing behavior.
        """
        cfg = cfg or {}
        mcfg = cfg.get("margin", {}) or {}
        pcfg = cfg.get("paper", {}) or {}
        p = profile or {}
        prules = p.get("rules") or {}
        name = str(p.get("name") or "").strip() or "والکس"
        # per-market overrides: profile (calibrated) > global config
        mo = prules.get("market_overrides") or pcfg.get("market_overrides") or {}
        return cls(
            min_order_usdt=float(prules.get("min_order_usdt", pcfg.get("min_order_usdt", DEFAULT_MIN_ORDER_USDT))),
            min_collateral_usdt=float(prules.get("min_collateral_usdt", mcfg.get("min_collateral_usdt", DEFAULT_MIN_COLLATERAL_USDT))),
            max_collateral_usdt=float(prules.get("max_collateral_usdt", mcfg.get("max_collateral_usdt", DEFAULT_MAX_COLLATERAL_USDT))),
            max_risk_coef=float(prules.get("max_risk_coef", mcfg.get("max_risk_coef", DEFAULT_MAX_RISK_COEF))),
            price_band_pct=float(prules.get("price_band_pct", mcfg.get("price_band_pct", DEFAULT_PRICE_BAND_PCT))),
            qty_step=int(prules.get("qty_step", pcfg.get("qty_step", DEFAULT_QTY_STEP))),
            interest_per_4h_pct=float(prules.get("interest_per_4h_pct", mcfg.get("interest_per_4h_pct", DEFAULT_INTEREST_4H_PCT))),
            exchange_name=name,
            market_overrides=dict(mo),
        )

    # ── per-market accessors ───────────────────────────────────────
    def _m(self, market: str, key: str, default):
        ov = self.market_overrides.get(market, {}) or {}
        return ov.get(key, default)

    def min_order(self, market: str) -> float:
        return float(self._m(market, "min_order_usdt", self.min_order_usdt))

    def min_collateral(self, market: str) -> float:
        return float(self._m(market, "min_collateral_usdt", self.min_collateral_usdt))

    def max_collateral(self, market: str) -> float:
        return float(self._m(market, "max_collateral_usdt", self.max_collateral_usdt))

    # ── validations (return Persian error string, or None if OK) ───
    def check_spot_order(self, market: str, qty: float, price: float = 0,
                         notional: Optional[float] = None) -> Optional[str]:
        if qty <= 0 or (notional is None and price <= 0):
            return "حجم یا قیمت نامعتبر است"
        notional_usdt = float(notional) if notional is not None else (qty * float(price or 0))
        if notional_usdt < self.min_order(market):
            return f"ارزش سفارش ({notional_usdt:.2f} USDT) کمتر از حداقل {self.exchange_name} ({self.min_order(market):.2f} USDT) است"
        return None

    def check_price_band(self, market: str, open_price: float, market_price: float) -> Optional[str]:
        """Exchange may reject open_price outside ±price_band_pct of the live
        price (Wallex: ±5% verified). `price_band_pct <= 0` disables the check
        for exchanges whose band is wide/unverified (e.g. Nobitex accepted a
        limit order ~98% off-market — its band is effectively open)."""
        if self.price_band_pct <= 0:
            return None
        if market_price <= 0 or open_price <= 0:
            return None  # nothing to compare against
        band = self.price_band_pct / 100.0
        if abs(open_price - market_price) / market_price > band:
            return f"قیمت باز کردن خارج از بازه مجاز ±{self.price_band_pct:.0f}٪ قیمت بازار است"
        return None

    def clamp_risk_coef(self, risk_coef: float) -> float:
        return max(1.0, min(float(risk_coef or 1.0), self.max_risk_coef))

    def check_margin_order(self, market: str, collateral: float, risk_coef: float,
                           open_price: float, market_price: float,
                           notional_usdt: Optional[float] = None) -> Optional[str]:
        if collateral <= 0:
            return "وثیقه نامعتبر است"
        rc = self.clamp_risk_coef(risk_coef)
        if rc != float(risk_coef or 1.0):
            return f"اهرم {risk_coef} خارج از بازه مجاز {self.exchange_name} (۱ تا {self.max_risk_coef:g}) است"
        # if caller supplied a USDT notional, use it for collateral checks;
        # otherwise fall back to raw collateral (legacy single-currency path)
        col_usdt = float(notional_usdt) / rc if notional_usdt else float(collateral)
        if col_usdt < self.min_collateral(market):
            return f"وثیقه ({col_usdt:.2f} USDT) کمتر از حداقل {self.exchange_name} ({self.min_collateral(market):.2f} USDT) است"
        if col_usdt > self.max_collateral(market):
            return f"وثیقه ({col_usdt:.2f} USDT) بیشتر از سقف {self.exchange_name} ({self.max_collateral(market):.2f} USDT) است"
        return self.check_price_band(market, open_price, market_price)

    # ── calculations (mirror Wallex loan/liquidation math) ─────────
    @staticmethod
    def loan_calc(collateral: float, risk_coef: float) -> dict:
        """notional = collateral * risk_coef ; loan = notional - collateral."""
        rc = max(1.0, float(risk_coef or 1.0))
        notional = collateral * rc
        return {
            "collateral": collateral,
            "risk_coef": rc,
            "notional": notional,
            "loan": max(notional - collateral, 0.0),
        }

    @staticmethod
    def liquidation_price(entry: float, side: str, risk_coef: float, mmr: float) -> float:
        rc = max(1.0, float(risk_coef or 1.0))
        if side == "short":
            return entry * (1 + (1 - mmr) / rc)
        return entry * (1 - (1 - mmr) / rc)

    def round_qty(self, qty: float) -> float:
        return round(qty, self.qty_step)
