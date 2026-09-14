"""PDF parsing — text layer first, optional OCR backend.

Approach, in order of preference:

1. Ruling-line tables via ``page.extract_tables()`` (headers = first row).
2. Layout fallback: cluster ``extract_words()`` by line and horizontal
   position, so borderless print-outs still become proper rows/columns.
3. A PDF with no extractable text at all is OCR'd when the operator asks for
   it (``RECONCHECK_OCR=1`` + the optional ``pdf-ocr`` extra installed);
   otherwise it raises :class:`PdfOcrRequiredError` — scanned documents are
   never silently parsed into garbage.

Every cell keeps its ``page`` in :class:`SourceLoc`, so findings still link
back to the original file (``cell://path#<page>#row:col``).
"""

from __future__ import annotations

import os
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
        ocr = os.environ.get("RECONCHECK_OCR", "").strip().lower()
        if ocr in ("1", "true", "yes", "on", "auto"):
            text_lines = _ocr_rows(p)
        else:
            raise PdfOcrRequiredError(
                "PDF has no extractable text layer — scanned documents need OCR: "
                "install the `pdf-ocr` extra (pytesseract + a Tesseract binary) and "
                "run with RECONCHECK_OCR=1 (or set RECONCHECK_TESSERACT_CMD to the binary). "
                "Text-layer PDFs work without any of that."
            )

    return _document_from_parts(p, tables, text_lines)


def _ocr_rows(p: Path) -> list[tuple[int, list[str]]]:
    """OCR every page with Tesseract; returns ``(page_no, [line])`` rows.

    Each text line becomes a single cell (column segmentation of OCR output is
    a follow-up), then :func:`_table_from_lines` treats the first page's first
    line as the header — degraded but honest, never silently empty.
    """
    try:
        import pytesseract  # noqa: F401
    except ImportError as err:
        raise PdfOcrRequiredError(
            "OCR requested but pytesseract is missing — install `reconcheck[pdf-ocr]`"
        ) from err
    cmd = os.environ.get("RECONCHECK_TESSERACT_CMD")
    if cmd:
        pytesseract.pytesseract.tesseract_cmd = cmd
    try:
        import pdfplumber

        out: list[tuple[int, list[str]]] = []
        with pdfplumber.open(p) as pdf:
            for page_no, page in enumerate(pdf.pages, start=1):
                img = page.to_image(resolution=300).original
                data = pytesseract.image_to_data(img, output_type=pytesseract.Output.DICT)
                groups: dict[tuple[int, int, int], list[tuple[int, str]]] = {}
                for i, text in enumerate(data.get("text") or []):
                    txt = (text or "").strip()
                    if not txt:
                        continue
                    group_id = (data["block_num"][i], data["par_num"][i], data["line_num"][i])
                    groups.setdefault(group_id, []).append((data["left"][i], txt))
                for group_id in sorted(groups):
                    words = sorted(groups[group_id], key=lambda w: w[0])
                    line = " ".join(w[1] for w in words).strip()
                    if line:
                        out.append((page_no, [line]))
        return out
    except PdfOcrRequiredError:
        raise
    except Exception as err:  # noqa: BLE001 - missing binary / bad image etc.
        raise PdfOcrRequiredError(f"OCR failed: {type(err).__name__}: {err}") from err


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
        out_tables = [
            Table(headers=headers, rows=rows, loc=SourceLoc(path=str(path), page=page_no))
        ]
        if text_lines:
            # mixed file: some pages have ruling-line tables, others are
            # borderless — keep both, never drop the borderless rows silently
            out_tables.append(_table_from_lines(path, text_lines))
        return Document(path=str(path), tables=out_tables)

    if not text_lines:
        return Document(path=str(path))
    return Document(path=str(path), tables=[_table_from_lines(path, text_lines)])


def _table_from_lines(path: Path, text_lines: list[tuple[int, list[str]]]) -> Table:
    """Build a Table from clustered text lines (first line = header)."""
    header_line = text_lines[0][1]
    headers = _unique(header_line)
    rows: list[Row] = []
    for page_no, values in text_lines[1:]:
        if values == header_line:
            continue  # repeated header line
        row_idx = len(rows) + 1
        rows.append(_row(values, path, page_no, row_idx, headers))
    page_no = text_lines[0][0]
    return Table(headers=headers, rows=rows, loc=SourceLoc(path=str(path), page=page_no))


def _unique(headers: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for raw in headers:
        header = raw.strip()
        if not header:
            header = f"col{len(out) + 1}"
        base = header
        n = 1
        while header in seen:
            header = f"{base}_{n}"
            n += 1
        seen.add(header)
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
