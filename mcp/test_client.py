#!/usr/bin/env python3
"""Smoke-тест amori MCP-сервера через stdio-клиент: список инструментов + read-вызовы."""
import asyncio
import os

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

HERE = os.path.dirname(os.path.abspath(__file__))


async def main():
    params = StdioServerParameters(
        command=os.path.join(HERE, ".venv/bin/python"),
        args=[os.path.join(HERE, "server.py")],
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            print("TOOLS:", [t.name for t in tools.tools])
            for name, args in [
                ("system_status", {}),
                ("sql_read", {"db": "ops_db", "query": "select count(*) c from tasks"}),
                ("recent_reports", {"limit": 3}),
                ("list_projects", {}),
            ]:
                r = await session.call_tool(name, args)
                txt = r.content[0].text if r.content else ""
                print(f"\n{name} →", txt[:400])


if __name__ == "__main__":
    asyncio.run(main())
