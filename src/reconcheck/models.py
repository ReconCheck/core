"""Core data model.

The engine is a four-stage pipeline — parse, align, judge, cite — and this
module defines the objects that flow through it:

* :class:`Document` / :class:`Table` / :class:`Row` / :class:`Cell` — the
  parsed representation of an input file. Every cell remembers where it came
  from (:class:`SourceLoc`), which is what makes "cite" possible.
* :class:`Finding` — one concrete claim about a pair of documents.
* :class:`Evidence` — the provenance attached to a finding.

The model is deliberately small and has no knowledge of PDFs, OCR or YAML;
those live in the ``parse`` and ``rules`` packages.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import Any

_CURRENCY_CHARS = "¥$€£"
# multi-letter currency codes, longest first (hkd before hk)
_CURRENCY_CODES = (
    "usd", "eur", "gbp", "cny", "hkd", "jpy", "aud", "cad", "chf",
    "sek", "krw", "twd", "sgd", "rmb",
)
_NUM_RE = re.compile(
    r"^\s*([+-]?(?:(?:\d[\d,]*)(?:\.\d+)?|\.\d+)(?:[eE][+-]?\d+)?)\s*"
    r"([A-Za-z\u4e00-\u9fff%]+)?\s*$"
)


def _strip_currency_prefix(text: str) -> str:
    """Strip a leading currency marker, keeping the sign: ``-¥12.30``, ``USD 12.30``,
    ``HK$5``, ``$ 1,200.00`` all reduce to their bare number."""
    t = text.strip()
    sign = ""
    if t and t[0] in "+-":
        sign, t = t[0], t[1:].strip()
    changed = True
    while changed and t:
        changed = False
        # symbolic codes glued to the symbol: HK$ / HK ¥
        if len(t) > 2 and t[:2].upper() == "HK" and t[2] in _CURRENCY_CHARS:
            t = t[3:].strip()
            changed = True
            continue
        low = t.lower()
        for code in _CURRENCY_CODES:
            if low.startswith(code) and (
                len(low) == len(code) or low[len(code)].isspace()
            ):
                t = t[len(code) :].strip()
                changed = True
                break
        if not changed and t and t[0] in _CURRENCY_CHARS:
            t = t[1:].strip()
            changed = True
    return (sign + t) if sign else t


class Severity(str, Enum):
    """Three severity levels — deliberately coarse."""

    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True)
class SourceLoc:
    """Exact location of a cell in the original file.

    ``row`` and ``col`` are 1-based positions *in the file* (not the table),
    so a finding can link straight to the original cell.
    """

    path: str
    sheet: str = ""
    page: int | None = None
    row: int = 0
    col: int = 0
    header: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "sheet": self.sheet,
            "page": self.page,
            "row": self.row,
            "col": self.col,
            "header": self.header,
        }


@dataclass
class Cell:
    """One cell of a parsed table."""

    text: str
    loc: SourceLoc
    value: Decimal | None = None
    unit: str | None = None

    def coerce(self) -> None:
        """Parse ``text`` into a numeric value and an optional unit.

        ``"5.00"`` -> value 5.00, unit None; ``"5000 g"`` -> value 5000,
        unit "g"; ``"¥ 12.30"`` / ``"USD 12.30"`` -> value 12.30;
        anything else -> value None.
        """
        t = _strip_currency_prefix(self.text)
        m = _NUM_RE.match(t)
        if not m:
            self.value = None
            self.unit = None
            return
        try:
            self.value = Decimal(m.group(1).replace(",", ""))
        except InvalidOperation:
            self.value = None
        self.unit = (m.group(2) or "").lower() or None

    def to_dict(self) -> dict[str, Any]:
        value: float | None = None
        if self.value is not None:
            try:
                value = float(self.value)
            except (OverflowError, ValueError):
                value = None
            if value is not None and not math.isfinite(value):
                value = None  # inf/NaN are not valid JSON numbers
        return {
            "text": self.text,
            "header": self.loc.header,
            "value": value,
            "unit": self.unit,
            "loc": self.loc.to_dict(),
        }


@dataclass
class Row:
    """One data row of a table; ``cells`` aligns with the table headers."""

    cells: list[Cell]
    key: str = ""

    def by_header(self, header: str) -> Cell | None:
        """Return the cell whose header equals ``header``, or None."""
        for cell in self.cells:
            if cell.loc.header == header:
                return cell
        return None

    def to_dict(self) -> list[dict[str, Any]]:
        return [cell.to_dict() for cell in self.cells]


@dataclass
class Table:
    """A two-dimensional grid with headers and per-cell provenance."""

    headers: list[str]
    rows: list[Row]
    loc: SourceLoc
    path: str = ""

    def __post_init__(self) -> None:
        if not self.path:
            self.path = self.loc.path

    def header_index(self, name: str) -> int | None:
        try:
            return self.headers.index(name)
        except ValueError:
            return None

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "sheet": self.loc.sheet,
            "headers": self.headers,
            "rows": [row.to_dict() for row in self.rows],
        }


@dataclass
class Document:
    """A parsed input file: one or more tables plus provenance."""

    path: str
    tables: list[Table] = field(default_factory=list)
    kind: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "kind": self.kind,
            "tables": [table.to_dict() for table in self.tables],
        }


@dataclass
class Evidence:
    """Provenance of one side of a finding."""

    side: str
    loc: SourceLoc
    excerpt: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "side": self.side,
            "excerpt": self.excerpt,
            "loc": self.loc.to_dict(),
            "href": f"cell://{self.loc.path}#{self.loc.sheet}#{self.loc.row}:{self.loc.col}",
        }


@dataclass
class Finding:
    """One concrete, checkable claim about a pair of documents."""

    id: str
    rule_id: str
    severity: Severity
    claim: str
    field: str
    left_value: str
    right_value: str
    evidence: list[Evidence] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "rule_id": self.rule_id,
            "severity": str(self.severity),
            "claim": self.claim,
            "field": self.field,
            "left_value": self.left_value,
            "right_value": self.right_value,
            "evidence": [evidence.to_dict() for evidence in self.evidence],
        }
