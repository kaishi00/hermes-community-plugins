# kanban-context plugin 🗂️

Injects recent Kanban board activity + cross-bot messaging into agent context via the `pre_llm_call` hook.

## Two Features

### 1. Kanban Activity Injection

Gives every agent awareness of what work items are flowing through the board.

### 2. Cross-Bot Messaging (v2.0)

Because Telegram bots cannot see messages from other bots (hard API limitation), this plugin implements a **cross-bot message bus** using the Kanban board + a shared SQLite ``outbox`` table.

**How it works:**

1. **Bot A** (sender) writes a message to the shared ``outbox`` table and creates a Kanban task assigned to **Bot B**
2. The **Kanban dispatcher** picks up the task and spawns a worker for Bot B
3. **Bot B** reads the task body (= the message), processes it, and can respond
4. **Bot B** marks the outbox as ``done`` and completes the Kanban task with a summary

This gives full transparency: every cross-bot exchange is tracked both in the SQLite outbox and in the Kanban board.

**API for plugins/scripts:**

```python
from plugins.kanban_context import crossbot_send, crossbot_respond, crossbot_get_history

# Send a message
outbox_id = crossbot_send(
    to_bot="ti",
    subject="Check plugin version",
    body="Please verify the plugin.yaml version matches __init__.py",
    kanban_task_id="t_abc123"
)

# Respond to a message
crossbot_respond(outbox_id, "All versions match. Done!")
```

### Relationship to multi-agent-context

The existing `multi-agent-context` plugin shares conversational history via a shared SQLite DB. `kanban-context` complements it by sharing **board** activity + **cross-bot messages**. Together they give agents conversational context, operational context, AND a reliable bot-to-bot messaging channel.

## Requirements

- Hermes Agent v0.13.0+ with plugin system
- Python 3.11+
- No extra dependencies (stdlib only)

## Install

```bash
cp -r kanban-context ~/.hermes/plugins/kanban-context
```

Add to your profile's `config.yaml`:
```yaml
plugins:
  enabled:
    - kanban-context
```

Restart the gateway.

## Configuration (Environment Variables)

| Variable | Default | Description |
|----------|---------|-------------|
| `KANBAN_CONTEXT_EVENT_LIMIT` | `10` | Max events to inject per pre-LLM context block |
| `KANBAN_CONTEXT_LOOKBACK_H` | `12` | Lookback window in hours |
| `CROSSBOT_BOT_NAME` | *(profile name)* | This bot's name for outbox addressing |
| `MULTI_AGENT_TG_DB_PATH` | `$HERMES_HOME/data/multi_agent_tg_shared.db` | Shared SQLite DB path |

