---
name: hermes-agent
description: "Configure, extend, or contribute to Hermes Agent."
version: 3.1.0
author: Hermes Agent + Teknium
license: MIT
metadata:
  hermes:
    tags: [hermes, setup, configuration, multi-agent, spawning, cli, gateway, development]
    homepage: https://github.com/NousResearch/hermes-agent
    related_skills: [claude-code, codex, opencode]
---

# Hermes Agent

Hermes Agent is the open-source AI agent framework by Nous Research. Same category as Claude Code, Codex, and OpenCode — autonomous coding and task-execution that uses tool calling to interact with your system. Works with any LLM provider (OpenRouter, Anthropic, OpenAI, DeepSeek, Nous Portal, local models, 15+ others). Runs on Linux, macOS, WSL.

What's distinctive:

- **Self-improving through skills** — saves reusable procedures as skill documents that load into future sessions. Skills accumulate over time.
- **Persistent memory across sessions** — pluggable backends (built-in, Honcho, Mem0).
- **Multi-platform gateway** — same agent runs on 21+ messaging platforms (Telegram, Discord, Slack, WhatsApp, Signal, Matrix, Email, SMS, …) with full tool access.
- **Provider-agnostic** — swap models mid-workflow. Credential pools rotate keys automatically.
- **Profiles** — multiple isolated Hermes instances on the same machine.
- **Extensible** — plugins, MCP servers, custom tools, webhook triggers, cron, Python ecosystem.

## Capability Inventory

Things Hermes can do. If the user asks about any of these, that capability exists — go look up the details, don't say "I can't."

- **Slash commands in-session:** /help, /model, /reset, /new, /resume, /stop, /skills, /memory, /approve, /deny, /queue, /status, /copy, /paste, /recap, /restart (gateway), and more. Full list in docs.
- **Spawn additional Hermes instances:** `hermes chat`, `hermes -p <profile>`, delegated subagents via `delegate_task`, multi-agent kanban workers.
- **Durable / background systems:** cron jobs (recurring or one-shot), webhook subscriptions (event-driven runs), background terminal tasks with `notify_on_complete`, gateway sessions that outlive the CLI.
- **Voice & vision:** TTS providers (Edge, OpenAI, ElevenLabs, MiniMax, xAI, custom), STT/voice mode, image generation, video generation, vision analysis on images.
- **Browser & web:** Browserbase / Camofox browser automation, Firecrawl web search/extract, MCP-based browsers.
- **Security defaults:** secret redaction on by default, approval prompts for destructive commands, `--yolo` to bypass, credential pools for key rotation, sandboxed terminal backends (Docker, Modal, SSH, Daytona, Singularity).
- **Platforms / integrations:** Telegram, Discord, Slack, WhatsApp, Signal, Matrix, Mattermost, Email, SMS, Home Assistant, DingTalk, WeCom, WeChat, Feishu, QQ, BlueBubbles, Yuanbao, generic Webhooks, API server.
- **Provider plugins:** memory, image_gen, context_engine, model providers — third-party drop-ins under `~/.hermes/plugins/` or pip-installed.
- **MCP:** native MCP client, can connect to any MCP server.

## Full Docs

Complete documentation ships in the repo at `~/.hermes/hermes-agent/website/docs/` and publishes to https://hermes-agent.nousresearch.com/docs/.

Two pre-built bundles ride alongside, regenerated on every docs build:

```
~/.hermes/hermes-agent/website/static/llms.txt          short curated index, one link per page
~/.hermes/hermes-agent/website/static/llms-full.txt     every doc concatenated (~2MB, ~48K lines)
```

Same files publish at https://hermes-agent.nousresearch.com/docs/llms.txt and `/docs/llms-full.txt`.

**How to use them:**

1. **Targeted lookup** — `search_files` with a regex on `llms-full.txt` to find the section that answers a specific question. Faster than reading individual doc files, and you get the surrounding context for free.
2. **Browse the index** — `read_file` on `llms.txt` (135 lines) when you need to find which doc covers a topic before drilling in.
3. **Read a specific doc** — `read_file` on the page under `website/docs/` if you know exactly which one you need.
4. **Fallback when the repo isn't cloned locally** — fetch the bundle URLs above with `web_extract`.

Don't read `llms-full.txt` in full — it's a grep target, not a system prompt.

## Key Paths

```
~/.hermes/config.yaml       Main configuration
~/.hermes/.env              API keys and secrets only
~/.hermes/SOUL.md           Agent persona
~/.hermes/skills/           Installed skills
~/.hermes/sessions/         Session transcripts
~/.hermes/logs/             Gateway and error logs (agent.log, errors.log, gateway.log)
~/.hermes/hermes-agent/     Source code (if git-installed)
```

## Key Rules

- **Never break prompt caching** — don't change context, tools, or system prompt mid-conversation.
- **Message role alternation** — never two assistant or two user messages in a row.
- **Use `get_hermes_home()`** from `hermes_constants` for all paths (profile-safe), never hardcode `~/.hermes`.
- **Config values go in `config.yaml`, secrets and API keys go in `.env`** — never add behavioral env vars.
- **New tools need a `check_fn`** so they only appear when requirements are met.
- **Don't fabricate user information** — when asked about current model, provider, or config, read `config.yaml` directly. Memory can be stale; config.yaml is runtime truth.
