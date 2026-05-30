"""kanban-context — injects recent Kanban board activity into agent context.

Reads from the shared Kanban SQLite database (default board + any named boards)
and injects recent task events (created, completed, blocked, promoted, heartbeat,
etc.) as a formatted context block before each LLM call. This gives agents
awareness of what work items are flowing through the board without requiring
them to query the board explicitly.

Why this exists
---------------
The Hermes Kanban system (`hermes kanban`) is a powerful multi-agent work queue,
but by default the individual cards live in a SQLite database that agents never
read during conversation. Workers that use the ``kanban_*`` toolset already see
their assigned task, but orchestrators & other agents in the same session don't
see board-level activity.

This plugin bridges the gap by injecting ``[Recent Kanban Activity]`` into the
pre-LLM prompt, so every agent gets lightweight situational awareness of:
- Tasks being created and moving through the pipeline
- Blocked items that may affect downstream work
- Completed tasks whose output may be useful (summary) or whose dependencies
  were just unblocked
- Heartbeat notes from workers, which signal progress (or stagnation)

Duplicate / overlap with multi-agent-context
---------------------------------------------
The ``multi-agent-context`` plugin (also in this repo) shares Telegram/Discord
channel history across bot processes. ``kanban-context`` complements it by
sharing *board* history — the two together give agents both conversational
and operational context.

Configuration via environment variables
-----------------------------------------
    KANBAN_CONTEXT_EVENT_LIMIT  — Max events to inject per call (default: 10)
    KANBAN_CONTEXT_LOOKBACK_H   — Lookback window in hours (default: 12)

What gets injected
-------------------
A block like:

    [Recent Kanban Activity]

    - [2h ago] [kanban] **Design auth schema** (created → ready)
    - [30m ago] [kanban] **Implement auth API** (completed)
    - [5m ago] [linkedin-content] **Weekly trends post** (in progress: scraper running)
    - [End Kanban Activity]

This text appears before the agent's system prompt, within the same context
window. It is **not** a tool — the agent cannot act on it directly. It's pure
context so the agent *knows* what is happening on the board.

Events tracked
---------------
The plugin reads from the ``task_events`` table and recognises these ``kind``
values (human-readable description in parentheses):

- ``created`` → task entered the board
- ``assigned`` → assignee changed
- ``claimed`` → a worker picked it up
- ``completed`` → worker finished
- ``blocked`` → waiting on external input (includes reason)
- ``unblocked`` → no longer blocked
- ``heartbeat`` → periodic progress note from worker
- ``spawned`` → worker process started
- ``archived`` → removed from active view
- ``commented`` → discussion added
- ``linked`` → dependency link set
- ``edited`` → metadata changed
- ``promoted`` → dependency engine moved it (e.g. todo → ready)

Multi-board support
--------------------
The plugin scans:
1. The default board at ``{$HERMES_HOME}/kanban.db`` (legacy / single-board)
2. Named boards under ``{$HERMES_HOME}/kanban/boards/*/kanban.db``

Events from all boards are merged and sorted chronologically.
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
# Helpers — resolve paths relative to HERMES_HOME
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
    """Path to the default kanban DB."""
    return _hermes_home() / "kanban.db"


def _boards_dir() -> Path:
    """Path to named boards."""
    return _hermes_home() / "kanban" / "boards"


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


@functools.lru_cache(maxsize=1)
def _event_limit() -> int:
    try:
        val = os.environ.get("KANBAN_CONTEXT_EVENT_LIMIT", "10")
        return int(val)
    except (ValueError, TypeError):
        return 10


@functools.lru_cache(maxsize=1)
def _lookback_hours() -> int:
    try:
        val = os.environ.get("KANBAN_CONTEXT_LOOKBACK_H", "12")
        return int(val)
    except (ValueError, TypeError):
        return 12


def _clear_config_cache() -> None:
    """Clear cached config values (call after env var changes at runtime)."""
    _event_limit.cache_clear()
    _lookback_hours.cache_clear()


# ---------------------------------------------------------------------------
# Core logic
# ---------------------------------------------------------------------------


def _iter_boards() -> List[Tuple[str, str]]:
    """Yield (db_path, board_label) pairs for all available kanban boards.

    Labels let the user distinguish events from different boards in the
    injected context block.
    """
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

    # Sort newest-first, then take the top N, reverse to chronological
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


def _fmt_time(ts: float) -> str:
    """Format a unix timestamp to a human-friendly relative string."""
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
    """Map event kinds to short human-readable descriptions."""
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
# Hook callback
# ---------------------------------------------------------------------------


def _inject_kanban_context(**kwargs) -> Optional[Dict[str, str]]:
    """pre_llm_call hook — injects recent Kanban board activity as context."""
    ctx_text = _read_kanban_events()
    if ctx_text:
        logger.info(
            "kanban-context: injected %d chars of recent board activity",
            len(ctx_text),
        )
        return {"context": ctx_text}
    return None


# ---------------------------------------------------------------------------
# Plugin registration
# ---------------------------------------------------------------------------


def register(ctx) -> None:
    ctx.register_hook("pre_llm_call", _inject_kanban_context)
    logger.info(
        "kanban-context plugin v1.0.0 registered "
        "(event_limit=%d, lookback=%dh, home=%s)",
        _event_limit(), _lookback_hours(), _hermes_home(),
    )
