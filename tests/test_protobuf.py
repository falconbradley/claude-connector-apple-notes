"""Unit tests for the minimal protobuf reader and body extraction."""

import gzip
import struct

import pytest

from apple_notes_mcp.protobuf import extract_note_text, iter_fields


def _varint(value: int) -> bytes:
    out = b""
    while True:
        b = value & 0x7F
        value >>= 7
        if value:
            out += bytes([b | 0x80])
        else:
            return out + bytes([b])


def _length_field(field_number: int, payload: bytes) -> bytes:
    return _varint((field_number << 3) | 2) + _varint(len(payload)) + payload


def _note_blob(text: str) -> bytes:
    """Build a synthetic ZDATA blob shaped like Apple's: gzip(root{2: document{3: note{2: text}}})."""
    note = _length_field(2, text.encode("utf-8"))
    document = _length_field(3, note)
    root = _varint(0x08) + b"\x00" + _length_field(2, document)
    return gzip.compress(root)


def test_extract_simple_text():
    assert extract_note_text(_note_blob("Hello\nWorld")) == "Hello\nWorld"


def test_extract_unicode():
    text = "Grocery 🛒 list — crème fraîche"
    assert extract_note_text(_note_blob(text)) == text


def test_object_placeholder_replaced():
    out = extract_note_text(_note_blob("before ￼ after"))
    assert "￼" not in out
    assert "[attachment]" in out


def test_line_separator_normalised():
    out = extract_note_text(_note_blob("line1 line2"))
    assert out == "line1\nline2"


def test_not_gzip_returns_none():
    assert extract_note_text(b"\x00\x01\x02not-gzip") is None


def test_gzip_but_not_expected_shape_returns_none():
    assert extract_note_text(gzip.compress(b"\x0a\x03abc")) is None


def test_iter_fields_all_wire_types():
    buf = (
        _varint(0x08) + _varint(300)              # field 1, varint
        + _varint(0x11) + struct.pack("<d", 1.5)  # field 2, fixed64
        + _length_field(3, b"abc")                # field 3, length-delimited
        + _varint(0x25) + struct.pack("<f", 2.5)  # field 4, fixed32
    )
    fields = list(iter_fields(buf))
    assert [f[0] for f in fields] == [1, 2, 3, 4]
    assert fields[2][2] == b"abc"


def test_iter_fields_truncated_raises():
    with pytest.raises(ValueError):
        list(iter_fields(_varint(0x12) + _varint(100) + b"short"))
