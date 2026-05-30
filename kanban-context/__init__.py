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

_OUTBOX_TABLE = """
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


def _open_shared_db():
    """Open the shared multi-agent DB, ensuring outbox table exists."""
    path = _shared_db_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    conn = sqlite3.connect(path, timeout=10, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute(_OUTBOX_TABLE)
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
        to_bot: Target bot profile name (e.g. 'ti', 'bravo')
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
# Hook callbacks
# ---------------------------------------------------------------------------


def _inject_kanban_context(**kwargs) -> Optional[Dict[str, str]]:
    """pre_llm_call hook — injects board activity + pending messages."""
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
# Plugin registration
# ---------------------------------------------------------------------------


def register(ctx) -> None:
    ctx.register_hook("pre_llm_call", _inject_kanban_context)
    logger.info(
        "kanban-context plugin v2.0 registered "
        "(event_limit=%d, lookback=%dh, home=%s, bot=%s)",
        _event_limit(), _lookback_hours(), _hermes_home(), _my_bot_name(),
    )
