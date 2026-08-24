"""Markdown -> the HTML subset Notes.app actually honours.

Notes' scripting body is HTML, but it keeps only part of what it is
given. Verified against a live Notes.app by writing a probe note and
decoding the resulting internal format:

  honoured    bold, italic, underline, strikethrough, monospace
              (becomes Courier), links, bulleted lists, numbered lists
  dropped     colour, font size, superscript, blockquote — the text
              survives, the styling does not
  degraded    <h1>/<h2>/<h3> render bold but do not become Notes'
              semantic Title/Heading paragraph styles
  impossible  checklists — see protobuf.has_checklist

No third-party Markdown dependency: the shipped extension resolves its
own dependencies at launch, so the parser here stays small and explicit
rather than adding weight to the bundle for one feature.
"""

from __future__ import annotations

import html
import re
from typing import Optional

__all__ = ["markdown_to_notes_html", "plain_to_notes_html", "SUPPORTED_MARKDOWN"]

SUPPORTED_MARKDOWN = (
    "**bold**, *italic*, ~~strikethrough~~, `code`, [links](url), "
    "# headings, - bullet lists, and 1. numbered lists"
)

_HEADING = re.compile(r"^(#{1,6})\s+(.*)$")
_BULLET = re.compile(r"^\s*[-*+]\s+(.*)$")
_NUMBERED = re.compile(r"^\s*\d+[.)]\s+(.*)$")

# Inline rules, applied to already-escaped text. Order matters: the
# two-character markers must run before their single-character forms.
_INLINE = (
    (re.compile(r"\*\*(?=\S)(.+?)(?<=\S)\*\*"), r"<b>\1</b>"),
    (re.compile(r"__(?=\S)(.+?)(?<=\S)__"), r"<b>\1</b>"),
    (re.compile(r"~~(?=\S)(.+?)(?<=\S)~~"), r"<s>\1</s>"),
    (re.compile(r"(?<![\w*])\*(?=\S)([^*]+?)(?<=\S)\*(?![\w*])"), r"<i>\1</i>"),
    (re.compile(r"(?<![\w_])_(?=\S)([^_]+?)(?<=\S)_(?![\w_])"), r"<i>\1</i>"),
)

_LINK = re.compile(r"\[([^\]]+)\]\(([^)\s]+)\)")


def _inline(text: str) -> str:
    """Render inline Markdown in one line of plain text.

    Code spans are extracted first and restored last so their contents
    are never treated as Markdown.
    """
    spans: list[str] = []

    def _stash_code(match: re.Match) -> str:
        spans.append(html.escape(match.group(1)))
        return f"\x00{len(spans) - 1}\x00"

    text = re.sub(r"`([^`]+)`", _stash_code, text)
    text = html.escape(text)

    def _link(match: re.Match) -> str:
        label, url = match.group(1), match.group(2)
        # html.escape ran over the whole line first (quote=True by
        # default), so any quote in the URL is already &quot; and cannot
        # terminate the attribute.
        return f'<a href="{url}">{label}</a>'

    text = _LINK.sub(_link, text)
    for pattern, repl in _INLINE:
        text = pattern.sub(repl, text)

    for i, code in enumerate(spans):
        text = text.replace(f"\x00{i}\x00", f"<tt>{code}</tt>")
    return text


def _blank_div() -> str:
    return "<div><br></div>"


def markdown_to_notes_html(body_text: str, title: Optional[str] = None) -> str:
    """Convert Markdown to the HTML Notes.app understands.

    Unsupported constructs degrade to their text content rather than
    being dropped, so nothing the caller wrote goes missing.
    """
    parts: list[str] = []
    if title:
        parts.append(f"<div><h1>{html.escape(title)}</h1></div>")

    lines = body_text.split("\n")
    i = 0
    while i < len(lines):
        line = lines[i]
        if not line.strip():
            parts.append(_blank_div())
            i += 1
            continue

        heading = _HEADING.match(line)
        if heading:
            level = min(len(heading.group(1)), 3)
            parts.append(
                f"<div><h{level}>{_inline(heading.group(2))}</h{level}></div>"
            )
            i += 1
            continue

        # Runs of list items become one <ul>/<ol>, which is what makes
        # Notes treat them as a single list rather than separate ones.
        for pattern, tag in ((_BULLET, "ul"), (_NUMBERED, "ol")):
            if pattern.match(line):
                items = []
                while i < len(lines) and pattern.match(lines[i]):
                    items.append(f"<li>{_inline(pattern.match(lines[i]).group(1))}</li>")
                    i += 1
                parts.append(f"<{tag}>{''.join(items)}</{tag}>")
                break
        else:
            parts.append(f"<div>{_inline(line)}</div>")
            i += 1

    return "".join(parts) or _blank_div()


def plain_to_notes_html(body_text: str, title: Optional[str] = None) -> str:
    """Escape plain text into Notes HTML, formatting nothing."""
    parts: list[str] = []
    if title:
        parts.append(f"<div><h1>{html.escape(title)}</h1></div>")
    for line in body_text.split("\n"):
        parts.append(f"<div>{html.escape(line) or '<br>'}</div>")
    return "".join(parts)
