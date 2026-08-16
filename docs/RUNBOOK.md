# amori-infra — runbook (что делать когда сломалось)

Быстрые процедуры реагирования. Большинство проблем монитор пришлёт в Telegram сам.

## Диагностика «всё ли живо»
```bash
cd ~/ai-infra
make doctor          # процессы, HTTP, Telegram, backup, disk
make security-check  # права, bind-адреса, секреты, firewall
make dependency-audit # CVE Python-пакетов + high-confidence static scan
make audit           # состояние агентов и качество последних ответов
make llm-report      # токены, точность измерения, кэш и главные потребители
make model-eval      # безопасный canary 20B против текущего 120B baseline
```

Изолированная Python-среда создаётся командой `make bootstrap-runtime`. Runtime
использует `~/ai-infra/.venv/bin/python`; lock-файл обновляется только после полного
`make release-check` и live-smoke основных провайдеров.

Launchd указывает прямо на `.venv`. Старые пользовательские cron-записи могут
содержать путь Anaconda, но `task_sync.py`, `calendar_agent.py` и `lead_manager.py`
до импорта внешних пакетов выполняют безопасный re-exec в `.venv`. Это оставлено как
совместимый fallback для macOS, где системный `crontab` может зависать при записи.

## Алерт «контейнер не запущен»
```bash
cd ~/ai-infra && docker compose up -d            # поднять всё
docker logs <ai_postgres|ai_n8n|...> --tail 50   # если падает — смотреть логи
```

Новый алерт всегда содержит строку `Источник: <host> · ai.monitor v3.1` и не
повторяет одинаковую ошибку чаще одного раза в 6 часов. Старый шаблон
`🚨 Health Check ... Проверь агентов на Mac Mini!` текущим кодом не отправляется.
Если он снова появился, работает забытая копия со старым Telegram-токеном на другом
хосте. Проверь cron/launchd на доступных машинах; если источник недоступен, отзови
токен нужного бота через BotFather и обнови его только в `agents/.env` Mac Mini.

Текущий монитор запускается в `:07`, чтобы не пересекаться с бэкапом в `04:00`:
```bash
plutil -p ~/Library/LaunchAgents/ai.monitor.plist
launchctl print gui/$UID/ai.monitor | grep -E 'path|state|runs'
tail -30 ~/ai-infra/agents/monitor.log
```

## Groq сообщил об удалении Llama 3.3 70B

Production replacement: `openai/gpt-oss-120b`; запасной вариант для отдельных
сценариев — `qwen/qwen3.6-27b`. Активная конфигурация не должна содержать старую
модель:

```bash
rg '^DEFAULT_GROQ_MODEL=' ~/ai-infra/agents/.env
cd ~/ai-infra && .venv/bin/python -c 'import sys; sys.path.insert(0,"agents"); import provider_health; print(provider_health.check_groq())'
```

Groq остаётся opt-in fallback. При `ALLOW_EXTERNAL_LLM_FALLBACK=0` локальные агенты
его не вызывают, но ручной health-check всё равно позволяет проверить ключ и модель.

## Алерт «мало места» / диск заполнен (РИСК: Docker зависает при ~98%)
```bash
df -h ~                                            # реальное место (Data-том, НЕ df /)
docker builder prune -af && docker image prune -f  # вернуть место (build cache, dangling)
du -sh ~/Library/* | sort -rh | head               # крупные потребители
ls -lt ~/ai-infra/backups/local/                   # старые снимки (retention 30д сам чистит)
```
Если Docker завис из-за нехватки места: освободить диск → перезапустить Docker:
```bash
osascript -e 'quit app "Docker"'; sleep 5; open -a Docker
until docker info >/dev/null 2>&1; do sleep 5; done   # ждать демон (может ~1-2 мин)
```

## Алерт «бэкап PARTIAL/FAIL» или «без off-site»
```bash
tail -40 ~/ai-infra/backups/backup.log             # что упало
ls /Volumes/                                       # подключён ли внешний диск?
diskutil mount /dev/disk4s1                         # примонтировать One Touch если нет
cd ~/ai-infra && make backup                        # перезапустить вручную
```

## Восстановление из бэкапа (DR)
```bash
# 1. проверить, что бэкап восстановим (одноразовый контейнер, прод не трогает)
cd ~/ai-infra && make restore-test
# 2. реальное восстановление БД в прод (ОСТОРОЖНО — перезапишет!)
LATEST=$(ls -1dt ~/ai-infra/backups/local/20* | head -1)        # или с /Volumes/One Touch/amori-backups
gunzip -c "$LATEST/pg_agents.sql.gz" | docker exec -i ai_postgres psql -U agent_user -d agents
# 3. Qdrant — восстановить снапшот через API; код агентов — распаковать agents_code.tar.gz
# 4. Telegram .session внутри agents_code.tar.gz — критичны для ботов
```

## Алерт «агент не загружен / молчит»
```bash
launchctl list | grep <label>                                  # загружен?
launchctl unload ~/Library/LaunchAgents/<label>.plist
launchctl load   ~/Library/LaunchAgents/<label>.plist          # перезагрузить
tail -50 ~/ai-infra/agents/<name>.log                          # последние ошибки
```

## Лимит подписки Codex или Claude исчерпан
Это не роняет локальные ответы. Автовыбор Claude при проблеме авторизации/лимита
переходит в Codex; явный `--to claude` честно возвращает ошибку. Проверка:
```bash
amori-ai --doctor
amori-ai --doctor --live-check   # расходует по одному реальному запросу
claude auth login               # если OAuth Claude отозван
codex login status
```

## Ротация секрета (пока без secret-backend)
1. Обновить значение в `~/ai-infra/agents/.env` (и в compose, если это PG/n8n ключ).
2. `cd ~/ai-infra && docker compose up -d <service>` для перечитки.
3. Перезапустить затронутых агентов (unload/load).

## Локальный Ollama на Mac недоступен
Простые ответы временно не работают, но маршрут можно явно направить в подписочный CLI.
Сначала восстанови локальный сервис:

```bash
brew services restart ollama
curl --max-time 5 http://127.0.0.1:11434/api/tags
ollama list
amori-ai --doctor
```

Обязательные модели:

```bash
ollama pull qwen3:1.7b
ollama pull qwen3-vl:2b
```

## Опциональная GPU-нода Windows недоступна
Это не авария: основной контур работает на Mac. Для тяжёлых ручных задач Windows
по-прежнему можно проверить отдельно:

Проверить ноду:
```bash
python3 ~/ai-infra/scripts/check_remote_ollama.py
curl -g -s 'http://[fd7a:115c:a1e0::b43b:954]:11434/api/tags'
```

Если API отвечает, но моделей нет, на Windows выполни:
```powershell
ollama pull qwen3.6:35b-a3b-q4_K_M
ollama pull qwen3.6:27b-q4_K_M
ollama pull gemma4:12b-it-qat
ollama list
```

Если Windows только что перезагрузился:
1. Tailscale должен быть online под тем же аккаунтом.
2. Ollama должна стартовать после входа пользователя.
3. `OLLAMA_HOST` на Windows должен быть `0.0.0.0:11434`.
4. Firewall должен разрешать `11434` для Tailscale.

Проверка на Windows:
```powershell
curl http://127.0.0.1:11434/api/tags
curl http://100.77.9.84:11434/api/tags
[Environment]::GetEnvironmentVariable("OLLAMA_HOST", "User")
[Environment]::GetEnvironmentVariable("OLLAMA_HOST", "Machine")
```

## Проверка карты агентов в n8n
http://localhost:5678 → owner-аккаунт (разово) → workflow «Amori · Agent Map».
Если пусто: Workflows → Import from File → `~/ai-infra/n8n/workflows/amori-agent-map.json`.
