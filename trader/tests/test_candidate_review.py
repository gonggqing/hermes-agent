from datetime import datetime, timezone

from swing_trader.candidate_review import LLMCandidateReviewer
from swing_trader.interfaces import Bar, NewsItem, Quote
from swing_trader.llm import LLMSettings
from swing_trader.schemas import CandidateOrder, CandidateStatus, OrderType, Side

NOW = datetime(2026, 7, 16, 15, 0, tzinfo=timezone.utc)


def candidate() -> CandidateOrder:
    return CandidateOrder(
        symbol="VST", side=Side.BUY, qty=1, order_type=OrderType.BRACKET,
        limit=164.0, stop=152.0, tp=184.0, rationale="original",
        confidence=0.72, ref_px=165.0, status=CandidateStatus.APPROVED,
    )


def context():
    quote = Quote("VST", NOW, 166.0, 165.9, 166.1)
    bars = [Bar("VST", NOW, 164, 167, 163, 166, 1_000_000)]
    news = [NewsItem("VST", NOW, "Fresh capacity update", "source")]
    return quote, bars, news


def reviewer(response: str) -> LLMCandidateReviewer:
    settings = LLMSettings("https://example.test", "primary", "secret")
    return LLMCandidateReviewer(settings, complete=lambda *_args: response)


def test_keep_has_no_edits():
    quote, bars, news = context()
    result = reviewer('{"action":"keep","reason_zh":"行情稳定","qty":null,"limit":null,"stop":null,"tp":null}').review(
        candidate(), quote, bars, news, "risk_on"
    )
    assert result is not None and not result.needs_reconfirmation
    assert result.edits == {}


def test_revision_is_structured_and_requires_second_confirmation():
    quote, bars, news = context()
    result = reviewer('{"action":"revise","reason_zh":"价格上移","limit":165.5,"stop":153,"tp":186,"qty":1}').review(
        candidate(), quote, bars, news, "neutral"
    )
    assert result is not None and result.needs_reconfirmation
    assert result.edits["limit"] == 165.5


def test_invalid_or_unparseable_output_fails_closed():
    quote, bars, news = context()
    assert reviewer("not json").review(candidate(), quote, bars, news, "neutral") is None
    assert reviewer('{"action":"keep","reason_zh":"x","limit":1}').review(
        candidate(), quote, bars, news, "neutral"
    ) is None
