"""Evidence-bound AI brief writer tests (no network)."""

import json
from datetime import datetime, timezone

from swing_trader.brief import (
    FreshnessInfo,
    HoldingView,
    NewsDigestItem,
    NewsSection,
    ResearchBrief,
    SignalView,
)
from swing_trader.brief_writer import ResearchBriefWriter
from swing_trader.discovery import (
    DiscoveryCandidate,
    DiscoveryEvidence,
    DiscoveryFeatures,
    DiscoveryPool,
    EvidenceKind,
)
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
        holdings=[
            HoldingView(
                symbol="TEST",
                display_name="测试标的",
                currency="CNY",
                qty=100,
                avg_px=10,
                environment="live",
                account_names=["test account"],
                source="portfolio_journal",
            )
        ],
        uncertainty=["earnings evidence missing"],
    )


def _reply() -> str:
    return """<think>ground the answer</think>
    {
      "headline": "流动性线索不足，暂以验证催化为主",
      "summary": "市场与新闻证据不完整，当前不能形成高置信度方向判断。\\n\\n重点不是重复缺失项，而是确认缺口会怎样改变主题判断。",
      "change_summary": ["没有新增催化，原判断保持观察"],
      "action_views": [
        {
          "symbol": "TEST",
          "display_name": "测试标的",
          "stance": "watch",
          "thesis_state": "unchanged",
          "confidence": 0.55,
          "horizon_sessions": 5,
          "what_changed": "没有新增基本面证据",
          "rationale": "技术趋势尚可，但缺少非技术催化支持",
          "invalidation": "跌破已提供的趋势基准",
          "evidence_refs": ["technical"]
        }
      ],
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
    assert first.change_summary == ["没有新增催化，原判断保持观察"]
    assert first.action_views[0].stance == "watch"
    assert first.action_views[0].display_name == "测试标的"
    assert len(calls) == 1
    assert "A-share brief" in calls[0]
    assert '"id": "CN"' in calls[0]
    assert '"freshness"' in calls[0] and '"uncertainty"' in calls[0]
    assert '"must_not_reconcile_together": true' in calls[0]
    assert "read-only live accounts" in calls[0]
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


def test_writer_safely_caps_oversized_model_arrays_before_validation() -> None:
    payload = json.loads(_reply().split("</think>", 1)[1])
    payload["change_summary"] = [f"change-{index}" for index in range(7)]
    payload["sections"] = [*payload["sections"], *payload["sections"]]
    payload["watch_next"] = [f"watch-{index}" for index in range(8)]
    payload["action_views"] = payload["action_views"] * 13

    narrative = ResearchBriefWriter(
        SETTINGS,
        complete=lambda _settings, _system, _prompt: json.dumps(payload),
    ).write(_brief(), market_id="CN", market_label="Mainland China")

    assert narrative is not None
    assert len(narrative.change_summary) == 6
    assert len(narrative.sections) == 7
    assert len(narrative.watch_next) == 6
    assert len(narrative.action_views) == 12


def test_writer_supplies_prior_recurrence_and_signal_delta_to_primary_model() -> None:
    prompts: list[str] = []
    current = _brief().model_copy(
        update={
            "news": NewsSection(
                items=[
                    NewsDigestItem(
                        headline="Repeated catalyst",
                        source="Reuters",
                        url="https://example.test/event",
                        sentiment=0.4,
                        symbol="TEST",
                        published_at=NOW,
                        age_hours=0.0,
                        source_quality="major",
                    )
                ]
            ),
            "signals_today": [
                SignalView(
                    symbol="TEST",
                    direction="long",
                    confidence=0.7,
                    source_agent="technical",
                    thesis="trend improved",
                )
            ],
        }
    )
    history = [
        {
            "trading_date": "2026-07-14",
            "news": {
                "items": [
                    {
                        "headline": "Repeated catalyst",
                        "url": "https://example.test/event",
                    }
                ]
            },
            "signals_today": [
                {
                    "symbol": "TEST",
                    "source_agent": "technical",
                    "direction": "neutral",
                    "confidence": 0.5,
                    "thesis": "no trend",
                }
            ],
            "narrative": {
                "edition": "evening",
                "headline": "prior view",
                "summary": "prior summary",
                "action_views": [],
                "claims": [],
            },
        }
    ]

    def complete(_settings, _system, prompt):
        prompts.append(prompt)
        return _reply()

    writer = ResearchBriefWriter(
        SETTINGS,
        complete=complete,
        history_loader=lambda _market, _before, _limit: history,
    )
    assert writer.write(current, market_id="CN", market_label="Mainland China")

    packet = prompts[0]
    assert '"novel_vs_prior_briefs": false' in packet
    assert '"seen_in_prior_briefs": 1' in packet
    assert '"previous_direction": "neutral"' in packet
    assert '"current_direction": "long"' in packet


def test_writer_rejects_an_invented_action_symbol() -> None:
    bad = _reply().replace('"symbol": "TEST"', '"symbol": "INVENTED"')
    writer = ResearchBriefWriter(
        SETTINGS,
        complete=lambda _settings, _system, _prompt: bad,
    )

    assert writer.write(_brief(), market_id="CN", market_label="Mainland China") is None


def test_writer_rejects_technical_only_buy_guidance() -> None:
    bad = _reply().replace('"stance": "watch"', '"stance": "buy_on_confirmation"')
    writer = ResearchBriefWriter(
        SETTINGS,
        complete=lambda _settings, _system, _prompt: bad,
    )

    assert writer.write(_brief(), market_id="CN", market_label="Mainland China") is None


def test_writer_rejects_market_screen_only_discovery_buy_guidance() -> None:
    evidence = DiscoveryEvidence(
        kind=EvidenceKind.MARKET_SCREEN,
        source="live screen",
        url="https://example.test/screen",
        observed_at=NOW,
        summary="turnover and trend screen",
        confidence=0.7,
    )
    candidate = DiscoveryCandidate(
        symbol="SCREEN.SS",
        display_name="筛选标的",
        market="CN",
        exchange="SSE",
        currency="CNY",
        theme="A股/旧分类",
        theme_verified=False,
        component="旧分类",
        relationship="仅为动态行情抽样",
        score=72,
        rank=1,
        reasons=["成交活跃"],
        features=DiscoveryFeatures(
            adv20=1_000_000,
            volume_ratio=2,
            trend_20d_pct=8,
            relative_strength_20d_pct=5,
            news_heat=0,
            event_score=0,
            moat_score=0,
            etf_change_score=0,
        ),
        evidence=[evidence],
    )
    brief = _brief().model_copy(
        update={
            "discovery": DiscoveryPool(
                market="CN",
                as_of=NOW,
                source_count=1,
                candidates=[candidate],
            )
        }
    )
    bad = (
        _reply()
        .replace('"symbol": "TEST"', '"symbol": "SCREEN.SS"')
        .replace('"display_name": "测试标的"', '"display_name": "筛选标的"')
        .replace('"stance": "watch"', '"stance": "buy_on_confirmation"')
    )

    writer = ResearchBriefWriter(
        SETTINGS,
        complete=lambda _settings, _system, _prompt: bad,
    )

    assert writer.write(brief, market_id="CN", market_label="Mainland China") is None
