# Apple Notes MCP

A Claude Desktop extension that gives **Claude fast access to Apple Notes** on macOS. Reads come straight from Notes.app's local store — searching thousands of notes, *including full body text*, takes **milliseconds** and works even when Notes.app isn't running — while Notes.app remains the sync and auth engine (iCloud, On My Mac, anything Notes supports), so no credentials are ever handled. Writes — create, append, rewrite, new folders, move — go through Notes.app's native scripting interface.

Packaged as an [MCPB desktop extension](https://support.claude.com/en/articles/12922929-building-desktop-extensions-with-mcpb) with the Apple Notes icon and one-click install.

Sibling project: [claude-connector-apple-mail](https://github.com/falconbradley/claude-connector-apple-mail) — the same architecture for Apple Mail.

---

## What it does

| Tool | Description |
|------|-------------|
| `get_stats` | Total notes, pinned, trashed, password-protected, folder and account counts |
| `list_folders` | Every account/folder with note counts |
| `search_notes` | Rich search: free text over titles, snippets, **and full body text**; folder, date range, pinned. Every result includes clickable open-in-Notes links |
| `get_note` | Full note with extracted plain-text body and metadata |
| `get_note_link` | An `applenotes://` deep link + a clickable http link for the note |
| `open_note_in_notes` | Open a note directly in Notes.app (for chat UIs that block custom URL schemes) |
| `get_selected_notes` | The notes you currently have selected in Notes.app, with bodies — for "this note" / "what I'm looking at" |
| `create_note` | Create a new note (optionally in a specific folder) — synced natively by Notes.app |
| `append_to_note` | Append plain text to an existing note |
| `update_note` | Replace a note's body (overwrites — use `append_to_note` to add without replacing). Keeps the note's title unless you pass a new one |
| `create_folder` | Create a new folder in any account |
| `move_note` | Move a note into an existing folder |

## How it works

Notes.app remains the **sync and auth engine** — it holds your Apple ID / iCloud credentials natively and continuously mirrors every note to disk. This server has two engines on top of that:

### Fast read path (default, needs Full Disk Access)

Reads are served directly from Notes.app's local store:

- **`~/Library/Group Containers/group.com.apple.notes/NoteStore.sqlite`** — the Core Data store holding every note, folder, and account (titles, snippets, dates, pinned/locked state). Searches complete in **milliseconds**, and don't require Notes.app to be running.
- **Note bodies** are gzip-compressed protobufs inside the same store; the server decompresses and parses them with a minimal wire-format reader to extract the full plain text — which is what makes fast *body* search possible.

No credentials are ever handled: the server is a read-only consumer of data Notes.app has already synced. The store is opened read-only (`PRAGMA query_only`) and never mutated. The only extra requirement is **Full Disk Access**, granted once in System Settings — see [Permissions](#permissions) for the `uv` gotcha.

The Core Data schema varies across macOS releases (entity ids and column names drift: `ZTITLE1` vs `ZTITLE`, `ZACCOUNT4` vs `ZACCOUNT7`, …), so the server introspects the live schema at runtime and adapts. Run `uv run python -m apple_notes_mcp.selftest` from a terminal with Full Disk Access to verify the fast path on your machine.

**Password-protected notes are respected**: their bodies are encrypted in the store and are never decrypted — the server returns metadata only and says why.

### Clickable open-in-Notes links

Every search/note result carries two links:

- **`notes_link`** — an `applenotes://showNote?identifier=<uuid>` deep link. Works from Terminal (`open '<url>'`) and native apps — but most chat UIs (including Claude Desktop and Claude Code) block custom URL schemes in rendered links.
- **`open_link`** — `http://127.0.0.1:<port>/open/<id>?t=<token>`. Chat UIs open http links fine: the click routes through your browser to a tiny localhost-only server inside the extension, which tells macOS to front Notes.app on that note. Requests require a per-install random token (persisted, so links in old conversations keep working); the endpoint's only capability is focusing Notes — it never serves note content.

There is also an `open_note_in_notes` tool so Claude can jump to a note directly without any clicking.

### JXA fallback + writes

When Full Disk Access is missing, reads transparently fall back to a **JXA (JavaScript for Automation)** bridge scripting Notes.app (much slower, and Notes.app must be running). Search results include an `engine` field (`"sqlite"` or `"applescript"`) so you can tell which path served them. Set `APPLE_NOTES_MCP_DISABLE_FAST=1` to force the JXA path.

Writes — creating notes and folders, appending, rewriting a body, moving notes — always go through Notes.app scripting (Automation permission), so Notes.app owns every mutation and syncs it to iCloud itself. The note ids used by scripting (`x-coredata://…/ICNote/p<n>`) line up 1:1 with the fast path's ids, so the two engines compose cleanly.

Notes derives a note's **title from the first line of its body**, which makes `update_note` subtler than it looks: replacing the body would rename the note to whatever the new first line is. So when you don't pass a `title`, the server reads the note's current title and re-emits it as the first line — the note keeps its name — and skips that if your new body already starts with the title, so it never ends up duplicated. Pass `title` explicitly to rename.

### Reading the current selection

`get_selected_notes` is the one read with no fast-path equivalent: the local store records no UI selection, so it asks Notes.app. Selection specifiers expose little beyond an id, so the ids are handed straight back to the normal read path — meaning titles and bodies come back correctly, and the selection tool costs one cheap scripting call. It returns `count` (how many are selected) alongside the notes actually included, so a selection larger than `limit` is visible rather than silently truncated.

---

## Requirements

- macOS 13 Ventura or later
- Notes set up with at least one account
- Python 3.11+
- Claude Desktop (with extension support)

---

## Installation

### Option 1: Desktop Extension (recommended)

Download the latest `.mcpb` from [Releases](../../releases), then **double-click** to install.

Or build from source:

```bash
git clone https://github.com/falconbradley/claude-connector-apple-notes.git
cd claude-connector-apple-notes
./build.sh
```

Then double-click `dist/apple-notes.mcpb` (or drag it into Claude Desktop).

The extension appears in **Settings > Extensions** with the Apple Notes icon.

### Option 2: Manual MCP config

Edit `~/Library/Application Support/Claude/claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "apple-notes": {
      "command": "uv",
      "args": ["run", "--project", "/path/to/claude-connector-apple-notes", "apple-notes-mcp"]
    }
  }
}
```

### Permissions

Two macOS permissions matter:

1. **Full Disk Access** (for the fast read path): System Settings > Privacy & Security > Full Disk Access > enable **uv**. Claude Desktop launches extension servers through a helper that makes the spawned process itself responsible for permissions, so macOS attributes FDA to the `uv` launcher binary — enabling Claude Desktop alone is *not* sufficient. If `uv` isn't in the list, add it with **+** (press Cmd+Shift+G): `~/.local/bin/uv` and/or `~/Library/Application Support/Claude/uv-runtime/<version>/uv`. Then disable/re-enable the extension. Without FDA, reads still work via the slow scripting fallback. (Enable your terminal app too if you want to run the selftest.)
2. **Automation** (for writes and the fallback): Notes.app must be running for writes; macOS prompts automatically on first use — click **OK**. If the prompt doesn't appear, check System Settings > Privacy & Security > Automation.

---

## Usage examples

Once installed, just ask Claude naturally:

- *"Find my note about the HVAC contractor"*
- *"Search my notes for anything mentioning crème fraîche"*
- *"What notes did I edit this week?"*
- *"Show me my pinned notes"*
- *"Read my 'Chase IRA Accounts' note"*
- *"Create a note in Recipes titled 'Weeknight pasta' with this ingredient list…"*
- *"Append today's meeting takeaways to my 'Meeting notes' note"*
- *"Summarize the note I have open"* / *"What am I looking at?"*
- *"Rewrite this note's body to clean up the formatting"*
- *"Make a folder called Travel and move my Japan notes into it"*
- *"Open that note in Notes"*

---

## Building from source

```bash
# Install mcpb CLI (one time)
npm install -g @anthropic-ai/mcpb

# Build the extension
./build.sh

# Or manually:
mcpb validate manifest.json
mcpb pack . dist/apple-notes.mcpb
```

### Project layout

```
claude-connector-apple-notes/
├── manifest.json              # MCPB desktop extension manifest
├── icon.png                   # Apple Notes icon (512x512)
├── icons/                     # Multi-size icons
├── pyproject.toml             # Python package + dependencies
├── build.sh                   # Validate + pack build script
├── src/
│   └── apple_notes_mcp/
│       ├── server.py          # MCP tools (FastMCP)
│       ├── notestore.py       # Fast SQLite read engine
│       ├── protobuf.py        # Note body wire-format reader
│       ├── applescript.py     # JXA bridge to Notes.app (writes + fallback)
│       ├── hybrid.py          # Engine selection
│       ├── weblink.py         # Localhost open-in-Notes redirector
│       ├── selftest.py        # Fast-path verification
│       └── models.py          # Pydantic data models
├── tests/                     # Unit tests (synthetic store fixture, mocked JXA)
└── .github/workflows/         # CI (tests + manifest checks) and release
```

### Tests

```bash
uv run pytest -q
```

No test touches Notes.app or needs Full Disk Access: the store tests build a synthetic `NoteStore.sqlite`, and the write/selection tests mock the JXA bridge. `tests/test_server.py` additionally launches the real server over stdio and completes an MCP handshake, which is what catches SDK drift — the store tests pass perfectly well against a server module that no longer imports.

---

## Roadmap

**Phase 1 — Read (v0.1)**
- [x] List folders and accounts with counts
- [x] Search notes (title, snippet, full body, folder, dates, pinned)
- [x] Read full note body as plain text
- [x] Clickable open-in-Notes links

**Phase 2 — Write (v0.2)**
- [x] Create notes (in any folder)
- [x] Append to existing notes
- [x] Replace/update note body
- [x] Create folders
- [x] Move notes between folders
- [x] Read the current Notes.app selection
- [ ] ~~Pin / unpin~~ — **not possible.** Notes.app's scripting dictionary exposes no `pinned` property (verified against `sdef /System/Applications/Notes.app`), so the only route would be writing `ZISPINNED` directly into `NoteStore.sqlite`. That would break the read-only-store invariant and race Core Data and CloudKit sync, so this server won't do it.

**Phase 3 — Rich content**
- [ ] Checklists (read + toggle)
- [ ] Attachments (list, retrieve)
- [ ] Tables (read as markdown)
- [ ] Hashtags and mentions

---

## Security & privacy

- Read operations never modify your notes: the store is opened with `PRAGMA query_only` and URI `mode=ro` — the local store is never written to, by any code path.
- Write operations go through Notes.app scripting only, and **nothing is ever deleted** — there is no delete tool, and no tool removes a note or a folder. One tool does overwrite: `update_note` replaces a note's body, and the previous body is not recoverable from this server (Notes.app's own version history still applies). Every other write is additive: `create_note`, `create_folder`, `append_to_note`, and `move_note` (which relocates a note without altering its content).
- `update_note` refuses password-protected notes.
- Password-protected (locked) notes stay locked: their bodies are encrypted at rest and this server never attempts decryption — metadata only.
- No data leaves your machine — this is a local MCP server. Notes.app keeps sole custody of account credentials (iCloud sign-in etc.).
- The open-in-Notes link redirector binds to 127.0.0.1 only, requires a per-install random token on every request, and can only focus Notes.app on a note — it never serves note content.
- macOS-only (`"platforms": ["darwin"]` in manifest).

---

## Troubleshooting

**Reads are slow / `engine: "applescript"` in results**
Full Disk Access is missing for the `uv` binary. See [Permissions](#permissions), then disable/re-enable the extension.

**"Automation permission denied"**
Go to **System Settings > Privacy & Security > Automation** and ensure Claude Desktop (or Terminal) is allowed to control Notes.app. Then restart Claude Desktop.

**Writes fail with "Notes.app scripting failed"**
Notes.app must be running for writes. The server launches it automatically, but the very first write after login can race the launch — retry once.

**Extension doesn't appear after install**
Make sure you're running a recent version of Claude Desktop that supports MCPB extensions. Restart Claude Desktop after installing.

---

## Changelog

See [CHANGELOG.md](CHANGELOG.md).

---

## License

[MIT](LICENSE)
