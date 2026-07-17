"""Unified Beijing-time US/HK/CN/KR brief delivery (no network)."""

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
    assert "主模型简报生成失败" in runtime.latest_brief["uncertainty"][-1]
    assert "没有使用模板或弱模型替代" in sent[0]


def test_failed_synthesis_preserves_last_good_narrative(tmp_path):
    """Regression: a flaky completion must not blank a market that already had
    prose. The prior edition's complete brief (evidence+narrative) is kept."""
    runtime = _runtime(tmp_path)
    # First edition succeeds → US gets a real narrative.
    BriefCycleCoordinator(
        runtime, _Writer(), notify=[].append, markets=("us",)
    ).run_cycle("morning")
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
    # last-good prose is retained rather than wiped to None
    assert runtime.latest_brief["narrative"] is not None
    assert runtime.latest_brief["narrative"]["headline"] == good_headline
    assert runtime.brief_store.get_latest("us")["narrative"] is not None
    assert "已保留上一版完整简报" in sent[0]


def test_latest_due_slot_uses_beijing_wall_clock():
    assert latest_due_brief_slot(NOW) == ("evening", NOW)
    before_morning = datetime(2026, 7, 16, 0, 30, tzinfo=timezone.utc)
    edition, due = latest_due_brief_slot(before_morning)
    assert edition == "evening"
    assert due.astimezone(timezone.utc) == datetime(
        2026, 7, 15, 13, 0, tzinfo=timezone.utc
    )
