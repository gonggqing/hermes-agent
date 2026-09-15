"""Unified Beijing-time US/HK/CN/KR brief delivery (no network)."""

import threading
from datetime import datetime, timezone

from swing_trader.api import FinanceRuntime
from swing_trader.brief import (
    FreshnessInfo,
    NarrativeSection,
    ResearchBrief,
    ResearchNarrative,
)
from swing_trader.brief_cycle import BriefCycleCoordinator, latest_due_brief_slot
from swing_trader.brief_store import BriefStore
from swing_trader.ledger import Ledger
from swing_trader.schemas import Mode

NOW = datetime(2026, 7, 16, 13, 0, tzinfo=timezone.utc)  # Beijing 21:00


def _brief(market: str) -> dict:
    return ResearchBrief(
        as_of=NOW,
        trading_date="2026-07-16",
        mode=Mode.PAPER,
        freshness=FreshnessInfo(warnings=[]),
        uncertainty=[f"{market} evidence packet"],
    ).model_dump(mode="json")


class _Writer:
    def __init__(self):
        self.calls: list[str] = []

    def write(self, source, *, market_id, market_label, language):
        self.calls.append(market_id.lower())
        return ResearchNarrative(
            generated_at=source.as_of,
            market=market_id,
            language=language,
            model="primary-model",
            headline=f"{market_label} 有效信息摘要",
            summary="综合证据形成的投资研究结论。",
            sections=[
                NarrativeSection(title="市场", analysis="市场结构需要保持选择性。"),
                NarrativeSection(title="主题", analysis="主题强弱存在明显分化。"),
                NarrativeSection(title="标的", analysis="标的仍需结合估值验证。"),
                NarrativeSection(title="风险", analysis="当前主要风险来自证据变化。"),
            ],
            watch_next=["跟踪下一批有效证据"],
            prompt_version="test-v2",
            evidence_hash="hash",
        )


def _runtime(tmp_path):
    ledger = Ledger(url=f"sqlite:///{tmp_path / 'ledger.db'}")
    runtime = FinanceRuntime(ledger=ledger, clock=lambda: NOW)
    runtime.brief_store = BriefStore(url=f"sqlite:///{tmp_path / 'brief.db'}")
    for market in ("us", "cn", "hk", "kr"):

        def refresh(key=market):
            payload = _brief(key)
            if key == "us":
                runtime.latest_brief = payload
            else:
                runtime.latest_briefs[key] = payload

        runtime.run_research[market] = refresh
    return runtime


def test_evening_cycle_uses_same_pipeline_for_all_markets(tmp_path):
    runtime = _runtime(tmp_path)
    writer = _Writer()
    sent: list[str] = []
    result = BriefCycleCoordinator(runtime, writer, notify=sent.append).run_cycle("evening")

    assert result["completed"] == ["us", "hk", "cn", "kr"]
    assert writer.calls == ["us", "hk", "cn", "kr"]
    assert len(sent) == 4
    for market in ("us", "cn", "hk", "kr"):
        payload = runtime.latest_brief if market == "us" else runtime.latest_briefs[market]
        assert payload["narrative"]["model"] == "primary-model"
        assert payload["narrative"]["edition"] == "evening"
        assert payload["publication"]["status"] == "complete"
        assert payload["publication"]["edition_id"] == f"2026-07-16:evening:{market}"
        assert runtime.brief_store.get_latest(market)["narrative"] is not None


def test_failed_primary_synthesis_is_visible_and_never_template_replaced(tmp_path):
    runtime = _runtime(tmp_path)

    class _FailingWriter:
        def write(self, *_args, **_kwargs):
            return None

    sent: list[str] = []
    result = BriefCycleCoordinator(
        runtime,
        _FailingWriter(),
        notify=sent.append,
        markets=("us",),
    ).run_cycle("morning")

    assert result["failed"] == ["us"]
    assert runtime.latest_brief["narrative"] is None
    assert runtime.latest_brief["publication"]["status"] == "narrative_failed"
    assert "主模型简报生成失败" in runtime.latest_brief["uncertainty"][-1]
    assert "旧版文字不会与本版数据混合" in sent[0]


def test_failed_synthesis_keeps_new_evidence_without_old_narrative(tmp_path):
    """A flaky completion cannot attach the prior edition to new evidence."""
    runtime = _runtime(tmp_path)
    # First edition succeeds → US gets a real narrative.
    BriefCycleCoordinator(runtime, _Writer(), notify=[].append, markets=("us",)).run_cycle(
        "morning"
    )
    good_headline = runtime.latest_brief["narrative"]["headline"]
    assert good_headline

    class _FailingWriter:
        def write(self, *_args, **_kwargs):
            return None

    sent: list[str] = []
    result = BriefCycleCoordinator(
        runtime, _FailingWriter(), notify=sent.append, markets=("us",)
    ).run_cycle("evening")

    assert result["failed"] == ["us"]
    assert runtime.latest_brief["narrative"] is None
    assert runtime.latest_brief["publication"]["status"] == "narrative_failed"
    assert runtime.latest_brief["publication"]["edition"] == "evening"
    assert good_headline not in str(runtime.latest_brief)
    assert runtime.brief_store.get_latest("us")["narrative"] is None
    assert "旧版文字不会与本版数据混合" in sent[0]


def test_fresh_same_edition_market_is_skipped_on_restart(tmp_path):
    """A market already holding THIS edition's brief (<4h old) is not
    regenerated when catch-up re-runs the cycle for another stale market —
    stops the duplicate reports a container restart otherwise produced."""
    runtime = _runtime(tmp_path)
    writer = _Writer()
    # First run populates all four with an 'evening' narrative at NOW.
    BriefCycleCoordinator(runtime, writer, notify=[].append).run_cycle("evening")

    # Simulate a restart 1h later: CN went missing, the rest are still fresh.
    runtime.clock = lambda: datetime(2026, 7, 16, 14, 0, tzinfo=timezone.utc)
    runtime.latest_briefs["cn"] = _brief("cn")  # narrative-less again
    # This scenario represents an actually missing publication, not merely an
    # intraday runtime overwrite of a still-durable edition.
    runtime.brief_store = BriefStore(url=f"sqlite:///{tmp_path / 'empty-brief.db'}")
    writer2 = _Writer()
    sent: list[str] = []
    result = BriefCycleCoordinator(runtime, writer2, notify=sent.append).run_cycle("evening")

    # only CN regenerated; us/hk/kr skipped (fresh) → no duplicate notify
    assert writer2.calls == ["cn"]
    assert set(result["skipped"]) == {"us", "hk", "kr"}
    assert result["completed"] == ["cn"]
    assert len(sent) == 1


def test_archived_publication_stays_fresh_after_intraday_runtime_overwrite(tmp_path):
    runtime = _runtime(tmp_path)
    BriefCycleCoordinator(runtime, _Writer(), markets=("us",)).run_cycle("evening")
    runtime.latest_brief = _brief("us")  # volatile monitor evidence has no publication
    writer = _Writer()

    result = BriefCycleCoordinator(runtime, writer, markets=("us",)).run_cycle("evening")

    assert result["skipped"] == ["us"]
    assert writer.calls == []


def test_late_prior_edition_does_not_satisfy_restart_catch_up(tmp_path):
    runtime = _runtime(tmp_path)
    coordinator = BriefCycleCoordinator(runtime, _Writer(), markets=("us",))
    coordinator.run_cycle("morning")
    # The prior morning cycle finishes after the following morning is due.
    # Its generated timestamp is recent, but its stable edition ID is stale.
    runtime.clock = lambda: datetime(2026, 7, 17, 2, 0, tzinfo=timezone.utc)
    runtime.latest_brief["narrative"]["generated_at"] = "2026-07-17T01:30:00Z"
    triggered: list[str] = []
    coordinator.trigger = lambda edition: not triggered.append(edition)

    assert coordinator.catch_up_if_due() is True
    assert triggered == ["morning"]


def test_force_bypasses_freshness_guard(tmp_path):
    runtime = _runtime(tmp_path)
    BriefCycleCoordinator(runtime, _Writer(), notify=[].append).run_cycle("evening")
    writer2 = _Writer()
    # force=True + only=('us',) → regenerate even though us is fresh
    result = BriefCycleCoordinator(runtime, writer2, notify=[].append).run_cycle(
        "evening", force=True, only=("us",)
    )
    assert writer2.calls == ["us"]
    assert result["completed"] == ["us"]


def test_stuck_market_times_out_and_does_not_block_following_market(tmp_path):
    runtime = _runtime(tmp_path)
    release = threading.Event()

    def stuck_refresh():
        release.wait()

    runtime.run_research["us"] = stuck_refresh
    sent: list[str] = []
    coordinator = BriefCycleCoordinator(
        runtime,
        _Writer(),
        notify=sent.append,
        markets=("us", "hk"),
        refresh_timeout_s=0.02,
    )
    try:
        result = coordinator.run_cycle("evening")
    finally:
        release.set()

    assert result["failed"] == ["us"]
    assert result["completed"] == ["hk"]
    assert any("已继续处理下一个市场" in message for message in sent)


def test_latest_due_slot_uses_beijing_wall_clock():
    assert latest_due_brief_slot(NOW) == ("evening", NOW)
    before_morning = datetime(2026, 7, 16, 0, 30, tzinfo=timezone.utc)
    edition, due = latest_due_brief_slot(before_morning)
    assert edition == "evening"
    assert due.astimezone(timezone.utc) == datetime(2026, 7, 15, 13, 0, tzinfo=timezone.utc)
