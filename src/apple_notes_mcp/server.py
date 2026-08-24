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
  get_selected_notes   - The notes currently selected in Notes.app
  create_note          - Create a new note (optionally in a folder)
  append_to_note       - Append text to an existing note
  update_note          - Replace a note's body (overwrites)
  create_folder        - Create a new folder
  move_note            - Move a note to another folder
  list_note_attachments- Images, PDFs, tables, links embedded in a note
  get_attachment       - One attachment, including its path on disk
  read_table           - A table embedded in a note, as rows + Markdown
"""

from __future__ import annotations

import logging
import sys
from datetime import datetime
from typing import Optional

from mcp.server.mcpserver import MCPServer

from .hybrid import HybridBridge
from .richtext import SUPPORTED_MARKDOWN
from .models import (
    Attachment,
    Folder,
    NoteDetail,
    NoteLink,
    NotesStats,
    SearchResult,
    SelectionResult,
    Table,
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
# MCP server app
# ---------------------------------------------------------------------------

mcp = MCPServer(
    "Apple Notes",
    instructions=(
        "Fast access to Apple Notes on this Mac. You can list folders, "
        "search notes (title, snippet, and full body text), read notes, "
        "see what the user has selected in Notes.app, open notes, create "
        "notes and folders, append to or rewrite notes, and move notes "
        "between folders. Use the open_link field for clickable links — "
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


def _is_markdown(format: str) -> bool:
    """Validate the format argument, rejecting typos loudly.

    Silently treating an unrecognised value as plain text would publish
    raw Markdown markers into the user's note.
    """
    normalised = (format or "plain").strip().lower()
    if normalised not in {"plain", "markdown"}:
        raise ValueError(
            f'Unknown format {format!r}: use "plain" or "markdown".'
        )
    return normalised == "markdown"


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

    Embedded attachments are rendered in place: tables appear inline as
    Markdown (and also come back structured in `tables`), links as their
    URL, and files as [kind: name]. Password-protected notes return
    metadata only (their bodies are encrypted by Notes)."""
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


@mcp.tool()
def list_note_attachments(note_id: int) -> list[Attachment]:
    """List everything embedded in a note: images, PDFs, scans, drawings,
    tables, and links.

    Each attachment reports a `kind` (image, pdf, link, table, scan,
    drawing, contact, ...). Link attachments carry the target `url`.
    Attachments backed by a real file report `file_path` and
    `size_bytes` with `has_local_file` true — read that path directly to
    view the file. Tables, links and drawings have no file by design.
    """
    return _require_bridge().list_attachments(note_id)


@mcp.tool()
def get_attachment(attachment_id: int) -> Attachment:
    """Get one attachment by id, including its path on disk.

    Use list_note_attachments to find ids. When `has_local_file` is
    true, `file_path` points at the real file and can be read or opened
    directly; the server never copies or modifies it.
    """
    attachment = _require_bridge().get_attachment(attachment_id)
    if attachment is None:
        raise ValueError(f"No attachment with id {attachment_id}")
    return attachment


@mcp.tool()
def read_table(attachment_id: int) -> Table:
    """Read a table embedded in a note, as rows and as Markdown.

    Find table ids with list_note_attachments (kind "table"). get_note
    already renders a note's tables inline in its body text, so reach
    for this when you want the cells as structured data.
    """
    table = _require_bridge().get_table(attachment_id)
    if table is None:
        raise ValueError(
            f"Attachment {attachment_id} is not a table, or its contents "
            "could not be decoded."
        )
    return table


@mcp.tool()
def get_selected_notes(limit: int = 25) -> SelectionResult:
    """Get the notes currently selected in Notes.app, including bodies.

    Use this when the user refers to what they're looking at — "this
    note", "the note I have open", "summarize what's on screen".

    Returns count (how many are selected) alongside the notes actually
    included, so a selection larger than limit is visible rather than
    silently truncated. Returns nothing when Notes.app isn't running.
    """
    return _require_bridge().selected_notes(limit=max(1, min(limit, 100)))


# ---------------------------------------------------------------------------
# Tools — writes (via Notes.app scripting; Automation permission)
# ---------------------------------------------------------------------------


@mcp.tool()
def create_note(
    title: str,
    body: str,
    folder: Optional[str] = None,
    format: str = "plain",
) -> WriteResult:
    """Create a new note in Notes.app.

    Args:
        title: The note title (shown as the first line in Notes).
        body: Plain-text body; line breaks are preserved.
        folder: Optional folder name (e.g. "Recipes"). Defaults to the
            account's default Notes folder.
        format: "plain" (default) leaves the text exactly as given.
            "markdown" renders rich text — **bold**, *italic*, ~~strikethrough~~, `code`, [links](url), # headings, - bullet lists, and 1. numbered lists.
            Notes silently ignores colour, font size and blockquotes,
            and headings render bold rather than becoming Notes' own
            heading styles.

    The note is created by Notes.app itself, so it syncs to iCloud
    natively. Never overwrites existing notes."""
    result = _require_bridge().create_note(
        title, body, folder, markdown=_is_markdown(format)
    )
    if result.success and result.id:
        result.open_link = _require_weblink().open_link(result.id)
    return result


@mcp.tool()
def append_to_note(
    note_id: int, text: str, force: bool = False, format: str = "plain"
) -> WriteResult:
    """Append plain text to the end of an existing note.

    Line breaks in text are preserved. The edit is performed by
    Notes.app scripting, so it syncs natively.

    Refused for notes containing checklists: Notes.app's scripting
    interface cannot represent checkboxes, so any rewrite would turn
    them into plain bullets and lose which items are checked. Also
    refused for notes with very large attachments, since those are
    inlined as base64 and can time out — pass force=true for that case
    only (it never overrides the checklist refusal)

    Set format="markdown" for rich text — **bold**, *italic*, ~~strikethrough~~, `code`, [links](url), # headings, - bullet lists, and 1. numbered lists."""
    result = _require_bridge().append_to_note(
        note_id, text, force=force, markdown=_is_markdown(format)
    )
    if result.success and result.id:
        result.open_link = _require_weblink().open_link(result.id)
    return result


@mcp.tool()
def update_note(
    note_id: int,
    body: str,
    title: Optional[str] = None,
    force: bool = False,
    format: str = "plain",
) -> WriteResult:
    """Replace an existing note's body with new text.

    This OVERWRITES the note — the previous body is not recoverable from
    this server. Prefer append_to_note when adding to a note. Read the
    note first with get_note if you need to preserve existing content.

    Args:
        note_id: The note to rewrite.
        body: The new plain-text body; line breaks are preserved.
        title: Optional new title. Omit to keep the note's current
            title — Notes takes a note's title from the first line of
            its body, so the existing title is re-emitted as that line.

    Password-protected notes are refused, as are notes containing
    checklists (Notes.app's scripting interface cannot represent
    checkboxes, so a rewrite would destroy their state) and notes with
    very large attachments (pass force=true for the size case only)

    Set format="markdown" for rich text — **bold**, *italic*, ~~strikethrough~~, `code`, [links](url), # headings, - bullet lists, and 1. numbered lists."""
    result = _require_bridge().replace_note_body(
        note_id, body, title, force=force, markdown=_is_markdown(format)
    )
    if result.success and result.id:
        result.open_link = _require_weblink().open_link(result.id)
    return result


@mcp.tool()
def create_folder(name: str, account: Optional[str] = None) -> Folder:
    """Create a new folder in Notes.app.

    Args:
        name: Folder name. Fails if the account already has one.
        account: Account name (e.g. "iCloud"). Defaults to the default
            account.
    """
    return _require_bridge().create_folder(name, account)


@mcp.tool()
def move_note(note_id: int, folder: str) -> WriteResult:
    """Move a note into an existing folder.

    Args:
        note_id: The note to move.
        folder: Destination folder name (e.g. "Recipes") or
            "Account/Folder". Must already exist — use create_folder
            first if needed.
    """
    result = _require_bridge().move_note(note_id, folder)
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
