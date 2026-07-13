"""Daily-loop orchestrator (Loop.md §4 state machine, backlog 17).

Wires every module into the scheduled day:

  09:00 MORNING_REPORT  sync fills, snapshot, memory update, morning summary
  09:30 MONITOR_START   market / portfolio / news monitors poll
  11:00 DECIDE_START    sub-agents → debate → decision core → RiskEngine
  11:30 PUSH_CANDIDATES publish to the ConfirmationService (portal) + Telegram
  12:30 CONFIRM_CUTOFF  expire stragglers; execute human-approved candidates
  16:00 MARKET_CLOSE    feed daily bars to the PaperBroker; sync fills; snapshot

Authority chain (Loop.md §3): decision core PROPOSES → RiskEngine (pure code)
approves/shrinks/vetoes → ConfirmationService collects HUMAN approval from
Desktop/Web/Telegram → ExecutionEngine re-validates and places. This class
only moves data between those parties; it holds no approval power itself.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from typing import Callable, Optional

from swing_trader.analysis import (
    DebateAgent,
    FundamentalAgent,
    FundamentalsProvider,
    SentimentAgent,
    StaticFundamentals,
    TechnicalAgent,
)
from swing_trader.api import FinanceRuntime
from swing_trader.confirmation import ConfirmationService, Surface
from swing_trader.datafeed import DataFeedError
from swing_trader.decision import RuleBasedDecisionCore, SymbolView
from swing_trader.execution import ExecutionEngine
from swing_trader.interfaces import BrokerInterface, DataFeed, NewsItem
from swing_trader.ledger import Ledger
from swing_trader.log import get_logger
from swing_trader.monitors import (
    AccountRiskMonitor,
    MarketMonitor,
    NewsMonitor,
    PortfolioMonitor,
)
from swing_trader.reporter import morning_summary, push_window_preamble
from swing_trader.risk import RiskEngine, RiskParams
from swing_trader.scheduler import Event
from swing_trader.schemas import CandidateOrder, Mode, Role, Side, Signal
from swing_trader.telegram_gateway import (
    CALLBACK_ID_LEN,
    TelegramTransport,
    build_keyboard,
    render_card,
)
from swing_trader import watchlist as watchlist_mod

logger = get_logger(__name__)

__all__ = ["DailyLoop", "TelegramSurfaceAdapter"]


class TelegramSurfaceAdapter:
    """Telegram as ONE surface of the shared ConfirmationService.

    Approve / reject flow through :meth:`ConfirmationService.act` exactly like
    Desktop/Web — same idempotency, same audit trail. Edits are directed to
    the portal (keeping the Telegram side minimal and the state machine
    single-authority; Loop.md §5.6).
    """

    def __init__(
        self,
        transport: TelegramTransport,
        chat_id: str,
        interactive: bool = True,
        allowed_users: Optional[set[str]] = None,
    ) -> None:
        """``interactive=False`` = OUTBOUND ONLY (cards/reports are sent, but
        poll() is a no-op). Required when the Hermes gateway long-polls
        getUpdates with the SAME bot token — two consumers would 409 each
        other off Telegram. Use a DEDICATED finance bot token for interactive
        approvals (Loop.md Phase 0.5).

        ``allowed_users``: Telegram usernames/ids permitted to act (§5.6:
        authenticated actor). Interactive mode with an EMPTY allowlist
        refuses every action — auth must be explicit, never open."""
        self._transport = transport
        self._chat_id = chat_id
        self.interactive = interactive
        self._allowed_users = {u.strip().lower().lstrip("@")
                               for u in (allowed_users or set()) if u.strip()}
        self._by_short_id: dict[str, str] = {}
        self._offset: Optional[int] = None

    def _is_authorized(self, sender: dict) -> bool:
        username = str(sender.get("username", "")).lower()
        user_id = str(sender.get("id", ""))
        return bool(self._allowed_users) and (
            username in self._allowed_users or user_id in self._allowed_users
        )

    def push_cards(self, candidates: list[CandidateOrder], preamble: str = "") -> None:
        if preamble:
            self._transport.send_message(self._chat_id, preamble)
        for cand in candidates:
            self._by_short_id[cand.id[:CALLBACK_ID_LEN]] = cand.id
            self._transport.send_message(
                self._chat_id, render_card(cand), reply_markup=build_keyboard(cand)
            )

    def poll(self, service: ConfirmationService, now_utc: datetime) -> None:
        if not self.interactive:
            return  # outbound-only: never touch getUpdates (see __init__)
        updates = self._transport.get_updates(offset=self._offset)
        for update in updates:
            update_id = update.get("update_id")
            if isinstance(update_id, int):
                self._offset = update_id + 1
            callback = update.get("callback_query")
            if not callback:
                continue
            cb_id = str(callback.get("id", ""))
            try:
                data = json.loads(callback.get("data", "") or "{}")
            except json.JSONDecodeError:
                self._transport.answer_callback(cb_id, "unrecognized action")
                continue
            full_id = self._by_short_id.get(str(data.get("id", "")))
            action = {"ok": "approve", "no": "reject"}.get(data.get("a"))
            if full_id is None or action is None:
                if data.get("a") == "edit":
                    self._transport.answer_callback(
                        cb_id, "edit via the Finance portal (Desktop/Web)"
                    )
                else:
                    self._transport.answer_callback(cb_id, "unknown candidate")
                continue
            sender = callback.get("from", {}) or {}
            if not self._is_authorized(sender):
                logger.warning(
                    "unauthorized telegram action refused",
                    extra={"sender_id": str(sender.get("id", "?"))},
                )
                self._transport.answer_callback(
                    cb_id, "not authorized for finance approvals"
                )
                continue
            actor = f"telegram:{sender.get('username') or sender.get('id') or 'user'}"
            result = service.act(
                full_id, action, actor=actor, surface=Surface.TELEGRAM,
                idempotency_key=f"tg:{cb_id}", now_utc=now_utc,
            )
            self._transport.answer_callback(cb_id, result.message[:180])


class DailyLoop:
    """One instance per process; drives one mode (paper in Phase 0)."""

    def __init__(
        self,
        feed: DataFeed,
        broker: BrokerInterface,
        ledger: Ledger,
        mode: Mode = Mode.PAPER,
        risk_params: RiskParams | None = None,
        symbols: list[str] | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        runtime: FinanceRuntime | None = None,
        telegram: TelegramSurfaceAdapter | None = None,
        notify: Callable[[str], None] | None = None,
        fundamentals: FundamentalsProvider | None = None,
        decision_core: RuleBasedDecisionCore | None = None,
        llm_analyst=None,  # Optional[swing_trader.llm.LLMAnalyst] — analysis only (§3)
        knowledge=None,  # Optional[FinanceKnowledge] (Phase 0.5 ingestion)
        knowledge_index=None,  # Optional[KnowledgeIndex] — None = fail-closed
    ) -> None:
        self.feed = feed
        self.broker = broker
        self.ledger = ledger
        self.mode = mode
        self.clock = clock
        self.risk_params = risk_params or RiskParams()
        self.symbols = symbols or watchlist_mod.enabled_symbols()
        self.runtime = runtime
        self.telegram = telegram
        self.notify = notify or (lambda text: logger.info("notify", extra={"text": text[:200]}))

        self.risk_engine = RiskEngine(self.risk_params)
        self.decision = decision_core or RuleBasedDecisionCore(risk_params=self.risk_params)
        self.execution = ExecutionEngine(broker, ledger, mode=mode)
        self.market_monitor = MarketMonitor(feed, breadth_symbols=self.symbols)
        self.portfolio_monitor = PortfolioMonitor(feed, broker, symbols=self.symbols)
        self.news_monitor = NewsMonitor(feed)
        self.account_monitor = AccountRiskMonitor(broker, self.risk_params)
        self.tech = TechnicalAgent()
        self.fund = FundamentalAgent(fundamentals or StaticFundamentals({}))
        self.senti = SentimentAgent()
        self.debate = DebateAgent()
        self.llm_analyst = llm_analyst
        self.knowledge = knowledge
        self.knowledge_index = knowledge_index

        self._market = None
        self._portfolio = None
        self._news = None
        self._risk_approved: list[CandidateOrder] = []
        self._confirmation: Optional[ConfirmationService] = None
        self._entries_placed_today = 0
        self._memory_seen_trades: set[str] = set()

    # ---------------------------------------------------------------- events

    def on_morning_report(self) -> None:
        now = self.clock()
        self.execution.sync_fills()
        self._update_memory_outcomes()
        self.broker.start_of_day()
        status = self.account_monitor.poll()
        self.ledger.record_snapshot(status.snapshot)
        text = morning_summary(self.broker, self.ledger, self.mode,
                               since_utc=now - timedelta(hours=24))
        if self.runtime is not None:
            self.runtime.latest_reports["morning"] = text
        self.notify(text)
        logger.info("morning report done", extra={"warnings": status.warnings})

    def on_monitor(self) -> None:
        self._market = self.market_monitor.poll()
        self._portfolio = self.portfolio_monitor.poll()
        self._news = self.news_monitor.poll(self.symbols)
        if self.runtime is not None:
            self.runtime.market = self._market.model_dump(mode="json")
        self._ingest_news()
        self._publish_brief()

    def on_decide(self) -> None:
        if self._portfolio is None:  # monitors have not run (fresh start mid-day)
            self.on_monitor()
        self._entries_placed_today = 0
        debates, views = self._build_signals()
        status = self.account_monitor.poll()
        account = status.snapshot
        positions = self._portfolio.positions
        open_syms = {o.symbol for o in self.broker.get_orders(active_only=True)}

        candidates = self.decision.propose(
            debates, views, account, positions,
            risk_on_off=self._market.risk_on_off if self._market else "neutral",
            open_order_symbols=open_syms,
        )

        self._risk_approved = []
        entries_seen = 0
        for cand in candidates:
            self.ledger.record_candidate(cand, self.mode)
            liquidity = self.portfolio_monitor.liquidity_for(cand.symbol)
            decision = self.risk_engine.evaluate(
                cand, account, positions, liquidity, entries_today=entries_seen
            )
            self.ledger.update_candidate(
                cand.id, decision.candidate.status,
                risk_note=decision.candidate.risk_note,
            )
            if decision.approved:
                if decision.candidate.side is Side.BUY:
                    entries_seen += 1
                self._risk_approved.append(decision.candidate)

        self._confirmation = ConfirmationService(
            self.ledger, mode=self.mode, revalidate=self._revalidate_edit
        )
        if self.runtime is not None:
            self.runtime.confirmation = self._confirmation
        self._publish_brief()  # refresh with today's signals/candidates
        logger.info(
            "decide complete",
            extra={"proposed": len(candidates),
                   "risk_approved": len(self._risk_approved)},
        )

    def on_push(self) -> None:
        if self._confirmation is None or not self._risk_approved:
            logger.info("nothing to push")
            return
        now = self.clock()
        published = self._confirmation.publish(self._risk_approved, now)
        if not published:
            return
        preamble = push_window_preamble(
            self._market.model_dump(mode="json") if self._market else {}
        )
        # Distinct bot roles (Loop.md two-session extension): the REPORTER bot
        # (``notify`` — shared gateway token, outbound-only) announces the push
        # context to the group; the GATEKEEPER bot (``self.telegram`` —
        # dedicated finance token) sends the interactive approval cards and ONLY
        # asks for permission. Both live in the same chat.
        self.notify(preamble)
        if self.telegram is not None:
            self.telegram.push_cards(published)
        else:
            self.notify(
                f"{len(published)} candidate(s) await review in the Finance "
                "portal (no interactive Telegram bot configured)"
            )

    def on_confirm_poll(self) -> None:
        """Call repeatedly inside the window (Telegram long-poll surface)."""
        if self._confirmation is None or self.telegram is None:
            return
        self.telegram.poll(self._confirmation, self.clock())

    def on_cutoff(self) -> None:
        if self._confirmation is None:
            return
        now = self.clock()
        if self.telegram is not None:
            self.telegram.poll(self._confirmation, now)
        self._confirmation.expire(now)
        finalized = self._confirmation.finalized()
        approved = finalized.human_approved
        quotes: dict[str, float] = {}
        for cand in approved:
            try:
                quotes[cand.symbol] = self.feed.get_quote(cand.symbol).last
            except DataFeedError:
                pass  # execution treats a missing quote conservatively
        report = self.execution.execute(approved, quotes, now)
        self._entries_placed_today = sum(
            1 for o in report.placed if o.side is Side.BUY
        )
        logger.info(
            "cutoff execution done",
            extra={"approved": len(approved), "placed": len(report.placed),
                   "skipped": len(report.skipped),
                   "rejected": len(report.rejected),
                   "expired": len(finalized.expired)},
        )

    def on_close(self, bars=None) -> None:
        """16:00 ET: feed today's daily bar so MOC/LOC + resting orders fill."""
        if bars is None:
            bars = self._fetch_close_bars()
        if bars:
            self.broker.step(bars)
        self.execution.sync_fills()
        self.broker.end_of_day()
        status = self.account_monitor.poll()
        self.ledger.record_snapshot(status.snapshot)
        self._update_memory_outcomes()

    # ------------------------------------------------------------- wiring

    def callbacks(self) -> dict[Event, Callable[[], None]]:
        return {
            Event.MORNING_REPORT: self.on_morning_report,
            Event.MONITOR_START: self.on_monitor,
            Event.DECIDE_START: self.on_decide,
            Event.PUSH_CANDIDATES: self.on_push,
            Event.CONFIRM_CUTOFF: self.on_cutoff,
            Event.MARKET_CLOSE: self.on_close,
        }

    # ---------------------------------------------------------- internals

    def _build_signals(self) -> tuple[list[Signal], dict[str, SymbolView]]:
        debates: list[Signal] = []
        views: dict[str, SymbolView] = {}
        watch = self._portfolio.watch if self._portfolio else {}
        news_items = self._rebuild_news_items()
        for symbol in self.symbols:
            state = watch.get(symbol)
            if state is None:
                continue
            try:
                bars = self.feed.get_bars(symbol, "1d", limit=120)
            except DataFeedError:
                continue
            signals = []
            tech = self.tech.analyze(symbol, bars)
            if tech is not None:
                signals.append(tech)
            fund = self.fund.analyze(symbol)
            if fund is not None:
                signals.append(fund)
            sym_news = [n for n in news_items if n.symbol == symbol]
            senti = self.senti.analyze(symbol, sym_news)
            if senti is not None:
                signals.append(senti)
            if self.llm_analyst is not None and tech is not None:
                llm_sig = self.llm_analyst.analyze(
                    symbol,
                    features=tech.features_json,
                    headlines=[n.headline for n in sym_news],
                    regime=self._market.risk_on_off if self._market else "neutral",
                )
                if llm_sig is not None:  # fail-safe: None on any LLM trouble
                    signals.append(llm_sig)
            if not signals:
                continue
            for sig in signals:
                self.ledger.record_signal(sig, self.mode)
            verdict = self.debate.debate(symbol, signals)
            self.ledger.record_signal(verdict, self.mode)
            debates.append(verdict)
            item = watchlist_mod.get(symbol)
            views[symbol] = SymbolView(
                symbol=symbol,
                last=state.last,
                atr_pct=state.atr_pct,
                pool=item.role if item is not None else Role.ROTATION,
            )
        return debates, views

    def _rebuild_news_items(self) -> list[NewsItem]:
        if self._news is None:
            return []
        items: list[NewsItem] = []
        for raw in self._news.items:
            try:
                ts = datetime.fromisoformat(raw["ts"])
                items.append(NewsItem(
                    symbol=raw.get("symbol"),
                    ts=ts,
                    headline=raw.get("headline", ""),
                    source=raw.get("source", ""),
                    url=raw.get("url", ""),
                    sentiment=raw.get("sentiment"),
                ))
            except (KeyError, ValueError, TypeError):
                continue
        return items

    def _revalidate_edit(self, cand: CandidateOrder) -> tuple[bool, str]:
        """Loop.md §5.6: every human edit re-passes the RiskEngine."""
        status = self.account_monitor.poll()
        positions = self._portfolio.positions if self._portfolio else []
        liquidity = self.portfolio_monitor.liquidity_for(cand.symbol)
        decision = self.risk_engine.evaluate(
            cand.model_copy(update={"status": cand.status}),
            status.snapshot, positions, liquidity,
            entries_today=self._entries_placed_today,
        )
        if not decision.approved:
            return False, decision.candidate.risk_note
        if decision.final_qty < cand.qty:
            return False, (
                f"edited qty {cand.qty:g} exceeds risk limits "
                f"(max allowed {decision.final_qty:g})"
            )
        return True, ""

    def _fetch_close_bars(self):
        symbols = {p.symbol for p in self.broker.get_positions()}
        symbols |= {o.symbol for o in self.broker.get_orders(active_only=True)}
        bars = {}
        for symbol in symbols:
            try:
                candles = self.feed.get_bars(symbol, "1d", limit=1)
            except (DataFeedError, ValueError):
                continue
            if candles:
                bars[symbol] = candles[-1]
        return bars

    def _publish_brief(self) -> None:
        """Build the Investment Research brief and expose it via the runtime
        (Loop.md Phase 0.5: research first — the tab reads /research/brief)."""
        if self.runtime is None:
            return
        from swing_trader.brief import build_research_brief

        try:
            brief = build_research_brief(
                self.ledger, self.mode,
                market=self._market, portfolio=self._portfolio,
                news=self._news,
                llm_enabled=self.llm_analyst is not None,
                now=self.clock(),
            )
            self.runtime.latest_brief = brief.model_dump(mode="json")
        except Exception:  # brief must never break the trading loop
            logger.exception("research brief build failed")

    def _ingest_news(self) -> None:
        """Persist today's scored news into the knowledge store (§5.10).
        Vector-down and any pipeline error are fail-closed: logged, never
        allowed to break the loop; the ledger/facts are unaffected."""
        if self.knowledge is None or self._news is None:
            return
        from swing_trader.knowledge_pipeline import ingest_news_snapshot

        try:
            trading_date = (
                self.clock().astimezone(ZoneInfo("America/New_York")).date()
            )
            report = ingest_news_snapshot(
                self.knowledge, self.knowledge_index, self._news, trading_date
            )
            logger.info(
                "news ingested into knowledge store",
                extra={"n_docs": report.n_docs_written,
                       "n_dupes": report.n_duplicates,
                       "vector_ok": report.vector_ok},
            )
        except Exception:
            logger.exception("knowledge ingestion failed (fail-closed)")

    def _update_memory_outcomes(self) -> None:
        memory = self.decision.memory
        if memory is None:
            return
        for trade in self.ledger.get_trades(self.mode, closed_only=True):
            if trade.id in self._memory_seen_trades or trade.pnl is None:
                continue
            self._memory_seen_trades.add(trade.id)
            note = f"r={trade.r_multiple:.2f}" if trade.r_multiple is not None else ""
            memory.record_outcome(trade.symbol, trade.pnl, note=note)
