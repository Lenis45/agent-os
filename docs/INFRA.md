# amori-infra — инвентарь (single source of truth)

Последнее обновление: 2026-08-16 · хост: **Mac-mini.local** (прод-ядро, 24/7)

> Это краткий инвентарь. Полное проверенное состояние и известные ограничения:
> [`SYSTEM_BASELINE_2026-08-12.md`](SYSTEM_BASELINE_2026-08-12.md).

## Узлы
| Узел | Tailscale IP | Роль |
|---|---|---|
| mac-mini (этот) | 100.66.130.21 | прод-ядро: Docker, агенты, n8n, бэкап |
| denis-k | 100.77.9.84 / fd7a:115c:a1e0::b43b:954 | GPU-нода: Ollama (:11434), ComfyUI (:8188) |
| macbook-1 | 100.95.200.39 / fd7a:115c:a1e0::aa3b:c828 | workstation; SSH работает по Tailscale IPv6 |
| msi | 100.90.154.18 | дополнительная Windows-нода |
| One Touch (USB) | — | внешний 1TB exFAT, off-site бэкап (`/Volumes/One Touch/amori-backups`) |

MacBook не запускает production-агентов. Устаревшая копия от мая-июля 2026 года,
которая дублировала Telegram-ботов и отправляла ложный `Health Check`, отключена
16.08.2026 и сохранена локально как `~/ai-infra.disabled-legacy-20260816`.

## Docker-контейнеры (`~/ai-infra/docker-compose.yml`)
| Контейнер | Образ | Порт | Назначение |
|---|---|---|---|
| ai_postgres | postgres:16-alpine (digest pinned) | `127.0.0.1:5432` | БД: `agents`, `ops_db`, `customer_db`, `n8n` |
| ai_qdrant | qdrant/qdrant:v1.18.0 (digest pinned) | `127.0.0.1:6333/6334` | векторная память |
| ai_redis | redis:7-alpine (digest pinned) | `127.0.0.1:6379` | кэш/очереди |
| ai_langfuse | langfuse/langfuse:2.95.11 (digest pinned) | `127.0.0.1:3000` | LLM observability |
| ai_n8n | n8nio/n8n:2.25.7 (digest pinned) | `127.0.0.1:5678` | workflow-оркестратор |

## Базы данных (в ai_postgres, user `agent_user`)
- **`agents`** — личная/командная память, дайджесты и исторические agent-таблицы.
- **`ops_db`** — операционка/observability: проекты, очередь задач, отчёты, контент,
  `llm_usage`, `budget_config`, `infra_runs`, `infra_heartbeats`.
- **`customer_db`** — отдельный клиентский контур: лиды и support tickets/messages.
- **`n8n`** — workflow-движок.

## Qdrant-коллекции
`project_knowledge`, `shared_memory` (384-dim, all-MiniLM-L6-v2).

## Агенты (`~/ai-infra/agents/*.py`, интерпретатор `/opt/anaconda3/bin/python3`)
| Агент | launchd label | Расписание | Что делает |
|---|---|---|---|
| orchestrator (Emilia) | ai.orchestrator | 24/7 | главный ассистент (TG, голос, инструменты) |
| support_agent | amori.support | 24/7 | клиентский бот |
| knowledge_curator | knowledge.curator | 24/7 | Obsidian vault, переводы |
| chief_of_staff | chief.of.staff | 9:00, 19:00 | дайджест команды из TG |
| email_watchdog | email.watchdog | 8:00 | IMAP → важное → Obsidian |
| **infra_monitor** | ai.monitor | ежечасно в `:07` | мониторинг → Telegram; подпись хоста, дедупликация 6ч, recovery |
| **backup** (NEW) | amori.backup | 4:00 | бэкап + off-site + ротация логов |
| **restore_test** | ai.restoretest | Сб 5:00 | еженедельная проверка восстановимости бэкапа |
| **storage maintenance** | ai.storage-maintenance | ежедневно 3:00 | чистит воспроизводимые кэши; определяет подготовленное обновление macOS |
| **digest** (NEW) | ai.digest | Пн 9:00 | еженедельная сводка инфры |
| calendar_agent | ai.calendar-digest | ежедневно 8:00 | встречи на сегодня + календарь на неделю в чате Emilia |
| task_sync / lead_manager | cron | по расписанию | задачи и CRM-отчёты |

## Библиотеки (общие, не агенты)
- `router.py` — local-first выбор модели per-agent; внешние API только по opt-in.
- `llm.py` — Ollama text/vision + bridge в `amori-ai` для Codex/Claude subscriptions.
- `cost_guard.py` — учёт LLM-расходов + предохранитель платного API.
- `tier1_log.py` — лог ручных Claude/GPT сессий.
- `ops_store.py` — доступ к ops_db + heartbeat/runs.
- `notify.py` — единая отправка в Telegram (и для bash, и для python).
- `memory.py` — Qdrant + PG память.

## LLM / локальные модели
- Основной endpoint: Mac Ollama `http://127.0.0.1:11434`; Windows GPU-нода больше
  не является обязательной для работоспособности агентов.
- `qwen3:1.7b` — классификация сложности и быстрые текстовые ответы.
- `qwen3-vl:2b` — локальный анализ изображений для Emilia.
- `amori-hermes:4b` — отдельный Hermes-профиль; прямой быстрый чат идёт через
  `amori-ai`, чтобы не загружать полный tool prompt Hermes на каждый простой вопрос.
- `amori-ai`: local → Codex CLI для кода/действий → Claude Code для архитектуры,
  требований, глубокого анализа и актуального research.
- Codex и Claude используют OAuth существующих подписок. Это не отдельные API-вызовы,
  но на них действуют лимиты соответствующих планов.
- DeepSeek/OpenModel, Gemini, Groq и browser proxies отключены по умолчанию.
  Внешний API-fallback возможен только при `ALLOW_EXTERNAL_LLM_FALLBACK=1`.
- Groq text fallback: `openai/gpt-oss-120b`. Удалённый
  `llama-3.3-70b-versatile` принимается только как legacy-настройка и автоматически
  нормализуется в актуальную модель.
- Windows Ollama/ComfyUI остаётся опциональной тяжёлой нодой; её проверяет
  `python3 ~/ai-infra/scripts/check_remote_ollama.py`, но её выключение не роняет Mac-контур.

## Agent tooling / skills / MCP
- Shared skills source: `~/.agents/skills` (17 личных skills Amori/Codex workflow).
- Синхронизация: `~/ai-infra/scripts/sync_agent_skills.sh`.
- Doctor: `~/ai-infra/scripts/agent_tooling_doctor.sh`.
- Codex MCP: `amori`, `memory`, `sequential-thinking`, `node_repl`, `sites-design-picker`.
- Claude MCP: `amori`, `memory`, `sequential-thinking`, `github`.
- Hermes MCP: `amori`, `memory`, `sequential-thinking`, `filesystem`, `fetch`.
- OpenCode MCP: `amori`, `memory`, `sequential-thinking`, `fetch`; OpenCode also reads
  `~/.agents/skills` and the local skill collection.

## Файловая система
```
~/ai-infra/
├── agents/                 # python-агенты + библиотеки + .env + .session (КРИТИЧНО)
├── backups/
│   ├── backup.sh           # ежедневный бэкап (+off-site +ротация логов)
│   ├── restore_test.sh     # проверка восстановления
│   └── local/<stamp>/      # снимки + status.json
├── n8n/
│   ├── build_agent_map.py  # генератор карты агентов
│   └── workflows/*.json
├── docs/                   # INFRA.md · PRINCIPLES.md · RUNBOOK.md
├── docker-compose.yml
└── V3_IMPLEMENTATION.md
~/Knowledge_base/           # Obsidian vault
~/Library/LaunchAgents/     # *.plist (расписание агентов)
/Volumes/One Touch/amori-backups/   # off-site копии
```

## Секреты
- `~/ai-infra/agents/.env` — интеграционные secrets (TG, WEEEK, Google, DB); LLM API keys optional/disabled.
  Права: должны быть `600`. Файлы `.en`/`.env.save` — мусор/бэкапы, проверить и удалить.
- Compose получает пароли и encryption keys из untracked корневого `.env`; LaunchAgent
  plist не содержат токены. Следующий уровень — отдельный local secret backend.

## Сервисные URL
dashboard http://localhost:8099 · office http://localhost:5070 · langfuse http://localhost:3000 · n8n http://localhost:5678 · qdrant http://localhost:6333/dashboard
