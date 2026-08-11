"""Startup recovery policy for durable research briefs."""

from datetime import datetime, timezone

from swing_trader.__main__ import (
    _backfill_brief_narratives,
    _markets_missing_today,
    _restore_latest_briefs,
)
from swing_trader.api import FinanceRuntime
from swing_trader.brief import NarrativeSection, ResearchNarrative, build_research_brief
from swing_trader.brief_store import BriefStore
from swing_trader.ledger import Ledger
from swing_trader.schemas import Mode


NOW = datetime(2026, 7, 14, 12, 0, tzinfo=timezone.utc)


def _runtime(tmp_path):
    ledger = Ledger(url=f"sqlite:///{tmp_path / 'ledger.db'}")
    runtime = FinanceRuntime(ledger=ledger, clock=lambda: NOW)
    runtime.brief_store = BriefStore(url=f"sqlite:///{tmp_path / 'briefs.db'}")
    return runtime


def test_restore_hydrates_all_runtime_slots(tmp_path):
    runtime = _runtime(tmp_path)
    for market in ("us", "cn", "hk", "kr"):
        runtime.brief_store.save(
            market,
            {
                "as_of": NOW.isoformat(),
                "trading_date": "2026-07-14",
                "marker": market,
            },
        )

    assert _restore_latest_briefs(runtime) == ["us", "hk", "cn", "kr"]
    assert runtime.latest_brief["marker"] == "us"
    assert runtime.latest_briefs["cn"]["marker"] == "cn"
    assert runtime.latest_brief_cn["marker"] == "cn"
    assert runtime.latest_briefs["kr"]["marker"] == "kr"
    assert runtime.latest_briefs["hk"]["marker"] == "hk"


def test_missing_today_uses_each_markets_local_date(tmp_path):
    runtime = _runtime(tmp_path)
    runtime.latest_brief = {"trading_date": "2026-07-14"}
    runtime.latest_briefs["cn"] = {"trading_date": "2026-07-14"}
    runtime.latest_briefs["kr"] = {"trading_date": "2026-07-13"}

    missing = _markets_missing_today(
        runtime,
        {
            "us": "America/New_York",
            "cn": "Asia/Shanghai",
            "kr": "Asia/Seoul",
        },
    )

    # At 12:00 UTC it is July 14 in all three zones; only KR is stale.
    assert missing == ["kr"]


def test_current_beijing_edition_avoids_restart_refresh_at_us_date_rollover(tmp_path):
    runtime = _runtime(tmp_path)
    # At NOW (20:00 Beijing / 08:00 ET), the due edition is Beijing 09:00.
    # Its US evidence can legitimately carry the prior US trading date.
    runtime.latest_brief = {
        "trading_date": "2026-07-13",
        "narrative": {"generated_at": "2026-07-14T02:00:00Z"},
    }

    missing = _markets_missing_today(runtime, {"us": "America/New_York"})

    assert missing == []


def test_backfill_adds_runtime_narrative_without_noncanonical_archive(tmp_path):
    runtime = _runtime(tmp_path)
    brief = build_research_brief(runtime.ledger, Mode.PAPER, now=NOW)
    runtime.latest_brief = brief.model_dump(mode="json")

    class FakeWriter:
        def write(self, source, **context):
            return ResearchNarrative(
                generated_at=source.as_of,
                market=context["market_id"],
                language=context["language"],
                model="brief-test",
                headline="市场信息需要重新形成一致证据",
                summary="这是基于已保存结构化研究数据生成的分析。",
                sections=[
                    NarrativeSection(title="市场", analysis="指数与广度需要合并判断。"),
                    NarrativeSection(title="主题", analysis="行业方向需要更多内部确认。"),
                    NarrativeSection(title="标的", analysis="异动本身不能替代投资逻辑。"),
                    NarrativeSection(title="风险", analysis="证据缺口限制了结论强度。"),
                ],
                watch_next=["等待下一轮市场数据"],
            )

    assert _backfill_brief_narratives(runtime, FakeWriter(), ["us"]) == ["us"]
    assert runtime.latest_brief["narrative"]["model"] == "brief-test"
    assert runtime.brief_store.get_latest("us") is None
    # The enriched in-process slot is stable and does not repeat the model call.
    assert _backfill_brief_narratives(runtime, FakeWriter(), ["us"]) == []
