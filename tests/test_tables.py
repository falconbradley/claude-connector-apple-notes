"""Table decoding, on synthetic CRDT payloads built to the real layout.

The structure encoded here mirrors what a live Notes store produces —
including the indirection from ordering UUIDs to identity UUIDs, which
is the part that silently yields an empty table when missed.
"""

import gzip
import re
import zlib

import pytest

from apple_notes_mcp.tables import decode_table, table_to_markdown


# ---------------------------------------------------------------------------
# Minimal encoder for the MergeableData layout
# ---------------------------------------------------------------------------


def _varint(v: int) -> bytes:
    out = b""
    while True:
        b = v & 0x7F
        v >>= 7
        if v:
            out += bytes([b | 0x80])
        else:
            return out + bytes([b])


def _lf(field: int, payload: bytes) -> bytes:
    return _varint((field << 3) | 2) + _varint(len(payload)) + payload


def _vf(field: int, value: int) -> bytes:
    return _varint((field << 3) | 0) + _varint(value)


def _object_id_index(i: int) -> bytes:
    return _vf(6, i)


def _object_id_uint(i: int) -> bytes:
    return _vf(2, i)


def _custom_map(type_index: int, entries: list[tuple[int, bytes]]) -> bytes:
    body = _vf(1, type_index)
    for key, value in entries:
        body += _lf(3, _vf(1, key) + _lf(2, value))
    return _lf(13, body)


def _dictionary(pairs: list[tuple[bytes, bytes]]) -> bytes:
    body = b"".join(_lf(1, _lf(1, k) + _lf(2, v)) for k, v in pairs)
    return _lf(6, body)


def _note(text: str) -> bytes:
    return _lf(10, _lf(2, text.encode()))


def _ordered_set(order_uuids: list[bytes], remap: list[tuple[int, int]]) -> bytes:
    attachments = b"".join(
        _lf(2, _vf(1, i) + _lf(2, u)) for i, u in enumerate(order_uuids)
    )
    array = _lf(1, _lf(1, _lf(2, ("￼" * len(order_uuids)).encode())) + attachments)
    contents = _dictionary(
        [(_object_id_index(a), _object_id_index(b)) for a, b in remap]
    )
    # contents is a Dictionary message; inside ordering it is field 2
    contents_body = contents[1 + len(_varint(len(contents) - 2)):]  # strip the field-6 header
    ordering = _lf(1, array + _lf(2, contents_body))
    return _lf(16, ordering)


KEYS = ["identity", "crTableColumnDirection", "self", "crRows", "UUIDIndex",
        "crColumns", "cellColumns"]
TYPES = ["com.apple.CRDT.NSNumber", "com.apple.CRDT.NSString",
         "com.apple.CRDT.NSUUID", "com.apple.notes.ICTable"]
UUID_T, TABLE_T = 2, 3
K_CRROWS, K_UUIDINDEX, K_CRCOLUMNS, K_CELLCOLUMNS = 3, 4, 5, 6


def build_table_blob(cells: list[list[str]]) -> bytes:
    """Encode a grid the way Notes does, then gzip it."""
    n_rows, n_cols = len(cells), len(cells[0])
    uuids = [bytes([i + 1]) * 16 for i in range(2 * (n_rows + n_cols))]

    entries: list[bytes] = []

    def add(entry: bytes) -> int:
        entries.append(entry)
        return len(entries) - 1

    root_slot = add(b"")                        # placeholder, filled below

    # identity + ordering NSUUID objects for rows and columns
    row_identity, row_order, col_identity, col_order = [], [], [], []
    u = 0
    for _ in range(n_rows):
        row_identity.append(add(_custom_map(UUID_T, [(K_UUIDINDEX, _object_id_uint(u))]))); u += 1
        row_order.append(add(_custom_map(UUID_T, [(K_UUIDINDEX, _object_id_uint(u))]))); u += 1
    for _ in range(n_cols):
        col_identity.append(add(_custom_map(UUID_T, [(K_UUIDINDEX, _object_id_uint(u))]))); u += 1
        col_order.append(add(_custom_map(UUID_T, [(K_UUIDINDEX, _object_id_uint(u))]))); u += 1

    rows_set = add(_ordered_set([uuids[i] for i in range(1, 2 * n_rows, 2)],
                                list(zip(row_order, row_identity))))
    cols_set = add(_ordered_set([uuids[i] for i in range(2 * n_rows + 1, 2 * (n_rows + n_cols), 2)],
                                list(zip(col_order, col_identity))))

    column_dicts = []
    for c in range(n_cols):
        pairs = []
        for r in range(n_rows):
            note_slot = add(_note(cells[r][c]))
            pairs.append((_object_id_index(row_identity[r]), _object_id_index(note_slot)))
        column_dicts.append(add(_dictionary(pairs)))

    cell_columns = add(_dictionary([
        (_object_id_index(col_identity[c]), _object_id_index(column_dicts[c]))
        for c in range(n_cols)
    ]))

    entries[root_slot] = _custom_map(TABLE_T, [
        (K_CRROWS, _object_id_index(rows_set)),
        (K_CRCOLUMNS, _object_id_index(cols_set)),
        (K_CELLCOLUMNS, _object_id_index(cell_columns)),
    ])

    data = b"".join(_lf(3, e) for e in entries)
    data += b"".join(_lf(4, k.encode()) for k in KEYS)
    data += b"".join(_lf(5, t.encode()) for t in TYPES)
    data += b"".join(_lf(6, uuids[i]) for i in range(len(uuids)))
    return gzip.compress(_lf(2, _lf(3, data)))


# ---------------------------------------------------------------------------
# Decoding
# ---------------------------------------------------------------------------


def test_round_trips_a_simple_grid():
    cells = [["a", "b"], ["c", "d"]]
    assert decode_table(build_table_blob(cells)) == cells


def test_preserves_row_and_column_order():
    cells = [["r1c1", "r1c2", "r1c3"], ["r2c1", "r2c2", "r2c3"]]
    assert decode_table(build_table_blob(cells)) == cells


def test_empty_cells_survive_as_empty_strings():
    cells = [["x", ""], ["", "y"]]
    assert decode_table(build_table_blob(cells)) == cells


def test_unicode_cells():
    cells = [["café", "naïve"], ["日本語", "🎉"]]
    assert decode_table(build_table_blob(cells)) == cells


def test_garbage_returns_none_rather_than_raising():
    assert decode_table(b"not gzip") is None
    assert decode_table(gzip.compress(b"\x08\x00")) is None


def test_truncated_payload_returns_none():
    blob = build_table_blob([["a"]])
    raw = zlib.decompress(blob, 47)
    assert decode_table(gzip.compress(raw[: len(raw) // 2])) is None


# ---------------------------------------------------------------------------
# Markdown rendering
# ---------------------------------------------------------------------------


def test_markdown_uses_first_row_as_header():
    md = table_to_markdown([["H1", "H2"], ["a", "b"]])
    assert md.splitlines() == ["| H1 | H2 |", "|---|---|", "| a | b |"]


def test_markdown_escapes_pipes_so_rows_do_not_split():
    md = table_to_markdown([["a|b"], ["c"]])
    header = md.splitlines()[0]
    assert r"a\|b" in header
    # only the two cell delimiters are unescaped, so the row stays one cell
    assert len(re.findall(r"(?<!\\)\|", header)) == 2


def test_markdown_flattens_newlines_inside_cells():
    md = table_to_markdown([["one\ntwo"], ["x"]])
    assert "one two" in md
    assert len(md.splitlines()) == 3


def test_markdown_pads_ragged_rows():
    md = table_to_markdown([["a", "b"], ["c"]])
    assert md.splitlines()[-1] == "| c |   |"


def test_markdown_of_empty_table():
    assert table_to_markdown([]) == ""
