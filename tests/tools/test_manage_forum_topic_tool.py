"""Telegram forum-topic tool scope, confirmation, and state contracts."""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock

import gateway.telegram_forum_topics as topic_state
import tools.manage_forum_topic_tool as topic_tool
from toolsets import resolve_toolset


def _context(name: str, default: str = "") -> str:
    return {
        "HERMES_SESSION_PLATFORM": "telegram",
        "HERMES_SESSION_CHAT_ID": "-100123",
        "HERMES_SESSION_THREAD_ID": "",
    }.get(name, default)


def _configure(monkeypatch, tmp_path):
    monkeypatch.setattr(topic_state, "STATE_PATH", tmp_path / "topics.json")
    monkeypatch.setattr(topic_tool, "_session_value", _context)
    monkeypatch.setattr(
        topic_tool,
        "_settings",
        lambda: {"enabled": True, "auto_archive_days": 7},
    )


def test_tool_is_available_only_in_telegram_toolset():
    assert "manage_forum_topic" in resolve_toolset("hermes-telegram")
    assert "manage_forum_topic" not in resolve_toolset("hermes-discord")


def test_schema_availability_uses_global_config_not_session_context(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-token")
    monkeypatch.delenv("HERMES_CRON_SESSION", raising=False)
    monkeypatch.setattr(
        topic_tool,
        "_settings",
        lambda: {"enabled": True, "auto_archive_days": 7},
    )
    monkeypatch.setattr(topic_tool, "_session_value", lambda _name: "")

    assert topic_tool._check_available() is True


def test_handler_still_enforces_telegram_group_scope(monkeypatch):
    monkeypatch.setattr(
        topic_tool,
        "_settings",
        lambda: {"enabled": True, "auto_archive_days": 7},
    )
    monkeypatch.setattr(topic_tool, "_session_value", lambda _name: "")

    result = json.loads(asyncio.run(topic_tool._handle({"action": "list"})))

    assert result == {
        "success": False,
        "error": "current session is not a Telegram group",
    }


def test_create_posts_context_and_registers_managed_topic(monkeypatch, tmp_path):
    _configure(monkeypatch, tmp_path)
    action = AsyncMock(return_value={"topic_id": "42"})
    monkeypatch.setattr(topic_tool, "_telegram_action", action)

    result = json.loads(asyncio.run(topic_tool._handle({
        "action": "create",
        "name": "机器人供应链",
        "opening_message": "集中讨论产业链标的与风险。",
    })))

    assert result["success"] is True
    assert result["topic"]["topic_id"] == "42"
    action.assert_awaited_once_with(
        "create",
        "-100123",
        name="机器人供应链",
        opening_message="集中讨论产业链标的与风险。",
    )
    assert topic_state.get_topic("-100123", "42")["managed_by"] == "hermes"


def test_delete_requires_explicit_confirmation(monkeypatch, tmp_path):
    _configure(monkeypatch, tmp_path)
    topic_state.register_topic(
        "-100123", "42", "Research", auto_archive_days=7
    )
    action = AsyncMock(return_value={})
    monkeypatch.setattr(topic_tool, "_telegram_action", action)

    refused = json.loads(asyncio.run(topic_tool._handle({
        "action": "delete", "topic_id": "42"
    })))
    accepted = json.loads(asyncio.run(topic_tool._handle({
        "action": "delete", "topic_id": "42", "confirm_delete": True
    })))

    assert refused["success"] is False
    assert accepted["topic"]["status"] == "deleted"
    action.assert_awaited_once_with(
        "delete", "-100123", topic_id="42", name=""
    )


def test_tool_refuses_general_and_unmanaged_topics(monkeypatch, tmp_path):
    _configure(monkeypatch, tmp_path)

    general = json.loads(asyncio.run(topic_tool._handle({
        "action": "archive", "topic_id": "1"
    })))
    unmanaged = json.loads(asyncio.run(topic_tool._handle({
        "action": "archive", "topic_id": "88"
    })))

    assert general["success"] is False
    assert unmanaged["error"] == "topic is not managed by Hermes"


def test_list_is_current_group_only(monkeypatch, tmp_path):
    _configure(monkeypatch, tmp_path)
    topic_state.register_topic(
        "-100123", "42", "Current", auto_archive_days=7
    )
    topic_state.register_topic(
        "-100999", "99", "Other", auto_archive_days=7
    )

    result = json.loads(asyncio.run(topic_tool._handle({"action": "list"})))

    assert [topic["name"] for topic in result["topics"]] == ["Current"]
