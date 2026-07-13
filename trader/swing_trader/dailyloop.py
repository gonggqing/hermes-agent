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
import re
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
from swing_trader.health import HealthLevel, HealthStatus, assess_health
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
from swing_trader.reconcile import reconcile_broker_ledger
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
        respond_text: Optional[Callable[[str], Optional[str]]] = None,
    ) -> None:
        """``interactive=False`` = OUTBOUND ONLY (cards/reports are sent, but
        poll() is a no-op). Required when the Hermes gateway long-polls
        getUpdates with the SAME bot token — two consumers would 409 each
        other off Telegram. Use a DEDICATED finance bot token for interactive
        approvals (Loop.md Phase 0.5).

        ``allowed_users``: Telegram usernames/ids permitted to act (§5.6:
        authenticated actor). Interactive mode with an EMPTY allowlist
        refuses every action — auth must be explicit, never open.

        ``respond_text``: optional handler ``text -> reply|None`` making the
        finance bot answer ONLY when directly addressed — a DM, or an @mention
        of this bot in a group (human directive: the finance bot stays quiet in
        the group except for confirmations, and replies only when @-mentioned or
        DMed). Replies are gated by the same allowlist as approvals. When None,
        the bot never replies to text (confirmations/buttons only)."""
        self._transport = transport
        self._chat_id = chat_id
        self.interactive = interactive
        self._allowed_users = {u.strip().lower().lstrip("@")
                               for u in (allowed_users or set()) if u.strip()}
        self._respond_text = respond_text
        self._by_short_id: dict[str, str] = {}
        self._offset: Optional[int] = None
        self._bot_username: Optional[str] = None
        self._bot_identified = False

    def set_text_responder(
        self, fn: Optional[Callable[[str], Optional[str]]]
    ) -> None:
        """Wire the DM/@mention text responder after construction."""
        self._respond_text = fn

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
            if callback:
                self._handle_callback(callback, service, now_utc)
                continue
            message = update.get("message")
            if message:
                self._handle_message(message)

    def _handle_callback(
        self, callback: dict, service: ConfirmationService, now_utc: datetime
    ) -> None:
        cb_id = str(callback.get("id", ""))
        try:
            data = json.loads(callback.get("data", "") or "{}")
        except json.JSONDecodeError:
            self._transport.answer_callback(cb_id, "unrecognized action")
            return
        full_id = self._by_short_id.get(str(data.get("id", "")))
        action = {"ok": "approve", "no": "reject"}.get(data.get("a"))
        if full_id is None or action is None:
            if data.get("a") == "edit":
                self._transport.answer_callback(
                    cb_id, "edit via the Finance portal (Desktop/Web)"
                )
            else:
                self._transport.answer_callback(cb_id, "unknown candidate")
            return
        sender = callback.get("from", {}) or {}
        if not self._is_authorized(sender):
            logger.warning(
                "unauthorized telegram action refused",
                extra={"sender_id": str(sender.get("id", "?"))},
            )
            self._transport.answer_callback(
                cb_id, "not authorized for finance approvals"
            )
            return
        actor = f"telegram:{sender.get('username') or sender.get('id') or 'user'}"
        result = service.act(
            full_id, action, actor=actor, surface=Surface.TELEGRAM,
            idempotency_key=f"tg:{cb_id}", now_utc=now_utc,
        )
        self._transport.answer_callback(cb_id, result.message[:180])

    def _identity(self) -> Optional[str]:
        """This bot's @username (cached), for @mention detection; None if the
        transport has no getMe (older mocks) or the call fails."""
        if not self._bot_identified:
            self._bot_identified = True
            getter = getattr(self._transport, "get_me", None)
            if callable(getter):
                try:
                    me = getter() or {}
                    self._bot_username = str(me.get("username") or "").lower() or None
                except Exception:  # identity is best-effort
                    self._bot_username = None
        return self._bot_username

    def _handle_message(self, message: dict) -> None:
        """Reply ONLY when directly addressed: a DM, or an @mention of this bot
        in a group — and only for allowlisted users. Otherwise stay quiet (the
        finance bot is the confirmation channel, not a group chatterbox)."""
        if self._respond_text is None:
            return
        text = str(message.get("text") or "").strip()
        if not text:
            return
        chat = message.get("chat", {}) or {}
        is_dm = chat.get("type") == "private"
        username = self._identity()
        mentioned = bool(username) and f"@{username}" in text.lower()
        if not (is_dm or mentioned):
            return  # group message not addressed to the finance bot
        if not self._is_authorized(message.get("from", {}) or {}):
            return  # only allowlisted users get a finance-bot reply
        if mentioned and username:
            text = re.sub(rf"@{re.escape(username)}", "", text,
                          flags=re.IGNORECASE).strip()
        try:
            reply = self._respond_text(text)
        except Exception:
            logger.exception("finance bot text responder failed")
            reply = None
        if reply:
            self._transport.send_message(str(chat.get("id") or self._chat_id), reply)


class DailyLoop:
    """One instance per process; drives one mode (paper in Phase 0)."""

    def __init__(
        self,
        feed: DataFeed,
        broker: BrokerInterface,
        ledger: Ledger,
        mode: Mode = Mode.PAPER,
        live_orders_allowed: bool = False,
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
        earnings_provider=None,  # Optional[EarningsProvider] (Phase 0.75)
        kill_switch=None,  # Optional[KillSwitch] — manual operator HALT (§3)
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
        # live_orders_allowed defaults False (fail-closed): even in Mode.LIVE the
        # ExecutionEngine refuses to place unless the caller explicitly threads
        # the triple gate through (Loop.md §3). __main__ passes
        # settings.live_orders_allowed here.
        self.execution = ExecutionEngine(
            broker, ledger, mode=mode, live_orders_allowed=live_orders_allowed
        )
        # Stamp snapshots with the loop's clock so Phase 0.8 health-freshness is
        # consistent with ``self.clock()`` under the simulator/backtester too.
        self.market_monitor = MarketMonitor(feed, breadth_symbols=self.symbols,
                                            clock=self.clock)
        self.portfolio_monitor = PortfolioMonitor(feed, broker, symbols=self.symbols,
                                                  clock=self.clock)
        self.news_monitor = NewsMonitor(feed, clock=self.clock)
        self.account_monitor = AccountRiskMonitor(broker, self.risk_params,
                                                  clock=self.clock)
        self.tech = TechnicalAgent()
        self.fundamentals_provider = fundamentals
        self.fund = FundamentalAgent(fundamentals or StaticFundamentals({}))
        self.senti = SentimentAgent()
        self.debate = DebateAgent()
        self.llm_analyst = llm_analyst
        self.knowledge = knowledge
        self.knowledge_index = knowledge_index
        self.earnings_provider = earnings_provider

        self._market = None
        self._earnings: list = []
        self._portfolio = None
        self._news = None
        self._risk_approved: list[CandidateOrder] = []
        self._confirmation: Optional[ConfirmationService] = None
        self._entries_placed_today = 0
        self._memory_seen_trades: set[str] = set()
        self._health: Optional[HealthStatus] = None  # Phase 0.8 (dead-man's switch)
        self.kill_switch = kill_switch  # Phase 0.95 manual HALT (may be None)

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
        self._compute_earnings()
        self._ingest_research()
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

        # Phase 0.8: assess system health BEFORE risk-evaluating candidates.
        # When unhealthy (stale data / ledger-broker drift), the dead-man's
        # switch vetoes every NEW entry in the RiskEngine below; exits still
        # flow. Alert the reporter bot so the operator sees why (Loop.md §5.10).
        health = self._assess_health(account)
        if not health.entries_allowed:
            self._alert_unhealthy(health)

        candidates = self.decision.propose(
            debates, views, account, positions,
            risk_on_off=self._market.risk_on_off if self._market else "neutral",
            open_order_symbols=open_syms,
            earnings_symbols={e.symbol for e in self._earnings
                              if getattr(e, "imminent", False)},
        )

        self._risk_approved = []
        entries_seen = 0
        for cand in candidates:
            self.ledger.record_candidate(cand, self.mode)
            liquidity = self.portfolio_monitor.liquidity_for(cand.symbol)
            decision = self.risk_engine.evaluate(
                cand, account, positions, liquidity, entries_today=entries_seen,
                system_healthy=health.entries_allowed,
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

    def run_session_now(self, now: datetime | None = None, *,
                        window_minutes: int = 60) -> dict:
        """Manually run a full trading session on demand (Loop.md §4b
        missed-session catch-up): fresh monitors → decide (RiskEngine + the P0.8
        dead-man's switch) → push the risk-approved candidates into a
        confirmation window anchored to NOW, so they reach the approval queue
        even off-schedule (the fixed daily window would otherwise refuse the
        publish). Does NOT auto-execute — the human still approves each candidate
        and :meth:`finalize_session_now` places only what was approved (§3)."""
        now = now or self.clock()
        self.on_monitor()  # fresh snapshots (also refreshes health/freshness)
        self.on_decide()  # RiskEngine + dead-man's switch produce risk_approved

        # Anchor the confirmation window to NOW; clamp so it never wraps past
        # the ET midnight (the window is compared as a time-of-day).
        et_now = now.astimezone(ZoneInfo("America/New_York"))
        push_t = et_now.time()
        end_of_day = et_now.replace(hour=23, minute=59, second=0, microsecond=0)
        cutoff_dt = min(et_now + timedelta(minutes=max(5, window_minutes)), end_of_day)
        cutoff_t = cutoff_dt.time()
        if cutoff_t <= push_t:  # extreme late-night edge → minimal same-day window
            cutoff_t = et_now.replace(hour=23, minute=59, second=59).time()

        self._confirmation = ConfirmationService(
            self.ledger, mode=self.mode, push_time_et=push_t, cutoff_et=cutoff_t,
            market_tz="America/New_York", revalidate=self._revalidate_edit,
        )
        if self.runtime is not None:
            self.runtime.confirmation = self._confirmation
        self.on_push()  # publishes into the now-anchored window

        halted = self._health is not None and not self._health.entries_allowed
        summary = {
            "ran_at": now.isoformat(),
            "risk_approved": len(self._risk_approved),
            "pushed": len(self._risk_approved),
            "cutoff_et": cutoff_t.strftime("%H:%M"),
            "entries_halted": halted,  # dead-man's switch state
            "health_level": self._health.level.value if self._health else None,
        }
        logger.info("manual session run", extra=summary)
        return summary

    def finalize_session_now(self, now: datetime | None = None) -> dict:
        """Manually finalize the current confirmation window (the off-schedule
        equivalent of the 12:30 cutoff): place the human-APPROVED candidates and
        expire the rest. Execution is still gated on human approval per candidate
        (§3) — this only acts on what the human already confirmed."""
        stamp = now or self.clock()
        if self._confirmation is None:
            return {"ran_at": stamp.isoformat(), "approved": 0, "expired": 0,
                    "note": "no active session to finalize"}
        placed_before = len(self.broker.get_orders(active_only=True))
        self.on_cutoff()  # poll + expire + execute approved
        placed_after = len(self.broker.get_orders(active_only=True))
        fin = self._confirmation.finalized()
        summary = {
            "ran_at": stamp.isoformat(),
            "approved": len(fin.human_approved),
            "expired": len(fin.expired),
            "orders_now_active": placed_after,
            "orders_added": max(0, placed_after - placed_before),
        }
        logger.info("manual session finalize", extra=summary)
        return summary

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
                from swing_trader.rag import research_snippets, retrieve_research

                query = f"{symbol} " + " ".join(n.headline for n in sym_news[:2])
                hits = retrieve_research(
                    self.knowledge, self.knowledge_index, query, k=4
                )
                llm_sig = self.llm_analyst.analyze(
                    symbol,
                    features=tech.features_json,
                    headlines=[n.headline for n in sym_news],
                    regime=self._market.risk_on_off if self._market else "neutral",
                    research=research_snippets(hits),
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

    def _assess_health(self, account) -> HealthStatus:
        """Phase 0.8 (Loop.md §5.10): assess whether the loop can be TRUSTED to
        open NEW entries right now — from data freshness, ledger↔broker
        reconciliation and the drawdown breaker. Stores the result on the
        runtime (read-only Finance tab / reporter) and returns it. Pure w.r.t.
        the injected snapshots; never raises (reconciliation fails closed)."""
        recon = reconcile_broker_ledger(self.broker, self.ledger, self.mode)
        ks_state = self.kill_switch.state() if self.kill_switch is not None else None
        health = assess_health(
            market=self._market,
            portfolio=self._portfolio,
            news=self._news,
            breaker_state=getattr(account, "breaker_state", None),
            reconciliation=recon,
            kill_switch_engaged=ks_state.engaged if ks_state else False,
            kill_switch_reason=ks_state.reason if ks_state else "",
            now=self.clock(),
        )
        self._health = health
        if self.runtime is not None:
            self.runtime.health = health
        return health

    def _alert_unhealthy(self, health: HealthStatus) -> None:
        """Push a plain-language health alert to the reporter bot (outbound
        only) when new entries are halted, so the operator sees the reason.
        Never raises — an alert must not break the decide cycle."""
        icon = "🔴" if health.level is HealthLevel.UNHEALTHY else "🟠"
        reasons = "; ".join(health.warnings) or "system health degraded"
        text = (
            f"{icon} Trading halted — new entries paused (dead-man's switch).\n"
            f"Reason: {reasons}\n"
            f"Exits and protective stops are unaffected. "
            f"(Loop.md §5.10, {health.level.value})"
        )
        try:
            self.notify(text)
        except Exception:  # noqa: BLE001 — alerting must never break the loop
            logger.warning("health alert failed", extra={"level": health.level.value})

    def _revalidate_edit(self, cand: CandidateOrder) -> tuple[bool, str]:
        """Loop.md §5.6: every human edit re-passes the RiskEngine."""
        status = self.account_monitor.poll()
        positions = self._portfolio.positions if self._portfolio else []
        liquidity = self.portfolio_monitor.liquidity_for(cand.symbol)
        decision = self.risk_engine.evaluate(
            cand.model_copy(update={"status": cand.status}),
            status.snapshot, positions, liquidity,
            entries_today=self._entries_placed_today,
            # Dead-man's switch also gates human edits: if the data the loop
            # depends on went stale/drifted since decide, don't let an edit
            # slip a fresh entry through (Loop.md §5.10). Exits are unaffected.
            system_healthy=self._health.entries_allowed if self._health else True,
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
                earnings=(self._earnings
                          if self.earnings_provider is not None else None),
            )
            self.runtime.latest_brief = brief.model_dump(mode="json")
        except Exception:  # brief must never break the trading loop
            logger.exception("research brief build failed")

    def _compute_earnings(self) -> None:
        """Refresh the upcoming-earnings list for the watchlist (fail-closed).
        Cached in the provider, so repeated calls in a day are cheap."""
        if self.earnings_provider is None:
            return
        from swing_trader.earnings import upcoming_earnings

        try:
            self._earnings = upcoming_earnings(
                self.earnings_provider, self.symbols,
                now=self.clock(), within_days=14,
            )
        except Exception:  # earnings must never break the loop
            logger.exception("earnings calendar refresh failed")
            self._earnings = []

    def _ingest_research(self) -> None:
        """Archive per-symbol fundamentals + earnings docs into the knowledge
        store so RAG has citable substance beyond news (Loop.md §5.10, Phase
        0.75). Fail-closed: dedupe means unchanged docs are not re-indexed."""
        if self.knowledge is None:
            return
        from swing_trader.research_ingest import (
            build_earnings_doc,
            build_fundamentals_doc,
            ingest_research_documents,
        )

        try:
            trading_date = self.clock().astimezone(ZoneInfo("America/New_York")).date()
            docs = []
            if self.fundamentals_provider is not None:
                for symbol in self.symbols:
                    try:
                        metrics = self.fundamentals_provider.get_metrics(symbol)
                    except Exception:  # one bad symbol never blocks the rest
                        metrics = None
                    if metrics:
                        doc = build_fundamentals_doc(symbol, metrics, trading_date,
                                                     self.clock())
                        if doc is not None:
                            docs.append(doc)
            for e in self._earnings:
                docs.append(build_earnings_doc(e.symbol, e.date, e.days_until,
                                               trading_date, self.clock()))
            if docs:
                ingest_research_documents(
                    self.knowledge, self.knowledge_index, docs, trading_date
                )
        except Exception:
            logger.exception("research ingestion failed (fail-closed)")

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
