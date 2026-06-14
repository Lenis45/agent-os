# amori-infra — инвентарь (single source of truth)

Последнее обновление: 2026-06-11 · хост: **Mac-mini.local** (прод-ядро, 24/7)

> Это фактическое состояние системы, а не план. Архитектурное видение — в
> `amori_agent/docs/amori-infra-architecture-v3.html`. При расхождении приоритет у ЭТОГО файла.

## Узлы
| Узел | Tailscale IP | Роль |
|---|---|---|
| mac-mini (этот) | 100.66.130.21 | прод-ядро: Docker, агенты, n8n, бэкап |
| denis-k | 100.77.9.84 | GPU-нода: Ollama (:11434), ComfyUI (:8188) |
| macbook-air | 100.90.154.18 | workstation |
| One Touch (USB) | — | внешний 1TB exFAT, off-site бэкап (`/Volumes/One Touch/amori-backups`) |

## Docker-контейнеры (`~/ai-infra/docker-compose.yml`)
| Контейнер | Образ | Порт | Назначение |
|---|---|---|---|
| ai_postgres | postgres:16-alpine | 5432 | БД: `agents`, `ops_db`, `n8n` |
| ai_qdrant | qdrant/qdrant | 6333/6334 | векторная память |
| ai_redis | redis:7-alpine | 6379 | кэш/очереди |
| ai_langfuse | langfuse/langfuse:2 | 3000 | LLM observability |
| ai_n8n | n8nio/n8n | 5678 | workflow-оркестратор |

## Базы данных (в ai_postgres, user `agent_user`)
- **`agents`** — app-данные + схема Langfuse (исторически смешаны). Таблицы: leads,
  chief_digests, conversations, pending_actions, support_*, team_members, task_*, known_entities + Langfuse.
- **`ops_db`** (NEW v3.0) — операционка/observability: `llm_usage`, `tier1_sessions`,
  `budget_config`, `infra_runs`, `infra_heartbeats`.
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
| **infra_monitor** (NEW) | ai.monitor | ежечасно | мониторинг всей инфры → Telegram при проблемах |
| **backup** (NEW) | amori.backup | 4:00 | бэкап + off-site + ротация логов |
| **restore_test** (NEW) | ai.restoretest | 1-е число 5:00 | проверка восстановимости бэкапа |
| **digest** (NEW) | ai.digest | Пн 9:00 | еженедельная сводка инфры |
| calendar_agent / task_sync / lead_manager | — | НЕ в расписании | есть код, плановый запуск не настроен |

## Библиотеки (общие, не агенты)
- `router.py` — выбор модели per-agent (Groq/Ollama) + бюджет-гард.
- `cost_guard.py` — учёт LLM-расходов + предохранитель платного API.
- `tier1_log.py` — лог ручных Claude/GPT сессий.
- `ops_store.py` — доступ к ops_db + heartbeat/runs.
- `notify.py` — единая отправка в Telegram (и для bash, и для python).
- `memory.py` — Qdrant + PG память.

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
- `~/ai-infra/agents/.env` — все токены (TG, WEEEK, Groq, Gemini, Yandex, POSTGRES_PASSWORD).
  Права: должны быть `600`. Файлы `.en`/`.env.save` — мусор/бэкапы, проверить и удалить.
- Compose содержит PG-пароль и N8N_ENCRYPTION_KEY в открытом виде (план: secret-backend Infisical/OpenBao).

## Сервисные URL
langfuse http://localhost:3000 · n8n http://localhost:5678 · qdrant http://localhost:6333/dashboard
