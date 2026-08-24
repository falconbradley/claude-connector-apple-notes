"""
JXA (JavaScript for Automation) bridge to Notes.app.

All writes go through here — Notes.app owns every mutation and syncs it
to iCloud itself, so this server never touches credentials. Reads are
also available as a fallback for when the fast NoteStore.sqlite path is
unavailable (no Full Disk Access).

Notes.app scripting notes
-------------------------
- A note's scripting `id` is a Core Data URI:
      x-coredata://<store-UUID>/ICNote/p<Z_PK>
  which lines up 1:1 with rows in NoteStore.sqlite, so ids from the fast
  path can be handed straight to this bridge.
- Note bodies are HTML in the scripting interface.
- Requires Notes.app running (we launch it if needed) and Automation
  permission for the host process (macOS prompts on first use).

Arguments are passed to JXA as a single JSON argv element and parsed
with JSON.parse inside the script — no string escaping games.
"""

from __future__ import annotations

import html
import json
import logging
import subprocess
from typing import Any, Optional

logger = logging.getLogger("apple_notes_mcp.applescript")

_OSASCRIPT_TIMEOUT = 120


class NotesScriptError(RuntimeError):
    pass


def _run_jxa(script: str, params: Optional[dict] = None) -> Any:
    """Run a JXA snippet; the snippet's `run(argv)` receives [json_params]."""
    cmd = ["osascript", "-l", "JavaScript", "-e", script]
    if params is not None:
        cmd.append(json.dumps(params))
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=_OSASCRIPT_TIMEOUT
        )
    except subprocess.TimeoutExpired as exc:
        raise NotesScriptError(
            f"Notes.app scripting timed out after {_OSASCRIPT_TIMEOUT}s"
        ) from exc
    if proc.returncode != 0:
        err = (proc.stderr or "").strip()
        if "-1743" in err or "not allowed" in err.lower():
            raise NotesScriptError(
                "Automation permission denied. Allow this app to control "
                "Notes.app in System Settings > Privacy & Security > "
                "Automation, then retry."
            )
        raise NotesScriptError(f"Notes.app scripting failed: {err or 'unknown error'}")
    out = (proc.stdout or "").strip()
    if not out:
        return None
    try:
        return json.loads(out)
    except ValueError:
        return out


_COMMON = """
function app_() {
    const notes = Application('Notes');
    notes.includeStandardAdditions = true;
    if (!notes.running()) { notes.activate(); delay(1.5); }
    return notes;
}
function findFolder(notes, name) {
    if (!name) return null;
    const target = name.toLowerCase();
    const accounts = notes.accounts();
    for (const acct of accounts) {
        for (const f of acct.folders()) {
            const fname = f.name().toLowerCase();
            if (fname === target ||
                (acct.name() + '/' + f.name()).toLowerCase() === target) {
                return f;
            }
        }
    }
    return null;
}
"""


def text_to_note_html(title: Optional[str], body_text: str) -> str:
    """Convert plain text to the HTML Notes.app expects for a body.

    The first <div> becomes the note title in Notes' UI, so when a title
    is given we emit it as a leading heading div.
    """
    lines = body_text.split("\n")
    parts = []
    if title:
        parts.append(f"<div><h1>{html.escape(title)}</h1></div>")
    for line in lines:
        parts.append(f"<div>{html.escape(line) or '<br>'}</div>")
    return "".join(parts)


# ---------------------------------------------------------------------------
# Writes
# ---------------------------------------------------------------------------

def create_note(
    title: str, body_text: str, folder: Optional[str] = None
) -> dict[str, Any]:
    """Create a note; returns {id, identifier_hint, folder}."""
    script = _COMMON + """
function run(argv) {
    const p = JSON.parse(argv[0]);
    const notes = app_();
    let container = findFolder(notes, p.folder);
    if (p.folder && !container) {
        return JSON.stringify({error: 'folder not found: ' + p.folder});
    }
    if (!container) container = notes.defaultAccount();
    const note = notes.Note({body: p.html});
    container.notes.push(note);
    return JSON.stringify({
        id: note.id(),
        name: note.name(),
        folder: p.folder || 'Notes'
    });
}
"""
    result = _run_jxa(
        script, {"html": text_to_note_html(title, body_text), "folder": folder}
    )
    if isinstance(result, dict) and result.get("error"):
        raise NotesScriptError(result["error"])
    return result


def append_to_note(coredata_id: str, body_text: str) -> dict[str, Any]:
    """Append plain text (as HTML divs) to an existing note's body."""
    script = _COMMON + """
function run(argv) {
    const p = JSON.parse(argv[0]);
    const notes = app_();
    let note;
    try { note = notes.notes.byId(p.id); note.name(); }
    catch (e) { return JSON.stringify({error: 'note not found: ' + p.id}); }
    note.body = note.body() + p.html;
    return JSON.stringify({id: note.id(), name: note.name()});
}
"""
    result = _run_jxa(
        script, {"id": coredata_id, "html": text_to_note_html(None, body_text)}
    )
    if isinstance(result, dict) and result.get("error"):
        raise NotesScriptError(result["error"])
    return result


def replace_note_body(
    coredata_id: str, body_text: str, title: Optional[str] = None
) -> dict[str, Any]:
    """Overwrite an existing note's body.

    Notes takes a note's title from the first line of its body, so a
    title given here is emitted as that first line. Passing None lets
    the new body's own first line become the title — callers that mean
    to keep the current title must pass it explicitly (HybridBridge
    does this for them).
    """
    script = _COMMON + """
function run(argv) {
    const p = JSON.parse(argv[0]);
    const notes = app_();
    let note;
    try { note = notes.notes.byId(p.id); note.name(); }
    catch (e) { return JSON.stringify({error: 'note not found: ' + p.id}); }
    if (note.passwordProtected()) {
        return JSON.stringify({error: 'note is password-protected'});
    }
    note.body = p.html;
    return JSON.stringify({id: note.id(), name: note.name()});
}
"""
    result = _run_jxa(
        script,
        {"id": coredata_id, "html": text_to_note_html(title, body_text)},
    )
    if isinstance(result, dict) and result.get("error"):
        raise NotesScriptError(result["error"])
    return result


def create_folder(name: str, account: Optional[str] = None) -> dict[str, Any]:
    """Create a folder in the named account (default account if omitted)."""
    script = _COMMON + """
function run(argv) {
    const p = JSON.parse(argv[0]);
    const notes = app_();
    let acct = null;
    if (p.account) {
        const target = p.account.toLowerCase();
        for (const a of notes.accounts()) {
            if (a.name().toLowerCase() === target) { acct = a; break; }
        }
        if (!acct) return JSON.stringify({error: 'account not found: ' + p.account});
    } else {
        acct = notes.defaultAccount();
    }
    for (const f of acct.folders()) {
        if (f.name().toLowerCase() === p.name.toLowerCase()) {
            return JSON.stringify({error: 'folder already exists: ' + f.name()});
        }
    }
    const folder = notes.Folder({name: p.name});
    acct.folders.push(folder);
    return JSON.stringify({name: folder.name(), account: acct.name()});
}
"""
    result = _run_jxa(script, {"name": name, "account": account})
    if isinstance(result, dict) and result.get("error"):
        raise NotesScriptError(result["error"])
    return result


def move_note(coredata_id: str, folder: str) -> dict[str, Any]:
    """Move a note into an existing folder (name or "Account/Folder")."""
    script = _COMMON + """
function run(argv) {
    const p = JSON.parse(argv[0]);
    const notes = app_();
    let note;
    try { note = notes.notes.byId(p.id); note.name(); }
    catch (e) { return JSON.stringify({error: 'note not found: ' + p.id}); }
    const dest = findFolder(notes, p.folder);
    if (!dest) return JSON.stringify({error: 'folder not found: ' + p.folder});
    notes.move(note, {to: dest});
    // move() invalidates the old specifier; re-read through the folder.
    return JSON.stringify({
        id: p.id, name: note.name(),
        folder: dest.name(), account: dest.container().name()
    });
}
"""
    result = _run_jxa(script, {"id": coredata_id, "folder": folder})
    if isinstance(result, dict) and result.get("error"):
        raise NotesScriptError(result["error"])
    return result


def show_note(coredata_id: str) -> bool:
    """Front Notes.app with the given note selected."""
    script = _COMMON + """
function run(argv) {
    const p = JSON.parse(argv[0]);
    const notes = app_();
    let note;
    try { note = notes.notes.byId(p.id); note.name(); }
    catch (e) { return JSON.stringify({error: 'not found'}); }
    notes.activate();
    note.show();
    return JSON.stringify({ok: true});
}
"""
    result = _run_jxa(script, {"id": coredata_id})
    return isinstance(result, dict) and bool(result.get("ok"))


# ---------------------------------------------------------------------------
# Fallback reads (no Full Disk Access)
# ---------------------------------------------------------------------------

def list_folders() -> list[dict[str, Any]]:
    script = _COMMON + """
function run() {
    const notes = app_();
    const out = [];
    for (const acct of notes.accounts()) {
        const aname = acct.name();
        for (const f of acct.folders()) {
            out.push({name: f.name(), account: aname,
                      note_count: f.notes().length});
        }
    }
    return JSON.stringify(out);
}
"""
    return _run_jxa(script) or []


def search_notes(
    query: Optional[str], folder: Optional[str], limit: int
) -> list[dict[str, Any]]:
    """Slow JXA search: bulk-fetches names/dates, filters in process."""
    script = _COMMON + """
function run(argv) {
    const p = JSON.parse(argv[0]);
    const notes = app_();
    let pool;
    if (p.folder) {
        const f = findFolder(notes, p.folder);
        if (!f) return JSON.stringify([]);
        pool = f.notes;
    } else {
        pool = notes.notes;
    }
    const names = pool.name();
    const ids = pool.id();
    const mods = pool.modificationDate();
    const q = (p.query || '').toLowerCase();
    const out = [];
    for (let i = 0; i < names.length && out.length < p.limit * 4; i++) {
        if (q && names[i].toLowerCase().indexOf(q) === -1) continue;
        out.push({id: ids[i], title: names[i],
                  modified: mods[i] ? mods[i].toISOString() : null});
    }
    out.sort((a, b) => (b.modified || '').localeCompare(a.modified || ''));
    return JSON.stringify(out.slice(0, p.limit));
}
"""
    return _run_jxa(script, {"query": query, "folder": folder, "limit": limit}) or []


def get_note_body(coredata_id: str) -> Optional[dict[str, Any]]:
    script = _COMMON + """
function run(argv) {
    const p = JSON.parse(argv[0]);
    const notes = app_();
    let note;
    try { note = notes.notes.byId(p.id); note.name(); }
    catch (e) { return JSON.stringify(null); }
    return JSON.stringify({
        id: note.id(), title: note.name(), body_html: note.body(),
        plaintext: note.plaintext(),
        created: note.creationDate() ? note.creationDate().toISOString() : null,
        modified: note.modificationDate() ? note.modificationDate().toISOString() : null,
        password_protected: note.passwordProtected()
    });
}
"""
    return _run_jxa(script, {"id": coredata_id})


# ---------------------------------------------------------------------------
# Selection (scripting-only — the local store records no UI selection)
# ---------------------------------------------------------------------------

def get_selection() -> list[str]:
    """Return the Core Data ids of the notes selected in Notes.app.

    Only ids are read here: selection specifiers resolve their id
    reliably, and callers re-read the note through the fast path to get
    title/body/folder. Returns [] when Notes.app isn't running, since
    launching it would front an app with nothing selected.
    """
    script = _COMMON + """
function run() {
    const notes = Application('Notes');
    if (!notes.running()) return JSON.stringify([]);
    let sel;
    try { sel = notes.selection(); }
    catch (e) { return JSON.stringify([]); }
    const out = [];
    for (const n of sel) {
        try { out.push(n.id()); } catch (e) {}
    }
    return JSON.stringify(out);
}
"""
    result = _run_jxa(script)
    return [i for i in (result or []) if isinstance(i, str)]
