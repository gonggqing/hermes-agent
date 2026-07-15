from __future__ import annotations

from types import SimpleNamespace

from fastapi.testclient import TestClient

from swing_trader.api import FinanceRuntime, create_app
from swing_trader.ledger import Ledger
from swing_trader.research_watchlists import ResearchWatchlistStore


def _client(tmp_path):
    url = f"sqlite:///{tmp_path / 'finance.db'}"
    runtime = FinanceRuntime(ledger=Ledger(url=url))
    runtime.research_watchlists = ResearchWatchlistStore(url=url)
    return TestClient(create_app(runtime)), runtime


def test_group_and_member_crud_is_durable(tmp_path):
    client, _ = _client(tmp_path)

    created = client.post("/v1/research/watchlists", json={"name": "  AI supply chain  "})
    assert created.status_code == 201
    group = created.json()
    assert group["name"] == "AI supply chain"
    assert group["members"] == []

    added = client.post(
        f"/v1/research/watchlists/{group['id']}/members",
        json={
            "symbol": "nvda",
            "display_name": "NVIDIA",
            "market": "us",
            "exchange": "NASDAQ",
            "currency": "USD",
            "security_type": "stock",
        },
    )
    assert added.status_code == 201
    assert added.json()["members"][0]["symbol"] == "NVDA"

    # Duplicate add is an idempotent no-op, not a second row.
    replay = client.post(
        f"/v1/research/watchlists/{group['id']}/members",
        json={"symbol": "NVDA", "display_name": "NVIDIA"},
    )
    assert len(replay.json()["members"]) == 1

    renamed = client.patch(
        f"/v1/research/watchlists/{group['id']}", json={"name": "AI infrastructure"}
    )
    assert renamed.json()["name"] == "AI infrastructure"

    reopened = ResearchWatchlistStore(url=f"sqlite:///{tmp_path / 'finance.db'}")
    assert reopened.list_groups()[0].members[0].symbol == "NVDA"

    removed = client.delete(f"/v1/research/watchlists/{group['id']}/members/NVDA")
    assert removed.status_code == 200
    assert removed.json()["members"] == []
    assert client.delete(f"/v1/research/watchlists/{group['id']}").json() == {"ok": True}
    assert client.get("/v1/research/watchlists").json() == []


def test_unconfigured_store_fails_closed(tmp_path):
    runtime = FinanceRuntime(ledger=Ledger(url=f"sqlite:///{tmp_path / 'ledger.db'}"))
    client = TestClient(create_app(runtime))
    assert client.get("/v1/research/watchlists").status_code == 503


def test_unknown_group_and_invalid_name_are_rejected(tmp_path):
    client, _ = _client(tmp_path)
    assert client.patch("/v1/research/watchlists/missing", json={"name": "x"}).status_code == 404
    assert client.post("/v1/research/watchlists", json={"name": "   "}).status_code == 422


def test_personal_groups_do_not_change_trading_watchlist(tmp_path):
    client, _ = _client(tmp_path)
    before = client.get("/v1/watchlist").json()
    group = client.post("/v1/research/watchlists", json={"name": "Mine"}).json()
    client.post(
        f"/v1/research/watchlists/{group['id']}/members",
        json={"symbol": "NEWCO", "display_name": "New Company"},
    )
    assert client.get("/v1/watchlist").json() == before
    assert all(item["symbol"] != "NEWCO" for item in before)


def test_holding_recommendations_resolve_missing_fund_names(tmp_path):
    client, runtime = _client(tmp_path)

    class Portfolio:
        def aggregate(self):
            return SimpleNamespace(
                holdings=[
                    SimpleNamespace(
                        symbol="017470",
                        qty=100,
                        market=SimpleNamespace(value="CN"),
                        currency="CNY",
                    )
                ]
            )

        def get_events(self, _account_id=None):
            return []

    class Navs:
        def get_nav(self, symbol):
            return SimpleNamespace(symbol=symbol, name="嘉实上证科创板芯片ETF联接C")

    runtime.portfolio = Portfolio()
    runtime.nav_provider = Navs()
    response = client.get("/v1/research/watchlists/recommendations/holdings")
    assert response.status_code == 200
    assert response.json()[0]["display_name"] == "嘉实上证科创板芯片ETF联接C"


def test_legacy_code_only_watchlist_member_is_hydrated_on_read(tmp_path):
    client, runtime = _client(tmp_path)
    group = client.post("/v1/research/watchlists", json={"name": "Funds"}).json()
    client.post(
        f"/v1/research/watchlists/{group['id']}/members",
        json={"symbol": "005698", "display_name": "005698"},
    )

    class Navs:
        def get_nav(self, symbol):
            return SimpleNamespace(symbol=symbol, name="华夏全球科技先锋混合(QDII)A")

    runtime.nav_provider = Navs()
    member = client.get("/v1/research/watchlists").json()[0]["members"][0]
    assert member["display_name"] == "华夏全球科技先锋混合(QDII)A"
