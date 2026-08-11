"""Mainland A-share PAPER execution invariants."""

from datetime import date, datetime, timezone

import pytest

from swing_trader.broker_view import CurrencyBrokerView
from swing_trader.cn_market import (
    ASharePhase,
    cn_phase,
    cn_tick_size,
    normalize_cn_buy_qty,
    off_grid_cn_prices,
)
from swing_trader.decision import RuleBasedDecisionCore, SymbolView
from swing_trader.interfaces import Bar
from swing_trader.execution import ExecutionEngine
from swing_trader.ledger import Ledger
from swing_trader.paper_broker import PaperBroker
from swing_trader.scheduler import CN_TRADING_SCHEDULE, Event, is_trading_day
from swing_trader.schemas import (
    Mode,
    CandidateOrder,
    CandidateStatus,
    AccountSnapshot,
    Direction,
    Order,
    OrderStatus,
    OrderType,
    Position,
    Signal,
    Side,
    TimeInForce,
)


def _cn(hh: int, mm: int, day: int = 11) -> datetime:
    from zoneinfo import ZoneInfo

    return datetime(2026, 8, day, hh, mm, tzinfo=ZoneInfo("Asia/Shanghai")).astimezone(
        timezone.utc
    )


def _bar(symbol: str, *, ts: datetime, px: float = 10.0) -> Bar:
    return Bar(
        symbol=symbol,
        ts=ts,
        open=px,
        high=px * 1.02,
        low=px * 0.98,
        close=px,
        volume=1_000_000,
    )


def test_cn_schedule_has_one_hour_confirmation_and_official_holidays():
    assert CN_TRADING_SCHEDULE.event_times[Event.PUSH_CANDIDATES].isoformat() == "10:15:00"
    assert CN_TRADING_SCHEDULE.event_times[Event.CONFIRM_CUTOFF].isoformat() == "11:15:00"
    assert CN_TRADING_SCHEDULE.event_times[Event.MARKET_CLOSE].isoformat() == "15:00:00"
    assert not is_trading_day(date(2026, 2, 23), CN_TRADING_SCHEDULE)
    assert not is_trading_day(date(2026, 5, 4), CN_TRADING_SCHEDULE)
    assert is_trading_day(date(2026, 5, 6), CN_TRADING_SCHEDULE)


@pytest.mark.parametrize(
    ("stamp", "phase"),
    [
        (_cn(9, 20), ASharePhase.OPENING_AUCTION),
        (_cn(10, 0), ASharePhase.MORNING),
        (_cn(12, 0), ASharePhase.LUNCH),
        (_cn(13, 30), ASharePhase.AFTERNOON),
        (_cn(14, 58), ASharePhase.CLOSING_AUCTION),
        (_cn(15, 1), ASharePhase.CLOSED),
    ],
)
def test_cn_market_phases(stamp, phase):
    assert cn_phase(stamp) is phase


def test_cn_stock_and_listed_fund_ticks_and_lots():
    assert str(cn_tick_size("600519.SS")) == "0.01"
    assert str(cn_tick_size("510300.SS")) == "0.001"
    assert str(cn_tick_size("159995.SZ")) == "0.001"
    assert normalize_cn_buy_qty(299) == 200
    assert off_grid_cn_prices("600519.SS", {"limit": 10.001})
    assert off_grid_cn_prices("510300.SS", {"limit": 4.321}) == {}


def test_paper_broker_enforces_cn_buy_lot_and_t_plus_one():
    symbol = "600519.SS"
    broker = PaperBroker(
        starting_cash=1,
        starting_cash_by_currency={"CNY": 100_000},
        base_currency="CNY",
        apply_cn_fees=True,
    )
    bad = broker.place_order(Order(
        ts=_cn(10, 0), mode=Mode.PAPER, symbol=symbol, side=Side.BUY, qty=50,
        order_type=OrderType.LMT, limit=10, tif=TimeInForce.DAY,
    ))
    assert not bad.accepted and "100-share board lot" in bad.reason

    parent = broker.place_order(Order(
        ts=_cn(10, 0), mode=Mode.PAPER, symbol=symbol, side=Side.BUY, qty=100,
        order_type=OrderType.BRACKET, limit=10, stop=9, tp=12,
        tif=TimeInForce.DAY,
    ))
    assert parent.accepted
    broker.step({symbol: _bar(symbol, ts=_cn(13, 0))}, execution_ts=_cn(15, 0))
    for order in broker.get_orders(active_only=True):
        if order.side is Side.SELL:
            broker.cancel_order(order.id)

    same_day = broker.place_order(Order(
        ts=_cn(15, 0), mode=Mode.PAPER, symbol=symbol, side=Side.SELL, qty=100,
        order_type=OrderType.LMT, limit=10, tif=TimeInForce.DAY,
    ))
    assert not same_day.accepted and "T+1 restriction" in same_day.reason

    next_day = broker.place_order(Order(
        ts=_cn(10, 0, day=12), mode=Mode.PAPER, symbol=symbol, side=Side.SELL,
        qty=100, order_type=OrderType.LMT, limit=10, tif=TimeInForce.DAY,
    ))
    assert next_day.accepted


def test_currency_view_isolates_cash_positions_and_day_expiry():
    broker = PaperBroker(
        starting_cash_by_currency={"USD": 2_000, "CNY": 100_000}
    )
    usd = broker.place_order(Order(
        ts=_cn(10, 0), mode=Mode.PAPER, symbol="VST", side=Side.BUY, qty=1,
        order_type=OrderType.LMT, limit=100, tif=TimeInForce.DAY,
    ))
    cny = broker.place_order(Order(
        ts=_cn(10, 0), mode=Mode.PAPER, symbol="510300.SS", side=Side.BUY, qty=100,
        order_type=OrderType.LMT, limit=4, tif=TimeInForce.DAY,
    ))
    assert usd.accepted and cny.accepted

    view = CurrencyBrokerView(broker, "CNY")
    assert view.get_account().cash == 100_000
    assert all(order.currency == "CNY" for order in view.get_orders())
    view.end_of_day()
    states = {order.id: order.status for order in broker.get_orders()}
    assert states[cny.order.id] is OrderStatus.EXPIRED
    assert states[usd.order.id] is OrderStatus.SUBMITTED


def test_execution_rechecks_cn_grid_lot_and_session_after_approval(tmp_path):
    broker = PaperBroker(
        starting_cash=1,
        starting_cash_by_currency={"CNY": 100_000},
        base_currency="CNY",
    )
    ledger = Ledger(url=f"sqlite:///{tmp_path / 'cn-execution.db'}")
    engine = ExecutionEngine(broker, ledger, mode=Mode.PAPER)

    def candidate(**updates) -> CandidateOrder:
        base = dict(
            symbol="510300.SS",
            market="CN",
            side=Side.BUY,
            qty=100,
            order_type=OrderType.BRACKET,
            limit=4.321,
            stop=4.000,
            tp=4.800,
            tif=TimeInForce.DAY,
            rationale="paper",
            confidence=0.8,
            ref_px=4.321,
            status=CandidateStatus.APPROVED,
        )
        base.update(updates)
        row = CandidateOrder(**base)
        ledger.record_candidate(row, Mode.PAPER)
        return row

    lunch = candidate()
    assert "market closed / lunch" in engine.execute(
        [lunch], {lunch.symbol: 4.321}, _cn(12, 0)
    ).skipped[0][1]

    off_grid = candidate(limit=4.3215)
    assert "tick grid" in engine.execute(
        [off_grid], {off_grid.symbol: 4.321}, _cn(11, 0)
    ).skipped[0][1]

    off_lot = candidate(qty=150)
    assert "quantity invalid" in engine.execute(
        [off_lot], {off_lot.symbol: 4.321}, _cn(11, 0)
    ).skipped[0][1]

    valid = candidate()
    report = engine.execute([valid], {valid.symbol: 4.321}, _cn(11, 0))
    assert len(report.placed) == 1


def test_cn_discretionary_exit_is_day_limit_not_unsupported_moc():
    symbol = "600519.SS"
    signal = Signal(
        source_agent="debate",
        symbol=symbol,
        direction=Direction.SHORT,
        confidence=0.8,
        thesis="trend invalidated",
    )
    account = AccountSnapshot(
        mode=Mode.PAPER,
        equity=100_000,
        cash=80_000,
        base_currency="CNY",
        cash_by_currency={"CNY": 80_000},
        equity_by_currency={"CNY": 100_000},
        fx_to_base={"CNY": 1.0},
    )
    out = RuleBasedDecisionCore(market_id="CN").propose(
        [signal],
        {symbol: SymbolView(symbol=symbol, last=1340.357, atr_pct=2.0)},
        account,
        [Position(symbol=symbol, currency="CNY", qty=100, avg_px=1300)],
    )
    assert len(out) == 1
    assert out[0].order_type is OrderType.LMT
    assert out[0].tif is TimeInForce.DAY
    assert out[0].limit == pytest.approx(1340.35)
