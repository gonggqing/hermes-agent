from datetime import datetime, timezone

from swing_trader.research_synthesis import build_cn_hk_synthesis


def brief(market, regime, symbol):
    return {
        "as_of": "2026-07-15T03:30:00Z",
        "trading_date": "2026-07-15",
        "freshness": {"status": "fresh"},
        "regime": {"risk_on_off": regime},
        "themes": [{"theme": "semiconductor", "leaders": [symbol]}],
        "discovery": {"candidates": [{
            "symbol": symbol,
            "theme": "robotics",
            "evidence": [{"url": f"https://example.com/{market}"}],
        }]},
    }


def test_synthesis_keeps_market_state_independent():
    result = build_cn_hk_synthesis(
        brief("cn", "risk_off", "688001.SS"),
        brief("hk", "risk_on", "0001.HK"),
        now=datetime(2026, 7, 15, tzinfo=timezone.utc),
    )
    assert result.status == "complete"
    assert result.markets["cn"].regime == "risk_off"
    assert result.markets["hk"].regime == "risk_on"
    assert [row.theme for row in result.shared_themes] == ["robotics", "semiconductor"]


def test_synthesis_degrades_when_one_brief_is_missing():
    result = build_cn_hk_synthesis(brief("cn", "neutral", "A"), None)
    assert result.status == "degraded"
    assert result.markets["cn"].available
    assert not result.markets["hk"].available
    assert result.shared_themes == []
