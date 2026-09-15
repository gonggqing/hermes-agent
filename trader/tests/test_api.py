"""Tests for swing_trader.api — the Finance service HTTP layer (Loop.md §5.6/§5.9)."""

from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from swing_trader.api import FinanceRuntime, create_app
from swing_trader.confirmation import ConfirmationService
from swing_trader.ledger import Ledger
from swing_trader.paper_broker import PaperBroker
from swing_trader.portfolio_controls import PortfolioControlStore
from swing_trader.schemas import (
    AccountSnapshot,
    CandidateOrder,
    CandidateStatus,
    Mode,
    OrderType,
    Side,
)

IN_WINDOW = datetime(2026, 7, 13, 14, 45, tzinfo=timezone.utc)  # 10:45 EDT


def candidate(**kw) -> CandidateOrder:
    base = dict(
        symbol="NVDA",
        side=Side.BUY,
        qty=2,
        order_type=OrderType.BRACKET,
        limit=99.5,
        stop=91.5,
        tp=111.5,
        rationale="test",
        confidence=0.7,
        ref_px=100.0,
        status=CandidateStatus.RISK_APPROVED,
    )
    base.update(kw)
    return CandidateOrder(**base)


@pytest.fixture()
def env(tmp_path):
    ledger = Ledger(url=f"sqlite:///{tmp_path / 'api.db'}")
    broker = PaperBroker(starting_cash=5_000.0)
    service = ConfirmationService(ledger, mode=Mode.PAPER)
    runtime = FinanceRuntime(
        ledger=ledger,
        broker=broker,
        confirmation=service,
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
        ledger = Ledger(url=f"sqlite:///{tmp_path / 'l.db'}")
        ledger.record_snapshot(AccountSnapshot(mode=Mode.PAPER, equity=1000, cash=1000))
        client = TestClient(create_app(FinanceRuntime(ledger=ledger)))
        body = client.get("/v1/health").json()
        assert body["loop_attached"] is False
        assert body["breaker"] == "NORMAL"
        assert "health" not in body  # no assessment until the loop runs

    def test_instruments_search(self, tmp_path):
        from swing_trader.instruments import CachedInstrumentSearch, StaticInstrumentProvider

        ledger = Ledger(url=f"sqlite:///{tmp_path / 'i.db'}")
        runtime = FinanceRuntime(ledger=ledger)
        runtime.instrument_search = CachedInstrumentSearch(StaticInstrumentProvider())
        client = TestClient(create_app(runtime))

        body = client.get("/v1/instruments/search", params={"q": "腾讯"}).json()
        assert body["degraded"] is False
        assert body["matches"][0]["canonical_symbol"] == "0700.HK"
        assert body["matches"][0]["exchange"] == "SEHK"

        # market filter + unknown market
        hk = client.get("/v1/instruments/search", params={"q": "0", "market": "hk"}).json()
        assert all(m["market"] == "HK" for m in hk["matches"])
        assert (
            client.get("/v1/instruments/search", params={"q": "x", "market": "zz"}).status_code
            == 422
        )

    def test_instruments_search_unavailable_503(self, tmp_path):
        ledger = Ledger(url=f"sqlite:///{tmp_path / 'i2.db'}")
        client = TestClient(create_app(FinanceRuntime(ledger=ledger)))  # no provider
        assert client.get("/v1/instruments/search", params={"q": "nv"}).status_code == 503

    def test_health_exposes_dead_mans_switch(self, env):
        """Phase 0.8: when the loop has assessed health, /v1/health surfaces the
        dead-man's-switch state + per-check reasons (read-only)."""
        from datetime import datetime, timezone

        from swing_trader.health import HealthCheck, HealthLevel, HealthStatus

        _, _, runtime, client = env
        runtime.health = HealthStatus(
            level=HealthLevel.UNHEALTHY,
            as_of=datetime(2026, 7, 13, tzinfo=timezone.utc),
            entries_allowed=False,
            checks=[
                HealthCheck(
                    name="market", level=HealthLevel.UNHEALTHY, detail="market data is 200 min old"
                )
            ],
            warnings=["market data stale (200 min)"],
        )
        body = client.get("/v1/health").json()
        assert body["health"]["level"] == "unhealthy"
        assert body["health"]["entries_allowed"] is False
        assert body["health"]["warnings"] == ["market data stale (200 min)"]
        assert body["health"]["checks"][0]["name"] == "market"

    def test_account_live_view(self, env):
        _, _, _, client = env
        body = client.get("/v1/account").json()
        assert body["equity"] == pytest.approx(5000.0)

    def test_portfolio_controls_are_durable_and_apply_live(self, env, tmp_path):
        _, _, runtime, client = env
        runtime.portfolio_controls = PortfolioControlStore(f"sqlite:///{tmp_path / 'controls.db'}")
        applied = []
        runtime.apply_portfolio_controls = applied.append

        defaults = client.get("/v1/portfolio/controls")
        assert defaults.status_code == 200
        assert defaults.json()["cash_reserve_floor_pct"] == 35.0

        body = {
            "invested_target_pct": 55,
            "invested_tolerance_pct": 5,
            "agent_budget_pct": 25,
            "agent_budget_tolerance_pct": 5,
            "max_position_pct": 10,
            "per_trade_risk_pct": 0.8,
            "max_new_positions_per_day": 2,
            "base_currency": "usd",
        }
        response = client.put("/v1/portfolio/controls", json=body)
        assert response.status_code == 200
        assert response.json()["cash_reserve_floor_pct"] == 40.0
        assert response.json()["base_currency"] == "USD"
        assert applied[-1].max_new_positions_per_day == 2

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
        market = client.get("/v1/market").json()
        assert market["vix"] == 18.5
        assert market["source"] == "runtime"
        wl = client.get("/v1/watchlist").json()
        assert any(i["symbol"] == "NVDA" for i in wl)
        assert client.get("/v1/reports/latest").json()["morning"] == "all quiet"

    def test_market_prefers_runtime_over_research_brief(self, env):
        _, _, runtime, client = env
        runtime.latest_brief = {
            "as_of": "2026-07-12T15:00:00Z",
            "regime": {"risk_on_off": "risk_off", "vix": 99.0},
        }

        body = client.get("/v1/market").json()
        assert body["vix"] == 18.5
        assert body["source"] == "runtime"

    def test_market_falls_back_to_latest_in_memory_brief(self, env):
        _, _, runtime, client = env
        runtime.market = {}
        runtime.latest_brief = {
            "as_of": "2026-07-14T15:00:00Z",
            "freshness": {"market_as_of": "2026-07-14T14:55:00Z"},
            "regime": {
                "risk_on_off": "risk_on",
                "vix": 16.4,
                "breadth_pct_above_50dma": 56.7,
                "indices": {"SPY": {"last": 620.0}},
            },
        }

        body = client.get("/v1/market").json()
        assert body["risk_on_off"] == "risk_on"
        assert body["vix"] == 16.4
        assert body["ts"] == "2026-07-14T14:55:00Z"
        assert body["source"] == "research_brief"

    def test_market_restores_latest_brief_from_archive(self, env, tmp_path):
        from swing_trader.brief_store import BriefStore

        _, _, runtime, client = env
        runtime.market = {}
        runtime.latest_brief = {"as_of": "2026-07-14T15:00:00Z"}
        runtime.brief_store = BriefStore(url=f"sqlite:///{tmp_path / 'market.db'}")
        runtime.brief_store.save(
            "us",
            {
                "as_of": "2026-07-13T15:00:00Z",
                "trading_date": "2026-07-13",
                "regime": {"risk_on_off": "neutral", "vix": 17.2},
            },
        )

        body = client.get("/v1/market").json()
        assert body == {
            "risk_on_off": "neutral",
            "vix": 17.2,
            "ts": "2026-07-13T15:00:00Z",
            "source": "research_brief",
        }
        assert runtime.latest_brief["trading_date"] == "2026-07-13"

    def test_market_ignores_brief_without_regime(self, env):
        _, _, runtime, client = env
        runtime.market = {}
        runtime.latest_brief = {"as_of": "2026-07-14T15:00:00Z"}
        assert client.get("/v1/market").json() == {"status": "no snapshot yet"}

    def test_research_brief_keeps_latest_publication_during_intraday_refresh(
        self, env, tmp_path
    ):
        from swing_trader.brief_store import BriefStore

        _, _, runtime, client = env
        runtime.brief_store = BriefStore(url=f"sqlite:///{tmp_path / 'published.db'}")
        runtime.brief_store.save(
            "us",
            {
                "as_of": "2026-07-13T14:45:00Z",
                "trading_date": "2026-07-13",
                "mode": "paper",
                "freshness": {"warnings": []},
                "uncertainty": [],
                "narrative": {
                    "generated_at": "2026-07-13T14:45:00Z",
                    "market": "US",
                    "language": "zh-CN",
                    "model": "primary",
                    "headline": "已发布研报",
                    "summary": "正式版本",
                    "sections": [],
                    "watch_next": [],
                    "edition": "morning",
                    "prompt_version": "test",
                    "evidence_hash": "abc",
                },
                "publication": {
                    "edition_id": "2026-07-13:morning:us",
                    "edition": "morning",
                    "scheduled_for": "2026-07-13T13:00:00Z",
                    "evidence_as_of": "2026-07-13T14:45:00Z",
                    "status": "complete",
                    "failure": "",
                },
            },
        )
        runtime.latest_brief = {
            "as_of": "2026-07-14T14:45:00Z",
            "trading_date": "2026-07-14",
            "mode": "paper",
            "freshness": {"warnings": []},
            "uncertainty": ["intraday evidence"],
        }

        body = client.get("/v1/research/brief").json()

        assert body["narrative"]["headline"] == "已发布研报"
        assert body["publication"]["status"] == "complete"

    def test_empty_collections(self, env):
        _, _, _, client = env
        for path in ("/v1/orders", "/v1/fills", "/v1/trades", "/v1/snapshots"):
            assert client.get(path).json() == []
        assert client.get("/v1/stats").json()["n_closed"] == 0

    def test_pending_empty_without_service(self, tmp_path):
        ledger = Ledger(url=f"sqlite:///{tmp_path / 'p.db'}")
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

    def test_prediction_summary_is_read_only_and_filter_validated(self, env, tmp_path):
        from swing_trader.prediction_ledger import PredictionLedger

        _, _, runtime, client = env
        assert client.get("/v1/predictions/summary").status_code == 503
        runtime.prediction_ledger = PredictionLedger(f"sqlite:///{tmp_path / 'predictions.db'}")
        body = client.get(
            "/v1/predictions/summary",
            params={"market": "us", "due_as_of": "2026-07-17"},
        ).json()
        assert body["filters"]["market"] == "US"
        assert body["overview"]["series"] == 0
        assert body["overview"]["directional_accuracy"] is None
        assert client.get("/v1/predictions/summary", params={"market": "mars"}).status_code == 422

    def test_research_brief_kr_routes_to_per_market_slot(self, env):
        _, _, runtime, client = env
        runtime.latest_briefs["kr"] = {"mode": "paper", "marker": "kr-brief"}
        assert (
            client.get("/v1/research/brief", params={"market": "kr"}).json()["marker"] == "kr-brief"
        )
        # US brief unaffected by the KR slot
        runtime.latest_brief = {"mode": "paper", "marker": "us"}
        assert client.get("/v1/research/brief").json()["marker"] == "us"

    def test_research_brief_kr_degraded_when_no_session_yet(self, env):
        _, _, _, client = env
        body = client.get("/v1/research/brief", params={"market": "kr"}).json()
        assert any("Korea" in u for u in body["uncertainty"])

    def test_research_brief_cn_backcompat_slot(self, env):
        _, _, runtime, client = env
        runtime.latest_brief_cn = {"mode": "paper", "marker": "cn-legacy"}
        assert (
            client.get("/v1/research/brief", params={"market": "cn"}).json()["marker"]
            == "cn-legacy"
        )

    def test_cn_and_hk_briefs_stay_independent_and_include_synthesis(self, env):
        _, _, runtime, client = env
        runtime.latest_briefs["cn"] = {
            "marker": "cn",
            "as_of": "2026-07-15T03:00:00Z",
            "trading_date": "2026-07-15",
            "freshness": {"status": "fresh"},
            "regime": {"risk_on_off": "risk_off"},
        }
        runtime.latest_briefs["hk"] = {
            "marker": "hk",
            "as_of": "2026-07-15T03:00:00Z",
            "trading_date": "2026-07-15",
            "freshness": {"status": "stale"},
            "regime": {"risk_on_off": "risk_on"},
        }
        cn = client.get("/v1/research/brief", params={"market": "cn"}).json()
        hk = client.get("/v1/research/brief", params={"market": "hk"}).json()
        assert cn["marker"] == "cn" and hk["marker"] == "hk"
        synth = hk["cross_market_synthesis"]
        assert synth["markets"]["cn"]["regime"] == "risk_off"
        assert synth["markets"]["hk"]["regime"] == "risk_on"

    def test_research_synthesis_endpoint_degrades_without_hk(self, env):
        _, _, runtime, client = env
        runtime.latest_briefs["cn"] = {"trading_date": "2026-07-15"}
        body = client.get("/v1/research/synthesis").json()
        assert body["status"] == "degraded"
        assert body["markets"]["cn"]["available"] is True
        assert body["markets"]["hk"]["available"] is False

    def test_research_brief_restores_market_from_archive(self, env, tmp_path):
        from swing_trader.brief_store import BriefStore

        _, _, runtime, client = env
        runtime.brief_store = BriefStore(url=f"sqlite:///{tmp_path / 'briefs.db'}")
        runtime.brief_store.save(
            "kr",
            {
                "as_of": "2026-07-14T06:00:00Z",
                "trading_date": "2026-07-14",
                "mode": "paper",
                "marker": "kr-archive",
            },
        )

        body = client.get("/v1/research/brief", params={"market": "kr"}).json()
        assert body["marker"] == "kr-archive"
        assert runtime.latest_briefs["kr"]["marker"] == "kr-archive"

    def test_research_brief_restores_us_from_archive(self, env, tmp_path):
        from swing_trader.brief_store import BriefStore

        _, _, runtime, client = env
        runtime.brief_store = BriefStore(url=f"sqlite:///{tmp_path / 'us-briefs.db'}")
        runtime.brief_store.save(
            "us",
            {
                "as_of": "2026-07-14T15:00:00Z",
                "trading_date": "2026-07-14",
                "mode": "paper",
                "marker": "us-archive",
            },
        )

        body = client.get("/v1/research/brief").json()
        assert body["marker"] == "us-archive"
        assert runtime.latest_brief["marker"] == "us-archive"

    def test_research_brief_quarantines_legacy_stale_narrative(self, env):
        _, _, runtime, client = env
        runtime.latest_brief = {
            "as_of": "2026-09-09T07:00:00Z",
            "trading_date": "2026-09-09",
            "mode": "paper",
            "freshness": {"warnings": []},
            "movers": {"top": [], "bottom": []},
            "themes": [],
            "events": {"earnings": [], "notes": []},
            "news": {"items": [], "per_symbol_sentiment": {}},
            "signals_today": [],
            "candidates_today": {"counts": {}, "pending": []},
            "uncertainty": [],
            "provenance": [],
            "narrative": {
                "generated_at": "2026-08-12T07:00:00Z",
                "market": "US",
                "edition": "morning",
                "headline": "old prose",
            },
        }

        body = client.get("/v1/research/brief").json()

        assert body["narrative"] is None
        assert body["publication"]["status"] == "narrative_failed"
        assert any("旧文字已隔离" in row for row in body["uncertainty"])

    def test_research_run_triggers_hook(self, env):
        _, _, runtime, client = env
        import threading

        done = threading.Event()
        called = {"n": 0}

        def _run():
            called["n"] += 1
            done.set()
            return {"market": "KR", "brief_ready": True}

        runtime.run_research["kr"] = _run
        r = client.post("/v1/research/run", params={"market": "kr"})
        # Returns IMMEDIATELY (background run — avoids the proxy's 15s timeout).
        assert r.status_code == 200 and r.json()["status"] == "started"
        assert r.json()["market"] == "kr"
        assert done.wait(timeout=2)  # the background thread runs the hook
        assert called["n"] == 1

    def test_research_run_already_running_returns_fast(self, env):
        _, _, runtime, client = env
        runtime.research_running.add("kr")  # pretend a run is in flight
        runtime.run_research["kr"] = lambda: {}
        r = client.post("/v1/research/run", params={"market": "kr"})
        assert r.status_code == 200 and r.json()["status"] == "already_running"

    def test_research_run_404_for_unknown_market(self, env):
        _, _, runtime, client = env
        runtime.run_research["cn"] = lambda: {}
        r = client.post("/v1/research/run", params={"market": "kr"})
        assert r.status_code == 404 and "no research session" in r.json()["detail"]

    def test_research_run_is_ungated(self, env):
        # research is read-only → no human-surface gate (unlike /session/run)
        _, _, runtime, client = env
        runtime.run_research["cn"] = lambda: {"ok": True}
        r = client.post(
            "/v1/research/run", params={"market": "cn"}, headers={"X-Finance-Surface": "system"}
        )
        assert r.status_code == 200  # system surface allowed for research

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
        knowledge, index = build_knowledge(KnowledgeConfig(root_dir=tmp_path / "kb"))
        assert index is not None
        news = NewsSnapshot(
            items=[
                {
                    "ts": IN_WINDOW.isoformat(),
                    "symbol": "NVDA",
                    "headline": "NVDA announces quantum accelerator breakthrough",
                    "source": "sim",
                    "url": "https://example.invalid/a",
                    "sentiment": 0.5,
                }
            ]
        )
        ingest_news_snapshot(knowledge, index, news, date(2026, 7, 13))
        runtime.knowledge, runtime.knowledge_index = knowledge, index
        rows = client.get("/v1/knowledge/search", params={"q": "quantum accelerator"}).json()
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
            json={"action": "approve", "actor": "gongqing", "idempotency_key": "web-1"},
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
        assert (
            client.post(f"/v1/candidates/{c.id}/action", json=payload).json()["code"] == "applied"
        )
        second = client.post(f"/v1/candidates/{c.id}/action", json=payload)
        assert second.status_code == 200
        assert second.json()["code"] == "replayed"

    def test_edit_via_http(self, env):
        ledger, service, _, client = env
        c = publish_one(ledger, service)
        resp = client.post(
            f"/v1/candidates/{c.id}/action",
            json={
                "action": "edit",
                "actor": "u",
                "idempotency_key": "k1",
                "expected_version": 1,
                "edits": {"qty": 1},
            },
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
            json={
                "action": "edit",
                "actor": "u",
                "idempotency_key": "k1",
                "edits": {"symbol": "TSLA"},
            },
        )
        assert resp.status_code == 422

    def test_double_approve_conflict_409(self, env):
        ledger, service, _, client = env
        c = publish_one(ledger, service)
        client.post(
            f"/v1/candidates/{c.id}/action",
            json={"action": "approve", "actor": "u", "idempotency_key": "k1"},
        )
        resp = client.post(
            f"/v1/candidates/{c.id}/action",
            json={"action": "reject", "actor": "u", "idempotency_key": "k2"},
        )
        assert resp.status_code == 409

    def test_unknown_candidate_404(self, env):
        _, _, _, client = env
        resp = client.post(
            "/v1/candidates/nope/action",
            json={"action": "approve", "actor": "u", "idempotency_key": "k"},
        )
        assert resp.status_code == 404

    def test_window_closed_403(self, env, tmp_path):
        ledger, service, runtime, client = env
        c = publish_one(ledger, service)
        runtime.clock = lambda: datetime(2026, 7, 13, 16, 31, tzinfo=timezone.utc)
        resp = client.post(
            f"/v1/candidates/{c.id}/action",
            json={"action": "approve", "actor": "u", "idempotency_key": "k"},
        )
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
        ledger = Ledger(url=f"sqlite:///{tmp_path / 'n.db'}")
        client = TestClient(create_app(FinanceRuntime(ledger=ledger)))
        resp = client.post(
            "/v1/candidates/x/action",
            json={"action": "approve", "actor": "u", "idempotency_key": "k"},
        )
        assert resp.status_code == 503

    def test_body_surface_fallback_for_desktop(self, env):
        """Desktop's IPC bridge cannot set headers; body.surface must win
        when the header is absent (and the header wins when present)."""
        ledger, service, _, client = env
        c = publish_one(ledger, service)
        resp = client.post(
            f"/v1/candidates/{c.id}/action",
            json={"action": "approve", "actor": "u", "idempotency_key": "k1", "surface": "desktop"},
        )
        assert resp.status_code == 200
        audit = ledger.get_audit(candidate_id=c.id, idempotency_key="k1")
        assert audit[0].surface == "desktop"

    def test_header_beats_body_surface(self, env):
        ledger, service, _, client = env
        c = publish_one(ledger, service)
        client.post(
            f"/v1/candidates/{c.id}/action",
            json={"action": "reject", "actor": "u", "idempotency_key": "k2", "surface": "desktop"},
            headers={"X-Finance-Surface": "telegram"},
        )
        audit = ledger.get_audit(candidate_id=c.id, idempotency_key="k2")
        assert audit[0].surface == "telegram"

    def test_trades_endpoint_serializes_closed_trades(self, env):
        """Regression: TradeRecord has entry_ts/exit_ts, not ts."""
        from swing_trader.schemas import Fill

        ledger, _, _, client = env
        ledger.record_fill(
            Fill(order_id="o1", symbol="NVDA", side=Side.BUY, qty=2, px=99.0, commission=1.0),
            stop_px=91.5,
        )
        ledger.record_fill(
            Fill(order_id="o2", symbol="NVDA", side=Side.SELL, qty=2, px=105.0, commission=1.0),
        )
        rows = client.get("/v1/trades").json()
        assert len(rows) == 1
        row = rows[0]
        assert row["mode"] == "paper"
        assert row["entry_ts"] and row["exit_ts"]
        assert row["pnl"] == pytest.approx((105.0 - 99.0) * 2 - 2.0)

    def test_no_place_order_endpoint_exists(self, env):
        """Loop.md §3: order authority is service-bound — the HTTP surface
        must not expose any order-PLACEMENT route. The only order-authority
        route allowed is the human-gated safety CANCEL-ALL (reduces exposure,
        never places), part of the kill-switch drill."""
        _, _, _, client = env
        app = client.app
        paths = {r.path for r in app.routes}
        _SAFE = {"/v1/orders", "/v1/orders/cancel-all"}
        assert not any("order" in p and p not in _SAFE for p in paths)
        assert client.post("/v1/orders", json={}).status_code == 405


class TestMultiMarketConfirmation:
    """Two order-capable markets share one runtime. Before the per-market
    registry, ``runtime.confirmation`` was a single slot each loop overwrote,
    so the web dashboard could only ever SEE and ACT on the market whose loop
    assigned last (HK at startup). These tests pin the aggregation + routing.
    """

    def _two_market_client(self, tmp_path):
        ledger = Ledger(url=f"sqlite:///{tmp_path / 'mm.db'}")
        us = ConfirmationService(ledger, mode=Mode.PAPER, market_tz="America/New_York")
        hk = ConfirmationService(ledger, mode=Mode.PAPER, market_tz="Asia/Hong_Kong")
        # 10:45 in each market's own local time → a valid publish window there.
        us_at = datetime(2026, 7, 13, 14, 45, tzinfo=timezone.utc)  # 10:45 EDT
        hk_at = datetime(2026, 7, 13, 2, 45, tzinfo=timezone.utc)  # 10:45 HKT
        us_c = candidate(symbol="NVDA", market="US")
        hk_c = candidate(symbol="0700.HK", market="HK")
        ledger.record_candidate(us_c, Mode.PAPER)
        ledger.record_candidate(hk_c, Mode.PAPER)
        us.publish([us_c], us_at)
        hk.publish([hk_c], hk_at)
        # NOTE: legacy single slot deliberately left None — routing must work
        # off the per-market registry alone.
        runtime = FinanceRuntime(ledger=ledger, clock=lambda: us_at)
        runtime.confirmation_by_market = {"us": us, "hk": hk}
        return runtime, TestClient(create_app(runtime)), us_c, hk_c

    def test_pending_lists_every_market(self, tmp_path):
        _, client, us_c, hk_c = self._two_market_client(tmp_path)
        rows = client.get("/v1/candidates/pending").json()
        by_id = {r["candidate"]["id"]: r for r in rows}
        assert us_c.id in by_id and hk_c.id in by_id  # neither market vanishes
        # each row carries ITS market's real window, not a hard-coded US clock
        assert by_id[us_c.id]["window"]["tz"] == "America/New_York"
        assert by_id[us_c.id]["window"] == {
            "push": "10:30",
            "cutoff": "11:30",
            "tz": "America/New_York",
            "market": "US",
        }
        assert by_id[hk_c.id]["window"]["tz"] == "Asia/Hong_Kong"
        # clock sits in the US window → US open, HK closed, both still listed
        assert by_id[us_c.id]["window_open"] is True
        assert by_id[hk_c.id]["window_open"] is False

    def test_act_routes_to_owning_market(self, tmp_path):
        # runtime.confirmation is None; only the registry can locate candidates.
        _, client, us_c, hk_c = self._two_market_client(tmp_path)
        ok = client.post(
            f"/v1/candidates/{us_c.id}/action",
            json={"action": "approve", "actor": "gongqing", "idempotency_key": "u1"},
            headers={"X-Finance-Surface": "web"},
        )
        assert ok.status_code == 200 and ok.json()["code"] == "applied"
        # HK candidate is found in the HK service (not 404/503); its own window
        # is closed at this instant, so the gate refuses — proving it routed.
        hk = client.post(
            f"/v1/candidates/{hk_c.id}/action",
            json={"action": "approve", "actor": "gongqing", "idempotency_key": "h1"},
            headers={"X-Finance-Surface": "web"},
        )
        assert hk.status_code == 403 and hk.json()["code"] == "window_closed"


class TestByMarketStats:
    def test_stats_by_market_never_blends_currencies(self, env):
        from datetime import timedelta

        from swing_trader.schemas import Fill, Mode, Side

        ledger, _, _, client = env
        t0 = IN_WINDOW

        def close(symbol, currency, entry, exit_px, day):
            t = t0 + timedelta(days=day)
            ledger.record_fill(
                Fill(
                    order_id=f"e{symbol}{day}",
                    symbol=symbol,
                    currency=currency,
                    side=Side.BUY,
                    qty=10,
                    px=entry,
                    commission=1.0,
                    mode=Mode.PAPER,
                    ts=t,
                )
            )
            ledger.record_fill(
                Fill(
                    order_id=f"x{symbol}{day}",
                    symbol=symbol,
                    currency=currency,
                    side=Side.SELL,
                    qty=10,
                    px=exit_px,
                    commission=1.0,
                    mode=Mode.PAPER,
                    ts=t + timedelta(days=1),
                )
            )

        close("NVDA", "USD", 100.0, 120.0, 0)  # US win
        close("0700.HK", "HKD", 100.0, 80.0, 2)  # HK loss

        rows = client.get("/v1/stats/by-market").json()
        by = {r["market"]: r for r in rows}
        assert by["US"]["currency"] == "USD"
        assert by["US"]["stats"]["win_rate"] == 1.0
        assert by["HK"]["currency"] == "HKD"
        assert by["HK"]["stats"]["win_rate"] == 0.0
        # each market's P&L stays in its own currency, never summed
        assert by["US"]["stats"]["total_pnl"] > 0
        assert by["HK"]["stats"]["total_pnl"] < 0
