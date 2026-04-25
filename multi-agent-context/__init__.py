"""multi-agent-context plugin — shared Discord channel/thread history injection.

Wires one behaviour:

1. ``pre_llm_call`` hook — when platform=discord and DISCORD_BOT_TOKEN is set,
   fetches recent messages from the current channel or thread and injects them
   as context into the current turn's user message.

   **Contextvar-aware (v1.8):** Reads ``HERMES_SESSION_THREAD_ID`` then
   ``HERMES_SESSION_CHAT_ID`` from gateway.session_context (propagated via
   ``copy_context()`` into the thread pool before the hook fires). If neither
   is available, injects nothing — no fallback to a configured channel ID.

   This gives agents in a multi-agent channel awareness of what other agents
   and users have said recently, enabling coherent collaborative conversations.

Configuration (environment variables):
    MULTI_AGENT_HISTORY_COUNT — Number of recent messages to fetch (default: 20)
    DISCORD_BOT_TOKEN         — Bot token for API auth (required, already set by Hermes)

No core Hermes files are modified. Survives updates automatically.
"""

from __future__ import annotations

import logging
import os
import re
import time
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

_HISTORY_COUNT: int = 20
_BOT_TOKEN: Optional[str] = None
_SELF_BOT_ID: Optional[str] = None

# Cache: channel_id -> (timestamp, formatted_context)
_cache: Dict[str, Tuple[float, str]] = {}
_CACHE_TTL: float = 10.0


def _load_config() -> bool:
    """Load config from environment. Returns True if properly configured."""
    global _HISTORY_COUNT, _BOT_TOKEN

    _BOT_TOKEN = os.environ.get("DISCORD_BOT_TOKEN", "").strip()

    try:
        _HISTORY_COUNT = int(os.environ.get("MULTI_AGENT_HISTORY_COUNT", "20"))
    except ValueError:
        _HISTORY_COUNT = 20

    if not _BOT_TOKEN:
        logger.debug("multi-agent-context: DISCORD_BOT_TOKEN not set, skipping")
        return False

    return True


def _discord_get(endpoint: str) -> Optional[dict]:
    """Make a GET request to Discord API v10."""
    import requests  # Lazy import — sync hook, sync HTTP

    url = f"https://discord.com/api/v10/{endpoint}"
    headers = {
        "Authorization": f"Bot {_BOT_TOKEN}",
        "User-Agent": "HermesMultiAgentContext/1.8",
    }
    try:
        r = requests.get(url, headers=headers, timeout=5)
        if r.status_code == 200:
            return r.json()
        elif r.status_code == 429:
            retry_after = float(r.headers.get("Retry-After", "1"))
            time.sleep(min(retry_after, 2))
            r2 = requests.get(url, headers=headers, timeout=5)
            if r2.status_code == 200:
                return r2.json()
            logger.warning(
                "multi-agent-context: Discord API %s returned %d after retry",
                endpoint, r2.status_code,
            )
        else:
            logger.warning(
                "multi-agent-context: Discord API %s returned %d",
                endpoint, r.status_code,
            )
    except Exception as exc:
        logger.warning("multi-agent-context: Discord API request failed: %s", exc)
    return None


def _get_bot_user_id() -> Optional[str]:
    """Fetch and cache the bot's own user ID from Discord API."""
    global _SELF_BOT_ID
    if _SELF_BOT_ID:
        return _SELF_BOT_ID

    try:
        resp = _discord_get("users/@me")
        if resp and resp.get("id"):
            _SELF_BOT_ID = str(resp["id"])
            logger.debug("multi-agent-context: bot user_id=%s", _SELF_BOT_ID)
            return _SELF_BOT_ID
    except Exception as exc:
        logger.warning("multi-agent-context: failed to get bot user_id: %s", exc)
    return None


def _resolve_target(**kwargs) -> Tuple[Optional[str], bool]:
    """Resolve target channel/thread ID from gateway session contextvars.

    Reads HERMES_SESSION_THREAD_ID first (thread wins), then falls back to
    HERMES_SESSION_CHAT_ID (plain channel). Returns (None, False) with no
    further fallback if neither is set.

    Returns: (target_id, is_thread)
    """
    try:
        from gateway.session_context import get_session_env

        thread_id = get_session_env("HERMES_SESSION_THREAD_ID")
        if thread_id:
            logger.debug(
                "multi-agent-context: resolved thread id=%s from HERMES_SESSION_THREAD_ID",
                thread_id,
            )
            return thread_id, True

        chat_id = get_session_env("HERMES_SESSION_CHAT_ID")
        if chat_id:
            logger.debug(
                "multi-agent-context: resolved channel id=%s from HERMES_SESSION_CHAT_ID",
                chat_id,
            )
            return chat_id, False

        logger.debug(
            "multi-agent-context: neither HERMES_SESSION_THREAD_ID nor "
            "HERMES_SESSION_CHAT_ID set — injecting nothing"
        )
    except ImportError:
        logger.debug(
            "multi-agent-context: gateway.session_context not importable — injecting nothing"
        )

    return None, False


def _fetch_channel_history(channel_id: str, limit: int) -> List[dict]:
    """Fetch recent messages from a Discord channel or thread."""
    data = _discord_get(f"channels/{channel_id}/messages?limit={limit}")
    if isinstance(data, list):
        return data
    return []


def _format_messages(
    messages: List[dict], self_bot_id: Optional[str], label: str = "Channel"
) -> str:
    """Format Discord messages into a clean context block."""
    lines: List[str] = []
    lines.append(f"[Recent {label} History]")
    lines.append("")

    for msg in reversed(messages):  # Discord returns newest-first
        author = msg.get("author", {})
        author_id = str(author.get("id", ""))
        content = msg.get("content", "").strip()

        if author_id == self_bot_id:
            continue
        if not content or msg.get("type", 0) > 3:
            continue

        display = (
            author.get("global_name")
            or author.get("username")
            or f"User-{author_id[:6]}"
        )

        content = re.sub(r"<@!?(\d+)>", r"@<\1>", content)
        content = re.sub(r"<@&(\d+)>", r"@<role:\1>", content)
        content = re.sub(r"<#(\d+)>", r"#<\1>", content)

        if len(content) > 500:
            content = content[:497] + "..."

        lines.append(f"**{display}**: {content}")

    if len(lines) <= 2:
        return ""

    lines.append("")
    lines.append(f"[End {label} History]")
    return "\n".join(lines)


def _inject_channel_context(**kwargs) -> Optional[dict[str, str]]:
    """pre_llm_call hook callback."""
    platform = kwargs.get("platform", "")
    if platform != "discord":
        return None

    if not _load_config():
        return None

    target_id, is_thread = _resolve_target(**kwargs)
    if not target_id:
        return None

    label = "Thread" if is_thread else "Channel"

    # Check cache
    now = time.time()
    cached = _cache.get(target_id)
    if cached and (now - cached[0]) < _CACHE_TTL:
        ctx_text = cached[1]
        if ctx_text:
            return {"context": ctx_text}
        return None

    bot_id = _get_bot_user_id()
    messages = _fetch_channel_history(target_id, _HISTORY_COUNT)
    ctx_text = _format_messages(messages, bot_id, label=label)
    _cache[target_id] = (now, ctx_text)

    if ctx_text:
        logger.info(
            "multi-agent-context: injected %d chars of %s %s history (sender: %s)",
            len(ctx_text), label.lower(), target_id,
            kwargs.get("sender_id", "?"),
        )
        return {"context": ctx_text}

    return None


# ---------------------------------------------------------------------------
# Plugin registration
# ---------------------------------------------------------------------------


def register(ctx) -> None:
    """Register the pre_llm_call hook with Hermes."""
    ctx.register_hook("pre_llm_call", _inject_channel_context)
    logger.info(
        "multi-agent-context plugin v1.8 registered "
        "(history_count=%d, resolution=contextvar)",
        int(os.environ.get("MULTI_AGENT_HISTORY_COUNT", "20")),
    )
