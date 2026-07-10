---
name: agent-tooling-audit
description: Audit, update, and verify the local AI agent tooling layer across Codex, Claude Code, Hermes, and OpenCode. Use when checking versions, shared skills, MCP servers, plugins, provider safety, or agent utility drift on this Mac.
metadata:
  short-description: Audit local agent CLIs, skills, MCP, and plugins
---

# Agent Tooling Audit

Use this skill when the task is about the local agent stack: Codex, Claude Code, Hermes,
OpenCode, shared skills, MCP servers, plugins, provider config, or utility drift.

## Safety rules

- Never print secrets from config files. Treat API keys, provider tokens, Telegram tokens,
  session cookies, `.env` values, and private channel IDs as sensitive.
- Prefer structural checks: command presence, version, enabled/disabled state, file paths,
  counts, and redacted config summaries.
- Before updating a managed checkout, check `git status -sb` and preserve local changes with
  the tool's native backup/stash path.
- Do not install random community skills or MCP servers without source review. Prefer official
  documentation and locally maintained skills.

## Standard audit order

1. Check versions:
   - `codex --version`
   - `claude --version`
   - `hermes --version`
   - `opencode --version`
2. Check configured MCP:
   - `codex mcp list`
   - `claude mcp list`
   - `hermes mcp list`
   - `opencode debug config`
3. Check shared skill directories:
   - `~/.codex/skills`
   - `~/.claude/skills`
   - `~/.agents/skills`
   - `~/.config/opencode/skills`
   - Hermes `skills.external_dirs`
4. Run the repo doctor when available:
   - `/Users/denis/ai-infra/scripts/agent_tooling_doctor.sh`
5. Run sync after adding or updating local reusable skills:
   - `/Users/denis/ai-infra/scripts/sync_agent_skills.sh`

## Update policy

- Codex Desktop bundled CLI: verify with `codex doctor`; update through the desktop app when
  `codex update` cannot detect the install method.
- Claude Code: use `claude update`, then verify `claude --version`.
- Hermes: use `hermes update --backup --yes`, then verify `hermes --version`,
  `hermes skills list`, and `git -C ~/.hermes/hermes-agent status -sb`.
- OpenCode: install or update with the official Homebrew tap on macOS:
  `brew install anomalyco/tap/opencode` or `brew upgrade anomalyco/tap/opencode`.

## What good looks like

- All four CLIs are callable.
- Shared skills exist in `~/.agents/skills` and are mirrored into Codex, Claude Code, and
  OpenCode.
- Hermes scans `~/.agents/skills` through `skills.external_dirs`.
- MCP servers are intentionally small: Amori, memory, sequential-thinking, plus optional fetch.
- OpenCode has `.env` protection and asks before edits/destructive shell actions.
- No secret-looking values appear in tracked docs, scripts, skills, or public config templates.
