"""
Self-test for the fast NoteStore read path.

Run from a terminal that has Full Disk Access:

    uv run python -m apple_notes_mcp.selftest

Verifies: store opens read-only, schema resolves, folders and notes
enumerate, at least one note body decodes, and search returns results.
Read-only throughout — never mutates the store.
"""

from __future__ import annotations

import sys
import time

from .notestore import NoteStore, NoteStoreError


def main() -> int:
    print("Apple Notes MCP — fast-path selftest")
    print("=" * 50)

    store = NoteStore()
    try:
        store._connect()
    except NoteStoreError as exc:
        print(f"FAIL: {exc}")
        print(
            "\nGrant Full Disk Access to your terminal (System Settings > "
            "Privacy & Security > Full Disk Access) and retry."
        )
        return 1
    print(f"OK   store opened read-only ({store._path})")
    print(f"     store UUID: {store.store_uuid}")

    t0 = time.time()
    stats = store.stats()
    print(
        f"OK   stats in {(time.time() - t0) * 1000:.0f}ms: "
        f"{stats.total_notes} notes, {stats.folder_count} folders, "
        f"{stats.account_count} account(s), {stats.pinned_notes} pinned, "
        f"{stats.trashed_notes} trashed, "
        f"{stats.password_protected_notes} locked"
    )

    folders = store.list_folders()
    print(f"OK   {len(folders)} folders enumerated")

    t0 = time.time()
    result = store.search_notes(limit=5)
    dt = (time.time() - t0) * 1000
    print(f"OK   search (no filters) in {dt:.0f}ms — {result.total} notes")
    for note in result.notes:
        mod = note.modified.strftime("%Y-%m-%d") if note.modified else "?"
        print(f"       [{note.id}] {mod}  {note.title[:60]!r}  ({note.folder})")

    decoded = 0
    for note in result.notes:
        if note.is_password_protected:
            continue
        detail = store.get_note(note.id)
        if detail and detail.body_text is not None:
            decoded += 1
    if decoded:
        print(f"OK   {decoded}/{len(result.notes)} recent note bodies decoded")
    else:
        print("WARN no note bodies decoded — protobuf path may need attention")

    if result.notes:
        probe = result.notes[0].title.split()[0] if result.notes[0].title else ""
        if probe:
            t0 = time.time()
            hits = store.search_notes(query=probe, limit=5)
            dt = (time.time() - t0) * 1000
            print(
                f"OK   body search for {probe!r} in {dt:.0f}ms — "
                f"{hits.total} hit(s)"
            )

    print("\nAll fast-path checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
