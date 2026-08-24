"""Decoder for Apple Notes tables.

A table is not stored in the note body. The body carries a single U+FFFC
placeholder, and the table itself lives in the attachment row's
ZMERGEABLEDATA1 as a gzipped protobuf holding a CRDT document — the
structure Notes uses so two devices can edit the same table and merge.

Layout, as observed in a live store:

    MergeableDataProto
      2 MergeableDataObject
        3 MergeableDataObjectData
          3 repeated ObjectEntry     -- the objects, addressed by index
          4 repeated string key      -- "crRows", "crColumns", "cellColumns"
          5 repeated string type     -- "com.apple.notes.ICTable", ...
          6 repeated bytes uuid      -- row/column identities

    ObjectEntry
      1 register_latest   6 dictionary   10 note   13 custom_map   16 ordered_set

The root object is a custom_map of type com.apple.notes.ICTable whose
keys point at three objects:

    crRows      ordered set -> row order
    crColumns   ordered set -> column order
    cellColumns dictionary  -> column identity -> (row identity -> cell)

Row and column order needs one indirection that is easy to miss: the
ordered set's ordering array lists *ordering* UUIDs, not the identities
used as dictionary keys. The ordering's own contents dictionary maps one
to the other, and without it every cell lookup misses and the table
decodes as empty.
"""

from __future__ import annotations

import zlib
from typing import Any, Optional

from .protobuf import iter_fields, _read_varint

__all__ = ["decode_table", "table_to_markdown"]

# ObjectEntry field numbers
_DICTIONARY = 6
_NOTE = 10
_CUSTOM_MAP = 13
_ORDERED_SET = 16

_ICTABLE = "com.apple.notes.ICTable"
_NSUUID = "com.apple.CRDT.NSUUID"


def _first(buf: bytes, field: int) -> Optional[bytes]:
    for f, _w, payload in iter_fields(buf):
        if f == field:
            return payload
    return None


def _all(buf: bytes, field: int) -> list[bytes]:
    return [p for f, _w, p in iter_fields(buf) if f == field]


def _varint(payload: bytes) -> int:
    return _read_varint(payload, 0)[0]


def _object_id(buf: bytes) -> dict[str, Any]:
    """ObjectID: 2 = uint, 4 = string, 6 = index into the object table."""
    out: dict[str, Any] = {}
    for f, w, payload in iter_fields(buf):
        if f == 2 and w == 0:
            out["uint"] = _varint(payload)
        elif f == 4 and w == 2:
            out["str"] = payload.decode("utf-8", "replace")
        elif f == 6 and w == 0:
            out["index"] = _varint(payload)
    return out


class _Document:
    """The decoded object graph, addressed the way the format addresses it."""

    def __init__(self, raw: bytes) -> None:
        data = _first(_first(raw, 2) or b"", 3) or b""
        self.entries = _all(data, 3)
        self.keys = [p.decode("utf-8", "replace") for p in _all(data, 4)]
        self.types = [p.decode("utf-8", "replace") for p in _all(data, 5)]
        self.uuids = [p.hex() for p in _all(data, 6)]
        self._uuid_by_hex: Optional[dict[str, int]] = None

    def custom_map(self, index: int) -> tuple[Optional[str], dict[str, dict]]:
        entry = self.entries[index]
        payload = _first(entry, _CUSTOM_MAP)
        if payload is None:
            return None, {}
        type_index: Optional[int] = None
        fields: dict[str, dict] = {}
        for f, w, part in iter_fields(payload):
            if f == 1 and w == 0:
                type_index = _varint(part)
            elif f == 3:
                key = value = None
                for f2, w2, part2 in iter_fields(part):
                    if f2 == 1 and w2 == 0:
                        key = _varint(part2)
                    elif f2 == 2:
                        value = _object_id(part2)
                if key is not None and key < len(self.keys):
                    fields[self.keys[key]] = value or {}
        type_name = (
            self.types[type_index]
            if type_index is not None and type_index < len(self.types)
            else None
        )
        return type_name, fields

    def uuid_at(self, index: Optional[int]) -> Optional[str]:
        """The UUID an NSUUID object refers to."""
        if index is None or index >= len(self.entries):
            return None
        _type, fields = self.custom_map(index)
        value = fields.get("UUIDIndex") or {}
        position = value.get("uint")
        if position is None or position >= len(self.uuids):
            return None
        return self.uuids[position]

    def entry_for_uuid(self, uuid_hex: str) -> Optional[int]:
        if self._uuid_by_hex is None:
            self._uuid_by_hex = {}
            for i in range(len(self.entries)):
                type_name, _ = self.custom_map(i)
                if type_name == _NSUUID:
                    found = self.uuid_at(i)
                    if found is not None:
                        self._uuid_by_hex[found] = i
        return self._uuid_by_hex.get(uuid_hex)

    def dictionary(self, index: int) -> list[tuple[dict, dict]]:
        payload = _first(self.entries[index], _DICTIONARY)
        return self._dictionary_pairs(payload) if payload else []

    @staticmethod
    def _dictionary_pairs(payload: bytes) -> list[tuple[dict, dict]]:
        pairs = []
        for element in _all(payload, 1):
            key, value = _first(element, 1), _first(element, 2)
            pairs.append((_object_id(key or b""), _object_id(value or b"")))
        return pairs

    def ordered_identities(self, index: int) -> list[str]:
        """The identity UUIDs of an ordered set, in document order."""
        ordered_set = _first(self.entries[index], _ORDERED_SET)
        if ordered_set is None:
            return []
        ordering = _first(ordered_set, 1)
        if ordering is None:
            return []
        array = _first(ordering, 1)
        if array is None:
            return []

        placed: list[tuple[int, str]] = []
        for attachment in _all(array, 2):
            position = uuid_hex = None
            for f, w, part in iter_fields(attachment):
                if f == 1 and w == 0:
                    position = _varint(part)
                elif f == 2 and w == 2:
                    uuid_hex = part.hex()
            if position is not None and uuid_hex is not None:
                placed.append((position, uuid_hex))

        # Ordering UUIDs are not the identities used as cell keys; the
        # ordering's contents dictionary maps one to the other.
        contents = _first(ordering, 2)
        remap: dict[int, int] = {}
        if contents is not None:
            for key, value in self._dictionary_pairs(contents):
                if "index" in key and "index" in value:
                    remap[key["index"]] = value["index"]

        identities = []
        for _position, uuid_hex in sorted(placed):
            entry = self.entry_for_uuid(uuid_hex)
            identity = self.uuid_at(remap.get(entry, entry))
            if identity is not None:
                identities.append(identity)
        return identities

    def cell_text(self, index: int) -> str:
        note = _first(self.entries[index], _NOTE)
        if note is None:
            return ""
        text = _first(note, 2)
        return text.decode("utf-8", "replace") if text else ""


def decode_table(mergeable_data: bytes) -> Optional[list[list[str]]]:
    """Decode a table attachment's ZMERGEABLEDATA1 into rows of cells.

    Returns None when the blob is not a decodable table, so callers can
    report the attachment without its contents rather than failing.
    """
    try:
        raw = zlib.decompress(mergeable_data, 47)
    except zlib.error:
        return None
    try:
        document = _Document(raw)
        if not document.entries:
            return None
        root_index = next(
            (
                i
                for i in range(len(document.entries))
                if document.custom_map(i)[0] == _ICTABLE
            ),
            None,
        )
        if root_index is None:
            return None
        _type, root = document.custom_map(root_index)
        for required in ("crRows", "crColumns", "cellColumns"):
            if required not in root or "index" not in (root[required] or {}):
                return None

        rows = document.ordered_identities(root["crRows"]["index"])
        columns = document.ordered_identities(root["crColumns"]["index"])
        if not rows or not columns:
            return None

        cells: dict[tuple[str, str], str] = {}
        for column_key, column_value in document.dictionary(
            root["cellColumns"]["index"]
        ):
            column_uuid = document.uuid_at(column_key.get("index"))
            if column_uuid is None or "index" not in column_value:
                continue
            for row_key, row_value in document.dictionary(column_value["index"]):
                row_uuid = document.uuid_at(row_key.get("index"))
                if row_uuid is None or "index" not in row_value:
                    continue
                cells[(row_uuid, column_uuid)] = document.cell_text(
                    row_value["index"]
                )
        return [[cells.get((r, c), "") for c in columns] for r in rows]
    except (ValueError, IndexError, KeyError):
        return None


def table_to_markdown(rows: list[list[str]]) -> str:
    """Render decoded rows as a Markdown table.

    Notes tables have no header row of their own, but Markdown requires
    one, so the first row serves as the header — which matches how these
    tables are almost always written.
    """
    if not rows:
        return ""

    def cell(value: str) -> str:
        # A newline or pipe inside a cell would break the row apart.
        return value.replace("|", "\\|").replace("\n", " ").strip() or " "

    width = max(len(r) for r in rows)
    padded = [list(r) + [""] * (width - len(r)) for r in rows]
    header, body = padded[0], padded[1:]
    lines = [
        "| " + " | ".join(cell(c) for c in header) + " |",
        "|" + "|".join("---" for _ in header) + "|",
    ]
    lines += ["| " + " | ".join(cell(c) for c in row) + " |" for row in body]
    return "\n".join(lines)
