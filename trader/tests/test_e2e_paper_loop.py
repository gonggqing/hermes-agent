"""Phase-0 end-to-end paper dry run (Loop.md backlog 17, §7 exit criteria).

Drives the REAL daily loop for 22 simulated trading days across every layer:
monitors → analysis agents → debate → decision core → RiskEngine →
ConfirmationService → human surfaces (Web via the actual HTTP API, Telegram
via a mock transport) → ExecutionEngine → PaperBroker fills → Ledger →
reporter. A −12% crash on day 12 exercises stops, the risk-off regime, and
the audit/expiry paths.
"""

import json
from datetime import date, time, datetime, timezone
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from swing_trader.api import FinanceRuntime, create_app
from swing_trader.dailyloop import DailyLoop, TelegramSurfaceAdapter
from swing_trader.ledger import Ledger
from swing_trader.paper_broker import PaperBroker
from swing_trader.schemas import CandidateStatus, Mode, OrderStatus, OrderType, Side
from swing_trader.simulate import (
    MutableClock,
    SimFeed,
    build_sim_series,
    trading_days,
)

ET = ZoneInfo("America/New_York")
SYMBOLS = ["NVDA", "MU", "ANET"]
N_DAYS = 22
CRASH_DAY = 12


class MockTelegramTransport:
    def __init__(self):
        self.sent: list[tuple[str, dict | None]] = []
        self.queue: list[dict] = []
        self.answered: list[tuple[str, str]] = []
        self._mid = 0
        self._cb = 0

    def send_message(self, chat_id, text, reply_markup=None):
        self._mid += 1
        self.sent.append((text, reply_markup))
        return self._mid

    def get_updates(self, offset=None, timeout=0):
        batch, self.queue = self.queue, []
        return batch

    def answer_callback(self, callback_query_id, text=""):
        self.answered.append((callback_query_id, text))

    def script_approve_first_pending_card(self):
        """Simulate the human tapping Approve on the most recent card batch."""
        for text, markup in reversed(self.sent):
            if not markup:
                continue
            for row in markup.get("inline_keyboard", []):
                for btn in row:
                    data = json.loads(btn["callback_data"])
                    if data.get("a") == "ok":
                        self._cb += 1
                        self.queue.append({
                            "update_id": 1000 + self._cb,
                            "callback_query": {
                                "id": f"cb{self._cb}",
                                "data": btn["callback_data"],
                                "from": {"id": 777, "username": "gongqing"},
                                "message": {"message_id": 1},
                            },
                        })
                        return True
        return False


@pytest.fixture(scope="module")
def e2e(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("e2e")
    days = trading_days(date(2026, 7, 13), N_DAYS)
    series, warmup = build_sim_series(SYMBOLS, days, crash_day=CRASH_DAY)
    feed = SimFeed(series, vix_crash_at=warmup + CRASH_DAY)
    ledger = Ledger(url=f"sqlite:///{tmp/'e2e.db'}")
    broker = PaperBroker(starting_cash=5_000.0)
    clock = MutableClock(now=datetime.combine(
        days[0], time(8, 0), tzinfo=ET).astimezone(timezone.utc))
    runtime = FinanceRuntime(ledger=ledger, broker=broker, clock=clock)
    transport = MockTelegramTransport()
    telegram = TelegramSurfaceAdapter(transport, chat_id="42")
    reports: list[str] = []
    loop = DailyLoop(feed, broker, ledger, symbols=SYMBOLS, clock=clock,
                     runtime=runtime, telegram=telegram,
                     notify=reports.append)
    client = TestClient(create_app(runtime))

    morning: list[str] = []
    naked_violations: list[str] = []
    pending_seen_day0: list[dict] = []

    for i, d in enumerate(days):
        feed.set_day(warmup + i)
        clock.set_et(d, 9, 0)
        before = len(reports)
        loop.on_morning_report()
        morning.extend(reports[before:])

        clock.set_et(d, 9, 30)
        loop.on_monitor()
        clock.set_et(d, 11, 0)
        loop.on_decide()
        clock.set_et(d, 11, 30, 30)
        loop.on_push()

        clock.set_et(d, 11, 45)
        if i == 0:
            pending_seen_day0 = client.get("/v1/candidates/pending").json()
            rows = pending_seen_day0
            if len(rows) >= 1:  # human approves the first via the WEB surface
                r = client.post(
                    f"/v1/candidates/{rows[0]['candidate']['id']}/action",
                    json={"action": "approve", "actor": "e2e-user",
                          "idempotency_key": "e2e-day0-approve",
                          "expected_version": rows[0]["version"]},
                    headers={"X-Finance-Surface": "web"},
                )
                assert r.status_code == 200, r.text
            if len(rows) >= 2:  # rejects the second
                r = client.post(
                    f"/v1/candidates/{rows[1]['candidate']['id']}/action",
                    json={"action": "reject", "actor": "e2e-user",
                          "idempotency_key": "e2e-day0-reject"},
                    headers={"X-Finance-Surface": "web"},
                )
                assert r.status_code == 200, r.text
            # the third (if any) is deliberately ignored -> expires
        elif i == 1:
            if transport.script_approve_first_pending_card():
                loop.on_confirm_poll()
        # all other days: nobody responds -> server-side expiry

        clock.set_et(d, 12, 30, 30)
        loop.on_cutoff()

        clock.set_et(d, 16, 0, 30)
        bars = {sym: feed.bar_for_day(sym, warmup + i) for sym in series}
        loop.on_close(bars=bars)

        # §4 invariant: NEVER a position without a resting protective stop.
        active = broker.get_orders(active_only=True)
        for pos in broker.get_positions():
            stops = [o for o in active
                     if o.symbol == pos.symbol and o.side is Side.SELL
                     and o.order_type is OrderType.STP]
            if not stops:
                naked_violations.append(f"day {i} {pos.symbol}")

        feed.set_day(warmup + i + 1)

    return {
        "days": days, "ledger": ledger, "broker": broker, "client": client,
        "transport": transport, "morning": morning,
        "naked": naked_violations, "pending_day0": pending_seen_day0,
    }


class TestExitCriteria:
    """Loop.md §7 Phase-0 exit criteria, exercised end-to-end."""

    def test_twenty_plus_trading_days_logged(self, e2e):
        assert len(e2e["days"]) >= 20
        snapshots = e2e["ledger"].get_snapshots(Mode.PAPER)
        assert len(snapshots) == 2 * len(e2e["days"])  # morning + close

    def test_reporter_produced_daily_summaries(self, e2e):
        assert len(e2e["morning"]) == len(e2e["days"])
        assert all("mode=paper" in r for r in e2e["morning"])

    def test_trades_flowed_end_to_end(self, e2e):
        ledger = e2e["ledger"]
        assert ledger.get_signals(Mode.PAPER)
        assert ledger.get_candidates(mode=Mode.PAPER)
        assert ledger.get_fills(Mode.PAPER)
        assert ledger.get_trades(Mode.PAPER)
        stats = ledger.stats(Mode.PAPER)
        assert stats.n_closed >= 1

    def test_paper_live_separation(self, e2e):
        ledger = e2e["ledger"]
        assert ledger.get_orders(mode=Mode.LIVE) == []
        assert ledger.get_fills(Mode.LIVE) == []
        assert ledger.get_trades(Mode.LIVE) == []
        assert ledger.get_snapshots(Mode.LIVE) == []


class TestHumanAuthority:
    def test_day0_pending_visible_via_http(self, e2e):
        rows = e2e["pending_day0"]
        assert len(rows) >= 2
        assert all(r["window_open"] for r in rows)

    def test_web_approval_audited_and_placed(self, e2e):
        audit = e2e["ledger"].get_audit(mode=Mode.PAPER)
        approvals = [a for a in audit
                     if a.action == "approve" and a.surface == "web" and a.applied]
        assert approvals and approvals[0].actor == "e2e-user"
        placed = e2e["ledger"].get_candidates(mode=Mode.PAPER,
                                              status=CandidateStatus.PLACED)
        assert placed

    def test_web_rejection_audited(self, e2e):
        audit = e2e["ledger"].get_audit(mode=Mode.PAPER)
        assert any(a.action == "reject" and a.surface == "web" and a.applied
                   for a in audit)
        assert e2e["ledger"].get_candidates(mode=Mode.PAPER,
                                            status=CandidateStatus.REJECTED)

    def test_telegram_approval_shares_the_state_machine(self, e2e):
        audit = e2e["ledger"].get_audit(mode=Mode.PAPER)
        tg = [a for a in audit
              if a.action == "approve" and a.surface == "telegram" and a.applied]
        assert tg and tg[0].actor == "telegram:gongqing"
        assert e2e["transport"].answered  # callbacks were acknowledged

    def test_unattended_candidates_expired_server_side(self, e2e):
        expired = e2e["ledger"].get_candidates(mode=Mode.PAPER,
                                               status=CandidateStatus.EXPIRED)
        assert expired
        audit = e2e["ledger"].get_audit(mode=Mode.PAPER)
        assert any(a.action == "expire" and a.surface == "system" for a in audit)

    def test_every_publish_is_system_not_approval(self, e2e):
        audit = e2e["ledger"].get_audit(mode=Mode.PAPER)
        publishes = [a for a in audit if a.action == "publish"]
        assert publishes
        assert all(a.actor == "system" and a.surface == "system"
                   for a in publishes)


class TestSafetyInvariants:
    def test_never_a_naked_position(self, e2e):
        assert e2e["naked"] == []

    def test_crash_stops_executed(self, e2e):
        closed = e2e["ledger"].get_trades(Mode.PAPER, closed_only=True)
        assert any(t.pnl is not None and t.pnl < 0 for t in closed), \
            "crash day should have stopped out at least one position"

    def test_no_new_entries_after_crash_risk_off(self, e2e):
        crash_ts = e2e["ledger"].get_snapshots(Mode.PAPER)[2 * CRASH_DAY + 1].ts
        buys_after = [
            o for o in e2e["ledger"].get_orders(mode=Mode.PAPER)
            if o.side is Side.BUY and o.ts > crash_ts
        ]
        assert buys_after == []

    def test_final_account_consistent(self, e2e):
        account = e2e["broker"].get_account()
        assert account.equity > 0
        stats = e2e["ledger"].stats(Mode.PAPER)
        assert stats.n_closed > 0
        health = e2e["client"].get("/v1/health").json()
        assert health["mode"] == "paper"
