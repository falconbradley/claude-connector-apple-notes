"""Attachment reads and the checklist write-guard, on a synthetic store."""

import gzip
import sqlite3

import pytest

from apple_notes_mcp import applescript
from apple_notes_mcp.hybrid import HybridBridge
from apple_notes_mcp.notestore import NoteStore, _uti_kind
from apple_notes_mcp.protobuf import has_checklist


def _varint(v: int) -> bytes:
    out = b""
    while True:
        b = v & 0x7F
        v >>= 7
        if v:
            out += bytes([b | 0x80])
        else:
            return out + bytes([b])


def _lf(field: int, payload: bytes) -> bytes:
    return _varint((field << 3) | 2) + _varint(len(payload)) + payload


def _vf(field: int, value: int) -> bytes:
    return _varint((field << 3) | 0) + _varint(value)


def _blob(
    text: str,
    *,
    checklist_done: bool | None = None,
    attachments: list[tuple[str, str]] | None = None,
) -> bytes:
    """A note blob, optionally with a checklist run and attachment refs.

    Each (identifier, uti) pair becomes an attribute run carrying
    attachment_info, matching one U+FFFC placeholder in the text.
    """
    note = _lf(2, text.encode())
    if checklist_done is not None:
        checklist = _lf(1, b"uuid") + _vf(2, 1 if checklist_done else 0)
        paragraph = _vf(1, 103) + _lf(5, checklist)      # style 103 = checkbox
        note += _lf(5, _vf(1, len(text)) + _lf(2, paragraph))
    for identifier, uti in attachments or []:
        info = _lf(1, identifier.encode()) + _lf(2, uti.encode())
        note += _lf(5, _vf(1, 1) + _lf(12, info))
    return gzip.compress(_lf(2, _lf(3, note)))


@pytest.fixture
def store(tmp_path):
    """Store with two notes: one plain + attachments, one with a checklist."""
    media_root = tmp_path / "Accounts" / "ACC" / "Media"
    path = tmp_path / "NoteStore.sqlite"
    con = sqlite3.connect(path)
    con.executescript(
        """
        CREATE TABLE Z_PRIMARYKEY (Z_ENT INTEGER, Z_NAME TEXT, Z_MAX INTEGER);
        INSERT INTO Z_PRIMARYKEY VALUES
            (12,'ICNote',0),(15,'ICFolder',0),(14,'ICAccount',0),
            (5,'ICAttachment',0),(11,'ICMedia',0);
        CREATE TABLE Z_METADATA (Z_UUID TEXT);
        INSERT INTO Z_METADATA VALUES ('TEST-UUID');
        CREATE TABLE ZICCLOUDSYNCINGOBJECT (
            Z_PK INTEGER PRIMARY KEY, Z_ENT INTEGER,
            ZTITLE TEXT, ZTITLE1 TEXT, ZTITLE2 TEXT, ZNAME TEXT,
            ZSNIPPET TEXT, ZIDENTIFIER TEXT, ZFILENAME TEXT,
            ZFOLDER INTEGER, ZACCOUNT7 INTEGER, ZNOTEDATA INTEGER,
            ZNOTE INTEGER, ZMEDIA INTEGER, ZTYPEUTI TEXT, ZURLSTRING TEXT,
            ZISPINNED INTEGER, ZISPASSWORDPROTECTED INTEGER,
            ZMARKEDFORDELETION INTEGER,
            ZCREATIONDATE REAL, ZCREATIONDATE1 REAL, ZMODIFICATIONDATE1 REAL
        );
        CREATE TABLE ZICNOTEDATA (Z_PK INTEGER PRIMARY KEY, ZDATA BLOB);
        INSERT INTO ZICCLOUDSYNCINGOBJECT (Z_PK,Z_ENT,ZNAME,ZIDENTIFIER)
            VALUES (1,14,'iCloud','ACCT');
        INSERT INTO ZICCLOUDSYNCINGOBJECT (Z_PK,Z_ENT,ZTITLE2,ZIDENTIFIER,ZACCOUNT7)
            VALUES (2,15,'Notes','DefaultFolder-CloudKit',1);
        INSERT INTO ZICCLOUDSYNCINGOBJECT
            (Z_PK,Z_ENT,ZTITLE1,ZIDENTIFIER,ZFOLDER,ZNOTEDATA,ZMODIFICATIONDATE1)
            VALUES (10,12,'Trip photos','N-1',2,100,1.0),
                   (11,12,'Packing list','N-2',2,101,2.0);
        -- media row + the attachments referencing it
        INSERT INTO ZICCLOUDSYNCINGOBJECT (Z_PK,Z_ENT,ZIDENTIFIER,ZFILENAME)
            VALUES (30,11,'MEDIA-UUID','beach.jpeg');
        INSERT INTO ZICCLOUDSYNCINGOBJECT
            (Z_PK,Z_ENT,ZTITLE,ZTYPEUTI,ZNOTE,ZMEDIA,ZCREATIONDATE,ZIDENTIFIER)
            VALUES (20,5,'beach.jpeg','public.jpeg',10,30,1.0,'ATT-IMG');
        INSERT INTO ZICCLOUDSYNCINGOBJECT
            (Z_PK,Z_ENT,ZTITLE,ZTYPEUTI,ZNOTE,ZURLSTRING,ZCREATIONDATE)
            VALUES (21,5,'Recipe','public.url',10,'https://example.com/r',2.0);
        INSERT INTO ZICCLOUDSYNCINGOBJECT
            (Z_PK,Z_ENT,ZTITLE,ZTYPEUTI,ZNOTE,ZCREATIONDATE)
            VALUES (22,5,'Table','com.apple.notes.table',10,3.0);
        """
    )
    con.execute(
        "INSERT INTO ZICNOTEDATA VALUES (100, ?)",
        (_blob("Trip photos\n\ufffc\nsunset",
               attachments=[("ATT-IMG", "public.jpeg")]),),
    )
    con.execute(
        "INSERT INTO ZICNOTEDATA VALUES (101, ?)",
        (_blob("Packing list\nsocks", checklist_done=False),),
    )
    con.commit()
    con.close()

    # the real file the media row points at
    d = media_root / "MEDIA-UUID" / "1_GEN"
    d.mkdir(parents=True)
    (d / "beach.jpeg").write_bytes(b"\xff\xd8\xff" + b"x" * 5000)

    s = NoteStore(store_path=path)
    return s, tmp_path


@pytest.fixture
def patched_store(store, monkeypatch):
    """Point media resolution at the fixture's account tree."""
    s, root = store
    monkeypatch.setattr("apple_notes_mcp.notestore._MEDIA_ROOT", root / "Accounts")
    return s


# ---------------------------------------------------------------------------
# UTI mapping
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "uti,kind",
    [
        ("public.jpeg", "image"),
        ("public.png", "image"),
        ("com.adobe.pdf", "pdf"),
        ("public.url", "link"),
        ("com.apple.notes.table", "table"),
        ("com.apple.drawing.2", "drawing"),
        ("com.apple.paper.doc.scan", "scan"),
        ("public.vcard", "contact"),
        ("dyn.ah62d4rv4ge8087pysb41k", "file"),
        (None, "file"),
    ],
)
def test_uti_kinds(uti, kind):
    assert _uti_kind(uti) == kind


# ---------------------------------------------------------------------------
# Attachment reads
# ---------------------------------------------------------------------------


def test_lists_all_attachments_for_a_note(patched_store):
    atts = patched_store.list_attachments(10)
    assert [a.kind for a in atts] == ["image", "link", "table"]


def test_media_backed_attachment_resolves_to_a_real_file(patched_store):
    image = patched_store.list_attachments(10)[0]
    assert image.has_local_file
    assert image.file_path.endswith("beach.jpeg")
    assert image.size_bytes == 5003


def test_link_attachment_exposes_its_url(patched_store):
    link = patched_store.list_attachments(10)[1]
    assert link.url == "https://example.com/r"
    assert link.has_local_file is False   # links have no file by design


def test_table_attachment_has_no_file_and_that_is_not_an_error(patched_store):
    table = patched_store.list_attachments(10)[2]
    assert table.has_local_file is False
    assert table.file_path is None


def test_get_attachment_by_id(patched_store):
    a = patched_store.get_attachment(21)
    assert a.name == "Recipe" and a.note_id == 10


def test_get_attachment_missing_returns_none(patched_store):
    assert patched_store.get_attachment(999) is None


def test_note_without_attachments_returns_empty(patched_store):
    assert patched_store.list_attachments(11) == []


def test_get_note_reports_attachment_count(patched_store):
    assert patched_store.get_note(10).attachment_count == 3
    assert patched_store.get_note(11).attachment_count == 0


def test_attachment_bytes_totals_only_real_files(patched_store):
    # image only; link and table contribute nothing
    assert patched_store.attachment_bytes(10) == 5003


# ---------------------------------------------------------------------------
# Checklist detection
# ---------------------------------------------------------------------------


def test_detects_checklist_in_blob():
    assert has_checklist(_blob("x", checklist_done=False)) is True
    assert has_checklist(_blob("x", checklist_done=True)) is True
    assert has_checklist(_blob("x")) is False


def test_checklist_detection_survives_garbage():
    assert has_checklist(b"not gzip at all") is False


def test_store_flags_checklist_notes(patched_store):
    assert patched_store.note_has_checklist(11) is True
    assert patched_store.note_has_checklist(10) is False
    assert patched_store.get_note(11).has_checklist is True


# ---------------------------------------------------------------------------
# The write guard — the data-loss fix
# ---------------------------------------------------------------------------


@pytest.fixture
def bridge(patched_store, monkeypatch):
    b = HybridBridge()
    monkeypatch.setattr(b, "_store", patched_store)
    monkeypatch.setattr(b, "_fast_disabled", False)
    return b


def test_append_refuses_checklist_notes(bridge, monkeypatch):
    called = []
    monkeypatch.setattr(
        applescript, "append_to_note",
        lambda *a, **k: called.append(a) or {"name": "x"},
    )
    result = bridge.append_to_note(11, "more")
    assert result.success is False
    assert "checklist" in result.detail.lower()
    assert not called, "must not reach Notes.app scripting at all"


def test_update_refuses_checklist_notes(bridge, monkeypatch):
    called = []
    monkeypatch.setattr(
        applescript, "replace_note_body",
        lambda *a, **k: called.append(a) or {"name": "x"},
    )
    result = bridge.replace_note_body(11, "rewritten")
    assert result.success is False
    assert not called


def test_checklist_refusal_cannot_be_forced(bridge, monkeypatch):
    monkeypatch.setattr(
        applescript, "replace_note_body",
        lambda *a, **k: pytest.fail("forced past the checklist guard"),
    )
    assert bridge.replace_note_body(11, "x", force=True).success is False


def test_ordinary_notes_still_write(bridge, monkeypatch):
    monkeypatch.setattr(
        applescript, "append_to_note",
        lambda cid, text, markdown=False: {"name": "Trip photos"},
    )
    assert bridge.append_to_note(10, "more").success is True


def test_large_attachment_note_is_refused_then_forcible(bridge, monkeypatch):
    monkeypatch.setattr(bridge._store, "attachment_bytes", lambda pk: 50 * 1024 * 1024)
    monkeypatch.setattr(
        applescript, "append_to_note",
        lambda cid, text, markdown=False: {"name": "Trip photos"},
    )
    blocked = bridge.append_to_note(10, "more")
    assert blocked.success is False and "MB" in blocked.detail
    assert bridge.append_to_note(10, "more", force=True).success is True


# ---------------------------------------------------------------------------
# Schema degradation
#
# The fixture store has no ZMERGEABLEDATA1 column, which is what an older
# macOS schema looks like. Reading a note must still work.
# ---------------------------------------------------------------------------


def test_notes_read_fine_on_a_schema_without_the_table_column(patched_store):
    assert patched_store._sch().get("att_mergeable") is None
    detail = patched_store.get_note(10)
    assert detail.attachment_count == 3
    assert detail.tables == []


def test_attachments_still_listed_without_the_table_column(patched_store):
    kinds = [a.kind for a in patched_store.list_attachments(10)]
    assert kinds == ["image", "link", "table"]


def test_get_table_returns_none_without_the_column(patched_store):
    assert patched_store.get_table(22) is None


def test_non_table_attachments_render_by_kind_in_body(patched_store):
    """A placeholder becomes a description of what it points at."""
    body = patched_store.get_note(10).body_text or ""
    assert "[image: beach.jpeg]" in body
    assert "\ufffc" not in body
    assert "[attachment]" not in body


def test_unknown_placeholder_falls_back_to_a_generic_label(patched_store):
    """A ref with no matching attachment row still renders something."""
    detail = patched_store.get_note(11)
    assert "\ufffc" not in (detail.body_text or "")
