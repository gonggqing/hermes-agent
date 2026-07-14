"""PortfolioJournal — SQLite/SQLModel persistence for the Portfolio (P0.9).

An append-only journal of the user's REAL accounts and holdings, deliberately
SEPARATE from the trading :class:`~swing_trader.ledger.Ledger` (candidates /
orders / fills / trades) so that manual or externally-executed positions can
never be mistaken for system-executed trades (Loop.md §7 P0.9, boundary #1).
Both may live in the same SQLite file — they share no tables and the journal
never reads or writes the trading ledger's rows.

Mirrors the ledger's house style: ``*Row(SQLModel, table=True)`` with explicit
``__tablename__``, enums stored as string values, timestamps as ISO-8601 TEXT,
``create_all`` for schema (no migrations framework). Accounts are mutable
config; portfolio EVENTS are append-only (no update/delete API) — corrections
are new ``CORRECTION`` events (Loop.md P0.9 backlog).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Optional

from sqlalchemy import inspect as sa_inspect
from sqlalchemy import text
from sqlmodel import Field, Session, SQLModel, create_engine, select

from swing_trader.log import get_logger
from swing_trader.portfolio import (
    AccountHoldings,
    AccountType,
    DraftStatus,
    EventSource,
    EventType,
    MarketScope,
    PortfolioAccount,
    PortfolioDraft,
    PortfolioEvent,
    ProviderKind,
    aggregate_holdings,
    derive_holdings,
)

logger = get_logger(__name__)

__all__ = [
    "Mark",
    "PortfolioAccountRow",
    "PortfolioAuditEvent",
    "PortfolioAuditRow",
    "PortfolioDraftRow",
    "PortfolioEventRow",
    "PortfolioJournal",
    "PortfolioMarkRow",
]


# ------------------------------------------------------------------ helpers


def _to_iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        raise ValueError("timestamps must be timezone-aware (use UTC)")
    return dt.astimezone(timezone.utc).isoformat()


def _from_iso(raw: str) -> datetime:
    dt = datetime.fromisoformat(raw)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _to_date(d: Optional[date]) -> Optional[str]:
    return d.isoformat() if d is not None else None


def _from_date(raw: Optional[str]) -> Optional[date]:
    return date.fromisoformat(raw) if raw else None


# ------------------------------------------------------------------- tables


class PortfolioAccountRow(SQLModel, table=True):
    __tablename__ = "portfolio_accounts"

    id: str = Field(primary_key=True)
    name: str
    provider: str = Field(index=True)
    market_scope: str = Field(index=True)
    account_type: str
    base_currency: str
    include_in_risk: bool = True
    note: str = ""
    created_at: str
    updated_at: str


class PortfolioEventRow(SQLModel, table=True):
    """Append-only portfolio event (Loop.md P0.9). The journal exposes no
    update/delete — corrections are new ``CORRECTION`` rows."""

    __tablename__ = "portfolio_events"

    id: str = Field(primary_key=True)
    account_id: str = Field(index=True)
    event_type: str = Field(index=True)
    symbol: Optional[str] = Field(default=None, index=True)
    market: Optional[str] = None
    currency: str
    qty: float = 0.0
    price: Optional[float] = None
    commission: Optional[float] = None
    amount: Optional[float] = None
    occurred_at: str
    settlement_date: Optional[str] = None
    source: str
    external_id: Optional[str] = Field(default=None, index=True)
    idempotency_key: str = Field(index=True)
    reverses_event_id: Optional[str] = None
    actor: str
    surface: str
    note: str = ""
    created_at: str


class PortfolioDraftRow(SQLModel, table=True):
    """A proposed event awaiting human confirmation (mutable until terminal)."""

    __tablename__ = "portfolio_drafts"

    id: str = Field(primary_key=True)
    account_id: Optional[str] = Field(default=None, index=True)
    event_type: str
    symbol: Optional[str] = None
    market: Optional[str] = None
    currency: Optional[str] = None
    qty: Optional[float] = None
    price: Optional[float] = None
    commission: Optional[float] = None
    amount: Optional[float] = None
    occurred_at: Optional[str] = None
    settlement_date: Optional[str] = None
    source: str
    external_id: Optional[str] = None
    reverses_event_id: Optional[str] = None
    note: str = ""
    status: str = Field(index=True)
    version: int = 1
    original_text: str = ""
    missing: str = "[]"  # JSON TEXT list
    ambiguities: str = "[]"  # JSON TEXT list
    created_by: str
    created_surface: str
    confirmed_by: Optional[str] = None
    confirmed_at: Optional[str] = None
    created_at: str
    updated_at: str


class PortfolioAuditRow(SQLModel, table=True):
    """Append-only audit of every draft action + event commit (applied OR
    refused), with actor/surface/idempotency (Loop.md §3 pattern)."""

    __tablename__ = "portfolio_audit"

    id: Optional[int] = Field(default=None, primary_key=True)
    ts: str
    account_id: str = Field(default="", index=True)
    draft_id: str = Field(default="", index=True)
    event_id: str = ""
    action: str  # draft | edit | reject | expire | confirm | <refused ...>
    actor: str
    surface: str
    version: int = 1
    idempotency_key: str = Field(default="", index=True)
    applied: bool = True
    detail: str = ""


class PortfolioMarkRow(SQLModel, table=True):
    """Latest known price/NAV for a symbol — a MUTABLE, NON-authoritative
    valuation input (Loop.md P0.9). NOT a holding fact: it never affects
    qty/cost, only market-value/P&L display. Keyed globally by symbol."""

    __tablename__ = "portfolio_marks"

    symbol: str = Field(primary_key=True)
    price: float
    currency: str
    as_of: str  # ISO-8601 UTC
    source: str  # manual | csv | live
    actor: str = ""


@dataclass
class Mark:
    symbol: str
    price: float
    currency: str
    as_of: datetime
    source: str
    actor: str = ""


@dataclass
class PortfolioAuditEvent:
    ts: datetime
    action: str
    actor: str
    surface: str
    account_id: str = ""
    draft_id: str = ""
    event_id: str = ""
    version: int = 1
    idempotency_key: str = ""
    applied: bool = True
    detail: str = ""


# --------------------------------------------------------- schema <-> row


def _account_to_row(a: PortfolioAccount) -> PortfolioAccountRow:
    return PortfolioAccountRow(
        id=a.id,
        name=a.name,
        provider=a.provider.value,
        market_scope=a.market_scope.value,
        account_type=a.account_type.value,
        base_currency=a.base_currency,
        include_in_risk=a.include_in_risk,
        note=a.note,
        created_at=_to_iso(a.created_at),
        updated_at=_to_iso(a.updated_at),
    )


def _account_from_row(r: PortfolioAccountRow) -> PortfolioAccount:
    return PortfolioAccount(
        id=r.id,
        name=r.name,
        provider=ProviderKind(r.provider),
        market_scope=MarketScope(r.market_scope),
        account_type=AccountType(r.account_type),
        base_currency=r.base_currency,
        include_in_risk=r.include_in_risk,
        note=r.note,
        created_at=_from_iso(r.created_at),
        updated_at=_from_iso(r.updated_at),
    )


def _event_to_row(e: PortfolioEvent) -> PortfolioEventRow:
    return PortfolioEventRow(
        id=e.id,
        account_id=e.account_id,
        event_type=e.event_type.value,
        symbol=e.symbol,
        market=e.market.value if e.market is not None else None,
        currency=e.currency,
        qty=e.qty,
        price=e.price,
        commission=e.commission,
        amount=e.amount,
        occurred_at=_to_iso(e.occurred_at),
        settlement_date=_to_date(e.settlement_date),
        source=e.source.value,
        external_id=e.external_id,
        idempotency_key=e.idempotency_key,
        reverses_event_id=e.reverses_event_id,
        actor=e.actor,
        surface=e.surface,
        note=e.note,
        created_at=_to_iso(e.created_at),
    )


def _event_from_row(r: PortfolioEventRow) -> PortfolioEvent:
    return PortfolioEvent(
        id=r.id,
        account_id=r.account_id,
        event_type=EventType(r.event_type),
        symbol=r.symbol,
        market=MarketScope(r.market) if r.market is not None else None,
        currency=r.currency,
        qty=r.qty,
        price=r.price,
        commission=r.commission,
        amount=r.amount,
        occurred_at=_from_iso(r.occurred_at),
        settlement_date=_from_date(r.settlement_date),
        source=EventSource(r.source),
        external_id=r.external_id,
        idempotency_key=r.idempotency_key,
        reverses_event_id=r.reverses_event_id,
        actor=r.actor,
        surface=r.surface,
        note=r.note,
        created_at=_from_iso(r.created_at),
    )


def _draft_to_row(d: PortfolioDraft) -> PortfolioDraftRow:
    return PortfolioDraftRow(
        id=d.id,
        account_id=d.account_id,
        event_type=d.event_type.value,
        symbol=d.symbol,
        market=d.market.value if d.market is not None else None,
        currency=d.currency,
        qty=d.qty,
        price=d.price,
        commission=d.commission,
        amount=d.amount,
        occurred_at=_to_iso(d.occurred_at) if d.occurred_at is not None else None,
        settlement_date=_to_date(d.settlement_date),
        source=d.source.value,
        external_id=d.external_id,
        reverses_event_id=d.reverses_event_id,
        note=d.note,
        status=d.status.value,
        version=d.version,
        original_text=d.original_text,
        missing=json.dumps(d.missing),
        ambiguities=json.dumps(d.ambiguities),
        created_by=d.created_by,
        created_surface=d.created_surface,
        confirmed_by=d.confirmed_by,
        confirmed_at=_to_iso(d.confirmed_at) if d.confirmed_at is not None else None,
        created_at=_to_iso(d.created_at),
        updated_at=_to_iso(d.updated_at),
    )


def _draft_from_row(r: PortfolioDraftRow) -> PortfolioDraft:
    return PortfolioDraft(
        id=r.id,
        account_id=r.account_id,
        event_type=EventType(r.event_type),
        symbol=r.symbol,
        market=MarketScope(r.market) if r.market is not None else None,
        currency=r.currency,
        qty=r.qty,
        price=r.price,
        commission=r.commission,
        amount=r.amount,
        occurred_at=_from_iso(r.occurred_at) if r.occurred_at else None,
        settlement_date=_from_date(r.settlement_date),
        source=EventSource(r.source),
        external_id=r.external_id,
        reverses_event_id=r.reverses_event_id,
        note=r.note,
        status=DraftStatus(r.status),
        version=r.version,
        original_text=r.original_text,
        missing=json.loads(r.missing),
        ambiguities=json.loads(r.ambiguities),
        created_by=r.created_by,
        created_surface=r.created_surface,
        confirmed_by=r.confirmed_by,
        confirmed_at=_from_iso(r.confirmed_at) if r.confirmed_at else None,
        created_at=_from_iso(r.created_at),
        updated_at=_from_iso(r.updated_at),
    )


def _audit_to_row(e: PortfolioAuditEvent) -> PortfolioAuditRow:
    return PortfolioAuditRow(
        ts=_to_iso(e.ts),
        account_id=e.account_id,
        draft_id=e.draft_id,
        event_id=e.event_id,
        action=e.action,
        actor=e.actor,
        surface=e.surface,
        version=e.version,
        idempotency_key=e.idempotency_key,
        applied=e.applied,
        detail=e.detail,
    )


def _audit_from_row(r: PortfolioAuditRow) -> PortfolioAuditEvent:
    return PortfolioAuditEvent(
        ts=_from_iso(r.ts),
        action=r.action,
        actor=r.actor,
        surface=r.surface,
        account_id=r.account_id,
        draft_id=r.draft_id,
        event_id=r.event_id,
        version=r.version,
        idempotency_key=r.idempotency_key,
        applied=r.applied,
        detail=r.detail,
    )


# ------------------------------------------------------------------ journal


class PortfolioJournal:
    """Append-only multi-account portfolio store (Loop.md §7 P0.9).

    One instance per database URL. Accounts are mutable config; events are
    immutable and current holdings are DERIVED from them (rebuildable), never
    read off a mutable quantity column.
    """

    def __init__(self, url: str = "sqlite:///portfolio.db") -> None:
        self._engine = create_engine(url)
        SQLModel.metadata.create_all(self._engine)
        self._migrate_add_missing_columns()
        logger.info("portfolio journal ready", extra={"url": url})

    def _migrate_add_missing_columns(self) -> None:
        """Additive schema catch-up for existing DBs (no migration framework —
        ``create_all`` only CREATES tables, never ALTERs them). A model column
        added after a table was first created (e.g. ``reverses_event_id`` on
        ``portfolio_drafts``) is missing from the live DB, so writes fail with
        'no such column'. Add any nullable model columns the table lacks. Only
        ADDs columns (never drops/retypes) — append-only-safe; runs once at init.
        """
        insp = sa_inspect(self._engine)
        for table in SQLModel.metadata.tables.values():
            if not insp.has_table(table.name):
                continue
            existing = {c["name"] for c in insp.get_columns(table.name)}
            for col in table.columns:
                if col.name in existing or col.primary_key:
                    continue
                # SQLite ADD COLUMN can't add a NOT NULL column without a
                # constant default; only auto-add nullable columns (all the
                # optional model fields are), else surface the gap loudly.
                if not col.nullable and col.default is None and col.server_default is None:
                    logger.error("cannot auto-add NOT NULL column",
                                 extra={"table": table.name, "column": col.name})
                    continue
                coltype = col.type.compile(self._engine.dialect)
                with self._engine.begin() as conn:
                    conn.execute(text(
                        f'ALTER TABLE "{table.name}" ADD COLUMN "{col.name}" {coltype}'))
                logger.warning("added missing column (schema catch-up)",
                               extra={"table": table.name, "column": col.name})

    # ----------------------------------------------------------- accounts

    def create_account(
        self,
        *,
        name: str,
        market_scope: MarketScope | str,
        base_currency: str,
        provider: ProviderKind | str = ProviderKind.MANUAL,
        account_type: AccountType | str = AccountType.CASH,
        include_in_risk: bool = True,
        note: str = "",
    ) -> PortfolioAccount:
        account = PortfolioAccount(
            name=name,
            market_scope=MarketScope(market_scope),
            base_currency=base_currency,
            provider=ProviderKind(provider),
            account_type=AccountType(account_type),
            include_in_risk=include_in_risk,
            note=note,
        )
        with Session(self._engine) as session:
            session.add(_account_to_row(account))
            session.commit()
        return account

    def list_accounts(self) -> list[PortfolioAccount]:
        with Session(self._engine) as session:
            rows = session.exec(select(PortfolioAccountRow)).all()
        out = [_account_from_row(r) for r in rows]
        out.sort(key=lambda a: a.created_at)
        return out

    def get_account(self, account_id: str) -> Optional[PortfolioAccount]:
        with Session(self._engine) as session:
            row = session.get(PortfolioAccountRow, account_id)
        return _account_from_row(row) if row is not None else None

    def update_account(
        self,
        account_id: str,
        *,
        name: Optional[str] = None,
        include_in_risk: Optional[bool] = None,
        note: Optional[str] = None,
        account_type: Optional[AccountType | str] = None,
        now: Optional[datetime] = None,
    ) -> PortfolioAccount:
        """Mutate account CONFIG (not history). Bumps ``updated_at``."""
        with Session(self._engine) as session:
            row = session.get(PortfolioAccountRow, account_id)
            if row is None:
                raise ValueError(f"unknown account id: {account_id}")
            if name is not None:
                row.name = name
            if include_in_risk is not None:
                row.include_in_risk = include_in_risk
            if note is not None:
                row.note = note
            if account_type is not None:
                row.account_type = AccountType(account_type).value
            row.updated_at = _to_iso(now or datetime.now(timezone.utc))
            session.add(row)
            session.commit()
            session.refresh(row)
            return _account_from_row(row)

    # ------------------------------------------------------------- events

    def append_event(self, event: PortfolioEvent) -> tuple[PortfolioEvent, bool]:
        """Append one immutable event. Returns ``(event, created)``.

        Idempotent: an existing event with the same ``(account_id,
        idempotency_key)`` — or the same ``(account_id, external_id)`` when an
        external id is set — is returned unchanged with ``created=False`` (so a
        replayed commit or a duplicate broker execution never double-counts,
        P0.9 backlog). The account must exist.
        """
        with Session(self._engine) as session:
            if session.get(PortfolioAccountRow, event.account_id) is None:
                raise ValueError(f"unknown account id: {event.account_id}")

            existing = session.exec(
                select(PortfolioEventRow).where(
                    PortfolioEventRow.account_id == event.account_id,
                    PortfolioEventRow.idempotency_key == event.idempotency_key,
                )
            ).first()
            if existing is None and event.external_id is not None:
                existing = session.exec(
                    select(PortfolioEventRow).where(
                        PortfolioEventRow.account_id == event.account_id,
                        PortfolioEventRow.external_id == event.external_id,
                    )
                ).first()
            if existing is not None:
                return _event_from_row(existing), False

            # Guard compensating events: the reversed event must exist and
            # belong to the same account (append-only integrity).
            if event.reverses_event_id is not None:
                target = session.get(PortfolioEventRow, event.reverses_event_id)
                if target is None or target.account_id != event.account_id:
                    raise ValueError(
                        f"reverses_event_id {event.reverses_event_id} not found "
                        f"in account {event.account_id}"
                    )

            session.add(_event_to_row(event))
            session.commit()
        return event, True

    def get_events(
        self,
        account_id: Optional[str] = None,
        symbol: Optional[str] = None,
    ) -> list[PortfolioEvent]:
        with Session(self._engine) as session:
            stmt = select(PortfolioEventRow)
            if account_id is not None:
                stmt = stmt.where(PortfolioEventRow.account_id == account_id)
            if symbol is not None:
                stmt = stmt.where(PortfolioEventRow.symbol == symbol.strip().upper())
            rows = session.exec(stmt).all()
        out = [_event_from_row(r) for r in rows]
        out.sort(key=lambda e: (e.occurred_at, e.created_at, e.id))
        return out

    def get_event(self, event_id: str) -> Optional[PortfolioEvent]:
        with Session(self._engine) as session:
            row = session.get(PortfolioEventRow, event_id)
        return _event_from_row(row) if row is not None else None

    # ------------------------------------------------------------- drafts

    def save_draft(self, draft: PortfolioDraft) -> PortfolioDraft:
        """Upsert a draft by id (drafts are mutable until terminal)."""
        with Session(self._engine) as session:
            existing = session.get(PortfolioDraftRow, draft.id)
            row = _draft_to_row(draft)
            if existing is not None:
                session.delete(existing)
                session.flush()
            session.add(row)
            session.commit()
        return draft

    def get_draft(self, draft_id: str) -> Optional[PortfolioDraft]:
        with Session(self._engine) as session:
            row = session.get(PortfolioDraftRow, draft_id)
        return _draft_from_row(row) if row is not None else None

    def list_drafts(
        self,
        account_id: Optional[str] = None,
        status: Optional[DraftStatus | str] = None,
    ) -> list[PortfolioDraft]:
        with Session(self._engine) as session:
            stmt = select(PortfolioDraftRow)
            if account_id is not None:
                stmt = stmt.where(PortfolioDraftRow.account_id == account_id)
            if status is not None:
                stmt = stmt.where(PortfolioDraftRow.status == DraftStatus(status).value)
            rows = session.exec(stmt).all()
        out = [_draft_from_row(r) for r in rows]
        out.sort(key=lambda d: d.created_at)
        return out

    # -------------------------------------------------------------- audit

    def record_audit(self, event: PortfolioAuditEvent) -> None:
        with Session(self._engine) as session:
            session.add(_audit_to_row(event))
            session.commit()

    def get_audit(
        self,
        account_id: Optional[str] = None,
        draft_id: Optional[str] = None,
        idempotency_key: Optional[str] = None,
    ) -> list[PortfolioAuditEvent]:
        with Session(self._engine) as session:
            stmt = select(PortfolioAuditRow)
            if account_id is not None:
                stmt = stmt.where(PortfolioAuditRow.account_id == account_id)
            if draft_id is not None:
                stmt = stmt.where(PortfolioAuditRow.draft_id == draft_id)
            if idempotency_key is not None:
                stmt = stmt.where(PortfolioAuditRow.idempotency_key == idempotency_key)
            rows = session.exec(stmt).all()
        out = [_audit_from_row(r) for r in rows]
        out.sort(key=lambda e: e.ts)
        return out

    # --------------------------------------------------------------- marks

    def set_mark(
        self, symbol: str, price: float, *, currency: str, source: str,
        actor: str = "", as_of: Optional[datetime] = None,
    ) -> Mark:
        """Upsert the latest price/NAV for a symbol (valuation input only)."""
        symbol = symbol.strip().upper()
        stamp = as_of or datetime.now(timezone.utc)
        with Session(self._engine) as session:
            row = session.get(PortfolioMarkRow, symbol)
            if row is None:
                row = PortfolioMarkRow(symbol=symbol, price=price, currency=currency,
                                       as_of=_to_iso(stamp), source=source, actor=actor)
            else:
                row.price = price
                row.currency = currency
                row.as_of = _to_iso(stamp)
                row.source = source
                row.actor = actor
            session.add(row)
            session.commit()
        return Mark(symbol, price, currency, stamp, source, actor)

    def get_marks(self) -> dict[str, Mark]:
        with Session(self._engine) as session:
            rows = session.exec(select(PortfolioMarkRow)).all()
        return {
            r.symbol: Mark(r.symbol, r.price, r.currency, _from_iso(r.as_of),
                           r.source, r.actor)
            for r in rows
        }

    def get_mark(self, symbol: str) -> Optional[Mark]:
        return self.get_marks().get(symbol.strip().upper())

    # ---------------------------------------------------------- holdings

    def holdings(self, account_id: str) -> AccountHoldings:
        """Derive current holdings + cash for one account from its events."""
        return derive_holdings(account_id, self.get_events(account_id))

    def aggregate(self, *, include_in_risk_only: bool = False):
        """Combined, source-tagged holdings across accounts (Loop.md P0.9)."""
        accounts = self.list_accounts()
        if include_in_risk_only:
            accounts = [a for a in accounts if a.include_in_risk]
        items = [(a, self.holdings(a.id)) for a in accounts]
        return aggregate_holdings(items)
