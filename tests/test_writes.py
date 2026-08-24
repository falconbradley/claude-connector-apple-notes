"""Tests for the new write paths and selection, with JXA mocked out.

The JXA bridge itself needs a live Notes.app, so these cover the layer
we own: id resolution, HTML generation, and result shaping.
"""

import pytest

from apple_notes_mcp import applescript
from apple_notes_mcp.applescript import text_to_note_html
from apple_notes_mcp.hybrid import HybridBridge


@pytest.fixture
def bridge(monkeypatch):
    """A bridge with the fast path disabled and JXA calls recorded."""
    monkeypatch.setenv("APPLE_NOTES_MCP_DISABLE_FAST", "1")
    b = HybridBridge()
    monkeypatch.setattr(
        b, "_coredata_id_guess", lambda pk: f"x-coredata://U/ICNote/p{pk}"
    )
    return b


# ---------------------------------------------------------------------------
# HTML generation
# ---------------------------------------------------------------------------


def test_html_escapes_user_text():
    out = text_to_note_html("A & B", "<script>alert(1)</script>")
    assert "&amp;" in out
    assert "<script>" not in out
    assert "&lt;script&gt;" in out


def test_html_blank_lines_become_breaks():
    assert "<div><br></div>" in text_to_note_html(None, "a\n\nb")


def test_html_title_omitted_when_none():
    assert "<h1>" not in text_to_note_html(None, "body")


# ---------------------------------------------------------------------------
# update_note / replace_note_body
# ---------------------------------------------------------------------------


def test_replace_body_sends_full_html(bridge, monkeypatch):
    seen = {}

    def fake(coredata_id, body_text, title=None):
        seen.update(id=coredata_id, body=body_text, title=title)
        return {"id": coredata_id, "name": "Renamed"}

    monkeypatch.setattr(applescript, "replace_note_body", fake)
    result = bridge.replace_note_body(42, "new body", title="Renamed")

    assert result.success and result.id == 42
    assert result.title == "Renamed"
    assert seen["id"].endswith("/ICNote/p42")
    assert seen["title"] == "Renamed"


def test_replace_body_unresolvable_id_fails_cleanly(bridge, monkeypatch):
    monkeypatch.setattr(bridge, "_coredata_id_guess", lambda pk: None)
    result = bridge.replace_note_body(999, "x")
    assert result.success is False
    assert "999" in result.detail


def test_replace_body_propagates_password_protection(bridge, monkeypatch):
    def fake(*a, **k):
        raise applescript.NotesScriptError("note is password-protected")

    monkeypatch.setattr(applescript, "replace_note_body", fake)
    with pytest.raises(applescript.NotesScriptError, match="password-protected"):
        bridge.replace_note_body(13, "x")


# ---------------------------------------------------------------------------
# move_note / create_folder
# ---------------------------------------------------------------------------


def test_move_note_reports_destination(bridge, monkeypatch):
    monkeypatch.setattr(
        applescript, "move_note",
        lambda cid, folder: {"id": cid, "name": "N", "folder": folder,
                             "account": "iCloud"},
    )
    result = bridge.move_note(7, "Recipes")
    assert result.success and result.folder == "Recipes"


def test_create_folder_builds_full_name(bridge, monkeypatch):
    monkeypatch.setattr(
        applescript, "create_folder",
        lambda name, account: {"name": name, "account": "iCloud"},
    )
    folder = bridge.create_folder("Recipes")
    assert folder.full_name == "iCloud/Recipes"
    assert folder.note_count == 0


def test_create_folder_duplicate_raises(bridge, monkeypatch):
    def fake(name, account):
        raise applescript.NotesScriptError("folder already exists: Recipes")

    monkeypatch.setattr(applescript, "create_folder", fake)
    with pytest.raises(applescript.NotesScriptError, match="already exists"):
        bridge.create_folder("Recipes")


# ---------------------------------------------------------------------------
# Selection
# ---------------------------------------------------------------------------


def test_selection_resolves_ids_through_the_read_path(bridge, monkeypatch):
    monkeypatch.setattr(
        applescript, "get_selection",
        lambda: ["x-coredata://U/ICNote/p10", "x-coredata://U/ICNote/p11"],
    )
    monkeypatch.setattr(
        applescript, "get_note_body",
        lambda cid: {"title": f"Note {cid[-3:]}", "plaintext": "body",
                     "password_protected": False},
    )
    result = bridge.selected_notes()
    assert result.count == 2
    assert result.returned == 2
    assert [n.id for n in result.notes] == [10, 11]


def test_selection_empty_when_nothing_selected(bridge, monkeypatch):
    monkeypatch.setattr(applescript, "get_selection", lambda: [])
    result = bridge.selected_notes()
    assert result.count == 0 and result.notes == []


def test_selection_reports_total_when_limited(bridge, monkeypatch):
    ids = [f"x-coredata://U/ICNote/p{i}" for i in range(10)]
    monkeypatch.setattr(applescript, "get_selection", lambda: ids)
    monkeypatch.setattr(
        applescript, "get_note_body",
        lambda cid: {"title": "T", "plaintext": "b", "password_protected": False},
    )
    result = bridge.selected_notes(limit=3)
    assert result.count == 10      # the truncation is visible...
    assert result.returned == 3    # ...not silent


def test_selection_ignores_unparseable_ids(bridge, monkeypatch):
    monkeypatch.setattr(
        applescript, "get_selection",
        lambda: ["not-a-coredata-id", "x-coredata://U/ICNote/p5"],
    )
    monkeypatch.setattr(
        applescript, "get_note_body",
        lambda cid: {"title": "T", "plaintext": "b", "password_protected": False},
    )
    result = bridge.selected_notes()
    assert result.count == 1
    assert result.notes[0].id == 5


# ---------------------------------------------------------------------------
# Title preservation
#
# Notes derives a note's title from the first line of its body, so
# replacing the body without re-emitting the title silently renames the
# note. Caught by the live smoke test; guarded here.
# ---------------------------------------------------------------------------


@pytest.fixture
def titled_bridge(bridge, monkeypatch):
    """A bridge whose note 42 is titled 'Keep Me'."""
    monkeypatch.setattr(
        applescript, "get_note_body",
        lambda cid: {"title": "Keep Me", "plaintext": "Keep Me\nold body",
                     "password_protected": False},
    )
    sent = {}
    monkeypatch.setattr(
        applescript, "replace_note_body",
        lambda cid, body, title=None: sent.update(title=title, body=body)
        or {"id": cid, "name": title or body.split("\n")[0]},
    )
    return bridge, sent


def test_omitting_title_preserves_the_existing_one(titled_bridge):
    bridge, sent = titled_bridge
    bridge.replace_note_body(42, "brand new body")
    assert sent["title"] == "Keep Me"


def test_explicit_title_wins(titled_bridge):
    bridge, sent = titled_bridge
    bridge.replace_note_body(42, "brand new body", title="New Name")
    assert sent["title"] == "New Name"


def test_title_not_duplicated_when_body_already_starts_with_it(titled_bridge):
    bridge, sent = titled_bridge
    bridge.replace_note_body(42, "Keep Me\nrest of the body")
    assert sent["title"] is None  # the body already carries the title line


def test_unresolvable_title_falls_back_to_notes_native_behaviour(
    bridge, monkeypatch
):
    monkeypatch.setattr(applescript, "get_note_body", lambda cid: None)
    sent = {}
    monkeypatch.setattr(
        applescript, "replace_note_body",
        lambda cid, body, title=None: sent.update(title=title) or {"name": "x"},
    )
    bridge.replace_note_body(42, "body")
    assert sent["title"] is None
