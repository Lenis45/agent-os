# amori-infra v3.0 — реализация (инкремент 1)

Дата: 2026-06-11 · хост: Mac-mini.local (прод-ядро)

Внедрены безопасные, аддитивные, проверенные части архитектуры v3.0.
Ничего из работающего не сломано: `router.get_model()` обратносовместим,
orchestrator продолжал работать во время внедрения.

## Что сделано (реально, проверено)

### 1. Изоляция операционных данных — новая БД `ops_db`
Отдельная база в том же `ai_postgres`, чтобы app/observability-данные не
смешивались со схемой Langfuse в базе `agents`. Принцип разделения из v3.0,
без риска для существующих данных.
- `agents/ops_store.py` — подключение + идемпотентная схема (`init()`).
- Таблицы: `llm_usage`, `tier1_sessions`, `budget_config`.

### 2. Наблюдаемость Tier-1 — `agents/tier1_log.py`
Главный пробел v2.0: ручные Claude/GPT сессии были «слепыми». Теперь:
```python
import tier1_log
sid = tier1_log.open_session(source="telegram", task_type="code_debug", prompt_text=PROMPT)
tier1_log.answer(sid, response_text=ANSWER)
tier1_log.close(sid, status="applied")        # applied | abandoned
tier1_log.stats(30)                            # сводка для Chief of Staff
```
Точка интеграции — Context Builder (когда вернёт ответ из Telegram, дёргает `log_session`).

### 3. Учёт расходов + предохранитель — `agents/cost_guard.py`
- `record_usage(agent, model, prompt_tokens, completion_tokens)` — пишет КАЖДЫЙ вызов
  (free тоже — чтобы видеть нагрузку), считает ₽ по таблице цен.
- `guard_model(model, agent)` — если модель платная (tier 2) и месячный лимит
  исчерпан → даунгрейд на free tier (Groq). Проверено: при `cap=0` `claude-sonnet`→`groq`.
- Лимит и поведение в `budget_config`: `monthly_paid_cap_rub=2500`, `over_budget_action=downgrade`.

### 4. Интеграция в роутинг — `agents/router.py`
`get_model()` пропускает выбранную модель через `cost_guard.guard_model()`.
Для текущих free/local моделей это **no-op** — поведение не изменилось.
Импорт `cost_guard` защищён try/except: при любой проблеме с ops_db роутинг
продолжает работать. Платный API подключится «под гардом» с первого дня.

### 5. Backup-пайплайн — `backups/backup.sh` + `backups/restore_test.sh`
Закрывает риск #1 (его на машине **не было**: ни папки backups, ни restic, ни B2).
- `backup.sh`: дампы обеих БД, snapshot Qdrant-коллекций, код агентов + `.session` +
  токены, Obsidian; локально, retention 30 дней; off-site Restic→B2 — опц. блок
  (включится, когда установлен restic и заданы `RESTIC_REPOSITORY`/B2-ключи).
- `restore_test.sh`: поднимает одноразовый postgres, восстанавливает дампы,
  проверяет таблицы, удаляет контейнер. Проверено: `agents`=54 табл., `ops_db`=3 — PASS.
- launchd `amori.backup` — ежедневно 04:00 (загружен). Отключить: `launchctl unload ~/Library/LaunchAgents/amori.backup.plist`.

### 6. n8n — оркестратор + карта агентов (Layer 3)
Был пробел: n8n не установлен. Теперь развёрнут.
- Сервис `n8n` в `docker-compose.yml` (контейнер `ai_n8n`, порт **5678**), persistence в
  Postgres-базе `n8n` (включена в backup). Encryption key зафиксирован в compose.
- Workflow **«Amori · Agent Map»** (`n8n/workflows/amori-agent-map.json`, генератор
  `n8n/build_agent_map.py`) — визуальная карта всех агентов и data-flow связей по слоям.
  Импортирован (`n8n list:workflow` показывает). 
- Открыть: http://localhost:5678 → один раз создать owner-аккаунт → workflow «Amori · Agent Map».
  Если не виден после setup — Workflows → Import from File → `n8n/workflows/amori-agent-map.json`.

### 7. Ротация логов
На 24/7-ядре логи росли без предела (`curator.log` был 54MB). `backup.sh` теперь
ротирует любой `agents/*.log` > 10MB (gzip + truncate, держит 5 архивов). Проверено.

## Инкремент 2 — hardening-проход (2026-06-11)

Профессиональная самокритика инкремента 1 вскрыла дыры; закрыты:
- **Бэкап не переживал отказ SSD** (лежал на том же диске). → off-site на ВНЕШНИЙ диск
  `/Volumes/One Touch/amori-backups` с верификацией контрольными суммами. Риск #1 закрыт реально.
- **Тихие сбои** — никто не узнавал о падении бэкапа/сервиса. → `notify.py` (Telegram),
  алерты в `backup.sh`, и новый агент `infra_monitor.py` (ежечасно, ai.monitor).
- **Мониторинга по сути не было**: `health_check.py` не в расписании + мёртвый код. →
  помечен deprecated; `infra_monitor` покрывает контейнеры, БД, живость агентов, свежесть
  бэкапа+off-site, диск, раздувание Docker, размеры логов; пишет heartbeat в `ops_db`.
- **Баг проверки диска**: `df /` на macOS = запечатанный сис-том. → меряем по `$HOME` (Data).
- **restore_test не автоматизирован** → launchd `ai.restoretest` (1-е число 5:00) + запись в ops_db.
- **Секреты**: `POSTGRES_PASSWORD` добавлен в `.env`; `.env*` → права 600.
- **Документация принципов** (просьба): `docs/INFRA.md`, `docs/PRINCIPLES.md`, `docs/RUNBOOK.md`.
- **Дайджест**: `ai.digest` (Пн 9:00) — недельная сводка (расходы, Tier-1, бэкап).
- Инцидент по ходу: диск был забит на 98% → Docker завис → бэкап встал. Диск освобождён,
  Docker перезапущен, добавлены пороги-алерты по месту (warn <10GB / crit <5GB).

Новые launchd-задания: `ai.monitor` (час), `ai.restoretest` (месяц), `ai.digest` (неделя),
плюс ранее `amori.backup` (день). Схема ops_db: +`infra_runs`, +`infra_heartbeats`.

## Расхождения с документом v3.0 (исправить в доке)
- **БД называется `agents`** (делится с Langfuse), а не `amori_db`. Реальное
  переименование/полное переселение app-таблиц в `ops_db` — отдельный шаг (таблицы
  пустые, так что безопасно, но трогает много агентов → выношу в follow-up).
- **Бэкап в v2.0 не был установлен** — формулировка «done» была неверной. Теперь сделан.
- **Платный API (Anthropic/OpenAI) не сконфигурирован** — в `.env` только Groq/Gemini.
  cost_guard готов, но «спит» до подключения платного ключа.
- **Qdrant**: реально `project_knowledge` + `shared_memory` (не `agent_memory`/`support_faq`).

## Требует внешних ресурсов (не из этой среды)
- **Customer-контур → РФ VPS** (152-ФЗ): нужен РФ VPS (Yandex Cloud/Selectel) +
  перенос `leads`/`support_*` туда. Сейчас они в `agents` DB на Mini.
- **Secret-backend Infisical/OpenBao**: установка + миграция токенов из `.env`.
- **Off-site backup**: `brew install restic` + B2-бакет + ключи → off-site включится сам.

## Follow-up (безопасные, в этой среде)
1. Переселить app-таблицы (`leads`, `chief_digests`, `conversations`, `pending_actions`,
   `support_*`, `team_members`, `task_*`, `known_entities`) из `agents` в `ops_db`/`customer_db`
   и репойнтить агентов (таблицы пустые — риск минимален).
2. Litellm success-callback → `cost_guard.record_usage()` для авто-учёта всех вызовов.
3. Логротация: `curator.log` 56MB, `orchestrator.log` 3MB — добавить newsyslog/ротацию.
