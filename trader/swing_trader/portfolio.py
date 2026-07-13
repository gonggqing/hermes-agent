"""Portfolio domain model + holdings projection (Loop.md §7 Phase 0.9).

A SEPARATE, auditable record of the user's REAL multi-account holdings (US /
HK / mainland China), kept strictly apart from the trading loop's
candidates/orders/fills so that manually-reported or externally-executed
positions can NEVER contaminate strategy win-rate, execution attribution, or
the system trade audit (Loop.md §7 P0.9 "Portfolio authority", boundary #1).

Design invariants (Loop.md §3, §5.8, P0.9 backlog):

- **Append-only events.** Current holdings are DERIVED from an
  ``OPENING_BALANCE`` plus subsequent immutable :class:`PortfolioEvent` rows;
  history is never rewritten. Corrections/deletes are expressed as a
  ``CORRECTION`` event that reverses a prior event — never an in-place edit.
- **Never guess.** Unknown cost basis stays ``None`` — the LLM/agent must not
  synthesize a price (P0.9 backlog "Risk/analysis projection").
- **Pure + deterministic.** :func:`derive_holdings` rebuilds holdings from the
  event list alone (any projection/cache elsewhere must be reconstructable
  from these events). No I/O, no clock — callers pass events in.

This module is DB-free; :mod:`swing_trader.portfolio_journal` persists it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from enum import Enum
from typing import Optional
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

__all__ = [
    "AccountHoldings",
    "AccountType",
    "AggregateHolding",
    "AggregatePortfolio",
    "CashBalance",
    "DraftStatus",
    "aggregate_holdings",
    "EventSource",
    "EventType",
    "Holding",
    "MarketScope",
    "PortfolioAccount",
    "PortfolioDraft",
    "PortfolioEvent",
    "ProviderKind",
    "SecurityType",
    "SYSTEM_ACTORS",
    "derive_holdings",
    "draft_missing_fields",
    "new_id",
    "utcnow",
]

#: Actor identities that may NEVER finalize a portfolio event (boundary #4:
#: the LLM/system can DRAFT but a human must confirm). Matched case-insensitively.
SYSTEM_ACTORS: frozenset[str] = frozenset({"system", "llm", "hermes", "agent", "bot"})


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def new_id() -> str:
    return uuid4().hex


#: qty differences below this are treated as flat (fractional-share noise).
_QTY_TOL = 1e-9


# --------------------------------------------------------------------------- enums


class MarketScope(str, Enum):
    US = "US"
    HK = "HK"
    CN = "CN"


class AccountType(str, Enum):
    CASH = "cash"
    MARGIN = "margin"


class ProviderKind(str, Enum):
    """Who runs the account — decides whose state is authoritative (P0.9)."""

    MANUAL = "manual"  # human-maintained; authoritative for CN in Phase 0.9
    IBKR = "ibkr"  # broker-authoritative for US/HK once connected


class EventSource(str, Enum):
    """Where a single event came from (provenance, P0.9 backlog)."""

    MANUAL = "manual"
    CSV = "csv"
    IBKR_FLEX = "ibkr_flex"
    IBKR_API = "ibkr_api"
    SYSTEM = "system"  # reserved; MUST NOT fabricate broker fills (boundary #1)


class EventType(str, Enum):
    OPENING_BALANCE = "opening_balance"  # bootstrap a share lot OR opening cash
    BUY = "buy"
    SELL = "sell"
    DIVIDEND = "dividend"  # cash in
    FEE = "fee"  # cash out
    CASH_TRANSFER = "cash_transfer"  # cash in/out (signed amount)
    SPLIT = "split"  # qty *= factor, avg cost /= factor
    OTHER_CORPORATE_ACTION = "other_corporate_action"
    CORRECTION = "correction"  # reverses a prior event (compensating entry)


class SecurityType(str, Enum):
    STOCK = "stock"
    ETF = "etf"
    FUND = "fund"


class DraftStatus(str, Enum):
    DRAFT = "draft"  # awaiting human review
    CONFIRMED = "confirmed"  # human confirmed -> event appended
    REJECTED = "rejected"  # human rejected
    EXPIRED = "expired"  # went stale unconfirmed


# --------------------------------------------------------------------------- models


def _tz_aware(v: datetime) -> datetime:
    if v.tzinfo is None:
        raise ValueError("timestamps must be timezone-aware (use UTC)")
    return v.astimezone(timezone.utc)


class PortfolioAccount(BaseModel):
    """A real account the user maintains (Loop.md P0.9). One journal spans
    many accounts across US/HK/CN, each keeping its own source attribution."""

    model_config = ConfigDict(validate_assignment=True)

    id: str = Field(default_factory=new_id)
    name: str = Field(min_length=1, max_length=200)
    provider: ProviderKind = ProviderKind.MANUAL
    market_scope: MarketScope
    account_type: AccountType = AccountType.CASH
    base_currency: str = Field(min_length=1, max_length=8)
    include_in_risk: bool = True  # feeds RiskEngine exposure when True (P0.9)
    note: str = ""
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)

    @field_validator("base_currency")
    @classmethod
    def _ccy_upper(cls, v: str) -> str:
        return v.strip().upper()

    @field_validator("created_at", "updated_at")
    @classmethod
    def _aware(cls, v: datetime) -> datetime:
        return _tz_aware(v)


class PortfolioEvent(BaseModel):
    """One immutable portfolio journal entry (append-only). ``qty`` is a
    magnitude whose meaning is set by ``event_type``; ``amount`` carries a
    signed cash delta for cash-only events. Unknown ``price``/cost stays None.
    """

    model_config = ConfigDict(validate_assignment=True)

    id: str = Field(default_factory=new_id)
    account_id: str
    event_type: EventType
    symbol: Optional[str] = None
    market: Optional[MarketScope] = None
    currency: str = Field(min_length=1, max_length=8)
    qty: float = 0.0  # magnitude (>=0); direction implied by event_type
    price: Optional[float] = Field(default=None, ge=0)  # None = cost unknown
    commission: Optional[float] = Field(default=None, ge=0)
    amount: Optional[float] = None  # signed cash delta (dividend/fee/transfer)
    occurred_at: datetime
    settlement_date: Optional[date] = None
    source: EventSource = EventSource.MANUAL
    external_id: Optional[str] = None  # broker execution id (dedup key)
    idempotency_key: str = Field(min_length=1)
    reverses_event_id: Optional[str] = None
    actor: str = Field(min_length=1)  # authenticated human, or system importer
    surface: str = Field(min_length=1)  # desktop | web | telegram | csv | system
    note: str = ""
    created_at: datetime = Field(default_factory=utcnow)

    @field_validator("symbol")
    @classmethod
    def _sym(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        v = v.strip()
        return v.upper() if v else None

    @field_validator("currency")
    @classmethod
    def _ccy_upper(cls, v: str) -> str:
        return v.strip().upper()

    @field_validator("qty")
    @classmethod
    def _qty_nonneg(cls, v: float) -> float:
        if v < 0:
            raise ValueError("qty is a magnitude; use event_type for direction")
        return v

    @field_validator("occurred_at", "created_at")
    @classmethod
    def _aware(cls, v: datetime) -> datetime:
        return _tz_aware(v)

    @model_validator(mode="after")
    def _shape(self) -> "PortfolioEvent":
        et = self.event_type
        needs_symbol_qty = {EventType.BUY, EventType.SELL, EventType.SPLIT}
        if et in needs_symbol_qty:
            if not self.symbol:
                raise ValueError(f"{et.value} event requires a symbol")
            if self.qty <= 0:
                raise ValueError(f"{et.value} event requires qty > 0")
        if et is EventType.OPENING_BALANCE:
            # Either a share lot (symbol+qty) or opening cash (amount, no symbol).
            if self.symbol:
                if self.qty <= 0:
                    raise ValueError("opening share balance requires qty > 0")
            elif self.amount is None:
                raise ValueError(
                    "opening_balance requires a symbol+qty (share lot) or an amount (cash)"
                )
        if et in {EventType.DIVIDEND, EventType.FEE, EventType.CASH_TRANSFER}:
            if self.amount is None:
                raise ValueError(f"{et.value} event requires a cash amount")
        if et is EventType.CORRECTION and not self.reverses_event_id:
            raise ValueError("correction event requires reverses_event_id")
        return self


class PortfolioDraft(BaseModel):
    """A PROPOSED portfolio event awaiting human confirmation (Loop.md P0.9,
    boundary #4). Hermes parses a user statement into a draft; free-form
    conversation can NEVER mutate holdings — only an authenticated human
    confirmation turns a draft into an append-only :class:`PortfolioEvent`.

    Fields may be partial: ``missing``/``ambiguities`` record what must be
    clarified before confirmation (never guessed). The draft is a proposed
    FILLED event; an unfilled ORDER must be flagged in ``ambiguities`` and must
    not confirm (an order does not change holdings until filled).
    """

    model_config = ConfigDict(validate_assignment=True)

    id: str = Field(default_factory=new_id)
    account_id: Optional[str] = None
    event_type: EventType = EventType.BUY
    symbol: Optional[str] = None
    market: Optional[MarketScope] = None
    currency: Optional[str] = None
    qty: Optional[float] = Field(default=None, ge=0)
    price: Optional[float] = Field(default=None, ge=0)
    commission: Optional[float] = Field(default=None, ge=0)
    amount: Optional[float] = None
    occurred_at: Optional[datetime] = None
    settlement_date: Optional[date] = None
    source: EventSource = EventSource.MANUAL
    external_id: Optional[str] = None
    note: str = ""
    # -- draft workflow bookkeeping --
    status: DraftStatus = DraftStatus.DRAFT
    version: int = 1
    original_text: str = ""  # the user's utterance (audit + re-parse)
    missing: list[str] = Field(default_factory=list)
    ambiguities: list[str] = Field(default_factory=list)
    created_by: str = "hermes"  # who drafted (LLM or human); NOT the confirmer
    created_surface: str = "system"
    confirmed_by: Optional[str] = None
    confirmed_at: Optional[datetime] = None
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)

    @field_validator("symbol")
    @classmethod
    def _sym(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        v = v.strip()
        return v.upper() if v else None

    @field_validator("currency")
    @classmethod
    def _ccy(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        v = v.strip()
        return v.upper() if v else None

    @property
    def needs_clarification(self) -> bool:
        return bool(self.missing or self.ambiguities)


def draft_missing_fields(draft: PortfolioDraft) -> list[str]:
    """Hard-required fields a draft still lacks (DB-independent). Account
    existence/ambiguity is checked by the service, which has the journal."""
    missing: list[str] = []
    et = draft.event_type
    if draft.account_id is None:
        missing.append("account")
    if draft.currency is None:
        missing.append("currency")
    if draft.occurred_at is None:
        missing.append("time")
    if et in {EventType.BUY, EventType.SELL, EventType.SPLIT}:
        if not draft.symbol:
            missing.append("symbol")
        if draft.qty is None or draft.qty <= 0:
            missing.append("quantity")
    if et in {EventType.DIVIDEND, EventType.FEE, EventType.CASH_TRANSFER}:
        if draft.amount is None:
            missing.append("amount")
    if et is EventType.OPENING_BALANCE:
        if draft.symbol and (draft.qty is None or draft.qty <= 0):
            missing.append("quantity")
        if not draft.symbol and draft.amount is None:
            missing.append("quantity or amount")
    return missing


# ------------------------------------------------------------------ projection


@dataclass
class Holding:
    """Derived per-symbol position. ``avg_cost`` is None when ANY contributing
    lot had an unknown price — never synthesized (Loop.md P0.9)."""

    symbol: str
    market: Optional[MarketScope]
    currency: str
    qty: float
    avg_cost: Optional[float]
    cost_basis_known: bool


@dataclass
class CashBalance:
    currency: str
    amount: Optional[float]  # None when any contributing cash delta was unknown
    known: bool


@dataclass
class AccountHoldings:
    account_id: str
    holdings: list[Holding] = field(default_factory=list)
    cash: list[CashBalance] = field(default_factory=list)
    as_of: Optional[datetime] = None  # latest applied event's occurred_at
    n_events: int = 0


@dataclass
class AggregateHolding:
    symbol: str
    market: Optional[MarketScope]
    currency: str
    qty: float
    avg_cost: Optional[float]  # None if ANY contributing account's cost unknown
    cost_basis_known: bool
    accounts: list[str] = field(default_factory=list)  # source account ids


@dataclass
class AggregatePortfolio:
    """Combined, source-tagged view across accounts (Loop.md P0.9 risk/research
    projection). Sums DISTINCT accounts — reconciliation, not aggregation, is
    what prevents a manual+broker double-count of the SAME account."""

    holdings: list[AggregateHolding] = field(default_factory=list)
    cash: list[CashBalance] = field(default_factory=list)
    accounts: list[str] = field(default_factory=list)
    as_of: Optional[datetime] = None


def aggregate_holdings(
    items: list[tuple["PortfolioAccount", AccountHoldings]],
) -> AggregatePortfolio:
    """Combine per-account holdings into one source-tagged portfolio. A symbol
    held in several accounts sums; cost basis stays known only if EVERY
    contributing account's cost is known (else None — never guessed)."""
    by_sym: dict[str, dict] = {}
    cash: dict[str, list] = {}
    account_ids: list[str] = []
    as_of: Optional[datetime] = None

    for account, h in items:
        account_ids.append(account.id)
        if h.as_of is not None and (as_of is None or h.as_of > as_of):
            as_of = h.as_of
        for pos in h.holdings:
            agg = by_sym.setdefault(pos.symbol, {
                "market": pos.market, "currency": pos.currency,
                "qty": 0.0, "cost": 0.0, "known": True, "accounts": [],
            })
            agg["qty"] += pos.qty
            if pos.cost_basis_known and pos.avg_cost is not None:
                agg["cost"] += pos.avg_cost * pos.qty
            else:
                agg["known"] = False
            agg["accounts"].append(account.id)
            if agg["market"] is None:
                agg["market"] = pos.market
        for cb in h.cash:
            cur = cash.setdefault(cb.currency, [0.0, True])
            if cb.known and cb.amount is not None:
                cur[0] += cb.amount
            else:
                cur[1] = False

    holdings = []
    for sym, a in sorted(by_sym.items()):
        if abs(a["qty"]) <= _QTY_TOL:
            continue
        known = a["known"] and a["qty"] > _QTY_TOL
        holdings.append(AggregateHolding(
            symbol=sym, market=a["market"], currency=a["currency"], qty=a["qty"],
            avg_cost=(a["cost"] / a["qty"]) if known else None,
            cost_basis_known=known, accounts=a["accounts"]))
    cash_balances = [
        CashBalance(currency=c, amount=(amt if known else None), known=known)
        for c, (amt, known) in sorted(cash.items())
        if not known or abs(amt) > _QTY_TOL
    ]
    return AggregatePortfolio(holdings=holdings, cash=cash_balances,
                              accounts=account_ids, as_of=as_of)


@dataclass
class _Lot:
    qty: float = 0.0
    total_cost: float = 0.0  # sum(qty*price) + commissions, when known
    cost_known: bool = True
    market: Optional[MarketScope] = None
    currency: str = ""


def _event_sort_key(e: PortfolioEvent) -> tuple:
    return (e.occurred_at, e.created_at, e.id)


def derive_holdings(account_id: str, events: list[PortfolioEvent]) -> AccountHoldings:
    """Rebuild current holdings + cash for one account from its events alone.

    Deterministic and pure: events are applied in (occurred_at, created_at, id)
    order after excluding any event reversed by a ``CORRECTION``. Cost basis is
    weighted-average and becomes unknown (``None``) the moment a lot's price is
    unknown; it is never guessed. Cash is tracked per currency and likewise
    goes unknown if a contributing amount/price is unknown.
    """
    scoped = [e for e in events if e.account_id == account_id]
    reversed_ids = {
        e.reverses_event_id
        for e in scoped
        if e.event_type is EventType.CORRECTION and e.reverses_event_id
    }
    # Apply everything except reversed events and the CORRECTION markers.
    applied = [
        e
        for e in scoped
        if e.id not in reversed_ids and e.event_type is not EventType.CORRECTION
    ]
    applied.sort(key=_event_sort_key)

    lots: dict[str, _Lot] = {}
    cash: dict[str, list] = {}  # currency -> [amount, known]
    as_of: Optional[datetime] = None

    def bump_cash(ccy: str, delta: Optional[float]) -> None:
        cur = cash.setdefault(ccy, [0.0, True])
        if delta is None:
            cur[1] = False
        else:
            cur[0] += delta

    def buy_cost(e: PortfolioEvent) -> Optional[float]:
        """Cash paid for a BUY/opening lot, or None if price is unknown."""
        if e.price is None:
            return None
        return e.qty * e.price + (e.commission or 0.0)

    for e in applied:
        as_of = e.occurred_at
        et = e.event_type

        if et in {EventType.OPENING_BALANCE, EventType.BUY} and e.symbol:
            lot = lots.setdefault(
                e.symbol, _Lot(market=e.market, currency=e.currency)
            )
            lot.qty += e.qty
            cost = buy_cost(e)
            if cost is None:
                lot.cost_known = False
            else:
                lot.total_cost += cost
            if et is EventType.BUY:  # cash paid; opening balances need no cash move
                bump_cash(e.currency, None if e.price is None else -cost)
            if lot.market is None:
                lot.market = e.market

        elif et is EventType.OPENING_BALANCE and not e.symbol:
            bump_cash(e.currency, e.amount)  # opening cash

        elif et is EventType.SELL and e.symbol:
            lot = lots.setdefault(
                e.symbol, _Lot(market=e.market, currency=e.currency)
            )
            sold = min(e.qty, lot.qty) if lot.qty > 0 else e.qty
            # reduce cost basis proportionally (avg method); keep unknown sticky
            if lot.qty > _QTY_TOL and lot.cost_known:
                lot.total_cost -= lot.total_cost * (sold / lot.qty)
            lot.qty -= e.qty
            proceeds = None if e.price is None else e.qty * e.price - (e.commission or 0.0)
            bump_cash(e.currency, proceeds)

        elif et is EventType.SPLIT and e.symbol:
            lot = lots.get(e.symbol)
            if lot is not None and e.qty > 0:
                lot.qty *= e.qty  # qty carries the split factor (e.g. 2.0 for 2:1)
                # total_cost is unchanged → avg cost divides by the factor

        elif et is EventType.DIVIDEND:
            bump_cash(e.currency, e.amount)
        elif et is EventType.FEE:
            bump_cash(e.currency, -e.amount)  # amount required by validator
        elif et is EventType.CASH_TRANSFER:
            bump_cash(e.currency, e.amount)
        # OTHER_CORPORATE_ACTION: recorded for audit; no deterministic holdings
        # effect in Phase 0.9 (surfaced in the Activity view instead).

    holdings = []
    for sym, lot in sorted(lots.items()):
        if abs(lot.qty) <= _QTY_TOL:
            continue
        known = lot.cost_known and lot.qty > _QTY_TOL
        avg = (lot.total_cost / lot.qty) if known else None
        holdings.append(
            Holding(
                symbol=sym,
                market=lot.market,
                currency=lot.currency,
                qty=lot.qty,
                avg_cost=avg,
                cost_basis_known=known,
            )
        )

    cash_balances = [
        CashBalance(currency=ccy, amount=(amt if known else None), known=known)
        for ccy, (amt, known) in sorted(cash.items())
        if known is False or abs(amt) > _QTY_TOL
    ]

    return AccountHoldings(
        account_id=account_id,
        holdings=holdings,
        cash=cash_balances,
        as_of=as_of,
        n_events=len(applied),
    )
