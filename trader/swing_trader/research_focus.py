"""Bound expensive symbol research without narrowing broad-market evidence."""

from __future__ import annotations

from collections.abc import Mapping, Sequence

__all__ = ["select_analysis_symbols"]


def _symbol(row: object) -> str:
    return str(getattr(row, "symbol", "") or "").strip().upper()


def _trend_distance(state: object) -> float:
    last = float(getattr(state, "last", 0.0) or 0.0)
    anchor = float(
        getattr(state, "sma50", None) or getattr(state, "sma20", None) or 0.0
    )
    return 0.0 if last <= 0 or anchor <= 0 else last / anchor - 1.0


def select_analysis_symbols(
    universe: Sequence[str],
    *,
    watch: Mapping[str, object],
    discovered: Sequence[str] = (),
    positions: Sequence[object] = (),
    holdings: Sequence[object] = (),
    limit: int = 16,
) -> list[str]:
    """Prioritize owned, discovered, and tape-extreme names for deep analysis.

    Breadth and mover tables still use ``universe`` in full.  This selection
    only bounds expensive per-symbol news/fundamentals/LLM work, while ensuring
    existing exposure and newly discovered opportunities cannot be displaced
    by a static watchlist prefix.
    """

    cap = max(1, int(limit))
    ranked_watch = sorted(
        ((symbol, _trend_distance(state)) for symbol, state in watch.items()),
        key=lambda row: (-row[1], row[0]),
    )
    extremes: list[str] = []
    for index in range((len(ranked_watch) + 1) // 2):
        extremes.append(ranked_watch[index][0])
        opposite = len(ranked_watch) - index - 1
        if opposite != index:
            extremes.append(ranked_watch[opposite][0])

    ordered = [
        *(_symbol(row) for row in positions),
        *(_symbol(row) for row in holdings),
        *(str(symbol).strip().upper() for symbol in discovered),
        *extremes,
        *(str(symbol).strip().upper() for symbol in universe),
    ]
    return list(dict.fromkeys(symbol for symbol in ordered if symbol))[:cap]
