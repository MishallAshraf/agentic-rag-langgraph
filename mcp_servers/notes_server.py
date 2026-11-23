"""An MCP server that lets an agent save and list study notes.

MCP (Model Context Protocol) is a standard way to offer tools to ANY agent,
not just ours. This server is a separate program: the agent starts it, talks to
it over stdin/stdout, asks which tools it has, and calls them. The same server
would work unchanged with Claude Desktop, Claude Code or any MCP client.

Run on its own (it then waits for an MCP client on stdin):
    python mcp_servers/notes_server.py
"""

import re
from pathlib import Path

from mcp.server.fastmcp import FastMCP

NOTES_DIR = Path(__file__).resolve().parent.parent / "notes"
MAX_NOTE_CHARS = 5000

mcp = FastMCP("study-notes", log_level="WARNING")


def _slug(title: str) -> str:
    # Never build a file path straight from model output: "../../x" would escape
    # the notes folder. Keep only safe characters.
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    return slug[:60] or "note"


@mcp.tool()
def save_note(title: str, content: str) -> str:
    """Save a study note for the student as a Markdown file.

    Args:
        title: a short title for the note.
        content: the note text, in Markdown.
    """
    if len(content) > MAX_NOTE_CHARS:
        return f"Note not saved: content is longer than {MAX_NOTE_CHARS} characters."
    NOTES_DIR.mkdir(exist_ok=True)
    path = NOTES_DIR / f"{_slug(title)}.md"
    path.write_text(f"# {title}\n\n{content}\n", encoding="utf-8")
    return f"Saved note '{title}' as notes/{path.name}"


@mcp.tool()
def list_notes() -> str:
    """List the titles of the student's saved study notes."""
    files = sorted(NOTES_DIR.glob("*.md")) if NOTES_DIR.exists() else []
    if not files:
        return "No notes saved yet."
    return "\n".join(f"- {f.read_text(encoding='utf-8').splitlines()[0].lstrip('# ')}" for f in files)


if __name__ == "__main__":
    mcp.run(transport="stdio")
