"""Evidence-bound AI brief writer tests (no network)."""

from datetime import datetime, timezone

from swing_trader.brief import FreshnessInfo, ResearchBrief
from swing_trader.brief_writer import ResearchBriefWriter
from swing_trader.llm import LLMSettings
from swing_trader.schemas import Mode


SETTINGS = LLMSettings(base_url="https://invalid", model="brief-test", api_key="secret")
NOW = datetime(2026, 7, 15, 3, 30, tzinfo=timezone.utc)


def _brief() -> ResearchBrief:
    return ResearchBrief(
        as_of=NOW,
        trading_date="2026-07-15",
        mode=Mode.PAPER,
        freshness=FreshnessInfo(warnings=["news missing"]),
        uncertainty=["earnings evidence missing"],
    )


def _reply() -> str:
    return """<think>ground the answer</think>
    {
      "headline": "流动性线索不足，暂以验证催化为主",
      "summary": "市场与新闻证据不完整，当前不能形成高置信度方向判断。\\n\\n重点不是重复缺失项，而是确认缺口会怎样改变主题判断。",
      "sections": [
        {"title": "市场结构", "analysis": "当前没有可用的市场状态，无法判断广度是否支持指数方向。"},
        {"title": "主题与标的", "analysis": "没有形成可交叉验证的主题与异动组合。"},
        {"title": "催化与风险", "analysis": "新闻和财报证据缺失，使催化判断的可信度明显下降。"},
        {"title": "组合含义", "analysis": "在证据补齐前，当前信息不足以支持扩大方向性暴露。"}
      ],
      "watch_next": ["补齐市场广度", "核实财报日历"]
    }"""


def test_writer_returns_valid_market_specific_narrative_and_caches() -> None:
    calls: list[str] = []

    def complete(_settings, _system, prompt):
        calls.append(prompt)
        return _reply()

    writer = ResearchBriefWriter(SETTINGS, complete=complete)
    first = writer.write(_brief(), market_id="CN", market_label="Mainland China", language="zh-CN")
    second = writer.write(_brief(), market_id="CN", market_label="Mainland China", language="zh-CN")

    assert first is not None and second == first
    assert first.market == "CN" and first.model == "brief-test"
    assert len(first.sections) == 4 and len(first.watch_next) == 2
    assert len(calls) == 1
    assert "A-share brief" in calls[0]
    assert '"id": "CN"' in calls[0]
    assert '"freshness"' in calls[0] and '"uncertainty"' in calls[0]
    assert SETTINGS.api_key not in calls[0]


def test_writer_failure_returns_none_instead_of_template_prose() -> None:
    writer = ResearchBriefWriter(SETTINGS, complete=lambda _settings, _system, _prompt: "not json")
    assert writer.write(_brief(), market_id="US", market_label="United States") is None


def test_writer_strictly_retries_a_truncated_structured_response() -> None:
    replies = iter(['{"headline":"truncated"', _reply()])
    calls: list[str] = []

    def complete(_settings, _system, prompt):
        calls.append(prompt)
        return next(replies)

    narrative = ResearchBriefWriter(SETTINGS, complete=complete).write(
        _brief(), market_id="HK", market_label="Hong Kong", language="zh-CN"
    )

    assert narrative is not None and len(narrative.sections) == 4
    assert len(calls) == 2 and calls[1].startswith("STRICT JSON RETRY")
