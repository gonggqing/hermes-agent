"""Tests for swing_trader.api — the Finance service HTTP layer (Loop.md §5.6/§5.9)."""

from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from swing_trader.api import FinanceRuntime, create_app
from swing_trader.confirmation import ConfirmationService
from swing_trader.ledger import Ledger
from swing_trader.paper_broker import PaperBroker
from swing_trader.schemas import (
    AccountSnapshot,
    CandidateOrder,
    CandidateStatus,
    Mode,
    OrderType,
    Side,
)

IN_WINDOW = datetime(2026, 7, 13, 15, 45, tzinfo=timezone.utc)  # 11:45 EDT


def candidate(**kw) -> CandidateOrder:
    base = dict(
        symbol="NVDA", side=Side.BUY, qty=2, order_type=OrderType.BRACKET,
        limit=99.5, stop=91.5, tp=111.5, rationale="test", confidence=0.7,
        ref_px=100.0, status=CandidateStatus.RISK_APPROVED,
    )
    base.update(kw)
    return CandidateOrder(**base)


@pytest.fixture()
def env(tmp_path):
    ledger = Ledger(url=f"sqlite:///{tmp_path/'api.db'}")
    broker = PaperBroker(starting_cash=5_000.0)
    service = ConfirmationService(ledger, mode=Mode.PAPER)
    runtime = FinanceRuntime(
        ledger=ledger, broker=broker, confirmation=service,
        clock=lambda: IN_WINDOW,
    )
    runtime.market = {"risk_on_off": "neutral", "vix": 18.5}
    runtime.latest_reports = {"morning": "all quiet"}
    client = TestClient(create_app(runtime))
    return ledger, service, runtime, client


def publish_one(ledger, service) -> CandidateOrder:
    c = candidate()
    ledger.record_candidate(c, Mode.PAPER)
    return service.publish([c], IN_WINDOW)[0]


class TestReads:
    def test_health(self, env):
        _, _, _, client = env
        body = client.get("/v1/health").json()
        assert body["status"] == "ok"
        assert body["mode"] == "paper"
        assert body["loop_attached"] is True
        assert body["breaker"] == "NORMAL"

    def test_health_ledger_only(self, tmp_path):
        ledger = Ledger(url=f"sqlite:///{tmp_path/'l.db'}")
        ledger.record_snapshot(AccountSnapshot(mode=Mode.PAPER, equity=1000, cash=1000))
        client = TestClient(create_app(FinanceRuntime(ledger=ledger)))
        body = client.get("/v1/health").json()
        assert body["loop_attached"] is False
        assert body["breaker"] == "NORMAL"

    def test_account_live_view(self, env):
        _, _, _, client = env
        body = client.get("/v1/account").json()
        assert body["equity"] == pytest.approx(5000.0)

    def test_account_ledger_fallback_for_other_mode(self, env):
        _, _, _, client = env
        body = client.get("/v1/account", params={"mode": "live"}).json()
        assert body["source"] == "ledger"
        assert body["snapshot"] is None

    def test_unknown_mode_422(self, env):
        _, _, _, client = env
        assert client.get("/v1/account", params={"mode": "demo"}).status_code == 422

    def test_market_watchlist_reports(self, env):
        _, _, _, client = env
        assert client.get("/v1/market").json()["vix"] == 18.5
        wl = client.get("/v1/watchlist").json()
        assert any(i["symbol"] == "NVDA" for i in wl)
        assert client.get("/v1/reports/latest").json()["morning"] == "all quiet"

    def test_empty_collections(self, env):
        _, _, _, client = env
        for path in ("/v1/orders", "/v1/fills", "/v1/trades", "/v1/snapshots"):
            assert client.get(path).json() == []
        assert client.get("/v1/stats").json()["n_closed"] == 0

    def test_pending_empty_without_service(self, tmp_path):
        ledger = Ledger(url=f"sqlite:///{tmp_path/'p.db'}")
        client = TestClient(create_app(FinanceRuntime(ledger=ledger)))
        assert client.get("/v1/candidates/pending").json() == []

    def test_research_brief_degraded_on_demand(self, env):
        """No loop-produced brief -> a degraded ledger-only brief, never 500."""
        _, _, _, client = env
        body = client.get("/v1/research/brief").json()
        assert body["mode"] == "paper"
        assert body["freshness"]["warnings"]  # missing sources called out

    def test_research_brief_prefers_loop_version(self, env):
        _, _, runtime, client = env
        runtime.latest_brief = {"mode": "paper", "marker": "from-loop"}
        assert client.get("/v1/research/brief").json()["marker"] == "from-loop"

    def test_knowledge_search_503_when_unconfigured(self, env):
        _, _, _, client = env
        assert client.get("/v1/knowledge/search", params={"q": "nvda"}).status_code == 503

    def test_knowledge_search_roundtrip_embedded(self, env, tmp_path):
        from datetime import date

        from swing_trader.knowledge_pipeline import (
            KnowledgeConfig,
            build_knowledge,
            ingest_news_snapshot,
        )
        from swing_trader.monitors import NewsSnapshot

        _, _, runtime, client = env
        knowledge, index = build_knowledge(
            KnowledgeConfig(root_dir=tmp_path / "kb")
        )
        assert index is not None
        news = NewsSnapshot(items=[{
            "ts": IN_WINDOW.isoformat(), "symbol": "NVDA",
            "headline": "NVDA announces quantum accelerator breakthrough",
            "source": "sim", "url": "https://example.invalid/a", "sentiment": 0.5,
        }])
        ingest_news_snapshot(knowledge, index, news, date(2026, 7, 13))
        runtime.knowledge, runtime.knowledge_index = knowledge, index
        rows = client.get("/v1/knowledge/search",
                          params={"q": "quantum accelerator"}).json()
        assert rows and rows[0]["source_url"] == "https://example.invalid/a"


class TestPendingAndActions:
    def test_pending_lists_published(self, env):
        ledger, service, _, client = env
        c = publish_one(ledger, service)
        rows = client.get("/v1/candidates/pending").json()
        assert len(rows) == 1
        assert rows[0]["candidate"]["id"] == c.id
        assert rows[0]["version"] == 1
        assert rows[0]["window_open"] is True

    def test_approve_via_http(self, env):
        ledger, service, _, client = env
        c = publish_one(ledger, service)
        resp = client.post(
            f"/v1/candidates/{c.id}/action",
            json={"action": "approve", "actor": "gongqing",
                  "idempotency_key": "web-1"},
            headers={"X-Finance-Surface": "web"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] and body["code"] == "applied"
        assert body["candidate"]["status"] == "approved"
        audit = ledger.get_audit(candidate_id=c.id, idempotency_key="web-1")
        assert audit[0].surface == "web" and audit[0].actor == "gongqing"

    def test_idempotent_replay_via_http(self, env):
        ledger, service, _, client = env
        c = publish_one(ledger, service)
        payload = {"action": "approve", "actor": "u", "idempotency_key": "k1"}
        assert client.post(f"/v1/candidates/{c.id}/action", json=payload).json()["code"] == "applied"
        second = client.post(f"/v1/candidates/{c.id}/action", json=payload)
        assert second.status_code == 200
        assert second.json()["code"] == "replayed"

    def test_edit_via_http(self, env):
        ledger, service, _, client = env
        c = publish_one(ledger, service)
        resp = client.post(
            f"/v1/candidates/{c.id}/action",
            json={"action": "edit", "actor": "u", "idempotency_key": "k1",
                  "expected_version": 1, "edits": {"qty": 1}},
            headers={"X-Finance-Surface": "desktop"},
        )
        assert resp.status_code == 200
        assert resp.json()["candidate"]["qty"] == 1
        assert resp.json()["version"] == 2

    def test_invalid_edit_422(self, env):
        ledger, service, _, client = env
        c = publish_one(ledger, service)
        resp = client.post(
            f"/v1/candidates/{c.id}/action",
            json={"action": "edit", "actor": "u", "idempotency_key": "k1",
                  "edits": {"symbol": "TSLA"}},
        )
        assert resp.status_code == 422

    def test_double_approve_conflict_409(self, env):
        ledger, service, _, client = env
        c = publish_one(ledger, service)
        client.post(f"/v1/candidates/{c.id}/action",
                    json={"action": "approve", "actor": "u",
                          "idempotency_key": "k1"})
        resp = client.post(f"/v1/candidates/{c.id}/action",
                           json={"action": "reject", "actor": "u",
                                 "idempotency_key": "k2"})
        assert resp.status_code == 409

    def test_unknown_candidate_404(self, env):
        _, _, _, client = env
        resp = client.post("/v1/candidates/nope/action",
                           json={"action": "approve", "actor": "u",
                                 "idempotency_key": "k"})
        assert resp.status_code == 404

    def test_window_closed_403(self, env, tmp_path):
        ledger, service, runtime, client = env
        c = publish_one(ledger, service)
        runtime.clock = lambda: datetime(2026, 7, 13, 16, 31, tzinfo=timezone.utc)
        resp = client.post(f"/v1/candidates/{c.id}/action",
                           json={"action": "approve", "actor": "u",
                                 "idempotency_key": "k"})
        assert resp.status_code == 403

    def test_system_surface_forbidden(self, env):
        """Loop.md §3: model tools are not approval authority."""
        ledger, service, _, client = env
        c = publish_one(ledger, service)
        resp = client.post(
            f"/v1/candidates/{c.id}/action",
            json={"action": "approve", "actor": "llm", "idempotency_key": "k"},
            headers={"X-Finance-Surface": "system"},
        )
        assert resp.status_code == 403

    def test_unknown_surface_422(self, env):
        ledger, service, _, client = env
        c = publish_one(ledger, service)
        resp = client.post(
            f"/v1/candidates/{c.id}/action",
            json={"action": "approve", "actor": "u", "idempotency_key": "k"},
            headers={"X-Finance-Surface": "carrier-pigeon"},
        )
        assert resp.status_code == 422

    def test_no_service_503(self, tmp_path):
        ledger = Ledger(url=f"sqlite:///{tmp_path/'n.db'}")
        client = TestClient(create_app(FinanceRuntime(ledger=ledger)))
        resp = client.post("/v1/candidates/x/action",
                           json={"action": "approve", "actor": "u",
                                 "idempotency_key": "k"})
        assert resp.status_code == 503

    def test_body_surface_fallback_for_desktop(self, env):
        """Desktop's IPC bridge cannot set headers; body.surface must win
        when the header is absent (and the header wins when present)."""
        ledger, service, _, client = env
        c = publish_one(ledger, service)
        resp = client.post(
            f"/v1/candidates/{c.id}/action",
            json={"action": "approve", "actor": "u", "idempotency_key": "k1",
                  "surface": "desktop"},
        )
        assert resp.status_code == 200
        audit = ledger.get_audit(candidate_id=c.id, idempotency_key="k1")
        assert audit[0].surface == "desktop"

    def test_header_beats_body_surface(self, env):
        ledger, service, _, client = env
        c = publish_one(ledger, service)
        client.post(
            f"/v1/candidates/{c.id}/action",
            json={"action": "reject", "actor": "u", "idempotency_key": "k2",
                  "surface": "desktop"},
            headers={"X-Finance-Surface": "telegram"},
        )
        audit = ledger.get_audit(candidate_id=c.id, idempotency_key="k2")
        assert audit[0].surface == "telegram"

    def test_trades_endpoint_serializes_closed_trades(self, env):
        """Regression: TradeRecord has entry_ts/exit_ts, not ts."""
        from swing_trader.schemas import Fill

        ledger, _, _, client = env
        ledger.record_fill(
            Fill(order_id="o1", symbol="NVDA", side=Side.BUY, qty=2, px=99.0,
                 commission=1.0),
            stop_px=91.5,
        )
        ledger.record_fill(
            Fill(order_id="o2", symbol="NVDA", side=Side.SELL, qty=2, px=105.0,
                 commission=1.0),
        )
        rows = client.get("/v1/trades").json()
        assert len(rows) == 1
        row = rows[0]
        assert row["mode"] == "paper"
        assert row["entry_ts"] and row["exit_ts"]
        assert row["pnl"] == pytest.approx((105.0 - 99.0) * 2 - 2.0)

    def test_no_place_order_endpoint_exists(self, env):
        """Loop.md §3: order authority is service-bound — the HTTP surface
        must not expose any order-placement route."""
        _, _, _, client = env
        app = client.app
        paths = {r.path for r in app.routes}
        assert not any("order" in p and p != "/v1/orders" for p in paths)
        assert client.post("/v1/orders", json={}).status_code == 405
