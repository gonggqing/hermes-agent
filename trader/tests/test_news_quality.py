"""Current-catalyst policy for Finance research news."""

from datetime import datetime, timedelta, timezone

from swing_trader.interfaces import NewsItem
from swing_trader.news_quality import curate_news


NOW = datetime(2026, 8, 11, 12, 0, tzinfo=timezone.utc)


def _item(
    headline: str,
    *,
    age_hours: float = 1,
    symbol: str | None = "TEST",
    source: str = "Reuters",
    url: str = "",
) -> NewsItem:
    return NewsItem(
        symbol=symbol,
        ts=NOW - timedelta(hours=age_hours),
        headline=headline,
        source=source,
        url=url,
        sentiment=0.5,
    )


def test_articles_older_than_72_hours_are_not_current_catalysts() -> None:
    result = curate_news(
        [_item("fresh", age_hours=71.9), _item("stale", age_hours=72.1)],
        as_of=NOW,
    )

    assert [item.headline for item in result.items] == ["fresh"]
    assert result.stale_excluded == 1


def test_duplicate_url_or_headline_is_counted_once() -> None:
    result = curate_news(
        [
            _item("newer canonical title", age_hours=1, url="https://x.test/a?utm_source=y"),
            _item("older alternate title", age_hours=2, url="https://x.test/a"),
            _item("newer canonical title", age_hours=3, url="https://mirror.test/a"),
        ],
        as_of=NOW,
        across_symbols=True,
    )

    assert [item.headline for item in result.items] == ["newer canonical title"]
    assert result.duplicates_excluded == 2


def test_cross_symbol_dedupe_is_explicit() -> None:
    rows = [
        _item("same event", symbol="AAA", url="https://x.test/a"),
        _item("same event", symbol="BBB", url="https://x.test/a"),
    ]

    per_symbol = curate_news(rows, as_of=NOW, across_symbols=False)
    digest = curate_news(rows, as_of=NOW, across_symbols=True)

    assert len(per_symbol.items) == 2
    assert len(digest.items) == 1
    assert digest.duplicates_excluded == 1


def test_future_timestamp_beyond_clock_skew_is_rejected() -> None:
    future = NewsItem(
        symbol="TEST",
        ts=NOW + timedelta(minutes=31),
        headline="not published yet",
    )

    result = curate_news([future], as_of=NOW)

    assert result.items == []
    assert result.future_excluded == 1
