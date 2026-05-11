# async-delegate

**Spawn background subagents without blocking the current conversation turn.**

A Hermes Agent plugin that adds true async task delegation — fire off a subagent to work on something in the background while you keep chatting. When the task finishes, a notification is automatically injected back into the originating session.

## How It Works

```
┌─────────────┐    delegate_async     ┌──────────────────┐
│   Agent      │ ──────────────────►   │  Subagent        │
│   (turn)     │  returns task_id      │  (hermes chat)   │
│              │  immediately          │  runs in bg      │
│              │                       └──────┬───────────┘
│  continues   │                              │
│  chatting    │         .done file written    │
│  normally    │              │                │
└──────┬───────┘              ▼                │
       │              ┌──────────────┐         │
       │              │   Watcher    │◄────────┘
       │              │   Thread     │  polls every 5s
       │              │  (daemon)    │
       │              └──────┬───────┘
       │                     │
       │   ◄─────────────────┘
       │   notification injected
       │   (queue or steer)
       ▼
```

### Architecture

1. **`delegate_async` tool** — Agent calls this to spawn a background `hermes chat` process. Returns a `task_id` immediately. The agent's current turn is NOT blocked.

2. **File-based coordination** — Each task gets a set of files in `~/.hermes/async-tasks/`:
   - `async_<id>.json` — Task metadata (goal, status, routing info)
   - `async_<id>.prompt` — The subagent's full prompt
   - `async_<id>.sh` — Wrapper bash script that runs `hermes chat`
   - `async_<id>.output` — Subagent's stdout (the result)
   - `async_<id>.err` — Subagent's stderr
   - `async_<id>.done` — Contains the exit code (0 = success)

3. **Watcher thread** — A daemon thread polls `~/.hermes/async-tasks/` every 5 seconds for `.done` files. When it finds one, it:
   - Reads the task's routing info (which session to notify)
   - Injects a completion notification into that session

4. **Session injection** — Uses the gateway's internal APIs to deliver the notification:
   - Builds a synthetic `MessageEvent` with `internal=True`
   - Finds the correct platform adapter
   - Checks if the session is busy (agent mid-turn)
   - Routes through queue or steer mode accordingly

5. **Fallback: `pre_llm_call` hook** — If the watcher injection fails for any reason, a secondary hook scans for completed tasks before each LLM call and injects a text ping into the conversation context as a safety net.

## Injection Modes

### Queue (default)
Notification waits for the current turn to finish, then delivers as a clean new turn. The agent is never interrupted.

**Use for:** Background research, lookups, fire-and-forget tasks where you just need the result eventually.

**Implementation:** Uses `merge_pending_message_event()` — the same mechanism Hermes uses for photo handling. The notification sits in `_pending_messages` until the current turn completes, then gets processed as the next turn.

### Steer
Notification is interleaved into the agent's active tool loop. The agent sees the result between tool calls and can adjust its approach mid-turn — without being interrupted.

**Use for:** Results that might change what the agent is currently doing. For example:
- Checking if an API exists before writing code that calls it
- Validating a file path before editing
- Confirming a dependency version before installing
- Getting a quick answer that gates the next tool call

**Implementation:** Uses `agent.steer()` to inject text into the running agent's context. Falls back to queue mode if steer fails (no running agent, agent doesn't support steer).

## Tools

### `delegate_async`
```
delegate_async(goal: str, context?: str, inject_mode?: "queue"|"steer") -> JSON
```
Spawns a background subagent. Returns immediately with:
```json
{
  "task_id": "async_a1b2c3d4",
  "status": "running",
  "inject_mode": "queue",
  "message": "Async task `async_a1b2c3d4` spawned in background..."
}
```

### `check_async_tasks`
```
check_async_tasks(task_id?: str) -> JSON
```
Check a specific task or list all tasks. For completed tasks, includes the result preview.

## Hooks

| Hook | Function | Purpose |
|------|----------|---------|
| `pre_gateway_dispatch` | `capture_routing()` | Captures `GatewayRunner` reference + session routing from every incoming message |
| `pre_llm_call` | `pre_llm_inject_results()` | Fallback: scans for completed tasks before each LLM call |
| `on_session_end` | `cleanup_stale_tasks()` | Deletes task files older than 24 hours |

## Configuration

### plugin.yaml
```yaml
name: async-delegate
version: "1.1.0"
description: "Async task delegation with dual-mode injection (queue/steer) — spawn background subagents via the async-delegation toolset without blocking the current turn"
hooks:
  - pre_gateway_dispatch
  - pre_llm_call
  - on_session_end
```

No additional config needed. Drop the plugin folder into `~/.hermes/plugins/` and restart the gateway.

## Task Lifecycle

```
running ──► completed    (.done file with exit code 0)
       ──► failed       (.done file with non-zero exit code)
       ──► timeout      (no .done after 30 minutes)
```

All task files are auto-cleaned after 24 hours.

## Key Design Decisions

- **File-based, not database** — Tasks are just JSON + output files. Simple, debuggable, no migration headaches.
- **Session injection, not webhooks** — Notifications go through the gateway's internal message handling. No external HTTP endpoints, no auth complexity, works in any deployment.
- **Gateway's `build_session_key()`** — Session keys are built using the gateway's own function, not hand-constructed. This is critical because group chats use a different key format (`agent:main:telegram:group:{chat_id}:{thread_id}`) than what you'd naively build from routing info.
- **Routing stored in task metadata** — Each task JSON includes a `_routing` dict with platform, chat_id, thread_id, etc. The watcher reads this on completion so it knows where to send the notification, even if the in-memory routing dict has been cleared.
- **Dual routing lookup** — Watcher checks the in-memory `_task_routing` dict first (fast), then falls back to the JSON `_routing` field (survives gateway restarts).

## Changelog

### v1.1.0
- **Fixed toolset name collision** — Renamed toolset from `delegation` to `async-delegation`. The old name conflicted with the built-in delegation toolset in `toolsets.py` (which registers `delegate_task`). Plugin tools now properly appear in the agent's tool list without shadowing or being shadowed by built-in tools.
- **Tested working** with both `queue` and `steer` injection modes.
- **Important:** The toolset name registered by this plugin MUST NOT collide with any built-in toolset defined in `hermes_agent/toolsets.py`. If you rename the toolset, verify it doesn't conflict with: `default`, `delegation`, `search`, `browser`, etc.

### v1.0.0
- Initial release. File-based async task delegation with watcher thread, queue-mode injection, and `pre_llm_call` fallback.

## Limitations (Current)
- **No progress callbacks** — Subagents run to completion; there's no streaming progress or intermediate results.
- **Single output channel** — Notifications always go back to the originating session. No routing to a different chat/user.
- **Polling, not event-driven** — 5-second poll interval means up to 5s delay between task completion and notification. Could be replaced with `inotify` or `watchdog` for instant delivery.

## File Structure

```
~/.hermes/plugins/async-delegate/
├── __init__.py       # Main plugin (795 lines)
├── plugin.yaml       # Plugin metadata + hook declarations
└── README.md         # This file

~/.hermes/async-tasks/           # Created automatically
├── async_<id>.json    # Task metadata
├── async_<id>.prompt  # Subagent prompt
├── async_<id>.sh      # Wrapper script
├── async_<id>.output  # Result (subagent stdout)
├── async_<id>.err     # Errors (subagent stderr)
└── async_<id>.done    # Exit code marker
```

## Testing

### Quick smoke test
```
# As the agent:
delegate_async(goal="What time is it in EDT, PST, JST, and UTC? Just the 4 times.", inject_mode="queue")

# Should return task_id immediately.
# ~10-30 seconds later, notification appears as a new turn.
```

### Queue mode pressure test
Spawn a long tool chain (10+ calls with 2s sleeps), then fire an ultra-short async task ("say potato"). The notification should queue behind the active turn and deliver only after it completes — zero interruption.

### Check logs
```bash
strings ~/.hermes/logs/agent.log | grep "async-delegate" | tail -20
```
Look for `QUEUED notification for <task_id> behind active turn` to confirm queue mode is working.
