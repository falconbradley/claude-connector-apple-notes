"""
Minimal protobuf wire-format reader for Apple Notes body blobs.

A note body lives in ZICNOTEDATA.ZDATA as a gzip stream. Decompressed,
it is a protobuf message (Apple's private "versioned document" format):

    root
      2: document          (length-delimited)
         3: note           (length-delimited)
            2: note_text   (UTF-8 string — the full plain text)
            5: attribute_run (repeated — styling, checklists, links...)

Only the wire format is implemented here (varint / fixed / length-
delimited framing); no .proto schema is required. We navigate the known
field path 2 -> 3 -> 2 to extract the plain text. Formatting attribute
runs are intentionally ignored in v0.1 — the note text field already
contains the complete text content, including table/attachment
placeholder characters (U+FFFC), which we tidy up.
"""

from __future__ import annotations

import zlib
from typing import Iterator, Optional, Tuple

# Wire types
_VARINT = 0
_FIXED64 = 1
_LENGTH = 2
_FIXED32 = 5


def _read_varint(buf: bytes, pos: int) -> Tuple[int, int]:
    """Return (value, new_pos). Raises ValueError on truncation."""
    result = 0
    shift = 0
    while True:
        if pos >= len(buf):
            raise ValueError("truncated varint")
        b = buf[pos]
        pos += 1
        result |= (b & 0x7F) << shift
        if not b & 0x80:
            return result, pos
        shift += 7
        if shift > 63:
            raise ValueError("varint too long")


def iter_fields(buf: bytes) -> Iterator[Tuple[int, int, bytes]]:
    """Yield (field_number, wire_type, payload) for each field in buf.

    For length-delimited fields the payload is the raw bytes; for varint
    and fixed fields it is the encoded bytes (rarely needed here).
    """
    pos = 0
    end = len(buf)
    while pos < end:
        key, pos = _read_varint(buf, pos)
        field, wire = key >> 3, key & 0x07
        if wire == _VARINT:
            start = pos
            _, pos = _read_varint(buf, pos)
            yield field, wire, buf[start:pos]
        elif wire == _FIXED64:
            if pos + 8 > end:
                raise ValueError("truncated fixed64")
            yield field, wire, buf[pos:pos + 8]
            pos += 8
        elif wire == _LENGTH:
            length, pos = _read_varint(buf, pos)
            if pos + length > end:
                raise ValueError("truncated length-delimited field")
            yield field, wire, buf[pos:pos + length]
            pos += length
        elif wire == _FIXED32:
            if pos + 4 > end:
                raise ValueError("truncated fixed32")
            yield field, wire, buf[pos:pos + 4]
            pos += 4
        else:
            raise ValueError(f"unsupported wire type {wire}")


def _first_length_field(buf: bytes, field_number: int) -> Optional[bytes]:
    for field, wire, payload in iter_fields(buf):
        if field == field_number and wire == _LENGTH:
            return payload
    return None


def extract_note_text(
    zdata: bytes, attachment_placeholder: str = "\n[attachment]\n"
) -> Optional[str]:
    """Extract plain text from a raw ZICNOTEDATA.ZDATA blob.

    Returns None if the blob cannot be decoded (e.g. encrypted note —
    those carry a crypto tag and don't gunzip).
    """
    try:
        raw = zlib.decompress(zdata, 47)  # 47 = auto-detect zlib/gzip wrapper
    except zlib.error:
        return None
    try:
        document = _first_length_field(raw, 2)
        if document is None:
            return None
        note = _first_length_field(document, 3)
        if note is None:
            return None
        text = _first_length_field(note, 2)
        if text is None:
            return None
        decoded = text.decode("utf-8", errors="replace")
    except ValueError:
        return None
    # U+FFFC marks embedded objects (tables, images, drawings, scans);
    # U+2028 is the line separator Notes uses inside paragraphs.
    return (
        decoded.replace("\ufffc", attachment_placeholder)
        .replace("\u2028", "\n")
    )


def _iter_paragraph_styles(note: bytes) -> Iterator[bytes]:
    """Yield each attribute_run's paragraph_style payload (field 5 -> 2)."""
    for field, wire, payload in iter_fields(note):
        if field != 5 or wire != _LENGTH:      # attribute_run
            continue
        for f2, w2, p2 in iter_fields(payload):
            if f2 == 2 and w2 == _LENGTH:      # paragraph_style
                yield p2


def has_checklist(zdata: bytes) -> bool:
    """True if a note body contains checklist (checkbox) paragraphs.

    Checklist state lives only here — Notes' AppleScript HTML body
    renders checkboxes as plain <ul><li> with no checked attribute, and
    writing checklist markup back produces a plain list. So a body
    round-trip through scripting silently converts checkboxes to bullets
    and loses every done-state; callers use this to refuse that write.
    """
    try:
        raw = zlib.decompress(zdata, 47)
        document = _first_length_field(raw, 2)
        if document is None:
            return False
        note = _first_length_field(document, 3)
        if note is None:
            return False
        for style in _iter_paragraph_styles(note):
            for f3, w3, _ in iter_fields(style):
                if f3 == 5 and w3 == _LENGTH:  # checklist submessage
                    return True
    except (zlib.error, ValueError):
        return False
    return False


def extract_attachment_refs(zdata: bytes) -> list[Tuple[str, Optional[str]]]:
    """Embedded attachments in document order, as (identifier, type_uti).

    Each entry corresponds, in order, to one U+FFFC placeholder in the
    note text — verified 1:1 across every note in a real store. Ordering
    has to come from here rather than from the attachment table's row
    ids, which do not follow document order once a note has been edited.
    """
    refs: list[Tuple[str, Optional[str]]] = []
    try:
        raw = zlib.decompress(zdata, 47)
        document = _first_length_field(raw, 2)
        if document is None:
            return refs
        note = _first_length_field(document, 3)
        if note is None:
            return refs
        for field, wire, payload in iter_fields(note):
            if field != 5 or wire != _LENGTH:      # attribute_run
                continue
            for f2, w2, p2 in iter_fields(payload):
                if f2 != 12 or w2 != _LENGTH:      # attachment_info
                    continue
                identifier = uti = None
                for f3, w3, p3 in iter_fields(p2):
                    if f3 == 1 and w3 == _LENGTH:
                        identifier = p3.decode("utf-8", "replace")
                    elif f3 == 2 and w3 == _LENGTH:
                        uti = p3.decode("utf-8", "replace")
                if identifier:
                    refs.append((identifier, uti))
    except (zlib.error, ValueError):
        return refs
    return refs
