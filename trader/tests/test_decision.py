"""Tests for swing_trader.decision (Loop.md §5.4, backlog 10)."""

import pytest

from swing_trader.decision import (
    DecisionParams,
    JsonMemory,
    LLMDecisionCore,
    RuleBasedDecisionCore,
    SymbolView,
)
from swing_trader.risk import RiskParams
from swing_trader.schemas import (
    AccountSnapshot,
    CandidateOrder,
    Direction,
    Mode,
    OrderType,
    Position,
    Role,
    Side,
    Signal,
    TimeInForce,
)


def sig(symbol="NVDA", direction=Direction.LONG, conf=0.7, thesis="debate synthesis"):
    return Signal(
        source_agent="debate", symbol=symbol, thesis=thesis,
        direction=direction, confidence=conf,
    )


def view(symbol="NVDA", last=100.0, atr=4.0, pool=Role.CONVICTION):
    return SymbolView(symbol=symbol, last=last, atr_pct=atr, pool=pool)


def account(equity=2000.0, cash=2000.0):
    return AccountSnapshot(mode=Mode.PAPER, equity=equity, cash=cash)


def core(**kw) -> RuleBasedDecisionCore:
    return RuleBasedDecisionCore(**kw)


class TestEarningsAvoidance:
    def test_imminent_earnings_blocks_fresh_entry(self):
        # a strong long normally becomes one bracket entry...
        assert len(core().propose([sig()], {"NVDA": view()}, account(), [])) == 1
        # ...but not when NVDA has an imminent earnings print.
        out = core().propose([sig()], {"NVDA": view()}, account(), [],
                             earnings_symbols={"NVDA"})
        assert out == []

    def test_earnings_does_not_block_exit(self):
        # protecting capital always wins: a held name with a SHORT exit signal
        # still exits even if it reports earnings imminently.
        held = [Position(symbol="NVDA", qty=3, avg_px=90.0)]
        out = core().propose([sig(direction=Direction.SHORT, conf=0.7)],
                             {"NVDA": view()}, account(), held,
                             earnings_symbols={"NVDA"})
        assert len(out) == 1 and out[0].side is Side.SELL


class TestEntries:
    def test_long_signal_becomes_bracket_candidate(self):
        out = core().propose([sig()], {"NVDA": view()}, account(), [])
        assert len(out) == 1
        c = out[0]
        assert c.side is Side.BUY
        assert c.order_type is OrderType.BRACKET
        assert c.tif is TimeInForce.GTC
        # last=100: entry = 99.5, ATR$ = 4 -> sl = 99.5-8 = 91.5, tp = 99.5+12 = 111.5
        assert c.limit == pytest.approx(99.5)
        assert c.stop == pytest.approx(91.5)
        assert c.tp == pytest.approx(111.5)
        # risk sizing: 1% of 2000 = $20 / $8 per share -> 2 shares
        assert c.qty == 2
        assert c.pool is Role.CONVICTION
        assert c.ref_px == pytest.approx(100.0)
        assert c.signal_ids

    def test_low_confidence_filtered(self):
        out = core().propose([sig(conf=0.5)], {"NVDA": view()}, account(), [])
        assert out == []

    def test_neutral_and_short_without_position_ignored(self):
        signals = [sig(direction=Direction.NEUTRAL), sig("MU", Direction.SHORT)]
        views = {"NVDA": view(), "MU": view("MU")}
        assert core().propose(signals, views, account(), []) == []

    def test_risk_off_blocks_new_entries(self):
        out = core().propose([sig()], {"NVDA": view()}, account(), [],
                             risk_on_off="risk_off")
        assert out == []

    def test_no_pyramiding_when_held(self):
        held = [Position(symbol="NVDA", qty=2, avg_px=90.0)]
        assert core().propose([sig()], {"NVDA": view()}, account(), held) == []

    def test_open_order_symbol_skipped(self):
        out = core().propose([sig()], {"NVDA": view()}, account(), [],
                             open_order_symbols={"NVDA"})
        assert out == []

    def test_missing_atr_skipped(self):
        out = core().propose([sig()], {"NVDA": view(atr=None)}, account(), [])
        assert out == []

    def test_missing_view_skipped(self):
        assert core().propose([sig()], {}, account(), []) == []

    def test_qty_zero_skipped(self):
        # equity so small that 1% risk buys zero shares
        out = core().propose([sig()], {"NVDA": view()}, account(equity=100, cash=100), [])
        assert out == []

    def test_cash_constrains_qty(self):
        # risk-based qty would be 25 (1% of 20k = 200/8), cash allows only 1
        out = core().propose([sig()], {"NVDA": view()},
                             account(equity=20_000, cash=150.0), [])
        assert len(out) == 1
        assert out[0].qty == 1

    def test_max_new_candidates_by_confidence(self):
        symbols = ["NVDA", "MU", "ANET", "TSM"]
        confs = [0.6, 0.9, 0.7, 0.8]
        signals = [sig(s, conf=c) for s, c in zip(symbols, confs)]
        views = {s: view(s) for s in symbols}
        out = core().propose(signals, views, account(equity=50_000, cash=50_000), [])
        assert len(out) == 3
        assert [c.symbol for c in out] == ["MU", "TSM", "ANET"]  # sorted by conf

    def test_all_candidates_validate_protection(self):
        """Every generated entry satisfies the §4 never-naked invariant."""
        out = core().propose([sig()], {"NVDA": view()}, account(), [])
        c = out[0]
        assert isinstance(c, CandidateOrder)
        assert c.stop is not None and c.stop < c.limit


class TestExits:
    def test_short_debate_on_held_position_proposes_moc_exit(self):
        held = [Position(symbol="NVDA", qty=3, avg_px=90.0)]
        out = core().propose([sig(direction=Direction.SHORT, conf=0.65)],
                             {"NVDA": view()}, account(), held)
        assert len(out) == 1
        c = out[0]
        assert c.side is Side.SELL
        assert c.order_type is OrderType.MOC
        assert c.tif is TimeInForce.DAY
        assert c.qty == 3
        assert c.rationale.startswith("exit:")

    def test_exit_confidence_floor(self):
        held = [Position(symbol="NVDA", qty=3, avg_px=90.0)]
        out = core().propose([sig(direction=Direction.SHORT, conf=0.55)],
                             {"NVDA": view()}, account(), held)
        assert out == []

    def test_exits_survive_risk_off(self):
        held = [Position(symbol="NVDA", qty=3, avg_px=90.0)]
        out = core().propose([sig(direction=Direction.SHORT, conf=0.7)],
                             {"NVDA": view()}, account(), held,
                             risk_on_off="risk_off")
        assert len(out) == 1 and out[0].side is Side.SELL


class TestMemory:
    def test_repeated_losses_penalize_confidence(self, tmp_path):
        mem = JsonMemory(tmp_path / "mem.json")
        for _ in range(4):
            mem.record_outcome("NVDA", -10.0)
        mem.record_outcome("NVDA", +5.0)  # 1W/4L over 5 trades -> winrate 0.2
        c = core(memory=mem)
        # 0.6 * 0.8 = 0.48 < 0.55 -> filtered
        assert c.propose([sig(conf=0.6)], {"NVDA": view()}, account(), []) == []
        # 0.75 * 0.8 = 0.6 >= 0.55 -> survives, with reduced confidence
        out = c.propose([sig(conf=0.75)], {"NVDA": view()}, account(), [])
        assert len(out) == 1
        assert out[0].confidence == pytest.approx(0.6)

    def test_memory_never_boosts(self, tmp_path):
        mem = JsonMemory(tmp_path / "mem.json")
        for _ in range(10):
            mem.record_outcome("NVDA", +10.0)  # perfect record
        out = core(memory=mem).propose([sig(conf=0.7)], {"NVDA": view()},
                                       account(), [])
        assert out[0].confidence == pytest.approx(0.7)  # unchanged

    def test_memory_note_lands_in_rationale(self, tmp_path):
        mem = JsonMemory(tmp_path / "mem.json")
        mem.record_outcome("NVDA", +10.0, note="breakouts worked")
        out = core(memory=mem).propose([sig(conf=0.7)], {"NVDA": view()},
                                       account(), [])
        assert "memory: 1W/0L" in out[0].rationale
        assert "breakouts worked" in out[0].rationale

    def test_json_memory_roundtrip(self, tmp_path):
        path = tmp_path / "mem.json"
        JsonMemory(path).record_outcome("MU", -5.0, note="chased")
        reloaded = JsonMemory(path)
        assert reloaded.stats_for("MU") == (0, 1)
        assert "chased" in reloaded.note_for("mu")

    def test_corrupt_memory_file_starts_fresh(self, tmp_path):
        path = tmp_path / "mem.json"
        path.write_text("{not json", encoding="utf-8")
        assert JsonMemory(path).stats_for("NVDA") == (0, 0)


class TestSizingRespectsHardCap:
    def test_risk_params_clamped_in_sizing(self):
        """Even a mis-configured 5% per-trade param sizes at the 1.6% hard cap."""
        loose = RiskParams(per_trade_risk_pct=5.0)
        out = core(risk_params=loose).propose(
            [sig()], {"NVDA": view()}, account(equity=10_000, cash=10_000), []
        )
        # 1.6% of 10k = $160 / $8 rps = 20 shares (NOT 5% -> 62 shares)
        assert out[0].qty == 20


def test_llm_core_is_a_stub():
    with pytest.raises(NotImplementedError, match="Phase 0.5"):
        LLMDecisionCore().propose()


def test_determinism():
    a = core().propose([sig()], {"NVDA": view()}, account(), [])
    b = core().propose([sig()], {"NVDA": view()}, account(), [])
    assert a[0].qty == b[0].qty
    assert a[0].limit == b[0].limit
    assert a[0].confidence == b[0].confidence
