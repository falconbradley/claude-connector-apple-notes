"""
Hybrid engine: fast NoteStore.sqlite reads with a JXA fallback.

Reads prefer the local store (millisecond queries, no Notes.app needed);
when Full Disk Access is missing they transparently fall back to the
slower JXA bridge. Writes always go through Notes.app scripting so that
Notes.app owns every mutation and iCloud sync stays native.

Set APPLE_NOTES_MCP_DISABLE_FAST=1 to force the JXA path (useful for
debugging).
"""

from __future__ import annotations

import logging
import os
from datetime import datetime
from typing import Optional

from . import applescript
from .models import (
    Attachment,
    Folder,
    NoteDetail,
    NotesStats,
    NoteSummary,
    SearchResult,
    SelectionResult,
    WriteResult,
)
from .notestore import NoteStore, NoteStoreError

logger = logging.getLogger("apple_notes_mcp.hybrid")


class HybridBridge:
    def __init__(self) -> None:
        self._store = NoteStore()
        self._fast_disabled = os.environ.get(
            "APPLE_NOTES_MCP_DISABLE_FAST", ""
        ).strip() in {"1", "true", "yes"}

    # ------------------------------------------------------------------
    # Engine selection
    # ------------------------------------------------------------------

    def _fast(self) -> Optional[NoteStore]:
        if self._fast_disabled:
            return None
        return self._store if self._store.available() else None

    @property
    def store(self) -> NoteStore:
        return self._store

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------

    def stats(self) -> NotesStats:
        fast = self._fast()
        if fast:
            return fast.stats()
        folders = applescript.list_folders()
        return NotesStats(
            total_notes=sum(f["note_count"] for f in folders),
            pinned_notes=0,
            trashed_notes=0,
            password_protected_notes=0,
            folder_count=len(folders),
            account_count=len({f["account"] for f in folders}) or 1,
        )

    def list_folders(self) -> list[Folder]:
        fast = self._fast()
        if fast:
            return fast.list_folders()
        return [
            Folder(
                name=f["name"],
                account=f["account"],
                full_name=f"{f['account']}/{f['name']}",
                note_count=f["note_count"],
                is_trash=f["name"] == "Recently Deleted",
            )
            for f in applescript.list_folders()
        ]

    def search_notes(
        self,
        query: Optional[str] = None,
        folder: Optional[str] = None,
        since: Optional[datetime] = None,
        until: Optional[datetime] = None,
        pinned_only: bool = False,
        include_trashed: bool = False,
        search_bodies: bool = True,
        limit: int = 25,
        offset: int = 0,
    ) -> SearchResult:
        fast = self._fast()
        if fast:
            return fast.search_notes(
                query=query, folder=folder, since=since, until=until,
                pinned_only=pinned_only, include_trashed=include_trashed,
                search_bodies=search_bodies, limit=limit, offset=offset,
            )
        raw = applescript.search_notes(query, folder, limit + offset)
        notes = [
            NoteSummary(
                id=self._pk_from_coredata_id(n["id"]) or 0,
                identifier="",
                folder=folder or "Notes",
                account="",
                title=n["title"],
                modified=(
                    datetime.fromisoformat(n["modified"].replace("Z", "+00:00"))
                    if n.get("modified") else None
                ),
            )
            for n in raw[offset:offset + limit]
        ]
        return SearchResult(
            total=len(raw), offset=offset, limit=limit,
            notes=notes, engine="applescript",
        )

    def get_note(self, note_id: int) -> Optional[NoteDetail]:
        fast = self._fast()
        if fast:
            return fast.get_note(note_id)
        coredata_id = self._coredata_id_guess(note_id)
        if not coredata_id:
            return None
        raw = applescript.get_note_body(coredata_id)
        if not raw:
            return None
        return NoteDetail(
            id=note_id,
            identifier="",
            folder="",
            account="",
            title=raw["title"],
            body_text=raw.get("plaintext"),
            created=(
                datetime.fromisoformat(raw["created"].replace("Z", "+00:00"))
                if raw.get("created") else None
            ),
            modified=(
                datetime.fromisoformat(raw["modified"].replace("Z", "+00:00"))
                if raw.get("modified") else None
            ),
            is_password_protected=bool(raw.get("password_protected")),
        )

    # ------------------------------------------------------------------
    # Attachments (fast path only)
    #
    # Notes.app scripting exposes attachment names but no file data, and
    # the local store resolves both — so these have no JXA fallback and
    # say so plainly rather than returning a misleading empty list.
    # ------------------------------------------------------------------

    def list_attachments(self, note_id: int) -> list[Attachment]:
        fast = self._require_fast("Listing attachments")
        return fast.list_attachments(note_id)

    def get_attachment(self, attachment_id: int) -> Optional[Attachment]:
        fast = self._require_fast("Reading an attachment")
        return fast.get_attachment(attachment_id)

    def _require_fast(self, what: str) -> NoteStore:
        fast = self._fast()
        if fast is None:
            raise NoteStoreError(
                f"{what} needs the fast local-store path, which is "
                "unavailable — grant Full Disk Access to the launcher "
                "binary (see the README) and try again."
            )
        return fast

    # ------------------------------------------------------------------
    # Opening notes
    # ------------------------------------------------------------------

    def open_note(self, note_id: int) -> bool:
        """Front Notes.app on a note. Tries the applenotes:// deep link
        first (works without Automation permission), then JXA show()."""
        fast = self._fast()
        if fast:
            info = fast.resolve_note(note_id)
            if info is None:
                return False
            identifier = info.get("identifier")
            if identifier:
                from .weblink import WebLinkServer
                url = f"applenotes://showNote?identifier={identifier}"
                if WebLinkServer.open_url_with_macos(url):
                    return True
            coredata_id = info.get("coredata_id")
        else:
            coredata_id = self._coredata_id_guess(note_id)
        if not coredata_id:
            return False
        try:
            return applescript.show_note(coredata_id)
        except applescript.NotesScriptError:
            logger.exception("show_note failed for %s", coredata_id)
            return False

    def notes_link(self, note_id: int) -> Optional[str]:
        fast = self._fast()
        if fast:
            info = fast.resolve_note(note_id)
            if info and info.get("identifier"):
                return f"applenotes://showNote?identifier={info['identifier']}"
        return None

    # ------------------------------------------------------------------
    # Writes (always JXA)
    # ------------------------------------------------------------------

    def create_note(
        self, title: str, body_text: str, folder: Optional[str] = None,
        markdown: bool = False,
    ) -> WriteResult:
        raw = applescript.create_note(title, body_text, folder, markdown=markdown)
        pk = self._pk_from_coredata_id(raw.get("id", ""))
        return WriteResult(
            success=True,
            id=pk,
            title=raw.get("name") or title,
            folder=raw.get("folder"),
            notes_link=self.notes_link(pk) if pk else None,
        )

    def append_to_note(
        self, note_id: int, body_text: str, force: bool = False,
        markdown: bool = False,
    ) -> WriteResult:
        refusal = self._write_refusal(note_id, force)
        if refusal:
            return WriteResult(success=False, id=note_id, detail=refusal)
        coredata_id = self._resolve_coredata_id(note_id)
        if not coredata_id:
            return WriteResult(
                success=False, id=note_id,
                detail=f"Could not resolve note id {note_id}",
            )
        raw = applescript.append_to_note(coredata_id, body_text, markdown=markdown)
        return WriteResult(
            success=True,
            id=note_id,
            title=raw.get("name"),
            notes_link=self.notes_link(note_id),
        )

    def replace_note_body(
        self, note_id: int, body_text: str, title: Optional[str] = None,
        force: bool = False, markdown: bool = False,
    ) -> WriteResult:
        refusal = self._write_refusal(note_id, force)
        if refusal:
            return WriteResult(success=False, id=note_id, detail=refusal)
        coredata_id = self._resolve_coredata_id(note_id)
        if not coredata_id:
            return WriteResult(
                success=False, id=note_id,
                detail=f"Could not resolve note id {note_id}",
            )
        if title is None:
            # Notes derives a note's title from the first line of its
            # body, so replacing the body without re-emitting the title
            # would silently rename the note to the new first line.
            title = self._title_to_preserve(note_id, body_text)
        raw = applescript.replace_note_body(
            coredata_id, body_text, title, markdown=markdown
        )
        return WriteResult(
            success=True,
            id=note_id,
            title=raw.get("name"),
            notes_link=self.notes_link(note_id),
        )

    def move_note(self, note_id: int, folder: str) -> WriteResult:
        coredata_id = self._resolve_coredata_id(note_id)
        if not coredata_id:
            return WriteResult(
                success=False, id=note_id,
                detail=f"Could not resolve note id {note_id}",
            )
        raw = applescript.move_note(coredata_id, folder)
        return WriteResult(
            success=True,
            id=note_id,
            title=raw.get("name"),
            folder=raw.get("folder"),
            notes_link=self.notes_link(note_id),
        )

    def create_folder(self, name: str, account: Optional[str] = None) -> Folder:
        raw = applescript.create_folder(name, account)
        acct = raw.get("account") or ""
        fname = raw.get("name") or name
        return Folder(
            name=fname,
            account=acct,
            full_name=f"{acct}/{fname}" if acct else fname,
            note_count=0,
        )

    # ------------------------------------------------------------------
    # Write safety
    #
    # Both write paths hand Notes.app an HTML body. That representation
    # cannot carry checklist state (verified both directions: Notes
    # renders checkboxes as plain <ul><li>, and writing checklist markup
    # back produces a plain list), so rewriting a note that contains
    # checkboxes converts them to bullets and destroys every done-state.
    # Refuse rather than silently lose data.
    # ------------------------------------------------------------------

    # Bodies round-trip through osascript's stdout as one JSON payload,
    # and images ride along base64-encoded, so a photo-heavy note can be
    # tens of megabytes. Past this, refuse unless the caller insists.
    _LARGE_BODY_BYTES = 4 * 1024 * 1024

    def _write_refusal(self, note_id: int, force: bool) -> Optional[str]:
        """Why this note must not be rewritten, or None if it is safe."""
        fast = self._fast()
        if fast is None:
            # Without the local store we cannot inspect the note, and the
            # scripting body has already lost whatever it would tell us.
            return None
        try:
            if fast.note_has_checklist(note_id):
                return (
                    "This note contains checklist items, and Notes.app's "
                    "scripting interface cannot represent checkboxes — "
                    "rewriting the body would turn them into plain bullets "
                    "and lose which items are checked. Edit this note in "
                    "Notes.app instead. (This cannot be overridden.)"
                )
            if not force:
                size = fast.attachment_bytes(note_id)
                if size > self._LARGE_BODY_BYTES:
                    return (
                        f"This note carries about {size / 1_048_576:.0f} MB of "
                        "attachments, which Notes.app inlines as base64 when "
                        "the body is rewritten — likely to be very slow or to "
                        "time out. Pass force=true to attempt it anyway."
                    )
        except NoteStoreError:
            return None
        return None

    # ------------------------------------------------------------------
    # Selection (scripting-only; re-read through the fast path)
    # ------------------------------------------------------------------

    def selected_notes(self, limit: int = 25) -> SelectionResult:
        ids = applescript.get_selection()
        pks = [
            pk for pk in (self._pk_from_coredata_id(i) for i in ids)
            if pk is not None
        ]
        fast = self._fast()
        notes: list[NoteDetail] = []
        for pk in pks[:limit]:
            detail = self.get_note(pk)
            if detail is not None:
                notes.append(detail)
        return SelectionResult(
            count=len(pks),
            returned=len(notes),
            notes=notes,
            engine="sqlite" if fast else "applescript",
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _title_to_preserve(self, note_id: int, body_text: str) -> Optional[str]:
        """The note's current title, unless the new body already opens
        with it (which would otherwise duplicate the title line)."""
        current = self.get_note(note_id)
        if current is None or not current.title:
            return None
        first_line = body_text.split("\n", 1)[0].strip()
        if first_line == current.title.strip():
            return None
        return current.title

    def _resolve_coredata_id(self, note_id: int) -> Optional[str]:
        """The x-coredata id Notes.app scripting needs, via whichever
        engine can supply it."""
        fast = self._fast()
        if fast:
            info = fast.resolve_note(note_id)
            coredata_id = info.get("coredata_id") if info else None
            if coredata_id:
                return coredata_id
        return self._coredata_id_guess(note_id)

    def _coredata_id_guess(self, note_id: int) -> Optional[str]:
        """Best-effort x-coredata id without the fast path: the store UUID
        is still readable in some FDA-less setups; otherwise None."""
        try:
            return self._store.coredata_id(note_id)
        except NoteStoreError:
            return None

    @staticmethod
    def _pk_from_coredata_id(coredata_id: str) -> Optional[int]:
        # x-coredata://UUID/ICNote/p123 -> 123
        if "/ICNote/p" in (coredata_id or ""):
            try:
                return int(coredata_id.rsplit("p", 1)[1])
            except ValueError:
                return None
        return None
