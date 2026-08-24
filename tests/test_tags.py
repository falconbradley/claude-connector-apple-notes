"""Hashtags and mentions.

Notes stores these as inline attachments with no link back to their
note, so they are associated through the identifiers in each note's
attribute runs — the same mechanism that places table placeholders.
"""

import gzip
import sqlite3

import pytest

from apple_notes_mcp.notestore import NoteStore, _token_kind


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


def _blob(text: str, refs: list[tuple[str, str]]) -> bytes:
    note = _lf(2, text.encode())
    for identifier, uti in refs:
        note += _lf(5, _vf(1, 1) + _lf(12, _lf(1, identifier.encode()) + _lf(2, uti.encode())))
    return gzip.compress(_lf(2, _lf(3, note)))


@pytest.fixture
def store(tmp_path):
    path = tmp_path / "NoteStore.sqlite"
    con = sqlite3.connect(path)
    con.executescript(
        """
        CREATE TABLE Z_PRIMARYKEY (Z_ENT INTEGER, Z_NAME TEXT, Z_MAX INTEGER);
        INSERT INTO Z_PRIMARYKEY VALUES
            (12,'ICNote',0),(15,'ICFolder',0),(14,'ICAccount',0),
            (5,'ICAttachment',0),(11,'ICMedia',0),(9,'ICInlineAttachment',0);
        CREATE TABLE Z_METADATA (Z_UUID TEXT);
        INSERT INTO Z_METADATA VALUES ('T');
        CREATE TABLE ZICCLOUDSYNCINGOBJECT (
            Z_PK INTEGER PRIMARY KEY, Z_ENT INTEGER,
            ZTITLE TEXT, ZTITLE1 TEXT, ZTITLE2 TEXT, ZNAME TEXT,
            ZSNIPPET TEXT, ZIDENTIFIER TEXT, ZFILENAME TEXT,
            ZFOLDER INTEGER, ZACCOUNT7 INTEGER, ZNOTEDATA INTEGER,
            ZNOTE INTEGER, ZMEDIA INTEGER, ZTYPEUTI TEXT, ZURLSTRING TEXT,
            ZALTTEXT TEXT, ZTOKENCONTENTIDENTIFIER TEXT,
            ZISPINNED INTEGER, ZISPASSWORDPROTECTED INTEGER,
            ZMARKEDFORDELETION INTEGER,
            ZCREATIONDATE REAL, ZCREATIONDATE1 REAL, ZMODIFICATIONDATE1 REAL
        );
        CREATE TABLE ZICNOTEDATA (Z_PK INTEGER PRIMARY KEY, ZDATA BLOB);
        INSERT INTO ZICCLOUDSYNCINGOBJECT (Z_PK,Z_ENT,ZNAME,ZIDENTIFIER) VALUES (1,14,'iCloud','A');
        INSERT INTO ZICCLOUDSYNCINGOBJECT (Z_PK,Z_ENT,ZTITLE2,ZIDENTIFIER,ZACCOUNT7)
            VALUES (2,15,'Notes','DefaultFolder-CloudKit',1),
                   (3,15,'Recently Deleted','TrashFolder-CloudKit',1);
        INSERT INTO ZICCLOUDSYNCINGOBJECT
            (Z_PK,Z_ENT,ZTITLE1,ZIDENTIFIER,ZFOLDER,ZNOTEDATA,ZMODIFICATIONDATE1)
            VALUES (10,12,'Remodel','N1',2,100,3.0),
                   (11,12,'Planning','N2',2,101,2.0),
                   (12,12,'Old','N3',3,102,1.0);
        -- inline attachments: two hashtags, one mention, one divider
        INSERT INTO ZICCLOUDSYNCINGOBJECT (Z_PK,Z_ENT,ZIDENTIFIER,ZALTTEXT,ZTOKENCONTENTIDENTIFIER)
            VALUES (20,9,'T-A','#remodel','_hash1'),
                   (21,9,'T-B','@Brad','_hash2'),
                   (22,9,'T-C','#remodel','_hash1'),
                   (23,9,'T-D','---','com.apple.notes.inlinetextattachment.dividerline'),
                   (24,9,'T-E','#budget','_hash3');
        """
    )
    TOK = "com.apple.notes.inlinetextattachment"
    con.execute("INSERT INTO ZICNOTEDATA VALUES (100, ?)",
                (_blob("Remodel\n￼\n￼\n￼", [("T-A", TOK), ("T-B", TOK), ("T-D", TOK)]),))
    con.execute("INSERT INTO ZICNOTEDATA VALUES (101, ?)",
                (_blob("Planning\n￼\n￼", [("T-C", TOK), ("T-E", TOK)]),))
    con.execute("INSERT INTO ZICNOTEDATA VALUES (102, ?)",
                (_blob("Old\n￼", [("T-A", TOK)]),))
    con.commit(); con.close()
    return NoteStore(store_path=path)


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "alt,token,kind",
    [
        ("#remodel", "_x", "hashtag"),
        ("@Brad", "_x", "mention"),
        ("---", "com.apple.notes.inlinetextattachment.dividerline", "divider"),
        ("= 5", "ICInlineAttachmentCalculateStateValidLTR", "calculation"),
        (None, None, "other"),
        ("plain", None, "other"),
    ],
)
def test_token_classification(alt, token, kind):
    assert _token_kind(alt, token) == kind


def test_classification_prefers_text_over_token_id():
    """Two people can share a token id, so it identifies nothing."""
    assert _token_kind("@Dad", "_shared") == "mention"
    assert _token_kind("@John", "_shared") == "mention"


# ---------------------------------------------------------------------------
# Per-note extraction
# ---------------------------------------------------------------------------


def test_note_reports_its_hashtags_and_mentions(store):
    detail = store.get_note(10)
    assert detail.hashtags == ["#remodel"]
    assert detail.mentions == ["@Brad"]


def test_dividers_are_not_tags(store):
    assert "---" not in store.get_note(10).hashtags
    assert "---" not in store.get_note(10).mentions


def test_trashed_note_still_parses_its_tags(store):
    """get_note does not filter by folder; only listing/search do."""
    assert store.get_note(12).hashtags == ["#remodel"]


def test_tags_are_deduplicated_within_a_note(store):
    """A tag used twice in one note is listed once."""
    detail = store.get_note(11)
    assert detail.hashtags == ["#remodel", "#budget"]


# ---------------------------------------------------------------------------
# Store-wide listing
# ---------------------------------------------------------------------------


def test_list_tags_counts_across_notes(store):
    tags = {t.text: t for t in store.list_tags()}
    assert tags["#remodel"].count == 2           # notes 10 and 11
    assert sorted(tags["#remodel"].note_ids) == [10, 11]
    assert tags["@Brad"].kind == "mention"


def test_list_tags_excludes_trash_by_default(store):
    assert 12 not in {n for t in store.list_tags() for n in t.note_ids}


def test_list_tags_can_include_trash(store):
    assert 12 in {n for t in store.list_tags(include_trashed=True) for n in t.note_ids}


def test_list_tags_sorted_hashtags_before_mentions(store):
    kinds = [t.kind for t in store.list_tags()]
    assert kinds == sorted(kinds)


# ---------------------------------------------------------------------------
# Search filter
# ---------------------------------------------------------------------------


def test_search_by_hashtag(store):
    result = store.search_notes(tag="#remodel")
    assert sorted(n.id for n in result.notes) == [10, 11]


def test_search_by_mention(store):
    assert [n.id for n in store.search_notes(tag="@Brad").notes] == [10]


def test_sigil_is_optional(store):
    assert store.search_notes(tag="remodel").total == 2
    assert store.search_notes(tag="brad").total == 1


def test_tag_matching_is_case_insensitive(store):
    assert store.search_notes(tag="#REMODEL").total == 2


def test_unknown_tag_returns_nothing(store):
    assert store.search_notes(tag="#nope").total == 0


def test_tag_combines_with_other_filters(store):
    assert store.search_notes(tag="#remodel", folder="Notes").total == 2
    assert store.search_notes(tag="#remodel", folder="Nonexistent").total == 0
