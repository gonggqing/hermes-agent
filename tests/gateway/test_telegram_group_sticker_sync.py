"""Telegram Group Sticker Set automatic palette import tests."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from gateway.config import PlatformConfig
from gateway.config import Platform
from gateway.sticker_cache import get_sendable_stickers
from plugins.platforms.telegram.adapter import TelegramAdapter, _apply_yaml_config


def _adapter(*, enabled=True, interval=6):
    adapter = TelegramAdapter(PlatformConfig(
        enabled=True,
        token="test-token",
        extra={
            "stickers": {
                "enabled": enabled,
                "auto_import_group_set": True,
                "sync_interval_hours": interval,
            }
        },
    ))
    adapter._bot = AsyncMock()
    return adapter


@pytest.mark.asyncio
async def test_imports_complete_group_sticker_set(tmp_path):
    adapter = _adapter()
    adapter._bot.get_chat.return_value = SimpleNamespace(sticker_set_name="GroupPack")
    adapter._bot.get_sticker_set.return_value = SimpleNamespace(stickers=[
        SimpleNamespace(
            file_unique_id="u1", file_id="f1", emoji="👋",
            set_name="GroupPack", is_animated=False, is_video=False,
        ),
        SimpleNamespace(
            file_unique_id="u2", file_id="f2", emoji="🎉",
            set_name="GroupPack", is_animated=True, is_video=False,
        ),
    ])

    with patch("gateway.sticker_cache.CACHE_PATH", tmp_path / "stickers.json"):
        imported = await adapter._sync_group_sticker_set("-100123", force=True)
        palette = get_sendable_stickers()

    assert imported == 2
    assert {item["file_id"] for item in palette} == {"f1", "f2"}
    assert adapter._group_sticker_set_names["-100123"] == "GroupPack"
    adapter._bot.get_chat.assert_awaited_once_with(chat_id=-100123)
    adapter._bot.get_sticker_set.assert_awaited_once_with(name="GroupPack")


@pytest.mark.asyncio
async def test_sync_is_rate_limited_and_non_group_is_ignored():
    adapter = _adapter()
    adapter._bot.get_chat.return_value = SimpleNamespace(sticker_set_name=None)

    assert await adapter._sync_group_sticker_set("5961409264", force=True) == 0
    assert await adapter._sync_group_sticker_set("-100123", force=True) == 0
    assert await adapter._sync_group_sticker_set("-100123") == 0
    assert adapter._bot.get_chat.await_count == 1


@pytest.mark.asyncio
async def test_bot_api_failure_is_non_fatal():
    adapter = _adapter()
    adapter._bot.get_chat.side_effect = RuntimeError("secret transport URL")

    assert await adapter._sync_group_sticker_set("-100123", force=True) == 0


def test_top_level_sticker_config_is_forwarded_to_adapter():
    extras = _apply_yaml_config({}, {
        "stickers": {"enabled": True, "auto_import_group_set": True}
    })

    assert extras["stickers"]["enabled"] is True
    assert extras["stickers"]["auto_import_group_set"] is True


def test_previously_seen_group_is_discovered_from_session_store(monkeypatch):
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    adapter = _adapter()
    adapter._session_store = SimpleNamespace(list_sessions=lambda: [
        SimpleNamespace(origin=SimpleNamespace(
            platform=Platform.TELEGRAM, chat_type="group", chat_id="-100456"
        )),
        SimpleNamespace(origin=SimpleNamespace(
            platform=Platform.TELEGRAM, chat_type="dm", chat_id="5961409264"
        )),
        SimpleNamespace(origin=SimpleNamespace(
            platform=Platform.DISCORD, chat_type="group", chat_id="-100999"
        )),
    ])

    assert adapter._configured_sticker_group_ids() == {"-100456"}


def test_shared_telegram_chat_id_is_a_startup_group(monkeypatch):
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "-100789")
    adapter = _adapter()

    assert "-100789" in adapter._configured_sticker_group_ids()
