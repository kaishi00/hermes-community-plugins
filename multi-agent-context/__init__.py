"""multi-agent-context plugin — shared channel/thread history injection.

Wires two behaviours:

1. ``post_llm_call`` hook — for Telegram: after every turn, writes the
   triggering user message and the bot's response to a shared SQLite database
   on disk.  Because multiple Hermes processes share the same file, each bot
   instance can see what other instances said without any API calls.

   The Telegram Bot API has no message-history endpoint, and the in-process
   buffer approach (pre_gateway_dispatch) doesn't work across processes.
   SQLite WAL mode makes concurrent reads/writes safe.

2. ``pre_llm_call`` hook — injects recent channel/thread history as context:
   - platform=discord: fetches history via Discord REST API (unchanged).
   - platform=telegram: reads from the shared SQLite DB written by hook #1.

Configuration (environment variables):
    MULTI_AGENT_HISTORY_COUNT  — Messages to keep per chat in context (default: 20)
    MULTI_AGENT_BOT_NAME       — Display name for this bot in the shared history.
                                  Defaults to the agent name from the session key.
    MULTI_AGENT_TG_DB_PATH     — Override default SQLite DB path.
    DISCORD_BOT_TOKEN          — Discord Bot token (already set by Hermes).

No core Hermes files are modified.  Survives updates automatically.
"""

from __future__ import annotations

import logging
import os
import re
import time
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Shared config helpers
# ---------------------------------------------------------------------------

_BOT_TOKEN: Optional[str] = None  # Discord bot token
_SELF_BOT_ID: Optional[str] = None

# Discord cache: channel_id -> (timestamp, formatted_context)
_discord_cache: Dict[str, Tuple[float, str]] = {}
_CACHE_TTL: float = 10.0

_TG_DB_DEFAULT = "/root/.hermes/data/multi_agent_tg_shared.db"
_TG_DB_TTL_HOURS: float = 48.0  # Prune messages older than this


def _history_count() -> int:
    try:
        return int(os.environ.get("MULTI_AGENT_HISTORY_COUNT", "20"))
    except ValueError:
        return 20


# ---------------------------------------------------------------------------
# Discord helpers (v1.8 behaviour, unchanged)
# ---------------------------------------------------------------------------

def _load_discord_config() -> bool:
    global _BOT_TOKEN
    _BOT_TOKEN = os.environ.get("DISCORD_BOT_TOKEN", "").strip()
    if not _BOT_TOKEN:
        logger.debug("multi-agent-context: DISCORD_BOT_TOKEN not set, skipping Discord path")
        return False
    return True


def _discord_get(endpoint: str) -> Optional[dict]:
    import requests
    url = f"https://discord.com/api/v10/{endpoint}"
    headers = {
        "Authorization": f"Bot {_BOT_TOKEN}",
        "User-Agent": "HermesMultiAgentContext/2.0",
    }
    try:
        r = requests.get(url, headers=headers, timeout=5)
        if r.status_code == 200:
            return r.json()
        if r.status_code == 429:
            retry_after = float(r.headers.get("Retry-After", "1"))
            time.sleep(min(retry_after, 2))
            r2 = requests.get(url, headers=headers, timeout=5)
            if r2.status_code == 200:
                return r2.json()
            logger.warning("multi-agent-context: Discord API %s → %d after retry", endpoint, r2.status_code)
        else:
            logger.warning("multi-agent-context: Discord API %s → %d", endpoint, r.status_code)
    except Exception as exc:
        logger.warning("multi-agent-context: Discord API request failed: %s", exc)
    return None


def _get_discord_bot_user_id() -> Optional[str]:
    global _SELF_BOT_ID
    if _SELF_BOT_ID:
        return _SELF_BOT_ID
    try:
        resp = _discord_get("users/@me")
        if resp and resp.get("id"):
            _SELF_BOT_ID = str(resp["id"])
            return _SELF_BOT_ID
    except Exception as exc:
        logger.warning("multi-agent-context: failed to get Discord bot user_id: %s", exc)
    return None


def _resolve_target(**kwargs) -> Tuple[Optional[str], bool]:
    """Read HERMES_SESSION_THREAD_ID then HERMES_SESSION_CHAT_ID from contextvars."""
    try:
        from gateway.session_context import get_session_env
        thread_id = get_session_env("HERMES_SESSION_THREAD_ID")
        if thread_id:
            return thread_id, True
        chat_id = get_session_env("HERMES_SESSION_CHAT_ID")
        if chat_id:
            return chat_id, False
    except ImportError:
        pass
    return None, False


def _format_discord_messages(messages: List[dict], self_bot_id: Optional[str], label: str) -> str:
    lines: List[str] = [f"[Recent {label} History]", ""]
    for msg in reversed(messages):
        author = msg.get("author", {})
        author_id = str(author.get("id", ""))
        content = msg.get("content", "").strip()
        if author_id == self_bot_id or not content or msg.get("type", 0) > 3:
            continue
        display = author.get("global_name") or author.get("username") or f"User-{author_id[:6]}"
        content = re.sub(r"<@!?(\d+)>", r"@<\1>", content)
        content = re.sub(r"<@&(\d+)>", r"@<role:\1>", content)
        content = re.sub(r"<#(\d+)>", r"#<\1>", content)
        if len(content) > 500:
            content = content[:497] + "..."
        lines.append(f"**{display}**: {content}")
    if len(lines) <= 2:
        return ""
    lines.extend(["", f"[End {label} History]"])
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Telegram shared SQLite helpers
# ---------------------------------------------------------------------------

def _tg_db_path() -> str:
    return os.environ.get("MULTI_AGENT_TG_DB_PATH", _TG_DB_DEFAULT).strip()


def _tg_open_db():
    """Open (and initialise if needed) the shared SQLite DB."""
    import sqlite3
    conn = sqlite3.connect(_tg_db_path(), timeout=10, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS messages (
            id    INTEGER PRIMARY KEY AUTOINCREMENT,
            ts    REAL    NOT NULL,
            chat_key TEXT NOT NULL,
            sender   TEXT NOT NULL,
            text     TEXT NOT NULL
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_ck_ts ON messages (chat_key, ts)")
    conn.commit()
    return conn


def _tg_chat_key(session_id: str) -> str:
    """Derive a stable chat_key from a Hermes session_id.

    Session keys: ``agent:main:{platform}:{chat_type}:{chat_id}[:{thread_id}]``
    thread_id is only reliable for dm/thread chat types.

    Falls back to HERMES_SESSION_CHAT_ID / HERMES_SESSION_THREAD_ID
    context vars when the session_id is in short format (e.g. ``20260508_194909_uuid``).
    """
    parts = session_id.split(":")
    if len(parts) >= 5:
        chat_type = parts[3]
        chat_id = parts[4]
        if len(parts) > 5 and chat_type in ("dm", "thread"):
            return f"{chat_id}:{parts[5]}"
        return chat_id

    # Short session ID format — try session context vars
    try:
        from gateway.session_context import get_session_env
        thread_id = get_session_env("HERMES_SESSION_THREAD_ID")
        if thread_id:
            return str(thread_id)
        chat_id = get_session_env("HERMES_SESSION_CHAT_ID")
        if chat_id:
            return str(chat_id)
    except ImportError:
        pass

    return session_id


def _tg_bot_name(session_id: str) -> str:
    """Derive a display name for this bot.

    Priority:
    1. MULTI_AGENT_BOT_NAME env var (explicit override)
    2. Active profile name via HERMES_HOME (e.g. "zhongli", "raiden", "nahida")
    3. Fallback to "Bot"
    Note: session_id parts[1] is always the hardcoded string "main" and is useless here.
    """
    name = os.environ.get("MULTI_AGENT_BOT_NAME", "").strip()
    if name:
        return name
    try:
        from hermes_cli.profiles import get_active_profile_name
        profile = get_active_profile_name()
        if profile and profile != "default":
            return profile
    except Exception:
        pass
    return "Bot"


def _tg_write(session_id: str, user_message: str, assistant_response: str) -> None:
    """Write the user turn + bot response to the shared DB."""
    try:
        chat_key = _tg_chat_key(session_id)
        bot_name = _tg_bot_name(session_id)
        now = time.time()

        conn = _tg_open_db()
        with conn:
            if user_message and user_message.strip():
                conn.execute(
                    "INSERT INTO messages (ts, chat_key, sender, text) VALUES (?,?,?,?)",
                    (now, chat_key, "user", user_message.strip()[:1000]),
                )
            if assistant_response and assistant_response.strip():
                conn.execute(
                    "INSERT INTO messages (ts, chat_key, sender, text) VALUES (?,?,?,?)",
                    (now + 0.001, chat_key, bot_name, assistant_response.strip()[:1000]),
                )
            # Prune messages older than TTL
            cutoff = now - _TG_DB_TTL_HOURS * 3600
            conn.execute("DELETE FROM messages WHERE ts < ?", (cutoff,))
        conn.close()

        logger.debug(
            "multi-agent-context: [tg] wrote turn to DB chat_key=%s bot=%s",
            chat_key, bot_name,
        )
    except Exception as exc:
        logger.warning("multi-agent-context: [tg] DB write failed: %s", exc)


def _tg_read(session_id: str) -> str:
    """Read recent messages from shared DB and format as context block."""
    try:
        chat_key = _tg_chat_key(session_id)
        limit = _history_count()

        conn = _tg_open_db()
        rows = conn.execute(
            "SELECT sender, text FROM ("
            "  SELECT sender, text, ts FROM messages WHERE chat_key=?"
            "  ORDER BY ts DESC LIMIT ?"
            ") ORDER BY ts ASC",
            (chat_key, limit),
        ).fetchall()
        conn.close()

        if not rows:
            return ""

        lines = ["[Recent Group History]", ""]
        for sender, text in rows:
            if len(text) > 500:
                text = text[:497] + "..."
            lines.append(f"**{sender}**: {text}")
        lines.extend(["", "[End Group History]"])
        return "\n".join(lines)
    except Exception as exc:
        logger.warning("multi-agent-context: [tg] DB read failed: %s", exc)
        return ""


# ---------------------------------------------------------------------------
# Hook callbacks
# ---------------------------------------------------------------------------

def _record_telegram_turn(**kwargs) -> None:
    """post_llm_call hook — persists the turn to shared SQLite."""
    platform = kwargs.get("platform", "")
    if platform != "telegram":
        return

    session_id = kwargs.get("session_id", "")
    user_message = kwargs.get("user_message", "") or ""
    assistant_response = kwargs.get("assistant_response", "") or ""

    if not session_id:
        return

    _tg_write(session_id, user_message, assistant_response)


def _inject_channel_context(**kwargs) -> Optional[dict]:
    """pre_llm_call hook — injects shared channel history as context."""
    platform = kwargs.get("platform", "")

    if platform == "discord":
        if not _load_discord_config():
            return None

        target_id, is_thread = _resolve_target(**kwargs)
        if not target_id:
            return None

        label = "Thread" if is_thread else "Channel"
        now = time.time()
        cached = _discord_cache.get(target_id)
        if cached and (now - cached[0]) < _CACHE_TTL:
            ctx_text = cached[1]
            return {"context": ctx_text} if ctx_text else None

        bot_id = _get_discord_bot_user_id()
        messages = _discord_get(f"channels/{target_id}/messages?limit={_history_count()}")
        msgs = messages if isinstance(messages, list) else []
        ctx_text = _format_discord_messages(msgs, bot_id, label)
        _discord_cache[target_id] = (now, ctx_text)
        if ctx_text:
            logger.info(
                "multi-agent-context: [discord] injected %d chars of %s %s history",
                len(ctx_text), label.lower(), target_id,
            )
            return {"context": ctx_text}
        return None

    if platform == "telegram":
        session_id = kwargs.get("session_id", "")
        if not session_id:
            return None

        ctx_text = _tg_read(session_id)
        if ctx_text:
            logger.info(
                "multi-agent-context: [tg] injected %d chars of history (session=%s)",
                len(ctx_text), session_id,
            )
            return {"context": ctx_text}
        return None

    return None


# ---------------------------------------------------------------------------
# Plugin registration
# ---------------------------------------------------------------------------

def register(ctx) -> None:
    ctx.register_hook("post_llm_call", _record_telegram_turn)
    ctx.register_hook("pre_llm_call", _inject_channel_context)
    logger.info(
        "multi-agent-context plugin v2.0 registered "
        "(history_count=%d, platforms=discord+telegram, tg_db=%s)",
        _history_count(), _tg_db_path(),
    )
