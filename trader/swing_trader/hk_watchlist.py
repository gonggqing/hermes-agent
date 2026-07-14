"""Independent Hong Kong research universe (Phase 0.95)."""

from __future__ import annotations

from swing_trader.cn_watchlist import CN_UNIVERSE, CnWatchlist, _norm
from swing_trader.schemas import AiPhase, Role
from swing_trader.watchlist import WatchlistItem

__all__ = ["HK_INDEX_SYMBOLS", "HK_UNIVERSE", "build_hk_watchlist"]

HK_UNIVERSE: list[WatchlistItem] = [
    item for item in CN_UNIVERSE if item.symbol.endswith(".HK")
]
HK_INDEX_SYMBOLS: tuple[str, ...] = ("^HSI", "^HSCE")


def build_hk_watchlist(override: str = "") -> CnWatchlist:
    known = {_norm(i.symbol): i for i in HK_UNIVERSE}
    raw_symbols = [s for s in override.split(",") if s.strip()] if override.strip() else []
    items = []
    for raw in raw_symbols:
        sym = _norm(raw)
        if not sym.endswith(".HK"):
            continue
        items.append(known.get(sym) or WatchlistItem(
            symbol=sym, theme="hk-custom", ai_phase=AiPhase.NONE, role=Role.ROTATION
        ))
    if not override.strip():
        items = list(HK_UNIVERSE)
    by_symbol = {_norm(i.symbol): i for i in items}
    return CnWatchlist(items=items, lookup=lambda symbol: by_symbol.get(_norm(symbol)))
