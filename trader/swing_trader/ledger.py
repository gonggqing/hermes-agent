"""Ledger — SQLite/SQLModel persistence for the trading loop (Loop.md §5.8, §6).

Persists signals, candidates, orders, fills, trades, and account snapshots.
EVERY row carries ``mode`` ("paper" | "live") so paper and live histories
never mix; row schemas are identical across modes so paper-vs-live
comparison is exact (Loop.md §6).

Implementation notes:

- Timestamps are persisted as ISO-8601 TEXT strings (SQLite DATETIME columns
  silently drop tzinfo) and reconstructed as tz-aware UTC datetimes.
- Dict / list fields (``features_json``, ``signal_ids``) are stored as JSON
  TEXT strings.
- Trade tracking: BUY fills open / extend an open trade per (symbol, mode)
  with a weighted entry price; SELL fills close ``min(fill.qty, open_qty)``.
  A partial close SPLITS the trade: the sold quantity becomes a closed
  ``TradeRow`` (with proportional pnl and commission attribution) and the
  open trade shrinks. Because of the split model each trade row has exactly
  one exit fill; a position exited across several fills yields several
  closed rows whose per-row exit prices capture the weighted exit.
- No secrets are ever written to the ledger (Loop.md §3).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional
from uuid import uuid4

from sqlmodel import Field, Session, SQLModel, create_engine, select

from swing_trader.log import get_logger
from swing_trader.schemas import (
    AccountSnapshot,
    BreakerState,
    CandidateOrder,
    CandidateStatus,
    Direction,
    Fill,
    Mode,
    Order,
    OrderStatus,
    OrderType,
    Role,
    Side,
    Signal,
    TimeInForce,
)

__all__ = [
    "ACTIVE_ORDER_STATUSES",
    "AuditEvent",
    "AuditRow",
    "CandidateRow",
    "FillRow",
    "Ledger",
    "OrderRow",
    "SignalRow",
    "SnapshotRow",
    "TradeRecord",
    "TradeRow",
    "TradeStats",
]

logger = get_logger(__name__)

SECONDS_PER_DAY: float = 86400.0

ACTIVE_ORDER_STATUSES: frozenset[OrderStatus] = frozenset(
    {OrderStatus.NEW, OrderStatus.SUBMITTED, OrderStatus.PARTIALLY_FILLED}
)


# ------------------------------------------------------------------ helpers


def _to_iso(dt: datetime) -> str:
    """Normalize to UTC and serialize as ISO-8601 TEXT (tz preserved)."""
    if dt.tzinfo is None:
        raise ValueError("timestamps must be timezone-aware (use UTC)")
    return dt.astimezone(timezone.utc).isoformat()


def _from_iso(raw: str) -> datetime:
    """Reconstruct a tz-aware UTC datetime from stored ISO-8601 TEXT."""
    dt = datetime.fromisoformat(raw)
    if dt.tzinfo is None:  # defensive: should never happen with _to_iso
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _mode_value(mode: Mode | str) -> str:
    return Mode(mode).value


def _new_id() -> str:
    return uuid4().hex


def _r_multiple(
    pnl: float, closed_qty: float, risk_per_share: Optional[float]
) -> Optional[float]:
    """Per-share pnl (net of commissions) over initial risk per share."""
    if risk_per_share is None or risk_per_share <= 0 or closed_qty <= 0:
        return None
    return (pnl / closed_qty) / risk_per_share


# ------------------------------------------------------------------ tables
# Field-for-field mirrors of the frozen pydantic schemas, plus ``mode`` on
# every table. Enums stored as their string values; timestamps as ISO TEXT.


class SignalRow(SQLModel, table=True):
    __tablename__ = "signals"

    id: str = Field(primary_key=True)
    ts: str  # ISO-8601 UTC
    mode: str = Field(index=True)
    source_agent: str
    symbol: str = Field(index=True)
    thesis: str
    direction: str
    confidence: float
    features_json: str = "{}"  # JSON TEXT


class CandidateRow(SQLModel, table=True):
    __tablename__ = "candidates"

    id: str = Field(primary_key=True)
    ts: str
    mode: str = Field(index=True)
    symbol: str = Field(index=True)
    side: str
    qty: float
    order_type: str
    limit: Optional[float] = None
    stop: Optional[float] = None
    tp: Optional[float] = None
    sl: Optional[float] = None
    tif: str
    rationale: str
    confidence: float
    signal_ids: str = "[]"  # JSON TEXT list
    ref_px: Optional[float] = None
    valid_until: Optional[str] = None  # ISO-8601 UTC
    status: str = Field(index=True)
    risk_note: str = ""
    pool: str


class OrderRow(SQLModel, table=True):
    __tablename__ = "orders"

    id: str = Field(primary_key=True)
    ts: str
    mode: str = Field(index=True)
    symbol: str = Field(index=True)
    side: str
    qty: float
    order_type: str
    limit: Optional[float] = None
    stop: Optional[float] = None
    tp: Optional[float] = None
    tif: str
    status: str = Field(index=True)
    broker_ref: Optional[str] = None
    parent_order_id: Optional[str] = None
    oca_group: Optional[str] = None
    filled_qty: float = 0.0
    avg_fill_px: Optional[float] = None


class FillRow(SQLModel, table=True):
    __tablename__ = "fills"

    id: str = Field(primary_key=True)
    ts: str
    mode: str = Field(index=True)
    order_id: str = Field(index=True)
    symbol: str = Field(index=True)
    side: str
    qty: float
    px: float
    commission: float = 0.0


class TradeRow(SQLModel, table=True):
    __tablename__ = "trades"

    id: str = Field(primary_key=True)
    ts: str  # ENTRY timestamp (first entry fill), ISO-8601 UTC
    mode: str = Field(index=True)
    symbol: str = Field(index=True)
    qty: float
    entry_order_id: str
    exit_order_id: Optional[str] = None
    entry_px: float
    exit_px: Optional[float] = None
    pnl: Optional[float] = None
    r_multiple: Optional[float] = None
    hold_days: Optional[float] = None
    rationale: str = ""
    # trade-tracking bookkeeping (not part of the pydantic Trade schema)
    is_open: bool = Field(default=True, index=True)
    exit_ts: Optional[str] = None  # ISO-8601 UTC
    risk_per_share: Optional[float] = None  # entry_px - stop_px at entry
    entry_commission: float = 0.0  # cumulative, reduced proportionally on splits
    exit_commission: float = 0.0  # attributed share of the exit fill commission


class SnapshotRow(SQLModel, table=True):
    __tablename__ = "snapshots"

    id: Optional[int] = Field(default=None, primary_key=True)
    ts: str
    mode: str = Field(index=True)
    equity: float
    cash: float
    upnl: float = 0.0
    day_pnl: float = 0.0
    drawdown_pct: float = 0.0
    breaker_state: str = BreakerState.NORMAL.value


class AuditRow(SQLModel, table=True):
    """Immutable approval-audit trail (Loop.md §3/§5.6): every candidate
    transition (and every REFUSED attempt) with actor, surface, version,
    idempotency key. Append-only — the Ledger exposes no update/delete."""

    __tablename__ = "audit_events"

    id: Optional[int] = Field(default=None, primary_key=True)
    ts: str
    mode: str = Field(index=True)
    candidate_id: str = Field(index=True)
    action: str  # publish | approve | edit | reject | expire | <refused ...>
    actor: str  # authenticated human identity, or "system"
    surface: str  # desktop | web | telegram | system
    version: int = 1
    idempotency_key: str = Field(default="", index=True)
    prev_status: str = ""
    new_status: str = ""
    applied: bool = True  # False = refused attempt (window/terminal/conflict)
    detail: str = ""


# ------------------------------------------------------- schema <-> row


def _signal_to_row(sig: Signal, mode: Mode | str) -> SignalRow:
    return SignalRow(
        id=sig.id,
        ts=_to_iso(sig.ts),
        mode=_mode_value(mode),
        source_agent=sig.source_agent,
        symbol=sig.symbol,
        thesis=sig.thesis,
        direction=sig.direction.value,
        confidence=sig.confidence,
        features_json=json.dumps(sig.features_json),
    )


def _signal_from_row(row: SignalRow) -> Signal:
    return Signal(
        id=row.id,
        ts=_from_iso(row.ts),
        source_agent=row.source_agent,
        symbol=row.symbol,
        thesis=row.thesis,
        direction=Direction(row.direction),
        confidence=row.confidence,
        features_json=json.loads(row.features_json),
    )


def _candidate_to_row(c: CandidateOrder, mode: Mode | str) -> CandidateRow:
    return CandidateRow(
        id=c.id,
        ts=_to_iso(c.ts),
        mode=_mode_value(mode),
        symbol=c.symbol,
        side=c.side.value,
        qty=c.qty,
        order_type=c.order_type.value,
        limit=c.limit,
        stop=c.stop,
        tp=c.tp,
        sl=c.sl,
        tif=c.tif.value,
        rationale=c.rationale,
        confidence=c.confidence,
        signal_ids=json.dumps(c.signal_ids),
        ref_px=c.ref_px,
        valid_until=_to_iso(c.valid_until) if c.valid_until is not None else None,
        status=c.status.value,
        risk_note=c.risk_note,
        pool=c.pool.value,
    )


def _candidate_from_row(row: CandidateRow) -> CandidateOrder:
    return CandidateOrder(
        id=row.id,
        ts=_from_iso(row.ts),
        symbol=row.symbol,
        side=Side(row.side),
        qty=row.qty,
        order_type=OrderType(row.order_type),
        limit=row.limit,
        stop=row.stop,
        tp=row.tp,
        sl=row.sl,
        tif=TimeInForce(row.tif),
        rationale=row.rationale,
        confidence=row.confidence,
        signal_ids=json.loads(row.signal_ids),
        ref_px=row.ref_px,
        valid_until=_from_iso(row.valid_until) if row.valid_until else None,
        status=CandidateStatus(row.status),
        risk_note=row.risk_note,
        pool=Role(row.pool),
    )


def _order_to_row(order: Order) -> OrderRow:
    return OrderRow(
        id=order.id,
        ts=_to_iso(order.ts),
        mode=order.mode.value,
        symbol=order.symbol,
        side=order.side.value,
        qty=order.qty,
        order_type=order.order_type.value,
        limit=order.limit,
        stop=order.stop,
        tp=order.tp,
        tif=order.tif.value,
        status=order.status.value,
        broker_ref=order.broker_ref,
        parent_order_id=order.parent_order_id,
        oca_group=order.oca_group,
        filled_qty=order.filled_qty,
        avg_fill_px=order.avg_fill_px,
    )


def _order_from_row(row: OrderRow) -> Order:
    return Order(
        id=row.id,
        ts=_from_iso(row.ts),
        mode=Mode(row.mode),
        symbol=row.symbol,
        side=Side(row.side),
        qty=row.qty,
        order_type=OrderType(row.order_type),
        limit=row.limit,
        stop=row.stop,
        tp=row.tp,
        tif=TimeInForce(row.tif),
        status=OrderStatus(row.status),
        broker_ref=row.broker_ref,
        parent_order_id=row.parent_order_id,
        oca_group=row.oca_group,
        filled_qty=row.filled_qty,
        avg_fill_px=row.avg_fill_px,
    )


def _fill_to_row(fill: Fill) -> FillRow:
    return FillRow(
        id=fill.id,
        ts=_to_iso(fill.ts),
        mode=fill.mode.value,
        order_id=fill.order_id,
        symbol=fill.symbol,
        side=fill.side.value,
        qty=fill.qty,
        px=fill.px,
        commission=fill.commission,
    )


def _fill_from_row(row: FillRow) -> Fill:
    return Fill(
        id=row.id,
        ts=_from_iso(row.ts),
        mode=Mode(row.mode),
        order_id=row.order_id,
        symbol=row.symbol,
        side=Side(row.side),
        qty=row.qty,
        px=row.px,
        commission=row.commission,
    )


def _snapshot_to_row(snap: AccountSnapshot) -> SnapshotRow:
    return SnapshotRow(
        ts=_to_iso(snap.ts),
        mode=snap.mode.value,
        equity=snap.equity,
        cash=snap.cash,
        upnl=snap.upnl,
        day_pnl=snap.day_pnl,
        drawdown_pct=snap.drawdown_pct,
        breaker_state=snap.breaker_state.value,
    )


def _snapshot_from_row(row: SnapshotRow) -> AccountSnapshot:
    return AccountSnapshot(
        ts=_from_iso(row.ts),
        mode=Mode(row.mode),
        equity=row.equity,
        cash=row.cash,
        upnl=row.upnl,
        day_pnl=row.day_pnl,
        drawdown_pct=row.drawdown_pct,
        breaker_state=BreakerState(row.breaker_state),
    )


# ------------------------------------------------------------ trade record


@dataclass
class TradeRecord:
    """Reconstructed trade row (tz-aware timestamps, enum mode restored).

    Mirrors the pydantic ``Trade`` schema (Loop.md §6) plus the ledger's
    trade-tracking bookkeeping (``is_open``, risk, commissions).
    """

    id: str
    mode: Mode
    symbol: str
    qty: float
    entry_order_id: str
    exit_order_id: Optional[str]
    entry_px: float
    exit_px: Optional[float]
    pnl: Optional[float]
    r_multiple: Optional[float]
    hold_days: Optional[float]
    rationale: str
    entry_ts: datetime
    exit_ts: Optional[datetime]
    is_open: bool
    risk_per_share: Optional[float]
    entry_commission: float
    exit_commission: float


def _trade_record(row: TradeRow) -> TradeRecord:
    return TradeRecord(
        id=row.id,
        mode=Mode(row.mode),
        symbol=row.symbol,
        qty=row.qty,
        entry_order_id=row.entry_order_id,
        exit_order_id=row.exit_order_id,
        entry_px=row.entry_px,
        exit_px=row.exit_px,
        pnl=row.pnl,
        r_multiple=row.r_multiple,
        hold_days=row.hold_days,
        rationale=row.rationale,
        entry_ts=_from_iso(row.ts),
        exit_ts=_from_iso(row.exit_ts) if row.exit_ts else None,
        is_open=row.is_open,
        risk_per_share=row.risk_per_share,
        entry_commission=row.entry_commission,
        exit_commission=row.exit_commission,
    )


# ------------------------------------------------------------------- stats


@dataclass
class AuditEvent:
    """One approval-audit record (see :class:`AuditRow`)."""

    ts: datetime
    mode: str
    candidate_id: str
    action: str
    actor: str
    surface: str
    version: int = 1
    idempotency_key: str = ""
    prev_status: str = ""
    new_status: str = ""
    applied: bool = True
    detail: str = ""


def _audit_to_row(e: AuditEvent) -> AuditRow:
    return AuditRow(
        ts=_to_iso(e.ts),
        mode=_mode_value(e.mode),
        candidate_id=e.candidate_id,
        action=e.action,
        actor=e.actor,
        surface=e.surface,
        version=e.version,
        idempotency_key=e.idempotency_key,
        prev_status=e.prev_status,
        new_status=e.new_status,
        applied=e.applied,
        detail=e.detail,
    )


def _audit_from_row(row: AuditRow) -> AuditEvent:
    return AuditEvent(
        ts=_from_iso(row.ts),
        mode=row.mode,
        candidate_id=row.candidate_id,
        action=row.action,
        actor=row.actor,
        surface=row.surface,
        version=row.version,
        idempotency_key=row.idempotency_key,
        prev_status=row.prev_status,
        new_status=row.new_status,
        applied=row.applied,
        detail=row.detail,
    )


@dataclass
class TradeStats:
    """Ledger statistics over CLOSED trades of one mode (Loop.md §5.8).

    Conventions (all divisions guarded):
    - ``win`` = pnl > 0, ``loss`` = pnl < 0; scratches (pnl == 0) count in
      ``n_closed`` / ``expectancy`` but are neither win nor loss.
    - ``avg_loss`` is the (negative) mean loss; ``payoff_ratio`` is
      ``avg_win / |avg_loss|`` and None when there are no losses (or no wins).
    - ``expectancy`` = total_pnl / n_closed (0.0 with no closed trades).
    - ``max_drawdown_pct`` = largest peak-to-trough decline of the snapshot
      equity series, as a POSITIVE percentage; 0.0 with < 2 snapshots.
    """

    n_closed: int
    n_wins: int
    win_rate: float
    avg_win: Optional[float]
    avg_loss: Optional[float]
    payoff_ratio: Optional[float]
    expectancy: float
    total_pnl: float
    avg_hold_days: Optional[float]
    max_drawdown_pct: float


# ------------------------------------------------------------------ ledger


class Ledger:
    """SQLite-backed ledger (Loop.md §5.8). One instance per database URL."""

    def __init__(self, url: str = "sqlite:///trader.db") -> None:
        self._engine = create_engine(url)
        SQLModel.metadata.create_all(self._engine)
        logger.info("ledger ready", extra={"url": url})

    # ------------------------------------------------------------- signals

    def record_signal(self, sig: Signal, mode: Mode | str) -> None:
        with Session(self._engine) as session:
            session.add(_signal_to_row(sig, mode))
            session.commit()

    def get_signals(
        self, mode: Mode | str | None = None, symbol: str | None = None
    ) -> list[Signal]:
        with Session(self._engine) as session:
            stmt = select(SignalRow)
            if mode is not None:
                stmt = stmt.where(SignalRow.mode == _mode_value(mode))
            if symbol is not None:
                stmt = stmt.where(SignalRow.symbol == symbol.strip().upper())
            rows = session.exec(stmt).all()
        out = [_signal_from_row(r) for r in rows]
        out.sort(key=lambda s: s.ts)
        return out

    # ---------------------------------------------------------- candidates

    def record_candidate(self, candidate: CandidateOrder, mode: Mode | str) -> None:
        with Session(self._engine) as session:
            session.add(_candidate_to_row(candidate, mode))
            session.commit()

    def update_candidate(
        self,
        candidate_id: str,
        status: CandidateStatus | str,
        risk_note: str | None = None,
    ) -> None:
        with Session(self._engine) as session:
            row = session.get(CandidateRow, candidate_id)
            if row is None:
                raise ValueError(f"unknown candidate id: {candidate_id}")
            row.status = CandidateStatus(status).value
            if risk_note is not None:
                row.risk_note = risk_note
            session.add(row)
            session.commit()

    def get_candidates(
        self,
        mode: Mode | str | None = None,
        status: CandidateStatus | str | None = None,
    ) -> list[CandidateOrder]:
        with Session(self._engine) as session:
            stmt = select(CandidateRow)
            if mode is not None:
                stmt = stmt.where(CandidateRow.mode == _mode_value(mode))
            if status is not None:
                stmt = stmt.where(CandidateRow.status == CandidateStatus(status).value)
            rows = session.exec(stmt).all()
        out = [_candidate_from_row(r) for r in rows]
        out.sort(key=lambda c: c.ts)
        return out

    # -------------------------------------------------------------- orders

    def record_order(self, order: Order) -> None:
        with Session(self._engine) as session:
            session.add(_order_to_row(order))
            session.commit()

    def update_order(self, order: Order) -> None:
        """Upsert by id: refresh status, filled_qty, avg_fill_px, broker_ref."""
        with Session(self._engine) as session:
            row = session.get(OrderRow, order.id)
            if row is None:
                row = _order_to_row(order)
            else:
                row.status = order.status.value
                row.filled_qty = order.filled_qty
                row.avg_fill_px = order.avg_fill_px
                row.broker_ref = order.broker_ref
            session.add(row)
            session.commit()

    def get_orders(
        self, mode: Mode | str | None = None, active_only: bool = False
    ) -> list[Order]:
        with Session(self._engine) as session:
            stmt = select(OrderRow)
            if mode is not None:
                stmt = stmt.where(OrderRow.mode == _mode_value(mode))
            rows = session.exec(stmt).all()
        out = [_order_from_row(r) for r in rows]
        if active_only:
            out = [o for o in out if o.status in ACTIVE_ORDER_STATUSES]
        out.sort(key=lambda o: o.ts)
        return out

    # --------------------------------------------------------------- fills

    def record_fill(self, fill: Fill, stop_px: float | None = None) -> Optional[TradeRecord]:
        """Persist the fill and update trade tracking (Loop.md §5.8).

        BUY opens or extends the open trade for (symbol, mode); SELL closes
        ``min(fill.qty, open_qty)``. Returns the affected trade (the open
        trade after a BUY; the closed trade/split after a SELL), or None for
        a SELL with no open trade.

        ``stop_px``: protective stop attached to the entry; sets the initial
        risk per share (``entry_px - stop_px``) used for r_multiple.
        """
        with Session(self._engine) as session:
            session.add(_fill_to_row(fill))
            open_row = self._open_trade(session, fill.symbol, fill.mode)

            if fill.side is Side.BUY:
                trade_row = self._apply_entry(open_row, fill, stop_px)
                session.add(trade_row)
                session.commit()
                session.refresh(trade_row)
                return _trade_record(trade_row)

            if open_row is None:
                logger.warning(
                    "SELL fill with no open trade — recorded fill only",
                    extra={"symbol": fill.symbol, "mode": fill.mode.value},
                )
                session.commit()
                return None

            closed_row = self._apply_exit(session, open_row, fill)
            session.commit()
            session.refresh(closed_row)
            return _trade_record(closed_row)

    def get_fills(self, mode: Mode | str | None = None) -> list[Fill]:
        with Session(self._engine) as session:
            stmt = select(FillRow)
            if mode is not None:
                stmt = stmt.where(FillRow.mode == _mode_value(mode))
            rows = session.exec(stmt).all()
        out = [_fill_from_row(r) for r in rows]
        out.sort(key=lambda f: f.ts)
        return out

    # ----------------------------------------------------- trade tracking

    @staticmethod
    def _open_trade(session: Session, symbol: str, mode: Mode | str) -> Optional[TradeRow]:
        stmt = select(TradeRow).where(
            TradeRow.symbol == symbol.strip().upper(),
            TradeRow.mode == _mode_value(mode),
            TradeRow.is_open == True,  # noqa: E712 — SQL expression
        )
        return session.exec(stmt).first()

    @staticmethod
    def _apply_entry(
        open_row: Optional[TradeRow], fill: Fill, stop_px: float | None
    ) -> TradeRow:
        if open_row is None:
            return TradeRow(
                id=_new_id(),
                ts=_to_iso(fill.ts),
                mode=fill.mode.value,
                symbol=fill.symbol.strip().upper(),
                qty=fill.qty,
                entry_order_id=fill.order_id,
                entry_px=fill.px,
                entry_commission=fill.commission,
                is_open=True,
                risk_per_share=(fill.px - stop_px) if stop_px is not None else None,
            )
        # additional BUY while open -> weighted entry, cumulative qty
        new_qty = open_row.qty + fill.qty
        open_row.entry_px = (
            open_row.entry_px * open_row.qty + fill.px * fill.qty
        ) / new_qty
        open_row.qty = new_qty
        open_row.entry_commission += fill.commission
        if stop_px is not None:
            open_row.risk_per_share = open_row.entry_px - stop_px
        return open_row

    @staticmethod
    def _apply_exit(session: Session, open_row: TradeRow, fill: Fill) -> TradeRow:
        closed_qty = min(fill.qty, open_row.qty)
        exit_comm = fill.commission * (closed_qty / fill.qty)  # fill.qty > 0 (schema)
        gross = (fill.px - open_row.entry_px) * closed_qty
        hold_days = (fill.ts - _from_iso(open_row.ts)).total_seconds() / SECONDS_PER_DAY

        if closed_qty >= open_row.qty:  # full close
            if fill.qty > open_row.qty:
                logger.warning(
                    "SELL fill exceeds open trade qty — closing open qty only",
                    extra={
                        "symbol": open_row.symbol,
                        "mode": open_row.mode,
                        "fill_qty": fill.qty,
                        "open_qty": open_row.qty,
                    },
                )
            pnl = gross - open_row.entry_commission - exit_comm
            open_row.exit_order_id = fill.order_id
            open_row.exit_px = fill.px
            open_row.exit_ts = _to_iso(fill.ts)
            open_row.pnl = pnl
            open_row.hold_days = hold_days
            open_row.r_multiple = _r_multiple(pnl, closed_qty, open_row.risk_per_share)
            open_row.exit_commission = exit_comm
            open_row.is_open = False
            session.add(open_row)
            return open_row

        # partial close -> split off a CLOSED trade for the sold qty
        entry_comm = open_row.entry_commission * (closed_qty / open_row.qty)
        pnl = gross - entry_comm - exit_comm
        closed = TradeRow(
            id=_new_id(),
            ts=open_row.ts,
            mode=open_row.mode,
            symbol=open_row.symbol,
            qty=closed_qty,
            entry_order_id=open_row.entry_order_id,
            exit_order_id=fill.order_id,
            entry_px=open_row.entry_px,
            exit_px=fill.px,
            pnl=pnl,
            r_multiple=_r_multiple(pnl, closed_qty, open_row.risk_per_share),
            hold_days=hold_days,
            rationale=open_row.rationale,
            is_open=False,
            exit_ts=_to_iso(fill.ts),
            risk_per_share=open_row.risk_per_share,
            entry_commission=entry_comm,
            exit_commission=exit_comm,
        )
        open_row.qty -= closed_qty
        open_row.entry_commission -= entry_comm
        session.add(open_row)
        session.add(closed)
        return closed

    def get_trades(
        self,
        mode: Mode | str,
        open_only: bool = False,
        closed_only: bool = False,
    ) -> list[TradeRecord]:
        if open_only and closed_only:
            raise ValueError("open_only and closed_only are mutually exclusive")
        with Session(self._engine) as session:
            stmt = select(TradeRow).where(TradeRow.mode == _mode_value(mode))
            if open_only:
                stmt = stmt.where(TradeRow.is_open == True)  # noqa: E712
            if closed_only:
                stmt = stmt.where(TradeRow.is_open == False)  # noqa: E712
            rows = session.exec(stmt).all()
        out = [_trade_record(r) for r in rows]
        out.sort(key=lambda t: t.entry_ts)
        return out

    # ----------------------------------------------------------- snapshots

    def record_snapshot(self, snap: AccountSnapshot) -> None:
        with Session(self._engine) as session:
            session.add(_snapshot_to_row(snap))
            session.commit()

    def get_snapshots(self, mode: Mode | str) -> list[AccountSnapshot]:
        with Session(self._engine) as session:
            stmt = select(SnapshotRow).where(SnapshotRow.mode == _mode_value(mode))
            rows = session.exec(stmt).all()
        out = [_snapshot_from_row(r) for r in rows]
        out.sort(key=lambda s: s.ts)
        return out

    # --------------------------------------------------------------- audit

    def record_audit(self, event: AuditEvent) -> None:
        """Append one immutable approval-audit event (Loop.md §3/§5.6)."""
        with Session(self._engine) as session:
            session.add(_audit_to_row(event))
            session.commit()

    def get_audit(
        self,
        mode: Mode | str | None = None,
        candidate_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> list[AuditEvent]:
        with Session(self._engine) as session:
            stmt = select(AuditRow)
            if mode is not None:
                stmt = stmt.where(AuditRow.mode == _mode_value(mode))
            if candidate_id is not None:
                stmt = stmt.where(AuditRow.candidate_id == candidate_id)
            if idempotency_key is not None:
                stmt = stmt.where(AuditRow.idempotency_key == idempotency_key)
            rows = session.exec(stmt).all()
        out = [_audit_from_row(r) for r in rows]
        out.sort(key=lambda e: e.ts)
        return out

    # --------------------------------------------------------------- stats

    def stats(self, mode: Mode | str) -> TradeStats:
        """Win rate / payoff / expectancy over closed trades; equity drawdown."""
        closed = self.get_trades(mode, closed_only=True)
        pnls = [t.pnl for t in closed if t.pnl is not None]
        wins = [p for p in pnls if p > 0]
        losses = [p for p in pnls if p < 0]
        n_closed = len(closed)
        total_pnl = sum(pnls)

        win_rate = len(wins) / n_closed if n_closed else 0.0
        avg_win = sum(wins) / len(wins) if wins else None
        avg_loss = sum(losses) / len(losses) if losses else None
        payoff_ratio = (
            avg_win / abs(avg_loss)
            if (avg_win is not None and avg_loss is not None and avg_loss != 0)
            else None
        )
        expectancy = total_pnl / n_closed if n_closed else 0.0
        holds = [t.hold_days for t in closed if t.hold_days is not None]
        avg_hold_days = sum(holds) / len(holds) if holds else None

        return TradeStats(
            n_closed=n_closed,
            n_wins=len(wins),
            win_rate=win_rate,
            avg_win=avg_win,
            avg_loss=avg_loss,
            payoff_ratio=payoff_ratio,
            expectancy=expectancy,
            total_pnl=total_pnl,
            avg_hold_days=avg_hold_days,
            max_drawdown_pct=self._max_drawdown_pct(mode),
        )

    def _max_drawdown_pct(self, mode: Mode | str) -> float:
        """Largest peak-to-trough equity decline (positive %); 0 if < 2 snapshots."""
        equities = [s.equity for s in self.get_snapshots(mode)]
        if len(equities) < 2:
            return 0.0
        peak = equities[0]
        max_dd = 0.0
        for eq in equities:
            if eq > peak:
                peak = eq
            if peak > 0:  # guard division
                dd = (peak - eq) / peak * 100.0
                if dd > max_dd:
                    max_dd = dd
        return max_dd
