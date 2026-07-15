"""Telegram adapter integration for managed forum-topic activity."""

from __future__ import annotations

from types import SimpleNamespace

import gateway.telegram_forum_topics as topic_state
from plugins.platforms.telegram.adapter import TelegramAdapter, _apply_yaml_config


def test_yaml_forum_topic_config_reaches_adapter_extra():
    forum_config = {
        "enabled": True,
        "auto_archive_days": 7,
        "archive_check_hours": 6,
    }

    extras = _apply_yaml_config({}, {"forum_topics": forum_config})

    assert extras["forum_topics"] == forum_config
    assert extras["forum_topics"] is not forum_config


def test_authorized_topic_activity_touches_only_managed_non_general_topic(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(topic_state, "STATE_PATH", tmp_path / "topics.json")
    topic_state.register_topic(
        "-100123", "42", "Research", auto_archive_days=7, now=100.0
    )
    adapter = object.__new__(TelegramAdapter)
    adapter._forum_topics_enabled = True
    managed = SimpleNamespace(
        chat=SimpleNamespace(id=-100123, type="supergroup"),
        message_thread_id=42,
    )
    general = SimpleNamespace(
        chat=SimpleNamespace(id=-100123, type="supergroup"),
        message_thread_id=1,
    )

    monkeypatch.setattr(topic_state.time, "time", lambda: 200.0)
    adapter._touch_managed_forum_topic(managed)
    adapter._touch_managed_forum_topic(general)

    assert topic_state.get_topic("-100123", "42")["last_activity_at"] == 200.0
