"""Tests for NoteStore against a synthetic fixture database."""

import gzip
import sqlite3
from datetime import datetime, timezone

import pytest

from apple_notes_mcp.notestore import NoteStore, NoteStoreError, _cd_date


def _varint(value: int) -> bytes:
    out = b""
    while True:
        b = value & 0x7F
        value >>= 7
        if value:
            out += bytes([b | 0x80])
        else:
            return out + bytes([b])


def _length_field(field_number: int, payload: bytes) -> bytes:
    return _varint((field_number << 3) | 2) + _varint(len(payload)) + payload


def _note_blob(text: str) -> bytes:
    note = _length_field(2, text.encode("utf-8"))
    document = _length_field(3, note)
    return gzip.compress(_length_field(2, document))


CD = 700000000.0  # an arbitrary Core Data timestamp


@pytest.fixture
def store(tmp_path):
    """A minimal NoteStore.sqlite mimicking the real schema."""
    path = tmp_path / "NoteStore.sqlite"
    con = sqlite3.connect(path)
    con.executescript(
        """
        CREATE TABLE Z_PRIMARYKEY (Z_ENT INTEGER, Z_NAME TEXT, Z_MAX INTEGER);
        INSERT INTO Z_PRIMARYKEY VALUES
            (12, 'ICNote', 0), (15, 'ICFolder', 0), (14, 'ICAccount', 0);
        CREATE TABLE Z_METADATA (Z_UUID TEXT);
        INSERT INTO Z_METADATA VALUES ('TEST-UUID');
        CREATE TABLE ZICCLOUDSYNCINGOBJECT (
            Z_PK INTEGER PRIMARY KEY, Z_ENT INTEGER,
            ZTITLE1 TEXT, ZTITLE2 TEXT, ZNAME TEXT,
            ZSNIPPET TEXT, ZIDENTIFIER TEXT,
            ZFOLDER INTEGER, ZACCOUNT7 INTEGER, ZNOTEDATA INTEGER,
            ZISPINNED INTEGER, ZISPASSWORDPROTECTED INTEGER,
            ZMARKEDFORDELETION INTEGER,
            ZCREATIONDATE1 REAL, ZMODIFICATIONDATE1 REAL
        );
        CREATE TABLE ZICNOTEDATA (Z_PK INTEGER PRIMARY KEY, ZDATA BLOB);
        """
    )
    # account
    con.execute(
        "INSERT INTO ZICCLOUDSYNCINGOBJECT (Z_PK, Z_ENT, ZNAME, ZIDENTIFIER)"
        " VALUES (1, 14, 'iCloud', 'ACCT-UUID')"
    )
    # folders: default + trash
    con.execute(
        "INSERT INTO ZICCLOUDSYNCINGOBJECT (Z_PK, Z_ENT, ZTITLE2, ZIDENTIFIER, ZACCOUNT7)"
        " VALUES (2, 15, 'Notes', 'DefaultFolder-CloudKit', 1)"
    )
    con.execute(
        "INSERT INTO ZICCLOUDSYNCINGOBJECT (Z_PK, Z_ENT, ZTITLE2, ZIDENTIFIER, ZACCOUNT7)"
        " VALUES (3, 15, 'Recently Deleted', 'TrashFolder-CloudKit', 1)"
    )
    # notes
    notes = [
        (10, "Groceries", "milk eggs", "N-1", 2, 100, 0, 0, 0, CD + 30),
        (11, "Meeting notes", "agenda", "N-2", 2, 101, 1, 0, 0, CD + 20),
        (12, "Old junk", "trashed", "N-3", 3, 102, 0, 0, 0, CD + 10),
        (13, "Secret", None, "N-4", 2, None, 0, 1, 0, CD + 5),
    ]
    for pk, title, snippet, ident, folder, data_pk, pinned, prot, deleted, mod in notes:
        con.execute(
            "INSERT INTO ZICCLOUDSYNCINGOBJECT (Z_PK, Z_ENT, ZTITLE1, ZSNIPPET,"
            " ZIDENTIFIER, ZFOLDER, ZNOTEDATA, ZISPINNED, ZISPASSWORDPROTECTED,"
            " ZMARKEDFORDELETION, ZMODIFICATIONDATE1)"
            " VALUES (?, 12, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (pk, title, snippet, ident, folder, data_pk, pinned, prot, deleted, mod),
        )
    con.execute(
        "INSERT INTO ZICNOTEDATA VALUES (100, ?)",
        (_note_blob("Groceries\nmilk\neggs\nbread"),),
    )
    con.execute(
        "INSERT INTO ZICNOTEDATA VALUES (101, ?)",
        (_note_blob("Meeting notes\nagenda item one"),),
    )
    con.execute("INSERT INTO ZICNOTEDATA VALUES (102, NULL)")
    con.commit()
    con.close()
    return NoteStore(store_path=path)


def test_missing_store_raises(tmp_path):
    s = NoteStore(store_path=tmp_path / "nope.sqlite")
    assert not s.available()
    with pytest.raises(NoteStoreError):
        s.stats()


def test_stats(store):
    stats = store.stats()
    assert stats.total_notes == 3          # trashed note excluded
    assert stats.trashed_notes == 1
    assert stats.pinned_notes == 1
    assert stats.password_protected_notes == 1
    assert stats.folder_count == 1         # trash folder excluded
    assert stats.account_count == 1


def test_list_folders(store):
    folders = store.list_folders()
    names = {f.name: f for f in folders}
    assert names["Notes"].note_count == 3
    assert names["Notes"].account == "iCloud"
    assert names["Recently Deleted"].is_trash


def test_search_excludes_trash_by_default(store):
    result = store.search_notes()
    ids = [n.id for n in result.notes]
    assert 12 not in ids
    assert result.total == 3
    assert result.engine == "sqlite"
    # newest first
    assert ids[0] == 10


def test_search_include_trashed(store):
    result = store.search_notes(include_trashed=True)
    assert result.total == 4


def test_search_by_title(store):
    result = store.search_notes(query="grocer")
    assert result.total == 1
    assert result.notes[0].id == 10


def test_search_by_body(store):
    result = store.search_notes(query="bread")
    assert result.total == 1
    assert result.notes[0].id == 10


def test_search_body_disabled(store):
    result = store.search_notes(query="bread", search_bodies=False)
    assert result.total == 0


def test_search_pinned_only(store):
    result = store.search_notes(pinned_only=True)
    assert [n.id for n in result.notes] == [11]


def test_search_by_folder(store):
    assert store.search_notes(folder="Notes").total == 3
    assert store.search_notes(folder="iCloud/Notes").total == 3
    assert store.search_notes(folder="Nonexistent").total == 0


def test_search_since(store):
    since = _cd_date(CD + 15)
    result = store.search_notes(since=since)
    assert {n.id for n in result.notes} == {10, 11}


def test_pagination(store):
    page1 = store.search_notes(limit=2, offset=0)
    page2 = store.search_notes(limit=2, offset=2)
    assert page1.total == page2.total == 3
    assert len(page1.notes) == 2
    assert len(page2.notes) == 1


def test_get_note_body(store):
    detail = store.get_note(10)
    assert "bread" in detail.body_text
    assert detail.title == "Groceries"
    assert detail.folder == "Notes"
    assert detail.notes_link == "applenotes://showNote?identifier=N-1"


def test_get_note_password_protected(store):
    detail = store.get_note(13)
    assert detail.body_text is None
    assert "password" in detail.body_unavailable_reason


def test_get_note_missing(store):
    assert store.get_note(999) is None


def test_coredata_id(store):
    assert store.coredata_id(10) == "x-coredata://TEST-UUID/ICNote/p10"


def test_resolve_note(store):
    info = store.resolve_note(10)
    assert info["identifier"] == "N-1"
    assert info["coredata_id"].endswith("/ICNote/p10")


def test_pk_from_identifier(store):
    assert store.pk_from_identifier("N-2") == 11
    assert store.pk_from_identifier("nope") is None


def test_cd_date():
    assert _cd_date(None) is None
    dt = _cd_date(0)
    assert dt == datetime(2001, 1, 1, tzinfo=timezone.utc)
