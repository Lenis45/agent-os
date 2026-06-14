# amori-infra — runbook (что делать когда сломалось)

Быстрые процедуры реагирования. Большинство проблем монитор пришлёт в Telegram сам.

## Диагностика «всё ли живо»
```bash
cd ~/ai-infra/agents && /opt/anaconda3/bin/python3 infra_monitor.py   # полная проверка
docker ps --format "table {{.Names}}\t{{.Status}}" | grep ai_         # контейнеры
launchctl list | grep -E "ai\.|amori\."                               # агенты
cat ~/ai-infra/backups/local/status.json                              # статус бэкапа
```

## Алерт «контейнер не запущен»
```bash
cd ~/ai-infra && docker compose up -d            # поднять всё
docker logs <ai_postgres|ai_n8n|...> --tail 50   # если падает — смотреть логи
```

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
cd ~/ai-infra/backups && ./backup.sh               # перезапустить вручную
```

## Восстановление из бэкапа (DR)
```bash
# 1. проверить, что бэкап восстановим (одноразовый контейнер, прод не трогает)
cd ~/ai-infra/backups && ./restore_test.sh
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

## Бюджет платного API исчерпан
Это не авария: `cost_guard` сам даунгрейдит на free tier (Groq). Если хочешь больше лимит:
```bash
cd ~/ai-infra/agents && /opt/anaconda3/bin/python3 -c "import ops_store; ops_store.set_budget('monthly_paid_cap_rub','5000')"
```

## Ротация секрета (пока без secret-backend)
1. Обновить значение в `~/ai-infra/agents/.env` (и в compose, если это PG/n8n ключ).
2. `cd ~/ai-infra && docker compose up -d <service>` для перечитки.
3. Перезапустить затронутых агентов (unload/load).

## Ollama / GPU-нода (denis-k) недоступна
Не авария: `router.py` сам уводит ollama-задачи на Groq. Проверить ноду:
```bash
curl -s http://100.77.9.84:11434/ >/dev/null && echo up || echo "denis-k down"
```

## Проверка карты агентов в n8n
http://localhost:5678 → owner-аккаунт (разово) → workflow «Amori · Agent Map».
Если пусто: Workflows → Import from File → `~/ai-infra/n8n/workflows/amori-agent-map.json`.
