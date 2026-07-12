"""Tests for swing_trader.schemas (Loop.md §6)."""

from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from swing_trader.schemas import (
    AccountSnapshot,
    BreakerState,
    CandidateOrder,
    CandidateStatus,
    Direction,
    Fill,
    Mode,
    Order,
    OrderStatus,
    OrderType,
    Position,
    Role,
    Side,
    Signal,
    TimeInForce,
    Trade,
    utcnow,
)

NOW = datetime(2026, 7, 12, 15, 30, tzinfo=timezone.utc)


def make_order(**kw) -> Order:
    base = dict(
        mode=Mode.PAPER,
        symbol="NVDA",
        side=Side.BUY,
        qty=10,
        order_type=OrderType.LMT,
        limit=100.0,
    )
    base.update(kw)
    return Order(**base)


class TestSignal:
    def test_roundtrip(self):
        s = Signal(
            source_agent="technical",
            symbol="nvda",
            thesis="breakout above 20dma",
            direction=Direction.LONG,
            confidence=0.7,
            features_json={"rsi": 61.2},
        )
        assert s.symbol == "NVDA"  # normalized
        restored = Signal.model_validate_json(s.model_dump_json())
        assert restored == s

    def test_confidence_bounds(self):
        for bad in (-0.1, 1.1):
            with pytest.raises(ValidationError):
                Signal(
                    source_agent="a", symbol="X", thesis="t",
                    direction=Direction.LONG, confidence=bad,
                )

    def test_naive_timestamp_rejected(self):
        with pytest.raises(ValidationError, match="timezone-aware"):
            Signal(
                ts=datetime(2026, 7, 12, 15, 30),  # naive
                source_agent="a", symbol="X", thesis="t",
                direction=Direction.LONG, confidence=0.5,
            )

    def test_empty_symbol_rejected(self):
        with pytest.raises(ValidationError):
            Signal(source_agent="a", symbol="  ", thesis="t",
                   direction=Direction.LONG, confidence=0.5)


class TestOrder:
    def test_defaults(self):
        o = make_order()
        assert o.status is OrderStatus.NEW
        assert o.tif is TimeInForce.GTC
        assert o.filled_qty == 0.0

    def test_lmt_requires_limit(self):
        with pytest.raises(ValidationError, match="limit price"):
            make_order(limit=None)

    def test_loc_requires_limit(self):
        with pytest.raises(ValidationError, match="limit price"):
            make_order(order_type=OrderType.LOC, limit=None)

    def test_stp_requires_stop(self):
        with pytest.raises(ValidationError, match="stop price"):
            make_order(order_type=OrderType.STP, limit=None)

    def test_moc_needs_no_prices(self):
        o = make_order(order_type=OrderType.MOC, limit=None)
        assert o.limit is None and o.stop is None

    def test_buy_bracket_geometry(self):
        o = make_order(order_type=OrderType.BRACKET, limit=100.0, stop=95.0, tp=112.0)
        assert o.order_type is OrderType.BRACKET
        with pytest.raises(ValidationError, match="stop must be below"):
            make_order(order_type=OrderType.BRACKET, limit=100.0, stop=101.0)
        with pytest.raises(ValidationError, match="tp must be above"):
            make_order(order_type=OrderType.BRACKET, limit=100.0, stop=95.0, tp=99.0)

    def test_sell_bracket_geometry(self):
        o = make_order(side=Side.SELL, order_type=OrderType.BRACKET,
                       limit=100.0, stop=105.0, tp=90.0)
        assert o.side is Side.SELL
        with pytest.raises(ValidationError, match="stop must be above"):
            make_order(side=Side.SELL, order_type=OrderType.BRACKET,
                       limit=100.0, stop=95.0)

    def test_bracket_requires_both_prices(self):
        with pytest.raises(ValidationError, match="BRACKET requires"):
            make_order(order_type=OrderType.BRACKET, limit=100.0, stop=None)

    def test_qty_positive(self):
        with pytest.raises(ValidationError):
            make_order(qty=0)

    def test_filled_qty_cannot_exceed_qty(self):
        with pytest.raises(ValidationError, match="filled_qty"):
            make_order(qty=10, filled_qty=11)

    def test_json_roundtrip_preserves_mode(self):
        o = make_order(mode=Mode.PAPER)
        restored = Order.model_validate_json(o.model_dump_json())
        assert restored.mode is Mode.PAPER
        assert restored == o


class TestTrade:
    def test_open_trade_has_no_exit(self):
        t = Trade(mode=Mode.PAPER, symbol="MU", qty=5,
                  entry_order_id="o1", entry_px=120.0)
        assert t.exit_px is None and t.pnl is None

    def test_closed_trade(self):
        t = Trade(mode=Mode.PAPER, symbol="MU", qty=5, entry_order_id="o1",
                  exit_order_id="o2", entry_px=120.0, exit_px=126.0,
                  pnl=30.0, r_multiple=1.5, hold_days=3.0, rationale="memory upcycle")
        assert t.pnl == 30.0


class TestPosition:
    def test_upnl_computed(self):
        p = Position(symbol="NVDA", qty=10, avg_px=100.0, mkt_px=104.5, pool=Role.CONVICTION)
        assert p.upnl == pytest.approx(45.0)
        assert p.market_value == pytest.approx(1045.0)

    def test_upnl_none_without_mark(self):
        assert Position(symbol="NVDA", qty=10, avg_px=100.0).upnl is None


class TestAccountSnapshot:
    def test_defaults(self):
        s = AccountSnapshot(mode=Mode.PAPER, equity=2000.0, cash=1500.0)
        assert s.breaker_state is BreakerState.NORMAL
        assert s.drawdown_pct == 0.0


class TestCandidateOrder:
    def make(self, **kw) -> CandidateOrder:
        base = dict(
            symbol="NVDA", side=Side.BUY, qty=3, order_type=OrderType.BRACKET,
            limit=100.0, stop=95.0, rationale="test", confidence=0.6,
        )
        base.update(kw)
        return CandidateOrder(**base)

    def test_bracket_candidate_ok(self):
        c = self.make()
        assert c.status is CandidateStatus.PROPOSED

    def test_buy_entry_without_protection_rejected(self):
        """Loop.md §4: never leave a position without a resting stop."""
        with pytest.raises(ValidationError, match="protective stop"):
            self.make(order_type=OrderType.LMT, stop=None, sl=None)

    def test_buy_lmt_with_sl_ok(self):
        c = self.make(order_type=OrderType.LMT, stop=None, sl=94.0)
        assert c.sl == 94.0

    def test_sell_exit_needs_no_protection(self):
        c = self.make(side=Side.SELL, order_type=OrderType.MOC,
                      limit=None, stop=None, sl=None)
        assert c.side is Side.SELL

    def test_naive_valid_until_rejected(self):
        with pytest.raises(ValidationError, match="timezone-aware"):
            self.make(valid_until=datetime(2026, 7, 12, 16, 0))

    def test_aware_valid_until_ok(self):
        c = self.make(valid_until=NOW + timedelta(hours=1))
        assert c.valid_until.tzinfo is not None


class TestFill:
    def test_fill(self):
        f = Fill(order_id="o1", symbol="NVDA", side=Side.BUY, qty=3, px=99.9,
                 commission=1.0)
        assert f.mode is Mode.PAPER


def test_enum_values_match_loop_md():
    assert {e.value for e in OrderType} == {"LMT", "STP", "MOC", "LOC", "BRACKET"}
    assert {e.value for e in TimeInForce} == {"GTC", "DAY"}
    assert {e.value for e in Mode} == {"paper", "live"}
    assert {e.value for e in Role} == {"core", "conviction", "rotation", "hedge"}


def test_utcnow_is_aware():
    assert utcnow().tzinfo is not None
