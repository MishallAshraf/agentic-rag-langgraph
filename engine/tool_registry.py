"""The registry: every tool a bot is allowed to use, in one place.

A bot configuration only NAMES tools; it can never define new ones. That keeps
the platform safe when bots are generated (Step 10): the builder picks from
tested tools, but cannot invent code that runs on the server.

Tools come from two kinds of source:
  - local Python functions (tools.py, search_graph.py)
  - MCP servers: separate programs that offer tools over the MCP protocol

Step 10 makes tools per bot: the search tools are bound to the bot's own
collection, read_page to its own folder, and the notes server writes to its
own notes folder. The policy below says what each tool needs and whether it
changes anything; check_policy() enforces it in code, for hand-written and
generated bots alike.
"""

import sys
from pathlib import Path

from langchain_mcp_adapters.client import MultiServerMCPClient

from engine.bot_config import BotConfig
from engine.search_graph import STUDY_SUBJECT, make_search_tool
from engine.tools import make_read_page, search_documents
from engine.vectorstore import DOCUMENTS_DIR

ROOT = Path(__file__).resolve().parent.parent

# What the builder may choose from, and the rules each tool comes with.
#   writes:  changes something outside the conversation -> must need approval
#   needs:   "knowledge" (a document collection) or "pdf" (PDFs in the folder)
TOOL_POLICY = {
    "search_knowledge": {"writes": False, "needs": {"knowledge"},
                         "about": "Graded search of the bot's own documents. Every knowledge bot needs it."},
    "read_page": {"writes": False, "needs": {"knowledge", "pdf"},
                  "about": "Read a whole PDF page when a search passage is cut off. Only for PDF documents."},
    "save_note": {"writes": True, "needs": set(),
                  "about": "Save a Markdown note for the user. Always requires the user's approval."},
    "list_notes": {"writes": False, "needs": set(), "about": "List the titles of the user's saved notes."},
    # Earlier steps' tools, kept for the study assistant and older scripts.
    "search_course_material": {"writes": False, "needs": {"knowledge"}, "legacy": True,
                               "about": "The study assistant's graded search (Step 7)."},
    "search_documents": {"writes": False, "needs": {"knowledge"}, "legacy": True,
                         "about": "Ungraded search of the study assistant's documents (Step 3)."},
}

MCP_TOOLS = {"save_note", "list_notes"}


def check_policy(bot: BotConfig) -> list[str]:
    """Rules about tools that code enforces, whoever wrote the configuration."""
    problems = []
    unknown = [t for t in bot.tools if t not in TOOL_POLICY]
    if unknown:
        problems.append(f"unknown tools {unknown}; choose from {sorted(TOOL_POLICY)}")
    for name in bot.tools:
        policy = TOOL_POLICY.get(name, {})
        if policy.get("writes") and name not in bot.approval_required:
            problems.append(f"{name} changes something, so it must be in approval_required")
        needs = policy.get("needs", set())
        if "knowledge" in needs and bot.knowledge is None:
            problems.append(f"{name} needs a knowledge section")
        if "pdf" in needs and bot.knowledge and not any(bot.knowledge.path.rglob("*.pdf")):
            problems.append(f"{name} only works with PDF documents, and {bot.knowledge.folder} has none")
    return problems


def notes_dir(bot: BotConfig, user: str | None = None) -> Path:
    # Step 13: per user as well, so users of one bot cannot read each other's notes.
    # The username is validated (auth.USERNAME_PATTERN), so it is safe as a folder name.
    return ROOT / "notes" / bot.id / user if user else ROOT / "notes" / bot.id


def uses_user_data(bot: BotConfig) -> bool:
    """True if the bot's tools keep data per user, so each user needs their own tool set."""
    return bool(MCP_TOOLS & set(bot.tools))


def _local_tool(name: str, bot: BotConfig | None):
    knowledge = bot.knowledge if bot else None
    if name == "search_knowledge":
        return make_search_tool(knowledge.collection, knowledge.subject)
    if name == "search_course_material":
        if knowledge:
            return make_search_tool(knowledge.collection, knowledge.subject, name="search_course_material")
        return make_search_tool("documents_clean", STUDY_SUBJECT, name="search_course_material")
    if name == "read_page":
        return make_read_page(knowledge.path if knowledge else DOCUMENTS_DIR)
    if name == "search_documents":
        return search_documents
    return None


async def load_tools(names: list[str], bot: BotConfig | None = None, user: str | None = None) -> list:
    """Return the registered tools with these names, in the given order, made for this bot."""
    if bot is not None:
        problems = check_policy(bot)
        if problems:
            raise ValueError(f"Bot '{bot.id}' breaks the tool policy: " + "; ".join(problems))
    unknown = [n for n in names if n not in TOOL_POLICY]
    if unknown:
        raise ValueError(f"Unknown tools {unknown}. Registered: {sorted(TOOL_POLICY)}")

    mcp_tools = {}
    if MCP_TOOLS & set(names):
        # "stdio": launch the server as a child process and talk over stdin/stdout.
        # NOTES_DIR gives each bot its own notes folder.
        folder = notes_dir(bot, user) if bot else ROOT / "notes"
        server = {"command": sys.executable, "args": [str(ROOT / "mcp_servers" / "notes_server.py")],
                  "transport": "stdio", "env": {"NOTES_DIR": str(folder)}}
        mcp_tools = {t.name: t for t in await MultiServerMCPClient({"notes": server}).get_tools()}
    return [mcp_tools[n] if n in MCP_TOOLS else _local_tool(n, bot) for n in names]
