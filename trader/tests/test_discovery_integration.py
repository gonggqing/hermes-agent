"""Discovery enters the existing research/risk path, never a side door."""

from datetime import date, datetime, time, timezone
from zoneinfo import ZoneInfo

from swing_trader.dailyloop import DailyLoop
from swing_trader.discovery import (
    DiscoveryCandidate,
    DiscoveryFeatures,
    DiscoveryPool,
)
from swing_trader.ledger import Ledger
from swing_trader.paper_broker import PaperBroker
from swing_trader.simulate import MutableClock, SimFeed, build_sim_series, trading_days

ET = ZoneInfo("America/New_York")


class OneDiscovery:
    def __init__(self, now):
        self.now = now

    def scan(self, market):
        assert market == "US"
        return DiscoveryPool(
            market="US", as_of=self.now(), source_count=1,
            candidates=[DiscoveryCandidate(
                symbol="NEW", display_name="New Research Co", market="US",
                exchange="NASDAQ", currency="USD", theme="automation",
                component="precision motion", relationship="validated test edge",
                score=80, rank=1, reasons=["relative strength"],
                features=DiscoveryFeatures(
                    adv20=10_000_000, volume_ratio=2, trend_20d_pct=10,
                    relative_strength_20d_pct=5, news_heat=.5,
                    event_score=.5, moat_score=.8, etf_change_score=0,
                ),
                evidence=[],
            )],
        )


def test_discovered_symbol_uses_normal_decision_risk_confirmation_path(tmp_path):
    days = trading_days(date(2026, 7, 13), 2)
    series, warmup = build_sim_series(["NVDA", "NEW"], days)
    feed = SimFeed(series)
    feed.set_day(warmup)
    clock = MutableClock(datetime.combine(days[0], time(9, 30), tzinfo=ET)
                         .astimezone(timezone.utc))
    broker = PaperBroker(starting_cash=5_000)
    ledger = Ledger(url=f"sqlite:///{tmp_path/'discovery.db'}")
    loop = DailyLoop(
        feed, broker, ledger, symbols=["NVDA"], clock=clock,
        discovery_scanner=OneDiscovery(clock),
    )

    loop.on_monitor()
    loop.on_decide()

    assert "NEW" in loop._portfolio.watch
    assert any(row.symbol == "NEW" for row in ledger.get_signals())
    assert any(row.symbol == "NEW" for row in ledger.get_candidates())
    # Decision + Risk only prepare candidates. No human confirmation and no
    # ExecutionEngine cutoff means no broker order can exist.
    assert broker.get_orders() == []
