"""Research-only market session (Loop.md two-session extension).

The daily loop now runs TWO sessions in one process:

- the US **evening** trading session (:class:`~swing_trader.dailyloop.DailyLoop`,
  ET schedule) — full monitor → decide → confirm → execute flow; and
- the China **morning** RESEARCH session (this module, Asia/Shanghai schedule)
  — a lighter, technology-focused Investment Research brief with NO orders,
  NO confirmation window, and NO execution ("not place order in CN for now,
  but build the ability for future").

:class:`ResearchSession` reuses the same monitors, analysis sub-agents, and
:func:`~swing_trader.brief.build_research_brief` as the US session, so a future
upgrade to an order-capable CN session is a matter of adding a decision core +
RiskEngine + ExecutionEngine — the authority boundaries (Loop.md §3) are
untouched here because this session simply never proposes an order.

Reporting: the brief is pushed by the REPORTER bot (``notify`` — the shared
gateway token, outbound-only). The interactive GATEKEEPER (finance) bot is
never used here; there is nothing to approve.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Callable, Optional
from zoneinfo import ZoneInfo

from swing_trader.analysis import DebateAgent, SentimentAgent, TechnicalAgent
from swing_trader.api import FinanceRuntime
from swing_trader.brief import build_research_brief
from swing_trader.brief_telegram import render_research_brief
from swing_trader.datafeed import DataFeedError
from swing_trader.interfaces import DataFeed, NewsItem
from swing_trader.ledger import Ledger
from swing_trader.log import get_logger
from swing_trader.monitors import MarketMonitor, NewsMonitor, PortfolioMonitor
from swing_trader.paper_broker import PaperBroker
from swing_trader.scheduler import Event
from swing_trader.schemas import Mode, Signal
from swing_trader.watchlist import WatchlistItem

logger = get_logger(__name__)

__all__ = ["ResearchSession"]

#: Nominal cash for the research broker stub (never trades; keeps account math
#: well-defined for the PortfolioMonitor's watch-state build).
_STUB_CASH = 1.0

#: Bars pulled per symbol for the analysis sub-agents (TechnicalAgent needs
#: enough history for SMA50/ATR).
_ANALYSIS_BARS = 120


class ResearchSession:
    """A research-only market session that publishes a daily brief.

    Parameters mirror what a research brief needs; there is deliberately no
    broker/execution/confirmation wiring. ``ledger`` is required only to
    satisfy :func:`build_research_brief`'s signature — it is NEVER read here
    (``include_account=False`` and in-memory ``signals``/``candidates=[]``),
    so CN research never touches or pollutes the US trading ledger.
    """

    def __init__(
        self,
        *,
        market_id: str,
        market_label: str,
        feed: DataFeed,
        ledger: Ledger,
        symbols: list[str],
        watchlist_lookup: Callable[[str], Optional[WatchlistItem]],
        trading_tz: ZoneInfo,
        index_symbols: Optional[list[str]] = None,
        mode: Mode = Mode.PAPER,
        runtime: Optional[FinanceRuntime] = None,
        notify: Optional[Callable[[str], None]] = None,
        llm_analyst=None,
        knowledge=None,
        knowledge_index=None,
        focus_note: str = "",
        lang: str = "zh",
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        self.market_id = market_id
        self.market_label = market_label
        self.feed = feed
        self.ledger = ledger
        self.symbols = symbols
        self.watchlist_lookup = watchlist_lookup
        self.trading_tz = trading_tz
        self.mode = mode
        self.runtime = runtime
        self.notify = notify or (
            lambda text: logger.info("cn notify", extra={"text": text[:200]})
        )
        self.llm_analyst = llm_analyst
        self.knowledge = knowledge
        self.knowledge_index = knowledge_index
        self.focus_note = focus_note
        self.lang = lang
        self.clock = clock

        self._broker = PaperBroker(starting_cash=_STUB_CASH)
        self.market_monitor = MarketMonitor(
            feed,
            index_symbols=index_symbols or [],
            breadth_symbols=symbols,
        )
        self.portfolio_monitor = PortfolioMonitor(feed, self._broker, symbols=symbols)
        self.news_monitor = NewsMonitor(feed)
        self.tech = TechnicalAgent()
        self.senti = SentimentAgent()
        self.debate = DebateAgent()

        self._market = None
        self._portfolio = None
        self._news = None
        self._signals: list[Signal] = []

    # ---------------------------------------------------------------- events

    def on_monitor(self) -> None:
        """CN 09:30 — poll monitors, ingest news, publish an early brief."""
        try:
            self._market = self.market_monitor.poll()
        except Exception:  # a research session must never crash the process
            logger.exception("cn market monitor failed")
            self._market = None
        try:
            self._portfolio = self.portfolio_monitor.poll()
        except Exception:
            logger.exception("cn portfolio monitor failed")
            self._portfolio = None
        try:
            self._news = self.news_monitor.poll(self.symbols)
        except Exception:
            logger.exception("cn news monitor failed")
            self._news = None
        if self.runtime is not None and self._market is not None:
            self.runtime.market_cn = self._market.model_dump(mode="json")
        self._ingest_news()
        self._publish_brief()

    def on_research(self) -> None:
        """CN 11:00 — build the analysis signals, refresh the brief."""
        if self._portfolio is None:  # monitors have not run yet
            self.on_monitor()
        self._signals = self._build_signals()
        self._publish_brief()
        logger.info(
            "cn research complete",
            extra={"market": self.market_id, "signals": len(self._signals)},
        )

    def on_send(self) -> None:
        """CN 11:30 — send the research brief to the group (REPORTER bot)."""
        brief = self._publish_brief()
        if brief is None:
            return
        text = render_research_brief(
            brief,
            market_label=self.market_label,
            focus_note=self.focus_note,
            lang=self.lang,
        )
        self.notify(text)
        logger.info("cn research brief sent", extra={"market": self.market_id})

    def callbacks(self) -> dict[Event, Callable[[], None]]:
        return {
            Event.MONITOR_START: self.on_monitor,
            Event.DECIDE_START: self.on_research,
            Event.PUSH_CANDIDATES: self.on_send,
        }

    # -------------------------------------------------------------- internals

    def _build_signals(self) -> list[Signal]:
        """Technical + sentiment (+ optional LLM) → per-symbol debate verdicts.

        No FundamentalAgent (CN fundamentals are not wired) and no decision
        core: this session forms theses for the brief only, never candidates.
        """
        out: list[Signal] = []
        watch = self._portfolio.watch if self._portfolio else {}
        news_items = self._news_items()
        regime = self._market.risk_on_off if self._market else "neutral"
        for symbol in self.symbols:
            if watch.get(symbol) is None:
                continue
            try:
                bars = self.feed.get_bars(symbol, "1d", limit=_ANALYSIS_BARS)
            except (DataFeedError, ValueError):
                continue  # mainland symbols with no free data degrade out
            per: list[Signal] = []
            tech = self.tech.analyze(symbol, bars)
            if tech is not None:
                per.append(tech)
            sym_news = [n for n in news_items if n.symbol == symbol]
            senti = self.senti.analyze(symbol, sym_news)
            if senti is not None:
                per.append(senti)
            if self.llm_analyst is not None and tech is not None:
                llm_sig = self.llm_analyst.analyze(
                    symbol,
                    features=tech.features_json,
                    headlines=[n.headline for n in sym_news],
                    regime=regime,
                )
                if llm_sig is not None:  # fail-safe: None on any LLM trouble
                    per.append(llm_sig)
            if not per:
                continue
            out.extend(per)
            out.append(self.debate.debate(symbol, per))
        return out

    def _news_items(self) -> list[NewsItem]:
        if self._news is None:
            return []
        items: list[NewsItem] = []
        for raw in self._news.items:
            try:
                items.append(
                    NewsItem(
                        symbol=raw.get("symbol"),
                        ts=datetime.fromisoformat(raw["ts"]),
                        headline=raw.get("headline", ""),
                        source=raw.get("source", ""),
                        url=raw.get("url", ""),
                        sentiment=raw.get("sentiment"),
                    )
                )
            except (KeyError, ValueError, TypeError):
                continue
        return items

    def _publish_brief(self):
        """Build the CN research brief and expose it via the runtime."""
        try:
            brief = build_research_brief(
                self.ledger,
                self.mode,
                market=self._market,
                portfolio=self._portfolio,
                news=self._news,
                llm_enabled=self.llm_analyst is not None,
                now=self.clock(),
                signals=list(self._signals),
                candidates=[],  # report-only: the CN session never proposes orders
                watchlist_lookup=self.watchlist_lookup,
                trading_tz=self.trading_tz,
                include_account=False,
                extra_uncertainty=[
                    f"{self.market_label} session is RESEARCH-ONLY — no orders "
                    "are placed (Loop.md two-session extension)",
                ],
            )
        except Exception:  # brief must never break the loop
            logger.exception("cn research brief build failed")
            return None
        if self.runtime is not None:
            self.runtime.latest_brief_cn = brief.model_dump(mode="json")
        return brief

    def _ingest_news(self) -> None:
        """Archive CN news into the shared knowledge store (fail-closed)."""
        if self.knowledge is None or self._news is None:
            return
        from swing_trader.knowledge_pipeline import ingest_news_snapshot

        try:
            trading_date = self.clock().astimezone(self.trading_tz).date()
            report = ingest_news_snapshot(
                self.knowledge, self.knowledge_index, self._news, trading_date
            )
            logger.info(
                "cn news ingested",
                extra={"n_docs": report.n_docs_written, "vector_ok": report.vector_ok},
            )
        except Exception:
            logger.exception("cn knowledge ingestion failed (fail-closed)")
