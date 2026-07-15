"""Durable, research-only personal watchlists (Loop.md Phase 0.96).

These tables deliberately do not feed ``watchlist.UNIVERSE``.  A symbol added
here may be quoted and analysed by the Finance portal, but it never becomes a
trading candidate and grants no order authority.
"""

from __future__ import annotations

import threading
from datetime import datetime, timezone
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field as PydanticField
from sqlmodel import Field, Session, SQLModel, create_engine, select

__all__ = ["ResearchWatchlist", "ResearchWatchlistMember", "ResearchWatchlistStore"]


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


class ResearchWatchlistMember(BaseModel):
    model_config = ConfigDict(frozen=True)

    symbol: str
    display_name: str
    market: str | None = None
    exchange: str | None = None
    currency: str | None = None
    security_type: str | None = None
    position: int = 0
    created_at: datetime


class ResearchWatchlist(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    name: str
    position: int = 0
    members: list[ResearchWatchlistMember] = PydanticField(default_factory=list)
    created_at: datetime
    updated_at: datetime


class ResearchWatchlistRow(SQLModel, table=True):
    __tablename__ = "research_watchlists"

    id: str = Field(primary_key=True)
    name: str
    position: int = Field(default=0, index=True)
    created_at: str
    updated_at: str


class ResearchWatchlistMemberRow(SQLModel, table=True):
    __tablename__ = "research_watchlist_members"

    id: str = Field(primary_key=True)
    group_id: str = Field(index=True)
    symbol: str = Field(index=True)
    display_name: str
    market: str | None = None
    exchange: str | None = None
    currency: str | None = None
    security_type: str | None = None
    position: int = Field(default=0, index=True)
    created_at: str


class ResearchWatchlistStore:
    """Small SQLite repository for user-defined research groups."""

    def __init__(self, url: str) -> None:
        self._engine = create_engine(
            url,
            connect_args={"check_same_thread": False} if url.startswith("sqlite") else {},
        )
        self._write_lock = threading.RLock()
        SQLModel.metadata.create_all(
            self._engine,
            tables=[ResearchWatchlistRow.__table__, ResearchWatchlistMemberRow.__table__],
        )

    @staticmethod
    def _member(row: ResearchWatchlistMemberRow) -> ResearchWatchlistMember:
        return ResearchWatchlistMember(
            symbol=row.symbol,
            display_name=row.display_name,
            market=row.market,
            exchange=row.exchange,
            currency=row.currency,
            security_type=row.security_type,
            position=row.position,
            created_at=datetime.fromisoformat(row.created_at),
        )

    def _group(self, session: Session, row: ResearchWatchlistRow) -> ResearchWatchlist:
        members = session.exec(
            select(ResearchWatchlistMemberRow).where(ResearchWatchlistMemberRow.group_id == row.id)
        ).all()
        members.sort(key=lambda item: (item.position, item.created_at, item.symbol))
        return ResearchWatchlist(
            id=row.id,
            name=row.name,
            position=row.position,
            members=[self._member(item) for item in members],
            created_at=datetime.fromisoformat(row.created_at),
            updated_at=datetime.fromisoformat(row.updated_at),
        )

    def list_groups(self) -> list[ResearchWatchlist]:
        with Session(self._engine) as session:
            rows = session.exec(select(ResearchWatchlistRow)).all()
            rows.sort(key=lambda item: (item.position, item.created_at, item.id))
            return [self._group(session, row) for row in rows]

    def get_group(self, group_id: str) -> ResearchWatchlist | None:
        with Session(self._engine) as session:
            row = session.get(ResearchWatchlistRow, group_id)
            return self._group(session, row) if row is not None else None

    def create_group(self, name: str) -> ResearchWatchlist:
        clean = " ".join(name.split())
        if not clean:
            raise ValueError("watchlist name is required")
        now = _now()
        with self._write_lock, Session(self._engine) as session:
            positions = session.exec(select(ResearchWatchlistRow.position)).all()
            row = ResearchWatchlistRow(
                id=uuid4().hex,
                name=clean[:80],
                position=(max(positions) + 1) if positions else 0,
                created_at=_iso(now),
                updated_at=_iso(now),
            )
            session.add(row)
            session.commit()
            session.refresh(row)
            return self._group(session, row)

    def rename_group(self, group_id: str, name: str) -> ResearchWatchlist:
        clean = " ".join(name.split())
        if not clean:
            raise ValueError("watchlist name is required")
        with self._write_lock, Session(self._engine) as session:
            row = session.get(ResearchWatchlistRow, group_id)
            if row is None:
                raise KeyError(group_id)
            row.name = clean[:80]
            row.updated_at = _iso(_now())
            session.add(row)
            session.commit()
            session.refresh(row)
            return self._group(session, row)

    def delete_group(self, group_id: str) -> bool:
        with self._write_lock, Session(self._engine) as session:
            row = session.get(ResearchWatchlistRow, group_id)
            if row is None:
                return False
            members = session.exec(
                select(ResearchWatchlistMemberRow).where(
                    ResearchWatchlistMemberRow.group_id == group_id
                )
            ).all()
            for member in members:
                session.delete(member)
            session.delete(row)
            session.commit()
            return True

    def add_member(
        self,
        group_id: str,
        *,
        symbol: str,
        display_name: str = "",
        market: str | None = None,
        exchange: str | None = None,
        currency: str | None = None,
        security_type: str | None = None,
    ) -> ResearchWatchlist:
        canonical = symbol.strip().upper()
        if not canonical:
            raise ValueError("symbol is required")
        with self._write_lock, Session(self._engine) as session:
            group = session.get(ResearchWatchlistRow, group_id)
            if group is None:
                raise KeyError(group_id)
            existing = session.exec(
                select(ResearchWatchlistMemberRow).where(
                    ResearchWatchlistMemberRow.group_id == group_id,
                    ResearchWatchlistMemberRow.symbol == canonical,
                )
            ).first()
            if existing is not None:
                return self._group(session, group)
            members = session.exec(
                select(ResearchWatchlistMemberRow).where(
                    ResearchWatchlistMemberRow.group_id == group_id
                )
            ).all()
            now = _now()
            session.add(
                ResearchWatchlistMemberRow(
                    id=uuid4().hex,
                    group_id=group_id,
                    symbol=canonical,
                    display_name=(display_name.strip() or canonical)[:160],
                    market=market.upper() if market else None,
                    exchange=exchange,
                    currency=currency,
                    security_type=security_type,
                    position=max((item.position for item in members), default=-1) + 1,
                    created_at=_iso(now),
                )
            )
            group.updated_at = _iso(now)
            session.add(group)
            session.commit()
            session.refresh(group)
            return self._group(session, group)

    def remove_member(self, group_id: str, symbol: str) -> ResearchWatchlist:
        canonical = symbol.strip().upper()
        with self._write_lock, Session(self._engine) as session:
            group = session.get(ResearchWatchlistRow, group_id)
            if group is None:
                raise KeyError(group_id)
            row = session.exec(
                select(ResearchWatchlistMemberRow).where(
                    ResearchWatchlistMemberRow.group_id == group_id,
                    ResearchWatchlistMemberRow.symbol == canonical,
                )
            ).first()
            if row is not None:
                session.delete(row)
                group.updated_at = _iso(_now())
                session.add(group)
                session.commit()
                session.refresh(group)
            return self._group(session, group)
