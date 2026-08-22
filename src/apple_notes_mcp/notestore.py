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

from .models import Folder, NoteDetail, NotesStats, NoteSummary, SearchResult
from .protobuf import extract_note_text

logger = logging.getLogger("apple_notes_mcp.notestore")

_STORE_PATH = (
    Path.home() / "Library" / "Group Containers" / "group.com.apple.notes"
    / "NoteStore.sqlite"
)

_CORE_DATA_EPOCH = datetime(2001, 1, 1, tzinfo=timezone.utc)

_TRASH_FOLDER_IDENTIFIERS = {"TrashFolder-CloudKit", "TrashFolder-Local"}


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
        limit: int = 25,
        offset: int = 0,
    ) -> SearchResult:
        con = self._connect()
        s = self._sch()
        folders = self._folder_map(con)

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
            detail.body_text = extract_note_text(blob_row["ZDATA"])
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
