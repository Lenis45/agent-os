import os
import json
import requests
from datetime import datetime, timedelta
from dotenv import load_dotenv
from qdrant_client import QdrantClient
from qdrant_client.models import Distance, VectorParams, PointStruct
import hashlib

import db
import notify
import llm
from applog import get_logger

load_dotenv()
log = get_logger("task_sync")

qdrant = QdrantClient(host="localhost", port=6333)
COLLECTION = "project_knowledge"

# Создаём коллекцию если нет
try:
    qdrant.get_collection(COLLECTION)
except:
    qdrant.create_collection(
        collection_name=COLLECTION,
        vectors_config=VectorParams(size=384, distance=Distance.COSINE)
    )

def get_db():
    return db.connect("agents")

def init_db():
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS task_snapshots (
            id SERIAL PRIMARY KEY,
            date DATE DEFAULT CURRENT_DATE,
            source VARCHAR(20),
            project_name VARCHAR(200),
            total_tasks INT,
            completed_tasks INT,
            overdue_tasks INT,
            no_assignee_tasks INT,
            avg_task_age_days FLOAT,
            team_data JSONB,
            created_at TIMESTAMP DEFAULT NOW()
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS task_history (
            id SERIAL PRIMARY KEY,
            task_id VARCHAR(100),
            source VARCHAR(20),
            project_name VARCHAR(200),
            title TEXT,
            assignee VARCHAR(200),
            status VARCHAR(100),
            due_date DATE,
            description TEXT,
            snapshot_date DATE DEFAULT CURRENT_DATE,
            created_at TIMESTAMP DEFAULT NOW()
        )
    """)
    conn.commit()
    cur.close()
    conn.close()

def save_snapshot(source, project, stats, team_data):
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO task_snapshots 
            (source, project_name, total_tasks, completed_tasks, overdue_tasks, no_assignee_tasks, avg_task_age_days, team_data)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        """, (
            source,
            project,
            stats.get("total", 0),
            stats.get("completed", 0),
            stats.get("overdue", 0),
            stats.get("no_assignee", 0),
            stats.get("avg_age", 0),
            json.dumps(team_data)
        ))
        conn.commit()
        cur.close()
        conn.close()
    except Exception as e:
        print(f"DB save error: {e}")

def get_historical_snapshots(source, project, days=7):
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("""
            SELECT date, total_tasks, completed_tasks, overdue_tasks, avg_task_age_days
            FROM task_snapshots
            WHERE source = %s AND project_name = %s
            AND date >= CURRENT_DATE - %s
            ORDER BY date DESC
        """, (source, project, days))
        rows = cur.fetchall()
        cur.close()
        conn.close()
        return rows
    except:
        return []

# ===== WEEEK =====
def get_weeek_members(headers):
    """Получаем карту UUID -> имя"""
    members = {}
    try:
        r = requests.get("https://api.weeek.net/public/v1/ws/members", headers=headers, timeout=10)
        if r.status_code == 200:
            for m in r.json().get("members", []):
                name = m.get("firstName", "") or m.get("email", "?")
                members[m["id"]] = name
    except Exception as e:
        log.info(f"WEEEK members error: {e}")
    return members

def get_weeek_tasks():
    headers = {"Authorization": f"Bearer {os.getenv('WEEEK_TOKEN')}"}
    all_tasks = []

    try:
        members = get_weeek_members(headers)

        r = requests.get("https://api.weeek.net/public/v1/tm/projects", headers=headers, timeout=10)
        if r.status_code != 200:
            log.info(f"WEEEK projects error: {r.status_code}")
            return all_tasks

        projects = r.json().get("projects", [])
        log.info(f"WEEEK: найдено {len(projects)} проектов, участников: {len(members)}")

        for project in projects:
            pid = project.get("id")
            pname = project.get("title", "Без названия")

            r2 = requests.get(
                f"https://api.weeek.net/public/v1/tm/tasks?projectId={pid}",
                headers=headers, timeout=10
            )
            if r2.status_code != 200:
                continue

            for task in r2.json().get("tasks", []):
                # assignees — список UUID строк
                assignee_ids = task.get("assignees", []) or []
                assignee_names = [members.get(uid, uid[:8]) for uid in assignee_ids if isinstance(uid, str)]
                assignee = ", ".join(assignee_names) if assignee_names else "Не назначен"

                # статус через isCompleted
                is_completed = task.get("isCompleted", False)
                is_overdue = (task.get("overdue", 0) or 0) > 0
                status = "Завершено" if is_completed else ("Просрочено" if is_overdue else "В работе")

                all_tasks.append({
                    "source": "WEEEK",
                    "project": pname,
                    "id": str(task.get("id", "")),
                    "title": task.get("title", "Без названия"),
                    "description": task.get("description", "") or "",
                    "status": status,
                    "assignee": assignee,
                    "assignees": assignee_names,
                    "due_date": task.get("dueDate", "") or "",
                    "updated_at": task.get("updatedAt", "") or "",
                    "created_at": task.get("createdAt", "") or "",
                    "priority": task.get("priority", "") or "normal",
                    "tags": [str(t) for t in task.get("tags", [])],
                    "overdue_days": task.get("overdue", 0) or 0,
                    "is_completed": is_completed
                })

    except Exception as e:
        log.info(f"WEEEK error: {e}")

    return all_tasks

# ===== TAIGA =====
def get_taiga_token():
    try:
        r = requests.post(
            f"{os.getenv('TAIGA_URL')}/api/v1/auth",
            json={"type": "normal", "username": os.getenv("TAIGA_USERNAME"), "password": os.getenv("TAIGA_PASSWORD")},
            timeout=10
        )
        if r.status_code == 200:
            return r.json().get("auth_token")
    except Exception as e:
        log.info(f"Taiga auth error: {e}")
    return None

def get_taiga_tasks():
    all_tasks = []
    token = get_taiga_token()
    if not token:
        return all_tasks

    headers = {"Authorization": f"Bearer {token}"}

    try:
        # Получаем user_id
        auth_data = requests.post(
            f"{os.getenv('TAIGA_URL')}/api/v1/auth",
            json={"type": "normal", "username": os.getenv("TAIGA_USERNAME"), "password": os.getenv("TAIGA_PASSWORD")},
            timeout=10
        ).json()
        user_id = auth_data.get("id", 0)
        r = requests.get(f"{os.getenv('TAIGA_URL')}/api/v1/projects?member={user_id}", headers=headers, timeout=10)
        if r.status_code != 200:
            return all_tasks

        projects = r.json()
        log.info(f"Taiga: найдено {len(projects)} проектов")

        for project in projects:
            pid = project.get("id")
            pname = project.get("name", "Без названия")

            # User stories
            r2 = requests.get(
                f"{os.getenv('TAIGA_URL')}/api/v1/userstories?project={pid}",
                headers=headers, timeout=10
            )
            if r2.status_code == 200:
                for task in r2.json():
                    assignee_info = task.get("assigned_to_extra_info") or {}
                    assignee = assignee_info.get("full_name_display", "Не назначен")

                    all_tasks.append({
                        "source": "Taiga",
                        "project": pname,
                        "id": str(task.get("id", "")),
                        "title": task.get("subject", "Без названия"),
                        "description": task.get("description", "") or "",
                        "status": (task.get("status_extra_info") or {}).get("name", ""),
                        "assignee": assignee,
                        "assignees": [assignee],
                        "due_date": task.get("due_date", "") or "",
                        "updated_at": task.get("modified", "") or "",
                        "created_at": task.get("created_date", "") or "",
                        "priority": "",
                        "tags": task.get("tags", [])
                    })

            # Также берём задачи (tasks внутри историй)
            r3 = requests.get(
                f"{os.getenv('TAIGA_URL')}/api/v1/tasks?project={pid}",
                headers=headers, timeout=10
            )
            if r3.status_code == 200:
                for task in r3.json():
                    assignee_info = task.get("assigned_to_extra_info") or {}
                    assignee = assignee_info.get("full_name_display", "Не назначен")

                    all_tasks.append({
                        "source": "Taiga",
                        "project": f"{pname} (task)",
                        "id": f"t_{task.get('id', '')}",
                        "title": task.get("subject", "Без названия"),
                        "description": task.get("description", "") or "",
                        "status": (task.get("status_extra_info") or {}).get("name", ""),
                        "assignee": assignee,
                        "assignees": [assignee],
                        "due_date": "",
                        "updated_at": task.get("modified", "") or "",
                        "created_at": task.get("created_date", "") or "",
                        "priority": "",
                        "tags": []
                    })

    except Exception as e:
        log.info(f"Taiga error: {e}")

    return all_tasks

def calculate_kpis(tasks, source, project):
    now = datetime.now()
    total = len(tasks)
    if total == 0:
        return {}, {}

    completed = sum(1 for t in tasks if any(
        word in (t.get("status", "") or "").lower()
        for word in ["done", "завершено", "closed", "complete", "готово"]
    ))

    overdue = 0
    no_assignee = 0
    stale = 0
    ages = []
    team_load = {}

    for t in tasks:
        # Ответственный
        assignee = t.get("assignee", "Не назначен")
        if assignee == "Не назначен":
            no_assignee += 1
        else:
            team_load[assignee] = team_load.get(assignee, 0) + 1

        # Просрочка — только незавершённые задачи
        due = t.get("due_date", "")
        is_completed = t.get("is_completed", False)
        if due and not is_completed:
            try:
                due_dt = datetime.fromisoformat(due[:10])
                if due_dt < now:
                    overdue += 1
            except:
                pass

        # Возраст задачи
        created = t.get("created_at", "")
        if created:
            try:
                created_dt = datetime.fromisoformat(created[:10])
                ages.append((now - created_dt).days)
            except:
                pass

        # Зависшие
        updated = t.get("updated_at", "")
        if updated:
            try:
                upd_dt = datetime.fromisoformat(updated[:10])
                if (now - upd_dt).days > 3:
                    stale += 1
            except:
                pass

    avg_age = sum(ages) / len(ages) if ages else 0
    completion_rate = round(completed / total * 100, 1) if total > 0 else 0

    stats = {
        "total": total,
        "completed": completed,
        "completion_rate": completion_rate,
        "overdue": overdue,
        "no_assignee": no_assignee,
        "stale": stale,
        "avg_age": round(avg_age, 1)
    }

    return stats, team_load

def format_trend(history):
    if len(history) < 2:
        return "недостаточно данных для тренда"
    current = history[0]
    previous = history[1]
    total_diff = current[1] - previous[1]
    overdue_diff = current[3] - previous[3]
    trend = []
    if total_diff > 0:
        trend.append(f"задач стало больше на {total_diff}")
    elif total_diff < 0:
        trend.append(f"задач стало меньше на {abs(total_diff)}")
    if overdue_diff > 0:
        trend.append(f"просроченных выросло на {overdue_diff}")
    elif overdue_diff < 0:
        trend.append(f"просроченных стало меньше на {abs(overdue_diff)}")
    return ", ".join(trend) if trend else "без изменений"

agent = llm.build_agent(
    "task_sync",
    name="TaskSync",
    role="Менеджер задач и дедлайнов стартапа Amori",
    goal="""Ты анализируешь задачи из WEEEK и Taiga и находишь проблемы.
Отвечай на русском, конкретно, с именами и названиями задач.""",
)

def run():
    now_str = datetime.now().strftime("%d.%m.%Y %H:%M")
    now = datetime.now()
    log.info("Task Sync запущен")

    init_db()

    weeek_tasks = get_weeek_tasks()
    taiga_tasks = get_taiga_tasks()
    all_tasks = weeek_tasks + taiga_tasks

    if not all_tasks:
        notify.send(f"Task Sync | {now_str}\nНе удалось получить задачи.")
        return

    print(f"Всего задач: {len(all_tasks)}")

    # KPI по источникам
    weeek_stats, weeek_team = calculate_kpis(weeek_tasks, "WEEEK", "all")
    taiga_stats, taiga_team = calculate_kpis(taiga_tasks, "Taiga", "all")

    # Сохраняем снапшоты
    save_snapshot("WEEEK", "all", weeek_stats, weeek_team)
    save_snapshot("Taiga", "all", taiga_stats, taiga_team)

    # История за 7 дней
    weeek_history = get_historical_snapshots("WEEEK", "all", 7)
    taiga_history = get_historical_snapshots("Taiga", "all", 7)

    # Формируем детальный текст для агента
    now_date = now.strftime("%Y-%m-%d")
    text = ""
    for t in all_tasks:
        due = t.get("due_date", "") or ""
        updated = t.get("updated_at", "") or ""
        desc = t.get("description", "") or ""
        text += (
            f"[{t['source']}] {t['project']}\n"
            f"  Задача: {t['title']}\n"
            f"  Описание: {desc[:200] if desc else 'нет'}\n"
            f"  Статус: {t['status']} | Ответственный: {t['assignee']}\n"
            f"  Дедлайн: {due or 'не указан'} | Обновлено: {updated[:10] if updated else '?'}\n"
            f"  Теги: {', '.join([x[0] if isinstance(x, list) else str(x) for x in t.get('tags', [])]) or 'нет'}\n\n"
        )

    # KPI блок
    kpi_text = f"""
МЕТРИКИ WEEEK:
- Всего задач: {weeek_stats.get('total', 0)}
- Завершено: {weeek_stats.get('completed', 0)} ({weeek_stats.get('completion_rate', 0)}%)
- Просрочено: {weeek_stats.get('overdue', 0)}
- Без ответственного: {weeek_stats.get('no_assignee', 0)}
- Зависших (>3 дней без активности): {weeek_stats.get('stale', 0)}
- Средний возраст задачи: {weeek_stats.get('avg_age', 0)} дней
- Нагрузка команды: {json.dumps(weeek_team, ensure_ascii=False)}
- Тренд за неделю: {format_trend(weeek_history)}

МЕТРИКИ TAIGA:
- Всего задач: {taiga_stats.get('total', 0)}
- Завершено: {taiga_stats.get('completed', 0)} ({taiga_stats.get('completion_rate', 0)}%)
- Просрочено: {taiga_stats.get('overdue', 0)}
- Без ответственного: {taiga_stats.get('no_assignee', 0)}
- Зависших (>3 дней без активности): {taiga_stats.get('stale', 0)}
- Средний возраст задачи: {taiga_stats.get('avg_age', 0)} дней
- Нагрузка команды: {json.dumps(taiga_team, ensure_ascii=False)}
- Тренд за неделю: {format_trend(taiga_history)}
"""

    prompt = f"""Ты персональный аналитик Дениса Колесникова — CEO стартапа Amori (умные ошейники).
Сегодня {now_str}. Твоя задача — дать полную управленческую картину по задачам.

МЕТРИКИ:
{kpi_text}

ВСЕ ЗАДАЧИ (WEEEK — маркетинг/продажи/управление, Taiga — разработка):
{text[:8000]}

Напиши детальный CEO-отчёт БЕЗ таблиц, БЕЗ markdown, БЕЗ звёздочек.
Используй только текст, эмодзи и символы ━ ↳ •

━━━ 📊 ОБЩАЯ КАРТИНА ━━━
WEEEK [маркетинг/продажи/управление]: X/Y завершено (Z%)
Taiga [разработка]: X/Y завершено (Z%)
Ключевая проблема: [одна фраза о главном риске]

━━━ 🔴 КРИТИЧНО — ТРЕБУЕТ РЕШЕНИЯ СЕГОДНЯ ━━━
[Для каждой просроченной или горящей задачи:]
[Имя] — [Название задачи] ([источник])
  ↳ Просрочено [N] дней / Дедлайн сегодня
  ↳ Что делать: [конкретное действие — позвонить, передать, закрыть]

━━━ 🛠 РАЗРАБОТКА — ДЕТАЛЬНАЯ КАРТИНА ━━━
[Перечисли ВСЕ активные задачи из Taiga с исполнителем и статусом]
[Имя] — [Задача] → [Статус]
[Выдели что в работе, что зависло, что без исполнителя]
Завершено: [список что сделано]
Не начато: [список что ещё не взяли в работу]

━━━ 📋 WEEEK — ДЕТАЛЬНАЯ КАРТИНА ━━━
[Перечисли ВСЕ активные задачи WEEEK с исполнителем и статусом]
[Имя] — [Задача] → [Статус / Дедлайн]

━━━ 👥 НАГРУЗКА И УПРАВЛЕНИЕ КОМАНДОЙ ━━━
[Для каждого участника с задачами:]
[Имя] ([N] задач):
  • [Задача 1] — [статус]
  • [Задача 2] — [статус]
  Оценка: [✅ норма / ⚠️ перегружен / 💤 простаивает]
  Рекомендация: [что конкретно сделать — делегировать, разгрузить, дать новую задачу]

━━━ 🎯 УПРАВЛЕНЧЕСКИЕ РЕШЕНИЯ ━━━
[3-5 конкретных действий которые должен принять CEO прямо сейчас]
1. [Кому позвонить/написать — по какому поводу — ожидаемый результат]
2. [Что делегировать — кому — срок]
3. [Что эскалировать или закрыть]
4. [Что добавить в план]
5. [Что проконтролировать завтра]

━━━ 📈 ПРОГНОЗ ━━━
[Что случится если ничего не делать — конкретный риск]
[Что улучшится если выполнить рекомендации]"""

    result = llm.run(agent, prompt, "task_sync")

    header = (
        f"Task Sync | {now_str}\n"
        f"WEEEK: {len(weeek_tasks)} задач | Taiga: {len(taiga_tasks)} задач\n"
        f"Completion: WEEEK {weeek_stats.get('completion_rate', 0)}% | "
        f"Taiga {taiga_stats.get('completion_rate', 0)}%\n\n"
    )

    notify.send(header + str(result))
    log.info("Отчёт отправлен в Telegram")

if __name__ == "__main__":
    run()
