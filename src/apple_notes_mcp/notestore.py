"""
Fast read engine for Apple Notes: direct access to NoteStore.sqlite.

Notes.app persists every note (including iCloud-synced ones) in a Core
Data store at

    ~/Library/Group Containers/group.com.apple.notes/NoteStore.sqlite

Reading it directly is orders of magnitude faster than Apple Events and
works even when Notes.app is not running. The store is opened strictly
read-only (URI mode=ro + PRAGMA query_only) and never mutated; Notes.app
remains the sync and auth engine.

Requires Full Disk Access for the host process. When the store cannot be
opened, callers fall back to the AppleScript/JXA bridge.

Schema notes
------------
All synced objects live in one wide table, ZICCLOUDSYNCINGOBJECT, with
Z_ENT discriminating the entity (ICNote / ICFolder / ICAccount — the ids
vary across macOS releases, so we resolve them from Z_PRIMARYKEY at
runtime). Column names also drift between releases (ZTITLE1 vs ZTITLE,
ZACCOUNT4 vs ZACCOUNT7, ...), so we introspect the live schema and pick
whichever candidate column exists and is populated.

Note bodies are gzip-compressed protobufs in ZICNOTEDATA.ZDATA; see
protobuf.py for extraction. Password-protected notes carry a crypto tag
and their bodies are not readable (by design).

Core Data timestamps are seconds since 2001-01-01 UTC (Unix + 978307200).
"""

from __future__ import annotations

import logging
import re
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

from .models import (
    Attachment,
    Folder,
    NoteDetail,
    NotesStats,
    NoteSummary,
    SearchResult,
    Table,
    Tag,
)
from .protobuf import extract_attachment_refs, extract_note_text, has_checklist
from .tables import decode_table, table_to_markdown

logger = logging.getLogger("apple_notes_mcp.notestore")

_STORE_PATH = (
    Path.home() / "Library" / "Group Containers" / "group.com.apple.notes"
    / "NoteStore.sqlite"
)

_CORE_DATA_EPOCH = datetime(2001, 1, 1, tzinfo=timezone.utc)

_TRASH_FOLDER_IDENTIFIERS = {"TrashFolder-CloudKit", "TrashFolder-Local"}

# Attachment files live beside the store, one directory per account:
#   Accounts/<account-uuid>/Media/<media-uuid>/<generation>/<filename>
_MEDIA_ROOT = (
    Path.home() / "Library" / "Group Containers" / "group.com.apple.notes"
    / "Accounts"
)

# Friendly buckets for the UTIs Notes actually stores, so callers can
# filter without knowing Apple's type identifiers.
_UTI_KINDS = (
    ("com.apple.notes.table", "table"),
    ("com.apple.notes.gallery", "gallery"),
    ("com.apple.drawing", "drawing"),
    ("com.apple.paper.doc.scan", "scan"),
    ("com.apple.paper", "scan"),
    ("public.url", "link"),
    ("public.vcard", "contact"),
    ("com.adobe.pdf", "pdf"),
    ("public.jpeg", "image"),
    ("public.png", "image"),
    ("public.tiff", "image"),
    ("public.heic", "image"),
    ("public.image", "image"),
    ("public.movie", "video"),
    ("public.audio", "audio"),
)


def _token_kind(alt_text: Optional[str], token_id: Optional[str]) -> str:
    """Classify an inline attachment: hashtag, mention, divider, ...

    Classification keys off the display text rather than the token
    identifier, which is an opaque hash — two different people can share
    one (observed with @Dad and @John), so it identifies nothing useful.
    """
    text = (alt_text or "").strip()
    if text.startswith("#"):
        return "hashtag"
    if text.startswith("@"):
        return "mention"
    identifier = token_id or ""
    if "dividerline" in identifier:
        return "divider"
    if "Calculate" in identifier:
        return "calculation"
    return "other"


def _uti_kind(uti: Optional[str]) -> str:
    """Map a UTI to a friendly kind; unknown types fall back to "file"."""
    if not uti:
        return "file"
    for prefix, kind in _UTI_KINDS:
        if uti == prefix or uti.startswith(prefix + "."):
            return kind
    if uti.startswith("public.") and "image" in uti:
        return "image"
    return "file"


def _cd_date(value: Optional[float]) -> Optional[datetime]:
    if value is None:
        return None
    try:
        return _CORE_DATA_EPOCH + timedelta(seconds=float(value))
    except (OverflowError, ValueError):
        return None


class NoteStoreError(RuntimeError):
    """Raised when the local store is unavailable (usually missing FDA)."""


class NoteStore:
    """Read-only view over NoteStore.sqlite."""

    def __init__(self, store_path: Optional[Path] = None) -> None:
        self._path = store_path or _STORE_PATH
        self._local = threading.local()
        self._lock = threading.Lock()
        self._schema: Optional[dict] = None
        self.store_uuid: Optional[str] = None

    # ------------------------------------------------------------------
    # Connection / schema introspection
    # ------------------------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        con = getattr(self._local, "con", None)
        if con is not None:
            return con
        if not self._path.exists():
            raise NoteStoreError(
                f"Notes store not found at {self._path}. Is Notes set up on "
                "this Mac, and does the host process have Full Disk Access?"
            )
        try:
            con = sqlite3.connect(
                f"file:{self._path}?mode=ro", uri=True, check_same_thread=False
            )
            con.execute("PRAGMA query_only = 1")
            con.row_factory = sqlite3.Row
        except sqlite3.Error as exc:
            raise NoteStoreError(
                f"Could not open Notes store read-only: {exc}. This usually "
                "means Full Disk Access is missing — see the README."
            ) from exc
        self._local.con = con
        self._ensure_schema(con)
        return con

    def available(self) -> bool:
        try:
            self._connect()
            return True
        except NoteStoreError:
            return False

    def _ensure_schema(self, con: sqlite3.Connection) -> None:
        with self._lock:
            if self._schema is not None:
                return
            ents = {
                row["Z_NAME"]: row["Z_ENT"]
                for row in con.execute("SELECT Z_ENT, Z_NAME FROM Z_PRIMARYKEY")
            }
            for needed in ("ICNote", "ICFolder", "ICAccount"):
                if needed not in ents:
                    raise NoteStoreError(
                        f"Unrecognised NoteStore schema: entity {needed} not "
                        "found. This macOS release may not be supported yet."
                    )
            cols = {
                row["name"]
                for row in con.execute("PRAGMA table_info(ZICCLOUDSYNCINGOBJECT)")
            }
            schema = {
                "ent_note": ents["ICNote"],
                "ent_folder": ents["ICFolder"],
                "ent_account": ents["ICAccount"],
                "note_title": self._pick(cols, ["ZTITLE1", "ZTITLE"]),
                "folder_title": self._pick(cols, ["ZTITLE2", "ZTITLE"]),
                "note_created": self._pick(
                    cols, ["ZCREATIONDATE3", "ZCREATIONDATE2", "ZCREATIONDATE1", "ZCREATIONDATE"]
                ),
                "note_modified": self._pick(
                    cols, ["ZMODIFICATIONDATE1", "ZMODIFICATIONDATE"]
                ),
                "snippet": "ZSNIPPET" if "ZSNIPPET" in cols else None,
                "pinned": "ZISPINNED" if "ZISPINNED" in cols else None,
                "protected": (
                    "ZISPASSWORDPROTECTED" if "ZISPASSWORDPROTECTED" in cols else None
                ),
                "account_name": "ZNAME" if "ZNAME" in cols else None,
            }
            # The folder->account relation column drifts (ZACCOUNT4,
            # ZACCOUNT7, ...): pick the ZACCOUNT* column that is actually
            # populated on folder rows.
            schema["folder_account"] = self._pick_populated(
                con,
                sorted(c for c in cols if re.fullmatch(r"ZACCOUNT\d*", c)),
                ents["ICFolder"],
            )
            # Attachments (ICAttachment) and their on-disk files (ICMedia)
            # are optional: absent on very old schemas, so every attachment
            # read degrades to "no attachments" rather than failing.
            schema["ent_attachment"] = ents.get("ICAttachment")
            schema["ent_media"] = ents.get("ICMedia")
            # Dividers, hashtags and mentions are inline attachments and
            # carry their own display text.
            schema["ent_inline"] = ents.get("ICInlineAttachment")
            schema["inline_alt"] = "ZALTTEXT" if "ZALTTEXT" in cols else None
            schema["inline_token"] = (
                "ZTOKENCONTENTIDENTIFIER"
                if "ZTOKENCONTENTIDENTIFIER" in cols else None
            )
            if schema["ent_attachment"]:
                ent_a = schema["ent_attachment"]
                schema["att_title"] = self._pick_populated(
                    con, ["ZTITLE", "ZTITLE1", "ZTITLE2"], ent_a
                )
                schema["att_uti"] = self._pick_populated(
                    con, ["ZTYPEUTI", "ZTYPEUTI1"], ent_a
                )
                schema["att_url"] = self._pick_populated(
                    con, ["ZURLSTRING", "ZREMOTEFILEURLSTRING"], ent_a
                )
                schema["att_note"] = self._pick_populated(
                    con, sorted(c for c in cols if re.fullmatch(r"ZNOTE\d*", c)), ent_a
                )
                schema["att_media"] = self._pick_populated(
                    con, sorted(c for c in cols if re.fullmatch(r"ZMEDIA\d*", c)), ent_a
                )
                schema["att_created"] = self._pick(
                    cols, ["ZCREATIONDATE", "ZCREATIONDATE1"]
                )
                schema["att_filename"] = self._pick(cols, ["ZFILENAME"])
                schema["att_identifier"] = "ZIDENTIFIER" if "ZIDENTIFIER" in cols else None
                # Tables live here as a gzipped CRDT document.
                schema["att_mergeable"] = self._pick(
                    cols, ["ZMERGEABLEDATA1", "ZMERGEABLEDATA", "ZMERGEABLEDATA2"]
                )
            self._schema = schema
            try:
                row = con.execute("SELECT Z_UUID FROM Z_METADATA").fetchone()
                self.store_uuid = row["Z_UUID"] if row else None
            except sqlite3.Error:
                self.store_uuid = None
            logger.info(
                "NoteStore schema resolved: note ent=%d, folder ent=%d, "
                "title=%s, modified=%s, folder_account=%s",
                schema["ent_note"], schema["ent_folder"],
                schema["note_title"], schema["note_modified"],
                schema["folder_account"],
            )

    @staticmethod
    def _pick(cols: set, candidates: list[str]) -> Optional[str]:
        for c in candidates:
            if c in cols:
                return c
        return None

    @staticmethod
    def _pick_populated(
        con: sqlite3.Connection, candidates: list[str], ent: int
    ) -> Optional[str]:
        for c in candidates:
            try:
                row = con.execute(
                    f"SELECT count(*) AS n FROM ZICCLOUDSYNCINGOBJECT "
                    f"WHERE Z_ENT=? AND {c} IS NOT NULL",
                    (ent,),
                ).fetchone()
                if row and row["n"] > 0:
                    return c
            except sqlite3.Error:
                continue
        return None

    def _sch(self) -> dict:
        self._connect()
        assert self._schema is not None
        return self._schema

    # ------------------------------------------------------------------
    # Folder / account maps
    # ------------------------------------------------------------------

    def _account_names(self, con: sqlite3.Connection) -> dict[int, str]:
        s = self._sch()
        if not s["account_name"]:
            return {}
        return {
            row["Z_PK"]: row[s["account_name"]] or "Unknown"
            for row in con.execute(
                f"SELECT Z_PK, {s['account_name']} FROM ZICCLOUDSYNCINGOBJECT "
                f"WHERE Z_ENT=?",
                (s["ent_account"],),
            )
        }

    def _folder_map(self, con: sqlite3.Connection) -> dict[int, dict[str, Any]]:
        """Z_PK -> {name, account, is_trash} for every folder."""
        s = self._sch()
        accounts = self._account_names(con)
        acct_col = f", {s['folder_account']}" if s["folder_account"] else ""
        folders: dict[int, dict[str, Any]] = {}
        for row in con.execute(
            f"SELECT Z_PK, {s['folder_title']} AS title, ZIDENTIFIER{acct_col} "
            f"FROM ZICCLOUDSYNCINGOBJECT WHERE Z_ENT=? "
            f"AND (ZMARKEDFORDELETION IS NULL OR ZMARKEDFORDELETION=0)",
            (s["ent_folder"],),
        ):
            acct_pk = row[s["folder_account"]] if s["folder_account"] else None
            folders[row["Z_PK"]] = {
                "name": row["title"] or "Untitled",
                "account": accounts.get(acct_pk, "iCloud" if accounts else "Local"),
                "is_trash": (row["ZIDENTIFIER"] or "") in _TRASH_FOLDER_IDENTIFIERS,
            }
        return folders

    # ------------------------------------------------------------------
    # Public reads
    # ------------------------------------------------------------------

    def stats(self) -> NotesStats:
        con = self._connect()
        s = self._sch()
        folders = self._folder_map(con)
        trash_pks = {pk for pk, f in folders.items() if f["is_trash"]}
        total = pinned = trashed = protected = 0
        pinned_col = s["pinned"] or "NULL"
        prot_col = s["protected"] or "NULL"
        for row in con.execute(
            f"SELECT ZFOLDER, {pinned_col} AS pinned, {prot_col} AS prot "
            f"FROM ZICCLOUDSYNCINGOBJECT WHERE Z_ENT=? "
            f"AND (ZMARKEDFORDELETION IS NULL OR ZMARKEDFORDELETION=0)",
            (s["ent_note"],),
        ):
            if row["ZFOLDER"] in trash_pks:
                trashed += 1
                continue
            total += 1
            if row["pinned"]:
                pinned += 1
            if row["prot"]:
                protected += 1
        return NotesStats(
            total_notes=total,
            pinned_notes=pinned,
            trashed_notes=trashed,
            password_protected_notes=protected,
            folder_count=sum(1 for f in folders.values() if not f["is_trash"]),
            account_count=max(1, len(self._account_names(con))),
        )

    def list_folders(self) -> list[Folder]:
        con = self._connect()
        s = self._sch()
        folders = self._folder_map(con)
        counts: dict[int, int] = {}
        for row in con.execute(
            f"SELECT ZFOLDER, count(*) AS n FROM ZICCLOUDSYNCINGOBJECT "
            f"WHERE Z_ENT=? AND (ZMARKEDFORDELETION IS NULL OR ZMARKEDFORDELETION=0) "
            f"GROUP BY ZFOLDER",
            (s["ent_note"],),
        ):
            counts[row["ZFOLDER"]] = row["n"]
        out = [
            Folder(
                name=f["name"],
                account=f["account"],
                full_name=f"{f['account']}/{f['name']}",
                note_count=counts.get(pk, 0),
                is_trash=f["is_trash"],
            )
            for pk, f in folders.items()
        ]
        out.sort(key=lambda f: (f.is_trash, f.account.lower(), f.name.lower()))
        return out

    def search_notes(
        self,
        query: Optional[str] = None,
        folder: Optional[str] = None,
        since: Optional[datetime] = None,
        until: Optional[datetime] = None,
        pinned_only: bool = False,
        include_trashed: bool = False,
        search_bodies: bool = True,
        tag: Optional[str] = None,
        limit: int = 25,
        offset: int = 0,
    ) -> SearchResult:
        con = self._connect()
        s = self._sch()
        folders = self._folder_map(con)

        tagged: Optional[set[int]] = None
        if tag:
            tagged = self.notes_with_token(tag)
            if not tagged:
                return SearchResult(
                    total=0, offset=offset, limit=limit, notes=[], engine="sqlite"
                )

        where = [
            "n.Z_ENT = ?",
            "(n.ZMARKEDFORDELETION IS NULL OR n.ZMARKEDFORDELETION = 0)",
        ]
        params: list[Any] = [s["ent_note"]]

        if folder:
            matching = [
                pk for pk, f in folders.items()
                if f["name"].lower() == folder.lower()
                or f"{f['account']}/{f['name']}".lower() == folder.lower()
            ]
            if not matching:
                return SearchResult(
                    total=0, offset=offset, limit=limit, notes=[], engine="sqlite"
                )
            where.append(
                "n.ZFOLDER IN (" + ",".join("?" * len(matching)) + ")"
            )
            params.extend(matching)
        elif not include_trashed:
            trash_pks = [pk for pk, f in folders.items() if f["is_trash"]]
            if trash_pks:
                where.append(
                    "(n.ZFOLDER IS NULL OR n.ZFOLDER NOT IN ("
                    + ",".join("?" * len(trash_pks)) + "))"
                )
                params.extend(trash_pks)

        mod_col = s["note_modified"]
        if since and mod_col:
            where.append(f"n.{mod_col} >= ?")
            params.append((since.astimezone(timezone.utc) - _CORE_DATA_EPOCH).total_seconds())
        if until and mod_col:
            where.append(f"n.{mod_col} <= ?")
            params.append((until.astimezone(timezone.utc) - _CORE_DATA_EPOCH).total_seconds())
        if pinned_only and s["pinned"]:
            where.append(f"n.{s['pinned']} = 1")
        if tagged is not None:
            where.append("n.Z_PK IN (" + ",".join("?" * len(tagged)) + ")")
            params.extend(sorted(tagged))

        select_cols = self._note_select_columns()
        sql = (
            f"SELECT {select_cols} FROM ZICCLOUDSYNCINGOBJECT n "
            f"WHERE {' AND '.join(where)} "
            f"ORDER BY n.{mod_col} DESC" if mod_col else
            f"SELECT {select_cols} FROM ZICCLOUDSYNCINGOBJECT n "
            f"WHERE {' AND '.join(where)}"
        )
        rows = con.execute(sql, params).fetchall()

        if query:
            q = query.lower()
            title_snippet_hits = []
            body_check_rows = []
            for row in rows:
                title = (row["title"] or "").lower()
                snippet = (row["snippet"] or "").lower() if "snippet" in row.keys() else ""
                if q in title or q in snippet:
                    title_snippet_hits.append(row)
                elif search_bodies:
                    body_check_rows.append(row)
            body_hits = []
            if search_bodies and body_check_rows:
                body_hits = self._filter_by_body(con, body_check_rows, q)
            rows = title_snippet_hits + body_hits
            if mod_col:
                rows.sort(
                    key=lambda r: r["modified"] or 0, reverse=True
                )

        total = len(rows)
        page = rows[offset:offset + limit]
        notes = [self._row_to_summary(row, folders) for row in page]
        return SearchResult(
            total=total, offset=offset, limit=limit, notes=notes, engine="sqlite"
        )

    def get_note(self, note_id: int) -> Optional[NoteDetail]:
        con = self._connect()
        s = self._sch()
        folders = self._folder_map(con)
        row = con.execute(
            f"SELECT {self._note_select_columns()} FROM ZICCLOUDSYNCINGOBJECT n "
            f"WHERE n.Z_ENT=? AND n.Z_PK=?",
            (s["ent_note"], note_id),
        ).fetchone()
        if row is None:
            return None
        summary = self._row_to_summary(row, folders)
        detail = NoteDetail(**summary.model_dump())
        detail.attachment_count = self.attachment_count(note_id)
        if detail.is_password_protected:
            detail.body_unavailable_reason = (
                "password-protected — Notes encrypts locked note bodies"
            )
            return detail
        blob_row = con.execute(
            "SELECT ZDATA FROM ZICNOTEDATA WHERE Z_PK = ?",
            (row["ZNOTEDATA"],),
        ).fetchone() if row["ZNOTEDATA"] is not None else None
        if blob_row and blob_row["ZDATA"]:
            zdata = blob_row["ZDATA"]
            # Keep the raw placeholders so each one can be replaced with
            # the attachment it actually refers to, in document order.
            body = extract_note_text(zdata, attachment_placeholder="\ufffc")
            detail.has_checklist = has_checklist(zdata)
            if body is not None:
                refs = extract_attachment_refs(zdata)
                detail.tables, renderings, tokens = self._note_placeholders(
                    note_id, refs
                )
                seen_tags, seen_mentions = [], []
                for kind, text in tokens:
                    bucket = seen_tags if kind == "hashtag" else seen_mentions
                    if text not in bucket:
                        bucket.append(text)
                detail.hashtags, detail.mentions = seen_tags, seen_mentions
                for rendering in renderings:
                    body = body.replace("\ufffc", rendering, 1)
                body = body.replace("\ufffc", "[attachment]")
            detail.body_text = body
            if detail.body_text is None:
                detail.body_unavailable_reason = "body could not be decoded"
        else:
            detail.body_unavailable_reason = "no body data in local store"
        return detail

    def resolve_note(self, note_id: int) -> Optional[dict[str, Any]]:
        """Return {identifier, coredata_id, title} for the weblink opener."""
        con = self._connect()
        s = self._sch()
        row = con.execute(
            f"SELECT Z_PK, ZIDENTIFIER, {s['note_title']} AS title "
            f"FROM ZICCLOUDSYNCINGOBJECT WHERE Z_ENT=? AND Z_PK=?",
            (s["ent_note"], note_id),
        ).fetchone()
        if row is None:
            return None
        return {
            "identifier": row["ZIDENTIFIER"],
            "coredata_id": self.coredata_id(note_id),
            "title": row["title"],
        }

    def coredata_id(self, note_pk: int) -> Optional[str]:
        """The x-coredata:// object URI Notes.app scripting uses as `id`."""
        self._connect()
        if not self.store_uuid:
            return None
        return f"x-coredata://{self.store_uuid}/ICNote/p{note_pk}"

    def pk_from_identifier(self, identifier: str) -> Optional[int]:
        con = self._connect()
        s = self._sch()
        row = con.execute(
            "SELECT Z_PK FROM ZICCLOUDSYNCINGOBJECT WHERE Z_ENT=? AND ZIDENTIFIER=?",
            (s["ent_note"], identifier),
        ).fetchone()
        return row["Z_PK"] if row else None

    # ------------------------------------------------------------------
    # Attachments
    # ------------------------------------------------------------------

    def list_attachments(self, note_id: int) -> list[Attachment]:
        """Every attachment on a note, with on-disk paths where available."""
        con = self._connect()
        s = self._sch()
        if not s.get("ent_attachment") or not s.get("att_note"):
            return []
        rows = con.execute(
            f"SELECT {self._attachment_columns()} FROM ZICCLOUDSYNCINGOBJECT a "
            f"WHERE a.Z_ENT=? AND a.{s['att_note']}=?",
            (s["ent_attachment"], note_id),
        ).fetchall()
        media = self._media_map(con, [r["media_pk"] for r in rows])
        return [self._row_to_attachment(r, note_id, media) for r in rows]

    def get_attachment(self, attachment_id: int) -> Optional[Attachment]:
        con = self._connect()
        s = self._sch()
        if not s.get("ent_attachment"):
            return None
        row = con.execute(
            f"SELECT {self._attachment_columns()} FROM ZICCLOUDSYNCINGOBJECT a "
            f"WHERE a.Z_ENT=? AND a.Z_PK=?",
            (s["ent_attachment"], attachment_id),
        ).fetchone()
        if row is None:
            return None
        media = self._media_map(con, [row["media_pk"]])
        return self._row_to_attachment(row, row["note_pk"], media)

    def attachment_count(self, note_id: int) -> int:
        con = self._connect()
        s = self._sch()
        if not s.get("ent_attachment") or not s.get("att_note"):
            return 0
        row = con.execute(
            f"SELECT COUNT(*) AS n FROM ZICCLOUDSYNCINGOBJECT "
            f"WHERE Z_ENT=? AND {s['att_note']}=?",
            (s["ent_attachment"], note_id),
        ).fetchone()
        return row["n"] if row else 0

    def attachment_bytes(self, note_id: int) -> int:
        """Total on-disk size of a note's attachments.

        Used to predict how large the note's HTML body will be when
        Notes.app inlines attachments as base64 for a rewrite.
        """
        return sum(a.size_bytes or 0 for a in self.list_attachments(note_id))

    def note_has_checklist(self, note_id: int) -> bool:
        """True if the note contains checkboxes (see protobuf.has_checklist)."""
        con = self._connect()
        s = self._sch()
        row = con.execute(
            "SELECT d.ZDATA FROM ZICCLOUDSYNCINGOBJECT n "
            "JOIN ZICNOTEDATA d ON n.ZNOTEDATA = d.Z_PK "
            "WHERE n.Z_ENT=? AND n.Z_PK=?",
            (s["ent_note"], note_id),
        ).fetchone()
        if row is None or not row["ZDATA"]:
            return False
        return has_checklist(row["ZDATA"])

    def get_table(self, attachment_id: int) -> Optional[Table]:
        """Decode one table attachment, or None if it is not a table."""
        con = self._connect()
        s = self._sch()
        if not s.get("ent_attachment") or not s.get("att_mergeable"):
            return None
        row = con.execute(
            f"SELECT Z_PK, {s['att_note']} AS note_pk, "
            f"{s['att_mergeable']} AS blob "
            f"FROM ZICCLOUDSYNCINGOBJECT WHERE Z_ENT=? AND Z_PK=?",
            (s["ent_attachment"], attachment_id),
        ).fetchone()
        if row is None or not row["blob"]:
            return None
        rows = decode_table(row["blob"])
        if rows is None:
            return None
        return Table(
            attachment_id=row["Z_PK"],
            note_id=row["note_pk"] or 0,
            row_count=len(rows),
            column_count=max((len(r) for r in rows), default=0),
            rows=rows,
            markdown=table_to_markdown(rows),
        )

    def _note_placeholders(
        self, note_id: int, refs: list[tuple[str, Optional[str]]]
    ) -> tuple[list[Table], list[str], list[tuple[str, str]]]:
        """Resolve a note's attachments in document order.

        Returns the decoded tables, one rendering per placeholder so the
        caller can substitute them back into the body positionally, and
        the (kind, text) of each hashtag or mention encountered.
        """
        con = self._connect()
        s = self._sch()
        by_identifier: dict[str, sqlite3.Row] = {}
        if s.get("ent_attachment") and s.get("att_note"):
            # Any of these columns may be absent on an older schema, so
            # each degrades to NULL rather than breaking the whole read.
            def column(key: str, alias: str) -> str:
                return f"{s[key]} AS {alias}" if s.get(key) else f"NULL AS {alias}"

            select = ", ".join(
                ["Z_PK", "ZIDENTIFIER",
                 column("att_uti", "uti"), column("att_title", "title"),
                 column("att_url", "url"), column("att_mergeable", "blob")]
            )
            for row in con.execute(
                f"SELECT {select} FROM ZICCLOUDSYNCINGOBJECT "
                f"WHERE Z_ENT=? AND {s['att_note']}=?",
                (s["ent_attachment"], note_id),
            ):
                if row["ZIDENTIFIER"]:
                    by_identifier[row["ZIDENTIFIER"]] = row

        inline = self._inline_attachments(
            con, [i for i, _u in refs if i not in by_identifier]
        )

        tables: list[Table] = []
        renderings: list[str] = []
        tokens: list[tuple[str, str]] = []
        for identifier, uti in refs:
            row = by_identifier.get(identifier)
            if row is None:
                # Dividers, hashtags and mentions render as their own
                # display text ("---", "#tag", "@name").
                alt, token_id = inline.get(identifier, (None, None))
                kind = _token_kind(alt, token_id)
                if kind in ("hashtag", "mention") and alt:
                    tokens.append((kind, alt.strip()))
                renderings.append(alt if alt else "[attachment]")
                continue
            kind = _uti_kind(row["uti"] or uti)
            if kind == "table" and row["blob"]:
                decoded = decode_table(row["blob"])
                if decoded is not None:
                    markdown = table_to_markdown(decoded)
                    tables.append(
                        Table(
                            attachment_id=row["Z_PK"],
                            note_id=note_id,
                            row_count=len(decoded),
                            column_count=max((len(r) for r in decoded), default=0),
                            rows=decoded,
                            markdown=markdown,
                        )
                    )
                    renderings.append(f"\n{markdown}\n")
                    continue
            name = row["title"] or ""
            if kind == "link" and row["url"]:
                renderings.append(f"[link: {row['url']}]")
            elif name:
                renderings.append(f"[{kind}: {name}]")
            else:
                renderings.append(f"[{kind}]")
        return tables, renderings, tokens

    def _inline_attachments(
        self, con: sqlite3.Connection, identifiers: list[str]
    ) -> dict[str, tuple[Optional[str], Optional[str]]]:
        """(display text, token id) for inline attachments, by identifier."""
        s = self._sch()
        if not identifiers or not s.get("ent_inline") or not s.get("inline_alt"):
            return {}
        token_col = (
            "ZTOKENCONTENTIDENTIFIER" if s.get("inline_token") else "NULL"
        )
        out: dict[str, tuple[Optional[str], Optional[str]]] = {}
        for i in range(0, len(identifiers), 500):
            chunk = identifiers[i:i + 500]
            for row in con.execute(
                f"SELECT ZIDENTIFIER, {s['inline_alt']} AS alt, "
                f"{token_col} AS token "
                f"FROM ZICCLOUDSYNCINGOBJECT WHERE Z_ENT=? AND ZIDENTIFIER IN ("
                + ",".join("?" * len(chunk)) + ")",
                (s["ent_inline"], *chunk),
            ):
                if row["ZIDENTIFIER"]:
                    out[row["ZIDENTIFIER"]] = (row["alt"], row["token"])
        return out

    # ------------------------------------------------------------------
    # Hashtags and mentions
    #
    # Inline attachments carry no link back to their note, so the only
    # way to associate them is through the identifiers in each note's
    # attribute runs — which means decoding note bodies, the same cost
    # as a full-text body search.
    # ------------------------------------------------------------------

    def note_tokens(self, note_id: int) -> list[tuple[str, str]]:
        """(kind, text) for each hashtag/mention in a note, in order."""
        con = self._connect()
        s = self._sch()
        row = con.execute(
            "SELECT d.ZDATA FROM ZICCLOUDSYNCINGOBJECT n "
            "JOIN ZICNOTEDATA d ON n.ZNOTEDATA = d.Z_PK "
            "WHERE n.Z_ENT=? AND n.Z_PK=?",
            (s["ent_note"], note_id),
        ).fetchone()
        if row is None or not row["ZDATA"]:
            return []
        refs = extract_attachment_refs(row["ZDATA"])
        if not refs:
            return []
        inline = self._inline_attachments(
            con, [identifier for identifier, _uti in refs]
        )
        tokens = []
        for identifier, _uti in refs:
            alt, token_id = inline.get(identifier, (None, None))
            kind = _token_kind(alt, token_id)
            if kind in ("hashtag", "mention") and alt:
                tokens.append((kind, alt.strip()))
        return tokens

    def list_tags(self, include_trashed: bool = False) -> list[Tag]:
        """Every hashtag and mention across all notes, with counts."""
        con = self._connect()
        s = self._sch()
        folders = self._folder_map(con)
        found: dict[tuple[str, str], dict[str, Any]] = {}
        for row in con.execute(
            f"SELECT n.Z_PK, n.ZFOLDER, d.ZDATA FROM ZICCLOUDSYNCINGOBJECT n "
            f"JOIN ZICNOTEDATA d ON n.ZNOTEDATA = d.Z_PK WHERE n.Z_ENT=?",
            (s["ent_note"],),
        ):
            if not include_trashed:
                folder = folders.get(row["ZFOLDER"])
                if folder is None or folder["is_trash"]:
                    continue
            refs = extract_attachment_refs(row["ZDATA"])
            if not refs:
                continue
            inline = self._inline_attachments(
                con, [identifier for identifier, _uti in refs]
            )
            for identifier, _uti in refs:
                alt, token_id = inline.get(identifier, (None, None))
                kind = _token_kind(alt, token_id)
                if kind not in ("hashtag", "mention") or not alt:
                    continue
                key = (kind, alt.strip())
                entry = found.setdefault(
                    key, {"count": 0, "notes": []}
                )
                entry["count"] += 1
                if row["Z_PK"] not in entry["notes"]:
                    entry["notes"].append(row["Z_PK"])
        tags = [
            Tag(text=text, kind=kind, count=v["count"], note_ids=v["notes"])
            for (kind, text), v in found.items()
        ]
        tags.sort(key=lambda t: (t.kind, -t.count, t.text.lower()))
        return tags

    def notes_with_token(self, text: str) -> set[int]:
        """Note ids carrying a given hashtag or mention (case-insensitive)."""
        wanted = text.strip().lower()
        if not wanted:
            return set()
        # Accept "remodel" for "#remodel" so callers need not guess the sigil.
        candidates = {wanted, f"#{wanted}", f"@{wanted}"}
        return {
            note_id
            for tag in self.list_tags(include_trashed=True)
            if tag.text.lower() in candidates
            for note_id in tag.note_ids
        }

    # ------------------------------------------------------------------
    # Attachment internals
    # ------------------------------------------------------------------

    def _attachment_columns(self) -> str:
        s = self._sch()
        cols = ["a.Z_PK"]
        cols.append(f"a.{s['att_note']} AS note_pk" if s.get("att_note") else "NULL AS note_pk")
        cols.append(f"a.{s['att_media']} AS media_pk" if s.get("att_media") else "NULL AS media_pk")
        cols.append(f"a.{s['att_title']} AS title" if s.get("att_title") else "NULL AS title")
        cols.append(f"a.{s['att_uti']} AS uti" if s.get("att_uti") else "NULL AS uti")
        cols.append(f"a.{s['att_url']} AS url" if s.get("att_url") else "NULL AS url")
        cols.append(f"a.{s['att_created']} AS created" if s.get("att_created") else "NULL AS created")
        return ", ".join(cols)

    def _media_map(
        self, con: sqlite3.Connection, media_pks: list[Any]
    ) -> dict[int, dict[str, Any]]:
        """Media Z_PK -> {identifier, filename} for the given rows."""
        s = self._sch()
        pks = [pk for pk in media_pks if pk is not None]
        if not pks or not s.get("ent_media"):
            return {}
        out: dict[int, dict[str, Any]] = {}
        ident = s.get("att_identifier") or "ZIDENTIFIER"
        fname = s.get("att_filename") or "ZFILENAME"
        for i in range(0, len(pks), 500):
            chunk = pks[i:i + 500]
            for row in con.execute(
                f"SELECT Z_PK, {ident} AS ident, {fname} AS fname "
                f"FROM ZICCLOUDSYNCINGOBJECT WHERE Z_ENT=? AND Z_PK IN ("
                + ",".join("?" * len(chunk)) + ")",
                (s["ent_media"], *chunk),
            ):
                out[row["Z_PK"]] = {"identifier": row["ident"], "filename": row["fname"]}
        return out

    def _row_to_attachment(
        self, row: sqlite3.Row, note_id: int, media: dict[int, dict[str, Any]]
    ) -> Attachment:
        uti = row["uti"]
        info = media.get(row["media_pk"]) if row["media_pk"] is not None else None
        path = self._media_path(info) if info else None
        name = row["title"] or (info or {}).get("filename") or "Untitled"
        return Attachment(
            id=row["Z_PK"],
            note_id=note_id or 0,
            name=name,
            type_uti=uti,
            kind=_uti_kind(uti),
            url=row["url"],
            file_path=str(path) if path else None,
            size_bytes=path.stat().st_size if path else None,
            has_local_file=path is not None,
            created=_cd_date(row["created"]),
        )

    def _media_path(self, info: dict[str, Any]) -> Optional[Path]:
        """Resolve a media row to its file.

        Layout: Accounts/<account>/Media/<media-identifier>/<generation>/<file>
        The generation directory changes when an attachment is edited, so
        it is globbed rather than assumed.
        """
        ident, fname = info.get("identifier"), info.get("filename")
        if not ident or not fname:
            return None
        for candidate in _MEDIA_ROOT.glob(f"*/Media/{ident}/*/{fname}"):
            if candidate.is_file():
                return candidate
        return None

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _note_select_columns(self) -> str:
        s = self._sch()
        cols = [
            "n.Z_PK", "n.ZIDENTIFIER", "n.ZFOLDER", "n.ZNOTEDATA",
            f"n.{s['note_title']} AS title",
        ]
        cols.append(
            f"n.{s['snippet']} AS snippet" if s["snippet"] else "NULL AS snippet"
        )
        cols.append(
            f"n.{s['note_created']} AS created" if s["note_created"] else "NULL AS created"
        )
        cols.append(
            f"n.{s['note_modified']} AS modified" if s["note_modified"] else "NULL AS modified"
        )
        cols.append(f"n.{s['pinned']} AS pinned" if s["pinned"] else "0 AS pinned")
        cols.append(f"n.{s['protected']} AS prot" if s["protected"] else "0 AS prot")
        return ", ".join(cols)

    def _row_to_summary(
        self, row: sqlite3.Row, folders: dict[int, dict[str, Any]]
    ) -> NoteSummary:
        f = folders.get(row["ZFOLDER"], {"name": "Unknown", "account": "Unknown", "is_trash": False})
        identifier = row["ZIDENTIFIER"] or ""
        return NoteSummary(
            id=row["Z_PK"],
            identifier=identifier,
            folder=f["name"],
            account=f["account"],
            title=row["title"] or "Untitled",
            snippet=row["snippet"],
            created=_cd_date(row["created"]),
            modified=_cd_date(row["modified"]),
            is_pinned=bool(row["pinned"]),
            is_password_protected=bool(row["prot"]),
            is_trashed=f["is_trash"],
            notes_link=(
                f"applenotes://showNote?identifier={identifier}" if identifier else None
            ),
        )

    def _filter_by_body(
        self, con: sqlite3.Connection, rows: list[sqlite3.Row], q: str
    ) -> list[sqlite3.Row]:
        """Return the subset of rows whose decoded body contains q."""
        hits = []
        data_pks = [r["ZNOTEDATA"] for r in rows if r["ZNOTEDATA"] is not None]
        blobs: dict[int, bytes] = {}
        for i in range(0, len(data_pks), 500):
            chunk = data_pks[i:i + 500]
            for brow in con.execute(
                "SELECT Z_PK, ZDATA FROM ZICNOTEDATA WHERE Z_PK IN ("
                + ",".join("?" * len(chunk)) + ")",
                chunk,
            ):
                if brow["ZDATA"]:
                    blobs[brow["Z_PK"]] = brow["ZDATA"]
        for row in rows:
            blob = blobs.get(row["ZNOTEDATA"])
            if not blob:
                continue
            text = extract_note_text(blob)
            if text and q in text.lower():
                hits.append(row)
        return hits
