"""Korea (KRX) semiconductor research universe (Loop.md two-session extension).

A DELIBERATELY NARROW, semiconductor-only universe: Korea's memory/foundry
giants and a couple of key HBM-chain names. Rationale (human directive
2026-07-14): **KR semiconductor volatility/sentiment leads and transfers to the
CN semiconductor tape**, so a focused KR read (the giants' earnings + news +
price action) is worth a standalone research brief — but the rest of the KR
market is out of scope (only semis + those few giants).

Research-only (no orders), like the CN session. The universe is config-editable
via ``FINANCE_KR_SYMBOLS`` / ``Settings.kr_symbols``. yfinance symbols use the
``.KS`` (KOSPI) suffix; when the free feed has no data for a name it simply
returns no bars and is skipped (graceful degrade).

Reuses :class:`~swing_trader.watchlist.WatchlistItem` so the research brief's
mover/theme tagging works uniformly across the US, CN and KR universes.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional

from swing_trader.schemas import AiPhase, Role
from swing_trader.watchlist import WatchlistItem

__all__ = [
    "KR_UNIVERSE",
    "KR_INDEX_SYMBOLS",
    "KrWatchlist",
    "build_kr_watchlist",
]


def _mk(symbols: str, theme: str, phase: AiPhase, role: Role) -> list[WatchlistItem]:
    return [
        WatchlistItem(symbol=s, theme=theme, ai_phase=phase, role=role)
        for s in symbols.split()
    ]


#: Default KR semiconductor universe (NOT a buy list). Memory giants are the
#: sentiment drivers CN semis track; HBM-chain names carry the AI-memory theme.
KR_UNIVERSE: list[WatchlistItem] = [
    # --- Memory giants (the sentiment drivers → CN semis) ---
    *_mk("005930.KS", "kr-memory-giant", AiPhase.MEMORY, Role.CONVICTION),   # Samsung Electronics
    *_mk("000660.KS", "kr-memory-giant", AiPhase.MEMORY, Role.CONVICTION),   # SK Hynix
    # --- HBM / advanced-packaging chain (AI-memory demand read) ---
    *_mk("042700.KS", "kr-hbm-packaging", AiPhase.MEMORY, Role.ROTATION),    # Hanmi Semiconductor
    # --- Foundry / specialty (context) ---
    *_mk("000990.KS", "kr-foundry", AiPhase.INFRA, Role.ROTATION),           # DB HiTek
]

#: KOSPI as regime context (semis are a large KOSPI weight; no clean free
#: KR-semiconductor index exists on the feed).
KR_INDEX_SYMBOLS: tuple[str, ...] = ("^KS11",)


@dataclass(frozen=True)
class KrWatchlist:
    items: list[WatchlistItem]
    lookup: Callable[[str], Optional[WatchlistItem]]

    @property
    def symbols(self) -> list[str]:
        return [i.symbol for i in self.items]


def _norm(symbol: str) -> str:
    return symbol.strip().upper()


def build_kr_watchlist(override: str = "") -> KrWatchlist:
    """Resolve the KR universe, applying an optional comma-separated override.

    With no override the built-in :data:`KR_UNIVERSE` is used. An override
    restricts the universe to the listed symbols, preserving built-in theme/role
    tags for known symbols and tagging unknown ones ``kr-custom``.
    """
    known = {_norm(i.symbol): i for i in KR_UNIVERSE}
    if override.strip():
        items: list[WatchlistItem] = []
        for raw in override.split(","):
            sym = _norm(raw)
            if not sym:
                continue
            items.append(
                known.get(sym)
                or WatchlistItem(
                    symbol=sym, theme="kr-custom", ai_phase=AiPhase.NONE,
                    role=Role.ROTATION,
                )
            )
    else:
        items = list(KR_UNIVERSE)

    by_symbol = {_norm(i.symbol): i for i in items}

    def lookup(symbol: str) -> Optional[WatchlistItem]:
        return by_symbol.get(_norm(symbol))

    return KrWatchlist(items=items, lookup=lookup)
