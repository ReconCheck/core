"""PDF parsing — text layer only (scan/OCR is a later milestone).

Approach, in order of preference:

1. Ruling-line tables via ``page.extract_tables()`` (headers = first row).
2. Layout fallback: cluster ``extract_words()`` by line and horizontal
   position, so borderless print-outs still become proper rows/columns.
3. A PDF with no extractable text at all raises :class:`PdfOcrRequiredError`
   — scanned documents are not silently parsed into garbage.

Every cell keeps its ``page`` in :class:`SourceLoc`, so findings still link
back to the original file (``cell://path#<page>#row:col``).
"""

from __future__ import annotations

from pathlib import Path

from ..errors import PdfOcrRequiredError, UnsupportedFormatError
from ..models import Cell, Document, Row, SourceLoc, Table

_COLUMN_X_TOLERANCE = 8.0  # points; words closer than this form one column


def load_pdf(path: str | Path) -> Document:
    """Parse a text-layer PDF into a :class:`Document`."""
    import importlib.util

    if importlib.util.find_spec("pdfplumber") is None:
        raise UnsupportedFormatError(
            "PDF support needs the optional dependency: pip install pdfplumber"
        )

    p = Path(path)
    try:
        return _load_pdf(p)
    except (UnsupportedFormatError, PdfOcrRequiredError):
        raise
    except Exception as err:  # noqa: BLE001 - any pdf error is a parse failure
        raise UnsupportedFormatError(f"'{p.name}' is not a readable PDF: {err}") from err


def _load_pdf(p: Path) -> Document:
    import pdfplumber

    tables: list[tuple[int, list[list[str]]]] = []  # (page_no, rows)
    text_lines: list[tuple[int, list[str]]] = []  # (page_no, columns)
    with pdfplumber.open(p) as pdf:
        for page_no, page in enumerate(pdf.pages, start=1):
            extracted = page.extract_tables()
            if extracted:
                for table in extracted:
                    rows = [
                        [(cell or "").strip() if cell is not None else "" for cell in row]
                        for row in table
                        if row and any(cell not in (None, "") for cell in row)
                    ]
                    if rows:
                        tables.append((page_no, rows))
                continue
            words = page.extract_words()
            if words:
                for line in _cluster_lines(words):
                    text_lines.append((page_no, line))

    if not tables and not text_lines:
        raise PdfOcrRequiredError(
            "PDF has no extractable text layer — scanned documents need OCR "
            "(not wired yet; text-layer PDFs are supported)"
        )

    return _document_from_parts(p, tables, text_lines)


def _cluster_lines(words: list[dict[str, object]]) -> list[list[str]]:
    """Group words into lines, then into columns.

    A column boundary is an x-position that *recurs across lines* (real print
    tables repeat their column x-positions); words that appear on a single
    line (multi-word cells, headers) merge into the nearest boundary.
    """
    rows_by_top: dict[int, list[dict[str, object]]] = {}
    for word in words:
        top = round(float(word["top"]))
        rows_by_top.setdefault(top, []).append(word)

    line_sets: list[set[int]] = []
    for line_words in rows_by_top.values():
        line_sets.append({round(float(w["x0"])) for w in line_words})

    from collections import Counter

    occ = Counter(x for line in line_sets for x in line)
    boundaries = sorted(x for x, count in occ.items() if count >= 2)
    lines: list[list[str]] = []
    for top in sorted(rows_by_top):
        line_words = sorted(rows_by_top[top], key=lambda w: float(w["x0"]))
        cells: dict[int, list[str]] = {b: [] for b in boundaries}
        fallback: list[tuple[int, str]] = []
        for w in line_words:
            x = round(float(w["x0"]))
            nearest: int | None = min(boundaries, key=lambda b: abs(b - x), default=None)
            if nearest is not None and abs(nearest - x) <= _COLUMN_X_TOLERANCE:
                cells[nearest].append(str(w["text"]))
            else:
                fallback.append((x, str(w["text"])))
        if not boundaries:
            # no recurring column x: keep each line as one cell
            lines.append([" ".join(str(w["text"]) for w in line_words).strip()])
            continue
        row = [" ".join(cells[b]).strip() for b in boundaries]
        for x, text in fallback:
            nearest = min(boundaries, key=lambda b: abs(b - x))
            row[boundaries.index(nearest)] = (row[boundaries.index(nearest)] + " " + text).strip()
        if any(row):
            lines.append(row)
    return lines


def _document_from_parts(
    path: Path,
    tables: list[tuple[int, list[list[str]]]],
    text_lines: list[tuple[int, list[str]]],
) -> Document:
    if tables:
        # headers from the first table row; continuation pages repeat them
        page_no, first = tables[0]
        headers = _unique(first[0])
        rows: list[Row] = []
        for page_no, table in tables:
            data = table[1:]
            if data and data[0] == table[0]:
                data = data[1:]  # repeated header row
            for row_idx, values in enumerate(data, start=1):
                rows.append(_row(values, path, page_no, row_idx, headers))
        return Document(path=str(path), tables=[Table(headers=headers, rows=rows, loc=SourceLoc(path=str(path), page=page_no))])

    # fallback: first line is the header, the rest are data rows
    headers = _unique(text_lines[0][1])
    rows: list[Row] = []
    for page_no, values in text_lines[1:]:
        if values and values[0] == text_lines[0][1]:
            continue  # repeated header line
        row_idx = len(rows) + 1
        rows.append(_row(values, path, page_no, row_idx, headers))
    if not rows:
        return Document(path=str(path))
    page_no = text_lines[0][0]
    return Document(path=str(path), tables=[Table(headers=headers, rows=rows, loc=SourceLoc(path=str(path), page=page_no))])


def _unique(headers: list[str]) -> list[str]:
    seen: dict[str, int] = {}
    out: list[str] = []
    for header in headers:
        if not header:
            header = f"col{len(out) + 1}"
        if header in seen:
            seen[header] += 1
            header = f"{header}_{seen[header]}"
        else:
            seen[header] = 0
        out.append(header)
    return out


def _row(values: list[str], path: Path, page_no: int, row_idx: int, headers: list[str]) -> Row:
    cells: list[Cell] = []
    for col_idx, value in enumerate(values, start=1):
        header = headers[col_idx - 1] if col_idx <= len(headers) else f"col{col_idx}"
        cell = Cell(
            text=value,
            loc=SourceLoc(path=str(path), page=page_no, row=row_idx, col=col_idx, header=header),
        )
        cell.coerce()
        cells.append(cell)
    return Row(cells)
