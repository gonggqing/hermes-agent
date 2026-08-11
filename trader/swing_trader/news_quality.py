"""Deterministic news freshness, de-duplication and source-quality policy.

Yahoo's per-symbol feed frequently returns the same headline for days or even
weeks.  A new :class:`~swing_trader.monitors.NewsSnapshot` timestamp only says
when Hermes polled the endpoint; it does *not* make every returned article new.
This module keeps that distinction explicit before news reaches sentiment,
Debate or the primary-model daily brief.

The policy is deliberately deterministic and execution-independent:

* articles older than 72 hours are historical context, not a current catalyst;
* future-dated articles beyond a small clock-skew allowance are rejected;
* repeated URL/headline events are counted once;
* source quality affects ordering, never truth — the final brief must still
  cite the supplied article and distinguish fact from inference.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from swing_trader.interfaces import NewsItem

__all__ = [
    "FRESH_NEWS_MAX_AGE",
    "NewsCuration",
    "canonical_news_key",
    "curate_news",
    "source_quality",
]

FRESH_NEWS_MAX_AGE = timedelta(hours=72)
_MAX_FUTURE_SKEW = timedelta(minutes=30)
_TRACKING_QUERY_KEYS = {
    "guce_referrer",
    "guce_referrer_sig",
    "siteid",
    "soc_src",
    "soc_trk",
    "yptr",
}

_MAJOR_SOURCES = (
    "associated press",
    "bloomberg",
    "cnbc",
    "financial times",
    "nikkei",
    "reuters",
    "south china morning post",
    "the wall street journal",
    "wall street journal",
)
_SECONDARY_SOURCES = (
    "barron",
    "barchart",
    "guru",
    "insider monkey",
    "motley fool",
    "quartz",
    "simply wall st",
    "stocktwits",
    "zacks",
)


@dataclass(frozen=True)
class NewsCuration:
    items: list[NewsItem]
    stale_excluded: int = 0
    future_excluded: int = 0
    duplicates_excluded: int = 0
    malformed_excluded: int = 0


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _headline_key(headline: str) -> str:
    normalized = unicodedata.normalize("NFKC", headline).casefold()
    return re.sub(r"[^\w]+", " ", normalized, flags=re.UNICODE).strip()


def _canonical_url(url: str) -> str:
    raw = url.strip()
    if not raw:
        return ""
    try:
        parts = urlsplit(raw)
    except ValueError:
        return raw
    query = [
        (key, value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if not key.casefold().startswith("utm_") and key.casefold() not in _TRACKING_QUERY_KEYS
    ]
    return urlunsplit(
        (parts.scheme.casefold(), parts.netloc.casefold(), parts.path, urlencode(query), "")
    )


def canonical_news_key(item: NewsItem) -> str:
    """Stable primary event identity for logging and deterministic sorting."""

    headline = _headline_key(item.headline)
    url = _canonical_url(item.url)
    if headline:
        return f"headline:{headline}"
    return f"url:{url}" if url else ""


def _event_identities(item: NewsItem) -> frozenset[str]:
    """All identities that can prove two rows describe the same event.

    Yahoo can return one URL with slightly different titles for several
    symbols, while syndicated stories can keep one title across several URLs.
    Treating only one field as authoritative misses one of those duplicate
    classes, so either a canonical URL *or* a normalized headline is enough.
    """

    identities: set[str] = set()
    headline = _headline_key(item.headline)
    url = _canonical_url(item.url)
    if headline:
        identities.add(f"headline:{headline}")
    if url:
        identities.add(f"url:{url}")
    return frozenset(identities)


def source_quality(source: str) -> tuple[str, float]:
    """Return a transparent ranking label and weight for one publisher."""

    normalized = source.strip().casefold()
    if any(name in normalized for name in _MAJOR_SOURCES):
        return "major", 1.0
    if any(name in normalized for name in _SECONDARY_SOURCES):
        return "secondary", 0.65
    return "unrated", 0.4


def curate_news(
    items: list[NewsItem],
    *,
    as_of: datetime,
    max_age: timedelta = FRESH_NEWS_MAX_AGE,
    across_symbols: bool = False,
) -> NewsCuration:
    """Return fresh, de-duplicated news sorted newest/strongest-source first.

    ``across_symbols=False`` keeps one copy per symbol for per-symbol sentiment;
    the human digest uses ``True`` so one syndicated event is shown once.
    """

    now = _utc(as_of)
    chosen: list[NewsItem] = []
    identities_by_scope: dict[str, set[str]] = {}
    stale = future = duplicate = malformed = 0

    eligible: list[NewsItem] = []
    for item in items:
        identities = _event_identities(item)
        if not identities or not item.headline.strip():
            malformed += 1
            continue
        published = _utc(item.ts)
        age = now - published
        if age < -_MAX_FUTURE_SKEW:
            future += 1
            continue
        if age > max_age:
            stale += 1
            continue
        eligible.append(item)

    # Best row wins deterministically before duplicate elimination. This also
    # prevents a low-quality, older duplicate encountered first from masking a
    # newer primary-source version of the same event.
    eligible.sort(
        key=lambda item: (
            -_utc(item.ts).timestamp(),
            -source_quality(item.source)[1],
            canonical_news_key(item),
        )
    )
    for item in eligible:
        scope = "*" if across_symbols else (item.symbol or "MARKET").upper()
        identities = _event_identities(item)
        seen = identities_by_scope.setdefault(scope, set())
        if identities & seen:
            duplicate += 1
            continue
        seen.update(identities)
        chosen.append(item)

    curated = sorted(
        chosen,
        key=lambda item: (
            -_utc(item.ts).timestamp(),
            -source_quality(item.source)[1],
            canonical_news_key(item),
        ),
    )
    return NewsCuration(
        items=curated,
        stale_excluded=stale,
        future_excluded=future,
        duplicates_excluded=duplicate,
        malformed_excluded=malformed,
    )
