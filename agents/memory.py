import os
import json
import hashlib
from datetime import datetime
from typing import Optional
from qdrant_client import QdrantClient
from qdrant_client.models import Distance, VectorParams, PointStruct, Filter, FieldCondition, MatchValue
from sentence_transformers import SentenceTransformer
import db

# Инициализация
qdrant = QdrantClient(host="127.0.0.1", port=6333)
embedder = SentenceTransformer("all-MiniLM-L6-v2")

COLLECTION = "shared_memory"
VECTOR_SIZE = 384

def init_memory():
    """Создаём коллекцию если нет"""
    try:
        qdrant.get_collection(COLLECTION)
    except:
        qdrant.create_collection(
            collection_name=COLLECTION,
            vectors_config=VectorParams(size=VECTOR_SIZE, distance=Distance.COSINE)
        )

def get_db():
    # Пароль из окружения (.env), без хардкода — см. db.py
    return db.connect("agents")

def init_db():
    """Создаём таблицы"""
    conn = get_db()
    cur = conn.cursor()

    # Таблица команды
    cur.execute("""
        CREATE TABLE IF NOT EXISTS team_members (
            id SERIAL PRIMARY KEY,
            name VARCHAR(100) UNIQUE,
            role VARCHAR(100),
            direction VARCHAR(100),
            telegram_username VARCHAR(100),
            active BOOLEAN DEFAULT true,
            created_at TIMESTAMP DEFAULT NOW(),
            updated_at TIMESTAMP DEFAULT NOW()
        )
    """)

    # Таблица известных сущностей (дедупликация)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS known_entities (
            id SERIAL PRIMARY KEY,
            entity_hash VARCHAR(64) UNIQUE,
            entity_type VARCHAR(50),
            content TEXT,
            source VARCHAR(50),
            agent VARCHAR(50),
            status VARCHAR(20) DEFAULT 'active',
            first_seen TIMESTAMP DEFAULT NOW(),
            last_seen TIMESTAMP DEFAULT NOW(),
            resolved_at TIMESTAMP,
            metadata JSONB
        )
    """)

    # Таблица дайджестов Chief of Staff
    cur.execute("""
        CREATE TABLE IF NOT EXISTS chief_digests (
            id SERIAL PRIMARY KEY,
            digest_date DATE DEFAULT CURRENT_DATE,
            period VARCHAR(10),
            tasks TEXT[],
            agreements TEXT[],
            deadlines TEXT[],
            important TEXT[],
            raw_output TEXT,
            created_at TIMESTAMP DEFAULT NOW()
        )
    """)

    # Таблица фидбека
    cur.execute("""
        CREATE TABLE IF NOT EXISTS agent_feedback (
            id SERIAL PRIMARY KEY,
            agent_name VARCHAR(50),
            input_text TEXT,
            output_text TEXT,
            rating INTEGER,
            comment TEXT,
            created_at TIMESTAMP DEFAULT NOW()
        )
    """)

    # Вставляем команду если пусто
    cur.execute("SELECT COUNT(*) FROM team_members")
    if cur.fetchone()[0] == 0:
        team = [
            ("Андрей", "Технический лид", "Общий", None),
            ("Лева", "Лид направления", "Приложение", None),
            ("Макс", "Бэкенд разработчик", "Приложение", "konovodov03"),
            ("Саша", "Фронтенд/Мобайл", "Приложение", None),
            ("Олег", "QA", "Приложение", None),
            ("Лиза", "Дизайнер", "Приложение", None),
            ("Никита", "Лид направления", "Шоп/Сайт", None),
            ("Паша", "Бэкенд разработчик", "Шоп/Сайт", None),
            ("Петя", "Бэкенд разработчик", "Шоп/Сайт", None),
            ("Ася", "Дизайнер", "Шоп/Сайт", None),
            ("Даня", "Лид направления", "Ошейники", None),
            ("Максим", "Финансы/Экономика", "Экономика", None),
            ("Станислав", "Разработчик", "Общий", None),
        ]
        for name, role, direction, tg in team:
            cur.execute(
                "INSERT INTO team_members (name, role, direction, telegram_username) VALUES (%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                (name, role, direction, tg)
            )

    conn.commit()
    cur.close()
    conn.close()

def get_team():
    """Получить актуальную структуру команды"""
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT name, role, direction FROM team_members WHERE active=true ORDER BY direction, name")
    rows = cur.fetchall()
    cur.close()
    conn.close()

    team = {}
    for name, role, direction in rows:
        if direction not in team:
            team[direction] = []
        team[direction].append({"name": name, "role": role})
    return team

def get_team_prompt():
    """Генерируем промпт о команде из базы"""
    team = get_team()
    lines = ["СТРУКТУРА КОМАНДЫ Amori:"]
    for direction, members in team.items():
        lines.append(f"\nНаправление {direction.upper()}:")
        for m in members:
            lines.append(f"  - {m['name']} ({m['role']})")
    return "\n".join(lines)

def remember(content: str, entity_type: str, source: str, agent: str, metadata: dict = None) -> bool:
    """
    Сохранить сущность в память.
    Возвращает True если новая, False если уже известна.
    """
    init_memory()

    # Хэш для дедупликации
    content_hash = hashlib.sha256(content.lower().strip().encode()).hexdigest()

    conn = get_db()
    cur = conn.cursor()

    # Проверяем знаем ли уже
    cur.execute(
        "SELECT id, status FROM known_entities WHERE entity_hash = %s",
        (content_hash,)
    )
    existing = cur.fetchone()

    if existing:
        # Обновляем last_seen
        cur.execute(
            "UPDATE known_entities SET last_seen = NOW() WHERE entity_hash = %s",
            (content_hash,)
        )
        conn.commit()
        cur.close()
        conn.close()
        return False  # Уже известно

    # Новая сущность — сохраняем
    cur.execute("""
        INSERT INTO known_entities (entity_hash, entity_type, content, source, agent, metadata)
        VALUES (%s, %s, %s, %s, %s, %s)
    """, (content_hash, entity_type, content, source, agent, json.dumps(metadata or {})))

    conn.commit()
    cur.close()
    conn.close()

    # Сохраняем в Qdrant для семантического поиска
    vector = embedder.encode(content).tolist()
    qdrant.upsert(
        collection_name=COLLECTION,
        points=[PointStruct(
            id=int(content_hash[:15], 16) % (2**31),  # детерминированный id из хэша
            vector=vector,
            payload={
                "content": content,
                "entity_type": entity_type,
                "source": source,
                "agent": agent,
                "timestamp": datetime.now().isoformat(),
                "metadata": metadata or {}
            }
        )]
    )

    return True  # Новая сущность

def recall(query: str, entity_type: str = None, limit: int = 5) -> list:
    """Семантический поиск по памяти"""
    init_memory()
    vector = embedder.encode(query).tolist()

    results = qdrant.query_points(
        collection_name=COLLECTION,
        query=vector,
        limit=limit
    ).points

    found = []
    for r in results:
        if entity_type and r.payload.get("entity_type") != entity_type:
            continue
        if r.score > 0.7:  # порог релевантности
            found.append({
                "content": r.payload.get("content"),
                "type": r.payload.get("entity_type"),
                "source": r.payload.get("source"),
                "score": r.score,
                "timestamp": r.payload.get("timestamp")
            })

    return found

def is_known(content: str) -> bool:
    """Быстрая проверка знаем ли уже об этом"""
    content_hash = hashlib.sha256(content.lower().strip().encode()).hexdigest()
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT id FROM known_entities WHERE entity_hash = %s", (content_hash,))
    result = cur.fetchone()
    cur.close()
    conn.close()
    return result is not None

def resolve(content: str):
    """Отметить сущность как решённую"""
    content_hash = hashlib.sha256(content.lower().strip().encode()).hexdigest()
    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        "UPDATE known_entities SET status='resolved', resolved_at=NOW() WHERE entity_hash=%s",
        (content_hash,)
    )
    conn.commit()
    cur.close()
    conn.close()

def save_digest(period: str, tasks: list, agreements: list, deadlines: list, important: list, raw: str):
    """Сохранить дайджест Chief of Staff"""
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO chief_digests (period, tasks, agreements, deadlines, important, raw_output)
        VALUES (%s, %s, %s, %s, %s, %s)
    """, (period, tasks, agreements, deadlines, important, raw))
    conn.commit()
    cur.close()
    conn.close()

def get_recent_digests(days: int = 3) -> list:
    """Получить дайджесты за последние N дней"""
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        SELECT digest_date, period, tasks, agreements, deadlines, important
        FROM chief_digests
        WHERE digest_date >= CURRENT_DATE - %s
        ORDER BY created_at DESC
        LIMIT 10
    """, (days,))
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return rows

def update_team_member(name: str, role: str = None, direction: str = None, active: bool = None):
    """Обновить информацию о члене команды"""
    conn = get_db()
    cur = conn.cursor()

    # Проверяем существует ли
    cur.execute("SELECT id FROM team_members WHERE name = %s", (name,))
    if cur.fetchone():
        updates = ["updated_at = NOW()"]
        params = []
        if role:
            updates.append("role = %s")
            params.append(role)
        if direction:
            updates.append("direction = %s")
            params.append(direction)
        if active is not None:
            updates.append("active = %s")
            params.append(active)
        params.append(name)
        cur.execute(f"UPDATE team_members SET {', '.join(updates)} WHERE name = %s", params)
    else:
        cur.execute(
            "INSERT INTO team_members (name, role, direction) VALUES (%s, %s, %s)",
            (name, role or "Не указана", direction or "Общий")
        )

    conn.commit()
    cur.close()
    conn.close()

if __name__ == "__main__":
    print("Инициализация памяти...")
    init_memory()
    init_db()
    print("✅ Shared Memory готова")
    print("\nСтруктура команды:")
    print(get_team_prompt())
