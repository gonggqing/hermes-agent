"""Telegram-native sticker sending for warm, low-frequency group interaction.

This is deliberately a separate, platform-gated model tool rather than an
extension of the official ``send_message`` transport.  It appears in live
Telegram turns whenever the operator enables stickers in ``config.yaml``.
If the palette is empty, the tool explains how the user can teach the bot by
sending any ordinary sticker once.

Safety is enforced below the prompt: current-group-only routing, a persisted
60–120 minute cooldown, a user-observed file-id allowlist, and no cron access.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

from gateway.sticker_cache import get_sendable_stickers
from hermes_cli.config import get_hermes_home, load_config_readonly
from tools.registry import registry, tool_result

logger = logging.getLogger(__name__)

STATE_PATH = get_hermes_home() / "sticker_send_state.json"
_STATE_LOCK = threading.RLock()
_IN_FLIGHT_CHATS: set[str] = set()
_MIN_COOLDOWN_MINUTES = 60
_MAX_COOLDOWN_MINUTES = 120
_DEFAULT_COOLDOWN_MINUTES = 90

_INTENT_SIGNALS: dict[str, tuple[set[str], tuple[str, ...]]] = {
    "acknowledge": ({"👍", "👌", "✅", "🙏", "🫡"}, ("ok", "nod", "salute", "thumb", "收到")),
    "celebrate": ({"🎉", "🥳", "👏", "🔥", "🎊"}, ("celebr", "cheer", "clap", "party", "庆祝")),
    "encourage": ({"💪", "👏", "👍", "❤️", "🔥"}, ("encour", "support", "fighting", "加油", "鼓励")),
    "thinking": ({"🤔", "🧐", "👀", "🔎"}, ("think", "inspect", "watch", "analyse", "analyze", "思考")),
    "comfort": ({"🤗", "❤️", "🙏", "🥺"}, ("comfort", "hug", "care", "安慰", "抱抱")),
    "goodnight": ({"😴", "🌙", "💤", "🛌"}, ("sleep", "night", "tired", "晚安", "睡")),
    "friendly": ({"😊", "😄", "👋", "✨", "🙂"}, ("smile", "wave", "hello", "happy", "友好")),
}


def _sticker_settings() -> dict[str, Any]:
    try:
        config = load_config_readonly()
    except Exception:
        return {"enabled": False}
    raw = (
        ((config.get("platforms") or {}).get("telegram") or {})
        .get("stickers", {})
    )
    if not isinstance(raw, dict):
        return {"enabled": False}
    try:
        cooldown = int(raw.get("cooldown_minutes", _DEFAULT_COOLDOWN_MINUTES))
    except (TypeError, ValueError):
        cooldown = _DEFAULT_COOLDOWN_MINUTES
    return {
        "enabled": bool(raw.get("enabled", False)),
        "group_only": bool(raw.get("group_only", True)),
        "cooldown_minutes": max(
            _MIN_COOLDOWN_MINUTES, min(_MAX_COOLDOWN_MINUTES, cooldown)
        ),
    }


def _session_value(name: str) -> str:
    try:
        from gateway.session_context import get_session_env

        return str(get_session_env(name, "") or "")
    except Exception:
        return str(os.getenv(name, "") or "")


def _check_send_sticker_available() -> bool:
    """Expose only to configured, interactive Telegram sessions."""
    if os.getenv("HERMES_CRON_SESSION", "").lower() in {"1", "true", "yes"}:
        return False
    if _session_value("HERMES_SESSION_PLATFORM") != "telegram":
        return False
    if not _sticker_settings().get("enabled"):
        return False
    if not os.getenv("TELEGRAM_BOT_TOKEN", "").strip():
        return False
    return True


def _load_state() -> dict[str, Any]:
    try:
        raw = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _save_state(state: dict[str, Any]) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=str(STATE_PATH.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(state, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, STATE_PATH)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def _remaining_cooldown_unlocked(
    chat_id: str, cooldown_minutes: int, now: float
) -> int:
    """Read cooldown state; caller must hold ``_STATE_LOCK``."""
    raw = _load_state().get(chat_id)
    if not isinstance(raw, dict) or not raw.get("sent_at"):
        return 0
    sent_at = float(raw["sent_at"])
    remaining = cooldown_minutes * 60 - (now - sent_at)
    return max(0, int(remaining + 0.999))


def _claim_send(chat_id: str, cooldown_minutes: int, now: float) -> tuple[bool, int]:
    """Atomically reserve one chat so concurrent turns cannot double-send."""
    with _STATE_LOCK:
        if chat_id in _IN_FLIGHT_CHATS:
            return False, 60
        remaining = _remaining_cooldown_unlocked(chat_id, cooldown_minutes, now)
        if remaining:
            return False, remaining
        _IN_FLIGHT_CHATS.add(chat_id)
        return True, 0


def _release_send(chat_id: str) -> None:
    with _STATE_LOCK:
        _IN_FLIGHT_CHATS.discard(chat_id)


def _mark_sent(chat_id: str, sticker: dict[str, Any], intent: str, now: float) -> None:
    with _STATE_LOCK:
        state = _load_state()
        state[chat_id] = {
            "sent_at": now,
            "file_unique_id": sticker.get("file_unique_id", ""),
            "intent": intent,
        }
        _save_state(state)


def _select_sticker(intent: str, palette: list[dict]) -> dict | None:
    emojis, keywords = _INTENT_SIGNALS[intent]
    scored: list[tuple[int, float, str, dict]] = []
    for sticker in palette:
        emoji = str(sticker.get("emoji") or "")
        haystack = " ".join((
            str(sticker.get("description") or ""),
            str(sticker.get("set_name") or ""),
        )).lower()
        score = 4 if emoji in emojis else 0
        score += sum(1 for keyword in keywords if keyword in haystack)
        if score <= 0:
            continue
        received = float(
            sticker.get("last_received_at") or sticker.get("cached_at") or 0
        )
        scored.append((score, received, str(sticker.get("file_unique_id") or ""), sticker))
    if not scored:
        # A user asking for a generic friendly sticker should work immediately
        # after teaching the bot its first ordinary sticker, even when that
        # pack's emoji is not in our small intent map. Semantic intents such as
        # celebrate/comfort/goodnight still fail closed to avoid an awkward or
        # insensitive mismatch.
        if intent == "friendly" and palette:
            return max(
                palette,
                key=lambda item: (
                    float(item.get("last_received_at") or item.get("cached_at") or 0),
                    str(item.get("file_unique_id") or ""),
                ),
            )
        return None
    scored.sort(key=lambda row: (row[0], row[1], row[2]), reverse=True)
    return scored[0][3]


async def _send_native_sticker(chat_id: str, thread_id: str, file_id: str) -> str:
    from telegram import Bot
    from plugins.platforms.telegram.telegram_ids import normalize_telegram_chat_id

    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    bot: Bot
    try:
        from gateway.platforms.base import resolve_proxy_url

        proxy = resolve_proxy_url("TELEGRAM_PROXY", target_hosts=["api.telegram.org"])
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

    kwargs: dict[str, Any] = {}
    if thread_id:
        try:
            from plugins.platforms.telegram.adapter import TelegramAdapter

            effective = TelegramAdapter._message_thread_id_for_send(thread_id)
        except Exception:
            effective = None if thread_id == "1" else int(thread_id)
        if effective is not None:
            kwargs["message_thread_id"] = effective
    sent = await bot.send_sticker(
        chat_id=normalize_telegram_chat_id(chat_id), sticker=file_id, **kwargs
    )
    return str(sent.message_id)


async def _handle_send_sticker(args: dict, **_kwargs):
    settings = _sticker_settings()
    platform = _session_value("HERMES_SESSION_PLATFORM")
    chat_id = _session_value("HERMES_SESSION_CHAT_ID")
    thread_id = _session_value("HERMES_SESSION_THREAD_ID")
    intent = str(args.get("intent") or "friendly").strip().lower()

    if platform != "telegram" or not chat_id:
        return tool_result({"success": False, "error": "current session is not Telegram"})
    if not settings.get("enabled"):
        return tool_result({"success": False, "error": "Telegram stickers are disabled"})
    if settings.get("group_only", True) and not chat_id.startswith("-"):
        return tool_result({"success": False, "error": "stickers are limited to Telegram groups"})
    if intent not in _INTENT_SIGNALS:
        return tool_result({"success": False, "error": "unknown sticker intent"})
    if os.getenv("HERMES_CRON_SESSION", "").lower() in {"1", "true", "yes"}:
        return tool_result({"success": False, "error": "stickers are disabled for scheduled reports"})

    now = time.time()
    claimed, remaining = _claim_send(
        chat_id, int(settings["cooldown_minutes"]), now
    )
    if not claimed:
        return tool_result({
            "success": False,
            "error": "sticker cooldown active",
            "retry_after_minutes": (remaining + 59) // 60,
        })

    try:
        sticker = _select_sticker(intent, get_sendable_stickers())
        if sticker is None:
            return tool_result({
                "success": False,
                "error": f"no approved sticker matches intent '{intent}'",
                "hint": "Ask the user to send the bot a suitable sticker once.",
            })
        try:
            message_id = await _send_native_sticker(
                chat_id, thread_id, str(sticker["file_id"])
            )
        except Exception as exc:
            # Never include Bot API URLs in tool output/logs: they contain the token.
            logger.warning("Telegram sticker send failed: %s", type(exc).__name__)
            return tool_result({
                "success": False, "error": "Telegram rejected the sticker send"
            })

        _mark_sent(chat_id, sticker, intent, now)
        return tool_result({
            "success": True,
            "message_id": message_id,
            "intent": intent,
            "cooldown_minutes": settings["cooldown_minutes"],
        })
    finally:
        _release_send(chat_id)


registry.register(
    name="send_sticker",
    toolset="hermes-telegram",
    schema={
        "name": "send_sticker",
        "description": (
            "Send one native Telegram sticker to the CURRENT GROUP as an occasional warm "
            "social gesture. The sticker comes only from the user's observed allowlist; "
            "the server enforces a 60–120 minute cooldown. Use sparingly after a friendly "
            "acknowledgement, completed research, encouragement, celebration, or natural "
            "sign-off. NEVER use for scheduled/daily reports, market or risk alerts, order "
            "or approval cards, execution/audit messages, outages, losses, or firm "
            "corrections. Do not call merely because the tool is available. Continue with "
            "a normal text answer when the tool declines the send."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "intent": {
                    "type": "string",
                    "enum": list(_INTENT_SIGNALS),
                    "description": "Social meaning used to select an approved sticker.",
                },
            },
            "required": ["intent"],
        },
    },
    handler=_handle_send_sticker,
    check_fn=_check_send_sticker_available,
    is_async=True,
    emoji="🎨",
)
