"""Persistence and idle-archive behavior for Hermes-managed forum topics."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import gateway.telegram_forum_topics as topics


def test_topic_lifecycle_is_scoped_and_persistent(monkeypatch, tmp_path):
    monkeypatch.setattr(topics, "STATE_PATH", tmp_path / "topics.json")

    created = topics.register_topic(
        "-1001", "42", "半导体供应链", auto_archive_days=7, now=100.0
    )
    topics.register_topic(
        "-1002", "99", "Other chat", auto_archive_days=7, now=100.0
    )

    assert created["status"] == "open"
    assert [item["topic_id"] for item in topics.list_topics("-1001")] == ["42"]
    assert topics.touch_topic("-1001", "42", now=200.0)
    assert topics.get_topic("-1001", "42")["last_activity_at"] == 200.0

    archived = topics.update_topic("-1001", "42", status="archived", now=300.0)
    assert archived["status"] == "archived"
    reopened = topics.update_topic("-1001", "42", status="open", now=400.0)
    assert reopened["last_activity_at"] == 400.0


def test_activity_writes_are_coalesced(monkeypatch, tmp_path):
    monkeypatch.setattr(topics, "STATE_PATH", tmp_path / "topics.json")
    topics.register_topic("-1001", "42", "Research", auto_archive_days=7, now=100.0)

    assert topics.touch_topic("-1001", "42", now=120.0)
    assert topics.get_topic("-1001", "42")["last_activity_at"] == 100.0
    assert topics.touch_topic("-1001", "42", now=161.0)
    assert topics.get_topic("-1001", "42")["last_activity_at"] == 161.0


def test_archive_inactive_topics_updates_only_after_telegram_accepts(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(topics, "STATE_PATH", tmp_path / "topics.json")
    topics.register_topic("-1001", "42", "Due", auto_archive_days=1, now=0.0)
    topics.register_topic("-1001", "43", "Fresh", auto_archive_days=7, now=0.0)
    bot = AsyncMock()

    result = asyncio.run(topics.archive_inactive_topics(bot, now=2 * 86400.0))

    assert result == {"archived": 1, "failed": 0}
    bot.close_forum_topic.assert_awaited_once_with(
        chat_id=-1001, message_thread_id=42
    )
    assert topics.get_topic("-1001", "42")["status"] == "archived"
    assert topics.get_topic("-1001", "43")["status"] == "open"


def test_failed_archive_remains_open_for_retry(monkeypatch, tmp_path):
    monkeypatch.setattr(topics, "STATE_PATH", tmp_path / "topics.json")
    topics.register_topic("-1001", "42", "Due", auto_archive_days=1, now=0.0)
    bot = AsyncMock()
    bot.close_forum_topic.side_effect = RuntimeError("no permission")

    result = asyncio.run(topics.archive_inactive_topics(bot, now=2 * 86400.0))

    assert result == {"archived": 0, "failed": 1}
    assert topics.get_topic("-1001", "42")["status"] == "open"
