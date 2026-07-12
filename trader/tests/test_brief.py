"""Tests for swing_trader.brief (Loop.md §7 Phase 0.5, §5.9, §10 backlog #2).

Fully deterministic and network-free: synthetic monitor snapshots, a real
SQLite ledger on tmp_path, and an injected ``now``. Covers the full brief
(hand-checked movers ordering, theme aggregation, top-10 news by
|sentiment|, provenance dedupe), the DEGRADED brief with ALL None inputs,
freshness staleness boundaries, ET trading-date filtering of signals and
candidates, auto-collected uncertainty items, mode stamping, and the
``model_dump(mode="json")`` round trip.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from swing_trader.brief import (
    DATA_SOURCE_NOTE,
    EARNINGS_NOT_WIRED_NOTE,
    ResearchBrief,
    build_research_brief,
)
from swing_trader.ledger import Ledger
from swing_trader.monitors import (
    MarketSnapshot,
    NewsSnapshot,
    PortfolioSnapshot,
    RiskStatus,
    WatchState,
)
from swing_trader.schemas import (
    AccountSnapshot,
    BreakerState,
    CandidateOrder,
    CandidateStatus,
    Direction,
    Mode,
    OrderType,
    Role,
    Side,
    Signal,
)

UTC = timezone.utc

#: Injected clock: 2026-07-10 18:00 UTC == 14:00 ET -> trading date 2026-07-10.
NOW = datetime(2026, 7, 10, 18, 0, tzinfo=UTC)
TODAY_ET = "2026-07-10"
TODAY_TS = NOW - timedelta(hours=1)  # 13:00 ET same trading date
YESTERDAY_TS = NOW - timedelta(hours=26)  # 2026-07-09 ET


# -------------------------------------------------------------------- fixtures


@pytest.fixture()
def ledger(tmp_path) -> Ledger:
    return Ledger(url=f"sqlite:///{tmp_path / 'brief.db'}")


def make_market(ts: datetime = TODAY_TS) -> MarketSnapshot:
    return MarketSnapshot(
        ts=ts,
        indices={"SPY": {"last": 500.0, "sma50_dist_pct": 2.0}},
        vix=18.5,
        breadth_pct_above_50dma=62.0,
        risk_on_off="risk_on",
    )


def make_portfolio(ts: datetime = TODAY_TS) -> PortfolioSnapshot:
    """Hand-checkable watch universe.

    dist_sma20_pct (last vs sma20=100):
      NVDA +10, AMD +8, MU +6, MSFT +4, META +2, SPY +1, ZZZZ +0.5,
      GLD 0, TLT -2, XLE -4, ANET -6, SNOW -10
    dist_sma50_pct: NVDA +10 (sma50=100), AMD +12.5 (sma50=96),
      MU +32.5 (sma50=80); others sma50=None (excluded from themes).
    atr_pct: missing for SNOW and ZZZZ only.
    """
    watch = {
        "NVDA": WatchState(last=110.0, sma20=100.0, sma50=100.0, atr_pct=2.0),
        "AMD": WatchState(last=108.0, sma20=100.0, sma50=96.0, atr_pct=3.0),
        "MU": WatchState(last=106.0, sma20=100.0, sma50=80.0, atr_pct=4.0),
        "MSFT": WatchState(last=104.0, sma20=100.0, atr_pct=1.5),
        "META": WatchState(last=102.0, sma20=100.0, atr_pct=1.5),
        "SPY": WatchState(last=101.0, sma20=100.0, atr_pct=1.0),
        "ZZZZ": WatchState(last=100.5, sma20=100.0),  # not in watchlist
        "GLD": WatchState(last=100.0, sma20=100.0, atr_pct=0.8),
        "TLT": WatchState(last=98.0, sma20=100.0, atr_pct=0.7),
        "XLE": WatchState(last=96.0, sma20=100.0, atr_pct=1.2),
        "ANET": WatchState(last=94.0, sma20=100.0, atr_pct=2.5),
        "SNOW": WatchState(last=90.0, sma20=100.0),  # ATR missing
    }
    return PortfolioSnapshot(ts=ts, positions=[], pool_exposure_pct={}, watch=watch)


def news_item(
    headline: str,
    sentiment: float,
    symbol: str | None = None,
    source: str = "Reuters",
    url: str = "",
) -> dict:
    return {
        "symbol": symbol,
        "ts": TODAY_TS.isoformat(),
        "headline": headline,
        "source": source,
        "url": url,
        "sentiment": sentiment,
    }


def make_news(ts: datetime = TODAY_TS) -> NewsSnapshot:
    """12 items; |sentiment| ranking excludes h11 (0.02) and h12 (0.0)."""
    items = [
        news_item("h1 top", 0.9, "NVDA", url="https://x.com/a"),
        news_item("h2 dupe of h1 url", -0.8, "AMD", url="https://x.com/a"),
        news_item("h3", 0.7, "MU", url="https://x.com/b"),
        news_item("h4", -0.6, url="https://x.com/c"),
        news_item("h5", 0.5),
        news_item("h6", -0.4),
        news_item("h7", 0.3),
        news_item("h8", -0.2),
        news_item("h9", 0.1),
        news_item("h10", 0.05),
        news_item("h11", 0.02),
        news_item("h12", 0.0),
    ]
    return NewsSnapshot(
        ts=ts,
        items=items,
        per_symbol_sentiment={"NVDA": 0.9, "AMD": -0.8, "MU": 0.7, "MARKET": 0.05},
    )


def make_risk(
    ts: datetime = TODAY_TS, breaker: BreakerState = BreakerState.NORMAL
) -> RiskStatus:
    return RiskStatus(
        ts=ts,
        snapshot=AccountSnapshot(
            ts=ts,
            mode=Mode.PAPER,
            equity=100_000.0,
            cash=40_000.0,
            day_pnl=-500.0,
            drawdown_pct=-0.5,
            breaker_state=breaker,
        ),
        per_pool_exposure_pct={Role.CONVICTION: 30.0},
        warnings=["conviction pool exposure 30.0% is high"],
    )


def make_signal(
    ts: datetime,
    symbol: str = "NVDA",
    source_agent: str = "technical",
    confidence: float = 0.7,
    thesis: str = "breakout above SMA20",
) -> Signal:
    return Signal(
        ts=ts,
        source_agent=source_agent,
        symbol=symbol,
        thesis=thesis,
        direction=Direction.LONG,
        confidence=confidence,
    )


def make_candidate(
    ts: datetime,
    symbol: str = "NVDA",
    status: CandidateStatus = CandidateStatus.PROPOSED,
    qty: float = 10.0,
) -> CandidateOrder:
    return CandidateOrder(
        ts=ts,
        symbol=symbol,
        side=Side.BUY,
        qty=qty,
        order_type=OrderType.BRACKET,
        limit=100.0,
        stop=95.0,
        tp=110.0,
        rationale="test candidate",
        confidence=0.7,
        status=status,
        pool=Role.CONVICTION,
    )


def build_full(ledger: Ledger, **kwargs) -> ResearchBrief:
    return build_research_brief(
        ledger,
        Mode.PAPER,
        market=kwargs.pop("market", make_market()),
        portfolio=kwargs.pop("portfolio", make_portfolio()),
        news=kwargs.pop("news", make_news()),
        risk_status=kwargs.pop("risk_status", make_risk()),
        now=kwargs.pop("now", NOW),
        **kwargs,
    )


# ---------------------------------------------------------------- full brief


class TestFullBrief:
    def test_mode_as_of_trading_date(self, ledger: Ledger) -> None:
        brief = build_full(ledger)
        assert brief.mode is Mode.PAPER
        assert brief.as_of == NOW
        assert brief.trading_date == TODAY_ET

    def test_mode_stamped_live_from_string(self, ledger: Ledger) -> None:
        brief = build_research_brief(ledger, "live", now=NOW)
        assert brief.mode is Mode.LIVE
        assert brief.model_dump(mode="json")["mode"] == "live"

    def test_regime_copied_from_market_snapshot(self, ledger: Ledger) -> None:
        brief = build_full(ledger)
        assert brief.regime is not None
        assert brief.regime.risk_on_off == "risk_on"
        assert brief.regime.vix == pytest.approx(18.5)
        assert brief.regime.breadth_pct_above_50dma == pytest.approx(62.0)
        assert brief.regime.indices["SPY"]["last"] == pytest.approx(500.0)

    def test_movers_top_ordering(self, ledger: Ledger) -> None:
        brief = build_full(ledger)
        top = brief.movers.top
        assert [m.symbol for m in top] == ["NVDA", "AMD", "MU", "MSFT", "META"]
        assert top[0].dist_sma20_pct == pytest.approx(10.0)
        assert top[1].dist_sma20_pct == pytest.approx(8.0)
        assert top[0].dist_sma50_pct == pytest.approx(10.0)
        assert top[3].dist_sma50_pct is None  # MSFT has no sma50

    def test_movers_bottom_ordering(self, ledger: Ledger) -> None:
        brief = build_full(ledger)
        bottom = brief.movers.bottom
        assert [m.symbol for m in bottom] == ["SNOW", "ANET", "XLE", "TLT", "GLD"]
        assert bottom[0].dist_sma20_pct == pytest.approx(-10.0)
        assert bottom[-1].dist_sma20_pct == pytest.approx(0.0)

    def test_mover_watchlist_tags_and_unknown_fallback(self, ledger: Ledger) -> None:
        brief = build_full(ledger)
        by_symbol = {m.symbol: m for m in brief.movers.top + brief.movers.bottom}
        nvda = by_symbol["NVDA"]
        assert (nvda.theme, nvda.ai_phase, nvda.role) == (
            "compute-chips",
            "infra",
            "conviction",
        )
        gld = by_symbol["GLD"]
        assert (gld.theme, gld.role) == ("gold", "hedge")
        # ZZZZ is not in the watchlist: mid-range mover, unknown tags
        portfolio = make_portfolio()
        portfolio.watch = {"ZZZZ": WatchState(last=110.0, sma20=100.0)}
        brief2 = build_full(ledger, portfolio=portfolio)
        zzzz = brief2.movers.top[0]
        assert (zzzz.theme, zzzz.ai_phase, zzzz.role) == (
            "unknown",
            "none",
            "rotation",
        )

    def test_themes_aggregation_sorted_desc(self, ledger: Ledger) -> None:
        brief = build_full(ledger)
        # only NVDA/AMD/MU have sma50 -> memory-storage 32.5 > compute-chips 11.25
        assert [t.theme for t in brief.themes] == ["memory-storage", "compute-chips"]
        memory, compute = brief.themes
        assert memory.avg_dist_sma50_pct == pytest.approx(32.5)
        assert memory.n_symbols == 1
        assert memory.leaders == ["MU"]
        assert compute.avg_dist_sma50_pct == pytest.approx((10.0 + 12.5) / 2)
        assert compute.n_symbols == 2
        assert compute.leaders == ["AMD", "NVDA"]  # by dist_sma50 desc, top-2

    def test_news_top10_by_abs_sentiment(self, ledger: Ledger) -> None:
        brief = build_full(ledger)
        items = brief.news.items
        assert len(items) == 10
        assert [i.headline for i in items] == [
            "h1 top", "h2 dupe of h1 url", "h3", "h4", "h5",
            "h6", "h7", "h8", "h9", "h10",
        ]
        headlines = {i.headline for i in items}
        assert "h11" not in headlines and "h12" not in headlines
        assert brief.news.per_symbol_sentiment["NVDA"] == pytest.approx(0.9)

    def test_provenance_dedupes_urls_and_has_data_source_note(
        self, ledger: Ledger
    ) -> None:
        brief = build_full(ledger)
        urls = [p.url for p in brief.provenance]
        assert urls == [
            "https://finance.yahoo.com",  # data-source note first
            "https://x.com/a",  # h1 + h2 share this url -> once
            "https://x.com/b",
            "https://x.com/c",
        ]
        assert brief.provenance[0].label == DATA_SOURCE_NOTE
        assert "h1 top" in brief.provenance[1].label

    def test_risk_view_from_monitor_status(self, ledger: Ledger) -> None:
        ledger.record_snapshot(
            AccountSnapshot(ts=TODAY_TS, mode=Mode.PAPER, equity=1.0, cash=1.0)
        )
        brief = build_full(ledger)
        risk = brief.risk
        assert risk is not None
        assert risk.equity == pytest.approx(100_000.0)
        assert risk.cash == pytest.approx(40_000.0)
        assert risk.day_pnl == pytest.approx(-500.0)
        assert risk.drawdown_pct == pytest.approx(-0.5)
        assert risk.breaker_state == "NORMAL"
        assert risk.pool_exposure_pct == {"conviction": pytest.approx(30.0)}
        assert risk.warnings == ["conviction pool exposure 30.0% is high"]
        assert set(risk.stats) == {
            "n_closed",
            "win_rate",
            "expectancy",
            "max_drawdown_pct",
        }
        assert risk.stats["n_closed"] == 0.0

    def test_breaker_tripped_adds_actionable_warning(self, ledger: Ledger) -> None:
        brief = build_full(ledger, risk_status=make_risk(breaker=BreakerState.TRIPPED))
        assert brief.risk is not None
        assert brief.risk.breaker_state == "TRIPPED"
        assert any("TRIPPED" in w for w in brief.risk.warnings)

    def test_events_declare_earnings_feed_unwired(self, ledger: Ledger) -> None:
        brief = build_full(ledger)
        assert brief.events.earnings == []
        assert EARNINGS_NOT_WIRED_NOTE in brief.events.notes


# ------------------------------------------------------------- degraded brief


class TestDegradedBrief:
    def test_all_none_inputs_no_crash_warnings_name_every_source(
        self, ledger: Ledger
    ) -> None:
        brief = build_research_brief(ledger, Mode.PAPER, now=NOW)
        warnings = " | ".join(brief.freshness.warnings)
        for source in ("market", "news", "portfolio"):
            assert source in warnings and "missing" in warnings
        assert brief.freshness.market_stale
        assert brief.freshness.news_stale
        assert brief.freshness.portfolio_stale
        assert brief.freshness.market_as_of is None
        assert brief.freshness.market_age_minutes is None
        assert brief.regime is None
        assert brief.risk is None  # empty ledger -> no fallback snapshot
        assert brief.movers.top == [] and brief.movers.bottom == []
        assert brief.themes == []
        assert brief.news.items == []
        assert brief.provenance[0].label == DATA_SOURCE_NOTE

    def test_risk_falls_back_to_last_ledger_snapshot(self, ledger: Ledger) -> None:
        ledger.record_snapshot(
            AccountSnapshot(
                ts=YESTERDAY_TS, mode=Mode.PAPER, equity=90_000.0, cash=90_000.0
            )
        )
        ledger.record_snapshot(
            AccountSnapshot(
                ts=TODAY_TS,
                mode=Mode.PAPER,
                equity=99_000.0,
                cash=50_000.0,
                day_pnl=-1_000.0,
                drawdown_pct=-1.0,
            )
        )
        brief = build_research_brief(ledger, Mode.PAPER, now=NOW)
        risk = brief.risk
        assert risk is not None
        assert risk.equity == pytest.approx(99_000.0)  # LAST snapshot wins
        assert risk.drawdown_pct == pytest.approx(-1.0)
        assert risk.pool_exposure_pct == {}
        assert any("risk monitor has not run" in w for w in risk.warnings)

    def test_broken_ledger_never_raises(self) -> None:
        class BrokenLedger:
            def get_signals(self, mode=None, symbol=None):
                raise RuntimeError("db locked")

            def get_candidates(self, mode=None, status=None):
                raise RuntimeError("db locked")

            def get_snapshots(self, mode):
                raise RuntimeError("db locked")

            def stats(self, mode):
                raise RuntimeError("db locked")

        brief = build_research_brief(BrokenLedger(), Mode.PAPER, now=NOW)
        assert brief.risk is None
        assert brief.signals_today == []
        assert brief.candidates_today.counts == {}
        assert any("unavailable" in u for u in brief.uncertainty)


# ----------------------------------------------------------------- freshness


class TestFreshness:
    def test_stale_flag_above_120_minutes(self, ledger: Ledger) -> None:
        old = NOW - timedelta(minutes=121)
        brief = build_full(ledger, market=make_market(ts=old))
        assert brief.freshness.market_stale is True
        assert brief.freshness.market_age_minutes == pytest.approx(121.0)
        assert any(
            "market" in w and "stale" in w for w in brief.freshness.warnings
        )
        # other sources are fresh -> only the market warning
        assert len(brief.freshness.warnings) == 1

    def test_fresh_below_120_minutes(self, ledger: Ledger) -> None:
        recent = NOW - timedelta(minutes=30)
        brief = build_full(
            ledger,
            market=make_market(ts=recent),
            portfolio=make_portfolio(ts=recent),
            news=make_news(ts=recent),
        )
        assert brief.freshness.market_stale is False
        assert brief.freshness.news_stale is False
        assert brief.freshness.portfolio_stale is False
        assert brief.freshness.market_age_minutes == pytest.approx(30.0)
        assert brief.freshness.market_as_of == recent
        assert brief.freshness.warnings == []


# ------------------------------------------------------- today filtering (ET)


class TestTodayFiltering:
    def test_signals_yesterday_excluded(self, ledger: Ledger) -> None:
        ledger.record_signal(make_signal(TODAY_TS, symbol="NVDA"), Mode.PAPER)
        ledger.record_signal(make_signal(YESTERDAY_TS, symbol="AMD"), Mode.PAPER)
        brief = build_full(ledger)
        assert [s.symbol for s in brief.signals_today] == ["NVDA"]

    def test_signal_utc_date_is_yesterday_in_et(self, ledger: Ledger) -> None:
        # 02:00 UTC on 07-10 is 22:00 ET on 07-09 -> NOT today's ET date
        boundary = datetime(2026, 7, 10, 2, 0, tzinfo=UTC)
        ledger.record_signal(make_signal(boundary, symbol="MU"), Mode.PAPER)
        brief = build_full(ledger)
        assert brief.signals_today == []

    def test_signals_debate_first_then_confidence(self, ledger: Ledger) -> None:
        ledger.record_signal(
            make_signal(TODAY_TS, "NVDA", "technical", confidence=0.9), Mode.PAPER
        )
        ledger.record_signal(
            make_signal(TODAY_TS, "NVDA", "debate", confidence=0.5), Mode.PAPER
        )
        ledger.record_signal(
            make_signal(TODAY_TS, "AMD", "sentiment", confidence=0.6), Mode.PAPER
        )
        brief = build_full(ledger)
        agents = [s.source_agent for s in brief.signals_today]
        assert agents == ["debate", "technical", "sentiment"]

    def test_signal_thesis_truncated_to_200(self, ledger: Ledger) -> None:
        ledger.record_signal(
            make_signal(TODAY_TS, thesis="x" * 500), Mode.PAPER
        )
        brief = build_full(ledger)
        thesis = brief.signals_today[0].thesis
        assert len(thesis) <= 200
        assert thesis.endswith("…")

    def test_signals_other_mode_excluded(self, ledger: Ledger) -> None:
        ledger.record_signal(make_signal(TODAY_TS, symbol="NVDA"), Mode.LIVE)
        brief = build_full(ledger)
        assert brief.signals_today == []

    def test_candidates_counts_and_pending_today_only(self, ledger: Ledger) -> None:
        ledger.record_candidate(
            make_candidate(TODAY_TS, "NVDA", CandidateStatus.PROPOSED), Mode.PAPER
        )
        ledger.record_candidate(
            make_candidate(TODAY_TS, "AMD", CandidateStatus.PUSHED, qty=5.0),
            Mode.PAPER,
        )
        ledger.record_candidate(
            make_candidate(TODAY_TS, "MU", CandidateStatus.REJECTED), Mode.PAPER
        )
        ledger.record_candidate(  # yesterday -> excluded entirely
            make_candidate(YESTERDAY_TS, "TSM", CandidateStatus.PROPOSED), Mode.PAPER
        )
        brief = build_full(ledger)
        assert brief.candidates_today.counts == {
            "proposed": 1,
            "pushed": 1,
            "rejected": 1,
        }
        pending = brief.candidates_today.pending
        assert [(p.symbol, p.status) for p in pending] == [
            ("NVDA", "proposed"),
            ("AMD", "pushed"),
        ]
        amd = pending[1]
        assert (amd.side, amd.qty, amd.confidence) == ("BUY", 5.0, 0.7)


# --------------------------------------------------------------- uncertainty


class TestUncertainty:
    def test_auto_items_present(self, ledger: Ledger) -> None:
        brief = build_full(ledger)  # llm_enabled defaults False
        joined = " | ".join(brief.uncertainty)
        assert "ATR unavailable" in joined
        assert "SNOW, ZZZZ" in joined  # sorted missing-ATR symbols
        assert "fundamentals provider may be empty" in joined
        assert EARNINGS_NOT_WIRED_NOTE in brief.uncertainty
        assert "LLM analyst is disabled" in joined

    def test_llm_enabled_flag_flips_item(self, ledger: Ledger) -> None:
        brief = build_full(ledger, llm_enabled=True)
        joined = " | ".join(brief.uncertainty)
        assert "LLM analyst is enabled" in joined
        assert "LLM analyst is disabled" not in joined

    def test_fundamentals_item_absent_when_fundamental_signal_exists(
        self, ledger: Ledger
    ) -> None:
        ledger.record_signal(
            make_signal(TODAY_TS, source_agent="fundamental"), Mode.PAPER
        )
        brief = build_full(ledger)
        assert not any("fundamentals provider" in u for u in brief.uncertainty)


# ------------------------------------------------------------- serialization


class TestSerialization:
    def test_model_dump_json_roundtrip(self, ledger: Ledger) -> None:
        ledger.record_signal(make_signal(TODAY_TS), Mode.PAPER)
        ledger.record_candidate(make_candidate(TODAY_TS), Mode.PAPER)
        brief = build_full(ledger)
        dumped = brief.model_dump(mode="json")
        # fully JSON-serializable (datetimes/enums already primitives)
        text = json.dumps(dumped)
        restored = ResearchBrief.model_validate(json.loads(text))
        assert restored == brief
        assert dumped["mode"] == "paper"
        assert dumped["trading_date"] == TODAY_ET
        assert dumped["as_of"] == NOW.isoformat().replace("+00:00", "Z")

    def test_determinism_same_inputs_same_brief(self, ledger: Ledger) -> None:
        ledger.record_signal(make_signal(TODAY_TS), Mode.PAPER)
        a = build_full(ledger)
        b = build_full(ledger)
        assert a == b
