"""HTTP tests for the Phase 0.9 Portfolio API (accounts / holdings / events /
drafts / confirm / audit). Confirms the read+write surface the Desktop/Web
Portfolio UI consumes, and that the human-confirmation gate holds over HTTP.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from swing_trader.api import FinanceRuntime, create_app
from swing_trader.ledger import Ledger
from swing_trader.paper_broker import PaperBroker
from swing_trader.portfolio_draft import PortfolioDraftService
from swing_trader.portfolio_journal import DEFAULT_PAPER_ACCOUNT_ID, PortfolioJournal
from swing_trader.schemas import Position

NOW = datetime(2026, 7, 10, 15, 0, tzinfo=timezone.utc)


@pytest.fixture()
def client(tmp_path):
    url = f"sqlite:///{tmp_path / 'pf.db'}"
    journal = PortfolioJournal(url=url)
    runtime = FinanceRuntime(ledger=Ledger(url=url), clock=lambda: NOW)
    runtime.portfolio = journal
    runtime.portfolio_drafts = PortfolioDraftService(journal, clock=lambda: NOW)
    return TestClient(create_app(runtime))


def _make_account(client, **over):
    body = dict(name="IBKR US", market_scope="US", base_currency="USD", actor="gongqing")
    body.update(over)
    r = client.post("/v1/portfolio/accounts", json=body)
    assert r.status_code == 201, r.text
    return r.json()


def _draft_buy(client, account_id, **over):
    body = dict(
        account_id=account_id,
        event_type="buy",
        symbol="NVDA",
        market="US",
        currency="USD",
        qty=3,
        price=208.5,
        commission=1.0,
        occurred_at=NOW.isoformat(),
        created_by="hermes",
    )
    body.update(over)
    r = client.post("/v1/portfolio/drafts", json=body)
    assert r.status_code == 201, r.text
    return r.json()


class _RecordingTelegram:
    """Stands in for TelegramSurfaceAdapter: records pushed drafts. The
    ``boom`` flag makes push raise, proving draft-create survives a Telegram
    outage (the API push is best-effort)."""

    def __init__(self, boom=False):
        self.pushed: list[tuple[str, str]] = []
        self.boom = boom

    def push_draft_card(self, draft, account_label=""):
        if self.boom:
            raise RuntimeError("telegram down")
        self.pushed.append((draft.symbol, account_label))


class TestTelegramDraftPush:
    def _client_with_tg(self, tmp_path, tg):
        url = f"sqlite:///{tmp_path/'tg.db'}"
        journal = PortfolioJournal(url=url)
        runtime = FinanceRuntime(ledger=Ledger(url=url), clock=lambda: NOW)
        runtime.portfolio = journal
        runtime.portfolio_drafts = PortfolioDraftService(journal, clock=lambda: NOW)
        runtime.telegram = tg
        return TestClient(create_app(runtime))

    def test_create_pushes_card_with_account_label(self, tmp_path):
        tg = _RecordingTelegram()
        client = self._client_with_tg(tmp_path, tg)
        acct = _make_account(client, name="平安证券")
        _draft_buy(client, acct["id"], symbol="513310")
        assert tg.pushed == [("513310", "平安证券")]

    def test_create_survives_telegram_outage(self, tmp_path):
        tg = _RecordingTelegram(boom=True)
        client = self._client_with_tg(tmp_path, tg)
        acct = _make_account(client)
        r = client.post(
            "/v1/portfolio/drafts",
            json=dict(
                account_id=acct["id"],
                event_type="buy",
                symbol="NVDA",
                market="US",
                currency="USD",
                qty=3,
                price=208.5,
                occurred_at=NOW.isoformat(),
            ),
        )
        assert r.status_code == 201, r.text  # push failure never fails the draft


class TestVerifiedNameOverride:
    def test_verified_map_name_overrides_import_note_placeholder(self, client):
        acct = _make_account(client, name="平安证券", market_scope="CN", base_currency="CNY")
        # a buy recorded with the import's PLACEHOLDER note for a mapped symbol
        draft = _draft_buy(
            client,
            acct["id"],
            symbol="159518.SZ",
            market="CN",
            currency="CNY",
            qty=1200,
            price=1.1332,
            commission=0,
            note="平安证券 场内ETF；",
        )
        r = client.post(
            f"/v1/portfolio/drafts/{draft['id']}/action",
            json={
                "action": "confirm",
                "actor": "gongqing",
                "idempotency_key": "k-159518",
                "surface": "web",
            },
        )
        assert r.json()["ok"], r.text
        holds = client.get(f"/v1/portfolio/accounts/{acct['id']}/holdings").json()["holdings"]
        h = next(x for x in holds if x["symbol"] == "159518.SZ")
        # the curated/verified name wins over the placeholder note (cost basis
        # is untouched — the event is append-only).
        assert h["display_name"] == "标普油气ETF嘉实"
        assert h["avg_cost"] == 1.1332


class TestAccounts:
    def test_create_list_get(self, client):
        a = _make_account(client)
        assert a["market_scope"] == "US"
        assert a["environment"] == "live"
        assert client.get("/v1/portfolio/accounts").json()[0]["id"] == a["id"]
        assert client.get(f"/v1/portfolio/accounts/{a['id']}").json()["name"] == "IBKR US"

    def test_unknown_account_404(self, client):
        assert client.get("/v1/portfolio/accounts/ghost").status_code == 404

    def test_bad_market_422(self, client):
        r = client.post(
            "/v1/portfolio/accounts",
            json=dict(name="X", market_scope="ZZ", base_currency="USD", actor="g"),
        )
        assert r.status_code == 422

    def test_update_config(self, client):
        a = _make_account(client)
        r = client.post(
            f"/v1/portfolio/accounts/{a['id']}/update",
            json=dict(actor="g", include_in_risk=False, note="taxable"),
        )
        assert r.status_code == 200
        assert r.json()["include_in_risk"] is False

    def test_update_environment_and_filter(self, client):
        a = _make_account(client)
        r = client.post(
            f"/v1/portfolio/accounts/{a['id']}/update",
            json={"actor": "g", "environment": "paper"},
        )
        assert r.status_code == 200 and r.json()["environment"] == "paper"
        assert client.get("/v1/portfolio/accounts", params={"environment": "live"}).json() == []
        assert (
            client.get("/v1/portfolio/accounts", params={"environment": "paper"}).json()[0]["id"]
            == a["id"]
        )


class TestPaperAccountProjection:
    def test_default_account_projects_broker_without_journal_events(self, tmp_path):
        url = f"sqlite:///{tmp_path / 'paper.db'}"
        journal = PortfolioJournal(url=url)
        account = journal.ensure_default_paper_account()
        broker = PaperBroker(starting_cash=2_000)
        broker.restore_state(
            1_500,
            [Position(symbol="VST", qty=3, avg_px=165.33, mkt_px=170.0)],
            [],
        )
        runtime = FinanceRuntime(
            ledger=Ledger(url=url),
            broker=broker,
            clock=lambda: NOW,
        )
        runtime.portfolio = journal
        runtime.portfolio_drafts = PortfolioDraftService(journal, clock=lambda: NOW)
        paper_client = TestClient(create_app(runtime))

        rows = paper_client.get("/v1/portfolio/accounts", params={"environment": "paper"}).json()
        assert rows == [account.model_dump(mode="json")]
        assert rows[0]["id"] == DEFAULT_PAPER_ACCOUNT_ID
        holdings = paper_client.get(
            f"/v1/portfolio/accounts/{DEFAULT_PAPER_ACCOUNT_ID}/holdings"
        ).json()
        assert holdings["holdings"][0]["symbol"] == "VST"
        assert holdings["holdings"][0]["qty"] == 3
        assert holdings["holdings"][0]["avg_cost"] == 165.33
        assert holdings["cash"] == [{"currency": "USD", "amount": 1500.0, "known": True}]
        assert holdings["n_events"] == 0
        assert (
            paper_client.get(f"/v1/portfolio/accounts/{DEFAULT_PAPER_ACCOUNT_ID}/events").json()
            == []
        )

        valuation = paper_client.get(
            f"/v1/portfolio/accounts/{DEFAULT_PAPER_ACCOUNT_ID}/valuation"
        ).json()
        assert valuation["holdings"][0]["price"] == 170.0
        # No live feed on this runtime → the broker's last-close mark is honestly
        # labelled "close", not "live"/now (Loop.md §5.9 freshness).
        assert valuation["holdings"][0]["price_source"] == "close"

    def test_default_account_preserves_each_execution_currency(self, tmp_path):
        url = f"sqlite:///{tmp_path / 'paper-multi.db'}"
        journal = PortfolioJournal(url=url)
        journal.ensure_default_paper_account()
        broker = PaperBroker(
            starting_cash_by_currency={"USD": 2_000, "CNY": 100_000}
        )
        broker.restore_state(
            {"USD": 1_500, "CNY": 90_000},
            [
                Position(symbol="VST", currency="USD", qty=3, avg_px=165),
                Position(
                    symbol="510300.SS", currency="CNY", qty=1_000, avg_px=4.2
                ),
            ],
            [],
        )
        runtime = FinanceRuntime(ledger=Ledger(url=url), broker=broker, clock=lambda: NOW)
        runtime.portfolio = journal
        paper_client = TestClient(create_app(runtime))

        holdings = paper_client.get(
            f"/v1/portfolio/accounts/{DEFAULT_PAPER_ACCOUNT_ID}/holdings"
        ).json()
        assert {row["symbol"]: row["currency"] for row in holdings["holdings"]} == {
            "VST": "USD",
            "510300.SS": "CNY",
        }
        assert holdings["cash"] == [
            {"currency": "CNY", "amount": 90_000.0, "known": True},
            {"currency": "USD", "amount": 1_500.0, "known": True},
        ]

    def test_system_paper_account_cannot_be_reclassified_live(self, tmp_path):
        url = f"sqlite:///{tmp_path / 'fixed-paper.db'}"
        journal = PortfolioJournal(url=url)
        journal.ensure_default_paper_account()
        runtime = FinanceRuntime(ledger=Ledger(url=url), broker=PaperBroker())
        runtime.portfolio = journal
        paper_client = TestClient(create_app(runtime))
        response = paper_client.post(
            f"/v1/portfolio/accounts/{DEFAULT_PAPER_ACCOUNT_ID}/update",
            json={"actor": "g", "environment": "live"},
        )
        assert response.status_code == 422
        assert journal.get_account(DEFAULT_PAPER_ACCOUNT_ID).environment.value == "paper"


class TestDraftsAndHoldings:
    def test_confirm_flow_updates_holdings(self, client):
        a = _make_account(client)
        d = _draft_buy(client, a["id"])
        assert d["missing"] == []
        r = client.post(
            f"/v1/portfolio/drafts/{d['id']}/action",
            json=dict(action="confirm", actor="gongqing", idempotency_key="k1"),
            headers={"X-Finance-Surface": "web"},
        )
        assert r.status_code == 200 and r.json()["ok"] is True
        h = client.get(f"/v1/portfolio/accounts/{a['id']}/holdings").json()
        assert h["holdings"][0]["symbol"] == "NVDA" and h["holdings"][0]["qty"] == 3.0
        # event recorded + audited
        assert client.get(f"/v1/portfolio/accounts/{a['id']}/events").json()[0]["symbol"] == "NVDA"
        assert any(
            e["action"] == "confirm"
            for e in client.get("/v1/portfolio/audit", params={"account_id": a["id"]}).json()
        )

    def test_system_surface_cannot_confirm_403(self, client):
        a = _make_account(client)
        d = _draft_buy(client, a["id"])
        r = client.post(
            f"/v1/portfolio/drafts/{d['id']}/action",
            json=dict(action="confirm", actor="gongqing", idempotency_key="k"),
            headers={"X-Finance-Surface": "system"},
        )
        assert r.status_code == 403

    def test_llm_actor_cannot_confirm_403(self, client):
        a = _make_account(client)
        d = _draft_buy(client, a["id"])
        r = client.post(
            f"/v1/portfolio/drafts/{d['id']}/action",
            json=dict(action="confirm", actor="hermes", idempotency_key="k"),
            headers={"X-Finance-Surface": "web"},
        )
        assert r.status_code == 403

    def test_incomplete_draft_confirm_422(self, client):
        a = _make_account(client)
        d = _draft_buy(client, a["id"], qty=None)
        assert "quantity" in d["missing"]
        r = client.post(
            f"/v1/portfolio/drafts/{d['id']}/action",
            json=dict(action="confirm", actor="gongqing", idempotency_key="k"),
            headers={"X-Finance-Surface": "web"},
        )
        assert r.status_code == 422

    def test_edit_then_confirm(self, client):
        a = _make_account(client)
        d = _draft_buy(client, a["id"], qty=None)
        e = client.post(
            f"/v1/portfolio/drafts/{d['id']}/action",
            json=dict(action="edit", actor="gongqing", idempotency_key="e", edits={"qty": 5}),
            headers={"X-Finance-Surface": "web"},
        )
        assert e.status_code == 200 and e.json()["draft"]["qty"] == 5.0
        c = client.post(
            f"/v1/portfolio/drafts/{d['id']}/action",
            json=dict(action="confirm", actor="gongqing", idempotency_key="c"),
            headers={"X-Finance-Surface": "web"},
        )
        assert c.status_code == 200

    def test_unknown_draft_action_404(self, client):
        r = client.post(
            "/v1/portfolio/drafts/ghost/action",
            json=dict(action="confirm", actor="g", idempotency_key="k"),
        )
        assert r.status_code == 404

    def test_correct_draft_undoes_event(self, client):
        a = _make_account(client)
        d = _draft_buy(client, a["id"], qty=10)
        conf = client.post(
            f"/v1/portfolio/drafts/{d['id']}/action",
            json=dict(action="confirm", actor="gongqing", idempotency_key="s"),
            headers={"X-Finance-Surface": "web"},
        ).json()
        eid = conf["event"]["id"]
        # draft the undo (append-only "delete")
        corr = client.post(
            f"/v1/portfolio/accounts/{a['id']}/correct-draft", params={"event_id": eid}
        ).json()
        assert corr["event_type"] == "correction" and corr["reverses_event_id"] == eid
        client.post(
            f"/v1/portfolio/drafts/{corr['id']}/action",
            json=dict(action="confirm", actor="gongqing", idempotency_key="undo"),
            headers={"X-Finance-Surface": "web"},
        )
        h = client.get(f"/v1/portfolio/accounts/{a['id']}/holdings").json()
        assert h["holdings"] == []  # undone; history preserved (2 events)
        assert len(client.get(f"/v1/portfolio/accounts/{a['id']}/events").json()) == 2

    def test_correct_unknown_event_404(self, client):
        a = _make_account(client)
        r = client.post(
            f"/v1/portfolio/accounts/{a['id']}/correct-draft", params={"event_id": "ghost"}
        )
        assert r.status_code == 404

    def test_close_draft_derives_qty(self, client):
        a = _make_account(client)
        d = _draft_buy(client, a["id"], qty=10)
        client.post(
            f"/v1/portfolio/drafts/{d['id']}/action",
            json=dict(action="confirm", actor="gongqing", idempotency_key="seed"),
            headers={"X-Finance-Surface": "web"},
        )
        r = client.post(f"/v1/portfolio/accounts/{a['id']}/close-draft", params={"symbol": "NVDA"})
        assert r.status_code == 201
        body = r.json()
        assert body["event_type"] == "sell" and body["qty"] == 10.0
        assert any("completed trade" in x for x in body["ambiguities"])


class TestAggregateReconcileImport:
    def _seed(self, client, qty=10):
        a = _make_account(client)
        d = _draft_buy(client, a["id"], qty=qty)
        client.post(f"/v1/portfolio/drafts/{d['id']}/action",
                    json=dict(action="confirm", actor="gongqing", idempotency_key="s"),
                    headers={"X-Finance-Surface": "web"})
        return a

    def test_aggregate(self, client):
        self._seed(client)
        agg = client.get("/v1/portfolio/aggregate").json()
        assert agg["holdings"][0]["symbol"] == "NVDA" and agg["holdings"][0]["qty"] == 10.0

    def test_reconcile_manual_authoritative(self, client):
        a = self._seed(client)
        r = client.get(f"/v1/portfolio/accounts/{a['id']}/reconcile").json()
        assert r["ok"] is True and r["authority"] == "manual"

    def test_import_preview_then_commit(self, client):
        a = _make_account(client)
        csv = ("date,event_type,symbol,market,currency,qty,price\n"
               "2026-07-01,buy,NVDA,US,USD,10,100\n"
               "2026-07-02,buy,AMD,US,USD,5,50\n")
        pv = client.post(f"/v1/portfolio/accounts/{a['id']}/import/preview",
                         json={"csv": csv}).json()
        assert pv["n_valid"] == 2 and pv["committable"] is True
        c = client.post(f"/v1/portfolio/accounts/{a['id']}/import/commit",
                        json={"csv": csv, "actor": "gongqing"},
                        headers={"X-Finance-Surface": "web"}).json()
        assert c["n_committed"] == 2
        syms = {h["symbol"] for h in
                client.get(f"/v1/portfolio/accounts/{a['id']}/holdings").json()["holdings"]}
        assert syms == {"NVDA", "AMD"}

    def test_import_commit_requires_actor(self, client):
        a = _make_account(client)
        r = client.post(f"/v1/portfolio/accounts/{a['id']}/import/commit",
                        json={"csv": "date,event_type\n2026-07-01,fee\n"})
        assert r.status_code == 422


class TestSessionTrigger:
    def _client(self, tmp_path, attach=True):
        ledger = Ledger(url=f"sqlite:///{tmp_path/'sess.db'}")
        runtime = FinanceRuntime(ledger=ledger, clock=lambda: NOW)
        calls = {}
        if attach:
            runtime.run_session = lambda **kw: {"risk_approved": 2, "pushed": 2,
                                                "cutoff_et": "16:00", **calls}
            runtime.finalize_session = lambda: {"approved": 1, "expired": 1}
        return TestClient(create_app(runtime))

    def test_run_requires_human_surface(self, tmp_path):
        client = self._client(tmp_path)
        # system surface refused
        r = client.post("/v1/session/run", json={"actor": "gongqing"},
                        headers={"X-Finance-Surface": "system"})
        assert r.status_code == 403
        # LLM actor refused
        r = client.post("/v1/session/run", json={"actor": "hermes"},
                        headers={"X-Finance-Surface": "web"})
        assert r.status_code == 403

    def test_run_by_human_ok(self, tmp_path):
        import time as _t
        client = self._client(tmp_path)
        r = client.post("/v1/session/run", json={"actor": "gongqing", "window_minutes": 90},
                        headers={"X-Finance-Surface": "web"})
        # background run: returns immediately with "started"
        assert r.status_code == 200 and r.json()["status"] == "started"
        assert r.json()["market"] == "us"
        # poll status until the (instant) background run stores its summary
        for _ in range(50):
            s = client.get("/v1/session/status?market=us").json()
            if not s["running"] and s["summary"] is not None:
                break
            _t.sleep(0.02)
        assert s["summary"]["risk_approved"] == 2
        assert s["summary"]["actor"] == "gongqing"

    def test_finalize_by_human_ok(self, tmp_path):
        client = self._client(tmp_path)
        r = client.post("/v1/session/finalize", json={"actor": "gongqing"},
                        headers={"X-Finance-Surface": "web"})
        assert r.status_code == 200 and r.json()["approved"] == 1

    def test_503_when_loop_not_attached(self, tmp_path):
        client = self._client(tmp_path, attach=False)
        r = client.post("/v1/session/run", json={"actor": "gongqing"},
                        headers={"X-Finance-Surface": "web"})
        assert r.status_code == 503


class TestUnavailable:
    def test_503_without_portfolio(self, tmp_path):
        client = TestClient(create_app(FinanceRuntime(ledger=Ledger(
            url=f"sqlite:///{tmp_path/'x.db'}"))))
        assert client.get("/v1/portfolio/accounts").status_code == 503
        assert client.get("/v1/portfolio/drafts").status_code == 503
        assert client.get("/v1/portfolio/aggregate").status_code == 503
