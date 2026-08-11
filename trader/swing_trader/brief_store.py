"""Durable per-market research-brief snapshots (Loop.md P0.9+).

The rendered daily brief otherwise lives ONLY in ``FinanceRuntime.latest_briefs``
(in-memory — lost on restart, no history). This archives each published brief to
its OWN SQLite DB so there is a queryable HISTORY: the agent/bot can read "what
did CN look like on 07-13", and the desk keeps a record across container
restarts. Underlying NEWS is already persisted by the knowledge store; this adds
the rendered brief on top.

Private ``MetaData`` (same idiom as the knowledge store): the snapshot table is
NOT registered on the global ``SQLModel.metadata``, so ``Ledger.create_all`` can
never create it in the trading-ledger DB, and vice versa — it lives in its own
database file.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import MetaData
from sqlmodel import Field, Session, SQLModel, create_engine, select

__all__ = ["BriefSnapshotRow", "BriefStore"]

BRIEF_METADATA = MetaData()


class _BriefTable(SQLModel):
    metadata = BRIEF_METADATA


class BriefSnapshotRow(_BriefTable, table=True):
    __tablename__ = "research_brief_snapshots"

    id: str = Field(primary_key=True)
    market: str = Field(index=True)          # cn / kr / us
    trading_date: str = Field(index=True)    # YYYY-MM-DD
    generated_at: str = Field(index=True)    # ISO-8601 UTC (brief.as_of)
    n_movers: int = 0
    n_signals: int = 0
    payload_json: str = ""                   # the full brief dump (UTF-8 JSON)
    created_at: str = ""                     # ISO-8601 UTC (row write time)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _count_movers(brief: dict) -> int:
    mv = brief.get("movers")
    if isinstance(mv, dict):
        return len(mv.get("top", []) or [])
    if isinstance(mv, list):
        return len(mv)
    return 0


def _count_signals(brief: dict) -> int:
    sig = brief.get("signals_today")
    if isinstance(sig, list):
        return len(sig)
    if isinstance(sig, dict):
        return len(sig)
    return 0


def _publication_identity(brief: dict) -> tuple[str, str] | None:
    narrative = brief.get("narrative")
    if not isinstance(narrative, dict):
        return None
    edition = str(narrative.get("edition") or "")
    evidence_hash = str(narrative.get("evidence_hash") or "")
    if not edition or not evidence_hash:
        return None
    return edition, evidence_hash


class BriefStore:
    """Append-only archive of rendered market briefs, keyed by market + time."""

    def __init__(self, url: str = "sqlite:///briefs.db") -> None:
        self._engine = create_engine(url)
        BRIEF_METADATA.create_all(self._engine)

    def save(self, market: str, brief: dict) -> str:
        """Persist one brief snapshot; returns its id. Never raises on a
        malformed brief — the caller (a research publish) must not break."""
        market = (market or "").strip().lower()
        trading_date = str(brief.get("trading_date") or "")
        identity = _publication_identity(brief)
        if identity is not None:
            # Restart/manual retries of the same canonical publication are
            # idempotent. A changed evidence hash remains an append-only
            # revision even within the same morning/evening edition.
            with Session(self._engine) as s:
                rows = s.exec(
                    select(BriefSnapshotRow)
                    .where(BriefSnapshotRow.market == market)
                    .where(BriefSnapshotRow.trading_date == trading_date)
                    .order_by(BriefSnapshotRow.created_at.desc())
                ).all()
                for existing in rows:
                    try:
                        payload = json.loads(existing.payload_json)
                    except (TypeError, ValueError):
                        continue
                    if _publication_identity(payload) == identity:
                        return existing.id
        sid = uuid.uuid4().hex
        row = BriefSnapshotRow(
            id=sid,
            market=market,
            trading_date=trading_date,
            generated_at=str(
                brief.get("as_of") or brief.get("generated_at") or _now_iso()
            ),
            n_movers=_count_movers(brief),
            n_signals=_count_signals(brief),
            payload_json=json.dumps(brief, ensure_ascii=False, default=str),
            created_at=_now_iso(),
        )
        with Session(self._engine) as s:
            s.add(row)
            s.commit()
        return sid

    def list_snapshots(
        self, market: Optional[str] = None, limit: int = 20
    ) -> list[dict]:
        """Newest-first snapshot METADATA (no payload) for browsing history."""
        limit = max(1, min(int(limit), 200))
        with Session(self._engine) as s:
            q = select(BriefSnapshotRow)
            if market:
                q = q.where(BriefSnapshotRow.market == market.strip().lower())
            q = q.order_by(
                BriefSnapshotRow.generated_at.desc(),
                BriefSnapshotRow.created_at.desc(),
            ).limit(limit)
            return [
                {
                    "id": r.id, "market": r.market, "trading_date": r.trading_date,
                    "generated_at": r.generated_at, "n_movers": r.n_movers,
                    "n_signals": r.n_signals,
                }
                for r in s.exec(q).all()
            ]

    def get(self, snapshot_id: str) -> Optional[dict]:
        """Full brief payload for one snapshot id, or None."""
        with Session(self._engine) as s:
            row = s.get(BriefSnapshotRow, snapshot_id)
            return json.loads(row.payload_json) if row else None

    def prune_duplicates(self) -> int:
        """Collapse snapshots to one row per (market, trading_date, edition),
        keeping the NEWEST (same ordering as ``get_latest``). Edition comes from
        each payload's narrative; a missing narrative groups under ''. Two
        legitimate editions (morning/evening) on one day are both kept — only
        repeats of the SAME edition (e.g. from container restarts) collapse.
        Returns the number of rows deleted."""
        deleted = 0
        with Session(self._engine) as s:
            rows = list(s.exec(select(BriefSnapshotRow)).all())
            groups: dict[tuple[str, str, str], list[BriefSnapshotRow]] = {}
            for r in rows:
                edition = ""
                try:
                    narrative = json.loads(r.payload_json).get("narrative")
                    if isinstance(narrative, dict):
                        edition = str(narrative.get("edition") or "")
                except (ValueError, AttributeError):
                    pass
                groups.setdefault((r.market, r.trading_date, edition), []).append(r)
            for group in groups.values():
                if len(group) <= 1:
                    continue
                group.sort(
                    key=lambda r: (r.generated_at, r.created_at), reverse=True
                )
                for stale in group[1:]:  # keep newest, drop the rest
                    s.delete(stale)
                    deleted += 1
            if deleted:
                s.commit()
        return deleted

    def get_by_date(self, market: str, trading_date: str) -> Optional[dict]:
        """The latest snapshot for a market on a given trading date, or None."""
        with Session(self._engine) as s:
            q = (
                select(BriefSnapshotRow)
                .where(BriefSnapshotRow.market == market.strip().lower())
                .where(BriefSnapshotRow.trading_date == str(trading_date))
                .order_by(
                    BriefSnapshotRow.generated_at.desc(),
                    BriefSnapshotRow.created_at.desc(),
                )
                .limit(1)
            )
            row = s.exec(q).first()
            return json.loads(row.payload_json) if row else None

    def get_latest(self, market: str) -> Optional[dict]:
        """Newest archived full brief for ``market``, across all dates.

        This is the restart-recovery path for the Finance desk: rendered
        briefs remain useful immediately after a container rebuild while a
        fresh research run is pending.  The original ``as_of`` is preserved so
        consumers can still make an honest freshness decision.
        """
        with Session(self._engine) as s:
            q = (
                select(BriefSnapshotRow)
                .where(BriefSnapshotRow.market == market.strip().lower())
                .order_by(
                    BriefSnapshotRow.generated_at.desc(),
                    BriefSnapshotRow.created_at.desc(),
                )
                .limit(1)
            )
            row = s.exec(q).first()
            return json.loads(row.payload_json) if row else None

    def get_recent_distinct(
        self,
        market: str,
        *,
        before_generated_at: Optional[str] = None,
        limit: int = 6,
    ) -> list[dict]:
        """Newest-first prior publications with distinct evidence hashes.

        Old databases may contain repeated rows from intraday refreshes and
        restarts. The research writer must see actual thesis evolution, not six
        copies of the same prose, so duplicates and narrative-less rows are
        ignored without rewriting audit history.
        """

        limit = max(1, min(int(limit), 30))
        market = market.strip().lower()
        with Session(self._engine) as s:
            query = select(BriefSnapshotRow).where(
                BriefSnapshotRow.market == market
            )
            if before_generated_at:
                query = query.where(
                    BriefSnapshotRow.generated_at < str(before_generated_at)
                )
            rows = s.exec(
                query.order_by(
                    BriefSnapshotRow.generated_at.desc(),
                    BriefSnapshotRow.created_at.desc(),
                ).limit(500)
            ).all()

        found: list[dict] = []
        seen: set[str] = set()
        for row in rows:
            try:
                payload = json.loads(row.payload_json)
            except (TypeError, ValueError):
                continue
            identity = _publication_identity(payload)
            if identity is None:
                continue
            fingerprint = identity[1]
            if fingerprint in seen:
                continue
            seen.add(fingerprint)
            found.append(payload)
            if len(found) >= limit:
                break
        return found

    def iter_snapshots(self, limit: int = 10_000) -> list[tuple[str, str, dict]]:
        """Oldest-first full snapshots for idempotent derived-store backfills."""
        limit = max(1, min(int(limit), 100_000))
        with Session(self._engine) as s:
            rows = s.exec(
                select(BriefSnapshotRow)
                .order_by(
                    BriefSnapshotRow.generated_at.asc(),
                    BriefSnapshotRow.created_at.asc(),
                )
                .limit(limit)
            ).all()
            return [
                (row.id, row.market, json.loads(row.payload_json))
                for row in rows
            ]
