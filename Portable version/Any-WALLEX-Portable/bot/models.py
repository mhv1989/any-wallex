"""Data models — pure dataclasses, no I/O."""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import List, Optional


class Trend(str, Enum):
    UP = "up"
    DOWN = "down"
    RANGE = "range"


class LevelKind(str, Enum):
    SR = "sr"
    OB = "ob"
    FVG = "fvg"


class PosState(str, Enum):
    OPEN = "open"
    CLOSED = "closed"


class ExitReason(str, Enum):
    STOP = "stop"
    TRAIL = "trailing_stop"
    CHOCH = "bearish_choch"
    LEVEL_CONFIRM = "bearish_confirm_at_level"
    MANUAL = "manual"
    PARTIAL = "partial_take_profit"
    END_OF_DATA = "end_of_data"
    LIQUIDATION = "liquidation"
    MAX_AGE = "max_age_expired"
    SHORT_STOP = "short_stop"
    SHORT_TRAIL = "short_trailing_stop"
    SHORT_TARGET = "short_take_profit"


class EntryReason(str, Enum):
    SIGNAL = "signal_6_of_8"


@dataclass
class Candle:
    ts: int          # unix seconds — open time
    o: float
    h: float
    l: float
    c: float
    v: float

    @property
    def body(self) -> float:
        return abs(self.c - self.o)

    @property
    def range(self) -> float:
        return self.h - self.l

    @property
    def is_bull(self) -> bool:
        return self.c > self.o

    @property
    def is_bear(self) -> bool:
        return self.c < self.o

    @property
    def lower_wick(self) -> float:
        return min(self.o, self.c) - self.l

    @property
    def upper_wick(self) -> float:
        return self.h - max(self.o, self.c)


@dataclass
class Swing:
    ts: int
    price: float
    kind: str        # 'high' | 'low'
    index: int = -1  # candle index at detection time


@dataclass
class Structure:
    trend: Trend = Trend.RANGE
    last_high: Optional[float] = None   # last confirmed swing high
    last_low: Optional[float] = None    # last confirmed swing low
    prev_high: Optional[float] = None
    prev_low: Optional[float] = None
    bos_level: Optional[float] = None   # last level broken by close (BOS)
    choch_level: Optional[float] = None
    last_event: Optional[str] = None    # bos_up/bos_down/choch_up/choch_down
    hh: bool = False
    hl: bool = False
    lh: bool = False
    ll: bool = False

    @property
    def bullish_bias(self) -> bool:
        return self.trend == Trend.UP or self.last_event in ("bos_up", "choch_up")

    @property
    def bearish_bias(self) -> bool:
        return self.trend == Trend.DOWN or self.last_event in ("bos_down", "choch_down")


@dataclass
class Level:
    kind: LevelKind
    top: float
    bottom: float
    direction: str          # 'bull' | 'bear' | 'both'
    touches: int = 1
    ts: int = 0

    @property
    def mid(self) -> float:
        return (self.top + self.bottom) / 2.0


@dataclass
class Confirmation:
    key: str
    label_fa: str
    ok: bool
    detail: str = ""


@dataclass
class Signal:
    symbol: str
    ts: int
    direction: str          # 'long' (spot: long-only)
    entry: float
    stop: float
    target: float           # RR=min_rr target (display)
    rr: float
    score: int
    confirmations: List[Confirmation] = field(default_factory=list)
    pattern: str = ""
    atr: float = 0.0
    grid_factor: float = 1.0

    @property
    def risk_distance(self) -> float:
        return max(self.entry - self.stop, 1e-12)


@dataclass
class Position:
    id: str
    symbol: str
    qty: float
    entry: float
    stop: float
    opened_ts: int
    entry_reason: str = EntryReason.SIGNAL.value
    signal_score: int = 0
    atr_at_entry: float = 0.0
    peak_price: float = 0.0        # highest price since entry (for trailing)
    breakeven_done: bool = False
    trailing_on: bool = False
    partial_taken: bool = False
    initial_qty: float = 0.0
    state: str = PosState.OPEN.value
    close_price: Optional[float] = None
    closed_ts: Optional[int] = None
    exit_reason: Optional[str] = None
    pnl: float = 0.0
    fees_paid: float = 0.0
    realized_rr: float = 0.0
    notional: float = 0.0          # entry value (quote)
    risk_coef: float = 1.0         # margin leverage (1 = no leverage)
    side: str = "long"             # 'long' | 'short' (margin paper can short)
    initial_stop: float = 0.0      # stop at entry time — realized-R must be
                                   # measured against the INITIAL risk, not a
                                   # stop already trailed/breakeven-moved
    meta: dict = field(default_factory=dict)  # margin_id, side, leverage, liq_price, etc.

    @property
    def is_open(self) -> bool:
        return self.state == PosState.OPEN.value


@dataclass
class EquityPoint:
    ts: int
    equity: float
    drawdown_pct: float = 0.0


@dataclass
class SymbolSnapshot:
    """Everything the dashboard needs for one symbol."""
    symbol: str
    price: float = 0.0
    trend: str = "range"
    structure_event: str = ""
    rsi_h1: Optional[float] = None
    atr_h1: Optional[float] = None
    support: Optional[float] = None
    resistance: Optional[float] = None
    grid_active: bool = False
    grid_levels: List[float] = field(default_factory=list)
    signal: Optional[Signal] = None
    eligible: bool = False
    score: int = 0
    updated_ts: int = 0
