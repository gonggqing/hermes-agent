"""User-set display-name overrides for held symbols (finance-bot DM).

A holding's name normally comes from the curated ``instrument_names`` map or the
import's event note. This lets the USER fix a name from Telegram ("159518 改名
标普油气ETF嘉实") without a code deploy — a purely cosmetic override that takes
HIGHEST precedence in ``_symbol_names`` and never touches the append-only event
or its cost basis. Own DB file + own MetaData (like the knowledge/brief stores),
so it can never collide with the trading-ledger tables.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import MetaData
from sqlmodel import Field, Session, SQLModel, create_engine, select

from swing_trader.trade_parse import find_symbol

__all__ = ["NameOverrideStore", "SymbolNameOverrideRow", "parse_rename"]

_RENAME_KW = re.compile(r"(改名|重命名|改个名字|名字改成|名称改成|改名字|rename|叫做|叫)")
_SYM_TOKEN = re.compile(
    r"\d{6}(?:\.(?:SS|SZ))?|\d{4,5}\.HK|[A-Za-z]{2,5}(?:\.[A-Za-z]{2,4})?"
)


def parse_rename(text: str) -> Optional[tuple[str, str]]:
    """Parse a rename request → (normalized symbol, new name), or None.

    Handles "159518 改名 X", "改名 159518 X", "把 159518 名字改成 X" — the name is
    whatever follows BOTH the symbol and the rename keyword."""
    if not text:
        return None
    km = _RENAME_KW.search(text)
    if km is None:
        return None
    sym = find_symbol(text)
    if sym is None:
        return None
    sm = _SYM_TOKEN.search(text)
    start = max(km.end(), sm.end() if sm else 0)
    name = text[start:].strip(" \t:：为成到叫的“”\"'")
    if not name:
        return None
    return sym, name

NAME_OVERRIDE_METADATA = MetaData()


class _NameOverrideTable(SQLModel):
    metadata = NAME_OVERRIDE_METADATA


class SymbolNameOverrideRow(_NameOverrideTable, table=True):
    __tablename__ = "symbol_name_overrides"

    symbol: str = Field(primary_key=True)  # UPPERCASE
    name: str
    updated_at: str = ""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class NameOverrideStore:
    def __init__(self, url: str = "sqlite:///name_overrides.db") -> None:
        self._engine = create_engine(url)
        NAME_OVERRIDE_METADATA.create_all(self._engine)

    def set(self, symbol: str, name: str) -> None:
        """Upsert a display-name override. An empty ``name`` clears it (revert to
        the curated map / note)."""
        symbol = (symbol or "").strip().upper()
        name = (name or "").strip()
        if not symbol:
            return
        with Session(self._engine) as s:
            row = s.get(SymbolNameOverrideRow, symbol)
            if not name:
                if row is not None:
                    s.delete(row)
                    s.commit()
                return
            if row is None:
                row = SymbolNameOverrideRow(symbol=symbol, name=name, updated_at=_now_iso())
            else:
                row.name = name
                row.updated_at = _now_iso()
            s.add(row)
            s.commit()

    def get(self, symbol: str) -> Optional[str]:
        with Session(self._engine) as s:
            row = s.get(SymbolNameOverrideRow, (symbol or "").strip().upper())
            return row.name if row else None

    def all(self) -> dict[str, str]:
        with Session(self._engine) as s:
            return {r.symbol: r.name for r in s.exec(select(SymbolNameOverrideRow)).all()}
