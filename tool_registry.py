"""The registry: every tool a bot is allowed to use, in one place.

A bot configuration only NAMES tools; it can never define new ones. That keeps
the platform safe when bots are generated later (Step 10): the builder can pick
from tested tools, but cannot invent code that runs on the server.

Tools come from two kinds of source:
  - local Python functions (tools.py)
  - MCP servers: separate programs that offer tools over the MCP protocol
"""

import sys
from pathlib import Path

from langchain_mcp_adapters.client import MultiServerMCPClient

from search_graph import search_course_material
from tools import read_page, search_documents

ROOT = Path(__file__).parent

LOCAL_TOOLS = {t.name: t for t in [search_documents, search_course_material, read_page]}

# How to start each MCP server. "stdio" means: launch it as a child process
# and talk over its stdin/stdout.
MCP_SERVERS = {
    "study_notes": {
        "command": sys.executable,
        "args": [str(ROOT / "mcp_servers" / "notes_server.py")],
        "transport": "stdio",
    },
}


async def load_tools(names: list[str]) -> list:
    """Return the registered tools with these names, in the given order."""
    # Ask every MCP server which tools it offers (this starts each server once).
    mcp_tools = await MultiServerMCPClient(MCP_SERVERS).get_tools()
    available = {**LOCAL_TOOLS, **{t.name: t for t in mcp_tools}}

    unknown = [n for n in names if n not in available]
    if unknown:
        raise ValueError(f"Unknown tools {unknown}. Registered: {sorted(available)}")
    return [available[n] for n in names]
