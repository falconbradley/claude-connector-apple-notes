"""
Localhost HTTP redirector so chat UIs can open notes with one click.

Chat clients (Claude Desktop / Claude Code) block custom URL schemes
like applenotes:// in rendered links but happily open http(s) URLs in
the default browser. This module runs a tiny localhost-only HTTP server;
search results carry an open_link like

    http://127.0.0.1:46326/open/728?t=<token>

Clicking it opens the browser, which hits this server, which fronts
Notes.app on that note (via the applenotes:// deep link, falling back to
Notes.app scripting).

Security posture:
  - Bound to 127.0.0.1 only.
  - Every request must carry a per-install random token (persisted to
    disk so links in old chat transcripts keep working across restarts).
  - The only action is focusing Notes.app on a note; no note content is
    ever served over HTTP.

Multiple server instances (Claude Desktop + a Claude Code session) share
the persisted port: the first instance binds it, later instances detect
the sibling via /ping and emit links pointing at the same port.
"""

from __future__ import annotations

import hmac
import json
import logging
import re
import secrets
import subprocess
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import parse_qs, urlparse

logger = logging.getLogger("apple_notes_mcp.weblink")

_DEFAULT_PORT = 46326
_DEFAULT_STATE = (
    Path.home() / "Library" / "Application Support" / "apple-notes-mcp"
    / "weblink.json"
)
_PING_BODY = b"apple-notes-mcp-weblink"
_OPEN_PATH_RE = re.compile(r"^/open/(\d{1,12})$")

_PAGE = """<!doctype html><meta charset="utf-8">
<title>{title}</title>
<body style="font-family: -apple-system, sans-serif; margin: 3em; color: #333">
<h3>{title}</h3><p>{detail}</p></body>
"""


class WebLinkServer:
    """Serves /open/<note_id> links that focus Notes.app on a note."""

    def __init__(
        self,
        open_note: Callable[[int], bool],
        state_path: Optional[Path] = None,
        preferred_port: int = _DEFAULT_PORT,
    ) -> None:
        self._open_note = open_note
        self._state_path = state_path or _DEFAULT_STATE
        self._preferred_port = preferred_port
        self._lock = threading.Lock()
        self._httpd: Optional[ThreadingHTTPServer] = None
        self._started = False
        self.port: Optional[int] = None
        self.token: Optional[str] = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def open_link(self, note_id: int) -> Optional[str]:
        """Return the clickable http:// link for a note, or None if the
        redirector could not be started."""
        if not self.ensure_started():
            return None
        return f"http://127.0.0.1:{self.port}/open/{note_id}?t={self.token}"

    def ensure_started(self) -> bool:
        with self._lock:
            if self._started:
                return self.port is not None
            self._started = True
            try:
                self._start()
            except Exception:
                logger.exception("Web link redirector failed to start.")
                self.port = None
            return self.port is not None

    def shutdown(self) -> None:
        with self._lock:
            if self._httpd is not None:
                self._httpd.shutdown()
                self._httpd = None
            self._started = False

    # ------------------------------------------------------------------
    # Startup / state
    # ------------------------------------------------------------------

    def _start(self) -> None:
        state = self._load_state()
        self.token = state["token"]
        wanted_port = state.get("port", self._preferred_port)

        try:
            self._bind_and_serve(wanted_port)
        except OSError:
            if self._sibling_alive(wanted_port):
                # Another instance of this server owns the port; reuse it
                # in generated links, nothing to serve from here.
                logger.info("Web links served by sibling on port %d.", wanted_port)
                self.port = wanted_port
                return
            # Port taken by an unrelated process — fall back to ephemeral.
            self._bind_and_serve(0)

        state["port"] = self.port
        self._save_state(state)

    def _bind_and_serve(self, port: int) -> None:
        handler = self._make_handler()
        self._httpd = ThreadingHTTPServer(("127.0.0.1", port), handler)
        self._httpd.daemon_threads = True
        self.port = self._httpd.server_address[1]
        thread = threading.Thread(
            target=self._httpd.serve_forever, name="weblink", daemon=True
        )
        thread.start()
        logger.info("Web link redirector listening on 127.0.0.1:%d", self.port)

    def _sibling_alive(self, port: int) -> bool:
        try:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/ping?t={self.token}", timeout=1.0
            ) as resp:
                return resp.read(64).strip() == _PING_BODY
        except OSError:
            return False

    def _load_state(self) -> dict:
        try:
            state = json.loads(self._state_path.read_text())
            if isinstance(state.get("token"), str) and state["token"]:
                return state
        except (OSError, ValueError):
            pass
        return {"token": secrets.token_urlsafe(16), "port": self._preferred_port}

    def _save_state(self, state: dict) -> None:
        try:
            self._state_path.parent.mkdir(parents=True, exist_ok=True)
            self._state_path.write_text(json.dumps(state))
        except OSError as exc:
            logger.warning("Could not persist weblink state: %s", exc)

    # ------------------------------------------------------------------
    # Request handling
    # ------------------------------------------------------------------

    @staticmethod
    def open_url_with_macos(url: str) -> bool:
        proc = subprocess.run(["open", url], capture_output=True, timeout=15)
        return proc.returncode == 0

    def _make_handler(self) -> type:
        server = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, fmt: str, *args) -> None:
                logger.debug("weblink: " + fmt, *args)

            def _reply(self, status: int, title: str, detail: str = "") -> None:
                body = _PAGE.format(title=title, detail=detail).encode()
                self.send_response(status)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self) -> None:  # noqa: N802 (http.server API)
                parsed = urlparse(self.path)
                token = (parse_qs(parsed.query).get("t") or [""])[0]
                if not (server.token and hmac.compare_digest(token, server.token)):
                    self._reply(403, "Forbidden")
                    return

                if parsed.path == "/ping":
                    self.send_response(200)
                    self.send_header("Content-Length", str(len(_PING_BODY)))
                    self.end_headers()
                    self.wfile.write(_PING_BODY)
                    return

                match = _OPEN_PATH_RE.fullmatch(parsed.path)
                if not match:
                    self._reply(404, "Not found")
                    return

                note_id = int(match.group(1))
                try:
                    opened = server._open_note(note_id)
                except Exception:
                    logger.exception("weblink: open failed for %d", note_id)
                    opened = False
                if opened:
                    self._reply(
                        200,
                        "Opened in Notes",
                        "The note should now be front-most in Notes.app. "
                        "You can close this tab.",
                    )
                else:
                    self._reply(
                        404,
                        "Note not found",
                        f"No note with id {note_id} — it may have been "
                        "deleted, or the Notes store may have changed.",
                    )

        return Handler
