"""An MCP server that lets an agent save and list notes.

MCP (Model Context Protocol) is a standard way to offer tools to ANY agent,
not just ours. This server is a separate program: the agent starts it, talks to
it over stdin/stdout, asks which tools it has, and calls them. The same server
would work unchanged with Claude Desktop, Claude Code or any MCP client.

Run on its own (it then waits for an MCP client on stdin):
    python mcp_servers/notes_server.py
"""

import os
import re
from pathlib import Path

from mcp.server.fastmcp import FastMCP

# Step 10: each bot gets its own folder, passed in by the tool registry. The
# server never sees anything else of the parent's environment (no API keys).
NOTES_DIR = Path(os.environ.get("NOTES_DIR") or Path(__file__).resolve().parent.parent / "notes")
MAX_NOTE_CHARS = 5000

mcp = FastMCP("notes", log_level="WARNING")


def _slug(title: str) -> str:
    # Never build a file path straight from model output: "../../x" would escape
    # the notes folder. Keep only safe characters.
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    return slug[:60] or "note"


@mcp.tool()
def save_note(title: str, content: str) -> str:
    """Save a note for the user as a Markdown file.

    Args:
        title: a short title for the note.
        content: the note text, in Markdown.
    """
    if len(content) > MAX_NOTE_CHARS:
        return f"Note not saved: content is longer than {MAX_NOTE_CHARS} characters."
    NOTES_DIR.mkdir(parents=True, exist_ok=True)
    path = NOTES_DIR / f"{_slug(title)}.md"
    path.write_text(f"# {title}\n\n{content}\n", encoding="utf-8")
    return f"Saved note '{title}' as {path.name}"


@mcp.tool()
def list_notes() -> str:
    """List the titles of the user's saved notes."""
    files = sorted(NOTES_DIR.glob("*.md")) if NOTES_DIR.exists() else []
    if not files:
        return "No notes saved yet."
    return "\n".join(f"- {f.read_text(encoding='utf-8').splitlines()[0].lstrip('# ')}" for f in files)


if __name__ == "__main__":
    mcp.run(transport="stdio")
