"""kanban-context — injects Kanban activity + cross-bot messaging for agents.

Two integrated features:

FEATURE 1: Kanban Activity Injection
-------------------------------------
Reads recent task_events from all kanban boards and injects them as a
``[Recent Kanban Activity]`` context block before each LLM call.

FEATURE 2: Cross-Bot Messaging
-------------------------------
Because Telegram bots cannot see messages from other bots (a hard Telegram
API limitation), this plugin implements a **cross-bot message bus** using
a shared SQLite ``outbox`` table.

HOW IT WORKS
------------
1. Bot A (sender) calls ``crossbot_send()`` with the target bot name and
   message body.  This:
   a) Writes a row to the shared ``outbox`` table (pending status)
   b) Creates a Kanban task assigned to the target bot for tracking

2. Bot B (receiver) discovers the message in one of two ways:
   - **Kanban dispatcher** picks up the new task and spawns a worker
   - **pre_llm_call hook** reads the outbox and injects pending messages
     as ``[Pending Messages]`` context

3. Bot B processes the message by calling ``crossbot_respond()``, which:
   a) Marks the outbox row as ``done``
   b) Records the response text
   c) Completes the Kanban task with a summary

This gives full transparency: every cross-bot exchange is tracked both
in the shared SQLite outbox and in the Kanban board.

Configuration via environment variables
-----------------------------------------
    KANBAN_CONTEXT_EVENT_LIMIT   — Max events to inject (default: 10)
    KANBAN_CONTEXT_LOOKBACK_H    — Lookback window in hours (default: 12)
    MULTI_AGENT_TG_DB_PATH       — Shared SQLite DB path (from multi-agent-context)
    CROSSBOT_BOT_NAME            — This bot's name for outbox addressing
                                   (default: HERMES profile name or "bot")
"""

from __future__ import annotations

import functools
import json
import logging
import os
import sqlite3
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Path resolution
# ---------------------------------------------------------------------------

_HERMES_HOME: Optional[Path] = None


def _hermes_home() -> Path:
    global _HERMES_HOME
    if _HERMES_HOME is None:
        try:
            from hermes_constants import get_hermes_home
            _HERMES_HOME = Path(get_hermes_home())
        except ImportError:
            _HERMES_HOME = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))
    return _HERMES_HOME


def _kanban_db() -> Path:
    return _hermes_home() / "kanban.db"


def _boards_dir() -> Path:
    return _hermes_home() / "kanban" / "boards"


def _shared_db_path() -> str:
    """Path to the shared multi-agent SQLite DB (from multi-agent-context)."""
    return os.environ.get(
        "MULTI_AGENT_TG_DB_PATH",
        str(_hermes_home() / "data" / "multi_agent_tg_shared.db"),
    ).strip()


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


@functools.lru_cache(maxsize=1)
def _event_limit() -> int:
    try:
        return int(os.environ.get("KANBAN_CONTEXT_EVENT_LIMIT", "10"))
    except (ValueError, TypeError):
        return 10


@functools.lru_cache(maxsize=1)
def _lookback_hours() -> int:
    try:
        return int(os.environ.get("KANBAN_CONTEXT_LOOKBACK_H", "12"))
    except (ValueError, TypeError):
        return 12


def _my_bot_name() -> str:
    """Return this bot's display name for outbox addressing."""
    name = os.environ.get("CROSSBOT_BOT_NAME", "").strip()
    if name:
        return name
    try:
        from hermes_cli.profiles import get_active_profile_name
        profile = get_active_profile_name()
        if profile and profile != "default":
            return profile
    except Exception:
        pass
    return os.environ.get("MULTI_AGENT_BOT_NAME", "bot")


def _clear_config_cache() -> None:
    _event_limit.cache_clear()
    _lookback_hours.cache_clear()


# ---------------------------------------------------------------------------
# Shared outbox DB — cross-bot message bus
# ---------------------------------------------------------------------------

_OUTBOX_TABLE = """\
    CREATE TABLE IF NOT EXISTS outbox (
        id             INTEGER PRIMARY KEY AUTOINCREMENT,
        ts             REAL    NOT NULL,
        from_bot       TEXT    NOT NULL,
        to_bot         TEXT    NOT NULL,
        subject        TEXT    DEFAULT '',
        body           TEXT    NOT NULL,
        kanban_task_id TEXT    DEFAULT '',
        status         TEXT    DEFAULT 'pending',  -- pending | delivered | done
        response_text  TEXT    DEFAULT '',
        completed_at   REAL    DEFAULT NULL
    )
"""

_RESPONSE_LOG_TABLE = """\
    CREATE TABLE IF NOT EXISTS response_log (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        ts            REAL    NOT NULL,
        bot_name      TEXT    NOT NULL,
        chat_key      TEXT    NOT NULL,
        user_message  TEXT    NOT NULL,
        user_id       TEXT    DEFAULT '',
        responded     INTEGER DEFAULT 0,  -- 0=pending, 1=responded
        responder     TEXT    DEFAULT ''
    )
"""
_CREATE_INDEX_RL = """\
    CREATE INDEX IF NOT EXISTS idx_rl_chat_msg 
    ON response_log (chat_key, user_message)
"""


def _open_shared_db():
    """Open the shared multi-agent DB, ensuring outbox + response_log tables exist."""
    path = _shared_db_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    conn = sqlite3.connect(path, timeout=10, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute(_OUTBOX_TABLE)
    conn.execute(_RESPONSE_LOG_TABLE)
    conn.execute(_CREATE_INDEX_RL)
    conn.commit()
    return conn


def crossbot_send(
    to_bot: str,
    subject: str,
    body: str,
    kanban_task_id: str = "",
) -> int:
    """Send a cross-bot message via the shared outbox.

    Args:
        to_bot: Target bot profile name (e.g. 'profile_name')
        subject: Short message subject/headline
        body: Full message body
        kanban_task_id: Optional Kanban task ID for tracking

    Returns:
        The outbox row ID.
    """
    conn = _open_shared_db()
    now = time.time()
    from_bot = _my_bot_name()
    try:
        with conn:
            cur = conn.execute(
                "INSERT INTO outbox (ts, from_bot, to_bot, subject, body, kanban_task_id, status) "
                "VALUES (?, ?, ?, ?, ?, ?, 'pending')",
                (now, from_bot, to_bot, subject[:200], body, kanban_task_id),
            )
            row_id = cur.lastrowid
        logger.info(
            "crossbot: sent message #%d from '%s' to '%s' (subject='%s', kanban=%s)",
            row_id, from_bot, to_bot, subject[:60], kanban_task_id or "none",
        )
        return row_id
    finally:
        conn.close()


def crossbot_respond(outbox_id: int, response_text: str) -> bool:
    """Mark a message as done with the response text.

    Args:
        outbox_id: The outbox row ID from crossbot_send()
        response_text: The response/reply content

    Returns:
        True if successful, False if message not found.
    """
    conn = _open_shared_db()
    now = time.time()
    try:
        cur = conn.execute(
            "UPDATE outbox SET status='done', response_text=?, completed_at=? WHERE id=?",
            (response_text[:2000], now, outbox_id),
        )
        conn.commit()
        if cur.rowcount == 0:
            logger.warning("crossbot: message #%d not found", outbox_id)
            return False
        logger.info("crossbot: message #%d responded (%d chars)", outbox_id, len(response_text))
        return True
    finally:
        conn.close()


def _fetch_pending_messages(for_bot: Optional[str] = None) -> List[Dict[str, Any]]:
    """Fetch all pending (undelivered) messages for *for_bot*.

    If *for_bot* is None, uses the current bot name.
    """
    target = for_bot or _my_bot_name()
    conn = _open_shared_db()
    try:
        rows = conn.execute(
            "SELECT id, from_bot, subject, body, ts, kanban_task_id "
            "FROM outbox "
            "WHERE to_bot=? AND status='pending' "
            "ORDER BY ts ASC",
            (target,),
        ).fetchall()
        results = []
        for r in rows:
            results.append({
                "id": r[0],
                "from_bot": r[1],
                "subject": r[2] or "",
                "body": r[3],
                "ts": r[4],
                "kanban_task_id": r[5] or "",
            })
        return results
    finally:
        conn.close()


def crossbot_get_history(
    for_bot: Optional[str] = None,
    limit: int = 20,
) -> List[Dict[str, Any]]:
    """Get recent cross-bot message history for the given bot."""
    target = for_bot or _my_bot_name()
    conn = _open_shared_db()
    try:
        rows = conn.execute(
            "SELECT id, from_bot, to_bot, subject, body, status, response_text, ts, completed_at "
            "FROM outbox "
            "WHERE from_bot=? OR to_bot=? "
            "ORDER BY ts DESC LIMIT ?",
            (target, target, limit),
        ).fetchall()
        results = []
        for r in rows:
            results.append({
                "id": r[0],
                "from_bot": r[1],
                "to_bot": r[2],
                "subject": r[3] or "",
                "body": r[4],
                "status": r[5],
                "response_text": r[6] or "",
                "ts": r[7],
                "completed_at": r[8],
            })
        return results
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Kanban board reading
# ---------------------------------------------------------------------------


def _iter_boards() -> List[Tuple[str, str]]:
    """Yield (db_path, board_label) pairs for all available kanban boards."""
    results: List[Tuple[str, str]] = []
    default = _kanban_db()
    if default.is_file():
        results.append((str(default), "kanban"))
    boards = _boards_dir()
    if boards.is_dir():
        for name in sorted(os.listdir(str(boards))):
            board_db = boards / name / "kanban.db"
            if board_db.is_file():
                results.append((str(board_db), name))
    return results


def _read_kanban_events() -> str:
    """Read recent task_events from all kanban boards and format as context."""
    cutoff = time.time() - _lookback_hours() * 3600
    limit = _event_limit()
    events: List[Dict[str, Any]] = []

    for db_path, board_label in _iter_boards():
        try:
            with sqlite3.connect(db_path, timeout=5) as conn:
                rows = conn.execute(
                    """
                    SELECT e.id, e.task_id, e.kind, e.payload, e.created_at,
                           t.title, t.status
                    FROM task_events e
                    LEFT JOIN tasks t ON t.id = e.task_id
                    WHERE e.created_at >= ?
                    ORDER BY e.created_at DESC
                    LIMIT ?
                    """,
                    (cutoff, limit),
                ).fetchall()
            for row in rows:
                _eid, task_id, kind, payload_json, created_at, title, task_status = row
                payload: Dict[str, Any] = {}
                if payload_json:
                    try:
                        payload = json.loads(payload_json)
                    except (json.JSONDecodeError, TypeError):
                        payload = {}
                events.append({
                    "board": board_label,
                    "task_id": task_id,
                    "kind": kind,
                    "payload": payload,
                    "ts": created_at,
                    "title": title or task_id[:16],
                    "task_status": task_status,
                })
        except Exception as exc:
            logger.warning(
                "kanban-context: error reading board '%s' (%s): %s",
                board_label, db_path, exc,
            )

    if not events:
        logger.debug(
            "kanban-context: no recent events (lookback=%dh, boards=%d)",
            _lookback_hours(), len(_iter_boards()),
        )
        return ""

    events.sort(key=lambda e: e["ts"], reverse=True)
    events = events[:limit]
    events.reverse()

    lines = ["[Recent Kanban Activity]", ""]
    for ev in events:
        when = _fmt_time(ev["ts"])
        title = ev["title"][:60]
        kind = ev["kind"]
        board = ev["board"]
        task_status = ev["task_status"] or "?"
        desc = _describe_event(kind, ev["payload"], task_status)
        lines.append(f"- [{when}] [{board}] **{title}** ({desc})")
    lines.extend(["", "[End Kanban Activity]"])
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Pending cross-bot messages
# ---------------------------------------------------------------------------


def _read_pending_messages() -> str:
    """Read pending outbox messages for this bot and format as context."""
    pending = _fetch_pending_messages()
    if not pending:
        return ""

    lines = ["[Pending Messages]", ""]
    for msg in pending:
        when = _fmt_time(msg["ts"])
        subj = msg["subject"] or "(no subject)"
        body = msg["body"][:200]
        if len(msg["body"]) > 200:
            body += "..."
        task_ref = f" (kanban: {msg['kanban_task_id']})" if msg["kanban_task_id"] else ""
        lines.append(f"- [{when}] From **{msg['from_bot']}** — {subj}{task_ref}")
        lines.append(f"  > {body}")
    lines.extend(["", "To respond, process the linked Kanban task and call crossbot_respond().", ""])
    lines.append("[End Pending Messages]")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------


def _fmt_time(ts: float) -> str:
    elapsed = time.time() - ts
    if elapsed < 0:
        return "just now"
    if elapsed < 60:
        return "just now"
    if elapsed < 3600:
        return f"{int(elapsed // 60)}m ago"
    if elapsed < 86400:
        return f"{int(elapsed // 3600)}h ago"
    return f"{int(elapsed // 86400)}d ago"


def _describe_event(kind: str, payload: Dict[str, Any], task_status: str) -> str:
    descriptions = {
        "created": f"created → {payload.get('status', 'triage')}",
        "assigned": f"assigned to {payload.get('assignee', 'someone')}",
        "claimed": "claimed by worker",
        "completed": "completed",
        "blocked": _trunc(f"blocked: {payload.get('reason', '')}", 60),
        "unblocked": "unblocked",
        "heartbeat": _trunc(f"in progress: {payload.get('note', '')}", 60),
        "spawned": "worker spawned",
        "archived": "archived",
        "commented": f"comment by {payload.get('author', 'someone')}",
        "linked": _trunc(
            f"linked to parent={payload.get('parent', '')[:12]} "
            f"child={payload.get('child', '')[:12]}",
            60,
        ),
        "edited": "edited",
        "promoted": f"promoted → {task_status}",
    }
    return descriptions.get(kind, kind)


def _trunc(text: str, max_len: int) -> str:
    if len(text) > max_len:
        return text[: max_len - 3] + "..."
    return text


# ---------------------------------------------------------------------------
# Auto-cleaning — lightweight self-maintenance
# ---------------------------------------------------------------------------

_CLEANUP_INTERVAL: float = 86400.0
_last_cleanup: float = 0.0


def _get_cleanup_interval() -> int:
    try:
        return max(3600, int(os.environ.get("KANBAN_CONTEXT_CLEANUP_INTERVAL", "86400")))
    except (ValueError, TypeError):
        return 86400


def _get_outbox_retention_days() -> int:
    try:
        return max(1, int(os.environ.get("KANBAN_CONTEXT_OUTBOX_RETENTION", "14")))
    except (ValueError, TypeError):
        return 14


def _get_log_retention_days() -> int:
    try:
        return max(1, int(os.environ.get("KANBAN_CONTEXT_LOG_RETENTION", "7")))
    except (ValueError, TypeError):
        return 7


def _cleanup_old_outbox() -> int:
    """Delete completed outbox messages older than retention."""
    retention = _get_outbox_retention_days()
    cutoff = time.time() - retention * 86400
    conn = _open_shared_db()
    try:
        cur = conn.execute(
            "DELETE FROM outbox WHERE status='done' AND completed_at IS NOT NULL AND completed_at < ?",
            (cutoff,),
        )
        conn.commit()
        deleted = cur.rowcount

        # Also clean old response_log entries
        cur2 = conn.execute(
            "DELETE FROM response_log WHERE ts < ?",
            (cutoff,),
        )
        conn.commit()
        rl_deleted = cur2.rowcount

        if deleted > 0 or rl_deleted > 0:
            logger.info(
                "kanban-context: cleaned %d outbox + %d response_log (retention=%dd)",
                deleted, rl_deleted, retention,
            )
        return deleted
    except Exception as exc:
        logger.warning("kanban-context: outbox cleanup failed: %s", exc)
        return 0
    finally:
        conn.close()


def _cleanup_stale_pending() -> int:
    """Mark pending messages >7d as stale (target bot likely inactive)."""
    cutoff = time.time() - 7 * 86400
    conn = _open_shared_db()
    try:
        cur = conn.execute(
            "UPDATE outbox SET status='done', response_text='[stale - abandoned]' "
            "WHERE status='pending' AND ts < ?",
            (cutoff,),
        )
        conn.commit()
        stale = cur.rowcount
        if stale > 0:
            logger.info("kanban-context: marked %d stale pending as abandoned", stale)
        return stale
    except Exception as exc:
        logger.warning("kanban-context: stale cleanup failed: %s", exc)
        return 0
    finally:
        conn.close()


def _cleanup_old_log_files() -> int:
    """Remove kanban-context log files older than retention."""
    retention = _get_log_retention_days()
    log_dir = _hermes_home() / "logs" / "kanban-context"
    if not log_dir.is_dir():
        return 0
    cutoff = time.time() - retention * 86400
    removed = 0
    try:
        for f in log_dir.iterdir():
            if f.is_file() and f.stat().st_mtime < cutoff:
                f.unlink()
                removed += 1
        if removed > 0:
            logger.info("kanban-context: cleaned %d old log files (retention=%dd)", removed, retention)
        return removed
    except Exception as exc:
        logger.warning("kanban-context: log cleanup failed: %s", exc)
        return 0


def run_maintenance(force: bool = False) -> None:
    """Run periodic maintenance — outbox prune, stale mark, log rotate.

    Runs at most once per CLEANUP_INTERVAL unless *force* is True.
    """
    global _last_cleanup
    now = time.time()
    interval = _get_cleanup_interval()
    if not force and (now - _last_cleanup) < interval:
        return
    _cleanup_old_outbox()
    _cleanup_stale_pending()
    _cleanup_old_log_files()
    _last_cleanup = now


# ---------------------------------------------------------------------------
# Response coordination — prevent duplicate bot replies in shared groups
# ---------------------------------------------------------------------------

_RESPONSE_COOLDOWN: float = 30.0  # seconds — if bot A responded, bot B waits


def _resolve_chat_key_from_kwargs(kwargs: dict) -> str:
    """Derive a chat_key from the hook kwargs."""
    session_id = kwargs.get("session_id", "") or ""
    user_message = kwargs.get("user_message", "") or ""
    platform = kwargs.get("platform", "") or ""

    # Try to parse from session_id (format: agent:main:platform:type:chat_id[:thread_id])
    parts = session_id.split(":")
    if len(parts) >= 5:
        chat_id = parts[4]
        if len(parts) > 5:
            return f"{chat_id}:{parts[5]}"
        return chat_id

    # Fallback: use user_message hash as chat_key (less precise but functional)
    import hashlib
    return f"unknown_{hashlib.md5(session_id.encode()).hexdigest()[:12]}"


def claim_response(user_message: str, chat_key: str) -> bool:
    """Try to claim a response slot for this user_message in this chat.

    Returns True if caller should respond (first to claim),
    Returns False if another bot already claimed it (skip).
    """
    if not user_message or not chat_key:
        return True  # Can't check — default to respond

    conn = _open_shared_db()
    now = time.time()
    bot = _my_bot_name()
    try:
        # Check if someone already responded recently
        cutoff = now - _RESPONSE_COOLDOWN
        existing = conn.execute(
            "SELECT responder FROM response_log "
            "WHERE chat_key=? AND user_message=? AND ts >= ? AND responded=1 "
            "ORDER BY ts DESC LIMIT 1",
            (chat_key, user_message[:200], cutoff),
        ).fetchone()

        if existing is not None:
            other = existing[0]
            if other != bot:
                logger.info(
                    "kanban-context: skipping response — '%s' already replied "
                    "to msg in chat %s (cooldown=%ss)",
                    other, chat_key, _RESPONSE_COOLDOWN,
                )
                return False

        # Claim the slot
        conn.execute(
            "INSERT INTO response_log (ts, bot_name, chat_key, user_message, responded, responder) "
            "VALUES (?, ?, ?, ?, 1, ?)",
            (now, bot, chat_key, user_message[:200], bot),
        )
        conn.commit()
        logger.debug(
            "kanban-context: claimed response slot for chat=%s bot=%s",
            chat_key, bot,
        )
        return True
    except Exception as exc:
        logger.warning("kanban-context: claim_response failed: %s", exc)
        return True  # On error, let the bot respond (fail open)
    finally:
        conn.close()


def _inject_response_coordination(**kwargs) -> Optional[Dict[str, str]]:
    """pre_llm_call hook — injects context about other bots' responses.

    If another bot already responded to this user's message in this
    chat within the cooldown period, inject a warning so the agent
    knows to coordinate instead of repeating.
    """
    user_message = kwargs.get("user_message", "") or ""
    chat_key = _resolve_chat_key_from_kwargs(kwargs)

    if not user_message or not chat_key:
        return None

    conn = _open_shared_db()
    now = time.time()
    cutoff = now - _RESPONSE_COOLDOWN
    try:
        others = conn.execute(
            "SELECT responder, ts FROM response_log "
            "WHERE chat_key=? AND user_message=? AND ts >= ? AND responded=1 "
            "AND responder != ? "
            "ORDER BY ts DESC LIMIT 3",
            (chat_key, user_message[:200], cutoff, _my_bot_name()),
        ).fetchall()
    except Exception:
        return None
    finally:
        conn.close()

    if not others:
        return None

    lines = ["[Response Coordination]", ""]
    for responder, ts in others:
        ago = _fmt_time(ts)
        lines.append(f"- **{responder}** responded {ago}")
    lines.extend([
        "",
        "Another bot has already responded to this message. "
        "Unless explicitly @mentioned or asked a direct follow-up, "
        "avoid repeating what was already said. If you have something "
        "new to add, acknowledge the other bot's response first.",
        "",
        "[End Response Coordination]",
    ])
    ctx = "\n".join(lines)
    logger.info(
        "kanban-context: coordination context injected — %d other bot(s) "
        "already responded in chat %s", len(others), chat_key,
    )
    return {"context": ctx}


# ---------------------------------------------------------------------------
# Hook callbacks
# ---------------------------------------------------------------------------


def _inject_kanban_context(**kwargs) -> Optional[Dict[str, str]]:
    """pre_llm_call hook — injects board activity + pending messages."""
    run_maintenance()
    parts = []

    # Part 1: Kanban board activity
    board_ctx = _read_kanban_events()
    if board_ctx:
        parts.append(board_ctx)

    # Part 2: Pending cross-bot messages
    pending_ctx = _read_pending_messages()
    if pending_ctx:
        parts.append(pending_ctx)

    if parts:
        combined = "\n\n".join(parts)
        logger.info(
            "kanban-context: injected %d chars (%d parts)",
            len(combined), len(parts),
        )
        return {"context": combined}
    return None


# ---------------------------------------------------------------------------
# Proactive validation — runs at plugin load (install-time check)
# ---------------------------------------------------------------------------


class ValidationResult:
    """Collects warnings and errors during plugin validation."""

    errors: List[str]
    warnings: List[str]

    def __init__(self) -> None:
        self.errors = []
        self.warnings = []

    def ok(self) -> bool:
        return len(self.errors) == 0

    def log(self, label: str = "kanban-context") -> None:
        for w in self.warnings:
            logger.warning("%s: ⚠️  %s", label, w)
        for e in self.errors:
            logger.error("%s: ❌ %s", label, e)
        if not self.errors and not self.warnings:
            logger.info("%s: ✅ all validations passed", label)


def _validate_python_version(vr: ValidationResult) -> None:
    """Check Python >= 3.11."""
    import sys
    if sys.version_info < (3, 11):
        vr.errors.append(
            f"Python 3.11+ required (found {sys.version_info.major}.{sys.version_info.minor}). "
            "Please upgrade your Python interpreter."
        )


def _validate_hermes_version(vr: ValidationResult) -> None:
    """Try to detect Hermes Agent version."""
    try:
        from hermes_core.version import __version__ as hv
        parts = str(hv).lstrip("v").split(".")
        major = int(parts[0]) if len(parts) > 0 else 0
        minor = int(parts[1]) if len(parts) > 1 else 0
        if (major, minor) < (0, 13):
            vr.warnings.append(
                f"Hermes Agent v{'.'.join(parts)} detected — plugin was built for v0.13+. "
                "If you encounter issues, consider upgrading Hermes."
            )
    except ImportError:
        vr.warnings.append(
            "Could not detect Hermes Agent version (hermes_core.version not found). "
            "Assuming compatibility — if you hit issues, check Hermes >= v0.13.0."
        )


def _validate_multi_agent_plugin(vr: ValidationResult) -> None:
    """Check that multi-agent-context plugin is installed (required dependency)."""
    plugin_dirs = _hermes_home() / "plugins"
    if not plugin_dirs.is_dir():
        vr.warnings.append(
            f"Plugin directory not found at {plugin_dirs}. "
            "Make sure multi-agent-context is installed for the shared DB."
        )
        return

    found = False
    try:
        for d in plugin_dirs.iterdir():
            if d.is_dir() and d.name == "multi-agent-context":
                found = True
                break
    except PermissionError:
        vr.warnings.append(
            f"Cannot read plugin directory {plugin_dirs} (permission). "
            "Please verify multi-agent-context is installed."
        )
        return

    if not found:
        vr.warnings.append(
            "multi-agent-context plugin not found in plugins directory. "
            "kanban-context's cross-bot messaging requires multi-agent-context "
            "for the shared SQLite database. "
            "Install it from https://github.com/franklinbravos/hermes-community-plugins"
        )


def _validate_shared_db(vr: ValidationResult) -> None:
    """Check shared DB path is writable and outbox table can be created."""
    path = _shared_db_path()
    try:
        parent = os.path.dirname(path)
        os.makedirs(parent, exist_ok=True)
        # Try to open and create the table
        conn = sqlite3.connect(path, timeout=5)
        conn.execute(_OUTBOX_TABLE)
        conn.commit()
        conn.close()
        logger.debug("kanban-context: shared DB OK at %s", path)
    except Exception as exc:
        vr.errors.append(
            f"Cannot create/open shared database at '{path}': {exc}. "
            "Check directory permissions and disk space."
        )


def _validate_kanban_db(vr: ValidationResult) -> None:
    """Check that kanban DB exists or kanban boards dir exists."""
    default = _kanban_db()
    boards = _boards_dir()

    if default.is_file():
        logger.debug("kanban-context: kanban DB found at %s", default)
        return
    if boards.is_dir():
        logger.debug("kanban-context: kanban boards dir found at %s", boards)
        return

    vr.warnings.append(
        f"No kanban database found at {default} and no boards dir at {boards}. "
        "Kanban activity injection will be empty until a board is created. "
        "Use 'hermes kanban create-board <name>' to create one."
    )


def _validate_bot_name(vr: ValidationResult) -> None:
    """Check that a bot name can be resolved."""
    name = _my_bot_name()
    if name and name != "bot":
        logger.debug("kanban-context: bot name resolved as '%s'", name)
        return

    # Only a warning — the fallback name "bot" works for single-instance setups
    vr.warnings.append(
        "No explicit bot name set (CROSSBOT_BOT_NAME). "
        "Falling back to profile name or 'bot'. "
        "For multi-bot setups, set CROSSBOT_BOT_NAME env var "
        "to each bot's unique name to enable cross-bot messaging."
    )


def _validate_env_vars(vr: ValidationResult) -> None:
    """Validate numeric env vars at load time."""
    for key, default, label in [
        ("KANBAN_CONTEXT_EVENT_LIMIT", "10", "event limit"),
        ("KANBAN_CONTEXT_LOOKBACK_H", "12", "lookback hours"),
    ]:
        raw = os.environ.get(key, default)
        try:
            val = int(raw)
            if val < 0:
                vr.warnings.append(
                    f"{key}={raw} is negative — using default ({default})."
                )
            if key == "KANBAN_CONTEXT_EVENT_LIMIT" and val > 100:
                vr.warnings.append(
                    f"{key}={val} is very high — may exceed context window."
                )
            if key == "KANBAN_CONTEXT_LOOKBACK_H" and val > 168:
                vr.warnings.append(
                    f"{key}={val} (>{168}h = 1 week) — large lookback may "
                    "produce too many events."
                )
        except (ValueError, TypeError):
            vr.warnings.append(
                f"{key}={raw} is not a valid integer — using default ({default})."
            )


def _validate_log_dir(vr: ValidationResult) -> None:
    """Ensure the kanban plugin log directory exists and is writable."""
    log_dir = _hermes_home() / "logs" / "kanban-context"
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
    except Exception as exc:
        vr.warnings.append(
            f"Cannot create log directory {log_dir}: {exc}. "
            "Plugin will still work but logs won't be persisted to disk."
        )


def run_validation() -> ValidationResult:
    """Run all validation checks and return the result."""
    vr = ValidationResult()
    _validate_python_version(vr)
    _validate_hermes_version(vr)
    _validate_multi_agent_plugin(vr)
    _validate_shared_db(vr)
    _validate_kanban_db(vr)
    _validate_bot_name(vr)
    _validate_env_vars(vr)
    _validate_log_dir(vr)
    return vr


def _get_plugin_version() -> str:
    """Read plugin version from plugin.yaml."""
    plugin_yaml = _hermes_home() / "plugins" / "kanban-context" / "plugin.yaml"
    try:
        with open(str(plugin_yaml)) as f:
            for line in f:
                if line.startswith("version:"):
                    return line.split(":", 1)[1].strip()
    except Exception:
        pass
    return "2.0.0"


# ---------------------------------------------------------------------------
# Dashboard / Status — inspectable via agent or direct call
# ---------------------------------------------------------------------------


def kanban_status() -> str:
    """Return a human-readable status report of the kanban-context plugin.

    Can be called by the agent when the user asks about bot/plugin status.
    Returns a formatted string with sections:
      - Plugin version and config
      - Bot identity
      - Boards discovered
      - Outbox statistics
      - Validation status
    """
    lines: List[str] = []
    sep = "-" * 40

    # Header
    lines.append("[Kanban-Context Status]")
    lines.append("")

    # Version & config
    lines.append(f"Plugin version: {_get_plugin_version()}")
    lines.append(f"Python:         {__import__('sys').version_info.major}.{__import__('sys').version_info.minor}")
    try:
        from hermes_core.version import __version__ as hv
        lines.append(f"Hermes Agent:   v{hv}")
    except ImportError:
        lines.append("Hermes Agent:   (unknown)")
    lines.append(sep)

    # Bot identity
    lines.append(f"Bot name:       {_my_bot_name()}")
    lines.append(f"Hermes home:    {_hermes_home()}")
    lines.append(f"Shared DB:      {_shared_db_path()}")
    lines.append(f"Kanban DB:      {_kanban_db()}")

    # Configuration
    lines.append(sep)
    lines.append(f"Event limit:    {_event_limit()} events")
    lines.append(f"Lookback:       {_lookback_hours()}h")
    lines.append(f"Cleanup:        every {_get_cleanup_interval() // 3600}h")
    lines.append(f"Outbox retain:  {_get_outbox_retention_days()}d")
    lines.append(f"Log retain:     {_get_log_retention_days()}d")
    lines.append(sep)

    # Boards discovered
    boards = _iter_boards()
    if boards:
        lines.append(f"Kanban boards:  {len(boards)} found")
        for db_path, label in boards:
            try:
                db_size = os.path.getsize(db_path) / 1024
            except OSError:
                db_size = 0.0
            lines.append(f"  - {label} ({db_size:.0f} KB)")
    else:
        lines.append("Kanban boards:  none (activity injection will be empty)")
    lines.append(sep)

    # Outbox stats
    try:
        conn = _open_shared_db()
        rows = conn.execute(
            "SELECT status, COUNT(*) FROM outbox GROUP BY status"
        ).fetchall()
        conn.close()
        stats = {r[0]: r[1] for r in rows}
        total = sum(stats.values()) if stats else 0
        lines.append(f"Outbox total:   {total} messages")
        for status in ("pending", "done", "delivered"):
            count = stats.get(status, 0)
            if count > 0:
                lines.append(f"  - {status}: {count}")
    except Exception as exc:
        lines.append(f"Outbox:         error reading — {exc}")

    # Validation
    vr = run_validation()
    if vr.ok() and not vr.warnings:
        lines.append(sep)
        lines.append("Health:         ✅ all checks passed")
    else:
        lines.append(sep)
        lines.append(f"Health:         {'❌ errors' if vr.errors else '⚠ warnings'}")
        for w in vr.warnings[:3]:
            lines.append(f"  ⚠ {w[:80]}")
        for e in vr.errors[:3]:
            lines.append(f"  ❌ {e[:80]}")

    return "\n".join(lines)


def _handle_status_command(**kwargs) -> Optional[Dict[str, str]]:
    """pre_llm_call hook — detect /kanban-status and inject status context.

    When the user sends a message starting with /kanban-status, this
    hook replaces the normal context injection with a status report.
    """
    user_message = kwargs.get("user_message", "")
    if not user_message or not user_message.strip().startswith("/kanban-status"):
        return None

    # Return status as context so the agent can read and respond
    status = kanban_status()
    logger.info("kanban-context: status requested (size=%d chars)", len(status))
    return {"context": status}


def register(ctx) -> None:
    # Run proactive validation
    vr = run_validation()
    vr.log()

    ctx.register_hook("pre_llm_call", _inject_kanban_context)
    ctx.register_hook("pre_llm_call", _handle_status_command)
    ctx.register_hook("pre_llm_call", _inject_response_coordination)
