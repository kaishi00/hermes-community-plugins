# Hermes Community Plugins 🎭

Battle-tested plugins for [Hermes Agent](https://github.com/NousResearch/hermes-agent) — **zero core patches required**. Drop in, enable, restart.

## Plugins

### 1. [`async-delegate/`](./async-delegate/) 🚀

**Spawn background subagents without blocking the current conversation turn.**

A Hermes Agent plugin that adds true async task delegation — fire off a subagent to work on something in the background while you keep chatting. When the task finishes, a notification is automatically injected back into the originating session.

#### How It Works

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

**Architecture:**
- **`delegate_async` tool** — Agent calls this to spawn a background `hermes chat` process. Returns a `task_id` immediately. The agent's current turn is NOT blocked.
- **File-based coordination** — Each task gets a set of files in `~/.hermes/async-tasks/` (JSON metadata, prompt, wrapper script, output, error, done marker).
- **Watcher thread** — A daemon thread polls for `.done` files every 5 seconds. On completion, it injects a notification back into the originating session.
- **Session injection** — Uses the gateway's internal APIs to deliver the notification, with fallback via `pre_llm_call` hook.

#### Injection Modes

| Mode | Behavior | Use For |
|------|----------|---------|
| **Queue** (default) | Notification waits for the current turn to finish, then delivers as a clean new turn | Background research, lookups, fire-and-forget tasks |
| **Steer** | Notification is interleaved into the agent's active tool loop mid-turn | Results that might change what the agent is currently doing (API checks, validation, etc.) |

#### Tools

| Tool | Description |
|------|-------------|
| `delegate_async` | Spawns a background subagent. Returns `task_id` immediately. |
| `check_async_tasks` | Check a specific task or list all tasks. Includes result preview for completed tasks. |

#### Hooks

| Hook | Purpose |
|------|---------|
| `pre_gateway_dispatch` | Captures `GatewayRunner` reference + session routing from incoming messages |
| `pre_llm_call` | Fallback: scans for completed tasks before each LLM call |
| `on_session_end` | Cleans up task files older than 24 hours |

#### Key Design Decisions
- **File-based, not database** — Simple, debuggable, no migration headaches.
- **Session injection, not webhooks** — Works in any deployment, no external HTTP endpoints.
- **Dual routing lookup** — In-memory dict (fast) + JSON fallback (survives gateway restarts).

#### Quick Install
```bash
cp -r async-delegate ~/.hermes/plugins/async-delegate
# Add to config.yaml:
#   plugins:
#     enabled:
#       - async-delegate
# Restart gateway
```

No additional config needed. Drop the plugin folder into `~/.hermes/plugins/` and restart the gateway.

---

### 2. [`multi-agent-context/`](./multi-agent-context/) 🤝
Injects shared channel/group history into agent context so agents can see what other agents said — **without triggering infinite reply loops.** Supports Discord (REST API) and Telegram (shared SQLite).

#### The Problem This Solves

Running multiple Hermes agents in the same Discord channel creates a dilemma with no good built-in solution:

| Discord Trigger Mode | Problem |
|---------------------|---------|
| **`require_mention: true`** | ✅ Agents only respond when @mentioned — BUT they see **only the message they were tagged in**, zero context of what anyone else said before. They respond blind. |
| **`trigger: "all"`** | ✅ Agents see every message — BUT they respond to **each other's messages in an infinite loop**, burning tokens until you shut them down. |

You're forced to choose between **agents that are deaf** and **agents that won't shut up**. There is no middle ground in Hermes' built-in config.

#### How This Plugin Solves It

This plugin gives you **both**: agents get full channel context (so they understand what's happening) but only speak when @mentioned (so they don't loop).

```
┌─────────────────────────────────────────────────────┐
│  Discord Channel                                    │
│                                                     │
│  User: "@Furina look at this screenshot"            │
│  Zhongli: "I think it's a bug in run_agent.py"      │
│  Nahida: "Actually the issue is in the compressor"  │
│                                                     │
│  ┌──────────────────────────────────────────────┐   │
│  │  Furina receives @mention                     │   │
│  │                                               │   │
│  │  WITHOUT plugin:                              │   │
│  │   Sees ONLY: "@Furina look at this screenshot"│   │
│  │   → "What screenshot? What are we talking     │   │
│  │      about? I have no context!" 😵            │   │
│  │                                               │   │
│  │  WITH multi-agent-context plugin:              │   │
│  │   Sees: Full channel history injected via     │   │
│  │         pre_llm_call hook                      │   │
│  │   → "Ah! Zhongli says run_agent.py, Nahida    │   │
│  │      says compressor. Let me check both!" 💡   │   │
│  └──────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────┘
```

**How it works under the hood:**

*Discord:*
1. Every time an agent is about to call the LLM (`pre_llm_call` hook), the plugin fetches the last N messages from the current Discord channel/thread via the bot token
2. Formats them into a clean `[Recent Thread/Channel History]` block
3. Injects that block as context into the current turn

*Telegram:*
1. After every LLM response (`post_llm_call` hook), the plugin writes the user message and bot response to a shared SQLite database in WAL mode
2. On the next turn (`pre_llm_call` hook), it reads recent turns from that shared DB
3. Formats them as `[Recent Group History]` and injects them as context

Both platforms: the agent now knows what everyone said — but still only *responds* when triggered by its normal config (mention, keyword, etc.)

**Key features:**
- **Multi-platform (v2.0):** Discord (REST API) and Telegram (shared SQLite) — both work simultaneously
- **Contextvar-aware:** Reads thread/channel/chat IDs from `gateway.session_context` — no hardcoded IDs needed
- **Self-filtering (Discord):** Strips the bot's own messages from history (no echo chamber)
- **Cross-process shared state (Telegram):** SQLite WAL mode enables multiple agent processes to read/write the same DB safely
- **Cached (Discord):** 10-second TTL prevents redundant API calls within the same turn
- **Channel skip list (v2.1):** Exclude specific Discord channels from injection — useful for `free_response_channels` where the agent already sees every message and injection would be redundant
- **Rate-limit handling:** Respects Discord's `429 Retry-After`
- **Mention sanitization:** Strips Discord's `<@id>` formatting for readability
- **Auto-pruning (Telegram):** Messages older than 48 hours are automatically cleaned from the DB

#### Telegram Support (v2.0)

##### The Problem
Telegram's Bot API has **no message history endpoint** — unlike Discord, you can't just fetch recent messages from a group chat. Worse, when running multiple Hermes agent processes (one per bot), they **cannot share in-memory state**: each process has its own Python runtime, so a message received by one agent is invisible to the others.

The result: Telegram agents are deaf to each other, unable to build on what another agent just said.

##### The Solution
A **shared SQLite database** on disk with **WAL (Write-Ahead Logging) mode**, which allows safe concurrent reads and writes across processes:

```
┌───────────────────────────────────────────────────┐
│  Telegram Group Chat                               │
│                                                    │
│  User: "@Zhongli what's the status?"               │
│                                                    │
│  ┌─ Zhongli process ─┐  ┌─ Nahida process ──────┐ │
│  │                    │  │                        │ │
│  │ post_llm_call:     │  │ pre_llm_call:          │ │
│  │   writes turn to ──┼──┼─► reads recent turns  │ │
│  │   shared SQLite    │  │   from shared SQLite   │ │
│  │                    │  │                        │ │
│  │  Nahida now sees:  │  │ "Zhongli: All systems │ │
│  │                    │  │  nominal, PR #42       │ │
│  │                    │  │  merged!"              │ │
│  └────────────────────┘  └────────────────────────┘ │
│                                                    │
│              ┌─── shared SQLite DB ───┐            │
│              │ /root/.hermes/data/    │            │
│              │ multi_agent_tg_shared  │            │
│              │ .db (WAL mode)         │            │
│              └────────────────────────┘            │
└───────────────────────────────────────────────────┘
```

- **`post_llm_call` hook:** After every Telegram turn, writes the triggering user message and the bot's response to the shared DB
- **`pre_llm_call` hook:** Before the next LLM call, reads recent turns from the shared DB and injects them as context
- **WAL mode:** Multiple processes can read/write concurrently without locking each other out
- **Auto-pruning:** Messages older than 48 hours are automatically deleted to keep the DB lean

##### Hooks Registered (v2.0)
The plugin now registers **two hooks**:

| Hook | Trigger | Platforms | Purpose |
|------|---------|-----------|---------|
| `pre_llm_call` | Before every LLM call | Discord + Telegram | Injects channel/group history as context |
| `post_llm_call` | After every LLM response | Telegram only | Persists the turn to the shared SQLite DB |

Both platforms work simultaneously — Discord uses the REST API to fetch history, Telegram uses the shared SQLite database.

#### Quick Install
```bash
cp -r multi-agent-context ~/.hermes/plugins/multi-agent-context
# Add to config.yaml:
#   plugins:
#     enabled:
#       - multi-agent-context
# Keep require_mention: true (or your preferred trigger) in Discord config
# Restart gateway
```

#### Config (Environment Variables)
| Variable | Default | Description |
|----------|---------|-------------|
| `MULTI_AGENT_HISTORY_COUNT` | `20` | Number of recent messages to inject as context (both platforms) |
| `MULTI_AGENT_SKIP_CHANNELS` | *(empty)* | Comma-separated Discord channel IDs to skip injection entirely (e.g. `free_response_channels` that already have full context). Empty = inject everywhere. |
| `DISCORD_BOT_TOKEN` | *(auto-set)* | Discord bot token — set automatically by Hermes |
| `MULTI_AGENT_BOT_NAME` | *(profile name)* | Display name for this bot in Telegram shared history |
| `MULTI_AGENT_TG_DB_PATH` | `/root/.hermes/data/multi_agent_tg_shared.db` | Path to the shared SQLite database |

---

### 3. [`kanban-context/`](./kanban-context/) 🗂️

**Injects recent Kanban board activity into agent context** so agents can see what tasks are moving through the board — without requiring explicit board queries.

#### The Problem

The Hermes Kanban system powers multi-agent work queues with dependency chains, worker claims, and automatic task promotion. But the board lives in a SQLite database that agents never read during conversation. Workers using the `kanban_*` tools see their assigned task, but orchestrators and conversation agents have **zero visibility** into:

- Tasks being created and moving through the pipeline
- Blocked items affecting downstream work
- Completed tasks whose outputs may be useful
- Worker progress notes (heartbeats)

#### The Solution

This plugin hooks into `pre_llm_call` and reads the last N events from the shared Kanban SQLite database. It injects a structured context block before every LLM call:

```
[Recent Kanban Activity]

- [2h ago] [kanban] **Design auth schema** (created → ready)
- [30m ago] [kanban] **Implement auth API** (completed)
- [5m ago] [linkedin-content] **Weekly trends post** (in progress: scraper running)

[End Kanban Activity]
```

#### Key Features

- **Multi-board:** Scans both default and named boards (`kanban/boards/*/kanban.db`)
- **Chronological merge:** Events from all boards are sorted by time
- **13 event kinds recognised:** created, assigned, claimed, completed, blocked, unblocked, heartbeat, spawned, archived, commented, linked, edited, promoted
- **No extra dependencies:** Uses Python stdlib (`sqlite3`, `json`, `os`)
- **Path resolution via `get_hermes_home()`:** Works with any profile or `HERMES_HOME` override

#### Relationship to multi-agent-context

The `multi-agent-context` plugin (above) shares conversation history across Telegram/Discord bots. `kanban-context` complements it by sharing **board** history — together they give agents both conversational and operational context.

#### Config (Environment Variables)

| Variable | Default | Description |
|----------|---------|-------------|
| `KANBAN_CONTEXT_EVENT_LIMIT` | `10` | Max events to inject per pre-LLM context block |
| `KANBAN_CONTEXT_LOOKBACK_H` | `12` | Lookback window in hours |

#### Quick Install

```bash
cp -r kanban-context ~/.hermes/plugins/kanban-context
# Add to config.yaml:
#   plugins:
#     enabled:
#       - kanban-context
# Restart gateway
```

For multi-profile setups, symlink or copy into each profile's plugins dir. See `kanban-context/README.md` for details.

---

### 4. ~~[`native-vision/`](./native-vision/)~~ ⚡ — ⚠️ DEPRECATED

> ⚠️ **Now a built-in feature in Hermes Agent v0.11.0+ — this plugin is no longer needed. Kept here for historical reference.**

Bypass the auxiliary vision model and send images directly to vision-capable main LLMs (GPT-4o, Claude Sonnet 4, GLM-5V-Turbo, etc.).

- **What it solved:** Hermes routes all image analysis through an aux vision model (e.g., qwen-vl), even when your main model can see images natively. This adds latency, cost, and information loss (text description ≠ seeing pixels).
- **How it worked:** Runtime monkey-patching with signature-gated defensive checks. Inserts `[NATIVE_VISION_IMAGES:...]` markers into the text pipeline, then expands them into multimodal content blocks before the API call.
- **Survives updates:** If Hermes changes a method signature, that patch silently skips itself instead of crashing.
- **Patches 5 methods** across `gateway.run`, `cli`, and `run_agent` — all via `register(ctx)`.

#### Config (`plugin.yaml`) — For Reference Only
| Setting | Default | Description |
|---------|---------|-------------|
| `native_vision_enabled` | `true` | Master on/off toggle |
| `max_image_dimension` | `1024` | Resize max side in px (saves tokens) |
| `max_total_image_tokens` | `100000` | Token budget for all images combined |
| `vision_models` | *(see file)* | Model name allowlist (substring match) |

### 5. [`heartbeat/`](./heartbeat/) 💓

**Proactive scheduled agent runner — wakes a cheap LLM on a schedule, runs your checks, and only notifies you when something needs attention.**

A Hermes Agent plugin that spawns a lightweight subprocess agent on a configurable interval. It reads your `HEARTBEAT.md` as instructions (the prompt IS the program), runs with full agent tools, and stays silent when there's nothing to report (`NOTHING_TO_FLAG` suppression).

#### How It Works

```
1. Timer fires (every 30m, configurable)
2. Plugin reads HEARTBEAT.md from disk
3. Plugin reads last state from SQLite (what was already flagged)
4. Plugin spawns `hermes chat` subprocess with a cheap model
5. Subprocess runs with FULL agent tools — calls whatever HEARTBEAT.md told it to
6. Response comes back:
   - NOTHING_TO_FLAG → silent, no delivery
   - Findings → delivered to user's chat automatically
```

**Architecture:**
- **Background scheduler** — daemon thread polls every 10 seconds, guarded by cross-process `flock` (only one process runs the scheduler)
- **Subprocess execution** — each heartbeat runs as an isolated `hermes chat` process with a cheap model override (not your main agent model)
- **SQLite state** — persistent storage at `~/.hermes/data/heartbeat.db` (WAL mode)
- **Quiet hours** — heartbeats skip during quiet periods and reschedule automatically
- **Per-heartbeat locks** — prevents overlapping runs of the same heartbeat
- **Global concurrency semaphore** — limits simultaneous subprocess count (each loads ~500MB)

#### Tools

| Tool | Description |
|------|-------------|
| `heartbeat_create` | Create a recurring heartbeat with name, schedule, model, quiet hours, etc. |
| `heartbeat_list` | List all heartbeats with status, last run, next run, and run count |
| `heartbeat_run` | Manually trigger a heartbeat immediately (ignores schedule and quiet hours) |

#### Config (`plugin.yaml`)

| Setting | Default | Description |
|---------|---------|-------------|
| `default_interval` | `1800` | Default interval in seconds (30 min) |
| `default_model` | `deepseek/deepseek-v4-flash` | Cheap model for heartbeat runs |
| `default_prompt_file` | `~/.hermes/HEARTBEAT.md` | Default prompt file path |
| `quiet_hours_start` | `23:00` | Start of quiet period (24h format) |
| `quiet_hours_end` | `07:00` | End of quiet period (24h format) |
| `timezone` | `America/New_York` | Timezone for quiet hours |
| `state_ttl_hours` | `48` | How long to keep state entries |
| `log_retention_days` | `30` | How long to keep run logs |

---

## Requirements

- **Hermes Agent v0.11.0+** with plugin system support
- Python 3.11+
- **`async-delegate`:** No extra dependencies — uses Python stdlib only
- **`multi-agent-context`:** `pip install requests` (usually already installed). Telegram path uses Python's built-in `sqlite3` — no extra deps.
- **`kanban-context`:** No extra dependencies — uses Python stdlib only (`sqlite3`, `json`, `os`)
- **`heartbeat`:** No extra dependencies — uses Python stdlib only (`sqlite3`, `threading`, `subprocess`, `fcntl`)

## Deployment Notes

⚠️ **Agent plugins must live in per-profile directories:**

```bash
# Global plugin location (for reference)
~/.hermes/plugins/<plugin-name>/

# Each agent needs its own copy or symlink:
for agent in furina raiden zhongli nahida; do
  mkdir -p ~/.hermes/profiles/${agent}/plugins/
  ln -sf ~/.hermes/plugins/<plugin-name> \
          ~/.hermes/profiles/${agent}/plugins/<plugin-name>
done
```

Then enable in **each** agent's `~/.hermes/profiles/{agent}/config.yaml`.

See the [Hermes Plugin Development Guide](https://github.com/NousResearch/hermes-agent/blob/main/.hermes/skills/devops/hermes-plugin-development/SKILL.md) for full details on the plugin system.

## License

MIT — use freely, modify freely, contribute back if you'd like! 🎭
