"""Reporter tests (Loop.md §5.9, §4, §3, §9).

Fully deterministic: an in-test FakeBroker implementing BrokerInterface plus
a real Ledger on tmp_path SQLite, seeded with fills/trades/snapshots in BOTH
modes. No network, fixed timestamps. Covers: view assembly, paper/live
isolation (a paper report never contains live rows and vice versa), empty
ledger sanity ("no fills overnight", "n/a"), number formatting, clamp
truncation, visible breaker-tripped rendering, and no secrets in output.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from swing_trader.interfaces import BrokerInterface, PlaceResult
from swing_trader.ledger import ACTIVE_ORDER_STATUSES, Ledger
from swing_trader.reporter import (
    DEFAULT_MAX_CHARS,
    TRUNCATION_SUFFIX,
    AccountView,
    build_account_view,
    clamp,
    morning_summary,
    push_window_preamble,
    render_account,
)
from swing_trader.schemas import (
    AccountSnapshot,
    BreakerState,
    CandidateOrder,
    CandidateStatus,
    Fill,
    Mode,
    Order,
    OrderStatus,
    OrderType,
    Position,
    Role,
    Side,
    TimeInForce,
)

T0 = datetime(2026, 7, 10, 20, 0, tzinfo=timezone.utc)  # yesterday 16:00 ET
T_REPORT = datetime(2026, 7, 11, 13, 0, tzinfo=timezone.utc)  # next 09:00 ET

SECRET_PATTERN = re.compile(r"(?i)(token|secret|passw|api[_-]?key|bearer|\bsk-)")


# ------------------------------------------------------------------ fakes


class FakeBroker(BrokerInterface):
    """In-test broker: canned state, hard failure on any write attempt."""

    def __init__(
        self,
        snapshot: AccountSnapshot,
        positions: list[Position] | None = None,
        orders: list[Order] | None = None,
        fills: list[Fill] | None = None,
    ) -> None:
        self._snapshot = snapshot
        self._positions = list(positions or [])
        self._orders = list(orders or [])
        self._fills = list(fills or [])

    def get_account(self) -> AccountSnapshot:
        return self._snapshot

    def get_positions(self) -> list[Position]:
        return list(self._positions)

    def place_order(self, order: Order) -> PlaceResult:
        raise AssertionError("reporter must be read-only: place_order called")

    def cancel_order(self, order_id: str) -> bool:
        raise AssertionError("reporter must be read-only: cancel_order called")

    def get_orders(self, active_only: bool = False) -> list[Order]:
        if active_only:
            return [o for o in self._orders if o.status in ACTIVE_ORDER_STATUSES]
        return list(self._orders)

    def get_fills(self) -> list[Fill]:
        return list(self._fills)


# ------------------------------------------------------------------ builders


def make_snapshot(**kw) -> AccountSnapshot:
    base = dict(
        ts=T_REPORT,
        mode=Mode.PAPER,
        equity=10250.0,
        cash=5000.0,
        upnl=125.0,
        day_pnl=50.0,
        drawdown_pct=0.5,
        breaker_state=BreakerState.NORMAL,
    )
    base.update(kw)
    return AccountSnapshot(**base)


def make_position(**kw) -> Position:
    base = dict(symbol="NVDA", qty=10.0, avg_px=100.0, mkt_px=105.0, pool=Role.CONVICTION)
    base.update(kw)
    return Position(**base)


def make_order(**kw) -> Order:
    base = dict(
        ts=T0,
        mode=Mode.PAPER,
        symbol="NVDA",
        side=Side.BUY,
        qty=5.0,
        order_type=OrderType.LMT,
        limit=100.0,
        tif=TimeInForce.GTC,
        status=OrderStatus.SUBMITTED,
    )
    base.update(kw)
    return Order(**base)


def make_fill(**kw) -> Fill:
    base = dict(
        ts=T0 + timedelta(hours=1),
        mode=Mode.PAPER,
        order_id="ord-1",
        symbol="NVDA",
        side=Side.BUY,
        qty=10.0,
        px=100.0,
        commission=1.0,
    )
    base.update(kw)
    return Fill(**base)


def make_candidate(**kw) -> CandidateOrder:
    base = dict(
        ts=T0 + timedelta(hours=1),
        symbol="NVDA",
        side=Side.BUY,
        qty=10.0,
        order_type=OrderType.BRACKET,
        limit=100.0,
        stop=95.0,
        tp=120.0,
        rationale="momentum entry",
        confidence=0.7,
        status=CandidateStatus.APPROVED,
        pool=Role.CONVICTION,
    )
    base.update(kw)
    return CandidateOrder(**base)


@pytest.fixture()
def ledger(tmp_path: Path) -> Ledger:
    return Ledger(url=f"sqlite:///{tmp_path / 'reporter.db'}")


@pytest.fixture()
def seeded_ledger(ledger: Ledger) -> Ledger:
    """Fills/trades/snapshots in BOTH modes: paper NVDA win, live TSLA loss."""
    # paper: closed winning round trip (pnl = +100 - 2 commissions = +98)
    ledger.record_fill(make_fill(side=Side.BUY, px=100.0), stop_px=95.0)
    ledger.record_fill(
        make_fill(ts=T0 + timedelta(hours=2), order_id="ord-2", side=Side.SELL, px=110.0)
    )
    # live: closed losing round trip on a DIFFERENT symbol (pnl < 0)
    ledger.record_fill(
        make_fill(mode=Mode.LIVE, symbol="TSLA", order_id="ord-3", qty=5.0, px=50.0),
        stop_px=48.0,
    )
    ledger.record_fill(
        make_fill(
            ts=T0 + timedelta(hours=2),
            mode=Mode.LIVE,
            symbol="TSLA",
            order_id="ord-4",
            side=Side.SELL,
            qty=5.0,
            px=45.0,
        )
    )
    # snapshots: paper has a 5% max drawdown; live is flat
    for eq in (10000.0, 9500.0, 10500.0):
        ledger.record_snapshot(make_snapshot(equity=eq, ts=T0))
    ledger.record_snapshot(make_snapshot(mode=Mode.LIVE, equity=10000.0, ts=T0))
    return ledger


# ------------------------------------------------------- build_account_view


def test_build_account_view_fields(seeded_ledger: Ledger) -> None:
    broker = FakeBroker(
        make_snapshot(), positions=[make_position()], orders=[make_order()]
    )
    view = build_account_view(broker, seeded_ledger, Mode.PAPER)
    assert isinstance(view, AccountView)
    assert view.mode is Mode.PAPER
    assert view.ts == T_REPORT and view.ts.tzinfo is not None
    assert view.equity == 10250.0
    assert view.cash == 5000.0
    assert view.breaker_state is BreakerState.NORMAL
    assert [p.symbol for p in view.positions] == ["NVDA"]
    assert view.positions[0].upnl == pytest.approx(50.0)  # (105-100)*10
    assert view.positions[0].pool is Role.CONVICTION
    assert [o.symbol for o in view.open_orders] == ["NVDA"]
    assert view.open_orders[0].limit == 100.0
    assert view.stats.n_closed == 1
    assert view.stats.win_rate == pytest.approx(1.0)
    assert view.stats.max_drawdown_pct == pytest.approx(5.0)


def test_build_account_view_filters_orders_by_mode_and_activity(
    seeded_ledger: Ledger,
) -> None:
    orders = [
        make_order(symbol="NVDA"),  # paper, active -> kept
        make_order(symbol="TSLA", mode=Mode.LIVE),  # live -> excluded
        make_order(symbol="AMD", status=OrderStatus.FILLED),  # inactive -> excluded
    ]
    view = build_account_view(
        FakeBroker(make_snapshot(), orders=orders), seeded_ledger, "paper"
    )
    assert [o.symbol for o in view.open_orders] == ["NVDA"]


def test_stats_isolated_per_mode(seeded_ledger: Ledger) -> None:
    broker = FakeBroker(make_snapshot())
    paper = build_account_view(broker, seeded_ledger, Mode.PAPER)
    live = build_account_view(broker, seeded_ledger, Mode.LIVE)
    assert paper.stats.n_closed == 1 and paper.stats.win_rate == pytest.approx(1.0)
    assert live.stats.n_closed == 1 and live.stats.win_rate == pytest.approx(0.0)
    assert paper.stats.total_pnl > 0 > live.stats.total_pnl


# ------------------------------------------------------------ render_account


def test_render_account_numbers_present(seeded_ledger: Ledger) -> None:
    broker = FakeBroker(
        make_snapshot(), positions=[make_position()], orders=[make_order()]
    )
    text = render_account(build_account_view(broker, seeded_ledger, Mode.PAPER))
    assert "[PAPER]" in text
    assert "10,250.00" in text  # equity
    assert "5,000.00" in text  # cash
    assert "+125.00" in text  # upnl
    assert "+50.00" in text  # day pnl and position upnl
    assert "dd 0.50%" in text
    assert "NVDA" in text
    assert "win 100.0%" in text
    assert "max DD 5.00%" in text
    assert "<" not in text and ">" not in text  # no HTML


def test_render_account_empty_sections(ledger: Ledger) -> None:
    text = render_account(build_account_view(FakeBroker(make_snapshot()), ledger, "paper"))
    assert text.count("(none)") == 2  # positions AND open orders
    assert "win n/a" in text  # no closed trades


def test_render_account_breaker_tripped_visible(ledger: Ledger) -> None:
    broker = FakeBroker(make_snapshot(breaker_state=BreakerState.TRIPPED))
    text = render_account(build_account_view(broker, ledger, Mode.PAPER))
    assert "BREAKER TRIPPED — no new entries" in text
    assert "breaker NORMAL" not in text


def test_render_account_clamped(ledger: Ledger) -> None:
    positions = [make_position(symbol=f"SYM{i}") for i in range(200)]
    broker = FakeBroker(make_snapshot(), positions=positions)
    text = render_account(build_account_view(broker, ledger, Mode.PAPER))
    assert len(text) <= DEFAULT_MAX_CHARS
    assert text.endswith(TRUNCATION_SUFFIX)


# ---------------------------------------------------------- morning_summary


def test_morning_summary_lists_overnight_fills_since(seeded_ledger: Ledger) -> None:
    text = morning_summary(
        FakeBroker(make_snapshot()), seeded_ledger, Mode.PAPER, since_utc=T0
    )
    assert "NVDA BUY 10 @ 100.00 (comm 1.00)" in text
    assert "NVDA SELL 10 @ 110.00 (comm 1.00)" in text
    assert "no fills overnight" not in text


def test_morning_summary_excludes_fills_before_since(seeded_ledger: Ledger) -> None:
    text = morning_summary(
        FakeBroker(make_snapshot()),
        seeded_ledger,
        Mode.PAPER,
        since_utc=T0 + timedelta(hours=1, minutes=30),  # after BUY, before SELL
    )
    assert "NVDA BUY" not in text
    assert "NVDA SELL 10 @ 110.00" in text


def test_morning_summary_mode_isolation(seeded_ledger: Ledger) -> None:
    broker = FakeBroker(make_snapshot())
    paper = morning_summary(broker, seeded_ledger, Mode.PAPER, since_utc=T0)
    live = morning_summary(broker, seeded_ledger, Mode.LIVE, since_utc=T0)
    assert "NVDA" in paper and "TSLA" not in paper  # paper report: no live rows
    assert "TSLA" in live and "NVDA" not in live  # live report: no paper rows
    assert "win rate 100.0%" in paper
    assert "win rate 0.0%" in live
    assert "max DD 5.00%" in paper
    assert "max DD 0.00%" in live


def test_morning_summary_empty_ledger(ledger: Ledger) -> None:
    text = morning_summary(FakeBroker(make_snapshot()), ledger, "paper", since_utc=T0)
    assert "no fills overnight" in text
    assert "win rate n/a" in text
    assert "0 trades" in text
    assert "(none)" in text  # positions
    assert "Candidates:" not in text  # no candidate section without candidates


def test_morning_summary_equity_and_breaker_line(ledger: Ledger) -> None:
    text = morning_summary(FakeBroker(make_snapshot()), ledger, Mode.PAPER, since_utc=T0)
    assert "Equity 10,250.00" in text
    assert "day_pnl +50.00" in text
    assert "breaker NORMAL" in text


def test_morning_summary_breaker_tripped_visible(ledger: Ledger) -> None:
    broker = FakeBroker(make_snapshot(breaker_state=BreakerState.TRIPPED))
    text = morning_summary(broker, ledger, Mode.PAPER, since_utc=T0)
    assert "BREAKER TRIPPED — no new entries" in text


def test_morning_summary_candidate_outcomes(seeded_ledger: Ledger) -> None:
    seeded_ledger.record_candidate(make_candidate(status=CandidateStatus.APPROVED), "paper")
    seeded_ledger.record_candidate(make_candidate(status=CandidateStatus.REJECTED), "paper")
    seeded_ledger.record_candidate(make_candidate(status=CandidateStatus.EXPIRED), "paper")
    seeded_ledger.record_candidate(make_candidate(status=CandidateStatus.EXPIRED), "paper")
    # older than the window and wrong mode -> both excluded
    seeded_ledger.record_candidate(
        make_candidate(ts=T0 - timedelta(days=1), status=CandidateStatus.APPROVED), "paper"
    )
    seeded_ledger.record_candidate(make_candidate(status=CandidateStatus.APPROVED), "live")
    text = morning_summary(
        FakeBroker(make_snapshot()), seeded_ledger, Mode.PAPER, since_utc=T0
    )
    assert "Candidates: approved 1 | expired 2 | rejected 1" in text


def test_morning_summary_footer_is_last_line(seeded_ledger: Ledger) -> None:
    broker = FakeBroker(make_snapshot())
    paper = morning_summary(broker, seeded_ledger, Mode.PAPER, since_utc=T0)
    assert paper.splitlines()[-1] == "mode=paper — no live orders possible"
    live = morning_summary(broker, seeded_ledger, Mode.LIVE, since_utc=T0)
    assert live.splitlines()[-1].startswith("mode=live")


def test_morning_summary_rejects_naive_since(ledger: Ledger) -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        morning_summary(
            FakeBroker(make_snapshot()),
            ledger,
            Mode.PAPER,
            since_utc=datetime(2026, 7, 10, 20, 0),  # naive
        )


def test_morning_summary_clamped(ledger: Ledger) -> None:
    for i in range(200):
        ledger.record_fill(make_fill(order_id=f"ord-{i}", symbol=f"SYM{i}"))
    text = morning_summary(FakeBroker(make_snapshot()), ledger, Mode.PAPER, since_utc=T0)
    assert len(text) <= DEFAULT_MAX_CHARS
    assert text.endswith(TRUNCATION_SUFFIX)


# ------------------------------------------------------ push_window_preamble


def test_push_window_preamble_contents() -> None:
    text = push_window_preamble({"risk_on_off": "risk-on", "vix": 15.2, "breadth": 0.62})
    lines = text.splitlines()
    assert 2 <= len(lines) <= 3
    assert "confirm by 12:30 ET" in lines[0]
    assert "risk-on" in text
    assert "VIX 15.20" in text
    assert "breadth 0.62" in text


def test_push_window_preamble_missing_keys() -> None:
    text = push_window_preamble({})
    assert "Market: n/a | VIX n/a | breadth n/a" in text


# ----------------------------------------------------------------- clamp


def test_clamp_noop_when_short() -> None:
    assert clamp("hello", max_chars=10) == "hello"
    assert clamp("") == ""


def test_clamp_truncates_to_limit() -> None:
    out = clamp("x" * 5000)
    assert len(out) == DEFAULT_MAX_CHARS
    assert out.endswith(TRUNCATION_SUFFIX)
    out2 = clamp("x" * 100, max_chars=50)
    assert len(out2) == 50
    assert out2.endswith(TRUNCATION_SUFFIX)


# ----------------------------------------------------------------- secrets


def test_no_secrets_in_any_output(seeded_ledger: Ledger) -> None:
    """Loop.md §3: secrets never in logs, ledger, or (here) report text."""
    broker = FakeBroker(
        make_snapshot(), positions=[make_position()], orders=[make_order()]
    )
    outputs = [
        render_account(build_account_view(broker, seeded_ledger, Mode.PAPER)),
        morning_summary(broker, seeded_ledger, Mode.PAPER, since_utc=T0),
        push_window_preamble({"risk_on_off": "risk-off", "vix": 20.0, "breadth": 0.4}),
    ]
    for text in outputs:
        assert not SECRET_PATTERN.search(text)
