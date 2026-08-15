# Amori Unified Intelligence Gateway

The gateway gives Telegram, Hermes, OpenCode, and terminal clients one durable execution path.

## Request lifecycle

`accepted -> routed -> awaiting_confirmation (actions only) -> queued/waiting_for_device -> running -> verifying -> completed`

Terminal states are `completed`, `partial`, `failed`, and `cancelled`. Workers use a renewable 90-second lease. A cancelled request terminates its model CLI process and cannot be overwritten by a late result.

## Security boundary

- The Broker binds to the Mac Mini Tailscale address, not to the public interface.
- Every request, worker call, and artifact download requires a bearer token.
- The token lives in `~/.config/amori/broker_token` with mode `0600` and is never committed.
- Input documents are stored under `~/.local/share/amori/artifacts`, extracted as untrusted data, and expire after 30 days.
- `act` requests wait for explicit confirmation before a worker can claim them.
- Artifact discovery is limited to the selected workspace and excludes secret-looking files.
- Remote image URLs must use HTTPS and match the Qwen/Alibaba CDN allowlist. The worker validates image magic bytes and a 25 MB size limit before storing a result.

## Services

```bash
launchctl print gui/$UID/ai.request-broker
launchctl print gui/$UID/ai.request-worker
launchctl print gui/$UID/com.denis.freeqwenapi
curl -fsS http://100.66.130.21:8110/health
curl -fsS http://127.0.0.1:3264/api/status
```

Emilia uses:

```text
AMORI_BROKER_ENABLED=1
AMORI_BROKER_URL=http://100.66.130.21:8110
```

## Telegram flow

1. Send text, voice, or a document to Emilia.
2. Emilia creates an idempotent request and edits one progress message.
3. Read-only work starts immediately. Side effects wait for `ДА`.
4. The result and generated files return to the same Telegram chat.
5. Use `/jobs`, `/files`, `/cancel`, `/context`, and `/new` for control.

## Entry points

| Entry point | Command or UI | Result delivery |
| --- | --- | --- |
| Emilia | Telegram text, voice, image, or document | The same chat, including output files |
| Hermes Desktop | `smart_request` MCP tool or `amori-hermes` | Hermes response or local output directory |
| OpenCode | Primary agent `amori` | Exact Broker result; actions require a separate `ДА` or `НЕТ` |
| Terminal | `amori-request "question"` | stdout plus `.amori-results/<request-id>/` |

`ask` is read-only. `act` changes files or external state and cannot be claimed before confirmation. OpenCode keeps pending confirmation in the active session. Terminal automation must pass `--act --yes` explicitly; without `--yes`, an interactive terminal asks first and a non-interactive process cancels safely.

## Model and execution policy

- Hermes/Ollama handles short private answers first.
- Claude Code handles architecture, product reasoning, and current-information research.
- Codex handles code implementation, debugging, tests, browser QA, and repository work.
- Native handlers own calendar, CRM, email, notes, content-factory, and project-team side effects.
- Image generation uses the local Qwen Chat bridge and must return a real validated image artifact. A text-only claim never counts as success.

## Image provider recovery

The Qwen bridge uses an existing Qwen Chat account and no separate API key. If the dashboard shows `Автогенерация изображений недоступна` or `/api/status` reports `INVALID`, refresh the account session:

```bash
cd ~/ai-infra/FreeQwenApi
npm run auth
launchctl kickstart -k gui/$UID/com.denis.freeqwenapi
curl -fsS http://127.0.0.1:3264/api/status
```

This is the only account-interactive recovery step. Do not place Qwen cookies or tokens in git.

## MacBook worker

When the MacBook is reachable over Tailscale, install `ai-devkit`, copy the Broker token to `~/.config/amori/broker_token` with mode `0600`, and load `deploy/launchd/ai.gateway-worker.plist`. Requests targeted to an offline MacBook remain in `waiting_for_device` instead of pretending to run.

## Recovery

Requests and events are persisted in `ops_db`. If a worker disappears, an expired lease is retried once and then fails with `worker_lost`. Repeating the same Telegram message ID does not duplicate the request.

The dashboard section `Запросы AI` is the operator view for queue depth, confirmations, failures, worker freshness, target devices, and cancellation. It intentionally shows shortened prompts; bearer tokens, stored paths, and document contents are never returned there.
