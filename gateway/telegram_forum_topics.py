"""Persistent lifecycle state for Hermes-managed Telegram forum topics."""

from __future__ import annotations

import json
import os
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

from hermes_cli.config import get_hermes_home


STATE_PATH = get_hermes_home() / "telegram_forum_topics.json"
_LOCK = threading.RLock()
_TOUCH_WRITE_INTERVAL_SECONDS = 60.0


def _load() -> dict[str, Any]:
    try:
        raw = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _save(state: dict[str, Any]) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=str(STATE_PATH.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(state, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, STATE_PATH)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def _key(chat_id: str, topic_id: str) -> str:
    return f"{chat_id}:{topic_id}"


def register_topic(
    chat_id: str,
    topic_id: str,
    name: str,
    *,
    auto_archive_days: int,
    now: float | None = None,
) -> dict[str, Any]:
    stamp = float(now if now is not None else time.time())
    topic = {
        "chat_id": str(chat_id),
        "topic_id": str(topic_id),
        "name": str(name),
        "status": "open",
        "managed_by": "hermes",
        "created_at": stamp,
        "last_activity_at": stamp,
        "auto_archive_days": max(1, min(90, int(auto_archive_days))),
    }
    with _LOCK:
        state = _load()
        state[_key(str(chat_id), str(topic_id))] = topic
        _save(state)
    return dict(topic)


def get_topic(chat_id: str, topic_id: str) -> dict[str, Any] | None:
    with _LOCK:
        raw = _load().get(_key(str(chat_id), str(topic_id)))
    return dict(raw) if isinstance(raw, dict) else None


def list_topics(chat_id: str, *, include_deleted: bool = False) -> list[dict[str, Any]]:
    with _LOCK:
        values = list(_load().values())
    topics = [
        dict(item)
        for item in values
        if isinstance(item, dict)
        and str(item.get("chat_id")) == str(chat_id)
        and (include_deleted or item.get("status") != "deleted")
    ]
    topics.sort(key=lambda item: float(item.get("last_activity_at") or 0), reverse=True)
    return topics


def touch_topic(
    chat_id: str,
    topic_id: str,
    *,
    now: float | None = None,
) -> bool:
    stamp = float(now if now is not None else time.time())
    key = _key(str(chat_id), str(topic_id))
    with _LOCK:
        state = _load()
        topic = state.get(key)
        if not isinstance(topic, dict) or topic.get("status") != "open":
            return False
        prior = float(topic.get("last_activity_at") or 0)
        if stamp - prior < _TOUCH_WRITE_INTERVAL_SECONDS:
            return True
        topic["last_activity_at"] = stamp
        state[key] = topic
        _save(state)
    return True


def update_topic(
    chat_id: str,
    topic_id: str,
    *,
    status: str | None = None,
    name: str | None = None,
    now: float | None = None,
) -> dict[str, Any] | None:
    key = _key(str(chat_id), str(topic_id))
    stamp = float(now if now is not None else time.time())
    with _LOCK:
        state = _load()
        topic = state.get(key)
        if not isinstance(topic, dict):
            return None
        if status is not None:
            topic["status"] = status
            topic[f"{status}_at"] = stamp
        if name is not None:
            topic["name"] = str(name)
        if status == "open":
            topic["last_activity_at"] = stamp
        state[key] = topic
        _save(state)
    return dict(topic)


def due_for_archive(*, now: float | None = None) -> list[dict[str, Any]]:
    stamp = float(now if now is not None else time.time())
    with _LOCK:
        values = list(_load().values())
    due = []
    for item in values:
        if not isinstance(item, dict) or item.get("status") != "open":
            continue
        days = max(1, min(90, int(item.get("auto_archive_days") or 7)))
        last_activity = float(item.get("last_activity_at") or item.get("created_at") or 0)
        if stamp - last_activity >= days * 86400:
            due.append(dict(item))
    return due


async def archive_inactive_topics(bot, *, now: float | None = None) -> dict[str, int]:
    """Close due Hermes-managed topics; failures remain open for the next pass."""
    from plugins.platforms.telegram.telegram_ids import normalize_telegram_chat_id

    archived = 0
    failed = 0
    for topic in due_for_archive(now=now):
        try:
            await bot.close_forum_topic(
                chat_id=normalize_telegram_chat_id(topic["chat_id"]),
                message_thread_id=int(topic["topic_id"]),
            )
            update_topic(topic["chat_id"], topic["topic_id"], status="archived", now=now)
            archived += 1
        except Exception:
            failed += 1
    return {"archived": archived, "failed": failed}
