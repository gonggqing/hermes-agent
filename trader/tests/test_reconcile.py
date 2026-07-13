"""Tests for swing_trader.reconcile — ledger↔broker consistency (Phase 0.8).

Reconciliation compares the broker's live positions against the positions
IMPLIED by recorded fills (BUY +, SELL −). It must catch drift, tolerate
fractional-share noise, and — per Loop.md §5.8 — NEVER raise: any failure
degrades to a conservative unreconciled result (ok=False).
"""

from __future__ import annotations

from dataclasses import dataclass

from swing_trader.reconcile import reconcile_broker_ledger
from swing_trader.schemas import Mode, Side


@dataclass
class _Pos:
    symbol: str
    qty: float


@dataclass
class _Fill:
    symbol: str
    side: Side
    qty: float


class FakeBroker:
    def __init__(self, positions):
        self._positions = positions

    def get_positions(self):
        return list(self._positions)


class FakeLedger:
    def __init__(self, fills):
        self._fills = fills

    def get_fills(self, mode):
        return list(self._fills)


class RaisingBroker:
    def get_positions(self):
        raise RuntimeError("broker connection lost")


def _broker(*pairs):
    return FakeBroker([_Pos(s, q) for s, q in pairs])


def _ledger(*triples):
    return FakeLedger([_Fill(s, side, q) for s, side, q in triples])


# ------------------------------------------------------------------- consistent


class TestConsistent:
    def test_empty_both_sides_is_ok(self):
        r = reconcile_broker_ledger(_broker(), _ledger(), Mode.PAPER)
        assert r.ok is True
        assert r.mismatches == []
        assert r.n_symbols == 0
        assert "consistent" in r.summary()

    def test_buys_net_to_broker_position(self):
        broker = _broker(("NVDA", 15.0))
        ledger = _ledger(("NVDA", Side.BUY, 10.0), ("NVDA", Side.BUY, 5.0))
        r = reconcile_broker_ledger(broker, ledger, Mode.PAPER)
        assert r.ok is True
        assert r.n_symbols == 1

    def test_buys_minus_sells_net_flat_is_ignored(self):
        """A fully-closed symbol (net 0) is not a position on either side."""
        broker = _broker()  # broker flat
        ledger = _ledger(("NVDA", Side.BUY, 10.0), ("NVDA", Side.SELL, 10.0))
        r = reconcile_broker_ledger(broker, ledger, Mode.PAPER)
        assert r.ok is True
        assert r.n_symbols == 0

    def test_string_mode_accepted(self):
        r = reconcile_broker_ledger(_broker(("AMD", 3.0)),
                                    _ledger(("AMD", Side.BUY, 3.0)), "paper")
        assert r.ok is True

    def test_fractional_noise_within_tolerance_is_consistent(self):
        broker = _broker(("NVDA", 10.0000001))
        ledger = _ledger(("NVDA", Side.BUY, 10.0))
        r = reconcile_broker_ledger(broker, ledger, Mode.PAPER)
        assert r.ok is True


# ---------------------------------------------------------------------- drift


class TestDrift:
    def test_qty_mismatch_reported(self):
        broker = _broker(("NVDA", 10.0))
        ledger = _ledger(("NVDA", Side.BUY, 8.0))
        r = reconcile_broker_ledger(broker, ledger, Mode.PAPER)
        assert r.ok is False
        assert len(r.mismatches) == 1
        m = r.mismatches[0]
        assert m.symbol == "NVDA"
        assert m.broker_qty == 10.0 and m.ledger_qty == 8.0
        assert "NVDA" in r.summary()

    def test_position_missing_from_ledger(self):
        broker = _broker(("TSLA", 5.0))
        ledger = _ledger()
        r = reconcile_broker_ledger(broker, ledger, Mode.PAPER)
        assert r.ok is False
        assert r.mismatches[0].ledger_qty == 0.0

    def test_position_missing_from_broker(self):
        broker = _broker()
        ledger = _ledger(("AMD", Side.BUY, 4.0))
        r = reconcile_broker_ledger(broker, ledger, Mode.PAPER)
        assert r.ok is False
        assert r.mismatches[0].broker_qty == 0.0

    def test_multiple_symbols_partial_drift(self):
        broker = _broker(("NVDA", 10.0), ("AMD", 4.0))
        ledger = _ledger(("NVDA", Side.BUY, 10.0), ("AMD", Side.BUY, 3.0))
        r = reconcile_broker_ledger(broker, ledger, Mode.PAPER)
        assert r.ok is False
        assert {m.symbol for m in r.mismatches} == {"AMD"}
        assert r.n_symbols == 2


# ----------------------------------------------------------------- never raises


class TestFailClosed:
    def test_broker_exception_returns_unreconciled(self):
        r = reconcile_broker_ledger(RaisingBroker(), _ledger(), Mode.PAPER)
        assert r.ok is False
        assert r.mismatches[0].symbol == "<error>"

    def test_bad_mode_string_returns_unreconciled(self):
        r = reconcile_broker_ledger(_broker(), _ledger(), "not-a-mode")
        assert r.ok is False
        assert r.mismatches[0].symbol == "<error>"
