#!/usr/bin/env python3
"""
Генератор n8n-workflow «Amori · Agent Map» — визуальная карта всех агентов
и их взаимосвязей (data-flow топология). Документационный канвас:
NoOp-узлы = агенты/хранилища, стики = слои и пояснения.

Запуск: python3 build_agent_map.py  → workflows/amori-agent-map.json
Импорт: docker exec ai_n8n n8n import:workflow --input=/data/workflows/amori-agent-map.json
        либо в UI: Workflows → Import from File.
"""
import json
import os

COL = {  # колонки по X
    "trig": 0, "personal": 460, "business": 460, "router": 940,
    "store": 1400, "obs": 1400,
}

# узел: (key, name, type, x, y, color/optional note)
def noop(name, x, y, notes=""):
    return {
        "parameters": {} if not notes else {},
        "id": name.lower().replace(" ", "_").replace("·", "").replace("(", "").replace(")", ""),
        "name": name,
        "type": "n8n-nodes-base.noOp",
        "typeVersion": 1,
        "position": [x, y],
    }

def sticky(name, x, y, w, h, content, color=7):
    return {
        "parameters": {"content": content, "height": h, "width": w, "color": color},
        "id": "sticky_" + name,
        "name": "note_" + name,
        "type": "n8n-nodes-base.stickyNote",
        "typeVersion": 1,
        "position": [x, y],
    }

nodes = []
conns = {}

def link(src, dst):
    conns.setdefault(src, {"main": [[]]})
    conns[src]["main"][0].append({"node": dst, "type": "main", "index": 0})

# ── Триггеры ──
triggers = [
    ("⏰ Schedule (launchd/cron)", 120),
    ("💬 Telegram", 320),
    ("📧 IMAP (3 ящика)", 520),
    ("🖐 Manual / on-demand", 720),
]
for nm, y in triggers:
    nodes.append(noop(nm, COL["trig"], y))

# ── Personal OS агенты ──
personal = [
    ("🧠 orchestrator (Emilia)", 40),
    ("📊 chief_of_staff", 200),
    ("📧 email_watchdog", 360),
    ("📚 knowledge_curator", 520),
    ("🗂 task_sync", 680),
    ("📅 calendar_agent", 840),
    ("❤️ health_check", 1000),
    ("📝 context_builder", 1160),
]
for nm, y in personal:
    nodes.append(noop(nm, COL["personal"], y))

# ── Business агенты ──
business = [
    ("📇 lead_manager", 1380),
    ("✉️ email_agent", 1540),
    ("🆘 support_agent", 1700),
]
for nm, y in business:
    nodes.append(noop(nm, COL["business"], y))

# ── Router + cost_guard ──
router = [
    ("🚦 router.py", 360),
    ("⚡ Groq / 🦙 Ollama", 200),
    ("🛡 cost_guard (NEW v3.0)", 520),
]
for nm, y in router:
    nodes.append(noop(nm, COL["router"], y))

# ── Хранилища ──
stores = [
    ("🗄 PG · agents", 120),
    ("🗄 PG · ops_db (NEW)", 280),
    ("🔵 Qdrant", 440),
    ("⚡ Redis", 600),
    ("📓 Obsidian vault", 760),
]
for nm, y in stores:
    nodes.append(noop(nm, COL["store"], y))

# ── Наблюдаемость ──
obs = [
    ("📈 Langfuse", 980),
    ("🔍 tier1_sessions (NEW)", 1140),
]
for nm, y in obs:
    nodes.append(noop(nm, COL["obs"], y))

# ── Связи (data-flow) ──
T_SCHED = "⏰ Schedule (launchd/cron)"
T_TG = "💬 Telegram"
T_IMAP = "📧 IMAP (3 ящика)"
T_MAN = "🖐 Manual / on-demand"

link(T_SCHED, "📊 chief_of_staff")
link(T_SCHED, "🗂 task_sync")
link(T_SCHED, "📅 calendar_agent")
link(T_SCHED, "❤️ health_check")
link(T_SCHED, "📇 lead_manager")
link(T_IMAP, "📧 email_watchdog")
link(T_TG, "🧠 orchestrator (Emilia)")
link(T_TG, "📊 chief_of_staff")
link(T_TG, "🆘 support_agent")
link(T_MAN, "📝 context_builder")
link(T_MAN, "✉️ email_agent")

# агенты → router (LLM)
for a in ["🧠 orchestrator (Emilia)", "📊 chief_of_staff", "📚 knowledge_curator",
          "🗂 task_sync", "🆘 support_agent", "📝 context_builder"]:
    link(a, "🚦 router.py")
link("🚦 router.py", "⚡ Groq / 🦙 Ollama")
link("🚦 router.py", "🛡 cost_guard (NEW v3.0)")
link("🛡 cost_guard (NEW v3.0)", "🗄 PG · ops_db (NEW)")

# агенты → хранилища
link("📊 chief_of_staff", "🗄 PG · agents")
link("🗂 task_sync", "🗄 PG · agents")
link("📇 lead_manager", "🗄 PG · agents")
link("📇 lead_manager", "✉️ email_agent")
link("✉️ email_agent", "🗄 PG · agents")
link("🆘 support_agent", "🗄 PG · agents")
link("🆘 support_agent", "🔵 Qdrant")
link("📚 knowledge_curator", "📓 Obsidian vault")
link("📚 knowledge_curator", "🔵 Qdrant")
link("📧 email_watchdog", "📓 Obsidian vault")
link("🧠 orchestrator (Emilia)", "🔵 Qdrant")
link("🧠 orchestrator (Emilia)", "⚡ Redis")
link("🧠 orchestrator (Emilia)", "📈 Langfuse")
link("📝 context_builder", "🔍 tier1_sessions (NEW)")
link("📝 context_builder", "🗄 PG · agents")

# ── Стики (слои + пояснения) ──
nodes.append(sticky("triggers", -40, 40, 360, 760,
    "## ⏱ ТРИГГЕРЫ\nИсточники задач: launchd/cron по расписанию, "
    "Telegram (боты), IMAP (почта), ручные/on-demand вызовы.", 4))
nodes.append(sticky("personal", 420, -60, 380, 1380,
    "## 🏠 LAYER 1 · PERSONAL OS\nЛичные агенты CEO. Emilia (orchestrator) — "
    "центральный ассистент с памятью и инструментами. Остальные — узкие "
    "single-responsibility задачи.", 5))
nodes.append(sticky("business", 420, 1340, 380, 520,
    "## 💼 LAYER 2 · BUSINESS\nCRM лидов, рассылки, поддержка. В v3.0 эти данные "
    "(ПДн клиентов РФ) выносятся в отдельный customer-контур на РФ VPS.", 3))
nodes.append(sticky("router", 900, 140, 360, 520,
    "## 🚦 LAYER 5 · MODEL ROUTING\nrouter.py → Groq (free) / Ollama (local). "
    "cost_guard (NEW v3.0) пишет КАЖДЫЙ вызов в ops_db.llm_usage и режет платный "
    "API при превышении месячного лимита.", 6))
nodes.append(sticky("stores", 1360, 40, 360, 820,
    "## 🗄 LAYER 6 · DATA & MEMORY\nPG agents (факты, делится с Langfuse), "
    "PG ops_db (NEW — учёт+Tier-1), Qdrant (смысл), Redis (скорость), Obsidian (знания).", 4))
nodes.append(sticky("obs", 1360, 900, 360, 420,
    "## 📈 LAYER 7 · OBSERVABILITY\nLangfuse трейсит API-вызовы. tier1_sessions "
    "(NEW v3.0) логирует ручные Claude/GPT сессии — раньше Tier-1 был «слепым».", 7))

wf = {
    "name": "Amori · Agent Map",
    "nodes": nodes,
    "connections": conns,
    "settings": {"executionOrder": "v1"},
    "pinData": {},
    "active": False,
}

out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "workflows", "amori-agent-map.json")
with open(out, "w", encoding="utf-8") as f:
    json.dump(wf, f, ensure_ascii=False, indent=2)
print(f"[build] {out}")
print(f"[build] nodes={len(nodes)} (agents+stores+stickies), connections from {len(conns)} sources")
