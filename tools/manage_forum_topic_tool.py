"""Telegram forum-topic lifecycle management for the current group."""

from __future__ import annotations

import logging
import os
from typing import Any

from gateway.telegram_forum_topics import (
    get_topic,
    list_topics,
    register_topic,
    update_topic,
)
from hermes_cli.config import load_config_readonly
from tools.registry import registry, tool_result


logger = logging.getLogger(__name__)


def _session_value(name: str) -> str:
    try:
        from gateway.session_context import get_session_env

        return str(get_session_env(name, "") or "")
    except Exception:
        return str(os.getenv(name, "") or "")


def _settings() -> dict[str, Any]:
    try:
        cfg = load_config_readonly()
    except Exception:
        return {"enabled": False}
    raw = (((cfg.get("platforms") or {}).get("telegram") or {}).get("forum_topics", {}))
    if not isinstance(raw, dict):
        return {"enabled": False}
    try:
        days = int(raw.get("auto_archive_days", 7))
    except (TypeError, ValueError):
        days = 7
    return {
        "enabled": bool(raw.get("enabled", False)),
        "auto_archive_days": max(1, min(90, days)),
    }


def _check_available() -> bool:
    """Expose the platform capability from global config, not turn context.

    Tool definitions are assembled and memoized before a Telegram message's
    context variables are guaranteed to exist.  Session-specific scope is
    therefore enforced in ``_handle``; doing it here can cache a false result
    and hide the tool for the lifetime of an agent session.
    """
    if os.getenv("HERMES_CRON_SESSION", "").lower() in {"1", "true", "yes"}:
        return False
    return (
        _settings().get("enabled", False)
        and bool(os.getenv("TELEGRAM_BOT_TOKEN", "").strip())
    )


async def _telegram_action(
    action: str,
    chat_id: str,
    *,
    topic_id: str = "",
    name: str = "",
    opening_message: str = "",
) -> dict[str, Any]:
    from telegram import Bot
    from plugins.platforms.telegram.telegram_ids import normalize_telegram_chat_id

    token = os.environ["TELEGRAM_BOT_TOKEN"]
    try:
        from gateway.platforms.base import resolve_proxy_url

        proxy = resolve_proxy_url(
            "TELEGRAM_PROXY", target_hosts=["api.telegram.org"]
        )
    except Exception:
        proxy = None
    if proxy:
        from telegram.request import HTTPXRequest

        bot = Bot(
            token=token,
            request=HTTPXRequest(proxy=proxy),
            get_updates_request=HTTPXRequest(proxy=proxy),
        )
    else:
        bot = Bot(token=token)
    async with bot:
        normalized_chat = normalize_telegram_chat_id(chat_id)
        if action == "create":
            created = await bot.create_forum_topic(chat_id=normalized_chat, name=name)
            thread_id = str(created.message_thread_id)
            if opening_message:
                await bot.send_message(
                    chat_id=normalized_chat,
                    message_thread_id=int(thread_id),
                    text=opening_message[:4096],
                )
            return {"topic_id": thread_id}
        kwargs = {
            "chat_id": normalized_chat,
            "message_thread_id": int(topic_id),
        }
        if action == "archive":
            await bot.close_forum_topic(**kwargs)
        elif action == "reopen":
            await bot.reopen_forum_topic(**kwargs)
        elif action == "delete":
            await bot.delete_forum_topic(**kwargs)
        elif action == "rename":
            await bot.edit_forum_topic(**kwargs, name=name)
        return {}


def _topic_id(args: dict[str, Any]) -> str:
    return str(args.get("topic_id") or _session_value("HERMES_SESSION_THREAD_ID") or "").strip()


async def _handle(args: dict[str, Any], **_kwargs):
    action = str(args.get("action") or "list").strip().lower()
    chat_id = _session_value("HERMES_SESSION_CHAT_ID")
    if _session_value("HERMES_SESSION_PLATFORM") != "telegram" or not chat_id.startswith("-"):
        return tool_result({"success": False, "error": "current session is not a Telegram group"})
    if not _settings().get("enabled"):
        return tool_result({"success": False, "error": "Telegram forum-topic management is disabled"})

    if action == "list":
        return tool_result({"success": True, "topics": list_topics(chat_id)})

    name = str(args.get("name") or "").strip()
    if action == "create":
        if not name or len(name) > 128:
            return tool_result({"success": False, "error": "topic name must be 1-128 characters"})
        try:
            days = int(args.get("auto_archive_days") or _settings()["auto_archive_days"])
        except (TypeError, ValueError):
            days = _settings()["auto_archive_days"]
        opening = str(args.get("opening_message") or f"我们在这里继续讨论：{name}").strip()
        try:
            result = await _telegram_action(
                "create", chat_id, name=name, opening_message=opening
            )
            topic = register_topic(
                chat_id, result["topic_id"], name, auto_archive_days=days
            )
            return tool_result({"success": True, "topic": topic})
        except Exception as exc:
            logger.warning("Telegram forum topic create failed: %s", type(exc).__name__)
            return tool_result({
                "success": False,
                "error": "Telegram rejected topic creation",
                "hint": "Make the bot an administrator with Manage Topics permission.",
            })

    topic_id = _topic_id(args)
    if not topic_id or topic_id == "1":
        return tool_result({"success": False, "error": "a non-General topic_id is required"})
    topic = get_topic(chat_id, topic_id)
    if not topic:
        return tool_result({
            "success": False,
            "error": "topic is not managed by Hermes",
            "hint": "Only topics created through this tool can be changed or deleted.",
        })
    if action == "delete" and args.get("confirm_delete") is not True:
        return tool_result({"success": False, "error": "delete requires confirm_delete=true"})
    if action == "rename" and (not name or len(name) > 128):
        return tool_result({"success": False, "error": "topic name must be 1-128 characters"})
    if action not in {"archive", "reopen", "delete", "rename"}:
        return tool_result({"success": False, "error": "unknown topic action"})

    try:
        await _telegram_action(action, chat_id, topic_id=topic_id, name=name)
        status = {"archive": "archived", "reopen": "open", "delete": "deleted"}.get(action)
        updated = update_topic(
            chat_id, topic_id, status=status, name=name if action == "rename" else None
        )
        return tool_result({"success": True, "topic": updated})
    except Exception as exc:
        logger.warning("Telegram forum topic %s failed: %s", action, type(exc).__name__)
        return tool_result({
            "success": False,
            "error": f"Telegram rejected topic action '{action}'",
            "hint": "Check that the bot still has Manage Topics permission.",
        })


registry.register(
    name="manage_forum_topic",
    toolset="hermes-telegram",
    schema={
        "name": "manage_forum_topic",
        "description": (
            "Manage discussion topics in the CURRENT Telegram forum group. Create a topic only "
            "for a genuinely distinct, substantial issue likely to need multiple turns; do not "
            "create one for every question. New topics receive an opening context message and are "
            "automatically archived after inactivity. List, archive, reopen, or rename only "
            "Hermes-created topics. Delete is irreversible: ask the user first and set "
            "confirm_delete=true only after explicit confirmation."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["create", "list", "archive", "reopen", "rename", "delete"]},
                "name": {"type": "string", "description": "Topic name for create or rename (1-128 chars)."},
                "topic_id": {"type": "string", "description": "Managed topic id; defaults to the current topic."},
                "opening_message": {"type": "string", "description": "Concise context posted into a new topic."},
                "auto_archive_days": {"type": "integer", "minimum": 1, "maximum": 90},
                "confirm_delete": {"type": "boolean", "description": "Must be true after explicit user confirmation."},
            },
            "required": ["action"],
        },
    },
    handler=_handle,
    check_fn=_check_available,
    is_async=True,
    emoji="🗂️",
)
