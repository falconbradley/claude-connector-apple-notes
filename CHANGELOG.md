# Changelog

All notable changes to this project are documented here.
This project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.4.0] — 2026-08-24

### Added

- **Rich text on write.** `create_note`, `append_to_note` and `update_note`
  accept `format="markdown"`, rendering `**bold**`, `*italic*`,
  `~~strikethrough~~`, `` `code` ``, `[links](url)`, `# headings`, and
  bulleted and numbered lists. Verified end to end by writing a note and
  decoding Notes' internal format: bold, italic and strikethrough set the
  expected font traits, code becomes Courier, links store the real URL, and
  lists become paragraph styles 100 and 102 — with inline formatting
  preserved inside list items.
- Markdown is converted in-process (`richtext.py`); no third-party dependency
  is added to the shipped bundle.

### Notes on fidelity

- `format` defaults to `"plain"`, leaving existing behaviour byte-for-byte
  identical, so text containing `*asterisks*` or `# hashes` is never
  reformatted unexpectedly. An unrecognised value raises rather than silently
  falling back to plain, which would publish raw Markdown into a note.
- Notes ignores colour, font size, superscript and blockquotes; the text
  survives, the styling does not.
- Headings render bold but do not become Notes' semantic Title/Heading
  paragraph styles.
- Markdown checkbox syntax produces an ordinary bullet — checklists remain
  unwritable, so it is not advertised as supported.

## [0.3.0] — 2026-08-24

### Fixed

- **`append_to_note` and `update_note` no longer destroy checklists.** Both
  hand Notes.app an HTML body, and that representation cannot carry checkbox
  state in either direction — verified both ways: Notes renders a checklist as
  plain `<ul><li>` with no checked attribute, and writing
  `<ul class="checklist"><li checked>` back produces an ordinary bulleted list.
  So appending a single line to a checklist note silently converted every
  checkbox to a bullet and erased which items were done. Both tools now detect
  checklists through the local store and refuse, explaining why; the refusal
  cannot be overridden. Present since 0.1.0.
- Both write tools also refuse notes carrying very large attachments, which
  Notes.app inlines as base64 when rewriting a body (one real note produced an
  11 MB body). Overridable with `force=true`, since that is a performance risk
  rather than data loss.

### Added

- `list_note_attachments` — everything embedded in a note: images, PDFs,
  scans, drawings, tables, links. Reports a friendly `kind` alongside the raw
  UTI, and link attachments expose their target `url`.
- `get_attachment` — one attachment by id, including its path on disk.
- Attachments resolve entirely from the local store, with no scripting and no
  Notes.app: media files are located under
  `Accounts/<account>/Media/<media-uuid>/<generation>/<filename>`, globbing the
  generation directory since it changes when an attachment is edited.
  `has_local_file` distinguishes "file present on disk" from attachments that
  have no file by design (links, tables, drawings).
- `get_note` now reports `attachment_count` and `has_checklist`.

### Known limits

- **Checklist state is read-only.** It can be decoded from the local store but
  never written, for the reason above. Rendering item text and state in
  `get_note` is planned; toggling is struck from the roadmap.
- Tables are stored as attachments with a gzipped CRDT payload in
  `ZMERGEABLEDATA1`. Listed as attachments of kind `table`; decoding their
  contents is not implemented.

## [0.2.0] — 2026-08-23

### Fixed

- **The server could not start.** `mcp` 2.0.0 removed `mcp.server.fastmcp`, and
  `pyproject.toml` allowed `mcp[cli]>=1.3.0`, so a fresh resolve pulled 2.0.0
  against code written for the 1.x API. The v0.1.0 `.mcpb` shipped a `uv.lock`
  pinning `mcp==2.0.0`, so installs of that release failed at import. Migrated
  to the 2.0 API (`FastMCP` → `MCPServer`, a straight rename) and raised the
  floor to `mcp[cli]>=2.0.0`.
- `update_note` no longer silently renames a note. Notes derives a note's
  title from the first line of its body, so replacing a body without
  re-emitting the title renamed the note to the new first line — the opposite
  of what the tool description promised. The current title is now preserved
  when `title` is omitted (and not duplicated when the new body already opens
  with it). Found by the live smoke test, not the unit tests.
- Unit tests can no longer reach real Notes.app scripting. An unmocked call
  quietly succeeded against the live app locally while hanging for the full
  120s timeout on CI; `tests/conftest.py` now fails such calls immediately.
- `uv.lock` is now committed rather than gitignored. It is packed into the
  `.mcpb`, so it determines what the shipped server runs against; leaving it
  untracked is what let the break above ship unnoticed.

### Added

- `get_selected_notes` — the notes currently selected in Notes.app, with
  bodies. Reports `count` alongside `returned`, so a selection larger than
  `limit` is visible rather than silently truncated.
- `update_note` — replace a note's body. Overwrites; refuses
  password-protected notes.
- `create_folder` — create a folder in any account.
- `move_note` — move a note into an existing folder.
- CI (`.github/workflows/ci.yml`): tests on macOS across Python 3.11 and 3.13,
  a server-import check, manifest validation, and consistency checks that the
  manifest version matches `pyproject.toml` and the manifest tool list matches
  the tools actually registered in `server.py`.
- Release workflow (`.github/workflows/release.yml`): on a `v*` tag, runs the
  tests, verifies the tag matches the manifest version, builds, and publishes
  the `.mcpb` assets.
- `tests/test_server.py` and `tests/test_writes.py` — tool registration, a real
  stdio MCP handshake, and the new write/selection logic with JXA mocked.

### Not implemented

- **Pin / unpin.** Notes.app's scripting dictionary exposes no `pinned`
  property, so the only route would be writing `ZISPINNED` directly into
  `NoteStore.sqlite` — which would break the read-only-store invariant and race
  Core Data and CloudKit sync. Removed from the roadmap rather than left open.

## [0.1.0] — 2026-08-23

Initial release: fast local-store reads (folders, search over titles, snippets
and full body text, note bodies), clickable open-in-Notes links, a JXA fallback
for when Full Disk Access is unavailable, and native-scripting writes
(`create_note`, `append_to_note`).

[0.4.0]: https://github.com/falconbradley/claude-connector-apple-notes/releases/tag/v0.4.0
[0.3.0]: https://github.com/falconbradley/claude-connector-apple-notes/releases/tag/v0.3.0
[0.2.0]: https://github.com/falconbradley/claude-connector-apple-notes/releases/tag/v0.2.0
[0.1.0]: https://github.com/falconbradley/claude-connector-apple-notes/releases/tag/v0.1.0
