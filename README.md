# Hermes Community Plugins 🎭

Battle-tested plugins for [Hermes Agent](https://github.com/NousResearch/hermes-agent) — **zero core patches required**. Drop in, enable, restart.

## Plugins

### 1. [`native-vision/`](./native-vision/) ⚡
Bypass the auxiliary vision model and send images directly to vision-capable main LLMs (GPT-4o, Claude Sonnet 4, GLM-5V-Turbo, etc.).

- **What it solves:** Hermes routes all image analysis through an aux vision model (e.g., qwen-vl), even when your main model can see images natively. This adds latency, cost, and information loss (text description ≠ seeing pixels).
- **How it works:** Runtime monkey-patching with signature-gated defensive checks. Inserts `[NATIVE_VISION_IMAGES:...]` markers into the text pipeline, then expands them into multimodal content blocks before the API call.
- **Survives updates:** If Hermes changes a method signature, that patch silently skips itself instead of crashing.
- **Patches 5 methods** across `gateway.run`, `cli`, and `run_agent` — all via `register(ctx)`.

#### Quick Install
```bash
cp -r native-vision ~/.hermes/plugins/native-vision
# Add to config.yaml:
#   plugins:
#     enabled:
#       - native-vision
# Restart gateway
```

#### Config (`plugin.yaml`)
| Setting | Default | Description |
|---------|---------|-------------|
| `native_vision_enabled` | `true` | Master on/off toggle |
| `max_image_dimension` | `1024` | Resize max side in px (saves tokens) |
| `max_total_image_tokens` | `100000` | Token budget for all images combined |
| `vision_models` | *(see file)* | Model name allowlist (substring match) |

---

### 2. [`multi-agent-context/`](./multi-agent-context/) 🤝
Injects Discord channel/thread history into agent context so agents can see what other agents said — **without triggering infinite reply loops.**

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
1. Every time an agent is about to call the LLM (`pre_llm_call` hook), the plugin fetches the last N messages from the current Discord channel/thread via the bot token
2. Formats them into a clean `[Recent Thread/Channel History]` block
3. Injects that block as context into the current turn
4. The agent now knows what everyone said — but still only *responds* when triggered by its normal config (mention, keyword, etc.)

**Key features:**
- **Contextvar-aware (v1.8):** Reads thread/channel IDs from `gateway.session_context` — no hardcoded channel IDs needed
- **Self-filtering:** Strips the bot's own messages from history (no echo chamber)
- **Cached:** 10-second TTL prevents redundant API calls within the same turn
- **Rate-limit handling:** Respects Discord's `429 Retry-After`
- **Mention sanitization:** Strips Discord's `<@id>` formatting for readability

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
| `MULTI_AGENT_HISTORY_COUNT` | `20` | Number of recent messages to fetch |
| `DISCORD_BOT_TOKEN` | *(auto-set)* | Set automatically by Hermes |

---

## Requirements

- **Hermes Agent v0.11.0+** with plugin system support
- Python 3.11+
- **`native-vision`:** `pip install Pillow`
- **`multi-agent-context`:** `pip install requests` (usually already installed)

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
