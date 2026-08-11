"""Tests for durable research-brief snapshots (brief_store.py).

The rendered brief otherwise lives only in memory; this archive gives it a
queryable history that survives restart, so the agent can read a past day's
brief instead of re-pulling live data.
"""

from __future__ import annotations

from swing_trader.brief_store import BRIEF_METADATA, BriefStore
from swing_trader.ledger import Ledger


def _store(tmp_path):
    return BriefStore(url=f"sqlite:///{tmp_path/'briefs.db'}")


def _kr_brief(as_of="2026-07-14T09:11:34Z", trading_date="2026-07-14"):
    return {
        "as_of": as_of, "trading_date": trading_date, "mode": "paper",
        "movers": {"top": [{"symbol": "005930.KS"}, {"symbol": "000660.KS"}],
                   "bottom": []},
        "signals_today": [{"symbol": "005930.KS"}, {"symbol": "000660.KS"},
                          {"symbol": "000990.KS"}],
    }


def test_save_then_get_roundtrip(tmp_path):
    st = _store(tmp_path)
    sid = st.save("kr", _kr_brief())
    got = st.get(sid)
    assert got is not None and got["trading_date"] == "2026-07-14"
    assert got["movers"]["top"][0]["symbol"] == "005930.KS"


def test_list_is_newest_first_and_counts_are_derived(tmp_path):
    st = _store(tmp_path)
    st.save("kr", _kr_brief(as_of="2026-07-13T09:11:00Z", trading_date="2026-07-13"))
    st.save("kr", _kr_brief(as_of="2026-07-14T09:11:00Z", trading_date="2026-07-14"))
    rows = st.list_snapshots("kr")
    assert [r["trading_date"] for r in rows] == ["2026-07-14", "2026-07-13"]
    assert rows[0]["n_movers"] == 2 and rows[0]["n_signals"] == 3  # derived counts


def test_market_filter_and_get_by_date(tmp_path):
    st = _store(tmp_path)
    st.save("kr", _kr_brief())
    st.save("cn", {"as_of": "2026-07-14T01:30:00Z", "trading_date": "2026-07-14",
                   "movers": {"top": [{"symbol": "9988.HK"}]}})
    assert len(st.list_snapshots("kr")) == 1
    assert len(st.list_snapshots("cn")) == 1
    assert len(st.list_snapshots()) == 2  # no filter → all markets
    cn = st.get_by_date("cn", "2026-07-14")
    assert cn is not None and cn["movers"]["top"][0]["symbol"] == "9988.HK"
    assert st.get_by_date("cn", "2020-01-01") is None


def test_get_by_date_returns_latest_run_of_the_day(tmp_path):
    st = _store(tmp_path)
    st.save("kr", _kr_brief(as_of="2026-07-14T00:30:00Z"))
    st.save("kr", _kr_brief(as_of="2026-07-14T06:00:00Z"))  # later run same day
    got = st.get_by_date("kr", "2026-07-14")
    assert got["as_of"] == "2026-07-14T06:00:00Z"


def test_get_latest_returns_newest_snapshot_across_dates(tmp_path):
    st = _store(tmp_path)
    st.save("kr", _kr_brief(as_of="2026-07-13T09:11:00Z",
                             trading_date="2026-07-13"))
    st.save("kr", _kr_brief(as_of="2026-07-14T09:11:00Z",
                             trading_date="2026-07-14"))
    st.save("cn", {"as_of": "2026-07-15T01:00:00Z",
                   "trading_date": "2026-07-15", "marker": "other-market"})

    got = st.get_latest("KR")
    assert got is not None and got["trading_date"] == "2026-07-14"
    assert st.get_latest("us") is None


def test_same_evidence_timestamp_restores_last_enriched_snapshot(tmp_path):
    """Structured and AI-enriched writes can share one evidence ``as_of``.

    Restart recovery must use row creation order as the deterministic tie
    breaker or it can randomly restore the earlier narrative-less payload.
    """
    st = _store(tmp_path)
    evidence_time = "2026-07-14T09:11:00Z"
    base = _kr_brief(as_of=evidence_time)
    st.save("kr", {**base, "narrative": None})
    st.save("kr", {**base, "narrative": {"headline": "primary synthesis"}})

    latest = st.get_latest("kr")
    assert latest["narrative"]["headline"] == "primary synthesis"


def test_snapshot_table_isolated_from_ledger(tmp_path):
    """Own MetaData: the snapshot table must NOT be creatable by the ledger and
    vice versa (same DB-file could otherwise cross-pollute)."""
    assert "research_brief_snapshots" in BRIEF_METADATA.tables
    # the ledger's metadata knows nothing about the brief table
    Ledger(url=f"sqlite:///{tmp_path/'l.db'}")  # constructs cleanly, no collision


def test_unknown_snapshot_id_returns_none(tmp_path):
    assert _store(tmp_path).get("does-not-exist") is None


def test_same_edition_and_evidence_hash_is_idempotent(tmp_path):
    st = _store(tmp_path)
    brief = _brief_with_edition("morning", as_of="2026-07-14T01:00:00Z")
    brief["narrative"]["evidence_hash"] = "same-evidence"

    first = st.save("cn", brief)
    second = st.save("cn", {**brief, "as_of": "2026-07-14T01:01:00Z"})

    assert second == first
    assert len(st.list_snapshots("cn")) == 1


def test_recent_distinct_ignores_repeated_evidence_and_structured_only_rows(tmp_path):
    st = _store(tmp_path)
    old = _brief_with_edition("morning", as_of="2026-07-13T01:00:00Z")
    old["narrative"]["evidence_hash"] = "old"
    repeated = _brief_with_edition("evening", as_of="2026-07-13T13:00:00Z")
    repeated["narrative"]["evidence_hash"] = "old"
    current = _brief_with_edition("morning", as_of="2026-07-14T01:00:00Z")
    current["narrative"]["evidence_hash"] = "current"
    st.save("cn", old)
    st.save("cn", repeated)
    st.save("cn", {**current, "narrative": None})
    st.save("cn", current)

    history = st.get_recent_distinct(
        "cn", before_generated_at="2026-07-15T00:00:00Z", limit=6
    )

    assert [row["narrative"]["evidence_hash"] for row in history] == [
        "current",
        "old",
    ]


def _brief_with_edition(edition, *, as_of, trading_date="2026-07-14"):
    b = _kr_brief(as_of=as_of, trading_date=trading_date)
    b["narrative"] = {"edition": edition, "generated_at": as_of, "headline": "h"}
    return b


def test_prune_duplicates_keeps_newest_per_market_date_edition(tmp_path):
    st = _store(tmp_path)
    # three 'evening' repeats (restart artifacts) for the same day + market
    st.save("cn", _brief_with_edition("evening", as_of="2026-07-14T13:00:00Z"))
    st.save("cn", _brief_with_edition("evening", as_of="2026-07-14T13:30:00Z"))
    newest = st.save("cn", _brief_with_edition("evening", as_of="2026-07-14T14:00:00Z"))
    # a genuinely different edition + a different market must survive untouched
    st.save("cn", _brief_with_edition("morning", as_of="2026-07-14T01:00:00Z"))
    st.save("us", _brief_with_edition("evening", as_of="2026-07-14T13:00:00Z"))

    deleted = st.prune_duplicates()

    assert deleted == 2  # only the two older CN 'evening' repeats
    cn_evening = [
        r for r in st.list_snapshots("cn")
        if st.get(r["id"])["narrative"]["edition"] == "evening"
    ]
    assert len(cn_evening) == 1 and cn_evening[0]["id"] == newest
    assert len(st.list_snapshots("cn")) == 2  # evening (newest) + morning
    assert len(st.list_snapshots("us")) == 1
    assert st.prune_duplicates() == 0  # idempotent


def test_iter_snapshots_returns_full_payload_oldest_first(tmp_path):
    store = _store(tmp_path)
    first = store.save("kr", _kr_brief(as_of="2026-07-13T09:00:00Z"))
    second = store.save("us", _kr_brief(as_of="2026-07-14T09:00:00Z"))
    rows = store.iter_snapshots()
    assert [row[0] for row in rows] == [first, second]
    assert rows[0][1] == "kr" and rows[1][1] == "us"
    assert rows[0][2]["trading_date"] == "2026-07-14"
