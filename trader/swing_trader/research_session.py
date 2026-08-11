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
from swing_trader.discovery import DiscoveryPool, MarketDiscoveryScanner
from swing_trader.interfaces import DataFeed, NewsItem
from swing_trader.ledger import Ledger
from swing_trader.log import get_logger
from swing_trader.monitors import MarketMonitor, NewsMonitor, PortfolioMonitor
from swing_trader.news_quality import curate_news
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
    so CN research never touches or pollutes the US trading ledger. A supplied
    ``holdings_provider`` is a read-only portfolio projection for analysis.
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
        anchor_symbol: str = "",
        vix_symbol: str = "",
        mode: Mode = Mode.PAPER,
        runtime: Optional[FinanceRuntime] = None,
        notify: Optional[Callable[[str], None]] = None,
        llm_analyst=None,
        knowledge=None,
        knowledge_index=None,
        focus_note: str = "",
        lang: str = "zh",
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        discovery_scanner: Optional[MarketDiscoveryScanner] = None,
        brief_writer=None,
        holdings_provider: Optional[Callable[[str], list]] = None,
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
        self.notify = notify or (lambda text: logger.info("cn notify", extra={"text": text[:200]}))
        self.llm_analyst = llm_analyst
        self.knowledge = knowledge
        self.knowledge_index = knowledge_index
        self.focus_note = focus_note
        self.lang = lang
        self.clock = clock
        self.discovery_scanner = discovery_scanner
        self.brief_writer = brief_writer
        self.holdings_provider = holdings_provider

        self._broker = PaperBroker(starting_cash=_STUB_CASH)
        self.market_monitor = MarketMonitor(
            feed,
            index_symbols=index_symbols or [],
            breadth_symbols=symbols,
            anchor_symbol=anchor_symbol,
            vix_symbol=vix_symbol,
            require_vix_for_risk_on=bool(vix_symbol),
            clock=clock,
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
        self._discovery: Optional[DiscoveryPool] = None

    # ---------------------------------------------------------------- events

    def on_monitor(self) -> None:
        """CN 09:30 — poll monitors, ingest news, publish an early brief."""
        if self.discovery_scanner is not None:
            try:
                self._discovery = self.discovery_scanner.scan(self.market_id)
            except Exception:
                logger.exception("research discovery scan failed", extra={"market": self.market_id})
                self._discovery = None
        discovered = [row.symbol for row in (self._discovery.candidates if self._discovery else [])]
        research_symbols = list(dict.fromkeys([*self.symbols, *discovered]))
        self.market_monitor.set_breadth_symbols(research_symbols)
        try:
            self._market = self.market_monitor.poll()
        except Exception:  # a research session must never crash the process
            logger.exception("research market monitor failed", extra={"market": self.market_id})
            self._market = None
        try:
            self._portfolio = self.portfolio_monitor.poll(research_symbols)
        except Exception:
            logger.exception("research portfolio monitor failed", extra={"market": self.market_id})
            self._portfolio = None
        try:
            self._news = self.news_monitor.poll(research_symbols)
        except Exception:
            logger.exception("research news monitor failed", extra={"market": self.market_id})
            self._news = None
        if self.runtime is not None and self._market is not None:
            market_dump = self._market.model_dump(mode="json")
            self.runtime.market_snapshots[self.market_id.lower()] = market_dump
            if self.market_id.upper() == "CN":
                self.runtime.market_cn = market_dump
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
        # Do not push a contentless brief. This happens when the service is
        # (re)started mid-day AFTER the CN monitor/research events already
        # passed: the runner won't back-fire past events, so on_send fires with
        # no market/portfolio gathered. Still expose the degraded brief to the
        # API (?market=cn), but don't spam the group with an empty push.
        if self._market is None and self._portfolio is None:
            logger.info(
                "cn research: monitors did not run this session; "
                "skipping empty brief push (API brief still refreshed)"
            )
            return
        text = render_research_brief(
            brief,
            market_label=self.market_label,
            focus_note=self.focus_note,
            lang=self.lang,
        )
        self.notify(text)
        logger.info("cn research brief sent", extra={"market": self.market_id})

    def run_now(self, *, send: bool = False) -> dict:
        """Manually re-run this research market NOW (off-schedule catch-up /
        refresh button). Forces fresh monitors + rebuilds the brief (which
        refreshes ``runtime.latest_briefs[market_id]`` via ``_publish_brief``);
        ``send=True`` also pushes it to the REPORTER bot. Returns a summary.

        Read-only (no orders): safe to trigger from any surface (Loop.md §3 is
        not implicated), unlike the human-gated trading-session trigger."""
        self.on_monitor()
        self.on_research()
        if send:
            self.on_send()
        ready = self.runtime is not None and self.market_id.lower() in self.runtime.latest_briefs
        return {
            "market": self.market_id,
            "market_label": self.market_label,
            "ran_at": self.clock().isoformat(),
            "signals": len(self._signals),
            "sent": bool(send),
            "brief_ready": ready,
        }

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
        discovered = {row.symbol for row in (self._discovery.candidates if self._discovery else [])}
        analysis_symbols = list(dict.fromkeys([*self.symbols, *sorted(discovered)]))
        for symbol in analysis_symbols:
            if symbol not in discovered and watch.get(symbol) is None:
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
        return curate_news(items, as_of=self.clock(), across_symbols=False).items

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
                watchlist_lookup=self._research_lookup,
                display_name_lookup=self._display_name,
                trading_tz=self.trading_tz,
                include_account=False,
                extra_uncertainty=[
                    f"{self.market_label} session is RESEARCH-ONLY — no orders "
                    "are placed (Loop.md two-session extension)",
                ],
                discovery=self._discovery,
                additional_holdings=(
                    self.holdings_provider(self.market_id)
                    if self.holdings_provider is not None
                    else []
                ),
            )
            if self.brief_writer is not None:
                brief.narrative = self.brief_writer.write(
                    brief,
                    market_id=self.market_id,
                    market_label=self.market_label,
                    language="zh-CN" if self.lang == "zh" else self.lang,
                )
            elif self.runtime is not None:
                previous = self.runtime.latest_briefs.get(self.market_id.lower())
                if isinstance(previous, dict) and previous.get("narrative") is not None:
                    from swing_trader.brief import ResearchNarrative

                    brief.narrative = ResearchNarrative.model_validate(previous["narrative"])
        except Exception:  # brief must never break the loop
            logger.exception("cn research brief build failed")
            return None
        if self.runtime is not None:
            dump = brief.model_dump(mode="json")
            # Per-market slot (cn/kr/…) so any research market routes uniformly.
            self.runtime.latest_briefs[self.market_id.lower()] = dump
            if self.market_id.upper() == "CN":
                self.runtime.latest_brief_cn = dump  # back-compat
            # Structured intraday evidence is volatile by design. The unified
            # 09:00/21:00 coordinator adds the primary-model narrative and is
            # the sole publisher to the durable brief/prediction ledgers.
        return brief

    def _discovery_candidate(self, symbol: str):
        if self._discovery is None:
            return None
        canonical = symbol.strip().upper()
        return next(
            (row for row in self._discovery.candidates if row.symbol.strip().upper() == canonical),
            None,
        )

    def _research_lookup(self, symbol: str) -> Optional[WatchlistItem]:
        """Metadata for static anchors plus this run's dynamic discoveries."""

        static = self.watchlist_lookup(symbol)
        if static is not None:
            return static
        candidate = self._discovery_candidate(symbol)
        if candidate is None:
            return None
        from swing_trader.schemas import AiPhase, Role

        return WatchlistItem(
            symbol=candidate.symbol,
            theme=candidate.theme,
            ai_phase=AiPhase.NONE,
            role=Role.ROTATION,
        )

    def _display_name(self, symbol: str) -> str:
        from swing_trader.instrument_names import name_for

        candidate = self._discovery_candidate(symbol)
        return candidate.display_name if candidate is not None else name_for(symbol)

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
