# Changelog

All notable changes to this project are documented here.
This project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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

[0.2.0]: https://github.com/falconbradley/claude-connector-apple-notes/releases/tag/v0.2.0
[0.1.0]: https://github.com/falconbradley/claude-connector-apple-notes/releases/tag/v0.1.0
