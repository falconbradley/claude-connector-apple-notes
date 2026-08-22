"""
Apple Notes MCP Server
======================
Exposes fast access to Apple Notes via the Model Context Protocol so
Claude Desktop can search, read, and create notes.

Reads are served from Notes.app's local store (NoteStore.sqlite) when
the host process has Full Disk Access — millisecond search over
thousands of notes, no Notes.app required. When FDA is missing, reads
fall back to an AppleScript/JXA bridge. Writes (create, append) always
go through Notes.app scripting, which requires Automation permission
(System Settings -> Privacy & Security -> Automation).

Tools provided
--------------
  get_stats            - Overview: note/folder/account counts, pinned, trashed
  list_folders         - All accounts / folders with note counts
  search_notes         - Fast search: text (title/snippet/body), folder,
                         date range, pinned; clickable open-in-Notes links
  get_note             - Full note with extracted plain-text body
  get_note_link        - applenotes:// deep link + clickable open link
  open_note_in_notes   - Front Notes.app on a note directly
  create_note          - Create a new note (optionally in a folder)
  append_to_note       - Append text to an existing note
"""

from __future__ import annotations

import logging
import sys
from datetime import datetime
from typing import Optional

from mcp.server.fastmcp import FastMCP

from .hybrid import HybridBridge
from .models import (
    Folder,
    NoteDetail,
    NoteLink,
    NotesStats,
    SearchResult,
    WriteResult,
)
from .weblink import WebLinkServer

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    stream=sys.stderr,
)
logger = logging.getLogger("apple_notes_mcp")

# ---------------------------------------------------------------------------
# Lazy-initialised shared state. HybridBridge construction is free; the
# engines underneath initialise lazily on first use so the MCP client
# never times out waiting for the initialize response.
# ---------------------------------------------------------------------------

_bridge: Optional[HybridBridge] = None
_weblink: Optional[WebLinkServer] = None

# ---------------------------------------------------------------------------
# FastMCP app
# ---------------------------------------------------------------------------

mcp = FastMCP(
    "Apple Notes",
    instructions=(
        "Fast access to Apple Notes on this Mac. You can list folders, "
        "search notes (title, snippet, and full body text), read notes, "
        "open them in Notes.app, create new notes, and append to existing "
        "ones. Use the open_link field for clickable links in responses — "
        "chat UIs block the applenotes:// scheme but open http links fine."
    ),
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _require_bridge() -> HybridBridge:
    global _bridge
    if _bridge is None:
        _bridge = HybridBridge()
        logger.info("Apple Notes MCP ready (hybrid bridge).")
    return _bridge


def _require_weblink() -> WebLinkServer:
    global _weblink
    if _weblink is None:
        _weblink = WebLinkServer(open_note=_require_bridge().open_note)
    return _weblink


def _decorate(result: SearchResult) -> SearchResult:
    weblink = _require_weblink()
    for note in result.notes:
        if note.id:
            note.open_link = weblink.open_link(note.id)
    return result


def _parse_date(value: Optional[str], name: str) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(
            f"Invalid {name} date {value!r}: use ISO format, e.g. "
            "2026-08-01 or 2026-08-01T12:00:00"
        ) from exc


# ---------------------------------------------------------------------------
# Tools — reads
# ---------------------------------------------------------------------------


@mcp.tool()
def get_stats() -> NotesStats:
    """Get overall Notes statistics: total notes, pinned, trashed,
    password-protected, folder and account counts."""
    return _require_bridge().stats()


@mcp.tool()
def list_folders() -> list[Folder]:
    """List all Notes folders across all accounts with note counts."""
    return _require_bridge().list_folders()


@mcp.tool()
def search_notes(
    query: Optional[str] = None,
    folder: Optional[str] = None,
    since: Optional[str] = None,
    until: Optional[str] = None,
    pinned_only: bool = False,
    include_trashed: bool = False,
    search_bodies: bool = True,
    limit: int = 25,
    offset: int = 0,
) -> SearchResult:
    """Search notes. All filters are optional and combine with AND.

    Args:
        query: Free text matched against title, snippet, and (when
            search_bodies is true) the full note body, case-insensitive.
        folder: Folder name (e.g. "Recipes") or "Account/Folder".
        since: Only notes modified on/after this ISO date.
        until: Only notes modified on/before this ISO date.
        pinned_only: Only pinned notes.
        include_trashed: Include notes in Recently Deleted.
        search_bodies: Set false to skip body text matching (faster).
        limit: Max results per page (default 25).
        offset: Pagination offset.

    Results are sorted by modification date, newest first. Each result
    carries an open_link — use it for clickable links in chat.
    """
    result = _require_bridge().search_notes(
        query=query,
        folder=folder,
        since=_parse_date(since, "since"),
        until=_parse_date(until, "until"),
        pinned_only=pinned_only,
        include_trashed=include_trashed,
        search_bodies=search_bodies,
        limit=max(1, min(limit, 200)),
        offset=max(0, offset),
    )
    return _decorate(result)


@mcp.tool()
def get_note(note_id: int) -> NoteDetail:
    """Read a full note: metadata plus extracted plain-text body.

    Password-protected notes return metadata only (their bodies are
    encrypted by Notes)."""
    detail = _require_bridge().get_note(note_id)
    if detail is None:
        raise ValueError(f"No note with id {note_id}")
    detail.open_link = _require_weblink().open_link(note_id)
    return detail


@mcp.tool()
def get_note_link(note_id: int) -> NoteLink:
    """Get links that open the note in Notes.app: an applenotes:// deep
    link and a clickable http open_link (use the latter in chat)."""
    bridge = _require_bridge()
    link = bridge.notes_link(note_id)
    if link is None:
        raise ValueError(f"No note with id {note_id}")
    return NoteLink(
        id=note_id,
        notes_link=link,
        open_link=_require_weblink().open_link(note_id),
    )


@mcp.tool()
def open_note_in_notes(note_id: int) -> str:
    """Open a note directly in Notes.app (fronts the app on that note).
    Reliable alternative to applenotes:// links, which chat UIs block."""
    if _require_bridge().open_note(note_id):
        return f"Note {note_id} opened in Notes.app."
    raise ValueError(
        f"Could not open note {note_id} — it may have been deleted."
    )


# ---------------------------------------------------------------------------
# Tools — writes (via Notes.app scripting; Automation permission)
# ---------------------------------------------------------------------------


@mcp.tool()
def create_note(
    title: str,
    body: str,
    folder: Optional[str] = None,
) -> WriteResult:
    """Create a new note in Notes.app.

    Args:
        title: The note title (shown as the first line in Notes).
        body: Plain-text body; line breaks are preserved.
        folder: Optional folder name (e.g. "Recipes"). Defaults to the
            account's default Notes folder.

    The note is created by Notes.app itself, so it syncs to iCloud
    natively. Never overwrites existing notes."""
    result = _require_bridge().create_note(title, body, folder)
    if result.success and result.id:
        result.open_link = _require_weblink().open_link(result.id)
    return result


@mcp.tool()
def append_to_note(note_id: int, text: str) -> WriteResult:
    """Append plain text to the end of an existing note.

    Line breaks in text are preserved. The edit is performed by
    Notes.app scripting, so it syncs natively."""
    result = _require_bridge().append_to_note(note_id, text)
    if result.success and result.id:
        result.open_link = _require_weblink().open_link(result.id)
    return result


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    logger.info("Starting Apple Notes MCP server (stdio).")
    mcp.run()


if __name__ == "__main__":
    main()
