"""Core data schemas (Loop.md §6). Paper and live share identical schemas —
only the ``mode`` tag differs — so paper-vs-live comparison is exact.

Considered FROZEN for Phase 0 once the ledger lands: additive changes only.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Optional
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from swing_trader.config import Mode  # re-exported for convenience

__all__ = [
    "AccountSnapshot",
    "AiPhase",
    "BreakerState",
    "CandidateOrder",
    "CandidateStatus",
    "Direction",
    "Fill",
    "Mode",
    "Order",
    "OrderStatus",
    "OrderType",
    "Position",
    "Role",
    "Side",
    "Signal",
    "TimeInForce",
    "Trade",
    "utcnow",
]


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def new_id() -> str:
    return uuid4().hex


# --------------------------------------------------------------------------- enums


class Side(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class Direction(str, Enum):
    LONG = "long"
    SHORT = "short"
    NEUTRAL = "neutral"


class OrderType(str, Enum):
    LMT = "LMT"
    STP = "STP"
    MOC = "MOC"
    LOC = "LOC"
    BRACKET = "BRACKET"  # entry LMT + attached GTC stop (sl) + optional tp LMT (OCA)


class TimeInForce(str, Enum):
    GTC = "GTC"
    DAY = "DAY"


class OrderStatus(str, Enum):
    NEW = "NEW"  # created locally, not yet submitted
    SUBMITTED = "SUBMITTED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"


class BreakerState(str, Enum):
    NORMAL = "NORMAL"
    TRIPPED = "TRIPPED"  # daily drawdown breaker hit -> no new entries today


class Role(str, Enum):
    """Per-symbol portfolio role (Loop.md §11); risk engine caps exposure per role."""

    CORE = "core"
    CONVICTION = "conviction"
    ROTATION = "rotation"
    HEDGE = "hedge"


class AiPhase(str, Enum):
    """AI value-chain phase tag (Loop.md §11/§12)."""

    INFRA = "infra"
    MEMORY = "memory"
    NETWORK = "network"
    POWER = "power"
    APPLICATION = "application"
    CLOUD = "cloud"
    NONE = "none"  # non-AI holdings (hedges, base indices)


class CandidateStatus(str, Enum):
    PROPOSED = "proposed"  # decision core output
    RISK_APPROVED = "risk_approved"  # risk engine passed (possibly resized)
    RISK_VETOED = "risk_vetoed"
    PUSHED = "pushed"  # sent to Telegram
    APPROVED = "approved"  # human said yes
    EDITED = "edited"  # human edited then approved
    REJECTED = "rejected"  # human said no
    EXPIRED = "expired"  # confirmation window passed
    PLACED = "placed"  # translated into broker order(s)


# --------------------------------------------------------------------------- helpers


class _TsModel(BaseModel):
    """Base with tz-aware timestamp enforcement."""

    model_config = ConfigDict(validate_assignment=True)

    ts: datetime = Field(default_factory=utcnow)

    @field_validator("ts")
    @classmethod
    def _tz_aware(cls, v: datetime) -> datetime:
        if v.tzinfo is None:
            raise ValueError("timestamps must be timezone-aware (use UTC)")
        return v


# --------------------------------------------------------------------------- schemas


class Signal(_TsModel):
    """Output of an analysis sub-agent (Loop.md §6)."""

    id: str = Field(default_factory=new_id)
    source_agent: str
    symbol: str
    thesis: str
    direction: Direction
    confidence: float = Field(ge=0.0, le=1.0)
    features_json: dict = Field(default_factory=dict)

    @field_validator("symbol")
    @classmethod
    def _upper(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("symbol must be non-empty")
        return v.strip().upper()


class Order(_TsModel):
    id: str = Field(default_factory=new_id)
    mode: Mode
    symbol: str
    side: Side
    qty: float = Field(gt=0)
    order_type: OrderType
    limit: Optional[float] = Field(default=None, gt=0)
    stop: Optional[float] = Field(default=None, gt=0)
    tp: Optional[float] = Field(default=None, gt=0)  # take-profit (BRACKET only)
    tif: TimeInForce = TimeInForce.GTC
    status: OrderStatus = OrderStatus.NEW
    broker_ref: Optional[str] = None
    # Bracket/OCA bookkeeping (broker-generated legs)
    parent_order_id: Optional[str] = None
    oca_group: Optional[str] = None
    filled_qty: float = Field(default=0.0, ge=0)
    avg_fill_px: Optional[float] = None

    @field_validator("symbol")
    @classmethod
    def _upper(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("symbol must be non-empty")
        return v.strip().upper()

    @model_validator(mode="after")
    def _required_prices(self) -> "Order":
        ot = self.order_type
        if ot in (OrderType.LMT, OrderType.LOC) and self.limit is None:
            raise ValueError(f"{ot.value} order requires a limit price")
        if ot is OrderType.STP and self.stop is None:
            raise ValueError("STP order requires a stop price")
        if ot is OrderType.BRACKET:
            if self.limit is None or self.stop is None:
                raise ValueError("BRACKET requires entry limit AND protective stop")
            if self.side is Side.BUY:
                if self.stop >= self.limit:
                    raise ValueError("BUY bracket: stop must be below entry limit")
                if self.tp is not None and self.tp <= self.limit:
                    raise ValueError("BUY bracket: tp must be above entry limit")
            else:
                if self.stop <= self.limit:
                    raise ValueError("SELL bracket: stop must be above entry limit")
                if self.tp is not None and self.tp >= self.limit:
                    raise ValueError("SELL bracket: tp must be below entry limit")
        if self.filled_qty > self.qty:
            raise ValueError("filled_qty cannot exceed qty")
        return self


class Fill(_TsModel):
    id: str = Field(default_factory=new_id)
    order_id: str
    symbol: str
    side: Side
    qty: float = Field(gt=0)
    px: float = Field(gt=0)
    commission: float = Field(default=0.0, ge=0)
    mode: Mode = Mode.PAPER


class Trade(_TsModel):
    """A round trip. ``exit_*``/``pnl`` stay None while the position is open."""

    id: str = Field(default_factory=new_id)
    mode: Mode
    symbol: str
    qty: float = Field(gt=0)
    entry_order_id: str
    exit_order_id: Optional[str] = None
    entry_px: float = Field(gt=0)
    exit_px: Optional[float] = Field(default=None, gt=0)
    pnl: Optional[float] = None
    r_multiple: Optional[float] = None  # pnl / initial risk (entry - stop)
    hold_days: Optional[float] = None
    rationale: str = ""


class Position(BaseModel):
    model_config = ConfigDict(validate_assignment=True)

    symbol: str
    qty: float  # negative = short (not expected in Phase 0 cash account)
    avg_px: float = Field(ge=0)
    mkt_px: Optional[float] = Field(default=None, ge=0)
    pool: Role = Role.ROTATION

    @property
    def upnl(self) -> Optional[float]:
        if self.mkt_px is None:
            return None
        return (self.mkt_px - self.avg_px) * self.qty

    @property
    def market_value(self) -> Optional[float]:
        if self.mkt_px is None:
            return None
        return self.mkt_px * self.qty


class AccountSnapshot(_TsModel):
    mode: Mode
    equity: float
    cash: float
    upnl: float = 0.0
    day_pnl: float = 0.0
    drawdown_pct: float = 0.0  # intraday drawdown vs day-open equity, in %
    breaker_state: BreakerState = BreakerState.NORMAL


class CandidateOrder(_TsModel):
    """Decision-core output (Loop.md §5.4) flowing through risk -> Telegram -> execution."""

    id: str = Field(default_factory=new_id)
    symbol: str
    side: Side
    qty: float = Field(gt=0)
    order_type: OrderType
    limit: Optional[float] = Field(default=None, gt=0)
    stop: Optional[float] = Field(default=None, gt=0)
    tp: Optional[float] = Field(default=None, gt=0)
    sl: Optional[float] = Field(default=None, gt=0)  # protective stop for non-bracket entries
    tif: TimeInForce = TimeInForce.GTC
    rationale: str
    confidence: float = Field(ge=0.0, le=1.0)
    signal_ids: list[str] = Field(default_factory=list)
    ref_px: Optional[float] = Field(default=None, gt=0)  # price seen at decision time
    valid_until: Optional[datetime] = None  # execution re-validates before send (§5.7)
    status: CandidateStatus = CandidateStatus.PROPOSED
    risk_note: str = ""  # risk engine veto/shrink explanation
    pool: Role = Role.ROTATION

    @field_validator("symbol")
    @classmethod
    def _upper(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("symbol must be non-empty")
        return v.strip().upper()

    @field_validator("valid_until")
    @classmethod
    def _tz_aware_valid_until(cls, v: Optional[datetime]) -> Optional[datetime]:
        if v is not None and v.tzinfo is None:
            raise ValueError("valid_until must be timezone-aware")
        return v

    @model_validator(mode="after")
    def _protection_required(self) -> "CandidateOrder":
        """Loop.md §4: never leave a position without a resting stop."""
        if self.side is Side.BUY and self.order_type is not OrderType.BRACKET:
            if self.sl is None:
                raise ValueError(
                    "entry candidate requires a protective stop: use BRACKET or set sl"
                )
        if self.order_type is OrderType.BRACKET and (self.limit is None or self.stop is None):
            raise ValueError("BRACKET candidate requires limit and stop")
        return self
