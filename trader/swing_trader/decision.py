"""Decision core (Loop.md §5.4) — rule-based v0.

Consumes monitor context + post-debate signals + memory and proposes
:class:`CandidateOrder` objects. It NEVER places orders: every candidate must
pass the RiskEngine, then human confirmation via Telegram (Loop.md §3).

Order-type policy (Loop.md §4): entries are GTC BRACKET (limit entry +
protective stop + take-profit) so a position can never exist without a
resting stop; discretionary exits are MOC (fill at the 16:00 ET close while
the user sleeps).

Model plan (Loop.md §8): this rule-based core and the future LLM core share
the same ``propose()`` contract. The LLM core is a stub selected via config —
switching models/cores is a config change, not a rewrite. Hermes runtime
memory plugs in behind :class:`MemoryStore`; the JSON implementation below
keeps Phase 0 free of any Hermes-internal dependency (Loop.md §8).
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Protocol

from swing_trader.log import get_logger
from swing_trader.risk import RiskParams
from swing_trader.schemas import (
    AccountSnapshot,
    CandidateOrder,
    Direction,
    OrderType,
    Position,
    Role,
    Side,
    Signal,
    TimeInForce,
)

logger = get_logger(__name__)

__all__ = [
    "DecisionParams",
    "JsonMemory",
    "LLMDecisionCore",
    "MemoryStore",
    "RuleBasedDecisionCore",
    "SymbolView",
]


@dataclass
class SymbolView:
    """Per-symbol market context (decoupled from the monitors module)."""

    symbol: str
    last: float
    atr_pct: float | None  # daily ATR as % of price
    pool: Role = Role.ROTATION


@dataclass(frozen=True)
class DecisionParams:
    min_entry_confidence: float = 0.55
    min_exit_confidence: float = 0.60
    entry_limit_discount_pct: float = 0.5  # buy limit 0.5% below last
    sl_atr_mult: float = 2.0
    tp_atr_mult: float = 3.0  # 1.5R take-profit
    max_new_candidates: int = 3
    # memory-driven confidence penalty (self-improvement affects ANALYSIS
    # quality only — never risk limits; Loop.md §3)
    memory_min_trades: int = 5
    memory_winrate_floor: float = 0.30
    memory_conf_penalty: float = 0.8


class MemoryStore(Protocol):
    """Memory port (Loop.md §5.4/§5.8). Hermes adapter arrives in Phase 0.5;
    Phase 0 uses the JSON file implementation below."""

    def note_for(self, symbol: str) -> str: ...

    def stats_for(self, symbol: str) -> tuple[int, int]:
        """Return (wins, losses) of closed trades in this symbol."""
        ...

    def record_outcome(self, symbol: str, pnl: float, note: str = "") -> None: ...


class JsonMemory:
    """Tiny file-backed MemoryStore: per-symbol win/loss tallies + notes."""

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._data: dict = {}
        if self._path.exists():
            try:
                self._data = json.loads(self._path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                logger.warning("memory file unreadable; starting fresh",
                               extra={"path": str(self._path)})
                self._data = {}

    def _entry(self, symbol: str) -> dict:
        return self._data.setdefault(
            symbol.upper(), {"wins": 0, "losses": 0, "notes": []}
        )

    def note_for(self, symbol: str) -> str:
        e = self._data.get(symbol.upper())
        if not e:
            return ""
        notes = "; ".join(e.get("notes", [])[-3:])
        return f"memory: {e['wins']}W/{e['losses']}L" + (f" — {notes}" if notes else "")

    def stats_for(self, symbol: str) -> tuple[int, int]:
        e = self._data.get(symbol.upper())
        if not e:
            return (0, 0)
        return (int(e.get("wins", 0)), int(e.get("losses", 0)))

    def record_outcome(self, symbol: str, pnl: float, note: str = "") -> None:
        e = self._entry(symbol)
        if pnl > 0:
            e["wins"] += 1
        elif pnl < 0:
            e["losses"] += 1
        if note:
            e.setdefault("notes", []).append(note[:200])
            e["notes"] = e["notes"][-20:]
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(json.dumps(self._data, ensure_ascii=False, indent=1),
                              encoding="utf-8")


class RuleBasedDecisionCore:
    """v0 brain: debate signals -> BRACKET entry / MOC exit candidates."""

    def __init__(
        self,
        params: DecisionParams | None = None,
        risk_params: RiskParams | None = None,
        memory: MemoryStore | None = None,
    ) -> None:
        self.params = params or DecisionParams()
        self.risk_params = risk_params or RiskParams()
        self.memory = memory

    # ------------------------------------------------------------------ api

    def propose(
        self,
        signals: Iterable[Signal],
        views: dict[str, SymbolView],
        account: AccountSnapshot,
        positions: list[Position],
        risk_on_off: str = "neutral",
        open_order_symbols: set[str] | None = None,
        earnings_symbols: set[str] | None = None,
    ) -> list[CandidateOrder]:
        """Turn post-debate signals into candidate orders (entries + exits).

        ``earnings_symbols``: symbols with an imminent earnings print — no fresh
        ENTRY is opened into them (a known landmine); exits are unaffected.
        """
        p = self.params
        held = {pos.symbol: pos for pos in positions if pos.qty > 0}
        busy = set(open_order_symbols or set())
        earnings_syms = {s.strip().upper() for s in (earnings_symbols or set())}
        entries: list[CandidateOrder] = []
        exits: list[CandidateOrder] = []

        for sig in signals:
            view = views.get(sig.symbol)
            if view is None:
                continue

            # ---- exits first: protect capital regardless of regime --------
            if sig.symbol in held and sig.direction is Direction.SHORT \
                    and sig.confidence >= p.min_exit_confidence:
                pos = held[sig.symbol]
                exits.append(
                    CandidateOrder(
                        symbol=sig.symbol,
                        side=Side.SELL,
                        qty=pos.qty,
                        order_type=OrderType.MOC,
                        tif=TimeInForce.DAY,
                        rationale=f"exit: {sig.thesis[:240]}",
                        confidence=sig.confidence,
                        signal_ids=[sig.id],
                        ref_px=view.last,
                        pool=view.pool,
                    )
                )
                continue

            # ---- entries ---------------------------------------------------
            if risk_on_off == "risk_off":
                continue  # no new entries in a risk-off tape
            if sig.direction is not Direction.LONG:
                continue
            if sig.symbol in held or sig.symbol in busy:
                continue  # no pyramiding, no duplicate resting orders (v0)
            if sig.symbol in earnings_syms:
                continue  # never open a fresh position into an earnings print
            if view.atr_pct is None or view.atr_pct <= 0:
                continue  # cannot place stops without volatility context
            confidence = self._memory_adjusted_confidence(sig)
            if confidence < p.min_entry_confidence:
                continue

            entry = round(view.last * (1 - p.entry_limit_discount_pct / 100.0), 2)
            atr_dollars = view.last * view.atr_pct / 100.0
            sl = round(entry - p.sl_atr_mult * atr_dollars, 2)
            tp = round(entry + p.tp_atr_mult * atr_dollars, 2)
            if sl <= 0 or sl >= entry:
                continue
            qty = self._size(entry, sl, account)
            if qty <= 0:
                continue

            rationale = sig.thesis[:400]
            if self.memory is not None:
                note = self.memory.note_for(sig.symbol)
                if note:
                    rationale = f"{rationale} | {note}"

            entries.append(
                CandidateOrder(
                    symbol=sig.symbol,
                    side=Side.BUY,
                    qty=qty,
                    order_type=OrderType.BRACKET,
                    limit=entry,
                    stop=sl,
                    tp=tp,
                    tif=TimeInForce.GTC,
                    rationale=rationale,
                    confidence=confidence,
                    signal_ids=[sig.id],
                    ref_px=view.last,
                    pool=view.pool,
                )
            )

        entries.sort(key=lambda c: c.confidence, reverse=True)
        entries = entries[: p.max_new_candidates]
        result = exits + entries
        logger.info(
            "decision core proposal",
            extra={
                "n_exits": len(exits),
                "n_entries": len(entries),
                "regime": risk_on_off,
            },
        )
        return result

    # ------------------------------------------------------------- internals

    def _memory_adjusted_confidence(self, sig: Signal) -> float:
        """Down-weight signals in symbols we have repeatedly lost on.

        Guardrail (Loop.md §3): memory tunes ANALYSIS quality only; it can
        only lower confidence, never raise it, and never touches risk caps.
        """
        if self.memory is None:
            return sig.confidence
        wins, losses = self.memory.stats_for(sig.symbol)
        total = wins + losses
        p = self.params
        if total >= p.memory_min_trades and wins / total < p.memory_winrate_floor:
            return sig.confidence * p.memory_conf_penalty
        return sig.confidence

    def _size(self, entry: float, sl: float, account: AccountSnapshot) -> int:
        """Risk-based sizing; the RiskEngine independently re-checks the cap."""
        risk_per_share = entry - sl
        if risk_per_share <= 0 or account.equity <= 0:
            return 0
        risk_dollars = (
            self.risk_params.effective_per_trade_risk_pct / 100.0 * account.equity
        )
        qty = math.floor(risk_dollars / risk_per_share)
        # cash sanity so we do not propose obviously unaffordable candidates
        max_affordable = math.floor(
            max(0.0, account.cash - self.risk_params.est_commission) / entry
        )
        return max(0, min(qty, max_affordable))


class LLMDecisionCore:
    """Model-agnostic LLM decision core — Phase 0.5 (Loop.md §8 model plan).

    Selected via config; must produce the same CandidateOrder contract and
    still flows through RiskEngine + human confirmation. TODO(Phase 0.5):
    implement against a provider-agnostic client (OpenRouter/OpenAI/local),
    feeding monitors + signals + Hermes memory into a structured prompt.
    """

    def propose(self, *args, **kwargs) -> list[CandidateOrder]:
        raise NotImplementedError(
            "TODO(Phase 0.5): LLM decision core — model-agnostic via config "
            "(Loop.md §8); rule-based core is the Phase 0 default"
        )
