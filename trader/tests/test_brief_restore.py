"""Startup recovery policy for durable research briefs."""

from datetime import datetime, timezone

from swing_trader.__main__ import _markets_missing_today, _restore_latest_briefs
from swing_trader.api import FinanceRuntime
from swing_trader.brief_store import BriefStore
from swing_trader.ledger import Ledger


NOW = datetime(2026, 7, 14, 12, 0, tzinfo=timezone.utc)


def _runtime(tmp_path):
    ledger = Ledger(url=f"sqlite:///{tmp_path/'ledger.db'}")
    runtime = FinanceRuntime(ledger=ledger, clock=lambda: NOW)
    runtime.brief_store = BriefStore(url=f"sqlite:///{tmp_path/'briefs.db'}")
    return runtime


def test_restore_hydrates_all_runtime_slots(tmp_path):
    runtime = _runtime(tmp_path)
    for market in ("us", "cn", "kr"):
        runtime.brief_store.save(market, {
            "as_of": NOW.isoformat(),
            "trading_date": "2026-07-14",
            "marker": market,
        })

    assert _restore_latest_briefs(runtime) == ["us", "cn", "kr"]
    assert runtime.latest_brief["marker"] == "us"
    assert runtime.latest_briefs["cn"]["marker"] == "cn"
    assert runtime.latest_brief_cn["marker"] == "cn"
    assert runtime.latest_briefs["kr"]["marker"] == "kr"


def test_missing_today_uses_each_markets_local_date(tmp_path):
    runtime = _runtime(tmp_path)
    runtime.latest_brief = {"trading_date": "2026-07-14"}
    runtime.latest_briefs["cn"] = {"trading_date": "2026-07-14"}
    runtime.latest_briefs["kr"] = {"trading_date": "2026-07-13"}

    missing = _markets_missing_today(runtime, {
        "us": "America/New_York",
        "cn": "Asia/Shanghai",
        "kr": "Asia/Seoul",
    })

    # At 12:00 UTC it is July 14 in all three zones; only KR is stale.
    assert missing == ["kr"]
