import os
from dotenv import load_dotenv
load_dotenv()

# Тест 1 — Langfuse
print("Проверяем Langfuse...")
from langfuse import Langfuse
lf = Langfuse(
    secret_key=os.getenv("LANGFUSE_SECRET_KEY"),
    public_key=os.getenv("LANGFUSE_PUBLIC_KEY"),
    host=os.getenv("LANGFUSE_HOST")
)
lf.auth_check()
print("✅ Langfuse — OK")

# Тест 2 — Gemini через PraisonAI
print("Проверяем Gemini...")
from praisonaiagents import Agent
agent = Agent(
    name="test",
    role="assistant",
    goal="answer briefly",
    llm="gemini/gemini-2.0-flash"
)
result = agent.start("Say exactly: CONNECTED")
print(f"✅ Gemini — OK")

print("\n🟢 Всё работает, можно запускать агентов")
