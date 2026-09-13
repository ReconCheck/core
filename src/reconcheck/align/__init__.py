"""Row alignment between two tables."""

from __future__ import annotations

from dataclasses import dataclass

from ..errors import EmptyDocumentError
from ..models import Row, Table
from .keys import normalize_key

__all__ = ["AlignedPair", "align"]


@dataclass
class AlignedPair:
    """A matched pair of rows plus the key that matched them."""

    key: str
    left: Row
    right: Row


def align(
    left: Table,
    right: Table,
    match_on: list[str] | None = None,
    normalize: dict[str, str] | None = None,
) -> list[AlignedPair]:
    """Match rows of ``left`` and ``right`` by key columns.

    ``match_on`` is a list of column header names whose concatenated,
    normalised values form the row key. Normalisers come from ``normalize``
    (``{column: kind}`` where kind is ``part_no`` | ``entity`` | ``raw``).
    Defaults to the first header common to both tables, raw normalisation.

    Exact key equality for now; fuzzy and three-way matching are later phases.

    Raises :class:`EmptyDocumentError` when no usable key column exists on
    both sides — an empty alignment would otherwise produce a misleading
    "everything matches" report.
    """
    keys = match_on or _default_key_columns(left, right)
    left_headers = set(left.headers)
    right_headers = set(right.headers)
    for col in keys:
        if col not in left_headers or col not in right_headers:
            raise EmptyDocumentError(
                f"match key column {col!r} is missing on one side — pass --match-on "
                "with a header both documents share"
            )
    normalize = normalize or {}
    left_index = _index(left, keys, normalize)
    right_index = _index(right, keys, normalize)
    return [
        AlignedPair(key=key, left=left_index[key], right=right_index[key])
        for key in sorted(left_index.keys() & right_index.keys())
    ]


def _default_key_columns(left: Table, right: Table) -> list[str]:
    for header in left.headers:
        if header and header in right.headers:
            return [header]
    raise EmptyDocumentError(
        "no shared header between the documents — pass --match-on with a column "
        "both sides contain"
    )


def _index(table: Table, keys: list[str], normalize: dict[str, str]) -> dict[str, Row]:
    index: dict[str, Row] = {}
    for row in table.rows:
        parts: list[str] = []
        for col in keys:
            cell = row.by_header(col)
            if cell is None or not cell.text.strip():
                break
            parts.append(normalize_key(cell.text, normalize.get(col, "raw")))
        if len(parts) == len(keys):
            key = "\x1f".join(parts)
            index.setdefault(key, row)  # first occurrence wins
    return index
