"""Async Delegate — spawn background subagents without blocking the current turn.

Gives the agent two new tools:
  - delegate_async: Fire-and-forget task spawn (returns task_id immediately)
  - check_async_tasks: Poll task status / list all tasks

Plus a pre_gateway_dispatch hook that captures session routing info,
and a background thread that injects completion notifications into
the SAME session when tasks finish — no webhook needed.

Injection modes:
  - "queue" (default): notification queued behind current turn, no interrupt.
    Use for background research, fire-and-forget tasks.
  - "steer": notification interleaved into agent's tool loop without interrupting.
    Use when the result may change what the agent is doing mid-turn.
"""

import asyncio
import json
import logging
import os
import shutil
import subprocess
import threading
import time
import uuid
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

TASKS_DIR = Path.home() / ".hermes" / "async-tasks"
MAX_OUTPUT_CHARS = 8000
TASK_TIMEOUT_SECS = 1800  # 30 min — mark as timed out after this
CLEANUP_MAX_AGE_SECS = 86400  # 24 hrs — delete task files after this

# ---------------------------------------------------------------------------
# Module-level state — populated by pre_gateway_dispatch hook
# ---------------------------------------------------------------------------

# The GatewayRunner instance (captured from first dispatch)
_gateway_runner = None

# The gateway's event loop (captured in capture_routing which runs on the gateway thread)
_gateway_loop = None

# Per-task routing info: task_id -> {platform, chat_id, thread_id, user_id, user_name, session_key}
_task_routing: Dict[str, dict] = {}

# Lock for thread-safe access to _task_routing
_routing_lock = threading.Lock()

# The watcher thread reference
_watcher_thread: Optional[threading.Thread] = None
_watcher_stop = threading.Event()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _meta_path(task_id: str) -> Path:
    return TASKS_DIR / f"{task_id}.json"


def _output_path(task_id: str) -> Path:
    return TASKS_DIR / f"{task_id}.output"


def _done_path(task_id: str) -> Path:
    return TASKS_DIR / f"{task_id}.done"


def _err_path(task_id: str) -> Path:
    return TASKS_DIR / f"{task_id}.err"


def _read_meta(task_id: str) -> Optional[dict]:
    p = _meta_path(task_id)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text())
    except Exception:
        return None


def _write_meta(task_id: str, meta: dict) -> None:
    _meta_path(task_id).write_text(json.dumps(meta, indent=2))


def _find_hermes() -> str:
    """Locate the hermes executable."""
    hermes = shutil.which("hermes")
    if hermes:
        return hermes
    for candidate in [
        "/root/.local/bin/hermes",
        "/usr/local/bin/hermes",
        os.path.expanduser("~/.local/bin/hermes"),
    ]:
        if Path(candidate).exists():
            return candidate
    return "hermes"  # last resort


# ---------------------------------------------------------------------------
# Session injection — dual mode (queue / steer)
# ---------------------------------------------------------------------------

def _inject_task_notification(task_id: str, meta: dict, exit_code: str) -> None:
    """Inject a task completion notification into the originating session.

    Dual-mode injection:
      - "steer": interleaves into the agent's current tool loop (no interrupt).
        The agent sees the result between tool batches and can adjust course.
      - "queue": queues behind the current turn (no interrupt).
        Delivered as a clean new turn after the current one finishes.
      - If session is not busy: processes immediately as a new turn.
    """
    global _gateway_runner

    if not _gateway_runner:
        logger.warning("async-delegate: no gateway_runner captured, cannot inject — _gateway_runner=%s", _gateway_runner)
        return

    routing = meta.get("_routing")
    if not routing:
        logger.warning("async-delegate: task %s has no routing info, cannot inject", task_id)
        return

    platform_str = routing.get("platform", "")
    chat_id = routing.get("chat_id", "")
    thread_id = routing.get("thread_id")
    user_id = routing.get("user_id")
    user_name = routing.get("user_name")
    inject_mode = meta.get("inject_mode", "queue")  # default to queue

    if not platform_str or not chat_id:
        logger.warning("async-delegate: task %s missing platform/chat_id in routing", task_id)
        return

    # Build the synthetic notification text
    status_label = "✅ Completed" if exit_code == "0" else f"❌ Failed (exit {exit_code})"
    out_file = _output_path(task_id)
    goal = meta.get("goal", "unknown")[:100]

    synth_text = (
        f"[Async Task Done: {task_id}] {status_label} — "
        f"Goal: {goal} — "
        f"Result file: {out_file}"
    )

    logger.info("async-delegate: injecting notification for %s (mode=%s) into %s chat=%s thread=%s",
                task_id, inject_mode, platform_str, chat_id, thread_id)

    try:
        # Import gateway types
        from gateway.session import SessionSource, build_session_key
        from gateway.platforms.base import MessageEvent, MessageType, merge_pending_message_event
        from gateway.config import Platform

        # Resolve Platform enum
        platform_enum = None
        try:
            platform_enum = Platform(platform_str)
        except ValueError:
            for p in Platform:
                if p.value == platform_str:
                    platform_enum = p
                    break
        if not platform_enum:
            logger.error("async-delegate: unknown platform '%s'", platform_str)
            return

        # Build SessionSource
        source = SessionSource(
            platform=platform_enum,
            chat_id=chat_id,
            chat_type=routing.get("chat_type", "group"),
            user_id=user_id,
            user_name=user_name or "system",
            thread_id=thread_id,
        )

        # Build synthetic MessageEvent (internal=True bypasses auth)
        synth_event = MessageEvent(
            text=synth_text,
            message_type=MessageType.TEXT,
            source=source,
            internal=True,
        )

        # Find the adapter for this platform
        adapter = None
        for p, a in _gateway_runner.adapters.items():
            p_val = p.value if hasattr(p, "value") else str(p)
            if p_val == platform_str:
                adapter = a
                break

        if not adapter:
            logger.error("async-delegate: no adapter found for platform '%s'", platform_str)
            return

        loop = _gateway_loop
        if not loop:
            logger.error("async-delegate: no event loop available for injection")
            return

        # Build the session key using the GATEWAY's own function — 
        # our hand-built routing["session_key"] format is wrong for groups!
        # Gateway uses: agent:main:{platform}:group:{chat_id}:{thread_id}
        # We were using: {platform}:{chat_id}:{thread_id}:{user_id} ← WRONG
        try:
            session_key = build_session_key(source)
        except Exception:
            session_key = routing.get("session_key", "")
        if not session_key:
            logger.error("async-delegate: could not build session_key for %s", task_id)
            return

        # --- STEER MODE: inject into running agent's tool loop ---
        if inject_mode == "steer":
            running_agent = _gateway_runner._running_agents.get(session_key)
            if running_agent and running_agent is not None and hasattr(running_agent, "steer"):
                # Build a richer steer text with result preview
                result_preview = ""
                out_path = _output_path(task_id)
                if out_path.exists():
                    try:
                        result_preview = out_path.read_text()[:2000]
                    except Exception:
                        pass

                steer_text = (
                    f"[Async Task Done: {task_id}] {status_label}\n"
                    f"Goal: {goal}\n"
                    f"Result file: {out_file}\n"
                )
                if result_preview:
                    steer_text += f"Preview:\n{result_preview}\n"
                steer_text += "— Process this result and incorporate it into your current work."

                steered = bool(running_agent.steer(steer_text))
                if steered:
                    logger.info("async-delegate: STEERED notification for %s into running agent", task_id)
                    return
                else:
                    logger.warning("async-delegate: steer() failed for %s, falling back to queue", task_id)
                    # Fall through to queue mode
            else:
                logger.info("async-delegate: no running agent for steer, falling back to queue for %s", task_id)
                # Fall through to queue mode

        # --- QUEUE / DEFAULT MODE: non-interrupting delivery ---
        # Schedule on the event loop to check busy state and queue appropriately
        async def _async_inject():
            # Check if session is currently busy
            is_busy = session_key in adapter._active_sessions

            if is_busy:
                # Queue behind current turn — same pattern as photo handling
                merge_pending_message_event(adapter._pending_messages, session_key, synth_event)
                logger.info("async-delegate: QUEUED notification for %s behind active turn", task_id)
            else:
                # Session is free — process as a normal new turn
                await adapter.handle_message(synth_event)
                logger.info("async-delegate: DELIVERED notification for %s as new turn", task_id)

        future = asyncio.run_coroutine_threadsafe(_async_inject(), loop)
        # Wait for it with a timeout (don't hang the watcher thread)
        future.result(timeout=15)

        logger.info("async-delegate: injection complete for %s (mode=%s)", task_id, inject_mode)

    except Exception as e:
        logger.error("async-delegate: injection failed for %s: %s", task_id, e)


# ---------------------------------------------------------------------------
# Background watcher thread
# ---------------------------------------------------------------------------

def _watcher_loop() -> None:
    """Background thread: poll for .done files and inject notifications."""
    logger.info("async-delegate: watcher thread started")

    while not _watcher_stop.is_set():
        try:
            if not TASKS_DIR.exists():
                _watcher_stop.wait(5)
                continue

            now = time.time()
            for done_file in list(TASKS_DIR.glob("async_*.done")):
                task_id = done_file.stem  # e.g. "async_abcdef12"
                meta = _read_meta(task_id)
                if not meta:
                    continue

                # Only process tasks still marked as running
                if meta.get("status") != "running":
                    continue

                # Check routing: in-memory dict first, then fall back to JSON _routing
                with _routing_lock:
                    routing = _task_routing.get(task_id)

                if not routing:
                    routing = meta.get("_routing")
                    if routing:
                        logger.info("async-delegate: watcher using _routing from JSON for %s (not in _task_routing)", task_id)

                if not routing:
                    logger.warning("async-delegate: watcher skipping %s — no routing info anywhere", task_id)
                    continue

                exit_code = done_file.read_text().strip()
                meta["status"] = "completed" if exit_code == "0" else "failed"
                meta["exit_code"] = exit_code
                meta["completed_at"] = now
                _write_meta(task_id, meta)

                # Inject the notification!
                _inject_task_notification(task_id, meta, exit_code)

                # Clean up routing entry
                with _routing_lock:
                    _task_routing.pop(task_id, None)

        except Exception as e:
            logger.error("async-delegate: watcher error: %s", e)

        _watcher_stop.wait(5)  # Poll every 5 seconds

    logger.info("async-delegate: watcher thread stopped")


def _ensure_watcher() -> None:
    """Start the background watcher thread if not already running."""
    global _watcher_thread
    if _watcher_thread and _watcher_thread.is_alive():
        return
    _watcher_stop.clear()
    _watcher_thread = threading.Thread(
        target=_watcher_loop,
        name="async-delegate-watcher",
        daemon=True,
    )
    _watcher_thread.start()


# ---------------------------------------------------------------------------
# Tool: delegate_async
# ---------------------------------------------------------------------------

# Default toolsets for async subagents — covers the common use cases while
# keeping startup fast (~10s vs 30+ with everything loaded).
_ASYNC_DEFAULT_TOOLSETS = "web,terminal,file,browser,vision"


def delegate_async_tool(goal: str, context: str = "", inject_mode: str = "queue", toolsets: str = "") -> str:
    """Spawn a background subagent and return immediately."""
    TASKS_DIR.mkdir(parents=True, exist_ok=True)

    # Validate inject_mode
    if inject_mode not in ("queue", "steer"):
        inject_mode = "queue"

    # Resolve toolsets — use provided or fall back to sensible default
    resolved_toolsets = toolsets.strip() if toolsets.strip() else _ASYNC_DEFAULT_TOOLSETS

    task_id = f"async_{uuid.uuid4().hex[:8]}"

    # Build prompt for the subagent
    prompt = goal
    if context:
        prompt = f"{goal}\n\nAdditional context:\n{context}"
    prompt += (
        "\n\nIMPORTANT: Do NOT use the delegate_async or delegate_task tool. "
        "Complete this task yourself using your own tools."
    )

    # Write initial metadata
    meta = {
        "task_id": task_id,
        "goal": goal[:500],
        "status": "running",
        "spawned_at": time.time(),
        "inject_mode": inject_mode,
        "toolsets": resolved_toolsets,
    }
    _write_meta(task_id, meta)

    # Write prompt to a file to avoid shell quoting issues
    prompt_file = TASKS_DIR / f"{task_id}.prompt"
    prompt_file.write_text(prompt)

    # Write a wrapper bash script
    out_file = _output_path(task_id)
    done_file = _done_path(task_id)
    err_file = _err_path(task_id)
    hermes_bin = _find_hermes()

    wrapper_script = TASKS_DIR / f"{task_id}.sh"
    wrapper_script.write_text(
        f'#!/bin/bash\n'
        f'PROMPT=$(cat "{prompt_file}")\n'
        f'"{hermes_bin}" chat -q "$PROMPT" -Q -t "{resolved_toolsets}" >"{out_file}" 2>"{err_file}"\n'
        f'echo $? >"{done_file}"\n'
    )
    wrapper_script.chmod(0o755)

    proc = subprocess.Popen(
        ["bash", str(wrapper_script)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )

    meta["pid"] = proc.pid

    # Attach current session routing info so the watcher can inject notifications
    global _latest_routing
    if _latest_routing:
        meta["_routing"] = _latest_routing
        # Also store in the module-level routing dict for quick lookup
        with _routing_lock:
            _task_routing[task_id] = _latest_routing

    _write_meta(task_id, meta)

    logger.info(f"async-delegate: spawned {task_id} (PID {proc.pid}, mode={inject_mode})")

    return json.dumps({
        "task_id": task_id,
        "status": "running",
        "inject_mode": inject_mode,
        "toolsets": resolved_toolsets,
        "message": (
            f"Async task `{task_id}` spawned in background (mode: {inject_mode}, toolsets: {resolved_toolsets}). "
            "I will be notified when it completes and can process the results. "
            "You can continue chatting with me in the meantime!"
        ),
    })


# ---------------------------------------------------------------------------
# Tool: check_async_tasks
# ---------------------------------------------------------------------------

def check_async_tasks_tool(task_id: str = "") -> str:
    """Check status of async delegated tasks."""
    TASKS_DIR.mkdir(parents=True, exist_ok=True)

    if task_id:
        meta = _read_meta(task_id)
        if meta is None:
            return json.dumps({"error": f"Task {task_id} not found"})

        # Refresh status from disk
        _refresh_status(task_id, meta)
        _write_meta(task_id, meta)

        # Include output preview for completed tasks
        if meta.get("status") in ("completed", "failed"):
            out = _output_path(task_id)
            if out.exists():
                meta["result"] = out.read_text()[:MAX_OUTPUT_CHARS]

        return json.dumps(meta, indent=2)

    # List all tasks
    tasks = []
    for meta_file in sorted(TASKS_DIR.glob("async_*.json")):
        try:
            meta = json.loads(meta_file.read_text())
            _refresh_status(meta["task_id"], meta)
            tasks.append({
                "task_id": meta["task_id"],
                "goal": meta.get("goal", "")[:100],
                "status": meta.get("status", "unknown"),
                "inject_mode": meta.get("inject_mode", "queue"),
                "spawned_at": meta.get("spawned_at"),
            })
        except Exception:
            continue

    return json.dumps({"tasks": tasks, "count": len(tasks)}, indent=2)


def _refresh_status(task_id: str, meta: dict) -> None:
    """Update meta status from on-disk markers."""
    if meta.get("status") != "running":
        return

    done_file = _done_path(task_id)
    if done_file.exists():
        exit_code = done_file.read_text().strip()
        meta["status"] = "completed" if exit_code == "0" else "failed"
        meta["exit_code"] = exit_code
        meta["completed_at"] = time.time()
        return

    # Check for timeout
    elapsed = time.time() - meta.get("spawned_at", 0)
    if elapsed > TASK_TIMEOUT_SECS:
        meta["status"] = "timeout"
        meta["completed_at"] = time.time()
        logger.warning(f"async-delegate: task {task_id} timed out after {int(elapsed)}s")


# ---------------------------------------------------------------------------
# Hook: pre_gateway_dispatch — capture gateway runner + session routing
# ---------------------------------------------------------------------------

def capture_routing(**kwargs) -> Optional[Dict[str, str]]:

    """Capture the GatewayRunner and session routing info from every dispatch.

    This hook fires on EVERY incoming message. We use it to:
    1. Grab the gateway_runner reference (first time only)
    2. Store routing info that async tasks can use to inject notifications
    """
    global _gateway_runner

    event = kwargs.get("event")
    gateway = kwargs.get("gateway")

    # Capture the gateway runner and event loop
    global _gateway_runner, _gateway_loop
    if gateway and not _gateway_runner:
        _gateway_runner = gateway
        try:
            import asyncio as _asyncio
            _gateway_loop = _asyncio.get_running_loop()
            logger.info("async-delegate: captured GatewayRunner + event loop")
        except RuntimeError:
            try:
                _gateway_loop = _asyncio.get_event_loop()
            except Exception:
                pass
            logger.info("async-delegate: captured GatewayRunner (loop via fallback)")
        _ensure_watcher()

    if not event:
        return None

    source = getattr(event, "source", None)
    if not source:
        return None

    # Build routing info from the current message's source
    routing = {
        "platform": source.platform.value if hasattr(source.platform, "value") else str(source.platform),
        "chat_id": source.chat_id or "",
        "chat_type": source.chat_type or "dm",
        "thread_id": source.thread_id,
        "user_id": source.user_id,
        "user_name": source.user_name,
    }

    # Build session_key from source (same format gateway uses)
    session_store = kwargs.get("session_store")
    if session_store and source:
        try:
            platform_val = source.platform.value if hasattr(source.platform, "value") else str(source.platform)
            chat_id = source.chat_id or ""
            thread_id = source.thread_id or ""
            user_id = source.user_id or ""
            parts = [platform_val, chat_id]
            if thread_id:
                parts.append(thread_id)
            parts.append(user_id)
            routing["session_key"] = ":".join(parts)
        except Exception:
            pass

    # Store globally for delegate_async_tool to attach to spawned tasks
    global _latest_routing
    _latest_routing = routing

    return None  # Don't modify the event


# Global for the most recent routing info (written by capture_routing, read by delegate_async_tool)
_latest_routing: Optional[dict] = None


# ---------------------------------------------------------------------------
# Hook: pre_llm_call — fallback: inject completed results into context
# ---------------------------------------------------------------------------

def pre_llm_inject_results(**kwargs) -> Optional[Dict[str, str]]:
    """Fallback: inject completed async task results into conversation context.

    This serves as a safety net in case the watcher thread injection fails
    or the task completes while the agent is already in a turn.
    """
    if not TASKS_DIR.exists():
        return None

    now = time.time()
    completed_results: List[str] = []

    for meta_file in list(TASKS_DIR.glob("async_*.json")):
        try:
            meta = json.loads(meta_file.read_text())
            task_id = meta.get("task_id", "")

            # Only process tasks that are still marked running
            if meta.get("status") != "running":
                continue

            done_file = _done_path(task_id)
            if not done_file.exists():
                # Check timeout
                if now - meta.get("spawned_at", 0) > TASK_TIMEOUT_SECS:
                    meta["status"] = "timeout"
                    meta["completed_at"] = now
                    _write_meta(task_id, meta)
                    completed_results.append(
                        f"[Async Task Timed Out: {task_id}] "
                        f"Goal: {meta.get('goal', 'unknown')[:100]} "
                        f"(ran >{TASK_TIMEOUT_SECS // 60}min)"
                    )
                continue

            # Task just completed!
            exit_code = done_file.read_text().strip()
            out_file = _output_path(task_id)

            meta["status"] = "completed" if exit_code == "0" else "failed"
            meta["exit_code"] = exit_code
            meta["completed_at"] = now

            _write_meta(task_id, meta)

            status_label = "✅ Completed" if exit_code == "0" else f"❌ Failed (exit {exit_code})"

            # SMALL PING ONLY — do NOT dump full output into context!
            result_text = (
                f"[Async Task Done: {task_id}] "
                f"{status_label} — "
                f"Goal: {meta.get('goal', 'unknown')[:100]} — "
                f"Result file: {out_file} "
            )

            completed_results.append(result_text)
            logger.info(f"async-delegate: {task_id} completed (exit={exit_code}) — via pre_llm fallback")

        except Exception as e:
            logger.warning(f"async-delegate: error in pre_llm hook: {e}")
            continue

    if not completed_results:
        return None

    ping_lines = "\n".join(completed_results)
    context = (
        "[Async Delegate — Tasks Done]\n"
        "One or more background tasks finished. Read result files with read_file if needed.\n"
        f"{ping_lines}\n"
    )
    return {"context": context}


# ---------------------------------------------------------------------------
# Hook: on_session_end — cleanup stale tasks
# ---------------------------------------------------------------------------

def cleanup_stale_tasks(**kwargs) -> None:
    """Remove task files older than 24 hours."""
    if not TASKS_DIR.exists():
        return

    now = time.time()

    for meta_file in list(TASKS_DIR.glob("async_*.json")):
        try:
            meta = json.loads(meta_file.read_text())
            task_id = meta.get("task_id", "")
            age = now - meta.get("spawned_at", 0)

            if age > CLEANUP_MAX_AGE_SECS:
                for p in [
                    _meta_path(task_id),
                    _output_path(task_id),
                    _done_path(task_id),
                    _err_path(task_id),
                    TASKS_DIR / f"{task_id}.prompt",
                    TASKS_DIR / f"{task_id}.sh",
                ]:
                    p.unlink(missing_ok=True)
                logger.info(f"async-delegate: cleaned up stale task {task_id}")
        except Exception:
            continue


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

def register(ctx) -> None:
    """Register async-delegate plugin tools and hooks."""
    TASKS_DIR.mkdir(parents=True, exist_ok=True)

    # -- delegate_async --
    ctx.register_tool(
        name="delegate_async",
        handler=lambda args, **kw: delegate_async_tool(
            goal=args.get("goal", ""),
            context=args.get("context", ""),
            inject_mode=args.get("inject_mode", "queue"),
            toolsets=args.get("toolsets", ""),
        ),
        schema={
            "name": "delegate_async",
            "description": (
                "Spawn a background subagent to work on a task ASYNCHRONOUSLY. "
                "Returns immediately with a task_id — you are NOT blocked and can continue "
                "the conversation normally. When the task completes, a notification is "
                "automatically injected into this session so you can process results.\n\n"
                "INJECTION MODES — choose based on how the result will be used:\n"
                "- \"queue\" (default): The notification waits for your current turn to finish, "
                "then delivers as a clean new turn. Use for background research, lookups, "
                "fire-and-forget tasks where you just need the result later.\n"
                "- \"steer\": The notification is interleaved into your current tool loop "
                "WITHOUT interrupting. You'll see the result between tool calls and can "
                "adjust your approach mid-turn. Use when the result may CHANGE what you're "
                "currently doing (e.g., checking if an API exists before writing code that "
                "calls it, validating a file path before editing, confirming a dependency "
                "version before installing).\n\n"
                "The subagent has full tool access (terminal, web, file, etc.). "
                "Use check_async_tasks to manually poll task status if needed. "
                "Do NOT use this for trivial tasks — use delegate_task for those."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "goal": {
                        "type": "string",
                        "description": "What the subagent should accomplish. Be specific and self-contained — the subagent has no context about this conversation."
                    },
                    "context": {
                        "type": "string",
                        "description": "Optional background information the subagent needs (file paths, error messages, constraints)."
                    },
                    "inject_mode": {
                        "type": "string",
                        "enum": ["queue", "steer"],
                        "description": (
                            "How to deliver the result when the task finishes. "
                            "\"queue\" = wait for current turn to end, then deliver as new turn (default, safe). "
                            "\"steer\" = interleave into current tool loop so you can react mid-turn (for results that change your approach)."
                        ),
                        "default": "queue"
                    },
                    "toolsets": {
                        "type": "string",
                        "description": (
                            "Comma-separated toolsets to load for the subagent. "
                            "Default: \"web,terminal,file,browser,vision\" (covers research, code, files, browsing, images). "
                            "Specify fewer for faster startup (e.g. \"file\" for trivial tasks, \"web\" for lookups). "
                            "Add more if needed (e.g. \"web,terminal,file,image_gen\" for image generation tasks)."
                        ),
                        "default": ""
                    },
                },
                "required": ["goal"],
            },
        },
        toolset="async-delegation",
        description="Spawn a background subagent to work on a task ASYNCHRONOUSLY. Returns immediately with a task_id.",
        emoji="🚀",
        check_fn=lambda: True,
    )

    # -- check_async_tasks --
    ctx.register_tool(
        name="check_async_tasks",
        handler=lambda args, **kw: check_async_tasks_tool(
            task_id=args.get("task_id", ""),
        ),
        schema={
            "name": "check_async_tasks",
            "description": (
                "Check status of async delegated tasks. "
                "Pass a task_id to check a specific task, or leave empty to list all tasks. "
                "Completed task results are auto-injected when they finish."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "task_id": {
                        "type": "string",
                        "description": "Specific task ID to check, or empty string to list all."
                    },
                },
                "required": [],
            },
        },
        toolset="async-delegation",
        description="Check status of async delegated tasks.",
        emoji="📋",
        check_fn=lambda: True,
    )

    # Hooks
    ctx.register_hook("pre_gateway_dispatch", capture_routing)
    ctx.register_hook("pre_llm_call", pre_llm_inject_results)
    ctx.register_hook("on_session_end", cleanup_stale_tasks)

    logger.info("async-delegate plugin registered (v6 — dual-mode injection: queue + steer)")
