# Hermes Community Plugins 🎭

Battle-tested plugins for [Hermes Agent](https://github.com/NousResearch/hermes-agent) — **zero core patches required**. Drop in, enable, restart.

## Plugins

### 1. [`native-vision/`](./native-vision/) ⚡
Bypass the auxiliary vision model and send images directly to vision-capable main LLMs (GPT-4o, Claude Sonnet 4, GLM-5V-Turbo, etc.).

- **What it solves:** Hermes routes all image analysis through an aux vision model (e.g., qwen-vl), even when your main model can see images natively. This adds latency, cost, and information loss.
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
- `native_vision_enabled` — on/off toggle (default: `true`)
- `max_image_dimension` — resize max side in px (default: `1024`)
- `max_total_image_tokens` — token budget for images (default: `100000`)
- `vision_models` — allowlist of model names that support vision

---

### 2. [`multi-agent-context/`](./multi-agent-context/) 🤝
Injects Discord channel/thread history into agent context so agents can see what other agents said.

- **What it solves:** In multi-agent setups, each agent operates blind — they only see messages sent directly to them. This plugin gives them awareness of what others have said recently.
- **How it works:** Uses the `pre_llm_call` hook to fetch recent Discord messages via the bot token and injects them as context before each LLM call.
- **Contextvar-aware:** Reads thread/channel IDs from `gateway.session_context` (no hardcoded channel IDs needed).
- **Cached:** 10-second TTL prevents redundant API calls within the same turn.

#### Quick Install
```bash
cp -r multi-agent-context ~/.hermes/plugins/multi-agent-context
# Add to config.yaml:
#   plugins:
#     enabled:
#       - multi-agent-context
# Restart gateway
```

#### Config (Environment Variables)
- `MULTI_AGENT_HISTORY_COUNT` — number of recent messages to fetch (default: `20`)
- `DISCORD_BOT_TOKEN` — set automatically by Hermes (no manual config needed)

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
