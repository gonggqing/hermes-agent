"""Ports (Loop.md §5.1). The core is broker-agnostic: everything upstream of
an adapter talks only to these interfaces, and every adapter is mockable so
tests never hit the network (Loop.md §3).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from swing_trader.schemas import AccountSnapshot, Fill, Order, Position


@dataclass
class Quote:
    symbol: str
    ts: datetime
    last: float
    bid: Optional[float] = None
    ask: Optional[float] = None
    volume: Optional[float] = None


@dataclass
class Bar:
    symbol: str
    ts: datetime  # bar START time, timezone-aware
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass
class NewsItem:
    symbol: Optional[str]  # None = macro/market-wide
    ts: datetime
    headline: str
    source: str = ""
    url: str = ""
    sentiment: Optional[float] = None  # [-1, 1] once scored


@dataclass
class PlaceResult:
    accepted: bool
    order: Order
    reason: str = ""  # populated on rejection
    child_orders: list[Order] = field(default_factory=list)  # bracket legs


class BrokerInterface(ABC):
    """Loop.md §5.1. Implementations: PaperBroker (Phase 0), IBKRBroker (stub)."""

    @abstractmethod
    def get_account(self) -> AccountSnapshot: ...

    @abstractmethod
    def get_positions(self) -> list[Position]: ...

    @abstractmethod
    def place_order(self, order: Order) -> PlaceResult: ...

    @abstractmethod
    def cancel_order(self, order_id: str) -> bool: ...

    @abstractmethod
    def get_orders(self, active_only: bool = False) -> list[Order]: ...

    @abstractmethod
    def get_fills(self) -> list[Fill]: ...


class DataFeed(ABC):
    """Loop.md §5.1. Implementations: YFinanceFeed (now), IBKR/Polygon (stubs later)."""

    @abstractmethod
    def get_quote(self, symbol: str) -> Quote: ...

    @abstractmethod
    def get_bars(
        self, symbol: str, timeframe: str = "1d", limit: int = 100
    ) -> list[Bar]: ...

    @abstractmethod
    def get_news(self, symbol: Optional[str] = None, limit: int = 20) -> list[NewsItem]: ...
