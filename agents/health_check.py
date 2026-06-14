# ⚠️ DEPRECATED (v3.0): заменён на infra_monitor.py (launchd ai.monitor, ежечасно).
# Этот файл НЕ в расписании и содержал мёртвый код (groq-проверка после __main__
# никогда не выполнялась). Оставлен для истории. Используй: python3 infra_monitor.py
import os
import json
import subprocess
import urllib.request
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()

AGENTS = {
    "Knowledge Curator": "knowledge.curator",
    "Docker": None,
    "Langfuse": "http://localhost:3000",
    "Redis": None,
    "Qdrant": "http://localhost:6333/"
}

def send_telegram(text: str):
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_MY_ID")
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    data = json.dumps({"chat_id": chat_id, "text": text}).encode()
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    urllib.request.urlopen(req)

def check_launchd(label):
    try:
        result = subprocess.run(
            ["launchctl", "list", label],
            capture_output=True, text=True
        )
        return result.returncode == 0
    except:
        return False

def check_http(url):
    try:
        req = urllib.request.Request(url)
        urllib.request.urlopen(req, timeout=5)
        return True
    except:
        return False

def check_docker_container(name):
    try:
        result = subprocess.run(
            ["/usr/local/bin/docker", "inspect", "--format", "{{.State.Status}}", name],
            capture_output=True, text=True
        )
        return result.stdout.strip() == "running"
    except:
        return False

def run():
    now_str = datetime.now().strftime("%d.%m.%Y %H:%M")
    issues = []

    # Knowledge Curator (launchd)
    if not check_launchd("knowledge.curator"):
        issues.append("❌ Knowledge Curator — не запущен")
    
    # Docker контейнеры
    for container in ["ai_redis", "ai_postgres", "ai_qdrant", "ai_langfuse"]:
        if not check_docker_container(container):
            issues.append(f"❌ Docker {container} — не запущен")

    # HTTP endpoints
    if not check_http("http://localhost:3000"):
        issues.append("❌ Langfuse — недоступен")
    
    if not check_http("http://localhost:6333/"):
        issues.append("❌ Qdrant — недоступен")

    # Отправляем только если есть проблемы
    if issues:
        msg = f"🚨 Health Check | {now_str}\n\n"
        msg += "\n".join(issues)
        msg += "\n\nПроверь агентов на Mac Mini!"
        send_telegram(msg)
        print(f"Отправлен алерт: {len(issues)} проблем")
    else:
        print(f"[{now_str}] Всё работает")

if __name__ == "__main__":
    run()

def check_groq_limits():
    """Тестовый запрос к Groq чтобы проверить доступность"""
    try:
        import urllib.request, json, os
        url = "https://api.groq.com/openai/v1/models"
        req = urllib.request.Request(url)
        req.add_header("Authorization", f"Bearer {os.getenv('GROQ_API_KEY')}")
        urllib.request.urlopen(req, timeout=5)
        return True
    except Exception as e:
        return False

# Добавляем в run()
_original_run = run
def run():
    _original_run()
    if not check_groq_limits():
        send_telegram("⚠️ Groq API недоступен — проверь лимиты на console.groq.com")
