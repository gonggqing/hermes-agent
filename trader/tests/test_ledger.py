"""Ledger tests (Loop.md §5.8, §6, §9).

Fully deterministic: tmp_path file-backed SQLite, fixed timestamps, no
network. Covers roundtrips (values, enums, tz-aware ts), strict paper/live
isolation, trade-pairing math (weighted entries, partial-close splits,
commission attribution, r_multiple), stats math incl. edge cases, and the
update_order upsert.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from swing_trader.ledger import Ledger
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
    Role,
    Side,
    Signal,
    TimeInForce,
)

T0 = datetime(2026, 7, 1, 15, 30, tzinfo=timezone.utc)
SHANGHAI = timezone(timedelta(hours=8))


@pytest.fixture()
def ledger(tmp_path: Path) -> Ledger:
    return Ledger(url=f"sqlite:///{tmp_path / 'ledger.db'}")


# ------------------------------------------------------------------ builders


def make_signal(**kw) -> Signal:
    base = dict(
        source_agent="technical",
        symbol="NVDA",
        thesis="breakout above 20d high",
        direction=Direction.LONG,
        confidence=0.8,
        features_json={"rsi": 55.5, "trend": "up"},
        ts=T0,
    )
    base.update(kw)
    return Signal(**base)


def make_candidate(**kw) -> CandidateOrder:
    base = dict(
        symbol="NVDA",
        side=Side.BUY,
        qty=10.0,
        order_type=OrderType.BRACKET,
        limit=100.0,
        stop=95.0,
        tp=120.0,
        tif=TimeInForce.GTC,
        rationale="momentum entry",
        confidence=0.7,
        signal_ids=["sig-1", "sig-2"],
        ref_px=99.5,
        valid_until=T0 + timedelta(hours=1),
        status=CandidateStatus.PROPOSED,
        pool=Role.CONVICTION,
        ts=T0,
    )
    base.update(kw)
    return CandidateOrder(**base)


def make_order(**kw) -> Order:
    base = dict(
        mode=Mode.PAPER,
        symbol="NVDA",
        side=Side.BUY,
        qty=10.0,
        order_type=OrderType.LMT,
        limit=100.0,
        tif=TimeInForce.GTC,
        ts=T0,
    )
    base.update(kw)
    return Order(**base)


def make_fill(**kw) -> Fill:
    base = dict(
        order_id="ord-entry-1",
        symbol="NVDA",
        side=Side.BUY,
        qty=10.0,
        px=100.0,
        commission=1.0,
        mode=Mode.PAPER,
        ts=T0,
    )
    base.update(kw)
    return Fill(**base)


def make_snapshot(**kw) -> AccountSnapshot:
    base = dict(
        mode=Mode.PAPER,
        equity=10_000.0,
        cash=8_000.0,
        upnl=50.0,
        day_pnl=-20.0,
        drawdown_pct=-0.2,
        breaker_state=BreakerState.NORMAL,
        ts=T0,
    )
    base.update(kw)
    return AccountSnapshot(**base)


# ------------------------------------------------------------------- signals


def test_signal_roundtrip_preserves_fields(ledger: Ledger) -> None:
    sig = make_signal()
    ledger.record_signal(sig, Mode.PAPER)

    got = ledger.get_signals(mode=Mode.PAPER)
    assert len(got) == 1
    g = got[0]
    assert g.id == sig.id
    assert g.source_agent == "technical"
    assert g.symbol == "NVDA"
    assert g.thesis == sig.thesis
    assert g.direction is Direction.LONG
    assert g.confidence == pytest.approx(0.8)
    assert g.features_json == {"rsi": 55.5, "trend": "up"}
    assert g.ts == T0
    assert g.ts.tzinfo is not None


def test_signal_ts_normalized_to_utc(ledger: Ledger) -> None:
    local = datetime(2026, 7, 1, 23, 30, tzinfo=SHANGHAI)
    ledger.record_signal(make_signal(ts=local), Mode.PAPER)

    g = ledger.get_signals(mode=Mode.PAPER)[0]
    assert g.ts == local  # same instant
    assert g.ts.utcoffset() == timedelta(0)  # reconstructed as UTC


def test_signal_mode_isolation(ledger: Ledger) -> None:
    ledger.record_signal(make_signal(symbol="NVDA"), Mode.PAPER)
    ledger.record_signal(make_signal(symbol="AMD"), Mode.LIVE)

    paper = ledger.get_signals(mode=Mode.PAPER)
    live = ledger.get_signals(mode=Mode.LIVE)
    assert [s.symbol for s in paper] == ["NVDA"]
    assert [s.symbol for s in live] == ["AMD"]
    assert len(ledger.get_signals()) == 2  # no filter -> both


def test_signal_symbol_filter(ledger: Ledger) -> None:
    ledger.record_signal(make_signal(symbol="NVDA"), Mode.PAPER)
    ledger.record_signal(make_signal(symbol="AMD"), Mode.PAPER)

    got = ledger.get_signals(mode=Mode.PAPER, symbol="amd")
    assert [s.symbol for s in got] == ["AMD"]


# ---------------------------------------------------------------- candidates


def test_candidate_roundtrip_preserves_fields(ledger: Ledger) -> None:
    c = make_candidate()
    ledger.record_candidate(c, Mode.PAPER)

    got = ledger.get_candidates(mode=Mode.PAPER)
    assert len(got) == 1
    g = got[0]
    assert g.id == c.id
    assert g.symbol == "NVDA"
    assert g.side is Side.BUY
    assert g.qty == pytest.approx(10.0)
    assert g.order_type is OrderType.BRACKET
    assert g.limit == pytest.approx(100.0)
    assert g.stop == pytest.approx(95.0)
    assert g.tp == pytest.approx(120.0)
    assert g.sl is None
    assert g.tif is TimeInForce.GTC
    assert g.rationale == "momentum entry"
    assert g.confidence == pytest.approx(0.7)
    assert g.signal_ids == ["sig-1", "sig-2"]
    assert g.ref_px == pytest.approx(99.5)
    assert g.valid_until == T0 + timedelta(hours=1)
    assert g.valid_until.tzinfo is not None
    assert g.status is CandidateStatus.PROPOSED
    assert g.pool is Role.CONVICTION
    assert g.ts == T0


def test_candidate_mode_and_status_filters(ledger: Ledger) -> None:
    ledger.record_candidate(make_candidate(symbol="NVDA"), Mode.PAPER)
    ledger.record_candidate(
        make_candidate(symbol="AMD", status=CandidateStatus.RISK_APPROVED), Mode.PAPER
    )
    ledger.record_candidate(make_candidate(symbol="TSM"), Mode.LIVE)

    paper = ledger.get_candidates(mode=Mode.PAPER)
    assert {c.symbol for c in paper} == {"NVDA", "AMD"}
    approved = ledger.get_candidates(mode=Mode.PAPER, status=CandidateStatus.RISK_APPROVED)
    assert [c.symbol for c in approved] == ["AMD"]
    assert [c.symbol for c in ledger.get_candidates(mode=Mode.LIVE)] == ["TSM"]


def test_update_candidate_status_and_risk_note(ledger: Ledger) -> None:
    c = make_candidate()
    ledger.record_candidate(c, Mode.PAPER)

    ledger.update_candidate(c.id, CandidateStatus.RISK_VETOED, risk_note="size cap")
    g = ledger.get_candidates(mode=Mode.PAPER)[0]
    assert g.status is CandidateStatus.RISK_VETOED
    assert g.risk_note == "size cap"

    # risk_note=None leaves the existing note untouched
    ledger.update_candidate(c.id, CandidateStatus.EXPIRED)
    g = ledger.get_candidates(mode=Mode.PAPER)[0]
    assert g.status is CandidateStatus.EXPIRED
    assert g.risk_note == "size cap"


def test_update_candidate_unknown_id_raises(ledger: Ledger) -> None:
    with pytest.raises(ValueError, match="unknown candidate id"):
        ledger.update_candidate("nope", CandidateStatus.APPROVED)


# -------------------------------------------------------------------- orders


def test_order_roundtrip_preserves_fields(ledger: Ledger) -> None:
    o = make_order(
        order_type=OrderType.BRACKET,
        limit=100.0,
        stop=95.0,
        tp=120.0,
        status=OrderStatus.SUBMITTED,
        broker_ref="BR-1",
        parent_order_id="parent-1",
        oca_group="oca-1",
        filled_qty=4.0,
        avg_fill_px=99.9,
    )
    ledger.record_order(o)

    got = ledger.get_orders(mode=Mode.PAPER)
    assert len(got) == 1
    g = got[0]
    assert g.id == o.id
    assert g.mode is Mode.PAPER
    assert g.symbol == "NVDA"
    assert g.side is Side.BUY
    assert g.qty == pytest.approx(10.0)
    assert g.order_type is OrderType.BRACKET
    assert (g.limit, g.stop, g.tp) == (pytest.approx(100.0), pytest.approx(95.0), pytest.approx(120.0))
    assert g.tif is TimeInForce.GTC
    assert g.status is OrderStatus.SUBMITTED
    assert g.broker_ref == "BR-1"
    assert g.parent_order_id == "parent-1"
    assert g.oca_group == "oca-1"
    assert g.filled_qty == pytest.approx(4.0)
    assert g.avg_fill_px == pytest.approx(99.9)
    assert g.ts == T0 and g.ts.tzinfo is not None


def test_update_order_upserts_existing(ledger: Ledger) -> None:
    o = make_order()
    ledger.record_order(o)

    updated = o.model_copy(
        update={
            "status": OrderStatus.FILLED,
            "filled_qty": 10.0,
            "avg_fill_px": 99.5,
            "broker_ref": "BR-42",
        }
    )
    ledger.update_order(updated)

    got = ledger.get_orders(mode=Mode.PAPER)
    assert len(got) == 1  # upsert: still one row
    g = got[0]
    assert g.status is OrderStatus.FILLED
    assert g.filled_qty == pytest.approx(10.0)
    assert g.avg_fill_px == pytest.approx(99.5)
    assert g.broker_ref == "BR-42"


def test_update_order_inserts_when_missing(ledger: Ledger) -> None:
    o = make_order(status=OrderStatus.SUBMITTED)
    ledger.update_order(o)  # never recorded before -> insert

    got = ledger.get_orders(mode=Mode.PAPER)
    assert [g.id for g in got] == [o.id]
    assert got[0].status is OrderStatus.SUBMITTED


def test_get_orders_active_only(ledger: Ledger) -> None:
    ledger.record_order(make_order(status=OrderStatus.NEW, ts=T0))
    ledger.record_order(make_order(status=OrderStatus.SUBMITTED, ts=T0 + timedelta(minutes=1)))
    ledger.record_order(
        make_order(
            status=OrderStatus.PARTIALLY_FILLED, filled_qty=5.0, ts=T0 + timedelta(minutes=2)
        )
    )
    ledger.record_order(
        make_order(status=OrderStatus.FILLED, filled_qty=10.0, ts=T0 + timedelta(minutes=3))
    )
    ledger.record_order(make_order(status=OrderStatus.CANCELLED, ts=T0 + timedelta(minutes=4)))

    active = ledger.get_orders(mode=Mode.PAPER, active_only=True)
    assert {o.status for o in active} == {
        OrderStatus.NEW,
        OrderStatus.SUBMITTED,
        OrderStatus.PARTIALLY_FILLED,
    }
    assert len(ledger.get_orders(mode=Mode.PAPER)) == 5


def test_order_mode_isolation(ledger: Ledger) -> None:
    ledger.record_order(make_order(mode=Mode.PAPER, symbol="NVDA"))
    ledger.record_order(make_order(mode=Mode.LIVE, symbol="AMD"))

    assert [o.symbol for o in ledger.get_orders(mode=Mode.PAPER)] == ["NVDA"]
    assert [o.symbol for o in ledger.get_orders(mode=Mode.LIVE)] == ["AMD"]
    assert len(ledger.get_orders()) == 2


# --------------------------------------------------------------------- fills


def test_fill_roundtrip_and_mode_isolation(ledger: Ledger) -> None:
    f_paper = make_fill(mode=Mode.PAPER)
    f_live = make_fill(mode=Mode.LIVE, symbol="AMD", px=150.0, ts=T0 + timedelta(minutes=1))
    ledger.record_fill(f_paper)
    ledger.record_fill(f_live)

    paper = ledger.get_fills(mode=Mode.PAPER)
    assert len(paper) == 1
    g = paper[0]
    assert g.id == f_paper.id
    assert g.order_id == "ord-entry-1"
    assert g.symbol == "NVDA"
    assert g.side is Side.BUY
    assert g.qty == pytest.approx(10.0)
    assert g.px == pytest.approx(100.0)
    assert g.commission == pytest.approx(1.0)
    assert g.mode is Mode.PAPER
    assert g.ts == T0 and g.ts.tzinfo is not None

    assert [f.symbol for f in ledger.get_fills(mode=Mode.LIVE)] == ["AMD"]
    assert len(ledger.get_fills()) == 2


# ------------------------------------------------------------ trade tracking


def test_buy_fill_opens_trade_with_risk(ledger: Ledger) -> None:
    rec = ledger.record_fill(make_fill(px=100.0, qty=10.0, commission=1.0), stop_px=95.0)

    assert rec is not None and rec.is_open
    trades = ledger.get_trades(Mode.PAPER, open_only=True)
    assert len(trades) == 1
    t = trades[0]
    assert t.symbol == "NVDA"
    assert t.mode is Mode.PAPER
    assert t.qty == pytest.approx(10.0)
    assert t.entry_px == pytest.approx(100.0)
    assert t.entry_order_id == "ord-entry-1"
    assert t.risk_per_share == pytest.approx(5.0)
    assert t.entry_commission == pytest.approx(1.0)
    assert t.entry_ts == T0
    assert t.exit_order_id is None and t.exit_px is None and t.pnl is None


def test_buy_fill_without_stop_has_no_risk(ledger: Ledger) -> None:
    ledger.record_fill(make_fill())

    t = ledger.get_trades(Mode.PAPER, open_only=True)[0]
    assert t.risk_per_share is None


def test_additional_buy_weighted_entry(ledger: Ledger) -> None:
    ledger.record_fill(make_fill(qty=10.0, px=100.0, commission=1.0))
    ledger.record_fill(
        make_fill(
            qty=30.0, px=120.0, commission=3.0, order_id="ord-entry-2", ts=T0 + timedelta(hours=1)
        ),
        stop_px=105.0,
    )

    trades = ledger.get_trades(Mode.PAPER)
    assert len(trades) == 1  # merged into one open trade
    t = trades[0]
    assert t.is_open
    assert t.qty == pytest.approx(40.0)
    assert t.entry_px == pytest.approx(115.0)  # (10*100 + 30*120) / 40
    assert t.entry_commission == pytest.approx(4.0)
    assert t.entry_order_id == "ord-entry-1"  # first entry order kept
    assert t.risk_per_share == pytest.approx(10.0)  # 115 - 105


def test_full_close_sets_exit_fields(ledger: Ledger) -> None:
    ledger.record_fill(make_fill(qty=10.0, px=100.0, commission=1.0), stop_px=95.0)
    rec = ledger.record_fill(
        make_fill(
            side=Side.SELL,
            qty=10.0,
            px=110.0,
            commission=1.5,
            order_id="ord-exit-1",
            ts=T0 + timedelta(days=2),
        )
    )

    assert rec is not None and not rec.is_open
    assert ledger.get_trades(Mode.PAPER, open_only=True) == []
    closed = ledger.get_trades(Mode.PAPER, closed_only=True)
    assert len(closed) == 1
    t = closed[0]
    assert t.exit_order_id == "ord-exit-1"
    assert t.exit_px == pytest.approx(110.0)
    assert t.exit_ts == T0 + timedelta(days=2)
    # pnl = (110-100)*10 - 1.0 entry comm - 1.5 exit comm = 97.5
    assert t.pnl == pytest.approx(97.5)
    assert t.hold_days == pytest.approx(2.0)
    # r = per-share pnl / risk-per-share = 9.75 / 5.0
    assert t.r_multiple == pytest.approx(1.95)


def test_partial_close_splits_trade(ledger: Ledger) -> None:
    ledger.record_fill(make_fill(qty=40.0, px=115.0, commission=4.0), stop_px=105.0)
    rec = ledger.record_fill(
        make_fill(
            side=Side.SELL,
            qty=10.0,
            px=130.0,
            commission=2.0,
            order_id="ord-exit-1",
            ts=T0 + timedelta(days=1),
        )
    )

    assert rec is not None and not rec.is_open
    closed = ledger.get_trades(Mode.PAPER, closed_only=True)
    open_ = ledger.get_trades(Mode.PAPER, open_only=True)
    assert len(closed) == 1 and len(open_) == 1

    c = closed[0]
    assert c.qty == pytest.approx(10.0)
    assert c.entry_px == pytest.approx(115.0)
    assert c.exit_px == pytest.approx(130.0)
    # pnl = (130-115)*10 - entry share 4*(10/40)=1.0 - exit 2.0 = 147.0
    assert c.pnl == pytest.approx(147.0)
    assert c.r_multiple == pytest.approx(1.47)  # 14.7 / 10.0
    assert c.hold_days == pytest.approx(1.0)
    assert c.entry_commission == pytest.approx(1.0)
    assert c.exit_commission == pytest.approx(2.0)

    o = open_[0]
    assert o.qty == pytest.approx(30.0)
    assert o.entry_px == pytest.approx(115.0)  # entry unchanged
    assert o.entry_commission == pytest.approx(3.0)  # remaining share
    assert o.pnl is None and o.exit_px is None


def test_partial_then_full_close_commission_attribution(ledger: Ledger) -> None:
    ledger.record_fill(make_fill(qty=40.0, px=115.0, commission=4.0), stop_px=105.0)
    ledger.record_fill(
        make_fill(
            side=Side.SELL, qty=10.0, px=130.0, commission=2.0,
            order_id="ord-exit-1", ts=T0 + timedelta(days=1),
        )
    )
    rec = ledger.record_fill(
        make_fill(
            side=Side.SELL, qty=30.0, px=110.0, commission=3.0,
            order_id="ord-exit-2", ts=T0 + timedelta(days=3),
        )
    )

    assert rec is not None
    assert ledger.get_trades(Mode.PAPER, open_only=True) == []
    closed = ledger.get_trades(Mode.PAPER, closed_only=True)
    assert len(closed) == 2

    final = next(t for t in closed if t.exit_order_id == "ord-exit-2")
    # pnl = (110-115)*30 - remaining entry comm 3.0 - exit comm 3.0 = -156
    assert final.pnl == pytest.approx(-156.0)
    assert final.hold_days == pytest.approx(3.0)
    assert final.r_multiple == pytest.approx(-0.52)  # (-156/30)/10

    # total commissions across the split rows == total commissions paid (4+2+3)
    total_comm = sum(t.entry_commission + t.exit_commission for t in closed)
    assert total_comm == pytest.approx(9.0)
    # gross was flat (150 - 150) -> net pnl == -total commissions
    assert sum(t.pnl for t in closed) == pytest.approx(-9.0)


def test_r_multiple_none_without_stop(ledger: Ledger) -> None:
    ledger.record_fill(make_fill(qty=10.0, px=100.0, commission=0.0))  # no stop_px
    ledger.record_fill(
        make_fill(
            side=Side.SELL, qty=10.0, px=110.0, commission=0.0,
            order_id="ord-exit-1", ts=T0 + timedelta(days=1),
        )
    )

    t = ledger.get_trades(Mode.PAPER, closed_only=True)[0]
    assert t.pnl == pytest.approx(100.0)
    assert t.r_multiple is None


def test_sell_with_no_open_trade_records_fill_only(ledger: Ledger) -> None:
    rec = ledger.record_fill(make_fill(side=Side.SELL, order_id="ord-x"))

    assert rec is None
    assert len(ledger.get_fills(mode=Mode.PAPER)) == 1
    assert ledger.get_trades(Mode.PAPER) == []


def test_sell_exceeding_open_qty_closes_open_only(ledger: Ledger) -> None:
    ledger.record_fill(make_fill(qty=10.0, px=100.0, commission=0.0))
    rec = ledger.record_fill(
        make_fill(
            side=Side.SELL, qty=15.0, px=110.0, commission=3.0,
            order_id="ord-exit-1", ts=T0 + timedelta(days=1),
        )
    )

    assert rec is not None and not rec.is_open
    trades = ledger.get_trades(Mode.PAPER)
    assert len(trades) == 1
    t = trades[0]
    assert t.qty == pytest.approx(10.0)  # only the open qty closed
    # exit commission attributed proportionally: 3.0 * (10/15) = 2.0
    assert t.exit_commission == pytest.approx(2.0)
    assert t.pnl == pytest.approx((110.0 - 100.0) * 10.0 - 2.0)


def test_trade_mode_isolation(ledger: Ledger) -> None:
    ledger.record_fill(make_fill(mode=Mode.PAPER), stop_px=95.0)
    ledger.record_fill(make_fill(mode=Mode.LIVE, px=101.0), stop_px=95.0)
    # SELL in paper must not touch the live trade
    ledger.record_fill(
        make_fill(
            side=Side.SELL, qty=10.0, px=110.0, mode=Mode.PAPER,
            order_id="ord-exit-1", ts=T0 + timedelta(days=1),
        )
    )

    assert ledger.get_trades(Mode.PAPER, open_only=True) == []
    assert len(ledger.get_trades(Mode.PAPER, closed_only=True)) == 1
    live = ledger.get_trades(Mode.LIVE)
    assert len(live) == 1
    assert live[0].is_open
    assert live[0].entry_px == pytest.approx(101.0)


def test_get_trades_open_closed_flags(ledger: Ledger) -> None:
    ledger.record_fill(make_fill(symbol="NVDA"))
    ledger.record_fill(make_fill(symbol="AMD", order_id="ord-2", ts=T0 + timedelta(minutes=1)))
    ledger.record_fill(
        make_fill(
            symbol="NVDA", side=Side.SELL, qty=10.0, px=105.0,
            order_id="ord-exit", ts=T0 + timedelta(days=1),
        )
    )

    assert len(ledger.get_trades(Mode.PAPER)) == 2
    assert [t.symbol for t in ledger.get_trades(Mode.PAPER, open_only=True)] == ["AMD"]
    assert [t.symbol for t in ledger.get_trades(Mode.PAPER, closed_only=True)] == ["NVDA"]
    with pytest.raises(ValueError):
        ledger.get_trades(Mode.PAPER, open_only=True, closed_only=True)


# ----------------------------------------------------------------- snapshots


def test_snapshot_roundtrip_and_mode_isolation(ledger: Ledger) -> None:
    ledger.record_snapshot(make_snapshot(breaker_state=BreakerState.TRIPPED))
    ledger.record_snapshot(
        make_snapshot(mode=Mode.LIVE, equity=500.0, cash=500.0, ts=T0 + timedelta(minutes=1))
    )

    paper = ledger.get_snapshots(Mode.PAPER)
    assert len(paper) == 1
    s = paper[0]
    assert s.mode is Mode.PAPER
    assert s.equity == pytest.approx(10_000.0)
    assert s.cash == pytest.approx(8_000.0)
    assert s.upnl == pytest.approx(50.0)
    assert s.day_pnl == pytest.approx(-20.0)
    assert s.drawdown_pct == pytest.approx(-0.2)
    assert s.breaker_state is BreakerState.TRIPPED
    assert s.ts == T0 and s.ts.tzinfo is not None

    live = ledger.get_snapshots(Mode.LIVE)
    assert len(live) == 1 and live[0].equity == pytest.approx(500.0)


# --------------------------------------------------------------------- stats


def _round_trip(
    ledger: Ledger,
    symbol: str,
    entry_px: float,
    exit_px: float,
    hold_days: float,
    qty: float = 10.0,
    stop_px: float | None = None,
    mode: Mode = Mode.PAPER,
) -> None:
    ledger.record_fill(
        make_fill(symbol=symbol, qty=qty, px=entry_px, commission=0.0,
                  order_id=f"{symbol}-in", mode=mode),
        stop_px=stop_px,
    )
    ledger.record_fill(
        make_fill(
            symbol=symbol, side=Side.SELL, qty=qty, px=exit_px, commission=0.0,
            order_id=f"{symbol}-out", mode=mode, ts=T0 + timedelta(days=hold_days),
        )
    )


def test_stats_empty(ledger: Ledger) -> None:
    st = ledger.stats(Mode.PAPER)
    assert st.n_closed == 0
    assert st.n_wins == 0
    assert st.win_rate == 0.0
    assert st.avg_win is None
    assert st.avg_loss is None
    assert st.payoff_ratio is None
    assert st.expectancy == 0.0
    assert st.total_pnl == 0.0
    assert st.avg_hold_days is None
    assert st.max_drawdown_pct == 0.0


def test_stats_mixed_wins_and_losses(ledger: Ledger) -> None:
    _round_trip(ledger, "AAA", 100.0, 110.0, hold_days=2.0)  # +100
    _round_trip(ledger, "BBB", 100.0, 95.0, hold_days=1.0)  # -50
    _round_trip(ledger, "CCC", 100.0, 105.0, hold_days=3.0)  # +50
    # an open trade must not affect stats
    ledger.record_fill(make_fill(symbol="DDD", order_id="ddd-in"))

    st = ledger.stats(Mode.PAPER)
    assert st.n_closed == 3
    assert st.n_wins == 2
    assert st.win_rate == pytest.approx(2 / 3)
    assert st.avg_win == pytest.approx(75.0)
    assert st.avg_loss == pytest.approx(-50.0)
    assert st.payoff_ratio == pytest.approx(1.5)
    assert st.total_pnl == pytest.approx(100.0)
    assert st.expectancy == pytest.approx(100.0 / 3)
    assert st.avg_hold_days == pytest.approx(2.0)


def test_stats_all_wins_payoff_none(ledger: Ledger) -> None:
    _round_trip(ledger, "AAA", 100.0, 110.0, hold_days=1.0)  # +100
    _round_trip(ledger, "BBB", 100.0, 102.0, hold_days=2.0)  # +20

    st = ledger.stats(Mode.PAPER)
    assert st.n_closed == 2
    assert st.n_wins == 2
    assert st.win_rate == pytest.approx(1.0)
    assert st.avg_win == pytest.approx(60.0)
    assert st.avg_loss is None
    assert st.payoff_ratio is None  # no losses
    assert st.total_pnl == pytest.approx(120.0)


def test_stats_mode_isolation(ledger: Ledger) -> None:
    _round_trip(ledger, "AAA", 100.0, 110.0, hold_days=1.0, mode=Mode.PAPER)
    _round_trip(ledger, "BBB", 100.0, 90.0, hold_days=1.0, mode=Mode.LIVE)

    paper = ledger.stats(Mode.PAPER)
    live = ledger.stats(Mode.LIVE)
    assert paper.n_closed == 1 and paper.total_pnl == pytest.approx(100.0)
    assert live.n_closed == 1 and live.total_pnl == pytest.approx(-100.0)


def test_stats_max_drawdown(ledger: Ledger) -> None:
    equities = [100.0, 120.0, 90.0, 108.0, 80.0]
    for i, eq in enumerate(equities):
        ledger.record_snapshot(
            make_snapshot(equity=eq, cash=eq, ts=T0 + timedelta(days=i))
        )
    # live snapshots must not leak into the paper drawdown
    ledger.record_snapshot(make_snapshot(mode=Mode.LIVE, equity=1.0, cash=1.0))

    st = ledger.stats(Mode.PAPER)
    # peak 120 -> trough 80: (120-80)/120 = 33.333...%
    assert st.max_drawdown_pct == pytest.approx(100.0 * 40.0 / 120.0)


def test_stats_drawdown_zero_with_single_snapshot(ledger: Ledger) -> None:
    ledger.record_snapshot(make_snapshot(equity=100.0))
    st = ledger.stats(Mode.PAPER)
    assert st.max_drawdown_pct == 0.0
