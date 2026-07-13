"""Reviewed CSV import for the Portfolio Journal (Loop.md P0.9 backlog #4).

Bootstrap a manual/other-broker portfolio from a CSV, then (later) IBKR
Activity/Flex exports. Import is a THREE-step, human-gated flow:

1. :func:`parse_csv` — parse + per-row validate + dedup against what's already
   in the journal (by external id, else a deterministic content hash), with NO
   writes. This backs the preview screen.
2. the human reviews the preview;
3. :func:`commit_csv` — append only the valid, non-duplicate rows as ordinary
   append-only :class:`PortfolioEvent`s (idempotent).

Imports MUST NOT create system-generated candidates/orders/fills (boundary #1)
— they only ever call the Portfolio Journal. Pure/offline; no network.
"""

from __future__ import annotations

import csv
import hashlib
import io
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from swing_trader.log import get_logger
from swing_trader.portfolio import (
    EventSource,
    EventType,
    MarketScope,
    PortfolioEvent,
)
from swing_trader.portfolio_journal import PortfolioJournal

logger = get_logger(__name__)

__all__ = ["CsvCommitResult", "CsvPreview", "CsvRow", "commit_csv", "parse_csv"]

#: Canonical import columns. ``date`` + ``event_type`` are required; the rest
#: depend on the event (validated per row against PortfolioEvent's own rules).
REQUIRED_COLUMNS: tuple[str, ...] = ("date", "event_type")
KNOWN_COLUMNS: tuple[str, ...] = (
    "date", "event_type", "symbol", "market", "currency", "qty", "price",
    "commission", "amount", "external_id", "note",
)


@dataclass
class CsvRow:
    line: int  # 1-based source line (after the header)
    raw: dict
    fields: dict = field(default_factory=dict)  # parsed proposed-event fields
    idempotency_key: str = ""
    errors: list[str] = field(default_factory=list)
    duplicate: bool = False

    @property
    def ok(self) -> bool:
        return not self.errors and not self.duplicate


@dataclass
class CsvPreview:
    rows: list[CsvRow] = field(default_factory=list)
    header_error: Optional[str] = None

    @property
    def n_valid(self) -> int:
        return sum(1 for r in self.rows if r.ok)

    @property
    def n_invalid(self) -> int:
        return sum(1 for r in self.rows if r.errors)

    @property
    def n_duplicate(self) -> int:
        return sum(1 for r in self.rows if r.duplicate and not r.errors)

    @property
    def committable(self) -> bool:
        return self.header_error is None and self.n_valid > 0


@dataclass
class CsvCommitResult:
    n_committed: int = 0
    n_duplicate: int = 0
    n_skipped: int = 0  # invalid rows never committed
    event_ids: list[str] = field(default_factory=list)


def _parse_dt(raw: str) -> datetime:
    """Accept an ISO date (``2026-07-12``) or full ISO datetime; naive → UTC."""
    s = raw.strip()
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _f(raw: dict, key: str) -> Optional[float]:
    v = (raw.get(key) or "").strip()
    return float(v) if v else None


def _row_idempotency_key(account_id: str, raw: dict) -> str:
    ext = (raw.get("external_id") or "").strip()
    if ext:
        return f"csv:{ext}"
    basis = "|".join(
        (raw.get(k) or "").strip()
        for k in ("date", "event_type", "symbol", "qty", "price", "amount")
    )
    digest = hashlib.sha1(f"{account_id}|{basis}".encode()).hexdigest()[:16]
    return f"csv:{digest}"


def _build_fields(raw: dict) -> dict:
    et = EventType((raw.get("event_type") or "").strip().lower())
    market_raw = (raw.get("market") or "").strip()
    return {
        "event_type": et,
        "symbol": (raw.get("symbol") or "").strip() or None,
        "market": MarketScope(market_raw.upper()) if market_raw else None,
        "currency": (raw.get("currency") or "").strip() or None,
        "qty": _f(raw, "qty") or 0.0,
        "price": _f(raw, "price"),
        "commission": _f(raw, "commission"),
        "amount": _f(raw, "amount"),
        "occurred_at": _parse_dt(raw["date"]),
        "external_id": (raw.get("external_id") or "").strip() or None,
        "note": (raw.get("note") or "").strip(),
    }


def parse_csv(
    text: str, account_id: str, journal: Optional[PortfolioJournal] = None
) -> CsvPreview:
    """Parse + validate + dedup (no writes). If ``journal`` is given, rows whose
    idempotency key / external id already exist are flagged ``duplicate``."""
    reader = csv.DictReader(io.StringIO(text))
    header = [h.strip().lower() for h in (reader.fieldnames or [])]
    missing = [c for c in REQUIRED_COLUMNS if c not in header]
    if missing:
        return CsvPreview(header_error=f"missing required column(s): {', '.join(missing)}")

    seen_keys: set[str] = set()
    existing: set[str] = set()
    existing_ext: set[str] = set()
    if journal is not None:
        for e in journal.get_events(account_id):
            existing.add(e.idempotency_key)
            if e.external_id:
                existing_ext.add(e.external_id)

    preview = CsvPreview()
    for i, raw in enumerate(reader, start=1):
        raw = {(k or "").strip().lower(): v for k, v in raw.items()}
        row = CsvRow(line=i, raw=raw)
        try:
            fields = _build_fields(raw)
            # Validate by constructing a PortfolioEvent (its validators fire).
            PortfolioEvent(account_id=account_id, source=EventSource.CSV,
                           idempotency_key="preview", actor="csv", surface="csv", **fields)
            row.fields = fields
        except Exception as exc:  # noqa: BLE001 — collect per-row parse/validation errors
            row.errors.append(str(exc).splitlines()[0][:200])
            preview.rows.append(row)
            continue
        key = _row_idempotency_key(account_id, raw)
        row.idempotency_key = key
        ext = fields.get("external_id")
        if key in seen_keys or key in existing or (ext and ext in existing_ext):
            row.duplicate = True  # dupe within the file or vs the journal
        seen_keys.add(key)
        preview.rows.append(row)
    return preview


def commit_csv(
    journal: PortfolioJournal, account_id: str, text: str, *, actor: str, surface: str
) -> CsvCommitResult:
    """Append the valid, non-duplicate rows as append-only events (idempotent).
    Invalid rows are skipped; duplicates are counted, never re-appended."""
    if journal.get_account(account_id) is None:
        raise ValueError(f"unknown account id: {account_id}")
    preview = parse_csv(text, account_id, journal)
    if preview.header_error:
        raise ValueError(preview.header_error)

    result = CsvCommitResult()
    now = datetime.now(timezone.utc)
    for row in preview.rows:
        if row.errors:
            result.n_skipped += 1
            continue
        if row.duplicate:
            result.n_duplicate += 1
            continue
        event = PortfolioEvent(
            account_id=account_id, source=EventSource.CSV,
            idempotency_key=row.idempotency_key, actor=actor, surface=surface,
            created_at=now, **row.fields,
        )
        stored, created = journal.append_event(event)
        if created:
            result.n_committed += 1
            result.event_ids.append(stored.id)
        else:
            result.n_duplicate += 1
    logger.info("csv import committed", extra={
        "account_id": account_id, "committed": result.n_committed,
        "duplicate": result.n_duplicate, "skipped": result.n_skipped})
    return result
