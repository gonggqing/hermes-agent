"""Daily-loop orchestrator (Loop.md §4 state machine, backlog 17).

Wires every module into the scheduled day:

  09:00 MORNING_REPORT  sync fills, snapshot, memory update, morning summary
  09:30 MONITOR_START   market / portfolio / news monitors poll
  10:00 DECIDE_START    sub-agents → debate → decision core → RiskEngine
  10:30 PUSH_CANDIDATES publish to the ConfirmationService (portal) + Telegram
  11:30 CONFIRM_CUTOFF  expire stragglers; execute human-approved candidates
  16:00 MARKET_CLOSE    feed daily bars to the PaperBroker; sync fills; snapshot

Authority chain (Loop.md §3): decision core PROPOSES → RiskEngine (pure code)
approves/shrinks/vetoes → ConfirmationService collects HUMAN approval from
Desktop/Web/Telegram → ExecutionEngine re-validates and places. This class
only moves data between those parties; it holds no approval power itself.
"""

from __future__ import annotations

import json
import re
import threading
import uuid
from dataclasses import replace
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo
from typing import Any, Callable, Optional, Sequence

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
from swing_trader.execution import ExecutionEngine, ExecutionReport
from swing_trader.interfaces import BrokerInterface, DataFeed, NewsItem
from swing_trader.ledger import AuditEvent, Ledger
from swing_trader.log import get_logger
from swing_trader.monitors import (
    DEFAULT_INDEX_SYMBOLS,
    AccountRiskMonitor,
    MarketMonitor,
    NewsMonitor,
    PortfolioMonitor,
)
from swing_trader.news_quality import curate_news
from swing_trader.reconcile import reconcile_broker_ledger
from swing_trader.reporter import morning_summary, push_window_preamble
from swing_trader.risk import RiskEngine, RiskParams
from swing_trader.scheduler import US_SCHEDULE, Event, SessionSchedule
from swing_trader.schemas import (
    CandidateOrder,
    CandidateStatus,
    Mode,
    Role,
    Side,
    Signal,
)
from swing_trader.telegram_gateway import (
    CALLBACK_ID_LEN,
    DRAFT_CALLBACK_TYPE,
    TelegramTransport,
    build_draft_keyboard,
    build_keyboard,
    render_card,
    render_candidate_action_reply,
    render_draft_card,
)
from swing_trader import watchlist as watchlist_mod

logger = get_logger(__name__)

_ET = ZoneInfo("America/New_York")
_PUSH_TIME = time(10, 30)
_PRE_CUTOFF_REMINDER = time(11, 0)
_CUTOFF_TIME = time(11, 30)
_MARKET_CLOSE_TIME = time(16, 0)
_EXECUTION_RETRY_INTERVAL = timedelta(minutes=5)

#: Human-facing market labels for the research brief (per DailyLoop.market_id).
_MARKET_LABELS = {
    "US": "United States",
    "HK": "Hong Kong",
    "CN": "China",
    "KR": "Korea",
}
_REVIEW_RETRY_DELAYS = (
    timedelta(minutes=1),
    timedelta(minutes=5),
)
_REVIEW_MAX_ATTEMPTS = 3

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
        candidate_account_label: str = "IBHK Paper（模拟盘）",
        candidate_account_labels: Optional[dict[str, str]] = None,
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
        self._allowed_users = {
            u.strip().lower().lstrip("@") for u in (allowed_users or set()) if u.strip()
        }
        self._respond_text = respond_text
        self._candidate_account_label = candidate_account_label
        self._candidate_account_labels = {
            key.lower(): value for key, value in (candidate_account_labels or {}).items()
        }
        self._candidate_reasoner: Optional[Callable[[CandidateOrder], Optional[str]]] = None
        self._by_short_id: dict[str, str] = {}
        # short-id -> the ConfirmationService that OWNS that candidate. Lets a
        # single Telegram poller serve more than one concurrent market (US + HK):
        # a candidate's callback routes to its own service even when the poll is
        # driven by a different loop. Falls back to the polled service.
        self._service_by_short_id: dict[str, ConfirmationService] = {}
        self._offset: Optional[int] = None
        self._bot_username: Optional[str] = None
        self._bot_identified = False
        # Portfolio-draft confirmation (Loop.md P0.9 boundary #4): the API
        # thread pushes cards (push_draft_card) while the poll thread reads the
        # map in _handle_draft_callback — guard the shared dict with a lock.
        self._draft_by_short_id: dict[str, str] = {}
        self._draft_lock = threading.Lock()
        self._drafts: Any = None  # PortfolioDraftService, wired post-construction
        # Recording (记账) happens in the user's DM with the finance bot (user
        # decision 2026-07-14). We learn the private chat id the first time an
        # allowlisted user DMs, and route draft cards there (keeping the group
        # for candidate-order confirmations only). ``_record_trade`` parses a DM
        # trade message into a draft; None disables recording.
        self._dm_chat_id: Optional[str] = None
        self._record_trade: Optional[Callable[[str], Any]] = None
        # DM "/" command menu (/持仓 /研究 …) and update-holdings handler
        # (改名/现价 immediate, 成本/数量/账户 via correction card).
        self._command_handler: Optional[Callable[[str], Optional[str]]] = None
        self._update_handler: Optional[Callable[[str], Optional[str]]] = None

    def set_command_handler(
        self, fn: Optional[Callable[[str], Optional[str]]]
    ) -> None:
        """Wire the slash-command handler (``text -> reply`` or None)."""
        self._command_handler = fn

    def set_update_handler(
        self, fn: Optional[Callable[[str], Optional[str]]]
    ) -> None:
        """Wire the DM update-holdings handler (``text -> reply`` or None if the
        text isn't an update-holdings edit)."""
        self._update_handler = fn

    def register_commands(self, menu: list) -> None:
        """Register the "/" command menu with Telegram (setMyCommands). menu is
        a list of (command, description); best-effort — logged, never raised."""
        setter = getattr(self._transport, "set_my_commands", None)
        if not callable(setter):
            return
        try:
            setter([{"command": c, "description": d} for c, d in menu])
            logger.info("finance bot command menu registered", extra={"n_commands": len(menu)})
        except Exception:
            logger.warning("failed to register finance bot command menu")

    def set_text_responder(self, fn: Optional[Callable[[str], Optional[str]]]) -> None:
        """Wire the DM/@mention text responder after construction."""
        self._respond_text = fn

    def set_candidate_reasoner(
        self, fn: Optional[Callable[[CandidateOrder], Optional[str]]]
    ) -> None:
        """Optional fast-LLM evidence summarizer for Chinese outcome replies."""
        self._candidate_reasoner = fn

    def set_draft_service(self, service: Any) -> None:
        """Wire the PortfolioDraftService so tapped draft cards can confirm /
        reject real-holdings drafts in Telegram (Loop.md P0.9 boundary #4)."""
        self._drafts = service

    def push_draft_card(
        self, draft: Any, account_label: str = "", chat_id: Optional[str] = None
    ) -> None:
        """Push a portfolio-draft confirmation card so an allowlisted human can
        confirm the trade IN Telegram — no portal round-trip. The LLM/parser only
        DRAFTED it; the confirmer is still an authenticated human (boundary #4),
        and confirm goes through the SAME PortfolioDraftService.confirm_draft the
        portal uses (same audit trail, idempotency, incomplete-draft refusal).

        Recording is private: the card goes to ``chat_id`` if given, else the
        learned DM chat, else the group as a last resort. (Candidate-order cards
        still go to the group via push_cards — that flow is unchanged.)

        Best-effort: only the INTERACTIVE (dedicated) bot long-polls callbacks,
        so an outbound-only adapter skips the push; any transport error is
        logged, never raised — draft creation must not depend on Telegram.
        """
        if not self.interactive:
            return
        target = chat_id or self._dm_chat_id or self._chat_id
        try:
            with self._draft_lock:
                self._draft_by_short_id[draft.id[:CALLBACK_ID_LEN]] = draft.id
            self._transport.send_message(
                target,
                render_draft_card(draft, account_label),
                reply_markup=build_draft_keyboard(draft),
            )
            logger.info(
                "portfolio draft card pushed to telegram",
                extra={"draft_id": draft.id, "symbol": draft.symbol},
            )
        except Exception:  # best-effort: never break the draft-create request
            logger.warning(
                "failed to push telegram draft card", extra={"draft_id": getattr(draft, "id", "?")}
            )

    def set_trade_recorder(self, fn: Optional[Callable[[str], Any]]) -> None:
        """Wire the DM trade-record parser: ``text -> (draft, account_label, ack)``
        or None if the text isn't a trade record. Recording happens only in a DM
        with the finance bot (see _handle_message)."""
        self._record_trade = fn

    def _is_authorized(self, sender: dict) -> bool:
        username = str(sender.get("username", "")).lower()
        user_id = str(sender.get("id", ""))
        return bool(self._allowed_users) and (
            username in self._allowed_users or user_id in self._allowed_users
        )

    def push_cards(
        self,
        candidates: list[CandidateOrder],
        preamble: str = "",
        service: Optional[ConfirmationService] = None,
    ) -> None:
        if preamble:
            self._transport.send_message(self._chat_id, preamble)
        for cand in candidates:
            short = cand.id[:CALLBACK_ID_LEN]
            self._by_short_id[short] = cand.id
            if service is not None:
                self._service_by_short_id[short] = service
            self._transport.send_message(
                self._chat_id, render_card(cand), reply_markup=build_keyboard(cand)
            )

    def restore_cards(
        self,
        candidates: list[CandidateOrder],
        service: Optional[ConfirmationService] = None,
    ) -> None:
        """Restore callback-id routing without sending duplicate cards."""
        for cand in candidates:
            short = cand.id[:CALLBACK_ID_LEN]
            self._by_short_id[short] = cand.id
            if service is not None:
                self._service_by_short_id[short] = service

    def push_recovery_notice(self, text: str) -> None:
        """Send one durable confirmation/recovery status line to Telegram."""
        self._send_status(self._chat_id, text)

    def push_execution_outcome(self, report) -> None:
        """Persist the post-cutoff broker outcome in Chinese.

        Approval and submission are different states. This message is emitted
        only after ExecutionEngine returns and still never calls a resting order
        a fill.
        """
        lines: list[str] = []
        if report.placed:
            label = self._account_label_for(report.placed[0])
            lines.append(f"📨 已向 {label} 提交挂单")
            for order in report.placed:
                px = order.limit if order.limit is not None else order.stop
                side = "买入" if order.side is Side.BUY else "卖出"
                lines.append(
                    f"{side} {order.symbol} {order.qty:g} 股 · "
                    f"{order.order_type.value} · 价格 {px:g}"
                    if px is not None
                    else f"{side} {order.symbol} {order.qty:g} 股 · {order.order_type.value}"
                )
            lines.append("状态：订单已提交/挂起，尚不代表已经成交。")
        for candidate, order in report.recovered:
            lines.append(
                f"♻️ {candidate.symbol} 已恢复既有挂单 {order.id}，未重复提交。"
            )
        for candidate, reason in report.skipped:
            lines.append(f"⚠️ {candidate.symbol} 未提交：{reason}")
        for order, reason in report.rejected:
            lines.append(f"⛔ {order.symbol} 被券商拒绝：{reason}")
        if lines:
            self._send_status(self._chat_id, "\n".join(lines))

    def _send_status(self, chat_id: str, text: str) -> None:
        """Best-effort status delivery; never roll back a settled action."""
        try:
            self._transport.send_message(chat_id, text)
        except Exception:
            logger.warning("failed to send telegram candidate status")

    def _account_label_for(self, item: Any) -> str:
        market = str(getattr(item, "market", "") or "").lower()
        if not market:
            symbol = str(getattr(item, "symbol", "") or "").upper()
            market = (
                "cn" if symbol.endswith((".SS", ".SZ"))
                else "hk" if symbol.endswith(".HK")
                else "us"
            )
        return self._candidate_account_labels.get(
            market, self._candidate_account_label
        )

    def poll(self, service: Optional[ConfirmationService], now_utc: datetime) -> None:
        # ``service`` may be None before the daily decide phase — draft
        # callbacks don't need it, and candidate callbacks find no registered
        # id and are answered without touching it (see on_confirm_poll).
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
        # Portfolio-draft cards carry t="d" — route to the draft service
        # (real-holdings journal), NOT the candidate ConfirmationService.
        if data.get("t") == DRAFT_CALLBACK_TYPE:
            self._handle_draft_callback(
                cb_id, data, callback.get("from", {}) or {}, now_utc
            )
            return
        short = str(data.get("id", ""))
        full_id = self._by_short_id.get(short)
        # Route to the candidate's OWNING service (US or HK), falling back to the
        # service the current poll was driven with.
        service = self._service_by_short_id.get(short, service)
        action = {"ok": "approve", "no": "reject", "edit": "edit"}.get(data.get("a"))
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
            self._transport.answer_callback(cb_id, "你没有财经确认权限")
            return
        target = str(((callback.get("message") or {}).get("chat") or {}).get("id") or self._chat_id)
        if action == "edit":
            candidate_entry = service.get(full_id) if service is not None else None
            if candidate_entry is None:
                self._transport.answer_callback(cb_id, "候选单未知或已失效")
                return
            candidate = candidate_entry[0]
            self._transport.answer_callback(cb_id, "请到 Finance Portal 修改")
            self._send_status(
                target,
                render_candidate_action_reply(
                    candidate,
                    "edit",
                    account_label=self._account_label_for(candidate),
                ),
            )
            return
        if service is None:
            self._transport.answer_callback(cb_id, "当前没有可处理的候选单")
            return
        actor = f"telegram:{sender.get('username') or sender.get('id') or 'user'}"
        result = service.act(
            full_id,
            action,
            actor=actor,
            surface=Surface.TELEGRAM,
            idempotency_key=f"tg:{cb_id}",
            now_utc=now_utc,
        )
        if not result.ok or result.candidate is None:
            failure = {
                "window_closed": "确认窗口已关闭",
                "terminal": "该候选单已处理",
                "unknown_candidate": "候选单未知或已失效",
                "version_conflict": "候选单已更新，请刷新后重试",
            }.get(result.code.value, "操作未生效")
            self._transport.answer_callback(cb_id, failure)
            self._send_status(target, f"⚠️ {failure}。没有挂单，也没有成交。")
            return
        self._transport.answer_callback(
            cb_id, "已批准，等待提交" if action == "approve" else "已拒绝"
        )
        reason_zh = None
        if self._candidate_reasoner is not None:
            try:
                reason_zh = self._candidate_reasoner(result.candidate)
            except Exception:
                logger.warning(
                    "candidate reasoner failed",
                    extra={"candidate_id": result.candidate.id},
                )
        self._send_status(
            target,
            render_candidate_action_reply(
                result.candidate,
                action,
                account_label=self._account_label_for(result.candidate),
                reason_zh=reason_zh,
            ),
        )

    def _handle_draft_callback(
        self, cb_id: str, data: dict, sender: dict, now_utc: datetime
    ) -> None:
        """Confirm / reject a REAL-HOLDINGS portfolio draft from Telegram.

        Goes through :meth:`PortfolioDraftService.confirm_draft` /
        ``reject_draft`` — the SAME server-authoritative path the portal uses,
        so boundary #4 holds: an authenticated (allowlisted) human finalizes,
        the LLM never does; an INCOMPLETE draft is refused and the user is sent
        to the portal to fill the gaps (never guessed).
        """
        short = str(data.get("id", ""))
        action = data.get("a")
        with self._draft_lock:
            full_id = self._draft_by_short_id.get(short)
        if full_id is None or action not in ("ok", "no"):
            self._transport.answer_callback(cb_id, "未知或已处理的记录")
            return
        if self._drafts is None:  # draft service not wired (should not happen)
            self._transport.answer_callback(cb_id, "组合记账服务未就绪")
            return
        if not self._is_authorized(sender):
            logger.warning(
                "unauthorized telegram draft action refused",
                extra={"sender_id": str(sender.get("id", "?"))},
            )
            self._transport.answer_callback(cb_id, "无组合记账确认权限")
            return
        actor = f"telegram:{sender.get('username') or sender.get('id') or 'user'}"
        if action == "ok":
            result = self._drafts.confirm_draft(
                full_id, actor=actor, surface=Surface.TELEGRAM.value,
                idempotency_key=f"tg-draft:{cb_id}", now=now_utc,
            )
        else:
            result = self._drafts.reject_draft(
                full_id, actor=actor, surface=Surface.TELEGRAM.value,
                idempotency_key=f"tg-draft:{cb_id}",
            )
        self._transport.answer_callback(cb_id, self._draft_toast(result, action))
        # A settled draft (confirmed OR rejected) stops tracking so a stale tap
        # can't re-act, AND posts a persistent group line — the inline toast
        # vanishes, but a chat entry is the audit everyone in the group sees.
        # Confirm and reject are symmetric (both leave a record). INCOMPLETE
        # stays tracked (transient toast only) so the user can retry after
        # fixing it in the portal.
        if result.ok:
            with self._draft_lock:
                self._draft_by_short_id.pop(short, None)
            by = str(sender.get("username") or sender.get("id") or "")
            self._transport.send_message(
                self._chat_id, self._draft_outcome_line(result.draft, action, by)
            )

    def _draft_outcome_line(self, draft: Any, action: str, by: str) -> str:
        """Persistent group message for a settled draft (audit the group sees).
        Symmetric for confirm/reject, with the standing trade + who acted."""
        sym = (getattr(draft, "symbol", "") if draft else "") or "记录"
        bits = [sym]
        if draft is not None:
            if getattr(draft, "qty", None) is not None:
                bits.append(f"{draft.qty:g}")
            if getattr(draft, "price", None) is not None:
                bits.append(f"@ {draft.price:g}")
        detail = " ".join(bits)
        who = f"（@{by}）" if by else ""
        head = "✅ 已入账" if action == "ok" else "❌ 已拒绝"
        return f"{head}：{detail}{who}"

    def _draft_toast(self, result: Any, action: str) -> str:
        """Short inline-toast text for a draft confirm/reject outcome."""
        from swing_trader.portfolio_draft import DraftResultCode

        if result.ok:
            if result.code == DraftResultCode.REPLAYED:
                return "已处理过（重复点击）"
            return "✅ 已确认入账" if action == "ok" else "已拒绝该记录"
        code = result.code
        if code == DraftResultCode.INCOMPLETE:
            return "缺项未补全，请在门户完成：" + (result.message or "")[:120]
        if code == DraftResultCode.TERMINAL:
            return "该记录已处理，无法重复操作"
        if code == DraftResultCode.NOT_HUMAN:
            return "仅限授权用户确认"
        return (result.message or "操作失败")[:150]

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
        finance bot is the confirmation channel, not a group chatterbox). A DM
        also records trades (see _record_trade); analysis is _respond_text."""
        if (self._respond_text is None and self._record_trade is None
                and self._command_handler is None and self._update_handler is None):
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
        chat_id = str(chat.get("id") or self._chat_id)
        if is_dm:
            # Learn the private chat so ALL draft cards (DM-recorded or API-made)
            # route here instead of the group (recording stays private).
            self._dm_chat_id = chat_id
        if mentioned and username:
            text = re.sub(rf"@{re.escape(username)}", "", text,
                          flags=re.IGNORECASE).strip()
        # Slash command (/持仓 /研究 /记账 /帮助 …) — works in DM or @mention.
        if self._command_handler is not None and text.startswith("/"):
            try:
                reply = self._command_handler(text)
            except Exception:
                logger.exception("finance bot command handler failed")
                reply = None
            if reply:
                self._transport.send_message(chat_id, reply)
                return
        # Update-holdings — DM ONLY ("159518 改名 X", "159518 现价 1.15", …).
        # Name/mark apply immediately; cost/qty/account route to a correction
        # card. The handler returns the reply (or a card was pushed) or None.
        if is_dm and self._update_handler is not None:
            try:
                reply = self._update_handler(text)
            except Exception:
                logger.exception("finance bot update-holdings handler failed")
                reply = None
            if reply:
                self._transport.send_message(chat_id, reply)
                return
        # Recording (记账) — DM ONLY: parse a trade → draft → confirm card here.
        # The card IS the human safety net, so a rough parse is corrected/rejected
        # there, never silently recorded (boundary #4). Falls through to analysis
        # when the text isn't a trade record.
        if is_dm and self._record_trade is not None:
            try:
                rec = self._record_trade(text)
            except Exception:
                logger.exception("finance bot trade recorder failed")
                rec = None
            if rec is not None:
                if isinstance(rec, str):
                    self._transport.send_message(chat_id, rec)
                    return
                draft, account_label, ack = rec
                self.push_draft_card(draft, account_label=account_label, chat_id=chat_id)
                if ack:
                    self._transport.send_message(chat_id, ack)
                return
        try:
            reply = self._respond_text(text) if self._respond_text else None
        except Exception:
            logger.exception("finance bot text responder failed")
            reply = None
        if reply:
            self._transport.send_message(chat_id, reply)


class DailyLoop:
    """One instance per process; drives one mode (paper in Phase 0)."""

    def __init__(
        self,
        feed: DataFeed,
        broker: BrokerInterface,
        ledger: Ledger,
        mode: Mode = Mode.PAPER,
        market_id: str = "",
        schedule: SessionSchedule = US_SCHEDULE,
        live_orders_allowed: bool = False,
        risk_params: RiskParams | None = None,
        symbols: list[str] | None = None,
        index_symbols: Sequence[str] | None = None,  # regime indices (per market)
        anchor_symbol: str = "SPY",  # trend anchor (HK: ^HSI)
        vix_symbol: str = "^VIX",  # volatility index (HK: ^VHSI)
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
        discovery_scanner=None,  # Optional[MarketDiscoveryScanner] — research only
        brief_writer=None,  # Optional[ResearchBriefWriter] — narrative only
        holdings_provider=None,  # Optional read-only real-portfolio projection
        order_reviewer=None,  # Optional[LLMCandidateReviewer] — advisory only
        order_review_required: bool = False,
        close_timeframe: str | None = None,
        watchlist_lookup: Callable[[str], Any] | None = None,
    ) -> None:
        self.feed = feed
        self.broker = broker
        self.ledger = ledger
        self.mode = mode
        # Session identity + timing (default US_SCHEDULE → unchanged single-loop
        # behaviour). ``market_id`` scopes every candidate this loop creates and
        # queries, so a concurrent US and HK loop sharing a paper `mode` never
        # cross-expire or cross-execute (see get_candidates market filter). The
        # window/close wall-times and timezone come from the session schedule so
        # HK runs its own 10:30–11:30 Asia/Hong_Kong window and 16:00 close.
        self._schedule = schedule
        self.market_id = market_id or schedule.market_id
        self._tz = schedule.tz
        self._tz_name = schedule.tz.key
        self._push_time = schedule.event_times.get(Event.PUSH_CANDIDATES, _PUSH_TIME)
        self._cutoff_time = schedule.event_times.get(Event.CONFIRM_CUTOFF, _CUTOFF_TIME)
        self._market_close_time = schedule.event_times.get(
            Event.MARKET_CLOSE, _MARKET_CLOSE_TIME
        )
        self._market_label = _MARKET_LABELS.get(self.market_id, self.market_id)
        # Currency sleeve this market's brief risk/net-value reports — each
        # market its OWN sleeve, never the FX-blended base total (US → the USD
        # sleeve, HK → HKD), so US and HK net values are never conflated.
        self._brief_currency = {
            "US": "USD", "HK": "HKD", "CN": "CNY", "KR": "KRW"
        }.get(self.market_id)
        # Pre-cutoff nudge = 30 min before the cutoff (11:00 for a 11:30 cutoff).
        self._pre_cutoff_reminder = (
            datetime.combine(date(2000, 1, 1), self._cutoff_time)
            - timedelta(minutes=30)
        ).time()
        self.clock = clock
        self.risk_params = risk_params or RiskParams()
        self.symbols = symbols or watchlist_mod.enabled_symbols()
        self.watchlist_lookup = watchlist_lookup or watchlist_mod.get
        self.runtime = runtime
        self.telegram = telegram
        self.notify = notify or (lambda text: logger.info("notify", extra={"text": text[:200]}))

        self.risk_engine = RiskEngine(self.risk_params)
        self.decision = decision_core or RuleBasedDecisionCore(
            risk_params=self.risk_params, market_id=self.market_id
        )
        # live_orders_allowed defaults False (fail-closed): even in Mode.LIVE the
        # ExecutionEngine refuses to place unless the caller explicitly threads
        # the triple gate through (Loop.md §3). __main__ passes
        # settings.live_orders_allowed here.
        self.execution = ExecutionEngine(
            broker, ledger, mode=mode, live_orders_allowed=live_orders_allowed
        )
        # Stamp snapshots with the loop's clock so Phase 0.8 health-freshness is
        # consistent with ``self.clock()`` under the simulator/backtester too.
        self.market_monitor = MarketMonitor(
            feed,
            index_symbols=(
                list(index_symbols) if index_symbols is not None
                else list(DEFAULT_INDEX_SYMBOLS)
            ),
            breadth_symbols=self.symbols,
            clock=self.clock,
            anchor_symbol=anchor_symbol,
            vix_symbol=vix_symbol,
            require_vix_for_risk_on=bool(vix_symbol),
        )
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
        self._latest_signals: list[Signal] = []
        self._risk_approved: list[CandidateOrder] = []
        self._confirmation: Optional[ConfirmationService] = None
        self._entries_placed_today = 0
        self._memory_seen_trades: set[str] = set()
        self._health: Optional[HealthStatus] = None  # Phase 0.8 (dead-man's switch)
        self._risk_status = None
        self.kill_switch = kill_switch  # Phase 0.95 manual HALT (may be None)
        self.discovery_scanner = discovery_scanner
        self.brief_writer = brief_writer
        self.holdings_provider = holdings_provider
        self.order_reviewer = order_reviewer
        self.order_review_required = order_review_required
        # Mainland orders are submitted mid-morning; using the full daily OHLC
        # at close would allow a fill on a low printed before submission.  CN
        # therefore replays only post-submission hourly bars (fail-closed when
        # the feed cannot supply them). Other markets retain the established
        # daily-bar simulator until their own intraday acceptance work lands.
        self.close_timeframe = close_timeframe
        self._discovery = None
        self._confirmation_recovery_lock = threading.Lock()
        self._execution_lock = threading.Lock()
        self._pre_cutoff_alerted: set[date] = set()
        self._last_execution_retry: Optional[datetime] = None
        self._restart_risk_validated: set[str] = set()
        self._review_inflight: set[str] = set()
        self._review_lock = threading.Lock()
        self._review_alerted: set[str] = set()

    def apply_portfolio_controls(self, controls) -> None:
        """Refresh deterministic allocation gates after an operator save."""
        updated = replace(
            self.risk_params,
            per_trade_risk_pct=controls.per_trade_risk_pct,
            max_new_entries_per_day=controls.max_new_positions_per_day,
            max_invested_pct=controls.invested_ceiling_pct,
            max_agent_managed_pct=controls.agent_ceiling_pct,
            max_position_pct=controls.max_position_pct,
        )
        self.risk_params = updated
        self.risk_engine = RiskEngine(updated)
        if isinstance(self.decision, RuleBasedDecisionCore):
            self.decision.risk_params = updated
        self.account_monitor.update_params(updated)

    # ---------------------------------------------------------------- events

    def on_morning_report(self) -> None:
        now = self.clock()
        self.execution.sync_fills()
        self._update_memory_outcomes()
        self.broker.start_of_day()
        status = self.account_monitor.poll()
        self._risk_status = status
        self.ledger.record_snapshot(status.snapshot)
        text = morning_summary(self.broker, self.ledger, self.mode,
                               since_utc=now - timedelta(hours=24))
        if self.runtime is not None:
            self.runtime.latest_reports[f"morning_{self.market_id.lower()}"] = text
            if self.market_id == "US":
                self.runtime.latest_reports["morning"] = text
        self.notify(
            text if self.market_id == "US" else f"[{self._market_label}]\n{text}"
        )
        logger.info("morning report done", extra={"warnings": status.warnings})

    def on_monitor(self) -> None:
        self._market = self.market_monitor.poll()
        if self.discovery_scanner is not None:
            try:
                self._discovery = self.discovery_scanner.scan(self.market_id)
            except Exception:
                logger.exception(
                    "market discovery scan failed", extra={"market": self.market_id}
                )
                self._discovery = None
        discovered = [
            row.symbol for row in (self._discovery.candidates if self._discovery else [])
        ]
        research_symbols = list(dict.fromkeys([*self.symbols, *discovered]))
        self._portfolio = self.portfolio_monitor.poll(research_symbols)
        self._news = self.news_monitor.poll(research_symbols)
        self._risk_status = self.account_monitor.poll()
        if self.runtime is not None:
            market_dump = self._market.model_dump(mode="json")
            self.runtime.market_snapshots[self.market_id.lower()] = market_dump
            if self.market_id == "US":
                self.runtime.market = market_dump
        self._ingest_news()
        # Publish the core market/portfolio/news snapshot BEFORE optional
        # Yahoo earnings/fundamentals enrichment. Those endpoints are much
        # slower and occasionally hang; they must not leave the Finance desk
        # blank after a restart. A second publish below replaces this
        # preliminary snapshot once enrichment finishes.
        self._publish_brief()
        self._compute_earnings()
        self._ingest_research()
        self._publish_brief()

    def run_research_now(self) -> dict:
        """Refresh this market's research desk without entering the decision loop.

        Safe for startup catch-up and the research API: it gathers market,
        portfolio, news and earnings context and publishes a brief, but never
        proposes candidates, opens a confirmation window or places orders.
        """
        try:
            self.on_monitor()
        except Exception:  # a refresh failure must not crash the service
            logger.exception(
                "research refresh failed; publishing degraded brief",
                extra={"market": self.market_id},
            )
            self._publish_brief()
        slot = self.market_id.lower()
        ready = bool(
            self.runtime is not None
            and (
                self.runtime.latest_brief
                if self.market_id == "US"
                else self.runtime.latest_briefs.get(slot)
            )
        )
        return {
            "market": self.market_id,
            "ran_at": self.clock().isoformat(),
            "brief_ready": ready,
        }

    def on_decide(self) -> None:
        if self._portfolio is None:  # monitors have not run (fresh start mid-day)
            self.on_monitor()
        self._entries_placed_today = 0
        debates, views = self._build_signals()
        status = self.account_monitor.poll()
        self._risk_status = status
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
        # Stamp every candidate with this loop's market so it is scoped away
        # from any other concurrent session sharing the same paper `mode`.
        candidates = [
            c.model_copy(update={"market": self.market_id}) for c in candidates
        ]

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
            self.ledger, mode=self.mode, push_time_et=self._push_time,
            cutoff_et=self._cutoff_time, market_tz=self._tz_name,
            revalidate=self._revalidate_edit,
        )
        if self.runtime is not None:
            self.runtime.confirmation = self._confirmation
            self.runtime.confirmation_by_market[self.market_id.lower()] = (
                self._confirmation
            )
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
            self.telegram.push_cards(published, service=self._confirmation)
        else:
            self.notify(
                f"{len(published)} candidate(s) await review in the Finance "
                "portal (no interactive Telegram bot configured)"
            )

    def on_confirm_poll(self) -> None:
        """Poll the Telegram surface — drives BOTH candidate approvals AND
        portfolio-draft confirm/reject.

        We poll whenever the bot exists, NOT only when a candidate
        ConfirmationService is live: the ConfirmationService is created lazily
        in the daily decide phase (~11:00 ET), but a portfolio-draft card can be
        tapped at ANY time and its callback routes to the PortfolioDraftService
        (no ConfirmationService needed). Gating on ``_confirmation`` here meant a
        tapped draft card spun forever until the decide phase ran. Candidate
        callbacks arriving with no ConfirmationService find no registered id and
        are answered "unknown candidate" (never touch the None service)."""
        now = self.clock()
        self._poll_telegram_safely(now)
        self._watch_confirmation_window(now)

    def _poll_telegram_safely(self, now: datetime) -> None:
        """Keep Telegram transport failures outside the trading state machine.

        Long polling is a convenience surface, not an execution dependency.
        A timeout/disconnect must leave the process alive so portal approvals,
        cutoff execution, protection and market-close reconciliation continue.
        The next three-second tick naturally retries without replaying an
        applied callback (Telegram offset + ConfirmationService idempotency).
        """

        if self.telegram is None:
            return
        try:
            self.telegram.poll(self._confirmation, now)
        except Exception as exc:  # transport exceptions contain no token/URL
            logger.warning(
                "telegram poll unavailable; trading loop continues",
                extra={"error_type": type(exc).__name__},
            )

    def on_cutoff(self) -> None:
        now = self.clock()
        if self._confirmation is None:
            self._restore_current_confirmation(now)
        self._poll_telegram_safely(now)
        expired = self._confirmation.expire(now) if self._confirmation else []
        approved_count = self._approved_for_date(now)
        report = self._execute_approved(now, trigger="cutoff")
        self._entries_placed_today = sum(
            1 for o in report.placed if o.side is Side.BUY
        ) + sum(
            1 for _, o in report.recovered if o.side is Side.BUY
        )
        logger.info(
            "cutoff execution done",
            extra={"approved": approved_count,
                   "placed": len(report.placed),
                   "recovered": len(report.recovered),
                   "skipped": len(report.skipped),
                   "rejected": len(report.rejected),
                   "expired": len(expired)},
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
        et_now = now.astimezone(self._tz)
        push_t = et_now.time()
        end_of_day = et_now.replace(hour=23, minute=59, second=0, microsecond=0)
        cutoff_dt = min(et_now + timedelta(minutes=max(5, window_minutes)), end_of_day)
        cutoff_t = cutoff_dt.time()
        if cutoff_t <= push_t:  # extreme late-night edge → minimal same-day window
            cutoff_t = et_now.replace(hour=23, minute=59, second=59).time()

        self._confirmation = ConfirmationService(
            self.ledger, mode=self.mode, push_time_et=push_t, cutoff_et=cutoff_t,
            market_tz=self._tz_name, revalidate=self._revalidate_edit,
        )
        if self.runtime is not None:
            self.runtime.confirmation = self._confirmation
            self.runtime.confirmation_by_market[self.market_id.lower()] = (
                self._confirmation
            )
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
        equivalent of the 11:30 cutoff): place the human-APPROVED candidates and
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
        now = self.clock()
        if bars is None:
            bars = self._fetch_close_bars()
        if bars:
            if isinstance(bars, list):
                for packet in bars:
                    self.broker.step(packet, execution_ts=now)
            else:
                self.broker.step(bars, execution_ts=now)
        self.execution.sync_fills()
        self.broker.end_of_day()
        # §4 safety net: re-arm protection for any position a discretionary exit
        # stripped but that is still open (the exit rested unfilled and expired).
        self.execution.reprotect_positions(now)
        self._expire_unexecuted_through(
            self.clock(), reason="missed execution: market closed without an order"
        )
        status = self.account_monitor.poll()
        self._risk_status = status
        self.ledger.record_snapshot(status.snapshot)
        self.ledger.record_market_marks(self.broker.get_positions(), self.mode, now)
        self._update_memory_outcomes()

    # ------------------------------------------ durable confirmation recovery

    def _et_date(self, candidate: CandidateOrder) -> date:
        """The candidate's trading date in this session's timezone."""
        return candidate.ts.astimezone(self._tz).date()

    def _candidates(self, *statuses: CandidateStatus) -> list[CandidateOrder]:
        wanted = set(statuses)
        return [
            candidate for candidate in self.ledger.get_candidates(
                mode=self.mode, market=self.market_id
            )
            if candidate.status in wanted
        ]

    def _candidates_for_date(
        self, trading_date: date, *statuses: CandidateStatus
    ) -> list[CandidateOrder]:
        return [
            candidate for candidate in self._candidates(*statuses)
            if self._et_date(candidate) == trading_date
        ]

    def _approved_candidates(self, now: datetime) -> list[CandidateOrder]:
        trading_date = now.astimezone(self._tz).date()
        return self._candidates_for_date(
            trading_date, CandidateStatus.APPROVED, CandidateStatus.EDITED
        )

    def _approved_for_date(self, now: datetime) -> int:
        return len(self._approved_candidates(now))

    def _restore_current_confirmation(self, now: datetime) -> list[CandidateOrder]:
        trading_date = now.astimezone(self._tz).date()
        candidates = self._candidates_for_date(
            trading_date,
            CandidateStatus.PUSHED,
            CandidateStatus.APPROVED,
            CandidateStatus.EDITED,
            CandidateStatus.REJECTED,
        )
        service = ConfirmationService(
            self.ledger, mode=self.mode, push_time_et=self._push_time,
            cutoff_et=self._cutoff_time, market_tz=self._tz_name,
            revalidate=self._revalidate_edit,
        )
        restored = service.restore(candidates)
        self._confirmation = service
        if self.runtime is not None:
            self.runtime.confirmation = service
            self.runtime.confirmation_by_market[self.market_id.lower()] = service
        if self.telegram is not None:
            self.telegram.restore_cards(restored, service=service)
        return restored

    def recover_confirmation_state(self, now: datetime | None = None) -> dict:
        """Reconcile durable candidate state after a service restart.

        Before cutoff, the in-memory confirmation session is restored.  After
        cutoff but before the close, human-approved candidates are refreshed,
        re-risked and idempotently submitted.  At/after the close (or on a
        later date), every unsubmitted approval is expired as a missed
        execution; stale prices are never replayed.
        """
        now = now or self.clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("now must be timezone-aware")
        if not self._confirmation_recovery_lock.acquire(blocking=False):
            return {"status": "already_running"}
        try:
            et_now = now.astimezone(self._tz)
            today = et_now.date()
            expired = self._expire_unexecuted_through(
                now,
                reason="missed execution: service recovered after the trading session",
                include_current=et_now.time() >= self._market_close_time,
            )
            risk_approved = self._candidates_for_date(
                today, CandidateStatus.RISK_APPROVED
            )
            restored: list[CandidateOrder] = []
            report = ExecutionReport()

            # Build the confirmation service before the publish window too.
            # A restart at 10:20 ET must leave the scheduled 10:30 callback a
            # live service to publish into; publish() still enforces the window.
            if et_now.time() < self._market_close_time:
                restored = self._restore_current_confirmation(now)

            if et_now.time() < self._cutoff_time:
                self._risk_approved = risk_approved
                if risk_approved and et_now.time() >= self._push_time:
                    self.on_push()  # missed 10:30 event: publish inside window
            else:
                for candidate in risk_approved:
                    self._expire_candidate(
                        candidate, now,
                        "missed confirmation window after service restart",
                        action="expire",
                    )
                if self._confirmation is not None:
                    self._confirmation.expire(now)
                if et_now.time() < self._market_close_time:
                    report = self._execute_approved(
                        now, trigger="restart_recovery", rerisk=True
                    )

            summary = {
                "status": "recovered",
                "restored": len(restored),
                "expired": expired,
                "placed": len(report.placed),
                "recovered_orders": len(report.recovered),
                "still_approved": self._approved_for_date(now),
            }
            logger.info("confirmation recovery complete", extra=summary)
            return summary
        finally:
            self._confirmation_recovery_lock.release()

    def _post_approval_review_complete(self, candidate_id: str) -> bool:
        return any(
            event.action in {
                "post_approval_review_keep",
                "post_approval_review_revision",
            }
            for event in self.ledger.get_audit(
                mode=self.mode, candidate_id=candidate_id
            )
        )

    def _post_approval_review_failures(self, candidate_id: str) -> list[AuditEvent]:
        return sorted(
            (
                event
                for event in self.ledger.get_audit(
                    mode=self.mode, candidate_id=candidate_id
                )
                if event.action == "post_approval_review_failed"
            ),
            key=lambda event: event.ts,
        )

    def _review_retry_due(self, candidate_id: str, now: datetime) -> bool:
        """Bound failed primary-model calls and preserve retry state on restart."""
        failures = self._post_approval_review_failures(candidate_id)
        if len(failures) >= _REVIEW_MAX_ATTEMPTS:
            return False
        if not failures:
            return True
        delay = _REVIEW_RETRY_DELAYS[len(failures) - 1]
        return now >= failures[-1].ts + delay

    def _record_review_failure(
        self, candidate: CandidateOrder, now: datetime, detail: str
    ) -> bool:
        """Persist one bounded attempt; return True only for the first alert."""
        failures = self._post_approval_review_failures(candidate.id)
        attempt = len(failures) + 1
        if attempt > _REVIEW_MAX_ATTEMPTS:
            return False
        self._audit_once(
            candidate,
            now,
            action="post_approval_review_failed",
            prev=candidate.status,
            new=candidate.status,
            detail=f"attempt {attempt}/{_REVIEW_MAX_ATTEMPTS}: {detail}",
            key=f"post-approval-review-failed:{candidate.id}:{attempt}",
        )
        return not failures

    def _launch_post_approval_reviews(
        self, candidates: list[CandidateOrder]
    ) -> None:
        """Start one bounded, non-blocking fresh-market review per approval."""
        now = self.clock()
        for candidate in candidates:
            if self._post_approval_review_complete(candidate.id):
                continue
            if self.order_reviewer is None:
                if candidate.id not in self._review_alerted and self.telegram is not None:
                    self._review_alerted.add(candidate.id)
                    self.telegram.push_recovery_notice(
                        f"⚠️ {candidate.symbol} 已获首次批准，但主模型复核不可用；"
                        "尚未挂单，若截止前未恢复将自动过期。"
                    )
                continue
            if not self._review_retry_due(candidate.id, now):
                continue
            with self._review_lock:
                if candidate.id in self._review_inflight:
                    continue
                self._review_inflight.add(candidate.id)
            threading.Thread(
                target=self._run_post_approval_review,
                args=(candidate,),
                name=f"candidate-review-{candidate.id[:8]}",
                daemon=True,
            ).start()

    def _run_post_approval_review(self, candidate: CandidateOrder) -> None:
        """Review one approval; a changed view always creates a new card/id."""
        try:
            now = self.clock()
            try:
                quote = self.feed.get_quote(candidate.symbol)
                bars = self.feed.get_bars(candidate.symbol, "1d", limit=20)
                news = self.feed.get_news(candidate.symbol, limit=8)
            except (DataFeedError, ValueError) as exc:
                first_failure = self._record_review_failure(
                    candidate, now, f"market data unavailable: {type(exc).__name__}"
                )
                if first_failure and self.telegram is not None:
                    self.telegram.push_recovery_notice(
                        f"⚠️ {candidate.symbol} 复核行情不可用（{type(exc).__name__}）；"
                        "尚未挂单。系统将按 1/5 分钟间隔有限重试，期间不重复提醒；"
                        "截止前未恢复将自动过期。"
                    )
                return
            review = self.order_reviewer.review(
                candidate,
                quote,
                bars,
                news,
                self._market.risk_on_off if self._market else "neutral",
            )
            if review is None:
                first_failure = self._record_review_failure(
                    candidate, now, "primary-model response unavailable or invalid"
                )
                if first_failure and self.telegram is not None:
                    self.telegram.push_recovery_notice(
                        f"⚠️ {candidate.symbol} 主模型复核失败；尚未挂单，"
                        "系统将按 1/5 分钟间隔最多尝试 3 次，期间不重复提醒；"
                        "截止前未恢复将自动过期。"
                    )
                return
            service = self._confirmation
            if service is None:
                return
            if not review.needs_reconfirmation:
                kept = service.record_post_approval_review(
                    candidate.id, now, review.reason_zh
                )
                if kept is not None and self.telegram is not None:
                    self.telegram.push_recovery_notice(
                        f"🔎 {candidate.symbol} 已按最新行情完成二次复核："
                        f"参数不变。{review.reason_zh}"
                    )
                return

            payload = candidate.model_dump()
            payload.update(review.edits)
            payload.update({
                "id": uuid.uuid4().hex,
                "ts": now,
                "ref_px": quote.last,
                "status": CandidateStatus.RISK_APPROVED,
                "rationale": f"{candidate.rationale}\n二次复核：{review.reason_zh}",
            })
            revised = CandidateOrder.model_validate(payload)
            ok, reason = self._revalidate_edit(revised)
            if not ok:
                invalidated = service.invalidate_after_review(
                    candidate.id, now, f"revised terms failed RiskEngine: {reason}"
                )
                if invalidated is not None and self.telegram is not None:
                    self.telegram.push_recovery_notice(
                        f"⛔ {candidate.symbol} 最新行情复核后的建议未通过硬风控；"
                        "原批准已失效，没有挂单。"
                    )
                return
            pushed = service.request_reconfirmation(
                candidate.id, revised, now, review.reason_zh
            )
            if pushed is not None and self.telegram is not None:
                self.telegram.push_recovery_notice(
                    f"🔁 {candidate.symbol} 最新行情触发参数/观点修订；"
                    "原批准已失效，请确认下面的新卡片。"
                )
                self.telegram.push_cards([pushed], service=self._confirmation)
        except Exception:
            logger.exception(
                "post-approval candidate review crashed",
                extra={"candidate_id": candidate.id, "symbol": candidate.symbol},
            )
        finally:
            with self._review_lock:
                self._review_inflight.discard(candidate.id)

    def _watch_confirmation_window(self, now: datetime) -> None:
        et_now = now.astimezone(self._tz)
        approved = self._approved_candidates(now)
        if approved and self.order_review_required:
            self._launch_post_approval_reviews(approved)
        if (
            approved
            and self._pre_cutoff_reminder <= et_now.time() < self._cutoff_time
            and et_now.date() not in self._pre_cutoff_alerted
        ):
            self._pre_cutoff_alerted.add(et_now.date())
            if self.telegram is not None:
                symbols = ", ".join(candidate.symbol for candidate in approved)
                cutoff = self._cutoff_time.strftime("%H:%M")
                self.telegram.push_recovery_notice(
                    f"⏰ 距 {cutoff} {self._tz_name} 提交截止不足 30 分钟：已批准 "
                    f"{len(approved)} 笔（{symbols}），当前尚未挂单；系统将在 "
                    f"{cutoff} 自动复核并提交。"
                )

        if approved and self._cutoff_time <= et_now.time() < self._market_close_time:
            due = (
                self._last_execution_retry is None
                or now - self._last_execution_retry >= _EXECUTION_RETRY_INTERVAL
            )
            if due:
                self._execute_approved(now, trigger="watchdog_retry", rerisk=True)
        elif et_now.time() >= self._market_close_time:
            self._expire_unexecuted_through(
                now, reason="missed execution: market closed without an order"
            )

    def _execute_approved(
        self, now: datetime, *, trigger: str, rerisk: bool = False
    ) -> ExecutionReport:
        if not self._execution_lock.acquire(blocking=False):
            logger.info("candidate execution already running", extra={"trigger": trigger})
            return ExecutionReport()
        try:
            return self._execute_approved_once(now, trigger=trigger, rerisk=rerisk)
        finally:
            self._execution_lock.release()

    def _execute_approved_once(
        self, now: datetime, *, trigger: str, rerisk: bool = False
    ) -> ExecutionReport:
        candidates = self._approved_candidates(now)
        report = ExecutionReport()
        if not candidates:
            self._last_execution_retry = now
            return report

        if self.order_review_required:
            review_expired: list[CandidateOrder] = []
            for candidate in list(candidates):
                if not self._post_approval_review_complete(candidate.id):
                    if self._expire_candidate(
                        candidate,
                        now,
                        "post-approval LLM/fresh-market review incomplete before cutoff",
                        action="expire_post_approval_review_missing",
                    ):
                        review_expired.append(candidate)
            # Never drop a human-approved order silently: tell the operator the
            # approved single(s) expired because the fresh-market review did not
            # finish before the cutoff (fail-closed — no order was placed).
            if review_expired and self.telegram is not None:
                syms = "、".join(c.symbol for c in review_expired)
                self.telegram.push_recovery_notice(
                    f"⚠️ {syms}：主模型复核未在截止前完成，已批准单据已过期，未挂单、未成交。"
                )
            candidates = self._approved_candidates(now)
            if not candidates:
                self._last_execution_retry = now
                return report

        if rerisk:
            pending_risk = [
                candidate for candidate in candidates
                if candidate.id not in self._restart_risk_validated
            ]
            if pending_risk:
                try:
                    self.on_monitor()
                    account = self.account_monitor.poll().snapshot
                    self._health = self._assess_health(account)
                except Exception:
                    logger.exception("restart risk context refresh failed")
                    self._last_execution_retry = now
                    if self.telegram is not None:
                        self.telegram.push_recovery_notice(
                            "⚠️ 已批准候选的重启复核暂时失败，尚未挂单；"
                            "系统将在 5 分钟后重试，收盘仍失败则自动过期。"
                        )
                    return report
                for candidate in pending_risk:
                    ok, reason = self._revalidate_edit(candidate)
                    if not ok:
                        self._expire_candidate(
                            candidate, now,
                            f"restart risk re-validation failed: {reason}",
                            action="expire_risk_revalidation",
                        )
                    else:
                        self._restart_risk_validated.add(candidate.id)
                candidates = self._approved_candidates(now)
                if not candidates:
                    self._last_execution_retry = now
                    return report

        for candidate in candidates:
            self._audit_once(
                candidate, now, action="finalize",
                prev=candidate.status, new=candidate.status,
                detail=f"{trigger}: approved candidate selected for execution",
                key=f"finalize:{candidate.id}",
            )

        quotes: dict[str, float] = {}
        for candidate in candidates:
            try:
                quotes[candidate.symbol] = self.feed.get_quote(candidate.symbol).last
            except DataFeedError:
                pass
        report = self.execution.execute(candidates, quotes, now)

        for candidate, reason in report.skipped:
            if "no fresh quote" not in reason:
                self._expire_candidate(
                    candidate, now, f"execution re-validation failed: {reason}",
                    action="expire_execution_revalidation",
                )
        rejected_by_candidate = {
            (order.broker_ref or "").removeprefix("candidate:"): reason
            for order, reason in report.rejected
        }
        for candidate in self._approved_candidates(now):
            if candidate.id in rejected_by_candidate:
                self._expire_candidate(
                    candidate, now,
                    f"broker rejected order: {rejected_by_candidate[candidate.id]}",
                    action="expire_broker_rejected",
                )

        self._last_execution_retry = now
        if self.telegram is not None:
            self.telegram.push_execution_outcome(report)
            remaining = self._approved_candidates(now)
            if remaining:
                symbols = ", ".join(candidate.symbol for candidate in remaining)
                self.telegram.push_recovery_notice(
                    f"⚠️ {symbols} 已批准但尚未挂单；系统将在 5 分钟后自动复核重试，"
                    "最迟收盘自动标记为 EXPIRED / missed execution。"
                )
        return report

    def _expire_unexecuted_through(
        self,
        now: datetime,
        reason: str,
        *,
        include_current: bool = True,
    ) -> int:
        et_now = now.astimezone(self._tz)
        expired: list[CandidateOrder] = []
        candidates = self._candidates(
            CandidateStatus.RISK_APPROVED,
            CandidateStatus.PUSHED,
            CandidateStatus.APPROVED,
            CandidateStatus.EDITED,
        )
        for candidate in candidates:
            candidate_date = self._et_date(candidate)
            stale = candidate_date < et_now.date()
            current_closed = include_current and candidate_date == et_now.date()
            if not (stale or current_closed):
                continue
            action = (
                "expire_missed_execution"
                if candidate.status in {CandidateStatus.APPROVED, CandidateStatus.EDITED}
                else "expire"
            )
            if self._expire_candidate(candidate, now, reason, action=action):
                expired.append(candidate)
        if expired and self.telegram is not None:
            approved = [
                candidate for candidate in expired
                if candidate.status in {CandidateStatus.APPROVED, CandidateStatus.EDITED}
            ]
            if approved:
                symbols = ", ".join(candidate.symbol for candidate in approved)
                self.telegram.push_recovery_notice(
                    f"⌛ {symbols} 曾获批准但未成功提交，现已自动标记为 "
                    "EXPIRED / missed execution；不会补发旧订单，下一交易日需重新分析确认。"
                )
        return len(expired)

    def _expire_candidate(
        self,
        candidate: CandidateOrder,
        now: datetime,
        reason: str,
        *,
        action: str,
    ) -> bool:
        current = next(
            (row for row in self.ledger.get_candidates(
                mode=self.mode, market=self.market_id)
             if row.id == candidate.id),
            None,
        )
        if current is None or current.status not in {
            CandidateStatus.RISK_APPROVED,
            CandidateStatus.PUSHED,
            CandidateStatus.APPROVED,
            CandidateStatus.EDITED,
        }:
            return False
        note = f"missed execution: {reason}" if "missed execution" not in reason else reason
        self.ledger.update_candidate(
            current.id, CandidateStatus.EXPIRED, risk_note=note
        )
        self._audit_once(
            current, now, action=action, prev=current.status,
            new=CandidateStatus.EXPIRED, detail=note,
            key=f"{action}:{current.id}",
        )
        return True

    def _audit_once(
        self,
        candidate: CandidateOrder,
        now: datetime,
        *,
        action: str,
        prev: CandidateStatus,
        new: CandidateStatus,
        detail: str,
        key: str,
    ) -> None:
        if self.ledger.get_audit(
            mode=self.mode, candidate_id=candidate.id, idempotency_key=key
        ):
            return
        self.ledger.record_audit(AuditEvent(
            ts=now,
            mode=self.mode.value,
            candidate_id=candidate.id,
            action=action,
            actor="system",
            surface=Surface.SYSTEM.value,
            idempotency_key=key,
            prev_status=prev.value,
            new_status=new.value,
            detail=detail,
        ))

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
        session_signals: list[Signal] = []
        views: dict[str, SymbolView] = {}
        watch = self._portfolio.watch if self._portfolio else {}
        news_items = self._rebuild_news_items()
        discovered = [
            row.symbol for row in (self._discovery.candidates if self._discovery else [])
        ]
        for symbol in dict.fromkeys([*self.symbols, *discovered]):
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
                session_signals.append(sig)
            verdict = self.debate.debate(symbol, signals)
            self.ledger.record_signal(verdict, self.mode)
            session_signals.append(verdict)
            debates.append(verdict)
            item = self.watchlist_lookup(symbol)
            views[symbol] = SymbolView(
                symbol=symbol,
                last=state.last,
                atr_pct=state.atr_pct,
                pool=item.role if item is not None else Role.ROTATION,
            )
        self._latest_signals = session_signals
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
        return curate_news(items, as_of=self.clock(), across_symbols=False).items

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
            self.runtime.health_by_market[self.market_id.lower()] = health
            if self.market_id == "US":
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
        if self.close_timeframe:
            packets: dict[datetime, dict[str, Any]] = {}
            local_day = self.clock().astimezone(self._tz).date()
            active = self.broker.get_orders(active_only=True)
            for symbol in symbols:
                submitted = [order.ts for order in active if order.symbol == symbol]
                earliest = min(submitted) if submitted else None
                try:
                    candles = self.feed.get_bars(
                        symbol, self.close_timeframe, limit=12
                    )
                except (DataFeedError, ValueError):
                    continue
                for candle in candles:
                    if candle.ts.astimezone(self._tz).date() != local_day:
                        continue
                    # A candle begins before every trade in its interval. Skip
                    # the boundary candle rather than risk a pre-order fill.
                    if earliest is not None and candle.ts < earliest:
                        continue
                    packets.setdefault(candle.ts, {})[symbol] = candle
            return [packets[stamp] for stamp in sorted(packets)]
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
                discovery=self._discovery,
                currency=self._brief_currency,
                signals=list(self._latest_signals),
                candidates=self.ledger.get_candidates(
                    mode=self.mode, market=self.market_id
                ),
                risk_status=self._risk_status,
                watchlist_lookup=self.watchlist_lookup,
                trading_tz=self._tz,
                additional_holdings=(
                    self.holdings_provider(self.market_id)
                    if self.holdings_provider is not None
                    else []
                ),
            )
            slot = self.market_id.lower()  # per-market brief slot (us/hk/cn/kr)
            if self.brief_writer is not None:
                brief.narrative = self.brief_writer.write(
                    brief,
                    market_id=self.market_id,
                    market_label=self._market_label,
                    language="zh-CN",
                )
            else:
                # Intraday monitor refreshes update deterministic evidence but
                # must not erase the last promised 09:00/21:00 primary-model
                # narrative. Read THIS market's previous brief (not the US slot)
                # so an HK refresh keeps the HK narrative, never the US one.
                previous = self.runtime.latest_briefs.get(slot) or (
                    self.runtime.latest_brief if self.market_id == "US" else None
                )
                if (
                    isinstance(previous, dict)
                    and previous.get("narrative") is not None
                ):
                    from swing_trader.brief import ResearchNarrative

                    brief.narrative = ResearchNarrative.model_validate(
                        previous["narrative"]
                    )
            dump = brief.model_dump(mode="json")
            # Write the PER-MARKET slot; only US owns the canonical latest_brief
            # so an HK/CN/KR refresh never clobbers the US brief.
            self.runtime.latest_briefs[slot] = dump
            if self.market_id == "US":
                self.runtime.latest_brief = dump
            # Intraday monitor/decision refreshes update volatile evidence only.
            # The canonical 09:00/21:00 BriefCycleCoordinator archives exactly
            # one primary-model publication per market/edition; persisting here
            # created hundreds of duplicate snapshots and forecast runs.
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
                self.earnings_provider,
                watchlist_mod.earnings_symbols(self.symbols),
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
