"""
Sticker description cache for Telegram.

When users send stickers, we describe them via the vision tool and cache
the descriptions keyed by file_unique_id so we don't re-analyze the same
sticker image on every send. Descriptions are concise (1-2 sentences).

Cache location: ~/.hermes/sticker_cache.json
"""

import json
import os
import tempfile
import threading
import time
from typing import Optional

from hermes_cli.config import get_hermes_home


CACHE_PATH = get_hermes_home() / "sticker_cache.json"
_CACHE_LOCK = threading.RLock()

# Vision prompt for describing stickers -- kept concise to save tokens
STICKER_VISION_PROMPT = (
    "Describe this sticker in 1-2 sentences. Focus on what it depicts -- "
    "character, action, emotion. Be concise and objective."
)


def _load_cache() -> dict:
    """Load the sticker cache from disk."""
    if CACHE_PATH.exists():
        try:
            return json.loads(CACHE_PATH.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def _save_cache(cache: dict) -> None:
    """Save the sticker cache to disk atomically."""
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(
        dir=str(CACHE_PATH.parent), suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(cache, f, indent=2, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, str(CACHE_PATH))
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def get_cached_description(file_unique_id: str) -> Optional[dict]:
    """
    Look up a cached sticker description.

    Returns:
        dict with keys {description, emoji, set_name, cached_at} or None.
    """
    cache = _load_cache()
    return cache.get(file_unique_id)


def cache_sticker_metadata(
    file_unique_id: str,
    file_id: str,
    emoji: str = "",
    set_name: str = "",
    *,
    is_animated: bool = False,
    is_video: bool = False,
) -> None:
    """Remember a user-supplied Telegram sticker as an approved send palette item.

    ``file_id`` is reusable by ``sendSticker`` while ``file_unique_id`` is only
    suitable as a stable cache key.  We intentionally learn stickers only from
    messages received by the allowlisted bot; the model cannot inject arbitrary
    remote sticker identifiers into the outbound tool.
    """
    if not file_unique_id or not file_id:
        return
    with _CACHE_LOCK:
        cache = _load_cache()
        entry = dict(cache.get(file_unique_id) or {})
        entry.update({
            "file_id": file_id,
            "emoji": emoji,
            "set_name": set_name,
            "is_animated": bool(is_animated),
            "is_video": bool(is_video),
            "last_received_at": time.time(),
        })
        cache[file_unique_id] = entry
        _save_cache(cache)


def get_sendable_stickers() -> list[dict]:
    """Return only user-observed stickers that Telegram can send again."""
    with _CACHE_LOCK:
        cache = _load_cache()
    out = []
    for file_unique_id, raw in cache.items():
        if not isinstance(raw, dict) or not raw.get("file_id"):
            continue
        out.append({"file_unique_id": file_unique_id, **raw})
    out.sort(
        key=lambda item: (
            float(item.get("last_received_at") or item.get("cached_at") or 0),
            item["file_unique_id"],
        ),
        reverse=True,
    )
    return out


def cache_sticker_description(
    file_unique_id: str,
    description: str,
    emoji: str = "",
    set_name: str = "",
) -> None:
    """
    Store a sticker description in the cache.

    Args:
        file_unique_id: Telegram's stable sticker identifier.
        description:    Vision-generated description text.
        emoji:          Associated emoji (e.g. "😀").
        set_name:       Sticker set name if available.
    """
    with _CACHE_LOCK:
        cache = _load_cache()
        entry = dict(cache.get(file_unique_id) or {})
        entry.update({
            "description": description,
            "emoji": emoji,
            "set_name": set_name,
            "cached_at": time.time(),
        })
        cache[file_unique_id] = entry
        _save_cache(cache)


def build_sticker_injection(
    description: str,
    emoji: str = "",
    set_name: str = "",
) -> str:
    """
    Build the warm-style injection text for a sticker description.

    Returns a string like:
      [The user sent a sticker 😀 from "MyPack"~ It shows: "A cat waving" (=^.w.^=)]
    """
    context = ""
    if set_name and emoji:
        context = f" {emoji} from \"{set_name}\""
    elif emoji:
        context = f" {emoji}"

    return f"[The user sent a sticker{context}~ It shows: \"{description}\" (=^.w.^=)]"


def build_animated_sticker_injection(emoji: str = "") -> str:
    """
    Build injection text for animated/video stickers we can't analyze.
    """
    if emoji:
        return (
            f"[The user sent an animated sticker {emoji}~ "
            f"I can't see animated ones yet, but the emoji suggests: {emoji}]"
        )
    return "[The user sent an animated sticker~ I can't see animated ones yet]"
