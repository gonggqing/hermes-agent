"""Telegram send_sticker tool: palette, scope, cooldown and transport safety."""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, patch

import tools.send_sticker_tool as sticker_tool
from toolsets import resolve_toolset


def _settings(**overrides):
    return {
        "enabled": True,
        "group_only": True,
        "cooldown_minutes": 90,
        **overrides,
    }


def _palette(emoji="🎉", description="A character celebrating"):
    return [{
        "file_unique_id": "unique-1",
        "file_id": "bot-file-id",
        "emoji": emoji,
        "description": description,
        "set_name": "Friendly",
        "last_received_at": 100.0,
    }]


def _context(name, default=""):
    return {
        "HERMES_SESSION_PLATFORM": "telegram",
        "HERMES_SESSION_CHAT_ID": "-100123",
        "HERMES_SESSION_THREAD_ID": "",
    }.get(name, default)


def test_check_is_telegram_config_gated_even_before_palette_is_learned(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "secret-token")
    monkeypatch.delenv("HERMES_CRON_SESSION", raising=False)
    monkeypatch.setattr(sticker_tool, "_session_value", _context)
    monkeypatch.setattr(sticker_tool, "_sticker_settings", _settings)
    monkeypatch.setattr(sticker_tool, "get_sendable_stickers", lambda: [])

    assert sticker_tool._check_send_sticker_available()

    monkeypatch.setattr(
        sticker_tool, "_session_value",
        lambda name: "discord" if name == "HERMES_SESSION_PLATFORM" else "",
    )
    assert not sticker_tool._check_send_sticker_available()


def test_tool_is_separate_and_only_bundled_with_telegram():
    assert "send_sticker" in resolve_toolset("hermes-telegram")
    assert "send_sticker" not in resolve_toolset("hermes-discord")


def test_check_refuses_cron(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "secret-token")
    monkeypatch.setenv("HERMES_CRON_SESSION", "1")
    monkeypatch.setattr(sticker_tool, "_session_value", _context)
    monkeypatch.setattr(sticker_tool, "_sticker_settings", _settings)
    monkeypatch.setattr(sticker_tool, "get_sendable_stickers", _palette)

    assert not sticker_tool._check_send_sticker_available()


def test_handler_sends_learned_sticker_to_group_and_persists_cooldown(
    monkeypatch, tmp_path
):
    monkeypatch.delenv("HERMES_CRON_SESSION", raising=False)
    monkeypatch.setattr(sticker_tool, "STATE_PATH", tmp_path / "state.json")
    monkeypatch.setattr(sticker_tool, "_session_value", _context)
    monkeypatch.setattr(sticker_tool, "_sticker_settings", _settings)
    monkeypatch.setattr(sticker_tool, "get_sendable_stickers", _palette)
    send = AsyncMock(return_value="42")
    monkeypatch.setattr(sticker_tool, "_send_native_sticker", send)

    with patch("tools.send_sticker_tool.time.time", return_value=1_000.0):
        first = json.loads(asyncio.run(
            sticker_tool._handle_send_sticker({"intent": "celebrate"})
        ))
        second = json.loads(asyncio.run(
            sticker_tool._handle_send_sticker({"intent": "celebrate"})
        ))

    assert first["success"] is True
    assert second["success"] is False
    assert second["error"] == "sticker cooldown active"
    send.assert_awaited_once_with("-100123", "", "bot-file-id")


def test_handler_refuses_dm(monkeypatch):
    monkeypatch.setattr(
        sticker_tool, "_session_value",
        lambda name: {
            "HERMES_SESSION_PLATFORM": "telegram",
            "HERMES_SESSION_CHAT_ID": "5961409264",
        }.get(name, ""),
    )
    monkeypatch.setattr(sticker_tool, "_sticker_settings", _settings)

    result = json.loads(asyncio.run(
        sticker_tool._handle_send_sticker({"intent": "friendly"})
    ))

    assert result == {
        "success": False,
        "error": "stickers are limited to Telegram groups",
    }


def test_empty_palette_tells_user_how_to_teach_bot(monkeypatch, tmp_path):
    monkeypatch.delenv("HERMES_CRON_SESSION", raising=False)
    monkeypatch.setattr(sticker_tool, "STATE_PATH", tmp_path / "state.json")
    monkeypatch.setattr(sticker_tool, "_session_value", _context)
    monkeypatch.setattr(sticker_tool, "_sticker_settings", _settings)
    monkeypatch.setattr(sticker_tool, "get_sendable_stickers", lambda: [])

    result = json.loads(asyncio.run(
        sticker_tool._handle_send_sticker({"intent": "friendly"})
    ))

    assert result["success"] is False
    assert result["hint"] == "Ask the user to send the bot a suitable sticker once."


def test_inflight_reservation_blocks_concurrent_double_send(monkeypatch, tmp_path):
    monkeypatch.setattr(sticker_tool, "STATE_PATH", tmp_path / "state.json")
    sticker_tool._IN_FLIGHT_CHATS.clear()

    first, _ = sticker_tool._claim_send("-100123", 90, 1_000.0)
    second, retry = sticker_tool._claim_send("-100123", 90, 1_000.0)
    sticker_tool._release_send("-100123")
    third, _ = sticker_tool._claim_send("-100123", 90, 1_000.0)
    sticker_tool._release_send("-100123")

    assert first is True
    assert second is False and retry == 60
    assert third is True


def test_handler_does_not_fallback_to_wrong_intent(monkeypatch, tmp_path):
    monkeypatch.delenv("HERMES_CRON_SESSION", raising=False)
    monkeypatch.setattr(sticker_tool, "STATE_PATH", tmp_path / "state.json")
    monkeypatch.setattr(sticker_tool, "_session_value", _context)
    monkeypatch.setattr(sticker_tool, "_sticker_settings", _settings)
    monkeypatch.setattr(
        sticker_tool, "get_sendable_stickers",
        lambda: _palette(emoji="😴", description="A character sleeping"),
    )

    result = json.loads(asyncio.run(
        sticker_tool._handle_send_sticker({"intent": "celebrate"})
    ))

    assert result["success"] is False
    assert "no approved sticker matches" in result["error"]


def test_friendly_intent_falls_back_to_latest_learned_sticker():
    learned = _palette(emoji="👩‍❤️‍💋‍👨", description="")

    assert sticker_tool._select_sticker("friendly", learned) == learned[0]


def test_cooldown_config_is_clamped_to_one_or_two_hours(monkeypatch):
    monkeypatch.setattr(
        sticker_tool, "load_config_readonly",
        lambda: {"platforms": {"telegram": {"stickers": {
            "enabled": True, "cooldown_minutes": 5,
        }}}},
    )
    assert sticker_tool._sticker_settings()["cooldown_minutes"] == 60

    monkeypatch.setattr(
        sticker_tool, "load_config_readonly",
        lambda: {"platforms": {"telegram": {"stickers": {
            "enabled": True, "cooldown_minutes": 999,
        }}}},
    )
    assert sticker_tool._sticker_settings()["cooldown_minutes"] == 120
