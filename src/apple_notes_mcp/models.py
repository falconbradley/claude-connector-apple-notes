"""Pydantic models for Apple Notes MCP server."""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel


class Folder(BaseModel):
    name: str                       # e.g. "Notes", "Recipes"
    account: str                    # e.g. "iCloud", "On My Mac"
    full_name: str                  # "account/name"
    note_count: int = 0
    is_trash: bool = False


class NoteSummary(BaseModel):
    id: int                         # NoteStore Z_PK (stable per store)
    identifier: str                 # sync UUID (stable across devices)
    folder: str                     # folder name
    account: str                    # account name
    title: str
    snippet: Optional[str] = None
    created: Optional[datetime] = None
    modified: Optional[datetime] = None
    is_pinned: bool = False
    is_password_protected: bool = False
    is_trashed: bool = False
    notes_link: Optional[str] = None    # applenotes:// deep link (blocked by most chat UIs)
    # Localhost http:// URL that opens the note in Notes.app via the
    # browser — use THIS for links in chat responses, since chat UIs
    # block custom URL schemes but open http links fine.
    open_link: Optional[str] = None


class NoteDetail(NoteSummary):
    """Full note with extracted plain-text body."""
    body_text: Optional[str] = None
    body_unavailable_reason: Optional[str] = None  # e.g. "password-protected"
    attachment_count: int = 0
    # True when the note contains checkboxes. Their state exists only in
    # the local store, so the write tools refuse to rewrite such a note.
    has_checklist: bool = False


class SearchResult(BaseModel):
    total: int
    offset: int
    limit: int
    notes: list[NoteSummary]
    # Which engine served this search: "sqlite" (fast local-store path)
    # or "applescript" (JXA fallback).
    engine: Optional[str] = None


class Attachment(BaseModel):
    """A file, link, table, or drawing embedded in a note."""
    id: int                             # NoteStore Z_PK
    note_id: int
    name: str                           # display name / filename
    type_uti: Optional[str] = None      # e.g. "public.jpeg", "com.apple.notes.table"
    kind: str = "file"                  # friendly bucket: image, pdf, link, table, ...
    url: Optional[str] = None           # for link attachments (public.url)
    file_path: Optional[str] = None     # absolute path when the file is on disk
    size_bytes: Optional[int] = None
    # True when a real file backs this attachment and is present on
    # disk. Links, tables, drawings and scans have no media file at
    # all, so this is False for them by design — not a pending download.
    has_local_file: bool = False
    created: Optional[datetime] = None


class SelectionResult(BaseModel):
    """The notes currently selected in Notes.app, with bodies.

    Selection is only observable through Notes.app scripting; the notes
    themselves are re-read through the normal engine, so titles and
    bodies come back correct even though selection specifiers expose
    little more than an id.
    """
    count: int                      # notes selected in Notes.app
    returned: int                   # notes included below (see limit)
    notes: list[NoteDetail]
    engine: Optional[str] = None


class NotesStats(BaseModel):
    total_notes: int
    pinned_notes: int
    trashed_notes: int
    password_protected_notes: int
    folder_count: int
    account_count: int


class NoteLink(BaseModel):
    id: int
    notes_link: str                     # applenotes:// deep link
    open_link: Optional[str] = None     # clickable localhost redirector link


class WriteResult(BaseModel):
    success: bool
    id: Optional[int] = None            # NoteStore Z_PK when resolvable
    title: Optional[str] = None
    folder: Optional[str] = None
    notes_link: Optional[str] = None
    open_link: Optional[str] = None
    detail: Optional[str] = None
